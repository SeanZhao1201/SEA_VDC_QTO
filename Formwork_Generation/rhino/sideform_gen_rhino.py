#! python 2
# -*- coding: utf-8 -*-
"""Side-form + bulkhead generator — the vertical temporary works.

Companion to ``formwork_gen_rhino.py`` (which emits the horizontal works:
soffit platforms + shoring props). This script walks every structural
slab's soffit-face loops — outer outlines AND opening inner loops (shaft
and stair perimeters need edge forms too) — samples each loop, and
classifies every sample:

  1. **bearing** — a downward ray from a point probed slightly INSIDE the
     slab edge starts inside a solid, or hits one within
     ``suppress_clear`` of the soffit: the edge sits on a wall / beam /
     column, its face is a joint, no form needed (suppressed);
  2. **joint** — a point probed slightly OUTSIDE the edge at mid slab
     thickness lands inside ANOTHER slab solid: the edge abuts a
     neighbouring pour piece (or an independent adjacent slab) — a
     **bulkhead**. One bulkhead per joint: the run is emitted only by the
     lower-pour side (``POUR`` user strings from the pour-break splitter;
     deterministic object-id tiebreak when pours are absent/equal), with
     the panel body offset into the neighbour — where the later pour goes;
  3. **side** — an exposed edge face: a **side form**, offset outward,
     soffit to slab top, no kicker (placeholder fidelity, as ever).

Consecutive same-class samples become runs; each run becomes one capped
vertical panel solid. Every panel carries ``FW_TYPE`` (``side`` |
``bulkhead``), ``FLOOR``, ``POUR`` (when the slab piece has one) and
``FW_AREA_M2`` — the **net formwork contact area**, the takeoff quantity
this module owns (QTO keeps the gross areas; net + suppressed = gross is
the cross-check, asserted per slab in the report).

Modes mirror the platform generator: ``generate`` adds panels under the
``_FORMWORK`` tree (per floor: ``Sideforms`` / ``Bulkheads`` sublayers,
purgeable via the shared FW_GENERATED/FW_TYPE stamps); ``export`` touches
the document not at all — File3dm + a JSON handoff with per-floor net
areas; ``purge`` delegates to ``formwork_gen_rhino.purge_formwork``.

Prefer running it on the pour-break DERIVED model (the split pieces carry
``POUR``/``SOURCE_SLAB``, so pour-break joints get bulkheads
automatically); an unsplit model degrades to pure side forms.

Run inside Rhino 7/8 with ``_-RunPythonScript`` (IronPython 2.7), or
headless with FW_HEADLESS=1. Lengths in PARAMS are metres, converted to
model units at runtime.
"""
from __future__ import division, print_function

import io
import json
import math
import os
import sys
import traceback

STAGE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "qto_fw_test")
sys.path.insert(0, STAGE)

import Rhino
import System
from Rhino.Geometry import (AreaMassProperties, Brep, LoftType, Mesh,
                            MeshingParameters, Point3d, PointFaceRelation,
                            PolylineCurve, Ray3d, Vector3d)
from Rhino.Geometry.Intersect import Intersection
from System.Drawing import Color

import formwork_gen_rhino as fw

PARAMS = {
    "mode": "generate",          # generate | purge | export
    "panel_thickness": 0.05,     # panel body thickness (offset outward)
    "edge_sample": 0.25,         # loop sampling step
    "suppress_clear": 0.05,      # support top within this below soffit
                                 # -> edge is bearing, no form
    "probe_inset": 0.05,         # bearing probe: this far INSIDE the edge
    "joint_probe": 0.05,         # joint probe: this far OUTSIDE the edge
    "min_run": 0.10,             # runs shorter than this are dropped
                                 # (their area lands in unclassified)
    "slope_tol": 0.02,           # soffit face z-span beyond this ->
                                 # sloped, out of scope, loud warning
    "slab_layer_keyword": "slab",
    "slab_layer_exclude": ["sog", "topping"],
    "include_hidden_obstacles": False,
    "lock_layers": True,
    "export_path": None,         # None -> <doc>_sideforms.3dm
    "log_path": None,
}

COL_SIDE = Color.FromArgb(86, 156, 104)      # green
COL_BULKHEAD = Color.FromArgb(255, 64, 38)   # pour-break red


