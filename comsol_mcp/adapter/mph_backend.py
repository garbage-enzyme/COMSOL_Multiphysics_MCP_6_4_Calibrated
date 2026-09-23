"""MPh-backed COMSOL adapter backend (S4A steps 1-5).

This module is the **only** place in the project that constructs an MPh client
for operational work. It implements :class:`comsol_mcp.adapter.protocol.ComsolAdapter`
so callers depend on the protocol rather than on MPh.

Lane isolation
--------------

``MphReferenceBackend`` is the 1.3.1 reference lane. A 1.4.0 backend is a separate
class behind the same protocol; both share conversion, rollback, and error
translation from this module so a lane cannot quietly change those rules.

What stays with the project
---------------------------

This backend does not acquire or release solver leases, does not decide cores or
version (the caller supplies them and they are passed through unchanged), and
does not retry a mutation. ``CREATE_NO_WINDOW`` appears only in MPh 1.4.0, so the
backend never relies on MPh's own process-startup handling for the project's
process contract.

Import discipline
-----------------

``mph`` is imported inside methods, never at module import time, so importing this
module cannot start a JVM or create a solver connection.
"""

from __future__ import annotations

from typing import Any, Sequence

from comsol_mcp.adapter.conversion import convert_explicitly
from comsol_mcp.adapter.protocol import (
    REFERENCE_MPH_LANE,
    AdapterError,
    ConvertedValue,
    EvaluationRequest,
    ModelIdentity,
    NodeRef,
    PropertyWritePlan,
    PropertyWriteReceipt,
    SessionIdentity,
    SessionRequest,
)


def _client_kwargs(request: SessionRequest) -> dict[str, Any]:
    """Build MPh client kwargs, dropping only unset values.

    Ownership rule: the caller's declared cores/version/host/port are passed
    through unchanged.  A backend must not substitute its own defaults, because
    that would silently change solver resources.
    """
    candidates = {
        "cores": request.cores,
        "version": request.version,
        "host": request.host,
        "port": request.port,
    }
    return {key: value for key, value in candidates.items() if value is not None}


