# Jump-form + reshoring engine — design (DECIDED + BUILT, 2026-08-24)

Status: **all four decisions taken (user, 2026-08-24, every recommendation
accepted) and the build landed the same day** — see the repo CLAUDE.md
Development status for the as-built verification numbers. This closes
repo CLAUDE.md open item 1 (270 unbound Mast4D animation slots). Sources:
the Mast4D repo's own schedule and contract documents (primary), a measured
survey of `Tested2.ifc` (the 2026-08-23 take-off export), and the existing
engines' code. Everything below is verified against those, not assumed.

## 1. Demand — what the schedule actually binds (primary sources found)

The review that defined the gap is
`..\01_Mast4D\docs\geometry_requests_sunbreak.md`; the schedule that
consumes the geometry is `..\01_Mast4D\schedules\schedule_sunbreak_pb.md`
(SPO weekly cycle, floors **L02–L16**, 6-working-day cycle, 12 dark
template activities × 15 floors). Their component glossary is
`..\01_Mast4D\docs\naming_conventions.md`.

Slot arithmetic per floor N (from the activity table, verbatim):

| Component | Slots/floor | Referenced at | Example activities |
|---|---|---|---|
| `Jump Form Locked` | 8 | @N and @N+1 | 1040 pour (Maintain @N), 3030 lock (Install @N+1), 4010–6040 (Maintain @N+1) |
| `Jump Form Unlocked` | 4 | @N and @N+1 | 2020 strip (Install @N, **Remove Locked @N same task**), 2030 climb (Install @N+1, Remove @N) |
| `Pole Shore for Reshoring` | 6 | @N−2, @N−4 | 2080/3080 install @N−2, 3070 fly (Install @N−2, Remove @N−4), 8040 removal @N−2 |

Consequences that settle the old design questions:

- **Both states must exist as separate bindable geometry per floor.**
  Activity 2020 simultaneously Installs `Jump Form Unlocked @N`, Removes
  `Jump Form Locked @N` and Maintains `WALL CORE @N` — one element cannot
  satisfy two equality-bound search sets at once. Two overlapping sets per
  floor, shown/hidden per task, is also the industry pattern every 4D tool
  supports (Navisworks "Temporary" task type; Synchro appearance profiles —
  theirs is "3 Synchro Appearance Profiles (Install / Remove / Maintain)").
- **Names must not vary by floor.** Their contract: "one name `Jump Form`
  plus a property `STATE = Locked | Unlocked`, or two fixed names; either
  is fine, as long as it does not vary by floor." Floor lives in
  `QTO Properties.FLOOR = L<nn>`.
- **Reshoring `FLOOR` names the floor the shore SUPPORTS** (the slab whose
  soffit its head bears against), not the floor it stands on. This matches
  the existing props' semantics already (a prop's FLOOR is the slab it
  shores), and ACI 347.2R vocabulary.
- Known consumer edge (theirs, not ours): N+1 on L16 and N−2 on L02 fall
  off their floor table. Emitting geometry for floors outside L02–L16 is
  harmless — unbound elements simply never animate.

## 2. Supply — the measured core (`Tested2.ifc`, 2026-08-23)

- Core walls are unambiguous: nameAbb **`WALL CORE`**, 37 elements, the
  only wall name containing "core". Layer keyword filter: first `_`-segment
  `wall` + include `core` (same mechanism as `slab_layer_keyword`).
- **Two independent banks**, one joined solid per bank per storey:
  - Bank A: 27.5 × 24.4 ft plan, ~97.8 ft of wall run per lift, P1 → R2
    (19 lifts; R2 is a reduced east segment).
  - Bank B: 11.0 × 21.8 ft plan, ~59.7 ft per lift, P1 → R1 (18 lifts).
  - Banks are ~24.7 m apart in plan — two separate climbing units.
