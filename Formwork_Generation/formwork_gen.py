"""Deterministic local formwork-soffit generator (ToBe automation stand-in).

Reproduces, locally and offline, the geometry that ToBe Builder's cloud
``Formwork_Soffit`` automation returns: for each floor it emits a **soffit
platform** (the deck-form contact surface, the slab underside offset down by
one panel thickness) plus a **grid of vertical supports** (shoring props)
down to the floor below. Output is an IFC of ``IfcBuildingElementProxy``
elements — one ``platform`` + N ``support`` per floor — grouped under one
``IfcElementAssembly`` per floor, each element carrying a ``QTO
Properties.FLOOR`` = ``L<n>`` property so ``scripts/gen_searchsets.py``'s
two-condition search sets bind it unchanged.

Why this exists
---------------
ToBe's automation is a *cloud* Rhino/Grasshopper service (see the project
memory ``tobe-automation-is-cloud-grasshopper``): the ``.ghx`` lives on
IDDTech's server, this account's licence is ``AutomationDefinitions:Read``,
and the returned geometry is placeholder-grade (~6-face boxes: one platform
+ ~124 support proxies for the Sunbreak test). None of that needs the cloud
— the operation is *slab soffit → offset down → tile a prop grid*, which is
a few hundred lines here. This module is the local, deterministic,
zero-LLM equivalent, in the same spirit as ``qto_enrichment`` /
``ifc_enrichment``.

Fidelity note
-------------
This is a **placeholder** generator, matching ToBe's own fidelity — it does
not do engineered falsework design (panel modularisation, load-rated prop
selection, drophead layout). It does, however, trace the **true** slab
outline: each floor's footprint is the union of its slab mesh triangles
projected to plan (real edges, concave corners and openings preserved), not
a convex hull. The platform is that outline offset out by ``platform_overhang``
(slightly larger than the slab, so it supports it); the support grid is a
regular lattice clipped to the true outline. Vertically everything snaps:
platform top = slab underside, support top = platform underside, support
foot = the top surface of the slab below — so across floors the formwork
stacks leave a gap equal to the building slab thickness.

Dependencies
------------
Optional ``[formwork]`` group: ``ifcopenshell`` (read slab geometry + write
IFC), ``shapely`` (footprint union + point-in-polygon grid), ``numpy``
(vertex math). The deterministic core of the repo stays stdlib-only; this
module, like the QTO reader's ``openpyxl``, is import-guarded.

CLI
---
    python -m mast4d.pipelines.formwork_gen \
        --ifc "./ifcs/Sunbreak TestV5.ifc" \
        --out ./out/formwork_soffit.ifc \
        --floors 5
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ── optional-dependency guard ──────────────────────────────────────────────
try:
    import ifcopenshell
    import ifcopenshell.geom
    import ifcopenshell.guid
    import ifcopenshell.util.element as _ue
    import ifcopenshell.util.unit as _uu
    import numpy as np
    from shapely.geometry import MultiPolygon, Point, Polygon
    from shapely.ops import unary_union
    _HAVE_DEPS = True
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised only without deps
    _HAVE_DEPS = False
    _IMPORT_ERROR = exc


# ── parameters ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FormworkParams:
    """Tunables for the generated formwork. Lengths are in METRES.

    Attributes:
        panel_thickness: Vertical thickness of the soffit platform (the
            deck-form panel), extruded downward from the slab underside.
        prop_spacing: Centre-to-centre spacing of the support grid.
            ~3.0 m yields a ToBe-comparable count (~120-170 props on a
            1500 m^2 floor). Real shoring is denser (1.2-1.8 m).
        prop_size: Plan size (square side) of each support proxy box.
        platform_overhang: Outward offset of the platform beyond the true
            slab outline, so the deck form is slightly larger than the slab
            it supports (a real edge-form margin). 0 = trace exactly.
        edge_inset: Grid points closer than this to the (true) footprint
            edge are dropped, so props stay under the slab, not the overhang.
        default_prop_height: Support height used for the lowest floor,
            which has no floor below to stand its props on.
        platform_name: ``IfcName`` written on the platform proxy. Align
            this with the schedule's Component vocabulary (e.g. the
            ``Flyable Deck`` Component_2 slot) to make the previously
            unmatchable formwork slots bind in gen_searchsets.
        support_name: ``IfcName`` written on each support proxy (e.g.
            ``Pole Shore for Shoring``).
    """

    panel_thickness: float = 0.05
    prop_spacing: float = 3.0
    prop_size: float = 0.15
    platform_overhang: float = 0.1
    edge_inset: float = 0.5
    default_prop_height: float = 2.9
    platform_name: str = "Formwork Soffit Platform"
    support_name: str = "Formwork Support"


@dataclass
class FloorFormwork:
    """One floor's aggregated soffit geometry (metres, world coords)."""

    floor: str
    footprint: object            # shapely Polygon | MultiPolygon (true outline)
    soffit_z: float              # lowest slab underside (platform sits here)
    slab_top_z: float            # highest slab top (this floor's own slab)
    n_slabs: int
    support_height: float = 0.0
    n_supports: int = 0


