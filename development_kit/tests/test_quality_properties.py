"""Reproducible property and safety-branch tests for foundation contracts."""

from __future__ import annotations

import json
import math
import tempfile
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, assume, example, given, seed, settings
from hypothesis import strategies as st

import comsol_mcp.compatibility as compatibility
import comsol_mcp.durable.canonical as canonical
import comsol_mcp.durable.io as durable_io
from comsol_mcp.contracts.job_submission import job_submission_dict
from comsol_mcp.contracts.structural import (
    MAX_PUBLIC_COLLECTION_ITEMS,
    MAX_PUBLIC_NESTING_DEPTH,
    MAX_PUBLIC_NUMBER_MAGNITUDE,
    MAX_PUBLIC_OBJECT_FIELDS,
    MAX_PUBLIC_STRING_LENGTH,
    bounded_public_schema,
    structurally_guarded,
    validate_public_structure,
)
from comsol_mcp.durable import (
    canonical_json_v1,
    canonical_sha256_v1,
    domain_sha256_v2,
    json_document_bytes,
    read_complete_jsonl,
)
from comsol_mcp.evidence.material_expressions import preview_material_expression
from comsol_mcp.jobs.spectral_stages import inclusive_wavelength_grid
from comsol_mcp.path_policy import PathPolicy
from comsol_mcp.schema_registry import check_schema_support
from comsol_mcp.tools.catalog import (
    TOOL_SPECS,
    registrars_for_profile,
    validate_tool_specs,
)
from comsol_mcp.tools.session_status import get_session_status, set_session_status

_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=24,
)
_JSON_ATOMS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    _TEXT,
)
_JSON_VALUES = st.recursive(
    _JSON_ATOMS,
    lambda children: st.one_of(
        st.lists(children, max_size=6),
        st.dictionaries(_TEXT, children, max_size=6),
    ),
    max_leaves=24,
)
_ASCII_SEGMENTS = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=16,
)


@seed(20260718)
@settings(max_examples=100, deadline=None, print_blob=True)
@example(value={})
@given(value=st.dictionaries(_TEXT, _JSON_VALUES, max_size=12))
def test_canonical_json_is_order_invariant_and_hash_stable(value: dict) -> None:
    reversed_value = dict(reversed(tuple(value.items())))

    assert canonical_json_v1(value) == canonical_json_v1(reversed_value)
    assert canonical_sha256_v1(value) == canonical_sha256_v1(reversed_value)


@seed(20260719)
@settings(max_examples=100, deadline=None, print_blob=True)
@given(
    value=_JSON_VALUES,
    first=st.from_regex(r"[a-z][a-z0-9_.-]{0,31}", fullmatch=True),
    second=st.from_regex(r"[a-z][a-z0-9_.-]{0,31}", fullmatch=True),
)
def test_domain_separated_identities_do_not_alias(
    value: object,
    first: str,
    second: str,
) -> None:
    assume(first != second)

    assert domain_sha256_v2(first, value) != domain_sha256_v2(second, value)


@pytest.mark.parametrize(
    "value",
    [None, True, 1, 1.5, "text", [1], (1,), {"key": 1}],
)
def test_finite_json_accepts_every_supported_shape(value: object) -> None:
    canonical.validate_finite_json(value)


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), {1: "invalid"}, object()],
)
def test_finite_json_rejects_unsupported_values(value: object) -> None:
    with pytest.raises(ValueError):
        canonical.validate_finite_json(value)


def test_finite_json_limits_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(canonical, "MAX_CANONICAL_NODES", 0)
    with pytest.raises(ValueError, match="node limit"):
        canonical.validate_finite_json(None)

    monkeypatch.setattr(canonical, "MAX_CANONICAL_NODES", 100)
    monkeypatch.setattr(canonical, "MAX_CANONICAL_DEPTH", 0)
    with pytest.raises(ValueError, match="nesting limit"):
        canonical.validate_finite_json([None])


@pytest.mark.parametrize("domain", [None, "", "x" * 129, "thermal.µm"])
def test_domain_identity_rejects_invalid_names(domain: object) -> None:
    with pytest.raises(ValueError, match="identity domain"):
        domain_sha256_v2(domain, {})  # type: ignore[arg-type]


@seed(20260720)
@settings(max_examples=100, deadline=None, print_blob=True)
@given(value=_JSON_VALUES)
def test_public_structure_accepts_bounded_json(value: object) -> None:
    validate_public_structure(value)