# ── sampling & classification ──────────────────────────────────────────────
def loop_samples(curve, step, tol):
    """Closed loop -> [(Point3d, unit tangent)] at ~``step`` spacing."""
    length = curve.GetLength()
    if length <= 0:
        return []
    n = max(8, int(math.ceil(length / step)))
    params = curve.DivideByCount(n, True)
    if not params:
        return []
    out = []
    for t in params:
        p = curve.PointAt(t)
        tan = curve.TangentAt(t)
        tan = Vector3d(tan.X, tan.Y, 0.0)
        if not tan.Unitize():
            continue
        out.append((p, tan))
    return out


def _inside(brep, x, y, z, tol):
    try:
        return brep.IsPointInside(Point3d(x, y, z), tol, False)
    except Exception:
        return False


def edge_normals(piece, sample, probe, mid_z, tol):
    """(inward, outward, reason) at a loop sample, resolved by testing
    which side is inside the slab AT THE FACE'S OWN mid-thickness Z —
    orientation-proof for outer loops, opening inner loops and stepped
    slabs (a whole-brep bbox mid-Z lands between the steps).

    reason: "ok" | "internal" (both sides inside — an internal soffit-face
    seam, no exposed edge there) | "corner" (neither side resolves)."""
    p, tan = sample
    n = Vector3d(-tan.Y, tan.X, 0.0)       # tangent rotated +90
    a_in = _inside(piece, p.X + n.X * probe, p.Y + n.Y * probe, mid_z, tol)
    b_in = _inside(piece, p.X - n.X * probe, p.Y - n.Y * probe, mid_z, tol)
    if a_in and b_in:
        return None, None, "internal"
    if a_in:
        return n, -n, "ok"
    if b_in:
        return -n, n, "ok"
    return None, None, "corner"


def _own_mesh(brep):
    mesh = Mesh()
    parts = Mesh.CreateFromBrep(brep, MeshingParameters.FastRenderMesh)
    if parts:
        for m in parts:
            mesh.Append(m)
    mesh.FaceNormals.ComputeFaceNormals()
    return mesh


def face_interior_point(face):
    amp = AreaMassProperties.Compute(face)
    if amp is not None:
        ok, u, v = face.ClosestPoint(amp.Centroid)
        if ok and face.IsPointOnFace(u, v) != PointFaceRelation.Exterior:
            return face.PointAt(u, v)
    du, dv = face.Domain(0), face.Domain(1)
    for n in (3, 7):
        for i in range(1, n):
            for j in range(1, n):
                u = du.ParameterAt(i / float(n))
                v = dv.ParameterAt(j / float(n))
                if face.IsPointOnFace(u, v) == PointFaceRelation.Interior:
                    return face.PointAt(u, v)
    return None


def local_top_z(own_mesh, pt, soffit_z, eps, fallback):
    """The slab top directly above this soffit face — an upward ray on the
    slab's own mesh. The whole-brep bbox top is WRONG for the lower level
    of a stepped slab (inflated thickness, panels above the real top)."""
    if pt is None:
        return fallback
    t = Intersection.MeshRay(
        own_mesh, Ray3d(Point3d(pt.X, pt.Y, soffit_z + eps),
                        Vector3d(0, 0, 1)))
    if t >= 0.0:
        return soffit_z + eps + t
    return fallback


def classify_sample(p, inward, outward, soffit_z, probe_zs, mesh, others,
                    mu, tol):
    """'bearing' | ('joint', neighbour) | 'side' for one loop sample.

    Joint probes test several heights across THIS face's thickness so an
    abutting slab of different thickness or offset elevation is still
    seen (a single mid-thickness probe misses it, breaking the bulkhead
    dedupe symmetry)."""
    eps = mu["_eps"]
    # bearing: ray down from just inside the edge, just below the soffit
    origin = Point3d(p.X + inward.X * mu["probe_inset"],
                     p.Y + inward.Y * mu["probe_inset"], soffit_z - eps)
    t, hit_nz = fw._mesh_ray_with_face(mesh, Ray3d(origin,
                                                   Vector3d(0, 0, -1)))
    if t >= 0.0:
        if hit_nz is not None and hit_nz < 0:
            return "bearing", None          # ray started inside a solid
        if t <= mu["suppress_clear"] + eps:
            return "bearing", None
    # joint: probe just outside the edge across the face's thickness
    jx = p.X + outward.X * mu["joint_probe"]
    jy = p.Y + outward.Y * mu["joint_probe"]
    for nb_id, nb_brep, nb_bb, nb_pour in others:
        if jx < nb_bb.Min.X - tol or jx > nb_bb.Max.X + tol:
            continue
        if jy < nb_bb.Min.Y - tol or jy > nb_bb.Max.Y + tol:
            continue
        for jz in probe_zs:
            if jz < nb_bb.Min.Z - tol or jz > nb_bb.Max.Z + tol:
                continue
            if _inside(nb_brep, jx, jy, jz, tol):
                return "joint", (nb_id, nb_pour)
    return "side", None


