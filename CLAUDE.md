# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development status — 2026-08-23 (keep this section current when work lands)

**GlobalId cross-export verification EXECUTED (2026-08-23, the old open
item 4) — encoding PASSES, linkage FAILS, cause isolated.** The derived
`model_pourbreaks.3dm` was run through the real UI flow (RunQTO → checkup
811/811 solids clean → Calculate 6.2 s, 0 bad → IFC export,
`C:\Users\seanz\Desktop\Tested.ifc`, 19 storeys) and compared three-way
against `Sunbreak_formwork.ifc` and the checkup log's 811 `old id -> new id`
lines:

- **In-vivo encoding: 811/811.** Every exported `IfcGlobalId` equals
  `ifcopenshell.guid.compress` of the element's post-checkup Rhino id —
  xBIM `ConvertToBase64` and ifcopenshell agree over 811 real elements,
  zero duplicates, zero unparseable ids. The four-vector claim is now
  field-scale fact. `POUR` mirror (36 slabs) and `QTO Units`
  (all 82 slabs, VOLUME=CY/AREA=ft2/LENGTH=ft) confirmed in the same file.
- **Direct SLAB_GLOBALID linkage: 0/67 — then FIXED the same night.** Not
  an encoding problem: **the checkup re-mints every object's Rhino id**
  (`PrepareObject` Adds a copy while the original still lives — id
  collision forces a fresh Guid — then deletes the original;
  `Methods.cs:242,295,709`; the log's own `object <old> -> solid <new>`
  lines are the proof, and ids re-mint on EVERY checkup run). Formwork
  computes `SLAB_GLOBALID` from the .3dm file's ids; the take-off export
  goes through a fresh checkup first (Calculate is hard-gated on it).

**The fix (2026-08-23, user chose the stable-id route): `QTO_STABLE_ID`, a
checkup-surviving per-object user string, is now the identity everything
derives the 22-char id from.** Attributes ride onto the checkup's re-added
copies, so the stamp survives what obj.Id cannot. Four coordinated pieces:

- `Methods.EnsureStableId` — the checkup stamps each valid take-off object
  with its own (pre-checkup) id unless a usable stamp exists; first checkup
  of a file therefore stamps the FILE ids — exactly what an
  already-generated formwork IFC referenced — and re-checkups preserve
  them. A post-pass (`RestampDuplicateStableIds`) re-stamps duplicate
  values (join/block fan-out share one attributes instance; user
  copy-paste clones user strings), loudly.
- `IFCMethods.SetDeterministicGlobalId` now prefers the stamp from
  `template.AttributeUserStrings` over `template.id`, with a per-export
  `HashSet` guard: a colliding stable id falls back to the object id,
  never a duplicate GlobalId (reset in `CreateandInitIFCModel`).
- `split_pourbreaks.py` clears the parent's stamp off each piece pre-add
  (siblings would collide) and stamps every landed piece with its own new
  id post-add; `marker_claim` and the rollback re-add stay untouched.
- `formwork_gen_rhino._ident` prefers a valid-GUID stamp over `obj.Id`,
  so the live-doc flow (stamps S0, file ids S1) stays in agreement too.

Verified end-to-end the same night, same harness as the failing run:
checkup **811/811 newly stamped**, GlobalIds derive from FILE ids 811/811
(re-minted ids: 0), **direct SLAB_GLOBALID match 67/67** (was 0/67) with
FLOOR/POUR agreeing 67/67, zero duplicate GlobalIds — against the formwork
IFC generated BEFORE the fix, i.e. existing artifacts did not need
regeneration. Headless suites: pourbreaks synthetic (+2 new stable-id
asserts: every piece stamped with its own id, stamps unique), formwork
synthetic, and the golden Bellwether regression — **ALL PASS**; build 0/0.
The fingerprint gates are unaffected (bbox-only hash). Note: stamps are
written inside the checkup undo record, so REVERT also reverts them; and
`QTO_STABLE_ID` now appears in each element's `QTO Attributes` pset — a
deliberate trace of the raw GUID behind the GlobalId.

Hardened by an adversarial review (3 confirmed defects, all folded in;
3 refuted — among them "locked objects refuse attribute changes", disproved
empirically in real Rhino 8): the duplicate-stamp keeper now prefers a
CLOSED SOLID before document order (a red bad fragment sharing its source's
attributes could otherwise keep the 4D-linked stamp while the verified
solid churned); a stamp-only checkup keeps its undo serial (stamp commits
are real mutations — zeroing the serial made the user's next Ctrl+Z
silently pop them); and `formwork_gen_rhino._ident` got the same
first-claim-wins duplicate guard the exporter has, so a copy-pasted stamp
degrades to a loud, detectably-missing link instead of silently binding
two slabs' formwork to one element (both logs point at the remedy: re-run
Start Checkup). All suites re-run green after the hardening. Residual,
documented: between checkups, a duplicated stamp resolves first-claim-wins;
the next checkup repairs it deterministically. The UI acceptance numbers
above were measured on the pre-hardening build; every hardening change is
a no-op on that path (unique stamps, no fan-out).

