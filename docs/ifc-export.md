# IFC export design

How `IFCMethods.cs` writes the IFC file, and why. Read this before changing anything in the export path (`QTOUI.Export_IFC_Clicked` → `IFCMethods`).

## Schema and model

- **IFC4 only**, written with xBIM Essentials 5.1 (`Xbim.Ifc4.*` types throughout). Supporting IFC2x3 would require refactoring to xBIM's schema-agnostic interfaces (`IIfc*`) or a parallel `Xbim.Ifc2x3` code path — it is not a flag you can flip.
- The model is an **in-memory** store (`XbimStoreType.InMemoryModel`); the Esent database backend referenced by the packages is never used at runtime.
- Work happens in many small transactions (one per element, one for the building, one for the storeys). An xBIM transaction disposed **without** `Commit()` rolls back — `CreateAndAddIFCElement` relies on this to discard the orphan material relationship when it encounters an unknown template type.

## Spatial hierarchy

```
IfcProject  (name = 3dm file name without path/extension; "QTO Project" if unsaved)
└─ IfcSite "Site"
   └─ IfcBuilding "Building"
      ├─ IfcBuildingStorey  (one per floor from the elevation input, ordered by elevation)
      │  └─ elements (IfcRelContainedInSpatialStructure)
      └─ IfcBuildingStorey "Unassigned"  (only created when needed)
```

- `CreateBuildingStoreys(model, building, floorElevations, floorNamesInUse)` builds the storeys and returns a `Dictionary<string, IfcBuildingStorey>` keyed by **floor bucket name** — the same keys the `AllTemplates.allTemplates` dictionaries use. The mapping is total: every in-use bucket name resolves to a storey, so element routing cannot miss.
- The **fallback storey** `"Unassigned"` (elevation 0) is created lazily, only if some bucket name has no matching entry in the elevation input. The `"-"` bucket (used when no elevations were defined at Calculate time) always lands here.
- **Duplicate floor names** collapse into one storey placed at the lowest of their elevations. Floor *name* is the only join key between quantity buckets and storeys (elevations are the dictionary keys and thus unique; names are not).
- Floors defined in the elevation input but unused by any element still produce (empty) storeys — intentional, so the level structure of the project is complete.
- **Stale-name caveat:** `template.floor` is frozen at Calculate time (`Methods.FindFloor`), but storeys are built from the live `ElevationInput.floorElevations` at export time. If the user edits floors between Calculate and Export, renamed/removed floors send their elements to `Unassigned`. The fix is to re-run Calculate; the exporter deliberately does not guess.

## Units and placements

- `IfcProject.Initialize(ProjectUnits.SIUnitsUK)` declares SI units with a **millimeter** length unit (METRE + MILLI prefix — verified in xBIM `IfcProjectPartial.Initialize`). Everything exported must therefore be in mm.
- One scale factor for the whole export: `GetModelUnitToMillimeterFactor()` = `RhinoMath.UnitScale(doc.ModelUnitSystem, UnitSystem.Millimeters)`. It is applied to mesh vertices and to storey elevations. Never introduce a second conversion path.
- Placement chain: site (world origin) ← building (origin, relative to site) ← storeys (z = elevation in mm, relative to building).
- **Element placements are world-absolute on purpose** (no `PlacementRelTo`): the mesh geometry is written in absolute mm coordinates, and `IfcRelContainedInSpatialStructure` implies no geometric transform, so nothing shifts. Chaining element placements to their storey without also translating the geometry by −elevation would move everything. Strict best-practice checkers may warn about non-relative placements; that is a known, accepted trade-off.

## Geometry and appearance

- Each Brep is meshed with `MeshingParameters.QualityRenderMesh` and written as a tessellated `IfcFaceBasedSurfaceModel` (per-face `IfcPolyLoop`s), with the layer color attached via `IfcStyledItem`/`IfcSurfaceStyle`.
- The layer name is carried on an `IfcPresentationLayerAssignment` per shape representation.

## Property sets

- `QTO Properties` — element name abbreviation, `FLOOR` (the floor bucket name as text, kept for backward compatibility even though storeys now exist), and the per-type quantities (rounded to 2 decimals at calculation time).
- `QTO Attributes` — a copy of the Rhino object's attribute user text (`Methods.CopyRhinoAttributeUserStrings`), only when non-empty.

## xBIM API notes

The hierarchy is built with xBIM's partial-class convenience members, all verified to exist in the Essentials source bracketing the referenced 5.1.341 package (tag `v5.0.213` and master):

- `IfcProject.AddSite(IfcSite)`, `IfcSite.AddBuilding(IfcBuilding)` — create/reuse an `IfcRelAggregates`.
- `IfcSpatialStructureElement.AddToSpatialDecomposition(child)` — building → storey aggregation.
- `IfcSpatialStructureElement.AddElement(IfcProduct)` — creates/reuses **one** `IfcRelContainedInSpatialStructure` per spatial element, so containment relationships do not proliferate per element.

## Known quirks / future work

- `CreateIfcRelAssociatesMaterial` creates a fresh `IfcMaterial` per element with `Category = "Concrete"`, `Name = "Undefined"` (arguments look swapped); a single shared material instance would be cleaner.
- IFC2x3 export and a schema picker in the save dialog are the most-requested follow-up; see the refactoring note at the top.
