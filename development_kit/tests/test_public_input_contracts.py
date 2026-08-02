"""Public input schema and runtime structural contract tests."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError
from src.contracts import JobSubmissionSpec, structurally_guarded
from src.contracts.job_submission import job_submission_dict, validate_job_submission
from src.contracts.structural import (
    MAX_PUBLIC_COLLECTION_ITEMS,
    MAX_PUBLIC_NESTING_DEPTH,
    MAX_PUBLIC_STRING_LENGTH,
    validate_public_structure,
)
from src.jobs.manager import validate_staged_sweep_spec
from src.server import create_server


def _walk_schema(value, path="root"):
    if isinstance(value, dict):
        yield path, value
        for key, nested in value.items():
            yield from _walk_schema(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _walk_schema(nested, f"{path}[{index}]")


def test_job_submit_discovery_is_a_closed_finite_discriminated_union():
    server = create_server("job-contract-discovery", profile="core")
    schema = server._tool_manager._tools["job_submit"].parameters
    spec = schema["properties"]["spec"]

    assert schema["additionalProperties"] is False
    assert spec["discriminator"]["propertyName"] == "job_type"
    assert set(spec["discriminator"]["mapping"]) == {
        "staged_sweep",
        "validation_matrix",
        "spectral_characterization",
        "convergence_campaign",
        "branch_continuation_campaign",
    }
    assert len(spec["oneOf"]) == 5
    assert all(
        definition["additionalProperties"] is False for definition in schema["$defs"].values()
    )


def test_legacy_job_fields_reach_the_existing_normalizer_byte_identically(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"bounded-fixture")
    raw = {
        "job_type": "staged_sweep",
        "source_model_path": str(source),
        "parameter_name": "p",
        "parameter_values": [1.0, 2.0],
        "expressions": ["es.V"],
        "continue_on_error": False,
    }
    parsed = TypeAdapter(JobSubmissionSpec).validate_python(raw)
    transported = job_submission_dict(parsed)

    assert transported == raw
    assert (
        validate_staged_sweep_spec(transported)["spec_fingerprint"]
        == (validate_staged_sweep_spec(raw)["spec_fingerprint"])
    )


def test_unknown_job_fields_fail_at_the_contract_boundary():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        TypeAdapter(JobSubmissionSpec).validate_python(
            {
                "job_type": "staged_sweep",
                "source_model_path": "fixture.mph",
                "parameter_name": "p",
                "parameter_values": [1.0],
                "expressions": ["es.V"],
                "unknown_field": True,
            }
        )


def test_runtime_job_validator_uses_the_same_discriminated_contract():
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        validate_job_submission({"job_type": "unsupported"})


def test_structural_guard_validates_typed_pydantic_arguments():
    class TypedArgument(BaseModel):
        values: list[float]

    validate_public_structure(TypedArgument(values=[1.0, 2.0]))


def _validation_matrix_submission(nested_value):
    return {
        "job_type": "validation_matrix",
        "source_model_path": "fixture.mph",
        "points": [{"value": nested_value}],
        "point_limit": 1,
        "resource_policy": {},
        "cores": 1,
    }


def _overdeep_job_value():
    value = None
    for _ in range(MAX_PUBLIC_NESTING_DEPTH + 2):
        value = [value]
    return value


@pytest.mark.parametrize(
    "nested_value",
    [
        _overdeep_job_value(),
        [None] * (MAX_PUBLIC_COLLECTION_ITEMS + 1),
        "x" * (MAX_PUBLIC_STRING_LENGTH + 1),
        {1: None},
        ("non-json",),
        object(),
        float("nan"),
        float("inf"),
    ],
    ids=[
        "overdeep",
        "oversized_collection",
        "oversized_string",
        "non_string_key",
        "tuple",
        "arbitrary_object",
        "nan",
        "infinity",
    ],
)
def test_runtime_job_validator_rejects_unbounded_or_non_json_nested_values(nested_value):
    with pytest.raises((ValueError, ValidationError)):
        validate_job_submission(_validation_matrix_submission(nested_value))


def test_every_full_profile_schema_node_has_a_structural_limit():
    server = create_server("bounded-full-discovery", profile="full")
    issues = []
    for tool in asyncio.run(server.list_tools()):
        for path, node in _walk_schema(tool.inputSchema, tool.name):
            node_type = node.get("type")
            if node_type == "string" and "maxLength" not in node:
                issues.append((path, "unbounded_string"))
            elif node_type == "array" and "maxItems" not in node:
                issues.append((path, "unbounded_array"))
            elif node_type == "object" and "additionalProperties" not in node:
                issues.append((path, "open_object_rule_missing"))
            elif node_type in {"integer", "number"} and not (
                ("minimum" in node or "exclusiveMinimum" in node)
                and ("maximum" in node or "exclusiveMaximum" in node)
            ):
                issues.append((path, "unbounded_number"))
    assert issues == []


def test_runtime_structural_rejection_precedes_the_wrapped_operation():
    called = False

    @structurally_guarded
    def operation(value):
        nonlocal called
        called = True
        return value

    with pytest.raises(ValueError, match="public string limit"):
        operation("x" * 16_385)
    assert called is False
