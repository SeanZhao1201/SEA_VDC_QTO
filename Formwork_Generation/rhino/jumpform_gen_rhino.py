# -*- coding: utf-8 -*-
"""Jump-form + reshoring generator — the core climbing works.

Companion to ``formwork_gen_rhino.py`` (soffit platforms + props) and
``sideform_gen_rhino.py`` (edge forms + bulkheads). This engine fills the
270 unbound Mast4D animation slots (2026-08-20 review): per core-wall
lift it emits BOTH jump-form states as separate geometry sets, plus pole
shores for reshoring under every cycle slab.

* **Jump Form Locked** — one straight form strip per straight wall
  face (the section loop split into colinear runs), hugging the face
  on its away side, ``panel_thickness`` thick. Vertical panels ONLY —
  a core jump form has NO horizontal decks (field correction
  2026-08-24 against the Waverly reference model, which models zero
  horizontal geometry in its jump-form groups; platforms had been
  invented here and were removed).
* **Jump Form Unlocked** — the same per-face strips retracted
  ``roll_back`` along each face's own normal (exterior faces outward,
  shaft faces INTO the shaft — measured ~4 ft on Waverly), same
  z-span. The 4D consumer shows/hides each state per task — an
  equality-bound search set cannot re-task one element, so the two
  states MUST be separate elements (schedule activity 2020 installs
  Unlocked while removing Locked in the same task). The climb is
  animated purely by floor-set visibility; geometry never moves.
* Panel z (Waverly-verified): from the lift base (no downward lap) up
  to ``form_top_drop`` below the lift top.
* **Pole Shore for Reshoring** — a sparser prop lattice under every
  slab (``reshore_spacing``), reusing the platform engine's ray-cast
  feet. ``FLOOR`` is the slab's floor — the floor the shore SUPPORTS,
  which is the semantics the schedule reasons in (a reshore under the
  L05 deck belongs to L05 no matter which slab its base sits on).

Wall targets: layer name's first ``_`` segment must contain
``wall_layer_keyword`` AND the full name must contain one of
``wall_layer_include`` (the CORE filter — gang-formed perimeter walls
are not jump-formed). Wall solids are clustered into BANKS by plan
overlap (two independent cores climb as two units); banks are lettered
by descending plan area, so the big core is always "A".

Element names are the schedule's component vocabulary verbatim
("Jump Form Locked" / "Jump Form Unlocked" / "Pole Shore for
Reshoring") and never vary by floor; floor/state/bank live in user
strings here and in the ``QTO Properties`` pset in the IFC. Identity:
``WALL_GLOBALID`` derives from the wall's ``QTO_STABLE_ID`` stamp when
present (the QTO checkup re-mints ``obj.Id`` on every run), with the
same first-claim-wins duplicate guard the other consumers have.

Modes mirror the other engines: ``generate`` adds under ``_FORMWORK``
(purgeable via FW_GENERATED/FW_TYPE stamps, own types only on the
auto-purge), ``export`` writes a separate .3dm + JSON handoff and never
touches the document, ``purge`` delegates to the shared purge.

Placeholder fidelity, as ever: per-face strips locate and size the
climbing works for 4D sequencing — no rails, brackets, anchors or
engineered design, and deliberate corner gaps between the strips
(matching the Waverly reference).

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
from Rhino.Geometry import (Brep, Curve, Plane, Point3d, PolylineCurve,
                            Vector3d)
from System.Drawing import Color

import formwork_gen_rhino as fw
import sideform_gen_rhino as sf

PARAMS = {
    "mode": "generate",          # generate | purge | export
    "panel_thickness": 0.30,     # form strip thickness (Waverly: 1 ft)
    "roll_back": 1.20,           # UNLOCKED strip retreat off its face
                                 # (Waverly: ~4 ft clear)
    "form_top_drop": 0.35,       # strip top below the lift top
                                 # (Waverly: 1.15 ft on a 9'-8" lift)
    "min_loop_len": 1.0,         # section loops shorter than this skipped
    "min_face_len": 0.25,        # straight runs shorter than this dropped
    "reshore_spacing": 4.5,      # reshore lattice pitch (sparser than the
                                 # 3.0 m shoring props — reshores carry
                                 # redistribution, not the fresh pour)
    "reshore_size": 0.15,        # reshore plan size (square)
    "edge_inset": 0.5,           # reshores keep this from slab edges
    "min_clear": 0.30,           # ray hit closer -> bearing, no reshore
    "max_prop": 5.0,             # taller -> TALL flag
    "grade_z": None,             # metres world Z for no-hit reshore feet
    "wall_layer_keyword": "wall",  # first '_' segment must contain this
    "wall_layer_include": ["core"],  # full layer name must contain one
                                 # (jump forms climb the CORE only)
    "slab_layer_keyword": "slab",
    # same FORMWORK filter as the platform engine - nothing reshores a
    # slab on grade or a topping. Do not unify with the pour-break one.
    "slab_layer_exclude": ["sog", "topping"],
    "include_hidden_obstacles": False,
    "lock_layers": True,
    "export_path": None,         # None -> <doc>_jumpform.3dm
    "log_path": None,
}

# the schedule's component vocabulary, verbatim - binding is by property
# EQUALITY, so these strings must never vary by floor (Mast4D contract)
NAME_LOCKED = "Jump Form Locked"
NAME_UNLOCKED = "Jump Form Unlocked"
NAME_RESHORE = "Pole Shore for Reshoring"

COL_LOCKED = Color.FromArgb(90, 105, 120)     # slate — anchored steel
COL_UNLOCKED = Color.FromArgb(235, 140, 50)   # amber — released/climbing
COL_RESHORE = Color.FromArgb(204, 121, 167)   # reddish purple

STATE_LAYER = {"LOCKED": ("JumpForm_Locked", COL_LOCKED),
               "UNLOCKED": ("JumpForm_Unlocked", COL_UNLOCKED)}


def _bank_letter(i):
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return alphabet[i] if i < len(alphabet) else "X{0}".format(i + 1)


# ── section loops of a wall solid ──────────────────────────────────────────
def _closed_sections(brep, z, tol):
    """Closed plan curves where the horizontal plane at ``z`` cuts the
    wall solid. Multiple loops are normal (a ring core cuts to an outer
    and a shaft loop)."""
    plane = Plane(Point3d(0, 0, z), Vector3d.ZAxis)
    try:
        curves = Brep.CreateContourCurves(brep, plane)
    except Exception:
        curves = None
    if not curves:
        return []
    joined = Curve.JoinCurves(list(curves), tol * 10)
    if not joined:
        joined = curves
    return [c for c in joined if c is not None and c.IsClosed]


def section_loops(brep, z0, z1, min_len, tol, log, ctx):
    """Best set of closed section loops for one lift.

    Tries several heights: high first (above door heads — a mid-lift
    plane through a doorway breaks the ring into open segments), then
    mid. Falls back to the plan bounding-box rectangle — the precedented
    placeholder for a section that will not close."""
    # FIRST successful cut wins, tried high-to-low: the whole point is
    # cutting ABOVE door heads. A "more loops wins" preference inverts
    # on a non-ring wall with a doorway — the mid-lift cut severs the
    # wall into MORE closed pier loops than the one correct full-face
    # loop above the door, and jamb strips would be emitted inside the
    # opening (adversarial review, 2026-08-24).
    for f in (0.85, 0.7, 0.5, 0.95):
        zc = z0 + (z1 - z0) * f
        loops = [c for c in _closed_sections(brep, zc, tol)
                 if c.GetLength() >= min_len]
        if loops:
            # zc rides along: the away-side membership probes must run
            # at the SAME height the section was cut — at mid-lift a
            # probe can land inside a doorway void (doors reach past
            # mid-lift on a 9'-8" storey) and resolve nothing
            return loops, False, zc
    bb = brep.GetBoundingBox(True)
    rect = PolylineCurve([
        Point3d(bb.Min.X, bb.Min.Y, (z0 + z1) / 2.0),
        Point3d(bb.Max.X, bb.Min.Y, (z0 + z1) / 2.0),
        Point3d(bb.Max.X, bb.Max.Y, (z0 + z1) / 2.0),
        Point3d(bb.Min.X, bb.Max.Y, (z0 + z1) / 2.0),
        Point3d(bb.Min.X, bb.Min.Y, (z0 + z1) / 2.0)])
    log("  WARNING: no closed section loop on {0} — using the plan "
        "bounding box (placeholder)".format(ctx))
    return [rect], True, (z0 + z1) / 2.0


def _face_runs(loop, min_len, tol):
    """Closed section loop -> [(p0, p1)] straight runs, colinear-merged.

    A jump form is one straight strip per wall FACE (the Waverly
    reference convention), not a closed offset ring — so the loop is
    decomposed at its true corners. The ring points come from
    TryGetPolyline when the section is a polyline (exact corners) or
    64-pt sampling otherwise; colinear merging reconstitutes the
    straight faces either way. Runs shorter than ``min_len`` are
    dropped (counted by the caller)."""
    pts = _ring_pts(loop)
    if len(pts) < 4:
        return [], 0
    core = pts[:-1]
    n = len(core)

    def _dir(a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(dx, dy)
        if ln <= tol:
            return None
        return (dx / ln, dy / ln)

    # rotate the ring so index 0 starts at a corner — otherwise one
    # straight face split across the seam becomes two half strips
    start = 0
    for i in range(n):
        d_prev = _dir(core[(i - 1) % n], core[i])
        d_next = _dir(core[i], core[(i + 1) % n])
        if d_prev is None or d_next is None or \
                d_prev[0] * d_next[0] + d_prev[1] * d_next[1] < 0.999:
            start = i
            break
    ring = core[start:] + core[:start]

    runs = []
    run_start = ring[0]
    run_dir0 = None
    prev = ring[0]
    for i in range(1, n + 1):
        cur = ring[i % n]
        d = _dir(prev, cur)
        if d is None:
            continue
        # compare against the run's STARTING direction, not the
        # previous segment: a per-pair test never fires on a corner
        # tessellated finer than the threshold (a 1-deg-per-vertex
        # fillet), and the whole ring would collapse into one
        # zero-length run — the cumulative cap breaks the run once
        # total turning exceeds the threshold
        if run_dir0 is not None and \
                d[0] * run_dir0[0] + d[1] * run_dir0[1] < 0.999:
            runs.append((run_start, prev))
            run_start = prev
            run_dir0 = d
        elif run_dir0 is None:
            run_dir0 = d
        prev = cur
    runs.append((run_start, prev))

    kept, dropped = [], 0
    for p0, p1 in runs:
        if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) >= min_len:
            kept.append((p0, p1))
        else:
            dropped += 1
    return kept, dropped


def _run_away_normal(p0, p1, brep, probe_z, probe, tol, was_bbox,
                     centroid):
    """Unit normal of the run pointing AWAY from the wall solid.

    Resolved by membership probes ``probe`` off both sides at three
    points along the run (a single midpoint can sit in a doorway void),
    at ``probe_z`` — the height the section was CUT at, where the face
    is known to be solid. The probe distance is far below any wall
    thickness, where exactly one side is inside the solid — at
    roll_back scale both sides can be void. For the bbox-fallback
    rectangle the solid test is meaningless (the rect edge may lie off
    a concave wall), so away = away from the rect centroid. Returns
    (nx, ny) or None (caller skips loudly)."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    ln = math.hypot(dx, dy)
    if ln <= tol:
        return None
    nx, ny = -dy / ln, dx / ln
    if was_bbox:
        mx, my = (p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0
        if (mx - centroid[0]) * nx + (my - centroid[1]) * ny >= 0:
            return (nx, ny)
        return (-nx, -ny)
    votes = 0
    for f in (0.25, 0.5, 0.75):
        mx = p0[0] + dx * f
        my = p0[1] + dy * f
        a = sf._inside(brep, mx + nx * probe, my + ny * probe, probe_z,
                       tol)
        b = sf._inside(brep, mx - nx * probe, my - ny * probe, probe_z,
                       tol)
        if a and not b:
            votes += 1          # +n side is toward the solid
        elif b and not a:
            votes -= 1          # -n side is toward the solid
    if votes > 0:
        return (-nx, -ny)
    if votes < 0:
        return (nx, ny)
    return None


def _strip_clear(p0, p1, na, offset, thick, breps, probe_z, tol):
    """False when the retreat corridor (face -> strip outer edge) hits
    wall material. A shaft slot narrower than roll_back buries the
    UNLOCKED strip in — or teleports it beyond — the opposite wall; the
    physical unit cannot retreat there either, so the honest behaviour
    is a loud skip (same standard as an unresolvable away side).
    5 depths x 3 length fractions; two hits condemn (a doorway in the
    opposite wall can void single samples)."""
    span = offset + thick
    hits = 0
    for k in range(5):
        d = span * (k + 0.5) / 5.0
        for lf in (0.25, 0.5, 0.75):
            mx = p0[0] + (p1[0] - p0[0]) * lf + na[0] * d
            my = p0[1] + (p1[1] - p0[1]) * lf + na[1] * d
            for b in breps:
                if sf._inside(b, mx, my, probe_z, tol):
                    hits += 1
                    break
            if hits >= 2:
                return False
    return True


def _strip_profile(p0, p1, na, offset, thick):
    """Closed plan rectangle of one form strip: the face segment pushed
    ``offset`` along its away normal, ``thick`` deep."""
    ax = p0[0] + na[0] * offset
    ay = p0[1] + na[1] * offset
    bx = p1[0] + na[0] * offset
    by = p1[1] + na[1] * offset
    prof = [(ax, ay), (bx, by),
            (bx + na[0] * thick, by + na[1] * thick),
            (ax + na[0] * thick, ay + na[1] * thick)]
    prof.append(prof[0])
    return prof


def _ring_pts(curve):
    """Closed curve -> [(x, y) ...] ring in MODEL units, closed."""
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
    out = [(p.X, p.Y) for p in pts]
    if out and out[0] != out[-1]:
        out.append(out[0])
    return out


# ── document adapter ───────────────────────────────────────────────────────
def find_jumpform_inputs(doc, params, log):
    """(walls, slabs, obstacles) in one read-only pass.

    walls: [{'brep', 'bb', 'layer', 'id', 'name'}] — core walls only.
    slabs: [(Brep, layer, ident)] — reshore targets, platform-engine
    semantics (keyword + sog/topping excludes).
    obstacles: every solid, for the reshore rays.

    One claimed-stamps namespace across BOTH target kinds, so a slab and
    a wall sharing a copy-pasted QTO_STABLE_ID resolve first-claim-wins
    with one loud warning, same as everywhere else."""
    wall_kw = params["wall_layer_keyword"].lower()
    wall_inc = [w.lower() for w in params.get("wall_layer_include") or []
                if w]
    slab_kw = params["slab_layer_keyword"].lower()
    walls, slabs, obstacles = [], [], []
    n_instances = 0
    claimed_stamps = {}

    def _ident(obj, lname):
        """Same identity contract as formwork_gen_rhino._ident: prefer a
        valid-GUID QTO_STABLE_ID stamp (the checkup re-mints obj.Id on
        every run), first claim wins on duplicates — a copied stamp
        degrades to a loud, detectably-missing 4D link, never a silent
        bind to the wrong element."""
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
                            "jump form cannot bind to the wrong element - "
                            "re-run Start Checkup to repair the stamps"
                            .format(holder, obj.Id, norm))
        except Exception:
            pass
        return {"layer": lname, "name": (attrs.Name or ""),
                "pour": pour, "pour_floor": floor, "id": sid}

    def _accept(geom, lname, ident):
        if isinstance(geom, Rhino.Geometry.Extrusion):
            geom = geom.ToBrep(True)
        if not isinstance(geom, (Brep, Rhino.Geometry.Mesh)):
            return
        obstacles.append(geom)
        if not isinstance(geom, Brep):
            return
        first = lname.split("_")[0].lower()
        lower = lname.lower()
        if wall_kw in first and any(inc in lower for inc in wall_inc):
            walls.append({"brep": geom, "bb": geom.GetBoundingBox(True),
                          "layer": lname, "id": ident["id"],
                          "name": ident["name"]})
            return
        excluded = any(kw and kw.lower() in lower
                       for kw in params.get("slab_layer_exclude") or [])
        if slab_kw in first and not excluded:
            slabs.append((geom, lname, ident))

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
                _accept(g, lname, ident)
            continue
        _accept(obj.Geometry, lname, ident)
    log("document: {0} core walls, {1} reshore slabs, {2} obstacle solids"
        "{3}".format(len(walls), len(slabs), len(obstacles),
                     " ({0} block instances exploded in-memory)".format(
                         n_instances) if n_instances else ""))
    return walls, slabs, obstacles


