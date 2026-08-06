"""Named artifact schema registry and support-resolution tests."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from src.schema_registry import check_schema_support, get_schema_registry
from src.tools.capabilities import get_capabilities
from src.tools.profiles import ProfileSelection

from src import __version__

ROOT = Path(__file__).parents[2]


def _resolve_string(node: ast.AST, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve_string(node.left, constants)
        right = _resolve_string(node.right, constants)
        return left + right if left is not None and right is not None else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "replace"
        and len(node.args) == 2
    ):
        value = _resolve_string(node.func.value, constants)
        old = _resolve_string(node.args[0], constants)
        new = _resolve_string(node.args[1], constants)
        if value is not None and old is not None and new is not None:
            return value.replace(old, new)
    return None


def _module_string_constants(
    tree: ast.Module, seed: dict[str, str] | None = None
) -> dict[str, str]:
    constants = dict(seed or {})
    pending = list(tree.body)
    for _pass in range(len(pending) + 1):
        changed = False
        for node in pending:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    value = _resolve_string(node.value, constants)
                    if value is not None and constants.get(target.id) != value:
                        constants[target.id] = value
                        changed = True
                elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(
                    node.value, (ast.Tuple, ast.List)
                ):
                    for name, value_node in zip(target.elts, node.value.elts, strict=True):
                        value = _resolve_string(value_node, constants)
                        if isinstance(name, ast.Name) and value is not None:
                            if constants.get(name.id) != value:
                                constants[name.id] = value
                                changed = True
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                value = _resolve_string(node.value, constants) if node.value is not None else None
                if value is not None and constants.get(node.target.id) != value:
                    constants[node.target.id] = value
                    changed = True
        if not changed:
            break
    return constants


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _imported_string_constants(
    module_name: str,
    tree: ast.Module,
    constants_by_module: dict[str, dict[str, str]],
) -> dict[str, str]:
    imported: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if node.level:
            package = module_name.split(".")[: -node.level]
            target_module = ".".join([*package, node.module])
        else:
            target_module = node.module
        target_constants = constants_by_module.get(target_module, {})
        for alias in node.names:
            value = target_constants.get(alias.name)
            if value is not None:
                imported[alias.asname or alias.name] = value
    return imported


def _emitted_schemas_in_source() -> set[str]:
    trees = {
        _module_name(path): ast.parse(path.read_text(encoding="utf-8"))
        for path in (ROOT / "comsol_mcp").rglob("*.py")
    }
    constants_by_module = {
        module_name: _module_string_constants(tree) for module_name, tree in trees.items()
    }
    for _pass in range(len(trees) + 1):
        changed = False
        for module_name, tree in trees.items():
            imported = _imported_string_constants(module_name, tree, constants_by_module)
            resolved = _module_string_constants(tree, imported)
            if resolved != constants_by_module[module_name]:
                constants_by_module[module_name] = resolved
                changed = True
        if not changed:
            break

    names: set[str] = set()
    for module_name, tree in trees.items():
        constants = constants_by_module[module_name]
        for node in ast.walk(tree):
            candidates: list[ast.AST] = []
            if isinstance(node, ast.Dict):
                candidates.extend(
                    value
                    for key, value in zip(node.keys, node.values, strict=True)
                    if isinstance(key, ast.Constant) and key.value == "schema_name"
                )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "dict"
            ):
                candidates.extend(
                    keyword.value for keyword in node.keywords if keyword.arg == "schema_name"
                )
            for candidate in candidates:
                value = _resolve_string(candidate, constants)
                if value is not None and value.startswith("comsol_mcp."):
                    names.add(value)
    return names


def _selection() -> ProfileSelection:
    return ProfileSelection(
        name="core",
        source="schema-registry-test",
        environment_variable="COMSOL_MCP_PROFILE",
        default_used=False,
    )


def test_registry_is_complete_sorted_and_snapshot_stable():
    registry = get_schema_registry()
    entries = registry["entries"]
    names = [item["schema_name"] for item in entries]

    assert registry["schema_name"] == "comsol_mcp.schema_registry"
    assert registry["schema_version"] == "1.0.0"
    assert registry["producer"] == {"package": "comsol-mcp", "version": __version__}
    # These are deliberate public release snapshots. A registry change updates
    # both literals and development_kit/release/release_facts.json together.
    assert registry["entry_count"] == len(entries) == 88
    assert names == sorted(names)
    assert len(names) == len(set(names))
    emitted = _emitted_schemas_in_source()
    assert emitted
    registry_only = {
        "comsol_mcp.cleanup_outcome",
        "comsol_mcp.execution_evidence_outcome",
        "comsol_mcp.h1_licensed_gate",
        "comsol_mcp.portfolio_evidence_request",
        "comsol_mcp.runtime_compatibility",
        "comsol_mcp.simulation_configuration",
        "comsol_mcp.standalone_driver_event",
        "comsol_mcp.standalone_licensed_acceptance",
        "comsol_mcp.standalone_owner",
        "comsol_mcp.standalone_pause_ack",
        "comsol_mcp.standalone_pause_request",
        "comsol_mcp.standalone_status",
        "comsol_mcp.standalone_terminal",
        "comsol_mcp.thermal_material_ledger",
        "comsol_mcp.thermal_radiation_request",
        "comsol_mcp.thermo_optomechanical_replay_manifest",
        "comsol_mcp.wave_optics_point_audit",
    }
    assert set(names) == emitted | registry_only
    assert re.fullmatch(r"[0-9a-f]{64}", registry["registry_sha256"])
    assert registry["registry_sha256"] == (
        "ba051dd6d85d925e26dcb1760048b9b44946db3d541637f3e006147bebcab576"
    )
    assert registry["registry_sha256"] == get_schema_registry()["registry_sha256"]
    assert check_schema_support("comsol_mcp.session_startup_state", "1.0.0")["supported"] is True


def test_every_entry_declares_read_write_and_non_mutating_migration_policy():
    for entry in get_schema_registry()["entries"]:
        assert entry["producer_version"] == __version__
        assert entry["readable_versions"]
        assert len(entry["readable_versions"]) == len(set(entry["readable_versions"]))
        assert (
            entry["writable_version"] is None
            or entry["writable_version"] in entry["readable_versions"]
        )
        assert entry["migration"]["rewrites_source_in_place"] is False
        assert entry["migration"]["available"] == bool(entry["migration"]["source_schema_names"])
    physical = next(
        item
        for item in get_schema_registry()["entries"]
        if item["schema_name"] == "comsol_mcp.physical_evidence"
    )
    assert physical["readable_versions"] == ["1.0.0", "1.1.0"]
    assert physical["writable_version"] == "1.1.0"
    deployment = next(
        item
        for item in get_schema_registry()["entries"]
        if item["schema_name"] == "comsol_mcp.deployment_identity"
    )
    assert deployment["readable_versions"] == ["1.0.0", "1.1.0", "1.2.0"]
    assert deployment["writable_version"] == "1.2.0"
    path_policy = next(
        item
        for item in get_schema_registry()["entries"]
        if item["schema_name"] == "comsol_mcp.path_policy"
    )
    assert path_policy["readable_versions"] == ["1.0.0", "1.1.0"]
    assert path_policy["writable_version"] == "1.1.0"


def test_support_resolution_accepts_current_and_rejects_future_without_mutation():
    registry_before = get_schema_registry()

    accepted = check_schema_support("comsol_mcp.physical_evidence", "1.0.0")
    future = check_schema_support("comsol_mcp.physical_evidence", "99.0.0")
    unknown = check_schema_support("comsol_mcp.unknown_artifact", "1.0.0")

    assert accepted["supported"] is True
    assert accepted["reason_code"] == "supported"
    assert future == {
        "supported": False,
        "reason_code": "unsupported_schema_version",
        "schema_name": "comsol_mcp.physical_evidence",
        "schema_version": "99.0.0",
        "supported_versions": ["1.0.0", "1.1.0"],
        "migration_available": False,
    }
    assert unknown == {
        "supported": False,
        "reason_code": "unknown_schema_name",
        "schema_name": "comsol_mcp.unknown_artifact",
        "schema_version": "1.0.0",
    }
    assert check_schema_support("comsol_mcp.wave_optics_point_audit", "1", for_write=True) == {
        "supported": False,
        "reason_code": "unsupported_schema_version",
        "schema_name": "comsol_mcp.wave_optics_point_audit",
        "schema_version": "1",
        "supported_versions": [],
        "migration_available": True,
    }
    assert (
        check_schema_support("comsol_mcp.wave_optics_point_audit", "99")["migration_available"]
        is False
    )
    registry_after = get_schema_registry()
    assert registry_after == registry_before
    assert registry_after is not registry_before


def test_capabilities_embed_the_complete_schema_registry():
    capabilities = get_capabilities(_selection())
    assert capabilities["schema_registry"] == get_schema_registry()


def test_every_advertised_capability_schema_pair_is_readable():
    capabilities = get_capabilities(_selection())
    pairs: list[tuple[str, str, str]] = []

    def collect(value: object, path: str = "capabilities") -> None:
        if isinstance(value, dict):
            schema_name = value.get("schema_name")
            schema_version = value.get("schema_version")
            if isinstance(schema_name, str) and isinstance(schema_version, str):
                pairs.append((path, schema_name, schema_version))
            for key, nested in value.items():
                collect(nested, f"{path}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                collect(nested, f"{path}[{index}]")

    collect(capabilities)
    assert pairs
    unsupported = [
        (path, schema_name, schema_version)
        for path, schema_name, schema_version in pairs
        if not check_schema_support(schema_name, schema_version)["supported"]
    ]
    assert unsupported == []
