"""Probe the exact COMSOL 6.4 surrogate-training and DNN ClientAPI surface.

Repository-only and licensed.  ``--dry-run`` validates the isolation contract
without importing MPh, starting COMSOL, or acquiring a solver lease.

The probe never approximates an unavailable API with generic mutation.  Every
unreadable or absent control is recorded as ``unsupported`` or
``comsol_owned_default`` so that the 0.7.5 public contract can freeze a truthful
reproducibility claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from importlib import import_module
from pathlib import Path
from typing import Any

import psutil

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

atomic_write_json_exclusive = import_module("comsol_mcp.durable.io").atomic_write_json_exclusive
ownership_module = import_module("comsol_mcp.tools.ownership")
SolverOwnership = ownership_module.SolverOwnership
_command_signature = ownership_module._command_signature
process_control_module = import_module("comsol_mcp.jobs.process_control")
terminate_exact = process_control_module.terminate_exact
verify_absent = process_control_module.verify_absent

SCHEMA_NAME = "comsol_mcp.surrogate_dnn_capability_probe"
SCHEMA_VERSION = "1.0.0"
EXPECTED_BACKEND = {"major": 6, "minor": 4, "patch": 0, "build": 293}

# Bounded discovery limits.
MAX_PROPERTY_NAMES = 512
MAX_METHOD_NAMES = 512
MAX_VALUE_CHARS = 400
MAX_RECEIPT_BYTES = 8 * 1024 * 1024

# Documented SurrogateModelTraining study-step properties
# (COMSOL 6.4 ClientAPI reference, comsol_api_solver.51.80.html Table 6-174).
DOCUMENTED_STUDY_PROPERTIES = (
    "accumtable", "accumtableall", "activation", "adpevals", "automatictraining",
    "basefilepath", "clearsynchdata", "computeaction", "convinfo", "correlatedinput",
    "covfunction", "defsolvergen", "distributionselection", "errorhandling", "file",
    "filename", "funcname", "geometrysampling", "globaldnnfunction", "globalgpfunction",
    "globalleastsquarefitfunction", "globalpcefunction", "gpadpoptmethod",
    "includeintraining", "innermostparameter", "interptimes", "keepsol", "layertype",
    "lboundselection", "lcdfselection", "lsqexpression", "lsqlowerbound",
    "lsqparameternames", "lsqparametervalues", "lsqscale", "lsqupperbound", "maxgpevals",
    "maxgpiters", "meanfunction", "nsolvenonadp", "nsolvenonadpimprove", "objgrp",
    "outfeatures", "outputtable", "parameterselection", "paramfilename", "pcesettings",
    "pdistrib", "pname", "polydegreespce", "punitselection", "qoiconfigurestudyinput",
    "qoiexpression", "qoisolutionindv", "qoisolutionouterindv", "qnorm", "randseed",
    "reusesol", "s1selection", "s2selection", "savefile", "savemodelaftersynch",
    "studyinputparamname", "surrogatemodel", "synchsolutions", "uboundselection",
    "ucdfselection", "useaccumtable", "useseed",
)

# Documented DNN function properties
# (COMSOL 6.4 ClientAPI reference, comsol_api_general.47.34.html Table 2-104).
DOCUMENTED_DNN_PROPERTIES = (
    "activation", "args", "batchsize", "colscale", "descr", "epochs", "filename",
    "fraction", "gputraining", "ignorenaninf", "ignorenonpositive", "layertype", "lr",
    "loss", "momentum", "optmethod", "outfeatures", "plotargs", "plotaxis",
    "plotfixedvalue", "resultTable", "rndseed", "rndseedvalidation", "source",
    "testloss", "trainingloss", "useseed", "useseedvalidation", "validation",
    "validationloss",
)

# DNN properties discovered by the first S0 probe that are absent from the
# ClientAPI reference table.  They carry the training/test/export readback that
# a truthful 0.7.5 contract depends on, so S0 must resolve each one.
DISCOVERED_DNN_PROPERTIES = (
    "exportfilename", "isexporting", "isuploaded", "trained_chksum", "trained_info",
    "trained_ninput", "trained_noutput", "layerconfig", "information", "modelres",
    "weightdecay", "test", "testfraction", "testpredictiontable", "testtable",
    "validationtable", "sourcetype", "dseparator", "columnType", "columnKeys",
    "setupfromstudy", "funcs", "unit",
)

# Method-name fragments whose exact Java names S0 must resolve.
METHOD_INTEREST = ("train", "test", "export", "onnx", "continu", "save", "reload", "reset")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", choices=["RUN_REAL_COMSOL"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime-root", type=Path, default=Path("D:/comsol_runtime"))
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--minimum-free-gb", type=float, default=2.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    return parser


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_receipt(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json_exclusive(path, value)


def _git_identity() -> dict:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"commit": None, "dirty_entry_count": None, "error_type": type(exc).__name__}
    return {"commit": commit, "dirty_entry_count": len(dirty)}


def _process_identity(pid: int) -> dict:
    process = psutil.Process(pid)
    with process.oneshot():
        command_line = list(process.cmdline())
        try:
            executable = process.exe()
        except (psutil.AccessDenied, psutil.ZombieProcess):
            executable = None
        return {
            "pid": process.pid,
            "parent_pid": process.ppid(),
            "process_create_time": process.create_time(),
            "name": process.name(),
            "executable": executable,
            "command_line": command_line,
            "command_signature": _command_signature(command_line),
        }


def _descendant_identities(pid: int) -> dict:
    try:
        children = psutil.Process(pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        return {"complete": False, "error": f"{type(exc).__name__}: {exc}", "identities": []}
    identities = []
    incomplete = False
    detail = None
    for child in children:
        try:
            identities.append(_process_identity(child.pid))
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, psutil.ZombieProcess) as exc:
            incomplete = True
            detail = f"{type(exc).__name__}: {exc}"
    return {
        "complete": not incomplete,
        "error": None if not incomplete else (detail or "child identities could not be read"),
        "identities": sorted(
            identities, key=lambda item: (item["process_create_time"], item["pid"])
        ),
    }


def _listener_inventory(pids: set[int]) -> dict:
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError) as exc:
        return {"complete": False, "error": f"{type(exc).__name__}: {exc}", "listeners": []}
    listeners = []
    for connection in connections:
        if connection.pid not in pids or connection.status != psutil.CONN_LISTEN:
            continue
        local = connection.laddr
        listeners.append(
            {
                "pid": connection.pid,
                "address": getattr(local, "ip", local[0] if local else None),
                "port": getattr(local, "port", local[1] if local else None),
            }
        )
    return {
        "complete": True,
        "error": None,
        "listeners": sorted(listeners, key=lambda item: (item["pid"], item["port"])),
    }


def _status_evidence(status: dict) -> dict:
    durable = status.get("durable_jobs") or {}
    lease = status.get("lease") or {}
    return {
        "collision": status.get("collision"),
        "process_inventory": status.get("process_inventory"),
        "external_solver_processes": status.get("external_solver_processes"),
        "lease_state": lease.get("state"),
        "lease": lease.get("lease"),
        "durable_jobs_available": durable.get("available"),
        "active_job_count": durable.get("active_count"),
        "active_jobs": durable.get("active"),
    }


def _status_is_clean(status: dict) -> bool:
    durable = status.get("durable_jobs") or {}
    inventory = status.get("process_inventory") or {}
    return (
        inventory.get("complete") is True
        and inventory.get("fresh") is True
        and status.get("collision") is False
        and status.get("lease", {}).get("state") == "absent"
        and durable.get("available") is True
        and durable.get("active_count") == 0
    )


def _wait_clean(owner: SolverOwnership, timeout_seconds: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = owner.status(require_fresh_inventory=True)
        if _status_is_clean(status) or time.monotonic() >= deadline:
            return status
        time.sleep(0.25)


def _terminate_owned_tree(
    process: subprocess.Popen, captured_descendants: list[dict] | tuple[dict, ...] = ()
) -> dict:
    errors = []
    direct_was_running = process.poll() is None
    if direct_was_running:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            else:
                for child in reversed(psutil.Process(process.pid).children(recursive=True)):
                    child.kill()
                psutil.Process(process.pid).kill()
        except Exception as exc:
            errors.append({"stage": "direct_tree", "type": type(exc).__name__})
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=15)
            except Exception as exc:
                errors.append({"stage": "direct_wait", "type": type(exc).__name__})
        except Exception as exc:
            errors.append({"stage": "direct_wait", "type": type(exc).__name__})

    descendant_actions = []
    for identity in reversed(list(captured_descendants)):
        try:
            descendant_actions.append(terminate_exact(identity, force=True))
        except Exception as exc:
            errors.append({"stage": "descendant", "type": type(exc).__name__})
    try:
        deadline = time.monotonic() + 5.0
        while True:
            descendant_verification = verify_absent(list(captured_descendants))
            if descendant_verification.get("absent") is True or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    except Exception as exc:
        errors.append({"stage": "descendant_verification", "type": type(exc).__name__})
        descendant_verification = {"absent": False, "verdicts": []}
    return {
        "direct_was_running": direct_was_running,
        "direct_exited": process.poll() is not None,
        "descendant_actions": descendant_actions,
        "descendant_verification": descendant_verification,
        "errors": errors,
        "passed": (
            process.poll() is not None
            and descendant_verification.get("absent") is True
            and not errors
        ),
    }


def _backend_identity(backend: dict) -> dict:
    return {
        "name": str(backend.get("name")),
        "major": int(backend.get("major")),
        "minor": int(backend.get("minor")),
        "patch": int(backend.get("patch")),
        "build": int(backend.get("build")),
        "root": str(backend.get("root")),
        "jvm": str(backend.get("jvm")),
    }


def _select_expected_backend(backends: list[dict]) -> dict:
    matching = [
        backend
        for backend in backends
        if all(int(backend.get(key, -1)) == value for key, value in EXPECTED_BACKEND.items())
    ]
    if len(matching) != 1:
        raise RuntimeError(f"expected exactly one COMSOL 6.4.0.293 backend, found {len(matching)}")
    return _backend_identity(matching[0])


# --------------------------------------------------------------------------
# Java introspection helpers (worker side only).
# --------------------------------------------------------------------------


def _bounded(value: Any, limit: int = MAX_VALUE_CHARS) -> Any:
    """Convert one Java value to bounded JSON without raising."""
    try:
        if value is None:
            return None
        if isinstance(value, (bool, int, float, str)):
            text = value if isinstance(value, str) else repr(value)
            return value if len(str(text)) <= limit else str(text)[:limit]
        text = str(value)
        return text if len(text) <= limit else text[:limit] + "...<truncated>"
    except Exception as exc:  # pragma: no cover - defensive
        return f"<unrepresentable: {type(exc).__name__}>"


def _method_names(java_object: Any) -> dict:
    """Enumerate public Java method names via reflection."""
    result: dict[str, Any] = {"available": False, "names": [], "count": None}
    try:
        java_class = java_object.getClass()
        result["class_name"] = str(java_class.getName())
        methods = java_class.getMethods()
        names = sorted({str(method.getName()) for method in methods})
        result["available"] = True
        result["count"] = len(names)
        result["names"] = names[:MAX_METHOD_NAMES]
        result["truncated"] = len(names) > MAX_METHOD_NAMES
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _interesting_methods(method_report: dict) -> dict:
    """Group discovered method names by the S0 interest fragments."""
    names = method_report.get("names") or []
    grouped: dict[str, list[str]] = {}
    for fragment in METHOD_INTEREST:
        grouped[fragment] = sorted(name for name in names if fragment in name.lower())
    return grouped


# Split/seed/architecture controls whose allowed value sets decide whether the
# 0.7.5 split contract can be exactly reproducible or must be prepartitioned.
CRITICAL_ALLOWED_VALUE_PROPERTIES = {
    "study_step": (
        "surrogatemodel", "useseed", "computeaction", "sweeptype", "errorhandling",
        "keepsol", "convinfo", "defsolvergen", "paramfilename", "qoiconfigurestudyinput",
        "qoisolutionindv", "qoisolutionouterindv", "innermostparameter",
    ),
    "dnn_function": (
        "source", "validation", "test", "useseed", "useseedvalidation", "optmethod",
        "loss", "activation", "layertype", "colscale", "sourcetype",
    ),
}


def _allowed_values(java_object: Any, names: tuple[str, ...]) -> dict:
    """Resolve the COMSOL-declared allowed value set for each property.

    A ``null`` result means the property is not an enumerated choice (for
    example a string map or a free scalar), which is distinct from a failed
    query; the two must not be conflated in the capability matrix.
    """
    result: dict[str, Any] = {}
    for name in names:
        try:
            raw = java_object.getAllowedPropertyValues(name)
        except Exception as exc:
            result[name] = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
            continue
        if raw is None:
            result[name] = {"available": False, "reason": "not_an_enumerated_property"}
            continue
        try:
            result[name] = {
                "available": True,
                "values": [_bounded(item, 120) for item in list(raw)],
            }
        except Exception as exc:
            result[name] = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    return result


# Methods whose exact Java signatures the DNN adapter must bind.
SIGNATURE_INTEREST = (
    "run", "continueRun", "runTest", "export", "exportData", "importData",
    "discardData", "saveFile", "loadFile", "set", "getEntryKeys", "setEntry",
    "setIndex", "properties", "getAllowedPropertyValues",
)


def _method_signatures(java_object: Any, interest: tuple[str, ...]) -> dict:
    """Resolve exact Java signatures for the methods the adapter must call.

    Overload ambiguity is the main S4 risk: a bare name is not a binding.
    """
    result: dict[str, Any] = {"available": False, "signatures": {}}
    try:
        java_class = java_object.getClass()
        methods = java_class.getMethods()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    collected: dict[str, list[str]] = {name: [] for name in interest}
    for method in methods:
        name = str(method.getName())
        if name not in collected:
            continue
        try:
            parameter_types = [str(item.getName()) for item in method.getParameterTypes()]
            return_type = str(method.getReturnType().getName())
        except Exception:
            continue
        collected[name].append(f"{return_type} {name}({', '.join(parameter_types)})")
    result["available"] = True
    result["signatures"] = {
        name: sorted(set(signatures)) for name, signatures in collected.items()
    }
    result["absent"] = sorted(name for name, signatures in collected.items() if not signatures)
    return result


def _property_names(java_object: Any) -> dict:
    """Read the COMSOL property-name list when the object exposes it."""
    result: dict[str, Any] = {"available": False, "names": [], "count": None}
    for accessor in ("properties", "getPropertyNames"):
        try:
            raw = getattr(java_object, accessor)()
        except Exception:
            continue
        try:
            names = sorted({str(item) for item in list(raw)})
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            continue
        result["available"] = True
        result["accessor"] = accessor
        result["count"] = len(names)
        result["names"] = names[:MAX_PROPERTY_NAMES]
        result["truncated"] = len(names) > MAX_PROPERTY_NAMES
        return result
    result.setdefault("error", "no property enumeration accessor")
    return result


def _read_property(java_object: Any, name: str) -> dict:
    """Read one property through the typed accessors, recording which worked."""
    for accessor, kind in (
        ("getString", "string"),
        ("getBoolean", "boolean"),
        ("getInt", "int"),
        ("getDouble", "double"),
        ("getStringArray", "string_array"),
        ("getStringMatrix", "string_matrix"),
        ("getDoubleArray", "double_array"),
        ("getDoubleMatrix", "double_matrix"),
    ):
        try:
            value = getattr(java_object, accessor)(name)
        except Exception:
            continue
        return {"readable": True, "accessor": accessor, "kind": kind, "value": _bounded(value)}
    return {"readable": False, "reason": "no typed accessor accepted the property"}


def _probe_property_set(java_object: Any, documented: tuple[str, ...]) -> dict:
    """Confirm which documented properties are readable, and enumerate extras.

    A read failure is recorded per property; it never aborts the whole probe,
    because S0's product is the truthful availability matrix itself.
    """
    enumerated = _property_names(java_object)
    listed = set(enumerated.get("names") or [])
    confirmed = {}
    for name in documented:
        read = _read_property(java_object, name)
        read["enumerated"] = name in listed if enumerated.get("available") else None
        confirmed[name] = read
    readable = sorted(name for name, item in confirmed.items() if item["readable"])
    unreadable = sorted(name for name, item in confirmed.items() if not item["readable"])
    extras = sorted(listed - set(documented)) if enumerated.get("available") else []
    return {
        "enumeration": enumerated,
        "documented_count": len(documented),
        "readable_count": len(readable),
        "readable": readable,
        "unreadable": unreadable,
        "readback": confirmed,
        "enumerated_not_documented": extras,
    }


def _attempt(label: str, action) -> dict:
    """Run one probe action, capturing success or a bounded failure reason."""
    try:
        return {"label": label, "ok": True, "value": _bounded(action())}
    except Exception as exc:
        return {"label": label, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _run_worker(output: Path, cores: int, workspace: Path) -> int:
    result: dict[str, Any] = {
        "schema_name": f"{SCHEMA_NAME}_worker",
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "python": platform.python_version(),
        "python_executable": sys.executable,
    }
    client = None
    started = time.monotonic()
    try:
        import jpype
        import mph

        backend = _select_expected_backend(mph.discovery.find_backends())
        result["backend"] = backend
        client = mph.Client(cores=cores, version="6.4")
        result["client"] = {
            "version": str(client.version),
            "standalone": bool(client.standalone),
            "host": client.host,
            "port": client.port,
            "jvm_started": bool(jpype.isJVMStarted()),
        }
        java_system = jpype.JClass("java.lang.System")
        result["java"] = {
            "version": str(java_system.getProperty("java.version")),
            "vendor": str(java_system.getProperty("java.vendor")),
        }
        try:
            result["comsol_reported_version"] = str(client.java.getComsolVersion())
        except Exception as exc:
            result["comsol_reported_version"] = None
            result["comsol_reported_version_error"] = f"{type(exc).__name__}: {exc}"

        # ---- Minimal deterministic model -----------------------------------
        model = client.create("SurrogateDnnCapabilityProbe")
        jm = model.java
        jm.param().set("a1", "1.0")
        jm.param().set("a2", "2.0")
        component = jm.component().create("comp1", True)
        result["component_created"] = _attempt("component.create", lambda: str(component.tag()))

        # ---- Surrogate Model Training study step ---------------------------
        # The study step and the DNN function are model-level constructs; the
        # probe deliberately creates no geometry so that a geometry-feature
        # naming difference cannot masquerade as a surrogate-API result.
        study_section: dict[str, Any] = {}
        study = jm.study().create("std1")
        study_section["study_created"] = _attempt("study.create", lambda: str(study.tag()))
        step = None
        creation_attempts = []
        for label, factory in (
            (
                "study.feature().create(tag,type)",
                lambda: study.feature().create("smt1", "SurrogateModelTraining"),
            ),
            (
                "study.create(tag,type)",
                lambda: study.create("smt1", "SurrogateModelTraining"),
            ),
        ):
            attempt = _attempt(label, factory)
            creation_attempts.append(attempt)
            if attempt["ok"]:
                study_section["creation_method"] = label
                break
        study_section["creation_attempts"] = creation_attempts
        if step is None:
            try:
                step = study.feature("smt1")
                study_section["lookup_ok"] = True
            except Exception as exc:
                study_section["lookup_ok"] = False
                study_section["lookup_error"] = f"{type(exc).__name__}: {exc}"
        if step is not None:
            study_section["step_class"] = _attempt(
                "step.getClass", lambda: str(step.getClass().getName())
            )
            study_section["step_type"] = _attempt("step.getType", lambda: str(step.getType()))
            study_section["step_properties"] = _probe_property_set(
                step, DOCUMENTED_STUDY_PROPERTIES
            )
            study_section["step_methods"] = _method_names(step)
            study_section["step_methods_interesting"] = _interesting_methods(
                study_section["step_methods"]
            )
            study_section["step_method_signatures"] = _method_signatures(
                step, SIGNATURE_INTEREST
            )
            study_section["step_allowed_values"] = _allowed_values(
                step, CRITICAL_ALLOWED_VALUE_PROPERTIES["study_step"]
            )
        result["surrogate_study_step"] = study_section

        # ---- DNN function --------------------------------------------------
        func_section: dict[str, Any] = {}
        dnn = None
        dnn_creation = _attempt(
            'func.create("dnn1","DNN")', lambda: jm.func().create("dnn1", "DNN")
        )
        func_section["creation"] = dnn_creation
        try:
            dnn = jm.func("dnn1")
            func_section["lookup_ok"] = True
        except Exception as exc:
            func_section["lookup_ok"] = False
            func_section["lookup_error"] = f"{type(exc).__name__}: {exc}"
        if dnn is not None:
            func_section["func_class"] = _attempt(
                "dnn.getClass", lambda: str(dnn.getClass().getName())
            )
            func_section["func_type"] = _attempt("dnn.getType", lambda: str(dnn.getType()))
            func_section["func_properties"] = _probe_property_set(dnn, DOCUMENTED_DNN_PROPERTIES)
            func_section["func_methods"] = _method_names(dnn)
            func_section["func_methods_interesting"] = _interesting_methods(
                func_section["func_methods"]
            )
            func_section["func_method_signatures"] = _method_signatures(
                dnn, SIGNATURE_INTEREST
            )
            func_section["func_allowed_values"] = _allowed_values(
                dnn, CRITICAL_ALLOWED_VALUE_PROPERTIES["dnn_function"]
            )
        if dnn is not None:
            func_section["func_discovered_properties"] = _probe_property_set(
                dnn, DISCOVERED_DNN_PROPERTIES
            )
        result["dnn_function"] = func_section

        # ---- Determinism control readback (before any client.clear()) -------
        # The DNN handle is invalidated by client.clear(), so every in-memory
        # readback must complete before the save/reload round trip begins.
        if dnn is not None:
            seed_section = {}
            for name, value in (
                ("useseed", "manual"),
                ("rndseed", 17.0),
                ("useseedvalidation", "manual"),
                ("rndseedvalidation", 17.0),
            ):
                seed_section[f"set_{name}"] = _attempt(
                    f"set {name}={value!r}",
                    lambda name=name, value=value: dnn.set(name, value),
                )
            for name in ("useseed", "rndseed", "useseedvalidation", "rndseedvalidation"):
                seed_section[f"readback_{name}"] = _read_property(dnn, name)
            result["determinism_controls_in_memory"] = seed_section

        # ---- Save / reload round trip --------------------------------------
        save_section: dict[str, Any] = {}
        model_path = workspace / "surrogate_dnn_probe.mph"
        save_section["save"] = _attempt("model.save", lambda: model.save(str(model_path)))
        if model_path.is_file():
            save_section["saved_bytes"] = model_path.stat().st_size
            save_section["saved_sha256"] = _sha256_file(model_path)
        reloaded = None
        reload_section: dict[str, Any] = {}

        def _reload():
            nonlocal reloaded
            client.clear()
            reloaded = client.load(str(model_path))
            return str(reloaded.name())

        reload_section["reload"] = _attempt("client.load", _reload)
        if reloaded is not None:
            rjm = reloaded.java
            reload_section["study_tags"] = _attempt(
                "study().tags()", lambda: list(rjm.study().tags())
            )
            reload_section["func_tags"] = _attempt("func().tags()", lambda: list(rjm.func().tags()))
            reload_section["dnn_survived_reload"] = _attempt(
                "reloaded func(dnn1)", lambda: str(rjm.func("dnn1").getType())
            )
            try:
                rstep = rjm.study("std1").feature("smt1")
                reload_section["step_survived_reload"] = _attempt(
                    "reloaded step.getType", lambda: str(rstep.getType())
                )
                reload_section["step_readback_after_reload"] = _probe_property_set(
                    rstep, ("surrogatemodel", "layertype", "outfeatures", "activation")
                )
            except Exception as exc:
                reload_section["step_survived_reload"] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            try:
                rdnn = rjm.func("dnn1")
                reload_section["dnn_seed_readback_after_reload"] = {
                    name: _read_property(rdnn, name)
                    for name in ("useseed", "rndseed", "useseedvalidation", "rndseedvalidation")
                }
            except Exception as exc:
                reload_section["dnn_seed_readback_after_reload"] = {
                    "error": f"{type(exc).__name__}: {exc}"
                }
        save_section["reload"] = reload_section
        result["save_reload"] = save_section

        result["success"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            try:
                client.clear()
            except Exception as exc:
                result["client_clear_error"] = f"{type(exc).__name__}: {exc}"
                result["success"] = False
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        _write_receipt(output, result)
    return 0 if result["success"] else 1


def _run_parent(args) -> int:
    output = args.output.expanduser().resolve()
    if output.exists():
        raise ValueError("probe output must use a new path")
    git = _git_identity()

    output.parent.mkdir(parents=True, exist_ok=True)
    workspace = output.parent / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    worker_output = output.with_name(f".{output.stem}.worker.{uuid.uuid4().hex}.json")
    worker_stdout = worker_output.with_suffix(".stdout.log")
    worker_stderr = worker_output.with_suffix(".stderr.log")
    owner = SolverOwnership(args.runtime_root, owner="surrogate-dnn-capability-probe")

    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "source": {
            "git": git,
            "script": str(Path(__file__).resolve().relative_to(ROOT)),
            "script_sha256": _sha256_file(Path(__file__).resolve()),
        },
        "environment": {
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
    }
    process = None
    lease_acquired = False
    child_identities: dict[tuple[int, float], dict] = {}
    listeners: dict[tuple[int, int], dict] = {}
    started = time.monotonic()
    returncode = 1

    def observe_descendants() -> list[dict]:
        inventory = _descendant_identities(os.getpid())
        if not inventory["complete"]:
            raise RuntimeError(f"owned descendant inventory failed: {inventory['error']}")
        for identity in inventory["identities"]:
            child_identities[(identity["pid"], identity["process_create_time"])] = identity
        return inventory["identities"]

    try:
        before = _wait_clean(owner, timeout_seconds=1.0)
        receipt["ownership_before"] = _status_evidence(before)
        if not _status_is_clean(before):
            raise RuntimeError("probe requires no collision, lease, or active durable job")
        preflight = owner.preflight(
            output_path=str(output),
            requested_version="6.4",
            minimum_free_gb=args.minimum_free_gb,
        )
        receipt["preflight"] = preflight
        if not preflight.get("ready"):
            raise RuntimeError(f"solver preflight failed: {preflight.get('blockers')}")
        claim = owner.acquire(mode="surrogate-dnn-capability-probe")
        receipt["lease_claim"] = claim
        if not claim.get("success"):
            raise RuntimeError(claim.get("error", "solver lease acquisition failed"))
        lease_acquired = True

        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--confirm",
            "RUN_REAL_COMSOL",
            "--worker-output",
            str(worker_output),
            "--cores",
            str(args.cores),
        ]
        with worker_stdout.open("wb") as stdout_handle, worker_stderr.open("wb") as stderr_handle:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=stdout_handle,
                stderr=stderr_handle,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            deadline = time.monotonic() + args.timeout_seconds
            timed_out = False
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    _terminate_owned_tree(process)
                    break
                if not owner.heartbeat(refresh_server_processes=True):
                    raise RuntimeError("solver lease heartbeat failed")
                descendants = observe_descendants()
                ports = _listener_inventory({item["pid"] for item in descendants})
                if not ports["complete"]:
                    raise RuntimeError(f"owned listener inventory failed: {ports['error']}")
                for listener in ports["listeners"]:
                    listeners[(listener["pid"], listener["port"])] = listener
                time.sleep(0.25)
            observe_descendants()
            process.wait(timeout=15)
        stdout = worker_stdout.read_bytes().decode("utf-8", errors="replace")
        stderr = worker_stderr.read_bytes().decode("utf-8", errors="replace")
        receipt["worker_execution"] = {
            "returncode": process.returncode,
            "timed_out": timed_out,
            "stdout_tail": stdout[-4000:],
            "stderr_tail": stderr[-4000:],
        }
        if worker_output.is_file():
            receipt["worker_result_sha256"] = _sha256_file(worker_output)
            receipt["worker_result"] = json.loads(worker_output.read_text(encoding="utf-8"))
        else:
            receipt["worker_result"] = None

        if not owner.heartbeat(refresh_server_processes=True):
            raise RuntimeError("final solver lease heartbeat failed")
        active = owner.status(require_fresh_inventory=True)
        receipt["ownership_during"] = _status_evidence(active)
        lease = active.get("lease", {}).get("lease") or {}
        receipt["runtime_process_evidence"] = {
            "descendants": sorted(
                child_identities.values(),
                key=lambda item: (item["process_create_time"], item["pid"]),
            ),
            "owned_listeners": sorted(
                listeners.values(), key=lambda item: (item["pid"], item["port"])
            ),
            "lease_server_processes": lease.get("comsol_server_processes"),
            "lease_server_port": lease.get("comsol_server_port"),
        }
        worker_result = receipt.get("worker_result") or {}
        if (
            process.returncode != 0
            or timed_out
            or worker_result.get("success") is not True
            or active.get("durable_jobs", {}).get("active_count") != 0
        ):
            raise RuntimeError("probe phase did not satisfy its evidence contract")
        returncode = 0
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup_errors = []
        process_cleanup = None
        if process is not None:
            try:
                process_cleanup = _terminate_owned_tree(process, tuple(child_identities.values()))
            except Exception as exc:
                cleanup_errors.append({"stage": "process", "type": type(exc).__name__})
        lease_release = None
        if lease_acquired:
            try:
                lease_release = owner.release()
            except Exception as exc:
                cleanup_errors.append({"stage": "lease_release", "type": type(exc).__name__})
        receipt["lease_release"] = lease_release
        after = None
        try:
            after = _wait_clean(owner)
            receipt["ownership_after"] = _status_evidence(after)
        except Exception as exc:
            cleanup_errors.append({"stage": "ownership_after", "type": type(exc).__name__})
            receipt["ownership_after"] = None
        remaining_inventory = {"complete": False, "error": "not_collected", "identities": []}
        try:
            remaining_inventory = _descendant_identities(os.getpid())
        except Exception as exc:
            cleanup_errors.append({"stage": "descendants_after", "type": type(exc).__name__})
        remaining_descendants = remaining_inventory["identities"]
        final_listener_snapshot = {"complete": False, "error": "not_collected", "listeners": []}
        try:
            final_listener_snapshot = _listener_inventory(
                {item["pid"] for item in [*child_identities.values(), *remaining_descendants]}
            )
        except Exception as exc:
            cleanup_errors.append({"stage": "listeners_after", "type": type(exc).__name__})
        cleanup_passed = (
            not cleanup_errors
            and isinstance(after, dict)
            and _status_is_clean(after)
            and remaining_inventory["complete"] is True
            and not remaining_descendants
            and final_listener_snapshot.get("complete") is True
            and final_listener_snapshot.get("listeners") == []
            and (process_cleanup is None or process_cleanup.get("passed") is True)
        )
        receipt["cleanup"] = {
            "lease_absent": isinstance(after, dict)
            and after.get("lease", {}).get("state") == "absent",
            "collision_absent": isinstance(after, dict) and after.get("collision") is False,
            "active_job_count": (
                after.get("durable_jobs", {}).get("active_count")
                if isinstance(after, dict)
                else None
            ),
            "process_cleanup": process_cleanup,
            "owned_descendants": remaining_descendants,
            "owned_descendants_inventory_complete": remaining_inventory["complete"],
            "owned_descendants_absent": (
                remaining_inventory["complete"] is True and not remaining_descendants
            ),
            "final_listener_snapshot": final_listener_snapshot,
            "errors": cleanup_errors,
            "passed": cleanup_passed,
        }
        receipt["duration_seconds"] = round(time.monotonic() - started, 3)
        receipt["success"] = returncode == 0 and cleanup_passed
        if not receipt["success"]:
            returncode = 1
        try:
            _write_receipt(output, receipt)
        finally:
            worker_output.unlink(missing_ok=True)
            worker_stdout.unlink(missing_ok=True)
            worker_stderr.unlink(missing_ok=True)
    print(output)
    return returncode


def _dry_run(args) -> int:
    payload = {
        "schema_name": f"{SCHEMA_NAME}_dry_run",
        "schema_version": SCHEMA_VERSION,
        "source_sha256": _sha256_file(Path(__file__).resolve()),
        "isolation_contract": {
            "starts_comsol": False,
            "acquires_solver_lease": False,
            "imports_mph": False,
            "network_access": False,
            "mutates_source_models": False,
            "generic_property_mutation": False,
        },
        "documented_study_property_count": len(DOCUMENTED_STUDY_PROPERTIES),
        "documented_dnn_property_count": len(DOCUMENTED_DNN_PROPERTIES),
        "method_interest_fragments": list(METHOD_INTEREST),
        "bounds": {
            "max_property_names": MAX_PROPERTY_NAMES,
            "max_method_names": MAX_METHOD_NAMES,
            "max_value_chars": MAX_VALUE_CHARS,
            "timeout_seconds": args.timeout_seconds,
            "cores": args.cores,
        },
        "output": str(args.output.expanduser().resolve()) if args.output else None,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    print(text)
    return 0


def main() -> int:
    args = _parser().parse_args()
    if not 1 <= args.cores <= 64:
        raise SystemExit("--cores must be in 1..64")
    if not 1.0 <= args.timeout_seconds <= 7200.0:
        raise SystemExit("--timeout-seconds must be in 1..7200")
    if args.dry_run:
        return _dry_run(args)
    if args.worker:
        if args.confirm != "RUN_REAL_COMSOL":
            raise SystemExit("worker mode requires --confirm RUN_REAL_COMSOL")
        if args.worker_output is None:
            raise SystemExit("worker mode requires --worker-output")
        resolved = args.worker_output.resolve()
        return _run_worker(resolved, args.cores, resolved.parent / "workspace")
    if args.confirm != "RUN_REAL_COMSOL" or args.output is None:
        raise SystemExit("licensed probe requires --confirm RUN_REAL_COMSOL and --output")
    try:
        return _run_parent(args)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
