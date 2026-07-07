using System;
using System.IO;

namespace QTO_Tool
{
    /// <summary>
    /// Session log written to %AppData%\QTO_Tool\Logs. One file per RunQTO session.
    /// </summary>
    public static class Logger
    {
        private static readonly object writeLock = new object();

        public static string LogFilePath { get; private set; }

        /// <summary>Starts a new session log file and announces its path on the Rhino command line.</summary>
        public static void StartSession()
        {
            try
            {
                string logDirectory = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "QTO_Tool", "Logs");

                Directory.CreateDirectory(logDirectory);

                LogFilePath = Path.Combine(logDirectory,
                    "QTO_" + DateTime.Now.ToString("yyyy-MM-dd_HH-mm-ss") + ".log");

                Info("QTO_Tool session started. Plugin version: " +
                    typeof(Logger).Assembly.GetName().Version.ToString());

                Rhino.RhinoApp.WriteLine("QTO_Tool log file: " + LogFilePath);
            }
            catch
            {
                // Logging must never break the plugin.
                LogFilePath = null;
            }
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
