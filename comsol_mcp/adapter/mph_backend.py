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

from comsol_mcp.adapter.conversion import (
    convert_explicitly,
    normalize_evaluation_result,
    unwrap_backend_value,
)
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
        """Read a model's identity, calling MPh's methods rather than assuming attributes.

        Measured defect this fixes: on MPh 1.3.1, ``Model.name`` and ``Model.file``
        are **methods** (``def name(self) -> str``), not attributes. A first
        implementation used ``getattr(model, "name")``, which returned the bound
        method itself, so a receipt recorded
        ``<bound method Model.name of Model('s4a_probe')>`` instead of the name.
        The licensed gate caught it. Each accessor is therefore called when it is
        callable, and an unreadable value is reported as unknown rather than
        stringified.
        """

        def resolve(attribute: str) -> Any:
            raw = getattr(model, attribute, None)
            if raw is None:
                return None
            return raw() if callable(raw) else raw

        name_value = resolve("name")
        file_value = resolve("file")
        return ModelIdentity(
            name=str(name_value) if name_value else "",
            file=str(file_value) if file_value else None,
            # MPh exposes no content hash here. An unprovable identity is
            # reported as unknown rather than inferred from the path.
            content_sha256=None,
        )

    # -- step 2: tag/list/node lookup ---------------------------------------
    @staticmethod
    def node_path_shapes() -> dict[tuple[str, ...], int]:
        """The representable node path shapes, keyed by kind with their arity.

        Declared as data so a caller, a test, or a receipt can enumerate exactly
        which node kinds the adapter addresses, instead of discovering them by
        trial and error.
        """
        return {
            ("component",): 2,
            ("component", "geom"): 3,
            ("component", "geom", "feature"): 4,
            ("physics",): 3,
            ("study",): 2,
            ("study", "feature"): 3,
            ("solution",): 2,
            ("dataset",): 2,
        }

    @classmethod
    def _shape_is_representable(cls, path: Sequence[str]) -> bool:
        """Whether a path has a kind and arity the adapter can resolve."""
        if len(path) < 2:
            return False
        if path[0] == "component":
            return len(path) in {2, 3, 4}
        if path[0] == "physics":
            return len(path) == 3
        if path[0] == "study":
            return len(path) in {2, 3}
        if path[0] in {"solution", "dataset"}:
            return len(path) == 2
        return False

    def _java_node(self, path: Sequence[str]) -> Any:
        """Resolve a node path through the Java ClientAPI accessor chain.

        Measured defect this fixes: the high-level MPh ``Node`` view is unusable on
        this localized COMSOL install. ``model.components()`` returns the localized
        label ``组件 1`` rather than the tag ``comp1``, ``Node.exists()`` is False
        even for a created component, and ``Node.children()`` raises
        ``AttributeError: 'NoneType' object has no attribute 'tags'``. See
        ``D:\\mcp_tests\\a75s4api\\node_api.json`` and
        ``D:\\mcp_tests\\a75s4res\\resolution.json``.

        The repository already resolves nested nodes through the Java accessor
        chain used by its runtime jobs (``component(tag).geom(tag).feature(tag)``),
        so the adapter follows that proven route and addresses nodes by **tag**,
        which is stable and localized-label independent.

        The shape is validated before the model is touched, so an unrepresentable
        path is refused without needing a session at all.
        """
        if not self._shape_is_representable(path):
            raise AdapterError(
                "node_not_found",
                f"unsupported node path shape: {'/'.join(path) or '<empty>'}",
                operation="node_lookup",
            )
        model = self._require_model("node_lookup")
        java = model.java
        kind = path[0]
        try:
            if kind == "component" and len(path) == 2:
                return java.component(path[1])
            if kind == "component" and len(path) == 3:
                return java.component(path[1]).geom(path[2])
            if kind == "component" and len(path) == 4:
                return java.component(path[1]).geom(path[2]).feature(path[3])
            if kind == "physics" and len(path) == 3:
                return java.component(path[1]).physics(path[2])
            if kind == "study" and len(path) == 2:
                return java.study(path[1])
            if kind == "study" and len(path) == 3:
                return java.study(path[1]).feature(path[2])
            if kind == "solution" and len(path) == 2:
                return java.sol(path[1])
            if kind == "dataset" and len(path) == 2:
                return java.result().dataset(path[1])
        except Exception as exc:
            raise AdapterError(
                "node_not_found",
                f"no node at {'/'.join(path)}",
                operation="node_lookup",
            ) from exc
        # Unreachable while `node_path_shapes()` and this dispatch agree; kept as
        # a total-function guard so a future shape cannot silently return None.
        raise AdapterError(
            "node_not_found",
            f"no accessor is defined for {'/'.join(path)}",
            operation="node_lookup",
        )

    def find_node(self, path: Sequence[str]) -> NodeRef:
        """Resolve a node by an explicit kind-prefixed path of Java tags.

        The path is ``(kind, *tags)`` so a caller states what it is addressing
        instead of relying on an ambiguous label walk, for example
        ``("component", "comp1", "geom1")``.
        """
        if not path:
            raise AdapterError(
                "node_not_found",
                "A node path must contain at least one tag.",
                operation="node_lookup",
            )
        java = self._java_node(path)
        node_type = None
        try:
            type_method = getattr(java, "getType", None)
            if callable(type_method):
                node_type = str(type_method())
        except Exception:
            # A type that cannot be read is reported unknown, not guessed.
            node_type = None
        return NodeRef(tag=path[-1], path=tuple(path), node_type=node_type)

    def children(self, node: NodeRef) -> Sequence[NodeRef]:
        """List the child tags of a resolved node.

        Only node kinds with a well-defined child listing are supported, because
        guessing a listing would invent structure the model may not have.
        """
        if len(node.path) != 2 or node.path[0] not in {"component", "study"}:
            raise AdapterError(
                "node_not_found",
                f"children are not enumerated for {'/'.join(node.path)}",
                operation="node_children",
            )
        java = self._java_node(node.path)
        try:
            if node.path[0] == "component":
                tags = list(java.geom().tags())
            else:
                tags = list(java.feature().tags())
        except Exception as exc:
            raise AdapterError(
                "node_not_found",
                f"cannot list children of {'/'.join(node.path)}",
                operation="node_children",
            ) from exc
        prefix = node.path[0]
        return [
            NodeRef(
                tag=str(tag),
                path=(prefix, node.path[1], str(tag)),
                node_type=None,
            )
            for tag in tags
        ]

    # -- step 3: explicit conversion ----------------------------------------
    def convert(self, value: Any, *, form: str, target: str) -> ConvertedValue:
        # Delegated to the project's converter: neither lane defines conversion.
        del target
        return convert_explicitly(value, form=form)

    # -- step 4: typed property access with rollback ------------------------
    def read_property(self, node: NodeRef, name: str, *, form: str) -> ConvertedValue:
        """Read one property and convert it explicitly.

        The raw value is unwrapped first because COMSOL returns JVM proxies: a
        measured `getString` returns `jpype._jstring` (`java.lang.String`), so
        `isinstance(value, str)` is False and a strict conversion would report a
        spurious type mismatch. Only backend-produced values are unwrapped;
        caller-supplied values stay strictly checked.
        """
        try:
            java = self._java_node(node.path)
            raw = java.getString(name)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                "property_not_found",
                f"no property {name!r} on {'/'.join(node.path)}",
                operation="property_read",
            ) from exc
        unwrapped = unwrap_backend_value(raw)
        try:
            return convert_explicitly(unwrapped, form=form)
        except AdapterError as exc:
            raise AdapterError(
                "property_type_mismatch",
                f"property {name!r} on {'/'.join(node.path)} is not a {form} "
                f"(COMSOL returned {type(raw).__name__})",
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
        try:
            java = self._java_node(plan.node.path)
        except AdapterError as exc:
            return PropertyWriteReceipt(
                node=plan.node,
                name=plan.name,
                applied=False,
                rolled_back=False,
                reason_code=exc.reason_code,
            )
        try:
            if plan.value.form == "str":
                java.set(plan.name, plan.value.value)
            elif plan.value.form == "int":
                java.set(plan.name, str(plan.value.value))
            elif plan.value.form == "float":
                java.set(plan.name, repr(plan.value.value))
            else:
                java.set(plan.name, plan.value.value)
        except Exception as exc:
            rolled_back = self._rollback(java, plan)
            receipt_error = self._translate(exc, "property_write")
            return PropertyWriteReceipt(
                node=plan.node,
                name=plan.name,
                applied=False,
                rolled_back=rolled_back,
                reason_code=receipt_error.reason_code,
            )
        return PropertyWriteReceipt(node=plan.node, name=plan.name, applied=True, rolled_back=False)

    def _rollback(self, java: Any, plan: PropertyWritePlan) -> bool:
        try:
            if plan.previous.form == "str":
                java.set(plan.name, plan.previous.value)
            elif plan.previous.form == "float":
                java.set(plan.name, repr(plan.previous.value))
            else:
                java.set(plan.name, plan.previous.value)
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
        # MPh returns a numpy array even for a scalar expression, so the shape is
        # normalized explicitly rather than assumed to be a Python scalar.
        try:
            return normalize_evaluation_result(raw)
        except AdapterError as exc:
            raise AdapterError(
                "conversion_not_representable",
                str(exc),
                operation="model_evaluate",
            ) from exc

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
