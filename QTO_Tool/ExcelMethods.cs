using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;

namespace QTO_Tool
{
    /// <summary>
    /// UI side of the Excel export: picks the destination, scrapes the WPF result
    /// tables into a plain model, and hands it to <see cref="ExcelWorkbookWriter"/>.
    /// </summary>
    class ExcelMethods
    {
        public static void ExportExcel(StackPanel layerBasedConcreteTable, List<string> layerPropertyColumnHeaders)
        {
            System.Windows.Forms.SaveFileDialog saveFileDialog = new System.Windows.Forms.SaveFileDialog();
            saveFileDialog.Filter = "Excel |*.xlsx";

            if (saveFileDialog.ShowDialog() != System.Windows.Forms.DialogResult.OK)
            {
                MessageBox.Show("Export was canceled.");
                return;
            }

            string outputPath = saveFileDialog.FileName;

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

            try
            {
                ExcelWorkbookWriter.ExportModel model =
                    ScrapeConcreteTable(layerBasedConcreteTable, layerPropertyColumnHeaders);

                ExcelWorkbookWriter.Write(model, Resources.template, outputPath);

                Dispatcher.FromThread(newWindowThread).InvokeShutdown();

                if (model.ScrapeError == null)
                {
                    MessageBox.Show("Export was successful.");
                }
                else
                {
                    // This path used to save a partial workbook and still report success.
                    MessageBox.Show("Export finished with missing data.\n\n" + model.ScrapeError
                        + "\n\nThe workbook was written with the rows that could be read. "
                        + "Please repair the model and export again.");
                }
            }
            catch (Exception)
            {
                Dispatcher.FromThread(newWindowThread).InvokeShutdown();

                // The caller logs the failure and reports the log file path.
                throw;
            }
        }

        /// <summary>
        /// Reads the WPF result tables into a plain data model. One row per layer
        /// expander; each grid column contributes either a summed number or a text value.
        /// </summary>
        internal static ExcelWorkbookWriter.ExportModel ScrapeConcreteTable(
            StackPanel concreteTable, List<string> layerPropertyColumnHeaders)
        {
            var model = new ExcelWorkbookWriter.ExportModel();

            model.ProjectHeaders = new List<string>(ExcelWorkbookWriter.ProjectSheetHeaders);
            model.ProjectHeaders.InsertRange(2, layerPropertyColumnHeaders);

            int nameAbbreviationIndex =
                ExcelWorkbookWriter.SummarySheetHeaders.IndexOf(ExcelWorkbookWriter.NameAbbreviationHeader);

            foreach (UIElement container in concreteTable.Children)
            {
                Expander expander = (Expander)container;
                Grid contentGrid = (Grid)expander.Content;

                object[] row = new object[model.ProjectHeaders.Count];

                for (int i = 0; i < row.Length; i++)
                {
                    row[i] = "-";
                }

                for (int i = 0; i < contentGrid.ColumnDefinitions.Count - 1; i++)
                {
                    double numberValue = 0;
                    string textValue = string.Empty;
                    bool isTextColumn = false;

                    int projectColumnIndex = 0;
                    int summaryColumnIndex = 0;

                    for (int j = 0; j < contentGrid.RowDefinitions.Count; j++)
                    {
                        UIElement element = contentGrid.Children.Cast<UIElement>().
                            FirstOrDefault(e => Grid.GetColumn(e) == i && Grid.GetRow(e) == j);

                        if (element == null)
                        {
                            TextBlock errorElement = contentGrid.Children.Cast<TextBlock>().
                                FirstOrDefault(e => Grid.GetColumn(e) == i && Grid.GetRow(e) == 0);

                            string err = errorElement == null ? "(unknown)" : errorElement.Text;
                            string layerName = ((TextBlock)expander.Header).Text;

                            model.ScrapeError = string.Format(
                                "Could not read the {0} value of layer {1}.", err, layerName);

                            return model;
                        }

                        string value = ((TextBlock)element).Text;

                        if (j == 0)
                        {
                            projectColumnIndex = model.ProjectHeaders.IndexOf(value);
                            summaryColumnIndex = ExcelWorkbookWriter.SummarySheetHeaders.IndexOf(value);

                            // FLOOR, NAME ABB. and the layer-name segment columns
                            // carry text; a numeric-looking value there (a floor
                            // named "2") must not be summed like a quantity.
                            isTextColumn = value == ExcelWorkbookWriter.NameAbbreviationHeader
                                || value == ExcelWorkbookWriter.FloorHeader
                                || layerPropertyColumnHeaders.Contains(value);

                            continue;
                        }

                        // The first grid column is the object count, not a quantity.
                        if (i == 0)
                        {
                            numberValue += 1;
                            continue;
                        }

                        if (isTextColumn)
                        {
                            textValue = value == "N/A" ? "-" : value;

                            if (summaryColumnIndex == nameAbbreviationIndex
                                && !model.UniqueNameAbbreviations.Contains(textValue))
                            {
                                model.UniqueNameAbbreviations.Add(textValue);
                            }

                            continue;
                        }

                        try
                        {
                            numberValue += Convert.ToDouble(value);
                        }
                        catch
                        {
                            textValue = value == "N/A" ? "-" : value;
                        }
                    }

                    if (projectColumnIndex < 0 || projectColumnIndex >= row.Length)
                    {
                        continue;
                    }

                    if (textValue != string.Empty)
                    {
                        row[projectColumnIndex] = textValue;
                    }
                    else if (numberValue > 0)
                    {
                        row[projectColumnIndex] = numberValue;
                    }
                }

                model.ProjectRows.Add(row);
            }

            return model;
        }
    }
}
