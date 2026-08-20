#! python 2
# -*- coding: utf-8 -*-
"""Import the drawn BREAK SHEET back into ``pour_breaks_model.json`` (v2).

Reads ``breaksheet.3dm`` via ``Rhino.FileIO.File3dm`` - the sheet is never
opened as a document and the live model is never touched. Every OPEN curve
on a non-SHEET layer is a pour break; every numbered text dot is a pour
marker. The cell a curve sits in (its whole bounding box inside one cell
frame from ``breaksheet.meta.json``) binds it to that cell's floors -
binding by NAME via the cell map, strictly stronger than the harvest's
nearest-elevation fallback. A TYP cell fans its content out EXPLICITLY to
every member floor at that floor's own elevation (never the duplicate-
floor-name bucketing quirk).

Refusals are loud and total: a curve straddling frames or outside every
frame, a closed curve, or a missing/unreadable sidecar aborts the import
with the offenders listed - the existing JSON is left untouched, because a
half-imported break set under a green light is exactly the failure class
the derived-model sidecar exists to prevent.

Serialization reuses pourbreak_harvest's constants and writer, so an
unchanged sheet re-imports to the same bytes (ids and ordering included;
``provenance`` naturally differs between surfaces and is excluded from
that contract).

P2 (2026-08-19): per-floor ``target_area`` and the optional top-level
``grid_x``/``grid_y`` are PRESERVED from the previous JSON - the sheet
cannot express them, so a reimport must not wipe them. After a successful
import the log carries an ADVISORY block: estimated per-pour soffit areas
(cell footprints split by the drawn ink, extended by the splitter's first
ladder rung) and nearest-grid offsets per axis-parallel break segment.
Advisory means advisory - any failure there logs one line and changes
nothing; the splitter's report stays the binding review artifact.
"""
from __future__ import division, print_function

import io
import json
import os
import sys
import traceback

STAGE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "qto_fw_test")
sys.path.insert(0, STAGE)

import Rhino
from Rhino.Geometry import Curve, TextDot

import formwork_gen_rhino as fw
import pourbreak_harvest as pbh

SHEET_FILE = os.environ.get("BS_SHEET") or os.path.join(STAGE, "breaksheet.3dm")
META_FILE = os.environ.get("BS_META") or os.path.join(STAGE, "breaksheet.meta.json")
LOG_FILE = os.path.join(STAGE, "breaksheet_import_log.txt")

ADVISORY_EXT_M = 5.0     # the splitter's FIRST extension rung: under-drawn
                         # advisory cuts still sever, without the full ladder

_rnd = pbh._rnd

# QTO area convention (Methods.SetAreaConversionFactor): inch models report
# sf, feet models already are sf, everything else stays in model units.
_AREA_LABELS = {"meters": "m2", "millimeters": "mm2", "centimeters": "cm2"}


def _area_scale(units):
    u = (units or "").lower()
    if u == "inches":
        return 1.0 / 144.0, "sf"
    if u == "feet":
        return 1.0, "sf"
    return 1.0, _AREA_LABELS.get(u, (units or "model-unit") + "2")


def _load_meta(log):
    if not os.path.exists(META_FILE):
        log("REFUSED: no sheet sidecar at {0} - regenerate the sheet with "
            "MAKE BREAK SHEET".format(META_FILE))
        return None
    try:
        return json.loads(io.open(META_FILE, encoding="utf-8").read())
    except Exception as ex:
        log("REFUSED: sheet sidecar unreadable ({0}) - regenerate the "
            "sheet".format(ex))
        return None


def _cell_of(bb, cells, slack):
    """Cell whose frame contains the WHOLE bbox (slack for osnap overshoot).
    Returns (cell, status) - status 'ok' | 'straddle' | 'orphan'."""
    inside = []
    touching = []
    for cell in cells:
        x0, y0, x1, y1 = cell["frame"]
        if (bb.Min.X >= x0 - slack and bb.Max.X <= x1 + slack and
                bb.Min.Y >= y0 - slack and bb.Max.Y <= y1 + slack):
            inside.append(cell)
        elif not (bb.Max.X < x0 or bb.Min.X > x1 or
                  bb.Max.Y < y0 or bb.Min.Y > y1):
            touching.append(cell)
    if len(inside) == 1:
        return inside[0], "ok"
    if inside or touching:
        return None, "straddle"
    return None, "orphan"


