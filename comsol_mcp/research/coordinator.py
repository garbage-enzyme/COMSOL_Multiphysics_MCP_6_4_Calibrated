"""Solver-free durable coordinator for injected research evaluators."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from comsol_mcp.durable import atomic_write_json, canonical_json_v1
from comsol_mcp.jobs.store import JobLock

from .evaluations import normalize_evaluation_record
from .journal import append_research_journal_record, recover_research_journal
from .records import normalize_candidate_record

Evaluator = Callable[[Mapping[str, Any]], object]


def _require_ascii_path(path: Path) -> None:
    try:
        str(path.resolve(strict=False)).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "research campaign runtime paths must contain ASCII characters only"
        ) from exc


def _fingerprint(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _utc_text(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class ResearchCampaignCoordinator:
    """Serialize one campaign around an injected evaluator and durable journal."""

    def __init__(
        self,
        root: str | Path,
        campaign_manifest: Mapping[str, Any],
        evaluator: Evaluator,
        *,
        evaluator_identity: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        _require_ascii_path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = json.loads(canonical_json_v1(dict(campaign_manifest)))
        if self.manifest.get("schema_name") != "comsol_mcp.research_campaign_manifest":
            raise ValueError("campaign manifest schema is unsupported")
        self.campaign_fingerprint = _fingerprint(
            self.manifest.get("campaign_fingerprint"), "campaign_fingerprint"
        )
        self.evaluator = evaluator
        self.evaluator_identity = _fingerprint(evaluator_identity, "evaluator_identity")
        self.clock = clock
        self.manifest_path = self.root / "campaign_manifest.json"
        self.runtime_path = self.root / "campaign_runtime.json"
        self.journal_path = self.root / "research_journal.jsonl"
        self.control_path = self.root / "cancel_request.json"
        self.lock_path = self.root / ".coordinator.lock"
        self._initialize_identity()

    def _now(self) -> float:
        value = self.clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("clock must return a finite nonnegative epoch")
        epoch = float(value)
        if not math.isfinite(epoch) or epoch < 0.0:
            raise ValueError("clock must return a finite nonnegative epoch")
        return epoch

    def _initialize_identity(self) -> None:
        if self.manifest_path.exists():
            existing = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if existing != self.manifest:
                raise ValueError("campaign runtime already belongs to a different manifest")
        else:
            atomic_write_json(self.manifest_path, self.manifest)
        if self.runtime_path.exists():
            runtime = json.loads(self.runtime_path.read_text(encoding="utf-8"))
            if runtime.get("campaign_fingerprint") != self.campaign_fingerprint:
                raise ValueError("campaign runtime identity is inconsistent")
        else:
            now = self._now()
            atomic_write_json(
                self.runtime_path,
                {"campaign_fingerprint": self.campaign_fingerprint, "started_at_epoch": now},
            )

    def _runtime(self) -> dict[str, Any]:
        value = json.loads(self.runtime_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("campaign runtime must be an object")
        if value.get("campaign_fingerprint") != self.campaign_fingerprint:
            raise ValueError("campaign runtime identity is inconsistent")
        return dict(value)

    def _records(self) -> list[dict[str, Any]]:
        value = recover_research_journal(self.journal_path, repair_partial_tail=True)["records"]
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ValueError("research journal records must be objects")
        return [dict(item) for item in value]

    @staticmethod
    def _tail(records: list[dict[str, Any]]) -> str | None:
        return None if not records else records[-1]["record_fingerprint"]

    def request_cancel(self, request_id: str) -> dict[str, Any]:
        if not request_id or len(request_id) > 128:
            raise ValueError("request_id must be a bounded nonempty string")
        control = {
            "campaign_fingerprint": self.campaign_fingerprint,
            "request_id": request_id,
            "requested_at": _utc_text(self._now()),
        }
        atomic_write_json(self.control_path, control)
        return control

    def _cancel_requested(self) -> bool:
        if not self.control_path.exists():
            return False
        try:
            value = json.loads(self.control_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("cancel request is unreadable or corrupt") from exc
        if (
            not isinstance(value, dict)
            or value.get("campaign_fingerprint") != self.campaign_fingerprint
        ):
            raise ValueError("cancel request belongs to a different campaign")
        return True

    def _consume_cancel(self) -> None:
        if not self.control_path.exists():
            return
        observed = self.control_path.read_bytes()
        try:
            value = json.loads(observed.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("cancel request is unreadable or corrupt") from exc
        if (
            not isinstance(value, dict)
            or value.get("campaign_fingerprint") != self.campaign_fingerprint
        ):
            return
        if self.control_path.read_bytes() == observed:
            self.control_path.unlink()

    def _evaluation_records(
        self, records: list[dict[str, Any]], candidate_fingerprint: str
    ) -> list[dict[str, Any]]:
        return [
            record["payload"]
            for record in records
            if record["kind"] == "evaluation"
            and record["payload"]["candidate_fingerprint"] == candidate_fingerprint
        ]

    def _budget(self, records: list[dict[str, Any]], now: float) -> dict[str, Any]:
        budget = self.manifest["goal"]["resource_budget"]
        started = sum(
            1
            for record in records
            if record["kind"] == "evaluation" and record["payload"]["status"] == "started"
        )
        elapsed = max(0.0, now - float(self._runtime()["started_at_epoch"]))
        return {
            "started_fem_evaluations": started,
            "max_fem_evaluations": int(budget["max_fem_evaluations"]),
            "elapsed_wall_time_seconds": elapsed,
            "max_wall_time_seconds": int(budget["max_wall_time_seconds"]),
        }

    def status(self) -> dict[str, Any]:
        with JobLock(self.lock_path):
            records = self._records()
            budget = self._budget(records, self._now())
            terminal = sum(
                1
                for record in records
                if record["kind"] == "evaluation" and record["payload"]["status"] != "started"
            )
            return {
                "campaign_fingerprint": self.campaign_fingerprint,
                "journal_record_count": len(records),
                "started_evaluations": budget["started_fem_evaluations"],
                "terminal_evaluations": terminal,
                "remaining_evaluations": max(
                    0, budget["max_fem_evaluations"] - budget["started_fem_evaluations"]
                ),
                "cancel_requested": self._cancel_requested(),
            }

    def evaluate(self, candidate: object, *, retry_failed: bool = False) -> dict[str, Any]:
        normalized_candidate = normalize_candidate_record(candidate, self.manifest["design_space"])
        if normalized_candidate["campaign_fingerprint"] != self.campaign_fingerprint:
            raise ValueError("candidate belongs to a different campaign")
        point = normalized_candidate["candidate_fingerprint"]
        with JobLock(self.lock_path):
            records = self._records()
            evaluations = self._evaluation_records(records, point)
            terminals = [item for item in evaluations if item["status"] != "started"]
            if terminals and (
                terminals[-1]["status"] in {"completed", "infeasible"} or not retry_failed
            ):
                return {
                    "status": "duplicate_terminal",
                    "evaluation": terminals[-1],
                    "replayed": True,
                }
            started_attempts = {
                item["attempt"] for item in evaluations if item["status"] == "started"
            }
            terminal_attempts = {item["attempt"] for item in terminals}
            orphaned = sorted(started_attempts - terminal_attempts)
            if orphaned:
                return {"status": "orphaned_started", "attempts": orphaned, "replayed": True}
            now = self._now()
            budget = self._budget(records, now)
            if (
                budget["started_fem_evaluations"] >= budget["max_fem_evaluations"]
                or budget["elapsed_wall_time_seconds"] >= budget["max_wall_time_seconds"]
            ):
                return {"status": "budget_exhausted", "budget": budget, "replayed": False}
            if self._cancel_requested():
                return {"status": "cancel_requested", "budget": budget, "replayed": False}
            if not any(
                record["kind"] == "candidate"
                and record["payload"]["candidate_fingerprint"] == point
                for record in records
            ):
                appended = append_research_journal_record(
                    self.journal_path,
                    "candidate",
                    normalized_candidate,
                    expected_previous_record_fingerprint=self._tail(records),
                )
                records.append(appended)
            attempt = max(started_attempts | terminal_attempts, default=0) + 1
            started_at = _utc_text(now)
            evaluation_id = f"eval-{normalized_candidate['candidate_id'][:96]}-{attempt}"
            started = normalize_evaluation_record(
                {
                    "schema_name": "comsol_mcp.research_evaluation",
                    "schema_version": "1.0.0",
                    "evaluation_id": evaluation_id,
                    "campaign_fingerprint": self.campaign_fingerprint,
                    "candidate_id": normalized_candidate["candidate_id"],
                    "candidate_fingerprint": point,
                    "attempt": attempt,
                    "status": "started",
                    "fidelity": normalized_candidate["requested_fidelity"],
                    "evaluator_identity": self.evaluator_identity,
                    "started_at": started_at,
                    "completed_at": None,
                    "response": None,
                    "evidence_fingerprints": [],
                    "failure_reason": None,
                }
            )
            appended = append_research_journal_record(
                self.journal_path,
                "evaluation",
                started,
                expected_previous_record_fingerprint=self._tail(records),
            )
            records.append(appended)
            try:
                response = self.evaluator(normalized_candidate["normalized_values"])
                status = "cancelled" if self._cancel_requested() else "completed"
                failure_reason = "cancel_requested" if status == "cancelled" else None
                response_value = None if status == "cancelled" else response
                terminal = normalize_evaluation_record(
                    {
                        **{
                            key: value
                            for key, value in started.items()
                            if key != "evaluation_fingerprint"
                        },
                        "status": status,
                        "completed_at": _utc_text(self._now()),
                        "response": response_value,
                        "failure_reason": failure_reason,
                    }
                )
            except Exception as exc:
                status = "failed"
                terminal = normalize_evaluation_record(
                    {
                        **{
                            key: value
                            for key, value in started.items()
                            if key != "evaluation_fingerprint"
                        },
                        "status": "failed",
                        "completed_at": _utc_text(self._now()),
                        "response": None,
                        "failure_reason": type(exc).__name__,
                    }
                )
            append_research_journal_record(
                self.journal_path,
                "evaluation",
                terminal,
                expected_previous_record_fingerprint=self._tail(records),
            )
            if status == "cancelled":
                self._consume_cancel()
            return {"status": status, "evaluation": terminal, "replayed": False}

    def finalize_orphaned(self, *, reason: str = "coordinator_restart") -> list[dict[str, Any]]:
        if not reason or len(reason) > 128:
            raise ValueError("reason must be a bounded nonempty string")
        finalized: list[dict[str, Any]] = []
        with JobLock(self.lock_path):
            records = self._records()
            started = {
                (record["payload"]["candidate_fingerprint"], record["payload"]["attempt"]): record[
                    "payload"
                ]
                for record in records
                if record["kind"] == "evaluation" and record["payload"]["status"] == "started"
            }
            terminal_keys = {
                (record["payload"]["candidate_fingerprint"], record["payload"]["attempt"])
                for record in records
                if record["kind"] == "evaluation" and record["payload"]["status"] != "started"
            }
            for key in sorted(set(started) - terminal_keys):
                source = started[key]
                terminal = normalize_evaluation_record(
                    {
                        **{k: v for k, v in source.items() if k != "evaluation_fingerprint"},
                        "status": "failed",
                        "completed_at": _utc_text(self._now()),
                        "response": None,
                        "failure_reason": reason,
                    }
                )
                appended = append_research_journal_record(
                    self.journal_path,
                    "evaluation",
                    terminal,
                    expected_previous_record_fingerprint=self._tail(records),
                )
                records.append(appended)
                finalized.append(terminal)
        return finalized


__all__ = ["Evaluator", "ResearchCampaignCoordinator"]
