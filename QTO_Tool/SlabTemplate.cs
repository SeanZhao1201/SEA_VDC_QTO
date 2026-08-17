using Rhino.DocObjects;
using Rhino.Geometry;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using System.Windows;

namespace QTO_Tool
{
    class SlabTemplate
    {
        public Brep geometry { get; set; }
        public System.Drawing.Color color { get; set; }
        public string layerName { get; set; }
        public string nameAbb { get; set; }
        public string id { get; set; }

        public Dictionary<string, string> AttributeUserStrings { get; private set; }

        public Dictionary<string, string> parsedLayerName = new Dictionary<string, string>();
        public string floor { get; set; }
        public double grossVolume { get; set; }
        public double netVolume { get; set; }
        public double topArea { get; set; }
        public double bottomArea { get; set; }
        public double edgeArea { get; set; }
        public double perimeter { get; set; }
        public double openingPerimeter { get; set; }

        public string type = "SlabTemplate";

        private List<Brep> topBrepFaces = new List<Brep>();

        private List<double> downfacingFaceElevations = new List<double>();

        public Dictionary<string, BeamTemplate> beams = new Dictionary<string, BeamTemplate>();
        //public List<BeamTemplate> beams = new List<BeamTemplate>();

        public static string[] units = { "N/A", "N/A", "Cubic Yard", "Cubic Yard", "Square Foot", "Square Foot", "Square Foot", "Foot", "Foot", "N/A" };

        public SlabTemplate(RhinoObject rhobj, string _layerName, System.Drawing.Color layerColor, double angleThreshold, Dictionary<double, string> floorElevations)
        {
            this.color = layerColor;

            this.geometry = (Brep)rhobj.Geometry;

            this.layerName = _layerName;

            this.id = rhobj.Id.ToString();

            AttributeUserStrings = Methods.CopyRhinoAttributeUserStrings(rhobj);

            for (int i = 0; i < _layerName.Split('_').ToList().Count; i++)
            {
                parsedLayerName.Add("C" + (1 + i).ToString(), _layerName.Split('_').ToList()[i]);
            }

            this.nameAbb = parsedLayerName["C1"] + " " + parsedLayerName["C2"];

            var mass_properties = VolumeMassProperties.Compute(this.geometry);
            this.netVolume = Math.Round(mass_properties.Volume * RunQTO.volumeConversionFactor, 2);

            this.grossVolume = Math.Round(Methods.CalculateGrossVolume(this.geometry) * RunQTO.volumeConversionFactor, 2);

            this.topArea = TopArea(geometry, angleThreshold);

            this.bottomArea = BottomArea(geometry, angleThreshold);

            if (floorElevations.Count > 0)
            {
                this.floor = Methods.FindFloor(floorElevations, this.downfacingFaceElevations.Min());
            }
            else
            {
                this.floor = "-";
            }

            this.edgeArea = EdgeArea(geometry);

            this.PerimeterAndOpeningPerimeter(this.topBrepFaces);
        }

        double TopArea(Brep brep, double angleThreshold)
        {
            double area = 0;

            for (int i = 0; i < brep.Faces.Count; i++)
            {
                var area_properties = AreaMassProperties.Compute(brep.Faces[i]);

                Point3d center = area_properties.Centroid;

                double u, v;

                if (brep.Faces[i].ClosestPoint(center, out u, out v))
                {
                    Vector3d normal = brep.Faces[i].NormalAt(u, v);

                    normal.Unitize();

                    double dotProduct = Vector3d.Multiply(normal, Vector3d.ZAxis);

                    if (dotProduct > angleThreshold && dotProduct <= 1)
                    {
                        area += Math.Round(area_properties.Area, 2);

                        this.topBrepFaces.Add(brep.Faces[i].DuplicateFace(false));
                    }
                }
            }

            if (area == 0 && brep.Faces.Count > 0)
            {
                List<double> centerZValues = new List<double>();
                List<double> faceAreas = new List<double>();

                for (int i = 0; i < brep.Faces.Count; i++)
                {
                    var area_properties = AreaMassProperties.Compute(brep.Faces[i]);

                    Point3d center = area_properties.Centroid;

                    centerZValues.Add(center.Z);

                    faceAreas.Add(Math.Round(area_properties.Area, 2));
                }

                int topFaceIndex = centerZValues.IndexOf(centerZValues.Max());

                area = faceAreas[topFaceIndex];

                this.topBrepFaces.Add(brep.Faces[topFaceIndex].DuplicateFace(false));
            }

            return Math.Round(area * RunQTO.areaConversionFactor, 2);
        }

