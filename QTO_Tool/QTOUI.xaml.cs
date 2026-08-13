using System;
using System.Collections.Generic;
using System.Linq;
using System.IO;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using System.Windows;
using Rhino;
using Rhino.DocObjects;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Threading;
using System.Windows.Threading;
using Xbim.Ifc;
using Xbim.Ifc4.ProductExtension;
using System.Windows.Media;
using System.Diagnostics;

namespace QTO_Tool
{
    /// <summary>
    /// Interaction logic for QTOUI.xaml
    /// </summary>
    /// 
    public partial class QTOUI : Window
    {
        Dictionary<string, string> selectedConcreteTemplatesForLayers = new Dictionary<string, string>();

        AllBeams allBeams = new AllBeams();
        AllColumns allColumns = new AllColumns();
        AllContinuousFootings allContinuousFootings = new AllContinuousFootings();
        AllCurbs allCurbs = new AllCurbs();
        AllFootings allFootings = new AllFootings();
        AllSlabs allSlabs = new AllSlabs();
        AllWalls allWalls = new AllWalls();
        AllStyrofoams allStyrofoams = new AllStyrofoams();
        AllStairs allStairs = new AllStairs();

        Dictionary<string, object> allSelectedTemplates = new Dictionary<string, object>();
        Dictionary<string, List<string>> allSelectedTemplateValues = new Dictionary<string, List<string>>();

        List<string> quantityValues = new List<string>();

        List<string> layerPropertyColumnHeaders = new List<string>();

        List<RhinoObject> selectedObjects = new List<RhinoObject>();

        // Re-entry guards for the checkup, revert, and calculate buttons; clicks
        // queued while the UI thread was blocked must not start a second run.
        private bool checkupRunning = false;
        private bool revertRunning = false;
        private bool calculateRunning = false;

        // Serial number of the "QTO Checkup" undo record of the last successful
        // checkup; 0 when no revertable record exists. Revert verifies against
        // it that the checkup record is still the topmost undo record.
        private uint checkupUndoRecordSerial = 0;

        // Set while our own checkup/revert mutates the document, so the
        // revert-validity watch ignores the events we cause ourselves.
        private bool suppressDocEvents = false;

        //// Save/Load Dictionary
        //Dictionary<string, object> saveData = new Dictionary<string, object>();
        //Dictionary<string, object> loadData = new Dictionary<string, object>();

        ElevationInput elevationInput;

        public QTOUI()
        {
            InitializeComponent();
        }

        private void Window_Loaded(object sender, RoutedEventArgs e)
        {
            ElevationInput.floorElevations = Methods.RetrieveDictionaryFromDocumentStrings();

            if (ElevationInput.floorElevations.Count > 0)
            {
                this.SetFloor.Background = (Brush)new System.Windows.Media.BrushConverter().ConvertFrom("#98AD80");
            }
            else
            {
                this.SetFloor.Background = Brushes.Firebrick;
                //this.SetFloor.Background = (Brush)new System.Windows.Media.BrushConverter().ConvertFrom("#FF5858 ");
            }
        }

        public void SetFloor_Clicked(object sender, RoutedEventArgs e)
        {
            try
            {
                if (this.elevationInput != null)
                {
                    this.elevationInput.Close();
                }
                this.elevationInput = new ElevationInput();
                this.elevationInput.ChangeSetFloorButtonColorRequest += ChangeSetFloorButtonColor;

                this.elevationInput.Show();
            }
            catch (Exception ex)
            {
                MessageBox.Show(ex.ToString());
            }
        }

        // Event handler to change the color in Page1
        private void ChangeSetFloorButtonColor(object sender, EventArgs e)
        {
            if (ElevationInput.floorElevations.Count > 0)
            {
                this.SetFloor.Background = (Brush)new System.Windows.Media.BrushConverter().ConvertFrom("#98AD80");
            }
        }

        // The formwork window is its own command and window by design -
        // QTOUI only carries this launcher.
        private void Formwork_Clicked(object sender, RoutedEventArgs e)
        {
            RhinoApp.RunScript("_RunFormwork", false);
        }

        private void StartCheckup_Clicked(object sender, RoutedEventArgs e)
        {
            // Clicks queued while a checkup blocked the UI thread must not start
            // a second run on a half-processed document.
            if (this.checkupRunning)
            {
                return;
            }

            // The checkup deletes and re-adds EVERY object in the document.
            // With formwork geometry present that would destroy or double the
            // generated temporary works and pollute the template picker, so
            // this is a hard stop, not a warning. Refresh the doc reference
            // FIRST - the guard must inspect the same document the checkup
            // below will run on, not a stale one.
            if (RunQTO.doc == null || RunQTO.doc.IsAvailable == false)
            {
                RunQTO.doc = RhinoDoc.ActiveDoc;
            }
            if (FormworkMethods.FormworkGeometryPresent(RunQTO.doc))
            {
                this.StartCheckup.IsEnabled = false;
                MessageBox.Show("Formwork geometry (" + FormworkMethods.FormworkLayer +
                    " layer) is present in this document. Start Checkup is disabled " +
                    "until it is removed - purge the formwork or work on a copy.");
                return;
            }

            this.checkupRunning = true;
            this.StartCheckup.IsEnabled = false;

            try
            {
                // Always get the Active model
                if (RunQTO.doc.IsAvailable == false)
                {
                    RunQTO.doc = RhinoDoc.ActiveDoc;
                }

                Thread newWindowThread = new Thread(new ThreadStart(() =>
                {
                    // Create our context, and install it:
                    SynchronizationContext.SetSynchronizationContext(
                        new DispatcherSynchronizationContext(
                            Dispatcher.CurrentDispatcher));

                    // Create and configure the window
                    ProgressWindow progressWindow = new ProgressWindow();

                    // When the window closes, shut down the dispatcher
                    progressWindow.Closed += (s, eventArg) =>
                       Dispatcher.CurrentDispatcher.BeginInvokeShutdown(DispatcherPriority.Background);

                    progressWindow.Show();
                    // Start the Dispatcher Processing
                    Dispatcher.Run();
                }));

                newWindowThread.SetApartmentState(ApartmentState.STA);
                // Make the thread a background thread
                newWindowThread.IsBackground = true;
                // Start the thread
                newWindowThread.Start();

                this.layerPropertyColumnHeaders.Clear();

                if (this.ConcreteIsIncluded.IsChecked == true)
                {
                    Logger.Info("Checkup started.");

                    // Our own mutations must not trip the revert-validity watch left
                    // over from a previous checkup.
                    this.suppressDocEvents = true;

                    try
                    {
                        this.CheckupResults.Content = Methods.ConcreteModelSetup(out this.checkupUndoRecordSerial);
                    }
                    catch (Exception ex)
                    {
                        Logger.Error("Checkup failed with an unhandled exception.", ex);

                        this.CheckupResults.Content = "Checkup failed: " + ex.Message + Environment.NewLine +
                            "Details were written to the log: " + (Logger.LogFilePath ?? "<log unavailable>");
                        this.CheckupResults.Visibility = Visibility.Visible;

                        // The model may be half-processed and any result tables from a
                        // previous run reference deleted object ids (their row toggles
                        // would throw). Clear them and require a successful checkup
                        // before calculating or exporting again.
                        this.ConcreteTemplateGrid.Children.Clear();
                        this.ConcreteTemplateGrid.RowDefinitions.Clear();
                        this.DissipatedConcreteTablePanel.Children.Clear();

                        this.CalculateQuantitiesButton.IsEnabled = false;
                        this.ExportExcelButton.IsEnabled = false;
                        this.ExportIFC.IsEnabled = false;
                        this.Blockify.IsEnabled = false;

                        // A failed checkup left an unknown mix of changes behind; a
                        // one-click revert can no longer be trusted.
                        this.RevertCheckupButton.IsEnabled = false;
                        this.UnsubscribeRevertWatch();
                        this.checkupUndoRecordSerial = 0;

                        this.allSelectedTemplates.Clear();
                        this.allSelectedTemplateValues.Clear();
                        this.selectedConcreteTemplatesForLayers.Clear();

                        MessageBox.Show("The model checkup failed with an error." + Environment.NewLine + Environment.NewLine +
                            ex.Message + Environment.NewLine + Environment.NewLine +
                            "Details were written to the log file:" + Environment.NewLine +
                            (Logger.LogFilePath ?? "<log unavailable>"));

                        Dispatcher.FromThread(newWindowThread)?.InvokeShutdown();

                        return;
                    }
                    finally
                    {
                        this.suppressDocEvents = false;
                    }

                    Logger.Info("Checkup finished: " + this.CheckupResults.Content.ToString().Replace(Environment.NewLine, " | "));

                    this.CheckupResults.Visibility = Visibility.Visible;

                    // RhinoDoc.Undo pops only the top undo record, so Revert is valid
                    // only while the "QTO Checkup" record is topmost; the watch
                    // disables it as soon as any other change lands on the document.
                    // When no undo record could be started (serial 0, undo disabled
                    // or another record active), there is nothing to revert to and
                    // the button must stay disabled.
                    if (this.checkupUndoRecordSerial > 0)
                    {
                        this.RevertCheckupButton.IsEnabled = true;
                        this.SubscribeRevertWatch();
                    }
                    else
                    {
                        this.RevertCheckupButton.IsEnabled = false;
                        this.UnsubscribeRevertWatch();
                    }

                    if (this.ConcreteTemplateGrid.Children.Count == 0)
                    {
                        UIMethods.GenerateLayerTemplate(this.ConcreteTemplateGrid, this.layerPropertyColumnHeaders);

                        CalculateQuantitiesButton.IsEnabled = true;
                        AngleThresholdLabel.IsEnabled = true;
                        AngleThresholdSlider.IsEnabled = true;
                        CombineValuesLabel.IsEnabled = true;
                        CombinedValuesToggle.IsEnabled = true;
                        this.Blockify.IsEnabled = true;
                    }
                    else
                    {
                        this.ConcreteTemplateGrid.Children.Clear();
                        this.ConcreteTemplateGrid.RowDefinitions.Clear();
                        UIMethods.GenerateLayerTemplate(this.ConcreteTemplateGrid, this.layerPropertyColumnHeaders);
                        this.DissipatedConcreteTablePanel.Children.Clear();

                        this.ExportExcelButton.IsEnabled = false;
                        this.ExportIFC.IsEnabled = false;

                        this.allSelectedTemplates.Clear();
                        this.allSelectedTemplateValues.Clear();

                        this.selectedConcreteTemplatesForLayers.Clear();

                        //this.saveData.Clear();
                        //this.loadData.Clear();
                    }
                }
                if (this.ExteriorIsIncluded.IsChecked == true)
                {

                }

                if (this.ConcreteIsIncluded.IsChecked == false && this.ExteriorIsIncluded.IsChecked == false)
                {
                    MessageBox.Show("Please select at least one of the methods.");
                }

                // -= before += so re-running the checkup never stacks a second copy
                // of each selection-sync handler.
                RhinoDoc.SelectObjects -= OnSelectObjects;
                RhinoDoc.SelectObjects += OnSelectObjects;
                RhinoDoc.DeselectObjects -= OnDeselectObjects;
                RhinoDoc.DeselectObjects += OnDeselectObjects;
                RhinoDoc.DeselectAllObjects -= OnDeselectAllObjects;
                RhinoDoc.DeselectAllObjects += OnDeselectAllObjects;

                Dispatcher.FromThread(newWindowThread)?.InvokeShutdown();
            }
            finally
            {
                // ApplicationIdle: clicks queued while the UI thread was blocked are
                // dispatched at input priority first, so they land on the button
                // while it is still disabled instead of starting another run.
                this.Dispatcher.BeginInvoke(new Action(() =>
                {
                    this.StartCheckup.IsEnabled = true;
                    this.checkupRunning = false;
                }), DispatcherPriority.ApplicationIdle);
            }
        }

