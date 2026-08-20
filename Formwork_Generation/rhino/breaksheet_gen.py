#! python 2
# -*- coding: utf-8 -*-
"""Generate the pour-break BREAK SHEET: one plan cell per floor GROUP.

The modeler asked for "a plan view per slab / typical slab to draw pour
breaks on" instead of the ``_POURBREAK`` layer ceremony. This script reads
the OPEN model strictly read-only (no document events - REVERT CHECKUP is
unaffected, same contract as pourbreak_harvest) and writes a separate
``breaksheet.3dm`` to the staging folder via ``Rhino.FileIO.File3dm`` -
the live model file never gains an object, a layer, or a byte.

Sheet anatomy (all coordinates in MODEL units):
- Floors whose slab plan footprints are EXACTLY identical (quantized
  vertex fingerprint) share ONE cell, labelled with every member floor -
  the typical-floor collapse proven on the real model 2026-08-17 (tower
  grouped 5+5, podium floors individual). Near-identical groups are NOT
  auto-merged; they go into the meta as ``near_typ`` suggestions and the
  FormworkUI TYP MERGE dialog writes the user's picks to
  ``breaksheet_merge.json`` (P2) - this script applies them on the next
  MAKE, re-validating each pair against the live fingerprints first.
- Locked furniture per cell: slab top-face outlines (each slab separately
  - seeing existing deck joints avoids drawing redundant breaks, the L01
  lesson), opening loops, support footprints below (bbox rectangles -
  placeholder fidelity), cell frame, floor label.
- Everything the user draws on any UNLOCKED layer inside a frame IS a
  break (open curves) or a pour marker (numbered text dot). No layer
  discipline required.
- Existing breaks from the current pour_breaks_model.json are drawn into
  their cells as editable curves, so regenerate-edit-import round-trips.

The companion ``breaksheet.meta.json`` sidecar records the cell map
(frames, world offsets, member floors + elevations) and the source model
path; ``breaksheet_import.py`` refuses a sheet whose sidecar is missing -
the same loud-staleness stance as the derived-model sidecar.

Runs in-process via ``_-RunPythonScript`` (the bs_gui_make driver) or
headless in the dev loop with ``FW_HEADLESS=1``.
"""
from __future__ import division, print_function

import io
import json
import math
import os
import sys
import traceback

STAGE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "qto_fw_test")
sys.path.insert(0, STAGE)

import System
import Rhino
from Rhino.Geometry import Brep, Curve, Point3d, Polyline, PolylineCurve

import formwork_gen_rhino as fw

SCHEMA_VERSION = 1
SHEET_FILE = os.environ.get("BS_SHEET") or os.path.join(STAGE, "breaksheet.3dm")
META_FILE = os.environ.get("BS_META") or os.path.join(STAGE, "breaksheet.meta.json")
# P2: user-directed near-TYP merges, written by the FormworkUI merge dialog
MERGE_FILE = os.environ.get("BS_MERGE") or os.path.join(
    STAGE, "breaksheet_merge.json")
# P3: the active break-scheme note, written by FormworkUI at option
# save/load - the sheet says WHICH scheme it draws, so two printed
# option sheets can be told apart
OPTION_FILE = os.path.join(STAGE, "breaks_active_option.json")
LOG_FILE = os.path.join(STAGE, "breaksheet_log.txt")

SLAB_KEYWORD = "slab"
# Same excludes as split_pourbreaks.PARAMS: a cell for a slab the splitter
# will never cut would invite breaks that silently do nothing.
SLAB_EXCLUDE = ("sog", "topping")
SUPPORT_KEYWORDS = ("wall", "column")
UP_DOT = 0.94                  # cos(20 deg) - same top-face cutoff as QTO
FP_QUANT_M = 0.01              # fingerprint vertex quantization (metres)
MARGIN_M = 2.0                 # cell margin around the footprint
SUPPORT_BAND_M = 3.0           # how far below the floor top to look for supports
FLOOR_BIND_MAX_M = 2.0         # slab top farther than this from every floor
                               # elevation is left off the sheet, loudly
COORD_DECIMALS = 6             # matches pourbreak_harvest

LAYER_FRAME = "SHEET_FRAME"
LAYER_OUTLINE = "SHEET_OUTLINE"
LAYER_OPENING = "SHEET_OPENING"
LAYER_SUPPORT = "SHEET_SUPPORT"
LAYER_LABEL = "SHEET_LABEL"
LAYER_DRAW = "DRAW"


