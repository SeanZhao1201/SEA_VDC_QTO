# -*- coding: utf-8 -*-
"""Headless acceptance test for the pour-break authoring pipeline (v2).

Builds a synthetic METRIC scene (proving the feet-only era is over) in
the active throwaway document, then exercises the whole authored path:

    draw curves + dots on _POURBREAK  ->  harvest (read-only)
    ->  wipe (the checkup hazard)     ->  restore  ->  re-harvest
    ->  byte-identical JSON            ->  split_document  ->  asserts

Scene (metres) — every floor targets a defect class from the 2026-08-13
adversarial review:
  Slab_SOG  x[0,20] y[0,10] z 0.0->0.2   excluded by layer filter
  Slab_L1   x[0,20] y[0,10] z 3.0->3.2   under-drawn diagonal with a
                                          snap-noise micro tail (end-
                                          direction sampling); curve is
                                          HIDDEN (harvest must include
                                          hidden objects); sublayer bound
  Slab_L2   x[0,10] y[0,10] z 6.0->6.2   jogged polyline + dots, LOCKED
                                          layer (force_delete), column
                                          0.8 m away (support flag)
  Slab_L3   x[0,5]  y[0,5]  z 9.0->9.2   ARC break: curve_type "curve"
                                          must survive wipe/restore
  Slab_L4   x[0,5]  y[0,5]  z 12.0->12.2 0.08%-volume corner pour WITH a
                                          dot: sliver guard waived
  Slab_L5   x[0,10] y[0,10] z 15.0->15.2 U-notch break: concave piece
                                          whose centroid escapes it
                                          (dot containment, not centroid)
  Slab_L6   x[0,20] y[0,10] z 18.0->18.2 re-entrant break crossing the
                                          slab twice -> 3 regions, dots
                                          1/2/3 all exact
  Slab_L7   x[0,3]  y[0,3]  z 21.0->21.2 no breaks ("no break for floor")
  Column_1  x[4.8,5.2] y[7.8,8.2] z 3.2->6.0

Launched via  Rhino.exe /nosplash /notemplate
    /runscript="-_RunPythonScript <staged copy of this file>"
with FW_HEADLESS=1. Report: pb_test_report.txt in the staging folder.
"""
from __future__ import division, print_function

import json
import os
import sys

STAGE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "qto_fw_test")
sys.path.insert(0, STAGE)
REPORT = os.path.join(STAGE, "pb_test_report.txt")

import io


def write_report(text_lines):
    with io.open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(u"\n".join(u"{0}".format(l) for l in text_lines) + u"\n")


try:
    import Rhino
    from Rhino.Geometry import (Arc, ArcCurve, BoundingBox, Brep, Point3d,
                                PolylineCurve, TextDot)
    import formwork_gen_rhino as fw
    import pourbreak_harvest as pbh
    import pourbreak_restore as pbr
    import split_pourbreaks as pbs
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


def add_break_curve(doc, layer_names, pts):
    idx = fw.ensure_layer(doc, layer_names)
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = idx
    return doc.Objects.AddCurve(
        PolylineCurve([Point3d(p[0], p[1], p[2]) for p in pts]), attr)


def add_arc_curve(doc, layer_names, p1, p2, p3):
    idx = fw.ensure_layer(doc, layer_names)
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = idx
    arc = Arc(Point3d(*p1), Point3d(*p2), Point3d(*p3))
    return doc.Objects.AddCurve(ArcCurve(arc), attr)


def add_dot(doc, layer_names, text, x, y, z):
    idx = fw.ensure_layer(doc, layer_names)
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = idx
    doc.Objects.AddTextDot(TextDot(text, Point3d(x, y, z)), attr)


def count_objects(doc):
    es = Rhino.DocObjects.ObjectEnumeratorSettings()
    es.NormalObjects = True
    es.LockedObjects = True
    es.HiddenObjects = True
    return sum(1 for o in doc.Objects.GetObjectList(es) if o is not None)


def scrub(data):
    """Drop volatile fields (object GUIDs, doc path) for round-trip diff."""
    out = json.loads(json.dumps(data))    # deep copy
    out.pop("source", None)
    for cfg in out.get("floors", {}).values():
        for brk in cfg.get("breaks", []):
            brk.pop("provenance", None)
        for mk in cfg.get("pour_markers", []):
            mk.pop("provenance", None)
    return out