        /*---------------- Revert checkup ----------------*/
        private void RevertCheckup_Clicked(object sender, RoutedEventArgs e)
        {
            if (this.revertRunning)
            {
                return;
            }

            this.revertRunning = true;
            this.suppressDocEvents = true;

            try
            {
                // Undo pops the topmost record, which is the checkup record only if
                // no undo record of any kind was created since. The serial check
                // catches undoable changes the event watch has no event for
                // (linetype edits, ...): if the next record serial moved past
                // ours + 1, something else was recorded on top and a blind Undo
                // would revert the wrong thing while claiming success.
                bool checkupRecordIsTopmost = RunQTO.doc != null && RunQTO.doc.IsAvailable &&
                    this.checkupUndoRecordSerial > 0 &&
                    RunQTO.doc.NextUndoRecordSerialNumber == this.checkupUndoRecordSerial + 1;

                // Undo fires add/delete document events for every object it restores;
                // suppressDocEvents keeps the watch from reacting to our own revert.
                bool undoSucceeded = checkupRecordIsTopmost && RunQTO.doc.Undo();

                if (undoSucceeded)
                {
                    // Every result table and calculated quantity referenced objects
                    // that no longer exist after the undo.
                    this.ConcreteTemplateGrid.Children.Clear();
                    this.ConcreteTemplateGrid.RowDefinitions.Clear();

                    this.DissipatedConcreteTablePanel.Children.Clear();
                    this.DissipatedConcreteTablePanel.Visibility = Visibility.Collapsed;

                    this.allBeams.Clear();
                    this.allColumns.Clear();
                    this.allContinuousFootings.Clear();
                    this.allCurbs.Clear();
                    this.allFootings.Clear();
                    this.allSlabs.Clear();
                    this.allWalls.Clear();
                    this.allStyrofoams.Clear();
                    this.allStairs.Clear();

                    this.allSelectedTemplates.Clear();
                    this.allSelectedTemplateValues.Clear();
                    this.selectedConcreteTemplatesForLayers.Clear();
                    this.layerPropertyColumnHeaders.Clear();
                    this.selectedObjects.Clear();

                    this.CalculateQuantitiesButton.IsEnabled = false;
                    this.ExportExcelButton.IsEnabled = false;
                    this.ExportIFC.IsEnabled = false;
                    this.Blockify.IsEnabled = false;
                    this.AngleThresholdSlider.IsEnabled = false;
                    this.AngleThresholdLabel.IsEnabled = false;
                    this.CombinedValuesToggle.IsEnabled = false;
                    this.CombineValuesLabel.IsEnabled = false;
                    this.RevertCheckupButton.IsEnabled = false;
                    this.checkupUndoRecordSerial = 0;

                    this.CheckupResults.Content = "Checkup reverted — the model was restored to its pre-checkup state.";
                    this.CheckupResults.Visibility = Visibility.Visible;

                    RunQTO.doc.Views.Redraw();

                    Logger.Info("Checkup reverted: the QTO Checkup undo record was rolled back.");
                }
                else
                {
                    Logger.Warn("Revert checkup: the QTO Checkup record is no longer the topmost undo record " +
                        "(or the undo itself failed); nothing was reverted.");

                    // The record is not on top of the undo stack (or the document
                    // changed), so a blind Undo would revert the wrong thing.
                    this.RevertCheckupButton.IsEnabled = false;
                    this.checkupUndoRecordSerial = 0;

                    MessageBox.Show("The checkup could not be reverted automatically." + Environment.NewLine + Environment.NewLine +
                        "Use Rhino's Undo command to step back through the recent changes instead.");
                }
            }
            finally
            {
                this.suppressDocEvents = false;
                this.UnsubscribeRevertWatch();
                this.revertRunning = false;
            }
        }

        /*---------------- Revert-validity watch ----------------*/
        // Revert relies on RhinoDoc.Undo(), which pops only the topmost undo
        // record. Any document change after the checkup pushes a new record on
        // top, so the first foreign change permanently invalidates Revert.
        // Object events alone are not enough: layer edits from the modeless
        // Layers panel (lock, rename, color), material, group, dimstyle, and
        // block-definition edits are undoable but fire only their table events.
        // Undoable changes with no RhinoCommon event at all (linetype edits,
        // ...) are caught by the topmost-record serial check in
        // RevertCheckup_Clicked.
        private void SubscribeRevertWatch()
        {
            // Always -= before += so handlers never stack.
            this.UnsubscribeRevertWatch();

            RhinoDoc.AddRhinoObject += OnDocObjectChanged_InvalidateRevert;
            RhinoDoc.DeleteRhinoObject += OnDocObjectChanged_InvalidateRevert;
            RhinoDoc.UndeleteRhinoObject += OnDocObjectChanged_InvalidateRevert;
            RhinoDoc.ReplaceRhinoObject += OnDocObjectReplaced_InvalidateRevert;
            RhinoDoc.ModifyObjectAttributes += OnDocObjectAttributesModified_InvalidateRevert;
            RhinoDoc.LayerTableEvent += OnLayerTableChanged_InvalidateRevert;
            RhinoDoc.MaterialTableEvent += OnMaterialTableChanged_InvalidateRevert;
            RhinoDoc.GroupTableEvent += OnGroupTableChanged_InvalidateRevert;
            RhinoDoc.DimensionStyleTableEvent += OnDimStyleTableChanged_InvalidateRevert;
            RhinoDoc.InstanceDefinitionTableEvent += OnInstanceDefinitionTableChanged_InvalidateRevert;
        }