### Earlier status (2026-08-20)

**PR #19 (break sheet P3) MERGED** (`0f1d180`, 2026-08-20). The manual
Rhino pass it was waiting on was then run on the real Bellwether/Sunbreak
model, end to end, and it found five real defects plus one scope gap. All
are fixed; everything below is measured on that model, not estimated.

**Field pass on `..._With Floor info.3dm` (2026-08-20) — three defects the
synthetic suites could not see.** The synthetic scenes are too regular:

- *`breaksheet_gen` flipped face normals twice.* `BrepFace.NormalAt`
  already returns the OUTWARD normal, so the extra
  `if OrientationIsReversed: normal = -normal` deleted every top face
  whose flag happened to be set — **46 of 49 PT slabs**, silently (the
  `if faces:` branch has no else). The sheet came out with 3 cells for 19
  floors under a clean log. `formwork_gen_rhino._downward_faces` already
  carried the comment warning about exactly this; the break-sheet code
  reintroduced it.
- *The import ADVISORY could never match a pour marker.*
  `BrepLoop.To3dCurve()` on a face produced by `Brep.Split` comes back
  with a micro-gap (`IsClosed` false) and `Curve.Contains` answers
  `Unset`, not `Outside`, for an open curve — so exactly the SEVERED
  regions were markerless while uncut ones answered correctly. Judged
  with `BrepFace.IsPointOnFace` now (outer boundary and holes in one
  call), loop test kept as a gap-closing fallback.
- *`OPEN SHEET` took down both Rhino processes.* `Process.Start(<path>)`
  runs ShellExecuteEx on — and pumps messages into — Rhino's UI thread.
  Harmless on an empty Rhino (verified: two instances, no crash), fatal
  with a 19 MB model and two WPF windows loaded. Now launches the parent's
  own `Rhino.exe` with `UseShellExecute = false`, which also pins the child
  to the same Rhino version.

**Slabs on grade are pour-break targets (2026-08-20, user decision).** The
single shared `["sog", "topping"]` exclude was doing two different jobs.
Split in two, with a "do not re-merge" note at all four sites:
`split_pourbreaks` / `breaksheet_gen` exclude only `topping` (a SOG has no
soffit but is a real pour with real construction joints); `formwork_gen_rhino`
/ `sideform_gen_rhino` keep both (nothing shores a slab on grade). Topping
stays out on measurement: 6 of 8 sit 100% inside a PT slab's plan footprint,
so drawing them would double-count. Effect: 49 → 56 slabs, 18 → 19 floors
with cells, and P1 got a cell for the first time. The golden regression's
baseline predates this and now excludes SOG from its per-floor counts
explicitly (203 asserts, ALL PASS).

**Formwork is named after what it forms, and split per pour (2026-08-20).**
`@37.26m` told a scheduler nothing. The slab identity (layer, object name,
`POUR`, `POUR_FLOOR`, Rhino id) is now carried from collection all the way
to the level, soffit clustering keys on POUR before Z, and levels carry a
precomputed `name` with a per-storey uniqueness pass. Assemblies read
`Formwork for L03 Pour 1` / `Formwork for L01 Slab 1`; the elevation lives
on in `SOFFIT_Z_M`. 45 → 65 levels, 47 → 66 platforms, **prop count
unchanged** (1760 OK / 16 TALL) — only the platform was cut.

**4D binding contract, from downstream review (Mast4D, 2026-08-20).** Their
tool binds geometry to schedule tasks by property EQUALITY only — no regex,
no starts-with, and "blank" is not a value. Accepted and implemented:

- ELEMENT names are generic (`Side Form`, `Bulkhead`); floor and pour live
  in the pset. Descriptive naming belongs on the ASSEMBLY and stays there.
- An uncut slab that CONTAINS a pour dot claims that pour — the closure
  strip is declared by the modeller on the sheet, not hardcoded in an
  exporter (`split_pourbreaks.marker_claim`, attributes only).
- The deck's `POUR` is mirrored into `QTO Properties`, the same path the
  formwork uses.
