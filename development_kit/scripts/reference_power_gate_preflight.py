"""Solver-free validation entry point for the reference-power licensed-gate inputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comsol_mcp.durable.io import atomic_write_json_exclusive
from comsol_mcp.evidence.reference_power_acceptance import (
    MAX_INPUT_BYTES,
    build_reference_power_dry_run_receipt,
    load_bounded_json,
    validate_reference_power_acceptance_contract,
)

DEFAULT_CONTRACT = (
    ROOT / "development_kit" / "release" / "integration_fixtures" / "reference_power_evidence.json"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-files", action="store_true")
    args = parser.parse_args()

    contract = validate_reference_power_acceptance_contract(
        load_bounded_json(args.contract, MAX_INPUT_BYTES)
    )
    spec = None
    if args.spec is not None:
        spec = load_bounded_json(args.spec, contract["limits"]["max_spec_bytes"])
    receipt = build_reference_power_dry_run_receipt(contract, spec, verify_files=args.verify_files)
    text = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        atomic_write_json_exclusive(args.output, receipt)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
