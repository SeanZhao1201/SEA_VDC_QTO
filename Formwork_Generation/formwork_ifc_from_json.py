# -*- coding: utf-8 -*-
"""JSON -> IFC4 converter for the Rhino formwork generator.

`rhino/formwork_gen_rhino.py` (export mode) writes `<name>_formwork.json`
with per-level platform regions/holes and per-prop coordinates, all in
metres, absolute world coordinates. This script writes the matching IFC:

* IFC4, length unit **mm**, geometry absolute — co-registers with QTO_Tool's
  structural IFC exactly like the legacy generator's output did;
* spatial chain IfcProject -> IfcSite -> IfcBuilding -> one IfcBuildingStorey
  per floor name; one IfcElementAssembly per level;
* IfcBuildingElementProxy per element, ObjectType `platform` | `support`;
* pset "QTO Properties": FLOOR (the 4D search-set join key), plus STATUS and
  HEIGHT_M on supports.

Usage:
    python formwork_ifc_from_json.py --json out/..._formwork.json \
        --out out/..._formwork.ifc

Requires ifcopenshell + shapely (same venv as the analysis scripts).
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import ifcopenshell
import ifcopenshell.guid
from shapely.geometry import Point, Polygon

MM = 1000.0          # metres -> millimetres


def _floor_sort_key(name):
    if name and name[0] in "Ll" and name[1:].isdigit():
        return (0, int(name[1:]))
    return (1, name or "")


class Writer:
    def __init__(self):
        f = self.f = ifcopenshell.file(schema="IFC4")
        origin = f.create_entity("IfcCartesianPoint", [0.0, 0.0, 0.0])
        z = f.create_entity("IfcDirection", [0.0, 0.0, 1.0])
        x = f.create_entity("IfcDirection", [1.0, 0.0, 0.0])
        self.wcs = f.create_entity("IfcAxis2Placement3D", origin, z, x)
        length = f.create_entity("IfcSIUnit", None, "LENGTHUNIT", "MILLI",
                                 "METRE")
        units = f.create_entity("IfcUnitAssignment", [length])
        self.ctx = f.create_entity(
            "IfcGeometricRepresentationContext",
            None, "Model", 3, 1.0e-5, self.wcs, None)
        self.body = f.create_entity(
            "IfcGeometricRepresentationSubContext",
            "Body", "Model", None, None, None, None,
            self.ctx, None, "MODEL_VIEW", None)
        self.owner = None
        self.project = f.create_entity(
            "IfcProject", self._guid(), self.owner,
            "Formwork Soffit (ray-cast)", None, None, None, None,
            [self.ctx], units)
        self.site = f.create_entity(
            "IfcSite", self._guid(), self.owner, "Site", None, None,
            self._place(), None, None, "ELEMENT",
            None, None, None, None, None)
        self.building = f.create_entity(
            "IfcBuilding", self._guid(), self.owner, "Building", None, None,
            self._place(), None, None, "ELEMENT", None, None, None)
        self._aggregate(self.project, [self.site])
        self._aggregate(self.site, [self.building])

    def _guid(self):
        return ifcopenshell.guid.new()

    def _place(self):
        return self.f.create_entity("IfcLocalPlacement", None, self.wcs)

    def _aggregate(self, whole, parts):
        self.f.create_entity("IfcRelAggregates", self._guid(), self.owner,
                             None, None, whole, parts)

    def storey(self, name, elev_m):
        return self.f.create_entity(
            "IfcBuildingStorey", self._guid(), self.owner, name, None, None,
            self._place(), None, None, "ELEMENT", elev_m * MM)

    def _ring(self, coords):
        pts = [self.f.create_entity(
            "IfcCartesianPoint", [x * MM, y * MM]) for x, y in coords[:-1]]
        pts.append(pts[0])
        return self.f.create_entity("IfcPolyline", pts)

    def _extruded(self, outer, holes, top_z_m, depth_m):
        rings = [self._ring(h) for h in holes]
        if rings:
            profile = self.f.create_entity(
                "IfcArbitraryProfileDefWithVoids", "AREA", None,
                self._ring(outer), rings)
        else:
            profile = self.f.create_entity(
                "IfcArbitraryClosedProfileDef", "AREA", None,
                self._ring(outer))
        base = self.f.create_entity(
            "IfcAxis2Placement3D",
            self.f.create_entity("IfcCartesianPoint",
                                 [0.0, 0.0, (top_z_m - depth_m) * MM]),
            self.f.create_entity("IfcDirection", [0.0, 0.0, 1.0]),
            self.f.create_entity("IfcDirection", [1.0, 0.0, 0.0]))
        return self.f.create_entity(
            "IfcExtrudedAreaSolid", profile,
            base, self.f.create_entity("IfcDirection", [0.0, 0.0, 1.0]),
            depth_m * MM)

    def proxy(self, name, obj_type, solid):
        rep = self.f.create_entity(
            "IfcShapeRepresentation", self.body, "Body", "SweptSolid",
            [solid])
        shape = self.f.create_entity(
            "IfcProductDefinitionShape", None, None, [rep])
        return self.f.create_entity(
            "IfcBuildingElementProxy", self._guid(), self.owner, name, None,
            obj_type, self._place(), shape, None, None)

    def pset(self, elements, props):
        entities = [self.f.create_entity(
            "IfcPropertySingleValue", k, None,
            self.f.create_entity("IfcLabel", str(v)), None)
            for k, v in props.items()]
        pset = self.f.create_entity(
            "IfcPropertySet", self._guid(), self.owner, "QTO Properties",
            None, entities)
        self.f.create_entity(
            "IfcRelDefinesByProperties", self._guid(), self.owner, None,
            None, elements, pset)

    def contain(self, storey, elements):
        self.f.create_entity(
            "IfcRelContainedInSpatialStructure", self._guid(), self.owner,
            "Formwork", None, elements, storey)


def convert(json_path, out_path):
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    panel = data["panel_thickness"]
    prop_size = data["prop_size"]
    w = Writer()

    floor_z = {}
    for lv in data["levels"]:
        fl = lv["floor"]
        floor_z[fl] = min(floor_z.get(fl, lv["z"]), lv["z"])
    storeys = {fl: w.storey(fl, z) for fl, z in
               sorted(floor_z.items(), key=lambda kv: _floor_sort_key(kv[0]))}
    w._aggregate(w.building, list(storeys.values()))

    n_plat = n_sup = 0
    counts = defaultdict(int)
    for lv in data["levels"]:
        fl, z = lv["floor"], lv["z"]
        elements = []
        regions = [Polygon(r) for r in lv["regions"]]
        holes = lv["holes"]
        for poly, coords in zip(regions, lv["regions"]):
            my_holes = [h for h in holes
                        if poly.covers(Point(h[0][0], h[0][1]))]
            solid = w._extruded(coords, my_holes, z, panel)
            el = w.proxy("Formwork Soffit Platform", "platform", solid)
            w.pset([el], {"FLOOR": fl})
            elements.append(el)
            n_plat += 1
        for p in lv["props"]:
            if p["status"] == "NOHIT_SKIPPED" or p["foot_z"] is None:
                continue
            half = prop_size / 2.0
            outer = [[p["x"] - half, p["y"] - half],
                     [p["x"] + half, p["y"] - half],
                     [p["x"] + half, p["y"] + half],
                     [p["x"] - half, p["y"] + half],
                     [p["x"] - half, p["y"] - half]]
            solid = w._extruded(outer, [], p["top_z"],
                                p["top_z"] - p["foot_z"])
            el = w.proxy("Formwork Support", "support", solid)
            w.pset([el], {
                "FLOOR": fl, "STATUS": p["status"],
                "HEIGHT_M": "{0:.3f}".format(p["top_z"] - p["foot_z"])})
            elements.append(el)
            n_sup += 1
            counts[p["status"]] += 1
        if not elements:
            continue
        assembly = w.f.create_entity(
            "IfcElementAssembly", w._guid(), w.owner,
            "Formwork Soffit {0} @{1:.2f}m".format(fl, z), None, None,
            w._place(), None, None, "NOTDEFINED", "NOTDEFINED")
        w._aggregate(assembly, elements)
        w.contain(storeys[fl], [assembly])

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    w.f.write(str(out_path))
    print("wrote {0}: {1} platforms + {2} supports ({3}) across {4} storeys"
          .format(out_path, n_plat, n_sup, dict(counts), len(storeys)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    convert(a.json, a.out)


if __name__ == "__main__":
    main()
