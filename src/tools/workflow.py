"""Reusable workflow tools for COMSOL studies.

These tools capture patterns that are useful across projects:

- staged parameter sweeps that write CSV rows after each solved point
- mesh-convergence checks that rebuild, solve, and evaluate per mesh level

They intentionally do not encode project-specific physics, materials, or
variable names. Callers provide the parameter, study step, mesh feature, and
expressions that make sense for the model at hand.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import numpy as np
from mcp.server.fastmcp import FastMCP

from .session import session_manager
from .results import _json_safe
from .study import _resolve_study_tag


SWEEP_SCHEMA_VERSION = "2"
DEFAULT_RESPONSE_TAIL = 5


def _format_parameter_value(value: Any, unit: Optional[str] = None) -> str:
    """Format a COMSOL parameter value, adding a unit for numeric inputs."""
    if isinstance(value, str):
        return value
    if unit:
        return f"{value}[{unit}]"
    return str(value)


def _format_study_step_value(value: Any) -> str:
    """Format a study-step property value such as a Wavelength plist entry."""
    if isinstance(value, str):
        # Study step lists usually want the bare value, not "4e-6[m]".
        if "[" in value and value.endswith("]"):
            return value.split("[", 1)[0]
        return value
    return str(value)


def _ensure_parent_dir(file_path: Optional[str]) -> None:
    if not file_path:
        return
    Path(file_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _scalarize(value: Any) -> Any:
    """Return a JSON/CSV-friendly scalar or list from an MPh evaluate result."""
    arr = np.asarray(value)
    if arr.size == 0:
        return None
    if arr.size == 1:
        scalar = arr.reshape(-1)[0]
        if np.iscomplexobj(arr):
            return _json_safe(complex(scalar))
        return float(scalar)
    if np.iscomplexobj(arr):
        return [_json_safe(complex(v)) for v in arr.reshape(-1)]
    return [float(v) for v in arr.reshape(-1)]


def _csv_value(value: Any) -> Any:
    if isinstance(value, complex):
        return f"{value.real}+{value.imag}i"
    if isinstance(value, dict) and set(value) == {"real", "imag"}:
        return f"{value['real']}+{value['imag']}i"
    if isinstance(value, list):
        return ";".join(_csv_value(v) for v in value)
    return value


def _evaluate_expressions(model, expressions: Sequence[str]) -> dict[str, Any]:
    results = model.evaluate(list(expressions))
    if len(expressions) == 1:
        return {expressions[0]: _scalarize(results)}
    return {
        expr: _scalarize(value)
        for expr, value in zip(expressions, results)
    }


def _write_rows_csv(
    csv_path: Optional[str],
    fieldnames: Sequence[str],
    rows: Sequence[dict[str, Any]],
    append: bool,
) -> None:
    if not csv_path:
        return
    _ensure_parent_dir(csv_path)
    path = Path(csv_path)
    mode = "a" if append and path.exists() else "w"
    active_fieldnames = list(fieldnames)
    if mode == "a":
        with path.open(newline="", encoding="utf-8-sig") as existing:
            reader = csv.DictReader(existing)
            existing_fields = reader.fieldnames or []
            existing_rows = list(reader)
        active_fieldnames = [
            *existing_fields,
            *(field for field in fieldnames if field not in existing_fields),
        ]
        if existing_fields != active_fieldnames:
            with path.open("w", newline="", encoding="utf-8") as migrated:
                writer = csv.DictWriter(
                    migrated,
                    fieldnames=active_fieldnames,
                    extrasaction="ignore",
                )
                writer.writeheader()
                for row in existing_rows:
                    if "status" in active_fieldnames and not row.get("status"):
                        row["status"] = "success"
                    writer.writerow(
                        {key: _csv_value(row.get(key)) for key in active_fieldnames}
                    )
                migrated.flush()
                os.fsync(migrated.fileno())
    with path.open(mode, newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=active_fieldnames,
            extrasaction="ignore",
        )
        if mode == "w":
            writer.writeheader()
        for row in rows:
            writer.writerow(
                {key: _csv_value(row.get(key)) for key in active_fieldnames}
            )
        handle.flush()
        os.fsync(handle.fileno())


def _read_csv_rows(csv_path: Optional[str]) -> list[dict[str, str]]:
    """Read an existing workflow journal, returning no rows when absent."""
    if not csv_path:
        return []
    path = Path(csv_path)
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _read_csv_journal(csv_path: Optional[str]) -> tuple[list[str], list[dict[str, str]]]:
    if not csv_path:
        return [], []
    path = Path(csv_path)
    if not path.exists() or path.stat().st_size == 0:
        return [], []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _model_identity(model, source_model_path: Optional[str]) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "model_name": str(model.name()),
        "source_path": None,
        "source_sha256": None,
        "source_size": None,
        "source_mtime_ns": None,
    }
    if not source_model_path:
        return identity
    path = Path(source_model_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"source_model_path does not exist: {path}")
    stat = path.stat()
    identity.update(
        # Checkpoint saves can rename the in-memory model.  When an immutable
        # source is supplied, its path/hash—not that mutable runtime label—is
        # the provenance identity used for resume compatibility.
        model_name=None,
        source_path=str(path),
        source_sha256=_sha256_file(path),
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
    )
    return identity


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_parent_dir(str(path))
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _prepare_sweep_manifest(
    model,
    *,
    parameter_name: str,
    parameter_values: Sequence[Any],
    parameter_unit: Optional[str],
    expressions: Sequence[str],
    study_tag: str,
    study_step_tag: Optional[str],
    study_step_property: str,
    study_step_unit: Optional[str],
    csv_path: Optional[str],
    manifest_path: Optional[str],
    source_model_path: Optional[str],
    config_id: Optional[str],
    record_wavelength_controls: bool,
    physical_bounds: Optional[dict[str, Sequence[float]]],
    resume_csv: bool,
    append_csv: bool,
    allow_legacy_resume: bool,
) -> tuple[dict[str, Any], Optional[Path], bool]:
    model_identity = _model_identity(model, source_model_path)
    spec = {
        "workflow": "staged_parametric_sweep",
        "model": model_identity,
        "study_tag": study_tag,
        "study_step_tag": study_step_tag,
        "study_step_property": study_step_property,
        "study_step_unit": study_step_unit,
        "parameter_name": parameter_name,
        "parameter_unit": parameter_unit,
        "requested_values": [
            _format_parameter_value(value, parameter_unit) for value in parameter_values
        ],
        "expressions": list(expressions),
        "record_wavelength_controls": record_wavelength_controls,
        "physical_bounds": physical_bounds or {},
    }
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    spec_fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    active_config_id = config_id or f"sweep-{spec_fingerprint[:16]}"
    manifest = {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "config_id": active_config_id,
        "spec_fingerprint": spec_fingerprint,
        "created_at_epoch": time.time(),
        "spec": spec,
    }
    path = Path(manifest_path).expanduser().resolve() if manifest_path else None
    if path is None and csv_path:
        path = Path(str(Path(csv_path).expanduser().resolve()) + ".manifest.json")

    adopted_legacy = False
    if path and path.is_file() and (resume_csv or append_csv):
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot parse sweep manifest {path}: {exc}") from exc
        for key in ("schema_version", "config_id", "spec_fingerprint"):
            if existing.get(key) != manifest.get(key):
                raise ValueError(
                    f"Sweep manifest mismatch for {key}: "
                    f"existing={existing.get(key)!r}, active={manifest.get(key)!r}"
                )
        manifest = existing
    elif path and (resume_csv or append_csv) and csv_path and Path(csv_path).exists():
        if not allow_legacy_resume:
            raise ValueError(
                f"Existing CSV has no manifest at {path}; set allow_legacy_resume=true "
                "only after auditing that legacy file"
            )
        adopted_legacy = True

    if path and (not path.exists() or not (resume_csv or append_csv) or adopted_legacy):
        _atomic_write_json(path, manifest)
    return manifest, path, adopted_legacy


def _finite_csv_value(value: Optional[str]) -> tuple[bool, list[float]]:
    if value is None or not str(value).strip():
        return False, []
    numbers: list[float] = []
    for item in str(value).split(";"):
        token = item.strip()
        try:
            if token.endswith("i"):
                parsed = complex(token[:-1].replace("+-", "-") + "j")
                components = [float(parsed.real), float(parsed.imag)]
            else:
                components = [float(token)]
        except (TypeError, ValueError):
            return False, []
        if not all(math.isfinite(component) for component in components):
            return False, []
        numbers.extend(components)
    return True, numbers


def _resume_completed_values(
    csv_path: Optional[str],
    *,
    fieldnames: Sequence[str],
    manifest: dict[str, Any],
    expressions: Sequence[str],
    physical_bounds: Optional[dict[str, Sequence[float]]],
    adopted_legacy: bool,
) -> tuple[set[str], int]:
    existing_fields, rows = _read_csv_journal(csv_path)
    if not rows:
        return set(), 0
    required = {"parameter_value", *expressions}
    wavelength_fields = {
        "evaluated_wl",
        "evaluated_c_const_over_ewfd_freq",
    }
    if wavelength_fields <= set(fieldnames):
        required.update(wavelength_fields)
    if not adopted_legacy:
        required.update({"schema_version", "config_id", "status"})
    missing = sorted(required - set(existing_fields))
    if missing:
        raise ValueError(f"CSV schema is missing required columns: {missing}")

    completed: set[str] = set()
    invalid = 0
    for row in rows:
        status = (row.get("status") or ("success" if adopted_legacy else "")).strip().lower()
        if status not in ({"success", "ok"} if adopted_legacy else {"ok"}):
            invalid += 1
            continue
        if not adopted_legacy and (
            row.get("schema_version") != SWEEP_SCHEMA_VERSION
            or row.get("config_id") != manifest["config_id"]
        ):
            raise ValueError("CSV contains rows from a different schema or config_id")
        valid = True
        parsed_values: dict[str, list[float]] = {}
        for expression in expressions:
            finite, values = _finite_csv_value(row.get(expression))
            if not finite:
                valid = False
                break
            parsed_values[expression] = values
        for wavelength_field in wavelength_fields & required:
            finite, _ = _finite_csv_value(row.get(wavelength_field))
            if not finite:
                valid = False
                break
        if valid:
            for expression, bounds in (physical_bounds or {}).items():
                if expression not in parsed_values or len(bounds) != 2:
                    valid = False
                    break
                low, high = float(bounds[0]), float(bounds[1])
                if any(value < low or value > high for value in parsed_values[expression]):
                    valid = False
                    break
        parameter_value = row.get("parameter_value")
        if valid and parameter_value:
            completed.add(parameter_value)
        else:
            invalid += 1
    return completed, invalid


def _migrate_legacy_sweep_csv(
    csv_path: Optional[str],
    fieldnames: Sequence[str],
    manifest: dict[str, Any],
) -> None:
    existing_fields, rows = _read_csv_journal(csv_path)
    if not rows:
        return
    for row in rows:
        row["schema_version"] = SWEEP_SCHEMA_VERSION
        row["config_id"] = manifest["config_id"]
        row["source_model_sha256"] = manifest["spec"]["model"]["source_sha256"]
        row["status"] = "legacy_unverified"
        row.setdefault("error_type", "")
        row.setdefault("error", "")
    merged_fields = [
        *fieldnames,
        *(field for field in existing_fields if field not in fieldnames),
    ]
    _write_rows_csv(csv_path, merged_fields, rows, append=False)


def _validate_evaluated_values(
    evaluated: dict[str, Any],
    expressions: Sequence[str],
    physical_bounds: Optional[dict[str, Sequence[float]]],
) -> None:
    parsed_values: dict[str, list[float]] = {}
    for expression in expressions:
        finite, values = _finite_csv_value(str(_csv_value(evaluated.get(expression))))
        if not finite:
            raise ValueError(f"Expression {expression!r} did not evaluate to finite numeric data")
        parsed_values[expression] = values
    for expression, bounds in (physical_bounds or {}).items():
        if expression not in parsed_values or len(bounds) != 2:
            raise ValueError(f"Invalid physical bound declaration for {expression!r}")
        low, high = float(bounds[0]), float(bounds[1])
        if any(value < low or value > high for value in parsed_values[expression]):
            raise ValueError(
                f"Expression {expression!r} is outside physical bounds [{low}, {high}]"
            )


def _completed_keys(csv_path: Optional[str], key: str) -> set[str]:
    """Return successful journal keys, including rows from the legacy schema."""
    completed = set()
    for row in _read_csv_rows(csv_path):
        status = (row.get("status") or "success").strip().lower()
        value = row.get(key)
        if value is not None and status == "success":
            completed.add(value)
    return completed


def _save_model(model, file_path: str) -> None:
    """Save through the Java clientapi to preserve non-ASCII Windows paths."""
    _ensure_parent_dir(file_path)
    model.java.save(str(Path(file_path).expanduser().resolve()))


def run_staged_parametric_sweep(
    model,
    parameter_name: str,
    parameter_values: Sequence[Any],
    expressions: Sequence[str],
    *,
    parameter_unit: Optional[str] = None,
    study_name: Optional[str] = None,
    study_step_tag: Optional[str] = None,
    study_step_property: str = "plist",
    study_step_unit: Optional[str] = None,
    study_step_unit_property: str = "punit",
    csv_path: Optional[str] = None,
    append_csv: bool = False,
    resume_csv: bool = False,
    max_retries: int = 0,
    continue_on_error: bool = False,
    checkpoint_model_path: Optional[str] = None,
    checkpoint_every: int = 1,
    save_model_path: Optional[str] = None,
    manifest_path: Optional[str] = None,
    source_model_path: Optional[str] = None,
    config_id: Optional[str] = None,
    allow_legacy_resume: bool = False,
    record_wavelength_controls: Optional[bool] = None,
    physical_bounds: Optional[dict[str, Sequence[float]]] = None,
    response_tail: int = DEFAULT_RESPONSE_TAIL,
    max_new_points: Optional[int] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_durable_row: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Run a parameter sweep one point at a time and append CSV rows eagerly."""
    if not parameter_values:
        return {"success": False, "error": "parameter_values must not be empty."}
    if not expressions:
        return {"success": False, "error": "expressions must not be empty."}
    if max_retries < 0:
        return {"success": False, "error": "max_retries must be non-negative."}
    if checkpoint_every < 1:
        return {"success": False, "error": "checkpoint_every must be at least 1."}
    if response_tail < 0 or response_tail > 20:
        return {"success": False, "error": "response_tail must be between 0 and 20."}
    if max_new_points is not None and max_new_points < 0:
        return {"success": False, "error": "max_new_points must be non-negative."}
    if source_model_path:
        baseline = Path(source_model_path).expanduser().resolve()
        mutation_targets = [
            Path(path).expanduser().resolve()
            for path in (checkpoint_model_path, save_model_path)
            if path
        ]
        if baseline in mutation_targets:
            return {
                "success": False,
                "error": "checkpoint/save path must not overwrite source_model_path",
            }

    jm = model.java
    study_tag = _resolve_study_tag(model, study_name)
    if study_tag is None:
        tags = list(jm.study().tags())
        if not tags:
            return {"success": False, "error": "No studies found in model."}
        study_tag = str(tags[0])

    if record_wavelength_controls is None:
        record_wavelength_controls = parameter_name.casefold() in {"wl", "wavelength"}
    fieldnames = [
        "schema_version",
        "config_id",
        "source_model_sha256",
        parameter_name,
        "parameter_value",
        *(
            ["requested_wavelength", "evaluated_wl", "evaluated_c_const_over_ewfd_freq"]
            if record_wavelength_controls else []
        ),
        "status",
        "attempt",
        "error_type",
        "error",
        "solve_sec",
        *list(expressions),
    ]
    rows: list[dict[str, Any]] = []
    failed_rows: list[dict[str, Any]] = []
    journal_tail: list[dict[str, Any]] = []
    if csv_path and not append_csv and not resume_csv:
        Path(csv_path).unlink(missing_ok=True)
    try:
        manifest, resolved_manifest_path, adopted_legacy = _prepare_sweep_manifest(
            model,
            parameter_name=parameter_name,
            parameter_values=parameter_values,
            parameter_unit=parameter_unit,
            expressions=expressions,
            study_tag=study_tag,
            study_step_tag=study_step_tag,
            study_step_property=study_step_property,
            study_step_unit=study_step_unit,
            csv_path=csv_path,
            manifest_path=manifest_path,
            source_model_path=source_model_path,
            config_id=config_id,
            record_wavelength_controls=record_wavelength_controls,
            physical_bounds=physical_bounds,
            resume_csv=resume_csv,
            append_csv=append_csv,
            allow_legacy_resume=allow_legacy_resume,
        )
        if adopted_legacy:
            _migrate_legacy_sweep_csv(csv_path, fieldnames, manifest)
            adopted_legacy = False
        completed_values, invalid_existing_rows = (
            _resume_completed_values(
                csv_path,
                fieldnames=fieldnames,
                manifest=manifest,
                expressions=expressions,
                physical_bounds=physical_bounds,
                adopted_legacy=adopted_legacy,
            )
            if resume_csv else (set(), 0)
        )
    except (OSError, ValueError) as exc:
        return {
            "success": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "csv_path": csv_path,
            "manifest_path": manifest_path or (f"{csv_path}.manifest.json" if csv_path else None),
        }
    skipped = 0
    checkpointed_at = 0
    total_start = time.time()
    stopped_early = False
    stop_reason = None
    processed = 0

    for value in parameter_values:
        parameter_value = _format_parameter_value(value, parameter_unit)
        if parameter_value in completed_values:
            skipped += 1
            continue
        if max_new_points is not None and processed >= max_new_points:
            stopped_early = True
            stop_reason = "max_new_points"
            break
        if should_stop is not None and should_stop():
            stopped_early = True
            stop_reason = "control_request"
            break

        for attempt in range(1, max_retries + 2):
            solve_start = time.time()
            try:
                jm.param().set(parameter_name, parameter_value)

                if study_step_tag:
                    step = jm.study(study_tag).feature(study_step_tag)
                    step.set(study_step_property, _format_study_step_value(value))
                    if study_step_unit:
                        step.set(study_step_unit_property, study_step_unit)

                jm.study(study_tag).run()
                solve_sec = time.time() - solve_start
                evaluation_expressions = list(expressions)
                if record_wavelength_controls:
                    evaluation_expressions.extend(
                        expression for expression in ("wl", "c_const/ewfd.freq")
                        if expression not in evaluation_expressions
                    )
                evaluated_all = _evaluate_expressions(model, evaluation_expressions)
                evaluated = {expression: evaluated_all[expression] for expression in expressions}
                _validate_evaluated_values(
                    evaluated_all,
                    evaluation_expressions,
                    physical_bounds,
                )
                row = {
                    "schema_version": SWEEP_SCHEMA_VERSION,
                    "config_id": manifest["config_id"],
                    "source_model_sha256": manifest["spec"]["model"]["source_sha256"],
                    parameter_name: value,
                    "parameter_value": parameter_value,
                    "status": "ok",
                    "attempt": attempt,
                    "error_type": None,
                    "error": None,
                    "solve_sec": solve_sec,
                    **evaluated,
                }
                if record_wavelength_controls:
                    row.update(
                        requested_wavelength=parameter_value,
                        evaluated_wl=evaluated_all["wl"],
                        evaluated_c_const_over_ewfd_freq=evaluated_all[
                            "c_const/ewfd.freq"
                        ],
                    )
                rows.append(row)
                journal_tail.append(row)
                _write_rows_csv(csv_path, fieldnames, [row], append=True)
                if checkpoint_model_path and len(rows) % checkpoint_every == 0:
                    _save_model(model, checkpoint_model_path)
                    checkpointed_at = len(rows)
                processed += 1
                if on_durable_row is not None:
                    on_durable_row(dict(row))
                break
            except Exception as exc:
                if attempt <= max_retries:
                    continue
                row = {
                    "schema_version": SWEEP_SCHEMA_VERSION,
                    "config_id": manifest["config_id"],
                    "source_model_sha256": manifest["spec"]["model"]["source_sha256"],
                    parameter_name: value,
                    "parameter_value": parameter_value,
                    "status": "error",
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "solve_sec": time.time() - solve_start,
                }
                if record_wavelength_controls:
                    row["requested_wavelength"] = parameter_value
                failed_rows.append(row)
                journal_tail.append(row)
                _write_rows_csv(csv_path, fieldnames, [row], append=True)
                processed += 1
                if on_durable_row is not None:
                    on_durable_row(dict(row))
                if not continue_on_error:
                    raise

    if save_model_path:
        _save_model(model, save_model_path)
    elif checkpoint_model_path and rows and checkpointed_at != len(rows):
        _save_model(model, checkpoint_model_path)

    return {
        "success": not failed_rows,
        "model": str(model.name()),
        "study": study_name,
        "resolved_study_tag": study_tag,
        "parameter": parameter_name,
        "n_points": len(rows),
        "n_failed": len(failed_rows),
        "n_skipped": skipped,
        "n_invalid_existing": invalid_existing_rows,
        "n_processed": processed,
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
        "csv_path": csv_path,
        "manifest_path": str(resolved_manifest_path) if resolved_manifest_path else None,
        "config_id": manifest["config_id"],
        "schema_version": SWEEP_SCHEMA_VERSION,
        "save_model_path": save_model_path,
        "total_sec": time.time() - total_start,
        "last_point": journal_tail[-1] if journal_tail else None,
        "tail_rows": journal_tail[-response_tail:] if response_tail else [],
    }