def _rnd(v):
    return round(v, COORD_DECIMALS)


def _scale_from_m(doc):
    return Rhino.RhinoMath.UnitScale(Rhino.UnitSystem.Meters,
                                     doc.ModelUnitSystem)


def _slab_layer_first(doc, obj):
    layer = doc.Layers[obj.Attributes.LayerIndex]
    lname = layer.Name if layer is not None else ""
    return lname, lname.split("_")[0].lower()


def _as_brep(geom):
    if isinstance(geom, Rhino.Geometry.Extrusion):
        return geom.ToBrep(True)
    if isinstance(geom, Brep):
        return geom
    return None


def _loop_xy(loop):
    """BrepLoop -> list of [x, y] vertices (polyline exact, else sampled)."""
    curve = loop.To3dCurve()
    pts = None
    try:
        rc, pl = curve.TryGetPolyline()
        if rc:
            pts = list(pl)
    except Exception:
        pts = None
    if pts is None:
        params = curve.DivideByCount(64, True)
        pts = [curve.PointAt(t) for t in params] if params else []
    return [[p.X, p.Y] for p in pts]


def collect_floors(doc, log):
    """floors: [{name, z, slabs: [{outer: [xy..], inners: [[xy..]..]}],
    supports: [bbox rect xy..]}] sorted by z. Read-only."""
    floor_elev = fw.read_floor_elevations(doc)
    if not floor_elev:
        log("ERROR: no FloorElevations doc string - run the QTO Set Floor "
            "step first; the sheet needs the floor table")
        return None

    by_name = {}
    for z, name in floor_elev.items():
        # duplicate floor names collapse here exactly like the splitter's
        # floor buckets - harvest already warns on them
        by_name[name] = {"name": name, "z": float(z), "slabs": [],
                         "supports": []}

    elevs = sorted(floor_elev.keys())
    bind_max = FLOOR_BIND_MAX_M * _scale_from_m(doc)

    def floor_of(z_top):
        best = min(elevs, key=lambda e: abs(e - z_top))
        if abs(best - z_top) > bind_max:
            return None
        return floor_elev[best]

    n_slab = 0
    n_sup = 0
    n_unbound = 0
    # HIDDEN objects included, mirroring pourbreak_harvest: a floor whose
    # slabs are temporarily hidden must still get a cell, or its carried-in
    # breaks would vanish from the next import.
    es = Rhino.DocObjects.ObjectEnumeratorSettings()
    es.NormalObjects = True
    es.LockedObjects = True
    es.HiddenObjects = True
    for obj in doc.Objects.GetObjectList(es):
        if obj is None or obj.Attributes is None:
            continue
        lname, first = _slab_layer_first(doc, obj)
        brep = _as_brep(obj.Geometry)
        if brep is None:
            continue
        bb = brep.GetBoundingBox(True)
        if SLAB_KEYWORD in first and \
                not any(kw in lname.lower() for kw in SLAB_EXCLUDE):
            fl_name = floor_of(bb.Max.Z)
            if fl_name is None:
                n_unbound += 1
                log("WARNING: slab on '{0}' (top z={1:.2f}) is farther than "
                    "the binding cap from every floor elevation - left off "
                    "the sheet".format(lname, bb.Max.Z))
                continue
            entry = by_name.get(fl_name)
            if entry is None:
                continue
            faces = []
            for i in range(brep.Faces.Count):
                face = brep.Faces[i]
                dom_u = face.Domain(0)
                dom_v = face.Domain(1)
                normal = face.NormalAt(dom_u.Mid, dom_v.Mid)
                normal.Unitize()
                if face.OrientationIsReversed:
                    normal = -normal
                if normal.Z <= UP_DOT:
                    continue
                outer = None
                inners = []
                for j in range(face.Loops.Count):
                    loop = face.Loops[j]
                    xy = _loop_xy(loop)
                    if len(xy) < 3:
                        continue
                    if loop.LoopType == Rhino.Geometry.BrepLoopType.Outer:
                        outer = xy
                    else:
                        inners.append(xy)
                if outer:
                    faces.append({"outer": outer, "inners": inners})
            if faces:
                entry["slabs"].extend(faces)
                n_slab += 1
        elif any(k in first for k in SUPPORT_KEYWORDS):
            entry_name = None
            band = SUPPORT_BAND_M * _scale_from_m(doc)
            for e in elevs:
                if e - band <= bb.Max.Z <= e + 0.01:
                    entry_name = floor_elev[e]
                    break
            if entry_name and by_name.get(entry_name) is not None:
                by_name[entry_name]["supports"].append(
                    [[bb.Min.X, bb.Min.Y], [bb.Max.X, bb.Min.Y],
                     [bb.Max.X, bb.Max.Y], [bb.Min.X, bb.Max.Y],
                     [bb.Min.X, bb.Min.Y]])
                n_sup += 1

    floors = [f for f in by_name.values() if f["slabs"]]
    floors.sort(key=lambda f: f["z"])
    skipped = [f["name"] for f in by_name.values() if not f["slabs"]]
    if skipped:
        log("WARNING: no cell for floor(s) {0} (no slabs found for them) - "
            "any existing breaks on those floors are PRESERVED untouched by "
            "the next import".format(", ".join(sorted(skipped))))
    log("collected {0} slab breps across {1} floors, {2} supports"
        "{3}".format(n_slab, len(floors), n_sup,
                     " ({0} unbound slab(s) skipped)".format(n_unbound)
                     if n_unbound else ""))
    return floors


