"""S4A direct-call inventory enforcement.

The canonical 0.7.5 gate requires "an adapter inventory with a disposition for
every direct call".  A prose inventory cannot fail, so these tests make the
ledger executable: they compare the declaration in
:mod:`comsol_mcp.adapter.migration_inventory` against a fresh scan of the tree,
and they check that every rule the ledger relies on actually fires.

The rules are also exercised against synthetic trees, because a rule that is
only ever run against the real ledger cannot be told apart from a rule that
never runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comsol_mcp.adapter.migration_inventory import (
    BACKEND_BOUNDARY_PREFIX,
    CLIENTAPI_ACCESSORS,
    DISPOSITIONS,
    EXCLUDED_PREFIXES,
    LICENSED_PROBE_PREFIX,
    MEASURES,
    ModuleDisposition,
    declared_inventory,
    inventory_status,
    measure_module_sites,
    measure_repository,
)

REPO = Path(__file__).parents[2]
DNN_BRIDGE = "comsol_mcp/surrogate/dnn_clientapi_backend.py"


def _status() -> dict:
    return inventory_status(REPO)


# ---------------------------------------------------------------------------
# The ledger covers the tree
# ---------------------------------------------------------------------------


def test_every_measured_direct_site_has_a_declared_disposition() -> None:
    """The gate's core requirement, checked rather than asserted."""
    status = _status()
    assert status["violations"] == [], "\n".join(status["violations"])
    # Guard against a vacuous pass: the scan must actually have found something.
    assert status["measured_module_count"] > 0
    assert status["measured_site_count"] > 0
    assert status["deferred_count"] > 0


def test_the_declared_dispositions_are_exactly_the_four_the_handoff_permits() -> None:
    assert DISPOSITIONS == (
        "migrate_behind_protocol",
        "retain_at_backend_boundary",
        "retain_in_licensed_probe",
        "defer_with_written_reason",
    )
    assert {entry.disposition for entry in declared_inventory()} <= set(DISPOSITIONS)


def test_the_ledger_is_not_a_public_schema() -> None:
    """The status dict is a developer ledger, not an emitted evidence schema.

    Declaring a ``comsol_mcp.*`` ``schema_name`` here would add an entry to the
    frozen public schema registry for a document the MCP surface never emits.
    """
    status = _status()
    assert "schema_name" not in status
    assert "schema_version" not in status
    assert status["ledger"] == "comsol_mcp.s4a_migration_inventory"


def test_every_declared_module_is_named_once_and_gives_a_reason() -> None:
    inventory = declared_inventory()
    modules = [entry.module for entry in inventory]
    assert len(modules) == len(set(modules)), "a module is declared twice"
    for entry in inventory:
        assert entry.reason.strip(), f"{entry.module} has no reason"
        if entry.disposition == "defer_with_written_reason":
            assert entry.target_slice, f"{entry.module} is deferred with no owning slice"


def test_the_backend_boundary_claim_is_confined_to_the_adapter_package() -> None:
    for entry in declared_inventory():
        if entry.disposition == "retain_at_backend_boundary":
            assert entry.module.startswith(BACKEND_BOUNDARY_PREFIX), entry.module


def test_the_scan_covers_the_runtime_and_the_licensed_probes_but_not_test_doubles() -> None:
    """The scope rule is explicit: test doubles are not direct calls."""
    measured = measure_repository(REPO)
    assert any(module.startswith("comsol_mcp/") for module in measured)
    assert any(
        module.startswith("development_kit/scripts/")
        or module.startswith("development_kit/tests/integration/")
        for module in measured
    )
    assert EXCLUDED_PREFIXES == ("development_kit/tests/",)
    for module in measured:
        assert not module.startswith(EXCLUDED_PREFIXES), module


# ---------------------------------------------------------------------------
# The migration claim the gate names
# ---------------------------------------------------------------------------


def test_the_dnn_bridge_is_declared_migrated_and_measures_nothing() -> None:
    """S4A step 6, the finding this repair exists for."""
    entry = next(item for item in declared_inventory() if item.module == DNN_BRIDGE)
    assert entry.disposition == "migrate_behind_protocol"
    assert entry.zero_site_reason
    assert DNN_BRIDGE not in measure_repository(REPO)


def test_the_measure_detects_the_direct_access_the_migration_removed() -> None:
    """Red-before-green in test form.

    Each snippet is a characteristic line of the pre-migration bridge measured
    from the module as it stood at ``0d5918b``. If a future change reintroduced
    any of them, the corresponding measure would rise and the ledger rule for
    ``migrate_behind_protocol`` would fail.
    """
    samples = {
        "java_bridge": "def get_dnn_function(self, tag):\n    return self.model.java.func(tag)\n",
        "jpype_import": "def _coerce(value):\n    import jpype\n    return jpype.JString(value)\n",
        "mph_import": "def study_tags(self):\n    import mph\n    return mph.Client()\n",
        "clientapi_accessor_call": (
            "def read(feature):\n    return feature.getStringArray('activation')\n"
        ),
    }
    for measure, source in samples.items():
        counts = measure_module_sites(source)
        assert counts[measure] >= 1, (measure, counts)
    assert set(samples) <= set(MEASURES)

    # And the shipped bridge contains none of them.
    counts = measure_module_sites((REPO / DNN_BRIDGE).read_text(encoding="utf-8"))
    assert counts == dict.fromkeys(MEASURES, 0), counts


