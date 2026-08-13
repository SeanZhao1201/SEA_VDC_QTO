# CLAUDE.md — Formwork_Generation

This file gives Claude Code the context for the `Formwork_Generation/`
component when a session works in this folder. It is scoped to this subtree;
the repository-level `CLAUDE.md` (the `QTO_Tool` plugin) still applies above it.

## What this component is

A local, deterministic generator for concrete **soffit formwork** (a deck
platform) and **shoring** (support props), written out as IFC. It is the
downstream partner of `QTO_Tool`: that plugin **produces** the structural IFC
(slabs, floors, `QTO Properties`); this component **consumes** that IFC and
emits the temporary works that stand under each slab — the geometry a 4D
schedule animates during the pour cycle.

```
QTO_Tool  ──IFC4 (slabs + floors)──►  formwork_gen  ──IFC4 (platform + props)──►  4D / Synchro
   (C# / xBIM, this repo)               (Python, this folder)                       (schedule)
```

Provenance: migrated from the Mast4D 4D-scheduling pipeline
(`mast4d/pipelines/formwork_gen.py`). It was built as a **local stand-in for
ToBe Builder's cloud "Formwork_Soffit" automation** (Rhino/Grasshopper on a
remote server returning an IFC) — same result, no cloud, no license, no LLM,
fully reproducible. It is **placeholder-fidelity**: it locates and sizes
formwork for visualization and 4D sequencing, not engineered falsework design
(no panel modularization, no load-rated prop selection, no drophead layout).

## Files

- `rhino/formwork_gen_rhino.py` — **the current generator** (P1, 2026-07):
  runs inside Rhino 7/8 on the live model, per-soffit-face platforms +
  per-prop downward ray-cast. Supersedes the per-floor fixed-height logic
  below for accuracy; see `rhino/README.md` for modes, params, the upgraded
  invariants, and the headless test loop.