def fingerprint(floor, quant):
    verts = set()
    for slab in floor["slabs"]:
        for ring in [slab["outer"]] + slab["inners"]:
            for x, y in ring:
                verts.add((int(round(x / quant)), int(round(y / quant))))
    return frozenset(verts)


def group_floors(floors, doc, log):
    """Exact-fingerprint groups, ascending z."""
    quant = FP_QUANT_M * _scale_from_m(doc)
    groups = []
    by_fp = {}
    for f in floors:
        fp = fingerprint(f, quant)
        if fp in by_fp:
            by_fp[fp]["members"].append(f)
        else:
            g = {"members": [f], "fp": fp, "merged": False}
            by_fp[fp] = g
            groups.append(g)

    for g in groups:
        names = [m["name"] for m in g["members"]]
        if len(names) > 1:
            log("TYP group: {0} ({1} identical floors)".format(
                ", ".join(names), len(names)))
    return groups


def _near_typ(fp_a, fp_b):
    """(qualifies, diff, union) under the near-TYP rule. One rule, one
    metric for the suggestion report AND the merge-directive validation:
    both compare the CURRENT cells' fingerprints (union fingerprints for
    already-merged cells), so what the dialog offers is exactly what the
    validation accepts."""
    union = len(fp_a | fp_b)
    if union == 0:
        return False, 0, 0
    diff = len(fp_a ^ fp_b)
    return 0 < diff <= max(8, int(0.15 * union)), diff, union


def near_typ_pairs(groups, log):
    """Near-miss suggestions between the CURRENT groups (so pairs the user
    already merged drop out). The modeler decides via the FormworkUI merge
    dialog; nothing is ever auto-merged."""
    pairs = []
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            ok, diff, union = _near_typ(groups[i]["fp"], groups[j]["fp"])
            if ok:
                a, b = groups[i], groups[j]
                pairs.append({
                    "a": a["members"][0]["name"],
                    "b": b["members"][0]["name"],
                    "floors_a": [m["name"] for m in a["members"]],
                    "floors_b": [m["name"] for m in b["members"]],
                    "diff": diff, "union": union,
                })
                log("NEAR-TYP: cells '{0}' and '{1}' differ by only {2} of "
                    "{3} footprint vertices - review whether they should "
                    "share one cell (the TYP MERGE dialog applies it)".format(
                        a["members"][0]["name"], b["members"][0]["name"],
                        diff, union))
    return pairs


