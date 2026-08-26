# -*- coding: utf-8 -*-
"""Headless acceptance test for jumpform_gen_rhino.py.

Metric scene, two independent core banks + reshore slabs:

  SLAB_SOG   x[-2,26] y[-2,12] z[-0.2,0]   obstacle only (SOG excluded)
  SLAB_PT    x[0,8]   y[0,10]  z[2.8,3.0]  L1 slab, stamped
  SLAB_PT    x[0,8]   y[0,10]  z[5.8,6.0]  L2 slab, unstamped
  WALL_CORE  ring 6x5 m, 0.4 thick, x[10,16] y[0,5]   bank A (2 lifts:
             z 0..3 -> P1, z 3..6 -> L1; lift 0 stamped)
  WALL_CORE  stub x[12,13] y[2,2.4] per storey        bank A too: a
             DISJOINT second solid per lift (plan-overlaps the ring) -
             pins lift-by-elevation grouping, not lift-per-solid
  WALL_CORE  box x[22,22.3] y[0,4]                    bank B (2 lifts;
             BOTH lifts share one stamp -> duplicate-guard warning)
  WALL_CORE  narrow-slot pair x[30,30.3]+[31.1,31.4] y[0,0.7], 1 lift
             (bank C, smallest plan area): the 0.8 m slot cannot take
             the 1.2 m retreat - facing-face UNLOCKED strips must
             refuse loudly, never bury in / teleport past the opposite
             wall
  WALL_1SIDED x[26,26.3] y[0,4] z 0..3     excluded (no 'core' in name)

Expected (the Waverly-verified convention, 2026-08-24): one straight
strip per wall FACE per state — NO horizontal decks, NO downward lap;
strips run from the lift base to form_top_drop (0.35) below the lift
top; UNLOCKED strips retreat 1.2 m along each face's own normal ON THE
AWAY SIDE even though roll_back exceeds the 0.4 m wall thickness (the
ambiguous regime: shaft-face strips retreat INTO the shaft, outer-face
strips OUTSIDE the core); 2 reshores per slab floor with the supported
floor's name and the slab's stamped identity.

Launched via  Rhino.exe /nosplash /notemplate
    /runscript="-_RunPythonScript <staged copy of this file>"
with FW_HEADLESS=1. Report: jf_test_report.txt in the staging folder.
"""
from __future__ import division, print_function

import json
import os
import sys

STAGE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "qto_fw_test")
sys.path.insert(0, STAGE)
REPORT = os.path.join(STAGE, "jf_test_report.txt")

import io


def write_report(text_lines):
    with io.open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(u"\n".join(u"{0}".format(l) for l in text_lines) + u"\n")


try:
    import Rhino
    from Rhino.Geometry import BoundingBox, Brep, Point3d
    import formwork_gen_rhino as fw
    import jumpform_gen_rhino as jfm
except Exception:
    import traceback
    write_report(["IMPORT FAILURE", traceback.format_exc()])
    raise

failures = []
lines = []

STAMP_A0 = "aaaaaaa1-0000-4000-8000-00000000a0a0"
STAMP_B = "bbbbbbb1-0000-4000-8000-00000000b0b0"
STAMP_SL = "ccccccc1-0000-4000-8000-00000000c0c0"


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


def add_box(doc, layer, x0, x1, y0, y1, z0, z1, stamp=None, pour=None):
    idx = fw.ensure_layer(doc, [layer])
    brep = Brep.CreateFromBox(
        BoundingBox(Point3d(x0, y0, z0), Point3d(x1, y1, z1)))
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = idx
    if stamp is not None:
        attr.SetUserString("QTO_STABLE_ID", stamp)
    if pour is not None:
        attr.SetUserString("POUR", str(pour))
    doc.Objects.AddBrep(brep, attr)
    return brep


