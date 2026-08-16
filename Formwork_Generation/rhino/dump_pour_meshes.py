#! python 2
# -*- coding: utf-8 -*-
"""Dump the split POUR1/POUR2 slab pieces as triangle meshes (MODEL units,
world coords) for the IFC patcher. The JSON carries meters_per_unit so the
consumer scales correctly for ft, inch and metric models alike. Run headless
on the DERIVED pourbreaks model:
    Rhino.exe Bellwether_R7_pourbreaks.3dm /nosplash
        /runscript="-_RunPythonScript <this file>"
Writes pour_meshes.json to the staging folder. Read-only — saves nothing.
"""
from __future__ import division, print_function

import io
import json
import os
import traceback

STAGE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "qto_fw_test")
OUT = os.path.join(STAGE, "pour_meshes.json")

import Rhino
from Rhino.Geometry import Brep, Mesh, MeshingParameters, VolumeMassProperties

try:
    doc = Rhino.RhinoDoc.ActiveDoc
    pieces = []
    for obj in doc.Objects:
        if obj is None or obj.Attributes is None:
            continue
        layer = doc.Layers[obj.Attributes.LayerIndex]
        if layer is None or "_POUR" not in layer.Name.upper():
            continue
        geom = obj.Geometry
        if isinstance(geom, Rhino.Geometry.Extrusion):
            geom = geom.ToBrep(True)
        if not isinstance(geom, Brep):
            continue
        big = Mesh()
        parts = Mesh.CreateFromBrep(geom, MeshingParameters.FastRenderMesh)
        if not parts:
            continue
        for m in parts:
            big.Append(m)
        big.Faces.ConvertQuadsToTriangles()
        big.Compact()
        # Point3f/Single leaks through IronPython's float() into json;
        # ToPoint3dArray gives real Doubles (== IronPython float)
        verts = [[v.X * 1.0, v.Y * 1.0, v.Z * 1.0]
                 for v in big.Vertices.ToPoint3dArray()]
        faces = [[f.A + 0, f.B + 0, f.C + 0] for f in big.Faces]
        vm = VolumeMassProperties.Compute(geom)
        bb = geom.GetBoundingBox(True)
        pieces.append({
            "layer": str(layer.Name),
            "pour": str(obj.Attributes.GetUserString("POUR") or "?"),
            "floor": str(obj.Attributes.GetUserString("POUR_FLOOR") or "?"),
            "vol_cf": float(vm.Volume) if vm else None,
            "bbox": [float(c) for c in
                     (bb.Min.X, bb.Min.Y, bb.Min.Z,
                      bb.Max.X, bb.Max.Y, bb.Max.Z)],
            "verts": verts,
            "faces": faces,
        })
    # meters_per_unit is the authoritative scale for the consumer; the
    # units name is informational. The old dump stamped "ft"
    # unconditionally while writing raw model units, which corrupted
    # inch and metric models at the IFC-patch hop.
    with io.open(OUT, "w", encoding="utf-8") as fh:
        fh.write(u"{0}".format(json.dumps({
            "units": str(doc.ModelUnitSystem),
            "meters_per_unit": float(Rhino.RhinoMath.UnitScale(
                doc.ModelUnitSystem, Rhino.UnitSystem.Meters)),
            "pieces": pieces})))
    Rhino.RhinoApp.WriteLine("dumped {0} pieces".format(len(pieces)))
except Exception:
    with io.open(os.path.join(STAGE, "dump_error.txt"), "w",
                 encoding="utf-8") as fh:
        fh.write(u"{0}".format(traceback.format_exc()))
finally:
    # clear Modified ONLY in headless throwaway runs - on a live document
    # it would mask the user's own unsaved edits
    if os.environ.get("FW_HEADLESS") == "1":
        try:
            Rhino.RhinoDoc.ActiveDoc.Modified = False
        except Exception:
            pass
        try:
            Rhino.RhinoApp.Exit()
        except Exception:
            Rhino.RhinoApp.RunScript("_-Exit", False)
