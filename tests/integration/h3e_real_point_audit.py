"""Controlled COMSOL 6.4 matrix for the H3e one-point evidence audit."""

from __future__ import annotations

import csv
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


ITERATIONS = Path(r"C:\Users\陆星\Desktop\iterations")
CASES = (
    {
        "name": "chen_port",
        "source": ITERATIONS / "Chen2023_MIM" / "chen2023_c1_smoke_check.mph",
        "wavelength_um": 4.37,
        "policy": {
            "assumptions": {"passive": True, "port_power_normalized": True},
            "required_evidence": ["wavelength_controls", "flux_RTA", "top_air_region", "source_integrity"],
            "tolerances": {"closure_abs": 1e-3, "quantity_bounds_margin": 1e-3, "wavelength_abs_m": 1e-12},
        },
    },
    {
        "name": "zhou_port_airref",
        "source": ITERATIONS / "Zhou2025_QBIC" / "stage2_localmesh.mph",
        "wavelength_um": 4.254,
        "air_reference": True,
        "policy": {
            "assumptions": {"passive": True, "port_power_normalized": True},
            "required_evidence": ["wavelength_controls", "flux_RTA", "incident_polarization", "source_integrity"],
            "tolerances": {"closure_abs": 1e-3, "quantity_bounds_margin": 1e-3, "wavelength_abs_m": 1e-12},
            "polarization": {"reference_config_id": "zhou2025_step3_airref_v4_phi0_S", "target_vector": [1, 0, 0], "max_cross_power_fraction": 0.02},
        },
    },
    {
        "name": "sun_port_label_audit",
        "source": ITERATIONS / "Sun2024_NatComm_FlatBand" / "stage2_DDS_smoke.mph",
        "wavelength_um": 5.998,
        "policy": {
            "assumptions": {"passive": True, "port_power_normalized": True},
            "required_evidence": ["wavelength_controls", "flux_RTA", "incident_polarization", "source_integrity"],
            "tolerances": {"closure_abs": 1e-3, "quantity_bounds_margin": 1e-3},
            "polarization": {"target_vector": [0, 1, 0], "max_cross_power_fraction": 0.05},
        },
    },
    {
        "name": "sun_scattered_a_gt_1",
        "source": ITERATIONS / "Sun2024_NatComm_FlatBand" / "stage2_scattered_field_scan.mph",
        "wavelength_um": 5.449,
        "scattered": True,
        "policy": {
            "assumptions": {"passive": True, "port_power_normalized": True},
            "required_evidence": ["wavelength_controls", "flux_RTA", "source_integrity"],
            "tolerances": {"quantity_bounds_margin": 0.0},
        },
    },
)


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


def _port_air_region(component, physics_tag):
    geometry_tag = _first_tag(component.geom(), "geom1")
    geometry = component.geom().get(geometry_tag)
    bbox = [float(value) for value in list(geometry.getBoundingBox())]
    physics = component.physics().get(physics_tag)
    ps = physics.feature().get("ps1")
    excited = [int(value) for value in list(ps.selection("excitedPortSelection").entities())]
    if len(excited) != 1:
        raise ValueError(f"expected one excited port boundary, got {excited}")
    up_down = geometry.getUpDown()
    boundary = excited[0]
    domains = [int(up_down[row][boundary - 1]) for row in (0, 1)]
    domains = [domain for domain in domains if domain]
    zmin, zmax = bbox[4], bbox[5]
    return domains, {
        "x": [bbox[0], bbox[1]],
        "y": [bbox[2], bbox[3]],
        "z": [zmax - 0.2 * (zmax - zmin), zmax],
    }


