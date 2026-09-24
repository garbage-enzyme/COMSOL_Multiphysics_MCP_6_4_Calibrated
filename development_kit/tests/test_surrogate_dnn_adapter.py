"""Solver-free surrogate DNN configuration, write-plan, and rollback tests."""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from comsol_mcp.durable.canonical import canonical_sha256_v1
from comsol_mcp.surrogate.dnn_adapter import (
    ALLOWED_ACTIVATIONS,
    COMSOL_OWNED_DEFAULTS,
    DNN_FUNCTION_TAG,
    STUDY_STEP_TAG,
    STUDY_TAG,
    apply_surrogate_configuration,
    build_dnn_configuration,
    build_write_plan,
    validate_dnn_configuration,
)

SOLVER_FREE_BANNED = (
    "import mph",
    "from mph",
    "import jpype",
    "from jpype",
    "import comsol",
    "from comsol.",
    "onnxruntime",
    "torch",
    "tensorflow",
)


def _configuration(**overrides) -> dict:
    kwargs = {
        "configuration_id": "dnn-cfg-1",
        "input_features": ["w", "h"],
        "output_features": ["R"],
        "hidden_layers": [16, 8],
        "activation": "tanh",
        "optimizer": "adam",
        "loss": "mse",
        "learning_rate": 1e-3,
        "batch_size": 8,
        "maximum_epochs": 50,
        "seed": 17,
        "validation_mode": "table",
        "test_mode": "table",
    }
    kwargs.update(overrides)
    return build_dnn_configuration(**kwargs)


# --------------------------------------------------------------------------
# Configuration contract
# --------------------------------------------------------------------------


def test_configuration_seals_bounded_fully_connected_architecture() -> None:
    config = _configuration()
    assert config["architecture"]["kind"] == "fully_connected_dense"
    assert config["architecture"]["layer_configuration"] == [2, 16, 8, 1]
    assert config["custom_layers_allowed"] is False
    assert config["architecture_search_allowed"] is False
    assert config["gpu"]["gputraining"] is False
    assert config["determinism"]["seed"] == 17
    validate_dnn_configuration(config)


def test_configuration_rejects_out_of_bound_architecture() -> None:
    with pytest.raises(ValueError, match="1-8 entries"):
        _configuration(hidden_layers=[4] * 9)
    with pytest.raises(ValueError, match="1..512"):
        _configuration(hidden_layers=[1024])
    with pytest.raises(ValueError, match="2-12 entries"):
        _configuration(input_features=["only"])
    with pytest.raises(ValueError, match="must not overlap"):
        _configuration(output_features=["w"])


def test_configuration_rejects_unsupported_choices() -> None:
    with pytest.raises(ValueError, match="activation must be one of"):
        _configuration(activation="swish")
    with pytest.raises(ValueError, match="optimizer must be one of"):
        _configuration(optimizer="lbfgs")
    with pytest.raises(ValueError, match="loss must be one of"):
        _configuration(loss="huber")
    with pytest.raises(ValueError, match="validation_mode must be one of"):
        _configuration(validation_mode="kfold")
    with pytest.raises(ValueError, match="test_mode must be one of"):
        _configuration(test_mode="fraction")


def test_configuration_rejects_nonfinite_and_bad_budgets() -> None:
    with pytest.raises(ValueError, match="learning_rate"):
        _configuration(learning_rate=float("nan"))
    with pytest.raises(ValueError, match="learning_rate"):
        _configuration(learning_rate=0.0)
    with pytest.raises(ValueError, match="batch_size"):
        _configuration(batch_size=0)
    with pytest.raises(ValueError, match="maximum_epochs"):
        _configuration(maximum_epochs=0)
    with pytest.raises(ValueError, match="seed"):
        _configuration(seed=-1)


def test_momentum_is_only_valid_for_sgd() -> None:
    with pytest.raises(ValueError, match="only meaningful when optimizer is sgd"):
        _configuration(momentum=0.9)
    sgd = _configuration(optimizer="sgd", momentum=0.9, momentum_used=True)
    assert sgd["training"]["momentum"] == 0.9


def test_column_scales_reference_known_columns_only() -> None:
    config = _configuration(column_scales={"w": "std", "R": "to01"})
    assert config["column_scales"] == {"R": "to01", "w": "std"}
    with pytest.raises(ValueError, match="unknown columns"):
        _configuration(column_scales={"nope": "std"})
    with pytest.raises(ValueError, match="must be one of"):
        _configuration(column_scales={"w": "zscore"})


