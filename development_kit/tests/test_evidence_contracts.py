"""evidence contracts gates for versioned physical-evidence and policy contracts."""

from __future__ import annotations

import hashlib
import json
import math
import os
from copy import deepcopy
from pathlib import Path

import pytest
from src.evidence import contracts as contracts_module
from src.evidence.contracts import (
    PHYSICAL_EVIDENCE_SCHEMA_NAME,
    PHYSICAL_EVIDENCE_SCHEMA_VERSION,
    VALIDATION_POLICY_SCHEMA_NAME,
    VALIDATION_POLICY_SCHEMA_VERSION,
    build_physical_evidence,
    build_validation_policy,
    canonical_json_bytes,
    canonical_sha256,
    evaluate_physical_evidence_policy,
    example_validation_policies,
    migrate_legacy_point_audit,
    migrate_legacy_point_audit_file,
    read_physical_evidence,
    validate_physical_evidence,
    validate_validation_policy,
)

from comsol_mcp.durable import io as durable_io

SOURCE_HASH = "a" * 64
CONFIG_HASH = "b" * 64


def _envelope(*, polarization_state: str = "measured"):
    polarization = {"state": polarization_state}
    if polarization_state in {"measured", "derived_from_declared_convention", "label_only"}:
        polarization["value"] = 25.0 if polarization_state == "measured" else "S"
    return build_physical_evidence(
        {
            "schema_name": PHYSICAL_EVIDENCE_SCHEMA_NAME,
            "schema_version": PHYSICAL_EVIDENCE_SCHEMA_VERSION,
            "artifact_type": "unit_fixture",
            "producer": {"tool": "unit_fixture", "tool_schema_version": "1"},
            "identity": {
                "config_id": "unit-config",
                "config_sha256": CONFIG_HASH,
                "source_sha256": SOURCE_HASH,
            },
            "model": {
                "component_tag": "comp1",
                "physics_tag": "ewfd",
                "study_tag": "std1",
                "study_step_tag": "wl_step",
                "mesh_tag": "mesh1",
                "mesh_element_count": 1200,
                "mesh_vertex_count": 600,
            },
            "evidence": {
                "power.R": {
                    "state": "measured",
                    "value": 0.2,
                    "unit": "1",
                    "expression": "ewfd.Rtotal",
                },
                "power.T": {
                    "state": "measured",
                    "value": 0.1,
                    "unit": "1",
                    "expression": "ewfd.Ttotal",
                },
                "power.A": {
                    "state": "measured",
                    "value": 0.7,
                    "unit": "1",
                    "expression": "ewfd.Atotal",
                },
                "wavelength.evaluated_parameter_m": {
                    "state": "measured",
                    "value": 4.37e-6,
                    "unit": "m",
                },
                "wavelength.solved_frequency_m": {
                    "state": "measured",
                    "value": 4.37e-6,
                    "unit": "m",
                },
                "polarization.target_to_transverse_ratio": polarization,
                "polarization.reference_air_method_valid": (
                    {"state": "measured", "value": True}
                    if polarization_state == "measured"
                    else {"state": polarization_state}
                ),
                "mesh.element_count": {"state": "measured", "value": 1200, "unit": "1"},
            },
            "limitations": [],
        }
    )


def _policy(rule_type: str, *, tolerances: dict, assumptions: dict | None = None):
    examples = example_validation_policies()
    example_rule = examples[rule_type]["rules"][0]
    return build_validation_policy(
        {
            "schema_name": VALIDATION_POLICY_SCHEMA_NAME,
            "schema_version": VALIDATION_POLICY_SCHEMA_VERSION,
            "policy_id": f"unit.{rule_type}",
            "rules": [
                {
                    **{
                        key: value
                        for key, value in example_rule.items()
                        if key not in {"tolerances", "assumptions"}
                    },
                    "tolerances": tolerances,
                    "assumptions": example_rule["assumptions"]
                    if assumptions is None
                    else assumptions,
                }
            ],
        }
    )


def _reverse_mapping_order(value):
    if isinstance(value, dict):
        return {key: _reverse_mapping_order(item) for key, item in reversed(tuple(value.items()))}
    if isinstance(value, list):
        return [_reverse_mapping_order(item) for item in value]
    return value