def add_ring_wall(doc, layer, x0, x1, y0, y1, thick, z0, z1, stamp=None):
    """Closed ring core: union of four wall boxes."""
    parts = [
        Brep.CreateFromBox(BoundingBox(Point3d(x0, y0, z0),
                                       Point3d(x0 + thick, y1, z1))),
        Brep.CreateFromBox(BoundingBox(Point3d(x1 - thick, y0, z0),
                                       Point3d(x1, y1, z1))),
        Brep.CreateFromBox(BoundingBox(Point3d(x0, y0, z0),
                                       Point3d(x1, y0 + thick, z1))),
        Brep.CreateFromBox(BoundingBox(Point3d(x0, y1 - thick, z0),
                                       Point3d(x1, y1, z1)))]
    union = Brep.CreateBooleanUnion(parts, 0.001)
    brep = union[0] if union else parts[0]
    idx = fw.ensure_layer(doc, [layer])
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = idx
    if stamp is not None:
        attr.SetUserString("QTO_STABLE_ID", stamp)
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
    log("=== jumpform_gen_rhino headless test (METRIC scene) ===")

    add_box(doc, "SLAB_SOG", -2, 26, -2, 12, -0.2, 0.0)
    add_box(doc, "SLAB_PT", 0, 8, 0, 10, 2.8, 3.0, stamp=STAMP_SL,
            pour=1)                          # a split piece carries POUR
    add_box(doc, "SLAB_PT", 0, 8, 0, 10, 5.8, 6.0)
    add_ring_wall(doc, "WALL_CORE_8000PSI", 10, 16, 0, 5, 0.4,
                  0.0, 3.0, stamp=STAMP_A0)
    add_ring_wall(doc, "WALL_CORE_8000PSI", 10, 16, 0, 5, 0.4, 3.0, 6.0)
    # disjoint second solid per storey inside the shaft: same bank, same
    # lift - the lift ladder must group by base elevation, not by solid
    add_box(doc, "WALL_CORE_8000PSI", 12, 13, 2, 2.4, 0.0, 3.0)
    add_box(doc, "WALL_CORE_8000PSI", 12, 13, 2, 2.4, 3.0, 6.0)
    add_box(doc, "WALL_CORE_8000PSI", 22, 22.3, 0, 4, 0.0, 3.0,
            stamp=STAMP_B)
    add_box(doc, "WALL_CORE_8000PSI", 22, 22.3, 0, 4, 3.0, 6.0,
            stamp=STAMP_B)                       # duplicate stamp
    # narrow-slot pair: 0.8 m clear between the facing faces
    add_box(doc, "WALL_CORE_8000PSI", 30, 30.3, 0, 0.7, 0.0, 3.0)
    add_box(doc, "WALL_CORE_8000PSI", 31.1, 31.4, 0, 0.7, 0.0, 3.0)
    add_box(doc, "WALL_1SIDED", 26, 26.3, 0, 4, 0.0, 3.0)
    doc.Strings.SetString("FloorElevations", json.dumps(
        {"0.0": "P1", "3.0": "L1", "6.0": "L2"}))
    n_original = count_objects(doc)

    plog = fw.Log()
    p = dict(jfm.PARAMS)
    walls, slabs, obstacles = jfm.find_jumpform_inputs(doc, p, plog)
    check("wall detection (8 core solids, WALL_1SIDED excluded)",
          len(walls) == 8, "found {0}".format(len(walls)))
    check("slab detection (2 PT slabs, SOG excluded)", len(slabs) == 2,
          "found {0}".format(len(slabs)))
    check("obstacle detection (all 12 solids)", len(obstacles) == 12,
          "found {0}".format(len(obstacles)))

    def wall_at(x, z):
        for w in walls:
            if abs(w["bb"].Min.X - x) < 0.01 and \
                    abs(w["bb"].Min.Z - z) < 0.01:
                return w
        return None

    a0, a1 = wall_at(10, 0), wall_at(10, 3)
    b0, b1 = wall_at(22, 0), wall_at(22, 3)
    check("bank A lift 0 prefers its stamp",
          a0 is not None and a0["id"] == STAMP_A0, str(a0 and a0["id"]))
    check("unstamped wall falls back to its object id",
          a1 is not None and a1["id"] != STAMP_A0 and len(a1["id"]) == 36)
    # document enumeration order is Rhino's own (not add order), so WHICH
    # lift claims the shared stamp is not pinned — the contract is:
    # exactly one claims it, the other keeps its own object id, loudly
    b_ids = sorted([b0["id"], b1["id"]]) if (b0 and b1) else []
    check("duplicate stamp: exactly one holder claims it",
          len(b_ids) == 2 and b_ids.count(STAMP_B) == 1, str(b_ids))
    check("duplicate stamp: the other keeps its own object id",
          len(b_ids) == 2 and any(
              i != STAMP_B and len(i) == 36 for i in b_ids), str(b_ids))
    check("duplicate stamp warned loudly",
          any("share" in l and "QTO_STABLE_ID" in l for l in plog.lines))

    result = jfm.generate_jumpforms(walls, slabs, obstacles, doc, p, plog)
    lines.extend(plog.lines)
    jfs = result["jumpforms"]
    reshores = [r for r in result["reshores"]
                if r["status"] != "NOHIT_SKIPPED"]

    def bbw(j):
        return j["brep"].GetBoundingBox(True)

    # ---- banks: lettered by descending plan area; lifts grouped by
    # base ELEVATION (bank A has 4 solids but only 2 lifts) ----
    check("three banks (4 A-solids -> 2 lifts; slot pair = bank C)",
          result["banks"] == {"A": 2, "B": 2, "C": 1},
          str(result["banks"]))
    check("the ring core is bank A",
          all(j["bank"] == "A" for j in jfs if j["wall_id"] == STAMP_A0)
          and any(j["wall_id"] == STAMP_A0 for j in jfs))
    check("the straight core is bank B",
          all(j["bank"] == "B" for j in jfs if j["wall_id"] == STAMP_B)
          and any(j["wall_id"] == STAMP_B for j in jfs))

    # ---- both states exist per bank per lift ----
    for bank, fl in (("A", "P1"), ("A", "L1"), ("B", "P1"), ("B", "L1")):
        for state in ("LOCKED", "UNLOCKED"):
            check("panel {0}/{1}/{2} exists".format(bank, fl, state),
                  any(j["kind"] == "panel" and j["bank"] == bank and
                      j["floor"] == fl and j["state"] == state
                      for j in jfs))
    # per-face strips: ring 4+4 faces + stub 4 = 12/lift, x2 lifts = 24;
    # B box 4 faces x2 lifts = 8; slot pair 2x4 = 8 locked but only 6
    # unlocked (the two facing-face retreats do not fit the 0.8 m slot)
    check("strip counts (one per wall face per state)",
          result["stats"]["panels_locked"] == 40 and
          result["stats"]["panels_unlocked"] == 38,
          "L={0} U={1}".format(result["stats"]["panels_locked"],
                               result["stats"]["panels_unlocked"]))
    check("no bbox fallbacks on clean prisms",
          result["stats"]["bbox_fallbacks"] == 0,
          str(result["stats"]["bbox_fallbacks"]))
    check("exactly the 2 narrow-slot retreats skipped, loudly",
          result["stats"]["skipped_bands"] == 2 and
          any("does not fit" in l for l in plog.lines),
          str(result["stats"]["skipped_bands"]))
    cu = [j for j in jfs if j["bank"] == "C" and j["state"] == "UNLOCKED"]
    check("bank C: 6 unlocked strips, none inside the slot",
          len(cu) == 6 and not any(
              30.35 < (bbw(j).Min.X + bbw(j).Max.X) / 2.0 < 31.05
              for j in cu),
          str(sorted(round((bbw(j).Min.X + bbw(j).Max.X) / 2.0, 2)
                     for j in cu)))
    check("bank C: all 8 locked strips fit",
          sum(1 for j in jfs if j["bank"] == "C"
              and j["state"] == "LOCKED") == 8)
    check("no short runs dropped on clean prisms",
          result["stats"]["short_runs"] == 0,
          str(result["stats"]["short_runs"]))
    # the Waverly correction: a core jump form has NO horizontal decks
    check("no decks: every element is a vertical panel strip",
          all(j["kind"] == "panel" for j in jfs) and
          all((j["brep"].GetBoundingBox(True).Max.Z -
               j["brep"].GetBoundingBox(True).Min.Z) >= 2.0
              for j in jfs),
          "{0} elements".format(len(jfs)))

    # ---- bank A ring: a shaft-side panel exists inside the core ----
    shaft = [j for j in jfs if j["kind"] == "panel" and j["bank"] == "A"
             and j["state"] == "LOCKED"
             and j["brep"].GetBoundingBox(True).Min.X > 10.35
             and j["brep"].GetBoundingBox(True).Max.X < 15.65]
    check("bank A shaft panel (interior form) exists", len(shaft) >= 1,
          "{0} interior panels".format(len(shaft)))

    # ---- the AMBIGUOUS regime (roll_back > wall thickness): unlocked
    # shaft strips must land INSIDE the shaft, outer strips OUTSIDE ----
    # a shaft-face strip retreated INTO the shaft stays inside the
    # shaft region and keeps its ~5.2 face length; the stub's strips
    # are <= 1.0 long, and a wrong-side retreat lands outside the
    # region entirely
    shaft_u = [j for j in jfs if j["kind"] == "panel"
               and j["bank"] == "A" and j["state"] == "UNLOCKED"
               and bbw(j).Min.X > 10.35 and bbw(j).Max.X < 15.65
               and bbw(j).Min.Y > 0.35 and bbw(j).Max.Y < 4.65
               and (bbw(j).Max.X - bbw(j).Min.X) >= 3.5]
    check("unlocked SHAFT band rolled back INTO the shaft "
          "(roll_back > wall thickness)", len(shaft_u) >= 1,
          "{0} candidates".format(len(shaft_u)))
    outer_u = [j for j in jfs if j["kind"] == "panel"
               and j["bank"] == "A" and j["state"] == "UNLOCKED"
               and bbw(j).Min.X <= 9.35]
    check("unlocked OUTER band rolled back OUTSIDE the core",
          len(outer_u) >= 1, "{0} candidates".format(len(outer_u)))

    # ---- locked hugs each face, unlocked retreats 1.2 per face ----
    bl = [j for j in jfs if j["bank"] == "B"
          and j["state"] == "LOCKED" and j["floor"] == "P1"]
    bu = [j for j in jfs if j["bank"] == "B"
          and j["state"] == "UNLOCKED" and j["floor"] == "P1"]
    check("bank B: 4 locked strips (one per face)", len(bl) == 4,
          str(len(bl)))
    check("bank B: 4 unlocked strips", len(bu) == 4, str(len(bu)))
    if bl and bu:
        check("locked strips hug the wall (within panel_thickness)",
              all(bbw(j).Min.X >= 21.65 and bbw(j).Max.X <= 22.65 and
                  bbw(j).Min.Y >= -0.35 and bbw(j).Max.Y <= 4.35
                  for j in bl),
              str([(round(bbw(j).Min.X, 2), round(bbw(j).Max.X, 2))
                   for j in bl]))
        check("unlocked west strip retreated ~1.2 off the face",
              any(bbw(j).Max.X <= 20.85 for j in bu),
              str(sorted(round(bbw(j).Min.X, 2) for j in bu)))
        check("unlocked east strip retreated the other way",
              any(bbw(j).Min.X >= 23.45 for j in bu))
        check("strips run lift base to form_top_drop below the top",
              all(approx(bbw(j).Min.Z, 0.0, 0.01) and
                  approx(bbw(j).Max.Z, 2.65, 0.01)
                  for j in bl + bu),
              str(sorted(set((round(bbw(j).Min.Z, 2),
                              round(bbw(j).Max.Z, 2))
                             for j in bl + bu))))

    # ---- reshores: supported floor, stamped slab identity ----
    check("reshores generated (2 per slab floor)",
          len(reshores) == 4, str(len(reshores)))
    check("reshore floors are the SUPPORTED floors",
          sorted(set(r["floor"] for r in reshores)) == ["L1", "L2"],
          str(sorted(set(r["floor"] for r in reshores))))
    check("reshore heights ~2.8 (soffit to slab-below top)",
          all(approx(r["height"], 2.8, 0.05) for r in reshores),
          str([round(r["height"], 3) for r in reshores]))
    check("all reshores OK status",
          all(r["status"] == "OK" for r in reshores))
    l1r = [r for r in reshores if r["floor"] == "L1"]
    check("L1 reshores carry the slab's STAMPED identity",
          l1r and all(r["slab_ids"] == [STAMP_SL] for r in l1r),
          str([r["slab_ids"] for r in l1r]))
    l2r = [r for r in reshores if r["floor"] == "L2"]
    check("L2 reshores fall back to the slab's object id",
          l2r and all(len(r["slab_ids"]) == 1 and
                      r["slab_ids"] != [STAMP_SL] for r in l2r))
    # zone attribution (2026-08-24): reshores inherit the POUR of the
    # slab piece they support; un-poured slabs leave it unset
    check("L1 reshores inherit the slab's POUR",
          l1r and all(r.get("pour") == "1" for r in l1r),
          str([r.get("pour") for r in l1r]))
    check("L2 reshores carry no POUR (unsplit slab)",
          l2r and all(not r.get("pour") for r in l2r))

    # ---- solids sanity ----
    check("all jump-form breps are solid",
          all(j["brep"].IsSolid for j in jfs),
          "{0} not solid".format(
              sum(1 for j in jfs if not j["brep"].IsSolid)))

    # ---- generate/purge round trip (additive, reversible) ----
    n_added = jfm.write_to_doc(doc, result, p, plog)
    check("write_to_doc adds every element",
          n_added == len(jfs) + len(reshores),
          "{0} vs {1}+{2}".format(n_added, len(jfs), len(reshores)))
    check("document gained exactly the elements",
          count_objects(doc) == n_original + n_added)
    fw.purge_formwork(doc, plog, ("jumpform", "reshore"))
    check("purge restores object count",
          count_objects(doc) == n_original,
          "{0} vs {1}".format(count_objects(doc), n_original))

    # ---- export mode leaves the document untouched ----
    out3dm = os.path.join(STAGE, "jumpform_test_export.3dm")
    if os.path.exists(out3dm):
        os.remove(out3dm)
    ok3 = jfm.export_3dm(doc, result, out3dm, plog)
    check("export_3dm writes file", bool(ok3) and os.path.exists(out3dm))
    outjson = os.path.join(STAGE, "jumpform_test_export.json")
    jfm.dump_json(doc, result, outjson, plog)
    check("json handoff written", os.path.exists(outjson))
    with io.open(outjson, encoding="utf-8") as fh:
        data = json.loads(fh.read())
    check("json jumpform entries match", len(data["jumpforms"]) == len(jfs))
    check("json reshores match", len(data["reshores"]) == len(reshores))
    check("json profiles closed",
          all(el["profile"][0] == el["profile"][-1]
              for el in data["jumpforms"]))
    check("json states/floors/banks complete",
          all(el["state"] in ("LOCKED", "UNLOCKED") and el["floor"] and
              el["bank"] and el["wall_id"] for el in data["jumpforms"]))
    check("json strips carry no holes (plain rectangles)",
          not any(el.get("hole") for el in data["jumpforms"]))
    check("json banks reported",
          data["banks"] == {"A": 2, "B": 2, "C": 1}, str(data["banks"]))
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
