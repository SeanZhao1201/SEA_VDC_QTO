#! python 2
# -*- coding: utf-8 -*-
"""Formwork generator for Rhino — per-soffit-face platforms + ray-cast props.

Replaces the per-floor fixed-height logic of ../formwork_gen.py with geometry
queried from the live model:

* one platform per soffit Z-cluster (stepped/dropped slabs come out right),
* every prop foot found by shooting a ray straight down from the platform
  underside onto the merged solids of the model (double-height voids handled), with
  three rules: hit closer than ``min_clear`` or ray starting inside a solid
  -> no prop needed (bearing wall/column/downstand beam); no hit -> stand on
  ``grade_z`` if set, else skip and flag; taller than ``max_prop`` -> TALL tag.

Non-destructive by construction: the script only ever ADDS objects, all of
them under the ``_FORMWORK`` layer tree, and only in ``generate`` mode.
``purge`` removes exactly that tree. ``export`` touches the document not at
all — it writes platforms/props to a separate .3dm via File3dm.

Run inside Rhino 7/8 with ``_-RunPythonScript`` (IronPython 2.7) — or import
it and drive :func:`generate_formwork` from another script (the headless test
does this). Edit PARAMS below, then run.
"""
from __future__ import division, print_function

import json
import math
import os

import Rhino
import System
from Rhino.Geometry import (
    AreaMassProperties, BoundingBox, Brep, Curve, CurveOffsetCornerStyle,
    Interval, Mesh, MeshingParameters, Plane, Point3d, PointFaceRelation,
    Ray3d, Vector3d,
)
from Rhino.Geometry.Intersect import Intersection
from System.Drawing import Color


# ── parameters (lengths in METRES; converted to model units at runtime) ────
PARAMS = {
    "mode": "generate",          # generate | purge | export
    "panel_thickness": 0.05,     # soffit platform thickness
    "prop_spacing": 3.0,         # prop grid spacing (centre to centre)
    "prop_size": 0.15,           # prop plan size (square)
    "platform_overhang": 0.1,    # platform margin beyond the slab edge
    "edge_inset": 0.5,           # props keep this distance from slab edges
    "min_clear": 0.30,           # hit closer than this -> no prop (bearing)
    "max_prop": 5.0,             # taller than this -> TALL flag
    "grade_z": None,             # metres, world Z; None -> no-hit props are
                                 # skipped and flagged instead of grounded
    "min_hole": 0.5,             # m2; smaller slab openings ignored
    "z_cluster_tol": 0.02,       # soffits within this merge into one level
    "slab_layer_keyword": "slab",  # layer name first '_' segment must contain
    # FORMWORK filter - narrower on purpose: a slab on grade has no soffit
    # to form and nothing to shore. split_pourbreaks/breaksheet_gen use a
    # WIDER pour-break filter that keeps SOG (2026-08-20). Do not unify.
    "slab_layer_exclude": ["sog", "topping"],  # skip these slab layers
                                 # (slab-on-grade / toppings need no soffit
                                 # formwork; they stay ray obstacles)
    "include_hidden_obstacles": False,
    "lock_layers": True,         # lock _FORMWORK layers after generate
    "export_path": None,         # None -> <doc folder>/<doc name>_formwork.3dm
    "log_path": None,            # None -> alongside export path / doc
}

FW_ROOT = "_FORMWORK"
COL_PLATFORM = Color.FromArgb(196, 186, 166)
COL_OK = Color.FromArgb(0, 114, 178)        # blue
COL_TALL = Color.FromArgb(230, 159, 0)      # orange
COL_GROUNDED = Color.FromArgb(213, 94, 0)   # vermillion
STATUS_LAYER = {"OK": ("Props", COL_OK),
                "TALL": ("Props_TALL", COL_TALL),
                "GROUNDED": ("Props_GROUNDED", COL_GROUNDED),
                "GROUNDED_TALL": ("Props_GROUNDED", COL_GROUNDED)}


class Log(object):
    def __init__(self):
        self.lines = []

    def __call__(self, msg):
        self.lines.append(msg)
        try:
            Rhino.RhinoApp.WriteLine(msg)
        except Exception:
            print(msg)

    def save(self, path):
        try:
            import io
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(u"\n".join(u"{0}".format(l) for l in self.lines)
                         + u"\n")
        except Exception as exc:
            self("WARNING: could not write log to {0}: {1}".format(path, exc))


# ── unit helpers ───────────────────────────────────────────────────────────
def meters_to_model(doc):
    return Rhino.RhinoMath.UnitScale(
        Rhino.UnitSystem.Meters, doc.ModelUnitSystem)


def model_to_meters(doc):
    return Rhino.RhinoMath.UnitScale(
        doc.ModelUnitSystem, Rhino.UnitSystem.Meters)


# ── soffit extraction ──────────────────────────────────────────────────────
def soffit_faces(brep, cos_threshold=0.9397, log=None, layer=""):
    """Downward faces of ``brep`` (normal within acos(threshold) of -Z).

    Returns list of (BrepFace, plane_z) with plane_z = face centroid Z.
    Mirrors SlabTemplate's normal classification (default 20 deg -> cos).
    """
    out = []
    for face in brep.Faces:
        amp = AreaMassProperties.Compute(face)
        if amp is None:
            continue
        ok, u, v = face.ClosestPoint(amp.Centroid)
        if not ok:
            continue
        # BrepFace.NormalAt already returns the OUTWARD face normal (the
        # OrientationIsReversed flip is applied internally — flipping again
        # turns real-model top faces into phantom soffits).
        normal = face.NormalAt(u, v)
        normal.Unitize()
        if normal.Z < -cos_threshold:
            out.append((face, amp.Centroid.Z))
    if not out:  # fallback: single lowest face (SlabTemplate does the same)
        if log:
            log("  WARNING: slab on '{0}' has no downward face within the "
                "angle threshold; using its lowest face".format(layer))
        best = None
        for face in brep.Faces:
            amp = AreaMassProperties.Compute(face)
            if amp is None:
                continue
            if best is None or amp.Centroid.Z < best[1]:
                best = (face, amp.Centroid.Z)
        if best:
            out.append(best)
    return out