        double BottomArea(Brep brep, double angleThreshold)
        {
            double area = 0;

            for (int i = 0; i < brep.Faces.Count; i++)
            {
                var area_properties = AreaMassProperties.Compute(brep.Faces[i]);

                Point3d center = area_properties.Centroid;

                double u, v;

                if (brep.Faces[i].ClosestPoint(center, out u, out v))
                {
                    Vector3d normal = brep.Faces[i].NormalAt(u, v);

                    normal.Unitize();

                    double dotProduct = Vector3d.Multiply(normal, Vector3d.ZAxis);

                    if (dotProduct < -angleThreshold && dotProduct >= -1)
                    {
                        area += Math.Round(area_properties.Area, 2);

                        this.downfacingFaceElevations.Add(center.Z);
                    }
                }
            }

            if (area == 0 && brep.Faces.Count > 0)
            {
                List<double> centerZValues = new List<double>();
                List<double> faceAreas = new List<double>();

                for (int i = 0; i < brep.Faces.Count; i++)
                {
                    var area_properties = AreaMassProperties.Compute(brep.Faces[i]);

                    Point3d center = area_properties.Centroid;

                    centerZValues.Add(center.Z);
                    faceAreas.Add(Math.Round(area_properties.Area, 2));
                }

                int bottomFaceIndex = centerZValues.IndexOf(centerZValues.Min());

                this.downfacingFaceElevations.Add(centerZValues.Min());

                area = faceAreas[bottomFaceIndex];
            }

            return Math.Round(area * RunQTO.areaConversionFactor, 2);
        }

        double EdgeArea(Brep brep)
        {
            double area = 0;

            for (int i = 0; i < brep.Faces.Count; i++)
            {
                var area_properties = AreaMassProperties.Compute(brep.Faces[i]);

                double faceArea = Math.Round(area_properties.Area, 2);

                area += faceArea;
            }

            // The raw sum is in model units; topArea/bottomArea are already
            // converted, so convert BEFORE subtracting them.
            area *= RunQTO.areaConversionFactor;

            area -= (this.topArea + this.bottomArea);

            area = Math.Round(area, 2);

            return area;
        }

