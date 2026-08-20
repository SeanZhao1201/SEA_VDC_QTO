# -*- coding: utf-8 -*-
"""Headless acceptance test for the BREAK SHEET pipeline (P1 + P2).

Builds a synthetic METRIC scene, then exercises:

    MAKE (breaksheet_gen)  ->  cells + TYP grouping + near-TYP asserts
    ->  IMPORT (breaksheet_import)  ->  fixed point vs the seeded JSON,
        target_area/grid preservation, ADVISORY areas + grid offsets
    ->  user-edit simulation (File3dm append into a cell)  ->  fan-out
    ->  straddling curve  ->  loud total refusal, JSON untouched
    ->  merge directives (P2)  ->  invalid directive tolerated, valid
        merge collapses cells, import fans out to every merged member

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


SEED_GRID_X = {"A": 0.0, "B": 5.0}

# non-empty fake path on every directive this test writes: leaked onto a
# SAVED model it is refused by the docPath guard, while the untitled
# headless doc (doc.Path == "") still applies it (guard needs both sides)
TEST_DOCPATH = "HEADLESS-BS-TEST.3dm"


def seed_json(doc):
    data = {
        "version": 2,
        "units": str(doc.ModelUnitSystem),
        "source": {"kind": "sheet", "model": "", "sheet": ""},
        # target_area + grid cannot be expressed on the sheet: the P1 bug
        # was that a reimport silently wiped both (P2 preserves them and
        # feeds the ADVISORY block from them). L03's entry has a target
        # but NO breaks - its covered cell will hold no ink, the case
        # where P1's preservation dropped the whole entry.
        "floors": {"L01": {"breaks": [SEED_BREAK, SEED_CURVE_BREAK],
                           "pour_markers": [SEED_MARKER],
                           "target_area": 50.0},
                   "L03": {"breaks": [], "pour_markers": [],
                           "target_area": 75.0},
                   "L04": {"breaks": [SEED_L04_BREAK],
                           "pour_markers": [], "target_area": None},
                   "L99": SEED_L99},
        "grid_x": SEED_GRID_X,
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
    # the staging folder is machine-wide: a merge directive left behind by
    # an earlier run would regroup the FIRST make and break every count
    if os.path.exists(bsg.MERGE_FILE):
        os.remove(bsg.MERGE_FILE)

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

    # P2: near-TYP suggestions land in the meta; nothing auto-merges
    pair_names = set()
    for p in meta.get("near_typ") or []:
        pair_names.add((p["a"], p["b"]))
        pair_names.add((p["b"], p["a"]))
    check("near-TYP L01-group vs L03 suggested",
          ("L01", "L03") in pair_names, str(sorted(pair_names)))
    check("no merges applied yet", meta.get("merged") == [],
          str(meta.get("merged")))

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

    # P2: authored data the sheet cannot express survives the reimport
    # (P1 wiped both target_area and the grid on every import)
    check("target_area preserved",
          fl.get("L01", {}).get("target_area") == 50.0,
          str(fl.get("L01", {}).get("target_area")))
    check("ink-less covered cell keeps target_area (P1 dropped the entry)",
          fl.get("L03", {}).get("target_area") == 75.0 and
          fl.get("L03", {}).get("breaks") == [],
          str(fl.get("L03")))
    check("grid preserved", pb.get("grid_x") == SEED_GRID_X,
          str(pb.get("grid_x")))
    # P2 advisory block in the import log: 10x10 slab cut at x=2 and x=5,
    # marker (1,5) claims the 20 m2 region; the rest is unmarked. FULL
    # lines - the L04 cell coincidentally also logs "2 region(s) ... 80.0
    # m2" (10+70), so a bare tail would not pin the [L01, L02] accounting.
    ilog = io.open(bsi.LOG_FILE, encoding="utf-8").read()
    check("advisory pour areas + unmarked accounting logged",
          "ADVISORY pour areas [L01, L02]: pour 1 ~ 20.0 m2 (20%); "
          "2 region(s) without a marker (~ 80.0 m2)" in ilog)
    check("advisory target ratio logged",
          "ADVISORY vs target_area 50.0 (L01): pour 1 = 40%" in ilog)
    # full lines again: offset value, PB-id numbering and the nearest-grid
    # pick (A for x=2, B for x=5) - this fixed point is what keeps the
    # _grid_offsets replica in step with split_pourbreaks.grid_offsets
    check("advisory grid offset (break 1 nearest A, offset 2.0)",
          "ADVISORY grid [L01, L02] break 1 seg 0: 2.0 off grid 'A' "
          "(x-axis)" in ilog)
    check("advisory grid offset (break 2 nearest B discriminated)",
          "ADVISORY grid [L01, L02] break 2 seg 0: 0.0 off grid 'B' "
          "(x-axis)" in ilog)

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

    # ---- P2 merge directives: MAKE regenerates, so the straddler above
    # is wiped along with the rest of the drawn state ----
    # a malformed file (valid JSON, wrong shape) degrades to a warning
    with io.open(bsg.MERGE_FILE, "w", encoding="utf-8") as fh:
        fh.write(u"[]")
    meta = bsg.main()
    check("malformed merge file tolerated", meta is not None and
          len(meta["cells"]) == 3,
          "cells={0}".format(None if meta is None else len(meta["cells"])))

    # an unknown floor name in a directive is ignored loudly, never fatal
    with io.open(bsg.MERGE_FILE, "w", encoding="utf-8") as fh:
        fh.write(u"{0}".format(json.dumps(
            {"docPath": TEST_DOCPATH, "merge": [["L01", "NOPE"]]})))
    meta = bsg.main()
    check("invalid merge directive tolerated", meta is not None and
          len(meta["cells"]) == 3,
          "cells={0}".format(None if meta is None else len(meta["cells"])))

    # valid directive: the L01+L02 TYP group and L03 collapse into one cell
    with io.open(bsg.MERGE_FILE, "w", encoding="utf-8") as fh:
        fh.write(u"{0}".format(json.dumps(
            {"docPath": TEST_DOCPATH, "merge": [["L01", "L03"]]})))
    meta = bsg.main()
    check("merge applied: 2 cells", meta is not None and
          len(meta["cells"]) == 2,
          "cells={0}".format(None if meta is None else len(meta["cells"])))
    if meta is None:
        return
    mcell = next((c for c in meta["cells"] if len(c["floors"]) == 3), None)
    check("merged cell = L01+L02+L03", mcell is not None and
          sorted(mcell["floors"].keys()) == ["L01", "L02", "L03"])
    check("merge recorded in meta (z order)",
          meta.get("merged") == [["L01", "L02", "L03"]],
          str(meta.get("merged")))

    # import the merged, untouched sheet: the representative's ink fans
    # out to EVERY member - L03's diverging break set is REPLACED
    data = bsi.main()
    check("merged-sheet import succeeded", data is not None)
    if data is not None:
        pb = load_pb()
        fl = pb["floors"]
        check("fan-out reaches merged L03 (2 breaks at its own z)",
              len(fl.get("L03", {}).get("breaks", [])) == 2 and
              all(b["z"] == 9.2 for b in fl["L03"]["breaks"]),
              str(fl.get("L03"))[:120])
        check("merged L03 keeps its target_area",
              fl.get("L03", {}).get("target_area") == 75.0)
        check("L04 untouched by the merge",
              len(fl.get("L04", {}).get("breaks", [])) == 1)
        check("preserved floor survives the merged import",
              fl.get("L99") == SEED_L99)
        check("target_area survives the merged import",
              fl.get("L01", {}).get("target_area") == 50.0)
        check("grid survives the merged import",
              pb.get("grid_x") == SEED_GRID_X)

    # chained directives sharing a member union transitively - the only
    # behavior union-find adds over per-set merging, so pin it: each
    # union step validates against the ACCUMULATED fingerprint
    with io.open(bsg.MERGE_FILE, "w", encoding="utf-8") as fh:
        fh.write(u"{0}".format(json.dumps(
            {"docPath": TEST_DOCPATH,
             "merge": [["L01", "L03"], ["L03", "L04"]]})))
    meta = bsg.main()
    check("chained directives union transitively (1 cell)",
          meta is not None and len(meta["cells"]) == 1 and
          sorted(meta["cells"][0]["floors"].keys()) ==
          ["L01", "L02", "L03", "L04"],
          str(None if meta is None else
              [sorted(c["floors"].keys()) for c in meta["cells"]]))
    check("chained merge recorded", meta is not None and
          meta.get("merged") == [["L01", "L02", "L03", "L04"]],
          str(None if meta is None else meta.get("merged")))

    # the MERGE REFUSED branch and the 15% threshold arm are structurally
    # unreachable with 4-vertex rectangles (max diff 8 == the hard floor),
    # so pin them with synthetic fingerprints: disjoint 64-vertex sets ->
    # diff 128 > max(8, int(0.15 * 128)) = 19
    refuse_lines = []
    ga = {"members": [{"name": "LA", "z": 1.0}],
          "fp": frozenset((i, 0) for i in range(64)), "merged": False}
    gb = {"members": [{"name": "LB", "z": 2.0}],
          "fp": frozenset((i, 1) for i in range(64)), "merged": False}
    kept = bsg.apply_merges([ga, gb], [["LA", "LB"]], refuse_lines.append)
    check("beyond-threshold merge REFUSED", len(kept) == 2 and
          not ga["merged"] and not gb["merged"] and
          any("MERGE REFUSED" in l for l in refuse_lines),
          "; ".join(refuse_lines)[:120])
    ok, diff, union = bsg._near_typ(ga["fp"], gb["fp"])
    check("threshold arm exercised (128 of 128)",
          not ok and diff == 128 and union == 128,
          "{0}/{1}".format(diff, union))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        failures.append("unhandled exception")
        lines.append(traceback.format_exc())
    # the staging folder is machine-wide: never leave a directive behind,
    # even on a crashed run - a leaked merge file would regroup future
    # MAKEs (and TEST_DOCPATH only protects SAVED models)
    try:
        os.remove(bsg.MERGE_FILE)
    except OSError:
        pass
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
