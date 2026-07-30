"""Safety tests for standalone integration probe boundaries."""

import ast
import asyncio
import inspect
import runpy
from pathlib import Path
from types import SimpleNamespace

import mph
import pytest

from development_kit.tests.integration import clientapi_property_acceptance as property_gate
from development_kit.tests.integration import derived_geometry_acceptance as derived_gate
from development_kit.tests.integration import live_profile_acceptance as live_profile_gate


def test_clientapi_property_acceptance_uses_explicit_runtime_checks():
    script = (
        Path(__file__).parents[2]
        / "development_kit"
        / "tests"
        / "integration"
        / "clientapi_property_acceptance.py"
    )
    source = script.read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]
    assert "_require(" in source


def test_property_acceptance_independently_reads_temporary_value_and_restores(
    monkeypatch,
):
    reads = iter(("old", "temporary", "old"))
    writes = []
    monkeypatch.setattr(
        property_gate,
        "get_existing_property",
        lambda *_args: {"success": True, "value": next(reads)},
    )
    monkeypatch.setattr(
        property_gate,
        "set_existing_property",
        lambda *_args: writes.append(_args[-1]) or {"success": True, "new_value": _args[-1]},
    )

    result = property_gate._round_trip_case(
        object(), "geometry_feature", "geom1/blk1", "base", "temporary"
    )

    assert writes == ["temporary", "old"]
    assert result["temporary"] == "temporary"
    assert result["restored"] == "old"


def test_property_acceptance_restores_after_temporary_readback_mismatch(monkeypatch):
    reads = iter(("old", "unexpected", "old"))
    writes = []
    monkeypatch.setattr(
        property_gate,
        "get_existing_property",
        lambda *_args: {"success": True, "value": next(reads)},
    )
    monkeypatch.setattr(
        property_gate,
        "set_existing_property",
        lambda *_args: writes.append(_args[-1]) or {"success": True, "new_value": _args[-1]},
    )

    with pytest.raises(RuntimeError, match="temporary property readback mismatch"):
        property_gate._round_trip_case(
            object(), "geometry_feature", "geom1/blk1", "base", "temporary"
        )

    assert writes == ["temporary", "old"]


def test_property_acceptance_verifies_exact_runtime_and_observes_solution_tags():
    class Java:
        @staticmethod
        def getComsolVersion():
            return "COMSOL Multiphysics 6.4.0.293"

    client = type("Client", (), {"version": "6.4", "java": Java()})()
    release = property_gate._verify_runtime_release(client)

    assert release["verified"] is True
    assert release["expected_build"] == "6.4.0.293"
    with pytest.raises(RuntimeError, match="not 6.4.0.293"):
        property_gate._verify_runtime_release(
            type(
                "Client",
                (),
                {
                    "version": "6.4",
                    "java": type("J", (), {"getComsolVersion": lambda _self: "6.4.0.292"})(),
                },
            )()
        )

    java_model = type(
        "Model",
        (),
        {"sol": lambda _self: type("Solutions", (), {"tags": lambda _self: ["sol2", "sol1"]})()},
    )()
    assert property_gate._solution_tags(java_model) == ["sol1", "sol2"]


def test_live_profile_cleanup_continues_and_reports_every_failure():
    class Session:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, arguments, **_kwargs):
            self.calls.append((name, arguments))
            if name == "model_remove" and arguments["model_name"] == "first":
                raise OSError("injected removal failure")
            payload = {"success": name != "comsol_disconnect"}
            return SimpleNamespace(isError=False, structuredContent=payload)

    session = Session()
    cleanup = asyncio.run(live_profile_gate._cleanup_live_session(session, ["first", "second"]))

    assert session.calls == [
        ("model_remove", {"model_name": "second"}),
        ("model_remove", {"model_name": "first"}),
        ("comsol_disconnect", {}),
    ]
    assert cleanup["passed"] is False
    assert cleanup["steps"]["model_remove:first"]["error_type"] == "OSError"
    assert cleanup["steps"]["comsol_disconnect"]["passed"] is False


