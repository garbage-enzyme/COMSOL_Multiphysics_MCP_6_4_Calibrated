"""Private-data-redacted PEDOT fixture compiler tests."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from development_kit.scripts.robust_pedot_fixture import compile_pedot_fixture


def _delivery(root: Path) -> Path:
    root.mkdir()
    (root / "csv").mkdir()
    (root / "PEDOT-Tos_Lin2025_数据说明.pdf").write_bytes(b"synthetic private provenance")
    common = [500.0, 800.0, 1000.0, 1200.0, 1500.0]
    for state, real2, imag2 in (
        ("OX", "epsilon2_real_cited_prior", "epsilon2_imag_cited_prior"),
        ("MR", "epsilon2_real", "epsilon2_imag"),
    ):
        path = root / "csv" / f"{state}_optical_constants_and_permittivity.csv"
        columns = [
            "wavelength_nm",
            "epsilon1_real",
            "epsilon1_imag",
            real2,
            imag2,
            "comsol_epsilon1_imag",
            "comsol_epsilon2_imag",
        ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for wavelength in common:
                writer.writerow(
                    {
                        "wavelength_nm": wavelength,
                        "epsilon1_real": 2.0,
                        "epsilon1_imag": 0.2,
                        real2: 3.0,
                        imag2: 0.3,
                        "comsol_epsilon1_imag": -0.2,
                        "comsol_epsilon2_imag": -0.3,
                    }
                )
    return root


def test_compiler_builds_path_redacted_ox_mr_tensor_and_24_condition_fixture(ascii_tmp_path):
    delivery = _delivery(ascii_tmp_path / "private")
    receipt = compile_pedot_fixture(
        delivery,
        active_domain_id="active_material_domain",
        temperature_k=300.0,
        interpolation_method="linear",
    )
    table = receipt["condition_table"]
    assert len(table["conditions"]) == 24
    assert table["completeness"]["dimension_cardinalities"] == {
        "wavelength": 3,
        "incidence_elevation": 2,
        "incidence_azimuth": 1,
        "polarization_basis": 2,
        "material_state": 2,
    }
    mappings = {
        state["state_id"]: state["optical_property_mapping"] for state in table["material_states"]
    }
    assert mappings["OX"]["tensor"]["components"][2]["real_column"] == ("epsilon2_real_cited_prior")
    assert mappings["MR"]["tensor"]["components"][2]["real_column"] == "epsilon2_real"
    assert all(
        mapping["time_harmonic_convention"] == "exp_positive_i_omega_t"
        for mapping in mappings.values()
    )
    serialized = str(receipt)
    assert str(delivery) not in serialized
    assert receipt["private_paths_redacted"] is True
    tensor_rows = receipt["material_tensor_rows"]
    assert [item["state_id"] for item in tensor_rows["states"]] == ["OX", "MR"]
    assert [row["wavelength_m"] for row in tensor_rows["states"][0]["rows"]] == pytest.approx(
        [8e-7, 1e-6, 1.2e-6]
    )


def test_compiler_rejects_wrong_comsol_sign_missing_range_or_implicit_inputs(ascii_tmp_path):
    delivery = _delivery(ascii_tmp_path / "wrong-sign")
    ox = delivery / "csv" / "OX_optical_constants_and_permittivity.csv"
    text = ox.read_text(encoding="utf-8").replace(",-0.2,-0.3", ",0.2,-0.3", 1)
    ox.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="wrong sign"):
        compile_pedot_fixture(
            delivery,
            active_domain_id="active_material_domain",
            temperature_k=300.0,
            interpolation_method="linear",
        )
    delivery = _delivery(ascii_tmp_path / "missing-range")
    mr = delivery / "csv" / "MR_optical_constants_and_permittivity.csv"
    lines = mr.read_text(encoding="utf-8").splitlines()
    mr.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="common no-extrapolation range"):
        compile_pedot_fixture(
            delivery,
            active_domain_id="active_material_domain",
            temperature_k=300.0,
            interpolation_method="linear",
        )
    delivery = _delivery(ascii_tmp_path / "bad-temperature")
    with pytest.raises(ValueError, match="temperature"):
        compile_pedot_fixture(
            delivery,
            active_domain_id="active_material_domain",
            temperature_k=0.0,
            interpolation_method="linear",
        )


def test_compiler_accepts_utf8_bom_on_delivery_csv_headers(ascii_tmp_path):
    delivery = _delivery(ascii_tmp_path / "bom")
    ox = delivery / "csv" / "OX_optical_constants_and_permittivity.csv"
    ox.write_bytes(b"\xef\xbb\xbf" + ox.read_bytes())
    receipt = compile_pedot_fixture(
        delivery,
        active_domain_id="active_material_domain",
        temperature_k=300.0,
        interpolation_method="linear",
    )
    assert receipt["source_audits"]["OX"]["imaginary_sign_verified"] is True


def test_compiler_tolerates_parse_noise_on_exact_boundary_and_fixture_rows(
    ascii_tmp_path,
):
    # Boundary and fixture wavelengths written with sub-nanometre parse noise
    # still satisfy the exact-row requirements; exact equality on parsed CSV
    # floats was needlessly brittle.
    delivery = _delivery(ascii_tmp_path / "parse-noise")
    for state in ("OX", "MR"):
        path = delivery / "csv" / f"{state}_optical_constants_and_permittivity.csv"
        text = path.read_text(encoding="utf-8")
        text = (
            text.replace("\n500.0,", "\n500.000000000001,")
            .replace("\n800.0,", "\n799.999999999999,")
            .replace("\n1500.0,", "\n1499.999999999998,")
        )
        path.write_text(text, encoding="utf-8")
    receipt = compile_pedot_fixture(
        delivery,
        active_domain_id="active_material_domain",
        temperature_k=300.0,
        interpolation_method="linear",
    )
    assert receipt["source_audits"]["OX"]["common_range_row_count"] == 5
    assert [
        row["wavelength_m"] for row in receipt["material_tensor_rows"]["states"][0]["rows"]
    ] == pytest.approx([8e-7, 1e-6, 1.2e-6])


def test_compiler_emits_exact_off_design_tensor_samples_without_extrapolation(ascii_tmp_path):
    delivery = _delivery(ascii_tmp_path / "off-design")
    receipt = compile_pedot_fixture(
        delivery,
        active_domain_id="active_material_domain",
        temperature_k=300.0,
        interpolation_method="linear",
        validation_wavelength_relative_offsets=[-0.01, 0.01],
    )
    expected_nm = [792.0, 800.0, 808.0, 990.0, 1000.0, 1010.0, 1188.0, 1200.0, 1212.0]
    assert receipt["fixture_policy"]["validation_sample_wavelengths_nm"] == expected_nm
    assert [
        row["wavelength_m"] for row in receipt["material_tensor_rows"]["states"][0]["rows"]
    ] == pytest.approx([value * 1e-9 for value in expected_nm])
