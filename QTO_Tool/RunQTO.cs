using System;
using System.Collections.Generic;
using Rhino;
using Rhino.Commands;
using Rhino.Geometry;
using Rhino.Input;
using Rhino.Input.Custom;

namespace QTO_Tool
{

    enum ChildStatus { ChildOfGH, ChildOfRhino, AlwaysOnTop };

    enum ConcreteTemplates { FOOTING, CONTINOUS_FOOTING, COLUMN, SLAB, BEAM, WALL, CURB, STYROFOAM };

    public class RunQTO : Command
    {
        // SubWindows: Generate Initial Data Window
        QTOUI UI;

        public static RhinoDoc doc;
        public static double volumeConversionFactor = 1;

        // Areas and lengths follow the same reporting convention the volume
        // does (the ft-based units the takeoff reports in): inch models
        // convert in^2 -> ft^2 and in -> ft, feet models are already there,
        // any other unit system passes through unconverted. Before these
        // factors existed, inch models exported square-inch areas and inch
        // lengths next to cubic-yard volumes with no warning.
        public static double areaConversionFactor = 1;
        public static double lengthConversionFactor = 1;

        public RunQTO()
        {
            // Rhino only creates one instance of each command class defined in a
            // plug-in, so it is safe to store a refence in a static property.
            Instance = this;
        }

        ///<summary>The only instance of this command.</summary>
        public static RunQTO Instance
        {
            get; private set;
        }

        ///<returns>The command name as it appears on the Rhino command line.</returns>
        public override string EnglishName
        {
            get { return "RunQTO"; }
        }

        protected override Result RunCommand(RhinoDoc doc, RunMode mode)
        {
            // TODO: start here modifying the behaviour of your command.
            // ---

            ChildStatus winChildStatus = ChildStatus.ChildOfRhino;

            // Always get the Actice model
            RunQTO.doc = RhinoDoc.ActiveDoc;

            Logger.StartSession();
            Logger.Info("Document: " + RunQTO.doc.Path + " | Units: " + RunQTO.doc.GetUnitSystemName(true, true, true, true));

            // A partial install (the .rhp without its DLLs) must refuse here,
            // with the missing files named - not at the first export that
            // happens to touch the missing assembly. Re-checked per command
            // run, so re-extracting the zip recovers without restarting Rhino.
            if (!DependencyPreflight.Check())
            {
                return Result.Failure;
            }

            string modelUnit = RunQTO.doc.GetUnitSystemName(true, true, true, true);
            RunQTO.volumeConversionFactor = Methods.SetVolumeConversionFactor(modelUnit);
            RunQTO.areaConversionFactor = Methods.SetAreaConversionFactor(modelUnit);
            RunQTO.lengthConversionFactor = Methods.SetLengthConversionFactor(modelUnit);

            //try closing a window if it's already up
            try
            {
                this.UI.Close();
            }
            catch { }

            this.UI = new QTOUI();

            Methods.SetChildStatus(this.UI, winChildStatus);

            UI.Show();

            // ---

            return Result.Success;
        }
    }
}