def test_same_evidence_and_policy_serialize_byte_stably_across_mapping_orders():
    first_evidence = _envelope()
    first_policy = _policy(
        "passive_rta_bounds",
        tolerances={"margin": 0.0},
        assumptions={"passive": True, "power_normalized": True},
    )
    evidence_payload = deepcopy(first_evidence)
    evidence_payload.pop("contract_sha256")
    policy_payload = deepcopy(first_policy)
    policy_payload.pop("policy_sha256")
    second_evidence = build_physical_evidence(_reverse_mapping_order(evidence_payload))
    second_policy = build_validation_policy(_reverse_mapping_order(policy_payload))

    assert canonical_json_bytes(first_evidence) == canonical_json_bytes(second_evidence)
    assert canonical_json_bytes(first_policy) == canonical_json_bytes(second_policy)
    assert first_evidence["contract_sha256"] == second_evidence["contract_sha256"]
    assert first_policy["policy_sha256"] == second_policy["policy_sha256"]


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda value: value.update({"unexpected": True}), "unknown fields"),
        (lambda value: value["evidence"]["power.R"].update({"value": math.nan}), "non-finite"),
        (
            lambda value: value["evidence"]["power.R"].update({"state": "maybe"}),
            "state must be one",
        ),
        (
            lambda value: value["evidence"].update({"x" * 200: {"state": "unknown"}}),
            "invalid evidence key",
        ),
    ],
)
def test_physical_evidence_rejects_malformed_ambiguous_and_nonfinite(mutation, match):
    payload = _envelope()
    mutation(payload)
    with pytest.raises(ValueError, match=match):
        validate_physical_evidence(payload, verify_hash=False)


def test_physical_evidence_rejects_stale_contract_hash():
    payload = _envelope()
    payload["model"]["mesh_element_count"] += 1

    with pytest.raises(ValueError, match="contract_sha256 does not match"):
        validate_physical_evidence(payload)


def test_measured_evidence_rejects_null_value_at_envelope_boundary():
    payload = _envelope()
    payload["evidence"]["power.R"]["value"] = None
    with pytest.raises(ValueError, match="non-null value"):
        validate_physical_evidence(payload, verify_hash=False)


def test_contract_size_is_bounded():
    payload = _envelope()
    payload["limitations"] = ["x" * 5000]
    with pytest.raises(ValueError, match="exceeds"):
        validate_physical_evidence(payload, verify_hash=False)


def test_policy_rejects_unknown_nonfinite_and_hash_mismatch():
    policy = _policy(
        "passive_rta_bounds",
        tolerances={"margin": 0.0},
        assumptions={"passive": True, "power_normalized": True},
    )
    unknown = deepcopy(policy)
    unknown["rules"][0]["tolerances"]["other"] = 1.0
    with pytest.raises(ValueError, match="unknown fields"):
        validate_validation_policy(unknown, verify_hash=False)

    nonfinite = deepcopy(policy)
    nonfinite["rules"][0]["tolerances"]["margin"] = float("inf")
    with pytest.raises(ValueError, match="finite and non-negative"):
        validate_validation_policy(nonfinite, verify_hash=False)

    mismatch = deepcopy(policy)
    mismatch["policy_id"] = "unit.changed"
    with pytest.raises(ValueError, match="does not match"):
        validate_validation_policy(mismatch)


@pytest.mark.parametrize("value", [True, "0", 10**400])
def test_policy_rejects_coercive_or_overflowing_numeric_tolerances(value):
    policy = _policy(
        "passive_rta_bounds",
        tolerances={"margin": 0.0},
        assumptions={"passive": True, "power_normalized": True},
    )
    policy["rules"][0]["tolerances"]["margin"] = value

    with pytest.raises(ValueError, match="numeric|finite"):
        validate_validation_policy(policy, verify_hash=False)


def test_policy_assumptions_require_exact_json_booleans():
    policy = _policy(
        "passive_rta_bounds",
        tolerances={"margin": 0.0},
        assumptions={"passive": True, "power_normalized": True},
    )
    policy["rules"][0]["assumptions"] = {"passive": 1, "power_normalized": 1}

    with pytest.raises(ValueError, match="must be boolean"):
        validate_validation_policy(policy, verify_hash=False)


