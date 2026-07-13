using System;
using System.IO;

namespace QTO_Tool
{
    /// <summary>
    /// Session log, one file per RunQTO session. Written to a Logs subfolder next to
    /// the plugin (QTO_Tool ships as a folder, so the logs travel with it and can be
    /// sent along with bug reports); falls back to %AppData%\QTO_Tool\Logs when the
    /// plugin folder is not writable.
    /// </summary>
    public static class Logger
    {
        private static readonly object writeLock = new object();

        public static string LogFilePath { get; private set; }

        /// <summary>Starts a new session log file and announces its path on the Rhino command line.</summary>
        public static void StartSession()
        {
            LogFilePath = null;

            string fileName = "QTO_" + DateTime.Now.ToString("yyyy-MM-dd_HH-mm-ss") + ".log";

            foreach (string logDirectory in CandidateLogDirectories())
            {
                try
                {
                    Directory.CreateDirectory(logDirectory);

                    string candidate = Path.Combine(logDirectory, fileName);

                    // Probe writability now so a read-only install location falls
                    // through to %AppData% instead of losing the whole session log.
                    File.AppendAllText(candidate, string.Empty);

                    LogFilePath = candidate;
                    break;
                }
                catch
                {
                    // Try the next candidate; logging must never break the plugin.
                }
            }

            if (LogFilePath == null)
            {
                return;
            }

            try
            {
                Info("QTO_Tool session started. Plugin version: " +
                    typeof(Logger).Assembly.GetName().Version.ToString() +
                    ". Rhino version: " + Rhino.RhinoApp.Version.ToString() + ".");

                Rhino.RhinoApp.WriteLine("QTO_Tool log file: " + LogFilePath);
            }
            catch
            {
                // Logging must never break the plugin.
            }
        }

        private static string[] CandidateLogDirectories()
        {
            string pluginLogDirectory = null;

            try
            {
                string assemblyDirectory = Path.GetDirectoryName(typeof(Logger).Assembly.Location);

                if (!string.IsNullOrEmpty(assemblyDirectory))
                {
                    pluginLogDirectory = Path.Combine(assemblyDirectory, "Logs");
                }
            }
            catch
            {
                // Fall through to %AppData%.
            }

            string appDataLogDirectory = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "QTO_Tool", "Logs");

            if (pluginLogDirectory == null)
            {
                return new[] { appDataLogDirectory };
            }

            return new[] { pluginLogDirectory, appDataLogDirectory };
        }

        public static void Info(string message)
        {
            WriteLine("INFO ", message);
        }

        public static void Warn(string message)
        {
            WriteLine("WARN ", message);
        }

        public static void Error(string message, Exception ex = null)
        {
            if (ex != null)
            {
                message += Environment.NewLine + ex.ToString();
            }

            WriteLine("ERROR", message);
        }

        private static void WriteLine(string level, string message)
        {
            if (LogFilePath == null)
            {
                return;
            }

            try
            {
                lock (writeLock)
                {
                    File.AppendAllText(LogFilePath,
                        DateTime.Now.ToString("HH:mm:ss.fff") + " [" + level + "] " + message + Environment.NewLine);
                }
            }
            catch
            {
                // Logging must never break the plugin.
            }
        }
    }
}