- Lift heights: typical **2946.4 mm (9'-8")** for L05–L15; extremes
  5334 mm (P1, 17.5 ft), 4622.8 (L01), 3352.8 (L03/L16), 4572 (R1). The
  engine must take lift height from each wall solid's own z-extent, never
  a constant. Walls stack flush (z_max = next z_min ± 3 mm).
- Identity: `QTO_STABLE_ID` present on 127/127 IfcWall; `FLOOR` string on
  every wall. Walls carry no HEIGHT property — height comes from geometry.

## 3. Geometry — CORRECTED 2026-08-24 evening to the Waverly reference

> The first build followed the web-research picture (panel bands + lap
> + three working decks). The user then pointed at the Waverly
> reference model (`..\_Waverly WIP\Waverly SU 0423\Waverly SU 0423
> Test11_Tagged by Ruby.skp`); its jump-form groups were measured
> (read-only SketchUp probe, lvl-24/25/26) and the decks and the lap
> turned out to be INVENTED — the reference models vertical panels
> only. This section is the as-built, Waverly-verified geometry; the
> deck/lap description below it is superseded.

Per **bank × lift(floor) × state**, vertical prisms only:

- **Form strips (LOCKED)**: horizontal section of the wall solid (cut
  high in the lift, above door heads) → each closed loop split at its
  corners into straight FACE runs → one strip per face hugging it on
  the away side (`panel_thickness` 0.3 m — Waverly: 1 ft). z from the
  lift base (**no lap**) to `form_top_drop` 0.35 m below the lift top
  (Waverly: 1.15 ft under a 9'-8" lift).
- **Form strips (UNLOCKED)**: the same strips retreated `roll_back`
  1.2 m along each face's own normal (Waverly: ~4 ft) — exterior faces
  outward, shaft faces INTO the shaft; z identical to LOCKED. The
  climb is animated purely by floor-set visibility.
- **No horizontal geometry.** The Waverly jump-form groups contain
  zero decks; `Flyable Deck` is the slab table, a separate component.
- Away-side resolution: membership probes at probe scale AT the
  section-cut height (mid-lift probes land in doorway voids).
- **Reshores (`Pole Shore for Reshoring`)**: reuse the existing prop
  machinery (grid lattice + ray-cast feet) under each cycle floor's slabs,
  no platform, own spacing param (`reshore_spacing`, as built 4.5 m —
  sparser than the 3.0 m shoring props; reshores carry redistribution,
  not the fresh pour).
  `FLOOR` = the slab's floor (already the supported-floor semantics).

## 4. Engine architecture (all precedented)

- New `Formwork_Generation/rhino/jumpform_gen_rhino.py`, importing
  `formwork_gen_rhino as fw` (the `sideform_gen_rhino` pattern). Metre
  PARAMS + `mu` conversion; modes `generate | export | purge`; driver
  `run_jumpform_on_model.py` sets `mode="export"`, writes
  `jumpform_error.txt` on exception, clears `doc.Modified`,
  `RhinoApp.Exit()` under `FW_HEADLESS` (all mandatory — watchdog).
- Reused verbatim: `fw.Log`, unit helpers, `read_floor_elevations` /
  `floor_name`, `ensure_layer` + `FW_GENERATED`, `_attributes`,
  `purge_formwork(types=...)`, the document adapter loop (keyword on first
  `_`-segment, excludes, in-memory block explode, hidden skip),
  **`_ident` verbatim** (stamp preference + first-claim-wins guard), the
  ray-cast machinery for reshore feet, `write_to_doc` / `export_3dm` /
  `dump_json` sinks.
- New FW_TYPE values: `jumpform`, `reshore` (purge whitelists compose —
  three engines already share the `_FORMWORK` tree this way). Layers:
  `_FORMWORK::<floor>::JumpForm_Locked | JumpForm_Unlocked | Reshores`.
- New params (first build, superseded by the Waverly correction — the
  as-built values live in `jumpform_gen_rhino.py` PARAMS; see §3/§8):
  `wall_layer_keyword="wall"`, `wall_layer_include=["core"]`,
  `panel_thickness=0.30` (was 0.1), `roll_back=1.20` (was 0.7),
  `form_top_drop=0.35`, `reshore_spacing=4.5` (was 3.0); the deck/lap
  params (`lap`, `platform_width`, `platform_thickness`,
  `trailing_platforms`) were removed with the decks.
- **Identity**: every jump-form element carries `WALL_GLOBALID` — the same
  `guid.compress` of the bank wall's `_ident` (stamp-preferred) — one id
  per bank per lift, resolving to the take-off IFC's wall `IfcGlobalId`.
  Reshores carry `SLAB_GLOBALID` (existing pattern). Never copy a
  `QTO_STABLE_ID` onto generated geometry.
- Staging names (no collisions with existing fixed names):
  `jumpform_out.3dm`, `jumpform_out.json`, `jumpform_model_log.txt`,
  `jumpform_model_error.txt`.

## 5. IFC contract

- Writer: extend `formwork_ifc_from_json.py` with `--jumpforms
  jumpform_out.json` (sections `jumpforms` + `reshores`). Touch points:
  the `floor_z` pre-pass (storeys must exist before indexing), a new emit
  loop (`_extruded` → `proxy` → `pset`), assemblies, counters, CLI guard.
- Elements: `IfcBuildingElementProxy`.
  - Name **`Jump Form Locked` / `Jump Form Unlocked`** (two fixed names —
    matches the schedule's component vocabulary byte-for-byte, and their
    glossary; decision 3), ObjectType `jumpform`, pset `QTO Properties`:
    `FLOOR`, `STATE` (`LOCKED|UNLOCKED`), `BANK` (`A|B`), `WALL_GLOBALID`,
    `LIFT_Z0_M`/`LIFT_Z1_M`.
  - Name **`Pole Shore for Reshoring`** (their glossary warns the bare
    substring `Pole Shore` double-matches — always the full name),
    ObjectType `reshore`, pset: `FLOOR` (supported), `HEIGHT_M`, `STATUS`,
    `SLAB_GLOBALID`.
- Assemblies (descriptive, per the naming rule): one per bank × floor ×
  state — `Jump Form for L05 Core A (Locked)` — and `Reshoring for L05`;
  contained in the floor's storey; elements reach storeys only through
  assemblies (writer invariant).
- Known trait, unchanged: formwork-IFC element GlobalIds are random per
  run (`guid.new()`); Mast4D binds by property search sets, which
  re-resolve, so this does not break their links. Deterministic formwork
  GlobalIds stay out of scope (if ever added, a stable id may be spent
  only once per file — the wall's compress is already taken by the
  take-off IFC).

## 6. Plugin integration (clone of the sideforms pass)

csproj: two `EmbeddedResource` entries (`LogicalName` exactly
`QTO_Tool.Formwork.<filename>`); `FormworkMethods.Scripts` array +2; a
`GENERATE JUMP FORM` button in the Generate GroupBox wired like
`Generate_Clicked` (EnsureFresh → early `ChildRunning` refusal → staged
copy or gated derived model → `RunChildRhino(model,
"run_jumpform_on_model.py", …, "jumpform_model_error.txt")`), added to
`SetBusy` and the stamp gate in `RefreshStamp`. Results: fixed staging
outputs + log tail; never placed in the live document (REVERT invariant).

## 7. Testing

- `test_jumpform_headless.py` (synthetic metric scene, the established
  harness): a two-bank core (one L-shaped), 3 storeys with one non-typical
  lift height, a stamped + a stamp-less wall, a duplicate-stamp pair
  (expect first-claim-wins warning), slabs for reshores. Asserts: both
  states emitted per bank per floor, unlocked offset by `roll_back`,
  strips span lift base to `form_top_drop` below the lift top (no decks
  emitted), WALL_GLOBALID present and distinct per bank,
  purge round-trip additive, export leaves doc untouched.
- Golden Bellwether expectations: 37 core wall targets → per-floor
  assembly counts; reshore counts per floor; run AFTER restaging
  `pour_breaks_model.json` (launch fact).
- Field pass on the real model remains mandatory before merge — the
  2026-08-20 lesson (green synthetic ≠ working feature) stands.

## 8. Decisions (taken by the user, 2026-08-24)

1. **Scope**: jump form + reshoring together — all 270 slots in one build.
2. **Unlocked representation**: initially panels rolled back 0.7 m;
   **revised the same evening to the Waverly convention** (second user
   decision round): per-face straight strips retreated 1.2 m, no decks,
   no lap — see the corrected §3.
3. **Naming**: two fixed names `Jump Form Locked` / `Jump Form Unlocked`
   (+ `Pole Shore for Reshoring`), matching the schedule's component
   vocabulary byte-for-byte, with the `STATE` pset carried as well.
4. **Floor coverage**: everywhere the core/slabs exist, P1→R2 — unbound
   extras are harmless and honest.

## 9. Out of scope (this build)

Climbing rails/brackets/anchor detail; engineered falsework design (the
module-wide placeholder disclaimer applies); `Pole Shore for Shoring`
renaming of existing props (their own rule: where geometry already exists,
the schedule adopts the model's vocabulary); deterministic formwork
GlobalIds; `Jump Form … Body` variants (Waverly SKP artifacts).