def _derived_air_reference(artifact_dir: Path) -> Path:
    source = ITERATIONS / "Zhou2025_QBIC" / "p1_incident_pol_airref_v4.csv"
    with source.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    row = next(
        item for item in rows
        if item["config_id"] == "zhou2025_step3_airref_v4"
        and item["phi_deg"] == "0" and item["pol"] == "S"
    )
    rms = {axis: float(row[f"rms_E{axis}"]) for axis in ("x", "y", "z")}
    expected = {axis: float(row[f"expected_E{axis}"]) for axis in ("x", "y", "z")}
    payload = {
        "config_id": "zhou2025_step3_airref_v4_phi0_S",
        "component_statistics": {
            axis: {
                "rms_abs": rms[axis],
                "complex_mean": {"real": rms[axis] * (1 if expected[axis] >= 0 else -1), "imag": 0.0},
            }
            for axis in ("x", "y", "z")
        },
        "stokes_xy": {"S0": rms["x"] ** 2 + rms["y"] ** 2, "S3": 0.0},
        "provenance": {
            "source_csv": str(source),
            "source_sha256": _sha256(source),
            "selected_row": {key: row[key] for key in ("theta_deg", "phi_deg", "pol", "wl_requested_um", "gate_pass")},
            "limitation": "The archived calibration provides RMS amplitudes but not complex phase; zero relative phase is retained explicitly for this amplitude-purity gate.",
        },
    }
    path = artifact_dir / "zhou_air_reference.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _scattered_interfaces(component):
    from jpype import JArray, JDouble

    geometry_tag = _first_tag(component.geom(), "geom1")
    geometry = component.geom().get(geometry_tag)
    bbox = [float(value) for value in list(geometry.getBoundingBox())]
    up_down = geometry.getUpDown()
    candidates = []
    for boundary in range(1, int(geometry.getNBoundaries()) + 1):
        up = int(up_down[0][boundary - 1])
        down = int(up_down[1][boundary - 1])
        if not up or not down:
            continue
        parameter_range = [float(value) for value in list(geometry.faceParamRange(boundary))]
        point = JArray(JArray(JDouble))(1)
        point[0] = JArray(JDouble)([
            (parameter_range[0] + parameter_range[1]) / 2,
            (parameter_range[2] + parameter_range[3]) / 2,
        ])
        normal = [float(value) for value in list(geometry.faceNormal(boundary, point)[0])]
        center = [float(value) for value in list(geometry.faceX(boundary, point)[0])]
        if abs(normal[2]) > 0.9:
            candidates.append({"boundary": boundary, "domains": [up, down], "z": center[2]})
    if len(candidates) < 2:
        raise ValueError(f"could not identify two internal horizontal interfaces: {candidates}")
    top = max(candidates, key=lambda item: item["z"])
    bottom = min(candidates, key=lambda item: item["z"])

    external_top_domains = set()
    for boundary in range(1, int(geometry.getNBoundaries()) + 1):
        up = int(up_down[0][boundary - 1])
        down = int(up_down[1][boundary - 1])
        if up and down:
            continue
        parameter_range = [float(value) for value in list(geometry.faceParamRange(boundary))]
        point = JArray(JArray(JDouble))(1)
        point[0] = JArray(JDouble)([
            (parameter_range[0] + parameter_range[1]) / 2,
            (parameter_range[2] + parameter_range[3]) / 2,
        ])
        center = [float(value) for value in list(geometry.faceX(boundary, point)[0])]
        if abs(center[2] - bbox[5]) <= max(1e-12, 1e-8 * (bbox[5] - bbox[4])):
            external_top_domains.update(domain for domain in (up, down) if domain)
    main_domains = [domain for domain in top["domains"] if domain not in external_top_domains]
    if len(main_domains) != 1:
        raise ValueError(f"top main-air domain is ambiguous: top={top}, external={sorted(external_top_domains)}")
    coordinate_range = {
        "x": [bbox[0], bbox[1]],
        "y": [bbox[2], bbox[3]],
        "z": [top["z"] - 0.2 * (bbox[5] - bbox[4]), top["z"]],
    }
    return top["boundary"], bottom["boundary"], main_domains, coordinate_range


def _add_scattered_flux_operators(component, top_boundary, bottom_boundary):
    from jpype import JArray, JInt

    for tag, boundary in (("h3e_top", top_boundary), ("h3e_bottom", bottom_boundary)):
        tags = [str(value) for value in list(component.cpl().tags())]
        operator = component.cpl().get(tag) if tag in tags else component.cpl().create(tag, "Integration")
        operator.selection().geom(_first_tag(component.geom(), "geom1"), 2)
        operator.selection().set(JArray(JInt)([boundary]))


def main() -> None:
    artifact_dir = Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime")) / "H3e"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result_path = artifact_dir / "point_audit_gate_result.json"
    air_reference_path = _derived_air_reference(artifact_dir)
    owner = SolverOwnership(owner="h3e-real-point-audit")
    client = None
    selected_names = {
        name.strip() for name in os.environ.get("H3E_CASES", "").split(",")
        if name.strip()
    }
    active_cases = tuple(
        case for case in CASES if not selected_names or case["name"] in selected_names
    )
    if not active_cases:
        raise ValueError(f"H3E_CASES selected no known cases: {sorted(selected_names)}")
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
            if case.get("scattered"):
                top_boundary, bottom_boundary, domains, coordinate_range = _scattered_interfaces(component)
                _add_scattered_flux_operators(component, top_boundary, bottom_boundary)
                p_inc = "1.061767491194e-14[W]"
                r_expression = f"({p_inc}-h3e_top(ewfd.Poavz))/{p_inc}"
                t_expression = f"h3e_bottom(ewfd.Poavz)/{p_inc}"
                a_expression = f"intop1(ewfd.Qh)/{p_inc}"
                loss_map = [{
                    "label": "absorbing_domains",
                    "domains": [3, 4, 7, 8],
                    "expression": "intop1(ewfd.Qh)",
                    "unit": "W",
                    "normalization_expression": p_inc,
                }]
                power_provenance = {
                    "normalization": f"P_inc={p_inc}; cell area 4 um x 2 um; raw total-field/scattered-field interface flux expressions.",
                    "R_direction": "(P_inc - upward-interface Poavz integral)/P_inc; unclamped diagnostic.",
                    "T_direction": "bottom-interface Poavz integral/P_inc; unclamped diagnostic.",
                    "A_definition": "Volume intop1(ewfd.Qh)/P_inc.",
                }
            else:
                domains, coordinate_range = _port_air_region(component, physics_tag)
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
                air_reference_artifact_path=str(air_reference_path) if case.get("air_reference") else None,
                air_reference_config_id="zhou2025_step3_airref_v4_phi0_S" if case.get("air_reference") else None,
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
