using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using Newtonsoft.Json.Linq;
using Rhino;

namespace QTO_Tool
{
    /// <summary>
    /// The formwork sidecar window. Harvest/Restore act on the live
    /// document (harvest is read-only; restore adds - with a confirmation,
    /// because any addition permanently disables REVERT CHECKUP until the
    /// next successful checkup). Split and Generate always run in a
    /// second, headless Rhino on a staged WriteFile copy, watched by a
    /// timeout - there is deliberately no "place formwork in this model"
    /// button.
    /// </summary>
    public partial class FormworkUI : Window
    {
        static readonly string Stage = FormworkMethods.StagingDir;
        static readonly string BreaksJson = Path.Combine(Stage, "pour_breaks_model.json");
        static readonly string DerivedModel = Path.Combine(Stage, "model_pourbreaks.3dm");
        static readonly string SheetFile = Path.Combine(Stage, "breaksheet.3dm");
        static readonly string SheetMeta = Path.Combine(Stage, "breaksheet.meta.json");

        const int ChildTimeoutMs = 15 * 60 * 1000;

        Process currentChild;
        bool runBusy;
        volatile bool cancelRequested;
        bool stampAllowsGeneration;

        public FormworkUI()
        {
            InitializeComponent();
            this.Closing += FormworkUI_Closing;

            // The stale-reason tooltip sits on a DISABLED radio button, and
            // WPF suppresses tooltips on disabled elements by default.
            System.Windows.Controls.ToolTipService.SetShowOnDisabled(this.InputDerived, true);
        }

        private void Window_Loaded(object sender, RoutedEventArgs e)
        {
            try
            {
                FormworkMethods.ExtractScripts();
            }
            catch (Exception ex)
            {
                AppendLog("Script staging FAILED: " + ex.Message);
                Logger.Error("Formwork script staging failed.", ex);
            }
            RefreshStamp();
            RefreshDerivedAvailability();
        }

        // A busy window must not close silently: the Cancel button is the
        // only handle on the running child, and the RunFormwork command
        // closes this window on re-run.
        private void FormworkUI_Closing(object sender, System.ComponentModel.CancelEventArgs e)
        {
            if (!this.runBusy)
            {
                return;
            }
            MessageBoxResult confirm = MessageBox.Show(
                "A formwork child run is still in progress. Close the window and " +
                "terminate it?", "Formwork run in progress", MessageBoxButton.OKCancel);
            if (confirm != MessageBoxResult.OK)
            {
                e.Cancel = true;
                return;
            }
            this.cancelRequested = true;
            KillCurrentChild();
        }

        private void Recheck_Clicked(object sender, RoutedEventArgs e)
        {
            RefreshStamp();
            RefreshDerivedAvailability();
        }

        private void RefreshStamp()
        {
            if (RunQTO.doc == null || RunQTO.doc.IsAvailable == false)
            {
                RunQTO.doc = RhinoDoc.ActiveDoc;
            }
            string reason;
            FormworkStampStatus status;
            try
            {
                status = FormworkMethods.CheckStamp(RunQTO.doc, out reason);
            }
            catch (Exception ex)
            {
                status = FormworkStampStatus.Missing;
                reason = "Stamp check failed: " + ex.Message;
            }

            switch (status)
            {
                case FormworkStampStatus.Fresh:
                    this.StampLight.Fill = (Brush)new BrushConverter().ConvertFrom("#98AD80");
                    this.StampHeadline.Text = "READY";
                    this.stampAllowsGeneration = true;
                    break;
                case FormworkStampStatus.Unsaved:
                    this.StampLight.Fill = Brushes.Orange;
                    this.StampHeadline.Text = "READY (unsaved changes)";
                    this.stampAllowsGeneration = true;
                    break;
                default:
                    this.StampLight.Fill = Brushes.Firebrick;
                    this.StampHeadline.Text = "NOT READY";
                    this.stampAllowsGeneration = false;
                    break;
            }
            this.StampReason.Text = reason;

            // authoring aids stay available; generation is gated
            this.SplitButton.IsEnabled = this.stampAllowsGeneration && !this.runBusy;
            this.GenerateButton.IsEnabled = this.stampAllowsGeneration && !this.runBusy;
        }

