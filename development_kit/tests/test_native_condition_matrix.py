"""Solver-free public dispatch, raw evidence, ownership and replay regressions."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from comsol_mcp.contracts.native_condition import normalize_native_condition_request
from comsol_mcp.jobs import native_condition_collector as native
from comsol_mcp.jobs.native_wall_watchdog import native_wall_budget
from comsol_mcp.jobs.store import JobStore, atomic_write_json, process_identity
from comsol_mcp.jobs.validation_collectors import execute_validation_collector
from comsol_mcp.jobs.validation_matrix import normalize_validation_matrix_spec
from comsol_mcp.jobs.validation_rows import completed_point_fingerprints
from comsol_mcp.jobs.validation_runner import run_pending_validation_points
from comsol_mcp.jobs.validation_worker import NativeCleanupUnverified, _run
from comsol_mcp.server import create_server
from comsol_mcp.tools import jobs
from development_kit.tests.mcp_test_support import decode_tool_result
from development_kit.tests.test_validation_worker import FakeOwnership, _telemetry


def _raw(root):
    source = root / "native.mph"
    source.write_bytes(b"synthetic-native-source")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    path = Path(__file__).parent / "fixtures/native_condition_request.json"
    request = json.loads(path.read_text(encoding="utf-8"))
    request["support"]["source_identity"] = digest
    config = request["adapter_configuration"]["configuration"]
    config["fixture"]["source_identity"]["source_sha256"] = digest
    config["tree_readback"]["source_sha256"] = digest
    condition = request["condition_table"]["conditions"][0]
    return {
        "job_type": "validation_matrix",
        "source_model_path": str(source),
        "cores": 1,
        "version": request["support"]["comsol_version"],
        "point_limit": 1,
        "startup_admission": {
            "schema_name": "comsol_mcp.robust_startup_admission_policy",
            "schema_version": "1.0.0",
            "minimum_available_memory_bytes": 1,
            "minimum_runtime_free_bytes": 1,
            "check_frequency": "startup_only",
        },
        "resource_policy": {
            "wall_time_budget_seconds": 120.5,
            "minimum_next_point_seconds": 1,
            "max_mesh_elements": request["shape_policy"]["mesh_admission"][
                "max_elements_per_model"
            ],
        },
        "points": [
            {
                "point_id": "fixed-condition",
                "configuration_sha256": "a" * 64,
                "wavelength": {"value": condition["wavelength_m"], "unit": "m", "parameter": "wl"},
                "incidence": {
                    "theta_degrees": condition["incidence_elevation_deg"],
                    "phi_degrees": condition["incidence_azimuth_deg"],
                    "polarization": condition["polarization_basis_id"],
                },
                "collectors": [{"name": "native_condition_gradient", "inputs": request}],
                "expected_artifact_ids": ["raw-native"],
            }
        ],
    }


def _forward_raw(path, version="1.4.0"):
    from comsol_mcp.contracts import condition_controls as controls

    raw = _raw(path)
    collector = raw["points"][0]["collectors"][0]
    collector["name"] = "native_condition_forward"
    collector["inputs"]["schema_name"] = "comsol_mcp.native_condition_forward_request"
    mapping = collector["inputs"]["adapter_configuration"]["configuration"]["condition_controls"]
    if version == "1.2.0":
        mapping.update(
            coarse_solver_feature_path=[mapping["selected_linear_solver_tag"], "mg1", "cs1"],
            coarse_solver_out_of_core_property="ooc",
            coarse_solver_out_of_core_value="off",
        )
    if version == "1.5.0":
        mapping.update(
            forward_shape_application_mode="deformation_stage",
            forward_deformation_step_tag="dg_step",
            forward_deformation_step_type="Stationary",
            forward_deformation_physics_tag="dg_pedot72",
            forward_solved_shape_expressions=["u", "v", "w"],
            forward_solved_shape_relative_tolerance=1e-6,
        )
    fields = set(controls._BASE_FIELDS)
    if version >= "1.1.0":
        fields.update(controls._SOLVER_SELECTION_FIELDS)
    if version >= "1.2.0":
        fields.update(controls._COARSE_SOLVER_MEMORY_FIELDS)
    if version >= "1.3.0":
        fields.update(controls._MESH_REFERENCE_FIELDS)
    if version >= "1.4.0":
        fields.update(controls._NATIVE_SENSITIVITY_FIELDS)
    if version >= "1.5.0":
        fields.update(controls._FORWARD_SHAPE_FIELDS)
    collector["inputs"]["adapter_configuration"]["configuration"]["condition_controls"] = {
        **{key: value for key, value in mapping.items() if key in fields},
        "schema_version": version,
    }
    return raw


class _Java:
    def save(self, path, *_args):
        Path(path).write_bytes(b"synthetic-native-source")


class _Model:
    java = _Java()

    def name(self):
        return "immutable-source"


class _Client:
    port = None

    def __init__(self, *, fail_remove=False):
        self.loads = []
        self.removes = []
        self.fail_remove = fail_remove
        self.cleared = False

    def load(self, path):
        self.loads.append(path)
        return _Model()

    def remove(self, model):
        if self.fail_remove:
            raise RuntimeError("injected clone cleanup failure")
        self.removes.append(model)

    def clear(self):
        self.cleared = True


class _Backend:
    calls = []

    def __init__(self, model, spec, **_kwargs):
        self.controls = spec["adapter_configuration"]["configuration"]["condition_controls"]

    def prepare(self, values):
        self.calls.append("prepare")
        return {"receipt_fingerprint": "a" * 64, "values": list(values)}

    def evaluate_condition(self, condition, _expressions):
        self.calls.append("forward")
        return {
            "condition_id": condition["condition_id"],
            "observable_id": condition["observable_id"],
            "observable_value": 0.7,
            "requested_wavelength_m": condition["wavelength_m"],
            "evaluated_wavelength_m": condition["wavelength_m"],
            "solved_wavelength_m": condition["wavelength_m"],
            "reflectance": 0.1,
            "transmittance": 0.7,
            "absorption": 0.2,
            "mesh_elements": 100,
            "minimum_mesh_quality": 0.8,
            "dataset_id": self.controls["dataset_tag"],
            "solution_id": self.controls["solution_tag"],
        }

    def evaluate_condition_gradient(self, condition, expressions, variables):
        result = self.evaluate_condition(condition, expressions)
        self.calls[-1] = "gradient"
        return {
            **result,
            "derivative_dataset_id": "dset2",
            "derivative_solution_id": "sol2",
            "variable_ids": variables,
            "raw_gradients": [{"real": 0.1, "imaginary": 0.01}, {"real": 0.2, "imaginary": -0.02}],
            "accepted_real_gradients": [0.1, 0.2],
            "gradient_unit": "1/m",
            "identity_fingerprints": {
                name: str(index) * 64
                for index, name in enumerate(
                    ("primal", "adjoint", "study", "solution", "dataset"), 1
                )
            },
        }


@pytest.mark.parametrize("gradients", [True, False])
def test_public_preview_and_submit_reach_native_matrix_without_a_solver(
    ascii_tmp_path, monkeypatch, gradients
):
    monkeypatch.setattr("comsol_mcp.platform_support.native_solver_enabled", lambda: True)
    raw = _raw(ascii_tmp_path) if gradients else _forward_raw(ascii_tmp_path)
    received = []

    def submit(spec):
        received.append(normalize_validation_matrix_spec(spec))
        return {"success": True, "job_id": "dispatch-only"}

    monkeypatch.setattr(jobs, "job_manager", SimpleNamespace(submit=submit))
    server = create_server("native-raw-dispatch", profile="core")
    preview = decode_tool_result(asyncio.run(server.call_tool("job_spec_preview", {"spec": raw})))
    assert preview["success"] is True
    assert preview["inventory"]["declared_points"] == 1
    result = decode_tool_result(asyncio.run(server.call_tool("job_submit", {"spec": raw})))
    assert result["job_id"] == "dispatch-only"
    assert received[0]["points"][0]["collectors"][0]["name"] == (
        "native_condition_gradient" if gradients else "native_condition_forward"
    )
    assert native_wall_budget(received[0]) == (
        "resource_policy.rules.wall_time_budget_seconds",
        120.5,
    )


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda r: r.pop("startup_admission"), "startup admission"),
        (lambda r: r.update(version="6.3"), "version"),
        (lambda r: r["points"][0]["wavelength"].update(value=1.9e-6), "wavelength"),
        (lambda r: r["points"][0]["incidence"].update(polarization="wrong"), "incidence"),
        (lambda r: r["resource_policy"].update(max_mesh_elements=1), "mesh cap"),
        (
            lambda r: r["points"][0]["collectors"][0]["inputs"].update(objective={}),
            "fields mismatch",
        ),
    ],
)
def test_native_matrix_rejects_policy_or_point_drift_before_startup(
    ascii_tmp_path, mutation, match
):
    raw = _raw(ascii_tmp_path)
    mutation(raw)
    with pytest.raises(ValueError, match=match):
        normalize_validation_matrix_spec(raw)


def test_native_request_is_idempotent_and_tampering_is_rejected(ascii_tmp_path):
    request = _raw(ascii_tmp_path)["points"][0]["collectors"][0]["inputs"]
    normalized = normalize_native_condition_request(request)
    assert normalize_native_condition_request(normalized) == normalized
    changed = copy.deepcopy(normalized)
    changed["initial_values"][0] += 1
    with pytest.raises(ValueError, match="fingerprint"):
        normalize_native_condition_request(changed)


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0"])
def test_forward_collector_retains_control_versions_without_computing_gradients(
    ascii_tmp_path, version
):
    spec = normalize_validation_matrix_spec(_forward_raw(ascii_tmp_path, version))
    point = spec["points"][0]

    class ForwardBackend(_Backend):
        def run_shape_application(self):
            return {"receipt_fingerprint": "b" * 64}

        def evaluate_condition_gradient(self, *_args):
            pytest.fail("Explicit forward collection requested a gradient")

    root = ascii_tmp_path / "forward"
    client = _Client()
    result = native.execute_native_condition_collector(
        point,
        point["collectors"][0],
        root,
        model=_Model(),
        client=client,
        expected_source_sha256=spec["source_model_sha256"],
        cancel_requested=lambda: False,
        backend_factory=ForwardBackend,
    )
    assert result["success"]
    manifest = json.loads((root / "native-condition.json").read_text())
    assert manifest["schema_name"] == "comsol_mcp.native_condition_forward_manifest"
    assert manifest["gradient"] is None
    assert manifest["forward"]["observable_value"] == 0.7
    assert manifest["cleanup"]["clone_removed"]
    assert len(client.removes) == 1
    assert not list(root.glob("condition-gradient-*.json"))
    normalized = normalize_native_condition_request(
        point["collectors"][0]["inputs"], gradients=False
    )
    assert normalize_native_condition_request(normalized, gradients=False) == normalized
    with pytest.raises(ValueError, match="schema"):
        normalize_native_condition_request(normalized)


@pytest.mark.parametrize("gradients", [True, False])
def test_raw_collector_executes_validation_role_without_scoring_and_replays(
    ascii_tmp_path, monkeypatch, gradients
):
    raw = _raw(ascii_tmp_path) if gradients else _forward_raw(ascii_tmp_path)
    spec = normalize_validation_matrix_spec(raw)
    client = _Client()
    _Backend.calls = []
    monkeypatch.setattr(native, "ClientapiLin2025ConditionBackend", _Backend)

    def execute(point, collector, root):
        return execute_validation_collector(
            point,
            collector,
            root,
            model=_Model(),
            client=client,
            model_name="source",
            job_id="test",
            expected_source_sha256=spec["source_model_sha256"],
            session_state={"connected": True},
            ownership_preflight={"ready": True},
            cancel_requested=lambda: False,
        )

    result = run_pending_validation_points(
        spec, ascii_tmp_path / "job", attempt=1, collector_executor=execute
    )
    assert result["errors"] == 0
    expected = ["prepare", "forward", "gradient"] if gradients else ["prepare", "forward"]
    assert _Backend.calls == expected
    assert len(client.removes) == 1
    payload = next((ascii_tmp_path / "job").rglob("native-condition.json"))
    document = json.loads(payload.read_text())
    assert document["scientific_disposition"] == "UNVALIDATED"
    assert document["evidence_state"] == "native_unchecked"
    assert document["cleanup"]["clone_removed"] is True
    assert not list(payload.parent.glob("*aggregate*"))
    run_pending_validation_points(
        spec, ascii_tmp_path / "job", attempt=2, collector_executor=execute
    )
    assert _Backend.calls == expected
    payload.write_text(payload.read_text().replace('"native_unchecked"', '"tampered"'))
    with pytest.raises(ValueError, match="inner manifest"):
        completed_point_fingerprints(
            ascii_tmp_path / "job/matrix_rows.jsonl", spec, artifact_root=ascii_tmp_path / "job"
        )


@pytest.mark.parametrize("failure", ["cancel", "cleanup"])
def test_native_collector_does_not_publish_success_before_cleanup(ascii_tmp_path, failure):
    spec = normalize_validation_matrix_spec(_raw(ascii_tmp_path))
    point = spec["points"][0]
    _Backend.calls = []
    client = _Client(fail_remove=failure == "cleanup")
    root = ascii_tmp_path / "output"
    with pytest.raises((InterruptedError, RuntimeError)):
        native.execute_native_condition_collector(
            point,
            point["collectors"][0],
            root,
            model=_Model(),
            client=client,
            expected_source_sha256=spec["source_model_sha256"],
            cancel_requested=lambda: failure == "cancel",
            backend_factory=_Backend,
        )
    assert not (root / "matrix_collector.json").exists()
    if failure == "cancel":
        assert _Backend.calls == []
        assert client.loads == []


def test_out_of_core_requires_and_preserves_the_declared_directory(ascii_tmp_path):
    raw = _raw(ascii_tmp_path)
    raw["points"][0]["collectors"][0]["inputs"]["adapter_configuration"]["configuration"][
        "condition_controls"
    ]["out_of_core_value"] = "on"
    with pytest.raises(ValueError, match="temporary directory"):
        normalize_validation_matrix_spec(raw)
    raw["comsol_temporary_directory"] = str(ascii_tmp_path)
    spec = normalize_validation_matrix_spec(raw)
    assert spec["comsol_temporary_directory"] == str(ascii_tmp_path.resolve())


def test_midpoint_cancel_preserves_raw_forward_and_removes_clone(ascii_tmp_path):
    spec = normalize_validation_matrix_spec(_raw(ascii_tmp_path))
    point = spec["points"][0]
    _Backend.calls = []
    client = _Client()
    root = ascii_tmp_path / "cancelled"
    with pytest.raises(InterruptedError):
        native.execute_native_condition_collector(
            point,
            point["collectors"][0],
            root,
            model=_Model(),
            client=client,
            expected_source_sha256=spec["source_model_sha256"],
            cancel_requested=lambda: "forward" in _Backend.calls,
            backend_factory=_Backend,
        )
    assert _Backend.calls == ["prepare", "forward"]
    assert len(client.removes) == 1
    assert (root / "forward-raw.json").is_file()
    assert not (root / "matrix_collector.json").exists()


def _stored_native_job(root, raw):
    """Create a stored job whose watchdog window still contains the current time.

    The worker loop runs inside the pytest process, and the product measures the
    deadline from the worker's own creation time. On a machine that reaches this
    test later than the declared budget, the window has already closed and the
    worker refuses with "native wall watchdog deadline is invalid or expired".
    That is a property of the harness, not of the behaviour under test, so the
    stored spec declares a budget that still covers the elapsed time. The product
    validates the deadline against the same spec, so nothing is bypassed; the
    fixture only stops asserting that CI is fast.
    """
    budget = max(
        120.5,
        __import__("time").time()
        - process_identity(__import__("os").getpid())["process_create_time"]
        + 600.0,
    )
    raw = json.loads(json.dumps(raw))
    raw["resource_policy"]["wall_time_budget_seconds"] = budget
    spec = normalize_validation_matrix_spec(raw)
    store = JobStore(root)
    identity = process_identity(__import__("os").getpid())
    job_id = store.create(
        spec,
        {
            "status": "submitted",
            "attempt": 1,
            "worker_pid": identity["pid"],
            "worker_process_create_time": identity["process_create_time"],
            "worker_command_signature": identity["command_signature"],
        },
    )
    atomic_write_json(
        store.job_dir(job_id) / "wall-watchdog.json",
        {
            "status": "armed",
            "attempt": 1,
            "target_worker": identity,
            "budget_source": "resource_policy.rules.wall_time_budget_seconds",
            "budget_seconds": budget,
            "deadline_epoch": identity["process_create_time"] + budget,
        },
    )
    return store, job_id, spec


@pytest.mark.parametrize("gradients", [True, False])
def test_native_matrix_worker_uses_frozen_source_and_releases_owner(
    ascii_tmp_path, monkeypatch, gradients
):
    raw = _raw(ascii_tmp_path) if gradients else _forward_raw(ascii_tmp_path, "1.3.0")
    store, job_id, spec = _stored_native_job(ascii_tmp_path / "jobs", raw)
    client = _Client()
    ownership = FakeOwnership()
    _Backend.calls = []
    monkeypatch.setattr(native, "ClientapiLin2025ConditionBackend", _Backend)
    code = _run(
        str(store.root),
        job_id,
        client_factory=lambda _spec: client,
        ownership_factory=lambda *_args: ownership,
        telemetry_provider=_telemetry(),
        native_cancel_enabled=False,
    )
    assert code == 0
    assert store.read_state(job_id)["status"] == "completed"
    assert client.cleared and ownership.released
    assert client.loads[0] != spec["source_model_path"]
    assert not Path(client.loads[0]).exists()
    assert Path(spec["source_model_path"]).read_bytes() == b"synthetic-native-source"
    assert _Backend.calls == (
        ["prepare", "forward", "gradient"] if gradients else ["prepare", "forward"]
    )


def test_native_worker_preserves_out_of_core_directory_and_restores_environment(
    ascii_tmp_path, monkeypatch
):
    raw = _raw(ascii_tmp_path)
    raw["points"][0]["collectors"][0]["inputs"]["adapter_configuration"]["configuration"][
        "condition_controls"
    ]["out_of_core_value"] = "on"
    raw["comsol_temporary_directory"] = str(ascii_tmp_path)
    store, job_id, _spec = _stored_native_job(ascii_tmp_path / "jobs", raw)
    client = _Client()
    monkeypatch.setenv("COMSOL_TMPDIR", "previous-directory")
    monkeypatch.setattr(native, "ClientapiLin2025ConditionBackend", _Backend)

    def create_client(_spec):
        assert os.environ["COMSOL_TMPDIR"] == str(ascii_tmp_path)
        return client

    assert (
        _run(
            str(store.root),
            job_id,
            client_factory=create_client,
            ownership_factory=lambda *_args: FakeOwnership(),
            telemetry_provider=_telemetry(),
            native_cancel_enabled=False,
            java_environment_reader=lambda _name: os.environ["COMSOL_TMPDIR"],
        )
        == 0
    )
    assert os.environ["COMSOL_TMPDIR"] == "previous-directory"
    assert (store.job_dir(job_id) / "comsol-temporary-directory.json").is_file()


def test_native_worker_observes_cancel_during_startup_before_owner(ascii_tmp_path, monkeypatch):
    import comsol_mcp.jobs.native_wall_watchdog as watchdog

    store, job_id, _spec = _stored_native_job(ascii_tmp_path / "jobs", _raw(ascii_tmp_path))
    ownership = FakeOwnership()

    def cancel_during_wait(*_args):
        store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))

    monkeypatch.setattr(watchdog, "await_native_wall_watchdog", cancel_during_wait)
    assert (
        _run(
            str(store.root),
            job_id,
            client_factory=lambda _spec: pytest.fail("Cancelled startup created a client"),
            ownership_factory=lambda *_args: ownership,
            telemetry_provider=_telemetry(),
            native_cancel_enabled=False,
        )
        == 0
    )
    assert ownership.acquired is False
    assert not list(store.job_dir(job_id).glob("native-source-*"))


@pytest.mark.parametrize("fault", ["clear", "disconnect", "release_refused", "release_exception"])
def test_native_worker_defers_completion_until_owned_cleanup(ascii_tmp_path, monkeypatch, fault):
    store, job_id, _spec = _stored_native_job(ascii_tmp_path / "jobs", _raw(ascii_tmp_path))
    client = _Client()
    owner = FakeOwnership()
    monkeypatch.setattr(native, "ClientapiLin2025ConditionBackend", _Backend)

    def fail():
        raise RuntimeError("injected owned cleanup failure")

    if fault == "clear":
        monkeypatch.setattr(client, "clear", fail)
    elif fault == "disconnect":
        client.port = 2036
        monkeypatch.setattr(client, "disconnect", fail, raising=False)
    elif fault == "release_refused":
        monkeypatch.setattr(owner, "release", lambda: {"success": False})
    else:
        monkeypatch.setattr(owner, "release", fail)
    with pytest.raises(NativeCleanupUnverified):
        _run(
            str(store.root),
            job_id,
            client_factory=lambda _spec: client,
            ownership_factory=lambda *_args: owner,
            telemetry_provider=_telemetry(),
            native_cancel_enabled=False,
        )
    state = store.read_state(job_id)
    assert state["status"] == "failed"
    assert state["cleanup_pending"] is True
    assert state["owned_cleanup"]["success"] is False
    assert owner.released is False
    assert list(store.job_dir(job_id).glob("native-source-*/source.mph"))
    assert not state.get("source_unchanged", False)


def test_native_worker_detects_original_source_changes_despite_frozen_copy(
    ascii_tmp_path, monkeypatch
):
    store, job_id, spec = _stored_native_job(ascii_tmp_path / "jobs", _raw(ascii_tmp_path))

    class MutatingBackend(_Backend):
        def prepare(self, values):
            Path(spec["source_model_path"]).write_bytes(b"external-original-mutation")
            return super().prepare(values)

    owner = FakeOwnership()
    monkeypatch.setattr(native, "ClientapiLin2025ConditionBackend", MutatingBackend)
    assert (
        _run(
            str(store.root),
            job_id,
            client_factory=lambda _spec: _Client(),
            ownership_factory=lambda *_args: owner,
            telemetry_provider=_telemetry(),
            native_cancel_enabled=False,
        )
        == 1
    )
    state = store.read_state(job_id)
    assert state["status"] == "failed"
    assert "original source changed" in state["last_error"]["message"]
    assert owner.released


@pytest.mark.parametrize("failure", ["watchdog", "memory", "java_directory"])
def test_native_startup_refusals_precede_model_load(ascii_tmp_path, failure):
    raw = _raw(ascii_tmp_path)
    if failure == "memory":
        raw["startup_admission"]["minimum_available_memory_bytes"] = 10**18
    if failure == "java_directory":
        raw["comsol_temporary_directory"] = str(ascii_tmp_path)
    store, job_id, spec = _stored_native_job(ascii_tmp_path / "jobs", raw)
    if failure == "watchdog":
        path = store.job_dir(job_id) / "wall-watchdog.json"
        record = json.loads(path.read_text())
        record["budget_seconds"] -= 0.5
        atomic_write_json(path, record)
    client = _Client()
    ownership = FakeOwnership()
    code = _run(
        str(store.root),
        job_id,
        client_factory=lambda _spec: client,
        ownership_factory=lambda *_args: ownership,
        telemetry_provider=_telemetry(),
        native_cancel_enabled=False,
        java_environment_reader=lambda _name: "different-directory",
    )
    assert code == 1
    assert client.loads == []
    assert store.read_state(job_id)["status"] == "failed"
    if failure == "java_directory":
        assert client.cleared and ownership.released
    else:
        assert ownership.acquired is False
