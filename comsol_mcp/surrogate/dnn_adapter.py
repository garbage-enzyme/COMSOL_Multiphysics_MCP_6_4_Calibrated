"""Typed COMSOL DNN surrogate adapter with failure-atomic application.

The solver-free half of this module validates one bounded DNN configuration and
derives an exact property-write plan from the S0-proven COMSOL 6.4 surface.  The
ClientAPI half applies that plan to one already-loaded derived model and reads
every COMSOL-owned default back.

No generic property setter is exposed: every write is an explicit typed entry
derived from a validated configuration, and a partial batch is rolled back.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from comsol_mcp.durable.canonical import canonical_sha256_v1

SCHEMA_VERSION = "1.0.0"

# Allowed values proven by the S0 licensed capability probe
# (D:\mcp_tests\a75s0p06, receipt sha256 9581c9ef32...).
ALLOWED_ACTIVATIONS = frozenset(
    {"none", "relu", "elu", "sigmoid", "tanh", "softplus", "leakyrelu", "gelu"}
)
ALLOWED_OPTIMIZERS = frozenset({"adam", "sgd"})
ALLOWED_LOSSES = frozenset({"mse", "mae"})
ALLOWED_LAYER_TYPES = frozenset({"input", "dense"})
ALLOWED_VALIDATION_MODES = frozenset({"random", "fraction", "last", "table"})
ALLOWED_TEST_MODES = frozenset({"none", "random", "table"})

# Split modes that let COMSOL subset the imported data internally.  The
# authoritative group-disjoint split remains this project's own manifest: COMSOL
# internal subsetting is used only for training-time model selection, and its
# exact membership is not retrievable, so it must never be reported as the
# group-disjoint split.
COMSOL_INTERNAL_SUBSET_MODES = frozenset({"random", "fraction", "last"})
# Split modes that require a bound COMSOL result table.
TABLE_BOUND_MODES = frozenset({"table"})
ALLOWED_SURROGATE_MODELS = frozenset({"none", "gp", "pce", "dnn", "lsq"})
ALLOWED_COMPUTE_ACTIONS = frozenset({"recompute", "append"})
ALLOWED_SEED_MODES = frozenset({"manual", "currenttime"})
ALLOWED_COLUMN_SCALES = frozenset({"to01", "std", "log", "no"})

MAX_HIDDEN_LAYERS = 8
MAX_LAYER_WIDTH = 512
MAX_EPOCHS = 10_000
MAX_BATCH_SIZE = 65_536
MAX_COLUMNS = 32

DNN_FUNCTION_TAG = "dnn1"
STUDY_TAG = "std1"
STUDY_STEP_TAG = "smt1"
SURROGATE_STUDY_STEP_TYPE = "SurrogateModelTraining"
DNN_FUNCTION_TYPE = "DNN"

# COMSOL-owned defaults that S0 read back and that must never stay implicit.
# Values are the exact strings the licensed probe returned, not normalised
# guesses: an empty string means COMSOL reports the property as unset.
COMSOL_OWNED_DEFAULTS = {
    "activation": "none, tanh",
    "layertype": "input, dense",
    "outfeatures": "1, 1",
    "surrogatemodel": "none",
    "optmethod": "adam",
    "loss": "mse",
    "batchsize": "512",
    "epochs": "1000",
    "lr": "1e-3",
    "momentum": "0",
    "weightdecay": "",
    "gputraining": "off",
    "useseed": "manual",
    "rndseed": "0",
    "useseedvalidation": "manual",
    "rndseedvalidation": "0",
    "validation": "random",
    "test": "",
    "fraction": "0.1",
    "source": "file",
    "sourcetype": "",
}


def _require_str(name: str, value: Any, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{name} must be a non-empty string up to {max_len}")
    return value


def _require_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _require_number(name: str, value: Any, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number in {minimum}..{maximum}")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be a number in {minimum}..{maximum}")
    return number


def _require_choice(name: str, value: Any, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return str(value)


def _require_closed(mapping: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(unknown)}")


def build_dnn_configuration(
    *,
    configuration_id: str,
    input_features: Sequence[str],
    output_features: Sequence[str],
    hidden_layers: Sequence[int],
    activation: str,
    optimizer: str,
    loss: str,
    learning_rate: float,
    batch_size: int,
    maximum_epochs: int,
    seed: int,
    validation_mode: str,
    test_mode: str,
    weight_decay: float = 0.0,
    momentum: float = 0.0,
    momentum_used: bool = False,
    column_scales: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate and seal one bounded fully connected DNN configuration.

    Only dense hidden layers are representable, so architecture search and
    custom layers cannot be expressed.
    """
    features = [_require_str("input_features[]", item, max_len=64) for item in input_features]
    targets = [_require_str("output_features[]", item, max_len=64) for item in output_features]
    if not 2 <= len(features) <= 12:
        raise ValueError("input_features must contain 2-12 entries")
    if not 1 <= len(targets) <= 8:
        raise ValueError("output_features must contain 1-8 entries")
    if len(set(features)) != len(features):
        raise ValueError("input_features must be unique")
    if len(set(targets)) != len(targets):
        raise ValueError("output_features must be unique")
    if set(features) & set(targets):
        raise ValueError("input and output feature names must not overlap")

    layers = [
        _require_int("hidden_layers[]", item, minimum=1, maximum=MAX_LAYER_WIDTH)
        for item in hidden_layers
    ]
    if not 1 <= len(layers) <= MAX_HIDDEN_LAYERS:
        raise ValueError(f"hidden_layers must contain 1-{MAX_HIDDEN_LAYERS} entries")

    activation = _require_choice("activation", activation, ALLOWED_ACTIVATIONS)
    optimizer = _require_choice("optimizer", optimizer, ALLOWED_OPTIMIZERS)
    loss = _require_choice("loss", loss, ALLOWED_LOSSES)
    validation_mode = _require_choice("validation_mode", validation_mode, ALLOWED_VALIDATION_MODES)
    test_mode = _require_choice("test_mode", test_mode, ALLOWED_TEST_MODES)
    learning_rate = _require_number("learning_rate", learning_rate, minimum=1e-12, maximum=1.0)
    batch_size = _require_int("batch_size", batch_size, minimum=1, maximum=MAX_BATCH_SIZE)
    maximum_epochs = _require_int("maximum_epochs", maximum_epochs, minimum=1, maximum=MAX_EPOCHS)
    seed = _require_int("seed", seed, minimum=0, maximum=2**31 - 1)
    weight_decay = _require_number("weight_decay", weight_decay, minimum=0.0, maximum=1.0)
    momentum = _require_number("momentum", momentum, minimum=0.0, maximum=1.0)
    if momentum != 0.0 and optimizer != "sgd":
        raise ValueError("momentum is only meaningful when optimizer is sgd")
    if momentum_used and optimizer != "sgd":
        raise ValueError("momentum_used requires optimizer sgd")

    scales = dict(column_scales or {})
    unknown_scales = sorted(set(scales) - set(features) - set(targets))
    if unknown_scales:
        raise ValueError(f"column_scales references unknown columns: {', '.join(unknown_scales)}")
    for column, kind in scales.items():
        _require_choice(f"column_scales.{column}", kind, ALLOWED_COLUMN_SCALES)
    if len(features) + len(targets) > MAX_COLUMNS:
        raise ValueError(f"declared columns exceed the {MAX_COLUMNS} column limit")

    body = {
        "schema": "comsol_mcp.surrogate_dnn_configuration",
        "schema_version": SCHEMA_VERSION,
        "configuration_id": _require_str("configuration_id", configuration_id),
        "architecture": {
            "kind": "fully_connected_dense",
            "input_features": features,
            "output_features": targets,
            "hidden_layers": layers,
            "input_dim": len(features),
            "output_dim": len(targets),
            "layer_configuration": [len(features), *layers, len(targets)],
        },
        "training": {
            "activation": activation,
            "optimizer": optimizer,
            "loss": loss,
            "learning_rate": learning_rate,
            "batch_size": batch_size,
            "maximum_epochs": maximum_epochs,
            "weight_decay": weight_decay,
            "momentum": momentum,
            "momentum_used": bool(momentum_used),
        },
        "determinism": {
            "seed_mode": "manual",
            "seed": seed,
            "validation_seed_mode": "manual",
            "validation_seed": seed,
        },
        "splits": {"validation_mode": validation_mode, "test_mode": test_mode},
        "column_scales": dict(sorted(scales.items())),
        "gpu": {"gputraining": False},
        "custom_layers_allowed": False,
        "architecture_search_allowed": False,
    }
    return {**body, "configuration_sha256": canonical_sha256_v1(body)}


