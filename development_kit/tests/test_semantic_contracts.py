"""semantic benchmark gates for benchmark, manifests, limits, and import safety."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.knowledge.lexical_manual import build_index_from_records
from src.knowledge.semantic_contracts import (
    PUBLIC_LIMITS,
    SEMANTIC_CONTINUATION_GATE,
    THREAT_MODEL,
    canonical_json_bytes,
    evaluate_semantic_continuation,
    validate_evaluation_set,
    validate_index_manifest,
    validate_model_manifest,
)

from development_kit.benchmarks.semantic_benchmark import (
    _query_metrics,
    evaluate_lexical_baseline,
)
from development_kit.tests.integration import semantic_benchmark_soak as soak_module
from development_kit.tests.integration import semantic_profile_acceptance as profile_module
from development_kit.tests.integration import semantic_retrieval_acceptance as retrieval_module
from development_kit.tests.integration import semantic_worker_containment as containment_module
from development_kit.tests.integration.semantic_benchmark_soak import _promotion
from development_kit.tests.semantic_test_support import isolated_semantic_environment

ROOT = Path(__file__).parents[2]
EVALUATION_PATH = (
    ROOT / "development_kit" / "tests" / "fixtures" / "semantic_retrieval_evaluation.json"
)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _absent_ownership():
    return {"lease": {"state": "absent"}, "external_solver_processes": [], "collision": False}


def test_frozen_evaluation_has_sixty_reviewed_queries_and_declared_slices():
    payload = json.loads(EVALUATION_PATH.read_text(encoding="utf-8"))
    result = validate_evaluation_set(payload)

    assert len(result["queries"]) == 66
    assert len({item["id"] for item in result["queries"]}) == 66
    assert {item["category"] for item in result["queries"]} == {
        "exact_clientapi",
        "wave_optics",
        "conventional_fem",
        "troubleshooting",
        "negative",
    }
    assert {item["style"] for item in result["queries"]} == {
        "exact",
        "paraphrase",
        "multi_concept",
        "zh_cross_language",
    }
    assert all(item["judge_note"] for item in result["queries"])
    assert sum(item["expected_no_relevant"] for item in result["queries"]) == 6


def test_model_and_index_manifests_require_ascii_absolute_identity_paths():
    model = validate_model_manifest(
        {
            "schema_version": "1",
            "model_id": "all-MiniLM-L6-v2",
            "revision": "local-test",
            "model_path": "D:/comsol_semantic/models/minilm/local-test",
            "model_sha256": SHA_A,
            "dimension": 384,
            "license": "Apache-2.0",
        }
    )
    index = validate_index_manifest(
        {
            "schema_version": "1",
            "build_id": "fixture-v1",
            "index_path": "D:/comsol_semantic/indexes/corpus/model/fixture-v1",
            "corpus_fingerprint": SHA_A,
            "lexical_index_sha256": SHA_B,
            "model_manifest_sha256": SHA_C,
            "chunk_count": 100,
            "vector_dimension": 384,
            "distance_metric": "cosine",
            "file_set_sha256": SHA_A,
        }
    )

    assert model["dimension"] == index["vector_dimension"] == 384
    with pytest.raises(ValueError, match="ASCII"):
        validate_model_manifest({**model, "model_path": "C:/Users/陆星/model"})
    with pytest.raises(ValueError, match="absolute"):
        validate_model_manifest({**model, "model_path": "models/local-test"})
    with pytest.raises(ValueError, match="ASCII"):
        validate_index_manifest({**index, "index_path": "C:/Users/陆星/index"})
    with pytest.raises(ValueError, match="absolute"):
        validate_index_manifest({**index, "index_path": "indexes/fixture-v1"})
    with pytest.raises(ValueError, match="positive integer"):
        validate_index_manifest({**index, "chunk_count": 0})


def test_contract_json_rejects_nonfinite_values_and_limits_are_bounded():
    with pytest.raises(ValueError):
        canonical_json_bytes({"distance": float("nan")})

    assert PUBLIC_LIMITS["query_deadline_seconds"] == 5.0
    assert PUBLIC_LIMITS["maximum_results"] == 10
    assert PUBLIC_LIMITS["maximum_response_bytes"] == 65_536
    assert "no_solver_lease_or_COMSOL_start" in THREAT_MODEL["containment"]


def test_semantic_continuation_gate_requires_a_material_target_slice_gap():
    blocked = evaluate_semantic_continuation(
        {"target_styles": {"query_count": 30, "recall_at_5": 0.9, "misses_at_5": 3}}
    )
    continuing = evaluate_semantic_continuation(
        {
            "target_styles": {
                "query_count": SEMANTIC_CONTINUATION_GATE["minimum_target_queries"],
                "recall_at_5": SEMANTIC_CONTINUATION_GATE["maximum_lexical_recall_at_5"],
                "misses_at_5": SEMANTIC_CONTINUATION_GATE["minimum_target_misses_at_5"],
            }
        }
    )

    assert blocked["continue_to_semantic_worker"] is False
    assert continuing["continue_to_semantic_worker"] is True


@pytest.mark.parametrize(
    "target",
    [
        {"query_count": "20", "recall_at_5": "0.8", "misses_at_5": "10"},
        {"query_count": 20, "recall_at_5": -1.0, "misses_at_5": 10},
        {"query_count": 20, "recall_at_5": 0.8, "misses_at_5": 21},
        {"query_count": 20.9, "recall_at_5": 0.8, "misses_at_5": 10},
    ],
)
def test_semantic_continuation_rejects_coercive_or_impossible_metrics(target):
    with pytest.raises(ValueError):
        evaluate_semantic_continuation({"target_styles": target})


def test_semantic_continuation_requires_an_object_baseline():
    with pytest.raises(ValueError, match="baseline must be an object"):
        evaluate_semantic_continuation([])


def test_zero_lexical_baseline_cannot_create_infinite_promotion_gain():
    lexical = {
        "summary": {
            "by_style": {"exact": {"recall_at_5": 1.0}},
            "paraphrase_multi": {"recall_at_5": 0.0},
        },
        "citation_validity": 1.0,
        "latency_seconds": {"p95": 0.1, "maximum": 0.2},
    }
    hybrid = {
        "summary": {
            "by_style": {"exact": {"recall_at_5": 1.0}},
            "paraphrase_multi": {"recall_at_5": 0.01},
            "overall": {"negative_abstention_rate": 1.0},
        },
        "citation_validity": 1.0,
        "latency_seconds": {"p95": 0.1, "maximum": 0.2},
    }

    result = _promotion(lexical, hybrid)

    assert result["passed"] is False
    assert result["measurements"]["relative_gain"] is None
    json.dumps(result, allow_nan=False)


def test_lexical_baseline_computes_rank_metrics_without_semantic_dependencies():
    root = Path("D:/comsol_semantic_contract_test") / uuid.uuid4().hex
    index = root / "manuals.sqlite3"
    corpus = "d" * 64
    source = "COMSOL_Multiphysics/COMSOL_ReferenceManual.pdf"
    try:
        build_index_from_records(
            [
                {
                    "source": source,
                    "module": "COMSOL_Multiphysics",
                    "page": 10,
                    "heading": "Copy Face",
                    "text": "CopyFace copies a mesh from source to destination faces.",
                }
            ],
            index,
            corpus_fingerprint=corpus,
        )
        evaluation = {
            "schema_version": "1",
            "name": "unit",
            "frozen_at": "2026-07-13",
            "corpus_fingerprint": corpus,
            "queries": [
                {
                    "id": f"q{number:02d}",
                    "query": "CopyFace source destination",
                    "category": "exact_clientapi",
                    "style": "exact" if number < 30 else "paraphrase",
                    "relevant": [{"source": source, "page": 10}],
                    "judge_note": "Synthetic exact citation for rank-metric testing.",
                }
                for number in range(60)
            ],
        }
        result = evaluate_lexical_baseline(evaluation, index_path=index)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    assert result["query_count"] == 60
    assert result["summary"]["overall"]["recall_at_5"] == 1.0
    assert result["summary"]["overall"]["mrr_at_10"] == 1.0
    assert result["continuation_gate"]["continue_to_semantic_worker"] is False


def test_rank_metrics_validate_and_deduplicate_citations_before_dcg():
    citation = ("manual.pdf", 7)
    metrics = _query_metrics([citation, citation], {citation}, valid_citations={citation})

    assert metrics["ndcg_at_10"] == 1.0
    with pytest.raises(ValueError, match="pinned corpus"):
        _query_metrics([("invented.pdf", 99)], {citation}, valid_citations={citation})


def test_semantic_benchmark_installs_cleanup_before_process_inspection(monkeypatch):
    class Manager:
        def __init__(self):
            self.reset_called = False

        def start(self):
            return {"success": True, "identity": {"pid": 123}}

        def reset(self):
            self.reset_called = True
            return {"success": True, "reset": {"absent": True}}

    manager = Manager()
    monkeypatch.setattr(soak_module, "_manager", lambda: manager)
    monkeypatch.setattr(
        soak_module.psutil,
        "Process",
        lambda _pid: (_ for _ in ()).throw(RuntimeError("injected RSS failure")),
    )

    with pytest.raises(RuntimeError, match="RSS failure"):
        with soak_module._managed_worker("benchmark"):
            pass

    assert manager.reset_called is True


def test_semantic_worker_inventory_matches_actual_module_command(monkeypatch):
    processes = [
        SimpleNamespace(
            info={
                "pid": 1,
                "cmdline": ["python", "-m", "comsol_mcp.knowledge.semantic_worker", "--serve"],
            }
        ),
        SimpleNamespace(
            info={"pid": 2, "cmdline": ["python", "-m", "src.knowledge.semantic_worker", "--serve"]}
        ),
    ]
    monkeypatch.setattr(containment_module.psutil, "process_iter", lambda _fields: processes)

    assert containment_module._semantic_worker_pids() == [1]


def test_semantic_acceptance_uses_isolated_runtime_and_run_lock():
    runtime = Path("D:/comsol_runtime/semantic_profile/runs/test-run")
    parameters = profile_module._server("semantic_docs", runtime)

    assert parameters.env["COMSOL_MCP_RUNTIME_DIR"] == str(runtime)
    assert profile_module.RUN_LOCK.name == "acceptance.lock"
    assert profile_module.RUN_LOCK.is_absolute()


def test_concurrent_burst_requires_success_busy_and_no_unexpected_failures():
    responses = [
        {"success": True},
        {"success": False, "error": {"code": "busy"}},
        {"success": False, "error": {"code": "backend_failure"}},
    ]

    assert soak_module._classify_burst(responses) == {
        "requests": 3,
        "successes": 1,
        "busy": 1,
        "unexpected_failures": 1,
    }


def test_control_probe_watchdog_and_query_future_propagate_failures(monkeypatch):
    monkeypatch.setattr(
        containment_module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("control-probe", 10)
        ),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        containment_module._poll_controls(Path("D:/runtime"), "job-test")

    future = Future()
    future.set_exception(RuntimeError("query thread failed"))
    with pytest.raises(RuntimeError, match="query thread failed"):
        containment_module._require_future_result(future, 1.0)


def test_semantic_subprocess_environment_removes_host_overrides(monkeypatch):
    monkeypatch.setenv("COMSOL_SEMANTIC_ROOT", "D:/host-state")
    monkeypatch.setenv("COMSOL_SEMANTIC_MODEL_PATH", "D:/host-model")
    monkeypatch.setenv("PYTHONPATH", "D:/host-python")
    monkeypatch.setenv("PYTHONHOME", "D:/host-home")

    environment = isolated_semantic_environment()

    assert "COMSOL_SEMANTIC_ROOT" not in environment
    assert "COMSOL_SEMANTIC_MODEL_PATH" not in environment
    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows detached process audit")
def test_import_audit_hook_observes_a_detached_subprocess():
    code = r"""