def _pour_sort(pour):
    """Sort key: numbered pours first, unnumbered last."""
    return (0, pour) if pour is not None else (1, 0)


def own_joint(my_id, my_pour, neighbour):
    """One bulkhead per joint: the lower-pour side owns it (it is erected
    for the earlier pour); object-id tiebreak keeps it deterministic when
    pours are absent or equal."""
    nb_id, nb_pour = neighbour
    return (_pour_sort(my_pour), my_id) <= (_pour_sort(nb_pour), nb_id)


def runs_from_samples(classes):
    """Group consecutive same-class sample indices on a CLOSED loop into
    runs, merging the wrap-around pair. Returns [(class, [indices])]."""
    n = len(classes)
    if n == 0:
        return []
    runs = []
    start = 0
    for i in range(1, n + 1):
        if i == n or classes[i] != classes[start]:
            runs.append((classes[start], list(range(start, i))))
            start = i
        if i == n:
            break
    if len(runs) > 1 and runs[0][0] == runs[-1][0]:
        cls, tail = runs.pop()
        runs[0] = (cls, tail + runs[0][1])
    return runs


# ── panel building ─────────────────────────────────────────────────────────
def run_profile(pts, normals, thick):
    """Closed plan profile of a panel run: the sample sub-polyline plus
    its outward offset (the panel body sits OUTSIDE the slab edge)."""
    inner = [(p.X, p.Y) for p in pts]
    outer = [(p.X + n.X * thick, p.Y + n.Y * thick)
             for p, n in zip(pts, normals)]
    prof = inner + list(reversed(outer))
    prof.append(prof[0])
    return prof


def _outward_solid(brep):
    """Solids must face outward or volumes go negative downstream."""
    if brep is not None and brep.IsSolid and brep.SolidOrientation == \
            Rhino.Geometry.BrepSolidOrientation.Inward:
        brep.Flip()
    return brep


def panel_brep(profile, z0, z1, tol, log):
    """One capped vertical panel solid from a closed plan profile."""
    if len(profile) < 4:
        return None
    c0 = PolylineCurve([Point3d(x, y, z0) for x, y in profile])
    c1 = PolylineCurve([Point3d(x, y, z1) for x, y in profile])
    lofts = Brep.CreateFromLoft([c0, c1], Point3d.Unset, Point3d.Unset,
                                LoftType.Straight, False)
    if not lofts:
        if log:
            log("  WARNING: panel loft failed ({0}-pt profile)".format(
                len(profile)))
        return None
    brep = lofts[0]
    if len(lofts) > 1:
        joined = Brep.JoinBreps(list(lofts), tol * 10)
        if joined:
            brep = joined[0]
    capped = brep.CapPlanarHoles(tol * 10)
    return _outward_solid(capped if capped is not None else brep)


def _ring_area(ring):
    """Shoelace area of a closed [(x, y)...] ring (absolute)."""
    a = 0.0
    for i in range(len(ring) - 1):
        a += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(a) / 2.0


def ring_profile(pts, normals, thick):
    """Closed-loop run: the full ring profile (no slit) — inner ring and
    outer ring as separate closed rings."""
    inner = [(p.X, p.Y) for p in pts]
    inner.append(inner[0])
    outer = [(p.X + n.X * thick, p.Y + n.Y * thick)
             for p, n in zip(pts, normals)]
    outer.append(outer[0])
    return inner, outer


