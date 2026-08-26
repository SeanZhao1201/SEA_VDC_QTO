using Rhino;
using Rhino.Commands;

namespace QTO_Tool
{
    /// <summary>
    /// Opens the formwork sidecar window. A separate command and window by
    /// design (2026-08-12 decision) - NOT a tab in QTOUI: every generation
    /// step runs in a second, headless Rhino on a WriteFile copy of the
    /// model, so the destructive fail-open defaults in the Python engines
    /// can never touch the client document, and nothing here fires the
    /// doc events that permanently disable REVERT CHECKUP.
    /// </summary>
    public class RunFormwork : Command
    {
        FormworkUI UI;

        public RunFormwork()
        {
            Instance = this;
        }

        public static RunFormwork Instance
        {
            get; private set;
        }

        public override string EnglishName
        {
            get { return "RunFormwork"; }
        }

        protected override Result RunCommand(RhinoDoc doc, RunMode mode)
        {
            // Same refusal as RunQTO: FormworkUI reads JSON stamps at load, so
            // a partial install would crash the window open instead of naming
            // the missing files.
            if (!DependencyPreflight.Check())
            {
                return Result.Failure;
            }

            if (RunQTO.doc == null || RunQTO.doc.IsAvailable == false)
            {
                RunQTO.doc = RhinoDoc.ActiveDoc;
            }

            try
            {
                this.UI.Close();
            }
            catch { }

            // FormworkUI_Closing CANCELS the close while a child run is busy
            // and the user chooses to keep it - Close() returns void, so the
            // cancellation is invisible here. A second window would share the
            // fixed-name staging files with the first and, with its own fresh
            // runBusy state, could clobber the running child's inputs before
            // the plugin-wide gate refuses the launch. Front the surviving
            // window instead of opening a twin.
            if (this.UI != null && this.UI.IsLoaded)
            {
                this.UI.Activate();

                return Result.Success;
            }

            this.UI = new FormworkUI();

            Methods.SetChildStatus(this.UI, ChildStatus.ChildOfRhino);

            UI.Show();

            return Result.Success;
        }
    }
}
