# -*- coding: utf-8 -*-
"""Headless acceptance test for the BREAK SHEET pipeline (P1).

Builds a synthetic METRIC scene, then exercises:

    MAKE (breaksheet_gen)  ->  cells + TYP grouping asserts
    ->  IMPORT (breaksheet_import)  ->  fixed point vs the seeded JSON
    ->  user-edit simulation (File3dm append into a cell)  ->  fan-out
    ->  straddling curve  ->  loud total refusal, JSON untouched

Scene (metres):
  Slab_A  x[0,10] y[0,10] z 3.0->3.2   floor L01 } identical footprints
  Slab_B  x[0,10] y[0,10] z 6.0->6.2   floor L02 } -> ONE TYP cell
  Slab_C  x[0,12] y[0,10] z 9.0->9.2   floor L03   -> own cell
  Column  x[4,4.4] y[4,4.4] z 0->3.0   support under L01

Seeded pour_breaks_model.json: one break + one pour dot on L01 (the TYP
representative) - the generator draws them into the TYP cell; importing
the untouched sheet must reproduce the L01 entries byte-for-byte
(id/polyline/z/curve_type/note; provenance is surface-specific by design)
and fan them out to L02.

Launched via  Rhino.exe /nosplash /notemplate
    /runscript="-_RunPythonScript <staged copy of this file>"
with FW_HEADLESS=1. Report: bs_test_report.txt in the staging folder.
"""
from __future__ import division, print_function

import io
import json
import os
import sys

STAGE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "qto_fw_test")
sys.path.insert(0, STAGE)
REPORT = os.path.join(STAGE, "bs_test_report.txt")


def write_report(text_lines):
    with io.open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(u"\n".join(u"{0}".format(l) for l in text_lines) + u"\n")


try:
    import Rhino
    from Rhino.Geometry import (BoundingBox, Brep, Point3d, Polyline,
                                PolylineCurve, TextDot)
    import formwork_gen_rhino as fw
    import breaksheet_gen as bsg
    import breaksheet_import as bsi
except Exception:
    import traceback
    write_report(["IMPORT FAILURE", traceback.format_exc()])
    raise

failures = []
lines = []


def log(msg):
    lines.append(msg)
    try:
        Rhino.RhinoApp.WriteLine(msg)
    except Exception:
        pass


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    if not cond:
        failures.append(label)
    log("[{0}] {1} {2}".format(status, label, detail))


def add_box(doc, layer, x0, x1, y0, y1, z0, z1):
    idx = fw.ensure_layer(doc, [layer])
    brep = Brep.CreateFromBox(
        BoundingBox(Point3d(x0, y0, z0), Point3d(x1, y1, z1)))
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = idx
    return doc.Objects.AddBrep(brep, attr)


PB_JSON = os.path.join(STAGE, "pour_breaks_model.json")

# deliberately over-drawn 3.5 m past the slab on both ends (> the 2 m cell
# margin): the cell must grow to hold carried-in ink or the untouched sheet
# refuses its own import
SEED_BREAK = {
    "id": "L01-PB1",
    "polyline": [[2.0, -3.5], [2.0, 13.5]],
    "z": 3.2,
    "curve_type": "polyline",
    "binding": "sheet",
    "provenance": "seed",
    "note": "",
}
# a sampled non-polyline break: curve_type must survive the round trip
SEED_CURVE_BREAK = {
    "id": "L01-PB2",
    "polyline": [[5.0, -0.5], [5.0, 10.5]],
    "z": 3.2,
    "curve_type": "curve",
    "binding": "sheet",
    "provenance": "seed",
    "note": "",
}
SEED_MARKER = {"pour": 1, "at": [1.0, 5.0], "z": 3.2,
               "binding": "sheet", "provenance": "seed"}
# a hidden slab's floor: its cell must still exist (harvest-parity enumerator)
SEED_L04_BREAK = {
    "id": "L04-PB1",
    "polyline": [[1.0, -0.5], [1.0, 10.5]],
    "z": 12.2,
    "curve_type": "polyline",
    "binding": "sheet",
    "provenance": "seed",
    "note": "",
}
# a floor with NO slabs in the model at all: no cell, and the import must
# PRESERVE its entry instead of silently deleting it
SEED_L99 = {"breaks": [{"id": "L99-PB1",
                        "polyline": [[0.0, 0.0], [3.0, 3.0]],
                        "z": 99.0, "curve_type": "polyline",
                        "binding": "layer", "provenance": "seed",
                        "note": "orphan"}],
            "pour_markers": [], "target_area": None}


