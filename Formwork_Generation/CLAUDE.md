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
- `rhino/pourbreak_harvest.py`, `rhino/pourbreak_restore.py`,
  `rhino/split_pourbreaks.py` — the authored pour-break pipeline (v2,
  2026-08-13): read-only harvest of `_POURBREAK` curves + pour dots to
  JSON, faithful restore (the checkup-wipe recovery), and the splitter
  that runs on a staged copy. Tests: `rhino/test_pourbreaks_headless.py`
  (synthetic metric scene) and `rhino/test_pourbreaks_model.py` (golden
  regression against the 2026-07 Bellwether result). Details:
  `rhino/README.md`.
- `rhino/sideform_gen_rhino.py`, `rhino/run_sideforms_on_model.py`,
  `rhino/test_sideforms_headless.py` — the side-form + bulkhead engine
  (2026-08-13): edge/opening classification by ray-cast suppression and
  neighbour probing, net formwork areas, generate/export/purge modes.
  See the Side forms section below and `rhino/README.md`.
- `rhino/breaksheet_gen.py`, `rhino/breaksheet_import.py`,
  `rhino/test_breaksheet_headless.py` — the BREAK SHEET authoring surface
  (P1, 2026-08-17): generates a plan-cell sheet (one cell per
  exact-fingerprint floor group — the typical-floor collapse), imports the
  drawn sheet back to JSON v2 with explicit TYP fan-out. Both run
  IN-PROCESS on `Rhino.FileIO.File3dm` (no child Rhino, live model never
  touched). See "Break sheet" under Pour-break authoring below.
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

> **Element identity — `QTO_STABLE_ID` (2026-08-23).** The QTO checkup
> deletes and re-adds every solid, re-minting `obj.Id` on every run, so the
> object id alone cannot link the two exports. The checkup stamps each
> object's FIRST-seen id into the `QTO_STABLE_ID` user string (preserved on
> re-checkups; attributes survive the re-add), `split_pourbreaks` stamps
> every pour piece with its own new id, and `formwork_gen_rhino._ident`
> prefers a valid-GUID stamp over `obj.Id` — the same preference the QTO
> exporter's `SetDeterministicGlobalId` applies. `SLAB_GLOBALID` is the
> ifcopenshell `guid.compress` of that identity, so it resolves directly to
> the take-off IFC's `IfcGlobalId` (verified 67/67 on the Bellwether derived
> model, 2026-08-23). Never copy a `QTO_STABLE_ID` onto a new object.

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

**How the plugin reaches it (decided 2026-08-12, BUILT 2026-08-13 — see the
repo-level CLAUDE.md for the as-built description):** a separate
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

## Side forms — scope decided 2026-08-12, BUILT 2026-08-13

The vertical temporary works now exist: `rhino/sideform_gen_rhino.py`
(engine + generate/export/purge modes), `rhino/run_sideforms_on_model.py`
(batch driver), `rhino/test_sideforms_headless.py` (synthetic acceptance
test), and `formwork_ifc_from_json.py --sideforms` (combined IFC with all
four ObjectTypes; works standalone too). Verified on the Bellwether
derived model: 235 side forms + 21 bulkheads, per-floor area
reconciliation clean, combined IFC with 47 platforms + 1842 supports +
sides + bulkheads across 18 storeys.

