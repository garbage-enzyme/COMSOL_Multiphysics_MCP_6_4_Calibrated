"""Public-surface tests for the Model Manager write gate and owner limits.

These tests bind the default-off settings gate to the operation admission layer,
and verify that the capability metadata a client receives is truthful about what
is enabled. Nothing here starts COMSOL: the gate is a settings read plus a pure
decision, and the capability documents are computed from constants and settings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comsol_mcp.settings import (
    MODEL_MANAGER_ENABLED_ENV,
    SHARED_SERVER_ENV,
    load_settings,
)
from comsol_mcp.shared_session.dbmodel_operations import (
    DbmodelOperationError,
    build_request,
)
from comsol_mcp.shared_session.dbmodel_operations import (
    capability_document as dbmodel_capabilities,
)
from comsol_mcp.shared_session.solver_owners import (
    MAX_SOLVER_OWNERS,
)
from comsol_mcp.shared_session.solver_owners import (
    capability_document as owner_capabilities,
)

VALID_URI = "dbmodel://library/models/cell"
DIGEST = "a" * 64


def _write_settings(tmp_path: Path, **overrides: object) -> Path:
    """Write a settings file with only the groups this surface reads."""
    document: dict[str, object] = {
        "schema_name": "comsol_mcp.settings",
        "schema_version": "1.3.0",
        "profile": {"name": "core"},
        "shared_server": {"enabled": False},
        "model_manager": {"enabled": False},
    }
    for key, value in overrides.items():
        document[key] = value
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The gate is default-off and has its own switch
# ---------------------------------------------------------------------------


def test_the_model_manager_write_gate_defaults_to_off() -> None:
    assert load_settings({})["model_manager"]["enabled"] is False


def test_the_gate_has_a_dedicated_environment_switch() -> None:
    """A dedicated switch means sharing a session never implies write access."""
    assert MODEL_MANAGER_ENABLED_ENV == "COMSOL_MCP_ENABLE_MODEL_MANAGER"
    assert MODEL_MANAGER_ENABLED_ENV != SHARED_SERVER_ENV


@pytest.mark.parametrize("value", [True])
def test_the_gate_can_be_enabled_explicitly(tmp_path: Path, value: bool) -> None:
    path = _write_settings(tmp_path, model_manager={"enabled": value})
    from comsol_mcp.settings import SETTINGS_PATH_ENV

    loaded = load_settings({SETTINGS_PATH_ENV: str(path)})
    assert loaded["model_manager"]["enabled"] is True


@pytest.mark.parametrize("value", [False])
def test_the_gate_stays_off_when_disabled(tmp_path: Path, value: bool) -> None:
    path = _write_settings(tmp_path, model_manager={"enabled": value})
    from comsol_mcp.settings import SETTINGS_PATH_ENV

    loaded = load_settings({SETTINGS_PATH_ENV: str(path)})
    assert loaded["model_manager"]["enabled"] is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_a_stringly_typed_gate_value_is_rejected_not_coerced(tmp_path: Path, value: str) -> None:
    """A truthy string must not silently enable a write gate.

    The settings contract requires a real JSON boolean, so a string falls back to
    the default rather than being interpreted. Coercing it would let a quoted
    "false" enable writes.
    """
    path = _write_settings(tmp_path, model_manager={"enabled": value})
    from comsol_mcp.settings import SETTINGS_PATH_ENV

    loaded = load_settings({SETTINGS_PATH_ENV: str(path)})
    assert loaded["model_manager"]["enabled"] is False


def test_enabling_shared_server_does_not_enable_the_write_gate(tmp_path: Path) -> None:
    """The two gates are independent, in both directions."""
    from comsol_mcp.settings import SETTINGS_PATH_ENV

    path = _write_settings(
        tmp_path, shared_server={"enabled": True}, model_manager={"enabled": False}
    )
    loaded = load_settings({SETTINGS_PATH_ENV: str(path)})
    assert loaded["shared_server"]["enabled"] is True
    assert loaded["model_manager"]["enabled"] is False


def test_an_absent_group_from_an_older_file_defaults_to_off(tmp_path: Path) -> None:
    """A settings file that predates the gate must not gain write access."""
    from comsol_mcp.settings import SETTINGS_PATH_ENV

    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "schema_name": "comsol_mcp.settings",
                "schema_version": "1.3.0",
                "profile": {"name": "core"},
                "shared_server": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings({SETTINGS_PATH_ENV: str(path)})
    assert loaded["model_manager"]["enabled"] is False


def test_the_project_settings_file_declares_the_gate_off() -> None:
    root = Path(__file__).resolve().parents[2]
    document = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert document["model_manager"]["enabled"] is False


# ---------------------------------------------------------------------------
# Gate state reaches admission
# ---------------------------------------------------------------------------


def test_a_disabled_gate_refuses_an_upload_through_the_operation_layer() -> None:
    """The settings gate and admission must agree, not merely coexist."""
    gate = load_settings({})["model_manager"]["enabled"]
    assert gate is False
    with pytest.raises(DbmodelOperationError) as excinfo:
        build_request(
            operation="upload",
            uri=VALID_URI,
            derived_source_identity={"revision": "r1", "sha256": DIGEST},
            write_enabled=gate,
        )
    assert excinfo.value.reason_code == "write_gate_disabled"


def test_an_enabled_gate_admits_an_upload() -> None:
    request = build_request(
        operation="upload",
        uri=VALID_URI,
        derived_source_identity={"revision": "r1", "sha256": DIGEST},
        write_enabled=True,
    )
    assert request.operation == "upload"


@pytest.mark.parametrize("operation", ["open", "run", "download"])
def test_read_operations_work_with_the_gate_off(operation: str) -> None:
    """Read access must never depend on the write gate."""
    gate = load_settings({})["model_manager"]["enabled"]
    kwargs: dict[str, object] = {"write_enabled": gate}
    if operation == "download":
        kwargs["destination"] = "D:/downloads/cell.mph"
    request = build_request(operation=operation, uri=VALID_URI, **kwargs)  # type: ignore[arg-type]
    assert request.write_enabled is False


# ---------------------------------------------------------------------------
# Capability metadata is truthful
# ---------------------------------------------------------------------------


def test_the_dbmodel_capability_document_reports_the_disabled_gate() -> None:
    document = dbmodel_capabilities(upload_enabled=False)
    assert document["upload_gate"]["enabled"] is False
    assert document["upload_gate"]["default"] is False
    assert document["upload_gate"]["independent_of_shared_server"] is True
    assert document["overwrite_permitted"] is False


def test_the_dbmodel_capability_document_never_drops_the_overwrite_refusal() -> None:
    """Enabling writes must not advertise an overwrite capability."""
    for enabled in (False, True):
        document = dbmodel_capabilities(upload_enabled=enabled)
        assert document["overwrite_permitted"] is False
        assert document["generic_cloud_api"] is False


def test_the_dbmodel_capability_document_separates_read_from_write() -> None:
    document = dbmodel_capabilities(upload_enabled=False)
    mutating = {
        name for name, entry in document["operations"].items() if entry["mutates_model_manager"]
    }
    assert mutating == {"upload"}
    assert all(
        document["operations"][name]["requires_write_gate"] is False
        for name in ("open", "run", "download")
    )


def test_the_owner_capability_document_reports_the_frozen_ceiling() -> None:
    document = owner_capabilities()
    assert document["maximum_independent_solver_owners"] == MAX_SOLVER_OWNERS == 2
    assert document["cores_per_owner"] == 1
    assert document["multi_core_solver_permitted"] is False
    assert document["third_owner_permitted"] is False
    assert document["cross_owner_model_access_permitted"] is False


def test_the_capability_documents_are_serializable_and_path_free() -> None:
    """Capability metadata must not leak a local path or a credential."""
    for document in (
        dbmodel_capabilities(upload_enabled=False),
        owner_capabilities(),
    ):
        encoded = json.dumps(document, sort_keys=True)
        assert "C:\\" not in encoded
        assert "D:\\" not in encoded
        assert "password" not in encoded.casefold()
        assert "token" not in encoded.casefold()


def test_the_capability_documents_do_not_claim_a_heavy_runtime() -> None:
    """Advertising a capability is not a claim that COMSOL is running."""
    document = owner_capabilities()
    assert document["currently_registered"] == 0
    assert "solver_running" not in json.dumps(dbmodel_capabilities(upload_enabled=False))