- A `QTO Units` pset states what the numbers actually are (`VOLUME=CY`,
  `AREA=ft2`, `LENGTH=ft` on a ft/in model). Property NAMES were left alone
  deliberately: a `_CY` suffix would be wrong on a metric project. Note the
  file was never schema-invalid — `IfcReal` carries no unit claim.
- **Every element's `IfcGlobalId` is now derived from its Rhino object id**
  (`IFCMethods.SetDeterministicGlobalId`), so re-exports stop re-identifying
  everything and breaking 4D task links. xBIM's `ConvertToBase64` and
  ifcopenshell's `guid.compress` were verified byte-identical over four
  vectors, so the formwork exporter addresses the same element with no
  shared code: soffit platforms, props and assemblies carry
  `SLAB_GLOBALID`. One-time cost, accepted: the first export re-identifies
  everything once.
- REJECTED as not ours: their duplicated storey tree and seven "container
  node" proxies are artifacts of federating two files through SketchUp and
  re-exporting as IFC2X3. Our native output is IFC4. They were asked to
  re-measure against it.

**Open, in priority order:**

1. **Core jump-form + reshoring geometry does not exist** — 270 of their
   animation slots bind to nothing (`Jump Form Locked` 120, `Jump Form
   Unlocked` 60, `Pole Shore for Reshoring` 90). Minimum contract if built:
   a generic element name plus `STATE`, `QTO Properties.FLOOR` on every
   element, and for reshoring `FLOOR` names **the floor the shore supports**,
   not the one it stands on. Priority is Turner's call.
2. **Port `formwork_ifc_from_json.py` to C#/xBIM inside the plugin** so the
   IFC export becomes a button and the CPython venv dependency goes away.
   A verified 11-step plan exists (built and diffed as a POC against the
   reference output: zero signature differences across 2098 proxies and 83
   assemblies; extrusion-with-voids confirmed achievable on xBIM 6.1.605).
   Five decisions were left open — project name, sides pset, deterministic
   ids (now settled by the change above), Python drift, and the two
   representation contexts.
3. Side-form and bulkhead panels do not carry `SLAB_GLOBALID` —
   `sideform_gen_rhino` panels have no slab id yet.
4. ~~The take-off IFC has NOT been re-exported since the GlobalId change; the
   two exports' ids should be verified against each other once.~~ DONE
   2026-08-23 — encoding 811/811, direct linkage 0/67, cause = checkup id
   re-mint; see the top of this file.

### Earlier status (2026-08-19)


**Break-sheet P3 COMPLETE on branch `feat/breaksheet-p3`** (2026-08-19,
**PR #19**, open; pre-merge manual checklist in the PR body — overlay
pass, File>Open kill-switch, SAVE/LOAD round trip, plus the carried-over
#18 items): read-only overlay (`PourBreakOverlay`, a
DisplayConduit singleton — active breaks JSON drawn into the live
viewports, zero objects added, CloseDocument hook + whole-parse guard +
docPath refusal with a LOAD-OPTION-confirmed override), named break
schemes (SAVE/LOAD OPTION, snapshots stored NEXT TO the model file,
strict-docPath active-option note driving the "Active scheme" label
without dirtying the doc), and the `[OPTION: x]`/"(modified)" stamp on
the sheet title + meta. Hardened by a 13-agent adversarial review — 9
confirmed defects, all folded in (one superseded: strict guards made
the empty-docPath leak inert). Headless suite 53 → 60 asserts, **ALL
PASS in real Rhino 8**; builds 0/0. Also fixed in passing: IronPython
file-lock hygiene (`with` on every break-sheet JSON read — unclosed
.NET streams block deletes). Contract: `Formwork_Generation/CLAUDE.md`,
"Break sheet P3". Still user-only: the manual overlay/options pass
(toggle on real model, File>Open kill-switch, SAVE/LOAD round trip).

