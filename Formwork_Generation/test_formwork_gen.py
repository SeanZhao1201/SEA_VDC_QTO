"""Tests for the local formwork-soffit generator.

Split in two: pure-function tests that run everywhere (stdlib only), and an
end-to-end test guarded by ``importorskip`` — the generator needs the
optional ``[formwork]`` deps (ifcopenshell / shapely / numpy), so on an
environment without them this test skips instead of failing, mirroring the
repo's existing optional-dependency pattern.
"""
from __future__ import annotations

import importlib.util

import pytest

from formwork_gen import (
    FormworkParams,
    _floor_sort_key,
    _parse_floor_args,
    generate_formwork,
)

# Skip only the integration tests when the optional deps are absent — the
# pure-function tests below run everywhere (importorskip at module level
# would skip the whole file, pure tests included).
_HAVE_DEPS = all(
    importlib.util.find_spec(m) for m in ("ifcopenshell", "shapely", "numpy"))
requires_deps = pytest.mark.skipif(
    not _HAVE_DEPS, reason="needs optional [formwork] deps "
    "(ifcopenshell, shapely, numpy)")


# ── pure helpers (no optional deps) ─────────────────────────────────────────
def test_parse_floor_args_variants():
    assert _parse_floor_args(None) is None
    assert _parse_floor_args("5") == ["L05"]
    assert _parse_floor_args("5,6,7") == ["L05", "L06", "L07"]
    assert _parse_floor_args("L05") == ["L05"]
    assert _parse_floor_args(" 5 , l12 ") == ["L05", "L12"]


def test_floor_sort_key_orders_tower_then_special():
    labels = ["R1", "L02", "P1", "L10", "L01"]
    assert sorted(labels, key=_floor_sort_key) == \
        ["L01", "L02", "L10", "P1", "R1"]


# ── end-to-end (needs the [formwork] deps) ──────────────────────────────────
_SLAB_THICKNESS_MM = 200.0        # 0.2 m — the building slab thickness
# (floor label, slab-underside Z in mm); L04 sits below L05 so props on L05
# have a slab top to stand on.
_FIXTURE_FLOORS = (("L04", 49100.0), ("L05", 52300.0))


def _build_source_ifc(path: str, floors=_FIXTURE_FLOORS) -> None:
    """Minimal IFC4 (mm): each (floor, soffit) gets two slabs + FLOOR pset."""
    import ifcopenshell
    import ifcopenshell.guid

    f = ifcopenshell.file(schema="IFC4")
    origin = f.create_entity("IfcCartesianPoint", (0.0, 0.0, 0.0))
    zdir = f.create_entity("IfcDirection", (0.0, 0.0, 1.0))
    xdir = f.create_entity("IfcDirection", (1.0, 0.0, 0.0))
    wcs = f.create_entity("IfcAxis2Placement3D", origin, zdir, xdir)

    mm = f.create_entity("IfcSIUnit", None, "LENGTHUNIT", "MILLI", "METRE")
    units = f.create_entity("IfcUnitAssignment", [mm])
    ctx = f.create_entity(
        "IfcGeometricRepresentationContext", None, "Model", 3, 1e-5, wcs, None)
    body = f.create_entity(
        "IfcGeometricRepresentationSubContext", "Body", "Model",
        None, None, None, None, ctx, None, "MODEL_VIEW", None)

    def place():
        return f.create_entity("IfcLocalPlacement", None, wcs)

    def new(kind, *args):
        return f.create_entity(kind, ifcopenshell.guid.new(), None, *args)

    proj = f.create_entity(
        "IfcProject", ifcopenshell.guid.new(), None, "src", None, None,
        None, None, [ctx], units)
    site = new("IfcSite", "Site", None, None, place(), None, None,
               "ELEMENT", None, None, None, None, None)
    building = new("IfcBuilding", "B", None, None, place(), None, None,
                   "ELEMENT", None, None, None)
    f.create_entity("IfcRelAggregates", ifcopenshell.guid.new(), None,
                    None, None, proj, [site])
    f.create_entity("IfcRelAggregates", ifcopenshell.guid.new(), None,
                    None, None, site, [building])

    storeys = []
    for name, soffit in floors:
        storey = new("IfcBuildingStorey", name, None, None, place(),
                     None, None, "ELEMENT", soffit)
        storeys.append(storey)
        slabs = []
        # two 10 m x 8 m rectangles side by side
        for cx in (5000.0, 16000.0):
            pos2d = f.create_entity(
                "IfcAxis2Placement2D",
                f.create_entity("IfcCartesianPoint", (cx, 4000.0)), None)
            profile = f.create_entity(
                "IfcRectangleProfileDef", "AREA", None, pos2d, 10000.0, 8000.0)
            base = f.create_entity(
                "IfcAxis2Placement3D",
                f.create_entity("IfcCartesianPoint", (0.0, 0.0, soffit)),
                zdir, xdir)
            solid = f.create_entity(
                "IfcExtrudedAreaSolid", profile, base, zdir,
                _SLAB_THICKNESS_MM)
            rep = f.create_entity(
                "IfcShapeRepresentation", body, "Body", "SweptSolid", [solid])
            shape = f.create_entity(
                "IfcProductDefinitionShape", None, None, [rep])
            slabs.append(new("IfcSlab", "SLAB PT", None, None, place(),
                             shape, None, "FLOOR"))
        f.create_entity(
            "IfcRelContainedInSpatialStructure", ifcopenshell.guid.new(),
            None, None, None, slabs, storey)
        prop = f.create_entity(
            "IfcPropertySingleValue", "FLOOR", None,
            f.create_entity("IfcLabel", name), None)
        pset = new("IfcPropertySet", "QTO Properties", None, [prop])
        f.create_entity(
            "IfcRelDefinesByProperties", ifcopenshell.guid.new(), None,
            None, None, slabs, pset)

    f.create_entity("IfcRelAggregates", ifcopenshell.guid.new(), None,
                    None, None, building, storeys)
    f.write(path)


