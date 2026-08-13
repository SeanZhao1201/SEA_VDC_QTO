# -*- coding: utf-8 -*-
"""Headless acceptance test for sideform_gen_rhino.py.

Metric scene, one slab per defect class:

  Wall_1   x[0,0.2]  y[0,10] z 0.0->3.0   bears under Slab_A's west edge
  Slab_A   x[0,10]   y[0,10] z 3.0->3.2   opening [4,6]x[4,6]; no POUR
  Slab_B1  x[10,16]  y[0,10] z 3.0->3.2   POUR=1 (emulated split piece)
  Slab_B2  x[16,22]  y[0,10] z 3.0->3.2   POUR=2 (same SOURCE_SLAB)

Expected classification:
  A west edge          -> bearing (wall top at soffit)   ~10 m suppressed
  A east  / B1 west    -> joint; B1 owns (pour 1 beats none) -> 1 bulkhead
  B1 east / B2 west    -> joint; B1 owns (pour 1 < 2)        -> 1 bulkhead
  A opening inner loop -> side forms (8 m x 0.2)
  everything else      -> side forms

Launched via  Rhino.exe /nosplash /notemplate
    /runscript="-_RunPythonScript <staged copy of this file>"
with FW_HEADLESS=1. Report: sf_test_report.txt in the staging folder.
"""
from __future__ import division, print_function

import json
import os
import sys

STAGE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "qto_fw_test")
sys.path.insert(0, STAGE)
REPORT = os.path.join(STAGE, "sf_test_report.txt")

import io


def write_report(text_lines):
    with io.open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(u"\n".join(u"{0}".format(l) for l in text_lines) + u"\n")


try:
    import math

    import Rhino
    from Rhino.Geometry import (BoundingBox, Brep, Point3d, Transform,
                                Vector3d)
    import formwork_gen_rhino as fw
    import sideform_gen_rhino as sf
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


def add_box(doc, layer, x0, x1, y0, y1, z0, z1, pour=None, source=None):
    idx = fw.ensure_layer(doc, [layer])
    brep = Brep.CreateFromBox(
        BoundingBox(Point3d(x0, y0, z0), Point3d(x1, y1, z1)))
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = idx
    if pour is not None:
        attr.SetUserString("POUR", str(pour))
    if source is not None:
        attr.SetUserString("SOURCE_SLAB", source)
    doc.Objects.AddBrep(brep, attr)
    return brep


def add_slab_with_hole(doc, layer, x0, x1, y0, y1, z0, z1, hole):
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
    es = Rhino.DocObjects.ObjectEnumeratorSettings()
    es.NormalObjects = True
    es.LockedObjects = True
    es.HiddenObjects = True
    return sum(1 for o in doc.Objects.GetObjectList(es) if o is not None)


