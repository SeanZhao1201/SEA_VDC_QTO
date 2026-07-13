using System;
using System.Windows;
using System.Linq;
using System.Collections.Generic;
using System.Collections.Specialized;
using System.Windows.Interop;
using System.Reflection;
using Rhino;
using Rhino.Geometry;
using Rhino.DocObjects;
using Rhino.Collections;
using Newtonsoft.Json;

namespace QTO_Tool
{
    class Methods
    {
        public static Random random = new Random();

        /// <summary>
        /// Copies Rhino "Attribute User Text" (object user strings) into a dictionary for IFC export.
        /// </summary>
        public static Dictionary<string, string> CopyRhinoAttributeUserStrings(RhinoObject rhobj)
        {
            var dict = new Dictionary<string, string>(StringComparer.Ordinal);
            if (rhobj?.Attributes == null)
                return dict;

            NameValueCollection nvc = rhobj.Attributes.GetUserStrings();
            if (nvc == null)
                return dict;

            string[] keys = nvc.AllKeys;
            if (keys == null)
                return dict;

            foreach (string key in keys)
            {
                if (string.IsNullOrWhiteSpace(key))
                    continue;

                string[] values = nvc.GetValues(key);
                if (values != null && values.Length > 0)
                    dict[key] = string.Join(", ", values);
                else
                    dict[key] = nvc[key] ?? string.Empty;
            }

            return dict;
        }

        internal static void SetChildStatus(QTOUI mw, ChildStatus winChildStatus)
        {
            switch (winChildStatus)
            {
                //case childStatus.ChildOfGH:
                //    setOwner(Grasshopper.Instances.DocumentEditor, mw);
                //    break;
                case ChildStatus.AlwaysOnTop:
                    mw.Topmost = true;
                    break;
                case ChildStatus.ChildOfRhino:
                    setOwner(RhinoApp.MainWindowHandle(), mw);
                    break;
                default:
                    break;
            }
        }

        public static double CalculateAngleThreshold(double angleThresholdSlider)
        {
            double result = 1;

            Vector2d baseVector = new Vector2d(1, 0);

            Vector2d rotatedVector = new Vector2d(1, 0);

            rotatedVector.Rotate(angleThresholdSlider * (Math.PI / 180));

            result = (baseVector.X * rotatedVector.X) + (baseVector.Y * rotatedVector.Y);

            return result;
        }

        //Utility function to set the ownership of a window object
        static void setOwner(IntPtr ownerPtr, Window window)
        {
            WindowInteropHelper helper = new WindowInteropHelper(window);
            helper.Owner = ownerPtr;
        }

