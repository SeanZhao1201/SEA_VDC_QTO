# -*- coding: utf-8 -*-
"""Guard tests for formwork_ifc_from_json --into merge mode.

Pure CPython (ifcopenshell venv, no Rhino): builds tiny synthetic
take-off IFCs and asserts every merge guard fires with its intended
message, plus one happy-path mini merge and the STEP-header restamp.

    %LOCALAPPDATA%\\qto_fwenv\\Scripts\\python.exe test_ifc_merge.py

All scratch files land in a temp directory that is removed on success.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ifcopenshell
import ifcopenshell.util.element as ue

from formwork_ifc_from_json import Writer, convert

TMP = Path(tempfile.mkdtemp(prefix="qto_ifc_merge_test_"))
passed = 0


def ok(name, cond, detail=""):
    global passed
    if not cond:
        print("FAIL: {0}  {1}".format(name, detail))
        sys.exit(1)
    passed += 1
    print("PASS: {0}".format(name))


def mini_takeoff(path, storey_names, milli=True):
    w = Writer()
    for i, n in enumerate(storey_names):
        st = w.storey(n, float(i * 3))
        w._aggregate(w.building, [st])
    if not milli:
        # strip the MILLI prefix so the mm guard has something to catch
        for u in w.f.by_type("IfcSIUnit"):
            if u.UnitType == "LENGTHUNIT":
                u.Prefix = None
    w.f.write(str(path))


def mini_json(path, floor, slab_ids=None):
    Path(path).write_text(json.dumps({
        "panel_thickness": 0.05, "prop_size": 0.15,
        "levels": [{"floor": floor, "z": 3.0, "name": "Mini",
                    "regions": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                    "holes": [], "props": [],
                    "slab_ids": slab_ids or [],
                    "slabs": []}]}), encoding="utf-8")


def expect_exit(name, fn, *needles):
    try:
        fn()
    except SystemExit as e:
        msg = str(e)
        ok(name, all(n in msg for n in needles),
           "message was: {0}".format(msg))
        return
    print("FAIL: {0}  (no SystemExit raised)".format(name))
    sys.exit(1)


# 1. missing floor aborts loudly, names both vocabularies, writes nothing
mini_takeoff(TMP / "mini_L01.ifc", ["L01"])
mini_json(TMP / "mini_L99.json", "L99")
expect_exit("missing FLOOR aborts",
            lambda: convert(str(TMP / "mini_L99.json"),
                            str(TMP / "out1.ifc"),
                            into_path=str(TMP / "mini_L01.ifc")),
            "L99", "no IfcBuildingStorey")
ok("missing-floor abort wrote nothing", not (TMP / "out1.ifc").exists())

# 2. duplicate storey names abort
mini_takeoff(TMP / "mini_dup.ifc", ["L01", "L01"])
mini_json(TMP / "mini_L01.json", "L01")
expect_exit("duplicate storey Name aborts",
            lambda: convert(str(TMP / "mini_L01.json"),
                            str(TMP / "out2.ifc"),
                            into_path=str(TMP / "mini_dup.ifc")),
            "L01", "ambiguous")

# 3. non-mm take-off aborts
mini_takeoff(TMP / "mini_metre.ifc", ["L01"], milli=False)
expect_exit("non-mm take-off aborts",
            lambda: convert(str(TMP / "mini_L01.json"),
                            str(TMP / "out3.ifc"),
                            into_path=str(TMP / "mini_metre.ifc")),
            "millimetre")

# 4. non-IFC4 aborts
f2 = ifcopenshell.file(schema="IFC2X3")
f2.write(str(TMP / "mini_2x3.ifc"))
expect_exit("non-IFC4 take-off aborts",
            lambda: convert(str(TMP / "mini_L01.json"),
                            str(TMP / "out4.ifc"),
                            into_path=str(TMP / "mini_2x3.ifc")),
            "IFC4")

# 5. happy path: merges into the existing storey, take-off untouched
convert(str(TMP / "mini_L01.json"), str(TMP / "out5.ifc"),
        into_path=str(TMP / "mini_L01.ifc"))
m = ifcopenshell.open(str(TMP / "out5.ifc"))
ok("mini merge: still 1 storey", len(m.by_type("IfcBuildingStorey")) == 1)
ok("mini merge: 1 platform proxy",
   len(m.by_type("IfcBuildingElementProxy")) == 1)
asm = m.by_type("IfcElementAssembly")
ok("mini merge: 1 assembly", len(asm) == 1)
st = ue.get_container(asm[0])
ok("mini merge: assembly contained in L01",
   st is not None and st.Name == "L01")
ok("mini merge: single project", len(m.by_type("IfcProject")) == 1)
gids = [r.GlobalId for r in m.by_type("IfcRoot")]
ok("mini merge: no duplicate GlobalIds", len(gids) == len(set(gids)))

# 6. re-merging into a previous merge result aborts (silent-duplication
#    guard; the target passes every OTHER guard by construction)
expect_exit("re-merge into merged file aborts",
            lambda: convert(str(TMP / "mini_L01.json"),
                            str(TMP / "out6.ifc"),
                            into_path=str(TMP / "out5.ifc")),
            "previous merge result", "platform")

# 7. zero temp-works elements aborts instead of writing a take-off copy
(TMP / "mini_empty.json").write_text(json.dumps({
    "panel_thickness": 0.05, "prop_size": 0.15, "levels": []}),
    encoding="utf-8")
expect_exit("zero-element merge aborts",
            lambda: convert(str(TMP / "mini_empty.json"),
                            str(TMP / "out7.ifc"),
                            into_path=str(TMP / "mini_L01.ifc")),
            "ZERO")
ok("zero-element abort wrote nothing", not (TMP / "out7.ifc").exists())

# 8. dangling WALL/SLAB_GLOBALID references abort — the ONLY mechanical
#    catch for stale JSONs merged into a re-split take-off (floor names
#    repeat across model generations)
mini_json(TMP / "mini_dangle.json", "L01",
          slab_ids=["11111111-2222-3333-4444-555555555555"])
expect_exit("dangling guid reference aborts",
            lambda: convert(str(TMP / "mini_dangle.json"),
                            str(TMP / "out8.ifc"),
                            into_path=str(TMP / "mini_L01.ifc")),
            "do not resolve", "different model generations")
ok("dangling-ref abort wrote nothing", not (TMP / "out8.ifc").exists())

# 9. nonexistent --into path gets the curated abort, not a traceback
expect_exit("nonexistent --into aborts cleanly",
            lambda: convert(str(TMP / "mini_L01.json"),
                            str(TMP / "out9.ifc"),
                            into_path=str(TMP / "no_such_file.ifc")),
            "no_such_file.ifc")

# 10. merged output's STEP header names the merge, not the take-off tool
m5 = ifcopenshell.open(str(TMP / "out5.ifc"))
hdr = m5.header.file_name
ok("merged header name = output file", hdr.name == "out5.ifc", hdr.name)
ok("merged header names the merge script",
   "formwork_ifc_from_json" in (hdr.originating_system or ""),
   hdr.originating_system)
ok("merged header has a fresh timestamp",
   bool(hdr.time_stamp) and hdr.time_stamp.startswith("20"),
   hdr.time_stamp)

del m, m5, asm, st
shutil.rmtree(TMP, ignore_errors=True)
print("\nALL {0} MERGE GUARD ASSERTS PASS".format(passed))
