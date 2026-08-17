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

_rnd = pbh._rnd


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

    # File3dmLayerTable has no [] indexer under CPython 3 - enumerate.
    sheet_layers = set()
    layer_names = {}
    for layer in f3.AllLayers:
        layer_names[layer.Index] = layer.Name or ""
        if (layer.Name or "").upper().startswith("SHEET"):
            sheet_layers.add(layer.Index)

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
        if attr is None or attr.LayerIndex in sheet_layers:
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

    floors = {}
    for fl, z, xy, ctype, guid, name in sorted(
            raw_breaks, key=lambda t: (t[0], t[1], t[2][0][0], t[2][0][1])):
        entry = floors.setdefault(fl, {"breaks": [], "pour_markers": [],
                                       "target_area": None})
        bid = "{0}-PB{1}".format(fl, len(entry["breaks"]) + 1)
        entry["breaks"].append({
            "id": bid, "polyline": xy, "z": z, "curve_type": ctype,
            "binding": "sheet", "provenance": "sheet curve {0}".format(guid),
            "note": name})
        log("  {0}: {1} ({2}, {3} pts, z={4:.3f})".format(
            fl, bid, ctype, len(xy), z))
    for fl, pour, x, y, z, guid in sorted(
            raw_markers, key=lambda t: (t[0], t[1], t[2], t[3])):
        entry = floors.setdefault(fl, {"breaks": [], "pour_markers": [],
                                       "target_area": None})
        entry["pour_markers"].append({
            "pour": pour, "at": [x, y], "z": z, "binding": "sheet",
            "provenance": "sheet dot {0}".format(guid)})

    # Floors WITHOUT a cell on this sheet keep their previous JSON entries
    # untouched: a floor whose slabs were hidden or renamed at MAKE time
    # must not lose its harvested breaks to a wholesale rewrite. (A floor
    # that HAS a cell and no ink was deliberately cleared by the user.)
    covered = set()
    for cell in cells:
        covered.update(cell["floors"].keys())
    pb_json = os.environ.get("PB_JSON") or os.path.join(
        STAGE, "pour_breaks_model.json")
    if os.path.exists(pb_json):
        try:
            old = json.loads(io.open(pb_json, encoding="utf-8").read())
            for fl, cfg in sorted((old.get("floors") or {}).items()):
                if fl not in covered and fl not in floors:
                    floors[fl] = cfg
                    log("  {0}: no cell on this sheet - previous entry "
                        "PRESERVED ({1} breaks, {2} markers)".format(
                            fl, len(cfg.get("breaks", [])),
                            len(cfg.get("pour_markers", []))))
        except Exception as ex:
            log("WARNING: previous JSON unreadable ({0}) - floors without a "
                "cell could not be preserved".format(ex))

    fanned = sum(len(c["floors"]) > 1 for c in cells)
    log("imported {0} curves + {1} dots into {2} floor bucket(s)"
        "{3}".format(n_curves, n_dots, len(floors),
                     " ({0} TYP cell(s) fanned out)".format(fanned)
                     if fanned else ""))
    return {
        "version": pbh.SCHEMA_VERSION,
        "units": meta.get("units", ""),
        "source": {"kind": "sheet", "model": meta.get("docPath", ""),
                   "sheet": SHEET_FILE},
        "floors": floors,
    }


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