def annulus_panel(inner, outer, z0, z1, tol, log):
    """Panel for a run covering a WHOLE loop (e.g. an opening perimeter):
    a closed ring solid, not a slit C-profile. Returns (solid,
    boundary_ring, hole_ring) — the larger ring is the boundary."""
    if _ring_area(outer) >= _ring_area(inner):
        boundary, hole = outer, inner
    else:
        boundary, hole = inner, outer
    c_in = PolylineCurve([Point3d(x, y, z0) for x, y in inner])
    c_out = PolylineCurve([Point3d(x, y, z0) for x, y in outer])
    faces = Brep.CreatePlanarBreps([c_in, c_out], tol)
    if faces:
        for pb in faces:
            for face in pb.Faces:
                dist = z1 - z0
                solid = Brep.CreateFromOffsetFace(face, dist, tol, False,
                                                  True)
                if solid is None:
                    continue
                sb = solid.GetBoundingBox(True)
                if sb.Max.Z < z1 - dist * 0.5:   # extruded down — flip
                    solid = Brep.CreateFromOffsetFace(face, -dist, tol,
                                                      False, True)
                    if solid is None:
                        continue
                return _outward_solid(solid), boundary, hole
    if log:
        log("  WARNING: ring panel failed — falling back to open loft")
    prof = inner + list(reversed(outer)) + [inner[0]]
    return panel_brep(prof, z0, z1, tol, log), boundary, hole


def run_length(pts):
    total = 0.0
    for i in range(len(pts) - 1):
        total += math.hypot(pts[i + 1].X - pts[i].X,
                            pts[i + 1].Y - pts[i].Y)
    return total