def test_public_schema_adds_limits_without_overwriting_explicit_policy() -> None:
    source = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "limited_text": {"type": "string", "maxLength": 7},
            "items": {"type": "array", "items": {"type": "integer"}},
            "closed": {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
            "number": {"type": "number"},
            "bounded": {"type": "integer", "minimum": 0, "maximum": 9},
            "union": {"oneOf": [{"type": "string"}, None]},
        },
    }

    result = bounded_public_schema(source)

    assert "additionalProperties" not in source
    assert result["additionalProperties"] is False
    assert result["maxProperties"] == MAX_PUBLIC_OBJECT_FIELDS
    assert result["properties"]["text"]["maxLength"] == MAX_PUBLIC_STRING_LENGTH
    assert result["properties"]["limited_text"]["maxLength"] == 7
    assert result["properties"]["items"]["maxItems"] == MAX_PUBLIC_COLLECTION_ITEMS
    assert result["properties"]["closed"]["additionalProperties"] is True
    assert result["properties"]["number"]["minimum"] == (-MAX_PUBLIC_NUMBER_MAGNITUDE)
    assert result["properties"]["bounded"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 9,
    }


def _overdeep_value() -> list[object]:
    value: list[object] = []
    for _ in range(MAX_PUBLIC_NESTING_DEPTH + 2):
        value = [value]
    return value


@pytest.mark.parametrize(
    "value",
    [
        "x" * (MAX_PUBLIC_STRING_LENGTH + 1),
        float("nan"),
        float("inf"),
        MAX_PUBLIC_NUMBER_MAGNITUDE * 1.01,
        [None] * (MAX_PUBLIC_COLLECTION_ITEMS + 1),
        {str(index): None for index in range(MAX_PUBLIC_OBJECT_FIELDS + 1)},
        {1: None},
        _overdeep_value(),
        {"unsupported": object()},
    ],
)
def test_public_structure_rejects_every_unbounded_shape(value: object) -> None:
    with pytest.raises(ValueError):
        validate_public_structure(value)


def test_structural_guard_executes_after_successful_validation() -> None:
    @structurally_guarded
    def operation(value: str, *, enabled: bool) -> tuple[str, bool]:
        return value, enabled

    assert operation("bounded", enabled=True) == ("bounded", True)


@seed(20260721)
@settings(max_examples=50, deadline=None, print_blob=True)
@given(segment=_ASCII_SEGMENTS)
def test_owned_paths_are_absolute_ascii_and_contained(segment: str) -> None:
    base = (
        Path("D:/comsol_runtime/property_paths")
        if Path("D:/").exists()
        else Path(tempfile.gettempdir()) / "comsol_mcp_property_paths"
    )
    root = base / "owned"
    root.mkdir(parents=True, exist_ok=True)
    policy = PathPolicy((), root)
    inside = root / segment / "artifact.json"
    outside = base / "outside" / segment / "artifact.json"

    decision = policy.validate_artifact_write(str(inside))

    assert decision.normalized_path == inside.resolve(strict=False)
    assert str(decision.normalized_path).isascii()
    with pytest.raises(ValueError, match="escapes"):
        policy.validate_artifact_write(str(outside))
    with pytest.raises(ValueError, match="absolute"):
        policy.validate_artifact_write(f"{segment}/artifact.json")
    with pytest.raises(ValueError, match="ASCII-only"):
        policy.validate_artifact_write(str(root / "µm.json"))


@seed(20260722)
@settings(
    max_examples=75,
    deadline=None,
    print_blob=True,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@given(
    records=st.lists(st.dictionaries(_TEXT, _JSON_ATOMS, max_size=5), max_size=20),
    incomplete=st.booleans(),
)
def test_jsonl_recovery_preserves_complete_prefixes(
    tmp_path: Path,
    records: list[dict],
    incomplete: bool,
) -> None:
    path = tmp_path / "property-events.jsonl"
    payload = b"".join(
        json.dumps(record, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"
        for record in records
    )
    if incomplete:
        payload += b'{"partial"'
    path.write_bytes(payload)

    result = read_complete_jsonl(path)

    assert result["records"] == records
    assert result["state"] == ("incomplete" if incomplete else "current_valid")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b'{"schema_version":"2"}\n', "current_valid"),
        (b'{"schema_version":"1"}\n', "legacy_valid"),
        (b'{"schema_version":"1"}\n{"schema_version":"2"}\n', "corrupt"),
        (b'{"schema_version":"unknown"}\n', "corrupt"),
        (b"\n", "corrupt"),
        (b"\xff\n", "corrupt"),
    ],
)
def test_versioned_recovery_state_machine(
    tmp_path: Path,
    payload: bytes,
    expected: str,
) -> None:
    path = tmp_path / "versioned-events.jsonl"
    path.write_bytes(payload)

    result = read_complete_jsonl(
        path,
        version_field="schema_version",
        current_version="2",
        legacy_versions=("1",),
    )

    assert result["state"] == expected


