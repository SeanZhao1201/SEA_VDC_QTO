# -*- coding: utf-8 -*-
"""Batch run of jumpform_gen_rhino on the currently open model, EXPORT mode.

Launched as:  Rhino.exe <model.3dm> /nosplash
              /runscript="-_RunPythonScript <this file>"
The document is never modified and never saved. Output lands in the
staging folder: jumpform_out.3dm + jumpform_out.json +
jumpform_model_log.txt (+ jumpform_model_error.txt on crash).
"""
from __future__ import division, print_function

import io
import os
import sys
import traceback

STAGE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "qto_fw_test")
sys.path.insert(0, STAGE)

import Rhino

try:
    import formwork_gen_rhino as fw
    import jumpform_gen_rhino as jf

    doc = Rhino.RhinoDoc.ActiveDoc
    pre = fw.Log()
    pre("model: {0}".format(doc.Path))
    pre("units: {0}  abs tol: {1}".format(
        doc.ModelUnitSystem, doc.ModelAbsoluteTolerance))
    pre("FloorElevations doc-string: {0}".format(
        "present" if doc.Strings.GetValue("FloorElevations") else "ABSENT"))
    n_stamped = sum(1 for o in doc.Objects
                    if o is not None and o.Attributes is not None
                    and o.Attributes.GetUserString("QTO_STABLE_ID"))
    pre("objects with QTO_STABLE_ID stamps: {0}".format(n_stamped))

    jf.main({
        "mode": "export",
        "export_path": os.path.join(STAGE, "jumpform_out.3dm"),
        "log_path": os.path.join(STAGE, "jumpform_model_log.txt"),
    })
    log_path = os.path.join(STAGE, "jumpform_model_log.txt")
    try:
        with io.open(log_path, "r", encoding="utf-8") as fh:
            body = fh.read()
    except Exception:
        body = u""
    with io.open(log_path, "w", encoding="utf-8") as fh:
        fh.write(u"\n".join(u"{0}".format(l) for l in pre.lines)
                 + u"\n" + body)
except Exception:
    with io.open(os.path.join(STAGE, "jumpform_model_error.txt"), "w",
                 encoding="utf-8") as fh:
        fh.write(u"{0}".format(traceback.format_exc()))
finally:
    try:
        Rhino.RhinoDoc.ActiveDoc.Modified = False
    except Exception:
        pass
    if os.environ.get("FW_HEADLESS") == "1":
        try:
            Rhino.RhinoApp.Exit()
        except Exception:
            Rhino.RhinoApp.RunScript("_-Exit", False)
