# -*- coding: utf-8 -*-
"""Golden regression: the authored pour-break path must reproduce the
verified 2026-07 Bellwether result.

Launched on the staged Bellwether copy:
    Rhino.exe <stage>\\model_R7.3dm /nosplash
        /runscript="-_RunPythonScript <stage>\\test_pourbreaks_model.py"

Path under test (everything new):
    v1 pour_breaks_model.json -> upconvert_v1 -> restore_document
    (curves + dots into the doc) -> harvest_document -> split_document

Reference: the v1 report (stage\\pourbreak_report.json) produced by the
original axis-aligned splitter — 18 slabs split into 36 pieces. Per slab:
status must match; per piece: pour label identical, volume and soffit
area within 1 %.

Output: pb_golden_report.txt in the staging folder.
"""
from __future__ import division, print_function

import json
import os
import sys

STAGE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "qto_fw_test")
sys.path.insert(0, STAGE)
REPORT = os.path.join(STAGE, "pb_golden_report.txt")
V1_JSON = os.path.join(STAGE, "pour_breaks_model.json")
V1_REPORT = os.path.join(STAGE, "pourbreak_report.json")

import io


def write_report(text_lines):
    with io.open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(u"\n".join(u"{0}".format(l) for l in text_lines) + u"\n")


try:
    import Rhino
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


def rel_ok(a, b, rel=0.01, abs_slack=0.5):
    if a is None or b is None:
        return a == b
    return abs(a - b) <= max(abs_slack, rel * max(abs(a), abs(b)))


def main():
    doc = Rhino.RhinoDoc.ActiveDoc
    log("=== pour-break golden regression on {0} ===".format(doc.Path))
    check("model units are Feet (Bellwether)",
          doc.ModelUnitSystem == Rhino.UnitSystem.Feet)

    v1 = json.loads(io.open(V1_JSON, encoding="utf-8").read())
    golden = json.loads(io.open(V1_REPORT, encoding="utf-8").read())
    n_cuts = sum(len(c.get("cuts", [])) for c in v1["floors"].values())
    n_markers = sum(1 for c in v1["floors"].values()
                    if c.get("pour1_centroid_ft"))
    log("v1 input: {0} floors, {1} cuts, {2} centroids".format(
        len(v1["floors"]), n_cuts, n_markers))

    # ---- the new authored path, end to end ----
    data = pbs.upconvert_v1(v1, fw.Log())
    rlog = fw.Log()
    n_restored = pbr.restore_document(doc, data, rlog)
    lines.extend(rlog.lines)
    check("restore added every break + marker",
          n_restored == n_cuts + n_markers,
          "restored {0}, want {1}".format(n_restored, n_cuts + n_markers))

    hlog = fw.Log()
    h = pbh.harvest_document(doc, hlog)
    n_h_breaks = sum(len(c["breaks"]) for c in h["floors"].values())
    n_h_marks = sum(len(c["pour_markers"]) for c in h["floors"].values())
    check("harvest recovers every break", n_h_breaks == n_cuts,
          "{0}/{1}".format(n_h_breaks, n_cuts))
    check("harvest recovers every marker", n_h_marks == n_markers,
          "{0}/{1}".format(n_h_marks, n_markers))
    check("harvest floors match v1 floors",
          sorted(h["floors"].keys()) == sorted(v1["floors"].keys()),
          str(sorted(h["floors"].keys())))

    slog = fw.Log()
    report = pbs.split_document(doc, h, None, slog)
    lines.extend(slog.lines)
    check("split returns a report", report is not None)
    if report is None:
        return

    # ---- compare against the golden v1 report, slab by slab ----
    n_split_new = n_pieces_new = 0
    for fl, frep in report["floors"].items():
        for srec in frep["slabs"]:
            if (srec.get("status") or "").startswith("split into"):
                n_split_new += 1
                n_pieces_new += len(srec["pieces"])
    check("18 slabs split (as in 2026-07)", n_split_new == 18,
          "got {0}".format(n_split_new))
    check("36 pieces total (as in 2026-07)", n_pieces_new == 36,
          "got {0}".format(n_pieces_new))

    for fl, gfl in sorted(golden["floors"].items()):
        nfl = report["floors"].get(fl)
        g_slabs = gfl.get("slabs", [])
        if nfl is None:
            # golden lists every floor with cuts; a floor can be absent
            # from the new report only if it had no target slabs at all
            check("floor {0} present".format(fl), len(g_slabs) == 0,
                  "golden has {0} slabs".format(len(g_slabs)))
            continue
        n_slabs = list(nfl["slabs"])
        check("{0}: slab count".format(fl),
              len(n_slabs) == len(g_slabs),
              "{0} vs golden {1}".format(len(n_slabs), len(g_slabs)))
        for g in g_slabs:
            # match by layer + original volume (stable identity)
            match = None
            for n in n_slabs:
                if n["layer"] == g["layer"] and \
                        rel_ok(n.get("orig_vol"), g.get("orig_vol_cf")):
                    match = n
                    break
            if match is None:
                check("{0}: slab {1} matched".format(fl, g["layer"]),
                      False, "no new-report slab matches vol {0}".format(
                          g.get("orig_vol_cf")))
                continue
            n_slabs.remove(match)
            g_status = g.get("status", "")
            m_status = match.get("status", "")
            if g_status == "no break for floor":
                # v2 phrases an uncrossed floor the same way
                check("{0}/{1}: status".format(fl, g["layer"]),
                      m_status == g_status,
                      "{0} vs {1}".format(m_status, g_status))
                continue
            check("{0}/{1}: status".format(fl, g["layer"]),
                  m_status == g_status,
                  "{0} vs {1}".format(m_status, g_status))
            if not g_status.startswith("split into"):
                continue
            g_pieces = sorted(g.get("pieces", []),
                              key=lambda p: (p.get("pour"),
                                             p.get("vol_cf") or 0))
            m_pieces = sorted(match.get("pieces", []),
                              key=lambda p: (p.get("pour"),
                                             p.get("vol") or 0))
            check("{0}/{1}: piece count".format(fl, g["layer"]),
                  len(m_pieces) == len(g_pieces),
                  "{0} vs {1}".format(len(m_pieces), len(g_pieces)))
            for gp, mp in zip(g_pieces, m_pieces):
                tag = "{0}/{1} pour{2}".format(fl, g["layer"],
                                               gp.get("pour"))
                check(tag + ": pour label",
                      mp.get("pour") == gp.get("pour"),
                      "{0} vs {1}".format(mp.get("pour"), gp.get("pour")))
                check(tag + ": volume",
                      rel_ok(mp.get("vol"), gp.get("vol_cf")),
                      "{0} vs {1}".format(mp.get("vol"),
                                          gp.get("vol_cf")))
                check(tag + ": soffit area",
                      rel_ok(mp.get("area"), gp.get("soffit_sf")),
                      "{0} vs {1}".format(mp.get("area"),
                                          gp.get("soffit_sf")))

    # v2-only sanity on the feet model
    any_floor = None
    for fl, frep in report["floors"].items():
        if frep.get("pours"):
            any_floor = frep
            break
    check("vol_cy present on the feet model",
          any_floor is not None and
          all("vol_cy" in t for t in any_floor["pours"].values()))

    out = os.path.join(STAGE, "model_R7_pourbreak_report_v2.json")
    with io.open(out, "w", encoding="utf-8") as fh:
        fh.write(u"{0}".format(json.dumps(report, indent=1,
                                          sort_keys=True)))
    log("v2 report -> {0}".format(out))


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
