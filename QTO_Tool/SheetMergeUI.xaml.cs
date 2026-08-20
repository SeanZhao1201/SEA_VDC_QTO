using System.Collections.Generic;
using System.Windows;
using System.Windows.Controls;

namespace QTO_Tool
{
    /// <summary>
    /// The near-TYP merge picker (break sheet P2). MAKE reports floor
    /// groups whose footprints differ by only a few vertices; nothing is
    /// ever auto-merged - this dialog is where the modeler decides. APPLY
    /// hands the checked sets back to the caller, which writes the staged
    /// merge-directive file and regenerates the sheet; the generator
    /// re-validates every pair against the live fingerprints before
    /// honoring the file, so a stale directive cannot glue diverged floors.
    /// </summary>
    public partial class SheetMergeUI : Window
    {
        public class MergeChoice
        {
            public List<string> Floors;
            public string Label;
            public bool Applied;
        }

        readonly List<MergeChoice> choices;
        readonly List<CheckBox> boxes = new List<CheckBox>();

        /// <summary>True when APPLY changed at least one checkbox against
        /// the state the dialog opened with - an unchanged APPLY must not
        /// trigger a pointless regeneration.</summary>
        public bool Changed { get; private set; }
        public List<List<string>> CheckedSets { get; private set; }

        public SheetMergeUI(List<MergeChoice> items)
        {
            InitializeComponent();
            this.choices = items;
            foreach (MergeChoice choice in items)
            {
                CheckBox box = new CheckBox
                {
                    Content = choice.Label,
                    IsChecked = choice.Applied,
                    FontSize = 15,
                    Margin = new Thickness(5, 6, 5, 6),
                };
                this.boxes.Add(box);
                this.MergeListWrapper.Children.Add(box);
            }
        }

        private void Apply_Clicked(object sender, RoutedEventArgs e)
        {
            this.CheckedSets = new List<List<string>>();
            this.Changed = false;
            for (int i = 0; i < this.boxes.Count; i++)
            {
                bool isChecked = this.boxes[i].IsChecked == true;
                if (isChecked)
                {
                    this.CheckedSets.Add(this.choices[i].Floors);
                }
                if (isChecked != this.choices[i].Applied)
                {
                    this.Changed = true;
                }
            }
            this.DialogResult = true;
        }

        private void Cancel_Clicked(object sender, RoutedEventArgs e)
        {
            this.DialogResult = false;
        }
    }
}
