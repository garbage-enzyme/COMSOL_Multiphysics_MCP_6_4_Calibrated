"""Safety tests for standalone integration probe boundaries."""

import ast
import asyncio
import inspect
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import mph
import pytest
from mcp.types import PaginatedRequestParams

from development_kit.tests.integration import clientapi_property_acceptance as property_gate
from development_kit.tests.integration import derived_geometry_acceptance as derived_gate
from development_kit.tests.integration import live_profile_acceptance as live_profile_gate
from development_kit.tests.integration import periodic_mesh_acceptance as periodic_mesh_gate
from development_kit.tests.integration import test_native_cancel_candidate as native_cancel_gate
from development_kit.tests.integration import test_real_comsol as real_comsol_gate
from development_kit.tests.integration import wave_optics_point_audit_acceptance as point_gate
from development_kit.tests.integration import wave_optics_preflight_acceptance as preflight_gate

ROOT = Path(__file__).parents[2]
STANDALONE_PROBES = tuple(
    path.relative_to(ROOT).as_posix()
    for path in sorted((ROOT / "development_kit/tests/integration/probes").glob("*.py"))
    if path.name != "__init__.py"
)
if not STANDALONE_PROBES:
    raise RuntimeError("standalone integration probe collection must not be empty")


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


def test_property_acceptance_rejects_a_noop_setter_and_restores(monkeypatch):
    reads = iter(("old", "old", "old"))
    writes = []
    monkeypatch.setattr(
        property_gate,
        "get_existing_property",
        lambda *_args: {"success": True, "value": next(reads)},
    )
    monkeypatch.setattr(
        property_gate,
        "set_existing_property",
        lambda *_args: writes.append(_args[-1]) or {"success": True, "new_value": "old"},
    )

    with pytest.raises(RuntimeError, match="did not apply"):
        property_gate._round_trip_case(
            object(), "geometry_feature", "geom1/blk1", "base", "temporary"
        )

    assert writes == ["temporary", "old"]


def test_property_acceptance_clears_client_when_gate_fails(tmp_path, monkeypatch):
    calls = []

    class Client:
        def clear(self):
            calls.append("clear")

    monkeypatch.setenv("COMSOL_MCP_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("COMSOL_MCP_ACCEPTANCE_CORES", "1")
    monkeypatch.setattr(property_gate.mph, "Client", lambda **_kwargs: Client())
    monkeypatch.setattr(
        property_gate,
        "_run_gate",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("injected gate failure")),
    )

    with pytest.raises(RuntimeError, match="gate failure"):
        property_gate.main()

    assert calls == ["clear"]


def test_property_acceptance_verifies_exact_runtime_and_observes_solution_tags():
    class Java:
        @staticmethod
        def getComsolVersion():
            return "COMSOL Multiphysics 6.4.0.293"

    client = type("Client", (), {"version": "6.4", "java": Java()})()
    release = property_gate._verify_runtime_release(client)

    assert release["verified"] is True
    assert release["expected_build"] == "6.4.0.293"
    assert release["parsed_build"] == 293
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


def test_property_acceptance_accepts_a_localized_version_string_and_still_pins_the_build():
    """A localized COMSOL reports the build without the zero patch component.

    Measured on the acceptance host: the Chinese installation reports
    ``COMSOL Multiphysics 6.4 (开发版本: 293)``. Positional four-number parsing made
    this gate fail while the build genuinely was 293.
    """
    localized = "COMSOL Multiphysics 6.4 (开发版本: 293)"
    assert property_gate._parse_runtime_release(localized) == ("6.4", 293)

    client = type(
        "Client",
        (),
        {
            "version": "6.4",
            "java": type("J", (), {"getComsolVersion": lambda _self: localized})(),
        },
    )()
    release = property_gate._verify_runtime_release(client)
    assert release["verified"] is True
    assert release["parsed_build"] == 293

    # The build pin must still reject a genuinely different build.
    with pytest.raises(RuntimeError, match="not 6.4.0.293"):
        property_gate._verify_runtime_release(
            type(
                "Client",
                (),
                {
                    "version": "6.4",
                    "java": type(
                        "J", (), {"getComsolVersion": lambda _self: "COMSOL 6.4 (开发版本: 292)"}
                    )(),
                },
            )()
        )


def test_property_acceptance_release_parse_rejects_unparseable_versions():
    assert property_gate._parse_runtime_release("") is None
    assert property_gate._parse_runtime_release("COMSOL Multiphysics") is None
    assert property_gate._parse_runtime_release("6.4") == ("6.4", 4)


