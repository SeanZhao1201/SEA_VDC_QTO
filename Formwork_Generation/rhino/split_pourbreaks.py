#! python 2
# -*- coding: utf-8 -*-
"""Split structural deck slabs at authored pour breaks (schema v2).

Runs headless on a STAGED COPY of the model (never the original):
    Rhino.exe <staged copy>.3dm /nosplash
        /runscript="-_RunPythonScript <this file>"

Reads the pour-break JSON written by ``pourbreak_harvest.py`` (schema v2:
free-orientation plan polylines with jogs, text-dot pour markers); the
PDF-era schema v1 (axis-aligned ``dir``/``pos_ft``/``span_ft`` cuts) is
upconverted in memory, so historical break sets keep working. Splits
every matching slab crossed by its floor's breaks, tags pieces
``POUR<n>`` on suffixed layers with ``POUR`` / ``POUR_FLOOR`` /
``SOURCE_SLAB`` user strings, and ``WriteFile()``s a NEW derived .3dm —
the opened copy is never saved.

Pour numbering: a piece inherits the pour number of the text-dot marker
in its region — regions are side-key cells over the floor's break
polylines, so pieces of different slabs on the same side of a break share
one pour, exactly as a pour does on site. Cells without a marker fall
back to v1 binary semantics when the floor has exactly one marker
(everything else is pour 2), else to deterministic centroid-ordered
numbering with a warning to add dots.

Unit handling: coordinates are model units (the JSON records which; a
mismatch aborts — the model's unit system changed since harvest).
Constants are metres, converted at runtime — feet models are no longer
special-cased.

Paths: ``PB_JSON`` / ``PB_OUT3DM`` / ``PB_REPORT`` env vars, defaulting
to ``<doc folder>/<doc name>_pourbreaks.json`` / ``..._pourbreaks.3dm`` /
``..._pourbreak_report.json`` (the staged copy lives in the staging
folder, so defaults land there on staged runs).

The report is the engineer-facing review artifact: per-pour soffit area
and volume (plus CY on feet models) against the optional ``target_area``,
minimum distance from each break to the vertical supports below it
(construction joints belong near mid-span — flagged, never blocked),
axis-parallel grid offsets when a grid is present, and every reverted or
uncrossed slab with its reason.
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
from Rhino.Geometry import (AreaMassProperties, Brep, LoftType, Point3d,
                            PolylineCurve, VolumeMassProperties)

import formwork_gen_rhino as fw
from pourbreak_harvest import is_pb_layer

PARAMS = {
    "slab_layer_keyword": "slab",           # first '_' segment must contain
    # POUR-BREAK filter - deliberately WIDER than the formwork one.
    # A slab on grade has no soffit and no shoring, but it is very much a
    # real pour with real construction joints, so it belongs here (asked
    # for on the Bellwether podium 2026-08-20, where L01 is part suspended
    # / part on grade and P1 is SOG-only). Topping stays out: it is poured
    # on an existing deck and overlaps it in plan (measured on that model:
    # 6 of 8 toppings sit 100% inside a PT slab's footprint), so including
    # it would double-draw the cell and double-count the advisory areas.
    # formwork_gen_rhino / sideform_gen_rhino keep ["sog", "topping"] -
    # nothing shores a slab on grade. Do not re-merge the two lists.
    "slab_layer_exclude": ["topping"],
    # Columns inherit the pour zone they stand in (decided 2026-08-24):
    # first '_' segment must contain this to be tagged. Attributes only -
    # the column keeps its layer, its name and its geometry; the zone is
    # the POUR user string, mirrored into "QTO Properties" by the QTO
    # exporter exactly like the deck's. None/"" disables the pass.
    "column_layer_keyword": "column",
    "extensions_m": (5.0, 20.0, 50.0),      # progressive end extension
    "min_dir_m": 0.05,                      # end-direction sampling chord —
                                            # snap-noise micro segments must
                                            # not steer the extension
    "support_warn_m": 1.0,                  # break-to-support flag distance
    "support_band_m": 0.3,                  # support top within +/- of soffit
    "support_rise_m": 1.0,                  # support extends this far down
    "sliver_frac": 0.01,                    # smaller piece -> tangent cut
                                            # (kept if a pour dot sits in it —
                                            # authored small pours are legal)
    "volume_tol_frac": 0.01,                # pieces must sum to the original
}

_UNIT_ALIASES = {"ft": "feet", "foot": "feet", "feet": "feet",
                 "in": "inches", "inch": "inches", "inches": "inches",
                 "m": "meters", "meter": "meters", "meters": "meters",
                 "mm": "millimeters", "millimeter": "millimeters",
                 "millimeters": "millimeters",
                 "cm": "centimeters", "centimeter": "centimeters",
                 "centimeters": "centimeters"}


def units_match(json_units, doc, log):
    ju = _UNIT_ALIASES.get((json_units or "").strip().lower(),
                           (json_units or "").strip().lower())
    du = str(doc.ModelUnitSystem).lower()
    if not ju:
        log("WARNING: breaks JSON has no units field — assuming it matches "
            "the model ({0})".format(doc.ModelUnitSystem))
        return True
    if ju != du:
        log("ERROR: breaks JSON is in '{0}' but the model is {1} — "
            "coordinates would be wrong. Re-harvest from this model."
            .format(json_units, doc.ModelUnitSystem))
        return False
    return True


def upconvert_v1(data, log=None):
    """Schema v1 (PDF-era axis-aligned cuts) -> schema v2 in memory.

    A v1 cut is a two-point polyline; ``pour1_centroid_ft`` becomes a
    pour-1 marker; ``pdf_sf`` becomes ``target_area``. ``z`` is null (v1
    had no curve elevation) — consumers resolve it from the floor.
    """
    if data.get("version", 1) >= 2:
        return data
    floors = {}
    for fl, cfg in data.get("floors", {}).items():
        breaks = []
        for i, cut in enumerate(cfg.get("cuts", [])):
            a, b = cut["span_ft"]
            pos = cut["pos_ft"]
            if cut["dir"] == "NS":
                poly = [[pos, a], [pos, b]]
            else:
                poly = [[a, pos], [b, pos]]
            breaks.append({"id": "{0}-PB{1}".format(fl, i + 1),
                           "polyline": poly, "z": None,
                           "curve_type": "polyline", "binding": "floor-key",
                           "provenance": "v1 cut",
                           "note": cut.get("note", "")})
        markers = []
        cen = cfg.get("pour1_centroid_ft")
        if cen:
            markers.append({"pour": 1, "at": [cen[0], cen[1]], "z": None,
                            "provenance": "v1 pour1_centroid"})
        floors[fl] = {"breaks": breaks, "pour_markers": markers,
                      "target_area": cfg.get("pdf_sf")}
    out = {"version": 2, "units": data.get("units", ""),
           "source": {"kind": "v1-upconvert"}, "floors": floors}
    for key in ("grid_x", "grid_y"):
        if key in data:
            out[key] = data[key]
    if log:
        log("upconverted v1 breaks JSON: {0} floors".format(len(floors)))
    return out


# ── cut geometry ───────────────────────────────────────────────────────────
def _end_direction(ded, tip_index, min_dir):
    """Unit outward direction at an end of the polyline, sampled over at
    least ``min_dir`` of chord — a snap-noise micro segment at the tip must
    not steer the 5–50 m safety extension into a garbage direction."""
    tip = ded[tip_index]
    step = -1 if tip_index == 0 else 1
    inner = None
    i = tip_index
    while 0 <= i + step * -1 < len(ded):
        i += step * -1
        inner = ded[i]
        if math.hypot(tip[0] - inner[0], tip[1] - inner[1]) >= min_dir:
            break
    d = math.hypot(tip[0] - inner[0], tip[1] - inner[1])
    if d <= 0:
        return None
    return ((tip[0] - inner[0]) / d, (tip[1] - inner[1]) / d)


def extend_polyline_xy(xy, ext, min_dir=0.0):
    """[[x, y]...] with both END segments extended outward by ``ext``.

    Under-drawn break lines still sever their slab; interior jog vertices
    are untouched. Consecutive duplicate points are dropped, and the
    extension direction is sampled over ``min_dir`` of chord length so a
    micro end segment cannot steer it. Returns None for degenerate input.
    """
    pts = [(float(p[0]), float(p[1])) for p in xy]
    ded = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - ded[-1][0], p[1] - ded[-1][1]) > 1e-9:
            ded.append(p)
    if len(ded) < 2:
        return None
    hd = _end_direction(ded, 0, min_dir)
    td = _end_direction(ded, len(ded) - 1, min_dir)
    if hd is None or td is None:
        return None
    head = (ded[0][0] + hd[0] * ext, ded[0][1] + hd[1] * ext)
    tail = (ded[-1][0] + td[0] * ext, ded[-1][1] + td[1] * ext)
    return [head] + ded + [tail]


def cutter_brep(brk, ext, z0, z1, min_dir=0.0):
    """Vertical cutting Brep: the extended break polyline lofted z0->z1."""
    xy = extend_polyline_xy(brk["polyline"], ext, min_dir)
    if xy is None:
        return None
    c0 = PolylineCurve([Point3d(x, y, z0) for x, y in xy])
    c1 = PolylineCurve([Point3d(x, y, z1) for x, y in xy])
    lofts = Brep.CreateFromLoft([c0, c1], Point3d.Unset, Point3d.Unset,
                                LoftType.Straight, False)
    if lofts and len(lofts) == 1:
        return lofts[0]
    if lofts and len(lofts) > 1:
        joined = Brep.JoinBreps(list(lofts), 1e-6)
        if joined and len(joined) == 1:
            return joined[0]
        return lofts[0]
    ext_geo = Rhino.Geometry.Extrusion.Create(c0, z1 - z0, False)
    if ext_geo is not None:
        return ext_geo.ToBrep()
    return None


def side_of(xy, px, py):
    """Which side of the break polyline the plan point lies on (bool).

    Sign of the cross product against the nearest segment — the polyline
    generalization of v1's single-coordinate comparison; stable for jogs.
    """
    best = None
    for i in range(len(xy) - 1):
        ax, ay = float(xy[i][0]), float(xy[i][1])
        bx, by = float(xy[i + 1][0]), float(xy[i + 1][1])
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 <= 0:
            continue
        t = ((px - ax) * dx + (py - ay) * dy) / seg2
        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
        cx, cy = ax + t * dx, ay + t * dy
        d2 = (px - cx) ** 2 + (py - cy) ** 2
        if best is None or d2 < best[0]:
            best = (d2, dx * (py - ay) - dy * (px - ax))
    return best[1] > 0 if best else True


def side_key(breaks, px, py):
    return tuple(side_of(b["polyline"], px, py) for b in breaks)


def point_in_piece(piece, px, py, tol):
    """Is the plan point inside the solid piece (tested at bbox mid-Z)?"""
    bb = piece.GetBoundingBox(True)
    pt = Point3d(px, py, (bb.Min.Z + bb.Max.Z) / 2.0)
    try:
        return piece.IsPointInside(pt, tol, False)
    except Exception:
        return False


def marker_claim(brep, markers, tol):
    """(pour, all_pours) of the dot(s) sitting inside an UNCUT slab.

    The closure strip on every Bellwether floor is a separate small slab
    that no break line crosses, so it reached the derived model with no
    POUR at all - and a downstream 4D binder that matches on property
    EQUALITY cannot select "blank" (Mast4D, 2026-08-20). Rather than
    invent a token in the exporter, the MODELLER declares it: drop a
    numbered dot on that slab in the break sheet and it is claimed here,
    exactly like a split piece. Lowest number wins when a slab holds
    several; the caller says so out loud.
    """
    hits = []
    for mk in markers or []:
        if mk.get("pour") is None:
            continue
        try:
            mx, my = float(mk["at"][0]), float(mk["at"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if point_in_piece(brep, mx, my, tol):
            hits.append(mk["pour"])
    if not hits:
        return None, []
    return min(hits), sorted(set(hits))


def interior_plan_point(piece, tol):
    """A plan point guaranteed inside the piece.

    A concave piece's volume centroid can fall OUTSIDE it (and across the
    break, which mislabels its pour) — so verify the centroid and fall
    back to sampling the largest soffit face for an interior point.
    """
    vm = VolumeMassProperties.Compute(piece)
    if vm is not None and point_in_piece(piece, vm.Centroid.X,
                                         vm.Centroid.Y, tol):
        return (vm.Centroid.X, vm.Centroid.Y)
    cos20 = math.cos(math.radians(20.0))
    best = None
    for face, _z in fw.soffit_faces(piece, cos20):
        amp = AreaMassProperties.Compute(face)
        if amp is None:
            continue
        if best is None or amp.Area > best[1]:
            best = (face, amp.Area)
    if best is not None:
        face = best[0]
        du, dv = face.Domain(0), face.Domain(1)
        for n in (3, 7, 13):            # coarse-to-fine domain lattice
            for i in range(1, n):
                for j in range(1, n):
                    u = du.ParameterAt(i / float(n))
                    v = dv.ParameterAt(j / float(n))
                    if face.IsPointOnFace(u, v) == \
                            Rhino.Geometry.PointFaceRelation.Interior:
                        p = face.PointAt(u, v)
                        return (p.X, p.Y)
    if vm is not None:                  # last resort: unverified centroid
        return (vm.Centroid.X, vm.Centroid.Y)
    c = piece.GetBoundingBox(True).Center
    return (c.X, c.Y)


# ── splitting (tolerance ladder + boolean fallback + guards, from v1) ──────
def split_with_breaks(brep, breaks, markers, ext_ladder, params, tol, log,
                      why, min_dir=0.0):
    """Apply every break; returns (pieces, ok). Progressive extension per
    cut, CreateBooleanSplit fallback when capping fails, sliver guard
    against tangent cuts (waived when a pour dot sits inside the small
    piece — an authored small pour is legal), volume-conservation guard."""
    bb = brep.GetBoundingBox(True)
    pieces = [brep]
    crossed = False
    for ci, brk in enumerate(breaks):
        nxt = []
        for pc in pieces:
            done = None
            for ext in ext_ladder:
                cutter = cutter_brep(brk, ext, bb.Min.Z - 2, bb.Max.Z + 2,
                                     min_dir)
                if cutter is None:
                    why.append("{0}: no cutter".format(brk["id"]))
                    continue
                # doc tolerance can be absurdly tight (1e-5 ft on the
                # Bellwether model); Split needs slack to close sections.
                for mult in (1.0, 10.0, 100.0):
                    t = tol * mult
                    parts = pc.Split(cutter, t)
                    if parts and len(parts) >= 2:
                        capped = [part.CapPlanarHoles(t) for part in parts]
                        if all(c is not None and c.IsSolid for c in capped):
                            done = capped
                            if mult > 1:
                                why.append("{0}: split@tol*{1:g}".format(
                                    brk["id"], mult))
                            break
                    try:
                        bs = Brep.CreateBooleanSplit(pc, cutter, t)
                    except Exception:
                        bs = None
                    if bs and len(bs) >= 2 and all(b.IsSolid for b in bs):
                        done = list(bs)
                        why.append("{0}: boolean@tol*{1:g}".format(
                            brk["id"], mult))
                        break
                    why.append("{0}: {2}@{1:g}/x{3:g}".format(
                        brk["id"], ext, "cap-fail" if parts and
                        len(parts) >= 2 else "no-int", mult))
                if done is not None:
                    break
            if done is not None:
                nxt.extend(done)
                crossed = True
            else:
                nxt.append(pc)
        pieces = nxt
    if not crossed:
        return [brep], False
    vol0 = VolumeMassProperties.Compute(brep)
    vols = [VolumeMassProperties.Compute(p) for p in pieces]
    if vol0 and all(vols):
        total = sum(v.Volume for v in vols)
        rel = abs(total - vol0.Volume) / vol0.Volume
        if rel > params["volume_tol_frac"]:
            why.append("VOLUME MISMATCH {0:.4f}".format(rel))
            log("    VOLUME MISMATCH {0:.2%} — keeping slab uncut".format(
                rel))
            return [brep], False
        i_min = min(range(len(vols)), key=lambda i: vols[i].Volume)
        if vols[i_min].Volume < params["sliver_frac"] * vol0.Volume:
            authored = any(point_in_piece(pieces[i_min],
                                          float(mk["at"][0]),
                                          float(mk["at"][1]), tol * 10)
                           for mk in (markers or []))
            if authored:
                why.append("sub-{0:.0%} piece kept — pour dot inside "
                           "(authored small pour)".format(
                               params["sliver_frac"]))
            else:
                why.append("tangent cut (sliver piece) — reverted")
                return [brep], False
    return pieces, True


# ── pour assignment (dot containment first, side-key cells as fallback) ────
def assign_floor_pours(floor, pieces, breaks, markers, log):
    """Assign a pour number to every piece dict of one floor (in place).

    Exact first: a piece that CONTAINS a pour dot takes that dot's number
    — correct for any break shape (concave pieces whose centroid escapes
    them, notches routed around openings, re-entrant polylines crossing a
    slab twice). Only pieces without a dot fall back to side-key cells,
    keyed by a guaranteed-interior point: a cell holding a marker inherits
    its number; with exactly one marker everything else is pour 2 (v1
    binary semantics — the golden regression depends on it); otherwise
    unmatched cells get deterministic unused numbers. Every fallback is
    summarized in the log — nothing is silent.
    """
    for pc in pieces:
        inside = [mk for mk in markers
                  if point_in_piece(pc["brep"], float(mk["at"][0]),
                                    float(mk["at"][1]), pc["tol"])]
        if len(inside) > 1:
            log("  WARNING: {0}: one piece contains {1} pour dots — "
                "keeping {2}".format(floor, len(inside),
                                     min(mk["pour"] for mk in inside)))
        if inside:
            pc["pour"] = min(mk["pour"] for mk in inside)
            pc["how"] = "dot"
    unassigned = [pc for pc in pieces if pc.get("pour") is None]
    if not unassigned:
        return
    marker_cells = {}
    for mk in markers:
        k = side_key(breaks, float(mk["at"][0]), float(mk["at"][1]))
        if k not in marker_cells or mk["pour"] < marker_cells[k]:
            marker_cells[k] = mk["pour"]
    cells = {}
    for pc in unassigned:
        k = side_key(breaks, pc["rep"][0], pc["rep"][1])
        pc["key"] = k
        if k not in cells or pc["rep"] < cells[k]:
            cells[k] = pc["rep"]
    assigned = {}
    for k in cells:
        if k in marker_cells:
            assigned[k] = marker_cells[k]
    leftover = sorted([k for k in cells if k not in assigned],
                      key=lambda k: cells[k])
    if leftover:
        if len(markers) == 1:
            other = 2 if markers[0]["pour"] == 1 else 1
            for k in leftover:
                assigned[k] = other
            log("  note: {0}: {1} region(s) without a dot -> pour {2} "
                "(single-marker binary rule)".format(
                    floor, len(leftover), other))
        else:
            used = set(mk["pour"] for mk in markers)
            used.update(assigned.values())
            nxt = 1
            for k in leftover:
                while nxt in used:
                    nxt += 1
                assigned[k] = nxt
                used.add(nxt)
            if markers:
                log("  WARNING: {0}: {1} region(s) have no dot — "
                    "auto-numbered; add dots to author the "
                    "sequence".format(floor, len(leftover)))
    n_side = n_auto = 0
    for pc in unassigned:
        pc["pour"] = assigned[pc["key"]]
        if pc["key"] in marker_cells:
            pc["how"] = "side"
            n_side += 1
        else:
            pc["how"] = "auto"
            n_auto += 1
    n_dot = sum(1 for pc in pieces if pc.get("how") == "dot")
    log("  {0}: pours — {1} by dot, {2} by side cell, {3} auto".format(
        floor, n_dot, n_side, n_auto))


def cy_divisor(unit_system):
    """Cubic-yard divisor for imperial models — QTO converts both ft and
    in models to CY, so the report must too. None for metric."""
    if unit_system == Rhino.UnitSystem.Feet:
        return 27.0
    if unit_system == Rhino.UnitSystem.Inches:
        return 46656.0
    return None


def force_delete(doc, obj, log):
    """Delete even when the object is hidden, locked, or on a locked
    layer chain — the staged copy preserves client object state, and a
    silently failed delete would leave the original slab duplicated
    beside its pieces in the derived model. Returns success."""
    if doc.Objects.Delete(obj, True):
        return True
    try:
        layer = doc.Layers[obj.Attributes.LayerIndex]
        while layer is not None:
            if layer.IsLocked:
                layer.IsLocked = False
            pid = layer.ParentLayerId
            layer = doc.Layers.FindId(pid) \
                if pid != System.Guid.Empty else None
    except Exception:
        pass
    try:
        doc.Objects.Show(obj.Id, True)
    except Exception:
        pass
    try:
        if obj.Attributes.Mode != Rhino.DocObjects.ObjectMode.Normal:
            obj.Attributes.Mode = Rhino.DocObjects.ObjectMode.Normal
            obj.CommitChanges()
    except Exception:
        pass
    return doc.Objects.Delete(obj, True)


# ── review metrics ─────────────────────────────────────────────────────────
def soffit_area(brep, tol):
    cos20 = math.cos(math.radians(20.0))
    area = 0.0
    for face, _z in fw.soffit_faces(brep, cos20):
        amp = AreaMassProperties.Compute(face)
        if amp:
            area += amp.Area
    return area


def collect_support_boxes(doc, log):
    """Bounding boxes of every solid that could bear under a slab edge.
    Skips _POURBREAK and _FORMWORK layers. Read-only."""
    boxes = []
    for obj in doc.Objects:
        if obj is None or obj.Attributes is None:
            continue
        li = obj.Attributes.LayerIndex
        if is_pb_layer(doc, li) or fw._is_formwork_layer(doc, li):
            continue
        geom = obj.Geometry
        if isinstance(geom, Rhino.Geometry.Extrusion):
            geom = geom.ToBrep(True)
        if isinstance(geom, Brep) and geom.IsSolid:
            boxes.append(geom.GetBoundingBox(True))
    log("support scan: {0} candidate solids".format(len(boxes)))
    return boxes


def _dist_xy_to_box(px, py, bb):
    dx = max(bb.Min.X - px, 0.0, px - bb.Max.X)
    dy = max(bb.Min.Y - py, 0.0, py - bb.Max.Y)
    return math.hypot(dx, dy)


def break_sample_points(brk):
    """Vertices + segment midpoints of the raw (unextended) polyline."""
    xy = [(float(p[0]), float(p[1])) for p in brk["polyline"]]
    pts = list(xy)
    for i in range(len(xy) - 1):
        pts.append(((xy[i][0] + xy[i + 1][0]) / 2.0,
                    (xy[i][1] + xy[i + 1][1]) / 2.0))
    return pts


def min_support_distance(brk, soffit_zs, boxes, band, rise):
    """Approximate min plan distance from the break to a vertical support
    whose top reaches ANY of the floor's slab undersides — stepped floors
    bucket several soffit elevations under one name, and checking only
    the lowest would miss supports under the higher slabs (bbox-level
    accuracy — a review warning, not an engineering check)."""
    best = None
    for bb in boxes:
        near = False
        for sz in set(soffit_zs):
            if bb.Max.Z < sz - band or bb.Max.Z > sz + band:
                continue
            if bb.Min.Z > sz - rise:
                continue
            near = True
            break
        if not near:
            continue
        for px, py in break_sample_points(brk):
            d = _dist_xy_to_box(px, py, bb)
            if best is None or d < best:
                best = d
    return best


def grid_offsets(brk, grid_x, grid_y):
    """Nearest-grid offset for each axis-parallel segment (informational)."""
    out = []
    xy = [(float(p[0]), float(p[1])) for p in brk["polyline"]]
    for i in range(len(xy) - 1):
        (ax, ay), (bx, by) = xy[i], xy[i + 1]
        seg_len = math.hypot(bx - ax, by - ay)
        if seg_len <= 0:
            continue
        if abs(bx - ax) < 0.01 * seg_len and grid_x:
            pos = (ax + bx) / 2.0
            name = min(grid_x, key=lambda g: abs(grid_x[g] - pos))
            out.append({"segment": i, "axis": "x", "grid": name,
                        "offset": round(pos - grid_x[name], 3)})
        elif abs(by - ay) < 0.01 * seg_len and grid_y:
            pos = (ay + by) / 2.0
            name = min(grid_y, key=lambda g: abs(grid_y[g] - pos))
            out.append({"segment": i, "axis": "y", "grid": name,
                        "offset": round(pos - grid_y[name], 3)})
    return out


# ── main pass ──────────────────────────────────────────────────────────────
def split_document(doc, data, params=None, log=None):
    """Split every matching slab at its floor's breaks. Returns the report
    dict, or None on a hard precondition failure. Mutates the document
    (adds pieces, deletes split originals) — run it on a staged copy."""
    log = log or fw.Log()
    p = dict(PARAMS)
    if params:
        p.update(params)
    if not units_match(data.get("units"), doc, log):
        return None
    floor_elev = fw.read_floor_elevations(doc)
    if not floor_elev:
        log("ERROR: no FloorElevations doc string — aborting")
        return None
    names = list(floor_elev.values())
    for n in set(names):
        if names.count(n) > 1:
            log("WARNING: duplicate floor name '{0}' ({1} elevations) — "
                "breaks bucket by NAME, so they will apply to every "
                "same-named level".format(n, names.count(n)))
    tol = doc.ModelAbsoluteTolerance
    to_mu = fw.meters_to_model(doc)
    ext_ladder = [e * to_mu for e in p["extensions_m"]]
    min_dir = p["min_dir_m"] * to_mu
    band = p["support_band_m"] * to_mu
    rise = p["support_rise_m"] * to_mu
    warn_d = p["support_warn_m"] * to_mu
    cy_div = cy_divisor(doc.ModelUnitSystem)

    def floor_of(z):
        return floor_elev[min(floor_elev, key=lambda e: abs(e - z))]

    keyword = p["slab_layer_keyword"].lower()
    excludes = p.get("slab_layer_exclude") or []
    targets = []
    for obj in doc.Objects:
        if obj is None or obj.Attributes is None:
            continue
        li = obj.Attributes.LayerIndex
        if is_pb_layer(doc, li) or fw._is_formwork_layer(doc, li):
            continue
        layer = doc.Layers[li]
        lname = layer.Name if layer is not None else ""
        first = lname.split("_")[0].lower()
        if keyword not in first:
            continue
        if any(kw and kw.lower() in lname.lower() for kw in excludes):
            continue
        geom = obj.Geometry
        if isinstance(geom, Rhino.Geometry.Extrusion):
            geom = geom.ToBrep(True)
        if isinstance(geom, Brep):
            targets.append((obj, geom, layer))
    log("targets: {0} structural deck slabs (keyword '{1}', excludes {2})"
        .format(len(targets), p["slab_layer_keyword"], excludes))

    floors_cfg = data.get("floors", {})
    support_boxes = collect_support_boxes(doc, log)
    grid_x = data.get("grid_x") or {}
    grid_y = data.get("grid_y") or {}

    report = {"version": 2, "units": str(doc.ModelUnitSystem),
              "params": {"extensions_m": list(p["extensions_m"]),
                         "support_warn_m": p["support_warn_m"]},
              "floors": {}}

    def floor_report(fl):
        cfg = floors_cfg.get(fl) or {}
        if fl not in report["floors"]:
            report["floors"][fl] = {
                "breaks": [{"id": b["id"], "curve_type": b["curve_type"],
                            "note": b.get("note", "")}
                           for b in cfg.get("breaks", [])],
                "target_area": cfg.get("target_area"),
                "total_soffit_area": 0.0,
                "pours": {}, "slabs": []}
        return report["floors"][fl]

    # pass 1 — split everything, remember pieces; no doc mutation yet
    pending = []        # (obj, layer, fl, srec, [piece dicts])
    claims = []         # (obj, fl, pour, all_pours, srec) - uncut, dot-claimed
    floor_pieces = {}   # fl -> every piece dict on that floor
    floor_soffits = {}  # fl -> soffit elevations seen (support review)
    for obj, brep, layer in targets:
        bb = brep.GetBoundingBox(True)
        fl = floor_of(bb.Min.Z)
        cfg = floors_cfg.get(fl)
        frep = floor_report(fl)
        v0 = VolumeMassProperties.Compute(brep)
        srec = {"layer": layer.Name, "source_id": str(obj.Id),
                "orig_vol": round(v0.Volume, 1) if v0 else None,
                "pieces": []}
        frep["slabs"].append(srec)
        a0 = soffit_area(brep, tol)
        frep["total_soffit_area"] = round(
            frep["total_soffit_area"] + a0, 1)
        floor_soffits.setdefault(fl, []).append(bb.Min.Z)
        breaks = (cfg or {}).get("breaks") or []
        markers = (cfg or {}).get("pour_markers") or []
        if not breaks:
            srec["status"] = "no break for floor"
            claimed, all_hits = marker_claim(brep, markers, tol)
            if claimed is not None:
                claims.append((obj, fl, claimed, all_hits, srec))
            continue
        why = []
        pieces, ok = split_with_breaks(brep, breaks, markers, ext_ladder,
                                       p, tol, log, why, min_dir)
        srec["why"] = why
        if not ok:
            srec["status"] = "not crossed"
            claimed, all_hits = marker_claim(brep, markers, tol)
            if claimed is not None:
                claims.append((obj, fl, claimed, all_hits, srec))
            continue
        srec["status"] = "split into {0}".format(len(pieces))
        rec_pieces = []
        for pc in pieces:
            vm = VolumeMassProperties.Compute(pc)
            rec = {"brep": pc, "vm": vm, "tol": tol,
                   "rep": interior_plan_point(pc, tol), "pour": None}
            rec_pieces.append(rec)
            floor_pieces.setdefault(fl, []).append(rec)
        # the source brep and its soffit area ride along: pass 3 deletes
        # the original FIRST, so a failed piece-add needs the brep back to
        # roll the slab uncut - and if even that fails, a0 comes off the
        # floor total so the report cannot count vanished volume
        pending.append((obj, layer, fl, srec, rec_pieces, brep, a0))

    # pass 2 — per-floor pour assignment (dot containment, then cells)
    for fl, pieces in floor_pieces.items():
        cfg = floors_cfg.get(fl) or {}
        assign_floor_pours(fl, pieces, cfg.get("breaks") or [],
                           cfg.get("pour_markers") or [], log)

    # pass 3 — mutate the (staged) document
    def pour_layer(orig_layer, pour):
        name = orig_layer.Name + "_POUR{0}".format(pour)
        for l in doc.Layers:
            if l is not None and not l.IsDeleted and l.Name == name \
                    and l.ParentLayerId == orig_layer.ParentLayerId:
                return l.Index
        layer = Rhino.DocObjects.Layer()
        layer.Name = name
        layer.Color = orig_layer.Color
        layer.ParentLayerId = orig_layer.ParentLayerId
        return doc.Layers.Add(layer)

    n_cut = 0
    for obj, layer, fl, srec, rec_pieces, src_brep, src_soffit in pending:
        frep = report["floors"][fl]
        # capture identity/attributes, then delete FIRST — a failed
        # delete (locked object/layer in the client model, preserved by
        # staging) must not leave the original duplicated beside pieces
        base_attr = obj.Attributes.Duplicate()
        source_id = str(obj.Id)
        if not force_delete(doc, obj, log):
            srec["status"] = "SPLIT ABORTED — original not deletable"
            srec["delete_failed"] = True
            log("  ERROR: {0}/{1}: original slab could not be deleted — "
                "pieces NOT added, slab left uncut".format(fl, layer.Name))
            continue

        # Add every piece FIRST, checking each Guid: the original is
        # already gone, so a piece AddBrep can silently return Guid.Empty
        # and the derived model would lose that volume while the report
        # (written from the in-memory pieces) still claimed a clean,
        # conserving split. On any failure, roll the whole slab back.
        added_ids = []
        add_failed = False
        for rec in rec_pieces:
            pour = rec["pour"] if rec["pour"] is not None else 0
            attr = base_attr.Duplicate()
            attr.LayerIndex = pour_layer(layer, pour)
            attr.Mode = Rhino.DocObjects.ObjectMode.Normal
            attr.SetUserString("POUR", str(pour))
            attr.SetUserString("POUR_FLOOR", fl)
            attr.SetUserString("SOURCE_SLAB", source_id)
            # the parent's checkup-surviving stable id must NOT ride onto the
            # pieces: sibling pieces would collide on it and the take-off IFC
            # could not tell them apart; each piece gets its OWN id stamped
            # right after the add below (SOURCE_SLAB keeps the parentage)
            attr.SetUserString("QTO_STABLE_ID", None)
            new_id = doc.Objects.AddBrep(rec["brep"], attr)
            if new_id == System.Guid.Empty:
                add_failed = True
                break
            added_ids.append(new_id)
        if add_failed:
            for aid in added_ids:
                doc.Objects.Delete(aid, True)
            restored = doc.Objects.AddBrep(src_brep, base_attr)
            srec["add_failed"] = True
            srec["pieces"] = []
            if restored == System.Guid.Empty:
                # the report is the engineer-facing truth: a slab that is
                # GONE from the derived model must say so there, not only
                # in a transient log line - and its soffit must come off
                # the floor total or target_ratio counts vanished volume
                srec["status"] = ("PIECE ADD FAILED — restore ALSO "
                                  "failed, slab MISSING from derived "
                                  "model")
                srec["restore_failed"] = True
                frep["total_soffit_area"] = round(
                    frep["total_soffit_area"] - src_soffit, 1)
            else:
                srec["status"] = "PIECE ADD FAILED — slab restored uncut"
            log("  ERROR: {0}/{1}: a split piece could not be added to the "
                "derived model — pieces rolled back, original slab "
                "re-added {2}".format(
                    fl, layer.Name,
                    "OK" if restored != System.Guid.Empty else
                    "FAILED (slab MISSING — do NOT use this derived "
                    "model)"))
            continue

        # Stamp each piece with its own id as the checkup-surviving identity:
        # the formwork generator and the QTO IFC export both derive the
        # cross-export id from QTO_STABLE_ID, and the take-off session's
        # checkup preserves an existing stamp instead of re-minting. A failed
        # stamp is harmless - with no stamp, both sides fall back to the same
        # object id - so it only warns.
        for aid in added_ids:
            pobj = doc.Objects.FindId(aid)
            if pobj is None:
                continue
            pattr = pobj.Attributes.Duplicate()
            pattr.SetUserString("QTO_STABLE_ID", str(aid))
            if not doc.Objects.ModifyAttributes(pobj, pattr, True):
                log("  WARNING: {0}/{1}: piece {2} could not be stamped with "
                    "QTO_STABLE_ID; the take-off export falls back to the "
                    "same object id, links still resolve".format(
                        fl, layer.Name, aid))

        # bookkeeping only after every piece verifiably landed
        for rec in rec_pieces:
            pour = rec["pour"] if rec["pour"] is not None else 0
            vm = rec["vm"]
            a = soffit_area(rec["brep"], tol)
            prec = {"pour": pour,
                    "vol": round(vm.Volume, 1) if vm else None,
                    "area": round(a, 1),
                    "centroid": [round(rec["rep"][0], 2),
                                 round(rec["rep"][1], 2)],
                    "assigned_by": rec.get("how", "auto")}
            if cy_div and vm:
                prec["vol_cy"] = round(vm.Volume / cy_div, 1)
            srec["pieces"].append(prec)
            tot = frep["pours"].setdefault(
                str(pour), {"area": 0.0, "vol": 0.0})
            tot["area"] = round(tot["area"] + a, 1)
            if vm:
                tot["vol"] = round(tot["vol"] + vm.Volume, 1)
        n_cut += 1
    log("split {0} slabs".format(n_cut))

    # Uncut slabs claimed by a pour dot: ATTRIBUTES ONLY, deliberately.
    # The slab was never cut, so it is not deleted, not re-added and not
    # moved to a _POUR layer - and srec["status"] keeps saying "not
    # crossed". Only srec["claimed_pour"] and the POUR user string are
    # added, which is exactly what the formwork generator and the IFC
    # exporters read.
    n_claimed = 0
    for obj, fl, pour, all_hits, srec in claims:
        if len(all_hits) > 1:
            log("  NOTE: {0}/{1}: uncut slab holds dots for pours {2} - "
                "claiming the lowest ({3}); split it if they are meant to "
                "be separate pours".format(
                    fl, srec["layer"],
                    ", ".join(str(h) for h in all_hits), pour))
        attr = obj.Attributes.Duplicate()
        attr.SetUserString("POUR", str(pour))
        attr.SetUserString("POUR_FLOOR", fl)
        if doc.Objects.ModifyAttributes(obj, attr, True):
            srec["claimed_pour"] = pour
            n_claimed += 1
        else:
            log("  WARNING: {0}/{1}: could not tag the uncut slab with "
                "POUR {2} - it stays untagged".format(
                    fl, srec["layer"], pour))
    if n_claimed:
        log("{0} uncut slab(s) claimed by a pour dot".format(n_claimed))

    # pass 4 - columns inherit the pour zone they stand in (decided
    # 2026-08-24). ATTRIBUTES ONLY: the column is never cut, never moved
    # off its layer, and its name stays generic - the zone lives in the
    # POUR user string, which the QTO exporter mirrors into
    # "QTO Properties" the same way it does for the deck. Assignment is
    # plan-centroid containment against this floor's POUR-tagged deck
    # solids (split pieces AND dot-claimed uncut slabs, read back from
    # the document so both are treated identically); a column riding the
    # break line goes to whichever side holds its centroid, and one
    # outside every deck footprint (an edge column) takes the NEAREST
    # deck's pour - counted separately in the report. NOTE the schedule
    # pours all columns of a floor in one activity and Mast4D asked for
    # no column tags: this tag serves per-zone quantity rollups and a
    # future zoned schedule, and is harmless to FLOOR+name binding.
    col_kw = (p.get("column_layer_keyword") or "").lower()
    n_col_tagged = n_col_untagged = 0
    if col_kw:
        deck_by_floor = {}
        for obj in doc.Objects:
            if obj is None or obj.Attributes is None:
                continue
            li = obj.Attributes.LayerIndex
            if is_pb_layer(doc, li) or fw._is_formwork_layer(doc, li):
                continue
            layer = doc.Layers[li]
            lname = layer.Name if layer is not None else ""
            if keyword not in lname.split("_")[0].lower():
                continue
            if any(kw and kw.lower() in lname.lower() for kw in excludes):
                continue
            pour = obj.Attributes.GetUserString("POUR")
            pfl = obj.Attributes.GetUserString("POUR_FLOOR")
            if not pour or pour == "0" or not pfl:
                continue
            geom = obj.Geometry
            if isinstance(geom, Rhino.Geometry.Extrusion):
                geom = geom.ToBrep(True)
            if isinstance(geom, Brep):
                deck_by_floor.setdefault(pfl, []).append((geom, pour))

        def _col_rep(fl):
            return floor_report(fl).setdefault(
                "columns", {"tagged": 0, "nearest": 0, "untagged": 0,
                            "by_pour": {}})

        for obj in list(doc.Objects):
            if obj is None or obj.Attributes is None:
                continue
            li = obj.Attributes.LayerIndex
            if is_pb_layer(doc, li) or fw._is_formwork_layer(doc, li):
                continue
            layer = doc.Layers[li]
            lname = layer.Name if layer is not None else ""
            if col_kw not in lname.split("_")[0].lower():
                continue
            geom = obj.Geometry
            if isinstance(geom, Rhino.Geometry.Extrusion):
                geom = geom.ToBrep(True)
            if not isinstance(geom, Brep):
                continue
            bb = geom.GetBoundingBox(True)
            fl = floor_of(bb.Min.Z)
            decks = deck_by_floor.get(fl)
            if not decks:
                # a floor with no tagged deck (no breaks, no claims -
                # e.g. the single-pour P1) leaves its columns untagged
                n_col_untagged += 1
                _col_rep(fl)["untagged"] += 1
                continue
            vm = VolumeMassProperties.Compute(geom)
            cx = vm.Centroid.X if vm else (bb.Min.X + bb.Max.X) / 2.0
            cy = vm.Centroid.Y if vm else (bb.Min.Y + bb.Max.Y) / 2.0
            hits = sorted(set(pr for g, pr in decks
                              if point_in_piece(g, cx, cy, tol)))
            how = "inside"
            if hits:
                pour = hits[0]
            else:
                pour = min(decks, key=lambda gp: _dist_xy_to_box(
                    cx, cy, gp[0].GetBoundingBox(True)))[1]
                how = "nearest"
            attr = obj.Attributes.Duplicate()
            attr.SetUserString("POUR", str(pour))
            attr.SetUserString("POUR_FLOOR", fl)
            if doc.Objects.ModifyAttributes(obj, attr, True):
                n_col_tagged += 1
                crep = _col_rep(fl)
                crep["tagged"] += 1
                if how == "nearest":
                    crep["nearest"] += 1
                crep["by_pour"][str(pour)] = \
                    crep["by_pour"].get(str(pour), 0) + 1
            else:
                log("  WARNING: {0}: column could not be tagged with "
                    "POUR {1}".format(fl, pour))
    if n_col_tagged or n_col_untagged:
        log("{0} column(s) tagged with their pour zone, {1} left "
            "untagged (no zoned deck on their floor)".format(
                n_col_tagged, n_col_untagged))

    # review metrics per floor
    for fl, frep in report["floors"].items():
        cfg = floors_cfg.get(fl) or {}
        soffit_zs = floor_soffits.get(fl)
        for brec, brk in zip(frep["breaks"], cfg.get("breaks") or []):
            if soffit_zs:
                d = min_support_distance(brk, soffit_zs, support_boxes,
                                         band, rise)
                if d is not None:
                    brec["min_support_dist"] = round(d, 2)
                    brec["support_flag"] = bool(d < warn_d)
                    if brec["support_flag"]:
                        log("  FLAG: {0} {1} runs {2:.2f} units from a "
                            "vertical support — joints belong near "
                            "mid-span".format(fl, brk["id"], d))
            if brk.get("curve_type") == "curve":
                log("  FLAG: {0} {1} has non-line segments (curved "
                    "bulkhead)".format(fl, brk["id"]))
            g = grid_offsets(brk, grid_x, grid_y)
            if g:
                brec["grid_offsets"] = g
        if cy_div:
            for tot in frep["pours"].values():
                tot["vol_cy"] = round(tot["vol"] / cy_div, 1)
        if frep.get("target_area") and frep["total_soffit_area"]:
            frep["target_ratio"] = round(
                frep["total_soffit_area"] / frep["target_area"], 3)
    return report


def _doc_base(doc):
    if doc.Path:
        return os.path.splitext(doc.Path)[0]
    return os.path.join(STAGE, "pourbreaks")


def main():
    doc = Rhino.RhinoDoc.ActiveDoc
    log = fw.Log()
    log("split_pourbreaks — model units: {0}".format(doc.ModelUnitSystem))
    base = _doc_base(doc)
    json_path = os.environ.get("PB_JSON") or base + "_pourbreaks.json"
    if not os.path.exists(json_path):
        # No silent fallback to a shared/staging JSON: cutting one
        # project's slabs at another project's break coordinates would
        # look like a clean run. Missing input is a hard stop.
        log("ERROR: no breaks JSON at {0} — run pourbreak_harvest first "
            "(or point PB_JSON at the intended file)".format(json_path))
        log.save(base + "_pourbreak_log.txt")
        return
    log("breaks <- {0}".format(json_path))
    data = json.loads(io.open(json_path, encoding="utf-8").read())
    data = upconvert_v1(data, log)
    report = split_document(doc, data, None, log)
    if report is not None:
        out3dm = os.environ.get("PB_OUT3DM") or base + "_pourbreaks.3dm"
        opts = Rhino.FileIO.FileWriteOptions()
        opts.SuppressDialogBoxes = True
        ok = doc.WriteFile(out3dm, opts)
        log("derived model -> {0} ({1})".format(
            out3dm, "ok" if ok else "WRITE FAILED"))
        report["source_json"] = json_path
        report_path = os.environ.get("PB_REPORT") \
            or base + "_pourbreak_report.json"
        with io.open(report_path, "w", encoding="utf-8") as fh:
            fh.write(u"{0}".format(json.dumps(report, indent=1,
                                              sort_keys=True)))
        log("report -> {0}".format(report_path))
    log.save(base + "_pourbreak_log.txt")


if __name__ == "__main__":
    # The splitter force-deletes original slabs and adds pour pieces on
    # whatever document it is handed. It is meant ONLY for the staged
    # copies that the plugin and the dev loop open in a headless child
    # (both set FW_HEADLESS=1). Typed interactively it would shred the
    # live model AND clear its Modified flag, so it refuses to run - the
    # same stance as the QTOCheckupReport worker command.
    if os.environ.get("FW_HEADLESS") != "1":
        Rhino.RhinoApp.WriteLine(
            "split_pourbreaks is the headless worker behind the formwork "
            "SPLIT BREAKS button and only runs in a child Rhino on a "
            "staged copy (FW_HEADLESS=1). Nothing was changed.")
    else:
        try:
            main()
        except Exception:
            with io.open(os.path.join(STAGE, "pourbreak_error.txt"), "w",
                         encoding="utf-8") as fh:
                fh.write(u"{0}".format(traceback.format_exc()))
            # a swallowed failure looks exactly like a clean run; the
            # sibling engines all re-raise
            raise
        finally:
            # clear Modified ONLY here, in the headless throwaway run -
            # on a live document it would mask the user's own unsaved
            # edits (sideform_gen got this fix first; the splitter is
            # now aligned)
            try:
                Rhino.RhinoDoc.ActiveDoc.Modified = False
            except Exception:
                pass
            try:
                Rhino.RhinoApp.Exit()
            except Exception:
                Rhino.RhinoApp.RunScript("_-Exit", False)
