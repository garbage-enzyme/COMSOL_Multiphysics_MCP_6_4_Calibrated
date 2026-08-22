"""Compile the private OX/MR PEDOT delivery into a redacted robust fixture manifest."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from comsol_mcp.durable import domain_sha256_v2
from comsol_mcp.jobs.store import atomic_write_json
from comsol_mcp.research.robust_conditions import normalize_optimization_condition_table
from comsol_mcp.research.robust_material_mapping import normalize_optical_property_mapping
from comsol_mcp.research.robust_material_tensor_rows import normalize_robust_material_tensor_rows

PEDOT_FIXTURE_SCHEMA_NAME = "comsol_mcp.robust_pedot_fixture_manifest"
PEDOT_FIXTURE_SCHEMA_VERSION = "1.0.0"

_WAVELENGTHS_NM = (800.0, 1000.0, 1200.0)
_ANGLES_DEG = (0.0, 30.0)
_POLARIZATIONS = ("x_linear", "y_linear")
_STATES = ("OX", "MR")
_COMMON_MIN_NM = 500.0
_COMMON_MAX_NM = 1500.0
_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_STATE_FILES = {
    "OX": "OX_optical_constants_and_permittivity.csv",
    "MR": "MR_optical_constants_and_permittivity.csv",
}
_REQUIRED_COMMON_COLUMNS = {
    "wavelength_nm",
    "epsilon1_real",
    "epsilon1_imag",
    "comsol_epsilon1_imag",
    "comsol_epsilon2_imag",
}
_STATE_REAL_COLUMNS = {"OX": "epsilon2_real_cited_prior", "MR": "epsilon2_real"}
_STATE_IMAG_COLUMNS = {"OX": "epsilon2_imag_cited_prior", "MR": "epsilon2_imag"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_file(root: Path, relative: str) -> Path:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"PEDOT source {relative} must be a regular file")
    if path.stat().st_size > _MAX_SOURCE_BYTES:
        raise ValueError(f"PEDOT source {relative} exceeds its byte limit")
    return path


def _number(value: str | None, label: str) -> float:
    try:
        number = float(value) if value is not None else math.nan
    except ValueError as exc:
        raise ValueError(f"PEDOT {label} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"PEDOT {label} must be finite")
    return number


def _audit_csv(
    path: Path,
    state: str,
    sample_wavelengths: tuple[float, ...],
    *,
    interpolation_method: str = "linear",
) -> dict[str, Any]:
    real2 = _STATE_REAL_COLUMNS[state]
    imag2 = _STATE_IMAG_COLUMNS[state]
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames
        required = _REQUIRED_COMMON_COLUMNS | {real2, imag2}
        if columns is None or not required.issubset(columns):
            raise ValueError(f"PEDOT {state} CSV is missing required tensor columns")
        rows = list(reader)
    if not rows or len(rows) > 100_000:
        raise ValueError(f"PEDOT {state} CSV row count is outside the allowed range")
    wavelengths: list[float] = []
    common_rows = 0
    tensor_rows: list[dict[str, float]] = []
    for index, row in enumerate(rows, start=2):
        wavelength = _number(row.get("wavelength_nm"), f"{state} row {index} wavelength")
        if wavelengths and wavelength <= wavelengths[-1]:
            raise ValueError(f"PEDOT {state} wavelengths must be strictly increasing")
        wavelengths.append(wavelength)
        epsilon1_imag = _number(row.get("epsilon1_imag"), f"{state} row {index} epsilon1_imag")
        epsilon2_imag = _number(row.get(imag2), f"{state} row {index} {imag2}")
        comsol1 = _number(
            row.get("comsol_epsilon1_imag"), f"{state} row {index} comsol_epsilon1_imag"
        )
        comsol2 = _number(
            row.get("comsol_epsilon2_imag"), f"{state} row {index} comsol_epsilon2_imag"
        )
        _number(row.get("epsilon1_real"), f"{state} row {index} epsilon1_real")
        _number(row.get(real2), f"{state} row {index} {real2}")
        if not math.isclose(
            comsol1, -epsilon1_imag, rel_tol=1e-12, abs_tol=1e-12
        ) or not math.isclose(comsol2, -epsilon2_imag, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f"PEDOT {state} COMSOL imaginary columns have the wrong sign")
        if _COMMON_MIN_NM <= wavelength <= _COMMON_MAX_NM:
            common_rows += 1
        tensor_rows.append(
            {
                "wavelength_nm": wavelength,
                "xx_real": _number(
                    row.get("epsilon1_real"), f"{state} row {index} epsilon1_real"
                ),
                "xx_imag": comsol1,
                "yy_real": _number(
                    row.get("epsilon1_real"), f"{state} row {index} epsilon1_real"
                ),
                "yy_imag": comsol1,
                "zz_real": _number(row.get(real2), f"{state} row {index} {real2}"),
                "zz_imag": comsol2,
            }
        )
    if wavelengths[0] > _COMMON_MIN_NM or wavelengths[-1] < _COMMON_MAX_NM:
        raise ValueError(f"PEDOT {state} CSV does not cover the common no-extrapolation range")
    if _COMMON_MIN_NM not in wavelengths or _COMMON_MAX_NM not in wavelengths:
        raise ValueError(f"PEDOT {state} CSV lacks exact common-range boundary rows")
    if any(wavelength not in wavelengths for wavelength in _WAVELENGTHS_NM):
        raise ValueError(f"PEDOT {state} CSV lacks an exact fixture wavelength row")
    samples: list[dict[str, float]] = []
    for target in sample_wavelengths:
        if target < _COMMON_MIN_NM or target > _COMMON_MAX_NM:
            # The mapping declares the common range with extrapolation
            # forbidden, so samples must stay inside it even when the source
            # CSV itself covers more.
            raise ValueError(
                f"PEDOT {state} sample wavelength {target} nm is outside the "
                f"{_COMMON_MIN_NM}-{_COMMON_MAX_NM} nm mapping range"
            )
        if interpolation_method != "linear" and target not in wavelengths:
            # The audit interpolates linearly; a non-linear declared method
            # would disagree at any non-exact wavelength. Require exact rows
            # so both representations agree by construction.
            raise ValueError(
                f"PEDOT {state} sample wavelength {target} nm must match an exact "
                f"source row when interpolation_method={interpolation_method!r}"
            )
        exact = next((item for item in tensor_rows if item["wavelength_nm"] == target), None)
        if exact is None:
            upper_index = next(
                index for index, item in enumerate(tensor_rows) if item["wavelength_nm"] > target
            )
            lower = tensor_rows[upper_index - 1]
            upper = tensor_rows[upper_index]
            fraction = (target - lower["wavelength_nm"]) / (
                upper["wavelength_nm"] - lower["wavelength_nm"]
            )
            exact = {
                key: lower[key] + fraction * (upper[key] - lower[key])
                for key in ("xx_real", "xx_imag", "yy_real", "yy_imag", "zz_real", "zz_imag")
            }
        samples.append(
            {
                "wavelength_m": target * 1e-9,
                **{
                    key: exact[key]
                    for key in (
                        "xx_real",
                        "xx_imag",
                        "yy_real",
                        "yy_imag",
                        "zz_real",
                        "zz_imag",
                    )
                },
            }
        )
    return {
        "source_sha256": _sha256(path),
        "row_count": len(rows),
        "common_range_row_count": common_rows,
        "source_wavelength_min_nm": wavelengths[0],
        "source_wavelength_max_nm": wavelengths[-1],
        "common_wavelength_min_nm": _COMMON_MIN_NM,
        "common_wavelength_max_nm": _COMMON_MAX_NM,
        "epsilon2_real_column": real2,
        "epsilon2_imaginary_column": imag2,
        "imaginary_sign_verified": True,
        "sample_rows": samples,
    }


def _mapping(
    *,
    state: str,
    source_sha256: str,
    active_domain_id: str,
    interpolation_method: str,
) -> dict[str, Any]:
    real2 = _STATE_REAL_COLUMNS[state]
    mapping: dict[str, Any] = normalize_optical_property_mapping(
        {
            "schema_name": "comsol_mcp.optimization_optical_property_mapping",
            "schema_version": "1.0.0",
            "mapping_id": f"pedot-{state.lower()}-tensor",
            "active_domain_id": active_domain_id,
            "source_sha256": source_sha256,
            "independent_variable": {
                "kind": "vacuum_wavelength",
                "source_column": "wavelength_nm",
                "source_unit": "nm",
                "valid_min": _COMMON_MIN_NM,
                "valid_max": _COMMON_MAX_NM,
                "interpolation_method": interpolation_method,
                "extrapolation": "forbidden",
            },
            "tensor": {
                "basis": "model_cartesian",
                "form": "diagonal",
                "off_diagonal_zero": True,
                "components": [
                    {
                        "component": "xx",
                        "source_axis_id": "epsilon1",
                        "real_column": "epsilon1_real",
                        "imaginary_column": "comsol_epsilon1_imag",
                    },
                    {
                        "component": "yy",
                        "source_axis_id": "epsilon1",
                        "real_column": "epsilon1_real",
                        "imaginary_column": "comsol_epsilon1_imag",
                    },
                    {
                        "component": "zz",
                        "source_axis_id": "epsilon2",
                        "real_column": real2,
                        "imaginary_column": "comsol_epsilon2_imag",
                    },
                ],
            },
            "time_harmonic_convention": "exp_positive_i_omega_t",
        }
    )
    return mapping


def compile_pedot_fixture(
    delivery_root: str | Path,
    *,
    active_domain_id: str,
    temperature_k: float,
    interpolation_method: str,
    validation_wavelength_relative_offsets: list[float] | tuple[float, ...] = (),
) -> dict[str, Any]:
    """Compile a path-redacted, hash-bound OX/MR 24-condition fixture."""
    root = Path(delivery_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("PEDOT delivery root must be a directory")
    if not math.isfinite(temperature_k) or temperature_k <= 0.0:
        raise ValueError("PEDOT fixture temperature must be finite and positive")
    pdf = _source_file(root, "PEDOT-Tos_Lin2025_数据说明.pdf")
    pdf_sha256 = _sha256(pdf)
    offsets = sorted({float(item) for item in validation_wavelength_relative_offsets})
    if any(not math.isfinite(item) or item == 0.0 or abs(item) > 0.5 for item in offsets):
        raise ValueError("validation wavelength offsets are outside the allowed range")
    sample_wavelengths = tuple(
        sorted(
            set(_WAVELENGTHS_NM)
            | {base * (1.0 + offset) for base in _WAVELENGTHS_NM for offset in offsets}
        )
    )
    audits = {}
    states = []
    for state in _STATES:
        relative = f"csv/{_STATE_FILES[state]}"
        source = _source_file(root, relative)
        audit = _audit_csv(
            source, state, sample_wavelengths, interpolation_method=interpolation_method
        )
        mapping = _mapping(
            state=state,
            source_sha256=audit["source_sha256"],
            active_domain_id=active_domain_id,
            interpolation_method=interpolation_method,
        )
        ledger = {
            "state_id": state,
            "delivery_document_sha256": pdf_sha256,
            "optical_property_source_sha256": audit["source_sha256"],
            "mapping_fingerprint": mapping["mapping_fingerprint"],
            "temperature_k": float(temperature_k),
        }
        states.append(
            {
                "schema_name": "comsol_mcp.optimization_material_state",
                "schema_version": "1.0.0",
                "state_id": state,
                "material_ledger_sha256": domain_sha256_v2(
                    "comsol_mcp.robust_pedot_material_ledger", ledger
                ),
                "optical_property_source_sha256": audit["source_sha256"],
                "optical_property_mapping": mapping,
                "temperature_k": float(temperature_k),
                "provenance_disposition": "private_input_hash_bound",
            }
        )
        audits[state] = {"source_label": relative, **audit}
    rows: list[dict[str, Any]] = []
    for wavelength in _WAVELENGTHS_NM:
        for angle in _ANGLES_DEG:
            for polarization in _POLARIZATIONS:
                for state in _STATES:
                    excitation = {
                        "wavelength_nm": wavelength,
                        "incidence_elevation_deg": angle,
                        "incidence_azimuth_deg": 0.0,
                        "incidence_plane": "x_z",
                        "polarization_basis_id": polarization,
                    }
                    order = len(rows)
                    rows.append(
                        {
                            "condition_id": f"pedot-{order:02d}",
                            "order": order,
                            "wavelength_m": wavelength * 1e-9,
                            "incidence_elevation_deg": angle,
                            "incidence_azimuth_deg": 0.0,
                            "polarization_basis_id": polarization,
                            "excitation_sha256": domain_sha256_v2(
                                "comsol_mcp.robust_pedot_excitation", excitation
                            ),
                            "material_state_id": state,
                            "objective_role": "objective",
                            "observable_id": "transmission_order_0_0",
                            "weight": 1.0,
                            "target": None,
                            "scale": 1.0,
                            "active": True,
                        }
                    )
    condition_table = normalize_optimization_condition_table(
        {
            "schema_name": "comsol_mcp.optimization_condition_table",
            "schema_version": "1.0.0",
            "table_id": "pedot-ox-mr-24",
            "material_states": states,
            "conditions": rows,
            "completeness": {"mode": "cartesian_complete", "sparse_justification": None},
        }
    )
    material_tensor_rows = normalize_robust_material_tensor_rows(
        {
            "schema_name": "comsol_mcp.robust_material_tensor_rows",
            "schema_version": "1.0.0",
            "source_sha256": hashlib.sha256(
                (audits["OX"]["source_sha256"] + audits["MR"]["source_sha256"]).encode()
            ).hexdigest(),
            "states": [
                {
                    "state_id": state,
                    "source_sha256": audits[state]["source_sha256"],
                    "rows": audits[state]["sample_rows"],
                }
                for state in _STATES
            ],
        }
    )
    body = {
        "schema_name": PEDOT_FIXTURE_SCHEMA_NAME,
        "schema_version": PEDOT_FIXTURE_SCHEMA_VERSION,
        "source_labels": {
            "delivery_document": "PEDOT-Tos_Lin2025_数据说明.pdf",
            "OX": audits["OX"]["source_label"],
            "MR": audits["MR"]["source_label"],
        },
        "source_sha256": {
            "delivery_document": pdf_sha256,
            "OX": audits["OX"]["source_sha256"],
            "MR": audits["MR"]["source_sha256"],
        },
        "source_audits": audits,
        "condition_table": condition_table,
        "material_tensor_rows": material_tensor_rows,
        "fixture_policy": {
            "geometry_adapter_id": "periodic_mim_patch_v1",
            "active_domain_id": active_domain_id,
            "temperature_k": float(temperature_k),
            "wavelengths_nm": list(_WAVELENGTHS_NM),
            "validation_wavelength_relative_offsets": offsets,
            "validation_sample_wavelengths_nm": list(sample_wavelengths),
            "incidence_elevation_deg": list(_ANGLES_DEG),
            "incidence_azimuth_deg": 0.0,
            "incidence_plane": "x_z",
            "polarizations": list(_POLARIZATIONS),
            "material_states": list(_STATES),
            "interpolation_method": interpolation_method,
            "extrapolation": "forbidden",
        },
        "private_paths_redacted": True,
    }
    body["fixture_fingerprint"] = domain_sha256_v2(PEDOT_FIXTURE_SCHEMA_NAME, body)
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--active-domain-id", required=True)
    parser.add_argument("--temperature-k", required=True, type=float)
    parser.add_argument(
        "--interpolation-method", required=True, choices=("linear", "piecewise_cubic")
    )
    parser.add_argument(
        "--validation-wavelength-relative-offset",
        action="append",
        type=float,
        default=[],
    )
    args = parser.parse_args()
    output = Path(args.output).expanduser()
    if not output.is_absolute() or not str(output).isascii() or output.suffix.lower() != ".json":
        raise SystemExit("output must be an absolute ASCII JSON path")
    receipt = compile_pedot_fixture(
        args.delivery_root,
        active_domain_id=args.active_domain_id,
        temperature_k=args.temperature_k,
        interpolation_method=args.interpolation_method,
        validation_wavelength_relative_offsets=args.validation_wavelength_relative_offset,
    )
    atomic_write_json(output, receipt)
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
