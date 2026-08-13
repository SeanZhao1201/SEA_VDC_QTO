# Rhino formwork generator (P1)

`formwork_gen_rhino.py` generates soffit formwork **platforms** and shoring
**props** directly from the open Rhino model. It replaces the per-floor
fixed-height logic of `../formwork_gen.py` (kept as legacy reference) with
per-soffit-face platforms and a **downward ray-cast per prop** onto the merged
solids of the model — so double-height voids, stepped podium slabs, and
partial basements all get correct prop heights automatically.

## Running in Rhino (7 or 8)

Open the concrete model, then `_-RunPythonScript` this file (legacy IronPython
engine — the supported one). Edit the `PARAMS` block first:

- `mode`
  - `"generate"` — add platforms + props to the doc under the `_FORMWORK`
    layer tree (locked, colored, one undo record). **Only ever adds objects.**
  - `"purge"` — remove exactly the `_FORMWORK` tree (objects + layers).
  - `"export"` — touch the document **not at all**; write the formwork to a
    separate `<model>_formwork.3dm` via File3dm.
- Lengths in metres: `panel_thickness` 0.05, `prop_spacing` 3.0, `prop_size`
  0.15, `platform_overhang` 0.1, `edge_inset` 0.5.
- Ray rules: hit closer than `min_clear` (0.3) or ray starting inside a solid
  → no prop (bearing wall / column / same-level downstand beam); height >
  `max_prop` (5.0) → `TALL` flag (needs engineered multi-tier shoring); no hit
  → stand on `grade_z` if set, else skip + `NOHIT_SKIPPED` flag.
- Slab selection: layers whose first `_` segment contains
  `slab_layer_keyword` ("slab"), minus `slab_layer_exclude` (["sog",
  "topping"] — slabs-on-grade and toppings need no soffit formwork; they
  remain ray obstacles).

Floor names come from the QTO plugin's `FloorElevations` document string
(nearest-elevation match, same as `Methods.FindFloor`); without it, levels are
named by elevation. Props carry `FLOOR` / `FW_STATUS` / `FW_HEIGHT_M` /
`FW_FOOT_Z` user strings; prop layers split by status: `Props` (blue),
`Props_TALL` (orange), `Props_GROUNDED` (vermillion).

## Acceptance invariants (upgrade of the six in ../CLAUDE.md)

1. Platform traces the true slab outline per **soffit level** (not per floor):
   concavities and openings ≥ `min_hole` preserved, stepped slabs get stepped
   platforms.
2. Platform = outline offset out by `platform_overhang`; top at that level's
   soffit; thickness `panel_thickness`.
3. Prop top = platform underside, exactly.
4. **Prop foot = first solid surface a downward ray hits at that (x,y)** —
   whatever floor it belongs to. No per-floor constants anywhere.
5. No prop where a solid bears directly under the soffit (< `min_clear`).
6. Every prop taller than `max_prop` is tagged `TALL`; every prop with no
   support below is tagged (`GROUNDED`/`NOHIT_SKIPPED`), never silent.

## IFC output

`export` mode also writes `<name>_formwork.json` (levels, platform
regions/holes, per-prop coordinates — metres, world coords). Convert it with
the CPython venv:

```powershell
& "$env:LOCALAPPDATA\qto_fwenv\Scripts\python.exe" ..\formwork_ifc_from_json.py `
    --json <name>_formwork.json --out <name>_formwork.ifc