@dataclass
class GenerationResult:
    floors: list[FloorFormwork] = field(default_factory=list)
    out_path: str = ""
    total_supports: int = 0

    def summary(self) -> str:
        lines = [f"wrote {self.out_path}"]
        for fw in self.floors:
            lines.append(
                f"    {fw.floor}: platform (area "
                f"{_area(fw.footprint):.0f} m^2) + {fw.n_supports} supports "
                f"(h={fw.support_height:.2f} m)"
            )
        lines.append(
            f"    total: {len(self.floors)} floors, "
            f"{self.total_supports} supports"
        )
        return "\n".join(lines)


def _require_deps() -> None:
    if not _HAVE_DEPS:
        raise ImportError(
            "formwork_gen needs the optional '[formwork]' dependencies "
            "(ifcopenshell, shapely, numpy). Install with: "
            "uv pip install -e '.[formwork]'  —  underlying error: "
            f"{_IMPORT_ERROR!r}"
        )


def _area(poly) -> float:
    return getattr(poly, "area", 0.0)


def _floor_sort_key(name: str) -> tuple[int, object]:
    """Order L01..L16 numerically; push P1/R1/R2 etc. to the end."""
    if name and name[0] in "Ll" and name[1:].isdigit():
        return (0, int(name[1:]))
    return (1, name or "")


# ── geometry extraction ────────────────────────────────────────────────────
def _floor_of(element) -> str | None:
    """Read ``QTO Properties.FLOOR`` off an element, else its storey name."""
    psets = _ue.get_psets(element)
    floor = (psets.get("QTO Properties", {}) or {}).get("FLOOR")
    if floor:
        return str(floor).strip()
    for rel in getattr(element, "ContainedInStructure", []) or []:
        st = rel.RelatingStructure
        if st.is_a("IfcBuildingStorey"):
            return (st.Name or "").strip() or None
    return None


def _triangle_polys(verts, faces, min_area: float = 1e-6) -> list:
    """Project each mesh triangle to XY as a Polygon (drop vertical faces)."""
    tris = []
    for a, b, c in faces:
        tri = Polygon([
            (verts[a, 0], verts[a, 1]),
            (verts[b, 0], verts[b, 1]),
            (verts[c, 0], verts[c, 1]),
        ])
        if tri.area > min_area:            # vertical side faces -> ~0, skipped
            tris.append(tri)
    return tris


def _trace_footprint(tri_polys, hole_min_area: float = 0.5):
    """Union projected triangles into the true plan outline.

    Preserves concave edges and real openings; interior holes smaller than
    ``hole_min_area`` (m^2) are treated as triangulation noise and dropped.
    """
    union = unary_union(tri_polys).buffer(0)     # heal slivers / self-touches
    cleaned = []
    for part in _polygon_parts(union):
        holes = [
            ring for ring in part.interiors
            if Polygon(ring).area >= hole_min_area
        ]
        cleaned.append(Polygon(part.exterior.coords, holes))
    if not cleaned:
        return union
    return cleaned[0] if len(cleaned) == 1 else MultiPolygon(cleaned)