def test_configuration_is_tamper_evident_and_deterministic() -> None:
    config = _configuration()
    assert _configuration()["configuration_sha256"] == config["configuration_sha256"]
    tampered = dict(config)
    tampered["training"] = {**config["training"], "learning_rate": 0.5}
    with pytest.raises(ValueError):
        validate_dnn_configuration(tampered)


# --------------------------------------------------------------------------
# Write plan
# --------------------------------------------------------------------------


def test_write_plan_uses_only_proven_properties() -> None:
    plan = build_write_plan(_configuration())
    written = {item["property"] for item in plan["scalar_writes"]}
    assert {"activation", "layertype", "outfeatures", "optmethod", "loss"} <= written
    assert {"useseed", "rndseed", "useseedvalidation", "rndseedvalidation"} <= written
    assert {"validation", "test", "gputraining"} <= written
    # Every write must target a property the S0 probe proved readable.
    proven = set(COMSOL_OWNED_DEFAULTS) | {
        "ignorenaninf",
        "layerconfig",
        "trained_chksum",
        "globaldnnfunction",
        "args",
        "colscale",
    }
    assert written <= proven, f"unproven writes: {sorted(written - proven)}"
    step_written = {item["property"] for item in plan["step_scalar_writes"]}
    step_entries = {item["property"] for item in plan["step_entry_writes"]}
    assert step_written <= proven, f"unproven step writes: {sorted(step_written - proven)}"
    assert step_entries <= proven, f"unproven step entries: {sorted(step_entries - proven)}"
    assert plan["write_count"] == (
        len(plan["scalar_writes"])
        + len(plan["entry_writes"])
        + len(plan["step_scalar_writes"])
        + len(plan["step_entry_writes"])
    )


def test_write_plan_layer_types_match_declared_architecture() -> None:
    plan = build_write_plan(_configuration(hidden_layers=[32]))
    layertype = next(item for item in plan["scalar_writes"] if item["property"] == "layertype")
    assert layertype["value"] == ["input", "dense", "dense"]
    outfeatures = next(item for item in plan["scalar_writes"] if item["property"] == "outfeatures")
    assert outfeatures["value"] == [2, 32, 1]


def test_activation_array_is_per_layer_matching_layertype() -> None:
    """COMSOL requires len(activation) == len(layertype); a short array errors."""
    plan = build_write_plan(_configuration(hidden_layers=[16, 8], activation="gelu"))
    activation = next(item for item in plan["scalar_writes"] if item["property"] == "activation")
    layertype = next(item for item in plan["scalar_writes"] if item["property"] == "layertype")
    # input, hidden, hidden, output dense
    assert layertype["value"] == ["input", "dense", "dense", "dense"]
    assert len(activation["value"]) == len(layertype["value"])
    # The input layer never carries an activation.
    assert activation["value"] == ["none", "gelu", "gelu", "gelu"]


def test_step_activation_array_also_matches_layertype() -> None:
    plan = build_write_plan(_configuration(hidden_layers=[4], activation="tanh"))
    step_activation = next(
        item for item in plan["step_scalar_writes"] if item["property"] == "activation"
    )
    step_layertype = next(
        item for item in plan["step_scalar_writes"] if item["property"] == "layertype"
    )
    assert len(step_activation["value"]) == len(step_layertype["value"])
    assert step_activation["value"] == ["none", "tanh", "tanh"]


def test_write_plan_omits_momentum_for_adam_and_includes_it_for_sgd() -> None:
    adam = build_write_plan(_configuration())
    assert "momentum" not in {item["property"] for item in adam["scalar_writes"]}
    sgd = build_write_plan(_configuration(optimizer="sgd", momentum=0.9, momentum_used=True))
    momentum = next(item for item in sgd["scalar_writes"] if item["property"] == "momentum")
    assert momentum["value"] == 0.9


def test_write_plan_defers_args_until_a_data_source_is_bound() -> None:
    """COMSOL refuses `args` while no data columns exist; it must be deferred."""
    unbound = build_write_plan(_configuration(column_scales={"w": "std"}))
    deferred = {item["property"]: item for item in unbound["deferred_writes"]}
    assert set(deferred) == {"args", "colscale"}
    assert deferred["args"]["reason"] == "data_source_not_bound"
    assert unbound["entry_writes"] == []
    assert unbound["data_source_bound"] is False


def test_write_plan_binds_arg_and_colscale_entries_once_data_is_bound() -> None:
    plan = build_write_plan(_configuration(column_scales={"w": "std"}), data_source_bound=True)
    entries = {item["property"]: item for item in plan["entry_writes"]}
    assert entries["args"]["entries"] == {"w": "w", "h": "h", "R": "R"}
    assert entries["colscale"]["entries"] == {"w": "std"}
    # args/colscale are alternating arrays and must go through setEntry.
    assert entries["args"]["writer"] == "setEntry"
    assert entries["colscale"]["writer"] == "setEntry"
    assert plan["deferred_writes"] == []
    assert plan["data_source_bound"] is True