def run_mesh_convergence(
    model,
    levels: Sequence[dict[str, Any]],
    expressions: Sequence[str],
    *,
    component_name: str = "comp1",
    mesh_name: str = "mesh1",
    size_feature_tag: str = "sz1",
    parameter_name: Optional[str] = None,
    parameter_value: Optional[Any] = None,
    parameter_unit: Optional[str] = None,
    study_name: Optional[str] = None,
    study_step_tag: Optional[str] = None,
    study_step_property: str = "plist",
    study_step_unit: Optional[str] = None,
    study_step_unit_property: str = "punit",
    csv_path: Optional[str] = None,
    append_csv: bool = False,
    resume_csv: bool = False,
    max_retries: int = 0,
    continue_on_error: bool = False,
    checkpoint_model_path: Optional[str] = None,
    checkpoint_every: int = 1,
    save_model_path: Optional[str] = None,
) -> dict[str, Any]:
    """Run mesh rebuild + solve + evaluation for each mesh-property level."""
    if not levels:
        return {"success": False, "error": "levels must not be empty."}
    if not expressions:
        return {"success": False, "error": "expressions must not be empty."}
    if max_retries < 0:
        return {"success": False, "error": "max_retries must be non-negative."}
    if checkpoint_every < 1:
        return {"success": False, "error": "checkpoint_every must be at least 1."}

    jm = model.java
    study_tag = _resolve_study_tag(model, study_name)
    if study_tag is None:
        tags = list(jm.study().tags())
        if not tags:
            return {"success": False, "error": "No studies found in model."}
        study_tag = str(tags[0])

    if parameter_name is not None and parameter_value is not None:
        jm.param().set(parameter_name, _format_parameter_value(parameter_value, parameter_unit))
        if study_step_tag:
            step = jm.study(study_tag).feature(study_step_tag)
            step.set(study_step_property, _format_study_step_value(parameter_value))
            if study_step_unit:
                step.set(study_step_unit_property, study_step_unit)

    mesh = jm.component(component_name).mesh(mesh_name)
    size_feature = mesh.feature(size_feature_tag)

    property_keys: list[str] = []
    for level in levels:
        for key in (level.get("properties") or {}).keys():
            if key not in property_keys:
                property_keys.append(key)

    fieldnames = [
        "level",
        "status",
        "attempt",
        "error",
        *property_keys,
        "mesh_elements",
        "mesh_vertices",
        "mesh_sec",
        "solve_sec",
        *list(expressions),
    ]
    rows: list[dict[str, Any]] = []
    failed_rows: list[dict[str, Any]] = []
    if csv_path and not append_csv and not resume_csv:
        Path(csv_path).unlink(missing_ok=True)
    completed_levels = _completed_keys(csv_path, "level") if resume_csv else set()
    skipped = 0
    checkpointed_at = 0
    total_start = time.time()

    for idx, level in enumerate(levels, start=1):
        label = str(level.get("name") or f"level_{idx}")
        if label in completed_levels:
            skipped += 1
            continue
        properties = level.get("properties") or {}
        for attempt in range(1, max_retries + 2):
            mesh_start = time.time()
            try:
                for key, value in properties.items():
                    size_feature.set(key, value)

                mesh.run()
                mesh_sec = time.time() - mesh_start
                try:
                    mesh_elements = int(mesh.getNumElem())
                    mesh_vertices = int(mesh.getNumVertex())
                except Exception:
                    mesh_elements = None
                    mesh_vertices = None

                solve_start = time.time()
                jm.study(study_tag).run()
                solve_sec = time.time() - solve_start
                evaluated = _evaluate_expressions(model, expressions)
                row = {
                    "level": label,
                    "status": "success",
                    "attempt": attempt,
                    "error": None,
                    "mesh_elements": mesh_elements,
                    "mesh_vertices": mesh_vertices,
                    "mesh_sec": mesh_sec,
                    "solve_sec": solve_sec,
                    **{key: properties.get(key) for key in property_keys},
                    **evaluated,
                }
                rows.append(row)
                _write_rows_csv(csv_path, fieldnames, [row], append=True)
                if checkpoint_model_path and len(rows) % checkpoint_every == 0:
                    _save_model(model, checkpoint_model_path)
                    checkpointed_at = len(rows)
                break
            except Exception as exc:
                if attempt <= max_retries:
                    continue
                row = {
                    "level": label,
                    "status": "error",
                    "attempt": attempt,
                    "error": str(exc),
                    "mesh_sec": time.time() - mesh_start,
                    **{key: properties.get(key) for key in property_keys},
                }
                failed_rows.append(row)
                _write_rows_csv(csv_path, fieldnames, [row], append=True)
                if not continue_on_error:
                    raise

    if save_model_path:
        _save_model(model, save_model_path)
    elif checkpoint_model_path and rows and checkpointed_at != len(rows):
        _save_model(model, checkpoint_model_path)

    return {
        "success": not failed_rows,
        "model": model.name(),
        "component": component_name,
        "mesh": mesh_name,
        "size_feature": size_feature_tag,
        "study": study_name,
        "resolved_study_tag": study_tag,
        "n_levels": len(rows),
        "n_failed": len(failed_rows),
        "n_skipped": skipped,
        "csv_path": csv_path,
        "save_model_path": save_model_path,
        "total_sec": time.time() - total_start,
        "rows": rows,
        "failed_rows": failed_rows,
    }


