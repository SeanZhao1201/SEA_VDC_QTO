using System;
using System.Collections.Generic;
using System.IO;
using Newtonsoft.Json.Linq;
using Rhino;
using Rhino.Display;
using Rhino.Geometry;

namespace QTO_Tool
{
    /// <summary>
    /// Read-only display conduit for the ACTIVE breaks JSON (break sheet
    /// P3): break polylines and numbered pour dots are drawn INTO the
    /// viewports of the live model without adding a single object to the
    /// model file - REVERT CHECKUP is unaffected, the checkup has nothing
    /// to delete, and there is deliberately no in-model editing (the sheet
    /// and the _POURBREAK layer stay the authoring surfaces feeding the
    /// same JSON). One static instance, plugin-wide: reopening FormworkUI
    /// must toggle the SAME conduit, never stack a second one.
    /// </summary>
    class PourBreakOverlay : DisplayConduit
    {
        public static readonly PourBreakOverlay Instance = new PourBreakOverlay();

        static PourBreakOverlay()
        {
            // Conduit registration is APPLICATION-wide and Enabled survives
            // File > Open: without this hook, model A's breaks keep drawing
            // into model B's viewports (and A's bounds keep inflating B's
            // Zoom Extents) until someone finds the checkbox. CloseDocument
            // fires for both File > Open and File > New. No redraw here -
            // the document is going away.
            RhinoDoc.CloseDocument += (s, e) =>
            {
                Instance.Enabled = false;
                Instance.ClearPrimitives();
            };
        }

        // the restore path draws its curves in this exact magenta - the
        // overlay showing the same breaks must read as the same thing
        static readonly System.Drawing.Color BreakColor =
            System.Drawing.Color.FromArgb(255, 0, 144);
        const int BreakThicknessPx = 3;

        readonly List<Polyline> breakLines = new List<Polyline>();
        readonly List<KeyValuePair<Point3d, string>> pourDots =
            new List<KeyValuePair<Point3d, string>>();
        BoundingBox bounds = BoundingBox.Empty;

        public string Summary { get; private set; }

        PourBreakOverlay()
        {
            this.Summary = "";
        }

        /// <summary>Parse the active breaks JSON into drawable primitives.
        /// False (with a reason) when there is nothing safe to draw - no
        /// JSON, unreadable, or a JSON from a DIFFERENT model file (the
        /// staging folder is machine-wide; overlaying another project's
        /// breaks on this model would be quietly wrong in the worst way).
        /// A failure leaves the previous primitives cleared.</summary>
        public bool Rebuild(RhinoDoc doc, out string reason)
        {
            ClearPrimitives();

            string jsonPath = Path.Combine(FormworkMethods.StagingDir,
                FormworkMethods.BreaksJsonFile);
            if (!File.Exists(jsonPath))
            {
                reason = "no breaks JSON yet - run HARVEST or IMPORT SHEET first.";
                return false;
            }

            // The whole parse is guarded, not just JObject.Parse: a
            // syntactically valid but shape-malformed JSON (object points,
            // scalar floors) must land in the callers' false-branch, never
            // escape as an unhandled dispatcher exception - and never leave
            // a PARTIAL primitive set drawn under a checked toggle.
            try
            {
                return RebuildCore(doc, jsonPath, out reason);
            }
            catch (Exception ex)
            {
                // an empty primitive set draws nothing even if a redraw
                // slips in before the caller unchecks the toggle
                ClearPrimitives();
                Logger.Error("Overlay rebuild failed on a malformed breaks JSON.", ex);
                reason = "breaks JSON malformed (" + ex.Message + ").";
                return false;
            }
        }