**PR #18 MERGED to master** (`4fcd3a7`, 2026-08-19) - one big PR
carrying audit batch-3 AND break-sheet P2 (same pattern as #17); the
merged tree is byte-identical to the branch state all five headless
suites passed on, so no post-merge re-run was needed. Still user-only
(the PR body's checklist): the manual P2 flow pass (MAKE → merge dialog
→ draw → badge → Split auto-import), a gross-volume spot check on a
slab with an opening (batch-3 changed the probe semantics), and the
30-second floor-staleness retest.

**Audit batch-3** (2026-08-19, landed in PR #18). The 16 unverified
2026-08-15 audit findings were adversarially verified (10-agent
workflow): **10 confirmed and fixed, 6 refuted** (VolumeMassProperties
guards — per-object catch already contains it; SET FLOOR red — fixed in
batch 1.5; Excel zombie window, IfcStore dispose, =SUM degenerate,
sideform panel_thickness — impact chains broken in current code). Fixed:
gross-volume probe (both directions, wall-depth-bounded rays, null
guards on CreatePlanarBreps/RemoveHoles — degrade to net, never drop the
element), Blockify re-run name collisions (bump past existing
definitions), StairTemplate `DegradeToSideOnly` at 4 failure points,
layers added after Start Checkup now tallied + dialog, measured-zero
exports as 0 not "-", `_FORMWORK`/`_POURBREAK` matchers split into
strict-tree (`LayerInTree`, fingerprint/scan/harvest-parity sites,
Python `::` tightened to match) vs gate-union (segment OR prefix — the
destructive-checkup gate must stay at least as broad as every earlier
build), Restore judged by a fresh `pourbreak_restore_result.json`
(skipped floors loud), splitter pass-3 AddBrep Guid checks with full
rollback + honest report. Its own 14-agent diff review confirmed 8
follow-up defects, all folded in. Verified in real Rhino 8: formwork,
sideforms, pourbreaks, golden Bellwether, breaksheet — **ALL PASS**;
builds 0/0.

**Break-sheet P2 COMPLETE** (2026-08-19, in PR #18 via the stacked
branch): near-TYP merge UI (`SheetMergeUI` dialog after MAKE →
`breaksheet_merge.json` directives, union-fingerprint validation in file
order — one metric with the suggestions), dirty badge + auto-import-on-
Split (`FormworkMethods.SheetDirty`, docPath-guarded, only a sheet-kind
JSON clears it; Split offers Yes/No/Cancel), and the import-summary
ADVISORY (per-pour areas + nearest-grid offsets) with the P1 preservation
fix (`target_area`/`grid_x`/`grid_y` survive reimports, foreign-JSON
laundering guard, ink-less covered cells keep their target). Hardened by
a 19-agent adversarial review — 14 confirmed defects, all fixed; headless
test grew 30 → 53 asserts, **ALL PASS in real Rhino 8**; C# builds 0/0.
Contract details: `Formwork_Generation/CLAUDE.md`, "Break sheet P2".
Still user-only: the manual Rhino pass over the P2 flows (MAKE → merge
dialog → draw → badge → Split auto-import).

**PR #17 MERGED to master** (`3ddcfe8`, 2026-08-17). It landed two bodies
of work, both agent-reviewed and green-built:

1. **The 2026-08-15 audit remediation — all 17 confirmed bugs fixed** across
   three batches (stair face classification, inch-model area/length
   conversion, slab-beam deduction units, hidden-object exclusion, IFC
   per-element mesh guard, floor-staleness export gates, derived-model
   sidecar fencing, `split_pourbreaks` headless guard, driver coding lines,
   logging coverage, "model file" wording). Field-verified 2026-08-17 on a
   793-solid model. Two documentation issues: #15 (Excel text-sum),
   #16 (slab-beam feet assumption). 16 lower-priority *unverified* audit
   findings remain as a batch-3 candidate list (see the session memory's
   audit note, or re-derive from the audit titles in issue history).
2. **Break-sheet pour-break authoring P1** (`breaksheet_gen.py` /
   `breaksheet_import.py` / FormworkUI MAKE-OPEN-IMPORT): draw pour breaks
   on a generated plan sheet with TYP typical-floor cells instead of the
   `_POURBREAK` layer ceremony. Contract: `Formwork_Generation/CLAUDE.md`,
   "Break sheet". 30-assert headless test ALL PASS in real Rhino 8.

**Post-merge verification, 2026-08-17:** `test_pourbreaks_model.py`
(golden) and `test_sideforms_headless.py` both re-run in real Rhino 8 —
**ALL PASS**. Still pending, user-only: the manual Rhino pass over the
formwork/break-sheet flows (MAKE → draw → IMPORT → SPLIT on the real
model) and the 30-second floor-staleness retest (edit a floor after
Calculate, confirm exports disable).

**Agreed next phases:** break-sheet P2, audit batch-3, break-sheet P3 —
all DONE (the 2026-08-15 audit is fully dispositioned: 17+10 fixed, 1+6
refuted). No further break-sheet phase is agreed; open backlog = issue
#3 (Rhino 8 modernization, `net48;net8.0-windows` + yak), issues
#15/#16 (documentation). Grid-line furniture is deferred: the
user's grids come from imported PDF/DWG files with arbitrary layer names;
the osnap-able slab outlines are the drawing reference for now (the
import ADVISORY reads the optional `grid_x`/`grid_y` from the breaks
JSON when present).

Build with the machine-local SDK when no system one exists:
`%LOCALAPPDATA%\Microsoft\dotnet\dotnet.exe build QTO_Tool\QTO_Tool.csproj -c Release`.
Headless test loop: stage `Formwork_Generation/rhino/*.py` into
`%LOCALAPPDATA%\qto_fw_test`, then
`Rhino.exe /nosplash /notemplate /runscript="-_RunPythonScript <staged test>"`
with `FW_HEADLESS=1`; reports land next to the staged scripts. Two
hard-won launch facts: the quote must open **after** `=`
(`/runscript="-_Run…"`) — a fully-quoted argument token (what bash/MSYS
produces) makes Rhino open and idle forever, so launch via PowerShell
`Start-Process -ArgumentList` with the exact string above; and the golden
test consumes the staged `pour_breaks_model.json`, which
`test_breaksheet_headless.py` overwrites with its synthetic scene — restage
`Formwork_Generation/out/pour_breaks_model.json` before a golden run.

## What this is

A Windows-only Rhino 7 plugin (`QTO_Tool`) for concrete quantity takeoff: it validates solid geometry in a Rhino model, computes per-element quantities (volumes, face areas, lengths) from Breps, groups elements by floor, and exports to Excel and IFC. The solution also contains `Turner_Seattle_VDC_Server`, an unrelated standalone WPF app (SDK-style, net472) that reads QTO Excel output into MySQL — it does not reference the plugin project.

The repo also holds **`Formwork_Generation/`**, a Python module that generates soffit formwork, shoring and pour-break splits. It is **decoupled by design** — see "Formwork_Generation" below. It has its own `CLAUDE.md`; read that one before working in that subtree.

The old compiled installer (`QTO_Tool_Setup/`, an Inno Setup exe with no source, Rhino 6-era) was removed from the repo in July 2026 and survives only in git history. Distribution is the GitHub Release zip produced by CI; a yak package is planned (issue #3).

## Build

- `QTO_Tool.csproj` is SDK-style WPF targeting net48 with `PackageReference`: build with `dotnet build QTO_Tool\QTO_Tool.csproj -c Release` on Windows (any .NET 8+ SDK; the `Microsoft.NETFramework.ReferenceAssemblies` package supplies the net48 targeting pack, so nothing else needs to be installed). Output goes to `QTO_Tool\bin\<Configuration>\net48\`. Windows-only: on a Mac you can edit code but not compile or run it (the Windows Desktop SDK rejects `UseWPF` builds on non-Windows with NETSDK1100).
- `<TargetExt>.rhp</TargetExt>` names the output assembly `QTO_Tool.rhp` (the Rhino plugin extension) directly — there is no post-build rename. Load it in Rhino via Options > Plug-ins, then run the `RunQTO` command.
- The Excel export uses **ClosedXML**, not Excel COM automation: desktop Excel is not required and is never launched. The old `EmbedExcelInteropTypes` csproj target, the `Microsoft.Office.Interop.Excel` PIA and the explicit `Microsoft.CSharp` reference it needed are all gone — do not reintroduce them.
- References RhinoCommon 7.28 (Rhino 7). It also loads in Rhino 8 on Windows (fallback if IFC export misbehaves there: `SetDotNetRuntime` > NETFramework), never on Rhino 8 for Mac. Full assessment and the stale-installer situation: `docs/rhino8-compat.md`. The Rhino 8 modernization plan is tracked in issue #3.
- **The planned multi-target is `net48;net8.0-windows`** — *not* net7.0-windows (out of support, and no longer what Rhino 8.20+ runs) and not net10.0 (McNeel recommends net8.0 even for Rhino 9, whose RhinoCommon NuGet ships only net48 + net8.0). Dropping Rhino 7 is deliberately *not* part of this: multi-targeting keeps it for the price of one extra TFM. Rationale and sources: `docs/rhino8-compat.md`.
- There are no tests and no linting.

## Architecture

Everything flows through one WPF window driven by button clicks, with static globals as shared state.

**Entry point**: `RunQTO.cs` — the `RunQTO` Rhino command. Sets two statics used throughout the codebase: `RunQTO.doc` (the active `RhinoDoc`) and `RunQTO.volumeConversionFactor`, starts the session log (`Logger.StartSession`), then opens `QTOUI` (WPF window owned by the Rhino main window). `QTOToolPlugIn.cs` is an empty `PlugIn` subclass.

**Logging** (`Logger.cs`): one log file per `RunQTO` session, written to a `Logs` subfolder next to the plugin assembly (falls back to `%AppData%\QTO_Tool\Logs` when that folder isn't writable); the path is printed on the Rhino command line. The checkup logs a per-object verdict (solid / joined / bad + reason / skipped-locked / error), so user bug reports should come with this file. Logging must never throw — all Logger entry points swallow their own exceptions.

**User workflow / pipeline** (handlers in `QTOUI.xaml.cs`, ~1200 lines):
1. *Set Floor* → `ElevationInput` window. Floor data lives in `ElevationInput.floorElevations`, a **public static** `Dictionary<double, string>` (elevation Z in model units → floor name), persisted as JSON in the Rhino document user strings under key `"FloorElevations"` (`Methods.SaveDictionaryToDocumentStrings` / `RetrieveDictionaryFromDocumentStrings`). A **Scan Model** button (2026-08-13) proposes the floor table from the model itself — solids' bottom elevations, gap-clustered at 1 m, cluster max = top of slab = floor elevation; the field logs' most expensive failure was this dialog left empty, with the whole take-off landing in the `"-"` bucket.
2. *Start Checkup* → `Methods.ConcreteModelSetup()`. **Destructive**: it deletes every object in the document and re-adds joined/merged solids, coloring bad geometry red. A **Preview Checkup** button (2026-08-13) runs the REAL checkup on a staged `WriteFile` copy in a second headless Rhino (`QTOCheckupReport`, a worker command that refuses to run without its env var — typed interactively it would shred the live doc) and reports the deletion list — curves (`_POURBREAK` flagged: harvest first), text dots, non-solids; block instances/meshes counted as take-off geometry, not deletions — without touching the open document; the preview can never drift from the checkup because it IS the checkup, one process boundary away. Objects whose original can't be deleted (locked objects, locked layers — the default `ObjectTable` enumerator includes them) are **skipped and left untouched**, with the freshly added copies rolled back; the skip count is reported in the checkup summary. **Hidden take-off geometry** (hidden object mode or hidden layers — the default enumerator excludes both) is never checked; since 2026-08-15 the checkup counts and reports it, and Calculate excludes it via `Methods.IsHiddenFromTakeoff` so unverified hidden solids cannot inflate the export. One failing object logs an error and is skipped rather than aborting the run. Then `UIMethods.GenerateLayerTemplate` builds a per-layer template picker (`Methods.AutomaticTemplateSelect` guesses the element type from the layer name's first `_`-segment; a layer name containing "continuous" forces Continuous Footing).
3. *Calculate* → for each Rhino object, constructs one template object per its layer's assigned type, passing `ElevationInput.floorElevations` into the constructor. The success path snapshots the floor table (`floorsAtCalculate`); editing floors afterwards disables both exports until the next Calculate — the template `.floor` strings froze at Calculate time, and an IFC export against a renamed floor would silently land on "Unassigned".
4. Exports: Excel (ClosedXML), IFC, plus *Blockify* (`Methods.Blockify` wraps every object into a one-object block instance). One unmeshable Brep no longer aborts the IFC export: the element is skipped, logged with its id, and reported in the completion dialog (`IFCMethods.SkippedMeshElements`).

**Template pattern** — the core domain model. Nine element types: Wall, Beam, Column, Footing, ContinuousFooting, Curb, Slab, Styrofoam, Stair. Each `XTemplate.cs` class computes all its quantities in the constructor by classifying Brep faces via their normals (up/down/side against an angle threshold from the UI slider) — e.g. `WallTemplate` derives gross/net volume, top/end/side areas, and length. Each template stores `.floor` (a string) via `Methods.FindFloor`, which nearest-neighbor matches the element's bottom-face elevation against `floorElevations`; `"-"` when no floors are defined. Templates are bucketed into `AllX` containers (all trivial subclasses of `AllTemplates`), whose `allTemplates` is a `Dictionary<string, List<object>>` **keyed by floor name**. Values are `object` and every consumer type-switches with `GetType() == typeof(...)` — extending an element type means touching the template class, `QTOUI.xaml.cs`, `UIMethods.cs`, `ExcelMethods.cs`, and `IFCMethods.cs`.

**IFC export** (`IFCMethods.cs`, xBIM 6.1): builds an in-memory **IFC4-only** model with the spatial hierarchy `IfcProject` → `IfcSite` → `IfcBuilding` → one `IfcBuildingStorey` per floor (from `ElevationInput.floorElevations`, elevations in millimetres) plus an "Unassigned" fallback storey for floor buckets without an elevation entry. The project/site/building chain is required by the IFC spatial-structure rules (IfcSite is technically optional but viewers expect it), so importers like SketchUp always show these three ancestor levels above the storeys. Naming (issue #2): `IfcProject` is the `.3dm` file name without path or extension ("QTO Project" for unsaved documents), site/building are "Site" / "Building". Quantities go into a `"QTO Properties"` pset, Rhino attribute user strings into `"QTO Attributes"`. Geometry is tessellated `IfcFaceBasedSurfaceModel` meshes in absolute world coordinates, converted with one `RhinoMath.UnitScale` factor shared with the storey elevations. Design details (hierarchy rules, duplicate/stale floor names, placement strategy, xBIM API notes): `docs/ifc-export.md`.

**Excel export**: split across two files by design. `ExcelMethods.cs` is the UI side — save dialog, progress window, and scraping the WPF result tables into a plain `ExportModel`. `ExcelWorkbookWriter.cs` fills the embedded template with ClosedXML and saves it; it has **no WPF and no RhinoCommon references**, so the workbook output can be exercised headlessly (keep it that way — it is the only testable seam in the export path). No temp file: the template is opened straight from `Resources.template` in memory.

Two behaviours the COM version got from live Excel and this one must do explicitly: (a) project headers are written **before** the table is resized, because ClosedXML takes table column names from the header cells at resize time; (b) the summary `SUMIF` formulas are written into **every** data row, since ClosedXML has no calculated-column auto-fill. The summary formulas use absolute `PROJECT!$X$2:$X$n` ranges rather than structured references (`PROJECT_TABLE[COUNT]`) — ClosedXML's formula parser rejects intra-table references while converting A1 to R1C1 on save, and those cells sit inside `SUMMARY_TABLE`.

**UI plumbing**: `UIMethods.cs` (~1400 lines) builds all result tables as WPF grids in code. Table row toggle buttons sync selection with the Rhino viewport through the static `RhinoDoc.SelectObjects`/`DeselectObjects` events (subscribed in `StartCheckup_Clicked`, never unsubscribed — reopening the window stacks handlers).

**Dormant code**: save/load of calculated data and the "Exterior" checkup branch are commented out or empty. The old in-plugin MySQL export (`MySqlMethods.cs`, `Send_To_MySql`) was removed in issue #3 Phase 1 — MySQL ingestion lives in `Turner_Seattle_VDC_Server`; if it is ever revived in the plugin, use MySqlConnector rather than MySql.Data.

## Formwork_Generation

Python module (~3,100 lines) that consumes QTO output and emits temporary works:
soffit platforms + shoring props (`rhino/formwork_gen_rhino.py`, runs inside Rhino),
pour-break slab splitting (`rhino/split_pourbreaks.py`), and IFC writers
(`formwork_ifc_from_json.py`, `patch_ifc_pourbreaks.py`, CPython + ifcopenshell).
Details, invariants and the IFC contract: `Formwork_Generation/CLAUDE.md`.

**Architecture decision (2026-07-30, still current): formwork stays DECOUPLED from
the plugin.** The dependency is one-way — formwork consumes verified solids and floor
assignments produced by a completed QTO pass; QTO never waits on formwork. Target
release v1.2, after v1.1 (preview checkup + wizard UX).

**GUI (decided 2026-08-12, BUILT 2026-08-13): a separate command and window, not a
new tab in `QTOUI`.** The `RunFormwork` command opens `FormworkUI`
(`RunFormwork.cs`, `FormworkUI.xaml(.cs)`, `FormworkMethods.cs`); `QTOUI.xaml`
gained exactly one launcher button. Harvest/restore of pour breaks run in-process
on the live document (harvest is read-only; restore confirms because adding
objects kills REVERT). Pour-break authoring's primary surface is the **BREAK
SHEET** (2026-08-17): MAKE/OPEN/IMPORT buttons generate and re-import a
plan-cell sheet file via in-process `File3dm` — no child Rhino, the live
model never gains an object; identical floors collapse into TYP cells with
explicit fan-out at import. Contract details:
`Formwork_Generation/CLAUDE.md`, "Break sheet". Split and Generate always run in a **second, headless
Rhino process on a `RhinoDoc.WriteFile` copy** of the model, launched with a
watchdog timeout (a hidden dialog — e.g. the second-seat licence check — would
otherwise hang invisibly), never in the Rhino process that owns the user's
document. The Python engines are embedded in the `.rhp` as LINKED resources from
`Formwork_Generation/rhino/` (one source of truth) and extracted at runtime into
the `%LOCALAPPDATA%\qto_fw_test` staging folder — the same contract the headless
dev loop uses (fixed filenames: one run at a time, enforced in-plugin by a
static gate; do not run the GUI and the dev loop concurrently). Three facts
make the process boundary load-bearing rather than stylistic:

- `rhino/formwork_gen_rhino.py` defaults to `"mode": "generate"` (the destructive
  branch, `doc.Objects.AddBrep`). Process isolation makes that fail-open default
  harmless — bad geometry lands in a throwaway staged `.3dm`, not the client model.
- Any `doc.Objects.Add` into the live document fires `OnDocObjectChanged_InvalidateRevert`
  (`QTOUI.xaml.cs`), which disables REVERT CHECKUP permanently — it is re-enabled at
  exactly one site, the checkup success path. Ctrl+Z does not bring it back. So there is
  deliberately **no "place formwork in this model" button**; results are opened in a
  second Rhino instead.
- `UIMethods.GenerateLayerTemplate` enumerates `doc.Layers` regardless of lock state, so
  a `_FORMWORK` layer in the document would grow template-picker rows and pollute the
  checkup counts. Start Checkup is therefore **hard-disabled** (button off + hard
  return, `StartCheckup_Clicked`) whenever `_FORMWORK` objects are detected.

The **freshness stamp** is implemented (`FormworkMethods.WriteStamp/CheckStamp`):
written at exactly one site — the Calculate success path — it fingerprints the
QTO solids (Breps/Extrusions, `_FORMWORK`/`_POURBREAK` layers excluded so
authoring breaks after Calculate stays green) plus the floor dictionary, and
gates FormworkUI's Split/Generate buttons red/amber/green. The **floor count is
validated separately** — a model where floors were never set fingerprints as a
perfect match while the whole take-off is bucketed under `"-"`, so a green light
there would be actively misleading. The stamp only vouches for the LIVE
document; the staged **pour-break derived model** (fixed machine-wide filename)
carries its own sidecar `model_pourbreaks.meta.json` (fingerprint + floors +
breaks-JSON SHA + doc path, captured at Split launch, deleted at Split launch
and re-issued only when the child verifiably rewrote the file) — Generate on
the derived model and the input radio both check it via
`FormworkMethods.DerivedModelMatches`, so a stale or foreign-project derived
model fails loudly instead of generating under a green light.

`_FORMWORK`-layer objects must never be present during a checkup: the checkup deletes
and re-adds every object in the document.

## Progress-report HTML — Turner design language

Development reports for Turner Construction (like
`Formwork_Generation/out/Turner_Progress_Report.html`, the reference
implementation) follow Turner's visual identity, extracted 2026-07 from the
production stylesheet of turnerconstruction.com. Reuse these tokens instead of
inventing a look — the drafting-sheet/blueprint aesthetic was explicitly
rejected as off-brand:

- **Colors**: ink `#17171b`, muted text `#73737b`, hairlines `#dcdcdc`, page
  ground `#f6f6f6`, white cards; **action blue `#0b5dd0`** (links, eyebrows,
  card accents), **deep navy `#012471`** (the logo blue — top bars, big
  numbers), **signature orange-red `#ff4026`** (their arrow/CTA color — use
  for flow arrows, warnings, pour-break/flag elements, sparingly).
- **Type**: Turner uses Apercu Pro with *light weights for large headings*.
  Approximate with `"Segoe UI", "Helvetica Neue", "Open Sans", Helvetica,
  Arial` (their own fallback stack); headings `font-weight: 300`, large sizes,
  with a single bold word for emphasis (their "Making a **Difference**"
  pattern). Body at normal weight; monospace only for file/property names.
- **Aesthetic**: minimal corporate — generous whitespace, thin hairlines, no
  heavy borders, figure-forward, arrow motifs. Slide decks: white sheet cards
  with a 6px navy top bar, a title-block strip with sheet numbers (PR-00…),
  keyboard navigation, light+dark themes, print-friendly (one sheet per page).
- **Language**: Turner-facing reports are written in English; claims carry the
  verified numbers from the run logs, never rounded marketing figures.

## Conventions and gotchas

- Layer names are `_`-separated; `nameAbb` shown everywhere is the first two segments. Quantities are rounded to 2 decimals at computation time, inside template constructors.
- Units: volumes convert to cubic yards for ft/in models (`Methods.SetVolumeConversionFactor`); areas and lengths convert to ft²/ft for inch models (`SetAreaConversionFactor`/`SetLengthConversionFactor`, applied inside the template constructors); other model units pass through unconverted in all three.
- Comparing floats: `FindFloor` has no tolerance/tie-breaking; duplicate floor elevations silently collapse in the dictionary (elevation is the key), and duplicate floor *names* are allowed.
- `RunQTO.doc` can go stale if the user switches documents; some paths re-fetch `RhinoDoc.ActiveDoc`, others don't.
