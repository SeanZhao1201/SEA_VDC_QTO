using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using Newtonsoft.Json;
using Rhino;
using Rhino.DocObjects;

namespace QTO_Tool
{
    enum FormworkStampStatus { Fresh, Unsaved, Missing, FloorsNeverSet, FloorsChanged, GeometryChanged }

    /// <summary>
    /// Everything the formwork sidecar needs from the plugin side: the
    /// freshness stamp written after Calculate, the staging folder shared
    /// with the headless dev loop, embedded-script extraction, and the
    /// child-Rhino launcher with a watchdog. The Python engines themselves
    /// live in Formwork_Generation/rhino and are embedded as linked
    /// resources - one source of truth, no forked copies.
    /// </summary>
    class FormworkMethods
    {
        public const string StampKey = "FormworkStamp";
        public const string FormworkLayer = "_FORMWORK";
        public const string PourBreakLayer = "_POURBREAK";

        /// <summary>The strict TREE matcher: the layer IS the root or is
        /// nested under it. Mirrors pourbreak_harvest.is_pb_layer, so C# and
        /// the Python engines agree on what the _FORMWORK/_POURBREAK trees
        /// are - a sibling like "_POURBREAK_OLD" is NOT in the tree (a raw
        /// prefix test wrongly excluded its real take-off solids from the
        /// freshness fingerprint and promised harvest coverage the strict
        /// Python walker never delivers).</summary>
        public static bool LayerInTree(string fullPath, string root)
        {
            string path = fullPath ?? "";
            return path.Equals(root, StringComparison.OrdinalIgnoreCase) ||
                path.StartsWith(root + "::", StringComparison.OrdinalIgnoreCase);
        }