def test_label_only_or_unknown_required_evidence_cannot_pass_policy():
    policy = _policy("reference_air_polarization_ratio", tolerances={"minimum_ratio": 20.0})

    measured = evaluate_physical_evidence_policy(_envelope(polarization_state="measured"), policy)
    label_only = evaluate_physical_evidence_policy(
        _envelope(polarization_state="label_only"), policy
    )
    unknown = evaluate_physical_evidence_policy(_envelope(polarization_state="unknown"), policy)

    assert measured["overall"] == "pass"
    assert label_only["overall"] == "missing"
    assert unknown["overall"] == "missing"
    assert label_only["rules"][0]["required_measurement_states"] == {
        "polarization.reference_air_method_valid": "label_only",
        "polarization.target_to_transverse_ratio": "label_only",
    }


@pytest.mark.parametrize("value", [True, "0.2", 10**400])
def test_policy_evaluation_rejects_coercive_or_overflowing_measured_numbers(value):
    payload = deepcopy(_envelope())
    payload.pop("contract_sha256")
    payload["evidence"]["power.R"]["value"] = value
    evidence = build_physical_evidence(payload)
    policy = _policy("passive_rta_bounds", tolerances={"margin": 0.0})

    result = evaluate_physical_evidence_policy(evidence, policy)

    assert result["overall"] == "missing"
    assert "invalid shape" in result["rules"][0]["reason"]


def _with_evidence_records(envelope, records):
    payload = deepcopy(envelope)
    payload.pop("contract_sha256")
    payload["evidence"].update(records)
    return build_physical_evidence(payload)


def _declared_flux_evidence(*, reflected=0.4, transmitted=1.2, eligible=True, convention=True):
    incident = 2.0
    r_value = reflected / incident
    t_value = transmitted / incident
    a_value = (incident - reflected - transmitted) / incident
    return _with_evidence_records(
        _envelope(),
        {
            "flux.incident_raw_power_w": {"state": "measured", "value": -incident, "unit": "W"},
            "flux.reflected_raw_power_w": {"state": "measured", "value": reflected, "unit": "W"},
            "flux.transmitted_raw_power_w": {
                "state": "measured",
                "value": -transmitted,
                "unit": "W",
            },
            "flux.incident_positive_power_sign": {
                "state": "derived_from_declared_convention",
                "value": -1,
            },
            "flux.reflected_positive_power_sign": {
                "state": "derived_from_declared_convention",
                "value": 1,
            },
            "flux.transmitted_positive_power_sign": {
                "state": "derived_from_declared_convention",
                "value": -1,
            },
            "flux.incident_power_w": {
                "state": "derived_from_declared_convention",
                "value": incident,
                "unit": "W",
            },
            "flux.reflected_power_w": {
                "state": "derived_from_declared_convention",
                "value": reflected,
                "unit": "W",
            },
            "flux.transmitted_power_w": {
                "state": "derived_from_declared_convention",
                "value": transmitted,
                "unit": "W",
            },
            "flux.R": {"state": "derived_from_declared_convention", "value": r_value, "unit": "1"},
            "flux.T": {"state": "derived_from_declared_convention", "value": t_value, "unit": "1"},
            "flux.A": {"state": "derived_from_declared_convention", "value": a_value, "unit": "1"},
            "flux.closure_abs": {
                "state": "derived_from_declared_convention",
                "value": 0.0,
                "unit": "1",
            },
            "flux.convention_complete": {
                "state": "derived_from_declared_convention",
                "value": convention,
            },
            "flux.physical_flux_closure_eligible": {
                "state": "derived_from_declared_convention",
                "value": eligible,
            },
        },
    )