        //Concrete model preparations
        public static string ConcreteModelSetup()
        {
            string modelUnitSystem = "Model's current unit system is: " + RunQTO.doc.GetUnitSystemName(true, true, true, true);
            string modelAngleTolerance = "Model's current angle tolerance is: " + RunQTO.doc.ModelAngleToleranceDegrees.ToString();
            string modelAbsoluteTolerance = "Model's current absolute tolerance is: " + RunQTO.doc.ModelAbsoluteTolerance.ToString();

            string examinationResult = "";
            int invalidObjCount = 0;
            int badGeometryCount = 0;
            int skippedObjCount = 0;

            List<Brep> surfaceList = new List<Brep>();
            List<Guid> addedObjectIds = new List<Guid>();
            List<CheckupBrep> joinedBreps = new List<CheckupBrep>();

            // The loop adds and deletes document objects, which invalidates a live
            // ObjectTable enumeration, so it iterates over a snapshot instead.
            List<RhinoObject> docObjects = RunQTO.doc.Objects.ToList();

            Logger.Info("Checkup: processing " + docObjects.Count + " objects (" +
                docObjects.Count(o => o is InstanceObject) + " block instances). Absolute tolerance: " +
                RunQTO.doc.ModelAbsoluteTolerance + ", angle tolerance: " +
                RunQTO.doc.ModelAngleToleranceDegrees + " degrees.");

            foreach (RhinoObject obj in docObjects)
            {
                surfaceList.Clear();
                addedObjectIds.Clear();

                try
                {
                    bool objectHandled = true;

                    if (obj.IsValid)
                    {
                        objectHandled = Methods.PrepareObject(obj, obj.Attributes, surfaceList, addedObjectIds);
                    }
                    else
                    {
                        invalidObjCount++;

                        Logger.Warn("Checkup: object " + obj.Id + " on layer '" + Methods.LayerPathOf(obj.Attributes) +
                            "' is not valid; it will be removed from the model.");
                    }

                    if (!objectHandled)
                    {
                        // Geometry conversion failed; keep the original instead of silently dropping it.
                        skippedObjCount++;
                        Methods.RollbackAddedObjects(addedObjectIds);
                        continue;
                    }

                    Brep[] tempBreps = Brep.JoinBreps(surfaceList, RunQTO.doc.ModelAbsoluteTolerance);

                    // Build the staged entries before deleting, so nothing that can
                    // throw runs between a successful delete and the staging.
                    List<CheckupBrep> stagedBreps = new List<CheckupBrep>();

                    if (tempBreps != null)
                    {
                        foreach (Brep tempBrep in tempBreps)
                        {
                            stagedBreps.Add(new CheckupBrep(tempBrep, obj));
                        }
                    }

                    // Delete can fail (locked object, locked layer). Re-adding the rebuilt
                    // copies next to an undeletable original would duplicate it in place,
                    // so roll the copies back and leave the object untouched instead.
                    if (RunQTO.doc.Objects.Delete(obj))
                    {
                        joinedBreps.AddRange(stagedBreps);
                    }
                    else
                    {
                        skippedObjCount++;
                        Methods.RollbackAddedObjects(addedObjectIds);

                        Logger.Warn("Checkup: could not delete object " + obj.Id + " on layer '" +
                            Methods.LayerPathOf(obj.Attributes) + "' (locked object or locked layer?); it was left unchecked.");
                    }
                }
                catch (Exception ex)
                {
                    skippedObjCount++;
                    Methods.RollbackAddedObjects(addedObjectIds);

                    Logger.Error("Checkup: processing object " + obj.Id + " on layer '" +
                        Methods.LayerPathOf(obj.Attributes) + "' failed; it was left unchecked.", ex);
                }
            }

            foreach (CheckupBrep joinedBrep in joinedBreps)
            {
                try
                {
                    joinedBrep.Brep.MergeCoplanarFaces(RunQTO.doc.ModelAbsoluteTolerance, RunQTO.doc.ModelAngleToleranceRadians);

                    // Compute returns null for degenerate geometry; treat that as bad
                    // geometry instead of crashing after the originals are already gone.
                    VolumeMassProperties massProperties = VolumeMassProperties.Compute(joinedBrep.Brep);

                    double volumeErrorPercentage = double.NaN;

                    // Volume != 0 (not > 0): an inward-oriented closed brep has a negative
                    // volume and a negative error percentage, which the old code accepted
                    // as good; only guard the null and divide-by-zero crash paths.
                    if (massProperties != null && massProperties.Volume != 0)
                    {
                        volumeErrorPercentage = Math.Round((massProperties.VolumeError / massProperties.Volume) * 100, 3);
                    }

                    if (joinedBrep.IsSolid && volumeErrorPercentage <= 1)
                    {
                        Guid newObjectId = RunQTO.doc.Objects.AddBrep(joinedBrep.Brep, joinedBrep.Attributes);

                        Logger.Info("Checkup: source object " + joinedBrep.SourceObjectId + " -> joined solid " +
                            newObjectId + " on layer '" + joinedBrep.LayerPath + "', volume error " +
                            volumeErrorPercentage + "%.");
                    }
                    else
                    {
                        badGeometryCount++;

                        Guid newObjectId = Methods.AddBadGeometry(joinedBrep.Brep, joinedBrep.Attributes);

                        Logger.Warn("Checkup: BAD geometry from source object " + joinedBrep.SourceObjectId + " -> " +
                            newObjectId + " on layer '" + joinedBrep.LayerPath + "': " +
                            (joinedBrep.IsSolid ? "" : "open shell with " + Methods.CountNakedEdges(joinedBrep.Brep) + " naked edges; ") +
                            "volume error " + (double.IsNaN(volumeErrorPercentage) ? "not computable" : volumeErrorPercentage + "%") + ".");
                    }
                }
                catch (Exception ex)
                {
                    badGeometryCount++;

                    Logger.Error("Checkup: could not classify joined brep from source object " +
                        joinedBrep.SourceObjectId + " on layer '" + joinedBrep.LayerPath + "'; marked as bad.", ex);

                    try { Methods.AddBadGeometry(joinedBrep.Brep, joinedBrep.Attributes); } catch { }
                }
            }

            examinationResult = invalidObjCount.ToString() + " invalid objects exist in the model. \n";
            examinationResult += badGeometryCount.ToString() + " bad geometry objects exist in the model.";

            if (skippedObjCount > 0)
            {
                examinationResult += "\n" + skippedObjCount.ToString() +
                    " objects could not be processed (locked or failed) and were left unchanged, see log: " +
                    (Logger.LogFilePath ?? "<log unavailable>");
            }

            Logger.Info("Checkup summary: " + invalidObjCount + " invalid, " + badGeometryCount + " bad, " +
                skippedObjCount + " skipped, " + joinedBreps.Count + " joined breps, of " +
                docObjects.Count + " objects.");

            RunQTO.doc.Views.Redraw();

            return String.Join(Environment.NewLine, examinationResult, modelUnitSystem, modelAngleTolerance, modelAbsoluteTolerance);
        }

