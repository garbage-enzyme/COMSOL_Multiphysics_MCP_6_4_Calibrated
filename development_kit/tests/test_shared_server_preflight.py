"""Solver-free local Desktop/Server state-classification tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.shared_session.preflight import classify_shared_server_preflight


def _process(
    pid,
    kind,
    *,
    version="6.4.0.293",
    windows=0,
    responding=True,
    created=None,
):
    return {
        "pid": pid,
        "parent_pid": 0,
        "kind": kind,
        "create_time": float(created if created is not None else pid),
        "command_signature": f"{pid:064x}",
        "file_version": version,
        "window_count": windows,
        "responding": responding,
    }


def _snapshot(processes=(), listeners=(), observed=1000.0):
    return {
        "inventory_complete": True,
        "observed_at_epoch": observed,
        "processes": list(processes),
        "listeners": list(listeners),
    }


def _listener(pid=20):
    return {"host": "127.0.0.1", "port": 2036, "pid": pid}


def _classify(first, second=None):
    return classify_shared_server_preflight(
        endpoint={"host": "127.0.0.1", "port": 2036},
        first_probe=first,
        second_probe=deepcopy(first) if second is None else second,
    )


def _ready(*, desktop_version="6.4.0.293", server_version="6.4.0.293"):
    return _snapshot(
        [
            _process(10, "comsol_desktop", version=desktop_version, windows=1),
            _process(20, "comsol_server", version=server_version),
        ],
        [_listener()],
    )


def test_exact_release_line_is_ready_without_paths_or_mph():
    result = _classify(_ready())

    assert result["success"] is True
    assert result["state"] == "ready_for_attach"
    assert result["accepted_release_line"] == "6.4.0.*"
    assert result["warnings"] == []
    assert result["paths_included"] is False
    assert result["mph_imported"] is False
    assert result["client_constructed"] is False
    assert result["lease_acquired"] is False
    assert all("pid" not in item for item in result["processes"])


def test_final_build_difference_is_accepted_and_reported():
    result = _classify(
        _ready(desktop_version="6.4.0.310", server_version="6.4.0.293")
    )

    assert result["success"] is True
    assert result["warnings"] == ["same_accepted_release_line_build_difference"]


@pytest.mark.parametrize("version", ["6.3.0.405", "6.4.1.12", "6.5.0.1"])
def test_other_release_lines_are_rejected(version):
    result = _classify(_ready(desktop_version=version))

    assert result["success"] is False
    assert result["state"] == "unsupported_or_ambiguous_comsol_version"


@pytest.mark.parametrize(
    ("snapshot", "state", "retryable"),
    [
        (_snapshot(), "desktop_and_server_absent", True),
        (
            _snapshot([_process(20, "comsol_server")], [_listener()]),
            "desktop_absent",
            True,
        ),
        (
            _snapshot([_process(10, "comsol_desktop", windows=0)]),
            "desktop_or_server_starting",
            True,
        ),
        (
            _snapshot([
                _process(10, "comsol_desktop", windows=1),
                _process(11, "comsol_desktop", windows=1),
                _process(20, "comsol_server"),
            ], [_listener()]),
            "ambiguous_gui_clients",
            False,
        ),
    ],
)
def test_absent_starting_and_multiple_gui_states(snapshot, state, retryable):
    result = _classify(snapshot)

    assert result["state"] == state
    assert result["retryable"] is retryable


def test_changed_listener_owner_is_rejected():
    first = _ready()
    second = _snapshot(
        [
            _process(10, "comsol_desktop", windows=1),
            _process(20, "comsol_server"),
            _process(21, "comsol_server"),
        ],
        [_listener(21)],
        observed=1001.0,
    )

    result = _classify(first, second)

    assert result["state"] in {
        "process_identity_changed_between_probes",
        "listener_owner_changed_between_probes",
    }
    assert result["success"] is False


def test_pid_reuse_between_probes_is_rejected():
    first = _ready()
    second = _ready()
    second["observed_at_epoch"] = 1001.0
    second["processes"][1]["create_time"] = 9999.0

    result = _classify(first, second)

    assert result["state"] == "process_identity_changed_between_probes"


def test_unclassified_mph_collision_is_rejected():
    snapshot = _ready()
    snapshot["processes"].append(_process(30, "mph_client"))

    assert _classify(snapshot)["state"] == "unclassified_comsol_or_mph_collision"


def test_incomplete_inventory_fails_and_unreadable_version_is_classified():
    incomplete = _ready()
    incomplete["inventory_complete"] = False
    unreadable = _ready()
    unreadable["processes"][0]["file_version"] = "unknown"

    with pytest.raises(ValueError, match="complete"):
        _classify(incomplete)
    assert _classify(unreadable)["state"] == (
        "unsupported_or_ambiguous_comsol_version"
    )
