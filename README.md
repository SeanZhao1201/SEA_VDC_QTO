# QTO_Tool

A Rhino plugin for concrete quantity takeoff (QTO). It validates the solid geometry in a Rhino model, computes per-element quantities (volumes, face areas, lengths, counts) directly from Breps, groups elements by floor, and exports the results to Excel and IFC.

This repository (`SEA_VDC_QTO`, from the Seattle VDC quantity-takeoff collaboration with Turner Construction) contains two independent tools: **QTO_Tool**, the Rhino plugin this README describes, and **Turner_Seattle_VDC_Server**, a standalone WPF utility that loads the plugin's Excel output into a MySQL database. The two share no code — only the Excel file format connects them.

## Features

- **Model checkup** — scans every object in the document, joins and heals surfaces into solids, and highlights bad geometry in red before any quantities are computed.
- **Nine concrete element types** — Wall, Beam, Column, Footing, Continuous Footing, Curb, Slab, Styrofoam (void form), and Stair, each with its own quantity set (gross/net volume, top/bottom/side/end areas, opening area, length, tread count, ...).
- **Floor assignment** — floor names and elevations are entered once per project and persisted inside the `.3dm` document; every element is assigned to the nearest floor by its bottom-face elevation.
- **Excel export** — writes a formatted workbook (summary sheet plus per-element sheet) through desktop Excel.
- **IFC export** — writes an IFC4 file with a full spatial hierarchy (`IfcProject` → `IfcSite` → `IfcBuilding` → `IfcBuildingStorey`), one storey per defined floor with its real elevation, all quantities in a `QTO Properties` property set, and any Rhino attribute user texts in a `QTO Attributes` property set.
- **Blockify** — optionally wraps every object into a single-object block instance named after its layer path.

## Requirements

- Windows. The plugin UI is WPF and the Excel export uses COM automation, so it cannot run on Rhino for Mac.
- Rhino 7 (the plugin is compiled against RhinoCommon 7.x). It also loads in Rhino 8 for Windows; if the IFC export misbehaves under Rhino 8's default .NET Core runtime, run the `SetDotNetRuntime` command, choose `NETFramework`, and restart Rhino.
- Desktop Microsoft Excel, for the Excel export only.

## Installation

1. Download `QTO_Tool.zip` from the latest [GitHub Release](../../releases) (or build from source, see below).
2. Unzip the **entire archive** into a permanent folder of your choice (e.g. `Documents\QTO_Tool`). The zip contains `QTO_Tool.rhp` plus the `.dll` libraries it needs at runtime — the plugin loads its IFC and Excel libraries from its own folder, so all files must stay together and the `.rhp` must never be moved on its own.
3. In Rhino, open `Options` > `Plug-ins` > `Install...` and select `QTO_Tool.rhp` (or drag and drop the `.rhp` into the Rhino viewport).
4. Restart Rhino and run the `RunQTO` command. Rhino remembers the plugin's location — if you later move or delete the folder, repeat the install step from the new location.

## Usage

1. **Prepare layers.** Place each concrete element on a layer named with underscore-separated segments, e.g. `Wall_W1_Interior`. The first segment is used to auto-select the element type (a name containing `continuous` selects Continuous Footing); the first two segments become the element's display name.
2. **Run `RunQTO`.** The QTO window opens attached to Rhino.
3. **Set Floor (optional but recommended).** Enter floor names and their elevations in model units. They are saved into the document and reused on the next session.
4. **Start Checkup.** The model is examined and rebuilt into clean solids; bad geometry is colored red. Review the per-layer template table and correct any auto-selected element types.
5. **Calculate.** Quantities are computed for every element and shown in tables; selecting rows highlights the matching objects in the viewport.
6. **Export.** Use *Export Excel* or *Export IFC*. Both exports include the floor assignment.

> **Warning:** the checkup step deletes and re-adds objects while normalizing the model. Run it on a copy of your file, not your only original.

> **Locked layers:** unlock all objects and layers before running the checkup. Locked objects cannot be rebuilt; the checkup leaves them untouched, skips them, and reports how many were skipped.

## Log files

Every `RunQTO` session writes a log file (`QTO_<date>_<time>.log`) to a `Logs` subfolder next to `QTO_Tool.rhp` — inside the plugin folder you unzipped, so it travels with the tool. If that folder is not writable, the log goes to `%AppData%\QTO_Tool\Logs` instead; the actual path is printed on the Rhino command line when the command starts. The log records every checkup decision per object (solid, joined, bad geometry and why, skipped because locked, errors), plus the exports. When reporting a bug, please attach the log file of the session, and if possible the `.3dm` model.

## IFC export details

- Schema: IFC4, written with [xBIM Essentials](https://github.com/xBimTeam/XbimEssentials) 5.1.
- Spatial structure: `IfcProject` (named after the `.3dm` file) → `IfcSite` ("Site") → `IfcBuilding` ("Building") → one `IfcBuildingStorey` per floor defined in the elevation input, ordered by elevation. Elements that have no floor assignment are contained in a fallback storey named `Unassigned`.
- Storey `Elevation` values are converted from the Rhino model unit to millimeters, matching the exported geometry (the IFC length unit is the millimeter).
- Geometry: tessellated `IfcFaceBasedSurfaceModel` render meshes with layer color, in absolute world coordinates.
- Properties: every element carries a `QTO Properties` set (name, floor, and its computed quantities) and, when present, a `QTO Attributes` set copied from the Rhino object's attribute user text.

## Building from source

1. On Windows, with any .NET 8+ SDK installed, run `dotnet build QTO_Tool\QTO_Tool.csproj -c Release` (or open `QTO_Tool.sln` in Visual Studio — the plugin project is SDK-style, targets .NET Framework 4.8, and restores its NuGet packages automatically).
2. The build names the output assembly `QTO_Tool.rhp` directly, in `QTO_Tool\bin\Release\net48\`.
3. Load `QTO_Tool\bin\Release\net48\QTO_Tool.rhp` in Rhino as described under Installation.

The solution contains a second project, `Turner_Seattle_VDC_Server`, a standalone WPF utility for pushing QTO Excel output into a MySQL database. It is not required by, and not referenced from, the plugin.

## Creating a release

CI (GitHub Actions, `.github/workflows/build.yml`) compiles the plugin on a Windows runner for every push and pull request and uploads the plugin folder as a build artifact — so no local Windows machine is needed to produce binaries.

To publish a release:

1. Bump `AssemblyVersion`/`AssemblyFileVersion` in `QTO_Tool/Properties/AssemblyInfo.cs`.
2. Tag the commit and push the tag: `git tag vX.Y.Z && git push --tags`.
3. CI builds the tag and automatically creates a GitHub release with `QTO_Tool.zip` attached.

For a manual build instead: build the `QTO_Tool` project in `Release` configuration on Windows and zip the contents of `QTO_Tool\bin\Release\net48\` (excluding `.pdb` files).

## License

[MIT](LICENSE). Third-party libraries redistributed in the release zip (xBIM and its dependencies) remain under their own licenses.