def test_jsonl_recovery_rejects_missing_version_policy_and_oversized_files(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bounded-events.jsonl"
    path.write_bytes(b"{}\n")

    with pytest.raises(ValueError, match="current_version"):
        read_complete_jsonl(path, version_field="schema_version")
    assert read_complete_jsonl(path, max_bytes=1)["state"] == "oversized"


@pytest.mark.parametrize("max_bytes", [True, -1, 1.5])
def test_bounded_hash_rejects_invalid_size_limits(
    tmp_path: Path,
    max_bytes: object,
) -> None:
    path = tmp_path / "hash.bin"
    path.write_bytes(b"x")

    with pytest.raises(ValueError, match="max_bytes"):
        durable_io.sha256_file_bounded(path, max_bytes=max_bytes)  # type: ignore[arg-type]


@pytest.mark.parametrize("chunk_bytes", [True, 0, 1.5])
def test_bounded_hash_rejects_invalid_chunks(
    tmp_path: Path,
    chunk_bytes: object,
) -> None:
    path = tmp_path / "hash.bin"
    path.write_bytes(b"x")

    with pytest.raises(ValueError, match="chunk_bytes"):
        durable_io.sha256_file_bounded(
            path,
            max_bytes=1,
            chunk_bytes=chunk_bytes,  # type: ignore[arg-type]
        )


def test_bounded_hash_rejects_directories_and_growth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="regular file"):
        durable_io.sha256_file_bounded(tmp_path, max_bytes=1)

    class GrowingFile:
        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_size=1)

        def is_file(self) -> bool:
            return True

        def open(self, mode: str) -> BytesIO:
            assert mode == "rb"
            return BytesIO(b"ab")

    monkeypatch.setattr(durable_io, "Path", lambda _value: GrowingFile())
    with pytest.raises(ValueError, match="grew"):
        durable_io.sha256_file_bounded("ignored", max_bytes=1)