import json, subprocess, sys
events = []
sys.addaudithook(
    lambda event, args: events.append(event)
    if event in {'os.system', 'os.startfile', 'os.spawn', 'os.posix_spawn', 'subprocess.Popen'} else None
)
process = subprocess.Popen(
    [sys.executable, '-c', 'pass'],
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
)
process.wait(timeout=10)
print(json.dumps(events))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
        env=isolated_semantic_environment(),
    )

    assert completed.returncode == 0, completed.stderr
    assert "subprocess.Popen" in json.loads(completed.stdout)


def test_semantic_contract_imports_do_not_load_heavy_semantic_or_comsol_modules():
    code = """
import json, sys
process_launch_events = []
sys.addaudithook(
    lambda event, args: process_launch_events.append(event)
    if event in {'os.system', 'os.startfile', 'os.spawn', 'os.posix_spawn', 'subprocess.Popen'} else None
)
import src.knowledge.semantic_contracts
import development_kit.benchmarks.semantic_benchmark
import psutil
for name in ('chromadb', 'torch', 'sentence_transformers', 'mph'):
    assert name not in sys.modules, name
assert psutil.Process().children(recursive=True) == []
assert process_launch_events == [], process_launch_events
print(json.dumps({'ok': True, 'children': 0, 'launches': process_launch_events}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=isolated_semantic_environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["ok"] is True
    assert json.loads(completed.stdout)["children"] == 0


def test_retrieval_acceptance_preserves_primary_failure_through_reset_failure(tmp_path):
    class Manager:
        @staticmethod
        def start():
            return {"success": True}

        @staticmethod
        def query(*_args, **_kwargs):
            raise RuntimeError("primary retrieval failure")

        @staticmethod
        def reset():
            raise OSError("cleanup reset failure")

    output = tmp_path / "failed.json"
    result, exit_code = retrieval_module.run_acceptance(
        manager=Manager(),
        index_path=tmp_path / "index",
        before={"sha256": SHA_A},
        ownership_before=_absent_ownership(),
        output_path=output,
        snapshot=lambda _path: {"sha256": SHA_A},
        ownership_status=_absent_ownership,
    )

    assert exit_code == 1
    assert result["error"]["message"] == "primary retrieval failure"
    assert result["cleanup"]["steps"]["worker_reset"]["error_type"] == "OSError"
    assert json.loads(output.read_text(encoding="utf-8")) == result


def test_retrieval_acceptance_rejects_unsuccessful_worker_reset(tmp_path):
    class Manager:
        def __init__(self):
            self.query_count = 0

        @staticmethod
        def start():
            return {"success": True}

        def query(self, *_args, **_kwargs):
            self.query_count += 1
            return {
                "success": True,
                "count": 1,
                "results": [{"source": "manual.pdf", "page": 1}],
                "ranker": {"mode": "hybrid"},
                "load_count": 1,
                "query_count": self.query_count,
            }

        @staticmethod
        def health():
            return {"success": True, "status": {"load_count": 1, "query_count": 5}}

        @staticmethod
        def reset():
            return {"success": False, "reset": {"absent": False}}

    result, exit_code = retrieval_module.run_acceptance(
        manager=Manager(),
        index_path=tmp_path / "index",
        before={"sha256": SHA_A},
        ownership_before=_absent_ownership(),
        output_path=tmp_path / "reset-failed.json",
        snapshot=lambda _path: {"sha256": SHA_A},
        ownership_status=_absent_ownership,
    )

    assert exit_code == 1
    assert result["success"] is False
    assert result["worker_reset"]["success"] is False
    assert result["cleanup"]["steps"]["worker_reset"]["passed"] is False