def test_live_profile_launches_the_installed_distribution_not_the_source_tree():
    """The gate asserts ``installed_site_package``, so it must start packaged code.

    ``src`` is a repository-only shim and is not packaged, so ``-m src.server``
    could never satisfy this gate's own identity requirement.
    """
    parameters = live_profile_gate._server("core")

    assert parameters.args == ["-m", "comsol_mcp.server"]
    assert "src.server" not in parameters.args
    assert parameters.cwd == live_profile_gate.RUNTIME
    assert parameters.env["COMSOL_MCP_PROFILE"] == "core"
    assert "PYTHONPATH" not in parameters.env


def test_live_profile_counts_stay_pinned_to_the_frozen_snapshot():
    """Keep the gate's expectations independent *and* impossible to silently rot.

    The numbers stay written out in the probe so it asserts an independent value
    rather than echoing the artifact it checks. This test is the drift alarm: it
    fails the moment they disagree with the frozen snapshot.
    """
    snapshot = json.loads(
        (ROOT / "development_kit/tests/snapshots/profile_tool_names.json").read_text(
            encoding="utf-8"
        )
    )

    assert live_profile_gate.PROFILE_COUNTS, "the gate must assert at least one profile"
    for profile, expected in live_profile_gate.PROFILE_COUNTS.items():
        assert profile in snapshot, f"{profile} is not a known profile"
        assert len(snapshot[profile]) == expected, (
            f"{profile}: gate expects {expected}, frozen snapshot has {len(snapshot[profile])}"
        )
        assert expected > 0


def test_live_profile_collects_every_page_of_a_cursored_listing(monkeypatch):
    """One ``tools/list`` call is not the whole surface when a cursor is served."""
    pages = [
        SimpleNamespace(
            tools=[SimpleNamespace(name="alpha")],
            next_cursor="tools-v1:1",
        ),
        SimpleNamespace(
            tools=[SimpleNamespace(name="beta"), SimpleNamespace(name="gamma")],
            next_cursor=None,
        ),
    ]
    seen_params = []

    class Session:
        async def list_tools(self, *, params=None):
            seen_params.append(params)
            return pages.pop(0)

    names, page_count = asyncio.run(live_profile_gate._list_every_tool(Session()))

    assert sorted(names) == ["alpha", "beta", "gamma"]
    assert page_count == 2
    assert seen_params[0] is None
    assert seen_params[1] == PaginatedRequestParams(cursor="tools-v1:1")


def test_live_profile_cursor_walk_is_bounded():
    """A server that never stops handing back a cursor must not loop forever."""

    class Session:
        async def list_tools(self, *, params=None):
            return SimpleNamespace(
                tools=[SimpleNamespace(name="looping")],
                next_cursor="tools-v1:never-ends",
            )

    names, page_count = asyncio.run(live_profile_gate._list_every_tool(Session()))

    assert page_count == live_profile_gate.MAX_DISCOVERY_PAGES
    assert len(names) == live_profile_gate.MAX_DISCOVERY_PAGES


def test_live_profile_cleanup_continues_and_reports_every_failure():
    class Session:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, arguments, **_kwargs):
            self.calls.append((name, arguments))
            if name == "comsol_status":
                payload = {
                    "success": True,
                    "models": [
                        {"name": "first", "revision_sha256": "a" * 64},
                        {"name": "second", "revision_sha256": "b" * 64},
                    ],
                }
                return SimpleNamespace(isError=False, structuredContent=payload)
            if name == "model_remove" and arguments["model_name"] == "first":
                raise OSError("injected removal failure")
            payload = {"success": name != "comsol_disconnect"}
            return SimpleNamespace(isError=False, structuredContent=payload)

    session = Session()
    cleanup = asyncio.run(live_profile_gate._cleanup_live_session(session, ["first", "second"]))

    # `model_remove` is destructive_session, so it is guarded by the required
    # model-revision contract; the cleanup must read and declare the revision
    # before each removal. Only the removals and the disconnect are asserted
    # here; the preceding revision reads are filtered out.
    assert [call for call in session.calls if call[0] != "comsol_status"] == [
        ("model_remove", {"model_name": "second", "expected_model_revision": "b" * 64}),
        ("model_remove", {"model_name": "first", "expected_model_revision": "a" * 64}),
        ("comsol_disconnect", {}),
    ]
    assert cleanup["passed"] is False
    assert cleanup["steps"]["model_remove:first"]["error_type"] == "OSError"
    assert cleanup["steps"]["comsol_disconnect"]["passed"] is False