def scan_slabs(
    ifc_file,
    floors: set[str] | None = None,
) -> tuple[dict[str, tuple[float, float]], dict[str, FloorFormwork]]:
    """One geometry pass over every ``IfcSlab``.

    Args:
        ifc_file: an opened ``ifcopenshell`` file.
        floors: build footprints only for these floor labels (e.g.
            ``{"L05"}``); None = every floor with slabs. Levels are always
            computed for *all* floors (a single-floor run still needs the
            floor below, to stand its props on).

    Returns:
        ``(levels, per_floor)`` where

        * ``levels[floor] = (soffit_z, slab_top_z)`` for EVERY floor with
          slabs — ``soffit_z`` = lowest slab underside, ``slab_top_z`` =
          highest slab top (both metres, world coords).
        * ``per_floor[floor] = FloorFormwork`` for the SELECTED floors,
          each carrying the true traced footprint.
    """
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)

    zmin: dict[str, float] = {}
    zmax: dict[str, float] = {}
    counts: dict[str, int] = {}
    tris: dict[str, list] = {}          # selected floors only

    for slab in ifc_file.by_type("IfcSlab"):
        floor = _floor_of(slab)
        if floor is None:
            continue
        try:
            shape = ifcopenshell.geom.create_shape(settings, slab)
        except Exception:
            continue
        verts = np.asarray(shape.geometry.verts, dtype=float).reshape(-1, 3)
        if len(verts) < 3:
            continue
        lo, hi = float(verts[:, 2].min()), float(verts[:, 2].max())
        zmin[floor] = min(zmin.get(floor, lo), lo)
        zmax[floor] = max(zmax.get(floor, hi), hi)
        counts[floor] = counts.get(floor, 0) + 1
        if floors is None or floor in floors:
            faces = np.asarray(
                shape.geometry.faces, dtype=int).reshape(-1, 3)
            tris.setdefault(floor, []).extend(_triangle_polys(verts, faces))

    levels = {f: (zmin[f], zmax[f]) for f in zmin}

    per_floor: dict[str, FloorFormwork] = {}
    for floor, tri_polys in tris.items():
        if not tri_polys:
            continue
        footprint = _trace_footprint(tri_polys)
        if footprint.is_empty:
            continue
        per_floor[floor] = FloorFormwork(
            floor=floor,
            footprint=footprint,
            soffit_z=zmin[floor],
            slab_top_z=zmax[floor],
            n_slabs=counts[floor],
        )
    return levels, per_floor


def _grid_points(poly, spacing: float, inset: float) -> list[tuple[float, float]]:
    """Regular lattice of (x, y) points inside ``poly`` (shrunk by inset)."""
    region = poly.buffer(-inset) if inset > 0 else poly
    if region.is_empty:
        region = poly
    minx, miny, maxx, maxy = region.bounds
    pts: list[tuple[float, float]] = []
    n = 0
    y = miny
    while y <= maxy and n < 100_000:
        x = minx
        while x <= maxx and n < 100_000:
            if region.covers(Point(x, y)):
                pts.append((x, y))
            x += spacing
            n += 1
        y += spacing
    return pts


# ── IFC writing (self-contained IFC4) ──────────────────────────────────────
def _polygon_parts(geom) -> list[Polygon]:
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]


