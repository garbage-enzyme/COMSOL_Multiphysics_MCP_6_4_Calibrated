"""Solver-free tests for the read-only PEDOT material ownership probe."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from development_kit.scripts import pedot_material_ownership_probe as probe


@pytest.fixture
def gate_root(tmp_path: Path):
    root = (
        Path("D:/mcp_tests") / f"p{uuid.uuid4().hex[:8]}" if os.name == "nt" else tmp_path / "probe"
    )
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _spec(tmp_path: Path, gate_root: Path, monkeypatch):
    source = tmp_path / "source.mph"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(probe.os, "cpu_count", lambda: 4)
    args = probe._parser().parse_args(
        [
            "--test-root",
            str(gate_root),
            "--source-model",
            str(source),
            "--source-sha256",
            hashlib.sha256(source.read_bytes()).hexdigest(),
            "--component-tag",
            "comp1",
            "--patch-domain",
            "3",
            "--cores",
            "3",
        ]
    )
    return probe._spec(args)


def test_dry_run_binds_source_domain_and_cores_without_solver(tmp_path, gate_root, monkeypatch):
    spec = _spec(tmp_path, gate_root, monkeypatch)
    receipt = probe._dry_run(spec)
    assert receipt["patch_domain"] == 3
    assert receipt["requested_cores"] == 3
    assert receipt["solver_started"] is False
    assert receipt["paths_included"] is False


def test_run_refuses_source_replaced_after_spec_validation(
    tmp_path: Path, gate_root: Path, monkeypatch
):
    spec = _spec(tmp_path, gate_root, monkeypatch)
    real_sha = probe._sha
    calls = {"count": 0}

    def staged_sha(_path):
        calls["count"] += 1
        if calls["count"] == 1:
            return "b" * 64  # baseline hash of an unapproved replacement file
        return real_sha(_path)

    monkeypatch.setattr(probe, "_sha", staged_sha)
    monkeypatch.setattr(probe, "_git_identity", lambda: {"revision": "a" * 40, "clean": True})
    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace())

    with pytest.raises(RuntimeError, match="changed after specification validation"):
        probe._run(spec)

    assert calls["count"] == 1


def test_inventory_identifies_unique_patch_owner_and_bounds_property_readback():
    class Selection:
        def entities(self):
            return [3]

    class Group:
        def getStringArray(self, name):
            if name == "relpermittivity":
                return ["2", "2", "3"]
            raise RuntimeError(name)

        def getString(self, _name):
            return ""

    class Groups:
        def tags(self):
            return ["def"]

        def get(self, _tag):
            return Group()

    class Material:
        def selection(self):
            return Selection()

        def propertyGroup(self):
            return Groups()

        def label(self):
            return "Patch material"

    class Materials:
        def __init__(self, tags):
            self._tags = tags

        def tags(self):
            return self._tags

        def get(self, _tag):
            return Material()

    class Component:
        def material(self):
            return Materials(["mat_patch"])

    class Java:
        def material(self):
            return Materials([])

        def component(self, tag):
            assert tag == "comp1"
            return Component()

    inventory = probe._inventory(
        SimpleNamespace(java=Java()), component_tag="comp1", patch_domain=3
    )
    assert inventory["patch_domain_owners"] == [
        {"scope": "component", "tag": "mat_patch", "label": "Patch material"}
    ]
    assert inventory["ownership_disposition"] == "unique"
    assert inventory["materials"][0]["property_groups"][0]["properties"] == {
        "relpermittivity": ["2", "2", "3"]
    }


def _single_material_java(selection, entities):
    class Selection:
        def entities(self):
            if isinstance(selection, Exception):
                raise selection
            return selection

    class Material:
        def selection(self):
            return Selection()

        def propertyGroup(self):
            raise RuntimeError("no groups")

        def label(self):
            return "M"

    class Materials:
        def tags(self):
            return ["mat_a"]

        def get(self, _tag):
            return Material()

    class Component:
        def material(self):
            return Materials()

    class Java:
        def material(self):
            class Empty:
                def tags(self):
                    return []

                def get(self, _tag):
                    raise AssertionError("no global materials")

            return Empty()

        def component(self, tag):
            assert tag == "comp1"
            return Component()

    return SimpleNamespace(java=Java())


def test_inventory_reports_incomplete_evidence_when_selection_is_unreadable():
    model = _single_material_java(RuntimeError("selection unavailable"), None)

    inventory = probe._inventory(model, component_tag="comp1", patch_domain=3)

    # An unreadable selection can be neither confirmed owner nor confirmed
    # non-owner; the disposition must surface the unknown state.
    assert inventory["ownership_disposition"] == "incomplete_evidence"
    assert inventory["unresolved_materials"] == [{"scope": "component", "tag": "mat_a"}]
    assert inventory["owner_count"] == 0


def test_inventory_decides_on_full_domains_and_flags_display_truncation():
    entities = [3] + list(range(10, 5100))
    model = _single_material_java(entities, None)

    inventory = probe._inventory(model, component_tag="comp1", patch_domain=3)

    # Ownership is decided on the full selection even though the published
    # domain list is bounded; truncation must downgrade the disposition.
    assert inventory["patch_domain_owners"] == [
        {"scope": "component", "tag": "mat_a", "label": "M"}
    ]
    material = inventory["materials"][0]
    assert material["domains_total"] == len(entities)
    assert material["domains_truncated"] is True
    assert material["domains"] == entities[:4096]
    assert inventory["ownership_disposition"] == "incomplete_evidence"


def test_runtime_failure_redacts_source_and_releases_ownership(tmp_path, gate_root, monkeypatch):
    spec = _spec(tmp_path, gate_root, monkeypatch)
    events = []

    class Ownership:
        def acquire(self, **kwargs):
            events.append(("acquire", kwargs))
            return {"success": True, "acquired": True}

        def heartbeat(self, **kwargs):
            events.append(("heartbeat", kwargs))
            return True

        def release(self):
            events.append(("release", {}))
            return {"success": True, "released": True}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def load(self, _path):
            raise RuntimeError("private source detail")

        def clear(self):
            events.append(("clear", {}))

    monkeypatch.setattr(probe, "_git_identity", lambda: {"revision": "a" * 40, "clean": True})
    monkeypatch.setattr(probe, "SolverOwnership", Ownership)
    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace(Client=Client))
    receipt, private = probe._run(spec)
    assert receipt["success"] is False
    assert receipt["error"] == {
        "code": "material_ownership_probe_failed",
        "type": "RuntimeError",
    }
    assert str(spec["source"]) not in str(receipt)
    assert "private source detail" in private["error"]
    assert receipt["cleanup"] == {
        "model_removed": True,
        "client_clear": True,
        "lease_released": True,
        "source_unchanged": True,
    }