```

Output follows the legacy IFC contract (IFC4, mm, absolute coords, storey per
floor, IfcElementAssembly per level, `QTO Properties.FLOOR` join key) plus
STATUS / HEIGHT_M on each support for 4D filtering. One deviation from the
legacy writer: storey elevations are soffit-level (min level z per floor)
rather than copied from the source IFC's storeys — geometry is absolute, so
co-registration is unaffected.

## Pour-break authoring (v2)

The modeler draws pour-break curves on the **`_POURBREAK`** layer — lines or
polylines, any orientation, jogs allowed; optionally on a per-floor sublayer
`_POURBREAK::<floor>` to pin the floor explicitly — and drops a text dot
("1", "2", …) inside each pour region to author the pour order. Three
scripts turn that into a derived model:

- `pourbreak_harvest.py` — **read-only** walk of the `_POURBREAK` tree →
  `<model>_pourbreaks.json` (schema v2: model-unit world coordinates,
  polylines + markers, per-item floor binding by sublayer or elevation).
  Read-only means it fires no document events, so QTO's REVERT CHECKUP
  survives a harvest. Hidden and locked objects are included (same
  enumerator as the wipe — a hidden break must still reach the JSON).
- `pourbreak_restore.py` — the inverse: redraws curves + dots from the JSON.
  This is the recovery path for the known hazard that the QTO checkup
  deletes every curve in the document; harvest early, restore after. It
  ADDS objects (which disables REVERT CHECKUP until the next checkup), so
  it belongs after the QTO pass. Faithful re-materialization: what was
  authored on a sublayer goes back there, elevation-bound items go back to
  the root — a re-harvest reproduces the same JSON byte for byte (sampled
  non-polyline breaks come back as polylines stamped `PB_CURVE_TYPE` so
  their curved-bulkhead flag survives). A floor whose name is missing from
  the document's `FloorElevations` and whose items carry no z is
  **refused loudly**, not drawn at z=0 — drawing it would rebind the
  breaks to whatever floor sits nearest zero and cut the wrong slabs.
- `split_pourbreaks.py` — splits every matching slab on a **staged copy**
  (never the original), tags pieces `POUR<n>` on suffixed layers with
  `POUR` / `POUR_FLOOR` / `SOURCE_SLAB` user strings, `WriteFile()`s the
  derived .3dm and writes the review report. Schema v1 (the PDF-era
  axis-aligned cuts) upconverts in memory — historical break sets keep
  working. Units are handled like the generator (metre constants ×
  `UnitScale`); the feet-only guard is gone, replaced by a JSON-vs-model
  unit match check, and a missing breaks JSON is a hard stop (no fallback
  to a stale staging file). Pour numbering is **dot-containment first**: a
  piece that contains a pour dot takes that dot's number — exact for any
  break shape (concave pieces whose centroid escapes them, notches routed
  around openings, re-entrant polylines crossing a slab twice). Only
  dotless pieces fall back to side-key cells keyed by a
  guaranteed-interior point; with exactly one marker the rest is pour 2
  (v1 binary semantics — the golden regression depends on it), and every
  fallback is summarized in the log. Originals are deleted with an
  unlock/show-and-retry (`force_delete`) so locked or hidden slabs cannot
  end up duplicated beside their pieces; an undeletable original aborts
  that slab's split loudly. The sliver guard is waived when a pour dot
  sits inside the small piece (an authored small pour is legal). Paths
  override via `PB_JSON` / `PB_OUT3DM` / `PB_REPORT` env vars.

The report is the engineer-facing review artifact: per-pour soffit area +
volume (CY on feet models) against the optional `target_area`, minimum
break-to-support distance (flagged under 1 m — joints belong near
mid-span; bbox accuracy, a warning not an engineering check), grid offsets
for axis-parallel segments when a grid is present, and every reverted or
uncrossed slab with its reason.

## Headless test loop (no Rhino UI)

- `test_headless.py` — synthetic podium scene (partial basement, stepped L1,
  L2 with opening + downstand beam + through column, void bay over nothing)
  with per-prop numeric assertions, plus write/purge round-trip and File3dm
  export checks.
- `run_on_model.py` — batch export run on the open model; dumps layer census
  and per-level table to `model_run_log.txt`.
- `test_pourbreaks_headless.py` — synthetic **metric** scene for the
  pour-break pipeline; one floor per defect class from the 2026-08-13
  adversarial review: under-drawn diagonal with a snap-noise micro tail
  (hidden curve), jogged polyline on a locked layer, arc break
  (curve-type persistence), authored sub-1% corner pour, U-notch whose
  concave piece loses its centroid, re-entrant break crossing a slab
  twice, plus SOG exclusion, support-distance flag, unit-mismatch guard,
  unknown-floor restore refusal, harvest read-only check, and the harvest
  → wipe → restore → harvest byte-identity round trip.
- `test_pourbreaks_model.py` — golden regression on the staged Bellwether
  copy: v1 JSON → upconvert → restore as curves → harvest → split must
  reproduce the verified 2026-07 result (18 slabs → 36 pieces; per-piece
  pour label, volume and soffit area within 1 %).

Both are launched by copying the scripts (and a model copy) to
`%LOCALAPPDATA%\qto_fw_test\` (short, space-free path) and running:

```powershell
$stage = Join-Path $env:LOCALAPPDATA 'qto_fw_test'
$env:FW_HEADLESS = '1'   # makes the scripts exit Rhino when done
& 'C:\Program Files\Rhino 8\System\Rhino.exe' /nosplash /notemplate `
  "/runscript=`"-_RunPythonScript $stage\test_headless.py`""
# real model: Rhino.exe $stage\model_R7.3dm /nosplash "/runscript=..."
```

Independent numeric verification: a CPython checker (venv
`%LOCALAPPDATA%\qto_fwenv`) reads the exported 3dm with rhino3dm and re-derives
every prop foot with a separate ray-cast over the QTO IFC triangle soup.

Status 2026-07-23: synthetic test ALL PASS; Bellwether R7 run produced 1842
props / 47 platforms with 100% agreement on all three checks (platform-on-
soffit, prop-top snap, prop-foot ray-cast); output copied to
`../out/Bellwether_R7_formwork.3dm` (+ `_log.txt`). A 19-agent adversarial
review then hardened the script: provenance-tagged purge (only deletes what
the tool generated), auto-purge before regenerate, block-instance explosion
(post-Blockify models work), loud warnings for every silent-degradation path
(sloped soffits, failed offsets, dropped holes, zero-prop platforms, CPython
engine fallback), case-insensitive layer matching, layer-name sanitizing.

Known accepted behaviors: a merged level sits at its LOWEST member soffit
(faces within `z_cluster_tol` above it get a shim-sized gap — platforms must
clear the lowest face); ramps/sloped soffits are out of scope (flat platform
+ warning); a face whose centroid falls outside itself (L-shapes) may end up
with a platform but no props — warned, not silent.
