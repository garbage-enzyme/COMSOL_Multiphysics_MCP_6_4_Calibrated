"""S4A direct-call inventory and disposition ledger.

The canonical 0.7.5 gate requires two things that a prose inventory cannot
provide: a disposition for **every** direct MPh/ClientAPI call site, and a
disposition that can be checked rather than asserted.  This module is that
ledger.  It holds

* the measurement (what counts as a direct site, and where the scan looks), and
* the declaration (which disposition each measured module carries and why),

so a test can compare the two and fail when either drifts.

Scope
-----

The scan covers the packaged runtime (``comsol_mcp`` and the repository-only
``src`` compatibility layer) and the explicit licensed probes under
``development_kit/scripts`` and ``development_kit/tests/integration``.
``development_kit/tests`` is deliberately **excluded**: those modules define
test doubles such as a fake ``Model.java`` attribute, which is a stub rather
than a call into MPh, so counting them would inflate the ledger without
describing a real direct call.  The exclusion is a single documented rule
rather than a per-file allowance.

Dispositions
------------

Exactly the four the handoff permits:

``migrate_behind_protocol``
    The sites moved behind :class:`comsol_mcp.adapter.protocol.ComsolAdapter`.
    A module carrying this disposition must measure **zero** direct sites, so
    the claim cannot outlive the migration.
``retain_at_backend_boundary``
    The module *is* the seam, so direct access is its job.  Only modules inside
    the adapter package may carry this.
``retain_in_licensed_probe``
    The module is an opt-in licensed entry point that never ships in a wheel.
    Only modules under ``development_kit`` may carry this.
``defer_with_written_reason``
    Not migrated yet, with a written reason and the slice that owns it.  A
    deferral without both is not a disposition.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

#: The dispositions the handoff permits, in its own order.
DISPOSITIONS = (
    "migrate_behind_protocol",
    "retain_at_backend_boundary",
    "retain_in_licensed_probe",
    "defer_with_written_reason",
)

#: What counts as a direct MPh/ClientAPI site.  Each measure is AST-grounded, so
#: a mention in a docstring or a module name can never be counted as a call:
#:
#: ``mph_import``
#:     ``import mph`` / ``from mph ... import ...`` anywhere in the module,
#:     including inside a function.  A lazily imported ``mph`` is still a direct
#:     dependency on MPh.
#: ``jpype_import``
#:     The same for ``jpype``: importing it is reaching for the Java bridge.
#: ``java_bridge``
#:     An attribute access named ``java``, which is how MPh exposes the ClientAPI
#:     object.
#: ``clientapi_call``
#:     A *call* whose function name mentions ClientAPI, which is how a module
#:     that never spells ``.java`` still reaches the ClientAPI through helpers.
#: ``clientapi_accessor_call``
#:     A call to one of the ClientAPI-only property accessor names. This closes
#:     the one hole the other measures leave: a module that receives an
#:     already-resolved Java feature as a parameter never writes ``.java``, but
#:     it still has to call an accessor to use it.
MEASURES = (
    "mph_import",
    "jpype_import",
    "java_bridge",
    "clientapi_call",
    "clientapi_accessor_call",
)

#: ClientAPI-only accessor names. Each exists on the ``com.comsol.model``
#: ClientAPI surface and on nothing else in this project, so a matched call is a
#: real accessor call rather than a coincidental name.
CLIENTAPI_ACCESSORS = (
    "setEntry",
    "getEntryKeys",
    "getEntryKeyIndex",
    "getAllowedPropertyValues",
    "getValueType",
    "importData",
    "continueRun",
    "runTest",
    "discardData",
    "getStringArray",
    "getStringMatrix",
    "getDoubleArray",
    "getDoubleMatrix",
    "getBooleanArray",
    "getIntArray",
)

#: Roots scanned for packaged-runtime direct access.
RUNTIME_ROOTS = ("comsol_mcp", "src")

#: Roots whose modules are explicit, opt-in licensed probes.
PROBE_ROOTS = ("development_kit/scripts", "development_kit/tests/integration")

#: Repository-relative prefixes excluded from the scan, with the reason recorded
#: here rather than as a per-file allowance.
EXCLUDED_PREFIXES = ("development_kit/tests/",)

#: The only prefix that may claim ``retain_at_backend_boundary``.
BACKEND_BOUNDARY_PREFIX = "comsol_mcp/adapter/"

#: The only prefix that may claim ``retain_in_licensed_probe``.
LICENSED_PROBE_PREFIX = "development_kit/"


def measure_module_sites(source: str) -> dict[str, int]:
    """Count each direct-access measure in one module's source."""
    tree = ast.parse(source)
    counts = dict.fromkeys(MEASURES, 0)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "mph":
                    counts["mph_import"] += 1
                elif alias.name == "jpype":
                    counts["jpype_import"] += 1
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root == "mph":
                counts["mph_import"] += 1
            elif root == "jpype":
                counts["jpype_import"] += 1
        elif isinstance(node, ast.Attribute) and node.attr == "java":
            counts["java_bridge"] += 1
        elif isinstance(node, ast.Call):
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else ""
            if not name and isinstance(function, ast.Name):
                name = function.id
            if "clientapi" in name.lower():
                counts["clientapi_call"] += 1
            if name in CLIENTAPI_ACCESSORS:
                counts["clientapi_accessor_call"] += 1
    return counts