def slab_label(ident):
    """Short human name for the slab a platform forms."""
    if not ident:
        return ""
    return (ident.get("name") or "").strip() or (ident.get("layer") or "")


def level_display_name(floor, pour, slab_names):
    """The name a soffit level carries into IFC.

    The soffit ELEVATION is deliberately not in the name: "@37.26m" told a
    scheduler nothing, and a level is far easier to find by what it forms
    (field feedback 2026-08-20). The elevation survives as SOFFIT_Z_M on
    the assembly's pset, so nothing is lost.
    """
    if pour:
        return "Formwork for {0} Pour {1}".format(floor, pour)
    if len(slab_names) == 1:
        return "Formwork for {0} {1}".format(floor, slab_names[0])
    if slab_names:
        return "Formwork for {0} ({1} slabs)".format(floor, len(slab_names))
    return "Formwork for {0}".format(floor)


def _pour_of(ident):
    """POUR user string, normalised. The splitter writes "0" for a piece it
    could not assign a pour to - that is "no pour", not pour zero."""
    pour = ((ident or {}).get("pour") or "").strip()
    return "" if pour == "0" else pour


def cluster_by_z(faces_with_z, tol):
    """Group (face, z[, ident]) into platform levels.

    Grouped by POUR first, then by soffit Z inside each pour. The two pour
    pieces of one floor share a soffit elevation, so a plain Z-clustering
    merged them into ONE platform - which could then be named after
    neither and scheduled with neither (decided 2026-08-20, option B).
    Slabs carrying no POUR (an unsplit model) fall in one group and keep
    the previous Z-only behaviour exactly.

    Returns [(z, [faces], [idents])] ordered by elevation, then pour.
    """
    groups = {}
    for item in faces_with_z:
        face, z = item[0], item[1]
        ident = item[2] if len(item) > 2 else None
        groups.setdefault(_pour_of(ident), []).append((face, z, ident))
    out = []
    for key in sorted(groups):
        clusters = []                 # list of [z_ref, [faces], [idents]]
        for face, z, ident in sorted(groups[key], key=lambda t: t[1]):
            if clusters and abs(z - clusters[-1][0]) <= tol:
                clusters[-1][1].append(face)
                clusters[-1][2].append(ident)
            else:
                clusters.append([z, [face], [ident]])
        out.extend((c[0], c[1], c[2]) for c in clusters)
    # ascending elevation keeps the run log readable; pour breaks ties
    out.sort(key=lambda c: (c[0], _pour_of(c[2][0] if c[2] else None)))
    return out


# ── planar region building (platform outlines) ─────────────────────────────
def _closed_area(curve):
    amp = AreaMassProperties.Compute(curve)
    return amp.Area if amp else 0.0


def _offset_outward(curve, dist, plane, tol, log=None, ctx=""):
    """Offset a closed planar curve outward by dist (larger-area result)."""
    best = None
    for d in (dist, -dist):
        try:
            res = curve.Offset(plane, d, tol, CurveOffsetCornerStyle.Sharp)
        except Exception:
            res = None
        if not res:
            continue
        joined = Curve.JoinCurves(res, tol * 10) if len(res) > 1 else res
        for c in joined:
            if c.IsClosed and (best is None or _closed_area(c) > best[1]):
                best = (c, _closed_area(c))
    if best and best[1] > _closed_area(curve) - tol:
        try:
            xs = Intersection.CurveSelf(best[0], tol)
            if xs is not None and xs.Count > 0:
                if log:
                    log("  WARNING: platform offset self-intersects {0}; "
                        "tracing the slab edge exactly there".format(ctx))
                return curve
        except Exception:
            pass
        return best[0]
    if log:
        log("  WARNING: platform offset failed {0}; tracing the slab edge "
            "exactly there".format(ctx))
    return curve                      # offset failed -> trace exactly


def face_loops(face, tol):
    """(outer_curve, [inner_curves]) of a BrepFace as flat closed curves."""
    outer, inners = None, []
    for loop in face.Loops:
        crv = loop.To3dCurve()
        if crv is None or not crv.IsClosed:
            continue
        if loop.LoopType == Rhino.Geometry.BrepLoopType.Outer:
            outer = crv
        elif loop.LoopType == Rhino.Geometry.BrepLoopType.Inner:
            inners.append(crv)
    return outer, inners


def flatten_to_z(curve, z):
    """Project a curve to the horizontal plane at ``z`` (world XY)."""
    dup = curve.DuplicateCurve()
    xf = Rhino.Geometry.Transform.PlanarProjection(
        Plane(Point3d(0, 0, z), Vector3d.ZAxis))
    dup.Transform(xf)
    return dup