def cluster_banks(walls, pad_mu):
    """Group wall solids into plan-overlap clusters (banks). Two cores
    ~25 m apart are two independent climbing units; the stacked lifts of
    one core overlap in plan (they stack flush), so greedy overlap
    clustering is stable. Banks are lettered by DESCENDING plan area —
    the big core is always bank A."""
    clusters = []
    for w in sorted(walls, key=lambda w: (w["bb"].Min.Z, w["bb"].Min.X,
                                          w["bb"].Min.Y)):
        bb = w["bb"]
        placed = None
        for cl in clusters:
            c = cl["bb"]
            if bb.Min.X <= c.Max.X + pad_mu and \
                    bb.Max.X >= c.Min.X - pad_mu and \
                    bb.Min.Y <= c.Max.Y + pad_mu and \
                    bb.Max.Y >= c.Min.Y - pad_mu:
                placed = cl
                break
        if placed is None:
            clusters.append({"walls": [w],
                             "bb": Rhino.Geometry.BoundingBox(bb.Min,
                                                              bb.Max)})
        else:
            placed["walls"].append(w)
            # BoundingBox is a value type: mutate a copy, write it back
            u = placed["bb"]
            u.Union(bb)
            placed["bb"] = u
    clusters.sort(key=lambda cl: -((cl["bb"].Max.X - cl["bb"].Min.X) *
                                   (cl["bb"].Max.Y - cl["bb"].Min.Y)))
    for i, cl in enumerate(clusters):
        cl["bank"] = _bank_letter(i)
        cl["walls"].sort(key=lambda w: w["bb"].Min.Z)
    return clusters