        /// <summary>
        /// A brep produced by the checkup join step, staged for classification, with
        /// everything needed to re-add it and to name its source object in the log.
        /// </summary>
        private class CheckupBrep
        {
            public readonly Brep Brep;
            public readonly bool IsSolid;
            public readonly ObjectAttributes Attributes;
            public readonly Guid SourceObjectId;
            public readonly string LayerPath;

            public CheckupBrep(Brep brep, RhinoObject sourceObject)
            {
                this.Brep = brep;
                this.IsSolid = brep.IsSolid;
                this.Attributes = sourceObject.Attributes;
                this.SourceObjectId = sourceObject.Id;
                this.LayerPath = Methods.LayerPathOf(sourceObject.Attributes);
            }
        }

        static void RollbackAddedObjects(List<Guid> addedObjectIds)
        {
            foreach (Guid addedObjectId in addedObjectIds)
            {
                // The copies inherit the source object's mode and layer, so a
                // mode-respecting delete would fail for exactly the locked objects
                // that make this rollback necessary; ignoreModes forces it through.
                RhinoObject addedObject = RunQTO.doc.Objects.FindId(addedObjectId);

                if (addedObject == null)
                {
                    continue;
                }

                if (!RunQTO.doc.Objects.Delete(addedObject, true, true))
                {
                    Logger.Warn("Checkup: rollback could not delete copy " + addedObjectId +
                        "; the model may contain a duplicate.");
                }
            }

            addedObjectIds.Clear();
        }

        internal static string LayerPathOf(ObjectAttributes attributes)
        {
            try
            {
                Layer layer = RunQTO.doc.Layers.FindIndex(attributes.LayerIndex);
                return layer == null ? "<unknown layer>" : layer.FullPath;
            }
            catch
            {
                return "<unknown layer>";
            }
        }

        static int CountNakedEdges(Brep brep)
        {
            int count = 0;

            foreach (BrepEdge edge in brep.Edges)
            {
                if (edge.Valence == EdgeAdjacency.Naked)
                {
                    count++;
                }
            }

            return count;
        }

        //Concrete model preparations
        static void ExteriorModelExamination()
        {

        }

        //Concrete model preparations
        static void ConcreteModelArrangements()
        {

        }