def test_live_profile_declares_the_model_revision_for_every_guarded_call():
    """Both guarded call sites must declare the revision, so the gate measures
    the real contract instead of failing closed with
    "expected_model_revision does not match current model state."
    """

    class Session:
        async def call_tool(self, name, _arguments, **_kwargs):
            assert name == "comsol_status"
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "success": True,
                    "models": [{"name": "m", "revision_sha256": "c" * 64}],
                },
            )

    assert asyncio.run(live_profile_gate._model_revision(Session(), "m")) == "c" * 64

    class Missing:
        async def call_tool(self, _name, _arguments, **_kwargs):
            return SimpleNamespace(isError=False, structuredContent={"success": True, "models": []})

    with pytest.raises(RuntimeError, match="absent from the session status"):
        asyncio.run(live_profile_gate._model_revision(Missing(), "m"))

    source = inspect.getsource(live_profile_gate)
    assert source.count('"expected_model_revision"') >= 2


def test_live_profile_call_timeout_is_bounded_by_absolute_deadline(monkeypatch):
    observed = []

    class Session:
        async def call_tool(self, _name, _arguments, **kwargs):
            observed.append(kwargs["read_timeout_seconds"])
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


def test_derived_gate_rejects_incorrect_finalization_execution_immediately():
    assert derived_gate._fin_execution_contract({"geometry_run": True, "mesh_run": False}) is True
    assert derived_gate._fin_execution_contract({"geometry_run": False, "mesh_run": False}) is False
    assert derived_gate._fin_execution_contract({"geometry_run": True, "mesh_run": True}) is False


def test_unicode_cleanup_continues_after_unlink_failure():
    namespace = runpy.run_path(
        str(Path(__file__).parents[2] / "development_kit/tests/integration/probes/unicode_save.py"),
        run_name="unicode_cleanup_test",
    )
    calls = []

    class Client:
        # A still-connected client, so the disconnect step is exercised here.
        port = 20320

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


def test_unicode_cleanup_skips_disconnect_once_clear_has_torn_the_server_down():
    """``clear()`` disconnects a standalone client, so ``disconnect()`` would raise.

    MPh's ``Client.disconnect`` only acts ``if self.port``; after ``clear()`` on a
    standalone client ``port`` is ``None`` and calling it raises "The client is not
    connected to a server." Measured on real COMSOL: that spurious failure was the
    only reason this probe failed after saving its model successfully.
    """
    namespace = runpy.run_path(
        str(Path(__file__).parents[2] / "development_kit/tests/integration/probes/unicode_save.py"),
        run_name="unicode_cleanup_test",
    )
    calls = []

    class Client:
        port = None

        def clear(self):
            calls.append("clear")

        def disconnect(self):  # pragma: no cover - must never be called
            calls.append("disconnect")
            raise AssertionError("disconnect must not run once clear() has disconnected")

    class Output:
        def unlink(self, *, missing_ok):
            assert missing_ok is True
            calls.append("unlink")

    class Directory:
        def rmdir(self):
            calls.append("rmdir")

    result = {"success": True}
    exit_code = namespace["_cleanup_probe"](Client(), Output(), Directory(), result)

    assert calls == ["clear", "unlink", "rmdir"]
    assert "client_disconnect" not in result["cleanup"]["steps"]
    assert exit_code == 0
    assert result["success"] is True


@pytest.mark.parametrize(
    "script_path",
    STANDALONE_PROBES,
)
def test_loading_standalone_probe_does_not_create_client(monkeypatch, script_path):
    def fail_client_creation(*args, **kwargs):
        raise AssertionError("mph.Client must not be called while loading a probe")

    monkeypatch.setattr(mph, "Client", fail_client_creation)
    full_path = ROOT / script_path

    namespace = runpy.run_path(str(full_path), run_name="probe_import_test")

    assert callable(namespace["main"])


def test_capacitor_probe_uses_one_parameterized_geometry_and_theory(monkeypatch):
    monkeypatch.setattr(mph, "Client", lambda *_args, **_kwargs: pytest.fail("no client"))
    namespace = runpy.run_path(
        str(Path(__file__).parents[2] / "development_kit/tests/integration/probes/capacitor.py"),
        run_name="capacitor_contract_test",
    )

    assert namespace["_geometry_size_expressions"]() == (
        "plate_side",
        "plate_side",
        "plate_gap",
    )
    expected = 8.8541878128e-12 * 2.1 * (0.01**2) / 0.001 * 1e12
    assert namespace["_theoretical_capacitance_pf"]() == pytest.approx(expected)


def test_real_probe_closes_tree_containment_after_normal_parent_exit(monkeypatch):
    class Process:
        pid = 42001

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(*, timeout):
            return 0

    class Containment:
        closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        real_comsol_gate.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("taskkill is unnecessary for an exited root"),
    )
    containment = Containment()

    cleanup = real_comsol_gate._terminate_owned_process_tree(Process(), containment)

    assert containment.closed is True
    assert cleanup == {
        "passed": True,
        "root_absent": True,
        "job_object_contained": True,
        "errors": [],
    }