def test_atomic_write_validates_payload_retries_and_compact_names(
    tmp_path: Path,
) -> None:
    target = tmp_path / "atomic.bin"
    with pytest.raises(ValueError, match="payload"):
        durable_io.atomic_write_bytes(target, "bytes required")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="retry_seconds"):
        durable_io.atomic_write_bytes(target, b"value", retry_seconds=-1)

    attempts = 0

    def replace_after_retry(source: object, destination: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("retry")
        durable_io.os.replace(source, destination)

    durable_io.atomic_write_bytes(
        target,
        b"complete",
        compact_temporary=True,
        replace_fn=replace_after_retry,
    )

    assert target.read_bytes() == b"complete"
    assert attempts == 2


def test_atomic_write_cleans_up_after_replace_deadline(tmp_path: Path) -> None:
    target = tmp_path / "blocked.bin"

    def blocked_replace(_source: object, _destination: object) -> None:
        raise PermissionError("blocked")

    with pytest.raises(PermissionError, match="blocked"):
        durable_io.atomic_write_bytes(
            target,
            b"complete",
            retry_seconds=0,
            replace_fn=blocked_replace,
        )

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_json_document_and_atomic_json_share_finite_serialization(
    tmp_path: Path,
) -> None:
    value = {"bounded": [1, True, None]}
    target = tmp_path / "document.json"

    durable_io.atomic_write_json(target, value)

    assert target.read_bytes() == json_document_bytes(value)


def _drude_preview(wavelength: float, unit: str) -> dict:
    return preview_material_expression(
        model_kind="drude",
        parameters={
            "epsilon_inf": 1.0,
            "plasma_angular_frequency": 1.37e16,
            "damping_angular_frequency": 4.08e13,
        },
        test_wavelengths=[wavelength],
        wavelength_unit=unit,
        harmonic_convention="exp(+i*omega*t)",
        imaginary_sign="positive",
        formulation="volumetric_material",
        parameter_units={
            "epsilon_inf": "1",
            "plasma_angular_frequency": "rad/s",
            "damping_angular_frequency": "rad/s",
        },
    )


@seed(20260723)
@settings(max_examples=75, deadline=None, print_blob=True)
@given(wavelength_m=st.floats(min_value=2e-7, max_value=1e-4, width=64))
def test_wavelength_units_map_to_the_same_meter_coordinate(
    wavelength_m: float,
) -> None:
    previews = [
        _drude_preview(wavelength_m, "m"),
        _drude_preview(wavelength_m * 1e6, "um"),
        _drude_preview(wavelength_m * 1e9, "nm"),
    ]
    points = [result["preview"][0] for result in previews]

    for point in points:
        assert point["wavelength_m"] == pytest.approx(wavelength_m, rel=1e-14)
        assert point["angular_frequency_rad_s"] == pytest.approx(
            2.0 * math.pi * 299_792_458.0 / wavelength_m,
            rel=1e-14,
        )
    assert [point["epsilon"]["real"] for point in points] == pytest.approx(
        [points[0]["epsilon"]["real"]] * 3,
        rel=1e-13,
    )
    assert [point["epsilon"]["imag"] for point in points] == pytest.approx(
        [points[0]["epsilon"]["imag"]] * 3,
        rel=1e-13,
    )


@seed(20260724)
@settings(max_examples=100, deadline=None, print_blob=True)
@given(
    lower_m=st.floats(min_value=1e-9, max_value=1e-4, width=64),
    span_m=st.floats(min_value=1e-10, max_value=1e-4, width=64),
    count=st.integers(min_value=2, max_value=128),
)
def test_inclusive_coordinate_grids_preserve_endpoints_and_order(
    lower_m: float,
    span_m: float,
    count: int,
) -> None:
    upper_m = lower_m + span_m
    assume(math.isfinite(upper_m) and upper_m > lower_m)

    values = inclusive_wavelength_grid(lower_m, upper_m, count)

    assert len(values) == count
    assert values[0] == pytest.approx(lower_m, rel=1e-14)
    assert values[-1] == pytest.approx(upper_m, rel=1e-14)
    assert all(left < right for left, right in zip(values, values[1:]))


def _invalid_tool_specs(case: str) -> object:
    base = TOOL_SPECS["capabilities"]
    if case == "empty":
        return ()
    if case == "duplicate":
        return (base, base)
    if case == "key":
        return {"different": base}
    if case == "registrar":
        changes = {"registrar": "invalid"}
    elif case == "contract":
        changes = {"input_contract": ""}
    elif case == "profiles_empty":
        changes = {"intended_profiles": ()}
    elif case == "profiles_unknown":
        changes = {"intended_profiles": ("unknown", "full")}
    elif case == "full_missing":
        changes = {"intended_profiles": ("core",)}
    elif case == "maturity":
        changes = {"maturity": "unknown"}
    elif case == "read_only_revision":
        changes = {"requires_model_revision": True}
    elif case == "solver_effect":
        changes = {"starts_solver": True}
    elif case == "experimental_profile":
        changes = {"maturity": "experimental"}
    elif case == "advance_without_revision":
        changes = {"advances_model_revision": True}
    elif case == "deprecated_without_replacement":
        changes = {"maturity": "deprecated", "deprecation_state": "deprecated"}
    elif case == "unknown_replacement":
        changes = {"replacement_tool": "not_registered"}
    else:
        raise AssertionError(f"unknown test case: {case}")
    invalid = replace(base, **changes)
    return {**TOOL_SPECS, base.name: invalid}


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "duplicate",
        "key",
        "registrar",
        "contract",
        "profiles_empty",
        "profiles_unknown",
        "full_missing",
        "maturity",
        "read_only_revision",
        "solver_effect",
        "experimental_profile",
        "advance_without_revision",
        "deprecated_without_replacement",
        "unknown_replacement",
    ],
)
def test_tool_spec_safety_decisions_fail_closed(case: str) -> None:
    with pytest.raises(ValueError):
        validate_tool_specs(_invalid_tool_specs(case))  # type: ignore[arg-type]