        //Prepare BlockInstance
        /// <summary>
        /// Returns false when a solid-type piece (brep/extrusion/mesh) could not be
        /// converted, so the caller keeps the whole instance instead of deleting it
        /// with pieces missing. Unsupported piece types (curves, points, ...) are
        /// dropped, matching how top-level unsupported objects are handled.
        /// </summary>
        static bool PrepareBlockInstance(RhinoObject inputObj, ObjectAttributes _mainObjectAttributes, List<Brep> _surfaceList, List<Guid> _addedObjectIds)
        {
            InstanceObject instanceObj = (InstanceObject)inputObj;

            RhinoObject[] geometryPieces = { };
            ObjectAttributes[] objAtts = { };
            Rhino.Geometry.Transform[] objTransform = { };

            // Explode(true) flattens nested instances. The piece geometry lives in
            // block definition space; objTransform maps each piece to its world location.
            instanceObj.Explode(true, out geometryPieces, out objAtts, out objTransform);

            for (int i = 0; i < geometryPieces.Length; i++)
            {
                GeometryBase pieceGeometry = geometryPieces[i].Geometry.Duplicate();
                pieceGeometry.Transform(objTransform[i]);

                Brep tempBrep;

                if (pieceGeometry is Brep)
                {
                    tempBrep = (Brep)pieceGeometry;
                }
                else if (pieceGeometry is Extrusion)
                {
                    tempBrep = Brep.TryConvertBrep(pieceGeometry);

                    if (tempBrep == null)
                    {
                        Logger.Warn("Checkup: extrusion piece " + i + " inside block instance " + inputObj.Id +
                            " could not be converted to a brep; the whole instance was left unchecked.");

                        return false;
                    }
                }
                else if (pieceGeometry is Mesh)
                {
                    tempBrep = Brep.CreateFromMesh((Mesh)pieceGeometry, true);

                    if (tempBrep == null)
                    {
                        Logger.Warn("Checkup: mesh piece " + i + " inside block instance " + inputObj.Id +
                            " could not be converted to a brep; the whole instance was left unchecked.");

                        return false;
                    }
                }
                else
                {
                    Logger.Warn("Checkup: dropping non-takeoff geometry '" + pieceGeometry.GetType().Name +
                        "' inside block instance " + inputObj.Id);

                    continue;
                }

                if (tempBrep.Faces.Count == 1)
                {
                    _surfaceList.Add(tempBrep);
                }
                else
                {
                    tempBrep.MergeCoplanarFaces(RunQTO.doc.ModelAbsoluteTolerance, RunQTO.doc.ModelAngleToleranceRadians);

                    if (tempBrep.IsSolid)
                    {
                        Guid newObjectId = RunQTO.doc.Objects.Add(tempBrep, _mainObjectAttributes);
                        _addedObjectIds.Add(newObjectId);

                        Logger.Info("Checkup: block instance " + inputObj.Id + " piece " + i + " -> solid " + newObjectId + ".");
                    }
                    else
                    {
                        _surfaceList.Add(tempBrep);
                    }
                }
            }

            return true;
        }

        //Prepare Mesh
        static bool PrepareMesh(RhinoObject inputObj, ObjectAttributes _mainObjectAttributes, List<Brep> _surfaceList, List<Guid> _addedObjectIds)
        {
            Brep tempBrep = Brep.CreateFromMesh(((Mesh)inputObj.Geometry), true);

            if (tempBrep == null)
            {
                Logger.Warn("Checkup: mesh object " + inputObj.Id + " on layer '" +
                    LayerPathOf(_mainObjectAttributes) + "' could not be converted to a brep; it was left unchecked.");

                return false;
            }

            if (tempBrep.Faces.Count == 1)
            {
                _surfaceList.Add(tempBrep);
            }

            else
            {
                tempBrep.MergeCoplanarFaces(RunQTO.doc.ModelAbsoluteTolerance, RunQTO.doc.ModelAngleToleranceRadians);

                if (tempBrep.IsSolid)
                {
                    Guid newObjectId = RunQTO.doc.Objects.Add(tempBrep, _mainObjectAttributes);
                    _addedObjectIds.Add(newObjectId);

                    Logger.Info("Checkup: mesh object " + inputObj.Id + " -> solid " + newObjectId + ".");
                }

                else
                {
                    _surfaceList.Add(tempBrep);
                }
            }

            return true;
        }