def test_native_cancel_probe_closes_tree_containment_after_normal_parent_exit(monkeypatch):
    class Process:
        pid = 42002

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(*, timeout):
            return 0

    class Containment:
        closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        native_cancel_gate.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("taskkill is unnecessary for an exited root"),
    )
    containment = Containment()

    cleanup = native_cancel_gate._terminate_owned_process_tree(Process(), containment)

    assert containment.closed is True
    assert cleanup["passed"] is True
    assert cleanup["job_object_contained"] is True


def test_periodic_mesh_allocates_model_artifacts_only_after_lease(tmp_path):
    with pytest.raises(RuntimeError, match="requires solver lease ownership"):
        periodic_mesh_gate._owned_model_path(tmp_path, lease_acquired=False)

    first = periodic_mesh_gate._owned_model_path(tmp_path, lease_acquired=True)
    second = periodic_mesh_gate._owned_model_path(tmp_path, lease_acquired=True)

    assert first != second
    assert first.parent == second.parent == tmp_path


def test_periodic_mesh_negative_probe_uses_exact_audited_copyface_identity():
    audit = {
        "group_recipes": [
            {
                "group_id": "periodic-y",
                "mesh_recipe_present": True,
                "copy_face_tag": "copy-y",
                "matching_copyface_tags": ["copy-y"],
            },
            {
                "group_id": "periodic-x",
                "mesh_recipe_present": True,
                "copy_face_tag": "copy-x",
                "matching_copyface_tags": ["copy-x"],
            },
        ]
    }

    assert periodic_mesh_gate._negative_copyface_recipe(audit) == ("periodic-x", "copy-x")
    audit["group_recipes"][0]["matching_copyface_tags"] = ["copy-y", "other"]
    with pytest.raises(AssertionError, match="exactly one CopyFace"):
        periodic_mesh_gate._negative_copyface_recipe(audit)


def test_periodic_mesh_cleanup_fails_if_a_loaded_model_cannot_be_removed(tmp_path):
    broken_path = tmp_path / "derived.mph"
    broken_path.write_bytes(b"derived")
    broken = object()
    source = object()

    class Client:
        @staticmethod
        def remove(model):
            if model is broken:
                raise RuntimeError("injected model retention")

        @staticmethod
        def clear():
            return None

    cleanup = periodic_mesh_gate._cleanup_periodic_session(
        Client(),
        {"broken": broken, "source": source},
        broken_path,
    )

    assert cleanup["passed"] is False
    assert cleanup["model_removals"] == {"broken": False, "source": True}
    assert cleanup["derived_file_removed"] is True
    assert cleanup["errors"] == [{"stage": "remove_broken", "type": "RuntimeError"}]


def test_point_audit_uses_caller_bound_source_hash(tmp_path):
    source = tmp_path / "source.mph"
    source.write_bytes(b"source")
    expected = __import__("hashlib").sha256(source.read_bytes()).hexdigest()

    assert (
        point_gate._bound_source_sha256({"source": source, "expected_source_sha256": expected})
        == expected
    )
    source.write_bytes(b"changed")
    with pytest.raises(AssertionError, match="caller-bound identity"):
        point_gate._bound_source_sha256({"source": source, "expected_source_sha256": expected})


def test_point_audit_requires_exact_component_physics_and_study_tags():
    class Container:
        @staticmethod
        def tags():
            return ["other"]

    with pytest.raises(ValueError, match="required clientapi tag is absent: ewfd"):
        point_gate._first_tag(Container(), "ewfd")


def test_point_audit_requires_an_identifiable_wavelength_step():
    class Feature:
        def __init__(self, kind=None, error=None):
            self.kind = kind
            self.error = error

        def getType(self):
            if self.error is not None:
                raise self.error
            return self.kind

    class Features:
        def __init__(self, values):
            self.values = values

        def tags(self):
            return list(self.values)

        def get(self, tag):
            return self.values[tag]

    class Study:
        def __init__(self, values):
            self.values = values

        def feature(self):
            return Features(self.values)

    assert (
        point_gate._study_step(Study({"freq": Feature("Frequency"), "wave": Feature("Wavelength")}))
        == "wave"
    )
    with pytest.raises(ValueError, match="no identifiable Wavelength step"):
        point_gate._study_step(
            Study({"freq": Feature("Frequency"), "broken": Feature(error=OSError())})
        )


def test_preflight_fixture_prerequisites_fail_with_explicit_errors():
    with pytest.raises(ValueError, match="no component"):
        preflight_gate._select_preflight_tags([], [], [])
    with pytest.raises(ValueError, match="no physics"):
        preflight_gate._select_preflight_tags(["comp1"], [], [])

    assert preflight_gate._select_preflight_tags(
        ["other", "comp1"], ["emw", "ewfd"], ["other", "std1"]
    ) == ("comp1", "ewfd", "std1")
