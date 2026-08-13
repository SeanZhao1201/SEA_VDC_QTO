# Rhino 8 compatibility assessment

Status of running this Rhino 7 plugin on Rhino 8, based on a full audit of the code, dependencies, and installer (2026-07). Summary: **works on Rhino 8 for Windows as-is; impossible on Rhino 8 for Mac.**

## Code

- No runtime version gating anywhere: `QTOToolPlugIn` overrides nothing, and there are no `RhinoApp.Version`/SDK checks. The plugin is a plain net48 assembly compiled against RhinoCommon 7.28.
- Every RhinoCommon API the plugin touches (PlugIn/Command, `RhinoDoc.ActiveDoc`, ObjectTable/LayerTable/InstanceDefinitions, `doc.Strings`, doc selection events, `Mesh.CreateFromBrep`, `Intersection.*`, `AreaMassProperties`, `RhinoMath.UnitScale`) is unchanged in Rhino 8.
- McNeel's compatibility position: Rhino 7 plugins load in Rhino 8 on Windows without recompiling unless they hit APIs removed from .NET Core ([Moving to .NET Core](https://developer.rhino3d.com/guides/rhinocommon/moving-to-dotnet-core/)).

## Runtime (the real risk area)

- Rhino 8 on Windows runs **.NET Core by default** (.NET 7 up to 8.19, .NET 8 from 8.20). Under Core, `app.config` binding redirects are ignored.
- Riskiest dependency stack **was** xBIM 5.1 (net47) + Microsoft.Extensions 2.1.1 — the classic assembly-version-conflict class of failure (`FileLoadException`) under Core, since the out-of-support 2.1.1 set is the version most likely to lose a first-load-wins race against another plugin. **Resolved by the xBIM 6.1.605 upgrade** (August 2026): the Microsoft.Extensions.\* closure now resolves at 8.0.x/10.0.x, and the Configuration/Logging-implementation packages disappear entirely. See "xBIM 6.1 upgrade" below.
- If IFC export fails on Rhino 8, the supported workaround is the `SetDotNetRuntime` command → `NETFramework` → restart (or launch with `/netfx`). Treat this as an escape hatch for Rhino 8 only, not a strategy — `/netfx` is deprecated in Rhino 9.
- Excel export no longer has environmental constraints: since the ClosedXML rewrite (August 2026) the workbook is written directly from the embedded template, with no desktop Excel and no temp file.
- The dormant in-plugin MySQL export (MySql.Data and its dependency chain) was removed outright in issue #3 Phase 1, along with `app.config` — whose binding redirects only served that dead chain and were ignored under Core anyway.
- WPF works normally on the Windows Desktop Core runtime. Excel COM is no longer used at all, which removes the largest .NET Core migration risk in the codebase: the old `ExcelMethods.cs` acquired 200+ runtime callable wrappers and never called `Marshal.ReleaseComObject`, and .NET Core has no AppDomain unload to reap them.

## Rhino 8 for Mac

Hard no, in any configuration: Rhino 8 Mac is Core-only (no netfx fallback), and the plugin depends on WPF, WinForms dialogs, and hardcoded Windows paths — none of which exist on Mac. Mac support would mean a rewrite of the UI (Eto.Forms). The Excel export is no longer a blocker there: ClosedXML is cross-platform.

## Installer