- `rhino/test_headless.py`, `rhino/run_on_model.py` — headless acceptance
  test (synthetic podium scene, per-prop numeric asserts) and batch runner
  for the real model; both launched via `Rhino.exe /runscript` from
  `%LOCALAPPDATA%\qto_fw_test\`.
- `formwork_gen.py` — the legacy IFC-based generator + CLI (Mast4D copy;
  stdlib + ifcopenshell/shapely/numpy). Kept as reference implementation and
  IFC-writing template. Known-wrong on podium floors: one soffit/foot level
  per floor collapses stepped slabs, and props under voids (挑空) get the
  height of a slab that is not there — quantified 2026-07-23 on the sample
  IFC (L02: 138/138 props >50 mm off; L03: 12 props need 7.5–12 m, not
  1.12 m; plots in `out/analysis/`).
- `test_formwork_gen.py` — pytest suite for the legacy generator.
- `requirements.txt` — ifcopenshell / shapely / numpy / pytest.
- `sample/Sunbreak_TestV5.ifc` — validated demo input (~4.9 MB). **Local only,
  git-ignored** — kept in the working repo, not pushed to GitHub. Exported
  from `_Sunbreak WIP\0706 Sunbreak Model\
  05-07-2024_Bellwhether_Concrete_UPDATED_R7.3dm` (18 MB, also local-only).

## Invariants — the formwork must satisfy all six (do not regress)

> **2026-07 update:** these six were written for the legacy per-floor
> generator and hold only on uniform tower floors. The Rhino generator
> upgrades #3/#4 to per-soffit-level platforms and per-prop ray-cast feet —
> the authoritative list is now in `rhino/README.md`. For any new work,
> satisfy the rhino/README invariants; the six below remain valid as the
> tower-floor special case.

These define *correct* formwork geometry for this project. Any change here, or
any C#/xBIM reimplementation, must preserve them. Each was verified to **0 mm**
on the tower floors (229 mm slabs).

1. **Trace the slab's real edge** — true plan outline (concavities + openings
   preserved), not a convex/simplified hull. Openings become voids; the prop
   grid skips them.
2. **Platform slightly larger than the slab** — the outline offset out by
   `platform_overhang` (default 0.1 m); it must fully cover the slab.
3. **Platform bears on the slab underside** — platform top Z = slab soffit.
4. **Prop feet on the slab-below top face** — support foot Z = the highest slab
   top of the floor immediately below.
5. **Prop heads snap to the platform** — support top Z = platform underside
   (soffit − panel thickness).
6. **Inter-floor gap = building slab thickness** — follows from #3 + #4;
   asserted by `test_interfloor_gap_equals_slab_thickness`.

Treat these six as the acceptance criteria. If you touch the geometry, re-run
the numeric check (see Verification).

## The IFC contract (why it chains with QTO_Tool)

Conventions already match `docs/ifc-export.md`, which is why the two connect.

**Reads** from the source IFC: `IfcSlab` geometry (any representation;
ifcopenshell tessellates it — `IfcFaceBasedSurfaceModel`, as `QTO_Tool` writes,
works directly); each slab's floor from its **`QTO Properties.FLOOR`** value
(falling back to `IfcBuildingStorey` containment); the source length unit.

**Writes**: IFC4, length unit copied from the source (**mm**, matching
`QTO_Tool`) so the output co-registers; spatial chain `IfcProject → IfcSite →
IfcBuilding → IfcBuildingStorey`; per floor one `IfcElementAssembly` of
`IfcBuildingElementProxy` (`ObjectType` = `platform` | `support`); every
element tagged with **`QTO Properties.FLOOR`** (the join key for 4D search
sets); geometry as `IfcExtrudedAreaSolid` in absolute mm.

> **Floor-naming — the most likely integration snag.** `--floors` selection and
> floor ordering assume labels of the form **`L<digits>`** (`L01`…`L16`), with
> `P1`/`R1`/`R2` sorted after. `QTO_Tool`'s `FLOOR` is whatever floor *name* the
> user typed. To chain the two, the QTO floor names must use the `L<nn>`
> convention, or extend `_floor_sort_key` / `_parse_floor_args`. Align the floor
> vocabulary before anything else.

## How it works (pipeline)

1. `scan_slabs()` — one `ifcopenshell.geom` pass over every `IfcSlab`; per floor
   records `soffit_z` (lowest underside) and `slab_top_z` (highest top), and
   collects footprint triangles for the selected floors.
2. `_trace_footprint()` — unions projected triangles into the true outline;
   drops triangulation-noise holes.
3. `add_floor()` — platform = outline `buffer(+overhang)` extruded down by the
   panel thickness with top at the soffit; supports = a grid clipped to the
   outline (skipping openings), each extruded from the platform underside down
   to the slab-below top.
4. `_IfcWriter` — authors the IFC4 entities, scaling metres back to source units
   so the output co-registers.

## Running it

```bash
python -m pip install -r requirements.txt

# whole building
python formwork_gen.py --ifc "./sample/Sunbreak_TestV5.ifc" \
  --out ./out/formwork.ifc --prop-spacing 3.8 --platform-overhang 0.1

# a few floors first (cheap, reversible probe)
python formwork_gen.py --ifc "./sample/Sunbreak_TestV5.ifc" \
  --out ./out/fw_5-7.ifc --floors 5,6,7