def test_tool_spec_iterables_and_unknown_profiles_are_explicit() -> None:
    result = validate_tool_specs(tuple(TOOL_SPECS.values()))

    assert result["tool_count"] == len(TOOL_SPECS)
    with pytest.raises(ValueError, match="Invalid profile"):
        registrars_for_profile("unknown")


@pytest.mark.parametrize("value", [None, 1, "src", "src.jobs.worker", "other.module"])
def test_legacy_module_identifiers_have_one_canonical_mapping(value: object) -> None:
    expected = {
        "src": "comsol_mcp",
        "src.jobs.worker": "comsol_mcp.jobs.worker",
    }.get(value, value)

    assert compatibility.canonical_module_identifier(value) == expected
    assert compatibility.module_identity_matches(value, expected)


def _invalid_manifest(case: str) -> object:
    value: object = json.loads(compatibility._MANIFEST_PATH.read_text(encoding="utf-8"))
    if case == "top_type":
        return []
    assert isinstance(value, dict)
    if case == "fields":
        value["extra"] = True
    elif case == "schema_name":
        value["schema_name"] = "unknown"
    elif case == "schema_version":
        value["schema_version"] = "2.0.0"
    elif case == "accepted_type":
        value["licensed_acceptance"] = {}
    elif case == "accepted_count":
        value["licensed_acceptance"] = []
    elif case == "lane_type":
        value["licensed_acceptance"] = [None]
    elif case == "lane_status":
        value["licensed_acceptance"][0]["status"] = "unknown"
    elif case == "lane_field":
        value["licensed_acceptance"][0]["comsol_build"] = ""
    elif case == "dependency_type":
        value["dependency_compatibility"] = []
    elif case == "dependency_status":
        value["dependency_compatibility"]["status"] = "unknown"
    elif case == "dependency_builds":
        value["dependency_compatibility"]["comsol_builds"] = ["unlicensed"]
    elif case == "dependency_claim":
        value["dependency_compatibility"]["establishes_licensed_compatibility"] = True
    elif case == "unknown_type":
        value["unknown_compatibility"] = []
    elif case == "unknown_status":
        value["unknown_compatibility"]["status"] = "accepted"
    elif case == "unknown_claim":
        value["unknown_compatibility"]["requires_independent_licensed_acceptance"] = False
    else:
        raise AssertionError(f"unknown test case: {case}")
    return value


@pytest.mark.parametrize(
    "case",
    [
        "top_type",
        "fields",
        "schema_name",
        "schema_version",
        "accepted_type",
        "accepted_count",
        "lane_type",
        "lane_status",
        "lane_field",
        "dependency_type",
        "dependency_status",
        "dependency_builds",
        "dependency_claim",
        "unknown_type",
        "unknown_status",
        "unknown_claim",
    ],
)
def test_compatibility_manifest_validation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    path = tmp_path / "compatibility.json"
    path.write_text(json.dumps(_invalid_manifest(case)), encoding="utf-8")
    monkeypatch.setattr(compatibility, "_MANIFEST_PATH", path)

    with pytest.raises(ValueError):
        compatibility.load_runtime_compatibility()


def test_compatibility_manifest_and_session_status_have_copy_semantics() -> None:
    assert compatibility.load_runtime_compatibility()["schema_version"] == "1.0.0"
    set_session_status(connected=1, starting=0)  # type: ignore[arg-type]
    snapshot = get_session_status()
    snapshot["connected"] = False

    assert get_session_status() == {"connected": True, "starting": False}
    set_session_status(connected=False, starting=False)


@pytest.mark.parametrize(
    ("schema_name", "schema_version", "reason"),
    [
        (None, "1.0.0", "invalid_schema_name"),
        ("", "1.0.0", "invalid_schema_name"),
        ("comsol_mcp.settings", None, "invalid_schema_version"),
        ("comsol_mcp.settings", "", "invalid_schema_version"),
    ],
)
def test_schema_support_rejects_unbounded_identifiers(
    schema_name: object,
    schema_version: object,
    reason: str,
) -> None:
    result = check_schema_support(schema_name, schema_version)

    assert result == {"supported": False, "reason_code": reason}


def test_job_submission_transport_rejects_non_objects() -> None:
    assert job_submission_dict({"job_type": "test"}) == {"job_type": "test"}
    with pytest.raises(ValueError, match="must be an object"):
        job_submission_dict(None)  # type: ignore[arg-type]