def seed_json(doc):
    data = {
        "version": 2,
        "units": str(doc.ModelUnitSystem),
        "source": {"kind": "sheet", "model": "", "sheet": ""},
        "floors": {"L01": {"breaks": [SEED_BREAK, SEED_CURVE_BREAK],
                           "pour_markers": [SEED_MARKER],
                           "target_area": None},
                   "L04": {"breaks": [SEED_L04_BREAK],
                           "pour_markers": [], "target_area": None},
                   "L99": SEED_L99},
    }
    with io.open(PB_JSON, "w", encoding="utf-8") as fh:
        fh.write(u"{0}".format(json.dumps(data, indent=1, sort_keys=True)))


def break_key(entry):
    return (entry["id"], json.dumps(entry["polyline"]), entry["z"],
            entry["curve_type"], entry["note"])


def load_pb():
    return json.loads(io.open(PB_JSON, encoding="utf-8").read())


def count_by_layer(f3):
    names = {}
    for layer in f3.AllLayers:
        names[layer.Index] = layer.Name or ""
    counts = {}
    for obj in f3.Objects:
        n = names.get(obj.Attributes.LayerIndex, "?")
        counts[n] = counts.get(n, 0) + 1
    return counts


def main():
    doc = Rhino.RhinoDoc.ActiveDoc
    doc.AdjustModelUnitSystem(Rhino.UnitSystem.Meters, False)
    doc.ModelAbsoluteTolerance = 0.001
    log("=== breaksheet headless test (METRIC scene) ===")

    add_box(doc, "Slab_A", 0, 10, 0, 10, 3.0, 3.2)
    add_box(doc, "Slab_B", 0, 10, 0, 10, 6.0, 6.2)
    add_box(doc, "Slab_C", 0, 12, 0, 10, 9.0, 9.2)
    hidden_id = add_box(doc, "Slab_D", 0, 8, 0, 10, 12.0, 12.2)
    doc.Objects.Hide(hidden_id, True)
    add_box(doc, "Slab_SOG_X", 0, 20, 0, 20, 0.0, 0.2)   # excluded by name
    add_box(doc, "Column_1", 4, 4.4, 4, 4.4, 0.0, 3.0)
    doc.Strings.SetString("FloorElevations", json.dumps(
        {"3.2": "L01", "6.2": "L02", "9.2": "L03", "12.2": "L04"}))
    seed_json(doc)

    # ---- MAKE ----
    meta = bsg.main()
    check("sheet generated", meta is not None)
    if meta is None:
        return
    cells = meta["cells"]
    check("3 cells (TYP collapse + hidden L04, SOG excluded)",
          len(cells) == 3, "got {0}".format(len(cells)))
    typ = next((c for c in cells if len(c["floors"]) == 2), None)
    solo = next((c for c in cells if list(c["floors"].keys()) == ["L03"]),
                None)
    l04 = next((c for c in cells if list(c["floors"].keys()) == ["L04"]),
               None)
    check("TYP cell = L01+L02", typ is not None and
          sorted(typ["floors"].keys()) == ["L01", "L02"])
    check("TYP z per member", typ is not None and
          typ["floors"].get("L01") == 3.2 and typ["floors"].get("L02") == 6.2)
    check("solo cell = L03", solo is not None)
    check("HIDDEN slab still got its cell", l04 is not None)

    f3 = Rhino.FileIO.File3dm.Read(bsg.SHEET_FILE)
    check("sheet readable", f3 is not None)
    counts = count_by_layer(f3)
    check("outlines drawn", counts.get("SHEET_OUTLINE", 0) == 3,
          str(counts))
    check("frames drawn", counts.get("SHEET_FRAME", 0) == 3)
    check("support drawn", counts.get("SHEET_SUPPORT", 0) == 1)
    # L01: 2 break curves + 1 pour dot; L04: 1 break curve
    check("seeded ink carried in", counts.get("DRAW", 0) == 4, str(counts))

    # ---- IMPORT the untouched sheet: fixed point + fan-out + preserve ----
    data = bsi.main()
    check("import succeeded (overhanging carried-in ink still in-frame)",
          data is not None)
    if data is None:
        return
    pb = load_pb()
    fl = pb["floors"]
    check("L01 still 2 breaks", len(fl.get("L01", {}).get("breaks", [])) == 2)
    check("fan-out to L02", len(fl.get("L02", {}).get("breaks", [])) == 2)
    check("no phantom L03 break", "L03" not in fl or
          not fl["L03"]["breaks"])
    check("hidden-slab floor kept its break (was silent data loss)",
          len(fl.get("L04", {}).get("breaks", [])) == 1)
    check("cell-less floor PRESERVED (was silent data loss)",
          fl.get("L99") == SEED_L99, str(fl.get("L99"))[:120])
    if len(fl.get("L01", {}).get("breaks", [])) == 2:
        got1, got2 = fl["L01"]["breaks"]
        check("fixed point (L01 break)",
              break_key(got1) == break_key(SEED_BREAK),
              "{0} vs {1}".format(break_key(got1), break_key(SEED_BREAK)))
        check("curve_type survives the round trip",
              break_key(got2) == break_key(SEED_CURVE_BREAK),
              "{0} vs {1}".format(break_key(got2),
                                  break_key(SEED_CURVE_BREAK)))
    if fl.get("L02", {}).get("breaks"):
        got = fl["L02"]["breaks"][0]
        check("L02 break at its own z", got["z"] == 6.2 and
              got["id"] == "L02-PB1")
    check("markers fanned", len(fl.get("L01", {}).get("pour_markers", [])) == 1
          and len(fl.get("L02", {}).get("pour_markers", [])) == 1)

    # ---- user-edit simulation: draw a new break in the L03 cell ----
    dx, dy = solo["offset"]
    draw_idx = None
    for layer in f3.AllLayers:
        if layer.Name == "DRAW":
            draw_idx = layer.Index
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = draw_idx
    pl = Polyline()
    for x, y in [[6.0, -0.5], [6.0, 10.5]]:      # world coords on L03
        pl.Add(x + dx, y + dy, 0.0)
    f3.Objects.AddCurve(PolylineCurve(pl), attr)
    check("sheet re-written", f3.Write(bsg.SHEET_FILE, 7))

    data = bsi.main()
    check("re-import succeeded", data is not None)
    pb = load_pb()
    fl = pb["floors"]
    check("L03 gained its break",
          len(fl.get("L03", {}).get("breaks", [])) == 1 and
          fl["L03"]["breaks"][0]["z"] == 9.2)
    check("TYP floors unchanged",
          len(fl.get("L01", {}).get("breaks", [])) == 2 and
          len(fl.get("L02", {}).get("breaks", [])) == 2)
    check("preserved floor survives re-import", fl.get("L99") == SEED_L99)
    if fl.get("L03", {}).get("breaks"):
        got = fl["L03"]["breaks"][0]["polyline"]
        check("L03 world coords recovered",
              got == [[6.0, -0.5], [6.0, 10.5]], str(got))

    # ---- straddling curve: loud total refusal, JSON untouched ----
    before = io.open(PB_JSON, encoding="utf-8").read()
    f3 = Rhino.FileIO.File3dm.Read(bsg.SHEET_FILE)
    frames = [c["frame"] for c in cells]
    x_mid_a = (frames[0][0] + frames[0][2]) / 2.0
    x_mid_b = (frames[1][0] + frames[1][2]) / 2.0
    y_mid = (frames[0][1] + frames[0][3]) / 2.0
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = draw_idx
    pl = Polyline()
    pl.Add(x_mid_a, y_mid, 0.0)
    pl.Add(x_mid_b, y_mid, 0.0)
    f3.Objects.AddCurve(PolylineCurve(pl), attr)
    check("straddler written", f3.Write(bsg.SHEET_FILE, 7))
    data = bsi.main()
    check("straddler refused", data is None)
    after = io.open(PB_JSON, encoding="utf-8").read()
    check("JSON untouched on refusal", before == after)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        failures.append("unhandled exception")
        lines.append(traceback.format_exc())
    lines.append("")
    lines.append("RESULT: {0}".format(
        "ALL PASS" if not failures else "{0} FAILURE(S): {1}".format(
            len(failures), ", ".join(failures))))
    write_report(lines)
    if os.environ.get("FW_HEADLESS") == "1":
        try:
            Rhino.RhinoApp.Exit()
        except Exception:
            Rhino.RhinoApp.RunScript("_-Exit", False)
