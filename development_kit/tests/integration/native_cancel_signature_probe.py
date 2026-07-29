"""Opt-in native-cancellation probe with an optional real cancellation gate.

Environment discovery and reflection do not invoke cancellation. Supplying the
explicit model environment variable additionally runs one real
``ProgressContext.cancel()`` candidate in this isolated subprocess.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import mph

ROOT = Path(__file__).parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.jobs.native_cancel_probe import (  # noqa
    discover_environment,
    reflect_candidate_signatures,
)

NATIVE_CANCEL_PROBE_MODEL_ENV = "COMSOL_MCP_NATIVE_CANCEL_PROBE_MODEL"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _progress_context_cancel_gate(
    client,
    model_path: Path,
    temporary_root: Path,
    *,
    startup_wait_seconds: float = 4.0,
    join_timeout_seconds: float = 90.0,
) -> dict:
    """Test whether an unbound public ProgressContext can stop a real study.

    Clientapi exposes no Study.run(ProgressContext) overload.  This gate still
    invokes the public cancellation candidate once while a known real study is
    blocking. Cleanup is permitted only after that solve thread has exited.
    """
    from jpype import JClass

    before_hash = _sha256(model_path)
    copied_model = temporary_root / "probe_model.mph"
    shutil.copy2(model_path, copied_model)
    model = client.load(copied_model)
    java_model = model.java
    java_model.param().set("wl", "4.253[um]")
    outcome: dict[str, object] = {}

    def solve() -> None:
        started = time.monotonic()
        try:
            java_model.study("std1").run()
            outcome["solve_return"] = "normal"
        except Exception as exc:
            outcome["solve_return"] = f"{type(exc).__name__}: {exc}"
        finally:
            outcome["solve_elapsed_s"] = round(time.monotonic() - started, 3)

    thread = threading.Thread(target=solve, name="native-cancel-real-study", daemon=True)
    thread.start()
    time.sleep(startup_wait_seconds)
    active_before = thread.is_alive()
    candidate_started = time.monotonic()
    try:
        context = JClass("com.comsol.model.util.ProgressContext")()
        context.cancel()
        candidate_outcome = "returned"
    except Exception as exc:
        candidate_outcome = f"{type(exc).__name__}: {exc}"
    candidate_elapsed = round(time.monotonic() - candidate_started, 3)
    thread.join(timeout=join_timeout_seconds)
    thread_alive = thread.is_alive()
    result = {
        "model_copy": str(copied_model),
        "temporary_root": str(temporary_root),
        "source_sha256_before": before_hash,
        "source_sha256_after": _sha256(model_path),
        "solve_active_before_request": active_before,
        "candidate": "ProgressContext.cancel() on a newly constructed context",
        "candidate_outcome": candidate_outcome,
        "candidate_elapsed_s": candidate_elapsed,
        "thread_alive_after_join": thread_alive,
        "join_timeout_seconds": float(join_timeout_seconds),
        "cleanup_safe": not thread_alive,
        **outcome,
    }
    if thread_alive:
        result["model_remove"] = "skipped_solve_thread_active"
        return result
    try:
        client.remove(model)
        result["model_remove"] = "verified"
    except Exception as exc:
        result["model_remove"] = f"failed: {type(exc).__name__}: {exc}"
    return result


def main() -> int:
    manifest: dict = {}
    configured_model = os.environ.get(NATIVE_CANCEL_PROBE_MODEL_ENV)
    model_path = Path(configured_model).resolve() if configured_model else None
    client = None
    cleanup_safe = True
    temporary_roots: list[Path] = []
    returncode = 1
    try:
        manifest.update(discover_environment())
        client = mph.Client(cores=1)
        manifest["client"] = {
            "standalone": bool(client.standalone),
            "port": client.port,
        }
        manifest["reflection"] = reflect_candidate_signatures()
        if model_path is not None:
            if not model_path.is_file():
                raise FileNotFoundError(
                    f"native cancellation probe model does not exist: {model_path}"
                )
            temporary_root = Path(
                tempfile.mkdtemp(prefix="comsol-native-cancel-", dir=r"D:\comsol_runtime")
            )
            temporary_roots.append(temporary_root)
            cleanup_safe = False
            gate_result = _progress_context_cancel_gate(client, model_path, temporary_root)
            manifest["progress_context_gate"] = gate_result
            cleanup_safe = gate_result.get("cleanup_safe") is True
        # Class availability/signatures alone never enable native cancellation.
        gate = manifest.get("progress_context_gate", {})
        cancelled = (
            gate.get("solve_active_before_request") is True
            and gate.get("candidate_outcome") == "returned"
            and "<CANCEL>" in str(gate.get("solve_return", ""))
            and float(gate.get("solve_elapsed_s", 999.0)) < 15.0
            and gate.get("source_sha256_before") == gate.get("source_sha256_after")
            and gate.get("cleanup_safe") is True
        )
        manifest["native_cancel"] = (
            "progress_context_candidate_passed_one_run_pending_three_run_gate"
            if cancelled
            else "unsupported_pending_blocking_gate"
        )
        returncode = 0
    except Exception as exc:
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        returncode = 1
    finally:
        cleanup: dict[str, object] = {"safe_to_start": cleanup_safe}
        if client is not None:
            if cleanup_safe:
                try:
                    client.clear()
                    cleanup["client_clear"] = "verified"
                except Exception as exc:
                    cleanup["client_clear"] = f"failed: {type(exc).__name__}: {exc}"
                    returncode = 1
            else:
                cleanup["client_clear"] = "skipped_solve_thread_active_or_unverified"
                returncode = 1
        else:
            cleanup["client_clear"] = "not_started"
        temporary_cleanup = []
        clear_verified = cleanup["client_clear"] in {"verified", "not_started"}
        for temporary_root in temporary_roots:
            if cleanup_safe and clear_verified:
                try:
                    shutil.rmtree(temporary_root)
                    temporary_cleanup.append(
                        {"root": str(temporary_root), "status": "verified_absent"}
                    )
                except OSError as exc:
                    temporary_cleanup.append(
                        {
                            "root": str(temporary_root),
                            "status": f"failed: {type(exc).__name__}: {exc}",
                        }
                    )
                    returncode = 1
            else:
                temporary_cleanup.append(
                    {"root": str(temporary_root), "status": "preserved_cleanup_unverified"}
                )
        cleanup["temporary_roots"] = temporary_cleanup
        manifest["cleanup"] = cleanup
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True), flush=True)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