def test_declared_flux_policy_requires_passive_bounds_and_exact_arithmetic():
    policy = _policy(
        "declared_flux_closure",
        tolerances={"closure_abs": 1e-9, "margin": 0.0},
    )

    valid = evaluate_physical_evidence_policy(_declared_flux_evidence(), policy)
    invalid_outgoing_sign = deepcopy(_declared_flux_evidence())
    invalid_outgoing_sign.pop("contract_sha256")
    invalid_outgoing_sign["evidence"]["flux.reflected_positive_power_sign"]["value"] = 0
    invalid_outgoing_sign = evaluate_physical_evidence_policy(
        build_physical_evidence(invalid_outgoing_sign), policy
    )
    absorption_above_one = evaluate_physical_evidence_policy(
        _declared_flux_evidence(reflected=-0.4, transmitted=-0.2),
        policy,
    )

    assert valid["overall"] == "pass"
    assert invalid_outgoing_sign["overall"] == "fail"
    assert absorption_above_one["overall"] == "fail"
    sign_checks = invalid_outgoing_sign["rules"][0]["checks"]
    assert sign_checks["passive_bounds"] is True
    assert sign_checks["signs_valid"] is False
    assert sign_checks["arithmetic_consistent"] is False

    negative_closure = deepcopy(_declared_flux_evidence())
    negative_closure.pop("contract_sha256")
    negative_closure["evidence"]["flux.closure_abs"]["value"] = -1.0
    inconsistent_closure = deepcopy(_declared_flux_evidence())
    inconsistent_closure.pop("contract_sha256")
    inconsistent_closure["evidence"]["flux.closure_abs"]["value"] = 1.0e-12

    negative_result = evaluate_physical_evidence_policy(
        build_physical_evidence(negative_closure), policy
    )
    inconsistent_result = evaluate_physical_evidence_policy(
        build_physical_evidence(inconsistent_closure), policy
    )

    assert negative_result["overall"] == "fail"
    assert inconsistent_result["overall"] == "fail"


def test_internal_normalization_cannot_substitute_for_physical_flux_closure():
    policy = _policy(
        "declared_flux_closure",
        tolerances={"closure_abs": 1e-9, "margin": 0.0},
    )

    ineligible = evaluate_physical_evidence_policy(
        _declared_flux_evidence(eligible=False),
        policy,
    )
    missing_convention = evaluate_physical_evidence_policy(
        _declared_flux_evidence(convention=False),
        policy,
    )

    assert ineligible["overall"] == "fail"
    assert missing_convention["overall"] == "fail"
    assert ineligible["rules"][0]["checks"]["physical_flux_closure_eligible"] is False

    derived_raw_payload = deepcopy(_declared_flux_evidence())
    derived_raw_payload.pop("contract_sha256")
    derived_raw_payload["evidence"]["flux.incident_raw_power_w"]["state"] = (
        "derived_from_declared_convention"
    )
    derived_raw = build_physical_evidence(derived_raw_payload)
    result = evaluate_physical_evidence_policy(derived_raw, policy)
    assert result["overall"] == "missing"
    assert result["rules"][0]["required_measurement_states"]["flux.incident_raw_power_w"] == (
        "derived_from_declared_convention"
    )


def test_reference_air_ratio_requires_valid_reference_method_marker():
    policy = _policy(
        "reference_air_polarization_ratio",
        tolerances={"minimum_ratio": 20.0},
    )
    valid = evaluate_physical_evidence_policy(_envelope(), policy)
    invalid_method = _with_evidence_records(
        _envelope(),
        {"polarization.reference_air_method_valid": {"state": "measured", "value": False}},
    )

    assert valid["overall"] == "pass"
    assert evaluate_physical_evidence_policy(invalid_method, policy)["overall"] == "fail"


def test_all_five_portable_examples_are_strict_and_hashed():
    examples = example_validation_policies()

    assert set(examples) == {
        "passive_rta_bounds",
        "wavelength_synchronization",
        "declared_flux_closure",
        "reference_air_polarization_ratio",
        "mesh_evidence_presence",
    }
    for policy in examples.values():
        assert validate_validation_policy(policy) == policy