# ── core generation (document-independent, headless-testable) ──────────────
def generate_sideforms(targets, obstacle_geoms, doc, params=None, log=None):
    """Compute side forms + bulkheads. Touches no document.

    Args:
        targets: list of (obj_id str, Brep, layer_name, pour or None)
                 for the slabs to edge.
        obstacle_geoms: geometries the bearing rays may land on.
        doc: RhinoDoc (units, tolerance, floor names only).

    Returns dict:
        panels: [{brep, type, floor, pour, area_m2, len_m, z0, z1,
                  pts_m (plan polyline, metres)}]
        floors: {floor: {side_area, bulkhead_area, suppressed_len,
                         gross_area}}   (all m2 / m)
        stats
    """
    log = log or fw.Log()
    p = dict(PARAMS)
    if params:
        p.update(params)
    to_mu = fw.meters_to_model(doc)
    to_m = fw.model_to_meters(doc)
    tol = doc.ModelAbsoluteTolerance
    mu = {}
    for key in ("panel_thickness", "edge_sample", "suppress_clear",
                "probe_inset", "joint_probe", "min_run", "slope_tol"):
        mu[key] = p[key] * to_mu
    mu["_eps"] = 0.01 * to_mu

    mesh = fw.obstacle_mesh(obstacle_geoms, log)
    if not fw._strongbox_available():
        log("WARNING: hit-face detection unavailable — bearing suppression "
            "under walls/beams may be WRONG. Run via _-RunPythonScript "
            "(IronPython).")
    floors = fw.read_floor_elevations(doc)

    slab_info = []          # (obj_id, brep, bbox, pour, layer, floor)
    for obj_id, brep, layer, pour in targets:
        bb = brep.GetBoundingBox(True)
        fl = fw.floor_name(bb.Min.Z, floors, to_m)
        slab_info.append((obj_id, brep, bb, pour, layer, fl))

    cos20 = math.cos(math.radians(20.0))
    panels = []
    freport = {}
    stats = {"side": 0, "bulkhead": 0, "joint_theirs": 0,
             "bearing_runs": 0, "degenerate_samples": 0,
             "internal_samples": 0, "dropped_runs": 0, "sloped_faces": 0}
    for obj_id, brep, bb, pour, layer, fl in slab_info:
        others = [(i2, b2, bb2, p2) for i2, b2, bb2, p2, _l2, _f2
                  in slab_info if i2 != obj_id]
        frec = freport.setdefault(fl, {
            "side_area": 0.0, "bulkhead_area": 0.0,
            "suppressed_len": 0.0, "suppressed_area": 0.0,
            "shared_area": 0.0, "unclassified_area": 0.0,
            "gross_area": 0.0})
        own = _own_mesh(brep)
        for face, soffit_z in fw.soffit_faces(brep, cos20, log, layer):
            # the LOCAL top above this face — the whole-brep bbox top is
            # wrong for the lower level of a stepped slab
            fpt = face_interior_point(face)
            top_z = local_top_z(own, fpt, soffit_z, mu["_eps"], bb.Max.Z)
            thick = top_z - soffit_z
            if thick <= tol:
                continue
            fb = face.GetBoundingBox(True)
            outer, inners = fw.face_loops(face, tol)
            loops = ([outer] if outer is not None else []) + list(inners)
            if fb.Max.Z - fb.Min.Z > max(mu["slope_tol"], tol * 3):
                # sloped soffit: the flattened-loop model would falsely
                # suppress the downhill half (ray origins inside the slab
                # body) and place panels at the wrong elevation. Out of
                # scope — loudly, with the area kept in the books.
                log("  WARNING: sloped soffit on '{0}' (z-span {1:.3f}) — "
                    "side forms SKIPPED for this face (out of scope)"
                    .format(layer, fb.Max.Z - fb.Min.Z))
                stats["sloped_faces"] += 1
                for loop in loops:
                    a = (loop.GetLength() * to_m) * (thick * to_m)
                    frec["unclassified_area"] += a
                    frec["gross_area"] += a
                continue
            mid_z = (soffit_z + top_z) / 2.0
            probe_zs = [mid_z, soffit_z + 0.25 * thick,
                        top_z - 0.25 * thick]
            for loop in loops:
                flat = fw.flatten_to_z(loop, soffit_z)
                samples = loop_samples(flat, mu["edge_sample"], tol)
                if len(samples) < 3:
                    continue
                cls, inw, outw = [], [], []
                prev = (None, None)
                for s in samples:
                    n_in, n_out, why = edge_normals(
                        brep, s, mu["probe_inset"], mid_z, tol)
                    if why == "internal":
                        # both sides inside: an internal soffit-face seam
                        # — no exposed edge, never inherit stale normals
                        stats["internal_samples"] += 1
                        cls.append("skip")
                        inw.append(None)
                        outw.append(None)
                        continue
                    if n_in is None:
                        n_in, n_out = prev
                        if n_in is None:
                            stats["degenerate_samples"] += 1
                            cls.append("skip")
                            inw.append(None)
                            outw.append(None)
                            continue
                    prev = (n_in, n_out)
                    c, nb = classify_sample(
                        s[0], n_in, n_out, soffit_z, probe_zs, mesh,
                        others, mu, tol)
                    if c == "joint":
                        c = "bulkhead" if own_joint(obj_id, pour, nb) \
                            else "joint_theirs"
                    cls.append(c)
                    inw.append(n_in)
                    outw.append(n_out)

                def midpoint(a, b):
                    return Point3d((a.X + b.X) / 2.0, (a.Y + b.Y) / 2.0,
                                   (a.Z + b.Z) / 2.0)

                def colinear(a, b, c):
                    """Direction a->b continues into b->c (cos > 0.99).
                    Extensions happen only along the run's own line — an
                    extension around a plan corner builds a bow-tie
                    profile (self-intersecting, inverted solid)."""
                    ax, ay = b.X - a.X, b.Y - a.Y
                    bx, by = c.X - b.X, c.Y - b.Y
                    la = math.hypot(ax, ay)
                    lb = math.hypot(bx, by)
                    if la <= 0 or lb <= 0:
                        return False
                    return (ax * bx + ay * by) / (la * lb) > 0.99

                n_s = len(samples)
                for c, idxs in runs_from_samples(cls):
                    full_loop = len(idxs) == n_s
                    pts = [samples[i][0] for i in idxs]
                    ns = [outw[i] for i in idxs]
                    if full_loop:
                        length = flat.GetLength()
                    else:
                        # tile the loop exactly: extend to the class-
                        # transition midpoint where the transition is
                        # straight; at corner transitions only the
                        # half-segment is billed (no bow-tie geometry)
                        p_pre = midpoint(
                            samples[(idxs[0] - 1) % n_s][0], pts[0])
                        p_post = midpoint(
                            pts[-1], samples[(idxs[-1] + 1) % n_s][0])
                        seg2 = flat.GetLength() / n_s / 2.0
                        ext_head = len(pts) >= 2 and \
                            colinear(p_pre, pts[0], pts[1])
                        ext_tail = len(pts) >= 2 and \
                            colinear(pts[-2], pts[-1], p_post)
                        if ext_head:
                            pts = [p_pre] + pts
                            if ns and ns[0] is not None:
                                ns = [ns[0]] + ns
                        if ext_tail:
                            pts = pts + [p_post]
                            if ns and ns[-1] is not None:
                                ns = ns + [ns[-1]]
                        length = run_length(pts) \
                            + (0.0 if ext_head else seg2) \
                            + (0.0 if ext_tail else seg2)
                    area = (length * to_m) * (thick * to_m)
                    if c == "skip":
                        # internal seams / unresolvable corners: no form
                        # (correct), but the area must still reconcile
                        frec["unclassified_area"] += area
                        continue
                    if c == "bearing":
                        frec["suppressed_len"] += length * to_m
                        frec["suppressed_area"] += area
                        stats["bearing_runs"] += 1
                        continue
                    if c == "joint_theirs":
                        # the neighbour emits this joint's bulkhead; the
                        # area still reconciles against gross here
                        frec["shared_area"] += area
                        stats["joint_theirs"] += 1
                        continue
                    if length < mu["min_run"]:
                        frec["unclassified_area"] += area
                        stats["dropped_runs"] += 1
                        continue
                    hole = None
                    if full_loop:
                        inner, outer_ring = ring_profile(
                            pts, ns, mu["panel_thickness"])
                        body, profile, hole = annulus_panel(
                            inner, outer_ring, soffit_z, top_z, tol, log)
                    else:
                        profile = run_profile(pts, ns,
                                              mu["panel_thickness"])
                        body = panel_brep(profile, soffit_z, top_z, tol,
                                          log)
                    if body is None:
                        frec["unclassified_area"] += area
                        stats["dropped_runs"] += 1
                        continue
                    panels.append({
                        "brep": body, "type": c, "floor": fl,
                        "pour": pour, "area_m2": round(area, 3),
                        "len_m": round(length * to_m, 3),
                        "z0": soffit_z, "z1": top_z,
                        "profile_mu": profile, "hole_mu": hole})
                    key = "side_area" if c == "side" else "bulkhead_area"
                    frec[key] += area
                    stats[c] += 1
                frec["gross_area"] += (flat.GetLength() * to_m) * \
                    (thick * to_m)
    for fl, frec in sorted(freport.items()):
        for k in frec:
            frec[k] = round(frec[k], 2)
        net = frec["side_area"] + frec["bulkhead_area"]
        log("  {0:10} side {1:8.1f} m2  bulkhead {2:7.1f} m2  "
            "suppressed {3:6.1f} m  gross {4:8.1f} m2".format(
                fl, frec["side_area"], frec["bulkhead_area"],
                frec["suppressed_len"], frec["gross_area"]))
        if net > frec["gross_area"] + 0.5:
            log("  WARNING: {0}: net {1:.1f} exceeds gross {2:.1f}".format(
                fl, net, frec["gross_area"]))
        accounted = net + frec["shared_area"] + frec["suppressed_area"] \
            + frec["unclassified_area"]
        drift = abs(accounted - frec["gross_area"])
        if drift > max(0.02 * frec["gross_area"], 1.0):
            log("  WARNING: {0}: accounted area {1:.1f} vs gross {2:.1f} "
                "— sampling drift beyond tolerance".format(
                    fl, accounted, frec["gross_area"]))
    log("panels: {0} side + {1} bulkhead ({2} joint runs owned by the "
        "other slab, {3} bearing runs suppressed)".format(
            stats["side"], stats["bulkhead"], stats["joint_theirs"],
            stats["bearing_runs"]))
    return {"panels": panels, "floors": freport, "stats": stats}