def test_the_accessor_measure_names_only_clientapi_only_methods() -> None:
    """A noisy accessor list would make the measure meaningless."""
    assert "setEntry" in CLIENTAPI_ACCESSORS
    assert "getAllowedPropertyValues" in CLIENTAPI_ACCESSORS
    # Generic names are excluded on purpose: they match unrelated objects.
    for generic in ("set", "get", "run", "export", "create", "remove", "tags"):
        assert generic not in CLIENTAPI_ACCESSORS


# ---------------------------------------------------------------------------
# Each rule fires, proven against a synthetic tree
# ---------------------------------------------------------------------------


def _tree(tmp_path: Path, modules: dict[str, str]) -> Path:
    for relative, source in modules.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return tmp_path


DIRECT_SOURCE = "def f(model):\n    return model.java.study('std1')\n"


def test_a_direct_module_with_no_disposition_is_a_violation(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"comsol_mcp/orphan.py": DIRECT_SOURCE})
    status = inventory_status(root, inventory=())
    assert any("no declared disposition" in item for item in status["violations"])


def test_a_migrated_module_that_regained_direct_access_is_a_violation(tmp_path: Path) -> None:
    """The rule that would have caught the scheduler finding."""
    root = _tree(tmp_path, {"comsol_mcp/adapter/migrated.py": DIRECT_SOURCE})
    ledger = (
        ModuleDisposition(
            module="comsol_mcp/adapter/migrated.py",
            disposition="migrate_behind_protocol",
            reason="declared migrated for this test",
            zero_site_reason="verified zero at declaration time",
        ),
    )
    status = inventory_status(root, inventory=ledger)
    assert any("claims to be migrated but still measures" in item for item in status["violations"])


def test_a_stale_deferral_for_a_module_with_no_sites_is_a_violation(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"comsol_mcp/clean.py": "def f():\n    return 1\n"})
    ledger = (
        ModuleDisposition(
            module="comsol_mcp/clean.py",
            disposition="defer_with_written_reason",
            reason="declared deferred for this test",
            target_slice="S4A step 2",
        ),
    )
    status = inventory_status(root, inventory=ledger)
    assert any("measures no direct site" in item for item in status["violations"])


def test_a_probe_claim_outside_the_probe_roots_is_a_violation(tmp_path: Path) -> None:
    """Packaged runtime code may not call itself an opt-in probe."""
    root = _tree(tmp_path, {"comsol_mcp/tools/fake_probe.py": DIRECT_SOURCE})
    ledger = (
        ModuleDisposition(
            module="comsol_mcp/tools/fake_probe.py",
            disposition="retain_in_licensed_probe",
            reason="claims to be a probe",
        ),
    )
    status = inventory_status(root, inventory=ledger)
    assert any("packaged code" in item for item in status["violations"])


def test_a_boundary_claim_outside_the_adapter_package_is_a_violation(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"comsol_mcp/tools/not_boundary.py": DIRECT_SOURCE})
    ledger = (
        ModuleDisposition(
            module="comsol_mcp/tools/not_boundary.py",
            disposition="retain_at_backend_boundary",
            reason="claims to be the boundary",
        ),
    )
    status = inventory_status(root, inventory=ledger)
    assert any("outside it" in item for item in status["violations"])


def test_a_deferral_without_a_target_slice_is_a_violation(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"comsol_mcp/later.py": DIRECT_SOURCE})
    ledger = (
        ModuleDisposition(
            module="comsol_mcp/later.py",
            disposition="defer_with_written_reason",
            reason="deferred, but nobody owns it",
        ),
    )
    status = inventory_status(root, inventory=ledger)
    assert any("no target slice" in item for item in status["violations"])


def test_a_declared_probe_prefix_covers_the_probe_modules(tmp_path: Path) -> None:
    """A probe directory is declared once rather than file by file."""
    root = _tree(tmp_path, {"development_kit/scripts/gate_a.py": DIRECT_SOURCE})
    ledger = (
        ModuleDisposition(
            module="development_kit/scripts",
            disposition="retain_in_licensed_probe",
            reason="opt-in licensed gate",
        ),
    )
    status = inventory_status(root, inventory=ledger)
    assert status["violations"] == []
    assert status["measured_module_count"] == 1


def test_the_excluded_test_tree_is_accepted_as_a_declared_probe() -> None:
    """The probe prefix for integration tests is inside the licensed prefix."""
    assert "development_kit/tests/integration".startswith(LICENSED_PROBE_PREFIX)
    assert not EXCLUDED_PREFIXES[0].startswith("development_kit/tests/integration")


@pytest.mark.parametrize("disposition", DISPOSITIONS)
def test_every_permitted_disposition_is_representable(disposition: str) -> None:
    """All four handoff dispositions must be usable, not just documented."""
    entry = ModuleDisposition(
        module="comsol_mcp/example.py",
        disposition=disposition,
        reason="representability check",
        target_slice="S4A step 1" if disposition == "defer_with_written_reason" else None,
    )
    assert entry.disposition in DISPOSITIONS