        /// <summary>The broad matcher for the destructive-checkup safety
        /// gate: ANY path segment equals the root, so a "_FORMWORK" layer
        /// dragged under a parent ("TEMP::_FORMWORK") still trips the gate.
        /// Over-matching is the safe direction there - the checkup deletes
        /// every object in the model file.</summary>
        public static bool LayerHasSegment(string fullPath, string root)
        {
            foreach (string segment in (fullPath ?? "").Split(
                new string[] { "::" }, StringSplitOptions.None))
            {
                if (segment.Equals(root, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
            return false;
        }

        // The staging contract shared with Formwork_Generation/rhino: the
        // scripts hardcode %LOCALAPPDATA%\qto_fw_test as their import root
        // and output folder, so the GUI stages there too. Space-free by
        // requirement of the /runscript argument.
        public static string StagingDir
        {
            get
            {
                string dir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "qto_fw_test");
                Directory.CreateDirectory(dir);
                return dir;
            }
        }

        /// <summary>The space-free requirement belongs ONLY to the Python
        /// script launches: their path rides bare inside the /runscript
        /// argument. Command launches (preview checkup) and quoted model
        /// paths are fine with spaces, so the check must not block them.
        /// No fallback dir: the engines HARDCODE this staging root as their
        /// import root, any other location silently breaks imports.</summary>
        public static void EnsureScriptSafeStaging()
        {
            if (StagingDir.Contains(" "))
            {
                throw new InvalidOperationException(
                    "The staging folder path contains a space (" + StagingDir + ") - " +
                    "the /runscript launch cannot parse a script path there. " +
                    "Formwork generation is unavailable on this account.");
            }
        }

        // One child at a time, plugin-wide: a second FormworkUI (the command
        // closes and reopens the window) must not race a running child over
        // the shared staging files.
        static readonly object childLock = new object();
        static bool childRunning;

        /// <summary>True while a child Rhino launched by this plugin is
        /// running (the gate is shared with QTOUI's preview checkup). Exposed
        /// so callers can refuse a run BEFORE their pre-launch side effects -
        /// deleting the derived-model sidecar, overwriting model.3dm - which
        /// RunChildCore's own refusal would come too late to protect.</summary>
        public static bool ChildRunning
        {
            get
            {
                lock (childLock)
                {
                    return childRunning;
                }
            }
        }

        // Logical resource names must match the csproj LogicalName entries.
        static readonly string[] Scripts = new string[]
        {
            "formwork_gen_rhino.py",
            "sideform_gen_rhino.py",
            "pourbreak_harvest.py",
            "pourbreak_restore.py",
            "split_pourbreaks.py",
            "run_on_model.py",
            "run_sideforms_on_model.py",
            "jumpform_gen_rhino.py",
            "run_jumpform_on_model.py",
            "breaksheet_gen.py",
            "breaksheet_import.py",
        };

        /// <summary>Extract the embedded Python engines into the staging
        /// folder (overwrite - the plugin build is the source of truth),
        /// plus two generated in-process drivers with the breaks-JSON path
        /// BAKED IN: IronPython's os.environ can be a snapshot from the
        /// engine's first start, so environment variables set later by the
        /// plugin are not reliably visible to _-RunPythonScript runs.</summary>
        public static void ExtractScripts()
        {
            EnsureScriptSafeStaging();
            Assembly asm = Assembly.GetExecutingAssembly();
            foreach (string name in Scripts)
            {
                using (Stream src = asm.GetManifestResourceStream("QTO_Tool.Formwork." + name))
                {
                    if (src == null)
                    {
                        throw new InvalidOperationException(
                            "Embedded formwork script missing from the build: " + name);
                    }
                    using (FileStream dst = File.Create(Path.Combine(StagingDir, name)))
                    {
                        src.CopyTo(dst);
                    }
                }
            }
            string breaksJson = Path.Combine(StagingDir, BreaksJsonFile);
            WriteDriver("pb_gui_harvest.py", "pourbreak_harvest", breaksJson);
            WriteDriver("pb_gui_restore.py", "pourbreak_restore", breaksJson);
            // The break-sheet pair runs in-process too (File3dm file I/O
            // only - no child Rhino, no window flash, live model untouched);
            // BS_SHEET/BS_META default to the fixed staging names inside the
            // modules themselves.
            WriteDriver("bs_gui_make.py", "breaksheet_gen", breaksJson);
            WriteDriver("bs_gui_import.py", "breaksheet_import", breaksJson);
        }

        static void WriteDriver(string fileName, string module, string breaksJson)
        {
            // The coding line is load-bearing: the baked staging path contains
            // the Windows user name, File.WriteAllText emits BOM-less UTF-8,
            // and IronPython 2 rejects non-ASCII bytes in an undeclared source
            // file with a SyntaxError - so without it, any accented or CJK
            // account name kills Harvest/Restore before main() runs.
            string body =
                "# -*- coding: utf-8 -*-\r\n" +
                "# generated by QTO_Tool - in-process driver, do not edit\r\n" +
                "import os\r\n" +
                "import sys\r\n" +
                "STAGE = r\"" + StagingDir + "\"\r\n" +
                "if STAGE not in sys.path:\r\n" +
                "    sys.path.insert(0, STAGE)\r\n" +
                "os.environ[\"PB_JSON\"] = r\"" + breaksJson + "\"\r\n" +
                "import " + module + "\r\n" +
                module + ".main()\r\n";
            File.WriteAllText(Path.Combine(StagingDir, fileName), body);
        }

        /// <summary>
        /// Geometry fingerprint of the QTO subject matter: solid Breps and
        /// Extrusions outside the _FORMWORK/_POURBREAK trees. Deliberately
        /// blind to curves and text dots - authoring pour breaks AFTER
        /// Calculate is the designed workflow and must not sour the stamp.
        /// </summary>
        public static void ComputeFingerprint(RhinoDoc doc, out int count, out long hash)
        {
            count = 0;
            unchecked
            {
                hash = 17;
                foreach (RhinoObject obj in doc.Objects)
                {
                    if (obj == null || obj.Attributes == null) { continue; }
                    Layer layer = doc.Layers[obj.Attributes.LayerIndex];
                    string path = layer == null ? "" : (layer.FullPath ?? "");
                    if (LayerInTree(path, FormworkLayer) ||
                        LayerInTree(path, PourBreakLayer))
                    {
                        continue;
                    }
                    Rhino.Geometry.GeometryBase geom = obj.Geometry;
                    if (!(geom is Rhino.Geometry.Brep) && !(geom is Rhino.Geometry.Extrusion))
                    {
                        continue;
                    }
                    Rhino.Geometry.BoundingBox bb = geom.GetBoundingBox(false);
                    hash = hash * 31 + Quantize(bb.Min.X) ;
                    hash = hash * 31 + Quantize(bb.Min.Y);
                    hash = hash * 31 + Quantize(bb.Min.Z);
                    hash = hash * 31 + Quantize(bb.Max.X);
                    hash = hash * 31 + Quantize(bb.Max.Y);
                    hash = hash * 31 + Quantize(bb.Max.Z);
                    count += 1;
                }
            }
        }

        static long Quantize(double v)
        {
            return (long)Math.Round(v * 1000.0);
        }

        // Invariant culture: the stamp travels inside the .3dm across
        // machines; a comma-decimal locale must not sour every comparison.
        static string FloorsJson(Dictionary<double, string> floors)
        {
            return JsonConvert.SerializeObject(
                floors.OrderBy(kv => kv.Key)
                      .ToDictionary(
                          kv => kv.Key.ToString("F6",
                              System.Globalization.CultureInfo.InvariantCulture),
                          kv => kv.Value));
        }

        /// <summary>Write the freshness stamp - called from the Calculate
        /// success path, the ONE site that certifies the take-off state.</summary>
        public static void WriteStamp(RhinoDoc doc)
        {
            int count;
            long hash;
            ComputeFingerprint(doc, out count, out hash);
            Dictionary<string, object> stamp = new Dictionary<string, object>
            {
                { "geomCount", count },
                { "geomHash", hash },
                { "floorCount", ElevationInput.floorElevations.Count },
                { "floorsJson", FloorsJson(ElevationInput.floorElevations) },
                { "docPath", doc.Path ?? "" },
                { "written", DateTime.UtcNow.ToString("o") },
            };
            doc.Strings.SetString(StampKey, JsonConvert.SerializeObject(stamp));
        }

        /// <summary>
        /// Compare the document against the stamp. The floor count is
        /// validated SEPARATELY: a model whose floors were never set
        /// fingerprints as a perfect match while the whole take-off sits in
        /// the "-" bucket - a green light there would actively mislead.
        /// </summary>
        public static FormworkStampStatus CheckStamp(RhinoDoc doc, out string reason)
        {
            string raw = doc.Strings.GetValue(StampKey);
            if (string.IsNullOrEmpty(raw))
            {
                reason = "No take-off stamp found. Run the QTO Calculate step first.";
                return FormworkStampStatus.Missing;
            }
            Dictionary<string, object> stamp;
            try
            {
                stamp = JsonConvert.DeserializeObject<Dictionary<string, object>>(raw);
            }
            catch
            {
                reason = "The take-off stamp is unreadable. Re-run Calculate.";
                return FormworkStampStatus.Missing;
            }

            Dictionary<double, string> floors = Methods.RetrieveDictionaryFromDocumentStrings();
            long stampFloorCount = Convert.ToInt64(stamp["floorCount"]);
            if (stampFloorCount == 0 || floors.Count == 0)
            {
                reason = "Floors were never set - the whole take-off sits in the \"-\" bucket. " +
                    "Run Set Floor and Calculate before generating formwork.";
                return FormworkStampStatus.FloorsNeverSet;
            }
            string floorsNow = FloorsJson(floors);
            if ((string)stamp["floorsJson"] != floorsNow)
            {
                reason = "The floor table changed after Calculate. Re-run Calculate so the " +
                    "take-off and the formwork agree on floors.";
                return FormworkStampStatus.FloorsChanged;
            }
            int count;
            long hash;
            ComputeFingerprint(doc, out count, out hash);
            if (Convert.ToInt64(stamp["geomCount"]) != count ||
                Convert.ToInt64(stamp["geomHash"]) != hash)
            {
                reason = "The model geometry changed after Calculate (" +
                    stamp["geomCount"] + " -> " + count + " solids). Re-run Calculate.";
                return FormworkStampStatus.GeometryChanged;
            }
            if (doc.Modified)
            {
                // "model file", not "document" - VDC vocabulary (2026-08-17).
                reason = "Take-off is current, but the model file has unsaved changes.";
                return FormworkStampStatus.Unsaved;
            }
            reason = "Take-off is current (" + count + " solids, " + floors.Count + " floors).";
            return FormworkStampStatus.Fresh;
        }

        // Sidecar tying the staged derived model to the document state it was
        // split from. The staging folder is machine-wide with one fixed file
        // name per artifact, so without this a stale derived model - or one
        // from a different project - would pass a File.Exists check under a
        // green freshness stamp.
        public const string DerivedModelMetaFile = "model_pourbreaks.meta.json";
        public const string BreaksJsonFile = "pour_breaks_model.json";
        public const string SheetFileName = "breaksheet.3dm";
        public const string SheetMetaFileName = "breaksheet.meta.json";
        public const string SheetMergeFile = "breaksheet_merge.json";

        /// <summary>Persist the TYP MERGE dialog's picks for breaksheet_gen.
        /// The docPath lets the generator refuse a directive file another
        /// project left behind in the machine-wide staging folder.</summary>
        public static void WriteSheetMergeFile(RhinoDoc doc, List<List<string>> sets)
        {
            Dictionary<string, object> payload = new Dictionary<string, object>
            {
                { "docPath", doc.Path ?? "" },
                { "merge", sets ?? new List<List<string>>() },
            };
            File.WriteAllText(Path.Combine(StagingDir, SheetMergeFile),
                JsonConvert.SerializeObject(payload));
        }

        // ── named break-scheme options (break sheet P3) ────────────────────
        // A scheme is a snapshot of the breaks JSON. Options live NEXT TO
        // THE MODEL FILE (<model>_breaks.<name>.json) - the staging folder
        // is machine-wide with fixed names, and per-project schemes there
        // would cross-contaminate. The active-option note is a staging
        // sidecar (docPath + sha guarded) so it never dirties the document.
        public const string ActiveOptionFile = "breaks_active_option.json";

        static string OptionPrefix(RhinoDoc doc)
        {
            return Path.Combine(Path.GetDirectoryName(doc.Path),
                Path.GetFileNameWithoutExtension(doc.Path) + "_breaks.");
        }

        public static string OptionFilePath(RhinoDoc doc, string name)
        {
            return OptionPrefix(doc) + name + ".json";
        }

        /// <summary>The single source of truth for what an option NAME may
        /// be: trimmed, no invalid-filename characters, and no '.' - a dot
        /// would split the "&lt;model&gt;_breaks.&lt;name&gt;.json" pattern, so
        /// listing and saving must agree on rejecting it.</summary>
        public static string SanitizeOptionName(string name)
        {
            name = (name ?? "").Trim();
            foreach (char c in Path.GetInvalidFileNameChars())
            {
                name = name.Replace(c.ToString(), "");
            }
            return name.Replace(".", "");
        }

        /// <summary>Option names saved next to this model file, sorted.
        /// Closed under the save-side name grammar: a dotted "name" (a
        /// hand-renamed file, or ANOTHER model whose base name embeds this
        /// one's option prefix) is not listed - loading it would round-trip
        /// but SAVE would silently retarget a sanitized different file.</summary>
        public static List<string> ListOptionNames(RhinoDoc doc)
        {
            List<string> names = new List<string>();
            string prefix = OptionPrefix(doc);
            foreach (string file in Directory.GetFiles(
                Path.GetDirectoryName(doc.Path),
                Path.GetFileName(prefix) + "*.json"))
            {
                string tail = Path.GetFileName(file).Substring(
                    Path.GetFileName(prefix).Length);
                string name = tail.Substring(0, tail.Length - ".json".Length);
                if (name.Length > 0 && name == SanitizeOptionName(name))
                {
                    names.Add(name);
                }
            }
            names.Sort(StringComparer.OrdinalIgnoreCase);
            return names;
        }

        /// <summary>Record which option the active breaks JSON IS right now
        /// (at save/load time) - name plus the JSON's sha, so any later
        /// rewrite (harvest, import, another load) shows as "modified".</summary>
        public static void WriteActiveOption(RhinoDoc doc, string name)
        {
            Dictionary<string, object> payload = new Dictionary<string, object>
            {
                { "docPath", doc.Path ?? "" },
                { "name", name },
                { "sha", BreaksJsonSha() },
                { "written", DateTime.UtcNow.ToString("o") },
            };
            File.WriteAllText(Path.Combine(StagingDir, ActiveOptionFile),
                JsonConvert.SerializeObject(payload));
        }

        /// <summary>The active option's name, "" when none applies to THIS
        /// model file. modified = the breaks JSON changed since the note
        /// was written (the name is then a provenance hint, not an
        /// identity). Never throws - the sidecar is advisory display.</summary>
        public static string ReadActiveOption(RhinoDoc doc, out bool modified)
        {
            modified = false;
            try
            {
                string path = Path.Combine(StagingDir, ActiveOptionFile);
                if (!File.Exists(path))
                {
                    return "";
                }
                Dictionary<string, object> data =
                    JsonConvert.DeserializeObject<Dictionary<string, object>>(
                        File.ReadAllText(path));
                if (data == null || !data.ContainsKey("name"))
                {
                    return "";
                }
                string notePath = data.ContainsKey("docPath")
                    ? (data["docPath"] as string ?? "") : "";
                string docPath = (doc == null ? "" : doc.Path) ?? "";
                // STRICT equality, unlike the lenient both-non-empty pattern
                // elsewhere: the note writers refuse unsaved docs, so a real
                // note always carries a path - an empty-vs-non-empty pair
                // here means "someone else's note on my unsaved doc" (or
                // vice versa), never a legitimate flow.
                if (!string.Equals(notePath, docPath, StringComparison.OrdinalIgnoreCase))
                {
                    return "";
                }
                string sha = data.ContainsKey("sha") ? (data["sha"] as string ?? "") : "";
                modified = sha.Length > 0 && sha != BreaksJsonSha();
                return data["name"] as string ?? "";
            }
            catch (Exception ex)
            {
                Logger.Error("Active break-option note unreadable.", ex);
                return "";
            }
        }

        /// <summary>The sheet meta's docPath, "" when absent or unreadable.
        /// The machine-wide staging folder means the sheet on disk may be
        /// another project's - every consumer must check.</summary>
        public static string SheetMetaDocPath()
        {
            try
            {
                string meta = Path.Combine(StagingDir, SheetMetaFileName);
                if (!File.Exists(meta))
                {
                    return "";
                }
                Newtonsoft.Json.Linq.JObject data =
                    Newtonsoft.Json.Linq.JObject.Parse(File.ReadAllText(meta));
                return (string)data.SelectToken("docPath") ?? "";
            }
            catch (Exception ex)
            {
                Logger.Error("Sheet meta docPath probe failed.", ex);
                return "";
            }
        }

        static string BreaksJsonSourceKind()
        {
            try
            {
                string json = Path.Combine(StagingDir, BreaksJsonFile);
                Newtonsoft.Json.Linq.JObject data =
                    Newtonsoft.Json.Linq.JObject.Parse(File.ReadAllText(json));
                return (string)data.SelectToken("source.kind") ?? "";
            }
            catch
            {
                return "";
            }
        }

        /// <summary>True when the drawn sheet on disk is AHEAD of the breaks
        /// JSON: saved after MAKE wrote it (the generator writes the meta
        /// sidecar right after the sheet, so a later sheet write can only be
        /// a user save) and not imported since. Only a SHEET-produced JSON
        /// counts as "imported since" - a harvest rewrite is newer but does
        /// not carry the sheet's ink, so the badge must stay on. A sheet
        /// generated from a DIFFERENT model file is never dirty for THIS
        /// one. Advisory - the import's own gates stay the authority; a
        /// probe failure reads as clean.</summary>
        public static bool SheetDirty(RhinoDoc doc, out string reason)
        {
            reason = "";
            try
            {
                string sheet = Path.Combine(StagingDir, SheetFileName);
                string meta = Path.Combine(StagingDir, SheetMetaFileName);
                if (!File.Exists(sheet) || !File.Exists(meta))
                {
                    return false;
                }
                string sheetDoc = SheetMetaDocPath();
                string docPath = (doc == null ? "" : doc.Path) ?? "";
                if (sheetDoc.Length > 0 && docPath.Length > 0 &&
                    !string.Equals(sheetDoc, docPath, StringComparison.OrdinalIgnoreCase))
                {
                    return false;
                }
                DateTime sheetWrite = File.GetLastWriteTimeUtc(sheet);
                if (sheetWrite <= File.GetLastWriteTimeUtc(meta))
                {
                    return false;
                }
                string json = Path.Combine(StagingDir, BreaksJsonFile);
                if (File.Exists(json) && File.GetLastWriteTimeUtc(json) >= sheetWrite &&
                    BreaksJsonSourceKind() == "sheet")
                {
                    return false;
                }
                reason = "The break sheet was saved " +
                    File.GetLastWriteTime(sheet).ToString("yyyy-MM-dd HH:mm") +
                    " but not imported since - its latest changes are not in " +
                    "the breaks JSON.";
                return true;
            }
            catch (Exception ex)
            {
                Logger.Error("Sheet-dirty probe failed.", ex);
                reason = "";
                return false;
            }
        }

        /// <summary>Fingerprint + floors + path of the document RIGHT NOW -
        /// captured at Split launch time (the moment the staged copy is
        /// written), not at completion, so edits made while the child runs
        /// cannot stamp the derived model with a state it was not split
        /// from.</summary>
        public static Dictionary<string, object> CaptureModelState(RhinoDoc doc)
        {
            int count;
            long hash;
            ComputeFingerprint(doc, out count, out hash);
            return new Dictionary<string, object>
            {
                { "geomCount", count },
                { "geomHash", hash },
                { "floorsJson", FloorsJson(Methods.RetrieveDictionaryFromDocumentStrings()) },
                // The breaks JSON is the split's PRIMARY input; the geometry
                // fingerprint is deliberately blind to _POURBREAK curves, so
                // without this a re-harvest after Split would leave a green
                // light on a derived model that no longer reflects the
                // authored breaks.
                { "breaksSha", BreaksJsonSha() },
                { "docPath", doc.Path ?? "" },
                { "written", DateTime.UtcNow.ToString("o") },
            };
        }

        public static string BreaksJsonSha()
        {
            string path = Path.Combine(StagingDir, BreaksJsonFile);

            if (!File.Exists(path))
            {
                return "";
            }

            using (var sha = System.Security.Cryptography.SHA256.Create())
            using (FileStream fs = File.OpenRead(path))
            {
                return BitConverter.ToString(sha.ComputeHash(fs)).Replace("-", "");
            }
        }

        /// <summary>Write the derived model's sidecar - called only after a
        /// successful Split, with the state captured at its launch.</summary>
        public static void WriteDerivedModelMeta(Dictionary<string, object> state)
        {
            File.WriteAllText(Path.Combine(StagingDir, DerivedModelMetaFile),
                JsonConvert.SerializeObject(state));
        }

        /// <summary>Drop the sidecar - called when a Split launches, so a
        /// failed or cancelled run cannot leave the previous sidecar blessing
        /// whatever half-written file the child left behind.</summary>
        public static void DeleteDerivedModelMeta()
        {
            string metaPath = Path.Combine(StagingDir, DerivedModelMetaFile);

            if (File.Exists(metaPath))
            {
                File.Delete(metaPath);
            }
        }

        /// <summary>
        /// True when the derived model in staging was split from THIS document
        /// in its CURRENT state; reason names the mismatch otherwise. A
        /// derived model without a sidecar (predating this check, or the file
        /// was deleted) counts as stale - the safe direction.
        /// </summary>
        public static bool DerivedModelMatches(RhinoDoc doc, out string reason)
        {
            string metaPath = Path.Combine(StagingDir, DerivedModelMetaFile);
            if (!File.Exists(metaPath))
            {
                reason = "The derived model has no record of which model state it was " +
                    "split from. Re-run SPLIT BREAKS.";
                return false;
            }
            // Any unreadable, truncated, or key-missing sidecar means STALE,
            // never an exception: this runs from Window_Loaded, where a throw
            // would kill the window over a corrupt staging file.
            try
            {
                Dictionary<string, object> meta = JsonConvert.DeserializeObject<Dictionary<string, object>>(
                    File.ReadAllText(metaPath));

                if (meta == null || !meta.ContainsKey("geomCount") || !meta.ContainsKey("geomHash"))
                {
                    reason = "The derived model's source record is incomplete. Re-run SPLIT BREAKS.";
                    return false;
                }

                string metaDocPath = meta.ContainsKey("docPath") ? (meta["docPath"] as string ?? "") : "";
                string docPath = doc.Path ?? "";
                if (!string.Equals(metaDocPath, docPath, StringComparison.OrdinalIgnoreCase))
                {
                    reason = "The derived model was split from a DIFFERENT model file (" +
                        (metaDocPath.Length == 0 ? "<unsaved>" : metaDocPath) +
                        "). Re-run SPLIT BREAKS on this model file.";
                    return false;
                }

                int count;
                long hash;
                ComputeFingerprint(doc, out count, out hash);
                if (Convert.ToInt64(meta["geomCount"]) != count ||
                    Convert.ToInt64(meta["geomHash"]) != hash)
                {
                    reason = "The model geometry changed after SPLIT BREAKS (" +
                        meta["geomCount"] + " -> " + count + " solids) - the derived model " +
                        "is stale. Re-run SPLIT BREAKS.";
                    return false;
                }

                string floorsNow = FloorsJson(Methods.RetrieveDictionaryFromDocumentStrings());
                if (meta.ContainsKey("floorsJson") && (meta["floorsJson"] as string) != floorsNow)
                {
                    reason = "The floor table changed after SPLIT BREAKS - the derived model " +
                        "is stale. Re-run SPLIT BREAKS.";
                    return false;
                }

                if (meta.ContainsKey("breaksSha") && (meta["breaksSha"] as string) != BreaksJsonSha())
                {
                    reason = "The harvested pour breaks changed after SPLIT BREAKS - the " +
                        "derived model no longer reflects them. Re-run SPLIT BREAKS.";
                    return false;
                }
            }
            catch (Exception ex)
            {
                // Swallow-but-record: a recurring IO/JSON failure on the
                // sidecar must be visible in the session file, not only in the
                // out reason (which some callers discard).
                Logger.Error("Derived-model sidecar check failed.", ex);

                reason = "The derived model's source record could not be checked (" + ex.Message +
                    "). Re-run SPLIT BREAKS.";
                return false;
            }

            reason = "";
            return true;
        }

        /// <summary>Objects under _FORMWORK - the checkup deletes and
        /// re-adds every object in the document, so a checkup with formwork
        /// present must be hard-disabled, not warned about.</summary>
        public static bool FormworkGeometryPresent(RhinoDoc doc)
        {
            foreach (RhinoObject obj in doc.Objects)
            {
                if (obj == null || obj.Attributes == null) { continue; }
                Layer layer = doc.Layers[obj.Attributes.LayerIndex];
                string path = layer == null ? "" : (layer.FullPath ?? "");
                // UNION of both matchers - this is a safety gate for a
                // destructive operation, so it must be at least as broad as
                // every earlier build: the segment match catches a nested
                // "TEMP::_FORMWORK", the prefix match keeps catching the
                // "_FORMWORK_OLD" sibling renames users actually make.
                if (LayerHasSegment(path, FormworkLayer) ||
                    path.StartsWith(FormworkLayer, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
            return false;
        }

        /// <summary>Write a staged copy of the document (never Save - the
        /// open document keeps its own path and dirty state). Callers pick
        /// distinct file names so the preview cannot clobber the model a
        /// running formwork child is reading.</summary>
        public static string StageModelCopy(RhinoDoc doc, string fileName = "model.3dm")
        {
            string path = Path.Combine(StagingDir, fileName);
            Rhino.FileIO.FileWriteOptions options = new Rhino.FileIO.FileWriteOptions
            {
                SuppressDialogBoxes = true,
            };
            if (!doc.WriteFile(path, options))
            {
                throw new IOException("Could not write the staged model copy to " + path);
            }
            return path;
        }

        /// <summary>
        /// Run a staged script in a second, headless Rhino on a model copy.
        /// The watchdog is not optional: a child launched without a window
        /// that hits ANY dialog - including the licence check for a second
        /// seat - hangs invisibly forever.
        /// </summary>
        public static bool RunChildRhino(string modelPath, string scriptName,
            Dictionary<string, string> envVars, int timeoutMs, out string summary,
            Action<Process> onStarted = null, string errorFileName = null)
        {
            EnsureScriptSafeStaging();
            string scriptPath = Path.Combine(StagingDir, scriptName);
            return RunChildCore(modelPath, "-_RunPythonScript " + scriptPath,
                scriptName, envVars, timeoutMs, out summary, onStarted,
                errorFileName);
        }

        /// <summary>Run a PLUGIN COMMAND (e.g. QTOCheckupReport) in the
        /// child Rhino - same staging, watchdog and verification rules as
        /// the script runs. The plugin is registered per-user, so the child
        /// loads it on demand when the command runs.</summary>
        public static bool RunChildRhinoCommand(string modelPath, string commandName,
            Dictionary<string, string> envVars, int timeoutMs, out string summary,
            Action<Process> onStarted = null, string errorFileName = null)
        {
            return RunChildCore(modelPath, "-_" + commandName, commandName,
                envVars, timeoutMs, out summary, onStarted, errorFileName);
        }

        static bool RunChildCore(string modelPath, string runscriptPayload,
            string label, Dictionary<string, string> envVars, int timeoutMs,
            out string summary, Action<Process> onStarted, string errorFileName)
        {
            lock (childLock)
            {
                if (childRunning)
                {
                    summary = "Another formwork child run is already in progress - " +
                        "wait for it to finish (or cancel it) first.";
                    return false;
                }
                childRunning = true;
            }
            try
            {
                string rhinoExe = Process.GetCurrentProcess().MainModule.FileName;
                string errorPath = errorFileName == null
                    ? null : Path.Combine(StagingDir, errorFileName);
                if (errorPath != null && File.Exists(errorPath))
                {
                    // a stale crash file from an earlier run must not
                    // masquerade as this run's failure - or vice versa
                    File.Delete(errorPath);
                }
                StringBuilder args = new StringBuilder();
                if (!string.IsNullOrEmpty(modelPath))
                {
                    args.Append("\"").Append(modelPath).Append("\" ");
                }
                args.Append("/nosplash /notemplate ");
                args.Append("/runscript=\"").Append(runscriptPayload).Append("\"");

                ProcessStartInfo psi = new ProcessStartInfo
                {
                    FileName = rhinoExe,
                    Arguments = args.ToString(),
                    UseShellExecute = false,
                    CreateNoWindow = true,
                };
                psi.EnvironmentVariables["FW_HEADLESS"] = "1";
                if (envVars != null)
                {
                    foreach (KeyValuePair<string, string> kv in envVars)
                    {
                        psi.EnvironmentVariables[kv.Key] = kv.Value;
                    }
                }

                Logger.Info("Formwork child run: " + label + " on " +
                    (modelPath ?? "<new doc>"));
                // NOT in a using: the Cancel button holds a reference to this
                // Process after we return; disposing here would turn a late
                // Cancel click into an ObjectDisposedException.
                Process child = Process.Start(psi);
                if (onStarted != null) { onStarted(child); }
                if (!child.WaitForExit(timeoutMs))
                {
                    try { child.Kill(); } catch { }
                    summary = label + " timed out after " + (timeoutMs / 1000) +
                        " s and was terminated. A hidden dialog (licence check for the " +
                        "second Rhino seat?) is the usual cause - try once with a visible " +
                        "Rhino open to clear it.";
                    Logger.Error(summary, null);
                    return false;
                }
                int exitCode = child.ExitCode;
                if (exitCode != 0)
                {
                    // killed (Cancel) or crashed - "exited within the timeout"
                    // is NOT success
                    summary = label + " was terminated or crashed (exit code " +
                        exitCode + ").";
                    Logger.Error(summary, null);
                    return false;
                }
                if (errorPath != null && File.Exists(errorPath))
                {
                    summary = label + " reported a Python error - see " + errorPath;
                    Logger.Error(summary, null);
                    return false;
                }
                summary = label + " finished.";
                return true;
            }
            finally
            {
                lock (childLock)
                {
                    childRunning = false;
                }
            }
        }

        /// <summary>Tail of a staged log file for the UI panel.</summary>
        public static string ReadLogTail(string fileName, int maxLines)
        {
            try
            {
                string path = Path.Combine(StagingDir, fileName);
                if (!File.Exists(path)) { return "(no log at " + path + ")"; }
                string[] lines = File.ReadAllLines(path);
                IEnumerable<string> tail = lines.Length <= maxLines
                    ? lines : lines.Skip(lines.Length - maxLines);
                return string.Join(Environment.NewLine, tail);
            }
            catch (Exception ex)
            {
                return "(log unreadable: " + ex.Message + ")";
            }
        }
    }
}
