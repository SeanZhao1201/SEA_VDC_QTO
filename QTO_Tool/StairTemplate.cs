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
    class StairTemplate
    {
        public Brep geometry { get; set; }
        public System.Drawing.Color color { get; set; }
        public string layerName { get; set; }
        public string nameAbb { get; set; }
        public string id { get; set; }

        public Dictionary<string, string> AttributeUserStrings { get; private set; }

        public Dictionary<string, string> parsedLayerName = new Dictionary<string, string>();
        public string floor { get; set; }
        public double volume { get; set; }
        public double treadArea { get; set; }
        public double riserArea { get; set; }
        public int treadCount { get; set; }
        public double bottomArea { get; set; }
        public double sideArea { get; set; }

        public string type = "StairTemplate";

        private List<double> upfacingFaceAreas = new List<double>();
        private List<Brep> upfacingFaces = new List<Brep>();

        private List<double> downfacingFaceAreas = new List<double>();
        private List<Brep> downfacingFaces = new List<Brep>();
        private List<double> downfacingFaceElevations = new List<double>();

        private List<double> sideAndRiserFaceAreas = new List<double>();
        private List<Brep> sideAndRiserFaces = new List<Brep>();

        private List<double> sideFaceAreas = new List<double>();
        private List<Brep> sideFaces = new List<Brep>();

        private List<double> riserFaceAreas = new List<double>();
        private List<Brep> riserFaces = new List<Brep>();

        public static string[] units = { "N/A", "N/A", "Cubic Yard", "Square Foot", "Square Foot", "Square Foot", "N/A", "Square Foot", "Square Foot", "N/A" };

        // Faces whose |normal . Z| is at or below this stay with the sides and
        // risers: a battered riser (a few degrees off plumb) must not become a
        // tread, while the sloped stair soffit (|dot| ~0.8) must still land in
        // the bottom bucket - so this cannot be the UI slider's threshold
        // (~0.94), which the linear templates use for the opposite split.
        private const double VerticalFaceDotEpsilon = 0.3;

        // angleThreshold is accepted for uniformity with the other template
        // constructors but takes no part in the stair classification - see
        // VerticalFaceDotEpsilon.
        public StairTemplate(RhinoObject rhobj, string _layerName, System.Drawing.Color layerColor, double angleThreshold, Dictionary<double, string> floorElevations)
        {
            this.layerName = _layerName;

            this.color = layerColor;

            this.geometry = (Brep)rhobj.Geometry;

            this.id = rhobj.Id.ToString();

            AttributeUserStrings = Methods.CopyRhinoAttributeUserStrings(rhobj);

            for (int i = 0; i < _layerName.Split('_').ToList().Count; i++)
            {
                parsedLayerName.Add("C" + (1 + i).ToString(), _layerName.Split('_').ToList()[i]);
            }

            nameAbb = parsedLayerName["C1"] + " " + parsedLayerName["C2"];

            var mass_properties = VolumeMassProperties.Compute(this.geometry);
            this.volume = Math.Round(mass_properties.Volume * RunQTO.volumeConversionFactor, 2);

            this.TreadAndRiserAndBottomArea(this.geometry);

            // A solid with no downward face at all (possible now that the
            // classification uses a real angle epsilon) must not die on
            // Min()-of-empty; it lands in the "-" bucket instead.
            if (floorElevations.Count > 0 && this.downfacingFaceElevations.Count > 0)
            {
                this.floor = Methods.FindFloor(floorElevations, this.downfacingFaceElevations.Min());
            }
            else
            {
                // The "-" bucket is the field logs' most expensive failure
                // domain; a stair landing there WHILE floors are defined is an
                // anomaly worth a trace.
                if (floorElevations.Count > 0)
                {
                    Logger.Warn("Stair " + this.id + " on layer '" + this.layerName +
                        "' has no downward face; its floor was set to \"-\".");
                }

                this.floor = "-";
            }
        }

        void TreadAndRiserAndBottomArea(Brep brep)
        {
            Dictionary<string, double> result = new Dictionary<string, double>();

            Vector3d normal;
            double u, v;
            Point3d center;

            Plane frame;

            double dotProduct, curveParameter;

            Vector3d curveTangent;

            for (int i = 0; i < brep.Faces.Count; i++)
            {
                var area_properties = AreaMassProperties.Compute(brep.Faces[i]);

                center = area_properties.Centroid;

                if (brep.Faces[i].ClosestPoint(center, out u, out v))
                {
                    normal = brep.Faces[i].NormalAt(u, v);

                    normal.Unitize();

                    brep.Faces[i].FrameAt(u, v, out frame);

                    dotProduct = Math.Round(Vector3d.Multiply(normal, Vector3d.ZAxis), 2);

                    // ModelAbsoluteTolerance is a distance (~0.001), not an
                    // angle: comparing the dot product against it sent every
                    // face tilted more than ~0.6 degrees off plumb into the
                    // tread or bottom bucket.
                    if (dotProduct > VerticalFaceDotEpsilon)
                    {
                        this.upfacingFaceAreas.Add(area_properties.Area);
                        this.upfacingFaces.Add(brep.Faces[i].DuplicateFace(false));
                    }

                    else if (dotProduct < -VerticalFaceDotEpsilon)
                    {
                        this.downfacingFaceAreas.Add(area_properties.Area);
                        this.downfacingFaces.Add(brep.Faces[i].DuplicateFace(false));
                        this.downfacingFaceElevations.Add(center.Z);
                    }

                    else
                    {
                        this.sideAndRiserFaces.Add(brep.Faces[i].DuplicateFace(false));
                        this.sideAndRiserFaceAreas.Add(area_properties.Area);
                    }
                }
            }

            this.treadArea = Math.Round(this.upfacingFaceAreas.Sum() * RunQTO.areaConversionFactor, 2);
            this.bottomArea = Math.Round(this.downfacingFaceAreas.Sum() * RunQTO.areaConversionFactor, 2);

            this.treadCount = this.upfacingFaceAreas.Count;

            // No tread-like face at all (a ramp-flight solid on a stair
            // layer): there is no tread boundary to derive the flight
            // centerline from, so everything vertical-ish counts as side area
            // instead of dying on an index error and dropping the whole solid
            // from the take-off.
            if (this.upfacingFaces.Count == 0)
            {
                DegradeToSideOnly("has no tread-like face");
                return;
            }

            double offsetDistance = double.MaxValue;

            for (int i = 0; i < this.upfacingFaces[0].Edges.Count; i++)
            {
                if (this.upfacingFaces[0].Edges[i].GetLength() < offsetDistance)
                {
                    offsetDistance = this.upfacingFaces[0].Edges[i].GetLength();
                }
            }

            offsetDistance *= 0.48;

            // JoinCurves and Offset both return null/empty on failure (a
            // kinked or non-planar tread boundary under the 0.3 dot
            // epsilon): degrade like the no-tread case instead of the index
            // error dropping a valid stair from the take-off.
            Curve[] joinedBoundary = Curve.JoinCurves(this.upfacingFaces[0].Edges);
            if (joinedBoundary == null || joinedBoundary.Length == 0)
            {
                DegradeToSideOnly("has a tread boundary that could not be joined");
                return;
            }
            Curve treadBoundary = joinedBoundary[0];
            treadBoundary = treadBoundary.Simplify(CurveSimplifyOptions.All, RunQTO.doc.ModelAbsoluteTolerance, RunQTO.doc.ModelAngleToleranceRadians) ?? treadBoundary;

            Curve[] offset1 = treadBoundary.Offset(Plane.WorldXY, offsetDistance, RunQTO.doc.ModelAbsoluteTolerance, CurveOffsetCornerStyle.Sharp);
            Curve[] offset2 = treadBoundary.Offset(Plane.WorldXY, -offsetDistance, RunQTO.doc.ModelAbsoluteTolerance, CurveOffsetCornerStyle.Sharp);
            if (offset1 == null || offset1.Length == 0 ||
                offset2 == null || offset2.Length == 0)
            {
                DegradeToSideOnly("has a tread boundary whose offset failed");
                return;
            }
            Curve curveOffset1 = offset1[0];
            Curve curveOffset2 = offset2[0];
            List<Curve> shorterSegments = new List<Curve>();
            Curve[] curveOffsetSegments;

            if (curveOffset1.GetLength() > curveOffset2.GetLength())
            {
                curveOffsetSegments = curveOffset2.DuplicateSegments();
            }
            else
            {
                curveOffsetSegments = curveOffset1.DuplicateSegments();
            }

            for (int i = 0; i < curveOffsetSegments.Length; i++)
            {
                if (i == 0)
                {
                    shorterSegments.Add(curveOffsetSegments[i]);
                }
                else
                {
                    if (Math.Round(shorterSegments[0].GetLength(), 2) > Math.Round(curveOffsetSegments[i].GetLength(), 2))
                    {
                        shorterSegments.Clear();
                        shorterSegments.Add(curveOffsetSegments[i]);
                    }

                    else if (Math.Round(shorterSegments[0].GetLength(), 2) == Math.Round(curveOffsetSegments[i].GetLength(), 2))
                    {
                        shorterSegments.Add(curveOffsetSegments[i]);
                    }
                }
            }

            // Two segments only TIE for shortest on a rectangular tread; a
            // winder/tapered tread or a curved offset (one segment) leaves a
            // single entry and [1] would throw.
            if (shorterSegments.Count < 2)
            {
                DegradeToSideOnly("has no pair of equal shortest tread edges " +
                    "(winder or tapered tread?)");
                return;
            }

            Curve centerLine = new Line(shorterSegments[0].PointAtLength(shorterSegments[0].GetLength() / 2), shorterSegments[1].PointAtLength(shorterSegments[1].GetLength() / 2)).ToNurbsCurve();
            
            //Side and Edges Calculation
            for (int i = 0; i < this.sideAndRiserFaces.Count; i++)
            {
                var area_properties = AreaMassProperties.Compute(this.sideAndRiserFaces[i]);

                center = area_properties.Centroid;

                centerLine.ClosestPoint(center, out curveParameter);

                curveTangent = centerLine.TangentAt(curveParameter);

                if (this.sideAndRiserFaces[i].Faces[0].ClosestPoint(center, out u, out v))
                {
                    normal = this.sideAndRiserFaces[i].Faces[0].NormalAt(u, v);

                    normal.Unitize();

                    dotProduct = Math.Round(Vector3d.Multiply(normal, curveTangent), 2);

                    if (dotProduct > -0.1 && dotProduct < 0.1)
                    {
                        this.riserFaces.Add(this.sideAndRiserFaces[i]);
                        this.riserFaceAreas.Add(this.sideAndRiserFaceAreas[i]);
                    }

                    else
                    {
                        this.sideFaces.Add(this.sideAndRiserFaces[i]);
                        this.sideFaceAreas.Add(this.sideAndRiserFaceAreas[i]);
                    }
                }
            }

            this.riserArea = Math.Round(this.riserFaceAreas.Sum() * RunQTO.areaConversionFactor, 2);
            this.sideArea = Math.Round(this.sideFaceAreas.Sum() * RunQTO.areaConversionFactor, 2);
        }

        /// <summary>The flight centerline could not be derived (no tread,
        /// unjoinable boundary, failed offset, no tied shortest edges):
        /// riser area goes to 0 and every near-vertical face counts as side
        /// area. Quantities are reshaped, not dropped - a field report about
        /// wrong stair numbers needs to see WHICH stairs degraded and why.</summary>
        private void DegradeToSideOnly(string why)
        {
            Logger.Warn("Stair " + this.id + " on layer '" + this.layerName +
                "' " + why + "; riser area set to 0 and all near-vertical " +
                "faces counted as side area.");

            this.riserArea = 0;
            this.sideFaceAreas.AddRange(this.sideAndRiserFaceAreas);
            this.sideFaces.AddRange(this.sideAndRiserFaces);
            this.sideArea = Math.Round(this.sideFaceAreas.Sum() * RunQTO.areaConversionFactor, 2);
        }
    }
}