# ── core generation (document-independent, headless-testable) ──────────────
def generate_jumpforms(walls, slabs, obstacle_geoms, doc, params=None,
                       log=None):
    """Compute both jump-form state sets + reshores. Touches no document.

    Returns dict:
        jumpforms: [{brep, kind: always "panel", state, floor, bank,
                     z0, z1, profile_mu, wall_id, wall_layer,
                     lift_z0, lift_z1}]
        reshores:  [{x, y, top_z, foot_z, height, status, floor,
                     slab_ids}]
        banks:     {letter: lift count}
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
    for key in ("panel_thickness", "roll_back", "form_top_drop",
                "min_loop_len", "min_face_len",
                "reshore_spacing", "reshore_size", "edge_inset",
                "min_clear", "max_prop"):
        mu[key] = p[key] * to_mu
    mu["grade_z"] = None if p["grade_z"] is None else p["grade_z"] * to_mu
    mu["_eps"] = 0.01 * to_mu

    floors = fw.read_floor_elevations(doc)
    if floors:
        log("floor names from QTO FloorElevations ({0} floors)".format(
            len(floors)))

    stats = {"panels_locked": 0, "panels_unlocked": 0,
             "bbox_fallbacks": 0, "skipped_bands": 0, "short_runs": 0,
             "OK": 0, "TALL": 0, "GROUNDED": 0, "GROUNDED_TALL": 0,
             "NOHIT_SKIPPED": 0, "bearing_skipped": 0}
    jumpforms = []
    banks_report = {}

    banks = cluster_banks(walls, 1.0 * to_mu)
    for cl in banks:
        bank = cl["bank"]
        # A LIFT is a base elevation, not a wall solid: a core modeled as
        # two disjoint runs per storey (the checkup joins only touching
        # solids) must not double the lift ladder the banks report counts.
        lift_tol = max(tol * 10, 0.1 * to_mu)
        lifts = []
        for w in cl["walls"]:                 # already sorted by z0
            wz0 = w["bb"].Min.Z
            if lifts and abs(wz0 - lifts[-1][0]) <= lift_tol:
                lifts[-1][1].append(w)
            else:
                lifts.append([wz0, [w]])
        banks_report[bank] = len(lifts)
        for lift_z0, lift_walls in lifts:
            # clearance set for the retreat-corridor check: every wall
            # solid of this lift, own included (a ring's own opposite
            # wall is the same brep)
            lift_breps = [lw["brep"] for lw in lift_walls]
            for w in lift_walls:
                brep, bb = w["brep"], w["bb"]
                z0, z1 = bb.Min.Z, bb.Max.Z
                if z1 - z0 <= tol:
                    continue
                fl = fw.floor_name(z0, floors, to_m)
                ctx = "bank {0} lift {1} ({2:.2f}..{3:.2f})".format(
                    bank, fl, z0, z1)
                loops, was_bbox, z_cut = section_loops(
                    brep, z0, z1, mu["min_loop_len"], tol, log, ctx)
                if was_bbox:
                    stats["bbox_fallbacks"] += 1
                # panel z, Waverly-verified: from the lift base (no
                # downward lap) up to form_top_drop below the lift top
                pz1 = max(z1 - mu["form_top_drop"], z0 + mu["_eps"])
                cx = (bb.Min.X + bb.Max.X) / 2.0
                cy = (bb.Min.Y + bb.Max.Y) / 2.0

                def _add(state, body, profile):
                    jumpforms.append({
                        "brep": body, "kind": "panel", "state": state,
                        "floor": fl, "bank": bank,
                        "z0": z0, "z1": pz1,
                        "profile_mu": profile,
                        "wall_id": w["id"], "wall_layer": w["layer"],
                        "lift_z0": z0, "lift_z1": z1})

                for loop in loops:
                    runs, dropped = _face_runs(
                        loop, mu["min_face_len"], tol)
                    stats["short_runs"] += dropped
                    if not runs:
                        log("  WARNING: no usable wall faces on a "
                            "section loop {0} — strips SKIPPED".format(
                                ctx))
                        stats["skipped_bands"] += 1
                        continue
                    for p0, p1 in runs:
                        # per-face away normal; at roll_back scale both
                        # sides can be void, so the side is resolved at
                        # probe scale. A wrong-side strip is worse than
                        # no strip: unresolvable skips loudly.
                        na = _run_away_normal(
                            p0, p1, brep, z_cut, mu["_eps"] * 2, tol,
                            was_bbox, (cx, cy))
                        if na is None:
                            log("  WARNING: could not resolve the away "
                                "side of a wall face {0} — strip "
                                "SKIPPED".format(ctx))
                            stats["skipped_bands"] += 1
                            continue
                        # LOCKED strip hugs the face; UNLOCKED is the
                        # same strip retreated roll_back along the same
                        # normal (Waverly: exterior faces move out, the
                        # shaft faces move INTO the shaft). Each strip's
                        # retreat corridor must be VOID — a slot
                        # narrower than the roll-back cannot take the
                        # unit and must refuse loudly, never bury the
                        # strip in the opposite wall.
                        if _strip_clear(p0, p1, na, 0.0,
                                        mu["panel_thickness"],
                                        lift_breps, z_cut, tol):
                            prof_l = _strip_profile(
                                p0, p1, na, 0.0, mu["panel_thickness"])
                            body = sf.panel_brep(prof_l, z0, pz1, tol,
                                                 log)
                            if body is not None:
                                _add("LOCKED", body, prof_l)
                                stats["panels_locked"] += 1
                        else:
                            log("  WARNING: locked strip does not fit "
                                "against its face {0} — SKIPPED".format(
                                    ctx))
                            stats["skipped_bands"] += 1
                        if _strip_clear(p0, p1, na, mu["roll_back"],
                                        mu["panel_thickness"],
                                        lift_breps, z_cut, tol):
                            prof_u = _strip_profile(
                                p0, p1, na, mu["roll_back"],
                                mu["panel_thickness"])
                            body = sf.panel_brep(prof_u, z0, pz1, tol,
                                                 log)
                            if body is not None:
                                _add("UNLOCKED", body, prof_u)
                                stats["panels_unlocked"] += 1
                        else:
                            log("  WARNING: unlocked retreat does not "
                                "fit ({0:.2f} into a narrower void) {1} "
                                "— strip SKIPPED".format(
                                    (mu["roll_back"] +
                                     mu["panel_thickness"]) * to_m,
                                    ctx))
                            stats["skipped_bands"] += 1
        log("  bank {0}: {1} lifts ({2} wall solids), z {3:.2f} .. "
            "{4:.2f}".format(bank, len(lifts), len(cl["walls"]),
                             cl["bb"].Min.Z, cl["bb"].Max.Z))

    # ── reshores: sparser prop lattice under every slab ────────────────
    reshores = []
    if slabs:
        mesh = fw.obstacle_mesh(obstacle_geoms, log)
        if not fw._strongbox_available():
            log("WARNING: hit-face detection unavailable — reshores under "
                "bearing walls/columns may be WRONG. Run via "
                "_-RunPythonScript (IronPython).")
        cos_thr = math.cos(math.radians(20.0))
        all_faces = []
        for brep, layer, ident in slabs:
            for face, z in fw.soffit_faces(brep, cos_thr, log, layer):
                all_faces.append((face, z, ident))
        clusters = fw.cluster_by_z(all_faces, 0.02 * to_mu)
        # resolve_prop reads panel_thickness to drop the head below a
        # platform; a reshore head bears on the slab soffit directly
        mu_prop = {"panel_thickness": 0.0, "_eps": mu["_eps"],
                   "min_clear": mu["min_clear"],
                   "max_prop": mu["max_prop"], "grade_z": mu["grade_z"]}
        for z, faces, idents in clusters:
            fl = fw.floor_name(z, floors, to_m)
            # cluster_by_z groups by POUR before Z, so the cluster is
            # single-pour by construction; reshores inherit the pour of
            # the slab piece they support (zone attribution, 2026-08-24)
            pours = set(fw._pour_of(i) for i in idents)
            pour = pours.pop() if len(pours) == 1 else ""
            slab_ids = []
            for i in idents:
                sid = (i or {}).get("id") or ""
                if sid and sid not in slab_ids:
                    slab_ids.append(sid)
            n_level, seen = 0, set()
            for face in faces:
                pts = fw.grid_points_on_face(
                    face, mu["reshore_spacing"], mu["edge_inset"], tol)
                for pt in pts:
                    key = (round(pt.X / tol) if tol > 0 else pt.X,
                           round(pt.Y / tol) if tol > 0 else pt.Y)
                    if key in seen:
                        continue
                    seen.add(key)
                    prop = fw.resolve_prop(pt, z, mesh, mu_prop)
                    if prop is None:
                        stats["bearing_skipped"] += 1
                        continue
                    prop["floor"] = fl
                    prop["pour"] = pour or None
                    prop["slab_ids"] = slab_ids
                    reshores.append(prop)
                    stats[prop["status"]] += 1
                    if prop["status"] != "NOHIT_SKIPPED":
                        n_level += 1
            log("  reshore {0:10} z={1:10.3f}  poles={2}".format(
                fl, z, n_level))

    log("jump forms: {0} locked + {1} unlocked strips "
        "({2} bbox fallbacks, {3} skipped, {4} short runs dropped); "
        "reshores: {5}".format(
            stats["panels_locked"], stats["panels_unlocked"],
            stats["bbox_fallbacks"], stats["skipped_bands"],
            stats["short_runs"],
            sum(1 for r in reshores
                if r["status"] != "NOHIT_SKIPPED")))
    return {"jumpforms": jumpforms, "reshores": reshores,
            "banks": banks_report, "stats": stats, "params_mu": mu}


# ── sinks ──────────────────────────────────────────────────────────────────
def _jf_attr(doc, layer_index, jf):
    name = NAME_LOCKED if jf["state"] == "LOCKED" else NAME_UNLOCKED
    extra = {"STATE": jf["state"], "BANK": jf["bank"],
             "FW_KIND": jf["kind"], "FW_WALL_ID": jf["wall_id"]}
    return fw._attributes(doc, layer_index, name, jf["floor"], "jumpform",
                          extra)


def _reshore_attr(doc, layer_index, prop, to_m):
    extra = {"FW_STATUS": prop["status"],
             "FW_HEIGHT_M": "{0:.3f}".format(prop["height"] * to_m),
             "FW_FOOT_Z": "{0:.3f}".format(prop["foot_z"])}
    if prop.get("pour"):
        extra["POUR"] = str(prop["pour"])
    return fw._attributes(
        doc, layer_index, NAME_RESHORE, prop["floor"], "reshore", extra)


def _layer_for(jf):
    return STATE_LAYER[jf["state"]]


def write_to_doc(doc, result, params, log):
    """Add jump forms + reshores under _FORMWORK. Only additions, one
    undo record."""
    to_m = fw.model_to_meters(doc)
    size_mu = result["params_mu"]["reshore_size"]
    sn = doc.BeginUndoRecord("Generate Jump Forms")
    n_added = 0
    try:
        for jf in result["jumpforms"]:
            fls = fw._safe_layer_name(jf["floor"])
            sub, col = _layer_for(jf)
            idx = fw.ensure_layer(doc, [fw.FW_ROOT, fls, sub], col)
            if doc.Objects.AddBrep(jf["brep"],
                                   _jf_attr(doc, idx, jf)) != \
                    System.Guid.Empty:
                n_added += 1
        for prop in result["reshores"]:
            if prop["status"] == "NOHIT_SKIPPED":
                continue
            fls = fw._safe_layer_name(prop["floor"])
            idx = fw.ensure_layer(doc, [fw.FW_ROOT, fls, "Reshores"],
                                  COL_RESHORE)
            brep = fw.prop_brep(prop, size_mu)
            if brep is None:
                continue
            if doc.Objects.AddBrep(
                    brep, _reshore_attr(doc, idx, prop, to_m)) != \
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
    log("added {0} objects under {1} (existing objects untouched)".format(
        n_added, fw.FW_ROOT))
    return n_added


def export_3dm(doc, result, path, log):
    """Write jump forms + reshores to a separate .3dm — document
    untouched."""
    to_m = fw.model_to_meters(doc)
    size_mu = result["params_mu"]["reshore_size"]
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
    for jf in result["jumpforms"]:
        fls = fw._safe_layer_name(jf["floor"])
        sub, col = _layer_for(jf)
        idx = add_layer([fw.FW_ROOT, fls, sub], col)
        f3.Objects.AddBrep(jf["brep"], _jf_attr(doc, idx, jf))
        n += 1
    for prop in result["reshores"]:
        if prop["status"] == "NOHIT_SKIPPED":
            continue
        fls = fw._safe_layer_name(prop["floor"])
        idx = add_layer([fw.FW_ROOT, fls, "Reshores"], COL_RESHORE)
        brep = fw.prop_brep(prop, size_mu)
        if brep is None:
            continue
        f3.Objects.AddBrep(brep, _reshore_attr(doc, idx, prop, to_m))
        n += 1
    ok = f3.Write(path, 7)
    log("export: {0} objects -> {1} ({2})".format(
        n, path, "ok" if ok else "WRITE FAILED"))
    return ok


def dump_json(doc, result, path, log):
    """Handoff for the IFC converter — metres, absolute world coords."""
    to_m = fw.model_to_meters(doc)
    mu = result["params_mu"]
    data = {"units": "m", "source_model": doc.Path or "",
            "reshore_size": mu["reshore_size"] * to_m,
            "banks": result["banks"],
            "jumpforms": [], "reshores": []}
    for jf in result["jumpforms"]:
        rec = {"kind": jf["kind"], "state": jf["state"],
               "floor": jf["floor"], "bank": jf["bank"],
               "z0": jf["z0"] * to_m, "z1": jf["z1"] * to_m,
               "lift_z0": jf["lift_z0"] * to_m,
               "lift_z1": jf["lift_z1"] * to_m,
               "wall_id": jf["wall_id"],
               "profile": [[x * to_m, y * to_m]
                           for x, y in jf["profile_mu"]]}
        data["jumpforms"].append(rec)
    for prop in result["reshores"]:
        if prop["status"] == "NOHIT_SKIPPED" or prop["foot_z"] is None:
            continue
        data["reshores"].append({
            "floor": prop["floor"], "pour": prop.get("pour"),
            "x": prop["x"] * to_m,
            "y": prop["y"] * to_m, "top_z": prop["top_z"] * to_m,
            "foot_z": prop["foot_z"] * to_m, "status": prop["status"],
            "slab_ids": prop.get("slab_ids") or []})
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
    log("jumpform_gen_rhino [{0}] — model units: {1}".format(
        p["mode"], doc.ModelUnitSystem))
    base = None
    if doc.Path:
        base = os.path.join(
            os.path.dirname(doc.Path),
            os.path.splitext(os.path.basename(doc.Path))[0])
    log_path = p["log_path"] or (
        (base or os.path.join(os.path.expanduser("~"), "jumpform"))
        + "_jumpform_log.txt")

    result = None
    if p["mode"] == "purge":
        fw.purge_formwork(doc, log)
    else:
        walls, slabs, obstacles = find_jumpform_inputs(doc, p, log)
        if not walls and not slabs:
            log("no core walls (keyword '{0}' + include {1}) and no "
                "reshore slabs — check layer names".format(
                    p["wall_layer_keyword"], p["wall_layer_include"]))
            log.save(log_path)
            return None
        if not walls:
            log("NOTE: no core walls matched (keyword '{0}' + include "
                "{1}) — reshores only".format(
                    p["wall_layer_keyword"], p["wall_layer_include"]))
        result = generate_jumpforms(walls, slabs, obstacles, doc, p, log)
        if p["mode"] == "generate":
            if doc.Layers.FindByFullPath(fw.FW_ROOT, -1) >= 0:
                log("existing {0} tree found — purging generated jump "
                    "forms/reshores before re-adding (platforms, props, "
                    "side forms left alone)".format(fw.FW_ROOT))
                fw.purge_formwork(doc, log, ("jumpform", "reshore"))
            write_to_doc(doc, result, p, log)
        elif p["mode"] == "export":
            path = p["export_path"] or (
                (base or os.path.join(os.path.expanduser("~"),
                                      "jumpform")) + "_jumpform.3dm")
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
        with io.open(os.path.join(STAGE, "jumpform_error.txt"), "w",
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