def build_platform_regions(faces, z, params_mu, tol, log):
    """Union the outward-offset outlines of same-level soffit faces.

    Returns (region_curves, hole_curves, bad_faces): closed planar curves at
    height z, plus faces whose outer loop could not be extracted (their props
    must be suppressed too — no platform, no props). Falls back to per-face
    outlines when boolean ops fail.
    """
    overhang, min_hole = params_mu["platform_overhang"], params_mu["min_hole"]
    plane = Plane(Point3d(0, 0, z), Vector3d.ZAxis)
    outlines, holes, bad_faces = [], [], []
    for face in faces:
        outer, inners = face_loops(face, tol)
        if outer is None:
            log("  WARNING: soffit face at z={0:.3f} has no closed outer "
                "loop — platform AND props omitted for it".format(z))
            bad_faces.append(face)
            continue
        outer = flatten_to_z(outer, z)
        outlines.append(_offset_outward(outer, overhang, plane, tol, log,
                                        "at z={0:.3f}".format(z)))
        for h in inners:
            h = flatten_to_z(h, z)
            if _closed_area(h) >= min_hole:
                holes.append(h)

    regions = outlines
    if len(outlines) > 1:
        try:
            union = Curve.CreateBooleanUnion(outlines, tol)
            if union and len(union) > 0:
                regions = list(union)
        except Exception:
            log("  WARNING: outline union failed at z={0:.3f}; "
                "keeping {1} separate platforms".format(z, len(outlines)))

    # keep only holes strictly inside a region: an opening that crosses the
    # (offset, unioned) boundary would otherwise fail CreatePlanarBreps and
    # silently cost the whole level its holes.
    kept = []
    for h in holes:
        verdict = None
        for r in regions:
            try:
                rel = Curve.PlanarClosedCurveRelationship(r, h, plane, tol)
            except Exception:
                rel = None
            if rel == Rhino.Geometry.RegionContainment.BInsideA:
                verdict = "in"
                break
            if rel == Rhino.Geometry.RegionContainment.MutualIntersection:
                verdict = "cross"
                break
        if verdict == "in":
            kept.append(h)
        else:
            log("  WARNING: dropped a {0:.1f}-unit2 opening at z={1:.3f} "
                "({2} the platform boundary)".format(
                    _closed_area(h), z,
                    "crosses" if verdict == "cross" else "outside"))
    return regions, kept, bad_faces


def platform_breps(regions, holes, z, panel_mu, tol, log):
    """Solid platform Breps with top at ``z``, extruded down panel_mu."""
    out = []
    curves = list(regions) + list(holes)
    planar = Brep.CreatePlanarBreps(curves, tol)
    if not planar:
        planar = Brep.CreatePlanarBreps(list(regions), tol)
        if planar and holes:
            log("  WARNING: platform holes dropped at z={0:.3f}".format(z))
    if not planar:
        log("  WARNING: no platform surface at z={0:.3f}".format(z))
        return out
    for pb in planar:
        for face in pb.Faces:
            normal = face.NormalAt(0.5, 0.5)
            dist = panel_mu if normal.Z < 0 else -panel_mu
            solid = Brep.CreateFromOffsetFace(face, dist, tol, False, True)
            if solid is None:
                continue
            bb = solid.GetBoundingBox(True)
            if bb.Max.Z > z + panel_mu * 0.5:   # extruded up -> retry flipped
                solid = Brep.CreateFromOffsetFace(
                    face, -dist, tol, False, True)
                if solid is None:
                    continue
            out.append(solid)
    return out


# ── prop grid + ray casting ────────────────────────────────────────────────
def obstacle_mesh(geometries, log):
    """Merge every obstacle into one Mesh (FastRenderMesh fidelity)."""
    big = Mesh()
    mp = MeshingParameters.FastRenderMesh
    n_ok = 0
    for g in geometries:
        try:
            if isinstance(g, Mesh):
                big.Append(g)
                n_ok += 1
                continue
            if isinstance(g, Rhino.Geometry.Extrusion):
                g = g.ToBrep(True)
            if isinstance(g, Brep):
                parts = Mesh.CreateFromBrep(g, mp)
                if parts:
                    for m in parts:
                        big.Append(m)
                    n_ok += 1
        except Exception:
            pass
    big.FaceNormals.ComputeFaceNormals()
    log("obstacle mesh: {0} solids, {1} faces".format(n_ok, big.Faces.Count))
    return big


_STRONGBOX_OK = [None]        # None = unprobed; capability cached module-wide


def _strongbox_available():
    """Can we get MeshRay's hit-face indices on this Python engine?"""
    if _STRONGBOX_OK[0] is None:
        try:
            import clr
            from System import Array, Int32
            clr.StrongBox[Array[Int32]]()
            _STRONGBOX_OK[0] = True
        except Exception:
            _STRONGBOX_OK[0] = False
    return _STRONGBOX_OK[0]


def _mesh_ray_with_face(mesh, ray):
    """(t, first_hit_face_normal_z) — normal_z None when unavailable."""
    if _strongbox_available():
        try:
            import clr
            from System import Array, Int32
            box = clr.StrongBox[Array[Int32]]()
            t = Intersection.MeshRay(mesh, ray, box)
            fids = box.Value
            if t >= 0.0 and fids is not None and fids.Length > 0:
                return t, mesh.FaceNormals[fids[0]].Z
            return t, None
        except Exception:
            pass
    return Intersection.MeshRay(mesh, ray), None


