using System;
using System.Collections.Generic;
using System.IO;
using Newtonsoft.Json;
using Rhino;
using Rhino.Commands;
using Rhino.DocObjects;

namespace QTO_Tool
{
    /// <summary>
    /// Headless worker behind PREVIEW CHECKUP: runs the REAL checkup on
    /// the active document (a staged WriteFile copy - launched by
    /// FormworkMethods.RunChildRhinoCommand) and writes a JSON report of
    /// what happened, framed by object inventories taken before and after.
    /// Running the actual checkup on a throwaway copy is the point: the
    /// preview can never drift from what Start Checkup would really do,
    /// because it IS Start Checkup - one process boundary away.
    /// </summary>
    public class QTOCheckupReport : Command
    {
        public QTOCheckupReport()
        {
            Instance = this;
        }

        public static QTOCheckupReport Instance
        {
            get; private set;
        }

        public override string EnglishName
        {
            get { return "QTOCheckupReport"; }
        }

        protected override Result RunCommand(RhinoDoc doc, RunMode mode)
        {
            string reportPath = Environment.GetEnvironmentVariable("QTO_CHECKUP_REPORT");
            if (string.IsNullOrEmpty(reportPath))
            {
                // Typed at the command line this would run the DESTRUCTIVE
                // checkup on the live document with none of QTOUI's guards.
                // This command is the headless worker behind PREVIEW
                // CHECKUP and refuses to run any other way.
                RhinoApp.WriteLine("QTOCheckupReport is the headless worker behind " +
                    "the QTO window's PREVIEW CHECKUP button and only runs from " +
                    "there (on a staged copy). Nothing was changed.");
                return Result.Cancel;
            }

            Result result = Result.Success;
            try
            {
                RunQTO.doc = doc;
                string modelUnit = doc.GetUnitSystemName(true, true, true, true);
                RunQTO.volumeConversionFactor = Methods.SetVolumeConversionFactor(modelUnit);
                RunQTO.areaConversionFactor = Methods.SetAreaConversionFactor(modelUnit);
                RunQTO.lengthConversionFactor = Methods.SetLengthConversionFactor(modelUnit);
                Logger.StartSession();
                Logger.Info("Checkup preview report on: " + (doc.Path ?? "<unsaved>"));

                Dictionary<string, int> before = Inventory(doc);
                uint undoRecordSerial;
                string summary = Methods.ConcreteModelSetup(out undoRecordSerial);
                Dictionary<string, int> after = Inventory(doc);

                Dictionary<string, object> report = new Dictionary<string, object>
                {
                    { "summary", summary },
                    { "before", before },
                    { "after", after },
                    { "written", DateTime.UtcNow.ToString("o") },
                };
                File.WriteAllText(reportPath,
                    JsonConvert.SerializeObject(report, Formatting.Indented));
            }
            catch (Exception ex)
            {
                Logger.Error("Checkup preview report failed.", ex);
                try
                {
                    File.WriteAllText(reportPath, JsonConvert.SerializeObject(
                        new Dictionary<string, object> { { "error", ex.ToString() } }));
                }
                catch { }
                result = Result.Failure;
            }
            finally
            {
                try { doc.Modified = false; } catch { }
                try { RhinoApp.Exit(); } catch { }
            }
            return result;
        }

        /// <summary>Counts by object class - the delta across the checkup
        /// is exactly what the checkup destroys and never re-adds (curves,
        /// text dots, annotations...). _POURBREAK curves counted separately:
        /// they are the user's freshest work.</summary>
        static Dictionary<string, int> Inventory(RhinoDoc doc)
        {
            // Instances and meshes are TAKE-OFF geometry: the checkup
            // explodes/rebuilds them into solids. Counting them as "other"
            // would make the preview report a Blockified model as entirely
            // about-to-be-deleted.
            int solids = 0, instances = 0, meshes = 0, curves = 0,
                pourbreakCurves = 0, dots = 0, other = 0, total = 0;
            ObjectEnumeratorSettings es = new ObjectEnumeratorSettings
            {
                NormalObjects = true,
                LockedObjects = true,
                HiddenObjects = true,
            };
            foreach (RhinoObject obj in doc.Objects.GetObjectList(es))
            {
                if (obj == null || obj.Attributes == null) { continue; }
                total += 1;
                Rhino.Geometry.GeometryBase geom = obj.Geometry;
                if (geom is Rhino.Geometry.Brep || geom is Rhino.Geometry.Extrusion)
                {
                    solids += 1;
                }
                else if (geom is Rhino.Geometry.InstanceReferenceGeometry)
                {
                    instances += 1;
                }
                else if (geom is Rhino.Geometry.Mesh)
                {
                    meshes += 1;
                }
                else if (geom is Rhino.Geometry.Curve)
                {
                    curves += 1;
                    Layer layer = doc.Layers[obj.Attributes.LayerIndex];
                    string path = layer == null ? "" : (layer.FullPath ?? "");
                    if (path.StartsWith(FormworkMethods.PourBreakLayer,
                        StringComparison.OrdinalIgnoreCase))
                    {
                        pourbreakCurves += 1;
                    }
                }
                else if (geom is Rhino.Geometry.TextDot)
                {
                    dots += 1;
                }
                else
                {
                    other += 1;
                }
            }
            return new Dictionary<string, int>
            {
                { "total", total }, { "solids", solids },
                { "instances", instances }, { "meshes", meshes },
                { "curves", curves }, { "pourbreakCurves", pourbreakCurves },
                { "dots", dots }, { "other", other },
            };
        }
    }
}