        /// <summary>
        /// Rebuilds one document object into merged solids (added to the document and
        /// recorded in _addedObjectIds) and open shells (staged in _surfaceList for
        /// joining). Returns false when the geometry could not be converted, in which
        /// case the caller must keep the original object.
        /// </summary>
        static bool PrepareObject(RhinoObject inputObj, ObjectAttributes _mainObjectAttributes, List<Brep> _surfaceList, List<Guid> _addedObjectIds)
        {
            _mainObjectAttributes.ObjectColor = System.Drawing.Color.Black;
            _mainObjectAttributes.ColorSource = ObjectColorSource.ColorFromObject;

            string objType = inputObj.GetType().ToString().Split('.').Last<string>();

            if (objType == "BrepObject")
            {
                // Work on a duplicate so MergeCoplanarFaces cannot mutate the document
                // object's own geometry (which also made the invalid-merge fallback
                // below re-fetch the already-mutated brep instead of the original).
                Brep tempBrep = (Brep)inputObj.Geometry.Duplicate();

                if (tempBrep.Faces.Count == 1)
                {
                    _surfaceList.Add(tempBrep);
                }

                else
                {
                    tempBrep.MergeCoplanarFaces(RunQTO.doc.ModelAbsoluteTolerance, RunQTO.doc.ModelAngleToleranceRadians);

                    if (tempBrep.IsSolid)
                    {
                        if (!tempBrep.IsValid)
                        {
                            tempBrep = (Brep)inputObj.Geometry.Duplicate();
                        }

                        Guid newObjectId = RunQTO.doc.Objects.Add(tempBrep, _mainObjectAttributes);
                        _addedObjectIds.Add(newObjectId);

                        Logger.Info("Checkup: object " + inputObj.Id + " (Brep, layer '" +
                            LayerPathOf(_mainObjectAttributes) + "') -> solid " + newObjectId + ".");
                    }

                    else
                    {
                        _surfaceList.Add(tempBrep);
                    }
                }
            }

            else if (objType == "ExtrusionObject")
            {
                Brep tempBrep = Brep.TryConvertBrep(inputObj.Geometry);

                if (tempBrep == null)
                {
                    Logger.Warn("Checkup: extrusion object " + inputObj.Id + " on layer '" +
                        LayerPathOf(_mainObjectAttributes) + "' could not be converted to a brep; it was left unchecked.");

                    return false;
                }

                if (tempBrep.Faces.Count == 1)
                {
                    _surfaceList.Add(tempBrep);
                }

                else
                {
                    tempBrep.MergeCoplanarFaces(RunQTO.doc.ModelAbsoluteTolerance, RunQTO.doc.ModelAngleToleranceRadians);

                    if (tempBrep.IsSolid)
                    {
                        Guid newObjectId = RunQTO.doc.Objects.Add(tempBrep, _mainObjectAttributes);
                        _addedObjectIds.Add(newObjectId);

                        Logger.Info("Checkup: object " + inputObj.Id + " (Extrusion, layer '" +
                            LayerPathOf(_mainObjectAttributes) + "') -> solid " + newObjectId + ".");
                    }

                    else
                    {
                        _surfaceList.Add(tempBrep);
                    }
                }
            }

            else if (objType == "MeshObject")
            {
                return Methods.PrepareMesh(inputObj, _mainObjectAttributes, _surfaceList, _addedObjectIds);
            }

            else if (objType == "InstanceObject")
            {
                return Methods.PrepareBlockInstance(inputObj, _mainObjectAttributes, _surfaceList, _addedObjectIds);
            }

            else
            {
                // Not takeoff geometry (curve, point, annotation, ...). The checkup
                // has always removed these; log it so a vanished object can be explained.
                Logger.Warn("Checkup: object " + inputObj.Id + " of type " + objType + " on layer '" +
                    LayerPathOf(_mainObjectAttributes) + "' is not takeoff geometry and will be removed from the model.");
            }

            return true;
        }

