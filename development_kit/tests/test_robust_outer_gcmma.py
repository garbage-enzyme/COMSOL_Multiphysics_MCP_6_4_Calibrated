"""Solver-free durable outer-loop GCMMA contract tests."""

from __future__ import annotations

import copy

import numpy as np
import pytest

from comsol_mcp.research.robust_outer_gcmma import (
    accept_gcmma_candidate,
    create_gcmma_state,
    normalize_gcmma_state,
    propose_gcmma_candidate,
)


def _identity() -> dict:
    return {
        "package_name": "mmapy",
        "package_version": "0.3.1",
        "distribution_license": "GPL-3.0-or-later",
        "distribution_sha256": "4" * 64,
        "distribution_path": "D:\\mcp_tests\\mmapy.whl",
    }


def _state(**overrides) -> dict:
    values = {
        "variable_ids": ["radius_x", "radius_y"],
        "lower_bounds": [200.0, 200.0],
        "upper_bounds": [320.0, 320.0],
        "initial_values": [260.0, 260.0],
        "move_limit": 0.1,
        "max_iterations": 3,
        "max_inner_iterations": 4,
        "max_condition_solves": 200,
        "backend_identity": _identity(),
    }
    values.update(overrides)
    return create_gcmma_state(**values)


def _backend() -> dict:
    def asymp(
        outer,
        n,
        xval,
        xold1,
        xold2,
        xmin,
        xmax,
        low,
        upp,
        raa0,
        raa,
        raa0eps,
        raaeps,
        df0dx,
        dfdx,
    ):
        del outer, n, xold1, xold2, xmin, xmax, raa0eps, raaeps, df0dx, dfdx
        return low, upp, np.asarray([[float(np.asarray(raa0).reshape(-1)[0])]]), raa

    def gcmmasub(
        m,
        n,
        outer,
        epsimin,
        xval,
        xmin,
        xmax,
        low,
        upp,
        raa0,
        raa,
        f0val,
        df0dx,
        fval,
        dfdx,
        a0,
        a,
        c,
        d,
    ):
        del m, outer, epsimin, low, upp, raa0, raa, fval, dfdx, a0, a, c, d
        direction = np.sign(-df0dx)
        candidate = np.minimum(xmax, np.maximum(xmin, xval + 0.05 * direction))
        empty = np.empty((0, 1))
        return (
            candidate,
            empty,
            0.0,
            empty,
            np.ones((n, 1)),
            np.ones((n, 1)),
            empty,
            0.0,
            empty,
            np.asarray([[float(f0val.reshape(-1)[0]) + 0.01]]),
            empty,
        )

    def concheck(m, epsimin, f0app, f0new, fapp, fnew):
        del m, epsimin, fapp, fnew
        return int(float(f0app.reshape(-1)[0]) >= float(f0new.reshape(-1)[0]))

    def raaupdate(
        xmma,
        xval,
        xmin,
        xmax,
        low,
        upp,
        f0new,
        fnew,
        f0app,
        fapp,
        raa0,
        raa,
        raa0eps,
        raaeps,
        epsimin,
    ):
        del xmma, xval, xmin, xmax, low, upp, f0new, fnew, f0app, fapp
        del raa0eps, raaeps, epsimin
        return np.asarray(raa0) * 2.0, raa

    return {
        "asymp": asymp,
        "gcmmasub": gcmmasub,
        "concheck": concheck,
        "raaupdate": raaupdate,
    }


def test_state_is_fingerprint_bound_and_round_trips():
    state = _state()
    assert normalize_gcmma_state(state) == state
    tampered = copy.deepcopy(state)
    tampered["move_limit"] = 0.2
    with pytest.raises(ValueError, match="fingerprint"):
        normalize_gcmma_state(tampered)


def test_proposal_enforces_physical_move_limit_and_accepts_fresh_forward_improvement():
    state = propose_gcmma_candidate(
        _state(),
        objective=0.2,
        gradient=[1e-3, 2e-3],
        condition_solves=48,
        backend=_backend(),
    )
    proposal = state["pending_proposal"]
    assert proposal["physical_values"] == pytest.approx([266.0, 266.0])
    assert max(abs(value - 260.0) / 120.0 for value in proposal["physical_values"]) <= 0.1
    accepted = accept_gcmma_candidate(
        state,
        candidate_objective=0.21,
        condition_solves=24,
        backend=_backend(),
    )
    assert accepted["outer_iteration"] == 1
    assert accepted["current_values"] == pytest.approx([266.0, 266.0])
    assert accepted["condition_solves_used"] == 72
    assert accepted["status"] == "accepted"


def test_nonconservative_candidate_creates_visible_revised_inner_proposal():
    state = propose_gcmma_candidate(
        _state(),
        objective=0.2,
        gradient=[1e-3, 2e-3],
        condition_solves=48,
        backend=_backend(),
    )
    first = state["pending_proposal"]["proposal_fingerprint"]
    revised = accept_gcmma_candidate(
        state,
        candidate_objective=0.1,
        condition_solves=24,
        backend=_backend(),
    )
    assert revised["status"] == "proposal_pending"
    assert revised["inner_iteration"] == 1
    assert revised["pending_proposal"]["revised_from_proposal_fingerprint"] == first
    assert revised["raa0"] > state["raa0"]


def test_condition_solve_and_iteration_budgets_fail_closed():
    with pytest.raises(ValueError, match="condition solve budget"):
        propose_gcmma_candidate(
            _state(max_condition_solves=47),
            objective=0.2,
            gradient=[1e-3, 2e-3],
            condition_solves=48,
            backend=_backend(),
        )
    exhausted = _state()
    exhausted = {**exhausted, "outer_iteration": 3}
    exhausted.pop("state_fingerprint")
    from comsol_mcp.durable import domain_sha256_v2

    exhausted["state_fingerprint"] = domain_sha256_v2(
        "comsol_mcp.robust_outer_gcmma_state", exhausted
    )
    with pytest.raises(ValueError, match="iteration budget"):
        propose_gcmma_candidate(
            exhausted,
            objective=0.2,
            gradient=[1e-3, 2e-3],
            condition_solves=48,
            backend=_backend(),
        )
