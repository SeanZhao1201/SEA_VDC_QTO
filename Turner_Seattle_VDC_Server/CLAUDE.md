# CLAUDE.md — Turner_Seattle_VDC_Server

Standalone WPF desktop utility (SDK-style csproj, net472, `dotnet build`/VS on Windows) that pushes QTO Excel output into a MySQL database and reads it back. It shares **no code and no project references** with the `QTO_Tool` Rhino plugin — only the Excel file format connects them. Do not couple the two projects.

Notes:

- The MySQL connection string (server IP + credentials) is hardcoded in `MainWindow.xaml.cs`.
- Uses Excel COM interop, so desktop Excel is required at runtime.
- The plugin's own MySQL code paths (`QTO_Tool/MySqlMethods.cs`) are commented out; this app is the only live MySQL consumer in the solution.
