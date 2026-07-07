# Rhino 8 compatibility assessment

Status of running this Rhino 7 plugin on Rhino 8, based on a full audit of the code, dependencies, and installer (2026-07). Summary: **works on Rhino 8 for Windows as-is; impossible on Rhino 8 for Mac; the shipped installer is stale and version-locked to a Rhino 6 path.**

## Code

- No runtime version gating anywhere: `QTOToolPlugIn` overrides nothing, and there are no `RhinoApp.Version`/SDK checks. The plugin is a plain net48 assembly compiled against RhinoCommon 7.28.
- Every RhinoCommon API the plugin touches (PlugIn/Command, `RhinoDoc.ActiveDoc`, ObjectTable/LayerTable/InstanceDefinitions, `doc.Strings`, doc selection events, `Mesh.CreateFromBrep`, `Intersection.*`, `AreaMassProperties`, `RhinoMath.UnitScale`) is unchanged in Rhino 8.
- McNeel's compatibility position: Rhino 7 plugins load in Rhino 8 on Windows without recompiling unless they hit APIs removed from .NET Core ([Moving to .NET Core](https://developer.rhino3d.com/guides/rhinocommon/moving-to-dotnet-core/)).

## Runtime (the real risk area)

- Rhino 8 on Windows runs **.NET Core by default** (.NET 7 up to 8.19, .NET 8 from 8.20). Under Core, `app.config` binding redirects are ignored.
- Riskiest dependency stack: **xBIM 5.1 (net47) + Microsoft.Extensions 2.1.1** — the classic assembly-version-conflict class of failure (`FileLoadException`) under Core. Mitigating factors: the export only uses the in-memory model, so the Esent/Windows-only I/O path is never exercised (since the issue #3 Phase 1 conversion, `Xbim.IO.Esent` is no longer shipped at all and xBIM falls back to its memory model provider), and the dependency closure is now resolved by a single NuGet graph instead of hand-pinned versions plus binding redirects. If IFC export fails on Rhino 8, the supported workaround is the `SetDotNetRuntime` command → `NETFramework` → restart (or launch with `/netfx`).
- Excel export constraints are environmental, not Rhino-8-related: desktop Excel must be installed and `C:\Temp` must exist.
- The dormant in-plugin MySQL export (MySql.Data and its dependency chain) was removed outright in issue #3 Phase 1, along with `app.config` — whose binding redirects only served that dead chain and were ignored under Core anyway.
- WPF and Excel COM interop work normally on the Windows Desktop Core runtime.

## Rhino 8 for Mac

Hard no, in any configuration: Rhino 8 Mac is Core-only (no netfx fallback), and the plugin depends on WPF, WinForms dialogs, Excel COM, and hardcoded Windows paths — none of which exist on Mac. Mac support would mean a rewrite of the UI (Eto.Forms) and the Excel export.

## Installer (`QTO_Tool_Setup/`)

The checked-in `QTO_Tool_Setup.exe` (Inno Setup 6.1.0, no source in the repo) is stale on every axis:

- Copies files to a hard-coded `C:\Program Files\Rhino 6\Plug-ins\QTO_Tool` — a folder no Rhino version scans.
- Writes **zero** registry keys (no McNeel plugin registration for any Rhino version); its own post-install text tells the user to load the plugin manually via Options > Plug-ins.
- The bundled `QTO_Tool.rhp` inside it was compiled against RhinoCommon **6.34** (net452) — two generations older than the current source.

Practical installation is therefore always the manual one described in the README. Replace this installer with a [yak package](https://developer.rhino3d.com/guides/yak/) when distribution matters.

## Path to first-class Rhino 8 support

The full plan is tracked in [issue #3](https://github.com/SeanZhao1201/SEA_VDC_QTO/issues/3). Phase 1 is **done** (July 2026): the csproj is SDK-style with `PackageReference`, the dead MySQL chain and unused references (BouncyCastle, Google.Protobuf, K4os.*, Extended.Wpf.Toolkit, Xbim.IO.Esent, Xbim.Tessellator) are gone, and `app.config` with its binding redirects is deleted. Remaining, per McNeel's recommended route: multi-target `net48;net7.0-windows` with the RhinoCommon 8 NuGet (multi-targeted distribution is supported since Rhino 8.2), upgrade xBIM to 6.x, and replace the stale installer with a yak package (issue #3 Phases 2–4). Keep in mind the project can only be compiled on Windows.

Key sources: [Moving to .NET Core](https://developer.rhino3d.com/guides/rhinocommon/moving-to-dotnet-core/) · [.NET Core in Rhino 8](https://www.rhino3d.com/en/docs/guides/netcore/) · [Rhino 8: Get ready for .NET 7 (forum)](https://discourse.mcneel.com/t/rhino-8-feature-get-ready-for-net-7/148051)
