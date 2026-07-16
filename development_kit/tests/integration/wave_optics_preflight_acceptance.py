"""Read-only COMSOL 6.4 gate for Wave Optics preflight evidence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

import mph

ROOT = Path(__file__).parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.ownership import SolverOwnership
from src.tools.wave_optics_preflight import collect_wave_optics_preflight
from src.evidence.real_fixture import controlled_fixture_from_environment


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _model_state(model) -> dict:
    jm = model.java
    component_tags = [str(value) for value in list(jm.component().tags())]
    mesh_counts = {}
    for component_tag in component_tags:
        component = jm.component().get(component_tag)
        for mesh_tag in [str(value) for value in list(component.mesh().tags())]:
            mesh = component.mesh().get(mesh_tag)
            mesh_counts[f"{component_tag}/{mesh_tag}"] = {
                "elements": int(mesh.getNumElem()),
                "vertices": int(mesh.getNumVertex()),
            }
    return {
        "component_tags": component_tags,
        "mesh_counts": mesh_counts,
        "solutions": [str(value) for value in model.solutions()],
        "datasets": [str(value) for value in model.datasets()],
        "parameters": {str(key): str(value) for key, value in model.parameters(evaluate=False).items()},
    }


def main() -> None:
    artifact_dir = Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime")) / "wave_optics_preflight"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = artifact_dir / "preflight_gate_result.json"
    owner = SolverOwnership(owner="wave-optics-preflight")
    client = None
    result = {"success": False, "solve_ran": False, "models": []}
    exit_code = 1
    try:
        models = (controlled_fixture_from_environment()["source"],)
        for source in models:
            if not source.is_file():
                raise FileNotFoundError(source)
        claim = owner.acquire(mode="wave_optics_preflight_read_only", model_path=str(models[0]))
        if not claim.get("acquired"):
            raise RuntimeError(f"solver lease unavailable: {claim}")
        client = mph.Client(cores=1)
        for source in models:
            source_hash = _sha256(source)
            source_stat = source.stat()
            model = client.load(str(source))
            before = _model_state(model)
            component_tag = before["component_tags"][0]
            physics_tags = [str(value) for value in list(model.java.component().get(component_tag).physics().tags())]
            study_tags = [str(value) for value in list(model.java.study().tags())]
            physics_tag = "ewfd" if "ewfd" in physics_tags else physics_tags[0]
            study_tag = "std1" if "std1" in study_tags else (study_tags[0] if study_tags else None)
            audit = collect_wave_optics_preflight(
                model,
                model_name=model.name(),
                session_state={"connected": True, "models": [model.name()]},
                active_profile="wave_optics",
                expected_component_tag=component_tag,
                expected_physics_tag=physics_tag,
                expected_study_tag=study_tag,
                expected_source_path=str(source),
                expected_source_sha256=source_hash,
                target_wavelength_parameter="wl",
            )
            after = _model_state(model)
            assert before == after, f"model read-only state changed for {source}"
            assert _sha256(source) == source_hash
            final_stat = source.stat()
            assert final_stat.st_mtime_ns == source_stat.st_mtime_ns
            assert final_stat.st_size == source_stat.st_size
            assert audit["assessment"]["mode"] == "evidence_only"
            assert audit["assessment"]["project_verdict"] is None
            result["models"].append({
                "source": str(source),
                "source_sha256": source_hash,
                "source_mtime_ns": source_stat.st_mtime_ns,
                "inspection_status": audit["inspection_status"],
                "evidence_codes": {
                    level: [item["code"] for item in audit["evidence"][level]]
                    for level in ("observations", "warnings", "unknowns", "integrity_errors")
                },
                "topology": {
                    "domains": audit["topology"].get("domain_count"),
                    "boundaries": audit["topology"].get("boundary_count"),
                    "pairs": len(audit["topology"].get("pairs", [])),
                },
                "periodic_structure_tag": audit["periodicity"].get("periodic_structure_tag"),
                "floquet_tags": [item["tag"] for item in audit["periodicity"].get("floquet_features", [])],
                "port_tags": [item["tag"] for item in audit["ports"].get("periodic_port_features", [])],
                "incidence": audit["incidence"].get("raw_properties"),
                "wavelength": audit["wavelength"],
                "meshes": audit["mesh_study_results"].get("meshes"),
            })
            client.remove(model)
        result.update(success=True, client={"standalone": client.port is None, "cores": 1})
        exit_code = 0
    except Exception as exc:
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc(limit=8)
    finally:
        if client is not None:
            try:
                client.clear()
            except Exception:
                pass
        result["lease_release"] = owner.release()
        manifest_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        os._exit(exit_code)


if __name__ == "__main__":
    main()
