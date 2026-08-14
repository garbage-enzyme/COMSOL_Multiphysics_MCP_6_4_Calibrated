from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from development_kit.scripts import lin2025_pedot_shape_licensed_gate as gate


def _root(tmp_path: Path) -> Path:
    if os.name == "nt":
        root = Path("D:/mcp_tests") / f"a72p{uuid.uuid4().hex[:6]}"
    else:
        root = tmp_path / f"a72p{uuid.uuid4().hex[:6]}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def _fixture(source_sha: str) -> dict:
    return {
        "schema_name": "comsol_mcp.lin2025_pedot_cylinder_fixture",
        "schema_version": "1.0.0",
        "adapter_id": "lin2025_pedot_cylinder_v1",
        "source_identity": {
            "source_sha256": source_sha,
            "tree_sha256": "b" * 64,
            "comsol_build": "6.4.0.293",
        },
        "geometry": {
            "lattice": "hexagonal_primitive",
            "period_um": 1.7,
            "pedot_height_um": 0.2,
            "air_above": True,
            "substrate_n": 1.5,
        },
        "domains": {"substrate": 1, "pedot_cylinder": 5, "air": 4},
        "material_states": [{"state_id": "OX"}, {"state_id": "MR"}],
        "mutable_variables": [
            {"variable_id": "pedot_cylinder_radius_x"},
            {"variable_id": "pedot_cylinder_radius_y"},
        ],
        "temperature_k": 300.0,
    }


def test_dry_run_is_solver_free_and_hash_bound(tmp_path: Path):
    root = _root(tmp_path)
    try:
        source = root / "source.mph"
        source.write_bytes(b"source")
        fixture_path = root / "fixture.json"
        tree_path = root / "tree.json"
        support_path = root / "support.json"
        fixture_path.write_text(
            json.dumps(_fixture(hashlib.sha256(b"source").hexdigest())), encoding="utf-8"
        )
        tree_path.write_text("{}", encoding="utf-8")
        support_path.write_text("{}", encoding="utf-8")
        args = argparse.Namespace(
            test_root=root,
            source_model=source,
            fixture=fixture_path,
            tree_readback=tree_path,
            support=support_path,
            cores=1,
            dry_run=True,
        )
        spec = gate._spec(args)
        result = gate._dry_run(spec)
        assert result["success"] is True
        assert result["solver_started"] is False
        assert result["filesystem_modified"] is False
        assert result["paths_included"] is False
    finally:
        shutil.rmtree(root, ignore_errors=True)