        static Guid AddBadGeometry(Brep brep, ObjectAttributes attributes)
        {
            // Several joined pieces of one source object share the same attributes
            // instance; paint a duplicate red so good sibling pieces added later
            // don't inherit the red color.
            ObjectAttributes redAttributes = attributes.Duplicate();

            if (redAttributes == null)
            {
                redAttributes = attributes;
            }

            redAttributes.ObjectColor = System.Drawing.Color.Red;
            redAttributes.ColorSource = ObjectColorSource.ColorFromObject;

            return RunQTO.doc.Objects.AddBrep(brep, redAttributes);
        }

        public static void HighlightBadGeometry(RhinoObject rhobj)
        {
            if (rhobj != null)
            {
                ObjectAttributes newObjectAttributes = rhobj.Attributes;
                newObjectAttributes.ObjectColor = System.Drawing.Color.Red;
                newObjectAttributes.ColorSource = ObjectColorSource.ColorFromObject;

                RunQTO.doc.Objects.ModifyAttributes(rhobj, newObjectAttributes, false);
            }
        }

        public static UIElement GetByUid(DependencyObject rootElement, string uid)
        {
            foreach (UIElement element in LogicalTreeHelper.GetChildren(rootElement).OfType<UIElement>())
            {
                if (element.Uid == uid)
                {
                    return element;
                }

                UIElement resultChildren = GetByUid(element, uid);

                if (resultChildren != null)
                {
                    return resultChildren;
                }
            }
            return null;
        }

        public static void CloseWindowUsingIdentifier(string windowName)
        {
            Assembly currentAssembly = Assembly.GetExecutingAssembly();
            string name;

            foreach (Window w in Application.Current.Windows)
            {

                try
                {
                    name = w.Name;
                }
                catch
                {
                    name = "";
                }

                if (name == windowName)
                {
                    w.Close();
                    break;
                }
            }
        }

        public static int AutomaticTemplateSelect(string layerName, List<string> concreteTemplateNames)
        {
            int result = 0;

            for (int i = 0; i < concreteTemplateNames.Count; i++)
            {
                if (layerName.ToLower().Split('_')[0].Contains(concreteTemplateNames[i].ToLower()))
                {
                    if (layerName.ToLower().Contains("continuous") == false)
                    {
                        result = i;
                    }
                }

                if (layerName.ToLower().Contains("continuous") == true)
                {
                    result = concreteTemplateNames.IndexOf("Continuous Footing");
                }
            }

            return result;
        }

        public static string FindFloor(Dictionary<double, string> floorElevations, double targetValue)
        {
            List<double> elevations = floorElevations.Keys.ToList();

            double closestValue = elevations[0];
            double minDifference = Math.Abs(elevations[0] - targetValue);

            for (int i = 1; i < elevations.Count; i++)
            {
                double difference = Math.Abs(elevations[i] - targetValue);
                if (difference < minDifference)
                {
                    minDifference = difference;
                    closestValue = elevations[i];
                }
            }

            return floorElevations[closestValue];
        }

        public static void SaveDictionaryToDocumentStrings(Dictionary<double, string> data)
        {
            // Serialize the dictionary to a JSON string
            string jsonString = JsonConvert.SerializeObject(data);

            // Store the JSON string in RhinoDoc.Strings
            RunQTO.doc.Strings.SetString("FloorElevations", jsonString);

            // Save changes to the Rhino document
            RunQTO.doc.Modified = true;
        }