def validate_dnn_configuration(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("DNN configuration must be a mapping")
    required = (
        "schema",
        "schema_version",
        "configuration_id",
        "architecture",
        "training",
        "determinism",
        "splits",
        "column_scales",
        "gpu",
        "custom_layers_allowed",
        "architecture_search_allowed",
    )
    if set(value) != set(required) | {"configuration_sha256"}:
        raise ValueError("DNN configuration keys are closed")
    if value["schema"] != "comsol_mcp.surrogate_dnn_configuration":
        raise ValueError("unexpected schema name")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    body = {key: value[key] for key in required}
    if value["configuration_sha256"] != canonical_sha256_v1(body):
        raise ValueError("configuration_sha256 mismatch")
    if value["custom_layers_allowed"] is not False:
        raise ValueError("custom layers must not be allowed")
    if value["architecture_search_allowed"] is not False:
        raise ValueError("architecture search must not be allowed")
    if value["gpu"]["gputraining"] is not False:
        raise ValueError("GPU training is excluded from the first acceptance")
    rebuilt = build_dnn_configuration(
        configuration_id=value["configuration_id"],
        input_features=value["architecture"]["input_features"],
        output_features=value["architecture"]["output_features"],
        hidden_layers=value["architecture"]["hidden_layers"],
        activation=value["training"]["activation"],
        optimizer=value["training"]["optimizer"],
        loss=value["training"]["loss"],
        learning_rate=value["training"]["learning_rate"],
        batch_size=value["training"]["batch_size"],
        maximum_epochs=value["training"]["maximum_epochs"],
        seed=value["determinism"]["seed"],
        validation_mode=value["splits"]["validation_mode"],
        test_mode=value["splits"]["test_mode"],
        weight_decay=value["training"]["weight_decay"],
        momentum=value["training"]["momentum"],
        momentum_used=value["training"]["momentum_used"],
        column_scales=value["column_scales"],
    )
    if rebuilt != value:
        raise ValueError("DNN configuration content is not canonical")
    return dict(value)


def build_write_plan(
    configuration: Mapping[str, Any], *, data_source_bound: bool = False
) -> dict[str, Any]:
    """Derive the exact typed COMSOL property writes for one configuration.

    The plan is deliberately explicit: the adapter may perform only these
    writes, so no generic property escape hatch exists.

    ``data_source_bound`` records whether a data source (file or result table)
    has already been bound to the DNN function.  The ``args`` property maps
    *data column names* to argument names, so COMSOL refuses it with
    "属性值无效 ... 交替键和唯一变元名称的数组" while no columns exist.  The
    write is therefore deferred rather than approximated until a data source is
    bound, and the deferral is recorded in the plan.
    """
    config = validate_dnn_configuration(configuration)
    architecture = config["architecture"]
    training = config["training"]
    determinism = config["determinism"]
    splits = config["splits"]

    layer_types = ["input", *["dense"] * len(architecture["hidden_layers"]), "dense"]
    for item in layer_types:
        if item not in ALLOWED_LAYER_TYPES:
            raise ValueError("derived layer type is not permitted")

    # `activation` is a per-layer array whose length must match `layertype`
    # (the COMSOL default is {"none", "tanh"} for an input+dense pair).  The
    # input layer never has an activation, so it takes "none"; every dense layer
    # takes the configured activation.  A single-element array raises
    # "数组长度错误" at training time.
    activation_per_layer = [
        "none" if kind == "input" else training["activation"] for kind in layer_types
    ]

    scalar_writes: list[dict[str, Any]] = [
        {
            "property": "activation",
            "kind": "string_array",
            "value": activation_per_layer,
        },
        {"property": "layertype", "kind": "string_array", "value": layer_types},
        {
            "property": "outfeatures",
            "kind": "int_array",
            "value": list(architecture["layer_configuration"]),
        },
        {"property": "optmethod", "kind": "string", "value": training["optimizer"]},
        {"property": "loss", "kind": "string", "value": training["loss"]},
        {"property": "lr", "kind": "double", "value": training["learning_rate"]},
        {"property": "batchsize", "kind": "int", "value": training["batch_size"]},
        {"property": "epochs", "kind": "int", "value": training["maximum_epochs"]},
        {"property": "weightdecay", "kind": "double", "value": training["weight_decay"]},
        {"property": "useseed", "kind": "string", "value": determinism["seed_mode"]},
        {"property": "rndseed", "kind": "double", "value": float(determinism["seed"])},
        {
            "property": "useseedvalidation",
            "kind": "string",
            "value": determinism["validation_seed_mode"],
        },
        {
            "property": "rndseedvalidation",
            "kind": "double",
            "value": float(determinism["validation_seed"]),
        },
        {"property": "validation", "kind": "string", "value": splits["validation_mode"]},
        {"property": "test", "kind": "string", "value": splits["test_mode"]},
        {"property": "gputraining", "kind": "boolean", "value": False},
        {"property": "ignorenaninf", "kind": "boolean", "value": True},
    ]
    if training["optimizer"] == "sgd" and training["momentum_used"]:
        scalar_writes.append(
            {"property": "momentum", "kind": "double", "value": training["momentum"]}
        )

    deferred_writes: list[dict[str, Any]] = []
    entry_writes: list[dict[str, Any]] = []
    if data_source_bound:
        entry_writes.append(
            {
                "property": "args",
                "writer": "setEntry",
                "entries": {
                    **{name: name for name in architecture["input_features"]},
                    **{name: name for name in architecture["output_features"]},
                },
            }
        )
    else:
        deferred_writes.append(
            {
                "property": "args",
                "reason": "data_source_not_bound",
                "detail": (
                    "COMSOL requires the data column names before args can be "
                    "written; bind a file or result table first."
                ),
            }
        )
    if config["column_scales"]:
        if data_source_bound:
            entry_writes.append(
                {
                    "property": "colscale",
                    "writer": "setEntry",
                    "entries": dict(config["column_scales"]),
                }
            )
        else:
            deferred_writes.append(
                {
                    "property": "colscale",
                    "reason": "data_source_not_bound",
                    "detail": "column scaling requires bound data columns.",
                }
            )

    # The study step carries the data-generation side of the lifecycle.  S0
    # proved `globaldnnfunction` is a keyed string map (keys "1", "2", ... for
    # each quantity of interest), not a plain scalar, so it is written through
    # the entry accessor.
    step_scalar_writes: list[dict[str, Any]] = [
        {"property": "surrogatemodel", "kind": "string", "value": "dnn"},
        {
            "property": "activation",
            "kind": "string_array",
            "value": activation_per_layer,
        },
        {"property": "layertype", "kind": "string_array", "value": layer_types},
        {
            "property": "outfeatures",
            "kind": "int_array",
            "value": list(architecture["layer_configuration"]),
        },
    ]
    step_entry_writes: list[dict[str, Any]] = [
        {
            "property": "globaldnnfunction",
            "writer": "stringMap",
            "entries": {"1": DNN_FUNCTION_TAG},
        }
    ]

    body = {
        "schema": "comsol_mcp.surrogate_dnn_write_plan",
        "schema_version": SCHEMA_VERSION,
        "configuration_sha256": config["configuration_sha256"],
        "dnn_function_tag": DNN_FUNCTION_TAG,
        "study_tag": STUDY_TAG,
        "study_step_tag": STUDY_STEP_TAG,
        "scalar_writes": scalar_writes,
        "entry_writes": entry_writes,
        "step_scalar_writes": step_scalar_writes,
        "step_entry_writes": step_entry_writes,
        "deferred_writes": deferred_writes,
        "data_source_bound": bool(data_source_bound),
        "write_count": (
            len(scalar_writes)
            + len(entry_writes)
            + len(step_scalar_writes)
            + len(step_entry_writes)
        ),
    }
    return {**body, "plan_sha256": canonical_sha256_v1(body)}


# --------------------------------------------------------------------------
# Licensed backend
# --------------------------------------------------------------------------


class SurrogateDnnBackend(Protocol):
    """Exact operations the typed adapter needs; no generic property setter.

    Construction is deliberately outside this protocol: a licensed gate builds
    the backend from the ``mph.Client`` and ``Model`` it already owns, and MPh
    1.3.1 exposes no client back-reference, so the caller must supply both.
    """

    def study_tags(self) -> list[str]: ...

    def func_tags(self) -> list[str]: ...

    def create_study(self, tag: str) -> Any: ...

    def create_study_step(self, study_tag: str, step_tag: str, step_type: str) -> Any: ...

    def create_dnn_function(self, tag: str) -> Any: ...

    def get_study_step(self, study_tag: str, step_tag: str) -> Any: ...

    def get_dnn_function(self, tag: str) -> Any: ...

    def remove_study(self, tag: str) -> None: ...

    def remove_function(self, tag: str) -> None: ...

    def write_scalar(self, feature: Any, name: str, value: Any, kind: str) -> None: ...

    def write_entries(self, feature: Any, name: str, entries: Mapping[str, str]) -> None: ...

    def write_string_map(self, feature: Any, name: str, entries: Mapping[str, str]) -> None: ...

    def bind_data_source(self, feature: Any, path: str) -> dict[str, Any]: ...

    def read_property(self, feature: Any, name: str) -> dict[str, Any]: ...

    def read_allowed_values(self, feature: Any, name: str) -> list[str] | None: ...

    def train(self, feature: Any) -> None: ...

    def continue_training(self, feature: Any) -> None: ...

    def run_test(self, feature: Any) -> None: ...

    def export_onnx(self, feature: Any, path: str) -> None: ...

    def discard_data(self, feature: Any) -> None: ...


def apply_surrogate_configuration(
    backend: SurrogateDnnBackend,
    configuration: Mapping[str, Any],
    *,
    create_study_step: bool = True,
    data_source_bound: bool = False,
    create_nodes: bool = True,
) -> dict[str, Any]:
    """Apply one configuration failure-atomically and read every default back.

    Every node this call creates is recorded.  If any step fails, the created
    nodes are removed in reverse order so a partial surrogate tree is never left
    behind, and the failure is reported with the rollback outcome.

    ``create_nodes=False`` writes properties onto nodes that already exist.  That
    path exists because the ``args`` property needs a bound data source, and a
    data source can only be bound to an existing DNN function; it never removes
    a node it did not create.
    """
    plan = build_write_plan(configuration, data_source_bound=data_source_bound)
    created: list[tuple[str, str]] = []
    readback: dict[str, Any] = {}
    try:
        existing_studies = set(backend.study_tags())
        existing_functions = set(backend.func_tags())
        if create_nodes:
            if plan["study_tag"] in existing_studies:
                raise ValueError(f"study tag {plan['study_tag']} already exists")
            if plan["dnn_function_tag"] in existing_functions:
                raise ValueError(f"function tag {plan['dnn_function_tag']} already exists")

            backend.create_study(plan["study_tag"])
            created.append(("study", plan["study_tag"]))
            if create_study_step:
                backend.create_study_step(
                    plan["study_tag"], plan["study_step_tag"], SURROGATE_STUDY_STEP_TYPE
                )
                created.append(("study_step", plan["study_step_tag"]))
            backend.create_dnn_function(plan["dnn_function_tag"])
            created.append(("function", plan["dnn_function_tag"]))
        else:
            if plan["dnn_function_tag"] not in existing_functions:
                raise ValueError(
                    f"function tag {plan['dnn_function_tag']} must already exist "
                    "when create_nodes is false"
                )

        dnn = backend.get_dnn_function(plan["dnn_function_tag"])
        for write in plan["scalar_writes"]:
            try:
                backend.write_scalar(dnn, write["property"], write["value"], write["kind"])
            except Exception as exc:
                raise ValueError(
                    f"write failed for DNN property {write['property']!r} "
                    f"(kind {write['kind']}): {type(exc).__name__}: {exc}"
                ) from exc
        for write in plan["entry_writes"]:
            try:
                if write["writer"] == "stringMap":
                    backend.write_string_map(dnn, write["property"], write["entries"])
                else:
                    backend.write_entries(dnn, write["property"], write["entries"])
            except Exception as exc:
                raise ValueError(
                    f"entry write failed for DNN property {write['property']!r}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

        if create_nodes and create_study_step:
            step = backend.get_study_step(plan["study_tag"], plan["study_step_tag"])
            for write in plan["step_scalar_writes"]:
                try:
                    backend.write_scalar(step, write["property"], write["value"], write["kind"])
                except Exception as exc:
                    raise ValueError(
                        f"write failed for study-step property {write['property']!r} "
                        f"(kind {write['kind']}): {type(exc).__name__}: {exc}"
                    ) from exc
            for write in plan["step_entry_writes"]:
                try:
                    if write["writer"] == "stringMap":
                        backend.write_string_map(step, write["property"], write["entries"])
                    else:
                        backend.write_entries(step, write["property"], write["entries"])
                except Exception as exc:
                    raise ValueError(
                        f"entry write failed for study-step property "
                        f"{write['property']!r}: {type(exc).__name__}: {exc}"
                    ) from exc

        for name in (
            "activation",
            "layertype",
            "outfeatures",
            "optmethod",
            "loss",
            "lr",
            "batchsize",
            "epochs",
            "weightdecay",
            "useseed",
            "rndseed",
            "useseedvalidation",
            "rndseedvalidation",
            "validation",
            "test",
            "gputraining",
            "layerconfig",
            "trained_chksum",
        ):
            readback[name] = backend.read_property(dnn, name)
        allowed: dict[str, Any] = {}
        for name in ("activation", "optmethod", "loss", "validation", "test"):
            allowed[name] = backend.read_allowed_values(dnn, name)
    except Exception as exc:
        rollback_errors: list[str] = []
        for kind, tag in reversed(created):
            try:
                if kind == "function":
                    backend.remove_function(tag)
                else:
                    backend.remove_study(tag)
            except Exception as rollback_exc:  # pragma: no cover - defensive
                rollback_errors.append(f"{kind}:{tag}:{type(rollback_exc).__name__}")
        return {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "rolled_back": not rollback_errors,
            "rollback_errors": rollback_errors,
            "created_nodes": created,
            "readback": {},
        }

    return {
        "success": True,
        "plan_sha256": plan["plan_sha256"],
        "configuration_sha256": plan["configuration_sha256"],
        "created_nodes": created,
        "readback": readback,
        "allowed_values": allowed,
        "deferred_writes": plan["deferred_writes"],
        "data_source_bound": plan["data_source_bound"],
        "rollback_errors": [],
        "rolled_back": False,
    }


def bind_data_source_and_arguments(
    backend: SurrogateDnnBackend,
    configuration: Mapping[str, Any],
    *,
    dataset_path: str,
) -> dict[str, Any]:
    """Bind one data file and write ``args`` from the columns COMSOL observed.

    ``args`` maps data column keys to argument names and cannot be written before
    the columns exist, so this call imports the data first and then builds the
    mapping only from keys COMSOL actually reports.

    COMSOL derives its own column keys from the imported file and does not
    necessarily reuse the file's header text; the observed keys are therefore
    bound positionally to the declared features and targets, and the positional
    binding is returned as evidence rather than being silently assumed.
    """
    config = validate_dnn_configuration(configuration)
    architecture = config["architecture"]
    try:
        dnn = backend.get_dnn_function(DNN_FUNCTION_TAG)
    except Exception as exc:
        return {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "blockers": ["dnn_function_absent"],
        }
    binding = backend.bind_data_source(dnn, dataset_path)
    columns = list(binding.get("column_keys") or [])
    if binding.get("import_error") is not None:
        return {
            "success": False,
            "error": binding["import_error"],
            "blockers": ["data_import_failed"],
            "column_keys": columns,
        }
    if not columns:
        return {
            "success": False,
            "error": "COMSOL did not report any data column keys",
            "blockers": ["data_columns_unavailable"],
            "column_keys": [],
        }

    expected = [
        *architecture["input_features"],
        *architecture["output_features"],
    ]
    if len(columns) != len(expected):
        return {
            "success": False,
            "error": (
                f"data file exposes {len(columns)} columns but the schema declares {len(expected)}"
            ),
            "blockers": ["column_count_mismatch"],
            "column_keys": columns,
            "declared_columns": expected,
        }

    # Prefer a name match when COMSOL preserved the header; otherwise bind
    # positionally, which is the observed COMSOL behaviour.
    if all(name in columns for name in expected):
        mapping = {name: name for name in expected}
        binding_mode = "by_name"
    else:
        mapping = {column: name for column, name in zip(columns, expected)}
        binding_mode = "by_position"

    try:
        backend.write_entries(dnn, "args", mapping)
    except Exception as exc:
        return {
            "success": False,
            "error": f"args write failed: {type(exc).__name__}: {exc}",
            "blockers": ["args_write_failed"],
            "column_keys": columns,
        }
    return {
        "success": True,
        "column_keys": columns,
        "declared_columns": expected,
        "binding_mode": binding_mode,
        "bound_arguments": expected,
        "args_mapping": mapping,
        "args_readback": backend.read_property(dnn, "args"),
    }


def _numeric_value(record: Any) -> float | None:
    """Return the numeric value of an accessor readback record, or None.

    COMSOL readbacks are strings, so the conversion is guarded rather than
    assumed: a non-numeric readback yields ``None`` instead of raising.
    """
    raw = record.get("value") if isinstance(record, Mapping) else record
    if isinstance(raw, str):
        raw = raw.strip()
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    try:
        number = float(raw)
    except TypeError, ValueError:
        return None
    return number if math.isfinite(number) else None


def train_surrogate(
    backend: SurrogateDnnBackend,
    *,
    continue_training: bool = False,
    run_test: bool = False,
) -> dict[str, Any]:
    """Train (or continue) the DNN and optionally evaluate its held-out test loss.

    The lifecycle methods live on the DNN function, not the study step.  The
    returned evidence records the COMSOL-owned trained checksum, the reported
    losses, and the layer configuration, so a model card can bind the exact
    trained artifact rather than a return code.

    Each loss is reported twice: as the raw accessor readback record and as a
    plain float (``*_value``) for numeric consumers.  A loss that COMSOL reports
    as unreadable or non-numeric yields ``None`` rather than a silent zero.
    """
    try:
        dnn = backend.get_dnn_function(DNN_FUNCTION_TAG)
    except Exception as exc:
        # Report the same shape as every other failure so a caller never has to
        # guess whether a missing key means "not trained" or "not attempted".
        return {
            "success": False,
            "trained": False,
            "continued": bool(continue_training),
            "test_executed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    prior_chksum = backend.read_property(dnn, "trained_chksum")
    try:
        if continue_training:
            backend.continue_training(dnn)
        else:
            backend.train(dnn)
    except Exception as exc:
        return {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "prior_trained_chksum": prior_chksum,
            "trained": False,
            "continued": bool(continue_training),
            "test_executed": False,
        }

    trainingloss = backend.read_property(dnn, "trainingloss")
    validationloss = backend.read_property(dnn, "validationloss")
    result: dict[str, Any] = {
        "success": True,
        "continued": bool(continue_training),
        "trained": True,
        "prior_trained_chksum": prior_chksum,
        "trainingloss": trainingloss,
        "trainingloss_value": _numeric_value(trainingloss),
        "validationloss": validationloss,
        "validationloss_value": _numeric_value(validationloss),
        "layerconfig": backend.read_property(dnn, "layerconfig"),
        "trained_chksum": backend.read_property(dnn, "trained_chksum"),
        "trained_ninput": backend.read_property(dnn, "trained_ninput"),
        "trained_noutput": backend.read_property(dnn, "trained_noutput"),
    }
    if run_test:
        try:
            backend.run_test(dnn)
            testloss = backend.read_property(dnn, "testloss")
            result["testloss"] = testloss
            result["testloss_value"] = _numeric_value(testloss)
            result["test_executed"] = True
        except Exception as exc:
            result["test_executed"] = False
            result["test_error"] = f"{type(exc).__name__}: {exc}"
            result["success"] = False
    else:
        result["test_executed"] = False
    return result


def assert_continuation_identity_matches(
    prior: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    """Refuse a continuation whose contract identities differ."""
    from comsol_mcp.surrogate.training import assert_continuation_allowed

    return assert_continuation_allowed(prior=prior, current=current)


__all__ = [
    "ALLOWED_ACTIVATIONS",
    "ALLOWED_COMPUTE_ACTIONS",
    "ALLOWED_COLUMN_SCALES",
    "ALLOWED_LAYER_TYPES",
    "ALLOWED_LOSSES",
    "ALLOWED_OPTIMIZERS",
    "ALLOWED_SEED_MODES",
    "ALLOWED_SURROGATE_MODELS",
    "ALLOWED_TEST_MODES",
    "ALLOWED_VALIDATION_MODES",
    "COMSOL_OWNED_DEFAULTS",
    "DNN_FUNCTION_TAG",
    "DNN_FUNCTION_TYPE",
    "MAX_HIDDEN_LAYERS",
    "SCHEMA_VERSION",
    "STUDY_STEP_TAG",
    "STUDY_TAG",
    "SURROGATE_STUDY_STEP_TYPE",
    "SurrogateDnnBackend",
    "apply_surrogate_configuration",
    "assert_continuation_identity_matches",
    "bind_data_source_and_arguments",
    "build_dnn_configuration",
    "build_write_plan",
    "train_surrogate",
    "validate_dnn_configuration",
]