def _scanned_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for prefix in (*RUNTIME_ROOTS, *PROBE_ROOTS):
        base = root / prefix
        if not base.is_dir():
            continue
        files.extend(sorted(base.rglob("*.py")))
    return files


def measure_repository(root: Path) -> dict[str, dict[str, int]]:
    """Return ``{module: counts}`` for every module with at least one site.

    Modules that measure zero are absent: they are not part of the direct-access
    population, and a ledger entry for one has to say why it is declared anyway.
    """
    measured: dict[str, dict[str, int]] = {}
    for path in _scanned_files(root):
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or relative.startswith(EXCLUDED_PREFIXES):
            continue
        counts = measure_module_sites(path.read_text(encoding="utf-8", errors="replace"))
        if any(counts.values()):
            measured[relative] = counts
    return measured


@dataclass(frozen=True)
class ModuleDisposition:
    """One module's disposition, with the evidence for it."""

    module: str
    disposition: str
    reason: str
    #: Required for a deferral: the slice that owns the migration.
    target_slice: str | None = None
    #: Required when the module measures no direct site, explaining why the
    #: ledger declares it anyway.
    zero_site_reason: str | None = None


@dataclass(frozen=True)
class Deferral:
    """One deferred group: the modules, the reason, and the owning slice."""

    modules: tuple[str, ...]
    target_slice: str
    reason: str


# --------------------------------------------------------------------------
# Declaration
# --------------------------------------------------------------------------

#: The seam itself. Direct MPh/ClientAPI access here is the design, not a gap.
BACKEND_BOUNDARY = (
    ModuleDisposition(
        module="comsol_mcp/adapter/mph_backend.py",
        disposition="retain_at_backend_boundary",
        reason=(
            "This module is the reference-lane backend: it is the one place in the "
            "project allowed to import mph, start a client, and traverse model.java. "
            "Every other module must reach COMSOL through the protocol it implements."
        ),
    ),
    ModuleDisposition(
        module="comsol_mcp/adapter/protocol.py",
        disposition="retain_at_backend_boundary",
        reason=(
            "The protocol module reads the installed MPh version to key capability on "
            "what is actually installed rather than on the declared lane, which is the "
            "version-aware decision the seam exists to make. It performs no COMSOL "
            "operation of its own."
        ),
    ),
    ModuleDisposition(
        module="comsol_mcp/adapter/mph14_backend.py",
        disposition="retain_at_backend_boundary",
        reason=(
            "The isolated 1.4 lane differs from the reference lane only in the two "
            "measured capability differences, so it inherits the boundary instead of "
            "duplicating it."
        ),
        zero_site_reason=(
            "Measures zero direct sites because it subclasses MphBackendBase and adds "
            "no second Java traversal path. That is the parity property the gate asks "
            "for: two lanes behind one boundary, not two boundaries."
        ),
    ),
)