# ── document adapter ───────────────────────────────────────────────────────
def find_targets_and_obstacles(doc, params, log):
    """(targets, obstacle_geoms) — same layer semantics as the platform
    generator; targets carry object id + POUR user string. Block
    instances (e.g. after QTO_Tool's Blockify, which wraps EVERY object)
    are exploded one level in-memory, reading layer and user strings from
    the definition parts. Read-only."""
    keyword = params["slab_layer_keyword"].lower()
    targets, obstacles = [], []
    n_instances = 0

    def _pour_of(attrs):
        pour = attrs.GetUserString("POUR")
        try:
            return int(pour) if pour is not None else None
        except (TypeError, ValueError):
            return None

    def _accept(geom, lname, attrs, uid):
        if isinstance(geom, Rhino.Geometry.Extrusion):
            geom = geom.ToBrep(True)
        if not isinstance(geom, (Brep, Rhino.Geometry.Mesh)):
            return
        obstacles.append(geom)
        first = lname.split("_")[0].lower()
        excluded = any(kw and kw.lower() in lname.lower()
                       for kw in params.get("slab_layer_exclude") or [])
        if isinstance(geom, Brep) and keyword in first and not excluded:
            targets.append((uid, geom, lname, _pour_of(attrs)))

    for obj in doc.Objects:
        if obj is None or obj.Attributes is None:
            continue
        li = obj.Attributes.LayerIndex
        if fw._is_formwork_layer(doc, li):
            continue
        if obj.IsHidden and not params["include_hidden_obstacles"]:
            continue
        layer = doc.Layers[li]
        if layer is not None and not layer.IsVisible \
                and not params["include_hidden_obstacles"]:
            continue
        lname = layer.Name if layer is not None else ""
        if isinstance(obj, Rhino.DocObjects.InstanceObject):
            idef = obj.InstanceDefinition
            if idef is None:
                continue
            n_instances += 1
            xf = obj.InstanceXform
            for k, part in enumerate(idef.GetObjects() or []):
                g = part.Geometry
                if g is None:
                    continue
                g = g.Duplicate()
                if not g.Transform(xf):
                    continue
                p_layer = doc.Layers[part.Attributes.LayerIndex]
                p_name = p_layer.Name if p_layer is not None else lname
                _accept(g, p_name, part.Attributes,
                        "{0}/{1}".format(obj.Id, k))
            continue
        _accept(obj.Geometry, lname, obj.Attributes, str(obj.Id))
    log("document: {0} slab targets, {1} obstacle solids{2}".format(
        len(targets), len(obstacles),
        " ({0} block instances exploded in-memory)".format(n_instances)
        if n_instances else ""))
    return targets, obstacles


