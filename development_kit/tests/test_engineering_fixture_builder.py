"""Solver-free safety tests for the controlled engineering fixture builder.

The builder is a licensed entry point, but every guarantee it makes about
overwriting, output isolation, release ordering, and cleanup accounting is
solver-free testable. These tests never create a COMSOL client.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from development_kit.scripts import build_engineering_fixture as fixture_builder

ROOT = Path(__file__).parents[2]


class FakeJavaModel:
    """Write a payload the way COMSOL writes a model, without COMSOL."""

    def __init__(self, payload: bytes = b"model-bytes", *, fail: bool = False):
        self.payload = payload
        self.fail = fail
        self.saved_paths: list[str] = []

    def save(self, path: str) -> None:
        self.saved_paths.append(path)
        if self.fail:
            raise RuntimeError("injected save failure")
        Path(path).write_bytes(self.payload)


def test_builder_requires_exactly_one_core():
    assert fixture_builder._require_single_core(1) == 1
    for rejected in (0, 2, 4, 8, -1, True, False):
        with pytest.raises(ValueError, match="exactly 1 core"):
            fixture_builder._require_single_core(rejected)


def test_builder_requires_ascii_absolute_output_root(tmp_path):
    with pytest.raises(ValueError, match="ASCII"):
        fixture_builder._require_ascii_absolute(Path("D:/mcp_tests/\u6a21\u578b"), "output root")
    with pytest.raises(ValueError, match="absolute"):
        fixture_builder._require_ascii_absolute(Path("relative/dir"), "output root")
    assert fixture_builder._require_ascii_absolute(tmp_path, "output root") == tmp_path.resolve()


def test_builder_publishes_the_model_and_removes_its_staging_file(tmp_path):
    destination = tmp_path / "controlled.mph"

    staging = fixture_builder._save_staged(FakeJavaModel(b"payload"), destination)
    assert staging.exists()
    assert not destination.exists(), "staging must not publish on its own"

    published = fixture_builder._publish_staged(staging, destination)

    assert published == destination
    assert destination.read_bytes() == b"payload"
    assert not staging.exists(), "the staging file must not survive publication"
    assert [p.name for p in tmp_path.iterdir()] == ["controlled.mph"]


def test_builder_refuses_to_overwrite_an_existing_model(tmp_path):
    destination = tmp_path / "controlled.mph"
    destination.write_bytes(b"pre-existing research model")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        fixture_builder._save_staged(FakeJavaModel(b"new"), destination)

    # The pre-existing model must be untouched, byte for byte.
    assert destination.read_bytes() == b"pre-existing research model"


def test_builder_publish_refuses_to_clobber_and_leaves_the_target_intact(tmp_path):
    destination = tmp_path / "controlled.mph"
    destination.write_bytes(b"original")
    staging = tmp_path / ".staged"
    staging.write_bytes(b"replacement")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        fixture_builder._publish_staged(staging, destination)

    assert destination.read_bytes() == b"original"


def test_builder_propagates_a_save_failure_without_leaving_a_model(tmp_path):
    destination = tmp_path / "controlled.mph"

    with pytest.raises(RuntimeError, match="injected save failure"):
        fixture_builder._save_staged(FakeJavaModel(fail=True), destination)

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == [], "a failed save must leave nothing behind"


def test_builder_rejects_an_empty_staging_model(tmp_path):
    destination = tmp_path / "controlled.mph"

    with pytest.raises(RuntimeError, match="nonempty staging model"):
        fixture_builder._save_staged(FakeJavaModel(b""), destination)

    assert not destination.exists()


def test_builder_release_records_clear_and_disconnect_failures():
    errors: list[str] = []

    class Client:
        port = 20320

        def clear(self):
            raise OSError("injected clear failure")

        def disconnect(self):
            raise RuntimeError("injected disconnect failure")

    fixture_builder._release_client(Client(), errors)

    assert errors == ["client.clear: OSError", "client.disconnect: RuntimeError"]


def test_builder_release_is_recorded_not_swallowed_when_already_disconnected():
    """After a successful clear the client is disconnected; that is not an error."""
    errors: list[str] = []
    calls: list[str] = []

    class Client:
        port = None

        def clear(self):
            calls.append("clear")

        def disconnect(self):  # pragma: no cover - must not be called
            calls.append("disconnect")

    fixture_builder._release_client(Client(), errors)

    assert calls == ["clear"]
    assert errors == []


def test_builder_sha256_matches_the_published_bytes(tmp_path):
    target = tmp_path / "model.mph"
    payload = b"deterministic-model-bytes"
    target.write_bytes(payload)

    assert fixture_builder._sha256_file(target) == hashlib.sha256(payload).hexdigest()


def test_builder_cli_requires_confirmation_and_a_core_count():
    source = (ROOT / "development_kit" / "scripts" / "build_engineering_fixture.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    parser_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
    ]

    flags = {
        call.args[0].value
        for call in parser_calls
        if call.args and isinstance(call.args[0], ast.Constant)
    }
    assert {"--cores", "--output-root", "--confirm"} <= flags

    # ``--confirm`` must demand one exact literal, so a real COMSOL run cannot
    # start by accident.
    confirm_choices = [
        [element.value for element in keyword.value.elts]
        for call in parser_calls
        for keyword in call.keywords
        if keyword.arg == "choices" and isinstance(keyword.value, ast.List)
    ]
    assert ["BUILD_REAL_COMSOL"] in confirm_choices


def test_builder_never_uses_an_overwriting_publish_call():
    """Publication must not be able to replace an existing model."""
    tree = ast.parse(
        (ROOT / "development_kit" / "scripts" / "build_engineering_fixture.py").read_text(
            encoding="utf-8"
        )
    )

    # Inspect real calls rather than prose: ``os.replace`` must never be invoked.
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "replace" not in called_attributes, "os.replace would silently clobber an existing model"
    assert "link" in called_attributes


def test_builder_declares_its_scope_and_does_not_claim_physical_validity():
    source = (ROOT / "development_kit" / "scripts" / "build_engineering_fixture.py").read_text(
        encoding="utf-8"
    )

    assert "is_real_metal" in source
    assert "no published-structure and no real-metal validation is claimed" in source
    # The layer is a plain lossless dielectric placeholder, not a real metal
    # dispersion model; a Drude/Lorentz permittivity would be a physical claim.
    assert fixture_builder.FIXTURE_RELATIVE_PERMITTIVITY == "2.1"
    assert fixture_builder.PERIOD_X_M == pytest.approx(0.6e-6)
    assert fixture_builder.LAYER_THICKNESS_M == pytest.approx(30e-9)
    assert fixture_builder.AIR_HEIGHT_M == pytest.approx(0.83e-6)


def test_builder_spec_written_by_a_fake_run_is_consumable(tmp_path, monkeypatch):
    """The spec the builder writes must satisfy the fixture contract readers."""
    from comsol_mcp.evidence.real_fixture import (
        controlled_fixture_environment_from_reference_power_spec,
    )

    model = tmp_path / "controlled.mph"
    model.write_bytes(b"model")
    digest = hashlib.sha256(b"model").hexdigest()
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "source_model_path": str(model),
                "expected_source_sha256": digest,
                "wavelength": {"value": fixture_builder.FIXTURE_WAVELENGTH_UM, "unit": "um"},
                "reference_air": {
                    "top_air_domain_ids": [2],
                    "top_air_coordinate_range": {
                        "x": [0.0, fixture_builder.PERIOD_X_M],
                        "y": [0.0, fixture_builder.PERIOD_Y_M],
                        "z": [
                            fixture_builder.LAYER_THICKNESS_M,
                            fixture_builder.LAYER_THICKNESS_M + fixture_builder.AIR_HEIGHT_M,
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    environment = controlled_fixture_environment_from_reference_power_spec(
        spec, base_environment={"PRESERVED": "yes"}
    )

    assert environment["PRESERVED"] == "yes"
    assert environment["COMSOL_REAL_TEST_MODEL"] == str(model)
    assert json.loads(environment["COMSOL_REAL_TEST_TOP_AIR_DOMAIN_IDS"]) == [2]
    assert float(environment["COMSOL_REAL_TEST_WAVELENGTH_UM"]) == pytest.approx(
        fixture_builder.FIXTURE_WAVELENGTH_UM
    )
    assert environment["COMSOL_REAL_TEST_SOURCE_SHA256"] == digest


def test_builder_does_not_import_development_kit_into_runtime():
    """The builder is repository-only and must not be imported by runtime code."""
    runtime_root = ROOT / "comsol_mcp"
    offenders = sorted(
        path.relative_to(ROOT).as_posix()
        for path in runtime_root.rglob("*.py")
        if "build_engineering_fixture" in path.read_text(encoding="utf-8")
    )
    assert offenders == []


def test_builder_ships_outside_the_distribution():
    """``development_kit`` is excluded from the wheel, so the builder cannot ship."""
    assert (ROOT / "development_kit" / "scripts" / "build_engineering_fixture.py").is_file()

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    wheel_excludes = [
        line.strip()
        for line in pyproject.splitlines()
        if line.strip().startswith("exclude") and "development_kit" in line
    ]
    assert wheel_excludes, "the wheel must exclude development_kit"
    assert any("/development_kit" in line for line in wheel_excludes)
