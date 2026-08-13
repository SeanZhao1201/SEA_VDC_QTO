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
  per floor collapses stepped slabs, and props under double-height voids get the
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

## Next feature: side forms — scope decided 2026-08-12, geometry not yet designed

Today the generator emits only the **horizontal** temporary works: a soffit platform
per slab underside plus shoring props. Side forms are next. The scope and reporting
decisions below were made 2026-08-12; the detailed geometry design (tolerances,
sampling, panel params, new invariants) still needs its own session.

What already exists and is directly reusable:

- `_trace_footprint()` already produces the slab's **true plan outline with
  concavities and openings preserved** (invariant #1). That polyline is exactly the
  path an edge form follows; today it is only used to size the platform and clip the
  prop grid.
- Slab thickness is already derivable per floor — `scan_slabs()` records both
  `soffit_z` and `slab_top_z`, and the Rhino generator works per soffit face.
- **Pour-break splitting already computes where construction joints fall** and writes
  derived slabs whose new edges are precisely where bulkheads belong.

**Decided (2026-08-12):**

- **Scope v1: slab edge forms + pour-break bulkheads + opening edges** (shaft and
  stair-opening perimeters — the opening inner loops are already in the traced
  outline, so they are geometrically free). Beam sides stay in P3; wall and
  column faces are out of scope — they belong to a different formwork system (gang
  or crane-cycled forms) and QTO already reports their gross side areas.
- **Input model: prefer the pour-break derived model when one exists.** Split-derived
  edges classify as bulkheads (detectable via the `POUR`/`SOURCE_SLAB` user strings
  the splitter writes), original outline edges as side forms — one generation path,
  two classifications. On an unsplit model it degrades to pure side forms.
- **Suppression** (an edge bearing on a wall/beam below needs no form): sample along
  the edge and **ray-cast down** — the same RayShoot machinery the props use; a hit
  whose top face is at the soffit elevation (within tolerance) suppresses that
  segment. The plan-overlap test against the floor-below outline is kept as the
  *independent verification* path, mirroring the P1 generate-vs-verify split.
- **IFC typing: `ObjectType` gains `side` and `bulkhead`** alongside
  `platform | support`. 4D search sets bind on ObjectType and the two are struck at
  different times (bulkheads come off before the adjacent pour), so a property-only
  distinction would push a filtering step onto the Synchro side.
- **Quantities: net formwork area is reported on the formwork side, not
  QTO's.** Each side/bulkhead element carries `AREA` in its `QTO Properties` pset
  (same pattern as the props' `HEIGHT_M`), with per-floor totals in the export JSON;
  reports are generated from that JSON by a standalone CPython script, in the same
  mold as `formwork_ifc_from_json.py`. Nothing is written back into the QTO Excel —
  the one-way dependency stands. QTO keeps its gross geometric areas
  (`SlabTemplate.edgeArea`/`perimeter` etc.); *net ≤ gross* is the cross-check
  between the two.
- **GUI cross-point** (the only one; GUI stays a separate discussion, see the repo
  memory): `FormworkUI` needs an input-model selector (original vs pour-break
  derived 3dm). Everything else rides in the config JSON the driver script reads.

**Draft invariants** to finalize in the design session (the existing six are
soffit-only):

1. Side-form path = the true plan outline, concavities and opening inner loops
   included.
2. Side-form height = slab thickness (`slab_top_z − soffit_z`, per soffit face).
3. A suppressed edge segment must have a supporting solid top face directly below
   within tolerance (necessary and sufficient; independently verifiable).
4. Bulkheads lie on pour-break joint lines, length = the joint segment.
5. Net formwork area ≤ outline perimeter × thickness; the deficit equals suppressed
   length × thickness (reconciles against QTO's `edgeArea`).

**Still open for the design session:** sampling spacing and suppression tolerance;
side-form panel thickness/offset detail (placeholder fidelity, as ever); whether
elements group per pour piece (one assembly per POUR block) so 4D strike semantics
fall out of the containment tree; kicker/edge details — likely rejected as
over-fidelity.

## Pour-break authoring — decided 2026-08-13, not yet built

The shipped pour-break pass was reverse-engineered from one PDF: markups →
`pour_breaks_model.json` → `rhino/split_pourbreaks.py`. The two front-end scripts
(`extract_pourbreaks.py`, `make_breaks_model.py`) were scratchpad-only and are not
in the repo, the JSON can only express axis-aligned orthogonal cuts
(`dir: NS|EW` + one coordinate + span), and pour numbering is binary
(POUR1/POUR2 via the PDF's centroid). Productizing means giving the modeler
authorship. Decisions:

**Invert the pipeline — curves are the single source of truth.**
`[optional PDF importer] → curves on a convention layer ← modeler draws/edits
freely → read-only harvest → JSON v2 → splitter`. The splitter's battle-tested
mechanics (tolerance ladder, `CreateBooleanSplit` fallback, sliver guard,
volume-conservation check, staged-copy `WriteFile` flow) are untouched by where
breaks come from; only the cut geometry generalizes. The PDF path is **demoted to
a bootstrapper**: it emits curves onto the layer for humans to review and adjust,
never JSON directly; its scripts get cleaned up and committed when that lands.

**Authoring surface v1: `_POURBREAK` layer + harvest.** Modeler draws plan
curves on `_POURBREAK` (optional `::L<nn>` sublayers to pin the floor). A pure
Python, **read-only** harvest walks the layer and writes JSON v2 — reading fires
no document events, so REVERT CHECKUP is unaffected. Two known hazards, both
accepted with mitigations rather than QTO changes:

- **The QTO checkup deletes curves** (by design — it deletes every object and
  re-adds only solids). Harvest *is* the persistence: a `restore` command redraws
  the curves from JSON after a wipe. Discipline: breaks are drawn after the QTO
  pass; a broken rule costs only the un-harvested delta.
- **Layer-table pollution**: `_POURBREAK` grows a `GenerateLayerTemplate` row and
  shows in checkup counts. Accepted for now — **QTO stays untouched** (decided
  2026-08-13); an "ignore `_`-prefixed layers, with a logged count" patch is a
  v1.1 candidate, not part of this work.

Target state once `FormworkUI` exists: breaks as pure data (doc user text /
JSON) picked with `GetPoint` and rendered via a **display conduit** — never in
the `ObjectTable`, so both hazards vanish; the layer path then remains as a
power-user back door feeding the same JSON.

**Cut semantics v2:** a break is a **plan polyline — any orientation, jogs
allowed** (routing around openings is normal practice); arbitrary planar curves
are accepted but non-line segments are flagged in the report, not rejected.
Cutter = vertical extrusion through full slab depth; the progressive
end-extension ladder (15/60/150 ft) is kept so under-drawn lines still sever.
Floor binding: the curve's own Z, nearest-matched against `FloorElevations`
(sublayer name overrides); the harvest report states every binding for review.
Every slab on that floor the cutter actually crosses is split ("not crossed"
status stays).

**Pour numbering: text dots + automatic fallback.** The modeler drops a text dot
("1", "2", "3"…) inside each pour region on the layer — the generalization of
the old `pour1_centroid_ft`. Floors without dots get automatic ordering along
the dominant cut direction. Pour order is scheduling intent: authorable, never
forced.

**Schema v2 sketch** (`pour_breaks_model.json`): `version: 2`; per floor
`breaks: [{id, polyline_ft: [[x,y]…], z_ft, provenance, note}]` replacing
`{dir, pos_ft, span_ft}` (a v1 cut converts to a two-point polyline);
`pour_markers: [{pour, at_ft}]` replacing `pour1_centroid_ft`; `pdf_sf` renamed
`target_sf` (optional pour-size target); `grid` optional; `source` records the
producer (`layer-harvest | pdf | ui`).

**The review report is freedom's counterpart** — break placement is an EOR
decision, so every run emits the engineer-facing artifact (extended
`pourbreak_report.json`, Turner-style HTML later): per-pour soffit sf and CY
against `target_sf`; minimum distance from each break segment to vertical
supports below (construction joints belong near mid-span — flagged, never
blocked); offset to the nearest grid line (grid now optional); sliver/volume
reversions surfaced with reasons. Iteration is cheap — draw, run headless on a
staged copy, take the report to the engineer.

**Portability (audited 2026-08-13 — "would this survive a non-Sunbreak
project?"):** the P1 generator already would (metre params ×
`RhinoMath.UnitScale`, configurable `slab_layer_keyword`/`slab_layer_exclude`,
floors from `FloorElevations` verbatim). The residue is in
`split_pourbreaks.py` and must go during the v2 rework: (a) drop the
Feet-only abort and the ft extension constants — adopt the generator's
metre-params + UnitScale pattern, honoring the JSON `units` field; (b) the
hardcoded "slab"/"sog"/"topping" layer filter becomes the same params the
generator has; (c) the Bellwether paths (already listed). Also
`formwork_ifc_from_json.py::_floor_sort_key` assumes `L<nn>`-style names —
sort storeys by **elevation** instead (always available; names then carry no
semantics). Note the v1 JSON could not even express a diagonal cut, so the old
pipeline was unusable on non-orthogonal buildings — the polyline schema fixes
that, it is not a nicety. Everything else that varies per project (grade_z,
prop spacing, `target_sf`, grid, the PDF) is an input, not a coupling.

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