def test_legacy_reader_preserves_labels_and_does_not_invent_flux_evidence():
    legacy = {
        "schema_version": "1",
        "config_id": "legacy-config",
        "config_sha256": CONFIG_HASH,
        "source_sha256": SOURCE_HASH,
        "measurement": {
            "schema_version": "1",
            "config_id": "legacy-config",
            "provenance": {
                "config_sha256": CONFIG_HASH,
                "source_sha256_before": SOURCE_HASH,
                "component_tag": "comp1",
                "physics_tag": "ewfd",
                "study_tag": "std1",
                "study_step_tag": "wl_step",
            },
            "wavelength": {
                "requested_m": 4.37e-6,
                "evaluated_parameter_m": 4.37e-6,
                "solved_frequency_wavelength_m": 4.37e-6,
                "absolute_difference_m": 0.0,
                "relative_difference": 0.0,
            },
            "power": {
                "R": 0.2,
                "T": 0.1,
                "A": 0.7,
                "closure_abs": 0.0,
                "expressions": {"R": "ewfd.Rtotal", "T": "ewfd.Ttotal", "A": "ewfd.Atotal"},
                "provenance": {"flux_directions": {}, "A_definition": "1-R-T"},
            },
            "polarization": {"evidence_level": "structure_total_field"},
            "mesh": {"mesh_tag": "mesh1", "element_count": 1200, "vertex_count": 600},
            "integrity": {"source_unchanged": True},
        },
    }

    migrated = migrate_legacy_point_audit(legacy)
    read_back = read_physical_evidence(legacy)

    assert migrated == read_back
    assert migrated["schema_version"] == "1.1.0"
    assert migrated["migration"]["semantics"] == "preserved_without_reinterpretation"
    assert migrated["migration"]["source_hash_basis"] == "canonical_json"
    assert migrated["migration"]["source_artifact_sha256"] == canonical_sha256(legacy)
    assert migrated["migration"]["output_mode"] == "new_artifact"
    assert migrated["evidence"]["polarization.physical_incident"]["state"] == "label_only"
    assert migrated["evidence"]["flux.closure_abs"]["state"] == "not_requested"


def test_legacy_declared_flux_state_cannot_substitute_for_nested_measurements():
    legacy = {
        "schema_version": "1",
        "config_id": "legacy-config",
        "config_sha256": CONFIG_HASH,
        "source_sha256": SOURCE_HASH,
        "measurement": {
            "schema_version": "1",
            "config_id": "legacy-config",
            "provenance": {
                "config_sha256": CONFIG_HASH,
                "source_sha256_before": SOURCE_HASH,
            },
            "wavelength": {},
            "power": {},
            "polarization": {},
            "mesh": {},
            "declared_plane_flux": {
                "state": "derived_from_declared_convention",
                "planes": {"incident": None},
            },
        },
    }

    migrated = migrate_legacy_point_audit(legacy)

    for name in (
        "incident_raw_power_w",
        "reflected_raw_power_w",
        "transmitted_raw_power_w",
        "R",
        "T",
        "A",
        "closure_abs",
        "convention_complete",
        "physical_flux_closure_eligible",
    ):
        assert migrated["evidence"][f"flux.{name}"]["state"] == "unknown"


@pytest.mark.parametrize("section", [None, 1, []])
def test_legacy_measured_internal_absorption_requires_object_sections(section):
    legacy = {
        "schema_version": "1",
        "config_id": "legacy-config",
        "config_sha256": CONFIG_HASH,
        "source_sha256": SOURCE_HASH,
        "measurement": {
            "schema_version": "1",
            "config_id": "legacy-config",
            "provenance": {"config_sha256": CONFIG_HASH, "source_sha256_before": SOURCE_HASH},
            "wavelength": {},
            "power": {},
            "polarization": {},
            "mesh": {},
            "internal_absorption_consistency": {
                "state": "measured",
                "cross_section": section,
                "volume_loss": {},
            },
        },
    }
    with pytest.raises(ValueError, match="cross_section must be a JSON object"):
        migrate_legacy_point_audit(legacy)