def test_live_profile_call_timeout_is_bounded_by_absolute_deadline(monkeypatch):
    observed = []

    class Session:
        async def call_tool(self, _name, _arguments, **kwargs):
            observed.append(kwargs["read_timeout_seconds"].total_seconds())
            return SimpleNamespace(isError=False, structuredContent={"connected": False})

    monkeypatch.setattr(live_profile_gate.time, "monotonic", lambda: 90.0)
    asyncio.run(live_profile_gate._call_before(Session(), "comsol_status", {}, deadline=100.0))
    assert observed == [10.0]

    with pytest.raises(TimeoutError, match="absolute deadline"):
        asyncio.run(live_profile_gate._call_before(Session(), "comsol_status", {}, deadline=89.0))


def test_live_profile_setup_is_inside_the_cleanup_boundary():
    tree = ast.parse(inspect.getsource(live_profile_gate._live_three_call_matrix))
    setup_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_setup_live_session"
    ]
    cleanup_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_cleanup_live_session"
    ]
    assert len(setup_calls) == 1
    assert len(cleanup_calls) == 1
    assert any(
        any(setup_calls[0] in list(ast.walk(statement)) for statement in node.body)
        and any(cleanup_calls[0] in list(ast.walk(statement)) for statement in node.finalbody)
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
    )


def test_derived_gate_distinguishes_acquired_and_reused_leases():
    assert derived_gate._lease_disposition(
        {"success": True, "acquired": True, "reused": False}
    ) == (True, False)
    assert derived_gate._lease_disposition(
        {"success": True, "acquired": False, "reused": True}
    ) == (False, True)
    with pytest.raises(RuntimeError, match="lease unavailable"):
        derived_gate._lease_disposition({"success": False, "acquired": False, "reused": False})


def test_derived_gate_requires_exact_explicit_build_semantics():
    fin = {"geometry_run": True, "mesh_run": False}
    blocks = {"geometry_run": False, "mesh_run": False}
    counts = {"domains": 1, "boundaries": 2, "elements": 3, "vertices": 4}
    assert derived_gate._build_acceptance(fin, blocks, counts) is True

    for mutated in (
        ({"geometry_run": False, "mesh_run": False}, blocks, counts),
        (fin, {"geometry_run": True, "mesh_run": False}, counts),
        (fin, blocks, {**counts, "domains": 0}),
        (fin, blocks, {**counts, "vertices": 0}),
    ):
        assert derived_gate._build_acceptance(*mutated) is False


def test_unicode_cleanup_continues_after_unlink_failure():
    namespace = runpy.run_path(
        str(Path(__file__).parents[2] / "development_kit/tests/integration/probes/unicode_save.py"),
        run_name="unicode_cleanup_test",
    )
    calls = []

    class Client:
        def clear(self):
            calls.append("clear")

        def disconnect(self):
            calls.append("disconnect")

    class Output:
        def unlink(self, *, missing_ok):
            assert missing_ok is True
            calls.append("unlink")
            raise OSError("injected unlink failure")

    class Directory:
        def rmdir(self):
            calls.append("rmdir")

    result = {"success": True}
    exit_code = namespace["_cleanup_probe"](Client(), Output(), Directory(), result)

    assert calls == ["clear", "disconnect", "unlink", "rmdir"]
    assert exit_code == 1
    assert result["success"] is False
    assert result["cleanup"]["steps"]["output_unlink"]["error_type"] == "OSError"


@pytest.mark.parametrize(
    "script_path",
    [
        "development_kit/tests/integration/probes/capacitor.py",
        "development_kit/tests/integration/probes/study_mesh.py",
        "development_kit/tests/integration/probes/unicode_save.py",
    ],
)
def test_loading_standalone_probe_does_not_create_client(monkeypatch, script_path):
    def fail_client_creation(*args, **kwargs):
        raise AssertionError("mph.Client must not be called while loading a probe")

    monkeypatch.setattr(mph, "Client", fail_client_creation)
    full_path = Path(__file__).parents[2] / script_path

    namespace = runpy.run_path(str(full_path), run_name="probe_import_test")

    assert callable(namespace["main"])