        bool RebuildCore(RhinoDoc doc, string jsonPath, out string reason)
        {
            JObject data;
            try
            {
                data = JObject.Parse(File.ReadAllText(jsonPath));
            }
            catch (Exception ex)
            {
                reason = "breaks JSON unreadable (" + ex.Message + ").";
                return false;
            }

            string foreignNote = "";
            string source = (string)data.SelectToken("source.model") ?? "";
            string docPath = (doc == null ? "" : doc.Path) ?? "";
            if (source.Length > 0 && docPath.Length > 0 &&
                !string.Equals(source, docPath, StringComparison.OrdinalIgnoreCase))
            {
                // A foreign JSON the user explicitly confirmed via LOAD
                // OPTION is drawable: the active-option note is docPath-
                // guarded for THIS model and its sha still matching means
                // the active JSON is byte-for-byte the one they OK'd. A
                // plain stale-staging leftover has no such note and stays
                // refused.
                bool modified;
                string confirmedOption = FormworkMethods.ReadActiveOption(doc, out modified);
                if (confirmedOption.Length == 0 || modified)
                {
                    reason = "the breaks JSON came from a DIFFERENT model file (" +
                        source + ") - not overlaying it here.";
                    return false;
                }
                foreignNote = " [scheme '" + confirmedOption +
                    "' loaded from another model file]";
            }

            // floor name -> elevation, for v1-upconverted entries whose z is
            // null (same fallback restore uses; duplicate names keep the
            // first match, mirroring pourbreak_restore._floor_elevation)
            Dictionary<string, double> floorElevations =
                new Dictionary<string, double>(StringComparer.OrdinalIgnoreCase);
            foreach (KeyValuePair<double, string> kv in
                Methods.RetrieveDictionaryFromDocumentStrings())
            {
                if (!floorElevations.ContainsKey(kv.Value))
                {
                    floorElevations.Add(kv.Value, kv.Key);
                }
            }

            // breaks sit ON slab tops: a small lift keeps the overlay from
            // z-fighting the surface it annotates
            double lift = 0.02 * RhinoMath.UnitScale(UnitSystem.Meters,
                doc == null ? UnitSystem.Meters : doc.ModelUnitSystem);

            int nBreaks = 0;
            int nDots = 0;
            int nSkipped = 0;
            int nFloors = 0;

            JObject floors = data["floors"] as JObject;
            if (floors != null)
            {
                foreach (KeyValuePair<string, JToken> floor in floors)
                {
                    bool floorUsed = false;

                    double? ResolveZ(JToken entry)
                    {
                        double? z = (double?)entry["z"];
                        if (z != null)
                        {
                            return z;
                        }
                        double elevation;
                        if (floorElevations.TryGetValue(floor.Key, out elevation))
                        {
                            return elevation;
                        }
                        return null;
                    }

                    foreach (JToken brk in floor.Value["breaks"] as JArray ?? new JArray())
                    {
                        double? z = ResolveZ(brk);
                        JArray points = brk["polyline"] as JArray;
                        if (z == null || points == null || points.Count < 2)
                        {
                            nSkipped++;
                            continue;
                        }
                        Polyline line = new Polyline();
                        foreach (JToken pt in points)
                        {
                            line.Add((double)pt[0], (double)pt[1], z.Value + lift);
                        }
                        this.breakLines.Add(line);
                        this.bounds.Union(line.BoundingBox);
                        nBreaks++;
                        floorUsed = true;
                    }

                    foreach (JToken marker in floor.Value["pour_markers"] as JArray ?? new JArray())
                    {
                        double? z = ResolveZ(marker);
                        JArray at = marker["at"] as JArray;
                        if (z == null || at == null || at.Count < 2)
                        {
                            nSkipped++;
                            continue;
                        }
                        Point3d point = new Point3d((double)at[0], (double)at[1],
                            z.Value + lift);
                        this.pourDots.Add(new KeyValuePair<Point3d, string>(
                            point, marker["pour"] == null ? "?" : marker["pour"].ToString()));
                        this.bounds.Union(point);
                        nDots++;
                        floorUsed = true;
                    }

                    if (floorUsed)
                    {
                        nFloors++;
                    }
                }
            }

            if (nBreaks == 0 && nDots == 0)
            {
                reason = "the breaks JSON holds nothing drawable" +
                    (nSkipped > 0 ? " (" + nSkipped + " item(s) skipped - " +
                     "floors missing from this model file's floor table?)" : "") + ".";
                return false;
            }

            this.Summary = nBreaks + " break(s) + " + nDots + " pour dot(s) on " +
                nFloors + " floor(s)" +
                (nSkipped > 0 ? "; " + nSkipped + " item(s) SKIPPED (floor not in " +
                 "this model file's floor table)" : "") + foreignNote;
            reason = "";
            return true;
        }

        /// <summary>Drop every primitive - Enabled is NOT touched here:
        /// Rebuild runs under a still-enabled conduit on the refresh path,
        /// and an empty set simply draws nothing.</summary>
        void ClearPrimitives()
        {
            this.breakLines.Clear();
            this.pourDots.Clear();
            this.bounds = BoundingBox.Empty;
            this.Summary = "";
        }

        public void TurnOff(RhinoDoc doc)
        {
            if (this.Enabled)
            {
                this.Enabled = false;
                if (doc != null)
                {
                    doc.Views.Redraw();
                }
            }
        }

        // Without this the overlay gets clipped by the camera's frustum
        // fitted to the model's real objects (worst on Zoom Extents in an
        // emptied model).
        protected override void CalculateBoundingBox(CalculateBoundingBoxEventArgs e)
        {
            base.CalculateBoundingBox(e);
            if (this.bounds.IsValid)
            {
                e.IncludeBoundingBox(this.bounds);
            }
        }

        protected override void PostDrawObjects(DrawEventArgs e)
        {
            base.PostDrawObjects(e);
            foreach (Polyline line in this.breakLines)
            {
                e.Display.DrawPolyline(line, BreakColor, BreakThicknessPx);
            }
            foreach (KeyValuePair<Point3d, string> dot in this.pourDots)
            {
                e.Display.DrawDot(dot.Key, dot.Value);
            }
        }
    }
}
