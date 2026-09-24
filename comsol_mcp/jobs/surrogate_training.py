"""Pure normalization for one bounded durable surrogate-training job.

This module never imports COMSOL, Java, MPh, or network clients.  It binds a
surrogate-training submission to exact dataset, split, schema, transform, and
architecture identities so that resume can refuse a changed contract.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from comsol_mcp.build_identity import get_build_identity
from comsol_mcp.compatibility import module_identity_matches

from .resource_admission import normalize_resource_policy
from .store import JOB_SCHEMA_VERSION

SURROGATE_TRAINING_DRIVER_VERSION = "1.0.0"
MAX_SURROGATE_SPEC_BYTES = 4 * 1024 * 1024
MAX_EPOCHS = 10_000
MAX_SEEDS = 8
SURROGATE_STAGES = (
    "preflight",
    "dataset_bound",
    "split_frozen",
    "configuration_readback",
    "training",
    "validation_selected",
    "test_evaluated",
    "finalized",
)
SURROGATE_TERMINAL_STATES = (
    "completed",
    "training_failed",
    "nonfinite_loss",
    "test_failed",
    "cancelled",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_hex64(name: str, value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a 64-character hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a 64-character hex digest") from exc
    return value.lower()


def _require_str(name: str, value: Any, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{name} must be a non-empty string up to {max_len}")
    return value


def _require_positive_int(name: str, value: Any, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer in 1..{maximum}")
    return value


def current_surrogate_training_driver_identity() -> dict[str, str]:
    """Bind resume to the exact implementation and package bytes."""
    build = get_build_identity()
    return {
        "implementation": "comsol_mcp.jobs.surrogate_training_worker",
        "driver_version": SURROGATE_TRAINING_DRIVER_VERSION,
        "package_content_sha256": build["package_content_sha256"],
        "build_identity_sha256": build["build_identity_sha256"],
    }


def validate_surrogate_training_driver_identity(spec: Mapping[str, Any]) -> dict[str, str]:
    observed = spec.get("driver_identity")
    expected = current_surrogate_training_driver_identity()
    if (
        not isinstance(observed, Mapping)
        or set(observed) != set(expected)
        or any(key != "implementation" and observed[key] != expected[key] for key in expected)
        or not module_identity_matches(expected["implementation"], observed.get("implementation"))
    ):
        raise ValueError("surrogate-training driver identity differs from the running package")
    return expected


def normalize_surrogate_training_spec(value: object) -> dict[str, Any]:
    """Normalize and bind one bounded surrogate-training submission.

    The spec is deliberately built from already-validated surrogate contracts so
    that a training job cannot bypass dataset, split, schema, or transform
    validation.
    """
    from comsol_mcp.surrogate.fields import validate_field_schema, validate_fitted_transforms
    from comsol_mcp.surrogate.manifests import validate_dataset_manifest
    from comsol_mcp.surrogate.splits import validate_split_plan

    if not isinstance(value, Mapping):
        raise ValueError("surrogate training spec must be a mapping")
    raw = dict(value)
    if raw.get("job_type") != "surrogate_training":
        raise ValueError("surrogate training spec requires job_type='surrogate_training'")

    allowed = {
        "job_type",
        "campaign_id",
        "dataset_manifest",
        "split_plan",
        "field_schema",
        "transforms",
        "architecture",
        "seeds",
        "maximum_epochs",
        "wall_time_budget_seconds",
        "resource_policy",
        "continue_from",
        "schema_version",
        "spec_fingerprint",
        "driver_identity",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"surrogate training spec has unknown fields: {', '.join(unknown)}")

    campaign_id = _require_str("campaign_id", raw.get("campaign_id"), max_len=128)
    dataset = validate_dataset_manifest(raw.get("dataset_manifest"))
    split_plan = validate_split_plan(raw.get("split_plan"))
    field_schema = validate_field_schema(raw.get("field_schema"))
    transforms = validate_fitted_transforms(raw.get("transforms"))

    # The dataset rows must all appear in the split plan's leakage groups, so a
    # training job can never be bound to a split that does not cover the data.
    split_groups = {item["group_id"] for item in split_plan["assignments"]}
    dataset_groups = {item["group_id"] for item in dataset["leakage_groups"]}
    if dataset_groups != split_groups:
        raise ValueError("dataset leakage groups must match the split plan exactly")

    # Comparing the group *IDs* alone is not enough: the same IDs could be bound
    # to different splits in the two documents, so a group the dataset calls
    # "train" could be "scientific_holdout" in the plan. That is exactly the
    # duplication a leakage check exists to prevent, so the split each group is
    # assigned to must agree as well.
    #
    # The dataset manifest assigns per row, the split plan assigns per group, so
    # each group's dataset-side split is derived from its member rows. A group
    # whose rows disagree with each other is already refused by
    # validate_group_disjoint_split, and is re-checked here because this
    # specification is the durable identity a resumed job depends on.
    plan_group_split = {item["group_id"]: item["split"] for item in split_plan["assignments"]}
    row_split = {item["row_id"]: item["split"] for item in dataset["split_assignments"]}
    for group in dataset["leakage_groups"]:
        group_id = group["group_id"]
        member_splits = {row_split[row_id] for row_id in group["row_ids"] if row_id in row_split}
        if len(member_splits) > 1:
            raise ValueError(
                "dataset leakage groups must match the split plan exactly: "
                f"group {group_id} spans multiple splits in the dataset manifest"
            )
        if not member_splits:
            continue
        dataset_split = member_splits.pop()
        if dataset_split != plan_group_split[group_id]:
            raise ValueError(
                "dataset leakage groups must match the split plan exactly: "
                f"group {group_id} is {dataset_split} in the dataset manifest but "
                f"{plan_group_split[group_id]} in the split plan"
            )

    architecture = raw.get("architecture")
    if not isinstance(architecture, Mapping):
        raise ValueError("architecture must be a mapping")
    arch_allowed = {"layers", "activation", "optimizer", "loss", "batch_size", "learning_rate"}
    arch_unknown = sorted(set(architecture) - arch_allowed)
    if arch_unknown:
        raise ValueError(f"architecture has unknown fields: {', '.join(arch_unknown)}")
    layers = architecture.get("layers")
    if not isinstance(layers, list) or not layers:
        raise ValueError("architecture.layers must be a non-empty list")
    if len(layers) > 8:
        raise ValueError("architecture.layers must contain at most 8 hidden layers")
    normalized_layers = [
        _require_positive_int("architecture.layers[]", item, maximum=512) for item in layers
    ]
    activation = _require_str("architecture.activation", architecture.get("activation"))
    if activation not in {
        "none",
        "relu",
        "elu",
        "sigmoid",
        "tanh",
        "softplus",
        "leakyrelu",
        "gelu",
    }:
        raise ValueError("unsupported activation function")
    optimizer = _require_str("architecture.optimizer", architecture.get("optimizer"))
    if optimizer not in {"adam", "sgd"}:
        raise ValueError("unsupported optimizer")
    loss = _require_str("architecture.loss", architecture.get("loss"))
    if loss not in {"mse", "mae"}:
        raise ValueError("unsupported loss function")
    # Validated for its side effect only: the value itself is carried into the
    # normalized specification by the architecture spread below, so binding it to
    # a local would leave an unused name behind.
    _require_positive_int("architecture.batch_size", architecture.get("batch_size"), maximum=65536)
    learning_rate = architecture.get("learning_rate")
    if isinstance(learning_rate, bool) or not isinstance(learning_rate, (int, float)):
        raise ValueError("architecture.learning_rate must be a positive number")
    if not 0 < float(learning_rate) <= 1.0:
        raise ValueError("architecture.learning_rate must be in (0, 1]")

    epochs = _require_positive_int("maximum_epochs", raw.get("maximum_epochs"), maximum=MAX_EPOCHS)

    seeds = raw.get("seeds")
    if not isinstance(seeds, list) or not seeds:
        raise ValueError("seeds must be a non-empty list")
    if len(seeds) > MAX_SEEDS:
        raise ValueError(f"seeds must contain at most {MAX_SEEDS} entries")
    normalized_seeds: list[int] = []
    for item in seeds:
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item < 2**31:
            raise ValueError("each seed must be a non-negative 31-bit integer")
        normalized_seeds.append(item)
    if len(set(normalized_seeds)) != len(normalized_seeds):
        raise ValueError("seeds must be unique")

    wall_time = raw.get("wall_time_budget_seconds")
    if (
        isinstance(wall_time, bool)
        or not isinstance(wall_time, (int, float))
        or not 1 <= float(wall_time) <= 31_536_000
    ):
        raise ValueError("wall_time_budget_seconds must be in 1..31536000")

    resource_policy = normalize_resource_policy(raw.get("resource_policy"))
    if resource_policy is None:
        raise ValueError("resource_policy must contain explicit rules")
    rules = resource_policy["rules"]
    declared_wall = rules.get("wall_time_budget_seconds")
    if declared_wall is None:
        raise ValueError("resource_policy must declare wall_time_budget_seconds")
    if declared_wall > float(wall_time):
        raise ValueError("resource policy wall time exceeds the surrogate job budget")

    continue_from = raw.get("continue_from")
    if continue_from is not None:
        if not isinstance(continue_from, Mapping):
            raise ValueError("continue_from must be a mapping")
        cf_allowed = {
            "parent_job_id",
            "trained_chksum",
            "dataset_manifest_sha256",
            "split_manifest_sha256",
            "field_schema_sha256",
            "transforms_sha256",
            "architecture_sha256",
        }
        cf_unknown = sorted(set(continue_from) - cf_allowed)
        if cf_unknown:
            raise ValueError(f"continue_from has unknown fields: {', '.join(cf_unknown)}")
        normalized_continue = {
            "parent_job_id": _require_str(
                "continue_from.parent_job_id", continue_from.get("parent_job_id")
            ),
            "trained_chksum": _require_hex64(
                "continue_from.trained_chksum", continue_from.get("trained_chksum")
            ),
            "dataset_manifest_sha256": _require_hex64(
                "continue_from.dataset_manifest_sha256",
                continue_from.get("dataset_manifest_sha256"),
            ),
            "split_manifest_sha256": _require_hex64(
                "continue_from.split_manifest_sha256",
                continue_from.get("split_manifest_sha256"),
            ),
            "field_schema_sha256": _require_hex64(
                "continue_from.field_schema_sha256", continue_from.get("field_schema_sha256")
            ),
            "transforms_sha256": _require_hex64(
                "continue_from.transforms_sha256", continue_from.get("transforms_sha256")
            ),
            "architecture_sha256": _require_hex64(
                "continue_from.architecture_sha256", continue_from.get("architecture_sha256")
            ),
        }
        # Continuation is permitted only across identical contract identities.
        for field, observed in (
            ("dataset_manifest_sha256", dataset["manifest_sha256"]),
            ("split_manifest_sha256", split_plan["manifest_sha256"]),
            ("field_schema_sha256", field_schema["manifest_sha256"]),
            ("transforms_sha256", transforms["manifest_sha256"]),
            ("architecture_sha256", _fingerprint(architecture)),
        ):
            if normalized_continue[field] != observed:
                raise ValueError(
                    f"continue_from.{field} does not match the current contract identity"
                )
    else:
        normalized_continue = None

    architecture_sha256 = _fingerprint(architecture)
    normalized: dict[str, Any] = {
        "job_type": "surrogate_training",
        "campaign_id": campaign_id,
        "dataset_manifest": dataset,
        "dataset_manifest_sha256": dataset["manifest_sha256"],
        "split_plan": split_plan,
        "split_manifest_sha256": split_plan["manifest_sha256"],
        "field_schema": field_schema,
        "field_schema_sha256": field_schema["manifest_sha256"],
        "transforms": transforms,
        "transforms_sha256": transforms["manifest_sha256"],
        "architecture": {**architecture, "layers": normalized_layers},
        "architecture_sha256": architecture_sha256,
        "seeds": normalized_seeds,
        "maximum_epochs": epochs,
        "wall_time_budget_seconds": float(wall_time),
        "resource_policy": resource_policy,
        "continue_from": normalized_continue,
        "declared_stages": list(SURROGATE_STAGES),
        "declared_stage_count": len(SURROGATE_STAGES),
        "schema_version": JOB_SCHEMA_VERSION,
        "driver_identity": current_surrogate_training_driver_identity(),
    }
    normalized["spec_fingerprint"] = _fingerprint(normalized)
    if len(_canonical_bytes(normalized)) > MAX_SURROGATE_SPEC_BYTES:
        raise ValueError("surrogate training specification exceeds its bound")
    return normalized


def surrogate_spec_path_is_ascii(value: str) -> bool:
    """Return whether a declared surrogate spec path is ASCII-safe."""
    return str(Path(value)).isascii()


__all__ = [
    "MAX_EPOCHS",
    "MAX_SEEDS",
    "MAX_SURROGATE_SPEC_BYTES",
    "SURROGATE_STAGES",
    "SURROGATE_TERMINAL_STATES",
    "SURROGATE_TRAINING_DRIVER_VERSION",
    "current_surrogate_training_driver_identity",
    "normalize_surrogate_training_spec",
    "surrogate_spec_path_is_ascii",
    "validate_surrogate_training_driver_identity",
]
