using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Navigation;
using System.Windows.Shapes;

namespace QTO_Tool
{
    /// <summary>
    /// Interaction logic for ElevationInput.xaml
    /// </summary>
    public partial class ElevationInput : Window
    {
        public static Dictionary<double, string> floorElevations = new Dictionary<double, string>();

        public event EventHandler ChangeSetFloorButtonColorRequest;

        public ElevationInput()
        {
            InitializeComponent();
        }

        // The document this dialog was opened against. The dialog is modeless
        // and survives File > Open; accepting it then would transplant the
        // previous project's floor table into the new document.
        private uint loadedDocSerial = 0;

        private void Window_Loaded(object sender, RoutedEventArgs e)
        {
            Rhino.RhinoDoc activeDoc = Rhino.RhinoDoc.ActiveDoc;
            this.loadedDocSerial = activeDoc == null ? 0 : activeDoc.RuntimeSerialNumber;

            ElevationInput.floorElevations = Methods.RetrieveDictionaryFromDocumentStrings();

            if (ElevationInput.floorElevations.Count > 0)
            {
                PopulateTextBoxesFromDictionary(floorElevations);
            }
        }

        private void AddLevel_Clicked(object sender, RoutedEventArgs e)
        {
            UIMethods.AddElevationInput(this.ElevationInputWrapper);
        }

        // The single most expensive failure in the field logs was this
        // dialog left empty: the whole take-off silently lands in the "-"
        // bucket. Scan proposes a floor table from the model itself -
        // solids' bottom elevations clustered per storey (each cluster's
        // TOP is where walls and columns sit, i.e. top of slab = the floor
        // elevation) - so the user edits a proposal instead of typing into
        // a blank form. Nothing is saved until Accept.
        private void ScanModel_Clicked(object sender, RoutedEventArgs e)
        {
            try
            {
                if (RunQTO.doc == null || RunQTO.doc.IsAvailable == false)
                {
                    RunQTO.doc = Rhino.RhinoDoc.ActiveDoc;
                }
                Rhino.RhinoDoc doc = RunQTO.doc;

                List<double> bottoms = new List<double>();
                foreach (Rhino.DocObjects.RhinoObject obj in doc.Objects)
                {
                    if (obj == null || obj.Attributes == null) { continue; }
                    Rhino.DocObjects.Layer layer = doc.Layers[obj.Attributes.LayerIndex];
                    string path = layer == null ? "" : (layer.FullPath ?? "");
                    // strict tree matcher, same exclusion set as the
                    // formwork fingerprint: a sibling layer like
                    // "_POURBREAK_OLD" holds real take-off solids and
                    // belongs in the floor scan
                    if (FormworkMethods.LayerInTree(path, FormworkMethods.FormworkLayer) ||
                        FormworkMethods.LayerInTree(path, FormworkMethods.PourBreakLayer))
                    {
                        continue;
                    }
                    // meshes and block instances (Blockify wraps every
                    // object) are take-off geometry too - a Blockified
                    // model must not scan to zero
                    Rhino.Geometry.GeometryBase geom = obj.Geometry;
                    if (!(geom is Rhino.Geometry.Brep) &&
                        !(geom is Rhino.Geometry.Extrusion) &&
                        !(geom is Rhino.Geometry.Mesh) &&
                        !(geom is Rhino.Geometry.InstanceReferenceGeometry))
                    {
                        continue;
                    }
                    Rhino.Geometry.BoundingBox bb = obj.Geometry.GetBoundingBox(false);
                    if (bb.IsValid)
                    {
                        bottoms.Add(bb.Min.Z);
                    }
                }
                if (bottoms.Count == 0)
                {
                    MessageBox.Show("No solid geometry found to scan.");
                    return;
                }
                bottoms.Sort();

                // GAP-based clustering: a new floor starts where the gap to
                // the previous bottom elevation exceeds 1 m. An anchored
                // band would split the foundation storey (footing
                // undersides sit metres below the slab) into phantom
                // floors; storeys are separated by far more than 1 m of
                // empty elevation, so gap merging is safe. The cluster MAX
                // (top of slab, where walls sit) is the floor elevation.
                double tol = Rhino.RhinoMath.UnitScale(
                    Rhino.UnitSystem.Meters, doc.ModelUnitSystem) * 1.0;
                List<double> proposed = new List<double>();
                double prev = bottoms[0];
                foreach (double z in bottoms)
                {
                    if (z - prev > tol)
                    {
                        proposed.Add(prev);   // close the cluster at its max
                    }
                    prev = z;
                }
                proposed.Add(prev);

                // blank every existing row, then fill the proposal
                int row = 0;
                while (true)
                {
                    TextBox name = FindTextBoxByGridRowAndColumn(ElevationInputWrapper, row, 1);
                    TextBox elev = FindTextBoxByGridRowAndColumn(ElevationInputWrapper, row, 2);
                    if (name == null && elev == null) { break; }
                    if (name != null) { name.Text = string.Empty; }
                    if (elev != null) { elev.Text = string.Empty; }
                    row++;
                }
                Dictionary<double, string> proposal = new Dictionary<double, string>();
                int floorNo = 0;
                foreach (double z in proposed)
                {
                    double key = Math.Round(z, 3);
                    if (proposal.ContainsKey(key))
                    {
                        continue;   // two clusters rounding onto one value
                    }
                    floorNo++;
                    proposal[key] = "L" + floorNo.ToString("00");
                }
                PopulateTextBoxesFromDictionary(proposal);

                MessageBox.Show(proposed.Count + " floor(s) proposed from " +
                    bottoms.Count + " solids. Edit the names and elevations, " +
                    "then Accept - nothing is saved until you do.");
            }
            catch (Exception ex)
            {
                MessageBox.Show("Scan failed: " + ex.Message);
            }
        }

