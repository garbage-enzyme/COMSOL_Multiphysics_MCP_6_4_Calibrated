"""Solver-free contracts for immutable durable attached execution targets."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess
import sys

import pytest

from src.jobs.attached_backend import normalize_attached_execution_backend
from src.shared_session.identity import normalize_attached_server_identity
from src.shared_session.locking import (
    build_shared_model_revision,
    normalize_shared_model_identity,
)


def _backend() -> dict:
    server = normalize_attached_server_identity(
        {
            "endpoint": {"host": "127.0.0.1", "port": 2036},
            "server_pid": 4200,
            "server_process_create_time": 1234.5,
            "server_command_signature": "a" * 64,
            "listener_bind_scope": "wildcard",
            "listener_observed_at_epoch": 2345.6,
        }
    )
    model = normalize_shared_model_identity(
        {
            "tag": "Model1",
            "label": "working.mph",
            "file_path": "D:/models/working.mph",
            "unsaved": False,
        }
    )
    revision = build_shared_model_revision(
        model,
        sequence=0,
        structural_readback={"components": ["comp1"], "studies": ["std1"]},
        state_readback={"parameters": {"gap": "10[nm]"}},
    )
    return {
        "kind": "attached_shared_server",
        "user_confirmed_automation_exclusive": True,
        "source_model_lock_sha256": "b" * 64,
        "attached_server": server.to_dict(),
        "model": model.to_dict(),
        "expected_revision": revision.to_dict(),
    }


def test_attached_backend_is_deterministic_idempotent_and_non_owned():
    first = normalize_attached_execution_backend(_backend())
    second = normalize_attached_execution_backend(deepcopy(first))

    assert first == second
    assert first["attached_server"]["ownership"] == "external_user_owned"
    assert first["attached_server"]["listener_bind_scope"] == "wildcard"
    assert first["model"]["tag"] == "Model1"
    assert len(first["backend_identity_sha256"]) == 64


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(user_confirmed_automation_exclusive=False),
        lambda value: value.update(kind="standalone_owned"),
        lambda value: value.update(source_model_lock_sha256="short"),
        lambda value: value.update(secret="not-allowed"),
        lambda value: value["attached_server"].update(ownership="owned"),
        lambda value: value["attached_server"].update(identity_sha256="0" * 64),
        lambda value: value["model"].update(identity_sha256="0" * 64),
        lambda value: value["expected_revision"].update(
            model_identity_sha256="0" * 64
        ),
        lambda value: value["expected_revision"].update(revision_sha256="0" * 64),
    ],
)
def test_attached_backend_rejects_ambiguous_or_tampered_identity(mutation):
    raw = _backend()
    mutation(raw)

    with pytest.raises(ValueError):
        normalize_attached_execution_backend(raw)


def test_attached_backend_rejects_tampered_aggregate_identity():
    normalized = normalize_attached_execution_backend(_backend())
    normalized["backend_identity_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="backend identity SHA-256"):
        normalize_attached_execution_backend(normalized)


def test_attached_backend_contract_import_does_not_import_mph():
    code = """
import sys
from development_kit.tests.test_attached_job_backend import _backend
from src.jobs.attached_backend import normalize_attached_execution_backend
assert 'mph' not in sys.modules
assert normalize_attached_execution_backend(_backend())['kind'] == 'attached_shared_server'
assert 'mph' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