**Hardened by a 29-agent adversarial review (2026-08-13, 18 confirmed
defects, all fixed):** per-face LOCAL top via an upward ray on the slab's
own mesh (the whole-brep bbox top inflated thickness on stepped slabs and
degenerated every probe on split-level ones — the fix recovered 70
previously-skipped panels on Bellwether); sloped soffits skip loudly with
the area kept in the books (the flattened-loop model falsely suppressed
the downhill half — same out-of-scope stance as the platform generator's
ramps); joint probes test three heights across the face thickness
(single mid-height probes broke bulkhead dedupe for unequal-thickness
neighbours); internal soffit-face seams classify explicitly (never
inherit stale normals into phantom panels); whole-loop runs build closed
ring solids exported as profile+hole (a slit C-annulus looked solid but
gapped); straight class-transitions extend to the midpoint while corner
transitions bill their half-segment without bow-tie geometry; dropped or
loft-failed runs land in `unclassified_area`; block instances explode
in-memory reading POUR from the definition parts (QTO's Blockify wraps
every object — the engine found zero targets on blockified models);
`fw.purge_formwork` gained a type whitelist so the platform generator's
auto-purge no longer eats side panels (and vice versa); `doc.Modified`
is cleared only in headless runs; the IFC writer guards schema-invalid
empty aggregates and assigns platform holes by representative point.

**Geometry parameters resolved at implementation (2026-08-13):** loops
sampled every 0.25 m; suppression reuses the props' RayShoot semantics
(ray from a point probed 0.05 m inside the edge, just below the soffit —
starting inside a solid or hitting one within 0.05 m suppresses); joint
detection probes 0.05 m outside the edge at mid slab thickness for
another slab solid; **one bulkhead per joint**, owned by the lower-pour
side (object-id tiebreak — the same test catches pour-break siblings AND
independent abutting slabs); panels are capped lofts offset outward by
`panel_thickness` (0.05 m), soffit to slab top, no kicker; runs shorter
than 0.10 m dropped; internal soffit-face seams classify as neither and
land in `unclassified_area` (no form on a seam — correct, but the area
still reconciles). 4D pour grouping is **pset-only** (`POUR` in
QTO Properties; assemblies stay per-floor) — Synchro search sets filter
on properties, and this avoided restructuring the assembly tree.
**Reconciliation invariant** per floor: `side + bulkhead + shared(joint
ceded to the neighbour) + suppressed + unclassified == gross` (loop
perimeter × thickness), asserted in the test and warned on at runtime.

The original 2026-08-12 decision record follows.

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

## Pour-break authoring — decided and built 2026-08-13