        private void Accept_Clicked(object sender, RoutedEventArgs e)
        {
            // Parsed into a local first: the static table (which Calculate,
            // the exports and the staleness check all read) must only change
            // once the save into the document has actually succeeded.
            Dictionary<double, string> edited = new Dictionary<double, string>();

            foreach (Grid wrapper in this.ElevationInputWrapper.Children)
            {
                string floor = ((TextBox)wrapper.Children[1]).Text;
                string elevationText = ((TextBox)wrapper.Children[2]).Text;

                if (floor != string.Empty && elevationText != string.Empty)
                {
                    try
                    {
                        double elevation = Convert.ToDouble(elevationText);

                        // The dictionary indexer silently overwrites on a
                        // duplicate key, and the old catch message promised a
                        // "repetitive" warning that could never fire (only
                        // Convert.ToDouble throws here). First row wins;
                        // the collision is reported instead of a floor
                        // silently vanishing.
                        if (edited.ContainsKey(elevation))
                        {
                            Logger.Warn("Floor row rejected: '" + floor + "' reuses elevation " +
                                elevation + ", already taken by '" + edited[elevation] + "'.");

                            MessageBox.Show(floor + " was not added because elevation " + elevationText +
                                " is already used by " + edited[elevation] + ". Give each floor its own elevation.");

                            continue;
                        }

                        edited[elevation] = floor;
                    }
                    catch
                    {
                        // A silently missing row shifts every nearest-neighbor
                        // floor bucket, so the rejection must survive in the
                        // session log, not just this dismissable box.
                        Logger.Warn("Floor row rejected: name '" + floor + "', elevation text '" +
                            elevationText + "' is not a number.");

                        MessageBox.Show(floor + " was not added to the program because the input elevation is not a number.");
                    }
                }
            }

            try
            {
                // This modeless dialog survives File > Open, and the save goes
                // through the static RunQTO.doc while the dialog read from the
                // active document - re-fetch so both sides target the same,
                // live document instead of a disposed one.
                if (RunQTO.doc == null || RunQTO.doc.IsAvailable == false)
                {
                    RunQTO.doc = Rhino.RhinoDoc.ActiveDoc;
                }

                // But never accept into a DIFFERENT document than the one this
                // dialog was opened against - that would transplant the old
                // project's floor table into the new model.
                if (RunQTO.doc == null || RunQTO.doc.RuntimeSerialNumber != this.loadedDocSerial)
                {
                    // Without this line the field symptom is indistinguishable
                    // from "the user never set floors".
                    Logger.Warn("Floor save refused: the dialog was opened against document serial " +
                        this.loadedDocSerial + " but the current document is " +
                        (RunQTO.doc == null ? "<none>" : RunQTO.doc.RuntimeSerialNumber + " (" + (RunQTO.doc.Path ?? "<unsaved>") + ")") +
                        "; nothing was saved.");

                    MessageBox.Show("The open model file changed since this dialog was opened - " +
                        "nothing was saved. Use SET FLOOR again for the current model file.");
                    this.Close();
                    return;
                }

                Methods.SaveDictionaryToDocumentStrings(edited);
            }
            catch (Exception ex)
            {
                // Closing here would silently claim success while nothing was
                // persisted; keep the dialog open instead. The static table is
                // untouched, so calculated results and exports stay valid.
                // This is the field logs' most expensive failure domain (floor
                // problems bucket the whole take-off under "-"), so the full
                // exception goes to the session file, not just the box.
                Logger.Error("The floor table could not be saved into the model file.", ex);

                MessageBox.Show("The floor table could not be saved into the model file: " + ex.Message);
                return;
            }

            ElevationInput.floorElevations = edited;

            // Raise the custom event when the button is clicked
            ChangeSetFloorButtonColorRequest?.Invoke(this, EventArgs.Empty);

            this.Close();
        }