        /// <summary>Stamp gate re-checked AT CLICK TIME - the state shown at
        /// window load can be minutes stale.</summary>
        private bool EnsureFresh()
        {
            RefreshStamp();
            if (!this.stampAllowsGeneration)
            {
                MessageBox.Show("The take-off is not current: " + this.StampReason.Text);
            }
            return this.stampAllowsGeneration;
        }

        private void RefreshDerivedAvailability()
        {
            bool exists = File.Exists(DerivedModel);
            bool matches = false;
            string staleReason = "";
            if (exists)
            {
                // The staging folder is machine-wide with one fixed file name,
                // so mere existence proves nothing: the file may be another
                // project's, or split from a state this document no longer has.
                matches = FormworkMethods.DerivedModelMatches(RunQTO.doc, out staleReason);
            }
            this.InputDerived.IsEnabled = exists && matches;
            this.InputDerived.ToolTip = null;
            if (exists && matches)
            {
                this.InputDerived.Content = "Pour-break derived model (built " +
                    File.GetLastWriteTime(DerivedModel).ToString("yyyy-MM-dd HH:mm") + ")";
            }
            else if (exists)
            {
                // Deliberately NOT flipping the selection to the original
                // model: formwork generated on the wrong input looks exactly
                // like a clean run, so a stale derived selection must fail
                // loudly at the Generate click, never silently switch inputs.
                // The WHY goes to the tooltip and the session log - "it never
                // goes green" is otherwise undiagnosable.
                this.InputDerived.Content = "Pour-break derived model (STALE - re-run Split)";
                this.InputDerived.ToolTip = staleReason;

                Logger.Info("Derived model marked stale: " + staleReason);
            }
            else
            {
                this.InputDerived.Content = "Pour-break derived model (run Split first)";
                this.InputOriginal.IsChecked = true;
            }
        }

        private void AppendLog(string text)
        {
            this.LogBox.AppendText(text + Environment.NewLine);
            this.LogBox.ScrollToEnd();
        }

        private void SetBusy(bool busy)
        {
            this.runBusy = busy;
            this.HarvestButton.IsEnabled = !busy;
            this.RestoreButton.IsEnabled = !busy;
            this.RecheckButton.IsEnabled = !busy;
            this.MakeSheetButton.IsEnabled = !busy;
            this.OpenSheetButton.IsEnabled = !busy;
            this.ImportSheetButton.IsEnabled = !busy;
            this.CancelRunButton.IsEnabled = busy;
            if (busy)
            {
                this.SplitButton.IsEnabled = false;
                this.GenerateButton.IsEnabled = false;
            }
            else
            {
                RefreshStamp();
                RefreshDerivedAvailability();
            }
        }

        /// <summary>The harvested breaks JSON records which model it came
        /// from; splitting or restoring another model's breaks is almost
        /// never intended.</summary>
        private bool BreaksMatchThisModel()
        {
            try
            {
                JObject data = JObject.Parse(File.ReadAllText(BreaksJson));
                string source = (string)data.SelectToken("source.model") ?? "";
                string docPath = RunQTO.doc.Path ?? "";
                if (source.Length > 0 && docPath.Length > 0 &&
                    !string.Equals(source, docPath, StringComparison.OrdinalIgnoreCase))
                {
                    MessageBoxResult confirm = MessageBox.Show(
                        "The harvested breaks were taken from a DIFFERENT model:\n" +
                        source + "\n\nUse them on this model file anyway?",
                        "Breaks from another model", MessageBoxButton.OKCancel);
                    return confirm == MessageBoxResult.OK;
                }
            }
            catch (Exception ex)
            {
                AppendLog("Breaks JSON unreadable: " + ex.Message);
                return false;
            }
            return true;
        }