def test_file_migration_writes_new_hash_bound_artifact_without_touching_source(
    tmp_path, monkeypatch
):
    legacy = {
        "schema_version": "1",
        "config_id": "legacy-config",
        "config_sha256": CONFIG_HASH,
        "source_sha256": SOURCE_HASH,
        "measurement": {
            "schema_version": "1",
            "config_id": "legacy-config",
            "provenance": {"config_sha256": CONFIG_HASH, "source_sha256_before": SOURCE_HASH},
            "wavelength": {},
            "power": {},
            "polarization": {},
            "mesh": {},
        },
    }
    source = tmp_path / "legacy.json"
    output = tmp_path / "physical_evidence.json"
    source_bytes = json.dumps(legacy, indent=2).encode("utf-8")
    source.write_bytes(source_bytes)
    source_stat = source.stat()
    source_open_flags = []
    real_open = durable_io.os.open

    def tracked_open(path, flags, *args, **kwargs):
        if Path(path).resolve() == source.resolve():
            source_open_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(durable_io.os, "open", tracked_open)

    migrated = migrate_legacy_point_audit_file(source, output)

    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    assert source_open_flags
    assert all(flags & write_flags == 0 for flags in source_open_flags)
    assert source.read_bytes() == source_bytes
    assert source.stat().st_mtime_ns == source_stat.st_mtime_ns
    assert migrated["migration"]["source_hash_basis"] == "file_bytes"
    assert (
        migrated["migration"]["source_artifact_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    )
    assert json.loads(output.read_text(encoding="utf-8")) == migrated
    with pytest.raises(FileExistsError):
        migrate_legacy_point_audit_file(source, output)
    with pytest.raises(ValueError, match="distinct"):
        migrate_legacy_point_audit_file(source, source)


def test_file_migration_publishes_nothing_when_source_changes_before_commit(tmp_path, monkeypatch):
    source = tmp_path / "legacy.json"
    output = tmp_path / "physical_evidence.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "config_id": "legacy-config",
                "config_sha256": CONFIG_HASH,
                "source_sha256": SOURCE_HASH,
                "measurement": {
                    "schema_version": "1",
                    "config_id": "legacy-config",
                    "provenance": {
                        "config_sha256": CONFIG_HASH,
                        "source_sha256_before": SOURCE_HASH,
                    },
                    "wavelength": {},
                    "power": {},
                    "polarization": {},
                    "mesh": {},
                },
            }
        ),
        encoding="utf-8",
    )
    real_read = contracts_module.read_file_bytes_bounded
    calls = 0

    def changing_read(path, *, max_bytes):
        nonlocal calls
        calls += 1
        payload = real_read(path, max_bytes=max_bytes)
        return payload if calls == 1 else payload + b" "

    monkeypatch.setattr(
        contracts_module,
        "read_file_bytes_bounded",
        changing_read,
    )

    with pytest.raises(RuntimeError, match="changed during migration"):
        migrate_legacy_point_audit_file(source, output)

    assert not output.exists()


def test_physical_evidence_reader_preserves_supported_previous_schema():
    previous = _envelope()
    previous["schema_version"] = "1.0.0"
    previous.pop("contract_sha256")
    previous = build_physical_evidence(previous)

    assert validate_physical_evidence(previous) == previous


def test_migration_transformation_identity_fails_closed_on_tampering():
    legacy = {
        "schema_version": "1",
        "config_id": "legacy-config",
        "config_sha256": CONFIG_HASH,
        "source_sha256": SOURCE_HASH,
        "measurement": {
            "schema_version": "1",
            "config_id": "legacy-config",
            "provenance": {"config_sha256": CONFIG_HASH, "source_sha256_before": SOURCE_HASH},
            "wavelength": {},
            "power": {},
            "polarization": {},
            "mesh": {},
        },
    }
    migrated = migrate_legacy_point_audit(legacy)
    migrated["migration"]["transformation_version"] = "9.9.9"
    migrated["contract_sha256"] = canonical_sha256(
        {key: value for key, value in migrated.items() if key != "contract_sha256"}
    )

    with pytest.raises(ValueError, match="transformation hash"):
        validate_physical_evidence(migrated)


def test_builders_reject_caller_supplied_self_hashes():
    envelope = _envelope()
    with pytest.raises(ValueError, match="callers must omit"):
        build_physical_evidence(envelope)

    policy = example_validation_policies()["mesh_evidence_presence"]
    with pytest.raises(ValueError, match="callers must omit"):
        build_validation_policy(policy)