def _panel_attr(doc, layer_index, panel):
    extra = {"FW_AREA_M2": "{0:.3f}".format(panel["area_m2"]),
             "FW_LEN_M": "{0:.3f}".format(panel["len_m"])}
    if panel["pour"] is not None:
        extra["POUR"] = str(panel["pour"])
    return fw._attributes(
        doc, layer_index,
        "Formwork Side Form" if panel["type"] == "side"
        else "Formwork Bulkhead",
        panel["floor"], panel["type"], extra)


def purge_sideforms(doc, log):
    """Remove ONLY generated side/bulkhead panels (FW_TYPE stamp) — the
    platform/prop content under _FORMWORK is left alone, so re-running
    generate does not stack panels and does not eat the other tool's
    output."""
    # unlock the whole tree first — write_to_doc locks parent layers too,
    # and a locked parent blocks Delete even with the leaf unlocked
    for layer in doc.Layers:
        if layer is not None and not layer.IsDeleted \
                and fw._is_formwork_layer(doc, layer.Index):
            layer.IsLocked = False
    es = Rhino.DocObjects.ObjectEnumeratorSettings()
    es.NormalObjects = True
    es.LockedObjects = True
    es.HiddenObjects = True
    n = 0
    for obj in list(doc.Objects.GetObjectList(es)):
        if obj is None or obj.Attributes is None:
            continue
        if not fw._is_formwork_layer(doc, obj.Attributes.LayerIndex):
            continue
        if obj.Attributes.GetUserString("FW_TYPE") in ("side", "bulkhead"):
            if doc.Objects.Delete(obj, True):
                n += 1
    if n:
        log("purged {0} previously generated panels".format(n))
    return n


def write_to_doc(doc, result, params, log):
    """Add panels under _FORMWORK. Only additions, one undo record."""
    sn = doc.BeginUndoRecord("Generate Side Forms")
    n_added = 0
    try:
        for panel in result["panels"]:
            fls = fw._safe_layer_name(panel["floor"])
            sub, col = (("Sideforms", COL_SIDE) if panel["type"] == "side"
                        else ("Bulkheads", COL_BULKHEAD))
            idx = fw.ensure_layer(doc, [fw.FW_ROOT, fls, sub], col)
            if doc.Objects.AddBrep(panel["brep"],
                                   _panel_attr(doc, idx, panel)) != \
                    System.Guid.Empty:
                n_added += 1
    finally:
        if sn > 0:
            doc.EndUndoRecord(sn)
    if params["lock_layers"]:
        for layer in doc.Layers:
            if layer is not None and not layer.IsDeleted and \
                    fw._is_formwork_layer(doc, layer.Index) and \
                    layer.FullPath != fw.FW_ROOT:
                layer.IsLocked = True
    doc.Views.Redraw()
    log("added {0} panels under {1} (existing objects untouched)".format(
        n_added, fw.FW_ROOT))
    return n_added


