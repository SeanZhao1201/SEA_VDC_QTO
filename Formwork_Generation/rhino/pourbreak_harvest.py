#! python 2
# -*- coding: utf-8 -*-
"""Harvest authored pour breaks from the _POURBREAK layer into JSON (v2).

The modeler draws pour-break curves (lines / polylines, any orientation,
jogs allowed) on the ``_POURBREAK`` layer — optionally on a per-floor
sublayer ``_POURBREAK::<floor name>`` to pin the floor explicitly — and
drops a text dot ("1", "2", ...) inside each pour region to author the
pour order. This script walks that layer tree **read-only** (it fires no
document events, so QTO_Tool's REVERT CHECKUP is unaffected) and writes
``pour_breaks_model.json`` schema v2:

    {"version": 2, "units": "<doc unit system>",
     "source": {"kind": "layer-harvest", "model": "...", "layer": "_POURBREAK"},
     "floors": {"<floor>": {
         "breaks": [{"id", "polyline": [[x,y]..], "z", "curve_type",
                     "binding", "provenance", "note"}],
         "pour_markers": [{"pour", "at": [x,y], "z", "provenance"}],
         "target_area": null}}}

Coordinates are MODEL units, absolute world XY — harvest and split run on
the same model, so no unit conversion happens on coordinates; the
``units`` field lets the splitter refuse a model whose units changed.

Floor binding: the sublayer name wins when it matches a floor name from
the QTO ``FloorElevations`` document string (case-insensitive); otherwise
the curve's own Z is nearest-matched against the floor elevations. Every
binding is logged for review.

Run inside Rhino with ``_-RunPythonScript`` (IronPython) or headless via
``Rhino.exe /runscript`` with ``FW_HEADLESS=1``. Output path: the
``PB_JSON`` environment variable, else ``<doc folder>/<doc name>_pourbreaks
.json``, else the staging folder.
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

PB_ROOT = "_POURBREAK"
COORD_DECIMALS = 6          # rounding => byte-identical harvest round-trips
SCHEMA_VERSION = 2


def _rnd(v):
    return round(v, COORD_DECIMALS)


def is_pb_layer(doc, layer_index):
    """Layer is _POURBREAK or nested under it (case-insensitive)."""
    layer = doc.Layers[layer_index]
    if layer is None:
        return False
    fp = (layer.FullPath or "").lower()
    root = PB_ROOT.lower()
    return fp == root or fp.startswith(root + "::")


def pb_sublayer(doc, layer_index):
    """First path segment below _POURBREAK, or None when on the root."""
    layer = doc.Layers[layer_index]
    if layer is None:
        return None
    parts = (layer.FullPath or "").split("::")
    if len(parts) >= 2 and parts[0].lower() == PB_ROOT.lower():
        return parts[1]
    return None


def _curve_plan_points(curve):
    """Open curve -> ([[x, y]...], mean_z, curve_type).

    Polylines keep their vertices exactly; any other curve is sampled at
    64 points and tagged ``"curve"`` so the report can flag it (curved
    bulkheads are unusual — flagged, not rejected).
    """
    pts = None
    try:
        rc, pl = curve.TryGetPolyline()
        if rc:
            pts = list(pl)
    except Exception:
        pts = None
    ctype = "polyline"
    if pts is None:
        params = curve.DivideByCount(64, True)
        pts = [curve.PointAt(t) for t in params] if params else []
        ctype = "curve"
    xy = [[_rnd(p.X), _rnd(p.Y)] for p in pts]
    z = _rnd(sum(p.Z for p in pts) / len(pts)) if pts else 0.0
    return xy, z, ctype


def _floor_by_name(name, floor_elevations):
    """Case-insensitive match of a sublayer name against floor names —
    both raw and layer-sanitized (':' is structural in Rhino layer paths,
    so a floor named 'L2: Podium' restores to sublayer 'L2- Podium')."""
    if not name:
        return None
    low = name.strip().lower()
    for fl in floor_elevations.values():
        if fl.strip().lower() == low:
            return fl
        if fw._safe_layer_name(fl).strip().lower() == low:
            return fl
    return None


def _floor_by_z(z, floor_elevations):
    if not floor_elevations:
        return None
    best = min(floor_elevations.keys(), key=lambda e: abs(e - z))
    return floor_elevations[best]


def harvest_document(doc, log):
    """Collect breaks + pour markers from the _POURBREAK tree. Read-only.

    Returns the schema-v2 data dict (floors sorted, breaks deterministic
    by (z, first vertex) so re-harvesting an unchanged model reproduces
    the same JSON byte for byte).
    """
    floor_elev = fw.read_floor_elevations(doc)
    if not floor_elev:
        log("WARNING: no FloorElevations doc string — floor binding falls "
            "back to '-'; run the QTO Set Floor step first")

    fl_names = list(floor_elev.values())
    for n in set(fl_names):
        if fl_names.count(n) > 1:
            log("WARNING: duplicate floor name '{0}' ({1} elevations) — "
                "breaks bucket by NAME and will apply to every same-named "
                "level".format(n, fl_names.count(n)))

    raw_breaks = []             # (floor, z, xy, ctype, binding, guid, name)
    raw_markers = []            # (floor, pour, x, y, z, binding, guid)
    n_skipped = 0
    n_hidden = 0
    # same enumerator as wipe_pourbreaks: normal + locked + HIDDEN — a
    # hidden break must still reach the JSON, or a wipe/restore cycle
    # would silently destroy it while the split silently misses its cut
    es = Rhino.DocObjects.ObjectEnumeratorSettings()
    es.NormalObjects = True
    es.LockedObjects = True
    es.HiddenObjects = True
    for obj in doc.Objects.GetObjectList(es):
        if obj is None or obj.Attributes is None:
            continue
        li = obj.Attributes.LayerIndex
        if not is_pb_layer(doc, li):
            continue
        if obj.IsHidden:
            n_hidden += 1
        sub = pb_sublayer(doc, li)
        geom = obj.Geometry
        if isinstance(geom, TextDot):
            txt = (geom.Text or "").strip()
            try:
                pour = int(txt)
            except (TypeError, ValueError):
                log("  WARNING: text dot '{0}' on {1} is not a pour number "
                    "— skipped".format(txt, PB_ROOT))
                n_skipped += 1
                continue
            p = geom.Point
            by_name = _floor_by_name(sub, floor_elev)
            fl = by_name or _floor_by_z(p.Z, floor_elev) or "-"
            binding = "layer" if by_name else "elevation"
            raw_markers.append((fl, pour, _rnd(p.X), _rnd(p.Y), _rnd(p.Z),
                                binding, str(obj.Id)))
            continue
        if isinstance(geom, Curve):
            if geom.IsClosed:
                log("  WARNING: closed curve on {0} skipped — pour breaks "
                    "are open cut lines".format(PB_ROOT))
                n_skipped += 1
                continue
            xy, z, ctype = _curve_plan_points(geom)
            if obj.Attributes.GetUserString("PB_CURVE_TYPE") == "curve":
                # restored from a sampled non-polyline source: keep the
                # classification so the curved-bulkhead flag survives
                # wipe/restore cycles
                ctype = "curve"
            if len(xy) < 2:
                log("  WARNING: degenerate curve on {0} skipped".format(
                    PB_ROOT))
                n_skipped += 1
                continue
            by_name = _floor_by_name(sub, floor_elev)
            if sub and not by_name:
                log("  WARNING: sublayer '{0}' matches no floor name — "
                    "using elevation binding for a curve on it".format(sub))
            fl = by_name or _floor_by_z(z, floor_elev) or "-"
            binding = "layer" if by_name else "elevation"
            raw_breaks.append((fl, z, xy, ctype, binding, str(obj.Id),
                               obj.Attributes.Name or ""))
            continue
        log("  WARNING: unsupported object type {0} on {1} — skipped".format(
            type(geom).__name__, PB_ROOT))
        n_skipped += 1

    floors = {}
    for fl, z, xy, ctype, binding, guid, name in sorted(
            raw_breaks, key=lambda t: (t[0], t[1], t[2][0][0], t[2][0][1])):
        entry = floors.setdefault(fl, {"breaks": [], "pour_markers": [],
                                       "target_area": None})
        bid = "{0}-PB{1}".format(fl, len(entry["breaks"]) + 1)
        entry["breaks"].append({
            "id": bid, "polyline": xy, "z": z, "curve_type": ctype,
            "binding": binding, "provenance": "curve {0}".format(guid),
            "note": name})
        log("  {0}: {1} ({2}, {3} pts, z={4:.3f}, bound by {5})".format(
            fl, bid, ctype, len(xy), z, binding))
    for fl, pour, x, y, z, binding, guid in sorted(
            raw_markers, key=lambda t: (t[0], t[1], t[2], t[3])):
        entry = floors.setdefault(fl, {"breaks": [], "pour_markers": [],
                                       "target_area": None})
        entry["pour_markers"].append({
            "pour": pour, "at": [x, y], "z": z, "binding": binding,
            "provenance": "dot {0}".format(guid)})
        log("  {0}: pour marker {1} at ({2:.2f}, {3:.2f})".format(
            fl, pour, x, y))

    for fl, cfg in sorted(floors.items()):
        if cfg["pour_markers"] and not cfg["breaks"]:
            log("  WARNING: floor {0} has pour markers but no break "
                "curves".format(fl))
        seen_pours = [m["pour"] for m in cfg["pour_markers"]]
        for p in set(seen_pours):
            if seen_pours.count(p) > 1:
                log("  WARNING: floor {0} has {1} markers labelled pour "
                    "{2}".format(fl, seen_pours.count(p), p))

    extras = []
    if n_skipped:
        extras.append("{0} objects skipped".format(n_skipped))
    if n_hidden:
        extras.append("{0} hidden objects included".format(n_hidden))
    log("harvested {0} breaks + {1} markers across {2} floors"
        "{3}".format(len(raw_breaks), len(raw_markers), len(floors),
                     " ({0})".format(", ".join(extras)) if extras else ""))
    return {
        "version": SCHEMA_VERSION,
        "units": str(doc.ModelUnitSystem),
        "source": {"kind": "layer-harvest", "model": doc.Path or "",
                   "layer": PB_ROOT},
        "floors": floors,
    }


def default_json_path(doc):
    env = os.environ.get("PB_JSON")
    if env:
        return env
    if doc.Path:
        base = os.path.splitext(doc.Path)[0]
        return base + "_pourbreaks.json"
    return os.path.join(STAGE, "pour_breaks_model.json")


def write_json(data, path, log):
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(u"{0}".format(json.dumps(data, indent=1, sort_keys=True)))
    log("pour breaks -> {0}".format(path))


def main():
    doc = Rhino.RhinoDoc.ActiveDoc
    log = fw.Log()
    log("pourbreak_harvest — model units: {0}".format(doc.ModelUnitSystem))
    data = harvest_document(doc, log)
    path = default_json_path(doc)
    write_json(data, path, log)
    log.save(os.path.splitext(path)[0] + "_log.txt")
    return data


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with io.open(os.path.join(STAGE, "pourbreak_harvest_error.txt"),
                     "w", encoding="utf-8") as fh:
            fh.write(u"{0}".format(traceback.format_exc()))
        raise
    finally:
        if os.environ.get("FW_HEADLESS") == "1":
            try:
                Rhino.RhinoApp.Exit()
            except Exception:
                Rhino.RhinoApp.RunScript("_-Exit", False)
