"""semantic benchmark gates for benchmark, manifests, limits, and import safety."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing, contextmanager
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
    _aggregate,
    _query_metrics,
    evaluate_lexical_baseline,
)
from development_kit.tests.integration import semantic_benchmark_soak as soak_module
from development_kit.tests.integration import semantic_feature_acceptance as feature_module
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


def test_empty_semantic_benchmark_aggregate_is_serializable_and_schema_complete():
    aggregate = _aggregate([])

    assert aggregate == {
        "query_count": 0,
        "judged_query_count": 0,
        "recall_at_5": 0.0,
        "recall_at_10": 0.0,
        "mrr_at_10": 0.0,
        "ndcg_at_10": 0.0,
        "zero_result_rate": 0.0,
        "misses_at_5": 0,
        "negative_query_count": 0,
        "negative_abstention_rate": None,
    }
    assert json.loads(json.dumps(aggregate, allow_nan=False)) == aggregate
    assert (
        evaluate_semantic_continuation({"target_styles": aggregate})["continue_to_semantic_worker"]
        is False
    )


def _absent_ownership():
    return {"lease": {"state": "absent"}, "external_solver_processes": [], "collision": False}


@contextmanager
def _lexical_test_root():
    root = Path("D:/comsol_semantic_contract_test") / uuid.uuid4().hex
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_semantic_benchmark_citations_and_digest_share_one_sqlite_snapshot(tmp_path):
    source = tmp_path / "source.sqlite3"
    snapshot = tmp_path / "snapshot.sqlite3"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE pages (source TEXT NOT NULL, page INTEGER NOT NULL)")
        connection.execute("INSERT INTO pages VALUES ('manual.pdf', 7)")
        connection.commit()

    receipt = soak_module._sqlite_snapshot(source, snapshot)
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("DELETE FROM pages")
        connection.execute("INSERT INTO pages VALUES ('replacement.pdf', 9)")
        connection.commit()

    assert soak_module._corpus_citations(snapshot) == {("manual.pdf", 7)}
    assert receipt["byte_count"] == snapshot.stat().st_size
    assert receipt["sha256"] == hashlib.sha256(snapshot.read_bytes()).hexdigest()


def test_frozen_evaluation_has_sixty_six_reviewed_queries_and_declared_slices():
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


@pytest.mark.parametrize("validator", [validate_model_manifest, validate_index_manifest])
@pytest.mark.parametrize("payload", [None, [], "manifest"])
def test_semantic_manifest_validators_require_objects(validator, payload):
    with pytest.raises(ValueError, match="must be an object"):
        validator(payload)


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
    continuing["thresholds"]["minimum_target_queries"] = 0
    repeated = evaluate_semantic_continuation(
        {
            "target_styles": {
                "query_count": 0,
                "recall_at_5": 0.0,
                "misses_at_5": 0,
            }
        }
    )
    assert repeated["continue_to_semantic_worker"] is False
    assert SEMANTIC_CONTINUATION_GATE["minimum_target_queries"] > 0


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
    corpus = "d" * 64
    source = "COMSOL_Multiphysics/COMSOL_ReferenceManual.pdf"
    with _lexical_test_root() as root:
        index = root / "manuals.sqlite3"
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
                    "query": (
                        "CopyFace source destination"
                        if number < 30
                        else "CopyFace copies mesh between source and destination faces"
                    ),
                    "category": "exact_clientapi",
                    "style": "exact" if number < 30 else "paraphrase",
                    "relevant": [{"source": source, "page": 10}],
                    "judge_note": "Synthetic citation for rank-metric testing.",
                }
                for number in range(60)
            ],
        }
        result = evaluate_lexical_baseline(evaluation, index_path=index)

    assert result["query_count"] == 60
    assert result["summary"]["overall"]["recall_at_5"] == 1.0
    assert result["summary"]["overall"]["mrr_at_10"] == 1.0
    assert result["summary"]["by_style"]["paraphrase"]["recall_at_5"] == 1.0
    assert result["summary"]["by_style"]["paraphrase"]["mrr_at_10"] == 1.0
    assert result["continuation_gate"]["continue_to_semantic_worker"] is False


def test_lexical_fixture_cleanup_covers_setup_failure():
    captured = None

    with pytest.raises(RuntimeError, match="injected setup failure"):
        with _lexical_test_root() as root:
            captured = root
            root.mkdir(parents=True)
            (root / "partial.sqlite3").write_bytes(b"partial")
            raise RuntimeError("injected setup failure")

    assert captured is not None and not captured.exists()


def test_rank_metrics_validate_and_deduplicate_citations_before_dcg():
    citation = ("manual.pdf", 7)
    metrics = _query_metrics([citation, citation], {citation}, valid_citations={citation})

    assert metrics["ndcg_at_10"] == 1.0
    with pytest.raises(ValueError, match="pinned corpus"):
        _query_metrics([("invented.pdf", 99)], {citation}, valid_citations={citation})


def test_semantic_benchmark_retrieval_validates_and_deduplicates_citations():
    citation = {"source": "manual.pdf", "page": 7}

    class Manager:
        def __init__(self, results):
            self.results = results

        def query(self, *_args, **_kwargs):
            return {"success": True, "results": self.results}

    evaluation = {
        "queries": [
            {
                "id": "citation-contract",
                "query": "query",
                "category": "exact_clientapi",
                "style": "exact",
                "relevant": [citation],
            }
        ]
    }
    result = soak_module._evaluate_mode(
        Manager([citation, citation]),
        evaluation,
        "hybrid",
        {("manual.pdf", 7)},
    )

    assert result["rows"][0]["ranked_citations"] == [citation]
    assert result["citation_validity"] == 1.0

    with pytest.raises(ValueError, match="pinned corpus"):
        soak_module._evaluate_mode(
            Manager([{"source": "invented.pdf", "page": 99}]),
            evaluation,
            "hybrid",
            {("manual.pdf", 7)},
        )


def test_semantic_soak_raw_request_classifies_truncated_worker_response(monkeypatch):
    class Connection:
        calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            return None

        def sendall(self, _payload):
            return None

        def recv(self, _size):
            self.calls += 1
            return b'{"success":true}' if self.calls == 1 else b""

    monkeypatch.setattr(
        soak_module.socket,
        "create_connection",
        lambda *_args, **_kwargs: Connection(),
    )
    manager = SimpleNamespace(_token="token", _port=1234)

    result = soak_module._raw_request(manager, "request", "query")

    assert result["success"] is False
    assert result["error"]["code"] == "worker_protocol_failure"


def test_semantic_benchmark_receipts_publish_concurrently_without_temp_alias(tmp_path):
    def publish(index: int) -> None:
        soak_module._atomic_write(
            tmp_path / f"receipt-{index}.json",
            {"index": index, "payload": "x" * 64_000},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(publish, range(8)))

    for index in range(8):
        assert (
            json.loads((tmp_path / f"receipt-{index}.json").read_text(encoding="utf-8"))["index"]
            == index
        )
    assert not list(tmp_path.glob(".*.tmp"))


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
    monkeypatch.setattr(soak_module, "_manager", lambda _lexical_path: manager)
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
    runtime = Path("D:/comsol_runtime/semantic_feature/runs/test-run")
    parameters = feature_module._server("core", runtime, semantic_enabled=True)

    assert parameters.env["COMSOL_MCP_RUNTIME_DIR"] == str(runtime)
    assert parameters.env["COMSOL_MCP_PROFILE"] == "core"
    assert parameters.env["COMSOL_MCP_ENABLE_SEMANTIC_DOCS"] == "true"
    assert feature_module.RUN_LOCK.name == "acceptance.lock"
    assert feature_module.RUN_LOCK.is_absolute()


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
    expected_by_query = {item[1]: item[3] for item in retrieval_module.QUERIES}
    assert len(expected_by_query) == len(retrieval_module.QUERIES)

    class Manager:
        def __init__(self):
            self.query_count = 0

        @staticmethod
        def start():
            return {"success": True}

        def query(self, query, **_kwargs):
            self.query_count += 1
            assert query in expected_by_query, f"missing retrieval expectation for {query!r}"
            expected = expected_by_query[query]
            return {
                "success": True,
                "count": 1,
                "results": [{"source": expected[0], "page": expected[1]}],
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


def test_retrieval_acceptance_rejects_successful_but_irrelevant_results(tmp_path):
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
                "results": [{"source": "unrelated.pdf", "page": 999}],
                "ranker": {"mode": "hybrid"},
                "load_count": 1,
                "query_count": self.query_count,
            }

        @staticmethod
        def health():
            return {"success": True, "status": {"load_count": 1, "query_count": 5}}

        @staticmethod
        def reset():
            return {"success": True, "reset": {"absent": True}}

    result, exit_code = retrieval_module.run_acceptance(
        manager=Manager(),
        index_path=tmp_path / "index",
        before={"sha256": SHA_A},
        ownership_before=_absent_ownership(),
        output_path=tmp_path / "irrelevant.json",
        snapshot=lambda _path: {"sha256": SHA_A},
        ownership_status=_absent_ownership,
    )

    assert exit_code == 1
    assert result["success"] is False
    assert "missed relevant evidence" in result["error"]["message"]
    assert all(item["relevant_rank_at_5"] is None for item in result["queries"])


def test_retrieval_acceptance_rejects_dirty_initial_ownership_before_worker_start(tmp_path):
    class Manager:
        started = False

        def start(self):
            self.started = True
            return {"success": True}

        @staticmethod
        def reset():
            return {"success": True, "reset": {"absent": True}}

    manager = Manager()
    dirty = _absent_ownership()
    dirty["collision"] = True
    result, exit_code = retrieval_module.run_acceptance(
        manager=manager,
        index_path=tmp_path / "index",
        before={"sha256": SHA_A},
        ownership_before=dirty,
        output_path=tmp_path / "dirty.json",
        snapshot=lambda _path: {"sha256": SHA_A},
        ownership_status=_absent_ownership,
    )

    assert manager.started is False
    assert exit_code == 1
    assert result["success"] is False
    assert "not clean before" in result["error"]["message"]


def test_retrieval_acceptance_compares_before_and_after_ownership_snapshots():
    before = _absent_ownership()
    after = _absent_ownership()
    after["lease"]["state"] = "present"

    assert retrieval_module._ownership_summary(before) == retrieval_module.CLEAN_OWNERSHIP
    assert retrieval_module._ownership_summary(after) != retrieval_module._ownership_summary(before)