        /// <summary>MAKE BREAK SHEET: lay every floor group out as a plan
        /// cell in a separate 3dm. Runs IN-PROCESS via File3dm (read-only on
        /// the open model file, writes only staging files) - no child Rhino,
        /// no REVERT impact.</summary>
        private void MakeSheet_Clicked(object sender, RoutedEventArgs e)
        {
            if (File.Exists(SheetFile))
            {
                MessageBoxResult confirm = MessageBox.Show(
                    "A break sheet already exists (" +
                    File.GetLastWriteTime(SheetFile).ToString("yyyy-MM-dd HH:mm") +
                    "). Regenerating OVERWRITES it - anything drawn there and " +
                    "not yet imported is lost. (Existing breaks from the " +
                    "current JSON are drawn back in.) Continue?",
                    "Regenerate break sheet", MessageBoxButton.OKCancel);
                if (confirm != MessageBoxResult.OK)
                {
                    return;
                }
            }
            if (RunQTO.doc == null || RunQTO.doc.IsAvailable == false)
            {
                RunQTO.doc = RhinoDoc.ActiveDoc;
            }
            // A stale sheet from a refused run must not read as success:
            // clear the error file, note the pre-run write time, and judge
            // the run by what actually changed.
            string errorFile = Path.Combine(Stage, "breaksheet_error.txt");
            try { if (File.Exists(errorFile)) { File.Delete(errorFile); } } catch { }
            DateTime? sheetBefore = File.Exists(SheetFile)
                ? File.GetLastWriteTimeUtc(SheetFile) : (DateTime?)null;

            string script = Path.Combine(Stage, "bs_gui_make.py");
            RhinoApp.RunScript("_-RunPythonScript " + script, false);
            AppendLog("--- make break sheet ---");
            AppendLog(FormworkMethods.ReadLogTail("breaksheet_log.txt", 40));

            if (File.Exists(errorFile))
            {
                Logger.Error("Break-sheet generation failed - " + errorFile, null);
                AppendLog("MAKE FAILED - see " + errorFile);
            }
            else if (!File.Exists(SheetFile) ||
                (sheetBefore != null &&
                 File.GetLastWriteTimeUtc(SheetFile) == sheetBefore.Value))
            {
                AppendLog("MAKE did not (re)write the sheet - see the log above.");
            }
            else
            {
                AppendLog("Sheet: " + SheetFile + " - OPEN SHEET, draw, save, " +
                    "then IMPORT SHEET.");
            }
        }

        private void OpenSheet_Clicked(object sender, RoutedEventArgs e)
        {
            if (!File.Exists(SheetFile))
            {
                MessageBox.Show("No break sheet yet - run MAKE BREAK SHEET first.");
                return;
            }
            // Opens in a separate Rhino instance (Windows file association) -
            // deliberately NOT in this one: opening a document here would
            // close the model file this window is working against.
            Process.Start(SheetFile);
            AppendLog("Sheet opened in a separate Rhino. Draw, SAVE there, " +
                "then IMPORT SHEET here.");
        }