def test_step_globaldnnfunction_uses_the_string_map_writer() -> None:
    plan = build_write_plan(_configuration())
    step_entries = {item["property"]: item for item in plan["step_entry_writes"]}
    assert step_entries["globaldnnfunction"]["entries"] == {"1": DNN_FUNCTION_TAG}
    assert step_entries["globaldnnfunction"]["writer"] == "stringMap"


def test_write_plan_is_deterministic() -> None:
    assert (
        build_write_plan(_configuration())["plan_sha256"]
        == build_write_plan(_configuration())["plan_sha256"]
    )


# --------------------------------------------------------------------------
# Fake backend: failure atomicity
# --------------------------------------------------------------------------


class FakeBackend:
    """Minimal in-memory backend implementing the typed adapter protocol."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.studies: dict[str, dict[str, Any]] = {}
        self.functions: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.fail_on = fail_on
        self.readback = {
            "activation": "none, tanh",
            "layertype": "input, dense",
            "outfeatures": "2, 16, 8, 1",
            "optmethod": "adam",
            "loss": "mse",
            "lr": "0.001",
            "batchsize": "8",
            "epochs": "50",
            "weightdecay": "0",
            "useseed": "manual",
            "rndseed": "17",
            "useseedvalidation": "manual",
            "rndseedvalidation": "17",
            "validation": "table",
            "test": "table",
            "gputraining": "false",
            "layerconfig": "[2,16,8,1]",
            "trained_chksum": "",
        }
        self.allowed = {
            "activation": sorted(ALLOWED_ACTIVATIONS),
            "optmethod": ["adam", "sgd"],
            "loss": ["mse", "mae"],
            "validation": ["random", "fraction", "last", "table"],
            "test": ["none", "random", "table"],
        }

    def _maybe_fail(self, stage: str) -> None:
        if self.fail_on == stage:
            raise RuntimeError(f"injected failure at {stage}")

    def study_tags(self) -> list[str]:
        return sorted(self.studies)

    def func_tags(self) -> list[str]:
        return sorted(self.functions)

    def create_study(self, tag: str) -> Any:
        self.calls.append(f"create_study:{tag}")
        self._maybe_fail("create_study")
        self.studies[tag] = {"features": {}}
        return self.studies[tag]

    def create_study_step(self, study_tag: str, step_tag: str, step_type: str) -> Any:
        self.calls.append(f"create_study_step:{study_tag}:{step_tag}")
        self._maybe_fail("create_study_step")
        self.studies[study_tag]["features"][step_tag] = {"type": step_type}
        return self.studies[study_tag]["features"][step_tag]

    def create_dnn_function(self, tag: str) -> Any:
        self.calls.append(f"create_dnn_function:{tag}")
        self._maybe_fail("create_dnn_function")
        self.functions[tag] = {}
        return self.functions[tag]

    def get_study_step(self, study_tag: str, step_tag: str) -> Any:
        return self.studies[study_tag]["features"][step_tag]

    def get_dnn_function(self, tag: str) -> Any:
        return self.functions[tag]

    def remove_study(self, tag: str) -> None:
        self.calls.append(f"remove_study:{tag}")
        self.studies.pop(tag, None)

    def remove_function(self, tag: str) -> None:
        self.calls.append(f"remove_function:{tag}")
        self.functions.pop(tag, None)

    def write_scalar(self, feature: Any, name: str, value: Any, kind: str) -> None:
        self.calls.append(f"write:{name}")
        self._maybe_fail(f"write:{name}")
        feature[name] = value

    def write_entries(self, feature: Any, name: str, entries: Mapping[str, str]) -> None:
        self.calls.append(f"write_entries:{name}")
        self._maybe_fail(f"write_entries:{name}")
        feature[name] = dict(entries)

    def write_string_map(self, feature: Any, name: str, entries: Mapping[str, str]) -> None:
        self.calls.append(f"write_string_map:{name}")
        self._maybe_fail(f"write_string_map:{name}")
        feature[name] = dict(entries)

    def read_property(self, feature: Any, name: str) -> dict[str, Any]:
        if name in feature:
            return {"readable": True, "value": feature[name]}
        if name in self.readback:
            return {"readable": True, "value": self.readback[name]}
        return {"readable": False, "reason": "not_readable"}

    def read_allowed_values(self, feature: Any, name: str) -> list[str] | None:
        return self.allowed.get(name)

    def bind_data_source(self, feature: Any, path: str) -> dict[str, Any]:
        self.calls.append(f"bind_data_source:{path}")
        self._maybe_fail("bind_data_source")
        self.source_path = path
        feature["source"] = "file"
        feature["filename"] = path
        return {"import_error": None, "column_keys": ["w", "h", "R"]}

    def train(self, feature: Any) -> None:
        self.calls.append("train")
        self._maybe_fail("train")
        # A real training run publishes losses and a trained identity.
        feature["trainingloss"] = "0.025"
        feature["validationloss"] = "0.027"
        feature["trained_chksum"] = "1234567890"
        feature["trained_ninput"] = "2"
        feature["trained_noutput"] = "1"
        feature["layerconfig"] = "[2,16,8,1]"

    def continue_training(self, feature: Any) -> None:
        self.calls.append("continue_training")
        self._maybe_fail("continue_training")
        feature["trained_chksum"] = "9876543210"

    def run_test(self, feature: Any) -> None:
        self.calls.append("run_test")
        self._maybe_fail("run_test")
        feature["testloss"] = "0.021"

    def export_onnx(self, feature: Any, path: str) -> None:
        self.calls.append(f"export_onnx:{path}")
        self._maybe_fail("export_onnx")

    def discard_data(self, feature: Any) -> None:
        self.calls.append("discard_data")
        self._maybe_fail("discard_data")


def test_apply_creates_nodes_and_reads_back_defaults() -> None:
    backend = FakeBackend()
    result = apply_surrogate_configuration(backend, _configuration())
    assert result["success"] is True
    assert result["created_nodes"] == [
        ("study", STUDY_TAG),
        ("study_step", STUDY_STEP_TAG),
        ("function", DNN_FUNCTION_TAG),
    ]
    assert backend.studies[STUDY_TAG]["features"][STUDY_STEP_TAG]["type"] == (
        "SurrogateModelTraining"
    )
    assert result["readback"]["useseed"]["value"] == "manual"
    assert result["readback"]["rndseed"]["value"] == 17.0
    assert result["allowed_values"]["validation"] == [
        "random",
        "fraction",
        "last",
        "table",
    ]
    assert result["rolled_back"] is False


def test_apply_rolls_back_every_created_node_on_failure() -> None:
    backend = FakeBackend(fail_on="write:optmethod")
    result = apply_surrogate_configuration(backend, _configuration())
    assert result["success"] is False
    assert "injected failure at write:optmethod" in result["error"]
    assert result["rolled_back"] is True
    assert result["rollback_errors"] == []
    # Nothing may survive a failed batch.
    assert backend.studies == {}
    assert backend.functions == {}


def test_apply_rolls_back_when_study_step_creation_fails() -> None:
    backend = FakeBackend(fail_on="create_study_step")
    result = apply_surrogate_configuration(backend, _configuration())
    assert result["success"] is False
    assert result["rolled_back"] is True
    assert backend.studies == {}
    assert backend.functions == {}


def test_apply_refuses_existing_tags_without_mutation() -> None:
    backend = FakeBackend()
    backend.studies[STUDY_TAG] = {"features": {}}
    result = apply_surrogate_configuration(backend, _configuration())
    assert result["success"] is False
    assert "already exists" in result["error"]
    assert result["created_nodes"] == []
    assert backend.functions == {}


def test_apply_refuses_existing_function_tag() -> None:
    backend = FakeBackend()
    backend.functions[DNN_FUNCTION_TAG] = {}
    result = apply_surrogate_configuration(backend, _configuration())
    assert result["success"] is False
    assert "already exists" in result["error"]
    assert backend.studies == {}


def test_apply_rejects_tampered_configuration_before_mutation() -> None:
    """Content changed without re-sealing the hash must be refused."""
    backend = FakeBackend()
    config = _configuration()
    tampered = {k: v for k, v in config.items() if k != "configuration_sha256"}
    tampered["training"] = {**config["training"], "learning_rate": 0.9}
    tampered["configuration_sha256"] = config["configuration_sha256"]  # stale seal
    with pytest.raises(ValueError, match="configuration_sha256 mismatch"):
        apply_surrogate_configuration(backend, tampered)
    assert backend.calls == []
    assert backend.studies == {}


def test_resealed_different_configuration_is_a_distinct_valid_input() -> None:
    """A correctly re-sealed edit is a different configuration, not tampering."""
    backend = FakeBackend()
    config = _configuration()
    body = {k: v for k, v in config.items() if k != "configuration_sha256"}
    body["training"] = {**config["training"], "learning_rate": 0.9}
    resealed = {**body, "configuration_sha256": canonical_sha256_v1(body)}
    result = apply_surrogate_configuration(backend, resealed)
    assert result["success"] is True
    assert result["configuration_sha256"] == resealed["configuration_sha256"]
    assert result["configuration_sha256"] != config["configuration_sha256"]


def test_apply_records_step_side_binding_to_the_dnn_function() -> None:
    backend = FakeBackend()
    apply_surrogate_configuration(backend, _configuration())
    step = backend.studies[STUDY_TAG]["features"][STUDY_STEP_TAG]
    assert step["surrogatemodel"] == "dnn"
    assert step["globaldnnfunction"] == {"1": DNN_FUNCTION_TAG}


def test_apply_without_node_creation_writes_onto_existing_function() -> None:
    """The deferred-write path must reuse an existing DNN function."""
    backend = FakeBackend()
    apply_surrogate_configuration(backend, _configuration())
    result = apply_surrogate_configuration(
        backend, _configuration(), create_nodes=False, data_source_bound=True
    )
    assert result["success"] is True
    assert result["created_nodes"] == []
    assert result["deferred_writes"] == []
    # The existing function now carries the resolved args mapping.
    assert backend.functions[DNN_FUNCTION_TAG]["args"] == {"w": "w", "h": "h", "R": "R"}


def test_apply_without_node_creation_requires_an_existing_function() -> None:
    backend = FakeBackend()
    result = apply_surrogate_configuration(
        backend, _configuration(), create_nodes=False, data_source_bound=True
    )
    assert result["success"] is False
    assert "must already exist" in result["error"]
    assert backend.studies == {}


def test_bind_data_source_resolves_args_by_name_when_header_is_preserved() -> None:
    from comsol_mcp.surrogate.dnn_adapter import bind_data_source_and_arguments

    backend = FakeBackend()
    apply_surrogate_configuration(backend, _configuration())
    result = bind_data_source_and_arguments(
        backend, _configuration(), dataset_path="D:/tmp/data.csv"
    )
    assert result["success"] is True
    assert result["column_keys"] == ["w", "h", "R"]
    assert result["binding_mode"] == "by_name"
    assert result["bound_arguments"] == ["w", "h", "R"]
    assert backend.functions[DNN_FUNCTION_TAG]["args"] == {"w": "w", "h": "h", "R": "R"}


def test_bind_data_source_binds_positionally_when_comsol_renames_columns() -> None:
    """COMSOL may derive generic col keys; binding is then positional."""
    from comsol_mcp.surrogate.dnn_adapter import bind_data_source_and_arguments

    class GenericColumnBackend(FakeBackend):
        def bind_data_source(self, feature: Any, path: str) -> dict[str, Any]:
            return {"import_error": None, "column_keys": ["col1", "col2", "col3"]}

    backend = GenericColumnBackend()
    apply_surrogate_configuration(backend, _configuration())
    result = bind_data_source_and_arguments(
        backend, _configuration(), dataset_path="D:/tmp/data.csv"
    )
    assert result["success"] is True
    assert result["binding_mode"] == "by_position"
    assert result["args_mapping"] == {"col1": "w", "col2": "h", "col3": "R"}


def test_bind_data_source_reports_a_column_count_mismatch() -> None:
    from comsol_mcp.surrogate.dnn_adapter import bind_data_source_and_arguments

    class ShortBackend(FakeBackend):
        def bind_data_source(self, feature: Any, path: str) -> dict[str, Any]:
            return {"import_error": None, "column_keys": ["col1", "col2"]}

    backend = ShortBackend()
    apply_surrogate_configuration(backend, _configuration())
    result = bind_data_source_and_arguments(
        backend, _configuration(), dataset_path="D:/tmp/data.csv"
    )
    assert result["success"] is False
    assert result["blockers"] == ["column_count_mismatch"]
    assert result["declared_columns"] == ["w", "h", "R"]


def test_bind_data_source_reports_import_failure_and_no_columns() -> None:
    from comsol_mcp.surrogate.dnn_adapter import bind_data_source_and_arguments

    class ImportFailBackend(FakeBackend):
        def bind_data_source(self, feature: Any, path: str) -> dict[str, Any]:
            return {"import_error": "RuntimeError: boom", "column_keys": []}

    backend = ImportFailBackend()
    apply_surrogate_configuration(backend, _configuration())
    failed = bind_data_source_and_arguments(
        backend, _configuration(), dataset_path="D:/tmp/data.csv"
    )
    assert failed["success"] is False
    assert failed["blockers"] == ["data_import_failed"]

    class NoColumnsBackend(FakeBackend):
        def bind_data_source(self, feature: Any, path: str) -> dict[str, Any]:
            return {"import_error": None, "column_keys": []}

    empty = NoColumnsBackend()
    apply_surrogate_configuration(empty, _configuration())
    result = bind_data_source_and_arguments(empty, _configuration(), dataset_path="D:/tmp/data.csv")
    assert result["success"] is False
    assert result["blockers"] == ["data_columns_unavailable"]


def test_bind_data_source_requires_an_existing_dnn_function() -> None:
    from comsol_mcp.surrogate.dnn_adapter import bind_data_source_and_arguments

    backend = FakeBackend()
    result = bind_data_source_and_arguments(
        backend, _configuration(), dataset_path="D:/tmp/data.csv"
    )
    assert result["success"] is False
    assert result["blockers"] == ["dnn_function_absent"]


# --------------------------------------------------------------------------
# Training lifecycle
# --------------------------------------------------------------------------


def test_train_reports_real_losses_and_a_trained_identity() -> None:
    from comsol_mcp.surrogate.dnn_adapter import train_surrogate

    backend = FakeBackend()
    backend.functions["dnn1"] = {}
    result = train_surrogate(backend, run_test=True)
    assert result["success"] is True
    assert result["trained"] is True
    assert result["continued"] is False
    assert result["test_executed"] is True
    # Both the raw readback record and a plain float are reported.
    assert result["trainingloss"] == {"readable": True, "value": "0.025"}
    assert result["trainingloss_value"] == pytest.approx(0.025)
    assert result["validationloss_value"] == pytest.approx(0.027)
    assert result["testloss_value"] == pytest.approx(0.021)
    assert result["trained_chksum"]["value"] == "1234567890"
    assert result["layerconfig"]["value"] == "[2,16,8,1]"
    assert result["trained_ninput"]["value"] == "2"


def test_train_without_test_does_not_evaluate_the_held_out_split() -> None:
    from comsol_mcp.surrogate.dnn_adapter import train_surrogate

    backend = FakeBackend()
    backend.functions["dnn1"] = {}
    result = train_surrogate(backend, run_test=False)
    assert result["test_executed"] is False
    assert "testloss" not in result
    assert "run_test" not in backend.calls


def test_continue_training_uses_the_continuation_entry_point() -> None:
    from comsol_mcp.surrogate.dnn_adapter import train_surrogate

    backend = FakeBackend()
    backend.functions["dnn1"] = {"trained_chksum": "111"}
    result = train_surrogate(backend, continue_training=True, run_test=False)
    assert result["continued"] is True
    assert result["prior_trained_chksum"]["value"] == "111"
    assert result["trained_chksum"]["value"] == "9876543210"
    assert "continue_training" in backend.calls
    assert "train" not in backend.calls


def test_train_requires_an_existing_dnn_function() -> None:
    from comsol_mcp.surrogate.dnn_adapter import train_surrogate

    result = train_surrogate(FakeBackend(), run_test=True)
    assert result["success"] is False
    assert result["trained"] is False
    assert "error" in result


def test_train_failure_is_reported_without_a_fabricated_checksum() -> None:
    from comsol_mcp.surrogate.dnn_adapter import train_surrogate

    backend = FakeBackend(fail_on="train")
    backend.functions["dnn1"] = {}
    result = train_surrogate(backend, run_test=True)
    assert result["success"] is False
    assert result["trained"] is False
    assert "injected failure" in result["error"]
    assert "trained_chksum" not in result


def test_unreadable_loss_is_none_not_zero() -> None:
    """An unreadable loss must never be silently reported as a perfect zero."""
    from comsol_mcp.surrogate.dnn_adapter import _numeric_value

    assert _numeric_value({"readable": False, "reason": "not_readable"}) is None
    assert _numeric_value({"readable": True, "value": "not_a_number"}) is None
    assert _numeric_value({"readable": True, "value": "0"}) == 0.0
    assert _numeric_value(None) is None


def test_test_failure_marks_the_result_unsuccessful() -> None:
    from comsol_mcp.surrogate.dnn_adapter import train_surrogate

    backend = FakeBackend(fail_on="run_test")
    backend.functions["dnn1"] = {}
    result = train_surrogate(backend, run_test=True)
    assert result["success"] is False
    assert result["test_executed"] is False
    assert "injected failure" in result["test_error"]
    # Training itself still succeeded and its identity is preserved.
    assert result["trained"] is True


def test_continuation_identity_helper_refuses_a_changed_build() -> None:
    from comsol_mcp.surrogate.dnn_adapter import assert_continuation_identity_matches
    from comsol_mcp.surrogate.training import build_continuation_identity

    def _identity(build: str) -> dict:
        return build_continuation_identity(
            dataset_manifest_sha256="a" * 64,
            split_manifest_sha256="b" * 64,
            field_schema_sha256="c" * 64,
            transforms_sha256="d" * 64,
            architecture_sha256="e" * 64,
            comsol_build=build,
            objective="minimize_mse",
            prior_checkpoint_sha256="f" * 64,
        )

    assert assert_continuation_identity_matches(_identity("6.4"), _identity("6.4"))[
        "continuation_allowed"
    ]
    with pytest.raises(ValueError, match="nonidentical_continuation"):
        assert_continuation_identity_matches(_identity("6.4"), _identity("6.5"))


# --------------------------------------------------------------------------
# Module boundary
# --------------------------------------------------------------------------


def test_dnn_adapter_module_is_solver_free() -> None:
    """The contract/plan module must stay importable without jpype or COMSOL."""
    import comsol_mcp.surrogate.dnn_adapter as module

    source = open(module.__file__, encoding="utf-8").read().lower()
    for banned in SOLVER_FREE_BANNED:
        assert banned not in source, f"dnn_adapter references {banned}"


def test_clientapi_backend_owns_no_java_typing_of_its_own() -> None:
    """The licensed bridge must not reach Java directly any more.

    S4A step 6 moved every Java typing rule -- jpype wrapping, accessor probing,
    node resolution -- behind ``comsol_mcp.adapter``.  A jpype reference
    reappearing here, or an attribute read of ``model.java``, would mean the seam
    was bypassed again, so both are asserted structurally (by AST) rather than by
    substring search, which a docstring could satisfy by accident.
    """
    import ast

    import comsol_mcp.surrogate.dnn_clientapi_backend as module

    source = open(module.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    java_attribute_reads = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "java"
    ]
    assert java_attribute_reads == [], "the licensed bridge must not read model.java"
    jpype_imports = [
        node.lineno
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Import)
            and any(alias.name.split(".")[0] == "jpype" for alias in node.names)
        )
        or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "jpype")
    ]
    assert jpype_imports == [], "Java wrapping belongs to the adapter, not the bridge"
    # The bridge must actually use the seam rather than merely avoid Java.
    assert "from comsol_mcp.adapter import" in source
    # The solver-free module must not re-export the licensed class.
    import comsol_mcp.surrogate.dnn_adapter as adapter

    assert "ClientapiSurrogateDnnBackend" not in dir(adapter)


def _adapter_backed_bridge() -> tuple[Any, Any]:
    """Return a bridge whose adapter is the deterministic fake, with a model open."""
    from comsol_mcp.adapter.fake_backend import FakeComsolBackend
    from comsol_mcp.adapter.protocol import SessionRequest
    from comsol_mcp.surrogate.dnn_clientapi_backend import ClientapiSurrogateDnnBackend

    adapter = FakeComsolBackend()
    adapter.open_session(SessionRequest(cores=1))
    adapter.load_model("derived.mph")
    adapter.set_container("study", [])
    adapter.set_container("function", [])
    return ClientapiSurrogateDnnBackend(adapter=adapter), adapter


def test_clientapi_backend_routes_every_java_operation_through_the_adapter() -> None:
    """Each bridge operation must land on the adapter, not on a Java object.

    The fake records typed writes, feature calls, and exports separately from the
    operation log, so this proves the migration is real: a bridge that still
    touched Java would leave those records empty.
    """
    from comsol_mcp.adapter import OPERATIONS

    bridge, adapter = _adapter_backed_bridge()

    assert bridge.create_study("std1").path == ("study", "std1")
    assert bridge.create_dnn_function(DNN_FUNCTION_TAG).path == ("function", DNN_FUNCTION_TAG)
    assert bridge.create_study_step("std1", "step1", "StudyStep").path == (
        "study",
        "std1",
        "step1",
    )
    assert bridge.study_tags() == ["std1"]
    assert bridge.func_tags() == [DNN_FUNCTION_TAG]

    dnn = bridge.get_dnn_function(DNN_FUNCTION_TAG)
    assert dnn.path == ("function", DNN_FUNCTION_TAG)

    bridge.write_scalar(dnn, "loss", "mse", "string")
    bridge.write_entries(dnn, "args", {"w": "1", "h": "2"})
    assert adapter.java_writes == [
        (DNN_FUNCTION_TAG, "loss", "string", "mse"),
        (DNN_FUNCTION_TAG, "args", "string_entry", {"w": "1", "h": "2"}),
    ]

    # ``globaldnnfunction`` accepts the nested form when the property declares it
    # and the alternating flat form otherwise; the bridge measures the declared
    # type through the adapter instead of assuming one.
    adapter.set_java_type(DNN_FUNCTION_TAG, "globaldnnfunction", "[[Ljava.lang.String;")
    bridge.write_string_map(dnn, "globaldnnfunction", {"1": DNN_FUNCTION_TAG})
    adapter._java_types.pop((DNN_FUNCTION_TAG, "globaldnnfunction"))
    bridge.write_string_map(dnn, "globaldnnfunction", {"1": DNN_FUNCTION_TAG})
    assert [row[2:] for row in adapter.java_writes[2:]] == [
        ("string_matrix", [["1"], [DNN_FUNCTION_TAG]]),
        ("string_array", ["1", DNN_FUNCTION_TAG]),
    ]

    bridge.train(dnn)
    bridge.continue_training(dnn)
    bridge.run_test(dnn)
    bridge.discard_data(dnn)
    bridge.export_onnx(dnn, "model.onnx")
    assert adapter.feature_calls == [
        (DNN_FUNCTION_TAG, "run"),
        (DNN_FUNCTION_TAG, "continueRun"),
        (DNN_FUNCTION_TAG, "runTest"),
        (DNN_FUNCTION_TAG, "discardData"),
    ]
    assert adapter.exports == [(DNN_FUNCTION_TAG, "model.onnx")]

    adapter.set_java_value(DNN_FUNCTION_TAG, "trainingloss", "getString", "0.025")
    adapter.set_java_value(DNN_FUNCTION_TAG, "activation", "getStringArray", ["tanh", "relu"])
    assert bridge.read_property(dnn, "trainingloss") == {
        "readable": True,
        "accessor": "getString",
        "value": "0.025",
    }
    assert bridge.read_property(dnn, "activation") == {
        "readable": True,
        "accessor": "getStringArray",
        "value": ["tanh", "relu"],
    }
    unreadable = bridge.read_property(dnn, "absent")
    assert unreadable == {
        "readable": False,
        "reason": "no typed accessor accepted the property",
        "rejected_accessors": ["getString: JException", "getDouble: JException"],
    }
    adapter.set_allowed_values(DNN_FUNCTION_TAG, "loss", ["mse", "mae"])
    assert bridge.read_allowed_values(dnn, "loss") == ["mse", "mae"]
    assert bridge.read_allowed_values(dnn, "absent") is None

    bridge.remove_function(DNN_FUNCTION_TAG)
    bridge.remove_study("std1")
    assert bridge.study_tags() == []
    assert bridge.func_tags() == []

    for operation, _ in adapter.calls:
        assert operation in OPERATIONS, f"{operation} is not a declared operation"


def test_clientapi_backend_accepts_the_raw_feature_handle_the_gates_hold() -> None:
    """The licensed gates resolve features themselves and pass the Java object.

    Such a handle cannot be re-walked by path, so the bridge presents it as an
    already-resolved node.  It must still be addressed by tag and must still be
    routed through the adapter.
    """

    class RawFeature:
        """Stands in for the JPype feature object a licensed gate holds."""

    bridge, adapter = _adapter_backed_bridge()
    raw = RawFeature()
    bridge.write_scalar(raw, "loss", "mse", "string")
    bridge.train(raw)
    bridge.export_onnx(raw, "raw.onnx")
    assert adapter.java_writes == [("RawFeature", "loss", "string", "mse")]
    assert adapter.feature_calls == [("RawFeature", "run")]
    assert adapter.exports == [("RawFeature", "raw.onnx")]


def test_clientapi_backend_attaches_an_existing_model_without_owning_it() -> None:
    """MPh forbids a second client, so the bridge adopts the caller's model.

    Adoption must not transfer ownership: closing the adapter has to drop the
    reference without clearing a client the caller created.
    """

    class Client:
        def __init__(self) -> None:
            self.cleared = False

        def clear(self) -> None:
            self.cleared = True

    class Model:
        def __init__(self) -> None:
            self.client = Client()

    from comsol_mcp.adapter.fake_backend import FakeComsolBackend
    from comsol_mcp.surrogate.dnn_clientapi_backend import ClientapiSurrogateDnnBackend

    adapter = FakeComsolBackend()
    model = Model()
    bridge = ClientapiSurrogateDnnBackend(model, adapter=adapter)
    assert bridge.model is model
    assert adapter.attached is True
    adapter.close_session()
    assert model.client.cleared is False


def test_clientapi_backend_requires_a_model_or_an_adapter() -> None:
    from comsol_mcp.adapter import AdapterError
    from comsol_mcp.surrogate.dnn_clientapi_backend import ClientapiSurrogateDnnBackend

    with pytest.raises(AdapterError) as excinfo:
        ClientapiSurrogateDnnBackend()
    assert excinfo.value.reason_code == "adapter_unavailable"