def grid_points_on_face(face, spacing, inset, tol):
    """Global-origin-aligned lattice of Point3d on the (trimmed) face,
    at least ``inset`` from every boundary loop."""
    bb = face.GetBoundingBox(True)
    z = bb.Min.Z
    loops = []
    for loop in face.Loops:
        crv = loop.To3dCurve()
        if crv:
            loops.append(crv)
    pts = []
    x = math.ceil(bb.Min.X / spacing) * spacing
    while x <= bb.Max.X + tol:
        y = math.ceil(bb.Min.Y / spacing) * spacing
        while y <= bb.Max.Y + tol:
            p = Point3d(x, y, z)
            ok, u, v = face.ClosestPoint(p)
            if ok and face.IsPointOnFace(u, v) == PointFaceRelation.Interior:
                far_enough = True
                for crv in loops:
                    rc, t = crv.ClosestPoint(p)
                    if rc:
                        cp = crv.PointAt(t)
                        dxy = math.hypot(cp.X - p.X, cp.Y - p.Y)
                        if dxy < inset:
                            far_enough = False
                            break
                if far_enough:
                    pts.append(p)
            y += spacing
        x += spacing
    return pts


def resolve_prop(pt_xy, soffit_z, mesh, params_mu):
    """Ray-cast one prop location. Returns dict or None (no prop needed).

    Statuses: OK | TALL | GROUNDED | GROUNDED_TALL | NOHIT_SKIPPED.
    """
    panel = params_mu["panel_thickness"]
    eps = params_mu["_eps"]
    prop_top = soffit_z - panel
    origin = Point3d(pt_xy.X, pt_xy.Y, prop_top - eps)

    ray = Ray3d(origin, Vector3d(0, 0, -1))
    t, hit_nz = _mesh_ray_with_face(mesh, ray)
    if t >= 0.0:
        if hit_nz is not None and hit_nz < 0:
            # first hit is a DOWNWARD face: the ray started inside a solid
            # (bearing wall / column / same-level downstand beam) — no prop.
            return None
        foot_z = origin.Z - t
        height = prop_top - foot_z
        if height < params_mu["min_clear"]:
            return None                  # solid right under the soffit
        status = "TALL" if height > params_mu["max_prop"] else "OK"
        return {"x": pt_xy.X, "y": pt_xy.Y, "top_z": prop_top,
                "foot_z": foot_z, "height": height, "status": status}
    # no hit at all
    if params_mu["grade_z"] is not None:
        foot_z = params_mu["grade_z"]
        height = prop_top - foot_z
        if height <= 0:
            return None
        status = ("GROUNDED_TALL" if height > params_mu["max_prop"]
                  else "GROUNDED")
        return {"x": pt_xy.X, "y": pt_xy.Y, "top_z": prop_top,
                "foot_z": foot_z, "height": height, "status": status}
    return {"x": pt_xy.X, "y": pt_xy.Y, "top_z": prop_top,
            "foot_z": None, "height": None, "status": "NOHIT_SKIPPED"}


def prop_brep(prop, size_mu):
    half = size_mu / 2.0
    bb = BoundingBox(
        Point3d(prop["x"] - half, prop["y"] - half, prop["foot_z"]),
        Point3d(prop["x"] + half, prop["y"] + half, prop["top_z"]))
    return Brep.CreateFromBox(bb)


# ── floor naming (QTO document-strings contract) ───────────────────────────
def read_floor_elevations(doc):
    """{elevation (model units): name} from doc strings key FloorElevations —
    the exact dictionary QTO_Tool persists. Empty dict when absent."""
    try:
        raw = doc.Strings.GetValue("FloorElevations")
        if raw:
            data = json.loads(raw)
            return dict((float(k), str(v)) for k, v in data.items())
    except Exception:
        pass
    return {}


def floor_name(z, floor_elevations, to_m):
    if floor_elevations:
        best = min(floor_elevations.keys(), key=lambda e: abs(e - z))
        return floor_elevations[best]
    return "Z{0:+.2f}m".format(z * to_m)