class MphBackendBase:
    """Shared, lane-independent behaviour for MPh-backed adapters.

    Conversion, rollback validation, rollback execution, and exception
    translation live here so both lanes cannot diverge on the rules the plan
    says must stay project-owned.
    """

    #: Overridden by each lane.
    lane: str = REFERENCE_MPH_LANE
    backend_name: str = "mph"

    def __init__(self) -> None:
        self._client: Any = None
        self._mph_version: str | None = None

    # -- lane metadata -------------------------------------------------------
    def _require_client(self, operation: str) -> Any:
        if self._client is None:
            raise AdapterError(
                "session_not_open",
                "No COMSOL session is open; call open_session first.",
                operation=operation,
            )
        return self._client

    def _require_model(self, operation: str) -> Any:
        client = self._require_client(operation)
        try:
            models = list(client.models())
        except Exception as exc:  # pragma: no cover - backend-specific
            raise self._translate(exc, operation) from exc
        if len(models) != 1:
            raise AdapterError(
                "backend_internal_error",
                f"expected exactly one loaded model, found {len(models)}",
                operation=operation,
            )
        return models[0]

    # -- step 1: session/client and model identity ---------------------------
    def open_session(self, request: SessionRequest) -> SessionIdentity:
        if self._client is not None:
            raise AdapterError(
                "session_already_open",
                "A COMSOL session is already open; close it before opening another.",
                operation="session_open",
            )
        import mph

        # ``mph.Client`` is resolved through getattr because MPh ships no type
        # information for it: a direct attribute access would not be provable
        # under --strict, and a blanket suppression would hide a real rename.
        factory = getattr(mph, "Client", None)
        if factory is None:
            raise AdapterError(
                "adapter_unavailable",
                "the installed MPh package exposes no Client; the lane cannot be used",
                operation="session_open",
            )
        try:
            client = factory(**self._client_kwargs(request))
        except Exception as exc:
            raise self._translate(exc, "session_open") from exc
        self._client = client
        self._mph_version = str(getattr(mph, "__version__", "") or "") or None
        return self.session_identity() or SessionIdentity(
            mph_version=self._mph_version or "unknown",
            comsol_version=None,
            host=request.host,
            port=request.port,
            standalone=request.standalone,
        )

    def _client_kwargs(self, request: SessionRequest) -> dict[str, Any]:
        return _client_kwargs(request)

    def close_session(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        # Clearing the wrapper is the project's cleanup step; MPh's own
        # disconnect semantics differ between lanes, so the project performs the
        # only action it can rely on and reports nothing it cannot verify.
        try:
            clear = getattr(client, "clear", None)
            if callable(clear):
                clear()
        except Exception as exc:
            raise self._translate(exc, "session_close") from exc

    def session_identity(self) -> SessionIdentity | None:
        if self._client is None:
            return None
        client = self._client
        try:
            versions = list(client.versions())
            comsol_version = versions[0] if versions else None
        except Exception:
            # An unreadable version is reported as unknown rather than guessed.
            comsol_version = None
        return SessionIdentity(
            mph_version=self._mph_version or "unknown",
            comsol_version=str(comsol_version) if comsol_version else None,
            host=getattr(client, "host", None),
            port=getattr(client, "port", None),
            standalone=bool(getattr(client, "standalone", False)),
        )

    def load_model(self, path: str) -> ModelIdentity:
        client = self._require_client("model_load")
        try:
            model = client.load(path)
        except Exception as exc:
            raise self._translate(exc, "model_load") from exc
        return self._model_identity(model)

    def save_model(self, path: str) -> ModelIdentity:
        model = self._require_model("model_save")
        try:
            model.save(path)
        except Exception as exc:
            raise self._translate(exc, "model_save") from exc
        return self._model_identity(model)

    def _model_identity(self, model: Any) -> ModelIdentity:
        name = str(getattr(model, "name", "") or "")
        file_value = getattr(model, "file", None)
        return ModelIdentity(
            name=name,
            file=str(file_value) if file_value else None,
            # MPh exposes no content hash here. An unprovable identity is
            # reported as unknown rather than inferred from the path.
            content_sha256=None,
        )

    # -- step 2: tag/list/node lookup ---------------------------------------
    def find_node(self, path: Sequence[str]) -> NodeRef:
        model = self._require_model("node_lookup")
        if not path:
            raise AdapterError(
                "node_not_found",
                "A node path must contain at least one tag.",
                operation="node_lookup",
            )
        node: Any = model
        walked: list[str] = []
        for tag in path:
            walked.append(tag)
            try:
                node = node.java.component(tag) if len(walked) == 1 else node.child(tag)
            except Exception as exc:
                raise AdapterError(
                    "node_not_found",
                    f"no node at {'/'.join(walked)}",
                    operation="node_lookup",
                ) from exc
        return NodeRef(tag=path[-1], path=tuple(path), node_type=None)

    def children(self, node: NodeRef) -> Sequence[NodeRef]:
        model = self._require_model("node_children")
        try:
            java = model.java
            parent = java.component(node.tag) if not node.path[:-1] else None
            tags = list(parent.features() if parent is not None else [])
        except Exception as exc:
            raise AdapterError(
                "node_not_found",
                f"cannot list children of {node.tag}",
                operation="node_children",
            ) from exc
        return [node.child(str(tag)) for tag in tags]

    # -- step 3: explicit conversion ----------------------------------------
    def convert(self, value: Any, *, form: str, target: str) -> ConvertedValue:
        # Delegated to the project's converter: neither lane defines conversion.
        del target
        return convert_explicitly(value, form=form)

    # -- step 4: typed property access with rollback ------------------------
    def read_property(self, node: NodeRef, name: str, *, form: str) -> ConvertedValue:
        model = self._require_model("property_read")
        try:
            raw = model.java.component(node.tag).getString(name)
        except Exception as exc:
            raise AdapterError(
                "property_not_found",
                f"no property {name!r} on {node.tag}",
                operation="property_read",
            ) from exc
        try:
            return convert_explicitly(raw, form=form)
        except AdapterError as exc:
            raise AdapterError(
                "property_type_mismatch",
                f"property {name!r} on {node.tag} is not a {form}",
                operation="property_read",
            ) from exc

    def plan_write(self, node: NodeRef, name: str, value: ConvertedValue) -> PropertyWritePlan:
        # Capture the previous value first. A write whose previous value cannot
        # be read is not representable, because it could not be rolled back.
        previous = self.read_property(node, name, form=value.form)
        return PropertyWritePlan(
            node=node,
            name=name,
            value=value,
            previous=previous,
            rollback_required=True,
        )

    def apply_write(self, plan: PropertyWritePlan) -> PropertyWriteReceipt:
        model = self._require_model("property_write")
        component = model.java.component(plan.node.tag)
        try:
            if plan.value.form == "str":
                component.set(plan.name, plan.value.value)
            elif plan.value.form == "int":
                component.set(plan.name, str(plan.value.value))
            elif plan.value.form == "float":
                component.set(plan.name, repr(plan.value.value))
            else:
                component.set(plan.name, plan.value.value)
        except Exception as exc:
            rolled_back = self._rollback(component, plan)
            receipt_error = self._translate(exc, "property_write")
            return PropertyWriteReceipt(
                node=plan.node,
                name=plan.name,
                applied=False,
                rolled_back=rolled_back,
                reason_code=receipt_error.reason_code,
            )
        return PropertyWriteReceipt(node=plan.node, name=plan.name, applied=True, rolled_back=False)

    def _rollback(self, component: Any, plan: PropertyWritePlan) -> bool:
        try:
            if plan.previous.form == "str":
                component.set(plan.name, plan.previous.value)
            elif plan.previous.form == "float":
                component.set(plan.name, repr(plan.previous.value))
            else:
                component.set(plan.name, plan.previous.value)
        except Exception:
            return False
        return True

    # -- step 5: evaluate and dataset/solution access -----------------------
    def evaluate(self, request: EvaluationRequest) -> ConvertedValue:
        model = self._require_model("model_evaluate")
        kwargs: dict[str, Any] = {}
        if request.unit is not None:
            kwargs["unit"] = request.unit
        if request.dataset is not None:
            kwargs["dataset"] = request.dataset
        if request.inner:
            kwargs["inner"] = list(request.inner)
        if request.outer:
            kwargs["outer"] = list(request.outer)
        try:
            raw = model.evaluate(request.expression, **kwargs)
        except Exception as exc:
            raise self._translate(exc, "model_evaluate") from exc
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            form = "int" if isinstance(raw, int) else "float"
            return convert_explicitly(raw, form=form)
        raise AdapterError(
            "conversion_not_representable",
            "the evaluation returned a shape this protocol does not represent yet",
            operation="model_evaluate",
        )

    def dataset_names(self) -> Sequence[str]:
        model = self._require_model("dataset_access")
        try:
            return [str(name) for name in model.datasets()]
        except Exception as exc:
            raise AdapterError(
                "dataset_unavailable",
                "dataset names could not be read",
                operation="dataset_access",
            ) from exc

    def solution_names(self) -> Sequence[str]:
        model = self._require_model("solution_access")
        try:
            return [str(name) for name in model.solutions()]
        except Exception as exc:
            raise AdapterError(
                "solution_unavailable",
                "solution names could not be read",
                operation="solution_access",
            ) from exc

    # -- error translation ---------------------------------------------------
    def _translate(self, exc: BaseException, operation: str) -> AdapterError:
        """Translate a backend exception into one stable reason code.

        Version-specific text is not passed through as a code: callers switch on
        ``reason_code``, and the original message is preserved only as detail.
        """
        if isinstance(exc, AdapterError):
            return exc
        name = type(exc).__name__
        if name == "NotImplementedError":
            return AdapterError(
                "session_already_open",
                "the backend refused a second client in this process",
                operation=operation,
            )
        if isinstance(exc, FileNotFoundError):
            return AdapterError(
                "model_load_failed", "the model file does not exist", operation=operation
            )
        if isinstance(exc, (KeyError, AttributeError)):
            return AdapterError(
                "node_not_found", f"the backend reported: {exc}", operation=operation
            )
        if isinstance(exc, (ValueError, TypeError)):
            return AdapterError(
                "property_type_mismatch",
                f"the backend rejected the value: {exc}",
                operation=operation,
            )
        return AdapterError(
            "backend_internal_error",
            f"{name}: {exc}",
            operation=operation,
        )


class MphReferenceBackend(MphBackendBase):
    """The MPh 1.3.1 reference lane.

    This is the default backend. It must never be replaced silently: a 1.4 lane
    is a separate class selected explicitly through
    :func:`comsol_mcp.adapter.make_backend`.
    """

    lane = REFERENCE_MPH_LANE
    backend_name = "mph-1.3.1"


__all__ = ["MphBackendBase", "MphReferenceBackend"]