#: The S4A step-6 migration. The DNN bridge is the module the gate named.
MIGRATED = (
    ModuleDisposition(
        module="comsol_mcp/surrogate/dnn_clientapi_backend.py",
        disposition="migrate_behind_protocol",
        reason=(
            "S4A step 6. Node resolution, Java type wrapping, typed reads, and the "
            "feature lifecycle now go through the adapter: find_node, java_typed_write, "
            "java_typed_read, run_feature, and export_feature. A raw Java feature is "
            "presented as ResolvedNode rather than touched here, and the module imports "
            "neither jpype nor mph."
        ),
        zero_site_reason=(
            "Measures zero direct sites after the migration; the licensed gates that "
            "construct this bridge keep working because they pass the feature they "
            "resolved and the adapter resolves it, so no caller had to change."
        ),
    ),
)

#: Licensed, opt-in entry points. These never enter a wheel, and each one is an
#: acceptance probe that must drive COMSOL itself.
LICENSED_PROBES = (
    ModuleDisposition(
        module="development_kit/scripts",
        disposition="retain_in_licensed_probe",
        reason=(
            "Opt-in licensed acceptance gates. Each script builds and solves its own "
            "model to produce a receipt, so it must reach COMSOL directly; none is "
            "packaged, imported by runtime code, or run by hosted CI."
        ),
    ),
    ModuleDisposition(
        module="development_kit/tests/integration",
        disposition="retain_in_licensed_probe",
        reason=(
            "Serial, opt-in licensed acceptance harnesses. They exist to drive real "
            "COMSOL outside the solver-free suite, which is the definition of a "
            "licensed probe."
        ),
    ),
)