        /// <summary>IMPORT SHEET: read the drawn sheet via File3dm and write
        /// the breaks JSON. Loud refusals live in the Python side; the
        /// dialogs here gate the two-truths cases.</summary>
        private void ImportSheet_Clicked(object sender, RoutedEventArgs e)
        {
            if (!File.Exists(SheetFile))
            {
                MessageBox.Show("No break sheet found - run MAKE BREAK SHEET first.");
                return;
            }
            if (RunQTO.doc == null || RunQTO.doc.IsAvailable == false)
            {
                RunQTO.doc = RhinoDoc.ActiveDoc;
            }
            // Two-truths gate 1: live _POURBREAK curves exist. The import
            // overwrites the JSON from the sheet; the layer curves stay in
            // the model but are no longer what Split will cut by.
            if (PourBreakCurvesPresent())
            {
                MessageBoxResult confirm = MessageBox.Show(
                    "This model file also has curves on the _POURBREAK layer. " +
                    "Importing the sheet REPLACES the breaks JSON - the layer " +
                    "curves stay in the model but will NOT be part of the next " +
                    "Split unless you re-run HARVEST instead. Import the sheet?",
                    "Layer breaks also present", MessageBoxButton.OKCancel);
                if (confirm != MessageBoxResult.OK)
                {
                    return;
                }
            }
            // Two-truths gate 2: an existing JSON from another surface.
            if (File.Exists(BreaksJson))
            {
                // an unreadable/unknown source is MORE reason to confirm, not
                // less - only a sheet-produced JSON skips the dialog
                string kind = ReadBreaksSourceKind();
                if (kind != "sheet")
                {
                    MessageBoxResult confirm = MessageBox.Show(
                        "The current breaks JSON came from '" +
                        (kind == "" ? "unknown" : kind) + "' (" +
                        File.GetLastWriteTime(BreaksJson).ToString("yyyy-MM-dd HH:mm") +
                        "). Importing the sheet OVERWRITES it (floors without " +
                        "a sheet cell are preserved). Continue?",
                        "Overwrite breaks JSON", MessageBoxButton.OKCancel);
                    if (confirm != MessageBoxResult.OK)
                    {
                        return;
                    }
                }
            }
            // Judge the run by what changed, never by the previous run's log.
            string errorFile = Path.Combine(Stage, "breaksheet_import_error.txt");
            try { if (File.Exists(errorFile)) { File.Delete(errorFile); } } catch { }
            DateTime? jsonBefore = File.Exists(BreaksJson)
                ? File.GetLastWriteTimeUtc(BreaksJson) : (DateTime?)null;

            string script = Path.Combine(Stage, "bs_gui_import.py");
            RhinoApp.RunScript("_-RunPythonScript " + script, false);
            AppendLog("--- import break sheet ---");
            AppendLog(FormworkMethods.ReadLogTail("breaksheet_import_log.txt", 40));

            if (File.Exists(errorFile))
            {
                Logger.Error("Break-sheet import failed - " + errorFile, null);
                AppendLog("IMPORT FAILED - see " + errorFile);
            }
            else if (!File.Exists(BreaksJson) ||
                (jsonBefore != null &&
                 File.GetLastWriteTimeUtc(BreaksJson) == jsonBefore.Value))
            {
                AppendLog("IMPORT wrote nothing - the refusal reasons are in " +
                    "the log above; the previous breaks JSON is unchanged.");
            }
            // a changed breaks JSON makes any derived model stale - reflect it
            RefreshDerivedAvailability();
        }

        /// <summary>Read-only probe for _POURBREAK curves in the open model
        /// file (default enumerator is fine here - a hidden authored curve
        /// still gets flagged by the harvest side).</summary>
        private bool PourBreakCurvesPresent()
        {
            try
            {
                // hidden breaks count too - the harvest includes them, so the
                // two-truths dialog must not be skippable by Ctrl+H
                Rhino.DocObjects.ObjectEnumeratorSettings es =
                    new Rhino.DocObjects.ObjectEnumeratorSettings
                    {
                        NormalObjects = true,
                        LockedObjects = true,
                        HiddenObjects = true,
                    };
                foreach (Rhino.DocObjects.RhinoObject obj in RunQTO.doc.Objects.GetObjectList(es))
                {
                    if (obj == null || obj.Attributes == null) { continue; }
                    Rhino.DocObjects.Layer layer = RunQTO.doc.Layers[obj.Attributes.LayerIndex];
                    string path = layer == null ? "" : (layer.FullPath ?? "");
                    if (path.StartsWith(FormworkMethods.PourBreakLayer,
                        StringComparison.OrdinalIgnoreCase))
                    {
                        return true;
                    }
                }
            }
            catch (Exception ex)
            {
                Logger.Error("_POURBREAK presence probe failed.", ex);
            }
            return false;
        }