# ── advisory pour areas + grid offsets (P2) ────────────────────────────────
# Everything below is INFORMATIONAL: it runs after the import has already
# succeeded, and any failure logs one line and changes nothing. The binding
# authority stays the splitter's report; this is the modeler's early read.

def _extended_ink(xy, ext):
    """Sheet-coord polyline extended ``ext`` past both ends, mirroring the
    splitter's first ladder rung, so an under-drawn advisory cut still
    severs the footprint."""
    pts = [Rhino.Geometry.Point3d(x, y, 0.0) for x, y in xy]
    if len(pts) >= 2:
        d0 = pts[0] - pts[1]
        if d0.Unitize():
            pts[0] = pts[0] + d0 * ext
        d1 = pts[-1] - pts[-2]
        if d1.Unitize():
            pts[-1] = pts[-1] + d1 * ext
    pl = Rhino.Geometry.Polyline()
    for p in pts:
        pl.Add(p.X, p.Y, p.Z)
    return Rhino.Geometry.PolylineCurve(pl)


def _face_contains(face_brep, x, y, tol):
    """Point-in-region for a single planar face: inside the outer loop and
    inside no hole (a marker dropped in an opening must not claim it)."""
    pt = Rhino.Geometry.Point3d(x, y, 0.0)
    face = face_brep.Faces[0]
    inside = False
    for li in range(face.Loops.Count):
        loop = face.Loops[li]
        cont = loop.To3dCurve().Contains(
            pt, Rhino.Geometry.Plane.WorldXY, tol)
        if loop.LoopType == Rhino.Geometry.BrepLoopType.Outer:
            if cont == Rhino.Geometry.PointContainment.Inside:
                inside = True
        elif cont == Rhino.Geometry.PointContainment.Inside:
            return False
    return inside


def _grid_offsets(xy, grid_x, grid_y):
    """Nearest-grid offset per axis-parallel segment - the same rule as
    split_pourbreaks.grid_offsets (kept in step by the headless test's
    fixed point, not by an import: loading the splitter module here would
    run its top level inside the host Rhino)."""
    out = []
    pts = [(float(p[0]), float(p[1])) for p in xy]
    for i in range(len(pts) - 1):
        (ax, ay), (bx, by) = pts[i], pts[i + 1]
        seg_len = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
        if seg_len <= 0:
            continue
        if abs(bx - ax) < 0.01 * seg_len and grid_x:
            pos = (ax + bx) / 2.0
            name = min(grid_x, key=lambda g: abs(grid_x[g] - pos))
            out.append((i, "x", name, round(pos - grid_x[name], 3)))
        elif abs(by - ay) < 0.01 * seg_len and grid_y:
            pos = (ay + by) / 2.0
            name = min(grid_y, key=lambda g: abs(grid_y[g] - pos))
            out.append((i, "y", name, round(pos - grid_y[name], 3)))
    return out