# ── core generation (document-independent, headless-testable) ──────────────
def generate_formwork(slab_breps, obstacle_geoms, doc, params=None, log=None):
    """Compute platforms + props. Touches no document.

    Args:
        slab_breps: list of (Brep, layer_name) for the slabs to form.
        obstacle_geoms: geometries rays may land on (slabs incl.), model units.
        doc: RhinoDoc (units + tolerance + floor names only).
        params: PARAMS overrides (metres).
        log: Log instance.

    Returns dict:
        levels: [{z, floor, platforms: [Brep], props: [prop dict],
                  n_skipped_bearing, n_faces}]
        stats:  totals per status.
    """
    log = log or Log()
    p = dict(PARAMS)
    if params:
        p.update(params)
    to_mu = meters_to_model(doc)
    to_m = model_to_meters(doc)
    tol = doc.ModelAbsoluteTolerance

    # params in model units
    mu = {}
    for key in ("panel_thickness", "prop_spacing", "prop_size",
                "platform_overhang", "edge_inset", "min_clear", "max_prop",
                "z_cluster_tol"):
        mu[key] = p[key] * to_mu
    mu["min_hole"] = p["min_hole"] * to_mu * to_mu
    mu["grade_z"] = None if p["grade_z"] is None else p["grade_z"] * to_mu
    mu["_eps"] = 0.01 * to_mu

    # soffit faces of every slab, clustered by level
    cos_thr = math.cos(math.radians(20.0))
    all_faces = []
    for entry in slab_breps:
        # 3-tuples since 2026-08-20; tolerate the old (brep, layer) shape
        brep, layer = entry[0], entry[1]
        ident = entry[2] if len(entry) > 2 else None
        for face, z in soffit_faces(brep, cos_thr, log, layer):
            bbf = face.GetBoundingBox(True)
            if bbf.Max.Z - bbf.Min.Z > max(mu["z_cluster_tol"], tol * 3):
                log("  WARNING: sloped/stepped soffit face on '{0}' spans "
                    "{1:.3f} units in Z — the flat platform at its centroid "
                    "level will not fit it (ramps are out of scope)".format(
                        layer, bbf.Max.Z - bbf.Min.Z))
            all_faces.append((face, z, ident))
    if not all_faces:
        log("no soffit faces found — nothing to do")
        return {"levels": [], "stats": {}}
    clusters = cluster_by_z(all_faces, mu["z_cluster_tol"])
    log("{0} slabs -> {1} soffit faces -> {2} levels".format(
        len(slab_breps), len(all_faces), len(clusters)))

    mesh = obstacle_mesh(obstacle_geoms, log)
    if not _strongbox_available():
        log("WARNING: hit-face detection unavailable on this Python engine "
            "— props under bearing walls/columns/downstand beams may be "
            "WRONG. Run via _-RunPythonScript (IronPython).")
    floors = read_floor_elevations(doc)
    if floors:
        log("floor names from QTO FloorElevations ({0} floors)".format(
            len(floors)))

    stats = {"OK": 0, "TALL": 0, "GROUNDED": 0, "GROUNDED_TALL": 0,
             "NOHIT_SKIPPED": 0, "bearing_skipped": 0}
    levels = []
    for z, faces, idents in clusters:
        fl = floor_name(z, floors, to_m)
        pours = set(_pour_of(i) for i in idents)
        pour = pours.pop() if len(pours) == 1 else ""
        slab_names = []
        slab_ids = []
        for i in idents:
            nm = slab_label(i)
            if nm and nm not in slab_names:
                slab_names.append(nm)
            # the Rhino object id of the slab this platform forms: the QTO
            # take-off export derives that element's IfcGlobalId from the
            # same id, so the IFC writer can hand a 4D consumer a link that
            # actually resolves instead of FLOOR+POUR inference
            sid = (i or {}).get("id") or ""
            if sid and sid not in slab_ids:
                slab_ids.append(sid)
        regions, holes, bad_faces = build_platform_regions(
            faces, z, mu, tol, log)
        plats = platform_breps(regions, holes, z, mu["panel_thickness"],
                               tol, log)
        props, n_bearing, seen = [], 0, set()
        for face in faces:
            if any(bf is face for bf in bad_faces):
                continue
            pts = grid_points_on_face(
                face, mu["prop_spacing"], mu["edge_inset"], tol)
            if not pts:
                # face too small for the lattice: one prop at its centroid
                amp = AreaMassProperties.Compute(face)
                if amp is not None:
                    ok, u, v = face.ClosestPoint(amp.Centroid)
                    if ok and face.IsPointOnFace(u, v) != \
                            PointFaceRelation.Exterior:
                        pts = [Point3d(amp.Centroid.X, amp.Centroid.Y, z)]
                        log("  note: face at z={0:.3f} too small for the "
                            "grid — single centroid prop".format(z))
            for pt in pts:
                key = (round(pt.X / tol) if tol > 0 else pt.X,
                       round(pt.Y / tol) if tol > 0 else pt.Y)
                if key in seen:
                    continue
                seen.add(key)
                prop = resolve_prop(pt, z, mesh, mu)
                if prop is None:
                    n_bearing += 1
                    continue
                props.append(prop)
                stats[prop["status"]] += 1
        stats["bearing_skipped"] += n_bearing
        heights = [pr["height"] * to_m for pr in props
                   if pr["height"] is not None]
        n_nohit = sum(1 for pr in props
                      if pr["status"] == "NOHIT_SKIPPED")
        n_written = len(props) - n_nohit
        if plats and not n_written:
            log("  WARNING: level below has platforms but ZERO props")
        log("  {0:10} z={1:10.3f}  faces={2:2}  platforms={3}  props={4:4}"
            "  no-hit-skip={5:2}  bearing-skip={6:3}  h(m) {7}".format(
                fl, z, len(faces), len(plats), n_written, n_nohit, n_bearing,
                "min {0:.2f} / max {1:.2f}".format(min(heights), max(heights))
                if heights else "-"))
        levels.append({"z": z, "floor": fl, "platforms": plats,
                       "props": props, "n_skipped_bearing": n_bearing,
                       "n_faces": len(faces),
                       "regions": regions, "holes": holes,
                       "pour": pour or None, "slabs": slab_names,
                       "slab_ids": slab_ids,
                       "name": level_display_name(fl, pour, slab_names)})
    # Names must be unique INSIDE a storey, or the IFC tree shows six
    # identical rows: one layer can carry several slabs at different soffit
    # levels (L01 on the real model has six). Colliding names fall back to
    # the indexed form the field asked for - "Formwork for L01 Slab 1" -
    # with the layer kept in SLABS and the elevation in SOFFIT_Z_M.
    for fl in sorted(set(lv["floor"] for lv in levels)):
        group = sorted([lv for lv in levels if lv["floor"] == fl],
                       key=lambda lv: lv["z"])
        counts = {}
        for lv in group:
            counts[lv["name"]] = counts.get(lv["name"], 0) + 1
        used = {}
        for lv in group:
            base = lv["name"]
            if counts[base] < 2:
                continue
            used[base] = used.get(base, 0) + 1
            lv["name"] = ("{0} Level {1}".format(base, used[base])
                          if lv.get("pour") else
                          "Formwork for {0} Slab {1}".format(fl, used[base]))
    log("totals: " + ", ".join(
        "{0}={1}".format(k, v) for k, v in sorted(stats.items()) if v))
    return {"levels": levels, "stats": stats, "params_mu": mu}