#: Deferred sites, grouped by the slice that will own the migration.
DEFERRALS = (
    Deferral(
        modules=("comsol_mcp/jobs/robust_shape_native_runtime.py",),
        target_slice="S4A steps 2-4",
        reason=(
            "The largest single consumer: it drives the ClientAPI directly through the "
            "durable shape-adjoint worker. Migrating it needs the node-lookup, "
            "conversion, and typed-property operations plus parity receipts for the "
            "gradient path, because the adjoint runtime is the most sensitive consumer "
            "of property write semantics."
        ),
    ),
    Deferral(
        modules=("comsol_mcp/research/adapters.py",),
        target_slice="S4A step 3",
        reason=(
            "The research ClientAPI helper layer wraps scalar, string, array, and "
            "matrix conversion, which is exactly step 3. Until conversion is migrated "
            "this module defines the only measured Java typing rules outside the "
            "boundary."
        ),
    ),
    Deferral(
        modules=(
            "comsol_mcp/jobs/native_adjoint_runtime.py",
            "comsol_mcp/research/adjoint_adapter.py",
        ),
        target_slice="S4A steps 2 and 4",
        reason=(
            "Native adjoint and research adjoint runtimes resolve study and physics "
            "nodes and then write typed properties. They need steps 2 and 4 together: "
            "migrating the lookup without the write would split one operation across "
            "two seams."
        ),
    ),
    Deferral(
        modules=(
            "comsol_mcp/jobs/thermo_optomechanical_replay_execution.py",
            "comsol_mcp/tools/physics.py",
            "comsol_mcp/tools/parameters.py",
            "comsol_mcp/tools/acoustics_pde.py",
            "comsol_mcp/tools/mim_patch.py",
        ),
        target_slice="S4A step 4",
        reason=(
            "Typed property read/write. The plan keeps failure-atomic rollback in the "
            "project, so these call sites cannot move until the protocol's rollback "
            "receipts cover the property shapes they use, including the JPype scalar "
            "wrapping they currently perform themselves."
        ),
    ),
    Deferral(
        modules=(
            "comsol_mcp/tools/periodic_mesh_audit.py",
            "comsol_mcp/tools/study.py",
            "comsol_mcp/tools/workflow.py",
            "comsol_mcp/tools/geometry.py",
            "comsol_mcp/tools/wave_optics_audit.py",
            "comsol_mcp/tools/wave_optics_preflight.py",
            "comsol_mcp/tools/electro_chemistry.py",
            "comsol_mcp/tools/mesh.py",
            "comsol_mcp/tools/geometry_selections.py",
            "comsol_mcp/tools/incidence_config.py",
            "comsol_mcp/tools/properties.py",
            "comsol_mcp/async_handler/solver.py",
            "comsol_mcp/evidence/field_discovery.py",
        ),
        target_slice="S4A step 2",
        reason=(
            "Tag/list/node lookup through model.java. Mechanical, but it is the "
            "largest group and every one of these tools has a solver-free test that "
            "injects a fake Java object, so the fake and the adapter have to be "
            "reconciled in the same change."
        ),
    ),
    Deferral(
        modules=(
            "comsol_mcp/shared_session/lifecycle.py",
            "comsol_mcp/tools/session.py",
            "comsol_mcp/jobs/worker.py",
            "comsol_mcp/jobs/robust_shape_worker.py",
            "comsol_mcp/jobs/spectral_worker.py",
            "comsol_mcp/jobs/validation_worker.py",
            "comsol_mcp/jobs/convergence_campaign_worker.py",
            "comsol_mcp/jobs/branch_continuation_campaign_worker.py",
            "comsol_mcp/jobs/thermo_optomechanical_replay_worker.py",
        ),
        target_slice="S4A step 1",
        reason=(
            "Session and client construction. This is the intended first migration and "
            "the highest-value one, but it changes solver ownership, so it is held "
            "until the step-1 session parity receipt exists for both lanes. The "
            "protocol's open_session and attach_client are already the target surface."
        ),
    ),
    Deferral(
        modules=(
            "comsol_mcp/research/lin2025_pedot_backend.py",
            "comsol_mcp/tools/derived_geometry.py",
            "comsol_mcp/tools/model.py",
        ),
        target_slice="S4A steps 2, 3, and 5",
        reason=(
            "Research backend and the geometry/model tools span lookup, conversion, and "
            "load/save, so they need three steps before they can move as one module "
            "without leaving half of an operation behind the seam."
        ),
    ),
    Deferral(
        modules=("comsol_mcp/jobs/native_cancel_probe.py",),
        target_slice="S4A step 1, reassessed",
        reason=(
            "The step-1 inventory proposed retaining this at the backend boundary "
            "because it calls mph.discovery. That proposal does not hold: six durable "
            "workers import request_native_cancel_once, so this is runtime code rather "
            "than an isolated probe and cannot claim the boundary disposition. It is "
            "deferred to step 1 with the rest of the client surface."
        ),
    ),
)


def declared_inventory() -> tuple[ModuleDisposition, ...]:
    """Return the flat declared ledger: one entry per module or probe group."""
    deferred = tuple(
        ModuleDisposition(
            module=module,
            disposition="defer_with_written_reason",
            reason=group.reason,
            target_slice=group.target_slice,
        )
        for group in DEFERRALS
        for module in group.modules
    )
    return (*BACKEND_BOUNDARY, *MIGRATED, *deferred, *LICENSED_PROBES)


def _prefix_disposition(
    module: str, inventory: Sequence[ModuleDisposition]
) -> ModuleDisposition | None:
    """Return the probe-group entry whose prefix contains ``module``."""
    for entry in inventory:
        if entry.disposition == "retain_in_licensed_probe" and module.startswith(
            f"{entry.module}/"
        ):
            return entry
    return None