def _cell_regions(cell, ext, tol):
    """Split the cell's slab footprints (sheet coords) by its drawn ink.
    Returns (regions, pre_split_count) - regions as single-face Breps -
    or (None, 0) when the furniture could not be assembled into planar
    regions. pre_split_count is the un-cut face count: the "did not
    sever" baseline on a multi-slab cell is that, never 1."""
    closed = []
    for c in (cell.get("_outlines") or []) + (cell.get("_openings") or []):
        d = c.DuplicateCurve()
        if not d.IsClosed:
            # sampled (non-polyline) loops are drawn open by a chord gap;
            # generous closing is fine at advisory fidelity
            bb = d.GetBoundingBox(True)
            if not d.MakeClosed(bb.Diagonal.Length * 0.05):
                continue
        closed.append(d)
    if not closed:
        return None, 0
    breps = Rhino.Geometry.Brep.CreatePlanarBreps(closed, tol)
    if not breps:
        return None, 0
    pre_split = sum(b.Faces.Count for b in breps)
    cutters = [_extended_ink(e["sheet"], ext)
               for e in (cell.get("_ink") or [])]
    regions = []
    for brep in breps:
        for fi in range(brep.Faces.Count):
            face = brep.Faces[fi]
            split = None
            if cutters:
                split = face.Split(cutters, tol)
            if split is None:
                regions.append(face.DuplicateFace(False))
            else:
                for fj in range(split.Faces.Count):
                    regions.append(split.Faces[fj].DuplicateFace(False))
    return regions, pre_split


def _advisory_report(cells, sheet_units, old, log):
    """Per-cell pour-area estimate + grid offsets, into the import log."""
    scale = Rhino.RhinoMath.UnitScale(Rhino.UnitSystem.Meters, sheet_units)
    tol = 0.001 * scale
    area_scale, area_label = _area_scale(str(sheet_units))
    old_floors = old.get("floors") or {}
    grid_x = old.get("grid_x") or {}
    grid_y = old.get("grid_y") or {}

    for cell in cells:
        ink = cell.get("_ink") or []
        if not ink:
            continue
        floors_txt = ", ".join(sorted(cell["floors"].keys()))
        regions, pre_split = _cell_regions(cell, ADVISORY_EXT_M * scale, tol)
        if regions is None:
            log("ADVISORY [{0}]: slab outlines did not assemble into "
                "planar regions - pour areas not estimated".format(
                    floors_txt))
        else:
            marks = cell.get("_marks") or []
            pour_area = {}
            unmarked = [0, 0.0]
            total = 0.0
            for region in regions:
                amp = Rhino.Geometry.AreaMassProperties.Compute(region)
                if amp is None:
                    continue
                area = amp.Area
                total += area
                hits = [p for p, mx, my in marks
                        if _face_contains(region, mx, my, tol)]
                if not hits:
                    unmarked[0] += 1
                    unmarked[1] += area
                    continue
                if len(set(hits)) > 1:
                    log("ADVISORY [{0}]: one region holds markers for "
                        "pours {1} - the breaks may not separate "
                        "them".format(floors_txt,
                                      ", ".join(str(p)
                                                for p in sorted(set(hits)))))
                pour_area[hits[0]] = pour_area.get(hits[0], 0.0) + area
            if len(regions) <= pre_split and ink:
                log("ADVISORY [{0}]: the drawn breaks did not sever the "
                    "footprint (even extended {1} m) - areas below are "
                    "the uncut footprint(s)".format(floors_txt,
                                                    ADVISORY_EXT_M))
            parts = []
            for p in sorted(pour_area):
                pct = 100.0 * pour_area[p] / total if total else 0.0
                parts.append("pour {0} ~ {1} {2} ({3:.0f}%)".format(
                    p, round(pour_area[p] * area_scale, 1), area_label,
                    pct))
            if unmarked[0]:
                parts.append("{0} region(s) without a marker (~ {1} "
                             "{2})".format(unmarked[0],
                                           round(unmarked[1] * area_scale,
                                                 1), area_label))
            if parts:
                log("ADVISORY pour areas [{0}]: {1}".format(
                    floors_txt, "; ".join(parts)))
            # target_area is compared RAW (model units), matching the
            # splitter's target_ratio arithmetic
            for fl in sorted(cell["floors"]):
                tgt = (old_floors.get(fl) or {}).get("target_area")
                if tgt and pour_area:
                    ratios = ", ".join(
                        "pour {0} = {1:.0f}%".format(
                            p, 100.0 * pour_area[p] / tgt)
                        for p in sorted(pour_area))
                    log("ADVISORY vs target_area {0} ({1}): {2}".format(
                        tgt, fl, ratios))
        if grid_x or grid_y:
            # same order the PB ids were assigned in (per-floor sort by
            # first vertex; z is constant within a cell), so "break n"
            # here IS L0x-PBn for every member floor - drawing order is
            # not id order
            ink_by_id = sorted(ink, key=lambda e: (e["world"][0][0],
                                                   e["world"][0][1]))
            for k, e in enumerate(ink_by_id):
                for seg, axis, name, off in _grid_offsets(
                        e["world"], grid_x, grid_y):
                    log("ADVISORY grid [{0}] break {1} seg {2}: {3} off "
                        "grid '{4}' ({5}-axis)".format(
                            floors_txt, k + 1, seg, off, name, axis))