def register_workflow_tools(mcp: FastMCP) -> None:
    """Register generic workflow tools."""

    @mcp.tool()
    def study_staged_parametric_sweep(
        parameter_name: str,
        parameter_values: Sequence[Any],
        expressions: Sequence[str],
        parameter_unit: Optional[str] = None,
        study_name: Optional[str] = None,
        study_step_tag: Optional[str] = None,
        study_step_property: str = "plist",
        study_step_unit: Optional[str] = None,
        study_step_unit_property: str = "punit",
        csv_path: Optional[str] = None,
        append_csv: bool = False,
        resume_csv: bool = False,
        max_retries: int = 0,
        continue_on_error: bool = False,
        checkpoint_model_path: Optional[str] = None,
        checkpoint_every: int = 1,
        save_model_path: Optional[str] = None,
        manifest_path: Optional[str] = None,
        source_model_path: Optional[str] = None,
        config_id: Optional[str] = None,
        allow_legacy_resume: bool = False,
        record_wavelength_controls: Optional[bool] = None,
        physical_bounds: Optional[dict[str, Sequence[float]]] = None,
        response_tail: int = DEFAULT_RESPONSE_TAIL,
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Run a parameter sweep one point at a time and write CSV incrementally.

        This avoids losing all results when a long COMSOL Parametric Sweep or
        MCP call times out: each parameter value is solved and appended to the
        CSV immediately.

        Args:
            parameter_name: COMSOL parameter name to set before each solve.
            parameter_values: Values to sweep. Numeric values can receive
                parameter_unit; strings are passed directly.
            expressions: Global expressions to evaluate after each solve.
            parameter_unit: Optional unit for numeric parameter values, e.g. "m".
            study_name: Study tag or label. Defaults to first study.
            study_step_tag: Optional study step to update per point, e.g.
                "wl_step" for Wavelength Domain.
            study_step_property: Step property to update, default "plist".
            study_step_unit: Optional unit property value for the step, e.g. "m".
            study_step_unit_property: Step unit property name, default "punit".
            csv_path: Optional CSV output path. Rows are written after each point.
            append_csv: Append to existing CSV instead of replacing it.
            resume_csv: Skip successful parameter values already in csv_path.
            max_retries: Number of retries after a failed point.
            continue_on_error: Continue after final retry and report failed rows.
            checkpoint_model_path: Optional model path saved during the sweep.
            checkpoint_every: Save a checkpoint after this many new successes.
            save_model_path: Optional path to save the model after the sweep.
            manifest_path: Optional manifest path; defaults beside csv_path.
            source_model_path: Immutable source model to fingerprint with SHA-256.
            config_id: Optional caller-supplied stable configuration identifier.
            allow_legacy_resume: Explicitly adopt an audited CSV lacking a manifest;
                old rows are marked unverified and rerun.
            record_wavelength_controls: Record requested wavelength, evaluated wl,
                and c_const/ewfd.freq. Defaults on for wl/wavelength parameters.
            physical_bounds: Optional expression bounds, e.g. {"A": [0, 1]}.
            response_tail: Number of recent rows returned in the MCP response (0-20).
            model_name: Model name (default: current).

        Returns:
            Rows containing parameter value, solve time, and expression values.
        """
        preflight = session_manager.preflight_long_operation(
            model_path=source_model_path,
            output_path=csv_path,
        )
        if not preflight["ready"]:
            return {
                "success": False,
                "error": "Long-operation solver preflight failed.",
                "preflight": preflight,
            }
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name or 'no current model'}"}

        try:
            return run_staged_parametric_sweep(
                model,
                parameter_name,
                parameter_values,
                expressions,
                parameter_unit=parameter_unit,
                study_name=study_name,
                study_step_tag=study_step_tag,
                study_step_property=study_step_property,
                study_step_unit=study_step_unit,
                study_step_unit_property=study_step_unit_property,
                csv_path=csv_path,
                append_csv=append_csv,
                resume_csv=resume_csv,
                max_retries=max_retries,
                continue_on_error=continue_on_error,
                checkpoint_model_path=checkpoint_model_path,
                checkpoint_every=checkpoint_every,
                save_model_path=save_model_path,
                manifest_path=manifest_path,
                source_model_path=source_model_path,
                config_id=config_id,
                allow_legacy_resume=allow_legacy_resume,
                record_wavelength_controls=record_wavelength_controls,
                physical_bounds=physical_bounds,
                response_tail=response_tail,
            )
        except Exception as exc:
            return {"success": False, "error": f"staged sweep failed: {exc}"}

    @mcp.tool()
    def mesh_convergence_study(
        levels: Sequence[dict[str, Any]],
        expressions: Sequence[str],
        component_name: str = "comp1",
        mesh_name: str = "mesh1",
        size_feature_tag: str = "sz1",
        parameter_name: Optional[str] = None,
        parameter_value: Optional[Any] = None,
        parameter_unit: Optional[str] = None,
        study_name: Optional[str] = None,
        study_step_tag: Optional[str] = None,
        study_step_property: str = "plist",
        study_step_unit: Optional[str] = None,
        study_step_unit_property: str = "punit",
        csv_path: Optional[str] = None,
        append_csv: bool = False,
        resume_csv: bool = False,
        max_retries: int = 0,
        continue_on_error: bool = False,
        checkpoint_model_path: Optional[str] = None,
        checkpoint_every: int = 1,
        save_model_path: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Run mesh-convergence solves for a size feature.

        Each level supplies a name and a property dictionary, for example:

        ``{"name": "fine", "properties": {"hmax": "0.03*wl", "hmin": "0.0012*wl"}}``

        Args:
            levels: Mesh levels with ``name`` and ``properties``.
            expressions: Global expressions to evaluate after each solve.
            component_name: Component tag containing the mesh.
            mesh_name: Mesh sequence tag.
            size_feature_tag: Mesh Size feature tag to modify.
            parameter_name: Optional model parameter to set once before all levels.
            parameter_value: Optional value for parameter_name.
            parameter_unit: Optional unit for numeric parameter_value.
            study_name: Study tag or label. Defaults to first study.
            study_step_tag: Optional study step to update with parameter_value.
            study_step_property: Step property to update, default "plist".
            study_step_unit: Optional unit property value for the step.
            study_step_unit_property: Step unit property name, default "punit".
            csv_path: Optional CSV output path. Rows are written per level.
            append_csv: Append to existing CSV instead of replacing it.
            resume_csv: Skip successful level names already in csv_path.
            max_retries: Number of retries after a failed level.
            continue_on_error: Continue after final retry and report failed rows.
            checkpoint_model_path: Optional model path saved during the run.
            checkpoint_every: Save a checkpoint after this many new successes.
            save_model_path: Optional path to save the model after the run.
            model_name: Model name (default: current).

        Returns:
            Rows containing mesh counts, timings, and expression values.
        """
        preflight = session_manager.preflight_long_operation(output_path=csv_path)
        if not preflight["ready"]:
            return {
                "success": False,
                "error": "Long-operation solver preflight failed.",
                "preflight": preflight,
            }
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name or 'no current model'}"}

        try:
            if csv_path and not append_csv and not resume_csv:
                Path(csv_path).unlink(missing_ok=True)
            return run_mesh_convergence(
                model,
                levels,
                expressions,
                component_name=component_name,
                mesh_name=mesh_name,
                size_feature_tag=size_feature_tag,
                parameter_name=parameter_name,
                parameter_value=parameter_value,
                parameter_unit=parameter_unit,
                study_name=study_name,
                study_step_tag=study_step_tag,
                study_step_property=study_step_property,
                study_step_unit=study_step_unit,
                study_step_unit_property=study_step_unit_property,
                csv_path=csv_path,
                append_csv=append_csv,
                resume_csv=resume_csv,
                max_retries=max_retries,
                continue_on_error=continue_on_error,
                checkpoint_model_path=checkpoint_model_path,
                checkpoint_every=checkpoint_every,
                save_model_path=save_model_path,
            )
        except Exception as exc:
            return {"success": False, "error": f"mesh convergence failed: {exc}"}