def inventory_status(
    root: Path, *, inventory: Sequence[ModuleDisposition] | None = None
) -> dict[str, Any]:
    """Compare a declaration against the measurement.

    Returns ``measured``, ``declared``, and ``violations``.  Every violation is a
    string a test can print, so a failure says which rule broke and where.

    ``inventory`` defaults to :func:`declared_inventory`.  It is a parameter so a
    test can check that each rule actually fires, by running a synthetic ledger
    against a synthetic tree; a rule that is only ever exercised by the real
    ledger cannot be distinguished from a rule that never runs.
    """
    measured = measure_repository(root)
    inventory = tuple(inventory) if inventory is not None else declared_inventory()
    violations: list[str] = []

    by_module = {entry.module: entry for entry in inventory}
    if len(by_module) != len(inventory):
        violations.append("the ledger declares the same module twice")

    for module, counts in sorted(measured.items()):
        entry = by_module.get(module) or _prefix_disposition(module, inventory)
        if entry is None:
            violations.append(
                f"{module} has {sum(counts.values())} direct site(s) and no declared disposition"
            )
            continue
        if entry.disposition not in DISPOSITIONS:
            violations.append(f"{module} declares unknown disposition {entry.disposition!r}")
        if not entry.reason.strip():
            violations.append(f"{module} declares a disposition with no reason")
        if entry.disposition == "retain_at_backend_boundary" and not module.startswith(
            BACKEND_BOUNDARY_PREFIX
        ):
            violations.append(f"{module} claims the backend boundary but is outside it")
        if entry.disposition == "retain_in_licensed_probe" and not module.startswith(
            LICENSED_PROBE_PREFIX
        ):
            violations.append(f"{module} claims to be a licensed probe but is packaged code")
        if entry.disposition == "defer_with_written_reason":
            if not entry.target_slice:
                violations.append(f"{module} is deferred with no target slice")
            if entry.zero_site_reason is not None:
                violations.append(f"{module} has sites, so a zero-site reason is meaningless")

    for entry in inventory:
        if entry.disposition == "retain_in_licensed_probe":
            continue
        measured_counts = measured.get(entry.module)
        if entry.disposition == "migrate_behind_protocol":
            # The whole point of the claim: it must measure nothing. A migration
            # that silently grew a new direct call has to fail the ledger.
            if measured_counts is not None:
                violations.append(
                    f"{entry.module} claims to be migrated but still measures "
                    f"{sum(measured_counts.values())} direct site(s)"
                )
            if not entry.zero_site_reason:
                violations.append(f"{entry.module} claims migration with no verification note")
            continue
        if measured_counts is not None:
            continue
        if entry.disposition == "retain_at_backend_boundary":
            if not entry.zero_site_reason:
                violations.append(
                    f"{entry.module} measures no direct site and gives no reason for "
                    "being declared anyway"
                )
            continue
        violations.append(f"{entry.module} measures no direct site and is not declared as migrated")

    for module in sorted(measured):
        entry = by_module.get(module) or _prefix_disposition(module, inventory)
        if entry is None or entry.disposition == "retain_in_licensed_probe":
            continue
        if entry.disposition == "retain_at_backend_boundary":
            continue
        if entry.disposition == "defer_with_written_reason":
            continue
        violations.append(f"{module} has an unhandled disposition {entry.disposition!r}")

    return {
        "schema_name": "comsol_mcp.s4a_migration_inventory",
        "schema_version": "1.0.0",
        "dispositions": list(DISPOSITIONS),
        "measures": list(MEASURES),
        "measured_module_count": len(measured),
        "measured_site_count": sum(sum(counts.values()) for counts in measured.values()),
        "declared_count": len(inventory),
        "deferred_count": sum(
            1 for entry in inventory if entry.disposition == "defer_with_written_reason"
        ),
        "violations": violations,
        "measured": {module: dict(counts) for module, counts in sorted(measured.items())},
    }


def inventory_summary(root: Path) -> Mapping[str, Any]:
    """A status dict without the per-module measurement, for receipts."""
    status = inventory_status(root)
    return {key: value for key, value in status.items() if key != "measured"}


__all__ = [
    "BACKEND_BOUNDARY_PREFIX",
    "CLIENTAPI_ACCESSORS",
    "DEFERRALS",
    "DISPOSITIONS",
    "EXCLUDED_PREFIXES",
    "LICENSED_PROBE_PREFIX",
    "MEASURES",
    "PROBE_ROOTS",
    "RUNTIME_ROOTS",
    "Deferral",
    "ModuleDisposition",
    "declared_inventory",
    "inventory_status",
    "inventory_summary",
    "measure_module_sites",
    "measure_repository",
]
