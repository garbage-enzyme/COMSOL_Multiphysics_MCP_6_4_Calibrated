"""Host-neutral licensed acceptance resource contracts."""

import pytest

from development_kit.tests.integration import acceptance_resources


def test_acceptance_cores_are_caller_declared_and_live_bounded(monkeypatch):
    monkeypatch.setattr(acceptance_resources.os, "cpu_count", lambda: 16)

    assert acceptance_resources.required_acceptance_cores(
        {acceptance_resources.ACCEPTANCE_CORES_ENV: "14"}
    ) == 14
    with pytest.raises(RuntimeError, match="explicitly configured"):
        acceptance_resources.required_acceptance_cores({})
    with pytest.raises(ValueError, match="canonical positive integer"):
        acceptance_resources.required_acceptance_cores(
            {acceptance_resources.ACCEPTANCE_CORES_ENV: "01"}
        )
    with pytest.raises(ValueError, match="exceeds live host"):
        acceptance_resources.required_acceptance_cores(
            {acceptance_resources.ACCEPTANCE_CORES_ENV: "17"}
        )


def test_acceptance_cores_fail_closed_without_live_capacity(monkeypatch):
    monkeypatch.setattr(acceptance_resources.os, "cpu_count", lambda: None)

    with pytest.raises(RuntimeError, match="capacity is unavailable"):
        acceptance_resources.required_acceptance_cores(
            {acceptance_resources.ACCEPTANCE_CORES_ENV: "1"}
        )