def _assembly_bounds(ifc_file, settings, floor: str):
    """(min_z, max_z) over every proxy carrying FLOOR == ``floor`` (metres)."""
    import ifcopenshell.geom
    import ifcopenshell.util.element as ue
    import numpy as np

    lo, hi = float("inf"), float("-inf")
    for p in ifc_file.by_type("IfcBuildingElementProxy"):
        fl = (ue.get_psets(p).get("QTO Properties", {}) or {}).get("FLOOR")
        if fl != floor:
            continue
        # Hold the shape in a variable — reading .geometry.verts off a
        # temporary create_shape() result comes back empty.
        shape = ifcopenshell.geom.create_shape(settings, p)
        v = np.array(shape.geometry.verts).reshape(-1, 3)
        if len(v):
            lo, hi = min(lo, v[:, 2].min()), max(hi, v[:, 2].max())
    return lo, hi


@requires_deps
def test_generate_formwork_end_to_end(tmp_path):
    import ifcopenshell
    import ifcopenshell.util.element as ue

    src = tmp_path / "src.ifc"
    out = tmp_path / "formwork.ifc"
    _build_source_ifc(str(src))

    result = generate_formwork(
        str(src), str(out), floors=["L05"],
        params=FormworkParams(prop_spacing=2.0))

    assert len(result.floors) == 1
    fw = result.floors[0]
    assert fw.floor == "L05"
    assert fw.n_supports > 0
    assert result.total_supports == fw.n_supports
    # props stand on the L04 slab TOP (49.3 m), heads at the L05 platform
    # underside (52.3 - 0.05): height = 52.25 - 49.3 = 2.95 m — NOT the
    # default fallback, and NOT reaching the L04 underside.
    assert fw.support_height == pytest.approx(2.95, abs=0.02)

    g = ifcopenshell.open(str(out))
    assert g.schema == "IFC4"
    proxies = g.by_type("IfcBuildingElementProxy")
    platforms = [p for p in proxies if p.ObjectType == "platform"]
    supports = [p for p in proxies if p.ObjectType == "support"]
    assert len(platforms) >= 1
    assert len(supports) == fw.n_supports
    assert len(g.by_type("IfcElementAssembly")) == 1

    # every element carries QTO Properties.FLOOR = L05 (search-set join key)
    for p in proxies:
        floor = (ue.get_psets(p).get("QTO Properties", {}) or {}).get("FLOOR")
        assert floor == "L05"


@requires_deps
def test_interfloor_gap_equals_slab_thickness(tmp_path):
    """Two stacked floors' formwork must be separated by one slab thickness."""
    import ifcopenshell
    import ifcopenshell.geom

    src = tmp_path / "src.ifc"
    out = tmp_path / "formwork.ifc"
    _build_source_ifc(str(src))

    generate_formwork(str(src), str(out), floors=["L04", "L05"],
                      params=FormworkParams(prop_spacing=3.0))

    g = ifcopenshell.open(str(out))
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    _l04_lo, l04_hi = _assembly_bounds(g, settings, "L04")   # platform top
    l05_lo, _l05_hi = _assembly_bounds(g, settings, "L05")   # support foot

    # L04 formwork tops out at the L04 slab underside; L05 formwork bottoms
    # out on the L04 slab top. The gap between them is the slab thickness.
    gap = l05_lo - l04_hi
    assert gap == pytest.approx(_SLAB_THICKNESS_MM / 1000.0, abs=0.02)


@requires_deps
def test_generate_formwork_unknown_floor_raises(tmp_path):
    src = tmp_path / "src.ifc"
    _build_source_ifc(str(src))
    with pytest.raises(ValueError):
        generate_formwork(str(src), str(tmp_path / "o.ifc"), floors=["L99"])
