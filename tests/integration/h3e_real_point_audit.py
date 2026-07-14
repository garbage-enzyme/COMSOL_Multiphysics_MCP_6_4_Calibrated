"""Controlled COMSOL 6.4 matrix for the H3e one-point evidence audit."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

import mph

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.ownership import SolverOwnership
from src.tools.wave_optics_audit import run_wave_optics_point_audit
from src.evidence.real_fixture import controlled_fixture_from_environment


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _first_tag(container, preferred):
    tags = [str(value) for value in list(container.tags())]
    if not tags:
        raise ValueError(f"empty clientapi container for {preferred}")
    return preferred if preferred in tags else tags[0]


def _study_step(study):
    tags = [str(value) for value in list(study.feature().tags())]
    for tag in tags:
        feature = study.feature().get(tag)
        try:
            if "wavelength" in str(feature.getType()).casefold():
                return tag
        except Exception:
            pass
    return tags[0]


def main() -> None:
    default_artifact_dir = Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime")) / "H3e"
    artifact_dir = Path(os.environ.get("H3E_ARTIFACT_DIR", str(default_artifact_dir)))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result_path = artifact_dir / "point_audit_gate_result.json"
    owner = SolverOwnership(owner="h3e-real-point-audit")
    client = None
    fixture = controlled_fixture_from_environment()
    fixture["policy"] = {
        "assumptions": {"passive": True, "port_power_normalized": True},
        "required_evidence": [
            "wavelength_controls", "flux_RTA", "top_air_region", "source_integrity"
        ],
        "tolerances": {
            "closure_abs": 1e-3,
            "quantity_bounds_margin": 1e-3,
            "wavelength_abs_m": 1e-12,
        },
    }
    active_cases = (fixture,)
    output = {"success": False, "solve_count": 0, "selected_cases": [case["name"] for case in active_cases], "cases": []}
    exit_code = 1
    try:
        for case in active_cases:
            if not case["source"].is_file():
                raise FileNotFoundError(case["source"])
        claim = owner.acquire(mode="h3e_one_point_matrix", model_path=str(active_cases[0]["source"]))
        if not claim.get("success"):
            raise RuntimeError(f"solver lease unavailable: {claim}")
        client = mph.Client(cores=8)
        for case in active_cases:
            source = case["source"]
            source_hash = _sha256(source)
            source_stat = source.stat()
            model = client.load(str(source))
            jm = model.java
            component_tag = _first_tag(jm.component(), "comp1")
            component = jm.component().get(component_tag)
            physics_tag = _first_tag(component.physics(), "ewfd")
            study_tag = _first_tag(jm.study(), "std1")
            study = jm.study().get(study_tag)
            step_tag = _study_step(study)
            r_expression = t_expression = a_expression = None
            loss_map = None
            power_provenance = {
                "normalization": "COMSOL PeriodicStructure total port powers normalized to the excited periodic port.",
                "R_direction": "Outgoing power through the excited periodic port.",
                "T_direction": "Outgoing power through the opposite periodic port.",
                "A_definition": "COMSOL ewfd.Atotal for the PeriodicStructure solution.",
            }
            domains = case["top_air_domain_ids"]
            coordinate_range = case["top_air_coordinate_range"]
            audit = run_wave_optics_point_audit(
                model,
                model_name=model.name(),
                component_tag=component_tag,
                physics_tag=physics_tag,
                study_tag=study_tag,
                wavelength_value=case["wavelength_um"],
                wavelength_unit="um",
                wavelength_parameter="wl",
                study_step_tag=step_tag,
                study_step_property="plist",
                expected_source_sha256=source_hash,
                config_id=f"h3e-{case['name']}",
                artifact_dir=str(artifact_dir / "audits"),
                r_expression=r_expression,
                t_expression=t_expression,
                a_expression=a_expression,
                top_air_domain_ids=domains,
                top_air_coordinate_range=coordinate_range,
                loss_map=loss_map,
                power_provenance=power_provenance,
                air_reference_artifact_path=None,
                air_reference_config_id=None,
                validation_policy=case["policy"],
                session_state={"connected": True, "models": [model.name()]},
                active_profile="wave_optics",
                ownership_preflight={"ready": True},
            )
            output["solve_count"] += int(audit.get("measurement", {}).get("solve", {}).get("ran", False))
            assert audit["success"], audit
            assert Path(audit["artifacts"]["csv"]).is_file()
            assert Path(audit["artifacts"]["manifest"]).is_file()
            assert _sha256(source) == source_hash
            final_stat = source.stat()
            assert final_stat.st_mtime_ns == source_stat.st_mtime_ns
            assert final_stat.st_size == source_stat.st_size
            output["cases"].append({
                "name": case["name"],
                "source": str(source),
                "source_sha256": source_hash,
                "audit_status": audit["audit_status"],
                "policy_overall": audit["assessment"].get("project_verdict"),
                "power": audit["measurement"]["power"],
                "wavelength": audit["measurement"]["wavelength"],
                "polarization_evidence_level": audit["measurement"]["polarization"]["evidence_level"],
                "field_statistics": audit["measurement"]["polarization"].get("structure_total_field"),
                "measurement_errors": audit["measurement"]["measurement_errors"],
                "integrity_errors": audit["measurement"]["integrity_errors"],
                "artifacts": audit["artifacts"],
            })
            client.remove(model)
        assert output["solve_count"] == len(active_cases)
        output["success"] = True
        exit_code = 0
    except Exception as exc:
        output["error"] = str(exc)
        output["traceback"] = traceback.format_exc(limit=12)
    finally:
        if client is not None:
            try:
                client.clear()
            except Exception:
                pass
        output["lease_release"] = owner.release()
        result_path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(output, ensure_ascii=False), flush=True)
        os._exit(exit_code)


if __name__ == "__main__":
    main()