        private void UnsubscribeRevertWatch()
        {
            RhinoDoc.AddRhinoObject -= OnDocObjectChanged_InvalidateRevert;
            RhinoDoc.DeleteRhinoObject -= OnDocObjectChanged_InvalidateRevert;
            RhinoDoc.UndeleteRhinoObject -= OnDocObjectChanged_InvalidateRevert;
            RhinoDoc.ReplaceRhinoObject -= OnDocObjectReplaced_InvalidateRevert;
            RhinoDoc.ModifyObjectAttributes -= OnDocObjectAttributesModified_InvalidateRevert;
            RhinoDoc.LayerTableEvent -= OnLayerTableChanged_InvalidateRevert;
            RhinoDoc.MaterialTableEvent -= OnMaterialTableChanged_InvalidateRevert;
            RhinoDoc.GroupTableEvent -= OnGroupTableChanged_InvalidateRevert;
            RhinoDoc.DimensionStyleTableEvent -= OnDimStyleTableChanged_InvalidateRevert;
            RhinoDoc.InstanceDefinitionTableEvent -= OnInstanceDefinitionTableChanged_InvalidateRevert;
        }

        private void OnDocObjectChanged_InvalidateRevert(object sender, RhinoObjectEventArgs e)
        {
            this.InvalidateRevert();
        }

        private void OnDocObjectReplaced_InvalidateRevert(object sender, RhinoReplaceObjectEventArgs e)
        {
            this.InvalidateRevert();
        }

        private void OnDocObjectAttributesModified_InvalidateRevert(object sender, RhinoModifyObjectAttributesEventArgs e)
        {
            this.InvalidateRevert();
        }

        private void OnLayerTableChanged_InvalidateRevert(object sender, Rhino.DocObjects.Tables.LayerTableEventArgs e)
        {
            this.InvalidateRevert();
        }

        private void OnMaterialTableChanged_InvalidateRevert(object sender, Rhino.DocObjects.Tables.MaterialTableEventArgs e)
        {
            this.InvalidateRevert();
        }

        private void OnGroupTableChanged_InvalidateRevert(object sender, Rhino.DocObjects.Tables.GroupTableEventArgs e)
        {
            this.InvalidateRevert();
        }

        private void OnDimStyleTableChanged_InvalidateRevert(object sender, Rhino.DocObjects.Tables.DimStyleTableEventArgs e)
        {
            this.InvalidateRevert();
        }

        private void OnInstanceDefinitionTableChanged_InvalidateRevert(object sender, Rhino.DocObjects.Tables.InstanceDefinitionTableEventArgs e)
        {
            this.InvalidateRevert();
        }

        private void InvalidateRevert()
        {
            if (this.suppressDocEvents)
            {
                return;
            }

            this.UnsubscribeRevertWatch();

            if (this.Dispatcher.CheckAccess())
            {
                this.RevertCheckupButton.IsEnabled = false;
            }
            else
            {
                this.Dispatcher.BeginInvoke(new Action(() =>
                {
                    this.RevertCheckupButton.IsEnabled = false;
                }));
            }
        }

        /*---------------- Window closing ----------------*/
        private void Window_Closing(object sender, System.ComponentModel.CancelEventArgs e)
        {
            // Static RhinoDoc events outlive the window; without this, reopening
            // the window stacks a second copy of every handler.
            RhinoDoc.SelectObjects -= OnSelectObjects;
            RhinoDoc.DeselectObjects -= OnDeselectObjects;
            RhinoDoc.DeselectAllObjects -= OnDeselectAllObjects;

            this.UnsubscribeRevertWatch();
        }

        /*---------------- Handeling Select Object Event ----------------*/
        private void ObjectSelection_Activated(object sender, RoutedEventArgs e)
        {
            ToggleButton btn = sender as ToggleButton;

            RhinoObject rhobj = RunQTO.doc.Objects.FindId(new Guid(btn.Uid));

            // The row can outlive its object (undo, revert, manual delete);
            // a stale toggle must not throw.
            if (rhobj == null)
            {
                return;
            }

            selectedObjects.Add(rhobj);

            rhobj.Select(true);

            RunQTO.doc.Views.Redraw();
        }

        /*---------------- Handeling Deselect Object Event ----------------*/
        private void ObjectDeselection_Activated(object sender, RoutedEventArgs e)
        {
            ToggleButton btn = sender as ToggleButton;

            RhinoObject rhobj = RunQTO.doc.Objects.FindId(new Guid(btn.Uid));

            // The row can outlive its object (undo, revert, manual delete);
            // a stale toggle must not throw.
            if (rhobj == null)
            {
                return;
            }

            selectedObjects.Remove(rhobj);

            rhobj.Select(false);

            RunQTO.doc.Views.Redraw();
        }

        private void Calculate_Concrete_Clicked(object sender, RoutedEventArgs e)
        {
            // Clicks queued while a calculation blocked the UI thread must not
            // start a second run: the template containers would end up holding
            // every element twice and the exports would double the quantities.
            if (this.calculateRunning)
            {
                return;
            }

            this.calculateRunning = true;
            this.CalculateQuantitiesButton.IsEnabled = false;

            // Calculate creates no undo record, but red-painting failed objects
            // fires ModifyObjectAttributes; without the suppression that event
            // would tear down the revert watch even though the "QTO Checkup"
            // record is still topmost and Revert would still work.
            this.suppressDocEvents = true;

            try
            {
                this.CalculateConcrete();
            }
            finally
            {
                this.suppressDocEvents = false;

                // ApplicationIdle: clicks queued while the UI thread was blocked
                // are dispatched at input priority first, so they land on the
                // button while it is still disabled instead of starting another run.
                this.Dispatcher.BeginInvoke(new Action(() =>
                {
                    this.CalculateQuantitiesButton.IsEnabled = true;
                    this.calculateRunning = false;
                }), DispatcherPriority.ApplicationIdle);
            }
        }