def main():
    doc = Rhino.RhinoDoc.ActiveDoc
    doc.AdjustModelUnitSystem(Rhino.UnitSystem.Meters, False)
    doc.ModelAbsoluteTolerance = 0.001
    log("=== sideform_gen_rhino headless test (METRIC scene) ===")

    add_box(doc, "Wall_1", 0, 0.2, 0, 10, 0.0, 3.0)
    add_slab_with_hole(doc, "Slab_A", 0, 10, 0, 10, 3.0, 3.2, (4, 6, 4, 6))
    add_box(doc, "Slab_B1", 10, 16, 0, 10, 3.0, 3.2, pour=1, source="S1")
    # B2 goes in as a BLOCK INSTANCE — the QTO workflow ends with
    # Blockify, so target detection must explode instances in-memory
    b2 = Brep.CreateFromBox(
        BoundingBox(Point3d(16, 0, 3.0), Point3d(22, 10, 3.2)))
    b2_attr = Rhino.DocObjects.ObjectAttributes()
    b2_attr.LayerIndex = fw.ensure_layer(doc, ["Slab_B2"])
    b2_attr.SetUserString("POUR", "2")
    b2_attr.SetUserString("SOURCE_SLAB", "S1")
    b2_def = doc.InstanceDefinitions.Add(
        "B2_blk", "", Point3d.Origin, [b2], [b2_attr])
    doc.Objects.AddInstanceObject(b2_def, Transform.Identity)
    # stepped slab on its own floor: two levels joined by a web — the
    # lower level's panels must stop at ITS top (6.2), not bbox max (6.5)
    step = Brep.CreateBooleanUnion([
        Brep.CreateFromBox(BoundingBox(Point3d(0, 0, 6.0),
                                       Point3d(3.0, 6, 6.2))),
        Brep.CreateFromBox(BoundingBox(Point3d(2.9, 0, 6.0),
                                       Point3d(3.1, 6, 6.5))),
        Brep.CreateFromBox(BoundingBox(Point3d(3.1, 0, 6.3),
                                       Point3d(6, 6, 6.5)))], 0.001)
    step_attr = Rhino.DocObjects.ObjectAttributes()
    step_attr.LayerIndex = fw.ensure_layer(doc, ["Slab_C"])
    doc.Objects.AddBrep(step[0] if step else None, step_attr)
    # sloped slab on its own floor: out of scope, must skip LOUDLY
    ramp = Brep.CreateFromBox(
        BoundingBox(Point3d(8, 0, 9.4), Point3d(14, 6, 9.6)))
    ramp.Transform(Transform.Rotation(
        math.radians(3.0), Vector3d.YAxis, Point3d(11, 3, 9.5)))
    ramp_attr = Rhino.DocObjects.ObjectAttributes()
    ramp_attr.LayerIndex = fw.ensure_layer(doc, ["Slab_D"])
    doc.Objects.AddBrep(ramp, ramp_attr)
    doc.Strings.SetString("FloorElevations", json.dumps(
        {"3.2": "L1", "6.7": "L2", "9.5": "L3"}))
    n_original = count_objects(doc)

    params = {}
    plog = fw.Log()
    targets, obstacles = sf.find_targets_and_obstacles(
        doc, dict(sf.PARAMS, **params), plog)
    check("target detection (5 slabs incl. blockified, wall excluded)",
          len(targets) == 5, "found {0}".format(len(targets)))
    check("obstacle detection (wall + 5 slabs)", len(obstacles) == 6,
          "found {0}".format(len(obstacles)))
    pours = sorted([t[3] for t in targets if t[3] is not None])
    check("POUR user strings read (incl. from the block part)",
          pours == [1, 2], str(pours))

    result = sf.generate_sideforms(targets, obstacles, doc, params, plog)
    lines.extend(plog.lines)
    panels = result["panels"]
    fl = result["floors"].get("L1")
    check("floors L1+L2+L3 reported",
          sorted(result["floors"].keys()) == ["L1", "L2", "L3"],
          str(sorted(result["floors"].keys())))
    if fl is None:
        return

    # ---- bulkheads: one per joint, both owned by B1 (pour 1) ----
    bulk = [p for p in panels if p["type"] == "bulkhead"]
    check("exactly 2 bulkheads (one per joint)", len(bulk) == 2,
          "got {0}".format(len(bulk)))
    check("both bulkheads owned by pour 1",
          all(p["pour"] == 1 for p in bulk),
          str([p["pour"] for p in bulk]))
    check("bulkhead areas ~2.0 m2 each",
          all(approx(p["area_m2"], 2.0, 0.15) for p in bulk),
          str([p["area_m2"] for p in bulk]))
    check("2 joint runs ceded to the neighbour",
          result["stats"]["joint_theirs"] == 2,
          str(result["stats"]["joint_theirs"]))

    # ---- opening inner loop gets side forms ----
    opening = [p for p in panels if p["type"] == "side"
               and p["floor"] == "L1" and p["pour"] is None
               and 7.5 <= p["len_m"] <= 8.5]
    check("opening perimeter side form (~8 m run)", len(opening) == 1,
          str([p["len_m"] for p in panels if p["type"] == "side"
               and p["floor"] == "L1"]))

    # ---- suppression under the wall ----
    check("~10 m suppressed under the wall",
          9.4 <= fl["suppressed_len"] <= 11.2,
          str(fl["suppressed_len"]))
    check("bearing runs counted", result["stats"]["bearing_runs"] >= 1)

    # ---- floor totals & the area reconciliation invariant ----
    check("bulkhead area total ~4 m2",
          approx(fl["bulkhead_area"], 4.0, 0.3), str(fl["bulkhead_area"]))
    check("side area total ~12.3 m2",
          11.6 <= fl["side_area"] <= 13.0, str(fl["side_area"]))
    check("gross area 22.4 m2",
          approx(fl["gross_area"], 22.4, 0.2), str(fl["gross_area"]))
    net = fl["side_area"] + fl["bulkhead_area"]
    check("net <= gross", net <= fl["gross_area"] + 0.1,
          "{0} vs {1}".format(net, fl["gross_area"]))
    accounted = net + fl["shared_area"] + fl["suppressed_area"] \
        + fl["unclassified_area"]
    check("side+bulkhead+shared+suppressed+unclassified == gross",
          abs(accounted - fl["gross_area"]) <= 0.5,
          "{0} vs {1}".format(round(accounted, 2), fl["gross_area"]))

    # ---- panel geometry sanity ----
    check("L1 panels span soffit to top",
          all(approx(p["z0"], 3.0) and approx(p["z1"], 3.2)
              for p in panels if p["floor"] == "L1"))
    check("all panel breps are solid",
          all(p["brep"].IsSolid for p in panels),
          "{0} not solid".format(
              sum(1 for p in panels if not p["brep"].IsSolid)))
    # opening ring must be CLOSED: ideal ring = 0.39 m2 x 0.2 = 0.078 m3;
    # per-sample normals chamfer the four outer corners (~0.005 m3 total,
    # placeholder-correct), landing at ~0.073 — a slit C-annulus loses a
    # further full segment (~0.0707), so 0.072 discriminates
    op_brep = opening[0]["brep"] if opening else None
    check("opening ring panel is closed (volume ~0.073)",
          op_brep is not None and op_brep.GetVolume() >= 0.072,
          str(op_brep.GetVolume() if op_brep else None))
    # billed length == built geometry: total panel volume reconciles to
    # net area x panel thickness (runs tile the loop via transition
    # midpoints — no more one-segment gaps at class boundaries)
    vol_built = sum(p["brep"].GetVolume() for p in panels
                    if p["floor"] == "L1")
    vol_billed = (fl["side_area"] + fl["bulkhead_area"]) * 0.05
    check("panel volumes reconcile to billed net area x thickness",
          abs(vol_built - vol_billed) <= 0.10 * vol_billed,
          "{0:.4f} vs {1:.4f}".format(vol_built, vol_billed))

    # ---- stepped slab (L2): local tops, not the brep bbox top ----
    l2p = [p for p in panels if p["floor"] == "L2"]
    check("stepped slab produced panels", len(l2p) > 0,
          str(len(l2p)))
    check("no L2 panel exceeds its step's top (bbox top is 6.5)",
          all(p["z1"] <= 6.501 for p in l2p),
          str(sorted(set(round(p["z1"], 2) for p in l2p))))
    check("lower step panels stop at 6.2 (thickness 0.2, not 0.5)",
          any(approx(p["z0"], 6.0) and approx(p["z1"], 6.2, 0.01)
              for p in l2p),
          str([(p["z0"], p["z1"]) for p in l2p]))
    check("upper step panels 6.3->6.5",
          any(approx(p["z0"], 6.3) and approx(p["z1"], 6.5, 0.01)
              for p in l2p))

    # ---- sloped slab (L3): loud out-of-scope skip, not silent junk ----
    fl3 = result["floors"].get("L3", {})
    check("sloped soffit skipped loudly",
          result["stats"]["sloped_faces"] >= 1,
          str(result["stats"]["sloped_faces"]))
    check("sloped slab emits no panels",
          not [p for p in panels if p["floor"] == "L3"])
    check("sloped slab area kept in the books",
          fl3.get("unclassified_area", 0) > 0 and
          approx(fl3.get("unclassified_area"), fl3.get("gross_area"),
                 0.01))

    # ---- generate/purge round trip (additive, reversible) ----
    n_added = sf.write_to_doc(doc, result, dict(sf.PARAMS), plog)
    check("write_to_doc adds every panel", n_added == len(panels),
          "{0} vs {1}".format(n_added, len(panels)))
    check("document gained exactly the panels",
          count_objects(doc) == n_original + n_added)
    n_purged = sf.purge_sideforms(doc, plog)
    check("purge removes exactly the panels", n_purged == n_added,
          "{0} vs {1}".format(n_purged, n_added))
    check("purge restores object count",
          count_objects(doc) == n_original)

    # ---- export mode leaves the document untouched ----
    out3dm = os.path.join(STAGE, "sideform_test_export.3dm")
    if os.path.exists(out3dm):
        os.remove(out3dm)
    ok3 = sf.export_3dm(doc, result, out3dm, plog)
    check("export_3dm writes file", bool(ok3) and os.path.exists(out3dm))
    outjson = os.path.join(STAGE, "sideform_test_export.json")
    sf.dump_json(doc, result, outjson, plog)
    check("json handoff written", os.path.exists(outjson))
    data = json.loads(io.open(outjson, encoding="utf-8").read())
    check("json panels match", len(data["panels"]) == len(panels))
    check("json profiles (and ring holes) are closed",
          all(pn["profile"][0] == pn["profile"][-1] and
              (not pn.get("hole") or pn["hole"][0] == pn["hole"][-1])
              for pn in data["panels"]))
    check("opening panel exports a ring hole",
          any(pn.get("hole") for pn in data["panels"]))
    check("export leaves document untouched",
          count_objects(doc) == n_original)


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
        "ALL PASS" if not failures else "{0} FAILURES".format(
            len(failures))))
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
