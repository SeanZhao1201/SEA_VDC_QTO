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

## Headless test loop (no Rhino UI)

- `test_headless.py` — synthetic podium scene (partial basement, stepped L1,
  L2 with opening + downstand beam + through column, void bay over nothing)
  with per-prop numeric assertions, plus write/purge round-trip and File3dm
  export checks.
- `run_on_model.py` — batch export run on the open model; dumps layer census
  and per-level table to `model_run_log.txt`.

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
