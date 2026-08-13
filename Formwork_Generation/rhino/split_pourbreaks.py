#! python 2
# -*- coding: utf-8 -*-
"""Split structural deck slabs at the approved Option-1 pour breaks.

Runs headless on a STAGED COPY of the model (never the original):
    Rhino.exe <staged copy>.3dm /nosplash
        /runscript="-_RunPythonScript <this file>"
Reads pour_breaks_model.json (feet, model world coords), splits every
SLAB_PT_* brep crossed by its floor's cut lines, tags pieces POUR1/POUR2 on
suffixed layers, and WriteFile()s a NEW derived .3dm — the opened copy is
never saved. Report: pourbreak_report.json (volumes, soffit areas, outlines).
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
BREAKS = os.path.join(STAGE, "pour_breaks_model.json")
OUT3DM = os.path.join(STAGE, "Bellwether_R7_pourbreaks.3dm")
REPORT = os.path.join(STAGE, "pourbreak_report.json")

import Rhino
from Rhino.Geometry import (AreaMassProperties, Brep, LineCurve, Point3d,
                            Surface, Vector3d, VolumeMassProperties)

import formwork_gen_rhino as fw

EXTENSIONS = (15.0, 60.0, 150.0)     # progressive cut-line extension, ft


def cutter_brep(cut, ext, z0, z1):
    a, b = cut["span_ft"][0] - ext, cut["span_ft"][1] + ext
    p = cut["pos_ft"]
    if cut["dir"] == "NS":
        line = LineCurve(Point3d(p, a, z0), Point3d(p, b, z0))
    else:
        line = LineCurve(Point3d(a, p, z0), Point3d(b, p, z0))
    srf = Surface.CreateExtrusion(line, Vector3d(0, 0, z1 - z0))
    return Brep.TryConvertBrep(srf)


def side_key(cuts, x, y):
    return tuple((x if c["dir"] == "NS" else y) > c["pos_ft"] for c in cuts)


def split_with_cuts(brep, cuts, tol, log, why):
    """Apply every cut; returns (pieces, ok). Progressive extension per cut,
    CreateBooleanSplit fallback when capping fails, sliver guard against
    tangent cuts."""
    bb = brep.GetBoundingBox(True)
    pieces = [brep]
    crossed = False
    for ci, cut in enumerate(cuts):
        nxt = []
        for pc in pieces:
            done = None
            for ext in EXTENSIONS:
                cutter = cutter_brep(cut, ext, bb.Min.Z - 2, bb.Max.Z + 2)
                if cutter is None:
                    why.append("cut{0}: no cutter".format(ci))
                    continue
                # doc tolerance can be absurdly tight (1e-5 ft on this
                # model); Split needs slack to close the section curves.
                for mult in (1.0, 10.0, 100.0):
                    t = tol * mult
                    parts = pc.Split(cutter, t)
                    if parts and len(parts) >= 2:
                        capped = [part.CapPlanarHoles(t) for part in parts]
                        if all(c is not None and c.IsSolid for c in capped):
                            done = capped
                            if mult > 1:
                                why.append("cut{0}: split@tol*{1:g}".format(
                                    ci, mult))
                            break
                    try:
                        bs = Brep.CreateBooleanSplit(pc, cutter, t)
                    except Exception:
                        bs = None
                    if bs and len(bs) >= 2 and all(b.IsSolid for b in bs):
                        done = list(bs)
                        why.append("cut{0}: boolean@tol*{1:g}".format(
                            ci, mult))
                        break
                    why.append("cut{0}: {2}@{1:g}/x{3:g}".format(
                        ci, ext, "cap-fail" if parts and len(parts) >= 2
                        else "no-int", mult))
                if done is not None:
                    break
            if done is not None:
                nxt.extend(done)
                crossed = True
            else:
                nxt.append(pc)
        pieces = nxt
    if not crossed:
        return [brep], False
    vol0 = VolumeMassProperties.Compute(brep)
    vols = [VolumeMassProperties.Compute(p) for p in pieces]
    if vol0 and all(vols):
        total = sum(v.Volume for v in vols)
        rel = abs(total - vol0.Volume) / vol0.Volume
        if rel > 0.01:
            why.append("VOLUME MISMATCH {0:.4f}".format(rel))
            log("    VOLUME MISMATCH {0:.2%} — keeping slab uncut".format(rel))
            return [brep], False
        if min(v.Volume for v in vols) < 0.01 * vol0.Volume:
            why.append("tangent cut (sliver piece) — reverted")
            return [brep], False
    return pieces, True


def soffit_area_and_outline(brep, tol, want_outline):
    cos20 = math.cos(math.radians(20.0))
    area = 0.0
    outlines = []
    for face, _z in fw.soffit_faces(brep, cos20):
        amp = AreaMassProperties.Compute(face)
        if amp:
            area += amp.Area
        if want_outline:
            outer, _inners = fw.face_loops(face, tol)
            if outer is not None:
                outlines.append(fw._curve_points_m(outer, 1.0))
    return area, outlines


def main():
    doc = Rhino.RhinoDoc.ActiveDoc
    tol = doc.ModelAbsoluteTolerance
    log = fw.Log()
    if doc.ModelUnitSystem != Rhino.UnitSystem.Feet:
        log("WARNING: model units {0}, expected Feet — aborting".format(
            doc.ModelUnitSystem))
        return

    data = json.loads(io.open(BREAKS, encoding="utf-8").read())
    floors_cfg = data["floors"]
    floor_elev = fw.read_floor_elevations(doc)
    if not floor_elev:
        log("no FloorElevations doc string — aborting")
        return

    def floor_of(bb_min_z):
        return floor_elev[min(floor_elev, key=lambda e: abs(e - bb_min_z))]

    def pour_layer(orig_layer, pour):
        name = orig_layer.Name + "_POUR{0}".format(pour)
        for l in doc.Layers:
            if l is not None and not l.IsDeleted and l.Name == name \
                    and l.ParentLayerId == orig_layer.ParentLayerId:
                return l.Index
        layer = Rhino.DocObjects.Layer()
        layer.Name = name
        layer.Color = orig_layer.Color
        layer.ParentLayerId = orig_layer.ParentLayerId
        return doc.Layers.Add(layer)

    targets = []
    for obj in doc.Objects:
        if obj is None or obj.Attributes is None:
            continue
        layer = doc.Layers[obj.Attributes.LayerIndex]
        lname = (layer.Name if layer else "")
        low = lname.lower()
        if not low.split("_")[0].startswith("slab"):
            continue
        if "sog" in low or "topping" in low:
            continue
        geom = obj.Geometry
        if isinstance(geom, Rhino.Geometry.Extrusion):
            geom = geom.ToBrep(True)
        if isinstance(geom, Brep):
            targets.append((obj, geom, layer))
    log("targets: {0} structural deck slabs".format(len(targets)))

    report = {"floors": {}, "grid_x": data["grid_x"],
              "grid_y": data["grid_y"]}
    n_cut = 0
    for obj, brep, layer in targets:
        bb = brep.GetBoundingBox(True)
        fl = floor_of(bb.Min.Z)
        cfg = floors_cfg.get(fl)
        frep = report["floors"].setdefault(fl, {
            "cuts": cfg["cuts"] if cfg else [],
            "pdf_sf": cfg["pdf_sf"] if cfg else None, "slabs": []})
        want_outline = (fl == "L01")
        v0 = VolumeMassProperties.Compute(brep)
        srec = {"layer": layer.Name, "orig_vol_cf":
                round(v0.Volume, 1) if v0 else None, "pieces": []}
        frep["slabs"].append(srec)
        if cfg is None:
            srec["status"] = "no break for floor"
            a, o = soffit_area_and_outline(brep, tol, want_outline)
            srec["pieces"].append({"pour": 0, "soffit_sf": round(a, 1),
                                   "outlines": o if want_outline else []})
            continue
        why = []
        pieces, ok = split_with_cuts(brep, cfg["cuts"], tol, log, why)
        srec["why"] = why
        if not ok:
            srec["status"] = "not crossed"
            a, o = soffit_area_and_outline(brep, tol, want_outline)
            srec["pieces"].append({"pour": 0, "soffit_sf": round(a, 1),
                                   "outlines": o if want_outline else []})
            continue
        srec["status"] = "split into {0}".format(len(pieces))
        n_cut += 1
        pc_cen = cfg.get("pour1_centroid_ft")
        pour1_key = (side_key(cfg["cuts"], pc_cen[0], pc_cen[1])
                     if pc_cen else None)
        for pc in pieces:
            vm = VolumeMassProperties.Compute(pc)
            cen = vm.Centroid if vm else pc.GetBoundingBox(True).Center
            key = side_key(cfg["cuts"], cen.X, cen.Y)
            pour = 1 if (pour1_key is not None and key == pour1_key) else 2
            a, o = soffit_area_and_outline(pc, tol, want_outline)
            attr = obj.Attributes.Duplicate()
            attr.LayerIndex = pour_layer(layer, pour)
            attr.SetUserString("POUR", str(pour))
            attr.SetUserString("POUR_FLOOR", fl)
            doc.Objects.AddBrep(pc, attr)
            srec["pieces"].append({
                "pour": pour,
                "vol_cf": round(vm.Volume, 1) if vm else None,
                "soffit_sf": round(a, 1),
                "centroid": [round(cen.X, 2), round(cen.Y, 2)],
                "outlines": o if want_outline else []})
        doc.Objects.Delete(obj, True)
    log("split {0} slabs".format(n_cut))

    opts = Rhino.FileIO.FileWriteOptions()
    opts.SuppressDialogBoxes = True
    ok = doc.WriteFile(OUT3DM, opts)
    log("derived model -> {0} ({1})".format(OUT3DM, "ok" if ok
                                            else "WRITE FAILED"))
    with io.open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(u"{0}".format(json.dumps(report, indent=1)))
    log("report -> {0}".format(REPORT))


try:
    main()
except Exception:
    with io.open(os.path.join(STAGE, "pourbreak_error.txt"), "w",
                 encoding="utf-8") as fh:
        fh.write(u"{0}".format(traceback.format_exc()))
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