class _IfcWriter:
    """Minimal IFC4 authoring helper over ifcopenshell create_entity.

    All input coordinates/lengths are in METRES; they are scaled by
    ``unit_scale`` (metres-per-file-unit reciprocal) so the output is
    written in the *source* file's length unit and therefore co-registers
    with the source model when both are loaded together.
    """

    def __init__(self, source_file, params: FormworkParams):
        self.p = params
        self.f = ifcopenshell.file(schema="IFC4")
        # metres -> source file units (e.g. mm => 1000)
        self.scale = 1.0 / _uu.calculate_unit_scale(source_file)
        self._build_header(source_file)

    # -- primitives --
    def _pt(self, coords):
        return self.f.create_entity(
            "IfcCartesianPoint",
            [float(c) * self.scale for c in coords],
        )

    def _dir(self, coords):
        return self.f.create_entity(
            "IfcDirection", [float(c) for c in coords]
        )

    def _guid(self):
        return ifcopenshell.guid.new()

    def _build_header(self, source_file):
        f = self.f
        origin = self._pt((0.0, 0.0, 0.0))
        z = self._dir((0.0, 0.0, 1.0))
        x = self._dir((1.0, 0.0, 0.0))
        self.wcs = f.create_entity("IfcAxis2Placement3D", origin, z, x)

        # Length unit copied from the source (keep coordinates aligned).
        src_len = next(
            (u for u in source_file.by_type("IfcSIUnit")
             if u.UnitType == "LENGTHUNIT"),
            None,
        )
        prefix = src_len.Prefix if src_len else None
        length = f.create_entity("IfcSIUnit", None, "LENGTHUNIT", prefix, "METRE")
        units = f.create_entity("IfcUnitAssignment", [length])

        self.ctx = f.create_entity(
            "IfcGeometricRepresentationContext",
            None, "Model", 3, 1.0e-5, self.wcs, None,
        )
        self.body = f.create_entity(
            "IfcGeometricRepresentationSubContext",
            "Body", "Model", None, None, None, None,
            self.ctx, None, "MODEL_VIEW", None,
        )
        self.owner = None
        self.project = f.create_entity(
            "IfcProject", self._guid(), self.owner, "Formwork Soffit (local)",
            None, None, None, None, [self.ctx], units,
        )
        self.site = f.create_entity(
            "IfcSite", self._guid(), self.owner, "Site", None, None,
            self._local_placement(), None, None, "ELEMENT",
            None, None, None, None, None,
        )
        self.building = f.create_entity(
            "IfcBuilding", self._guid(), self.owner, "Building", None, None,
            self._local_placement(), None, None, "ELEMENT", None, None, None,
        )
        self._aggregate(self.project, [self.site])
        self._aggregate(self.site, [self.building])

    def _local_placement(self, rel_to=None):
        return self.f.create_entity("IfcLocalPlacement", rel_to, self.wcs)

    def _aggregate(self, whole, parts):
        self.f.create_entity(
            "IfcRelAggregates", self._guid(), self.owner, None, None,
            whole, parts,
        )

    def _rel_contained(self, structure, elements, name):
        self.f.create_entity(
            "IfcRelContainedInSpatialStructure", self._guid(), self.owner,
            name, None, elements, structure,
        )

    # -- solids --
    def _ring_polyline(self, coords):
        """Closed IfcPolyline (file units) from a shapely ring's coords."""
        ring = list(coords)
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        pts = [self.f.create_entity(
            "IfcCartesianPoint",
            [float(x) * self.scale, float(y) * self.scale]) for x, y in ring]
        pts.append(pts[0])                        # explicit closure
        return self.f.create_entity("IfcPolyline", pts)

    def _extruded_polygon(self, poly: Polygon, top_z: float, depth: float):
        """Prism from ``poly`` with its top face at ``top_z``, extruded down.

        Interior rings (slab openings) become voids in the profile, so the
        platform is punched through where the slab is.
        """
        outer = self._ring_polyline(poly.exterior.coords)
        inners = [self._ring_polyline(r.coords) for r in poly.interiors]
        if inners:
            profile = self.f.create_entity(
                "IfcArbitraryProfileDefWithVoids", "AREA", None, outer, inners)
        else:
            profile = self.f.create_entity(
                "IfcArbitraryClosedProfileDef", "AREA", None, outer)
        # place the profile plane at the BOTTOM, extrude +Z by depth
        base = self.f.create_entity(
            "IfcAxis2Placement3D", self._pt((0.0, 0.0, top_z - depth)),
            self._dir((0.0, 0.0, 1.0)), self._dir((1.0, 0.0, 0.0)))
        return self.f.create_entity(
            "IfcExtrudedAreaSolid", profile, base,
            self._dir((0.0, 0.0, 1.0)), float(depth) * self.scale)

    def _extruded_box(self, cx, cy, top_z, side, depth):
        half = side / 2.0
        rect = Polygon([
            (cx - half, cy - half), (cx + half, cy - half),
            (cx + half, cy + half), (cx - half, cy + half),
        ])
        return self._extruded_polygon(rect, top_z, depth)

    def _proxy(self, name, obj_type, solid, storey_place):
        rep = self.f.create_entity(
            "IfcShapeRepresentation", self.body, "Body", "SweptSolid", [solid])
        shape = self.f.create_entity(
            "IfcProductDefinitionShape", None, None, [rep])
        return self.f.create_entity(
            "IfcBuildingElementProxy", self._guid(), self.owner, name, None,
            obj_type, self._local_placement(storey_place), shape, None, None,
        )

    def _floor_pset(self, elements, floor: str):
        """Attach ``QTO Properties.FLOOR = <floor>`` to every element."""
        prop = self.f.create_entity(
            "IfcPropertySingleValue", "FLOOR", None,
            self.f.create_entity("IfcLabel", floor), None)
        pset = self.f.create_entity(
            "IfcPropertySet", self._guid(), self.owner, "QTO Properties",
            None, [prop])
        self.f.create_entity(
            "IfcRelDefinesByProperties", self._guid(), self.owner, None, None,
            elements, pset)

    # -- public: one floor --
    def add_floor(self, fw: FloorFormwork, storey_entity) -> tuple[int, int]:
        """Emit one floor's platform(s) + supports. Returns (n_plat, n_sup)."""
        storey_place = storey_entity.ObjectPlacement
        elements = []

        # platform: the true slab outline offset out so it is slightly larger
        # than (and supports) the slab underside.
        platform_fp = (
            fw.footprint.buffer(self.p.platform_overhang)
            if self.p.platform_overhang else fw.footprint)
        n_plat = 0
        for part in _polygon_parts(platform_fp):
            solid = self._extruded_polygon(
                part, fw.soffit_z, self.p.panel_thickness)
            elements.append(self._proxy(
                self.p.platform_name, "platform", solid, storey_place))
            n_plat += 1

        # supports: top snapped to platform underside, extruded down to land
        # on the slab below (height already resolved into fw.support_height).
        prop_top = fw.soffit_z - self.p.panel_thickness
        n_sup = 0
        for (cx, cy) in _grid_points(
                fw.footprint, self.p.prop_spacing, self.p.edge_inset):
            solid = self._extruded_box(
                cx, cy, prop_top, self.p.prop_size, fw.support_height)
            elements.append(self._proxy(
                self.p.support_name, "support", solid, storey_place))
            n_sup += 1

        assembly = self.f.create_entity(
            "IfcElementAssembly", self._guid(), self.owner,
            f"Formwork Soffit {fw.floor}", None, None,
            self._local_placement(storey_place), None, None,
            "NOTDEFINED", "NOTDEFINED")
        self._aggregate(assembly, elements)
        self._rel_contained(storey_entity, [assembly], "Formwork")
        self._floor_pset(elements, fw.floor)
        return n_plat, n_sup

    def make_storey(self, name: str, elevation_m: float):
        st = self.f.create_entity(
            "IfcBuildingStorey", self._guid(), self.owner, name, None, None,
            self._local_placement(), None, None, "ELEMENT",
            float(elevation_m) * self.scale)
        return st

    def link_storeys(self, storeys):
        self._aggregate(self.building, storeys)

    def write(self, out_path: str):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        self.f.write(out_path)


