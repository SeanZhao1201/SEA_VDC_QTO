using ClosedXML.Excel;
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;

namespace QTO_Tool
{
    /// <summary>
    /// Fills the embedded take-off template and saves it, using ClosedXML.
    /// Desktop Excel is not required and is never launched.
    ///
    /// Deliberately free of WPF and RhinoCommon types: everything it needs arrives
    /// as parameters, so the workbook output can be exercised without a UI or a
    /// Rhino host. <see cref="ExcelMethods"/> owns the UI side and calls in here.
    /// </summary>
    internal static class ExcelWorkbookWriter
    {
        internal const string ProjectTableName = "PROJECT_TABLE";
        internal const string SummaryTableName = "SUMMARY_TABLE";
        internal const string NumberFormat = "#,#.00";
        internal const string NameAbbreviationHeader = "NAME ABB.";
        internal const string FloorHeader = "FLOOR";

        internal static readonly List<string> SummarySheetHeaders = new List<string>
        {
            "COUNT", NameAbbreviationHeader, "GROSS VOLUME", "NET VOLUME", "BOTTOM AREA", "OPENING AREA",
            "TOP AREA", "SIDE AREA", "END AREA", "SIDE-1", "SIDE-2", "EDGE AREA", "TREAD AREA",
            "RISER AREA", "TREAD COUNT", "LENGTH", "HEIGHT", "PERIMETER", "OPENING PERIMETER"
        };

        internal static readonly List<string> ProjectSheetHeaders = new List<string>
        {
            "COUNT", NameAbbreviationHeader, FloorHeader, "GROSS VOLUME", "NET VOLUME", "BOTTOM AREA", "OPENING AREA",
            "TOP AREA", "SIDE AREA", "END AREA", "SIDE-1", "SIDE-2", "EDGE AREA", "TREAD AREA",
            "RISER AREA", "TREAD COUNT", "LENGTH", "HEIGHT", "PERIMETER", "OPENING PERIMETER"
        };

        /// <summary>
        /// One take-off workbook's worth of data, scraped off the WPF result tables.
        /// </summary>
        internal class ExportModel
        {
            public List<string> ProjectHeaders = new List<string>();

            /// <summary>One entry per layer row; each array is aligned to
            /// <see cref="ProjectHeaders"/>. Entries are either a double or a string.</summary>
            public List<object[]> ProjectRows = new List<object[]>();

            public List<string> UniqueNameAbbreviations = new List<string>();

            /// <summary>Set when a layer could not be read. The workbook is still written
            /// with whatever was collected, and the caller reports a partial export.</summary>
            public string ScrapeError;
        }

        /// <param name="templateBytes">The embedded QTO template workbook (Resources.template).</param>
        internal static void Write(ExportModel model, byte[] templateBytes, string savePath)
        {
            using (MemoryStream templateStream = new MemoryStream(templateBytes))
            using (XLWorkbook workBook = new XLWorkbook(templateStream))
            {
                IXLWorksheet summarySheet = workBook.Worksheet(1);
                IXLWorksheet projectSheet = workBook.Worksheet(2);

                FillProjectSheet(projectSheet, model);
                FillSummarySheet(summarySheet, model);

                foreach (IXLWorksheet sheet in new[] { projectSheet, summarySheet })
                {
                    IXLRange used = sheet.RangeUsed();

                    if (used != null)
                    {
                        used.Style.Alignment.Horizontal = XLAlignmentHorizontalValues.Left;
                    }

                    sheet.Columns().AdjustToContents();
                }

                // Formulas are written without cached values; Excel recalculates on open.
                workBook.CalculateMode = XLCalculateMode.Auto;

                workBook.SaveAs(savePath);
            }
        }

        private static void FillProjectSheet(IXLWorksheet projectSheet, ExportModel model)
        {
            int rowCount = model.ProjectRows.Count;
            int totalRow = 3 + rowCount;

            // Headers must be in place before the table is resized: unlike live Excel,
            // ClosedXML takes the table's column names from the header cells at resize
            // time, and the summary sheet's structured references depend on them.
            for (int column = 1; column <= model.ProjectHeaders.Count; column++)
            {
                IXLCell headerCell = projectSheet.Cell(1, column);
                headerCell.Value = model.ProjectHeaders[column - 1];
                headerCell.Style.Fill.BackgroundColor = XLColor.FromColor(Color.YellowGreen);
            }

            for (int row = 0; row < rowCount; row++)
            {
                object[] values = model.ProjectRows[row];

                for (int column = 1; column <= model.ProjectHeaders.Count; column++)
                {
                    SetCellValue(projectSheet.Cell(2 + row, column),
                        column <= values.Length ? values[column - 1] : "-");
                }
            }

            for (int column = 1; column <= model.ProjectHeaders.Count; column++)
            {
                IXLCell cell = projectSheet.Cell(totalRow, column);

                // Matches the previous COM output, which summed one row past the last
                // data row; that row is blank and contributes nothing.
                cell.FormulaA1 = string.Format("=SUM({0}2:{0}{1})",
                    GetExcelColumnName(column), 2 + rowCount);
                ApplyTotalStyle(cell);
            }

            ResizeTable(projectSheet, ProjectTableName,
                projectSheet.Range(1, 1, Math.Max(2, rowCount + 1), model.ProjectHeaders.Count));
        }

