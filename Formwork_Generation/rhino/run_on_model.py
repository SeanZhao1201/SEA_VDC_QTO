# -*- coding: utf-8 -*-
"""Batch run of formwork_gen_rhino on the currently open model, EXPORT mode.

Launched as:  Rhino.exe <model.3dm> /nosplash
              /runscript="-_RunPythonScript <this file>"
The document is never modified and never saved. Output lands in the staging
folder: formwork_out.3dm + model_run_log.txt (+ model_run_error.txt on crash).
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

    doc = Rhino.RhinoDoc.ActiveDoc
    pre = fw.Log()
    pre("model: {0}".format(doc.Path))
    pre("units: {0}  abs tol: {1}".format(
        doc.ModelUnitSystem, doc.ModelAbsoluteTolerance))
    pre("FloorElevations doc-string: {0}".format(
        "present" if doc.Strings.GetValue("FloorElevations") else "ABSENT"))
    layer_counts = {}
    for obj in doc.Objects:
        if obj is None or obj.Attributes is None:
            continue
        layer = doc.Layers[obj.Attributes.LayerIndex]
        key = layer.FullPath if layer is not None else "?"
        layer_counts[key] = layer_counts.get(key, 0) + 1
    pre("layers with objects:")
    for k in sorted(layer_counts):
        pre("  {0:60} {1}".format(k, layer_counts[k]))

    result = fw.main({
        "mode": "export",
        "grade_z": None,             # flag no-hit props, don't invent ground
        "export_path": os.path.join(STAGE, "formwork_out.3dm"),
        "log_path": os.path.join(STAGE, "model_run_log.txt"),
    })
    # fw.main saved its own log; prepend our context lines to the same file
    log_path = os.path.join(STAGE, "model_run_log.txt")
    try:
        with io.open(log_path, "r", encoding="utf-8") as fh:
            body = fh.read()
    except Exception:
        body = u""
    with io.open(log_path, "w", encoding="utf-8") as fh:
        fh.write(u"\n".join(u"{0}".format(l) for l in pre.lines)
                 + u"\n" + body)
except Exception:
    with io.open(os.path.join(STAGE, "model_run_error.txt"), "w",
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
