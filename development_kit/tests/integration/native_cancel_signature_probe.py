"""Opt-in native cancellation environment/signature probe; it does not invoke cancellation."""

from __future__ import annotations

import json
import hashlib
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

from src.jobs.native_cancel_probe import discover_environment, reflect_candidate_signatures


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _progress_context_noop_gate(client, model_path: Path) -> dict:
    """Prove whether an unbound public ProgressContext can stop a real study.

    Clientapi exposes no Study.run(ProgressContext) overload.  This gate still
    calls the public candidate once while a known real study is blocking, so a
    normal completion is evidence that a newly constructed context is not a
    viable native cancellation route.
    """
    from jpype import JClass

    before_hash = _sha256(model_path)
    with tempfile.TemporaryDirectory(prefix="comsol-native-cancel-", dir=r"D:\comsol_runtime") as temporary:
        copied_model = Path(temporary) / "probe_model.mph"
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
            except BaseException as exc:  # record Java exceptions faithfully
                outcome["solve_return"] = f"{type(exc).__name__}: {exc}"
            finally:
                outcome["solve_elapsed_s"] = round(time.monotonic() - started, 3)

        thread = threading.Thread(target=solve, name="native-cancel-real-study", daemon=True)
        thread.start()
        time.sleep(4.0)
        active_before = thread.is_alive()
        candidate_started = time.monotonic()
        try:
            context = JClass("com.comsol.model.util.ProgressContext")()
            context.cancel()
            candidate_outcome = "returned"
        except BaseException as exc:
            candidate_outcome = f"{type(exc).__name__}: {exc}"
        candidate_elapsed = round(time.monotonic() - candidate_started, 3)
        thread.join(timeout=90)
        result = {
            "model_copy": str(copied_model),
            "source_sha256_before": before_hash,
            "source_sha256_after": _sha256(model_path),
            "solve_active_before_request": active_before,
            "candidate": "ProgressContext.cancel() on a newly constructed context",
            "candidate_outcome": candidate_outcome,
            "candidate_elapsed_s": candidate_elapsed,
            "thread_alive_after_90_s": thread.is_alive(),
            **outcome,
        }
        if not thread.is_alive():
            try:
                client.remove(model)
            except Exception as exc:
                result["model_remove_warning"] = f"{type(exc).__name__}: {exc}"
        return result


def main() -> int:
    manifest = discover_environment()
    model_path = Path(os.environ["COMSOL_durable cancellationA_PROBE_MODEL"]).resolve() if os.environ.get("COMSOL_durable cancellationA_PROBE_MODEL") else None
    client = None
    try:
        client = mph.Client(cores=1)
        manifest["client"] = {
            "standalone": bool(client.standalone),
            "port": client.port,
        }
        manifest["reflection"] = reflect_candidate_signatures()
        if model_path is not None:
            if not model_path.is_file():
                raise FileNotFoundError(f"native cancellation probe model does not exist: {model_path}")
            manifest["progress_context_gate"] = _progress_context_noop_gate(client, model_path)
        # Class availability/signatures alone never enable native cancellation.
        gate = manifest.get("progress_context_gate", {})
        cancelled = (
            gate.get("solve_active_before_request") is True
            and gate.get("candidate_outcome") == "returned"
            and "<CANCEL>" in str(gate.get("solve_return", ""))
            and float(gate.get("solve_elapsed_s", 999.0)) < 15.0
            and gate.get("source_sha256_before") == gate.get("source_sha256_after")
        )
        manifest["native_cancel"] = (
            "progress_context_candidate_passed_one_run_pending_three_run_gate"
            if cancelled
            else "unsupported_pending_blocking_gate"
        )
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    finally:
        if client is not None:
            try:
                client.clear()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