# ── orchestration ──────────────────────────────────────────────────────────
def generate_formwork(
    ifc_path: str | Path,
    out_path: str | Path,
    floors: Iterable[str] | None = None,
    params: FormworkParams | None = None,
) -> GenerationResult:
    """Read slabs from ``ifc_path``, write a formwork IFC to ``out_path``.

    Args:
        ifc_path: source project IFC (slab geometry + floor properties).
        out_path: destination IFC.
        floors: floor labels to build (e.g. ``["L05", "L06"]``); None =
            every floor carrying slabs.
        params: :class:`FormworkParams` overrides.

    Returns:
        :class:`GenerationResult` with per-floor counts.
    """
    _require_deps()
    params = params or FormworkParams()
    want = {f.strip() for f in floors} if floors is not None else None

    source = ifcopenshell.open(str(ifc_path))
    levels, per_floor = scan_slabs(source, want)
    if not per_floor:
        raise ValueError(
            f"no slabs found for floors {sorted(want) if want else 'ALL'} "
            f"in {ifc_path}"
        )

    ordered = sorted(per_floor.values(), key=lambda fw: _floor_sort_key(fw.floor))

    # Props stand on the TOP surface of the slab immediately below (found
    # across ALL floors, so a single-floor run still resolves it), and their
    # heads snap to the platform underside. Support height therefore leaves
    # a gap of one building-slab thickness between adjacent floors' formwork.
    def _support_height(floor: str) -> float:
        soffit_z, _top = levels[floor]
        prop_top = soffit_z - params.panel_thickness
        below = [lv for f, lv in levels.items() if lv[0] < soffit_z - 1e-6]
        if not below:
            return params.default_prop_height
        slab_top_below = max(below, key=lambda lv: lv[0])[1]
        return max(0.1, prop_top - slab_top_below)

    scale_m = _uu.calculate_unit_scale(source)   # metres per file unit
    src_storeys = {
        (s.Name or "").strip(): s.Elevation
        for s in source.by_type("IfcBuildingStorey")
        if s.Elevation is not None
    }

    writer = _IfcWriter(source, params)
    made_storeys = []
    result = GenerationResult(out_path=str(out_path))

    for fw in ordered:
        fw.support_height = _support_height(fw.floor)

        # storey elevation reused from the source (converted to metres; the
        # writer re-scales back to file units) so floors line up on reload.
        elev_file = src_storeys.get(fw.floor)
        elev_m = elev_file * scale_m if elev_file is not None else fw.soffit_z
        st = writer.make_storey(fw.floor, elev_m)
        made_storeys.append(st)

        _n_plat, fw.n_supports = writer.add_floor(fw, st)
        result.floors.append(fw)
        result.total_supports += fw.n_supports

    writer.link_storeys(made_storeys)
    writer.write(str(out_path))
    return result