        private static void FillSummarySheet(IXLWorksheet summarySheet, ExportModel model)
        {
            int nameColumn = SummarySheetHeaders.IndexOf(NameAbbreviationHeader) + 1;
            int lastDataRow = model.UniqueNameAbbreviations.Count + 1;
            int totalRow = lastDataRow + 2;

            // Absolute A1 ranges over the PROJECT sheet's data rows, not the structured
            // references (PROJECT_TABLE[COUNT]) the COM version used. ClosedXML's formula
            // parser rejects intra-table references while converting A1 to R1C1 on save
            // (XLTableField.IsConsistentFormula), and these cells sit inside SUMMARY_TABLE.
            // Plain ranges also drop the dependency on the resized table's column names.
            int projectLastRow = model.ProjectRows.Count + 1;
            string nameRange = ProjectColumnRange(model, NameAbbreviationHeader, projectLastRow);

            for (int i = 0; i < model.UniqueNameAbbreviations.Count; i++)
            {
                summarySheet.Cell(2 + i, nameColumn).Value = model.UniqueNameAbbreviations[i];
            }

            for (int column = 1; column <= SummarySheetHeaders.Count; column++)
            {
                string columnLetter = GetExcelColumnName(column);

                if (column != nameColumn)
                {
                    string valueRange = ProjectColumnRange(model, SummarySheetHeaders[column - 1], projectLastRow);

                    // The COM version wrote this formula only into row 2 and relied on
                    // Excel's calculated-column auto-fill to propagate it down the table.
                    // ClosedXML does not auto-fill, so write every data row explicitly.
                    if (nameRange != null && valueRange != null)
                    {
                        for (int row = 2; row <= lastDataRow; row++)
                        {
                            summarySheet.Cell(row, column).FormulaA1 = string.Format(
                                "=SUMIF({0},$B{1},{2})", nameRange, row, valueRange);
                        }
                    }

                    summarySheet.Cell(totalRow, column).FormulaA1 = string.Format("=SUM({0}2:{0}{1})",
                        columnLetter, lastDataRow);
                }
                else
                {
                    summarySheet.Cell(totalRow, column).Value = "-";
                }

                ApplyTotalStyle(summarySheet.Cell(totalRow, column));
            }

            ResizeTable(summarySheet, SummaryTableName,
                summarySheet.Range(1, 1, Math.Max(2, lastDataRow), SummarySheetHeaders.Count));
        }

        /// <summary>
        /// Absolute reference to one PROJECT-sheet column's data rows, located by header
        /// name because the layer-property columns shift every later column right.
        /// Returns null when the header is absent or there are no data rows.
        /// </summary>
        private static string ProjectColumnRange(ExportModel model, string header, int projectLastRow)
        {
            int index = model.ProjectHeaders.IndexOf(header);

            if (index < 0 || projectLastRow < 2)
            {
                return null;
            }

            string letter = GetExcelColumnName(index + 1);

            return string.Format("PROJECT!${0}$2:${0}${1}", letter, projectLastRow);
        }

        private static void ApplyTotalStyle(IXLCell cell)
        {
            cell.Style.Fill.BackgroundColor = XLColor.FromColor(Color.CornflowerBlue);
            cell.Style.Font.Bold = true;
            cell.Style.NumberFormat.Format = NumberFormat;
        }

        private static void SetCellValue(IXLCell cell, object value)
        {
            if (value is double)
            {
                cell.Value = (double)value;
                cell.Style.NumberFormat.Format = NumberFormat;
            }
            else
            {
                cell.Value = Convert.ToString(value);
            }
        }

        /// <summary>
        /// Grows the template's table to cover the written range. The template ships
        /// PROJECT_TABLE as a single column, so this is what gives the summary sheet's
        /// structured references their column names.
        /// </summary>
        private static void ResizeTable(IXLWorksheet sheet, string tableName, IXLRange range)
        {
            IXLTable table = sheet.Tables.FirstOrDefault(t => t.Name == tableName);

            if (table == null)
            {
                range.CreateTable(tableName);
                return;
            }

            table.Resize(range);
        }

        internal static string GetExcelColumnName(int columnIndex)
        {
            const int alphabetSize = 26;
            string columnName = String.Empty;

            // Loop to handle multi-letter columns (e.g., AA, AB)
            while (columnIndex > 0)
            {
                columnIndex--; // Adjust for 0-based index
                columnName = (char)('A' + columnIndex % alphabetSize) + columnName;
                columnIndex /= alphabetSize;
            }
            return columnName;
        }
    }
}