        private void CalculateConcrete()
        {
            // Create a new Stopwatch instance
            Stopwatch stopwatch = new Stopwatch();

            // Start the stopwatch
            stopwatch.Start();

            Thread newWindowThread = new Thread(new ThreadStart(() =>
            {
                // Create our context, and install it:
                SynchronizationContext.SetSynchronizationContext(
                    new DispatcherSynchronizationContext(
                        Dispatcher.CurrentDispatcher));

                // Create and configure the window
                ProgressWindow progressWindow = new ProgressWindow();

                // When the window closes, shut down the dispatcher
                progressWindow.Closed += (s, eventArg) =>
                   Dispatcher.CurrentDispatcher.BeginInvokeShutdown(DispatcherPriority.Background);

                progressWindow.Show();
                // Start the Dispatcher Processing
                Dispatcher.Run();
            }));

            newWindowThread.SetApartmentState(ApartmentState.STA);
            // Make the thread a background thread
            newWindowThread.IsBackground = true;
            // Start the thread
            newWindowThread.Start();

            ComboBox selectedConcreteTemplate;

            RhinoObject[] rhobjs;

            RhinoObject rhobj = null;

            string selectedTemplate;

            string layerName;

            System.Drawing.Color layerColor;

            Dictionary<string, List<object>> layerTemplates;

            int badGeometryCount = 0;

            bool calculateSucceeded = false;

            this.allBeams.Clear();
            this.allColumns.Clear();
            this.allCurbs.Clear();
            this.allFootings.Clear();
            this.allWalls.Clear();
            this.allContinuousFootings.Clear();
            this.allSlabs.Clear();
            this.allStyrofoams.Clear();
            // Without this clear a second run doubles every stair in the IFC
            // export, which iterates this.allStairs directly.
            this.allStairs.Clear();

            this.allSelectedTemplates.Clear();
            this.allSelectedTemplateValues.Clear();

            this.selectedConcreteTemplatesForLayers.Clear();

            this.DissipatedConcreteTablePanel.Children.Clear();
            //this.CombinedConcreteTablePanel.Children.Clear();

            double angleThreshold = Methods.CalculateAngleThreshold(this.AngleThresholdSlider.Value);

            Logger.Info("Calculate started. Angle threshold: " + angleThreshold + " | Floors defined: " + ElevationInput.floorElevations.Count);

            try
            {
                for (int i = 0; i < RunQTO.doc.Layers.Count; i++)
                {
                    if (RunQTO.doc.Layers[i].IsDeleted == false)
                    {
                        selectedConcreteTemplate = LogicalTreeHelper.FindLogicalNode(this.ConcreteTemplateGrid,
                            "ConcreteTemplates_" + i.ToString()) as ComboBox;

                        // Layers without a template row (no objects on them, e.g. level
                        // grouping layers) take no part in the calculation.
                        if (selectedConcreteTemplate == null)
                        {
                            Logger.Info("Calculate: skipping layer without a template row: " + RunQTO.doc.Layers[i].FullPath);

                            continue;
                        }

                        selectedTemplate = selectedConcreteTemplate.SelectedItem.ToString().Split(':').Last().Replace(" ", string.Empty);

                        layerName = RunQTO.doc.Layers[i].Name;

                        layerColor = RunQTO.doc.Layers[i].Color;

                        // Short layer names can repeat under different parent layers,
                        // so dictionary keys use the unique full path.
                        string layerPath = RunQTO.doc.Layers[i].FullPath;

                        if (layerName.Split('_').Length >= 2)
                        {
                            this.selectedConcreteTemplatesForLayers.Add(layerPath, selectedTemplate);

                            Logger.Info("Calculate: layer '" + layerPath + "' -> template '" + selectedTemplate + "'");

                            layerTemplates = new Dictionary<string, List<object>>();

                            if (selectedTemplate == "Beam")
                            {
                                rhobjs = RunQTO.doc.Objects.FindByLayer(RunQTO.doc.Layers[i]);

                                for (int j = 0; j < rhobjs.Length; j++)
                                {
                                    try
                                    {
                                        rhobj = rhobjs[j];

                                        // Curves and points have no quantities and are skipped, but
                                        // take-off geometry the checkup left as-is (locked
                                        // extrusions/meshes/blocks) is counted and flagged instead
                                        // of being silently dropped from the takeoff.
                                        if (!this.IsMeasurableBrep(rhobj, layerName, ref badGeometryCount))
                                        {
                                            continue;
                                        }

                                        BeamTemplate beam = new BeamTemplate(rhobj, layerName, layerColor, angleThreshold, ElevationInput.floorElevations);

                                        if (allBeams.allTemplates.ContainsKey(beam.floor))
                                        {
                                            allBeams.allTemplates[beam.floor].Add(beam);
                                        }
                                        else
                                        {
                                            allBeams.allTemplates.Add(beam.floor, new List<object> { beam });
                                        }

                                        string layernameAndFloorName = layerName + String.Format("({0})", beam.floor);

                                        if (!layerTemplates.ContainsKey(layernameAndFloorName))
                                        {
                                            layerTemplates[layernameAndFloorName] = new List<object>() { beam };
                                        }
                                        else
                                        {
                                            layerTemplates[layernameAndFloorName].Add(beam);
                                        }

                                        // This section is for future to capture interaction between slab and beam
                                        if (allSlabs.allTemplates.ContainsKey(beam.floor))
                                        {
                                            foreach (var item in allSlabs.allTemplates[beam.floor])
                                            {
                                                SlabTemplate slabTemplate = (SlabTemplate)item;

                                                if (!slabTemplate.beams.ContainsKey(beam.id))
                                                {
                                                    slabTemplate.beams.Add(beam.id, beam);
                                                }
                                            }
                                        }
                                    }
                                    catch (Exception ex)
                                    {
                                        badGeometryCount++;
                                        Logger.Error("Calculate: object " + rhobj.Id + " on layer '" + layerName + "' failed as '" + selectedTemplate + "'.", ex);
                                        Methods.HighlightBadGeometry(rhobj);
                                    }
                                }

                                quantityValues = new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "NET VOLUME", "BOTTOM AREA", "END AREA",
                            "SIDE-1", "SIDE-2", "LENGTH", "OPENING AREA" ,"ISOLATE" };
                            }

                            if (selectedTemplate == "Column")
                            {
                                rhobjs = RunQTO.doc.Objects.FindByLayer(RunQTO.doc.Layers[i]);

                                for (int j = 0; j < rhobjs.Length; j++)
                                {
                                    try
                                    {
                                        rhobj = rhobjs[j];

                                        // Curves and points have no quantities and are skipped, but
                                        // take-off geometry the checkup left as-is (locked
                                        // extrusions/meshes/blocks) is counted and flagged instead
                                        // of being silently dropped from the takeoff.
                                        if (!this.IsMeasurableBrep(rhobj, layerName, ref badGeometryCount))
                                        {
                                            continue;
                                        }

                                        ColumnTemplate column = new ColumnTemplate(rhobj, layerName, layerColor, true, ElevationInput.floorElevations);

                                        if (allColumns.allTemplates.ContainsKey(column.floor))
                                        {
                                            allColumns.allTemplates[column.floor].Add(column);
                                        }
                                        else
                                        {
                                            allColumns.allTemplates.Add(column.floor, new List<object> { column });
                                        }

                                        string layernameAndFloorName = layerName + String.Format("({0})", column.floor);

                                        if (!layerTemplates.ContainsKey(layernameAndFloorName))
                                        {
                                            layerTemplates[layernameAndFloorName] = new List<object>() { column };
                                        }
                                        else
                                        {
                                            layerTemplates[layernameAndFloorName].Add(column);
                                        }
                                    }
                                    catch (Exception ex)
                                    {
                                        badGeometryCount++;
                                        Logger.Error("Calculate: object " + rhobj.Id + " on layer '" + layerName + "' failed as '" + selectedTemplate + "'.", ex);
                                        Methods.HighlightBadGeometry(rhobj);
                                    }
                                }

                                quantityValues = new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "HEIGHT", "SIDE AREA", "ISOLATE" };
                            }

                            if (selectedTemplate.Contains("Non-Rectangular"))
                            {
                                rhobjs = RunQTO.doc.Objects.FindByLayer(RunQTO.doc.Layers[i]);

                                for (int j = 0; j < rhobjs.Length; j++)
                                {
                                    try
                                    {
                                        rhobj = rhobjs[j];

                                        // Curves and points have no quantities and are skipped, but
                                        // take-off geometry the checkup left as-is (locked
                                        // extrusions/meshes/blocks) is counted and flagged instead
                                        // of being silently dropped from the takeoff.
                                        if (!this.IsMeasurableBrep(rhobj, layerName, ref badGeometryCount))
                                        {
                                            continue;
                                        }

                                        ColumnTemplate column = new ColumnTemplate(rhobj, layerName, layerColor, false, ElevationInput.floorElevations);

                                        if (allColumns.allTemplates.ContainsKey(column.floor))
                                        {
                                            allColumns.allTemplates[column.floor].Add(column);
                                        }
                                        else
                                        {
                                            allColumns.allTemplates.Add(column.floor, new List<object> { column });
                                        }

                                        string layernameAndFloorName = layerName + String.Format("({0})", column.floor);

                                        if (!layerTemplates.ContainsKey(layernameAndFloorName))
                                        {
                                            layerTemplates[layernameAndFloorName] = new List<object>() { column };
                                        }
                                        else
                                        {
                                            layerTemplates[layernameAndFloorName].Add(column);
                                        }
                                    }
                                    catch (Exception ex)
                                    {
                                        badGeometryCount++;
                                        Logger.Error("Calculate: object " + rhobj.Id + " on layer '" + layerName + "' failed as '" + selectedTemplate + "'.", ex);
                                        Methods.HighlightBadGeometry(rhobj);
                                    }
                                }

                                quantityValues = new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "HEIGHT", "SIDE AREA", "ISOLATE" };
                            }

                            if (selectedTemplate == "ContinuousFooting")
                            {
                                rhobjs = RunQTO.doc.Objects.FindByLayer(RunQTO.doc.Layers[i]);

                                for (int j = 0; j < rhobjs.Length; j++)
                                {
                                    try
                                    {
                                        rhobj = rhobjs[j];

                                        // Curves and points have no quantities and are skipped, but
                                        // take-off geometry the checkup left as-is (locked
                                        // extrusions/meshes/blocks) is counted and flagged instead
                                        // of being silently dropped from the takeoff.
                                        if (!this.IsMeasurableBrep(rhobj, layerName, ref badGeometryCount))
                                        {
                                            continue;
                                        }

                                        ContinuousFootingTemplate continuousFooting = new ContinuousFootingTemplate(rhobj, layerName, layerColor, angleThreshold, ElevationInput.floorElevations);

                                        if (allContinuousFootings.allTemplates.ContainsKey(continuousFooting.floor))
                                        {
                                            allContinuousFootings.allTemplates[continuousFooting.floor].Add(continuousFooting);
                                        }
                                        else
                                        {
                                            allContinuousFootings.allTemplates.Add(continuousFooting.floor, new List<object> { continuousFooting });
                                        }

                                        string layernameAndFloorName = layerName + String.Format("({0})", continuousFooting.floor);

                                        if (!layerTemplates.ContainsKey(layernameAndFloorName))
                                        {
                                            layerTemplates[layernameAndFloorName] = new List<object>() { continuousFooting };
                                        }
                                        else
                                        {
                                            layerTemplates[layernameAndFloorName].Add(continuousFooting);
                                        }
                                    }
                                    catch (Exception ex)
                                    {
                                        badGeometryCount++;
                                        Logger.Error("Calculate: object " + rhobj.Id + " on layer '" + layerName + "' failed as '" + selectedTemplate + "'.", ex);
                                        Methods.HighlightBadGeometry(rhobj);
                                    }
                                }

                                quantityValues = new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "NET VOLUME", "TOP AREA", "BOTTOM AREA", "END AREA",
                            "SIDE-1", "SIDE-2", "LENGTH", "OPENING AREA" ,"ISOLATE" };
                            }

                            if (selectedTemplate == "Curb")
                            {
                                rhobjs = RunQTO.doc.Objects.FindByLayer(RunQTO.doc.Layers[i]);

                                for (int j = 0; j < rhobjs.Length; j++)
                                {
                                    try
                                    {
                                        rhobj = rhobjs[j];

                                        // Curves and points have no quantities and are skipped, but
                                        // take-off geometry the checkup left as-is (locked
                                        // extrusions/meshes/blocks) is counted and flagged instead
                                        // of being silently dropped from the takeoff.
                                        if (!this.IsMeasurableBrep(rhobj, layerName, ref badGeometryCount))
                                        {
                                            continue;
                                        }

                                        CurbTemplate curb = new CurbTemplate(rhobj, layerName, layerColor, angleThreshold, ElevationInput.floorElevations);

                                        if (allCurbs.allTemplates.ContainsKey(curb.floor))
                                        {
                                            allCurbs.allTemplates[curb.floor].Add(curb);
                                        }
                                        else
                                        {
                                            allCurbs.allTemplates.Add(curb.floor, new List<object> { curb });
                                        }

                                        string layernameAndFloorName = layerName + String.Format("({0})", curb.floor);

                                        if (!layerTemplates.ContainsKey(layernameAndFloorName))
                                        {
                                            layerTemplates[layernameAndFloorName] = new List<object>() { curb };
                                        }
                                        else
                                        {
                                            layerTemplates[layernameAndFloorName].Add(curb);
                                        }
                                    }
                                    catch (Exception ex)
                                    {
                                        badGeometryCount++;
                                        Logger.Error("Calculate: object " + rhobj.Id + " on layer '" + layerName + "' failed as '" + selectedTemplate + "'.", ex);
                                        Methods.HighlightBadGeometry(rhobj);
                                    }
                                }

                                quantityValues = new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "NET VOLUME", "TOP AREA", "END AREA",
                            "SIDE-1", "SIDE-2", "LENGTH", "OPENING AREA" ,"ISOLATE" };
                            }

