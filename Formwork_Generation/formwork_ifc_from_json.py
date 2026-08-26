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

Merge mode (`--into`): instead of authoring a fresh spatial tree, open the
take-off IFC the QTO plugin exported and append the temp works INTO it —
each assembly contained in the EXISTING IfcBuildingStorey whose Name equals
the element's FLOOR (exact string match; both vocabularies come from
FloorElevations). One file, one storey tree, geometry co-registered by
construction (both sides are IFC4 / mm / absolute world coordinates). The
take-off entities are never touched; a FLOOR with no matching storey aborts
before anything is written.

    python formwork_ifc_from_json.py --json ... --sideforms ... \
        --jumpforms ... --into Tested3.ifc --out out/..._unified.ifc

Requires ifcopenshell + shapely (same venv as the analysis scripts).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from collections import defaultdict
from pathlib import Path

import ifcopenshell
import ifcopenshell.guid
from shapely.geometry import Point, Polygon

MM = 1000.0          # metres -> millimetres


class Writer:
    """Fresh IFC4 file by default; `into=` opens an existing take-off IFC
    and appends instead (same entity-authoring methods, no new spatial
    tree — the caller containers into the file's own storeys)."""

    def __init__(self, into=None):
        # every GlobalId this run mints or inherits — the guard that keeps
        # the merged file free of duplicates (temp-works ids are random per
        # run by documented trait, so uniqueness must be enforced, not
        # assumed)
        self._used_guids = set()
        if into is not None:
            self._open_into(into)
            return
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

    def _open_into(self, into):
        """Open the take-off IFC for in-place appending (merge mode).

        Mirrors patch_ifc_pourbreaks.py: reuse the file's Model context,
        author a fresh world-origin placement, and never touch existing
        entities. Guards the contract assumptions loudly instead of
        co-registering garbage: IFC4 schema, millimetre length unit
        (the geometry below is written in absolute mm), and a target
        that is the actual take-off export — not a previous merge
        result, which would silently double every element.
        """
        if not os.path.isfile(str(into)):
            raise SystemExit(
                "--into {0} does not exist or is not a file".format(into))
        try:
            f = self.f = ifcopenshell.open(str(into))
        except Exception as e:
            raise SystemExit(
                "cannot open --into {0}: {1}".format(into, e))
        if f.schema != "IFC4":
            raise SystemExit(
                "--into {0} is {1}, not IFC4 - the temp works and the "
                "take-off export are IFC4-only".format(into, f.schema))
        mm = False
        for ua in f.by_type("IfcUnitAssignment"):
            for u in ua.Units:
                if (u.is_a("IfcSIUnit") and u.UnitType == "LENGTHUNIT"
                        and u.Prefix == "MILLI" and u.Name == "METRE"):
                    mm = True
        if not mm:
            raise SystemExit(
                "--into {0} does not declare millimetre length units - "
                "this writer emits absolute mm and would not "
                "co-register".format(into))
        try:
            self.ctx = next(
                c for c in f.by_type("IfcGeometricRepresentationContext")
                if c.ContextType == "Model"
                and not c.is_a("IfcGeometricRepresentationSubContext"))
        except StopIteration:
            raise SystemExit(
                "--into {0} has no Model representation context".format(into))
        # a previous merge result passes every other guard by construction
        # (same schema/units/context/storeys), and re-merging into it would
        # append a second, geometrically coincident copy of every element
        # under fresh random GlobalIds - detect this writer's own ObjectType
        # vocabulary and refuse
        own_types = ("platform", "support", "side", "bulkhead",
                     "jumpform", "reshore")
        prior = [p for p in f.by_type("IfcBuildingElementProxy")
                 if p.ObjectType in own_types]
        if prior:
            raise SystemExit(
                "--into {0} already contains {1} temp-works elements "
                "(ObjectTypes {2}) - it is a previous merge result, not "
                "the take-off export; merging again would duplicate every "
                "element. Point --into at the take-off IFC.".format(
                    into, len(prior),
                    sorted({p.ObjectType for p in prior})))
        # the take-off export writes its representations against the plain
        # Model context (no Body subcontext exists in the file) - do the same
        self.body = self.ctx
        origin = f.create_entity("IfcCartesianPoint", [0.0, 0.0, 0.0])
        z = f.create_entity("IfcDirection", [0.0, 0.0, 1.0])
        x = f.create_entity("IfcDirection", [1.0, 0.0, 0.0])
        self.wcs = f.create_entity("IfcAxis2Placement3D", origin, z, x)
        self.owner = None
        # project/site/building stay untouched in merge mode; nothing here
        # needs them (and indexing IfcProject on a degenerate file would
        # crash before the friendlier guards could speak)
        self.project = self.site = self.building = None
        for r in f.by_type("IfcRoot"):
            self._used_guids.add(r.GlobalId)
        # snapshot for validating WALL_GLOBALID/SLAB_GLOBALID references -
        # _used_guids keeps growing with freshly minted temp-works ids, so
        # the reference check must test against what the take-off HELD
        self.inherited_guids = frozenset(self._used_guids)

    def existing_storeys(self):
        """Name -> IfcBuildingStorey of the opened take-off file.

        A duplicate storey Name would make FLOOR containment ambiguous -
        abort rather than guess (QTO allows duplicate floor names, so this
        can genuinely happen on a mis-set floor table).
        """
        by_name = {}
        for s in self.f.by_type("IfcBuildingStorey"):
            if s.Name in by_name:
                raise SystemExit(
                    "take-off IFC has two storeys named {0!r} - FLOOR "
                    "containment would be ambiguous; fix the floor table "
                    "and re-export the take-off".format(s.Name))
            by_name[s.Name] = s
        return by_name

    def _guid(self):
        g = ifcopenshell.guid.new()
        while g in self._used_guids:
            g = ifcopenshell.guid.new()
        self._used_guids.add(g)
        return g

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


