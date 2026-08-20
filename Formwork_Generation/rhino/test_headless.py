# -*- coding: utf-8 -*-
"""Headless acceptance test for formwork_gen_rhino.py.

Builds a synthetic podium scene in the ACTIVE (empty, throwaway) document that
reproduces every defect class found in the Bellwether IFC analysis, runs the
generator core, and asserts per-prop numbers. Launched by run_headless_test.ps1
via  Rhino.exe /nosplash /notemplate /runscript="-_RunPythonScript <this file>".
Exits Rhino only when the FW_HEADLESS env var is set, so it is also safe to run
manually inside an open Rhino (on a NEW empty file — it adds objects!).

Scene (metres, doc units set to metres):
  B1  slab x[0,12.6] y[0,10]  z 0.0 -> 0.2   (partial basement)
  L1A slab x[0,8]    y[0,10]  z 3.0 -> 3.2   (upper step)
  L1B slab x[8,14]   y[0,10]  z 2.4 -> 2.6   (dropped step, 0.6 lower)
  L2  slab x[0,20]   y[0,10]  z 6.0 -> 6.2   with 2x2 opening x[4,6] y[2,4]
  beam under L2      x[0,14] y[5.6,6.4] z 5.5 -> 6.0 (downstand, same level)
  column             x[8.8,9.2] y[2.8,3.2]   z 2.6 -> 6.0 (under L2)
  x in [14,20]: L2 has NO slab below at all (double-height void over nothing -> grade).
"""
from __future__ import division, print_function

import json
import os
import sys

STAGE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "qto_fw_test")
sys.path.insert(0, STAGE)
REPORT = os.path.join(STAGE, "test_report.txt")

with open(os.path.join(STAGE, "boot.txt"), "w") as _fh:
    _fh.write("script started\n")

import io


def write_report(text_lines):
    with io.open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(u"\n".join(u"{0}".format(l) for l in text_lines) + u"\n")


try:
    import Rhino
    from Rhino.Geometry import BoundingBox, Brep, Point3d
    import formwork_gen_rhino as fw
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


def approx(a, b, tol=1e-3):
    return a is not None and b is not None and abs(a - b) <= tol


def add_box(doc, layer, x0, x1, y0, y1, z0, z1):
    idx = fw.ensure_layer(doc, [layer])
    brep = Brep.CreateFromBox(
        BoundingBox(Point3d(x0, y0, z0), Point3d(x1, y1, z1)))
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = idx
    doc.Objects.AddBrep(brep, attr)
    return brep


def add_slab_with_hole(doc, layer, x0, x1, y0, y1, z0, z1, hole):
    """Box slab minus a rectangular through-hole (boolean difference)."""
    idx = fw.ensure_layer(doc, [layer])
    slab = Brep.CreateFromBox(
        BoundingBox(Point3d(x0, y0, z0), Point3d(x1, y1, z1)))
    hx0, hx1, hy0, hy1 = hole
    cutter = Brep.CreateFromBox(
        BoundingBox(Point3d(hx0, hy0, z0 - 1), Point3d(hx1, hy1, z1 + 1)))
    res = Brep.CreateBooleanDifference(slab, cutter, 0.001)
    brep = res[0] if res else slab
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = idx
    doc.Objects.AddBrep(brep, attr)
    return brep


def count_objects(doc):
    """Live (non-deleted) objects incl. locked+hidden — NOT doc.Objects.Count,
    which can include undo-held deleted objects."""
    es = Rhino.DocObjects.ObjectEnumeratorSettings()
    es.NormalObjects = True
    es.LockedObjects = True
    es.HiddenObjects = True
    return sum(1 for o in doc.Objects.GetObjectList(es) if o is not None)


def props_by_status(level, status):
    return [p for p in level["props"] if p["status"] == status]


def find_level(result, z):
    for lv in result["levels"]:
        if approx(lv["z"], z):
            return lv
    return None