        private string ReadBreaksSourceKind()
        {
            try
            {
                JObject data = JObject.Parse(File.ReadAllText(BreaksJson));
                return (string)data.SelectToken("source.kind") ?? "";
            }
            catch
            {
                return "";
            }
        }

        private void Harvest_Clicked(object sender, RoutedEventArgs e)
        {
            // read-only on the live document: fires no doc events, REVERT
            // CHECKUP is unaffected. The generated driver bakes the PB_JSON
            // path in (IronPython may never see env vars set after start).
            string script = Path.Combine(Stage, "pb_gui_harvest.py");
            RhinoApp.RunScript("_-RunPythonScript " + script, false);
            AppendLog("--- harvest ---");
            AppendLog(FormworkMethods.ReadLogTail("pour_breaks_model_log.txt", 30));
        }

        private void Restore_Clicked(object sender, RoutedEventArgs e)
        {
            if (!File.Exists(BreaksJson))
            {
                MessageBox.Show("No harvested breaks found at " + BreaksJson +
                    ". Run HARVEST first.");
                return;
            }
            if (!BreaksMatchThisModel())
            {
                return;
            }
            MessageBoxResult confirm = MessageBox.Show(
                "Restore re-draws the harvested break curves and pour dots into the " +
                "OPEN model file. Adding objects disables REVERT CHECKUP until the next " +
                "successful checkup. Continue?",
                "Restore pour breaks", MessageBoxButton.OKCancel);
            if (confirm != MessageBoxResult.OK)
            {
                return;
            }
            string script = Path.Combine(Stage, "pb_gui_restore.py");
            RhinoApp.RunScript("_-RunPythonScript " + script, false);
            AppendLog("--- restore ---");
            AppendLog("Breaks re-drawn from " + BreaksJson);
        }

