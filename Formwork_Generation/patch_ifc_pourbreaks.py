# -*- coding: utf-8 -*-
"""Patch the QTO structural IFC with the pour-break split slabs.

Takes the original QTO export (all elements + QTO Properties intact) and the
mesh dump of the split POUR1/POUR2 pieces (from rhino/dump_pour_meshes.py),
then surgically replaces each split IfcSlab with its two pieces:

* piece geometry: IfcFaceBasedSurfaceModel triangles, absolute world mm —
  identical convention to QTO_Tool's exporter, so everything co-registers;
* piece psets: the original's non-quantity "QTO Properties" (FLOOR etc.)
  plus POUR, VOLUME_CY (recomputed for the piece), SOURCE_SLAB; stale
  full-slab quantity values are dropped rather than copied wrongly;
* containment: the original slab's storey. Every other entity is untouched.

Usage:
    python patch_ifc_pourbreaks.py --ifc sample/Sunbreak_TestV5.ifc \
        --meshes %LOCALAPPDATA%/qto_fw_test/pour_meshes.json \
        --out out/Bellwether_R7_pourbreaks.ifc
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.geom
import ifcopenshell.guid
import ifcopenshell.util.element as _ue
import numpy as np

FT = 0.3048
MM_PER_FT = 304.8
CY_PER_CF = 0.037037                 # QTO_Tool's ft-model volume factor
QUANTITY_KEYS = ("VOLUME", "AREA", "PERIMETER", "LENGTH", "HEIGHT")


def slab_bbox_m(f, settings, slab):
    shape = ifcopenshell.geom.create_shape(settings, slab)
    v = np.asarray(shape.geometry.verts, float).reshape(-1, 3)
    return v.min(axis=0), v.max(axis=0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ifc", required=True)
    ap.add_argument("--meshes", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    data = json.loads(Path(a.meshes).read_text(encoding="utf-8"))
    pieces = data["pieces"]
    f = ifcopenshell.open(a.ifc)
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)

    # bboxes of every existing IfcSlab (metres)
    slabs = []
    for slab in f.by_type("IfcSlab"):
        try:
            lo, hi = slab_bbox_m(f, settings, slab)
        except Exception:
            continue
        slabs.append((slab, lo, hi))

    # match every piece to the smallest slab bbox that contains it
    tol = 0.15
    by_slab = defaultdict(list)
    unmatched = 0
    for pc in pieces:
        b = pc["bbox"]
        lo_p = np.array([b[0], b[1], b[2]]) * FT
        hi_p = np.array([b[3], b[4], b[5]]) * FT
        best = None
        for slab, lo, hi in slabs:
            if np.all(lo_p >= lo - tol) and np.all(hi_p <= hi + tol):
                size = float(np.prod(hi - lo))
                if best is None or size < best[1]:
                    best = (slab, size)
        if best is None:
            unmatched += 1
            print("UNMATCHED piece", pc["layer"], pc["floor"], b[:3])
        else:
            by_slab[best[0]].append(pc)

    print("pieces: {0} -> {1} original slabs, {2} unmatched".format(
        len(pieces), len(by_slab), unmatched))

    ctx = next(c for c in f.by_type("IfcGeometricRepresentationContext")
               if c.ContextType == "Model"
               and not c.is_a("IfcGeometricRepresentationSubContext"))
    origin = f.create_entity("IfcCartesianPoint", [0.0, 0.0, 0.0])
    wcs = f.create_entity(
        "IfcAxis2Placement3D", origin,
        f.create_entity("IfcDirection", [0.0, 0.0, 1.0]),
        f.create_entity("IfcDirection", [1.0, 0.0, 0.0]))

    def fbsm(verts_ft, faces):
        pts = [f.create_entity(
            "IfcCartesianPoint",
            [v[0] * MM_PER_FT, v[1] * MM_PER_FT, v[2] * MM_PER_FT])
            for v in verts_ft]
        ifc_faces = []
        for tri in faces:
            loop = f.create_entity("IfcPolyLoop",
                                   [pts[tri[0]], pts[tri[1]], pts[tri[2]]])
            bound = f.create_entity("IfcFaceOuterBound", loop, True)
            ifc_faces.append(f.create_entity("IfcFace", [bound]))
        cfs = f.create_entity("IfcConnectedFaceSet", ifc_faces)
        return f.create_entity("IfcFaceBasedSurfaceModel", [cfs])

    n_new = 0
    for slab, pcs in sorted(by_slab.items(), key=lambda kv: kv[0].id()):
        psets = _ue.get_psets(slab)
        qto = psets.get("QTO Properties", {}) or {}
        keep = {k: v for k, v in qto.items()
                if k != "id" and not any(q in k.upper()
                                         for q in QUANTITY_KEYS)}
        storey = _ue.get_container(slab)
        rel = None
        if storey is not None and storey.ContainsElements:
            rel = storey.ContainsElements[0]
        for pc in sorted(pcs, key=lambda p: p["pour"]):
            solid = fbsm(pc["verts"], pc["faces"])
            rep = f.create_entity(
                "IfcShapeRepresentation", ctx, "Body", "SurfaceModel",
                [solid])
            shape = f.create_entity(
                "IfcProductDefinitionShape", None, None, [rep])
            place = f.create_entity("IfcLocalPlacement", None, wcs)
            new = f.create_entity(
                "IfcSlab", ifcopenshell.guid.new(), None,
                "{0} - POUR{1}".format(slab.Name or "Slab", pc["pour"]),
                None, slab.ObjectType, place, shape, None,
                slab.PredefinedType)
            props = dict(keep)
            props["POUR"] = pc["pour"]
            props["VOLUME"] = "{0:.2f}".format(
                (pc["vol_cf"] or 0.0) * CY_PER_CF)
            props["SOURCE_SLAB"] = slab.Name or slab.GlobalId
            entities = [f.create_entity(
                "IfcPropertySingleValue", k, None,
                f.create_entity("IfcLabel", str(v)), None)
                for k, v in props.items()]
            pset = f.create_entity(
                "IfcPropertySet", ifcopenshell.guid.new(), None,
                "QTO Properties", None, entities)
            f.create_entity(
                "IfcRelDefinesByProperties", ifcopenshell.guid.new(), None,
                None, None, [new], pset)
            if rel is not None:
                rel.RelatedElements = list(rel.RelatedElements) + [new]
            n_new += 1
        ifcopenshell.api.run("root.remove_product", f, product=slab)

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    f.write(a.out)
    print("wrote {0}: replaced {1} slabs with {2} pour pieces".format(
        a.out, len(by_slab), n_new))


if __name__ == "__main__":
    main()