def main():
    doc = Rhino.RhinoDoc.ActiveDoc
    doc.AdjustModelUnitSystem(Rhino.UnitSystem.Meters, False)
    doc.ModelAbsoluteTolerance = 0.001
    log("=== formwork_gen_rhino headless test ===")

    # ---- build scene ----
    add_box(doc, "Slab_B1", 0, 12.6, 0, 10, 0.0, 0.2)
    add_box(doc, "Slab_L1A", 0, 8, 0, 10, 3.0, 3.2)
    add_box(doc, "Slab_L1B", 8, 14, 0, 10, 2.4, 2.6)
    add_slab_with_hole(doc, "Slab_L2", 0, 20, 0, 10, 6.0, 6.2, (4, 6, 2, 4))
    add_box(doc, "Beam_L2", 0, 14, 5.6, 6.4, 5.5, 6.0)
    add_box(doc, "Column_1", 8.8, 9.2, 2.8, 3.2, 2.6, 6.0)
    n_original = count_objects(doc)
    doc.Strings.SetString("FloorElevations", json.dumps(
        {"0.2": "B1", "2.6": "L1B", "3.2": "L1A", "6.2": "L2"}))

    params = {"grade_z": -0.5}
    plog = fw.Log()
    slabs, obstacles = fw.find_slabs_and_obstacles(
        doc, dict(fw.PARAMS, **params), plog)
    check("slab detection", len(slabs) == 4,
          "found {0}, want 4".format(len(slabs)))
    check("obstacle detection", len(obstacles) == 6,
          "found {0}, want 6".format(len(obstacles)))

    result = fw.generate_formwork(slabs, obstacles, doc, params, plog)
    lines.extend(plog.lines)

    # ---- level structure ----
    check("4 soffit levels", len(result["levels"]) == 4,
          "got {0}".format(len(result["levels"])))
    lv_b1 = find_level(result, 0.0)
    lv_l1b = find_level(result, 2.4)
    lv_l1a = find_level(result, 3.0)
    lv_l2 = find_level(result, 6.0)
    check("levels at 0.0/2.4/3.0/6.0",
          None not in (lv_b1, lv_l1b, lv_l1a, lv_l2))
    if None in (lv_b1, lv_l1b, lv_l1a, lv_l2):
        return
    check("floor names from doc strings",
          [lv_b1["floor"], lv_l1b["floor"], lv_l1a["floor"],
           lv_l2["floor"]] == ["B1", "L1B", "L1A", "L2"],
          str([lv["floor"] for lv in result["levels"]]))

    # ---- L2: the interesting level ----
    ok = props_by_status(lv_l2, "OK")
    gt = props_by_status(lv_l2, "GROUNDED_TALL")
    check("L2 OK prop count", len(ok) == 6, "got {0}, want 6".format(len(ok)))
    check("L2 grounded-tall count (void over nothing)", len(gt) == 6,
          "got {0}, want 6".format(len(gt)))
    check("L2 bearing skips (4 beam + 1 column)",
          lv_l2["n_skipped_bearing"] == 5,
          "got {0}".format(lv_l2["n_skipped_bearing"]))
    for p in ok:
        want = 2.75 if p["x"] <= 8 else 3.35   # step in the L1 slab below!
        check("L2 prop ({0:.0f},{1:.0f}) h={2}".format(p["x"], p["y"], want),
              approx(p["height"], want),
              "got {0}".format(p["height"]))
        want_foot = 3.2 if p["x"] <= 8 else 2.6
        check("L2 prop ({0:.0f},{1:.0f}) foot={2}".format(
            p["x"], p["y"], want_foot), approx(p["foot_z"], want_foot))
    for p in gt:
        check("L2 grounded prop ({0:.0f},{1:.0f}) on grade".format(
            p["x"], p["y"]),
            approx(p["foot_z"], -0.5) and approx(p["height"], 6.45)
            and p["x"] >= 15)
    hole_pts = [p for p in lv_l2["props"] if 4 <= p["x"] <= 6
                and 2 <= p["y"] <= 4]
    check("no props in/at L2 opening", len(hole_pts) == 0)
    check("per-point heights differ within one level (core fix)",
          any(approx(p["height"], 2.75) for p in ok)
          and any(approx(p["height"], 3.35) for p in ok))

    # ---- platforms ----
    check("L2 single unioned platform", len(lv_l2["platforms"]) == 1,
          "got {0}".format(len(lv_l2["platforms"])))
    if lv_l2["platforms"]:
        plat = lv_l2["platforms"][0]
        bb = plat.GetBoundingBox(True)
        check("L2 platform top at soffit", approx(bb.Max.Z, 6.0))
        check("L2 platform 50mm thick", approx(bb.Min.Z, 5.95))
        check("L2 platform overhang bbox",
              approx(bb.Min.X, -0.1, 0.02) and approx(bb.Max.X, 20.1, 0.02))
        vol = plat.GetVolume()
        check("L2 platform volume (hole kept)", approx(vol, 10.102, 0.05),
              "got {0:.3f}, want 10.102 (no-hole would be 10.302)".format(vol))
        check("L2 platform closed solid", plat.IsSolid)

    # ---- other levels ----
    for lv, n_want, h_want, foot_want, label in (
            (lv_l1a, 6, 2.75, 0.2, "L1A"),
            (lv_l1b, 6, 2.15, 0.2, "L1B")):
        okp = props_by_status(lv, "OK")
        check("{0} prop count".format(label), len(okp) == n_want,
              "got {0}, want {1}".format(len(okp), n_want))
        check("{0} all heights {1}".format(label, h_want),
              all(approx(p["height"], h_want) for p in okp))
        check("{0} all feet {1}".format(label, foot_want),
              all(approx(p["foot_z"], foot_want) for p in okp))
    gb1 = props_by_status(lv_b1, "GROUNDED")
    check("B1 grounded prop count", len(gb1) == 12,
          "got {0}, want 12".format(len(gb1)))
    check("B1 grounded heights 0.45",
          all(approx(p["height"], 0.45) for p in gb1))

    # ---- grade_z=None variant: no-hit props become flagged skips ----
    r2 = fw.generate_formwork(slabs, obstacles, doc, {"grade_z": None},
                              fw.Log())
    check("grade_z=None -> 18 NOHIT_SKIPPED",
          r2["stats"]["NOHIT_SKIPPED"] == 18,
          "got {0}".format(r2["stats"]["NOHIT_SKIPPED"]))
    check("grade_z=None -> no grounded props",
          r2["stats"]["GROUNDED"] == 0 and r2["stats"]["GROUNDED_TALL"] == 0)

    # ---- document round-trip: additive only, purge removes exactly ours ----
    n_added = fw.write_to_doc(doc, result, dict(fw.PARAMS, **params), plog)
    want_added = sum(len(lv["platforms"])
                     + len([p for p in lv["props"]
                            if p["status"] != "NOHIT_SKIPPED"])
                     for lv in result["levels"])
    check("write_to_doc object count", n_added == want_added,
          "added {0}, want {1}".format(n_added, want_added))
    n_now = count_objects(doc)
    check("document gained exactly the formwork",
          n_now == n_original + n_added,
          "doc has {0}, want {1}".format(n_now, n_original + n_added))
    fw_layers = [l for l in doc.Layers if l is not None and not l.IsDeleted
                 and (l.FullPath == fw.FW_ROOT
                      or l.FullPath.startswith(fw.FW_ROOT + "::"))]
    check("formwork layers created", len(fw_layers) > 0,
          "{0} layers".format(len(fw_layers)))
    n_log = len(plog.lines)
    fw.purge_formwork(doc, plog)
    lines.extend(plog.lines[n_log:])
    n_after = count_objects(doc)
    check("purge restores object count", n_after == n_original,
          "doc has {0}, want {1}".format(n_after, n_original))
    fw_layers = [l for l in doc.Layers if l is not None and not l.IsDeleted
                 and (l.FullPath == fw.FW_ROOT
                      or l.FullPath.startswith(fw.FW_ROOT + "::"))]
    check("purge removes formwork layers", len(fw_layers) == 0,
          "{0} left".format(len(fw_layers)))

    # ---- File3dm export (document untouched) ----
    n_before = count_objects(doc)
    out3dm = os.path.join(STAGE, "formwork_test_export.3dm")
    if os.path.exists(out3dm):
        os.remove(out3dm)
    ok3 = fw.export_3dm(doc, result, out3dm, plog)
    check("export_3dm writes file", bool(ok3) and os.path.exists(out3dm))
    check("export leaves document untouched",
          count_objects(doc) == n_before)


try:
    try:
        main()
    except Exception as exc:
        import traceback
        failures.append("EXCEPTION")
        log("EXCEPTION: {0}".format(exc))
        log(traceback.format_exc())

    log("=" * 50)
    log("RESULT: {0}".format(
        "ALL PASS" if not failures else "{0} FAILURES".format(len(failures))))
    for f in failures:
        log("  FAILED: " + f)
    write_report(lines)
finally:
    try:
        Rhino.RhinoDoc.ActiveDoc.Modified = False
    except Exception:
        pass
    if os.environ.get("FW_HEADLESS") == "1":
        try:
            Rhino.RhinoApp.Exit()
        except Exception:
            Rhino.RhinoApp.RunScript("_-Exit", False)
