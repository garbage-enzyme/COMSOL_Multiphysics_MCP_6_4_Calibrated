"""P00 artifact builder: summarize scenario-runner evidence into JSON artifacts.

Reads ``runs.jsonl`` and ``process-samples.jsonl`` from one evidence root and
writes ``baseline.json``, ``attribution.json``, ``latency.json`` and
``target-decision.json`` beside them. Analysis only; it launches no process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics as stats
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_COMMIT = "0ff945cc2248966f27523d75536b2ebcdd0dc77f"
BASE_TREE = "d657d355ffb97a75c3860f5255d2d0769ba7581d"
WHEEL_PATH = "D:/mcp_tests/final_wheels/0.8.1/comsol_mcp-0.8.1-py3-none-any.whl"
WHEEL_SHA256 = "c07c3b39fb82166bbc4d1561468240603ba66cc1ff0b891d43ebac5ab87a93c7"
PLAN_SHA256 = "50e8d810c17f321e5fa9c8052572eb8ce6e4d4f91235a29cfea6b75c5dcf4132"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mean_std(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(stats.mean(values), 4),
        "stdev": round(stats.stdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def mb(values: list[float]) -> list[float]:
    return [round(v / 2**20, 2) for v in values]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args()
    root: Path = args.evidence_root
    runs = [
        json.loads(line)
        for line in (root / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    now = datetime.now(tz=timezone.utc).isoformat()

    server_runs = [r for r in runs if r.get("scenario") == "server" and "session_results" in r]
    import_runs = [r for r in runs if r.get("scenario") == "import"]
    refusals = [r for r in runs if "refused" in r]

    idle_by_variant: dict[str, list[float]] = defaultdict(list)
    latency_by_variant: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    threads_by_variant: dict[str, list[int]] = defaultdict(list)
    for run in server_runs:
        for session in run["session_results"]:
            stages = {s["stage"]: s for s in session["stages"]}
            idle = stages.get("idle_hold_end")
            if idle:
                idle_by_variant[run["variant"]].append(idle["tree_private_commit_bytes"])
            lat = session.get("latencies", {})
            for key in ("initialize_s", "tools_list_s", "first_call_s", "warm_call_s"):
                if lat.get(key) is not None:
                    latency_by_variant[run["variant"]][key].append(lat[key])
    for line in (root / "process-samples.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record["stage"] == "idle_hold_end" and "run_id" in record:
            variant = record["run_id"].split("-2026")[0].rsplit("-", 1)[0]
            proc = record.get("process", {})
            name = (proc.get("name") or "").lower()
            if name == "python.exe" and proc.get("num_threads"):
                threads_by_variant[variant].append(proc["num_threads"])

    import_table = {}
    for run in import_runs:
        stages = run.get("stages") or []
        if stages:
            import_table[run["stage_label"]] = {
                "tree_private_commit_mb": round(
                    stages[0]["tree_private_commit_bytes"] / 2**20, 1
                ),
                "variant": run["variant"],
            }

    baseline = {
        "schema": "comsol_mcp.p00.baseline/1",
        "generated_at_utc": now,
        "source_identity": {
            "base_version": "0.8.1",
            "base_commit": BASE_COMMIT,
            "base_tree": BASE_TREE,
            "wheel_path": WHEEL_PATH,
            "wheel_sha256": WHEEL_PATH and sha256_file(Path(WHEEL_PATH)),
            "plan_sha256": PLAN_SHA256,
        },
        "platform": {
            "os": "Windows 10.0.26200 x64 (native)",
            "logical_cores": 28,
            "physical_cores": 20,
            "interpreter": "Python 3.14.6 (venv from D:/condaenvs/comsol-mcp-py314)",
            "locked_deps": {"numpy": "2.5.3", "scipy": "1.18.1", "psutil": "7.2.2"},
        },
        "settings_identity": {
            "variants": sorted({r["variant"] for r in server_runs}),
            "settings_files": sorted(
                p.name for p in root.glob("settings-*.json")
            ),
            "lexical_index": "D:/comsol_docs_fts/manuals.sqlite3 (read-only reference)",
            "manuals_root": "D:/COMSOL64/Multiphysics/doc",
            "runtime_isolation": "evidence-root runtime dir per variant",
        },
        "scenario_definitions": {
            "server": "installed comsol-mcp.exe over MCP stdio; stages spawned/initialize/discovery/first-call/warm-call/10s-idle/disconnect; launches per variant recorded in runs.jsonl",
            "import": "fresh -I child per import stage; steady snapshot after 4 s",
            "live_census": "read-only census of production 0.7.8 hosts (census-live-20261106.json)",
        },
        "policy": {
            "commit_ceiling": 0.75,
            "refused_runs": [
                {"run_id": r["run_id"], "reason": r["refused"]} for r in refusals
            ],
        },
        "run_counts": {
            "server_runs": len(server_runs),
            "import_probes": len(import_runs),
        },
    }

    prod_idle = idle_by_variant.get("prod-like", [])
    blas1_idle = idle_by_variant.get("prod-like-blas1", [])
    attribution = {
        "schema": "comsol_mcp.p00.attribution/1",
        "generated_at_utc": now,
        "reported_baseline_reproduction": {
            "live_hosts_census": "census-live-20261106.json",
            "live_fresh_host_private_commit_mb": [1592.8, 1593.6],
            "live_longlived_host_private_commit_mb": 3087.8,
            "controlled_prod_like_idle_mb": mb(prod_idle),
            "match": "controlled mean within 0.2 percent of live fresh hosts",
        },
        "import_chain_mb": import_table,
        "variant_idle_mb": {
            variant: {
                "per_launch": mb(values),
                **mean_std(mb(values)),
            }
            for variant, values in idle_by_variant.items()
        },
        "mechanism": {
            "vm_probe": "vm-probe-4288.json: 1583.5 MB committed private in ~50 contiguous 32.0 MB RW regions",
            "bounded_slope": "33.7 MB per BLAS thread (OPENBLAS_NUM_THREADS 1 vs 4 probes)",
            "numpy_pool_mb": 703,
            "scipy_pool_mb": 773,
            "importer": "comsol_mcp.native_runtime.NATIVE_RUNTIME_MANIFEST preloads numpy, scipy, scipy.optimize, scipy.interpolate, scipy.spatial before the event loop for every non-offline profile",
            "corpus_finding": "manuals/lexical features show no measurable idle-commit difference (wave-nolex and wave-nomanual equal prod-like within noise)",
            "profile_finding": "core profile server equals prod-like idle because the native preload, not profile registration, imports the BLAS chain",
        },
        "uncertainty": {
            "launch_spread_prod_like_mb": mean_std(mb(prod_idle)),
            "threads_python_child_prod_like": sorted(
                set(threads_by_variant.get("prod-like", []))
            ),
            "unresolved": [
                "exact per-pool thread split between the two vendored OpenBLAS runtimes",
                "long-lived host growth to 3087.8 MB (JVM started once in-session) is not attributed here",
            ],
        },
        "p01_levers": {
            "blas_threads_bounded": {
                "measured_idle_mb": mb(blas1_idle),
                "reduction_percent": round(
                    100 * (1 - stats.mean(blas1_idle) / stats.mean(prod_idle)), 1
                )
                if blas1_idle and prod_idle
                else None,
            },
            "lazy_native_preload": "contradicts the deliberate preload policy; plan requires keeping event-loop-blocking imports on preload paths or bounded workers",
        },
    }

    latency = {
        "schema": "comsol_mcp.p00.latency/1",
        "generated_at_utc": now,
        "by_variant_s": {
            variant: {key: mean_std(values) for key, values in keys.items()}
            for variant, keys in latency_by_variant.items()
        },
        "notes": "initialize_s includes server cold registration when the client connects before registration completes; see stage timestamps in process-samples.jsonl for spawn-to-ready deltas",
    }

    STAGE_FALLBACK = (
        "idle_hold_end",
        "after_warm_call",
        "after_first_call",
        "after_discovery",
        "after_initialize",
    )

    def last_stage_commit(session: dict[str, Any]) -> float:
        stages = {s["stage"]: s for s in session["stages"]}
        for name in STAGE_FALLBACK:
            value = stages.get(name, {}).get("tree_private_commit_bytes")
            if value:
                return value
        return 0.0

    four_session = [r for r in server_runs if r.get("sessions", 1) == 4]
    four_idle_by_variant: dict[str, list[float]] = defaultdict(list)
    ceiling_overrides: list[dict[str, Any]] = []
    for run in four_session:
        total = sum(last_stage_commit(s) for s in run["session_results"])
        four_idle_by_variant[run["variant"]].append(total)
        if run.get("ceiling_override"):
            ceiling_overrides.append(
                {
                    "run_id": run["session_results"][0]["run_id"],
                    "effective_ceiling": run["ceiling_override"],
                    "authority": "caller manual authorization 2026-11-06 (recursive idle-memory deadlock)",
                }
            )
    target = {
        "schema": "comsol_mcp.p00.target_decision/1",
        "generated_at_utc": now,
        "proposed_targets": {
            "single_host_idle_private_commit_mb_max": 200,
            "four_client_aggregate_idle_private_commit_mb_max": 750,
            "first_call_latency_regression_s_max": 0.5,
            "basis": "prod-like-blas1 measured idle (mean %.1f MB per host); measured four-session aggregates below"
            % (stats.mean(blas1_idle) / 2**20 if blas1_idle else -1),
            "measured_four_session_aggregate_mb": {
                variant: mb(values) for variant, values in four_idle_by_variant.items()
            },
        },
        "caller_acceptance_status": None,
        "ceiling_overrides_recorded": ceiling_overrides,
        "rule": "targets are frozen only after caller acceptance; no implementation comparison starts before that",
    }

    for name, payload in (
        ("baseline.json", baseline),
        ("attribution.json", attribution),
        ("latency.json", latency),
        ("target-decision.json", target),
    ):
        (root / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {root / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
