"""Licensed shared Desktop/Server collaboration gate for COMSOL 6.4.0.*."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from comsol_mcp.jobs.store import atomic_write_json
from comsol_mcp.operation_arbiter import get_operation_arbiter
from comsol_mcp.shared_session.contracts import SHARED_SERVER_FEATURE_ENV
from comsol_mcp.shared_session.lifecycle import SharedSessionManager


SCHEMA_NAME = "comsol_mcp.shared_interactive_licensed_gate"
SCHEMA_VERSION = "1.0.0"
MCP_PARAMETER = "mcp_shared_value"
DESKTOP_PARAMETER = "desktop_shared_value"
MCP_PARAMETER_VALUE = "17[mm]"
DESKTOP_INITIAL_VALUE = "23[mm]"
SAVED_MODEL_PARAMETER = "saved_model_agent_value"
SAVED_MODEL_PARAMETER_VALUE = "31[mm]"
CAPACITANCE_EXPRESSION = "2*es.intWe/(1[V])^2"
CAPACITANCE_UNIT = "pF"
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one bounded non-owning shared-session acceptance phase."
    )
    parser.add_argument(
        "--mode",
        choices=("prepare", "readback", "saved", "saved_readback"),
        required=True,
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=2036)
    parser.add_argument("--model-tag", required=True)
    parser.add_argument("--expected-label", required=True)
    parser.add_argument("--expected-desktop-value")
    parser.add_argument("--expected-file-path", type=Path)
    parser.add_argument("--immutable-source-path", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git_head() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _spec(args: argparse.Namespace) -> dict[str, Any]:
    if args.mode in {"readback", "saved", "saved_readback"} and not (
        args.expected_desktop_value
    ):
        raise ValueError(f"{args.mode} mode requires --expected-desktop-value")
    if args.mode == "prepare" and (
        args.expected_desktop_value is not None
        or args.expected_file_path is not None
        or args.immutable_source_path is not None
    ):
        raise ValueError(
            "prepare mode does not accept saved-model confirmation inputs"
        )
    if args.mode == "readback" and (
        args.expected_file_path is not None
        or args.immutable_source_path is not None
    ):
        raise ValueError("readback mode does not accept saved-model paths")
    if args.mode in {"saved", "saved_readback"}:
        if args.expected_file_path is None:
            raise ValueError("saved mode requires --expected-file-path")
        if args.immutable_source_path is None:
            raise ValueError("saved mode requires --immutable-source-path")
        if (
            not args.expected_file_path.is_absolute()
            or not str(args.expected_file_path).isascii()
        ):
            raise ValueError("saved working-model path must be absolute and ASCII")
        if (
            not args.immutable_source_path.is_absolute()
            or not str(args.immutable_source_path).isascii()
            or args.immutable_source_path == args.expected_file_path
        ):
            raise ValueError(
                "immutable source must be a distinct absolute ASCII path"
            )
    if not args.receipt.is_absolute() or not str(args.receipt).isascii():
        raise ValueError("receipt must be an absolute ASCII path")
    selector = {
        "tag": args.model_tag,
        "expected_label": args.expected_label,
    }
    if args.mode in {"saved", "saved_readback"}:
        selector["expected_file_path"] = str(args.expected_file_path)
    else:
        selector["expected_unsaved"] = True
    return {
        "mode": args.mode,
        "endpoint": {"host": args.host, "port": args.port},
        "selector": selector,
        "expected_desktop_value": args.expected_desktop_value,
        "expected_file_path": (
            None
            if args.expected_file_path is None
            else str(args.expected_file_path)
        ),
        "immutable_source_path": (
            None
            if args.immutable_source_path is None
            else str(args.immutable_source_path)
        ),
        "mcp_parameter": {"name": MCP_PARAMETER, "value": MCP_PARAMETER_VALUE},
        "desktop_parameter": {
            "name": DESKTOP_PARAMETER,
            "initial_value": DESKTOP_INITIAL_VALUE,
        },
        "saved_model_parameter": {
            "name": SAVED_MODEL_PARAMETER,
            "value": SAVED_MODEL_PARAMETER_VALUE,
        },
        "solver_gate": {
            "kind": "controlled_parallel_plate_capacitor",
            "expression": CAPACITANCE_EXPRESSION,
            "unit": CAPACITANCE_UNIT,
            "publication_claim": False,
        },
    }


def _exact_model(manager: SharedSessionManager, tag: str) -> Any:
    matches = [
        model
        for model in list(manager._client.models())
        if str(model.java.tag()) == tag
    ]
    if len(matches) != 1:
        raise RuntimeError("locked model tag is no longer unique")
    return matches[0]


def _parameter_expressions(model: Any) -> dict[str, str]:
    return {
        str(name): str(value)
        for name, value in sorted(model.parameters().items())
    }


def _prepare_capacitor(model: Any) -> dict[str, Any]:
    import jpype

    jm = model.java
    if any(
        list(collection.tags())
        for collection in (jm.component(), jm.study(), jm.result().dataset())
    ):
        raise RuntimeError("prepare mode requires the exact blank unsaved model")

    for name, value in (
        (MCP_PARAMETER, MCP_PARAMETER_VALUE),
        (DESKTOP_PARAMETER, DESKTOP_INITIAL_VALUE),
        ("L", "0.01[m]"),
        ("d", "0.001[m]"),
        ("epsr", "2.1"),
        ("V0", "1[V]"),
    ):
        jm.param().set(name, value)

    component = jm.component().create("comp1", True)
    geometry = component.geom().create("geom1", 3)
    block = geometry.feature().create("blk1", "Block")
    block.set("size", jpype.JArray(jpype.JDouble)([0.01, 0.01, 0.001]))
    block.set("pos", jpype.JArray(jpype.JDouble)([0.0, 0.0, 0.0]))
    geometry.run()

    dimension = str(geometry.getSDim())
    electrostatics = component.physics().create("es", "Electrostatics", dimension)
    conservation = electrostatics.feature().create(
        "ccn1", "ChargeConservation", int(dimension)
    )
    conservation.selection().set([1])
    conservation.set("materialType", "from_mat")
    material = component.material().create("mat1", "Common")
    material.propertyGroup("def").set("relpermittivity", "2.1")
    material.selection().set([1])
    ground = electrostatics.feature().create("gnd1", "Ground", 2)
    ground.selection().set([3])
    potential = electrostatics.feature().create("ep1", "ElectricPotential", 2)
    potential.selection().set([4])
    potential.set("V0", "V0")

    mesh = component.mesh().create("mesh1")
    mesh.feature().create("ftr1", "FreeTet")
    mesh.run()
    study = jm.study().create("std1")
    study.create("step1", "Stationary")
    solve_started = time.monotonic()
    study.run()
    solve_seconds = time.monotonic() - solve_started

    measured = float(
        model.evaluate(CAPACITANCE_EXPRESSION, CAPACITANCE_UNIT).reshape(-1)[0]
    )
    theory = 8.8541878128e-12 * 2.1 * math.pow(0.01, 2) / 0.001 * 1e12
    relative_error = abs(measured - theory) / theory
    if not math.isfinite(measured) or relative_error > 1e-8:
        raise RuntimeError("controlled capacitance result is outside its declared gate")

    display_warning = None
    try:
        evaluation = jm.result().numerical().create("gev1", "EvalGlobal")
        evaluation.label("Shared Gate Capacitance")
        evaluation.set("expr", [CAPACITANCE_EXPRESSION])
        evaluation.set("unit", [CAPACITANCE_UNIT])
        evaluation.setResult()
    except Exception as exc:
        display_warning = f"{type(exc).__name__}: {exc}"

    return {
        "parameters": _parameter_expressions(model),
        "geometry": {
            "domains": int(geometry.getNDomains()),
            "boundaries": int(geometry.getNBoundaries()),
        },
        "mesh": {"elements": int(mesh.getNumElem())},
        "study": {"tag": "std1", "solve_seconds": round(solve_seconds, 6)},
        "result": {
            "expression": CAPACITANCE_EXPRESSION,
            "unit": CAPACITANCE_UNIT,
            "measured": measured,
            "theory": theory,
            "relative_error": relative_error,
            "evidence_state": "measured",
            "assessment": "clientapi_shared_state_acceptance_only",
            "publication_claim": False,
        },
        "desktop_display_warning": display_warning,
    }


def _readback(model: Any, expected_desktop_value: str) -> dict[str, Any]:
    parameters = _parameter_expressions(model)
    if parameters.get(MCP_PARAMETER) != MCP_PARAMETER_VALUE:
        raise RuntimeError("MCP-written parameter is not preserved")
    if parameters.get(DESKTOP_PARAMETER) != expected_desktop_value:
        raise RuntimeError("Desktop-edited parameter does not match the declared value")
    measured = float(
        model.evaluate(CAPACITANCE_EXPRESSION, CAPACITANCE_UNIT).reshape(-1)[0]
    )
    if not math.isfinite(measured):
        raise RuntimeError("persisted controlled result is non-finite")
    return {
        "parameters": {
            MCP_PARAMETER: parameters[MCP_PARAMETER],
            DESKTOP_PARAMETER: parameters[DESKTOP_PARAMETER],
        },
        "persisted_result": {
            "expression": CAPACITANCE_EXPRESSION,
            "unit": CAPACITANCE_UNIT,
            "measured": measured,
            "evidence_state": "measured",
            "publication_claim": False,
        },
    }


def _saved_model_edit(model: Any, expected_desktop_value: str) -> dict[str, Any]:
    before = _parameter_expressions(model)
    if before.get(MCP_PARAMETER) != MCP_PARAMETER_VALUE:
        raise RuntimeError("saved model does not preserve the MCP-written parameter")
    if before.get(DESKTOP_PARAMETER) != expected_desktop_value:
        raise RuntimeError("saved model does not preserve the Desktop-edited parameter")
    model.java.param().set(SAVED_MODEL_PARAMETER, SAVED_MODEL_PARAMETER_VALUE)
    after = _parameter_expressions(model)
    if after.get(SAVED_MODEL_PARAMETER) != SAVED_MODEL_PARAMETER_VALUE:
        raise RuntimeError("saved-model in-memory parameter readback failed")
    return {
        "parameters_before": {
            MCP_PARAMETER: before[MCP_PARAMETER],
            DESKTOP_PARAMETER: before[DESKTOP_PARAMETER],
        },
        "parameters_after": {
            MCP_PARAMETER: after[MCP_PARAMETER],
            DESKTOP_PARAMETER: after[DESKTOP_PARAMETER],
            SAVED_MODEL_PARAMETER: after[SAVED_MODEL_PARAMETER],
        },
        "source_write_attempted": False,
        "solve_requested": False,
    }


def _saved_model_readback(model: Any, expected_desktop_value: str) -> dict[str, Any]:
    parameters = _parameter_expressions(model)
    if parameters.get(MCP_PARAMETER) != MCP_PARAMETER_VALUE:
        raise RuntimeError("saved model does not preserve the MCP-written parameter")
    if parameters.get(DESKTOP_PARAMETER) != expected_desktop_value:
        raise RuntimeError("saved model does not preserve the Desktop-edited parameter")
    if SAVED_MODEL_PARAMETER not in parameters:
        raise RuntimeError("saved working model is missing the shared edit parameter")
    return {
        "parameters": {
            MCP_PARAMETER: parameters[MCP_PARAMETER],
            DESKTOP_PARAMETER: parameters[DESKTOP_PARAMETER],
            SAVED_MODEL_PARAMETER: parameters[SAVED_MODEL_PARAMETER],
        },
        "model_mutation_requested": False,
        "solve_requested": False,
        "snapshot_requested": False,
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    spec = _spec(args)
    result: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "source_revision": _git_head(),
        "spec": spec,
        "started_at_epoch": time.time(),
    }
    manager = SharedSessionManager()
    operation_claim = None
    active_lock_sha256 = None
    attached = False
    immutable_source = None
    try:
        if args.mode in {"saved", "saved_readback"}:
            source_path = Path(spec["immutable_source_path"])
            if not source_path.is_file():
                raise RuntimeError("declared saved model source does not exist")
            immutable_source = {
                "path": str(source_path),
                "sha256": _sha256(source_path),
            }
            result["immutable_source_before"] = dict(immutable_source)
        attach = manager.attach(
            {
                "endpoint": spec["endpoint"],
                "user_confirmed": True,
            },
            profile="desktop_shared",
            environ={SHARED_SERVER_FEATURE_ENV: "true"},
        )
        result["attach"] = attach
        if not attach.get("success"):
            raise RuntimeError("shared attach was rejected")
        attached = True

        inventory = manager.models()
        result["inventory"] = inventory
        adoption = manager.adopt_model(spec["selector"])
        result["adoption"] = adoption
        if not adoption.get("success"):
            raise RuntimeError("exact shared model adoption was rejected")
        locked = manager.lock_model(
            collaboration_mode="interactive_inspection",
            immutable_source=immutable_source,
        )
        result["initial_lock"] = locked
        if not locked.get("success"):
            raise RuntimeError("shared model lock was rejected")
        active_lock_sha256 = locked["model_lock"]["lock_sha256"]
        initial_revision = locked["model_lock"]["revision"]["revision_sha256"]
        verified = manager.verify_model_lock(
            expected_lock_sha256=active_lock_sha256,
            expected_revision_sha256=initial_revision,
        )
        result["initial_verification"] = verified
        if not verified.get("success"):
            raise RuntimeError("initial shared model verification failed")

        arbiter = get_operation_arbiter()
        operation_claim, acquisition = arbiter.try_acquire(
            tool_name=f"shared_interactive_gate_{args.mode}",
            side_effect_class={
                "prepare": "solver_execution",
                "readback": "filesystem_write",
                "saved": "model_mutation",
                "saved_readback": "read_only",
            }[args.mode],
        )
        result["operation_acquisition"] = acquisition
        if operation_claim is None:
            raise RuntimeError("shared interactive gate could not acquire operation ownership")

        model = _exact_model(manager, args.model_tag)
        if args.mode == "prepare":
            result["phase"] = _prepare_capacitor(model)
        elif args.mode == "saved":
            result["phase"] = _saved_model_edit(
                model, args.expected_desktop_value
            )
        elif args.mode == "saved_readback":
            result["phase"] = _saved_model_readback(
                model, args.expected_desktop_value
            )
        else:
            result["phase"] = _readback(model, args.expected_desktop_value)

        if args.mode in {"prepare", "saved"}:
            unlocked = manager.unlock_model(
                expected_lock_sha256=active_lock_sha256,
                reason=(
                    "Advance revision after controlled licensed solver gate"
                    if args.mode == "prepare"
                    else "Advance revision after saved-model in-memory edit"
                ),
            )
            result["revision_unlock"] = unlocked
            if not unlocked.get("success"):
                raise RuntimeError("could not advance the shared model revision")
            active_lock_sha256 = None
            relocked = manager.lock_model(
                collaboration_mode="interactive_inspection",
                immutable_source=immutable_source,
            )
            result["post_solve_lock"] = relocked
            if not relocked.get("success"):
                raise RuntimeError("post-solve shared model lock was rejected")
            active_lock_sha256 = relocked["model_lock"]["lock_sha256"]
        else:
            relocked = locked

        if immutable_source is not None:
            source_sha256_after_edit = _sha256(Path(immutable_source["path"]))
            result["immutable_source_after_edit_sha256"] = source_sha256_after_edit
            if source_sha256_after_edit != immutable_source["sha256"]:
                raise RuntimeError("saved source bytes changed during in-memory edit")

        if args.mode == "saved_readback":
            result["snapshot"] = {
                "success": True,
                "state": "not_requested",
            }
        else:
            current_revision = relocked["model_lock"]["revision"]["revision_sha256"]
            snapshot = manager.snapshot_model(
                expected_lock_sha256=active_lock_sha256,
                expected_revision_sha256=current_revision,
                max_snapshot_bytes=MAX_SNAPSHOT_BYTES,
            )
            result["snapshot"] = snapshot
            if not snapshot.get("success"):
                raise RuntimeError("identity-preserving Save Copy failed")
            if immutable_source is not None:
                source_sha256_after_snapshot = _sha256(
                    Path(immutable_source["path"])
                )
                result["immutable_source_after_snapshot_sha256"] = (
                    source_sha256_after_snapshot
                )
                if source_sha256_after_snapshot != immutable_source["sha256"]:
                    raise RuntimeError("saved source bytes changed during Save Copy")
        unlocked = manager.unlock_model(
            expected_lock_sha256=active_lock_sha256,
            reason=f"Complete licensed shared {args.mode} phase",
        )
        result["final_unlock"] = unlocked
        if not unlocked.get("success"):
            raise RuntimeError("final shared model unlock failed")
        active_lock_sha256 = None

        release = arbiter.release(operation_claim)
        result["operation_release"] = release
        if not release.get("verified"):
            raise RuntimeError("operation ownership release was not verified")
        operation_claim = None
        detach = manager.detach()
        result["detach"] = detach
        if not detach.get("success"):
            raise RuntimeError("attached resource preservation failed")
        attached = False
        if immutable_source is not None:
            source_sha256_after_detach = _sha256(Path(immutable_source["path"]))
            result["immutable_source_after_detach_sha256"] = (
                source_sha256_after_detach
            )
            if source_sha256_after_detach != immutable_source["sha256"]:
                raise RuntimeError("saved source bytes changed during detach")
        result["success"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if active_lock_sha256 is not None:
            result["cleanup_unlock"] = manager.unlock_model(
                expected_lock_sha256=active_lock_sha256,
                reason="Licensed shared gate failure cleanup",
            )
            active_lock_sha256 = None
        if operation_claim is not None:
            result["cleanup_operation_release"] = get_operation_arbiter().release(
                operation_claim
            )
        if attached:
            result["cleanup_detach"] = manager.detach()
        result["finished_at_epoch"] = time.time()
        result["duration_seconds"] = round(
            result["finished_at_epoch"] - result["started_at_epoch"], 6
        )
    return result


def main() -> int:
    args = _parser().parse_args()
    try:
        spec = _spec(args)
    except ValueError as exc:
        print(f"invalid gate specification: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(json.dumps({"success": True, "dry_run": True, "spec": spec}, indent=2))
        return 0

    result = _run(args)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(args.receipt, result)
    print(json.dumps({
        "success": result["success"],
        "receipt": str(args.receipt),
        "receipt_sha256": _sha256(args.receipt),
        "error": result.get("error"),
    }, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