                            if (selectedTemplate == "Footing")
                            {
                                rhobjs = RunQTO.doc.Objects.FindByLayer(RunQTO.doc.Layers[i]);

                                for (int j = 0; j < rhobjs.Length; j++)
                                {
                                    try
                                    {
                                        rhobj = rhobjs[j];

                                        // Curves and points have no quantities and are skipped, but
                                        // take-off geometry the checkup left as-is (locked
                                        // extrusions/meshes/blocks) is counted and flagged instead
                                        // of being silently dropped from the takeoff.
                                        if (!this.IsMeasurableBrep(rhobj, layerName, ref badGeometryCount))
                                        {
                                            continue;
                                        }

                                        FootingTemplate footing = new FootingTemplate(rhobj, layerName, layerColor, angleThreshold, ElevationInput.floorElevations);

                                        if (allFootings.allTemplates.ContainsKey(footing.floor))
                                        {
                                            allFootings.allTemplates[footing.floor].Add(footing);
                                        }
                                        else
                                        {
                                            allFootings.allTemplates.Add(footing.floor, new List<object> { footing });
                                        }

                                        string layernameAndFloorName = layerName + String.Format("({0})", footing.floor);

                                        if (!layerTemplates.ContainsKey(layernameAndFloorName))
                                        {
                                            layerTemplates[layernameAndFloorName] = new List<object>() { footing };
                                        }
                                        else
                                        {
                                            layerTemplates[layernameAndFloorName].Add(footing);
                                        }
                                    }
                                    catch (Exception ex)
                                    {
                                        badGeometryCount++;
                                        Logger.Error("Calculate: object " + rhobj.Id + " on layer '" + layerName + "' failed as '" + selectedTemplate + "'.", ex);
                                        Methods.HighlightBadGeometry(rhobj);
                                    }
                                }

                                quantityValues = new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "TOP AREA", "BOTTOM AREA", "SIDE AREA", "ISOLATE" };
                            }