def _read_merge_directives(doc, log):
    """User merge sets from MERGE_FILE: [[floor, floor, ...], ...].

    The staging folder is machine-wide with one fixed file name, so a
    directive file left behind by ANOTHER project must not silently
    regroup this sheet - same guard as the carried-in breaks JSON."""
    if not os.path.exists(MERGE_FILE):
        return []
    try:
        with io.open(MERGE_FILE, encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except Exception as ex:
        log("WARNING: merge directive file unreadable ({0}) - no merges "
            "applied".format(ex))
        return []
    # valid JSON is not enough: a foreign tool's '[]' or '"x"' in the
    # machine-wide staging folder must degrade to the same warning, never
    # crash the whole MAKE
    if not isinstance(data, dict):
        log("WARNING: merge directive file malformed (top level is not an "
            "object) - no merges applied")
        return []
    src = data.get("docPath")
    if not hasattr(src, "lower"):
        src = ""
    doc_path = doc.Path or ""
    if src and doc_path and src.lower() != doc_path.lower():
        log("WARNING: the merge directives were written for a DIFFERENT "
            "model file ({0}) - ignored. Re-pick the merges in the TYP "
            "MERGE dialog.".format(src))
        return []
    entries = data.get("merge")
    if not isinstance(entries, list):
        entries = []
    sets = []
    for entry in entries:
        try:
            names = [n for n in entry if hasattr(n, "lower")]
        except TypeError:
            continue
        if len(names) >= 2:
            sets.append(names)
    return sets


def apply_merges(groups, directives, log):
    """Union groups per the user's directives - validated, never blind.

    Directives apply IN FILE ORDER, each validated against the cells'
    CURRENT fingerprints - already-applied merges included, i.e. the
    union fingerprint - with the same near-TYP rule the suggestions are
    computed with. One metric on both sides: every merge the dialog
    offers is honorable, and a stale directive whose floors genuinely
    diverged since it was written fails loudly instead of gluing them.
    (The dialog writes applied merges before new picks, so an offered
    merged-group + neighbor suggestion validates against the same union
    fingerprint it was suggested from.)"""
    if not directives:
        return groups
    by_floor = {}
    for gi, g in enumerate(groups):
        for m in g["members"]:
            by_floor[m["name"]] = gi

    # union-find over the directive sets (chained directives merge, each
    # union step re-validated against the accumulated fingerprint)
    parent = list(range(len(groups)))
    fps = [g["fp"] for g in groups]      # current fingerprint per root

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for names in directives:
        roots = []
        for n in names:
            if n not in by_floor:
                log("WARNING: merge directive names floor '{0}' which has "
                    "no cell on this sheet - that name is ignored".format(n))
                continue
            r = find(by_floor[n])
            if r not in roots:
                roots.append(r)
        if len(roots) < 2:
            continue
        # validate every pair of CURRENT cells in the requested set
        # before unioning anything from this directive
        refused = None
        for i in range(len(roots)):
            for j in range(i + 1, len(roots)):
                a, b = fps[roots[i]], fps[roots[j]]
                if a == b:
                    continue
                ok, diff, union = _near_typ(a, b)
                if not ok:
                    refused = (groups[roots[i]]["members"][0]["name"],
                               groups[roots[j]]["members"][0]["name"],
                               diff, union)
                    break
            if refused:
                break
        if refused:
            log("MERGE REFUSED for {0}: the cells holding '{1}' and '{2}' "
                "differ by {3} of {4} footprint vertices - beyond the "
                "near-TYP threshold. The cells stay separate; re-pick in "
                "the TYP MERGE dialog.".format(
                    ", ".join(names), refused[0], refused[1],
                    refused[2], refused[3]))
            continue
        root0 = roots[0]
        merged_fp = fps[root0]
        for r in roots[1:]:
            parent[r] = root0
            merged_fp = merged_fp | fps[r]
        fps[root0] = merged_fp

    merged = {}
    out = []
    for gi, g in enumerate(groups):
        root = find(gi)
        if root not in merged:
            merged[root] = g
            out.append(g)
        else:
            target = merged[root]
            target["members"].extend(g["members"])
            target["merged"] = True
    for root, g in merged.items():
        # the surviving group carries the union fingerprint, so the
        # next MAKE's suggestions and validations both see it
        g["fp"] = fps[root]
    for g in out:
        if g["merged"]:
            g["members"].sort(key=lambda m: m["z"])
            log("MERGED per user directive: {0} share one cell "
                "(representative '{1}' - its footprint is the one "
                "drawn)".format(
                    ", ".join(m["name"] for m in g["members"]),
                    g["members"][0]["name"]))
    return out


def _existing_breaks(doc, log):
    pb_json = os.environ.get("PB_JSON") or os.path.join(
        STAGE, "pour_breaks_model.json")
    if not os.path.exists(pb_json):
        return {}
    try:
        # `with`, never a bare read: IronPython has no refcount collection
        # and an unclosed .NET stream blocks a later DELETE of the JSON
        # (rewrites share, deletes do not)
        with io.open(pb_json, encoding="utf-8") as fh:
            data = json.loads(fh.read())
        # The staging folder is machine-wide with one fixed file name: a
        # JSON left behind by ANOTHER project must not be drawn into this
        # sheet and re-imported with laundered provenance.
        src_model = (data.get("source") or {}).get("model") or ""
        doc_path = doc.Path or ""
        if src_model and doc_path and src_model.lower() != doc_path.lower():
            log("WARNING: the current breaks JSON came from a DIFFERENT "
                "model file ({0}) - NOT drawing it into this sheet. Run "
                "HARVEST here, or import a sheet made from this model, to "
                "rebuild it.".format(src_model))
            return {}
        floors = data.get("floors", {})
        log("carrying existing breaks in from {0} ({1} floors)".format(
            pb_json, len(floors)))
        return floors
    except Exception as ex:
        log("WARNING: existing breaks JSON unreadable ({0}) - the sheet "
            "starts empty".format(ex))
        return {}


def _active_option(doc, log):
    """The active scheme's name for the sheet label, or None.

    Same guards as every machine-wide staging artifact (shape, docPath);
    when the breaks JSON changed since the note was written, the name is
    provenance, not identity - say so on the sheet."""
    if not os.path.exists(OPTION_FILE):
        return None
    try:
        with io.open(OPTION_FILE, encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    src = data.get("docPath")
    if not hasattr(src, "lower"):
        src = ""
    doc_path = doc.Path or ""
    # STRICT equality (matches C# ReadActiveOption): the note writers
    # refuse unsaved docs, so a real note always carries a path -
    # empty-vs-non-empty is never a legitimate pairing here
    if src.lower() != doc_path.lower():
        return None
    name = data.get("name")
    if not name or not hasattr(name, "lower"):
        return None
    sha = data.get("sha")
    if not hasattr(sha, "lower"):
        sha = ""
    # mirror the C# predicate exactly: a MISSING or unreadable breaks
    # JSON hashes as "" and therefore reads as modified too - the name
    # is provenance, not identity, the moment the bytes stop matching
    if sha:
        pb_json = os.environ.get("PB_JSON") or os.path.join(
            STAGE, "pour_breaks_model.json")
        actual = ""
        if os.path.exists(pb_json):
            try:
                import hashlib
                # `with`, not a bare read: IronPython has no refcount
                # collection, and an unclosed .NET stream keeps the JSON
                # locked until some later GC
                with io.open(pb_json, "rb") as fh:
                    actual = hashlib.sha256(fh.read()).hexdigest().upper()
            except Exception:
                actual = ""
        if actual != sha.upper():
            name = name + " (modified)"
    log("active break scheme: {0}".format(name))
    return name


def _break_shape(entry):
    return (json.dumps(entry.get("polyline")), entry.get("curve_type"),
            entry.get("note") or "")


def build_sheet(doc, groups, existing, log, option=None):
    """Construct the File3dm + meta dict. Returns (f3dm, meta)."""
    scale = _scale_from_m(doc)
    margin = MARGIN_M * scale

    f3 = Rhino.FileIO.File3dm()
    f3.Settings.ModelUnitSystem = doc.ModelUnitSystem

    def add_layer(name, color, locked):
        # File3dmLayerTable has no [] indexer under CPython 3 (Rhino 8);
        # AddLayer returns the index and FindIndex fetches the live object.
        idx = f3.AllLayers.AddLayer(name, color)
        layer = f3.AllLayers.FindIndex(idx)
        if layer is not None:
            layer.IsLocked = locked
        return idx

    li_draw = add_layer(LAYER_DRAW, System.Drawing.Color.Red, False)
    li_frame = add_layer(LAYER_FRAME, System.Drawing.Color.DarkGray, True)
    li_outline = add_layer(LAYER_OUTLINE, System.Drawing.Color.Black, True)
    li_opening = add_layer(LAYER_OPENING, System.Drawing.Color.DarkOrange, True)
    li_support = add_layer(LAYER_SUPPORT, System.Drawing.Color.Silver, True)
    li_label = add_layer(LAYER_LABEL, System.Drawing.Color.DarkBlue, True)

    def attrs(layer_index, name=None):
        a = Rhino.DocObjects.ObjectAttributes()
        a.LayerIndex = layer_index
        if name:
            a.Name = name
        return a

    def add_poly(xy_pts, layer_index, name=None, curve_type=None):
        pl = Polyline()
        for x, y in xy_pts:
            pl.Add(x, y, 0.0)
        a = attrs(layer_index, name)
        if curve_type == "curve":
            # keep the sampled-curve classification alive across the sheet
            # round trip, exactly like restore does (PB_CURVE_TYPE)
            a.SetUserString("PB_CURVE_TYPE", "curve")
        f3.Objects.AddCurve(PolylineCurve(pl), a)

    def add_dot(text, x, y, layer_index):
        f3.Objects.AddTextDot(text, Point3d(x, y, 0.0), attrs(layer_index))

    # cell extents from each group's representative (identical by fingerprint)
    cells = []
    for g in groups:
        rep = g["members"][0]
        xs, ys = [], []
        for slab in rep["slabs"]:
            for ring in [slab["outer"]] + slab["inners"]:
                for x, y in ring:
                    xs.append(x)
                    ys.append(y)
        for ring in rep["supports"]:
            for x, y in ring:
                xs.append(x)
                ys.append(y)
        # carried-in ink counts toward the extent too: a harvested break
        # generously over-drawn past the slab edge (the splitter's extension
        # ladder exists because that is normal authoring) must still land
        # INSIDE its cell frame, or the untouched sheet would refuse import
        rep_ink = existing.get(rep["name"], {})
        for entry in rep_ink.get("breaks", []):
            for x, y in entry.get("polyline") or []:
                xs.append(x)
                ys.append(y)
        for marker in rep_ink.get("pour_markers", []):
            at = marker.get("at")
            if at:
                xs.append(at[0])
                ys.append(at[1])
        cells.append({
            "group": g, "rep": rep,
            "bb": (min(xs), min(ys), max(xs), max(ys)),
        })

    pitch_x = max(c["bb"][2] - c["bb"][0] for c in cells) + 2 * margin
    pitch_y = max(c["bb"][3] - c["bb"][1] for c in cells) + 2 * margin
    cols = max(1, int(math.ceil(math.sqrt(len(cells)))))

    meta_cells = []
    for k, cell in enumerate(cells):
        col = k % cols
        row = k // cols
        x0 = col * pitch_x
        y0 = -(row + 1) * pitch_y
        bb = cell["bb"]
        dx = x0 + margin - bb[0]
        dy = y0 + margin - bb[1]
        frame = [x0, y0, x0 + pitch_x, y0 + pitch_y]
        rep = cell["rep"]
        members = cell["group"]["members"]

        add_poly([[frame[0], frame[1]], [frame[2], frame[1]],
                  [frame[2], frame[3]], [frame[0], frame[3]],
                  [frame[0], frame[1]]], li_frame)
        for slab in rep["slabs"]:
            add_poly([[x + dx, y + dy] for x, y in slab["outer"]], li_outline)
            for ring in slab["inners"]:
                add_poly([[x + dx, y + dy] for x, y in ring], li_opening)
        for ring in rep["supports"]:
            add_poly([[x + dx, y + dy] for x, y in ring], li_support)

        names = [m["name"] for m in members]
        # a user-merged cell says so on the sheet: the outlines are the
        # representative's, and the other members' slabs only NEARLY match
        typ_tag = "TYP x{0} (MERGED)" if cell["group"].get("merged") \
            else "TYP x{0}"
        label = names[0] if len(names) == 1 else \
            "{0} {1}: {2}".format(names[0], typ_tag.format(len(names)),
                                  ", ".join(names))
        add_dot(label, frame[0] + margin * 0.25, frame[3] - margin * 0.25,
                li_label)

        # TYP grouping fingerprints SLABS only; members whose supports below
        # differ share this cell drawn with the representative's supports -
        # worth a line, since mid-span judgment reads off those footprints
        rep_sup = sorted(tuple(map(tuple, s)) for s in rep["supports"])
        for m in members[1:]:
            if sorted(tuple(map(tuple, s)) for s in m["supports"]) != rep_sup:
                log("NOTE: TYP member '{0}' has different supports below "
                    "than '{1}' - the cell shows '{1}'s".format(
                        m["name"], rep["name"]))

        # carry existing breaks in: the representative's set; warn when TYP
        # members disagree (the import will fan the sheet's version out)
        rep_breaks = existing.get(rep["name"], {})
        for m in members[1:]:
            other = existing.get(m["name"], {})
            a = sorted(_break_shape(e) for e in rep_breaks.get("breaks", []))
            b = sorted(_break_shape(e) for e in other.get("breaks", []))
            if a != b:
                log("WARNING: TYP members '{0}' and '{1}' carry DIFFERENT "
                    "existing break sets; the sheet shows '{0}' and the next "
                    "import will apply it to every member".format(
                        rep["name"], m["name"]))
        for entry in rep_breaks.get("breaks", []):
            pts = entry.get("polyline") or []
            if len(pts) >= 2:
                add_poly([[x + dx, y + dy] for x, y in pts], li_draw,
                         name=entry.get("note") or None,
                         curve_type=entry.get("curve_type"))
        for marker in rep_breaks.get("pour_markers", []):
            at = marker.get("at") or [0, 0]
            add_dot(str(marker.get("pour")), at[0] + dx, at[1] + dy, li_draw)

        meta_cells.append({
            "rep": rep["name"],
            "floors": {m["name"]: _rnd(m["z"]) for m in members},
            "frame": [_rnd(v) for v in frame],
            "offset": [_rnd(dx), _rnd(dy)],
        })

    title = ("BREAK SHEET{0} - draw pour-break curves anywhere inside a "
             "cell frame; a numbered text dot marks each pour region. "
             "Gray/black geometry is locked context. Save this file, then "
             "IMPORT SHEET in FormworkUI.".format(
                 " [OPTION: {0}]".format(option) if option else ""))
    add_dot(title, 0.0, margin * 0.5, li_label)

    meta = {
        "version": SCHEMA_VERSION,
        "docPath": doc.Path or "",
        "units": str(doc.ModelUnitSystem),
        "option": option,
        "cells": meta_cells,
        "written": None,   # stamped by the caller-side C# if ever needed
    }
    return f3, meta


def main():
    doc = Rhino.RhinoDoc.ActiveDoc
    log = fw.Log()
    log("breaksheet_gen - model units: {0}".format(doc.ModelUnitSystem))
    floors = collect_floors(doc, log)
    if not floors:
        if floors is not None:
            log("ERROR: no slabs found on SLAB_* layers - nothing to lay out")
        log.save(LOG_FILE)
        return None
    groups = group_floors(floors, doc, log)
    groups = apply_merges(groups, _read_merge_directives(doc, log), log)
    near = near_typ_pairs(groups, log)
    existing = _existing_breaks(doc, log)
    f3, meta = build_sheet(doc, groups, existing, log,
                           option=_active_option(doc, log))
    # the FormworkUI merge dialog reads both: suggestions still open, and
    # the merges currently applied (so unchecking one un-merges it)
    meta["near_typ"] = near
    meta["merged"] = [[m["name"] for m in g["members"]]
                      for g in groups if g.get("merged")]

    # write the sheet as a Rhino 7 file so a Rhino 7 host can read it back;
    # a Rhino 8 host reads both
    if not f3.Write(SHEET_FILE, 7):
        log("ERROR: could not write {0}".format(SHEET_FILE))
        log.save(LOG_FILE)
        return None
    with io.open(META_FILE, "w", encoding="utf-8") as fh:
        fh.write(u"{0}".format(json.dumps(meta, indent=1, sort_keys=True)))
    log("sheet -> {0} ({1} cells for {2} floors)".format(
        SHEET_FILE, len(meta["cells"]), len(floors)))
    log("meta  -> {0}".format(META_FILE))
    log.save(LOG_FILE)
    return meta


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with io.open(os.path.join(STAGE, "breaksheet_error.txt"), "w",
                     encoding="utf-8") as fh:
            fh.write(u"{0}".format(traceback.format_exc()))
        raise
    finally:
        # read-only on the doc: no Modified to clear; exit only in the
        # headless dev loop
        if os.environ.get("FW_HEADLESS") == "1":
            try:
                Rhino.RhinoApp.Exit()
            except Exception:
                Rhino.RhinoApp.RunScript("_-Exit", False)