# ── document adapter ───────────────────────────────────────────────────────
def _is_formwork_layer(doc, layer_index):
    # Rhino layer names are case-insensitively unique — compare accordingly.
    layer = doc.Layers[layer_index]
    if layer is None:
        return False
    fp = (layer.FullPath or "").lower()
    root = FW_ROOT.lower()
    return fp == root or fp.startswith(root + "::")


def _safe_layer_name(name):
    """Floor names come from user input; strip characters that are illegal
    or structural in Rhino layer paths."""
    clean = (name or "").replace("::", "-").replace(":", "-").strip()
    return clean or "unnamed"


def find_slabs_and_obstacles(doc, params, log):
    """(slab_breps, obstacle_geoms) from the active document. Read-only.

    Block instances (e.g. after QTO_Tool's Blockify) are exploded one level
    in-memory so their geometry still participates.
    """
    keyword = params["slab_layer_keyword"].lower()
    slabs, obstacles = [], []
    n_instances = 0
    # first-claim-wins guard, mirroring the QTO export's duplicate-GlobalId
    # guard: two objects sharing a stamp (copy-paste after the last checkup)
    # must not BOTH claim it, or the second slab's SLAB_GLOBALID would bind
    # the 4D link to the FIRST slab's take-off element - silently wrong.
    claimed_stamps = {}

    def _ident(obj, lname):
        """Identity a platform can be named after. The pour-break derived
        model tags every split piece with POUR / POUR_FLOOR / SOURCE_SLAB;
        an unsplit model carries none of those and the layer name is the
        whole identity.

        The "id" is what SLAB_GLOBALID is derived from, so it must be the
        SAME identity the QTO take-off export compresses into IfcGlobalId.
        That is the QTO_STABLE_ID user string when present (the checkup
        re-mints obj.Id on every run and stamps/preserves this key instead;
        the splitter stamps each piece with its own id), falling back to
        obj.Id for never-checked-up objects - where the take-off session's
        first checkup will stamp exactly that same id."""
        attrs = obj.Attributes
        pour = floor = ""
        try:
            pour = attrs.GetUserString("POUR") or ""
            floor = attrs.GetUserString("POUR_FLOOR") or ""
        except Exception:
            pass
        sid = str(obj.Id)
        try:
            stamp = attrs.GetUserString("QTO_STABLE_ID") or ""
            if stamp:
                ok, parsed = System.Guid.TryParse(stamp)
                if ok and parsed != System.Guid.Empty:
                    norm = str(parsed)
                    holder = claimed_stamps.get(norm)
                    if holder is None or holder == str(obj.Id):
                        claimed_stamps[norm] = str(obj.Id)
                        sid = norm
                    else:
                        log("  WARNING: objects {0} and {1} share "
                            "QTO_STABLE_ID {2} (copy-paste since the last "
                            "checkup?); {1} keeps its own object id so its "
                            "formwork cannot bind to the wrong slab - "
                            "re-run Start Checkup to repair the stamps"
                            .format(holder, obj.Id, norm))
        except Exception:
            pass
        return {"layer": lname, "name": (attrs.Name or ""),
                "pour": pour, "pour_floor": floor, "id": sid}

    def _accept(geom, lname, first, excluded, ident=None):
        if isinstance(geom, Rhino.Geometry.Extrusion):
            geom = geom.ToBrep(True)
        if not isinstance(geom, (Brep, Mesh)):
            return
        obstacles.append(geom)
        if isinstance(geom, Brep) and keyword in first and not excluded:
            slabs.append((geom, lname, ident))

    for obj in doc.Objects:
        if obj is None or obj.Attributes is None:
            continue
        li = obj.Attributes.LayerIndex
        if _is_formwork_layer(doc, li):
            continue
        if obj.IsHidden and not params["include_hidden_obstacles"]:
            continue
        layer = doc.Layers[li]
        if layer is not None and not layer.IsVisible \
                and not params["include_hidden_obstacles"]:
            continue
        lname = layer.Name if layer is not None else ""
        first = lname.split("_")[0].lower()
        excluded = any(kw and kw.lower() in lname.lower()
                       for kw in params.get("slab_layer_exclude") or [])
        ident = _ident(obj, lname)
        if isinstance(obj, Rhino.DocObjects.InstanceObject):
            idef = obj.InstanceDefinition
            if idef is None:
                continue
            n_instances += 1
            xf = obj.InstanceXform
            for part in idef.GetObjects() or []:
                g = part.Geometry
                if g is None:
                    continue
                g = g.Duplicate()
                if not g.Transform(xf):
                    continue
                _accept(g, lname, first, excluded, ident)
            continue
        _accept(obj.Geometry, lname, first, excluded, ident)
    log("document: {0} slab breps, {1} obstacle solids"
        "{2}".format(len(slabs), len(obstacles),
                     " ({0} block instances exploded in-memory)".format(
                         n_instances) if n_instances else ""))
    return slabs, obstacles


