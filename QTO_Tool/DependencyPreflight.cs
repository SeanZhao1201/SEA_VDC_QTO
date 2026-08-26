using System;
using System.Collections.Generic;
using System.IO;

namespace QTO_Tool
{
    /// <summary>
    /// Verifies that the dependency DLLs shipped in QTO_Tool.zip sit next to the
    /// plugin assembly before a window opens. A partial install (the .rhp copied
    /// on its own into an older folder, or an extract a sync/antivirus tool ate a
    /// file from) is otherwise silent until the first feature that touches the
    /// missing assembly throws a raw FileNotFoundException - checkup and Calculate
    /// run fine and the failure surfaces as a cryptic dialog at Excel export, or
    /// escapes uncaught at IFC export. This class must stay loadable when every
    /// dependency is missing: framework types only, nothing from the packages.
    /// </summary>
    internal static class DependencyPreflight
    {
        // Every DLL the release zip ships next to the .rhp; keep in step with
        // the PackageReferences in QTO_Tool.csproj (the dialog only ever names
        // the files actually missing, so listing all of them adds no noise -
        // and losing a single shim to an antivirus false positive would
        // otherwise slip through and still crash at export). This detects
        // ABSENCE only, not staleness: a stale-but-complete folder (every name
        // present at an old version) passes green, so "preflight passed" does
        // not clear an install of version skew.
        private static readonly string[] RequiredDlls = new string[]
        {
            // Excel export (ClosedXML and its satellites)
            "ClosedXML.dll",
            "ClosedXML.Parser.dll",
            "DocumentFormat.OpenXml.dll",
            "DocumentFormat.OpenXml.Framework.dll",
            "ExcelNumberFormat.dll",
            "RBush.dll",
            "SixLabors.Fonts.dll",
            // Floor table persistence and formwork stamps (all JSON round trips)
            "Newtonsoft.Json.dll",
            // IFC export (xBIM)
            "Esent.Interop.dll",
            "Xbim.Common.dll",
            "Xbim.Ifc.dll",
            "Xbim.Ifc2x3.dll",
            "Xbim.Ifc4.dll",
            "Xbim.Ifc4x3.dll",
            "Xbim.IO.Esent.dll",
            "Xbim.IO.MemoryModel.dll",
            // Transitive shims (Rhino supplies none of these)
            "Microsoft.Bcl.AsyncInterfaces.dll",
            "Microsoft.Bcl.HashCode.dll",
            "Microsoft.Extensions.DependencyInjection.dll",
            "Microsoft.Extensions.DependencyInjection.Abstractions.dll",
            "Microsoft.Extensions.Logging.Abstractions.dll",
            "Microsoft.Extensions.Options.dll",
            "Microsoft.Extensions.Primitives.dll",
            "System.Buffers.dll",
            "System.Memory.dll",
            "System.Numerics.Vectors.dll",
            "System.Runtime.CompilerServices.Unsafe.dll",
            "System.Threading.Tasks.Extensions.dll",
        };

        /// <summary>
        /// True when every required DLL is present (or the folder cannot be
        /// determined - the features then report their own load failures).
        /// On a partial install: logs, shows one plain-language dialog naming
        /// the missing files, and returns false so the command can refuse to
        /// open its window. Never throws.
        /// </summary>
        internal static bool Check()
        {
            try
            {
                // Checked BEFORE GetDirectoryName: net48 throws on an empty
                // path (the catch would still return true, but this keeps the
                // undeterminable-folder guard real on both runtimes).
                string assemblyLocation = typeof(DependencyPreflight).Assembly.Location;

                if (string.IsNullOrEmpty(assemblyLocation))
                {
                    return true;
                }

                string pluginDirectory = Path.GetDirectoryName(assemblyLocation);

                if (string.IsNullOrEmpty(pluginDirectory))
                {
                    return true;
                }

                List<string> missing = new List<string>();

                foreach (string dll in RequiredDlls)
                {
                    if (!File.Exists(Path.Combine(pluginDirectory, dll)))
                    {
                        missing.Add(dll);
                    }
                }

                if (missing.Count == 0)
                {
                    return true;
                }

                Logger.Error("Dependency preflight: " + missing.Count + " DLL(s) missing from " +
                    pluginDirectory + ": " + string.Join(", ", missing));

                System.Windows.MessageBox.Show(
                    "The QTO_Tool installation is incomplete. These files are missing from" +
                    Environment.NewLine + pluginDirectory + ":" +
                    Environment.NewLine + Environment.NewLine +
                    string.Join(Environment.NewLine, missing) +
                    Environment.NewLine + Environment.NewLine +
                    "Please re-extract the ENTIRE QTO_Tool.zip from the GitHub release into this folder. " +
                    "QTO_Tool.rhp and its DLLs must always stay together - never copy the .rhp on its own.",
                    "QTO_Tool - installation incomplete");

                return false;
            }
            catch
            {
                // The preflight must never take the plugin down; a broken install
                // still fails loudly at the feature that needs the missing DLL.
                return true;
            }
        }
    }
}