        public static Dictionary<double, string> RetrieveDictionaryFromDocumentStrings()
        {
            // Get the active Rhino document
            RhinoDoc doc = RhinoDoc.ActiveDoc;

            // Retrieve the JSON string from RhinoDoc.Strings
            string jsonString = doc.Strings.GetValue("FloorElevations");

            if (!string.IsNullOrEmpty(jsonString))
            {
                // Deserialize the JSON string back to a dictionary
                return JsonConvert.DeserializeObject<Dictionary<double, string>>(jsonString);
            }

            // Return an empty dictionary if the JSON string is not found
            return new Dictionary<double, string>();
        }

        public static double SetVolumeConversionFactor(string modelUnit)
        {
            double result;

            if (modelUnit == "ft")
            {
                result = 0.037037;
            }

            else if (modelUnit == "in")
            {
                result = 2.14335e-5;
            }
            else
            {
                result = 1;
            }

            return result;
        }

        public static void Blockify()
        {
            int objectIndex = 0;
            int skippedObjCount = 0;

            // Snapshot the object table: the loop adds instances and deletes originals,
            // which invalidates a live enumeration.
            List<RhinoObject> docObjects = RunQTO.doc.Objects.ToList();

            Logger.Info("Blockify: processing " + docObjects.Count + " objects.");

            foreach (RhinoObject obj in docObjects)
            {
                if (!(obj is InstanceObject))
                {
                    ObjectAttributes mainObjectAttributes = obj.Attributes;
                    Layer layer = RunQTO.doc.Layers.FindIndex(mainObjectAttributes.LayerIndex);

                    mainObjectAttributes.ColorSource = ObjectColorSource.ColorFromLayer;
                    mainObjectAttributes.ObjectColor = layer.Color;

                    string blockObjectName = LayerParentsPath(layer) + layer.Name + "_" + objectIndex.ToString();

                    // Duplicate the original geometry
                    GeometryBase geom = obj.Geometry.Duplicate();

                    // Calculate the center of the geometry's bounding box
                    BoundingBox bbox = geom.GetBoundingBox(true);
                    Point3d bboxCenter = bbox.Center;

                    // Create a block definition using the bounding box center as the base point
                    int blockDefIndex = RunQTO.doc.InstanceDefinitions.Add(blockObjectName, "Block containing one object", bboxCenter, new List<GeometryBase> { geom }, new List<ObjectAttributes> { mainObjectAttributes });

                    // Place the block instance at the original location
                    if (blockDefIndex != -1) // Check if the block was created successfully
                    {
                        // Calculate the transformation to move the block instance back to its original position
                        Transform placeBack = Transform.Translation(bboxCenter - Point3d.Origin);
                        Guid instanceId = RunQTO.doc.Objects.AddInstanceObject(blockDefIndex, placeBack, mainObjectAttributes);

                        if (instanceId == Guid.Empty)
                        {
                            // No instance was placed; deleting the original would lose the object.
                            skippedObjCount++;

                            if (!RunQTO.doc.InstanceDefinitions.Delete(blockDefIndex, true, true))
                            {
                                Logger.Warn("Blockify: could not delete the orphan block definition '" +
                                    blockObjectName + "'.");
                            }

                            Logger.Warn("Blockify: could not place a block instance for object " + obj.Id +
                                " on layer '" + layer.FullPath + "'; it was left as-is.");
                        }
                        // Delete can fail (locked object, locked layer). Keeping the new
                        // instance next to an undeletable original would duplicate the
                        // object in place, so undo the block instead.
                        else if (!RunQTO.doc.Objects.Delete(obj, true))
                        {
                            skippedObjCount++;

                            // The instance inherits the original's locked mode/layer, so a
                            // mode-respecting delete would fail for the same reason the
                            // original's did; ignoreModes forces the rollback through.
                            RhinoObject instanceObject = RunQTO.doc.Objects.FindId(instanceId);

                            if (instanceObject == null || !RunQTO.doc.Objects.Delete(instanceObject, true, true))
                            {
                                Logger.Warn("Blockify: rollback could not delete block instance " + instanceId +
                                    "; the model may contain a duplicate.");
                            }

                            if (!RunQTO.doc.InstanceDefinitions.Delete(blockDefIndex, true, true))
                            {
                                Logger.Warn("Blockify: could not delete the orphan block definition '" +
                                    blockObjectName + "'.");
                            }

                            Logger.Warn("Blockify: could not delete original object " + obj.Id + " on layer '" +
                                layer.FullPath + "' (locked object or locked layer?); it was left as-is.");
                        }
                    }
                    else
                    {
                        skippedObjCount++;

                        Logger.Warn("Blockify: could not create a block definition for object " + obj.Id +
                            " on layer '" + layer.FullPath + "'; it was left as-is.");
                    }

                    objectIndex++;
                }
            }

            Logger.Info("Blockify finished: " + (docObjects.Count(o => !(o is InstanceObject)) - skippedObjCount) +
                " objects blockified, " + skippedObjCount + " skipped.");

            RunQTO.doc.Views.Redraw();
        }