The formerly checked-in `QTO_Tool_Setup.exe` (Inno Setup, no source, stale on every axis — hard-coded `C:\Program Files\Rhino 6\Plug-ins\` target and a bundled RhinoCommon 6.34 build of the .rhp) was removed from the repo in July 2026 and survives only in git history. Installation is the manual route described in the README (GitHub Release zip); build a [yak package](https://developer.rhino3d.com/guides/yak/) when distribution matters.

## Path to first-class Rhino 8 support

The full plan is tracked in [issue #3](https://github.com/SeanZhao1201/SEA_VDC_QTO/issues/3). Phase 1 is **done** (July 2026): the csproj is SDK-style with `PackageReference`, the dead MySQL chain and unused references (BouncyCastle, Google.Protobuf, K4os.*, Extended.Wpf.Toolkit, Xbim.IO.Esent, Xbim.Tessellator) are gone, and `app.config` with its binding redirects is deleted. Remaining, per McNeel's recommended route: multi-target **`net48;net8.0-windows`** with the RhinoCommon 8 NuGet (multi-targeted distribution is supported since Rhino 8.2) and ship a yak package for distribution (issue #3 Phases 2–4). Keep in mind the project can only be compiled on Windows.

**The core TFM is `net8.0-windows`, not `net7.0-windows`.** Rhino 8 shipped on .NET 7.0.0, gained optional .NET 8 at 8.12, and **installs and defaults to .NET 8.0.14+ from 8.20**; .NET 7 is out of Microsoft support. Rhino 9 (public BETA since 2026-06-23, not yet released) defaults to .NET 10, but McNeel staff explicitly recommend targeting **net8.0** rather than net10.0, because the Rhino 9 RhinoCommon NuGet itself ships only `net48` and `net8.0` assets and rolls forward. One move to `net48;net8.0-windows` therefore covers Rhino 7, Rhino 8.20+ natively, and Rhino 9. Sources: [Moving to .NET Core](https://developer.rhino3d.com/guides/rhinocommon/moving-to-dotnet-core/) · [RhinoCommon NuGet target (Dale Fugier, 2026-07-17)](https://discourse.mcneel.com/t/rhinocommon-nuget-target/220948). Note the older [wiki page](https://wiki.mcneel.com/zoo/rhinonetcore) still says ".NET Core 7.0 (default)" and is stale — treat the developer guide as the single source of truth.

**Dropping Rhino 7 is not a prerequisite for any of this.** McNeel's own guide documents keeping Rhino 7 by multi-targeting net48 alongside modern TFMs, so the cost of Rhino 7 support is one extra TFM rather than a fork. .NET Framework is *deprecated but not removed* in Rhino 9 — netfx plugins still load — but McNeel explicitly discourages shipping new or updated netfx-only plugins, so `net48`-only is a slow-clock dead end from Rhino 9 onward. Rhino 7 itself has no EOL announcement (the 2026 EOL campaigns target Rhino 5 and earlier) but has not been sold since November 2023, so the Rhino 7 population can only shrink. Turner, the deployment this plugin is built for, runs Rhino 8 and tracks the latest release.

## xBIM 6.1 upgrade

Verified on branch `chore/xbim-6.1-upgrade-probe` (August 2026): bumping Xbim.Common / Xbim.Ifc / Xbim.Ifc4 from 5.1.341 to **6.1.605** on the existing `net48` target builds with **zero source changes, zero warnings, zero errors**, and a runtime smoke test of the full write path passes (`IfcStore.Create(…, Ifc4, InMemoryModel)` → `IfcProject.Initialize(ProjectUnits.SIUnitsUK)` → site/building/storey chain → `IfcSlab` with an `IfcFaceBasedSurfaceModel` → `"QTO Properties"` pset → `SaveAs` → read-back).

- xBIM 6.1 supports `netstandard2.0` (.NET Framework 4.7.2+), so **the upgrade does not require leaving net48** and is independent of the runtime migration.
- The headline 5→6 breaking change (`IfcStore.ModelProviderFactory` → `XbimServices` DI) is a no-op here: this code never used the v5 provider API, and v6's static constructor auto-configures on Windows.
- Cost is in **packaging, not code**. The shipped dependency set goes 18 → 20 assemblies (~13 MB): `Xbim.IO.Esent`, `Esent.Interop`, `Xbim.Ifc4x3`, `Microsoft.Extensions.DependencyInjection`, `Microsoft.Bcl.AsyncInterfaces` and `System.Threading.Tasks.Extensions` arrive; the `Microsoft.Extensions.Configuration.*` trio and the `Microsoft.Extensions.Logging` implementation leave. Esent ships as deploy weight but is never exercised — the export passes `XbimStoreType.InMemoryModel` explicitly. **All of these must be in the release zip**, or `IfcStore`'s static constructor throws when it resolves the heuristic provider.
- Still unverified: IFC output equivalence on a real model (diff a before/after export), and behaviour inside Rhino 8 under .NET Core. Both need a manual run.

Key sources: [Moving to .NET Core](https://developer.rhino3d.com/guides/rhinocommon/moving-to-dotnet-core/) · [.NET Core in Rhino 8](https://www.rhino3d.com/en/docs/guides/netcore/) · [Rhino 8: Get ready for .NET 7 (forum)](https://discourse.mcneel.com/t/rhino-8-feature-get-ready-for-net-7/148051)