        void PerimeterAndOpeningPerimeter(List<Brep> breps)
        {
            List<Brep> projectedBreps = new List<Brep>();

            Plane xyPlane = Plane.WorldXY;

            Vector3d projectionDirection = new Vector3d(0, 0, -1);

            Transform projectionTransform = Transform.ProjectAlong(xyPlane, projectionDirection);

            foreach (Brep brep in breps)
            {
                Brep projectedBrep = brep.DuplicateBrep();

                projectedBrep.Transform(projectionTransform);

                projectedBreps.Add(projectedBrep);
            }

            // JoinBreps returns null when nothing could be joined - the exact
            // family v1.02 guarded in the four linear templates but missed
            // here; a stepped slab must not be dropped as bad geometry over
            // its footprint. Fall back to measuring the unjoined faces.
            Brep[] joinedBreps = Brep.JoinBreps(projectedBreps, RunQTO.doc.ModelAbsoluteTolerance);

            if (joinedBreps == null || joinedBreps.Length == 0)
            {
                Logger.Warn("Slab " + this.id + " on layer '" + this.layerName +
                    "': the projected top faces could not be joined; perimeter is measured per face.");

                joinedBreps = projectedBreps.ToArray();
            }

            if (joinedBreps.Length > 1)
            {
                Logger.Warn("Slab " + this.id + " on layer '" + this.layerName +
                    "': the footprint joined into " + joinedBreps.Length + " separate shells; " +
                    "perimeter and opening perimeter are summed over all of them and may " +
                    "overcount where shells overlap in plan.");
            }

            this.perimeter = 0;
            this.openingPerimeter = 0;

            // Every shell and every face: the old code read Faces[0]'s outer
            // loop only, silently losing the other fragments' boundaries and
            // openings.
            foreach (Brep joinedBrep in joinedBreps)
            {
                joinedBrep.MergeCoplanarFaces(RunQTO.doc.ModelAbsoluteTolerance);

                // A shell that keeps multiple faces after the merge counts
                // each internal seam edge in two faces' outer loops - the
                // one degraded outcome the other two warnings don't cover.
                if (joinedBrep.Faces.Count > 1)
                {
                    Logger.Warn("Slab " + this.id + " on layer '" + this.layerName +
                        "': the joined footprint kept " + joinedBrep.Faces.Count +
                        " faces after the coplanar merge; internal seam edges are " +
                        "counted twice, so the perimeter may overreport.");
                }

                foreach (BrepFace face in joinedBrep.Faces)
                {
                    foreach (BrepLoop loop in face.Loops)
                    {
                        if (loop.LoopType == BrepLoopType.Inner)
                        {
                            this.openingPerimeter += loop.To3dCurve().GetLength();
                        }
                        else
                        {
                            this.perimeter += loop.To3dCurve().GetLength();
                        }
                    }
                }
            }

            this.perimeter = Math.Round(this.perimeter * RunQTO.lengthConversionFactor, 2);
            this.openingPerimeter = Math.Round(this.openingPerimeter * RunQTO.lengthConversionFactor, 2);
        }

        public void UpdateNetVolumeAndBottomAreaWithBeams()
        {
            if (this.beams.Count > 0)
            {
                Double intersectionVolume = 0;
                Double intersectedBeamBottomArea = 0;

                // The trivial-intersection gate means "at least 5 cubic feet",
                // expressed in model units so it holds in any unit system -
                // the old raw ">5" was 5 in^3 in an inch model (always passed)
                // and 5 m^3 in a metric one (never passed).
                double feetToModel = Rhino.RhinoMath.UnitScale(Rhino.UnitSystem.Feet, RunQTO.doc.ModelUnitSystem);
                double minIntersectionVolume = 5.0 * feetToModel * feetToModel * feetToModel;

                foreach (var item in this.beams)
                {
                    Brep[] intersectionBreps = Brep.CreateBooleanIntersection(this.geometry, item.Value.geometry, RunQTO.doc.ModelAbsoluteTolerance);

                    if (intersectionBreps != null && intersectionBreps.Length > 0)
                    {
                        foreach (Brep intersectionBrep in intersectionBreps)
                        {
                            var intersection_mass_properties = VolumeMassProperties.Compute(intersectionBrep);
                            var beam_mass_properties = VolumeMassProperties.Compute(item.Value.geometry);

                            if (intersection_mass_properties != null && beam_mass_properties != null &&
                                intersection_mass_properties.Volume > minIntersectionVolume &&
                                intersection_mass_properties.Volume < beam_mass_properties.Volume)
                            {
                                // The deduction must use the SAME factor the
                                // net volume was computed with; the hardcoded
                                // 0.037037 (ft3 -> yd3) was only right in
                                // feet models.
                                intersectionVolume += intersection_mass_properties.Volume * RunQTO.volumeConversionFactor;

                                intersectedBeamBottomArea += item.Value.bottomArea;
                            }
                        }
                    }
                }

                this.netVolume = Math.Round(this.netVolume - intersectionVolume, 2);

                this.bottomArea -= intersectedBeamBottomArea;
            }
        }
    }
}