def pieces_with_pour(doc, floor):
    out = []
    for obj in doc.Objects:
        if obj is None or obj.Attributes is None:
            continue
        pour = obj.Attributes.GetUserString("POUR")
        if pour is None or obj.Attributes.GetUserString("POUR_FLOOR") != \
                floor:
            continue
        geom = obj.Geometry
        if isinstance(geom, Brep):
            out.append((int(pour), geom, obj))
    return out


def piece_at(pieces, x, y, z, tol=1e-3):
    for pour, brep, obj in pieces:
        if brep.IsPointInside(Point3d(x, y, z), tol, False):
            return pour, brep
    return None, None


def lock_layer(doc, name):
    for l in doc.Layers:
        if l is not None and not l.IsDeleted and l.Name == name:
            l.IsLocked = True
            return True
    return False


def main():
    doc = Rhino.RhinoDoc.ActiveDoc
    doc.AdjustModelUnitSystem(Rhino.UnitSystem.Meters, False)
    doc.ModelAbsoluteTolerance = 0.001
    log("=== pour-break authoring headless test (METRIC scene) ===")

    # ---- build scene ----
    add_box(doc, "Slab_SOG", 0, 20, 0, 10, 0.0, 0.2)
    add_box(doc, "Slab_L1", 0, 20, 0, 10, 3.0, 3.2)
    add_box(doc, "Slab_L2", 0, 10, 0, 10, 6.0, 6.2)
    add_box(doc, "Slab_L3", 0, 5, 0, 5, 9.0, 9.2)
    add_box(doc, "Slab_L4", 0, 5, 0, 5, 12.0, 12.2)
    add_box(doc, "Slab_L5", 0, 10, 0, 10, 15.0, 15.2)
    add_box(doc, "Slab_L6", 0, 20, 0, 10, 18.0, 18.2)
    add_box(doc, "Slab_L7", 0, 3, 0, 3, 21.0, 21.2)
    add_box(doc, "Column_1", 4.8, 5.2, 7.8, 8.2, 3.2, 6.0)
    doc.Strings.SetString("FloorElevations", json.dumps(
        {"0.2": "P1", "3.2": "L1", "6.2": "L2", "9.2": "L3",
         "12.2": "L4", "15.2": "L5", "18.2": "L6", "21.2": "L7"}))

    # L1: under-drawn diagonal + 0.0007-long snap-noise tail, then HIDDEN
    l1_id = add_break_curve(doc, [pbh.PB_ROOT, "L1"],
                            [(2, 1, 3.2), (18, 9, 3.2),
                             (18.0005, 8.9995, 3.2)])
    doc.Objects.Hide(l1_id, True)
    # L2: jogged polyline + dots on the root layer (elevation-bound)
    add_break_curve(doc, [pbh.PB_ROOT],
                    [(5, -0.5, 6.2), (5, 4, 6.2), (6, 6, 6.2),
                     (6, 10.5, 6.2)])
    add_dot(doc, [pbh.PB_ROOT], "1", 2, 2, 6.2)
    add_dot(doc, [pbh.PB_ROOT], "2", 9, 8, 6.2)
    lock_layer(doc, "Slab_L2")
    # L3: arc break (curve_type "curve")
    add_arc_curve(doc, [pbh.PB_ROOT],
                  (2.5, -1, 9.2), (3.5, 2.5, 9.2), (2.5, 6, 9.2))
    # L4: authored 0.08%-volume corner pour (cut line x+y=0.2), dot
    # strictly inside it
    add_break_curve(doc, [pbh.PB_ROOT],
                    [(-0.2, 0.4, 12.2), (0.4, -0.2, 12.2)])
    add_dot(doc, [pbh.PB_ROOT], "1", 3, 3, 12.2)
    add_dot(doc, [pbh.PB_ROOT], "2", 0.05, 0.05, 12.2)
    # L5: U-notch routed around an opening — the lower piece is concave
    # and its centroid (5, ~2.36) lies OUTSIDE it (inside the notch)
    add_break_curve(doc, [pbh.PB_ROOT],
                    [(-0.5, 5, 15.2), (4, 5, 15.2), (4, 2, 15.2),
                     (6, 2, 15.2), (6, 5, 15.2), (10.5, 5, 15.2)])
    add_dot(doc, [pbh.PB_ROOT], "1", 1, 2, 15.2)
    add_dot(doc, [pbh.PB_ROOT], "2", 5, 7, 15.2)
    # L6: re-entrant polyline crossing the slab twice -> 3 regions
    add_break_curve(doc, [pbh.PB_ROOT],
                    [(0, 3, 18.2), (21, 3, 18.2), (21, 7, 18.2),
                     (0, 7, 18.2)])
    add_dot(doc, [pbh.PB_ROOT], "1", 10, 1, 18.2)
    add_dot(doc, [pbh.PB_ROOT], "2", 10, 5, 18.2)
    add_dot(doc, [pbh.PB_ROOT], "3", 10, 9, 18.2)
    n_before_harvest = count_objects(doc)
    N_PB = 6 + 9        # 6 break curves + 9 dots

    # ---- pure-function unit tests ----
    v1 = {"units": "ft",
          "grid_x": {"A": 10.0},
          "floors": {"L01": {
              "cuts": [{"dir": "NS", "pos_ft": 5.0,
                        "span_ft": [1.0, 9.0], "note": "n"}],
              "pdf_sf": 123.0, "pour1_centroid_ft": [2.0, 3.0]}}}
    v2 = pbs.upconvert_v1(v1)
    c = v2["floors"]["L01"]
    check("upconvert: version 2", v2.get("version") == 2)
    check("upconvert: cut -> 2-pt polyline",
          c["breaks"][0]["polyline"] == [[5.0, 1.0], [5.0, 9.0]])
    check("upconvert: centroid -> pour-1 marker",
          c["pour_markers"][0]["pour"] == 1 and
          c["pour_markers"][0]["at"] == [2.0, 3.0])
    check("upconvert: pdf_sf -> target_area", c["target_area"] == 123.0)
    check("upconvert: grid passthrough", v2.get("grid_x") == {"A": 10.0})
    check("cy divisor: Feet 27",
          pbs.cy_divisor(Rhino.UnitSystem.Feet) == 27.0)
    check("cy divisor: Inches 46656",
          pbs.cy_divisor(Rhino.UnitSystem.Inches) == 46656.0)
    check("cy divisor: Meters None",
          pbs.cy_divisor(Rhino.UnitSystem.Meters) is None)

    # ---- harvest (read-only, hidden objects included) ----
    hlog = fw.Log()
    h1 = pbh.harvest_document(doc, hlog)
    lines.extend(hlog.lines)
    check("harvest is read-only", count_objects(doc) == n_before_harvest)
    check("harvest units", h1["units"] == "Meters", h1["units"])
    check("harvest floors L1..L6",
          sorted(h1["floors"].keys()) == ["L1", "L2", "L3", "L4", "L5",
                                          "L6"],
          str(sorted(h1["floors"].keys())))
    if sorted(h1["floors"].keys()) != ["L1", "L2", "L3", "L4", "L5", "L6"]:
        return
    l1, l2 = h1["floors"]["L1"], h1["floors"]["L2"]
    check("hidden L1 break harvested (sublayer bound)",
          len(l1["breaks"]) == 1 and l1["breaks"][0]["binding"] == "layer")
    check("L2 break bound by elevation",
          len(l2["breaks"]) == 1 and
          l2["breaks"][0]["binding"] == "elevation")
    check("L2 jog kept (4 vertices)",
          len(l2["breaks"][0]["polyline"]) == 4)
    check("L2 markers harvested",
          [m["pour"] for m in l2["pour_markers"]] == [1, 2])
    check("L3 break is curve_type 'curve'",
          h1["floors"]["L3"]["breaks"][0]["curve_type"] == "curve")

    # ---- wipe (the checkup hazard) -> restore -> byte-identical ----
    rlog = fw.Log()
    n_wiped = pbr.wipe_pourbreaks(doc, rlog)
    check("wipe removed the authored objects", n_wiped == N_PB,
          "wiped {0}, want {1}".format(n_wiped, N_PB))
    n_restored, n_skipped = pbr.restore_document(doc, h1, rlog)
    lines.extend(rlog.lines)
    check("restore re-added them", n_restored == N_PB and n_skipped == 0,
          "restored {0}, skipped {1}".format(n_restored, n_skipped))
    h2 = pbh.harvest_document(doc, fw.Log())
    check("harvest -> wipe -> restore -> harvest is identical",
          json.dumps(scrub(h1), sort_keys=True) ==
          json.dumps(scrub(h2), sort_keys=True))
    check("curve_type 'curve' survives the round trip",
          h2["floors"]["L3"]["breaks"][0]["curve_type"] == "curve")

    # ---- restore refuses unknown floors instead of drawing at z=0 ----
    ghost = {"version": 2, "units": "Meters", "floors": {"GHOST": {
        "breaks": [{"id": "G-PB1", "polyline": [[0, 0], [1, 1]],
                    "z": None, "curve_type": "polyline",
                    "binding": "floor-key", "provenance": "x",
                    "note": ""}],
        "pour_markers": [], "target_area": None}}}
    n0 = count_objects(doc)
    glog = fw.Log()
    n_ghost, n_ghost_skipped = pbr.restore_document(doc, ghost, glog)
    lines.extend(glog.lines)
    check("unknown floor: nothing restored", n_ghost == 0)
    check("unknown floor: reported as skipped (F13)", n_ghost_skipped == 1)
    check("unknown floor: document untouched", count_objects(doc) == n0)

    # ---- unit-mismatch guard ----
    bad = json.loads(json.dumps(h2))
    bad["units"] = "Feet"
    check("unit mismatch aborts split",
          pbs.split_document(doc, bad, None, fw.Log()) is None)

    # ---- split ----
    slog = fw.Log()
    report = pbs.split_document(doc, h2, None, slog)
    lines.extend(slog.lines)
    check("split returns a report", report is not None)
    if report is None:
        return

    sog = [o for o in doc.Objects
           if o is not None and o.Attributes is not None
           and doc.Layers[o.Attributes.LayerIndex] is not None
           and doc.Layers[o.Attributes.LayerIndex].Name == "Slab_SOG"]
    check("SOG slab untouched (layer exclude)", len(sog) == 1)

    p_l1 = pieces_with_pour(doc, "L1")
    p_l2 = pieces_with_pour(doc, "L2")
    p_l3 = pieces_with_pour(doc, "L3")
    p_l4 = pieces_with_pour(doc, "L4")
    p_l5 = pieces_with_pour(doc, "L5")
    p_l6 = pieces_with_pour(doc, "L6")
    for label, plist, want in (("L1", p_l1, 2), ("L2", p_l2, 2),
                               ("L3", p_l3, 2), ("L4", p_l4, 2),
                               ("L5", p_l5, 2), ("L6", p_l6, 3)):
        check("{0} split into {1} pieces".format(label, want),
              len(plist) == want, "got {0}".format(len(plist)))

    # L1: micro tail must not deflect the extension — still two ~20 m3
    # triangles, auto-numbered (no dots on L1)
    vols1 = sorted(round(b.GetVolume(), 3) for _p, b, _o in p_l1)
    check("L1 volumes 20/20 (micro tail ignored)",
          all(approx(v, 20.0, 0.05) for v in vols1), str(vols1))
    pour_ul, _ = piece_at(p_l1, 2, 8, 3.1)
    pour_lr, _ = piece_at(p_l1, 18, 2, 3.1)
    check("L1 auto pour numbers", pour_ul == 1 and pour_lr == 2,
          "ul={0} lr={1}".format(pour_ul, pour_lr))

    # L2: jogged break, LOCKED layer -> force_delete; dots author pours
    pour_a, brep_a = piece_at(p_l2, 2, 2, 6.1)
    pour_b, brep_b = piece_at(p_l2, 9, 8, 6.1)
    check("L2 dot 1 region is POUR1", pour_a == 1, str(pour_a))
    check("L2 dot 2 region is POUR2", pour_b == 2, str(pour_b))
    if brep_a is not None and brep_b is not None:
        check("L2 volumes 11/9",
              approx(brep_a.GetVolume(), 11.0, 0.05) and
              approx(brep_b.GetVolume(), 9.0, 0.05))
    leftovers = [o for o in doc.Objects
                 if o is not None and o.Attributes is not None
                 and doc.Layers[o.Attributes.LayerIndex] is not None
                 and doc.Layers[o.Attributes.LayerIndex].Name == "Slab_L2"
                 and o.Attributes.GetUserString("POUR") is None]
    check("L2 locked original deleted (no duplication)",
          len(leftovers) == 0, "{0} leftover".format(len(leftovers)))

    # L3: arc cut — volume conserved, pieces real
    vols3 = [b.GetVolume() for _p, b, _o in p_l3]
    check("L3 arc split conserves volume",
          approx(sum(vols3), 5.0, 0.05), str(vols3))

    # L4: 0.08%-volume corner pour with a dot -> sliver guard waived
    pour_main, brep_main = piece_at(p_l4, 3, 3, 12.1)
    pour_tiny, brep_tiny = piece_at(p_l4, 0.05, 0.05, 12.1)
    check("L4 main piece is POUR1", pour_main == 1, str(pour_main))
    check("L4 tiny authored pour kept as POUR2", pour_tiny == 2,
          str(pour_tiny))
    if brep_tiny is not None:
        check("L4 tiny piece ~0.004 m3",
              approx(brep_tiny.GetVolume(), 0.004, 0.003),
              str(brep_tiny.GetVolume()))

    # L5: U-notch — the concave lower piece's centroid escapes it; dot
    # containment must still label it correctly
    pour_low, brep_low = piece_at(p_l5, 1, 1, 15.1)
    pour_notch, _ = piece_at(p_l5, 5, 3, 15.1)
    pour_up, brep_up = piece_at(p_l5, 5, 8, 15.1)
    check("L5 concave lower piece is POUR1", pour_low == 1, str(pour_low))
    check("L5 notch interior belongs to upper POUR2", pour_notch == 2,
          str(pour_notch))
    check("L5 upper piece is POUR2", pour_up == 2, str(pour_up))
    if brep_low is not None and brep_up is not None:
        check("L5 volumes 8.8/11.2",
              approx(brep_low.GetVolume(), 8.8, 0.05) and
              approx(brep_up.GetVolume(), 11.2, 0.05),
              "{0:.3f}/{1:.3f}".format(brep_low.GetVolume(),
                                       brep_up.GetVolume()))

    # L6: re-entrant break -> 3 regions, all dots exact
    pour_bot, _ = piece_at(p_l6, 10, 1, 18.1)
    pour_mid, brep_mid = piece_at(p_l6, 10, 5, 18.1)
    pour_top, _ = piece_at(p_l6, 10, 9, 18.1)
    check("L6 three regions numbered 1/2/3 by dots",
          pour_bot == 1 and pour_mid == 2 and pour_top == 3,
          "{0}/{1}/{2}".format(pour_bot, pour_mid, pour_top))
    if brep_mid is not None:
        check("L6 middle region 16 m3",
              approx(brep_mid.GetVolume(), 16.0, 0.05),
              str(brep_mid.GetVolume()))

    # ---- report ----
    fl1 = report["floors"].get("L1", {})
    fl2 = report["floors"].get("L2", {})
    check("report units", report.get("units") == "Meters")
    check("L1 pour totals 20/20",
          approx(fl1.get("pours", {}).get("1", {}).get("vol"), 20.0, 0.1)
          and approx(fl1.get("pours", {}).get("2", {}).get("vol"), 20.0,
                     0.1), str(fl1.get("pours")))
    check("L2 pour totals 11/9",
          approx(fl2.get("pours", {}).get("1", {}).get("vol"), 11.0, 0.1)
          and approx(fl2.get("pours", {}).get("2", {}).get("vol"), 9.0,
                     0.1), str(fl2.get("pours")))
    check("no vol_cy on a metric model",
          "vol_cy" not in fl2.get("pours", {}).get("1", {}))
    b2 = fl2.get("breaks", [{}])[0]
    check("L2 break flagged near column (0.8 m)",
          b2.get("support_flag") is True and
          approx(b2.get("min_support_dist"), 0.8, 0.05), str(b2))
    b1 = fl1.get("breaks", [{}])[0]
    check("L1 break not support-flagged", not b1.get("support_flag"))
    check("L3 report break is curve_type 'curve'",
          report["floors"].get("L3", {}).get("breaks", [{}])[0]
          .get("curve_type") == "curve")
    l2_by = [pc.get("assigned_by")
             for s in fl2.get("slabs", []) for pc in s.get("pieces", [])]
    check("L2 pieces assigned by dot", l2_by == ["dot", "dot"],
          str(l2_by))
    check("L7 slab reported unsplit (floor without breaks)",
          report["floors"].get("L7", {}).get("slabs", [{}])[0]
          .get("status") == "no break for floor")
    check("P1 absent from report (SOG excluded outright)",
          "P1" not in report["floors"])


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