def ensure_layer(doc, names, color=None, locked=False):
    """Find-or-create nested layer path; returns layer index."""
    full = "::".join(names)
    idx = doc.Layers.FindByFullPath(full, -1)
    if idx >= 0:
        return idx
    parent_id = System.Guid.Empty
    for depth in range(len(names)):
        partial = "::".join(names[:depth + 1])
        idx = doc.Layers.FindByFullPath(partial, -1)
        if idx < 0:
            layer = Rhino.DocObjects.Layer()
            layer.Name = names[depth]
            if parent_id != System.Guid.Empty:
                layer.ParentLayerId = parent_id
            if color is not None and depth == len(names) - 1:
                layer.Color = color
            layer.IsLocked = locked and depth == len(names) - 1
            idx = doc.Layers.Add(layer)
            if idx >= 0:
                # provenance stamp: purge only ever deletes stamped layers
                doc.Layers[idx].SetUserString("FW_GENERATED", "1")
        parent_id = doc.Layers[idx].Id
    return idx


def _attributes(doc, layer_index, name, floor, fw_type, extra=None):
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = layer_index
    attr.Name = name
    attr.SetUserString("FLOOR", floor)
    attr.SetUserString("FW_TYPE", fw_type)
    if extra:
        for k, v in extra.items():
            attr.SetUserString(k, v)
    return attr


def write_to_doc(doc, result, params, log):
    """Add platforms/props under _FORMWORK. Only additions, one undo record."""
    to_m = model_to_meters(doc)
    size_mu = result["params_mu"]["prop_size"]
    sn = doc.BeginUndoRecord("Generate Formwork")
    n_added = 0
    try:
        for level in result["levels"]:
            fl = level["floor"]
            fls = _safe_layer_name(fl)
            pl_idx = ensure_layer(doc, [FW_ROOT, fls, "Platform"],
                                  COL_PLATFORM)
            for plat in level["platforms"]:
                attr = _attributes(doc, pl_idx, "Formwork Soffit Platform",
                                   fl, "platform")
                if doc.Objects.AddBrep(plat, attr) != System.Guid.Empty:
                    n_added += 1
            for prop in level["props"]:
                if prop["status"] == "NOHIT_SKIPPED":
                    continue
                sub, col = STATUS_LAYER[prop["status"]]
                pr_idx = ensure_layer(doc, [FW_ROOT, fls, sub], col)
                brep = prop_brep(prop, size_mu)
                if brep is None:
                    continue
                attr = _attributes(
                    doc, pr_idx, "Formwork Support", fl, "support",
                    {"FW_STATUS": prop["status"],
                     "FW_HEIGHT_M": "{0:.3f}".format(prop["height"] * to_m),
                     "FW_FOOT_Z": "{0:.3f}".format(prop["foot_z"])})
                if doc.Objects.AddBrep(brep, attr) != System.Guid.Empty:
                    n_added += 1
    finally:
        if sn > 0:
            doc.EndUndoRecord(sn)
    if params["lock_layers"]:
        root = doc.Layers.FindByFullPath(FW_ROOT, -1)
        if root >= 0:
            for layer in doc.Layers:
                if layer is not None and not layer.IsDeleted and \
                        _is_formwork_layer(doc, layer.Index) and \
                        layer.FullPath != FW_ROOT:
                    layer.IsLocked = True
    doc.Views.Redraw()
    log("added {0} objects under {1} (existing objects untouched)".format(
        n_added, FW_ROOT))
    return n_added


def purge_formwork(doc, log, types=None):
    """Remove GENERATED formwork: only objects stamped with the FW_TYPE user
    string, only layers stamped FW_GENERATED. Anything else that a user put
    under _FORMWORK is reported and left untouched.

    ``types``: optional FW_TYPE whitelist — the platform generator's
    auto-purge passes ("platform", "support") so it never eats the side
    forms/bulkheads that sideform_gen_rhino.py parked in the same tree.
    None (the explicit purge mode) removes every generated type."""
    doomed_layers = [l for l in doc.Layers
                     if l is not None and not l.IsDeleted
                     and _is_formwork_layer(doc, l.Index)]
    if not doomed_layers:
        log("purge: no _FORMWORK layers found")
        return
    sn = doc.BeginUndoRecord("Purge Formwork")
    n_obj = n_lay = kept_obj = kept_lay = 0
    try:
        for layer in doomed_layers:
            layer.IsLocked = False
        es = Rhino.DocObjects.ObjectEnumeratorSettings()
        es.NormalObjects = True
        es.LockedObjects = True
        es.HiddenObjects = True
        for obj in list(doc.Objects.GetObjectList(es)):
            if obj is None or obj.Attributes is None:
                continue
            if not _is_formwork_layer(doc, obj.Attributes.LayerIndex):
                continue
            fw_type = obj.Attributes.GetUserString("FW_TYPE")
            if fw_type and (types is None or fw_type in types):
                if doc.Objects.Delete(obj, True):
                    n_obj += 1
            else:
                kept_obj += 1
        cur = doc.Layers.CurrentLayerIndex
        if cur >= 0 and _is_formwork_layer(doc, cur):
            for l in doc.Layers:
                if l is not None and not l.IsDeleted \
                        and not _is_formwork_layer(doc, l.Index):
                    doc.Layers.SetCurrentLayerIndex(l.Index, True)
                    break
        for layer in sorted(doomed_layers,
                            key=lambda l: -l.FullPath.count(":")):
            if layer.GetUserString("FW_GENERATED") == "1" \
                    and doc.Layers.Delete(layer.Index, True):
                n_lay += 1
            else:
                kept_lay += 1
    finally:
        if sn > 0:
            doc.EndUndoRecord(sn)
    doc.Views.Redraw()
    log("purged {0} formwork objects, {1} layers".format(n_obj, n_lay))
    if kept_obj or kept_lay:
        log("  kept {0} objects / {1} layers under {2} that this tool did "
            "not generate (or that are still occupied)".format(
                kept_obj, kept_lay, FW_ROOT))