        public static double CalculateGrossVolume(Brep brep)
        {
            List<BrepLoop> brepInnerLoopsToRemove = new List<BrepLoop>();
            List<ComponentIndex> brepInnerLoopsToRemoveIndices = new List<ComponentIndex>();

            foreach (BrepLoop loop in brep.Loops)
            {
                if (loop.LoopType == BrepLoopType.Inner)
                {

                    Curve innerLoopCurve = loop.To3dCurve();

                    Brep innerLoopBrep = Brep.CreatePlanarBreps(innerLoopCurve, RunQTO.doc.ModelAbsoluteTolerance)[0];
                    Surface innerLoopSurface = innerLoopBrep.Surfaces[0];

                    Point3d centroid = innerLoopSurface.PointAt(
                        innerLoopSurface.Domain(0).Min + (innerLoopSurface.Domain(0).Max - innerLoopSurface.Domain(0).Min) * 0.02,
                        innerLoopSurface.Domain(1).Min + (innerLoopSurface.Domain(1).Max - innerLoopSurface.Domain(1).Min) * 0.02);

                    Vector3d normal = innerLoopSurface.NormalAt(innerLoopSurface.Domain(0).Mid, innerLoopSurface.Domain(1).Mid);
                    normal.Unitize();

                    // Create a line representing the ray for the intersection test
                    Line normalLine_1 = new Line(centroid, normal * 1000);
                    Line normalLine_2 = new Line(centroid, normal * -1000);

                    LineCurve normalCurve = new List<LineCurve> { new LineCurve(normalLine_1), new LineCurve(normalLine_2) }[0];

                    Curve[] overlapCurves;
                    Point3d[] intersectionPoints;
                    bool intersect = Rhino.Geometry.Intersect.Intersection.CurveBrep(normalCurve, brep, RunQTO.doc.ModelAbsoluteTolerance, out overlapCurves, out intersectionPoints);

                    if (intersect && intersectionPoints.Length == 0)
                    {
                        // If there is an intersection, mark this inner loop for removal
                        brepInnerLoopsToRemove.Add(loop);
                        brepInnerLoopsToRemoveIndices.Add(loop.ComponentIndex());
                    }
                }
            }

            if (brepInnerLoopsToRemoveIndices.Count > 0)
            {
                Brep newBrep = brep.RemoveHoles(brepInnerLoopsToRemoveIndices, RunQTO.doc.ModelAbsoluteTolerance);

                return newBrep.GetVolume();
            }
            else {
                return brep.GetVolume();
            }
        }

        private static string LayerParentsPath(Layer layer)
        {
            Guid parentLayerId = layer.ParentLayerId;
            if (parentLayerId == Guid.Empty)
            {
                return ""; // Base case: no parent
            }

            Layer parentLayer = RhinoDoc.ActiveDoc.Layers.FindId(parentLayerId);
            if (parentLayer == null)
            {
                return "";
            }

            string layerParentsPath = LayerParentsPath(parentLayer);
            return string.IsNullOrEmpty(layerParentsPath)
                ? parentLayer.Name + "_"
                : layerParentsPath + parentLayer.Name + "_";
        }
    }
}