        private void Split_Clicked(object sender, RoutedEventArgs e)
        {
            if (!File.Exists(BreaksJson))
            {
                MessageBox.Show("No harvested breaks found. Run HARVEST first.");
                return;
            }
            if (!EnsureFresh() || !BreaksMatchThisModel())
            {
                return;
            }
            // Refuse BEFORE the pre-launch side effects: RunChildCore's own
            // gate (shared with QTOUI's preview checkup, which this window's
            // runBusy flag does not track) would refuse the launch anyway,
            // but only after the sidecar was deleted and model.3dm staged
            // over a file the running child may be reading. A tiny window
            // remains between this check and the launch; it shrinks the
            // practical race from the whole preview duration to milliseconds.
            if (FormworkMethods.ChildRunning)
            {
                AppendLog("Another child Rhino run (preview checkup or formwork) is in " +
                    "progress - try SPLIT again when it finishes.");
                Logger.Info("Split refused: another child Rhino run is in progress.");
                return;
            }
            string staged;
            Dictionary<string, object> splitState;
            DateTime? derivedWriteBefore = null;
            try
            {
                staged = FormworkMethods.StageModelCopy(RunQTO.doc);
                // Captured at the same moment as the staged copy: this is the
                // exact document state the derived model will be split from.
                splitState = FormworkMethods.CaptureModelState(RunQTO.doc);
                // A failed or cancelled run must not leave the previous sidecar
                // blessing whatever file the child left behind - drop it now,
                // re-issue it only on verified success below.
                FormworkMethods.DeleteDerivedModelMeta();
                if (File.Exists(DerivedModel))
                {
                    derivedWriteBefore = File.GetLastWriteTimeUtc(DerivedModel);
                }
            }
            catch (Exception ex)
            {
                // This try covers more than staging (fingerprint capture,
                // breaks-JSON hashing, sidecar delete) - label and log
                // accordingly.
                Logger.Error("Split staging or sidecar preparation failed.", ex);

                AppendLog("Split staging/sidecar preparation FAILED: " + ex.Message);
                return;
            }
            Dictionary<string, string> env = new Dictionary<string, string>
            {
                { "PB_JSON", BreaksJson },
                { "PB_OUT3DM", DerivedModel },
                { "PB_REPORT", Path.Combine(Stage, "pourbreak_report_v2.json") },
            };
            AppendLog("--- split pour breaks (child Rhino) ---");
            RunChildAsync(staged, "split_pourbreaks.py", env,
                "model_pourbreak_log.txt", "pourbreak_error.txt",
                () =>
                {
                    try
                    {
                        // A clean child exit does not prove the derived model
                        // was rewritten (an early no-op exit leaves the old
                        // file untouched): only a fresh write earns the
                        // sidecar. Without it the file reads as stale, which
                        // just forces a re-split - the safe direction.
                        bool rewritten = File.Exists(DerivedModel) &&
                            (derivedWriteBefore == null ||
                             File.GetLastWriteTimeUtc(DerivedModel) != derivedWriteBefore.Value);
                        if (rewritten)
                        {
                            FormworkMethods.WriteDerivedModelMeta(splitState);
                        }
                        else
                        {
                            // The signature field symptom "Split succeeds but the
                            // derived model is always STALE" must be traceable
                            // from the session file, not just this textbox.
                            Logger.Warn("Split finished but did not (re)write " + DerivedModel +
                                "; the derived-model sidecar was withheld.");

                            AppendLog("The split run did not (re)write " + DerivedModel +
                                " - the derived model stays marked stale.");
                        }
                    }
                    catch (Exception metaEx)
                    {
                        Logger.Error("Could not record the derived model's source state.", metaEx);

                        AppendLog("Could not record the derived model's source state: " +
                            metaEx.Message);
                    }
                });
        }