def export_3dm(doc, result, path, log):
    """Write platforms/props to a separate .3dm — document untouched."""
    to_m = model_to_meters(doc)
    size_mu = result["params_mu"]["prop_size"]
    f3 = Rhino.FileIO.File3dm()
    f3.Settings.ModelUnitSystem = doc.ModelUnitSystem

    seen = {}                       # full path -> (index, layer Guid)

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
    for level in result["levels"]:
        fl = level["floor"]
        fls = _safe_layer_name(fl)
        pl_idx = add_layer([FW_ROOT, fls, "Platform"], COL_PLATFORM)
        for plat in level["platforms"]:
            attr = _attributes(doc, pl_idx, "Formwork Soffit Platform",
                               fl, "platform")
            f3.Objects.AddBrep(plat, attr)
            n += 1
        for prop in level["props"]:
            if prop["status"] == "NOHIT_SKIPPED":
                continue
            sub, col = STATUS_LAYER[prop["status"]]
            pr_idx = add_layer([FW_ROOT, fls, sub], col)
            brep = prop_brep(prop, size_mu)
            if brep is None:
                continue
            attr = _attributes(
                doc, pr_idx, "Formwork Support", fl, "support",
                {"FW_STATUS": prop["status"],
                 "FW_HEIGHT_M": "{0:.3f}".format(prop["height"] * to_m),
                 "FW_FOOT_Z": "{0:.3f}".format(prop["foot_z"])})
            f3.Objects.AddBrep(brep, attr)
            n += 1
    if os.path.exists(path):
        log("export: overwriting existing {0}".format(path))
    ok = f3.Write(path, 7)
    log("export: {0} objects -> {1} ({2})".format(
        n, path, "ok" if ok else "WRITE FAILED"))
    return ok


def _curve_points_m(curve, to_m):
    """Closed curve -> [[x, y] ...] in metres (polyline or 64-pt sampling)."""
    pts = None
    try:
        rc, pl = curve.TryGetPolyline()
        if rc:
            pts = list(pl)
    except Exception:
        pts = None
    if pts is None:
        params = curve.DivideByCount(64, True)
        pts = [curve.PointAt(t) for t in params] if params else []
    out = [[p.X * to_m, p.Y * to_m] for p in pts]
    if out and out[0] != out[-1]:
        out.append(list(out[0]))
    return out


def dump_json(doc, result, path, log):
    """Geometry handoff for the IFC converter: levels, regions, holes, props
    — all in METRES, absolute world coordinates."""
    to_m = model_to_meters(doc)
    mu = result["params_mu"]
    data = {
        "units": "m",
        "source_model": doc.Path or "",
        "panel_thickness": mu["panel_thickness"] * to_m,
        "prop_size": mu["prop_size"] * to_m,
        "levels": [],
    }
    for level in result["levels"]:
        data["levels"].append({
            "floor": level["floor"],
            "name": level.get("name"),
            "pour": level.get("pour"),
            "slabs": level.get("slabs") or [],
            "slab_ids": level.get("slab_ids") or [],
            "z": level["z"] * to_m,
            "regions": [_curve_points_m(c, to_m) for c in level["regions"]],
            "holes": [_curve_points_m(c, to_m) for c in level["holes"]],
            "props": [{
                "x": p["x"] * to_m, "y": p["y"] * to_m,
                "top_z": p["top_z"] * to_m,
                "foot_z": None if p["foot_z"] is None else p["foot_z"] * to_m,
                "status": p["status"],
            } for p in level["props"]],
        })
    import io
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(u"{0}".format(json.dumps(data, indent=1)))
    log("json handoff -> {0}".format(path))


# ── entry point ────────────────────────────────────────────────────────────
def main(params=None):
    p = dict(PARAMS)
    if params:
        p.update(params)
    doc = Rhino.RhinoDoc.ActiveDoc
    log = Log()
    log("formwork_gen_rhino [{0}] — model units: {1}".format(
        p["mode"], doc.ModelUnitSystem))

    base = None
    if doc.Path:
        base = os.path.join(
            os.path.dirname(doc.Path),
            os.path.splitext(os.path.basename(doc.Path))[0])
    log_path = p["log_path"] or (
        (base or os.path.join(os.path.expanduser("~"), "formwork"))
        + "_formwork_log.txt")

    result = None
    if p["mode"] == "purge":
        purge_formwork(doc, log)
    else:
        slabs, obstacles = find_slabs_and_obstacles(doc, p, log)
        if not slabs:
            log("no slab breps found (layer keyword '{0}', excludes {1}) — "
                "check layer names and object types".format(
                    p["slab_layer_keyword"], p["slab_layer_exclude"]))
            log.save(log_path)
            return None
        result = generate_formwork(slabs, obstacles, doc, p, log)
        if p["mode"] == "generate":
            if doc.Layers.FindByFullPath(FW_ROOT, -1) >= 0:
                log("existing {0} tree found — purging generated "
                    "platforms/supports before re-adding (side forms and "
                    "bulkheads are left alone)".format(FW_ROOT))
                purge_formwork(doc, log, ("platform", "support"))
            write_to_doc(doc, result, p, log)
        elif p["mode"] == "export":
            path = p["export_path"] or (
                (base or os.path.join(os.path.expanduser("~"), "formwork"))
                + "_formwork.3dm")
            export_3dm(doc, result, path, log)
            dump_json(doc, result,
                      os.path.splitext(path)[0] + ".json", log)

    log.save(log_path)
    log("log saved: {0}".format(log_path))
    return result


if __name__ == "__main__":
    main()
