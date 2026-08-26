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

        // Any plugin window (QTOUI, FormworkUI) can be parented to Rhino.
        internal static void SetChildStatus(System.Windows.Window mw, ChildStatus winChildStatus)
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
        public static string ConcreteModelSetup(out uint checkupUndoRecordSerial)
        {
            string modelUnitSystem = "Model's current unit system is: " + RunQTO.doc.GetUnitSystemName(true, true, true, true);
            string modelAngleTolerance = "Model's current angle tolerance is: " + RunQTO.doc.ModelAngleToleranceDegrees.ToString();
            string modelAbsoluteTolerance = "Model's current absolute tolerance is: " + RunQTO.doc.ModelAbsoluteTolerance.ToString();

            string examinationResult = "";
            int solidObjCount = 0;
            int invalidObjCount = 0;
            int badGeometryCount = 0;
            int ignoredObjCount = 0;
            int lockedObjCount = 0;
            int skippedObjCount = 0;
            int stampedObjCount = 0;
            int restampedObjCount = 0;

            List<Brep> surfaceList = new List<Brep>();
            List<Guid> addedObjectIds = new List<Guid>();
            List<CheckupBrep> joinedBreps = new List<CheckupBrep>();

            // The loop adds and deletes document objects, which invalidates a live
            // ObjectTable enumeration, so it iterates over a snapshot instead.
            // Deleted-but-unpurged objects (left behind by an earlier checkup that
            // is still on the undo stack) show up in the enumeration and would
            // inflate every count, so they are excluded from the snapshot.
            List<RhinoObject> docObjects = new List<RhinoObject>();
            int ghostObjCount = 0;

            foreach (RhinoObject docObject in RunQTO.doc.Objects)
            {
                if (docObject.IsDeleted)
                {
                    ghostObjCount++;
                }
                else
                {
                    docObjects.Add(docObject);
                }
            }

            if (ghostObjCount > 0)
            {
                Logger.Info("Checkup: excluded " + ghostObjCount + " deleted-but-unpurged objects from the snapshot.");
            }

            // The snapshot's default enumerator never yields hidden objects
            // (hidden object mode or a hidden layer), so they are left
            // untouched and never verified - and Calculate excludes them for
            // the same reason. Count them so the summary says so instead of
            // reporting a clean model while hidden solids sit unchecked.
            int hiddenObjCount = 0;
            HashSet<Guid> snapshotIds = new HashSet<Guid>(docObjects.Select(o => o.Id));
            ObjectEnumeratorSettings includeHidden = new ObjectEnumeratorSettings
            {
                NormalObjects = true,
                LockedObjects = true,
                HiddenObjects = true,
            };

            foreach (RhinoObject candidate in RunQTO.doc.Objects.GetObjectList(includeHidden))
            {
                if (candidate.IsDeleted || snapshotIds.Contains(candidate.Id))
                {
                    continue;
                }

                // Hidden curves/points/annotations would be ignored as
                // non-takeoff geometry even if visible; counting them would
                // demand a pointless unhide-and-re-run for objects that never
                // carry quantities.
                string candidateTypeName = candidate.GetType().Name;

                if (candidateTypeName != "BrepObject" && candidateTypeName != "ExtrusionObject" &&
                    candidateTypeName != "MeshObject" && candidateTypeName != "InstanceObject")
                {
                    continue;
                }

                hiddenObjCount++;

                Logger.Info("Checkup: object " + candidate.Id + " on layer '" +
                    Methods.LayerPathOf(candidate.Attributes) +
                    "' is hidden; it was not checked and is excluded from the take-off.");
            }

            Logger.Info("Checkup: processing " + docObjects.Count + " objects (" +
                docObjects.Count(o => o is InstanceObject) + " block instances). Absolute tolerance: " +
                RunQTO.doc.ModelAbsoluteTolerance + ", angle tolerance: " +
                RunQTO.doc.ModelAngleToleranceDegrees + " degrees.");

            // One undo record around every document mutation, so the whole checkup
            // can be reverted with a single RhinoDoc.Undo().
            uint undoRecordSerial = RunQTO.doc.BeginUndoRecord("QTO Checkup");

            checkupUndoRecordSerial = undoRecordSerial;

            if (undoRecordSerial == 0)
            {
                // BeginUndoRecord returns 0 when undo recording is disabled or
                // another record is already active; the checkup still runs, but a
                // one-click revert is impossible and must not be offered.
                Logger.Warn("Checkup: no undo record could be started (undo recording disabled or " +
                    "another record active); REVERT CHECKUP will be unavailable for this run.");
            }

            try
            {
                foreach (RhinoObject obj in docObjects)
                {
                    surfaceList.Clear();
                    addedObjectIds.Clear();

                    try
                    {
                        // Locked objects (object mode or layer) cannot be deleted by the
                        // rebuild, so attempting it would only produce a rollback and a
                        // scary failure count; leave them untouched instead.
                        if (obj.IsLocked || Methods.IsLayerLocked(obj.Attributes))
                        {
                            lockedObjCount++;

                            Logger.Info("Checkup: object " + obj.Id + " on layer '" + Methods.LayerPathOf(obj.Attributes) +
                                "' left as-is (locked).");

                            continue;
                        }

                        // Non-takeoff objects (curves, points, annotations, ...) carry no
                        // quantities; they are left untouched instead of being deleted.
                        // The filter runs before the validity check so an invalid curve
                        // or annotation cannot fall into the invalid branch below and
                        // be deleted like a broken solid.
                        string objTypeName = obj.GetType().Name;

                        if (objTypeName != "BrepObject" && objTypeName != "ExtrusionObject" &&
                            objTypeName != "MeshObject" && objTypeName != "InstanceObject")
                        {
                            ignoredObjCount++;

                            Logger.Info("Checkup: object " + obj.Id + " on layer '" + Methods.LayerPathOf(obj.Attributes) +
                                "' ignored (not take-off geometry: " + objTypeName + ").");

                            continue;
                        }

                        bool objectHandled = true;
                        bool objectIsInvalid = false;

                        if (obj.IsValid)
                        {
                            // Stamp the checkup-surviving identity BEFORE the rebuild:
                            // every copy below is added with this same attributes
                            // instance, so the stamp rides onto whatever replaces the
                            // original. An existing stamp is preserved - obj.Id is
                            // re-minted by every checkup, the stamp is what stays.
                            if (Methods.EnsureStableId(obj))
                            {
                                stampedObjCount++;
                            }

                            objectHandled = Methods.PrepareObject(obj, obj.Attributes, surfaceList, addedObjectIds);
                        }
                        else
                        {
                            // Counted as removed only after the delete below succeeds, so
                            // a failed delete cannot report the object both as removed
                            // (invalid) and as could-not-be-processed (skipped).
                            objectIsInvalid = true;

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

                        // A valid object whose rebuild staged nothing and added nothing
                        // (a block instance holding only curves/points/annotations, or
                        // an empty block definition) must not be deleted: nothing would
                        // replace it, so it would silently vanish from the model.
                        if (!objectIsInvalid && surfaceList.Count == 0 && addedObjectIds.Count == 0)
                        {
                            ignoredObjCount++;

                            Logger.Info("Checkup: object " + obj.Id + " on layer '" + Methods.LayerPathOf(obj.Attributes) +
                                "' left as-is (its rebuild produced no take-off geometry).");

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

                        // Delete can still fail for reasons other than locking (locked
                        // objects never reach this point). Re-adding the rebuilt copies
                        // next to an undeletable original would duplicate it in place,
                        // so roll the copies back and leave the object untouched instead.
                        if (RunQTO.doc.Objects.Delete(obj))
                        {
                            joinedBreps.AddRange(stagedBreps);

                            // Solids added directly inside PrepareObject (closed breps,
                            // extrusions, meshes, block pieces) are verified solids the
                            // classification loop below never sees; count them here so
                            // the headline "N solids verified" covers the common case.
                            solidObjCount += addedObjectIds.Count;

                            if (objectIsInvalid)
                            {
                                invalidObjCount++;
                            }
                        }
                        else
                        {
                            skippedObjCount++;
                            Methods.RollbackAddedObjects(addedObjectIds);

                            Logger.Warn("Checkup: could not delete object " + obj.Id + " on layer '" +
                                Methods.LayerPathOf(obj.Attributes) + "' (IsDeleted=" + obj.IsDeleted +
                                ", IsLocked=" + obj.IsLocked + ", layer locked=" + Methods.IsLayerLocked(obj.Attributes) +
                                "); it was left unchecked.");
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
                            solidObjCount++;

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

                // Duplicate stable ids would become duplicate IfcGlobalIds
                // (schema-invalid) and ambiguous 4D links. Sources: one object
                // fanning out into several solids (joined shells, block pieces)
                // - every copy was added with the source's attributes - and
                // user copy-paste, which clones user strings. Keep the stamp on
                // a closed solid first, then the first holder in document
                // order; re-stamp the rest with their own ids. Runs inside the
                // undo record like every other mutation of this checkup.
                restampedObjCount = Methods.RestampDuplicateStableIds();
            }
            finally
            {
                if (undoRecordSerial > 0)
                {
                    RunQTO.doc.EndUndoRecord(undoRecordSerial);
                }
            }

            // Rhino discards an empty undo record while its serial stays consumed,
            // so the UI's topmost-record check would still pass and Undo() would pop
            // the user's own previous action. A run that changed no document object
            // therefore reports no revertable record. Stamp and re-stamp commits
            // are document mutations too: a run that only wrote stable ids leaves
            // a NON-empty record, so it must keep its serial or the user's next
            // Ctrl+Z would silently pop the invisible stamps.
            if (solidObjCount == 0 && invalidObjCount == 0 && badGeometryCount == 0 &&
                stampedObjCount == 0 && restampedObjCount == 0)
            {
                checkupUndoRecordSerial = 0;
            }

            examinationResult = "Checkup complete: " + solidObjCount.ToString() + " solids verified.";
            examinationResult += "\n" + badGeometryCount.ToString() + " bad geometry objects are highlighted in red.";

            if (invalidObjCount > 0)
            {
                examinationResult += "\n" + invalidObjCount.ToString() + " invalid objects were removed.";
            }

            if (ignoredObjCount > 0)
            {
                examinationResult += "\n" + ignoredObjCount.ToString() + " curves/points were ignored (not take-off geometry).";
            }

            if (lockedObjCount > 0)
            {
                examinationResult += "\n" + lockedObjCount.ToString() + " locked objects were left as-is.";
            }

            if (hiddenObjCount > 0)
            {
                examinationResult += "\n" + hiddenObjCount.ToString() +
                    " hidden objects were NOT checked and are excluded from the take-off;" +
                    " unhide them and re-run the checkup if they belong in it.";
            }

            if (skippedObjCount > 0)
            {
                examinationResult += "\n" + skippedObjCount.ToString() +
                    " objects could not be processed and were left unchanged (see log).";
            }

            if (undoRecordSerial == 0)
            {
                examinationResult += "\nUndo could not be recorded for this run; REVERT CHECKUP is unavailable.";
            }

            Logger.Info("Checkup summary: " + solidObjCount + " solids, " + invalidObjCount + " invalid, " +
                badGeometryCount + " bad, " + ignoredObjCount + " ignored (non-takeoff), " + lockedObjCount +
                " locked, " + hiddenObjCount + " hidden (unchecked), " + skippedObjCount + " skipped, " +
                joinedBreps.Count + " joined breps, of " + docObjects.Count + " objects. " +
                stampedObjCount + " objects newly stamped with " + StableIdKey +
                ", " + (docObjects.Count - stampedObjCount) + " kept an existing or no stamp.");

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

        /// <summary>
        /// Key of the per-object user string that survives the checkup's
        /// delete-and-re-add cycle (attributes ride onto the copies) and is the
        /// identity the IFC export derives IfcGlobalId from. obj.Id itself is
        /// re-minted by every checkup (verified 2026-08-23: all 811 ids of the
        /// Bellwether derived model changed in one run), so it cannot serve as
        /// a cross-export id on its own.
        /// </summary>
        public const string StableIdKey = "QTO_STABLE_ID";

        /// <summary>
        /// Stamps the object with its own id as the stable identity unless a
        /// usable stamp is already present. The first checkup of a file
        /// therefore stamps the FILE ids - the same ids the formwork generator
        /// reads - and every later checkup preserves them. Returns true when a
        /// new stamp was written. Never throws; a failed stamp only means the
        /// IFC export falls back to the (session-local) object id.
        /// </summary>
        internal static bool EnsureStableId(RhinoObject obj)
        {
            try
            {
                string existing = obj.Attributes.GetUserString(StableIdKey);

                Guid parsed;
                if (!string.IsNullOrWhiteSpace(existing) && Guid.TryParse(existing, out parsed) && parsed != Guid.Empty)
                {
                    return false;
                }

                if (!string.IsNullOrWhiteSpace(existing))
                {
                    Logger.Warn("Checkup: object " + obj.Id + " carried an unusable " + StableIdKey +
                        " ('" + existing + "'); re-stamped with its own id.");
                }

                obj.Attributes.SetUserString(StableIdKey, obj.Id.ToString());

                if (!obj.CommitChanges())
                {
                    Logger.Warn("Checkup: stamping " + StableIdKey + " on object " + obj.Id +
                        " did not commit; the IFC export will fall back to its object id.");

                    return false;
                }

                return true;
            }
            catch (Exception ex)
            {
                Logger.Warn("Checkup: could not stamp " + StableIdKey + " on object " + obj.Id + " - " + ex.Message);

                return false;
            }
        }

        /// <summary>
        /// Re-stamps every take-off object whose stable id another object
        /// already holds, and returns how many were re-stamped. The keeper is
        /// chosen with a preference for CLOSED SOLIDS (pass 1) before falling
        /// back to document order (pass 2): a join or block explode fans one
        /// source's attributes onto several outputs, and without the
        /// preference a red open shell added before its solid sibling would
        /// keep the 4D-linked stamp while the verified solid churned. Never
        /// throws; an object that cannot be re-stamped is reported and left
        /// for the IFC export's own duplicate guard.
        /// </summary>
        static int RestampDuplicateStableIds()
        {
            int restampedCount = 0;

            try
            {
                Dictionary<string, Guid> seen = new Dictionary<string, Guid>(StringComparer.OrdinalIgnoreCase);

                for (int pass = 0; pass < 2; pass++)
                {
                    foreach (RhinoObject obj in RunQTO.doc.Objects)
                    {
                        if (obj.IsDeleted)
                        {
                            continue;
                        }

                        string typeName = obj.GetType().Name;

                        if (typeName != "BrepObject" && typeName != "ExtrusionObject" &&
                            typeName != "MeshObject" && typeName != "InstanceObject")
                        {
                            continue;
                        }

                        if (pass == 0 && !Methods.IsClosedSolidGeometry(obj))
                        {
                            continue;
                        }

                        string stamp = null;
                        try { stamp = obj.Attributes.GetUserString(StableIdKey); } catch { }

                        if (string.IsNullOrWhiteSpace(stamp))
                        {
                            continue;
                        }

                        if (!seen.ContainsKey(stamp))
                        {
                            seen.Add(stamp, obj.Id);
                            continue;
                        }

                        if (seen[stamp] == obj.Id)
                        {
                            // the keeper itself, met again on pass 2
                            continue;
                        }

                        if (pass == 0)
                        {
                            // solid-vs-solid duplicates resolve on pass 2 in
                            // document order, like every other duplicate
                            continue;
                        }

                        try
                        {
                            obj.Attributes.SetUserString(StableIdKey, obj.Id.ToString());

                            if (obj.CommitChanges())
                            {
                                restampedCount++;

                                Logger.Warn("Checkup: objects " + seen[stamp] + " and " + obj.Id + " shared stable id '" +
                                    stamp + "' (fan-out or copy-paste); " + obj.Id + " was re-stamped with its own id.");
                            }
                            else
                            {
                                Logger.Warn("Checkup: re-stamping duplicate stable id '" + stamp + "' on object " +
                                    obj.Id + " did not commit - the IFC export will fall back to its object id.");
                            }
                        }
                        catch (Exception ex)
                        {
                            Logger.Warn("Checkup: could not re-stamp duplicate stable id '" + stamp + "' on object " +
                                obj.Id + " - the IFC export will fall back to its object id. (" + ex.Message + ")");
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Logger.Warn("Checkup: duplicate stable-id pass failed - " + ex.Message);
            }

            return restampedCount;
        }

        /// <summary>
        /// True for geometry the checkup would count as a verified solid:
        /// closed Breps and Extrusions, closed Meshes. Block instances and
        /// open shells return false. Never throws.
        /// </summary>
        static bool IsClosedSolidGeometry(RhinoObject obj)
        {
            try
            {
                Brep brep = obj.Geometry as Brep;
                if (brep != null)
                {
                    return brep.IsSolid;
                }

                Extrusion extrusion = obj.Geometry as Extrusion;
                if (extrusion != null)
                {
                    return extrusion.IsSolid;
                }

                Mesh mesh = obj.Geometry as Mesh;
                if (mesh != null)
                {
                    return mesh.IsClosed;
                }
            }
            catch
            {
            }

            return false;
        }

        /// <summary>
        /// True when the object's layer exists and is locked; null-safe against
        /// orphaned layer indices.
        /// </summary>
        internal static bool IsLayerLocked(ObjectAttributes attributes)
        {
            Layer layer = RunQTO.doc.Layers.FindIndex(attributes.LayerIndex);

            return layer != null && layer.IsLocked;
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
        /// dropped, matching how top-level unsupported objects are handled. An
        /// instance with no solid-type pieces at all returns true having staged
        /// nothing; the caller detects the empty staging and keeps the original
        /// instead of deleting an object nothing would replace.
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
        /// joining). Returns false when the geometry could not be converted or is
        /// not takeoff geometry, in which case the caller must keep the original object.
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
                // Defensive path only: the checkup pre-filters non-takeoff objects
                // (curves, points, annotations, ...) before calling PrepareObject.
                // Returning false makes the caller keep the original, so nothing
                // can ever silently delete geometry from here.
                Logger.Warn("Checkup: object " + inputObj.Id + " of type " + objType + " on layer '" +
                    LayerPathOf(_mainObjectAttributes) + "' is not takeoff geometry; it was left unchecked.");

                return false;
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

        /// <summary>
        /// True when the object is invisible - hidden object mode, or any
        /// layer in its ancestry turned off. These objects never go through
        /// the checkup (the default enumerator skips them), so the take-off
        /// must exclude them everywhere with the same test.
        /// </summary>
        public static bool IsHiddenFromTakeoff(RhinoDoc doc, RhinoObject obj)
        {
            if (obj == null || obj.Attributes == null)
            {
                return false;
            }

            if (!obj.Attributes.Visible)
            {
                return true;
            }

            Layer layer = doc.Layers[obj.Attributes.LayerIndex];
            int guard = 0;

            while (layer != null && guard++ < 64)
            {
                if (!layer.IsVisible)
                {
                    return true;
                }

                if (layer.ParentLayerId == Guid.Empty)
                {
                    break;
                }

                layer = doc.Layers.FindId(layer.ParentLayerId);
            }

            return false;
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

        // Areas and lengths report in the ft-based units the takeoff has
        // always used for feet models: inch models convert (in^2 -> ft^2,
        // in -> ft), feet models are already there, and any other unit
        // system passes through unconverted - the same convention the
        // volume factor above follows.
        public static double SetAreaConversionFactor(string modelUnit)
        {
            return modelUnit == "in" ? 1.0 / 144.0 : 1;
        }

        public static double SetLengthConversionFactor(string modelUnit)
        {
            return modelUnit == "in" ? 1.0 / 12.0 : 1;
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

                    // Re-runs restart objectIndex at 0 while the first run's
                    // definitions persist; a colliding name makes
                    // InstanceDefinitions.Add return -1 and the object stays
                    // loose. Bump past existing definitions instead.
                    while (RunQTO.doc.InstanceDefinitions.Find(blockObjectName) != null)
                    {
                        objectIndex++;
                        blockObjectName = LayerParentsPath(layer) + layer.Name + "_" + objectIndex.ToString();
                    }

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

                    // CreatePlanarBreps returns null for a non-planar loop
                    // (a penetration through a curved ramp slab): keep that
                    // hole as-is instead of the NRE dropping the whole
                    // element from the take-off as "bad geometry".
                    Brep[] innerLoopBreps = Brep.CreatePlanarBreps(innerLoopCurve, RunQTO.doc.ModelAbsoluteTolerance);
                    if (innerLoopBreps == null || innerLoopBreps.Length == 0)
                    {
                        Logger.Warn("Gross volume: an inner loop is not planar; " +
                            "that opening was kept as-is.");
                        continue;
                    }
                    Surface innerLoopSurface = innerLoopBreps[0].Surfaces[0];

                    Point3d centroid = innerLoopSurface.PointAt(
                        innerLoopSurface.Domain(0).Min + (innerLoopSurface.Domain(0).Max - innerLoopSurface.Domain(0).Min) * 0.02,
                        innerLoopSurface.Domain(1).Min + (innerLoopSurface.Domain(1).Max - innerLoopSurface.Domain(1).Min) * 0.02);

                    Vector3d normal = innerLoopSurface.NormalAt(innerLoopSurface.Domain(0).Mid, innerLoopSurface.Domain(1).Mid);
                    normal.Unitize();

                    double ExtentAlongNormal(BoundingBox box)
                    {
                        double min = double.MaxValue;
                        double max = double.MinValue;
                        foreach (Point3d corner in box.GetCorners())
                        {
                            double t = (corner - centroid) * normal;
                            if (t < min) { min = t; }
                            if (t > max) { max = t; }
                        }
                        return max - min;
                    }

                    // Bound the probe by the hole's own wall depth: a blind
                    // recess's floor lies within it, while an overhanging
                    // plate of the SAME solid (split-level slab, landing
                    // plate over a stair opening) lies a story away - an
                    // unbounded ray would classify that real through-opening
                    // as a recess and silently degrade gross to net.
                    double probeDepth = 0.0;
                    foreach (BrepTrim trim in loop.Trims)
                    {
                        if (trim.Edge == null)
                        {
                            continue;
                        }
                        foreach (int faceIndex in trim.Edge.AdjacentFaces())
                        {
                            if (faceIndex == loop.Face.FaceIndex)
                            {
                                continue;
                            }
                            probeDepth = Math.Max(probeDepth,
                                ExtentAlongNormal(brep.Faces[faceIndex].GetBoundingBox(true)));
                        }
                    }
                    if (probeDepth <= 0.0)
                    {
                        // no wall face resolved: the solid's own extent along
                        // the normal (the flat-slab thickness) is the bound
                        probeDepth = ExtentAlongNormal(brep.GetBoundingBox(true));
                    }
                    probeDepth += 10.0 * RunQTO.doc.ModelAbsoluteTolerance;

                    // Probe BOTH directions: the planar cap's normal follows
                    // the loop's curve direction, so a one-sided test made
                    // the opening-vs-recess verdict depend on modeling
                    // direction (the same geometry mirrored gave a different
                    // gross volume). A loop counts as a fillable
                    // through-opening only when BOTH rays verifiably miss
                    // the solid - an intersector failure keeps the hole
                    // (filling on failure would be the aggressive branch).
                    bool hitsSolid = false;
                    bool probeFailed = false;
                    foreach (Vector3d direction in new Vector3d[] { normal * probeDepth, normal * -probeDepth })
                    {
                        LineCurve probe = new LineCurve(new Line(centroid, direction));
                        Curve[] overlapCurves;
                        Point3d[] intersectionPoints;
                        bool intersect = Rhino.Geometry.Intersect.Intersection.CurveBrep(
                            probe, brep, RunQTO.doc.ModelAbsoluteTolerance,
                            out overlapCurves, out intersectionPoints);
                        if (!intersect)
                        {
                            probeFailed = true;
                            continue;
                        }
                        if (intersectionPoints.Length > 0 ||
                            (overlapCurves != null && overlapCurves.Length > 0))
                        {
                            hitsSolid = true;
                            break;
                        }
                    }

                    if (hitsSolid)
                    {
                        continue;
                    }
                    if (probeFailed)
                    {
                        Logger.Warn("Gross volume: the opening probe failed; " +
                            "that opening was kept as-is.");
                        continue;
                    }
                    brepInnerLoopsToRemove.Add(loop);
                    brepInnerLoopsToRemoveIndices.Add(loop.ComponentIndex());
                }
            }

            if (brepInnerLoopsToRemoveIndices.Count > 0)
            {
                Brep newBrep = brep.RemoveHoles(brepInnerLoopsToRemoveIndices, RunQTO.doc.ModelAbsoluteTolerance);

                // RemoveHoles returns null on failure: gross degrades to net
                // instead of the element dropping out of the export.
                if (newBrep == null)
                {
                    Logger.Warn("Gross volume: RemoveHoles failed; the net " +
                        "volume is used as gross for this element.");
                    return brep.GetVolume();
                }

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