        private void Generate_Clicked(object sender, RoutedEventArgs e)
        {
            if (!EnsureFresh())
            {
                return;
            }
            // Same early refusal as Split: staging the original model below
            // overwrites model.3dm before RunChildCore's gate could refuse.
            if (FormworkMethods.ChildRunning)
            {
                AppendLog("Another child Rhino run (preview checkup or formwork) is in " +
                    "progress - try GENERATE again when it finishes.");
                Logger.Info("Generate refused: another child Rhino run is in progress.");
                return;
            }
            string model;
            if (this.InputDerived.IsChecked == true)
            {
                if (!File.Exists(DerivedModel))
                {
                    // never silently fall back to the original: the user
                    // asked for the derived model, and formwork generated on
                    // the wrong input looks exactly like a clean run
                    MessageBox.Show("The pour-break derived model is missing (" +
                        DerivedModel + "). Run SPLIT BREAKS first, or select the " +
                        "original model.");
                    return;
                }
                // Existence is not enough: the staging file names are fixed
                // machine-wide, so the file may be stale for this document or
                // belong to a different project - and formwork generated on
                // the wrong input looks exactly like a clean run.
                string mismatch;
                if (!FormworkMethods.DerivedModelMatches(RunQTO.doc, out mismatch))
                {
                    // The reason names the exact cause (foreign doc, geometry,
                    // floors, re-harvested breaks, corrupt sidecar); it must
                    // outlive the message box.
                    Logger.Warn("Generate refused: derived model mismatch - " + mismatch);
                    AppendLog("Generate refused: " + mismatch);

                    MessageBox.Show(mismatch);
                    RefreshDerivedAvailability();
                    return;
                }
                model = DerivedModel;
            }
            else
            {
                try
                {
                    model = FormworkMethods.StageModelCopy(RunQTO.doc);
                }
                catch (Exception ex)
                {
                    Logger.Error("Generate staging failed.", ex);

                    AppendLog("Staging FAILED: " + ex.Message);
                    return;
                }
            }
            AppendLog("--- generate formwork (child Rhino, two passes) ---");
            SetBusy(true);
            this.cancelRequested = false;
            string chosen = model;
            Task.Run(() =>
            {
                try
                {
                    string summary;
                    bool ok = FormworkMethods.RunChildRhino(chosen, "run_on_model.py",
                        null, ChildTimeoutMs, out summary,
                        p => this.currentChild = p, "model_run_error.txt");
                    this.currentChild = null;
                    Dispatcher.Invoke(new Action(() =>
                    {
                        AppendLog(summary);
                        AppendLog(FormworkMethods.ReadLogTail("model_run_log.txt", 40));
                    }));
                    if (ok && !this.cancelRequested)
                    {
                        bool ok2 = FormworkMethods.RunChildRhino(chosen,
                            "run_sideforms_on_model.py", null, ChildTimeoutMs,
                            out summary, p => this.currentChild = p,
                            "sideform_model_error.txt");
                        this.currentChild = null;
                        Dispatcher.Invoke(new Action(() =>
                        {
                            AppendLog(summary);
                            AppendLog(FormworkMethods.ReadLogTail("sideform_model_log.txt", 40));
                            if (ok2)
                            {
                                AppendLog("Output: formwork_out.3dm + sideforms_out.3dm " +
                                    "(+ JSON) in " + Stage);
                            }
                        }));
                    }
                }
                catch (Exception ex)
                {
                    Logger.Error("Formwork generate run failed.", ex);
                    try
                    {
                        Dispatcher.Invoke(new Action(() =>
                            AppendLog("Generate FAILED: " + ex.Message)));
                    }
                    catch { }
                }
                finally
                {
                    this.currentChild = null;
                    try
                    {
                        Dispatcher.Invoke(new Action(() => SetBusy(false)));
                    }
                    catch { }
                }
            });
        }

        private void RunChildAsync(string modelPath, string scriptName,
            Dictionary<string, string> env, string logFile, string errorFile,
            Action onSuccess = null)
        {
            SetBusy(true);
            this.cancelRequested = false;
            Task.Run(() =>
            {
                try
                {
                    string summary;
                    bool ok = FormworkMethods.RunChildRhino(modelPath, scriptName, env,
                        ChildTimeoutMs, out summary, p => this.currentChild = p,
                        errorFile);
                    Dispatcher.Invoke(new Action(() =>
                    {
                        AppendLog(summary);
                        AppendLog(FormworkMethods.ReadLogTail(logFile, 40));
                    }));
                    if (ok && !this.cancelRequested && onSuccess != null)
                    {
                        Dispatcher.Invoke(onSuccess);
                    }
                }
                catch (Exception ex)
                {
                    Logger.Error("Formwork child run failed.", ex);
                    try
                    {
                        Dispatcher.Invoke(new Action(() =>
                            AppendLog(scriptName + " FAILED: " + ex.Message)));
                    }
                    catch { }
                }
                finally
                {
                    this.currentChild = null;
                    try
                    {
                        Dispatcher.Invoke(new Action(() => SetBusy(false)));
                    }
                    catch { }
                }
            });
        }

        private void KillCurrentChild()
        {
            Process child = this.currentChild;
            if (child == null)
            {
                return;
            }
            try
            {
                if (!child.HasExited)
                {
                    child.Kill();
                    AppendLog("Child Rhino terminated by user.");
                }
            }
            catch (Exception ex)
            {
                AppendLog("Cancel failed: " + ex.Message);
            }
        }

        private void CancelRun_Clicked(object sender, RoutedEventArgs e)
        {
            this.cancelRequested = true;
            KillCurrentChild();
        }

        private void OpenOutput_Clicked(object sender, RoutedEventArgs e)
        {
            Process.Start("explorer.exe", Stage);
        }
    }
}
