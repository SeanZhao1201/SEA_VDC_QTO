#! python 2
# -*- coding: utf-8 -*-
"""Redraw pour-break curves + pour dots from JSON onto the _POURBREAK layer.

The inverse of ``pourbreak_harvest.py`` — the recovery path for the one
known hazard of layer-based authoring: the QTO checkup deletes every
curve in the document (by design). The harvested JSON is the persistence;
this script re-materializes it so the modeler can keep editing.

Restores schema v2 directly; schema v1 (the PDF-era axis-aligned cuts) is
upconverted first via ``split_pourbreaks.upconvert_v1`` so the historical
Bellwether breaks can be re-authored as curves too. Breaks whose ``z`` is
null (v1 had no curve Z) are drawn at their floor's elevation from the
QTO ``FloorElevations`` document string.

NOTE: this ADDS objects to the live document. Adding objects permanently
disables QTO_Tool's REVERT CHECKUP until the next successful checkup —
restore therefore belongs AFTER the QTO pass, which is also when breaks
are meant to be drawn. Curves sampled from non-polyline source curves
(``curve_type: "curve"``) come back as 64-point polyline approximations.

Input path: ``PB_JSON`` env var, else ``<doc folder>/<doc name>
_pourbreaks.json``, else the staging folder default.
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
import System
from Rhino.Geometry import Point3d, PolylineCurve, TextDot
from System.Drawing import Color

import formwork_gen_rhino as fw
from pourbreak_harvest import PB_ROOT, default_json_path

COL_BREAK = Color.FromArgb(255, 0, 144)      # magenta — unmissable


def _floor_elevation(fl, floor_elevations):
    """Elevation for a floor name (case-insensitive), or None."""
    low = (fl or "").strip().lower()
    for elev, name in floor_elevations.items():
        if name.strip().lower() == low:
            return elev
    return None


def restore_document(doc, data, log):
    """Add every break curve + pour dot from ``data`` to the doc.

    Returns ``(n_added, n_floor_skipped)``. Layers:
    ``_POURBREAK::<floor>`` for floors with a real name, the root layer
    otherwise — so a re-harvest binds every restored break to the same
    floor by layer.
    """
    floor_elev = fw.read_floor_elevations(doc)
    n_added = 0
    n_floor_skipped = 0
    for fl in sorted(data.get("floors", {})):
        cfg = data["floors"][fl]
        breaks = cfg.get("breaks", [])
        markers = cfg.get("pour_markers", [])
        fallback_z = _floor_elevation(fl, floor_elev)
        needs_z = any(b.get("z") is None for b in breaks) \
            or any(m.get("z") is None for m in markers)
        if needs_z and fallback_z is None:
            # Drawing at z=0 would fabricate geometry that a re-harvest
            # rebinds to whatever floor is nearest zero — and the split
            # would then cut the WRONG slabs. Fail loudly instead.
            log("  ERROR: floor '{0}' is not in this document's "
                "FloorElevations and its items carry no z — {1} break(s) "
                "+ {2} marker(s) NOT restored. Re-run Set Floor or fix "
                "the JSON floor names.".format(fl, len(breaks),
                                               len(markers)))
            n_floor_skipped += 1
            continue

        def layer_for(binding):
            """Faithful re-materialization: what was authored on a floor
            sublayer goes back to it; what was elevation-bound goes back
            to the root layer — a re-harvest then reproduces the same
            bindings byte for byte. Floor names pass through
            _safe_layer_name (':' is structural in Rhino layer paths)."""
            if binding in ("layer", "floor-key") and fl != "-":
                return fw.ensure_layer(
                    doc, [PB_ROOT, fw._safe_layer_name(fl)], COL_BREAK)
            return fw.ensure_layer(doc, [PB_ROOT], COL_BREAK)

        for brk in breaks:
            z = brk.get("z")
            if z is None:
                z = fallback_z
            pts = [Point3d(p[0], p[1], z) for p in brk["polyline"]]
            if len(pts) < 2:
                log("  WARNING: {0} degenerate — skipped".format(
                    brk.get("id", "?")))
                continue
            attr = Rhino.DocObjects.ObjectAttributes()
            attr.LayerIndex = layer_for(brk.get("binding"))
            attr.Name = brk.get("note") or ""
            if brk.get("curve_type") == "curve":
                # sampled from a non-polyline source: stamp the restored
                # polyline so a re-harvest keeps the classification (and
                # the curved-bulkhead flag in the report)
                attr.SetUserString("PB_CURVE_TYPE", "curve")
            if doc.Objects.AddCurve(PolylineCurve(pts), attr) != \
                    System.Guid.Empty:
                n_added += 1
        for mk in markers:
            z = mk.get("z")
            if z is None:
                z = fallback_z
            dot = TextDot(str(mk["pour"]),
                          Point3d(mk["at"][0], mk["at"][1], z))
            attr = Rhino.DocObjects.ObjectAttributes()
            attr.LayerIndex = layer_for(mk.get("binding"))
            if doc.Objects.AddTextDot(dot, attr) != System.Guid.Empty:
                n_added += 1
    doc.Views.Redraw()
    log("restored {0} objects under {1}{2}".format(
        n_added, PB_ROOT,
        " ({0} floor(s) SKIPPED — see errors above)".format(
            n_floor_skipped) if n_floor_skipped else ""))
    return n_added, n_floor_skipped


def wipe_pourbreaks(doc, log):
    """Delete every object under _POURBREAK (test helper / re-restore prep).

    Uses the same enumerator settings as the formwork purge so locked and
    hidden leftovers go too.
    """
    from pourbreak_harvest import is_pb_layer
    from split_pourbreaks import force_delete
    es = Rhino.DocObjects.ObjectEnumeratorSettings()
    es.NormalObjects = True
    es.LockedObjects = True
    es.HiddenObjects = True
    n = 0
    for obj in list(doc.Objects.GetObjectList(es)):
        if obj is None or obj.Attributes is None:
            continue
        if is_pb_layer(doc, obj.Attributes.LayerIndex):
            if force_delete(doc, obj, log):
                n += 1
    log("wiped {0} objects under {1}".format(n, PB_ROOT))
    return n


def main():
    doc = Rhino.RhinoDoc.ActiveDoc
    log = fw.Log()
    path = default_json_path(doc)
    log("pourbreak_restore <- {0}".format(path))
    n_added = 0
    try:
        with io.open(path, encoding="utf-8") as fh:
            data = json.loads(fh.read())
        if data.get("version", 1) < 2:
            import split_pourbreaks
            data = split_pourbreaks.upconvert_v1(data, log)
        n_added, n_skipped = restore_document(doc, data, log)
        # The C# handler judges the run by THIS file appearing (deleted
        # pre-run), never by RunScript's return or the previous run's log
        # - the driver calls main() directly, so the __main__ error
        # writer below never runs on the GUI path.
        with io.open(os.path.join(STAGE, "pourbreak_restore_result.json"),
                     "w", encoding="utf-8") as fh:
            fh.write(u"{0}".format(json.dumps(
                {"added": n_added, "floors_skipped": n_skipped})))
    finally:
        # the log must reach disk even on a throw, or the UI pane has
        # nothing to show for a failed restore
        log.save(os.path.join(STAGE, "pourbreak_restore_log.txt"))
    return n_added


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with io.open(os.path.join(STAGE, "pourbreak_restore_error.txt"),
                     "w", encoding="utf-8") as fh:
            fh.write(u"{0}".format(traceback.format_exc()))
        raise
    finally:
        if os.environ.get("FW_HEADLESS") == "1":
            try:
                Rhino.RhinoApp.Exit()
            except Exception:
                Rhino.RhinoApp.RunScript("_-Exit", False)