def import_sheet(log):
    meta = _load_meta(log)
    if meta is None:
        return None
    if not os.path.exists(SHEET_FILE):
        log("REFUSED: no sheet at {0} - run MAKE BREAK SHEET first".format(
            SHEET_FILE))
        return None

    doc = Rhino.RhinoDoc.ActiveDoc
    doc_path = (doc.Path or "") if doc is not None else ""
    if meta.get("docPath") and doc_path and \
            meta["docPath"].lower() != doc_path.lower():
        log("WARNING: the sheet was generated from a DIFFERENT model file:")
        log("  sheet:   {0}".format(meta["docPath"]))
        log("  current: {0}".format(doc_path))
        log("  (the FormworkUI confirm dialog is the gate for this)")

    f3 = Rhino.FileIO.File3dm.Read(SHEET_FILE)
    if f3 is None:
        log("REFUSED: {0} could not be read - if it was saved by a newer "
            "Rhino, re-save it as a Rhino 7 file".format(SHEET_FILE))
        return None

    # The previous JSON is read ONCE, up front: it preserves cell-less
    # floors, per-floor target_area and the optional grid across the
    # rewrite, and feeds the advisory report.
    pb_json = os.environ.get("PB_JSON") or os.path.join(
        STAGE, "pour_breaks_model.json")
    old = {}
    if os.path.exists(pb_json):
        try:
            # `with`, never a bare read: IronPython has no refcount
            # collection and an unclosed .NET stream keeps the JSON locked
            # (a later delete fails; rewrites merely share)
            with io.open(pb_json, encoding="utf-8") as fh:
                old = json.loads(fh.read())
        except Exception as ex:
            log("WARNING: previous JSON unreadable ({0}) - floors without "
                "a cell, target areas and the grid could not be "
                "preserved".format(ex))
            old = {}
    # Same guard as breaksheet_gen._existing_breaks: the staging folder is
    # machine-wide with one fixed file name, so another project's JSON must
    # not launder its floors/targets/grid into this model's output under a
    # fresh source stamp.
    old_src = ((old.get("source") or {}).get("model") or "") if old else ""
    ref_doc = meta.get("docPath") or doc_path
    if old_src and ref_doc and old_src.lower() != ref_doc.lower():
        log("WARNING: the previous breaks JSON came from a DIFFERENT model "
            "file ({0}) - nothing is preserved from it; floors without a "
            "cell, target areas and the grid start empty.".format(old_src))
        old = {}
    old_floors = old.get("floors") or {}

    # File3dmLayerTable has no [] indexer under CPython 3 - enumerate.
    sheet_layers = set()
    furniture_layers = {}     # index -> "_outlines" | "_openings"
    layer_names = {}
    for layer in f3.AllLayers:
        layer_names[layer.Index] = layer.Name or ""
        name_up = (layer.Name or "").upper()
        if name_up.startswith("SHEET"):
            sheet_layers.add(layer.Index)
            if name_up == "SHEET_OUTLINE":
                furniture_layers[layer.Index] = "_outlines"
            elif name_up == "SHEET_OPENING":
                furniture_layers[layer.Index] = "_openings"

    cells = meta.get("cells", [])
    if not cells:
        log("REFUSED: the sidecar lists no cells")
        return None
    pitch = max(c["frame"][2] - c["frame"][0] for c in cells)
    slack = 0.01 * pitch

    raw_breaks = []      # (floor, z, xy_world, ctype, guid, name)
    raw_markers = []     # (floor, pour, x, y, z, guid)
    errors = []
    n_curves = 0
    n_dots = 0

    for obj in f3.Objects:
        attr = obj.Attributes
        if attr is None:
            continue
        if attr.LayerIndex in sheet_layers:
            # locked furniture is never imported, but the outlines and
            # opening loops feed the ADVISORY pour-area estimate
            key = furniture_layers.get(attr.LayerIndex)
            if key is not None and isinstance(obj.Geometry, Curve):
                cell, status = _cell_of(
                    obj.Geometry.GetBoundingBox(True), cells, slack)
                if status == "ok":
                    cell.setdefault(key, []).append(obj.Geometry)
            continue
        geom = obj.Geometry
        if isinstance(geom, TextDot):
            n_dots += 1
            txt = (geom.Text or "").strip()
            try:
                pour = int(txt)
            except (TypeError, ValueError):
                errors.append("text dot '{0}' is not a pour number".format(txt))
                continue
            bb = geom.GetBoundingBox(True)
            cell, status = _cell_of(bb, cells, slack)
            if status != "ok":
                errors.append("pour dot '{0}' at ({1:.1f}, {2:.1f}) is {3} "
                              "of every cell frame".format(
                                  txt, bb.Min.X, bb.Min.Y,
                                  "outside" if status == "orphan"
                                  else "straddling"))
                continue
            dx, dy = cell["offset"]
            p = geom.Point
            cell.setdefault("_marks", []).append((pour, p.X, p.Y))
            for fl, z in cell["floors"].items():
                raw_markers.append((fl, pour, _rnd(p.X - dx), _rnd(p.Y - dy),
                                    _rnd(z), str(obj.Attributes.ObjectId)))
            continue
        if isinstance(geom, Curve):
            n_curves += 1
            if geom.IsClosed:
                errors.append("closed curve on layer '{0}' - pour breaks are "
                              "open cut lines".format(
                                  layer_names.get(attr.LayerIndex, "?")))
                continue
            xy, _, ctype = pbh._curve_plan_points(geom)
            if attr.GetUserString("PB_CURVE_TYPE") == "curve":
                # a sampled non-polyline break carried into the sheet keeps
                # its classification (and the splitter's curved-bulkhead
                # flag) across the round trip - mirrors harvest/restore
                ctype = "curve"
            if len(xy) < 2:
                errors.append("degenerate curve on layer '{0}'".format(
                    layer_names.get(attr.LayerIndex, "?")))
                continue
            bb = geom.GetBoundingBox(True)
            cell, status = _cell_of(bb, cells, slack)
            if status != "ok":
                errors.append("curve at ({0:.1f}, {1:.1f}) is {2} the cell "
                              "frames - keep each break inside one cell"
                              .format(bb.Min.X, bb.Min.Y,
                                      "outside" if status == "orphan"
                                      else "straddling"))
                continue
            dx, dy = cell["offset"]
            world = [[_rnd(x - dx), _rnd(y - dy)] for x, y in xy]
            cell.setdefault("_ink", []).append({"sheet": xy, "world": world})
            for fl, z in cell["floors"].items():
                raw_breaks.append((fl, _rnd(z), world, ctype,
                                   str(obj.Attributes.ObjectId),
                                   attr.Name or ""))
            continue
        # anything else on an unlocked layer is a mistake worth naming
        errors.append("unsupported object type {0} on layer '{1}'".format(
            type(geom).__name__, layer_names.get(attr.LayerIndex, "?")))

    if errors:
        log("IMPORT REFUSED - {0} problem(s), nothing was written:".format(
            len(errors)))
        for e in errors:
            log("  - " + e)
        return None

    def _new_entry(fl):
        # target_area is authored data the sheet cannot express - a
        # reimport must not wipe it (P1 reset it to null every time)
        prev = old_floors.get(fl) or {}
        return {"breaks": [], "pour_markers": [],
                "target_area": prev.get("target_area")}

    floors = {}
    for fl, z, xy, ctype, guid, name in sorted(
            raw_breaks, key=lambda t: (t[0], t[1], t[2][0][0], t[2][0][1])):
        entry = floors.setdefault(fl, _new_entry(fl))
        bid = "{0}-PB{1}".format(fl, len(entry["breaks"]) + 1)
        entry["breaks"].append({
            "id": bid, "polyline": xy, "z": z, "curve_type": ctype,
            "binding": "sheet", "provenance": "sheet curve {0}".format(guid),
            "note": name})
        log("  {0}: {1} ({2}, {3} pts, z={4:.3f})".format(
            fl, bid, ctype, len(xy), z))
    for fl, pour, x, y, z, guid in sorted(
            raw_markers, key=lambda t: (t[0], t[1], t[2], t[3])):
        entry = floors.setdefault(fl, _new_entry(fl))
        entry["pour_markers"].append({
            "pour": pour, "at": [x, y], "z": z, "binding": "sheet",
            "provenance": "sheet dot {0}".format(guid)})

    # Floors WITHOUT a cell on this sheet keep their previous JSON entries
    # untouched: a floor whose slabs were hidden or renamed at MAKE time
    # must not lose its harvested breaks to a wholesale rewrite. A floor
    # that HAS a cell and no ink was deliberately cleared by the user -
    # but only its breaks/markers: erasing curves is expressible on the
    # sheet, target_area is not, so it rides on an otherwise-empty entry.
    covered = set()
    for cell in cells:
        covered.update(cell["floors"].keys())
    for fl, cfg in sorted(old_floors.items()):
        if fl in floors:
            continue
        if fl not in covered:
            floors[fl] = cfg
            log("  {0}: no cell on this sheet - previous entry "
                "PRESERVED ({1} breaks, {2} markers)".format(
                    fl, len(cfg.get("breaks", [])),
                    len(cfg.get("pour_markers", []))))
        else:
            floors[fl] = _new_entry(fl)
            if cfg.get("breaks") or cfg.get("pour_markers"):
                log("  {0}: cell has no ink - previous breaks/markers "
                    "cleared (target_area preserved)".format(fl))

    fanned = sum(len(c["floors"]) > 1 for c in cells)
    log("imported {0} curves + {1} dots into {2} floor bucket(s)"
        "{3}".format(n_curves, n_dots, len(floors),
                     " ({0} TYP cell(s) fanned out)".format(fanned)
                     if fanned else ""))

    # informational only - a failure here logs one line and never
    # touches what gets written
    try:
        _advisory_report(cells, f3.Settings.ModelUnitSystem, old, log)
    except Exception as ex:
        log("ADVISORY unavailable ({0}) - the import itself is "
            "unaffected".format(ex))

    out = {
        "version": pbh.SCHEMA_VERSION,
        "units": meta.get("units", ""),
        "source": {"kind": "sheet", "model": meta.get("docPath", ""),
                   "sheet": SHEET_FILE},
        "floors": floors,
    }
    # the optional grid rides at the top level (v1-upconvert convention);
    # the sheet cannot express it, so the previous JSON's survives
    for key in ("grid_x", "grid_y"):
        if key in old:
            out[key] = old[key]
    return out


def main():
    log = fw.Log()
    log("breaksheet_import <- {0}".format(SHEET_FILE))
    data = import_sheet(log)
    if data is not None:
        pb_json = os.environ.get("PB_JSON") or os.path.join(
            STAGE, "pour_breaks_model.json")
        pbh.write_json(data, pb_json, log)
    log.save(LOG_FILE)
    return data


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with io.open(os.path.join(STAGE, "breaksheet_import_error.txt"),
                     "w", encoding="utf-8") as fh:
            fh.write(u"{0}".format(traceback.format_exc()))
        raise
    finally:
        if os.environ.get("FW_HEADLESS") == "1":
            try:
                Rhino.RhinoApp.Exit()
            except Exception:
                Rhino.RhinoApp.RunScript("_-Exit", False)