def export_3dm(doc, result, path, log):
    """Write panels to a separate .3dm — document untouched."""
    f3 = Rhino.FileIO.File3dm()
    f3.Settings.ModelUnitSystem = doc.ModelUnitSystem
    seen = {}

    def add_layer(names, color):
        parent_id = System.Guid.Empty
        idx = -1
        for depth in range(len(names)):
            partial = "::".join(names[:depth + 1])
            if partial in seen:
                idx, parent_id = seen[partial]
                continue
            layer = Rhino.DocObjects.Layer()
            layer.Name = names[depth]
            layer.Id = System.Guid.NewGuid()
            if parent_id != System.Guid.Empty:
                layer.ParentLayerId = parent_id
            if depth == len(names) - 1:
                layer.Color = color
            idx = f3.AllLayers.Count
            f3.AllLayers.Add(layer)
            seen[partial] = (idx, layer.Id)
            parent_id = layer.Id
        return idx

    n = 0
    for panel in result["panels"]:
        fls = fw._safe_layer_name(panel["floor"])
        sub, col = (("Sideforms", COL_SIDE) if panel["type"] == "side"
                    else ("Bulkheads", COL_BULKHEAD))
        idx = add_layer([fw.FW_ROOT, fls, sub], col)
        f3.Objects.AddBrep(panel["brep"], _panel_attr(doc, idx, panel))
        n += 1
    ok = f3.Write(path, 7)
    log("export: {0} panels -> {1} ({2})".format(
        n, path, "ok" if ok else "WRITE FAILED"))
    return ok


def dump_json(doc, result, path, log):
    """Handoff for the IFC converter — metres, absolute world coords."""
    to_m = fw.model_to_meters(doc)
    data = {"units": "m", "source_model": doc.Path or "",
            "panel_thickness": PARAMS["panel_thickness"],
            "floors": result["floors"], "panels": []}
    for panel in result["panels"]:
        rec = {
            "type": panel["type"], "floor": panel["floor"],
            "pour": panel["pour"], "area_m2": panel["area_m2"],
            "len_m": panel["len_m"],
            "z0": panel["z0"] * to_m, "z1": panel["z1"] * to_m,
            "profile": [[x * to_m, y * to_m]
                        for x, y in panel["profile_mu"]]}
        if panel.get("hole_mu"):
            rec["hole"] = [[x * to_m, y * to_m]
                           for x, y in panel["hole_mu"]]
        data["panels"].append(rec)
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(u"{0}".format(json.dumps(data, indent=1)))
    log("json handoff -> {0}".format(path))


# ── entry point ────────────────────────────────────────────────────────────
def main(params=None):
    p = dict(PARAMS)
    if params:
        p.update(params)
    doc = Rhino.RhinoDoc.ActiveDoc
    log = fw.Log()
    log("sideform_gen_rhino [{0}] — model units: {1}".format(
        p["mode"], doc.ModelUnitSystem))
    base = None
    if doc.Path:
        base = os.path.join(
            os.path.dirname(doc.Path),
            os.path.splitext(os.path.basename(doc.Path))[0])
    log_path = p["log_path"] or (
        (base or os.path.join(os.path.expanduser("~"), "sideforms"))
        + "_sideform_log.txt")

    result = None
    if p["mode"] == "purge":
        fw.purge_formwork(doc, log)
    else:
        targets, obstacles = find_targets_and_obstacles(doc, p, log)
        if not targets:
            log("no slab targets (keyword '{0}', excludes {1})".format(
                p["slab_layer_keyword"], p["slab_layer_exclude"]))
            log.save(log_path)
            return None
        result = generate_sideforms(targets, obstacles, doc, p, log)
        if p["mode"] == "generate":
            purge_sideforms(doc, log)
            write_to_doc(doc, result, p, log)
        elif p["mode"] == "export":
            path = p["export_path"] or (
                (base or os.path.join(os.path.expanduser("~"),
                                      "sideforms")) + "_sideforms.3dm")
            export_3dm(doc, result, path, log)
            dump_json(doc, result,
                      os.path.splitext(path)[0] + ".json", log)
    log.save(log_path)
    log("log saved: {0}".format(log_path))
    return result


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with io.open(os.path.join(STAGE, "sideform_error.txt"), "w",
                     encoding="utf-8") as fh:
            fh.write(u"{0}".format(traceback.format_exc()))
        raise
    finally:
        # clear Modified ONLY in headless throwaway runs — on a live
        # document it would mask the user's own unsaved edits
        if os.environ.get("FW_HEADLESS") == "1":
            try:
                Rhino.RhinoDoc.ActiveDoc.Modified = False
            except Exception:
                pass
            try:
                Rhino.RhinoApp.Exit()
            except Exception:
                Rhino.RhinoApp.RunScript("_-Exit", False)
