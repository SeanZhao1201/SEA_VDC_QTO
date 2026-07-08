# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows-only Rhino 7 plugin (`QTO_Tool`) for concrete quantity takeoff: it validates solid geometry in a Rhino model, computes per-element quantities (volumes, face areas, lengths) from Breps, groups elements by floor, and exports to Excel and IFC. The solution also contains `Turner_Seattle_VDC_Server`, an unrelated standalone WPF app (SDK-style, net472) that reads QTO Excel output into MySQL — it does not reference the plugin project.

The old compiled installer (`QTO_Tool_Setup/`, an Inno Setup exe with no source, Rhino 6-era) was removed from the repo in July 2026 and survives only in git history. Distribution is the GitHub Release zip produced by CI; a yak package is planned (issue #3).

## Build

- `QTO_Tool.csproj` is SDK-style WPF targeting net48 with `PackageReference`: build with `dotnet build QTO_Tool\QTO_Tool.csproj -c Release` on Windows (any .NET 8+ SDK; the `Microsoft.NETFramework.ReferenceAssemblies` package supplies the net48 targeting pack, so nothing else needs to be installed). Output goes to `QTO_Tool\bin\<Configuration>\net48\`. Windows-only: on a Mac you can edit code but not compile or run it (the Windows Desktop SDK rejects `UseWPF` builds on non-Windows with NETSDK1100).
- `<TargetExt>.rhp</TargetExt>` names the output assembly `QTO_Tool.rhp` (the Rhino plugin extension) directly — there is no post-build rename. Load it in Rhino via Options > Plug-ins, then run the `RunQTO` command.
- Excel COM interop types are embedded via the `EmbedExcelInteropTypes` target in the csproj (not via PackageReference metadata) because the WPF markup-compile temp project drops `EmbedInteropTypes` metadata from PackageReference items — don't "simplify" this away, and keep the explicit `Microsoft.CSharp` reference that the resulting dynamic dispatch needs.
- References RhinoCommon 7.28 (Rhino 7). It also loads in Rhino 8 on Windows (fallback if IFC export misbehaves there: `SetDotNetRuntime` > NETFramework), never on Rhino 8 for Mac. Full assessment and the stale-installer situation: `docs/rhino8-compat.md`. The Rhino 8 modernization plan is tracked in issue #3.
- There are no tests and no linting.

## Architecture

Everything flows through one WPF window driven by button clicks, with static globals as shared state.

**Entry point**: `RunQTO.cs` — the `RunQTO` Rhino command. Sets two statics used throughout the codebase: `RunQTO.doc` (the active `RhinoDoc`) and `RunQTO.volumeConversionFactor`, then opens `QTOUI` (WPF window owned by the Rhino main window). `QTOToolPlugIn.cs` is an empty `PlugIn` subclass.

**User workflow / pipeline** (handlers in `QTOUI.xaml.cs`, ~1200 lines):
1. *Set Floor* → `ElevationInput` window. Floor data lives in `ElevationInput.floorElevations`, a **public static** `Dictionary<double, string>` (elevation Z in model units → floor name), persisted as JSON in the Rhino document user strings under key `"FloorElevations"` (`Methods.SaveDictionaryToDocumentStrings` / `RetrieveDictionaryFromDocumentStrings`).
2. *Start Checkup* → `Methods.ConcreteModelSetup()`. **Destructive**: it deletes every object in the document and re-adds joined/merged solids, coloring bad geometry red. Then `UIMethods.GenerateLayerTemplate` builds a per-layer template picker (`Methods.AutomaticTemplateSelect` guesses the element type from the layer name's first `_`-segment; a layer name containing "continuous" forces Continuous Footing).
3. *Calculate* → for each Rhino object, constructs one template object per its layer's assigned type, passing `ElevationInput.floorElevations` into the constructor.
4. Exports: Excel (COM interop), IFC, plus *Blockify* (`Methods.Blockify` wraps every object into a one-object block instance).

**Template pattern** — the core domain model. Nine element types: Wall, Beam, Column, Footing, ContinuousFooting, Curb, Slab, Styrofoam, Stair. Each `XTemplate.cs` class computes all its quantities in the constructor by classifying Brep faces via their normals (up/down/side against an angle threshold from the UI slider) — e.g. `WallTemplate` derives gross/net volume, top/end/side areas, and length. Each template stores `.floor` (a string) via `Methods.FindFloor`, which nearest-neighbor matches the element's bottom-face elevation against `floorElevations`; `"-"` when no floors are defined. Templates are bucketed into `AllX` containers (all trivial subclasses of `AllTemplates`), whose `allTemplates` is a `Dictionary<string, List<object>>` **keyed by floor name**. Values are `object` and every consumer type-switches with `GetType() == typeof(...)` — extending an element type means touching the template class, `QTOUI.xaml.cs`, `UIMethods.cs`, `ExcelMethods.cs`, and `IFCMethods.cs`.

**IFC export** (`IFCMethods.cs`, xBIM 5.1): builds an in-memory **IFC4-only** model with the spatial hierarchy `IfcProject` → `IfcSite` → `IfcBuilding` → one `IfcBuildingStorey` per floor (from `ElevationInput.floorElevations`, elevations in millimetres) plus an "Unassigned" fallback storey for floor buckets without an elevation entry. The project/site/building chain is required by the IFC spatial-structure rules (IfcSite is technically optional but viewers expect it), so importers like SketchUp always show these three ancestor levels above the storeys. Naming (issue #2): `IfcProject` is the `.3dm` file name without path or extension ("QTO Project" for unsaved documents), site/building are "Site" / "Building". Quantities go into a `"QTO Properties"` pset, Rhino attribute user strings into `"QTO Attributes"`. Geometry is tessellated `IfcFaceBasedSurfaceModel` meshes in absolute world coordinates, converted with one `RhinoMath.UnitScale` factor shared with the storey elevations. Design details (hierarchy rules, duplicate/stale floor names, placement strategy, xBIM API notes): `docs/ifc-export.md`.

**Excel export** (`ExcelMethods.cs`): desktop Excel COM automation. Writes the embedded workbook template to `QTO_Template.xlsx` in the user temp folder (`Path.GetTempPath()`), fills a summary sheet and a per-element sheet, saves via dialog.

**UI plumbing**: `UIMethods.cs` (~1400 lines) builds all result tables as WPF grids in code. Table row toggle buttons sync selection with the Rhino viewport through the static `RhinoDoc.SelectObjects`/`DeselectObjects` events (subscribed in `StartCheckup_Clicked`, never unsubscribed — reopening the window stacks handlers).

**Dormant code**: save/load of calculated data and the "Exterior" checkup branch are commented out or empty. The old in-plugin MySQL export (`MySqlMethods.cs`, `Send_To_MySql`) was removed in issue #3 Phase 1 — MySQL ingestion lives in `Turner_Seattle_VDC_Server`; if it is ever revived in the plugin, use MySqlConnector rather than MySql.Data.

## Conventions and gotchas

- Layer names are `_`-separated; `nameAbb` shown everywhere is the first two segments. Quantities are rounded to 2 decimals at computation time, inside template constructors.
- Volume units: hardcoded conversion to cubic yards for ft/in models (`Methods.SetVolumeConversionFactor`); other model units pass through unconverted.
- Comparing floats: `FindFloor` has no tolerance/tie-breaking; duplicate floor elevations silently collapse in the dictionary (elevation is the key), and duplicate floor *names* are allowed.
- `RunQTO.doc` can go stale if the user switches documents; some paths re-fetch `RhinoDoc.ActiveDoc`, others don't.
