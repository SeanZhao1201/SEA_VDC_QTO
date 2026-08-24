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


def _ifc_guid(rhino_object_id):
    """IFC GlobalId of a Rhino object id, or None.

    The standard IFC base64 compression - byte-for-byte what xBIM's
    IfcGloballyUniqueId.ConvertToBase64 produces (verified 2026-08-20 over
    four vectors including all-zero and all-F), which is exactly how the QTO
    take-off export identifies that same slab.
    """
    if not rhino_object_id:
        return None
    try:
        return ifcopenshell.guid.compress(str(rhino_object_id).replace("-", ""))
    except Exception:
        return None


def convert(json_path, out_path, sideforms_path=None):
    data = {"levels": [], "panel_thickness": 0.05, "prop_size": 0.15}
    if json_path:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    sides = None
    if sideforms_path:
        sides = json.loads(
            Path(sideforms_path).read_text(encoding="utf-8"))
    panel = data["panel_thickness"]
    prop_size = data["prop_size"]
    w = Writer()

    floor_z = {}
    for lv in data["levels"]:
        fl = lv["floor"]
        floor_z[fl] = min(floor_z.get(fl, lv["z"]), lv["z"])
    if sides:
        for pn in sides["panels"]:
            fl = pn["floor"]
            floor_z[fl] = min(floor_z.get(fl, pn["z0"]), pn["z0"])
    # storeys ordered by elevation, not by parsing the floor name — names
    # are whatever the QTO user typed and carry no format guarantee
    storeys = {fl: w.storey(fl, z) for fl, z in
               sorted(floor_z.items(), key=lambda kv: kv[1])}
    if storeys:
        # an IfcRelAggregates with empty RelatedObjects is schema-invalid
        w._aggregate(w.building, list(storeys.values()))

    n_plat = n_sup = 0
    counts = defaultdict(int)
    for lv in data["levels"]:
        fl, z = lv["floor"], lv["z"]
        # the generator names a level after what it FORMS (pour, or the
        # slab); the "@37.26m" fallback only fires for a pre-2026-08-20 JSON
        pour = lv.get("pour")
        # The take-off export derives every element's IfcGlobalId from its
        # Rhino object id (IFCMethods.SetDeterministicGlobalId), so the same
        # compression here yields the GlobalId of the slab this formwork
        # forms - a link a 4D consumer can RESOLVE, instead of re-deriving
        # the correspondence from FLOOR + POUR (Mast4D, 2026-08-20).
        slab_guids = [g for g in (_ifc_guid(i)
                                  for i in lv.get("slab_ids") or []) if g]
        lv_name = lv.get("name") or "Formwork Soffit {0} @{1:.2f}m".format(fl, z)
        elements = []
        regions = [Polygon(r) for r in lv["regions"]]
        holes = lv["holes"]
        for poly, coords in zip(regions, lv["regions"]):
            # membership by a point guaranteed inside the hole — the
            # first VERTEX of a hole can sit on a shared boundary and
            # miss every region
            my_holes = [h for h in holes
                        if poly.covers(Polygon(h).representative_point())]
            solid = w._extruded(coords, my_holes, z, panel)
            el = w.proxy("Formwork Soffit Platform", "platform", solid)
            plat_props = {"FLOOR": fl, "SOFFIT_Z_M": "{0:.3f}".format(z)}
            if pour:
                plat_props["POUR"] = str(pour)
            if slab_guids:
                plat_props["SLAB_GLOBALID"] = ", ".join(slab_guids)
            w.pset([el], plat_props)
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
            sup_props = {
                "FLOOR": fl, "STATUS": p["status"],
                "HEIGHT_M": "{0:.3f}".format(p["top_z"] - p["foot_z"])}
            if pour:
                sup_props["POUR"] = str(pour)
            if slab_guids:
                sup_props["SLAB_GLOBALID"] = ", ".join(slab_guids)
            w.pset([el], sup_props)
            elements.append(el)
            n_sup += 1
            counts[p["status"]] += 1
        if not elements:
            continue
        assembly = w.f.create_entity(
            "IfcElementAssembly", w._guid(), w.owner, lv_name, None, None,
            w._place(), None, None, "NOTDEFINED", "NOTDEFINED")
        asm_props = {"FLOOR": fl, "SOFFIT_Z_M": "{0:.3f}".format(z)}
        if pour:
            asm_props["POUR"] = str(pour)
        if lv.get("slabs"):
            asm_props["SLABS"] = ", ".join(lv["slabs"])
        if slab_guids:
            asm_props["SLAB_GLOBALID"] = ", ".join(slab_guids)
        w.pset([assembly], asm_props)
        w._aggregate(assembly, elements)
        w.contain(storeys[fl], [assembly])

    n_side = n_bulk = 0
    if sides:
        by_floor = defaultdict(list)
        for pn in sides["panels"]:
            depth = pn["z1"] - pn["z0"]
            if depth <= 0 or len(pn["profile"]) < 4:
                continue
            holes = [pn["hole"]] if pn.get("hole") else []
            solid = w._extruded(pn["profile"], holes, pn["z1"], depth)
            # GENERIC element names, deliberately. Floor and pour live in
            # the pset, never in the name: the 4D consumer binds geometry to
            # schedule tasks by property EQUALITY only (no regex, no
            # starts-with), so a name that varies per floor per pour becomes
            # one binding rule per string - 71 of them on this model, to be
            # rewritten for every new building (Mast4D, 2026-08-20). The
            # human-readable naming belongs on the ASSEMBLY above these, and
            # that is where it stays.
            name = "Side Form" if pn["type"] == "side" else "Bulkhead"
            el = w.proxy(name, pn["type"], solid)
            props = {"FLOOR": pn["floor"], "TYPE": pn["type"],
                     "AREA_M2": "{0:.3f}".format(pn["area_m2"])}
            if pn.get("pour") is not None:
                # pour lives in the pset, not the assembly tree — 4D
                # search sets filter on QTO Properties (decided v1)
                props["POUR"] = str(pn["pour"])
            w.pset([el], props)
            by_floor[pn["floor"]].append(el)
            if pn["type"] == "side":
                n_side += 1
            else:
                n_bulk += 1
        for fl, elements in by_floor.items():
            assembly = w.f.create_entity(
                "IfcElementAssembly", w._guid(), w.owner,
                "Formwork Sides {0}".format(fl), None, None,
                w._place(), None, None, "NOTDEFINED", "NOTDEFINED")
            w._aggregate(assembly, elements)
            w.contain(storeys[fl], [assembly])

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    w.f.write(str(out_path))
    print("wrote {0}: {1} platforms + {2} supports ({3}) + {4} side forms "
          "+ {5} bulkheads across {6} storeys".format(
              out_path, n_plat, n_sup, dict(counts), n_side, n_bulk,
              len(storeys)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", help="formwork JSON (platforms + props)")
    ap.add_argument("--sideforms",
                    help="side-form JSON from sideform_gen_rhino.py")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if not a.json and not a.sideforms:
        ap.error("need --json and/or --sideforms")
    convert(a.json, a.out, a.sideforms)


if __name__ == "__main__":
    main()