# ── CLI ────────────────────────────────────────────────────────────────────
def _parse_floor_args(raw: str | None) -> list[str] | None:
    """'5' or '5,6,7' or 'L05' -> ['L05', 'L06', 'L07']; None -> all."""
    if not raw:
        return None
    out = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok[0] in "Ll":
            out.append(f"L{int(tok[1:]):02d}")
        else:
            out.append(f"L{int(tok):02d}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ifc", required=True, help="source project IFC")
    ap.add_argument("--out", required=True, help="output formwork IFC")
    ap.add_argument("--floors", default=None,
                    help="e.g. '5' or '5,6,7'; omit for every floor")
    ap.add_argument("--panel-thickness", type=float, default=0.05,
                    help="soffit platform thickness, metres (default 0.05)")
    ap.add_argument("--prop-spacing", type=float, default=3.0,
                    help="support grid spacing, metres (default 3.0)")
    ap.add_argument("--prop-size", type=float, default=0.15,
                    help="support plan size, metres (default 0.15)")
    ap.add_argument("--platform-overhang", type=float, default=0.1,
                    help="platform outward margin beyond the slab edge, "
                         "metres (default 0.1; 0 = trace exactly)")
    ap.add_argument("--platform-name", default="Formwork Soffit Platform",
                    help="IfcName for the platform (align to schedule "
                         "Component vocabulary to bind search sets)")
    ap.add_argument("--support-name", default="Formwork Support",
                    help="IfcName for each support proxy")
    args = ap.parse_args(argv)

    _require_deps()
    params = FormworkParams(
        panel_thickness=args.panel_thickness,
        prop_spacing=args.prop_spacing,
        prop_size=args.prop_size,
        platform_overhang=args.platform_overhang,
        platform_name=args.platform_name,
        support_name=args.support_name,
    )
    result = generate_formwork(
        args.ifc, args.out, _parse_floor_args(args.floors), params)
    print(result.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