def convert(json_path, out_path, sideforms_path=None, jumpforms_path=None,
            into_path=None):
    data = {"levels": [], "panel_thickness": 0.05, "prop_size": 0.15}
    if json_path:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    sides = None
    if sideforms_path:
        sides = json.loads(
            Path(sideforms_path).read_text(encoding="utf-8"))
    jumps = None
    if jumpforms_path:
        jumps = json.loads(
            Path(jumpforms_path).read_text(encoding="utf-8"))
    panel = data["panel_thickness"]
    prop_size = data["prop_size"]
    w = Writer(into=into_path)
    # every WALL_GLOBALID/SLAB_GLOBALID this run writes - validated against
    # the take-off's own GlobalIds before a merged file is written
    ref_guids = set()

    floor_z = {}
    for lv in data["levels"]:
        fl = lv["floor"]
        floor_z[fl] = min(floor_z.get(fl, lv["z"]), lv["z"])
    if sides:
        for pn in sides["panels"]:
            fl = pn["floor"]
            floor_z[fl] = min(floor_z.get(fl, pn["z0"]), pn["z0"])
    if jumps:
        # the storey must exist BEFORE storeys[fl] is indexed below; the
        # wall lift base is the storey elevation (walls stand on their
        # own floor's slab), a reshore's head is that floor's soffit
        for el in jumps.get("jumpforms") or []:
            fl = el["floor"]
            floor_z[fl] = min(floor_z.get(fl, el["lift_z0"]),
                              el["lift_z0"])
        for rs in jumps.get("reshores") or []:
            fl = rs["floor"]
            floor_z[fl] = min(floor_z.get(fl, rs["top_z"]), rs["top_z"])
    if into_path:
        # merge mode: contain into the take-off file's OWN storeys, matched
        # by Name == FLOOR (both vocabularies come from FloorElevations).
        # A miss means the take-off IFC and the temp-works JSONs are from
        # different generations - abort before anything is written, never
        # invent a storey in someone else's spatial tree.
        existing = w.existing_storeys()
        missing = sorted(fl for fl in floor_z if fl not in existing)
        if missing:
            raise SystemExit(
                "no IfcBuildingStorey named {0} in {1} (its storeys: {2}) - "
                "the take-off IFC and the temp-works JSONs disagree on the "
                "floor vocabulary; re-export one side from the same "
                "model generation".format(
                    missing, into_path,
                    sorted(existing, key=lambda n: existing[n].Elevation)))
        storeys = {fl: existing[fl] for fl in floor_z}
    else:
        # storeys ordered by elevation, not by parsing the floor name —
        # names are whatever the QTO user typed and carry no format
        # guarantee
        storeys = {fl: w.storey(fl, z) for fl, z in
                   sorted(floor_z.items(), key=lambda kv: kv[1])}
        if storeys:
            # an IfcRelAggregates with empty RelatedObjects is
            # schema-invalid
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
        ref_guids.update(slab_guids)
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
        by_zone = defaultdict(list)         # (floor, pour|None) -> els
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
            pour = pn.get("pour")
            by_zone[(pn["floor"],
                     str(pour) if pour is not None else None)].append(el)
            if pn["type"] == "side":
                n_side += 1
            else:
                n_bulk += 1
        # one assembly per pour zone (2026-08-24) — the tree reads the
        # zones while element names stay generic; un-poured panels keep
        # the per-floor assembly. Assemblies now carry FLOOR/POUR too
        # (they had no pset at all before — a known 0820 review nit).
        for (fl, pour), elements in by_zone.items():
            name = "Formwork Sides for {0} Pour {1}".format(fl, pour) \
                if pour else "Formwork Sides {0}".format(fl)
            assembly = w.f.create_entity(
                "IfcElementAssembly", w._guid(), w.owner, name, None,
                None, w._place(), None, None, "NOTDEFINED", "NOTDEFINED")
            asm_props = {"FLOOR": fl}
            if pour:
                asm_props["POUR"] = pour
            w.pset([assembly], asm_props)
            w._aggregate(assembly, elements)
            w.contain(storeys[fl], [assembly])

    n_jf = n_rs = 0
    if jumps:
        # Jump form: BOTH states exist as separate elements per lift —
        # the consumer binds by property equality and shows/hides each
        # state per task (activity 2020 installs Unlocked while removing
        # Locked in one task, so one element can never serve both). The
        # element Name is the schedule's component vocabulary VERBATIM
        # ("Jump Form Locked" / "Jump Form Unlocked") and never varies
        # by floor; floor/state/bank live in the pset.
        state_name = {"LOCKED": "Jump Form Locked",
                      "UNLOCKED": "Jump Form Unlocked"}
        by_group = defaultdict(list)      # (floor, bank, state) -> els
        group_walls = defaultdict(list)   # same key -> wall guids
        for el in jumps.get("jumpforms") or []:
            depth = el["z1"] - el["z0"]
            if depth <= 0 or len(el["profile"]) < 4:
                continue
            holes = [el["hole"]] if el.get("hole") else []
            solid = w._extruded(el["profile"], holes, el["z1"], depth)
            proxy = w.proxy(state_name[el["state"]], "jumpform", solid)
            props = {"FLOOR": el["floor"], "STATE": el["state"],
                     "BANK": el["bank"], "KIND": el["kind"],
                     "LIFT_Z0_M": "{0:.3f}".format(el["lift_z0"]),
                     "LIFT_Z1_M": "{0:.3f}".format(el["lift_z1"])}
            wall_guid = _ifc_guid(el.get("wall_id"))
            if wall_guid:
                ref_guids.add(wall_guid)
                # resolves to the take-off IFC's IfcGlobalId of the core
                # wall this unit climbs — same compression, same
                # QTO_STABLE_ID-preferred identity as SLAB_GLOBALID
                props["WALL_GLOBALID"] = wall_guid
            w.pset([proxy], props)
            key = (el["floor"], el["bank"], el["state"])
            by_group[key].append(proxy)
            if wall_guid and wall_guid not in group_walls[key]:
                group_walls[key].append(wall_guid)
            n_jf += 1
        for (fl, bank, state), elements in by_group.items():
            assembly = w.f.create_entity(
                "IfcElementAssembly", w._guid(), w.owner,
                "Jump Form for {0} Core {1} ({2})".format(
                    fl, bank, state.title()), None, None,
                w._place(), None, None, "NOTDEFINED", "NOTDEFINED")
            asm_props = {"FLOOR": fl, "STATE": state, "BANK": bank}
            if group_walls[(fl, bank, state)]:
                asm_props["WALL_GLOBALID"] = ", ".join(
                    group_walls[(fl, bank, state)])
            w.pset([assembly], asm_props)
            w._aggregate(assembly, elements)
            w.contain(storeys[fl], [assembly])

        # Reshores: FLOOR is the floor the shore SUPPORTS (its head
        # bears on that floor's slab soffit), never the floor it stands
        # on — the schedule reasons about reshoring that way. POUR is
        # inherited from the slab piece supported (zone attribution,
        # 2026-08-24); assemblies split per pour so the tree reads the
        # zones, while element names stay generic.
        rs_size = jumps.get("reshore_size") or 0.15
        rs_by_zone = defaultdict(list)      # (floor, pour|None) -> els
        for rs in jumps.get("reshores") or []:
            depth = rs["top_z"] - rs["foot_z"]
            if depth <= 0:
                continue
            half = rs_size / 2.0
            outer = [[rs["x"] - half, rs["y"] - half],
                     [rs["x"] + half, rs["y"] - half],
                     [rs["x"] + half, rs["y"] + half],
                     [rs["x"] - half, rs["y"] + half],
                     [rs["x"] - half, rs["y"] - half]]
            solid = w._extruded(outer, [], rs["top_z"], depth)
            proxy = w.proxy("Pole Shore for Reshoring", "reshore", solid)
            props = {"FLOOR": rs["floor"], "STATUS": rs["status"],
                     "HEIGHT_M": "{0:.3f}".format(depth)}
            pour = rs.get("pour")
            if pour:
                props["POUR"] = str(pour)
            slab_guids = [g for g in (_ifc_guid(i)
                                      for i in rs.get("slab_ids") or [])
                          if g]
            ref_guids.update(slab_guids)
            if slab_guids:
                props["SLAB_GLOBALID"] = ", ".join(slab_guids)
            w.pset([proxy], props)
            rs_by_zone[(rs["floor"], str(pour) if pour else None)].append(
                proxy)
            n_rs += 1
        for (fl, pour), elements in rs_by_zone.items():
            name = "Reshoring for {0} Pour {1}".format(fl, pour) \
                if pour else "Reshoring for {0}".format(fl)
            assembly = w.f.create_entity(
                "IfcElementAssembly", w._guid(), w.owner, name, None,
                None, w._place(), None, None, "NOTDEFINED", "NOTDEFINED")
            asm_props = {"FLOOR": fl}
            if pour:
                asm_props["POUR"] = pour
            w.pset([assembly], asm_props)
            w._aggregate(assembly, elements)
            w.contain(storeys[fl], [assembly])

    if into_path:
        total = n_plat + n_sup + n_side + n_bulk + n_jf + n_rs
        if total == 0:
            raise SystemExit(
                "merge produced ZERO temp-works elements - refusing to "
                "write {0}, which would be nothing but a copy of the "
                "take-off; check the staging JSONs".format(out_path))
        # the FLOOR vocabulary is identical across model generations, so
        # the storey match alone cannot catch stale temp-works JSONs
        # merged into a re-split take-off (a re-SPLIT re-mints every slab
        # piece's QTO_STABLE_ID). The guid references CAN: every
        # WALL_GLOBALID/SLAB_GLOBALID must resolve inside the very file
        # it is being merged into.
        dangling = sorted(g for g in ref_guids
                          if g not in w.inherited_guids)
        if dangling:
            raise SystemExit(
                "{0} of {1} WALL_GLOBALID/SLAB_GLOBALID references do not "
                "resolve to any element in {2} (e.g. {3}) - the temp-works "
                "JSONs and this take-off IFC are from different model "
                "generations (a re-SPLIT re-mints slab ids; re-run the "
                "generators), or the take-off export skipped those "
                "elements. Refusing to write a unified file with dangling "
                "links.".format(len(dangling), len(ref_guids), into_path,
                                dangling[:3]))
        # the opened file kept the take-off's STEP header; stamp the
        # merge's own provenance so the unified deliverable is
        # distinguishable from the plain take-off by its own metadata
        h = w.f.header.file_name
        h.name = Path(out_path).name
        h.time_stamp = datetime.datetime.now().isoformat(
            timespec="seconds")
        h.originating_system = (
            "formwork_ifc_from_json.py --into merge "
            "(temp works appended to the take-off export)")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    w.f.write(str(out_path))
    merged = " merged into the {0} take-off storeys of {1}".format(
        len(w.f.by_type("IfcBuildingStorey")), into_path) if into_path else \
        " across {0} storeys".format(len(storeys))
    print("wrote {0}: {1} platforms + {2} supports ({3}) + {4} side forms "
          "+ {5} bulkheads + {6} jump-form elements + {7} reshores{8}".format(
              out_path, n_plat, n_sup, dict(counts), n_side, n_bulk,
              n_jf, n_rs, merged))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", help="formwork JSON (platforms + props)")
    ap.add_argument("--sideforms",
                    help="side-form JSON from sideform_gen_rhino.py")
    ap.add_argument("--jumpforms",
                    help="jump-form JSON from jumpform_gen_rhino.py")
    ap.add_argument("--into",
                    help="take-off IFC to merge the temp works into - one "
                         "file, one storey tree (the input is never "
                         "modified; the result goes to --out)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if not a.json and not a.sideforms and not a.jumpforms:
        ap.error("need --json, --sideforms and/or --jumpforms")
    if a.into and (os.path.normcase(str(Path(a.into).resolve()))
                   == os.path.normcase(str(Path(a.out).resolve()))):
        ap.error("--out must not overwrite the --into take-off IFC")
    convert(a.json, a.out, a.sideforms, a.jumpforms, a.into)


if __name__ == "__main__":
    main()