The original pour-break pass was reverse-engineered from one PDF: markups →
`pour_breaks_model.json` → `rhino/split_pourbreaks.py`. The two front-end scripts
(`extract_pourbreaks.py`, `make_breaks_model.py`) were scratchpad-only and are not
in the repo, the v1 JSON could only express axis-aligned orthogonal cuts
(`dir: NS|EW` + one coordinate + span), and pour numbering was binary
(POUR1/POUR2 via the PDF's centroid). Productizing meant giving the modeler
authorship. Decisions (all implemented — see the Files section and
`rhino/README.md` for the as-built pipeline; the splitter upconverts v1 JSON
in memory, so the historical Bellwether break set still runs, verified by the
golden regression):

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

**Break sheet — the primary authoring surface since P1 (2026-08-17,
decided over the earlier GetPoint/conduit target state after a judged
design study; field feedback drove it).** `MAKE BREAK SHEET` in FormworkUI
generates `breaksheet.3dm` in staging: one plan cell per floor GROUP
(floors with byte-identical quantized slab-footprint fingerprints share a
TYP cell — proven on the real Bellwether-class model, tower collapsed
5+5; near-identical groups are reported, never auto-merged). Locked
furniture per cell: per-slab top-face outlines (existing deck joints stay
visible — the L01 lesson), opening loops, support bbox footprints, frame,
label; anything the user draws on an unlocked layer inside a frame is a
break (open curve) or pour marker (numbered text dot) — no layer
discipline. `IMPORT SHEET` reads the drawn file via `File3dm`, binds by
cell containment (floor NAME binding, stronger than nearest-Z), fans TYP
cells out explicitly per member floor, and REFUSES totally on straddling/
orphan/closed curves with the JSON untouched. Both scripts run
**in-process** on `Rhino.FileIO.File3dm` — no child Rhino, the live model
file is never written to, REVERT CHECKUP is unaffected. Invariants the
30-assert headless test pins: floors without a cell on the sheet keep
their previous JSON entries verbatim (hidden/renamed slabs must never
cause silent break loss); carried-in ink counts toward the cell extents
(over-drawn harvested lines stay in-frame); `curve_type` survives the
round trip via `PB_CURVE_TYPE`; slab selection matches the splitter's
keyword/excludes; a MAKE-then-IMPORT of an untouched sheet reproduces the
JSON (ids/ordering included; `provenance` is surface-specific). The
sidecar `breaksheet.meta.json` carries the cell map + source doc path and
is required for import. The C# handlers judge each run by what changed on
disk (error file + mtime), never by the previous run's log.

**Break sheet P2 (2026-08-19) — same contract, three additions, all
pinned by the headless test (now 53 asserts, ALL PASS in real Rhino 8)
and hardened by a 19-agent adversarial review (14 confirmed defects, all
fixed pre-merge):**

- *Near-TYP merge*: the generator writes `near_typ` suggestions and the
  applied `merged` sets into the sheet meta; after a successful MAKE,
  FormworkUI offers both in the `SheetMergeUI` checkbox dialog (applied
  merges pre-checked — unchecking separates the cells again). Picks land
  in `breaksheet_merge.json` (`{docPath, merge: [[floors…]]}`, docPath-
  AND shape-guarded — malformed or foreign files degrade to a warning,
  never a crashed MAKE) and trigger ONE regeneration, never a re-offer.
  Directives apply IN FILE ORDER, each validated against the cells'
  CURRENT (union) fingerprints with the same near-TYP rule the
  suggestions use — one metric on both sides, so every offered merge is
  honorable and a stale directive whose floors genuinely diverged fails
  with a loud `MERGE REFUSED`. The dialog writes applied merges before
  new picks; the incremental validation depends on that order.
- *Dirty badge + auto-import on Split*: `FormworkMethods.SheetDirty`
  drives an orange dot on IMPORT SHEET, refreshed on window activation
  (the sheet is saved in a second Rhino). Dirty means: sheet saved after
  MAKE wrote the meta sidecar AND not imported since — where only a
  `source.kind == "sheet"` breaks JSON counts as "imported since" (a
  harvest rewrite is newer but lacks the sheet's ink, so the badge stays
  on), and a sheet whose meta docPath names another model is never dirty
  here. Split offers Yes(import, then split)/No(split with the current
  JSON)/Cancel and aborts when the pre-split import refuses. Advisory
  only — the import's own gates stay the authority. `ImportSheetCore`
  also gained the foreign-sheet confirm dialog the Python-side warning
  had always deferred to.
- *Import advisory + preservation*: the import PRESERVES per-floor
  `target_area` and top-level `grid_x`/`grid_y` (P1 wiped them on every
  reimport; a covered cell with no ink now clears breaks/markers but
  keeps the target on an otherwise-empty entry), and refuses to preserve
  anything from a previous JSON whose `source.model` names another model
  file — the same laundering guard as the generator's carried-in breaks.
  After success the log carries an ADVISORY block: per-pour soffit areas
  (furniture outlines split by the drawn ink extended by the splitter's
  first 5 m rung, pours assigned by marker containment, inch models
  reported in sf per the QTO convention, "did not sever" judged against
  the pre-split face count) with per-floor target ratios, plus
  nearest-grid offsets per axis-parallel segment numbered by PB id (a
  deliberate replica of `split_pourbreaks.grid_offsets` — importing the
  splitter module would run its top level in the host Rhino; the
  headless test's full-line fixed points keep the two in step). Advisory
  failures log one line and never block or alter what is written.

**Break sheet P3 (2026-08-19) — in-model rendering + named schemes,
C#-side, pinned where Python is involved (60-assert headless suite):**

- *Read-only overlay* (`QTO_Tool/PourBreakOverlay.cs`, a
  `DisplayConduit` singleton): the ACTIVE breaks JSON drawn into the
  live viewports — magenta polylines (lifted 0.02 m-equivalent off the
  slab tops) + numbered pour dots — with ZERO objects added to the
  model file, so REVERT CHECKUP is untouched and the checkup has
  nothing to delete. No editor debt: the sheet and the `_POURBREAK`
  layer stay the only authoring surfaces. Guards: whole-parse
  try/catch (a shape-malformed JSON lands in the callers' false-branch,
  never an unhandled dispatcher exception, never a partial draw);
  docPath refusal for a foreign JSON — EXCEPT one the user explicitly
  confirmed via LOAD OPTION (the active-option note for this doc with a
  matching sha), which draws with a provenance warning in the summary;
  a `RhinoDoc.CloseDocument` hook kills the conduit on File > Open/New
  (registration is application-wide and would otherwise keep painting
  model A's breaks into model B); FormworkUI's toggle resyncs on
  Activated and the window's Closed event turns the overlay off.
  Refresh sites: import success, harvest, option load, RE-CHECK.
- *Named break schemes* (Option-1/2…): a scheme is a snapshot of the
  breaks JSON stored NEXT TO the model file
  (`<model>_breaks.<name>.json` — never in the machine-wide staging
  folder). SAVE AS OPTION / LOAD OPTION in FormworkUI (Rhino-native
  ShowEditBox/ShowListBox); unsaved docs are refused; LOAD confirms,
  replaces the active JSON (the derived model then goes stale on its
  own via the sidecar's breaksSha), and refreshes every gate. The
  active-option note (`breaks_active_option.json` in staging:
  docPath + name + sha, STRICT docPath equality on both C# and Python
  sides) feeds the "Active scheme: X (modified since)" label without
  ever dirtying the document. `FormworkMethods.SanitizeOptionName` is
  the single name grammar - ListOptionNames is closed under it, so a
  dotted file (hand-renamed, or another model whose base name embeds
  this one's prefix) is never offered for a load that SAVE would
  silently retarget.
- *Option stamp on the sheet*: `breaksheet_gen` reads the note (same
  shape/docPath guards; sha compare — a missing or changed JSON stamps
  "(modified)", byte-matching the C# predicate) and labels the title
  dot `[OPTION: x]` + `meta["option"]`, so two printed option sheets
  can be told apart.
- *IronPython file-lock hygiene*: every JSON read in the break-sheet
  modules uses `with` — IronPython has no refcount collection, and an
  unclosed .NET stream blocks a later DELETE of the file even though
  rewrites merely share (found the day a test first deleted the JSON
  mid-process).

The original GetPoint-authoring idea stays superseded by the sheet. The
layer path below remains the power-user back door feeding the same JSON.

**What the sheet's locked furniture is, and is not (2026-08-20, from a
field question — "the reference lines don't always match that floor").**
The furniture is a *drawing reference*, not a survey of the floor. Four
documented reasons it can differ from the real level, in the order they
actually bite:

1. **A TYP cell draws its REPRESENTATIVE member only.** Exactly-identical
   floors are safe by construction (byte-identical quantized fingerprints).
   A *user-directed* near-TYP merge is not: the generator logs
   `MERGED per user directive: … representative 'X' - its footprint is the
   one drawn`, and every non-representative member differs by however many
   vertices the NEAR-TYP line reported. On the Bellwether the modeller
   merged L07…L16 into one cell whose members differ by 4 of 46 vertices,
   so L11–L15 are drawn with L07's outline. Supports differ more often than
   outlines and get their own per-member `NOTE:` line.
2. **Supports are bounding-box rectangles**, not true sections — placeholder
   fidelity, same as the formwork generator's prop proxies. A diagonal or
   L-shaped core wall shows as one rectangle.
3. **Only pour-break slab targets are drawn** (`SLAB_EXCLUDE`). Topping is
   excluded, so a floor with toppings — L01 has eight — is drawn with less
   area than it physically has. SOG *is* drawn since 2026-08-20.
4. **Supports are collected in a band** `SUPPORT_BAND_M = 3.0 m` below the
   floor top; anything stopping lower is not shown.

Explicitly NOT a cause on the Bellwether: sloped or stepped slab tops. All
56 pour-break slab targets were measured 2026-08-20 — every top face is
planar (max tilt 0.00 deg, zero Z-span) and every slab's up-faces sit at a
single level. Ramps remain out of scope for the formwork generator, which
warns per face when a soffit spans Z; the sheet has no equivalent warning
because no model has needed one yet.

The reliable reference for drawing against is the **slab outline** (it is
osnap-able and exact for the representative floor); treat supports as
indicative. When a merged TYP cell matters, un-merge it in the MAKE dialog
and draw that floor on its own cell.

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

**Batch-3 hardening (2026-08-19):** the layer-tree matchers tightened to
the true `::` path separator on BOTH sides (`is_pb_layer` /
`_is_formwork_layer` and the C# `FormworkMethods.LayerInTree` now agree
byte-for-byte: a top-level layer literally named `_POURBREAK:X` is a
name, not a sublayer); the splitter's pass 3 verifies every piece's
`AddBrep` Guid — on a failure it deletes the added siblings, re-adds the
ORIGINAL slab, and marks the report (`add_failed`, and `restore_failed`
+ soffit subtracted from the floor total when even the re-add fails), so
the derived model can no longer silently lose volume under a clean
report; and restore writes `pourbreak_restore_result.json`
(added/floors_skipped counts) + saves its log in a `finally` — the
FormworkUI handler judges the run by that file freshly appearing
(pre-deleted, mtime-guarded) and surfaces skipped floors loudly instead
of always claiming "Breaks re-drawn".

**Hardened by a 30-agent adversarial review (2026-08-13, 18 confirmed
defects, all fixed and covered by tests):** pour assignment is
dot-containment first (a concave piece's volume centroid can land across the
break — side keys alone mislabel notched and re-entrant cuts); originals are
force-deleted with unlock/show retry so locked or hidden slabs cannot end up
duplicated in the derived model; harvest enumerates hidden objects
(symmetric with the wipe); restore refuses floors missing from
`FloorElevations` instead of fabricating z=0 curves; sampled curve breaks
keep their `curve_type` through wipe/restore via a `PB_CURVE_TYPE` user
string; the splitter aborts on a missing breaks JSON rather than falling
back to a stale staging file; support review checks every soffit elevation
of a stepped floor; CY is emitted for inches models too (QTO converts both
ft and in); duplicate floor names warn loudly in harvest and split.

**Portability (audited 2026-08-13, residue cleared in the v2 rework):** the
P1 generator was already project-agnostic (metre params ×
`RhinoMath.UnitScale`, configurable `slab_layer_keyword`/`slab_layer_exclude`,
floors from `FloorElevations` verbatim). The splitter now matches it: the
Feet-only abort and ft extension constants are gone (metre constants ×
UnitScale + a JSON-vs-model unit match check), the "slab"/"sog"/"topping"
layer filter is the same params the generator has, and the Bellwether paths
became `PB_*` env vars with doc-adjacent defaults.
`formwork_ifc_from_json.py` now sorts storeys by **elevation** instead of
parsing `L<nn>` names. Note the v1 JSON could not even express a diagonal
cut, so the old pipeline was unusable on non-orthogonal buildings — the
polyline schema fixes that, it is not a nicety. Everything else that varies
per project (grade_z, prop spacing, `target_area`, grid, the PDF) is an
input, not a coupling. The legacy `formwork_gen.py` keeps its own
`_floor_sort_key` by policy (byte-for-byte Mast4D copy).

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