```

The module docstring shows the old Mast4D invocation
(`python -m mast4d.pipelines.formwork_gen …`); here it is a standalone module,
so call `python formwork_gen.py …`. Output IFCs (`out/`) are git-ignored.

Key flags: `--ifc` / `--out` (required); `--floors` (subset, omit for all);
`--panel-thickness` 0.05, `--prop-spacing` 3.0, `--prop-size` 0.15,
`--platform-overhang` 0.1 (metres); `--platform-name` / `--support-name`
(the `IfcName` on the proxies — align to the schedule's component vocabulary
to bind 4D search sets).

## Verification & tests

```bash
python -m pytest test_formwork_gen.py -v
```

Pure-function tests run everywhere; integration tests build a minimal two-floor
IFC and assert structure, the `FLOOR` pset, and invariant #6 (gap = slab
thickness). For the full numeric constraint check (all six invariants to 0 mm),
trace the source slabs and the output proxies and compare Z-planes and
footprints — the Mast4D scratchpad `verify_constraints.py` is the template.

## Integrating into this repo — decided

**This module stays Python and stays decoupled.** The earlier option B (port the
geometry to C#/xBIM beside `QTO_Tool`) is **rejected**: it would trade a module
that has been verified on 1,889 real elements and carries a 299-line headless
acceptance test for a C# rewrite with no tests at all. The dependency is one-way —
formwork consumes a completed QTO pass; QTO never waits on formwork.

**How the plugin will reach it (decided 2026-08-12, not yet built):** a separate
Rhino command `RunFormwork` opening its own `FormworkUI` window — *not* a new tab
in `QTOUI`. `QTOUI.xaml` gains a single button. Every Python run happens in a
**second, headless Rhino process opened on a `RhinoDoc.WriteFile` copy** of the
model, launched the same way `rhino/run_on_model.py` and `rhino/test_headless.py`
already do it.

Why the process boundary is the mechanism and not just tidiness:

- `rhino/formwork_gen_rhino.py` line ~43 defaults to `"mode": "generate"` — the
  destructive branch that calls `doc.Objects.AddBrep` on whatever `scriptcontext.doc`
  it is handed. Isolation makes that fail-open default *harmless*; a C# string
  literal setting `mode="export"` would only make it fail-*correct*.
- Anything added to the live document permanently disables `QTO_Tool`'s REVERT
  CHECKUP button (it is re-enabled at exactly one site, the checkup success path;
  Ctrl+Z does not restore it). Hence **no "place formwork into this model" button** —
  results open in a second Rhino instance.
- `UIMethods.GenerateLayerTemplate` enumerates `doc.Layers` regardless of lock state,
  so a `_FORMWORK` layer in the document grows template-picker rows and pollutes the
  checkup counts. If formwork geometry is detected in the document, Start Checkup must
  be hard-disabled, not warned about.

Implementation risks to plan for: `formwork_gen_rhino.py` has **no** `RhinoApp.Exit()`
(unlike the four scripts in `rhino/`), so `/runscript` must target a driver script or
the child Rhino never exits; a child launched with `CreateNoWindow=true` that hits a
dialog — including a licence check for the second seat — hangs invisibly, so a watchdog
timeout is required, not just a Cancel button.

## Next feature: side forms (边模) — requested, not yet designed

Today the generator emits only the **horizontal** temporary works: a soffit platform
per slab underside plus shoring props. The user has asked for **side forms** next.
Nothing below is decided — it is the context a design session should start from.

What already exists and is directly reusable:

- `_trace_footprint()` already produces the slab's **true plan outline with
  concavities and openings preserved** (invariant #1). That polyline is exactly the
  path an edge form follows; today it is only used to size the platform and clip the
  prop grid.
- Slab thickness is already derivable per floor — `scan_slabs()` records both
  `soffit_z` and `slab_top_z`, and the Rhino generator works per soffit face.
- **Pour-break splitting already computes where construction joints fall** and writes
  derived slabs whose new edges are precisely where bulkheads (施工缝端模) belong. The
  pour-break output is the natural input for that subset of side forms — see the
  pour-break section in the project memory and `out/pourbreak_report.json`.

Open questions for the design session:

- Scope: slab edges only, or also beam sides (梁侧模), wall faces and column faces?
  The original plan listed beam-side forms as phase P3.
- Suppression: an edge that sits on top of a wall or beam below does not need a form.
  What geometric test decides that — a downward ray, or an overlap against the
  floor-below outline (the same tightening already noted under Known limits)?
- Are pour-break bulkheads a distinct element type in the IFC (`ObjectType`), or the
  same `side` type distinguished by a property?
- Formwork **area** is itself a takeoff quantity (模板面积). Side forms are where that
  becomes worth reporting — decide whether quantities are emitted here or left to QTO.
- New invariants will be needed; the existing six are soffit-only and say nothing about
  vertical elements.

## Known limits

- Placeholder fidelity by design — not engineered falsework.
- Prop grid is clipped to the current floor's outline only, not intersected with
  the floor below; on a tower with similar outlines that leaves 0 % of props
  overhanging, but very different floor shapes could float edge props. Tighten
  by intersecting with the floor-below outline.
- Podium / transfer / roof levels inherit irregular prop heights from the
  model's own slab elevations; the tower cycle floors are clean and uniform. The
  Z-relationships still hold — "the slab below" is just at an unusual elevation.

## Keeping in sync

`formwork_gen.py` is a byte-for-byte copy of the Mast4D source. If upstream
changes, re-copy it (the file is self-contained, so nothing else travels with
it). Do not fork the logic in two places without noting it here.