                            if (selectedTemplate == "Wall")
                            {
                                rhobjs = RunQTO.doc.Objects.FindByLayer(RunQTO.doc.Layers[i]);

                                for (int j = 0; j < rhobjs.Length; j++)
                                {
                                    try
                                    {
                                        rhobj = rhobjs[j];

                                        // Curves and points have no quantities and are skipped, but
                                        // take-off geometry the checkup left as-is (locked
                                        // extrusions/meshes/blocks) is counted and flagged instead
                                        // of being silently dropped from the takeoff.
                                        if (!this.IsMeasurableBrep(rhobj, layerName, ref badGeometryCount))
                                        {
                                            continue;
                                        }

                                        WallTemplate wall = new WallTemplate(rhobj, layerName, layerColor, angleThreshold, ElevationInput.floorElevations);

                                        if (allWalls.allTemplates.ContainsKey(wall.floor))
                                        {
                                            allWalls.allTemplates[wall.floor].Add(wall);
                                        }
                                        else
                                        {
                                            allWalls.allTemplates.Add(wall.floor, new List<object> { wall });
                                        }

                                        string layernameAndFloorName = layerName + String.Format("({0})", wall.floor);

                                        if (!layerTemplates.ContainsKey(layernameAndFloorName))
                                        {
                                            layerTemplates[layernameAndFloorName] = new List<object>() { wall };
                                        }
                                        else
                                        {
                                            layerTemplates[layernameAndFloorName].Add(wall);
                                        }
                                    }
                                    catch (Exception ex)
                                    {
                                        badGeometryCount++;
                                        Logger.Error("Calculate: object " + rhobj.Id + " on layer '" + layerName + "' failed as '" + selectedTemplate + "'.", ex);
                                        Methods.HighlightBadGeometry(rhobj);
                                    }
                                }

                                quantityValues = new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "NET VOLUME", "TOP AREA", "END AREA",
                            "SIDE-1", "SIDE-2", "LENGTH", "OPENING AREA" ,"ISOLATE" };
                            }

                            if (selectedTemplate == "Slab")
                            {
                                rhobjs = RunQTO.doc.Objects.FindByLayer(RunQTO.doc.Layers[i]);

                                for (int j = 0; j < rhobjs.Length; j++)
                                {
                                    try
                                    {
                                        rhobj = rhobjs[j];

                                        // Curves and points have no quantities and are skipped, but
                                        // take-off geometry the checkup left as-is (locked
                                        // extrusions/meshes/blocks) is counted and flagged instead
                                        // of being silently dropped from the takeoff.
                                        if (!this.IsMeasurableBrep(rhobj, layerName, ref badGeometryCount))
                                        {
                                            continue;
                                        }

                                        SlabTemplate slab = new SlabTemplate(rhobj, layerName, layerColor, angleThreshold, ElevationInput.floorElevations);

                                        if (allSlabs.allTemplates.ContainsKey(slab.floor))
                                        {
                                            allSlabs.allTemplates[slab.floor].Add(slab);
                                        }
                                        else
                                        {
                                            allSlabs.allTemplates.Add(slab.floor, new List<object> { slab });
                                        }

                                        string layernameAndFloorName = layerName + String.Format("({0})", slab.floor);

                                        if (!layerTemplates.ContainsKey(layernameAndFloorName))
                                        {
                                            layerTemplates[layernameAndFloorName] = new List<object>() { slab };
                                        }
                                        else
                                        {
                                            layerTemplates[layernameAndFloorName].Add(slab);
                                        }

                                        if (allBeams.allTemplates.ContainsKey(slab.floor))
                                        {
                                            foreach (var item in allBeams.allTemplates[slab.floor])
                                            {
                                                BeamTemplate beamTemplate = (BeamTemplate)item;

                                                if (!slab.beams.ContainsKey(beamTemplate.id))
                                                {
                                                    slab.beams.Add(beamTemplate.id, beamTemplate);
                                                }
                                            }
                                        }
                                    }
                                    catch (Exception ex)
                                    {
                                        badGeometryCount++;
                                        Logger.Error("Calculate: object " + rhobj.Id + " on layer '" + layerName + "' failed as '" + selectedTemplate + "'.", ex);
                                        Methods.HighlightBadGeometry(rhobj);
                                    }
                                }

                                quantityValues = new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "NET VOLUME", "TOP AREA", "BOTTOM AREA", "EDGE AREA", "PERIMETER", "OPENING PERIMETER", "ISOLATE" };
                            }

                            if (selectedTemplate == "Styrofoam")
                            {
                                rhobjs = RunQTO.doc.Objects.FindByLayer(RunQTO.doc.Layers[i]);

                                for (int j = 0; j < rhobjs.Length; j++)
                                {
                                    try
                                    {
                                        rhobj = rhobjs[j];

                                        // Curves and points have no quantities and are skipped, but
                                        // take-off geometry the checkup left as-is (locked
                                        // extrusions/meshes/blocks) is counted and flagged instead
                                        // of being silently dropped from the takeoff.
                                        if (!this.IsMeasurableBrep(rhobj, layerName, ref badGeometryCount))
                                        {
                                            continue;
                                        }

                                        StyrofoamTemplate styrofoam = new StyrofoamTemplate(rhobj, layerName, layerColor, ElevationInput.floorElevations);

                                        if (allStyrofoams.allTemplates.ContainsKey(styrofoam.floor))
                                        {
                                            allStyrofoams.allTemplates[styrofoam.floor].Add(styrofoam);
                                        }
                                        else
                                        {
                                            allStyrofoams.allTemplates.Add(styrofoam.floor, new List<object> { styrofoam });
                                        }

                                        string layernameAndFloorName = layerName + String.Format("({0})", styrofoam.floor);

                                        if (!layerTemplates.ContainsKey(layernameAndFloorName))
                                        {
                                            layerTemplates[layernameAndFloorName] = new List<object>() { styrofoam };
                                        }
                                        else
                                        {
                                            layerTemplates[layernameAndFloorName].Add(styrofoam);
                                        }
                                    }
                                    catch (Exception ex)
                                    {
                                        badGeometryCount++;
                                        Logger.Error("Calculate: object " + rhobj.Id + " on layer '" + layerName + "' failed as '" + selectedTemplate + "'.", ex);
                                        Methods.HighlightBadGeometry(rhobj);
                                    }
                                }

                                quantityValues = new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "ISOLATE" };
                            }

                            if (selectedTemplate == "Stair")
                            {
                                rhobjs = RunQTO.doc.Objects.FindByLayer(RunQTO.doc.Layers[i]);

                                for (int j = 0; j < rhobjs.Length; j++)
                                {
                                    try
                                    {
                                        rhobj = rhobjs[j];

                                        // Curves and points have no quantities and are skipped, but
                                        // take-off geometry the checkup left as-is (locked
                                        // extrusions/meshes/blocks) is counted and flagged instead
                                        // of being silently dropped from the takeoff.
                                        if (!this.IsMeasurableBrep(rhobj, layerName, ref badGeometryCount))
                                        {
                                            continue;
                                        }

                                        StairTemplate stair = new StairTemplate(rhobj, layerName, layerColor, angleThreshold, ElevationInput.floorElevations);

                                        if (allStairs.allTemplates.ContainsKey(stair.floor))
                                        {
                                            allStairs.allTemplates[stair.floor].Add(stair);
                                        }
                                        else
                                        {
                                            allStairs.allTemplates.Add(stair.floor, new List<object> { stair });
                                        }

                                        string layernameAndFloorName = layerName + String.Format("({0})", stair.floor);

                                        if (!layerTemplates.ContainsKey(layernameAndFloorName))
                                        {
                                            layerTemplates[layernameAndFloorName] = new List<object>() { stair };
                                        }
                                        else
                                        {
                                            layerTemplates[layernameAndFloorName].Add(stair);
                                        }
                                    }
                                    catch (Exception ex)
                                    {
                                        badGeometryCount++;
                                        Logger.Error("Calculate: object " + rhobj.Id + " on layer '" + layerName + "' failed as '" + selectedTemplate + "'.", ex);
                                        Methods.HighlightBadGeometry(rhobj);
                                    }
                                }

                                quantityValues = new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "TREAD AREA", "RISER AREA", "TREAD COUNT", "SIDE AREA", "BOTTOM AREA", "ISOLATE" };
                            }

                            if (quantityValues.Count > 0)
                            {
                                quantityValues.InsertRange(1, this.layerPropertyColumnHeaders);
                            }

                            // Generate Dissipated Value Table
                            UIMethods.GenerateConcreteTableExpander(this.DissipatedConcreteTablePanel, layerName, selectedTemplate,
                                layerTemplates, quantityValues, this.layerPropertyColumnHeaders, ObjectSelection_Activated, ObjectDeselection_Activated);

                            quantityValues.Clear();

                            if (selectedTemplate == "N/A")
                            {
                                continue;
                            }
                        }
                        else
                        {
                            if (selectedTemplate != "N/A")
                            {
                                MessageBox.Show(layerName + "=>" + "INCOMPATIBLE LAYER NAMING SCHEME!");
                            }
                        }
                    }
                }

                foreach (var item in allSlabs.allTemplates)
                {
                    foreach (SlabTemplate slab in item.Value)
                    {
                        slab.UpdateNetVolumeAndBottomAreaWithBeams();

                        ((TextBlock)(Methods.GetByUid(this.DissipatedConcreteTablePanel, slab.id + "_NetVolume"))).Text = slab.netVolume.ToString();

                        ((TextBlock)(Methods.GetByUid(this.DissipatedConcreteTablePanel, slab.id + "_BottomArea"))).Text = slab.bottomArea.ToString();
                    }
                }

                this.allSelectedTemplates.Add("Beam", allBeams);
                this.allSelectedTemplateValues.Add("Beam", new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "BOTTOM AREA", "SIDE AREA", "LENGTH", "ISOLATE" });
                this.allSelectedTemplates.Add("Column", allColumns);
                this.allSelectedTemplateValues.Add("Column", new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "HEIGHT", "SIDE AREA", "ISOLATE" });
                this.allSelectedTemplates.Add("Curb", allCurbs);
                this.allSelectedTemplateValues.Add("Curb", new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "TOP AREA", "SIDE AREA", "LENGTH", "ISOLATE" });
                this.allSelectedTemplates.Add("Footing", allFootings);
                this.allSelectedTemplateValues.Add("Footing", new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "TOP AREA", "BOTTOM AREA", "SIDE AREA", "ISOLATE" });
                this.allSelectedTemplates.Add("Wall", allWalls);
                this.allSelectedTemplateValues.Add("Wall", new List<string>() { "COUNT", "NAME ABB.","FLOOR", "GROSS VOLUME", "NET VOLUME", "TOP AREA", "END AREA",
                        "SIDE-1", "SIDE-2", "LENGTH", "OPENING AREA" ,"ISOLATE" });
                this.allSelectedTemplates.Add("Continuous Footing", allContinuousFootings);
                this.allSelectedTemplateValues.Add("Continuous Footing", new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "TOP AREA", "BOTTOM AREA", "SIDE AREA", "LENGTH", "ISOLATE" });
                this.allSelectedTemplates.Add("Slab", allSlabs);
                this.allSelectedTemplateValues.Add("Slab", new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "NET VOLUME", "TOP AREA", "BOTTOM AREA", "EDGE AREA", "PERIMETER", "OPENING PERIMETER", "ISOLATE" });
                this.allSelectedTemplates.Add("Styrofoam", allStyrofoams);
                this.allSelectedTemplateValues.Add("Styrofoam", new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "GROSS VOLUME", "ISOLATE" });
                this.allSelectedTemplates.Add("Stair", allStairs);
                this.allSelectedTemplateValues.Add("Stair", new List<string>() { "COUNT", "NAME ABB.", "FLOOR", "VOLUME", "TREAD AREA", "RISER AREA", "TREAD COUNT", "SIDE AREA", "BOTTOM AREA", "ISOLATE" });

                // Generate Combined Value Table
                //UIMethods.GenerateCombinedTableExpander(this.CombinedConcreteTablePanel, this.allSelectedTemplates,
                //    this.allSelectedTemplateValues, ObjectSelection_Activated, ObjectDeselection_Activated);

                if (CombinedValuesToggle.IsChecked == true)
                {
                    this.DissipatedConcreteTablePanel.Visibility = Visibility.Collapsed;
                    //this.CombinedConcreteTablePanel.Visibility = Visibility.Visible;
                }

                else
                {
                    this.DissipatedConcreteTablePanel.Visibility = Visibility.Visible;
                    //this.CombinedConcreteTablePanel.Visibility = Visibility.Collapsed;
                }

                // Exports stay enabled even when some objects failed: the measured
                // objects export normally, the failed ones have no quantities anywhere.
                this.ExportExcelButton.IsEnabled = true;
                this.ExportIFC.IsEnabled = true;

                Dispatcher.FromThread(newWindowThread)?.InvokeShutdown();

                if (badGeometryCount > 0)
                {
                    RunQTO.doc.Views.Redraw();

                    MessageBox.Show(badGeometryCount.ToString() + " object(s) could not be measured and have NO quantities. " +
                        "They are highlighted in red and are excluded from all exports.");
                }

                calculateSucceeded = true;

                // The freshness stamp gates the formwork window: written at
                // exactly this one site, the completed take-off. Never let a
                // stamp failure break the calculation itself.
                try
                {
                    FormworkMethods.WriteStamp(RunQTO.doc);
                }
                catch (Exception stampEx)
                {
                    Logger.Error("Formwork freshness stamp could not be written.", stampEx);
                }
            }

            catch (Exception ex)
            {
                Dispatcher.FromThread(newWindowThread)?.InvokeShutdown();

                // The containers were cleared and only partially repopulated, so an
                // export now would silently miss part of the takeoff.
                this.ExportExcelButton.IsEnabled = false;
                this.ExportIFC.IsEnabled = false;

                Logger.Error("Calculate failed.", ex);

                MessageBox.Show("Something went wrong! Log file: " + Logger.LogFilePath);

                MessageBox.Show(ex.ToString());
            }
            // Get the elapsed time as a TimeSpan value
            TimeSpan ts = stopwatch.Elapsed;

            // Format and display the TimeSpan value
            string calculationTime = ts.TotalSeconds.ToString("F6");

            Logger.Info("Calculate finished in " + calculationTime + " s. Bad geometry count: " + badGeometryCount + ".");

            // The success dialog belongs to a clean run only; a run with failures
            // already showed its own message, and an aborted run showed the error.
            if (calculateSucceeded && badGeometryCount == 0)
            {
                MessageBox.Show("Calculation process completed in: " + calculationTime + " seconds!");
            }
        }

        /// <summary>
        /// True when the object is a Brep the templates can measure. Curves,
        /// points, and annotations return false silently; extrusions, meshes,
        /// and block instances — take-off geometry the checkup deliberately
        /// left as-is because it was locked — also return false but are counted
        /// as bad geometry, logged, and painted red, so they cannot silently
        /// vanish from the exported quantities.
        /// </summary>
        private bool IsMeasurableBrep(RhinoObject rhobj, string layerName, ref int badGeometryCount)
        {
            if (rhobj.Geometry is Rhino.Geometry.Brep)
            {
                return true;
            }

            if (rhobj.Geometry is Rhino.Geometry.Extrusion || rhobj.Geometry is Rhino.Geometry.Mesh ||
                rhobj is InstanceObject)
            {
                badGeometryCount++;

                Logger.Warn("Calculate: object " + rhobj.Id + " on layer '" + layerName +
                    "' was left as-is by the checkup (" + rhobj.GetType().Name +
                    ") and cannot be measured; it has NO quantities.");

                Methods.HighlightBadGeometry(rhobj);
            }

            return false;
        }

        //private void Concrete_Save_Clicked(object sender, RoutedEventArgs e)
        //{
        //    Stream stream = null;
        //    StreamWriter streamWriter = null;

        //    this.saveData.Add("SelectedConcreteTemplatesForLayers", this.selectedConcreteTemplatesForLayers);
        //    this.saveData.Add("AllSelectedTemplates", this.allSelectedTemplates);
        //    this.saveData.Add("AllSelectedTemplateValues", this.allSelectedTemplateValues);
        //    this.saveData.Add("DissipatedConcreteTablePanel", this.DissipatedConcreteTablePanel);
        //    this.saveData.Add("CombinedConcreteTablePanel", this.CombinedConcreteTablePanel);
        //    this.saveData.Add("LayerPropertyColumnHeaders", this.layerPropertyColumnHeaders);

        //    // Json String Of The Save Data
        //    string projectData = JsonConvert.SerializeObject(saveData, Formatting.Indented);

        //    System.Windows.Forms.SaveFileDialog saveFileDialog = new System.Windows.Forms.SaveFileDialog();

        //    if (saveFileDialog.ShowDialog() == System.Windows.Forms.DialogResult.OK)
        //    {
        //        using (stream = File.Open(saveFileDialog.FileName, FileMode.Create))
        //        {
        //            using (streamWriter = new StreamWriter(stream))
        //            {
        //                streamWriter.Write(projectData);
        //            }
        //        }

        //        MessageBox.Show("Save was successful.");
        //    }

        //    else
        //    {
        //        MessageBox.Show("Something went wrong, please try again.");
        //    }
        //}

        //private void Concrete_Load_Clicked(object sender, RoutedEventArgs e)
        //{
        //    System.Windows.Forms.OpenFileDialog openFileDialog = new System.Windows.Forms.OpenFileDialog();
        //    Stream stream = null;

        //    string pathToFile = "";

        //    // Read The File
        //    if (openFileDialog.ShowDialog() == System.Windows.Forms.DialogResult.OK)
        //    {
        //        try
        //        {
        //            stream = openFileDialog.OpenFile();
        //        }
        //        catch (Exception ex)
        //        {
        //            MessageBox.Show("Error: Could not read file from disk. original error: " + ex.Message);
        //            return;
        //        }

        //        if (stream != null)
        //        {
        //            pathToFile = openFileDialog.FileName;

        //            try
        //            {
        //                loadData = JsonConvert.DeserializeObject<Dictionary<string, object>>(File.ReadAllText(pathToFile));
        //            }
        //            catch (Exception ex)
        //            {
        //                MessageBox.Show("Error: Could not open the file. original error: " + ex.Message);
        //                return;
        //            }

        //            // Load The Project
        //            try
        //            {
        //                Dictionary<string, string> tempSelectedConcreteTemplatesForLayers =
        //                    ((JObject)loadData["SelectedConcreteTemplatesForLayers"]).ToObject<Dictionary<string, string>>();

        //                for (int i = 0; i < this.ConcreteTemplateGrid.Children.Count; i++)
        //                {
        //                    if (this.ConcreteTemplateGrid.Children[i].GetType().ToString().Split('.').Last() == "DockPanel")
        //                    {
        //                        DockPanel tempDockPanel = (DockPanel)this.ConcreteTemplateGrid.Children[i];

        //                        Label tempLabel = (Label)tempDockPanel.Children[0];

        //                        if (tempSelectedConcreteTemplatesForLayers.Keys.Contains(tempLabel.Content.ToString()))
        //                        {
        //                            ComboBox tempComboBox = LogicalTreeHelper.FindLogicalNode(this.ConcreteTemplateGrid,
        //                                    "ConcreteTemplates_" + tempLabel.Name.Split('_').Last()) as ComboBox;

        //                            tempComboBox.Text = tempSelectedConcreteTemplatesForLayers[tempLabel.Content.ToString()];
        //                        }

        //                        else
        //                        {
        //                            MessageBox.Show("No saved data exists for layer " + "\"" + tempLabel.Content.ToString() + "\"" + ".");
        //                        }
        //                    }
        //                }
        //            }
        //            catch (Exception ex)
        //            {
        //                MessageBox.Show("Error: Data is corupted, " + ex.Message);
        //            }
        //        }

        //        // Handeling Selected File Not Exist.
        //        else
        //        {
        //            MessageBox.Show("Error: File not found.");
        //            return;
        //        }
        //    }
        //}

        private void Export_Excel_Clicked(object sender, RoutedEventArgs e)
        {
            try
            {
                Logger.Info("Excel export started.");

                ExcelMethods.ExportExcel(this.DissipatedConcreteTablePanel, this.layerPropertyColumnHeaders);

                Logger.Info("Excel export finished.");
            }
            catch (Exception ex)
            {
                Logger.Error("Excel export failed.", ex);

                MessageBox.Show("Excel export failed! Log file: " + Logger.LogFilePath + Environment.NewLine + Environment.NewLine + ex.ToString());
            }
        }

        private void Combined_Values_Toggle_Clicked(object sender, RoutedEventArgs e)
        {
            MessageBox.Show("Not Implemented!");
            //if (this.DissipatedConcreteTablePanel.Visibility == Visibility.Collapsed)
            //{
            //    this.DissipatedConcreteTablePanel.Visibility = Visibility.Visible;

            //    //this.CombinedConcreteTablePanel.Visibility = Visibility.Collapsed;
            //}
            //else
            //{
            //    //this.CombinedConcreteTablePanel.Visibility = Visibility.Visible;

            //    this.DissipatedConcreteTablePanel.Visibility = Visibility.Collapsed;
            //}
        }

        void OnSelectObjects(object sender, RhinoObjectSelectionEventArgs args)
        {
            if (args.Selected && this.ExportExcelButton.IsEnabled) // objects were selected
            {
                foreach (RhinoObject obj in args.RhinoObjects)
                {
                    ToggleButton dissipatedSelectToggleButton = (ToggleButton)(Methods.GetByUid(this.DissipatedConcreteTablePanel, obj.Id.ToString()));

                    //ToggleButton combinedSelectToggleButton = (ToggleButton)(Methods.GetByUid(this.CombinedConcreteTablePanel, obj.Id.ToString()));

                    if (dissipatedSelectToggleButton != null)
                    {
                        dissipatedSelectToggleButton.IsChecked = true;
                    }

                    //if (combinedSelectToggleButton != null)
                    //{
                    //    combinedSelectToggleButton.IsChecked = true;
                    //}
                }
            }
        }

        void OnDeselectObjects(object sender, RhinoObjectSelectionEventArgs args)
        {
            if (!args.Selected && this.ExportExcelButton.IsEnabled) // objects were selected
            {
                // do something
                foreach (RhinoObject obj in args.RhinoObjects)
                {
                    ToggleButton dissipatedSelectToggleButton = (ToggleButton)(Methods.GetByUid(this.DissipatedConcreteTablePanel, obj.Id.ToString()));

                    //ToggleButton combinedSelectToggleButton = (ToggleButton)(Methods.GetByUid(this.CombinedConcreteTablePanel, obj.Id.ToString()));

                    if (dissipatedSelectToggleButton != null)
                    {
                        dissipatedSelectToggleButton.IsChecked = false;
                    }

                    //if (combinedSelectToggleButton != null)
                    //{
                    //    combinedSelectToggleButton.IsChecked = false;
                    //}
                }
            }
        }

        void OnDeselectAllObjects(object sender, RhinoDeselectAllObjectsEventArgs args)
        {
            if (this.ExportExcelButton.IsEnabled)
            {
                Grid contentGrid;
                string elementType;

                foreach (UIElement expander in this.DissipatedConcreteTablePanel.Children)
                {
                    contentGrid = (Grid)(((Expander)expander).Content);

                    foreach (UIElement element in contentGrid.Children)
                    {
                        elementType = (element.GetType().ToString().Split('.')).Last().ToLower();

                        if (elementType == "togglebutton")
                        {
                            ((ToggleButton)element).IsChecked = false;
                        }
                    }
                }

                //foreach (UIElement expander in this.CombinedConcreteTablePanel.Children)
                //{
                //    contentGrid = (Grid)(((Expander)expander).Content);

                //    foreach (UIElement element in contentGrid.Children)
                //    {
                //        elementType = (element.GetType().ToString().Split('.')).Last().ToLower();

                //        if (elementType == "togglebutton")
                //        {
                //            ((ToggleButton)element).IsChecked = false;
                //        }
                //    }
                //}
            }
        }

        private void Blockify_Clicked(object sender, RoutedEventArgs e)
        {
            Methods.Blockify();
        }

        private void Export_IFC_Clicked(object sender, RoutedEventArgs e)
        {
            try
            {
                System.Windows.Forms.SaveFileDialog saveFileDialog = new System.Windows.Forms.SaveFileDialog();
                saveFileDialog.Filter = "IFC |*.ifc";

                if (saveFileDialog.ShowDialog() == System.Windows.Forms.DialogResult.OK)
                {
                    string outputPath = saveFileDialog.FileName;

                    Logger.Info("IFC export started: " + outputPath);

                    // The IfcProject carries the document file name only; the full path
                    // would leak the local folder structure into shared files.
                    string ifcProjectName = System.IO.Path.GetFileNameWithoutExtension(RunQTO.doc.Name);

                    if (String.IsNullOrWhiteSpace(ifcProjectName))
                    {
                        ifcProjectName = "QTO Project";
                    }

                    IfcStore project = IFCMethods.CreateandInitIFCModel(ifcProjectName);

                    IfcBuilding building = IFCMethods.CreateBuilding(project, "Building");

                    AllTemplates[] templateContainers = new AllTemplates[]
                    {
                        this.allWalls, this.allBeams, this.allColumns, this.allContinuousFootings,
                        this.allFootings, this.allSlabs, this.allCurbs, this.allStyrofoams, this.allStairs
                    };

                    // Union of the floor-name bucket keys across all containers, so
                    // every bucket (including "-") can be routed to a storey.
                    HashSet<string> floorNamesInUse = new HashSet<string>();

                    foreach (AllTemplates templateContainer in templateContainers)
                    {
                        floorNamesInUse.UnionWith(templateContainer.allTemplates.Keys);
                    }

                    Dictionary<string, IfcBuildingStorey> storeysByFloorName = IFCMethods.CreateBuildingStoreys(
                        project, building, ElevationInput.floorElevations, floorNamesInUse);

                    Logger.Info("IFC export: " + storeysByFloorName.Count + " storeys created for floor buckets: " + String.Join(", ", storeysByFloorName.Keys));

                    foreach (AllTemplates templateContainer in templateContainers)
                    {
                        IFCMethods.CreateAndAddIFCElement(project, storeysByFloorName, templateContainer);

                        Logger.Info("IFC export: added container " + templateContainer.GetType().Name);
                    }

                    project.SaveAs(outputPath);

                    Logger.Info("IFC export finished: " + outputPath);

                    MessageBox.Show("Export was successful.");

                }

                else
                {
                    MessageBox.Show("Export was canceled.");
                }
            }
            catch (Exception ex)
            {
                Logger.Error("IFC export failed.", ex);

                MessageBox.Show("IFC export failed! Log file: " + Logger.LogFilePath + Environment.NewLine + Environment.NewLine + ex.ToString());
            }

        }
    }
}
