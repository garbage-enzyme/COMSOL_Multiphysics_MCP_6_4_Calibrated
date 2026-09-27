"""Independent numerical oracles for repository-only licensed DNN gates.

COMSOL and ONNX imports stay inside explicitly invoked operations. Synthetic
fixtures establish API behavior, never FEM validity or scientific promotion.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path


def content_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def frozen_split(rows: list[dict[str, float]]) -> dict:
    """Freeze membership before any training; each fixture coordinate is unique."""
    if len(rows) != 48:
        raise ValueError("the licensed fixture requires exactly 48 rows")
    order = list(range(len(rows)))
    random.Random(17).shuffle(order)  # noqa: S311 - reproducible fixture partition, not security
    ids = {"train": order[:34], "validation": order[34:41], "test": order[41:]}
    result = {
        "row_ids": ids,
        "rows": {key: [rows[i] for i in indexes] for key, indexes in ids.items()},
        "features": ["a1", "a2"],
        "target": "qoi",
        "units": "dimensionless",
        "scope": "synthetic_api_fixture_not_scientific_acceptance",
        "dataset_sha256": content_hash(rows),
    }
    result["split_sha256"] = content_hash(result)
    return result


def write_rows(path: Path, rows: list[dict[str, float]]) -> None:
    text = "\r\n".join(
        ", ".join(format(row[key], ".17g") for key in ("a1", "a2", "qoi")) for row in rows
    )
    path.write_text(text + "\r\n", encoding="utf-8", newline="")


def score_holdout(split: dict, predictions: list[list[float]]) -> dict:
    """Derive physical-unit metrics from predictions, never from COMSOL loss."""
    from comsol_mcp.surrogate.training import compute_metrics

    body = {key: value for key, value in split.items() if key != "split_sha256"}
    if content_hash(body) != split["split_sha256"]:
        raise ValueError("frozen split identity mismatch")
    identifiers = split["row_ids"]
    flattened = sum(identifiers.values(), [])
    if len(flattened) != len(set(flattened)):
        raise ValueError("a row occurs in more than one split")
    actual = [[row["qoi"]] for row in split["rows"]["test"]]
    return {
        "row_ids": identifiers["test"],
        "predictions": predictions,
        "targets": actual,
        "metrics": compute_metrics(predicted=predictions, actual=actual),
        "split_sha256": split["split_sha256"],
        "units": split["units"],
        "raw_testloss_is_rmse": "not_assumed",
    }


def bind_split_tables(model, backend, dnn, split: dict) -> dict:
    """Bind explicit validation/test result tables; training file contains train only."""
    import jpype

    result = {}
    for name in ("validation", "test"):
        tag = "gate_" + name
        rows = split["rows"][name]
        values = [[row[key] for key in ("a1", "a2", "qoi")] for row in rows]
        table = model.java.result().table().create(tag, "Table")
        table.setColumnHeaders(jpype.JArray(jpype.JString)(["a1", "a2", "qoi"]))
        table.setTableData(jpype.JArray(jpype.JDouble, 2)(values))
        observed = [[float(v) for v in row] for row in table.getReal()]
        if observed != values:
            raise RuntimeError(f"{name} table readback differs from the frozen rows")
        backend.write_scalar(dnn, name + "table", tag, "string")
        readback = backend.read_property(dnn, name + "table")
        if readback.get("value") != tag:
            raise RuntimeError(f"{name} table binding was not retained")
        result[name] = {"tag": tag, "row_ids": split["row_ids"][name], "values": observed}
    return result


def comsol_predictions(model, rows: list[dict[str, float]], function: str) -> list[list[float]]:
    """Evaluate through a tiny solved dataset, independently of the exported graph.

    A geometry-free Global node returns an empty result. This one-dimensional
    fixture creates the dataset required by COMSOL's expression evaluator; it
    is an API scaffold and is not scientific validation of the surrogate.
    """
    import jpype

    java = model.java
    prior = {str(item) for item in java.result().dataset().tags()}
    component = java.component("comp1")
    geometry = component.geom().create("gate_geom", 1)
    interval = geometry.create("gate_interval", "Interval")
    interval.set("coord", jpype.JArray(jpype.JString)(["0", "1"]))
    geometry.run()
    component.physics().create("gate_pde", "CoefficientFormPDE", "gate_geom")
    study = java.study().create("gate_eval_study")
    study.create("stat", "Stationary")
    study.run()
    created = {str(item) for item in java.result().dataset().tags()} - prior
    if len(created) != 1:
        raise RuntimeError("evaluation scaffold must produce exactly one new solution dataset")
    tag = "gate_predictions"
    numeric = java.result().numerical().create(tag, "Global")
    try:
        numeric.set("data", created.pop())
        expressions = [f"{function}({row['a1']:.17g},{row['a2']:.17g})" for row in rows]
        numeric.set("expr", jpype.JArray(jpype.JString)(expressions))
        predictions = [[float(v) for v in row] for row in numeric.getReal()]
        # Global getReal() returns solutions by expressions on this exact build.
        if len(predictions) == 1 and len(predictions[0]) == len(rows):
            predictions = [[value] for value in predictions[0]]
        if len(predictions) != len(rows) or any(len(row) != 1 for row in predictions):
            raise RuntimeError(
                f"COMSOL prediction shape differs from the frozen rows: {predictions}"
            )
        if any(not math.isfinite(row[0]) for row in predictions):
            raise RuntimeError("COMSOL returned nonfinite predictions")
        return predictions
    finally:
        model.java.result().numerical().remove(tag)


def onnx_predictions(path: Path, rows: list[dict[str, float]]) -> tuple[list[list[float]], dict]:
    """Parse/check the entire ONNX graph and execute its actual operators."""
    import numpy as np
    import onnx
    from onnx.reference import ReferenceEvaluator

    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("licensed gate ONNX payload exceeds 16 MiB")
    model = onnx.load_model(path, load_external_data=False)
    if any(t.data_location == onnx.TensorProto.EXTERNAL for t in model.graph.initializer):
        raise ValueError("external ONNX data is not permitted in the bounded gate")
    onnx.checker.check_model(model)
    if len(model.graph.input) != 1 or len(model.graph.output) != 1:
        raise ValueError("the scalar gate requires one input tensor and one output tensor")
    input_info = model.graph.input[0]
    dtype = onnx.helper.tensor_dtype_to_np_dtype(input_info.type.tensor_type.elem_type)
    inputs = np.asarray([[r["a1"], r["a2"]] for r in rows], dtype=dtype)
    prediction = np.asarray(ReferenceEvaluator(model).run(None, {input_info.name: inputs})[0])
    if prediction.shape != (len(rows), 1) or not np.isfinite(prediction).all():
        raise ValueError("ONNX predictions must be finite and match the frozen sample order")
    evidence = {
        "is_onnx_protobuf": True,
        "graph_nodes": len(model.graph.node),
        "initializers": [{"name": t.name, "shape": list(t.dims)} for t in model.graph.initializer],
        "operator_types": sorted({n.op_type for n in model.graph.node}),
        "producer": model.producer_name,
        "has_weights": bool(model.graph.initializer),
        "carries_trained_state": bool(model.graph.initializer) and bool(model.graph.node),
        "evaluator": "onnx.reference.ReferenceEvaluator",
        "onnx_version": onnx.__version__,
        "input_name": input_info.name,
        "input_dtype": str(dtype),
        "payload_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    return prediction.tolist(), evidence


def mutated_onnx_predictions(path: Path, rows: list[dict[str, float]]) -> dict:
    """Challenge activation, normalization and feature-order semantics independently."""
    import numpy as np
    import onnx
    from onnx import numpy_helper

    result = {}
    for kind in ("activation", "normalization"):
        model = onnx.load_model(path, load_external_data=False)
        if kind == "activation":
            candidates = [node for node in model.graph.node if node.op_type == "Tanh"]
            if not candidates:
                raise ValueError("the mutation fixture requires a Tanh activation")
            candidates[0].op_type = "Identity"
        else:
            candidates = [tensor for tensor in model.graph.initializer if tensor.name == "scale"]
            if len(candidates) != 1:
                raise ValueError("the mutation fixture requires an input scale tensor")
            tensor = candidates[0]
            values = np.array(numpy_helper.to_array(tensor), copy=True)
            values.flat[0] += 1.0
            tensor.CopyFrom(numpy_helper.from_array(values, name=tensor.name))
        destination = path.with_name(path.stem + "_" + kind + ".onnx")
        if destination.exists():
            raise FileExistsError(destination)
        onnx.save_model(model, destination)
        result[kind], _ = onnx_predictions(destination, rows)
    swapped = [{**row, "a1": row["a2"], "a2": row["a1"]} for row in rows]
    result["feature_order"], _ = onnx_predictions(path, swapped)
    return result