        private void PopulateTextBoxesFromDictionary(Dictionary<double, string> elevationInput)
        {
            int rowIndex = 0;

            foreach (var kvp in elevationInput)
            {
                TextBox textBoxRow1 = FindTextBoxByGridRowAndColumn(ElevationInputWrapper, rowIndex, 1);
                TextBox textBoxRow2 = FindTextBoxByGridRowAndColumn(ElevationInputWrapper, rowIndex, 2);

                if (textBoxRow1 != null && textBoxRow2 != null)
                {
                    textBoxRow1.Text = kvp.Value;
                    textBoxRow2.Text = kvp.Key.ToString();
                }
                else
                {
                    // Add a new row if necessary
                    AddRowToGrid(ElevationInputWrapper, kvp.Value, kvp.Key.ToString(), rowIndex);
                }

                rowIndex++;
            }
        }

        private TextBox FindTextBoxByGridRowAndColumn(Grid elevationInputWrapper, int rowIndex, int column)
        {
            foreach (UIElement grid in elevationInputWrapper.Children)
            {
                if (Grid.GetRow(grid) == rowIndex)
                {
                    Grid rowGrid = (Grid)grid;

                    foreach (UIElement uiElement in rowGrid.Children)
                    {
                        if (Grid.GetColumn(uiElement) == column && uiElement is TextBox textBox)
                        {
                            if (uiElement is TextBox)
                            {
                                return (TextBox)uiElement;
                            }
                        }
                    }
                }
            }

            return null;
        }

        private void AddRowToGrid(Grid elevationInputWrapper, string key, string value, int rowIndex)
        {
            Grid rowGrid = new Grid();
            rowGrid.Margin = new Thickness(0, 10, 0, 10);

            rowGrid.ColumnDefinitions.Add(new ColumnDefinition() { Width = new GridLength(50) });
            rowGrid.ColumnDefinitions.Add(new ColumnDefinition() { Width = new GridLength(1, GridUnitType.Star) });
            rowGrid.ColumnDefinitions.Add(new ColumnDefinition() { Width = new GridLength(1, GridUnitType.Star) });

            rowGrid.RowDefinitions.Add(new RowDefinition() { Height = new GridLength(30) });

            Label label = new Label();
            label.Content = rowIndex.ToString();
            label.FontSize = 16;
            label.HorizontalContentAlignment = HorizontalAlignment.Center;

            TextBox textBox1 = new TextBox();
            textBox1.FontSize = 20;
            textBox1.Margin = new Thickness(0, 0, 10, 0);
            textBox1.Text = key.ToString();

            TextBox textBox2 = new TextBox();
            textBox2.FontSize = 20;
            textBox2.Margin = new Thickness(10, 0, 0, 0);
            textBox2.Text = value;

            Grid.SetColumn(label, 0);
            Grid.SetRow(label, 0);

            Grid.SetColumn(textBox1, 1);
            Grid.SetRow(textBox1, 0);

            Grid.SetColumn(textBox2, 2);
            Grid.SetRow(textBox2, 0);

            rowGrid.Children.Add(label);
            rowGrid.Children.Add(textBox1);
            rowGrid.Children.Add(textBox2);

            elevationInputWrapper.RowDefinitions.Add(new RowDefinition() { Height = new GridLength(50) });
            Grid.SetRow(rowGrid, rowIndex);
            elevationInputWrapper.Children.Add(rowGrid);
        }
    }
}
