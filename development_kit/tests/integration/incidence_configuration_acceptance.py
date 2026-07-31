"""Controlled COMSOL 6.4 gate for typed incidence configuration."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.durable.io import atomic_write_json_exclusive
from src.evidence.real_fixture import controlled_fixture_from_environment
from src.tools.derived_geometry import create_derived_geometry_clone
from src.tools.incidence_config import apply_incidence, preview_incidence
from src.tools.ownership import SolverOwnership

from development_kit.scripts.acceptance_cleanup import CleanupRecorder, lease_released


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _first_tag(tags, preferred: str) -> str | None:
    values = [str(value) for value in list(tags)]
    if not values:
        return None
    return preferred if preferred in values else values[0]


def _new_result_path(artifact_dir: Path) -> Path:
    return artifact_dir / f"incidence_gate_result-{uuid.uuid4().hex}.json"


def _publish_result(path: Path, result: dict) -> None:
    atomic_write_json_exclusive(path, result)


def main() -> None:
    runtime = Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime"))
    artifact_dir = runtime / "incidence_configuration"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result_path = _new_result_path(artifact_dir)
    owner = SolverOwnership(owner="incidence-configuration-gate")
    client = None
    source = None
    clone = None
    record = None
    result = {"success": False, "solve_ran": False}
    exit_code = 1
    try:
        source_path = controlled_fixture_from_environment()["source"]
        source_hash = _sha256(source_path)
        source_stat = source_path.stat()
        claim = owner.acquire(mode="incidence_configuration", model_path=str(source_path))
        if not claim.get("acquired"):
            raise RuntimeError(f"solver lease unavailable: {claim}")
        import mph

        client = mph.Client(cores=1, version="6.4")
        source = client.load(str(source_path))
        component_tag = _first_tag(source.java.component().tags(), "comp1")
        if component_tag is None:
            raise AssertionError("real model exposes no component")
        component = source.java.component().get(component_tag)
        physics_tag = _first_tag(component.physics().tags(), "ewfd")
        if physics_tag is None:
            raise AssertionError("real model exposes no physics")

        clone, record = create_derived_geometry_clone(
            source,
            client,
            new_name="IncidenceConfigurationClone",
            runtime_dir=artifact_dir,
        )
        preview = preview_incidence(
            clone,
            record,
            alpha1_inc="20[deg]",
            alpha2_inc="0[deg]",
            alpha1_unit="deg",
            alpha2_unit="deg",
            polarization="S",
            physical_polarization_target="declared S basis; field-vector proof not requested",
            component_tag=component_tag,
            physics_tag=physics_tag,
        )
        if preview["mutated"] or preview["solver_started"]:
            raise AssertionError("incidence preview mutated or started a solver")
        applied = apply_incidence(
            clone,
            record,
            preview,
            expected_state_sha256=preview["pre_state_sha256"],
        )
        if not applied.get("success"):
            raise AssertionError(f"incidence apply failed: {applied}")
        angles = applied["evaluated_angles"]
        if abs(float(angles["alpha1_inc"]["evaluated_value"]) - 20.0) > 1e-12:
            raise AssertionError(f"alpha1 evaluation mismatch: {angles}")
        if abs(float(angles["alpha2_inc"]["evaluated_value"])) > 1e-12:
            raise AssertionError(f"alpha2 evaluation mismatch: {angles}")
        parent = applied["after"]["periodic_structure"]["settings"]
        ports = [item["settings"] for item in applied["after"]["periodic_ports"]]
        if parent.get("alpha1_inc") != "20[deg]" or parent.get("alpha2_inc") != "0[deg]":
            raise AssertionError(f"parent angle readback mismatch: {parent}")
        if parent.get("Polarization") != "LinearPol" or parent.get("LinearPol") != "S":
            raise AssertionError(f"parent polarization readback mismatch: {parent}")
        if len(ports) != 2 or any(
            item.get("alpha1_inc") != "20[deg]" or item.get("alpha2_inc") != "0[deg]"
            for item in ports
        ):
            raise AssertionError(f"PeriodicPort angle readback mismatch: {ports}")

        final_stat = source_path.stat()
        source_unchanged = (
            _sha256(source_path) == source_hash
            and final_stat.st_mtime_ns == source_stat.st_mtime_ns
            and final_stat.st_size == source_stat.st_size
        )
        if not source_unchanged:
            raise AssertionError("immutable source changed")
        result.update(
            success=True,
            source_sha256=source_hash,
            source_unchanged=True,
            derived_model_id=record.derived_model_id,
            derived_backing_sha256=record.backing_sha256,
            component_tag=component_tag,
            physics_tag=physics_tag,
            preview={
                "pre_state_sha256": preview["pre_state_sha256"],
                "request": preview["request"],
                "evaluated_angles": preview["evaluated_angles"],
                "reference_edge_ids": preview["reference_edge_ids"],
                "physical_polarization_evidence": preview["physical_polarization_evidence"],
            },
            apply=applied,
        )
    except Exception as exc:
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc(limit=10)
    finally:
        cleanup = CleanupRecorder(result)
        if client is not None:
            if clone is not None:
                cleanup.run("clone_remove", lambda: client.remove(clone), expose_result=False)
            if source is not None:
                cleanup.run("source_remove", lambda: client.remove(source), expose_result=False)
            cleanup.run("client_clear", client.clear, expose_result=False)
        if record is not None:
            backing = Path(record.backing_path)

            def remove_backing() -> bool:
                backing.unlink(missing_ok=True)
                backing.parent.rmdir()
                return not backing.exists() and not backing.parent.exists()

            cleanup.run("derived_cleanup", remove_backing, passed=lambda value: value is True)
        cleanup.run("lease_release", owner.release, passed=lease_released)
        exit_code = cleanup.finalize()
        result["receipt_name"] = result_path.name
        _publish_result(result_path, result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        os._exit(exit_code)


if __name__ == "__main__":
    main()
