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

from typing import Any, Mapping, Sequence

from comsol_mcp.adapter.conversion import (
    convert_explicitly,
    normalize_evaluation_result,
    unwrap_backend_value,
)
from comsol_mcp.adapter.protocol import (
    DNN_FEATURE_METHODS,
    JAVA_WRITE_KINDS,
    REFERENCE_MPH_LANE,
    AdapterError,
    ConvertedValue,
    EvaluationRequest,
    JavaTypedRead,
    JavaTypedWrite,
    ModelIdentity,
    NodeRef,
    PropertyWritePlan,
    PropertyWriteReceipt,
    ResolvedNode,
    SessionIdentity,
    SessionRequest,
    installed_mph_version,
    matrix_read_row_limit,
)


def _model_tag(java: Any) -> str | None:
    """Read one Java model's tag, or ``None`` when it cannot be read."""
    try:
        return str(java.tag())
    except Exception:
        # A tag that cannot be read is reported unknown, never guessed.
        return None


def _live_model_tags(models: Sequence[Any]) -> set[str]:
    """The set of Java tags a client currently holds."""
    tags: set[str] = set()
    for candidate in models:
        tag = _model_tag(getattr(candidate, "java", None))
        if tag is not None:
            tags.add(tag)
    return tags


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

    #: Overridden by each lane. `lane` is a class-level *intent*; the observed
    #: version is reported by `session_identity()` and is what capability
    #: decisions must use, because the installed version may be 1.3.2 within the
    #: same supported range.
    lane: str = REFERENCE_MPH_LANE
    backend_name: str = "mph"

    def __init__(self) -> None:
        self._client: Any = None
        self._mph_version: str | None = None
        #: True when the client was adopted from a caller rather than opened
        #: here. Initialized up front so a capability read cannot depend on
        #: whether ``attach_client`` has run yet.
        self._attached: bool = False
        #: The exact model this backend was told to operate on, when adopted.
        #: ``None`` means "whatever the session holds", which is only unambiguous
        #: for a session the backend opened itself.
        self._adopted_model: Any = None

    # -- lane metadata -------------------------------------------------------
    def observed_lane(self) -> str:
        """Return the MPh version actually installed, or the declared lane.

        Capability questions must use this rather than the class attribute: on an
        MPh 1.3.2 install a backend declared as 1.3.1 would otherwise claim a
        `DoubleRowMatrix` read limit that 1.3.2 does not have.
        """
        return installed_mph_version() or self.lane

    def matrix_row_capacity(self) -> int | None:
        """Return the installed lane's `DoubleRowMatrix` read limit, if any.

        An unknown version is reported as unknown by raising, rather than by
        reusing another version's limit.
        """
        return matrix_read_row_limit(self.observed_lane())

    def _require_client(self, operation: str) -> Any:
        if self._client is None:
            raise AdapterError(
                "session_not_open",
                "No COMSOL session is open; call open_session first.",
                operation=operation,
            )
        return self._client

    def _require_model(self, operation: str) -> Any:
        """Return the model this backend operates on.

        Measured need this fixes: the S6 training gate creates a *second* model on
        the same client (``SurrogateTrainingGateSeed17``) while the first is still
        loaded, and the strict "exactly one loaded model" rule then failed a
        licensed run with ``expected exactly one loaded model, found 2`` at a
        phase that had already trained successfully. An explicitly adopted model
        is therefore addressed by identity; the strict rule remains for a session
        this backend opened itself, where ambiguity really is a defect.
        """
        client = self._require_client(operation)
        if self._adopted_model is not None:
            return self._adopted_model
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
        attached = self._attached
        self._client = None
        self._attached = False
        self._adopted_model = None
        if client is None:
            return
        if attached:
            # An adopted client belongs to its creator. Dropping the reference is
            # the whole of this backend's cleanup; calling ``clear`` here would
            # mutate a resource the adapter never owned.
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
            # Step 6: the DNN surface addresses the function container and one
            # function feature inside it.
            ("function",): 2,
            ("function", "feature"): 3,
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
        if path[0] == "function":
            return len(path) in {2, 3}
        if path[0] in {"solution", "dataset"}:
            return len(path) == 2
        return False

    def _resolve_handle(self, target: Any) -> Any:
        """Return the Java object for a node reference or an already-resolved handle.

        ``ResolvedNode`` exists because the licensed gates build a model and then
        hold raw Java feature objects. Re-walking a path is impossible for such a
        handle, so it is passed through unchanged; a ``NodeRef`` is resolved
        through the accessor chain. Any other type is refused rather than being
        treated as a Java object, which would be an untyped escape hatch.
        """
        if isinstance(target, ResolvedNode):
            return target.handle
        if isinstance(target, NodeRef):
            return self._java_node(target.path)
        raise AdapterError(
            "node_not_found",
            f"expected a NodeRef or ResolvedNode, got {type(target).__name__}",
            operation="node_lookup",
        )

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
            if kind == "function" and len(path) == 2:
                return java.func(path[1])
            if kind == "function" and len(path) == 3:
                return java.func(path[1]).feature(path[2])
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

    # -- step 6: typed Java property access and DNN feature operations -------
    @staticmethod
    def _coerce_java(value: Any, kind: str) -> Any:
        """Wrap a Python value in the exact Java type the caller declared.

        Lifted from the DNN bridge so the coercion lives in one place: JPype
        cannot disambiguate ``set(String, int)`` from ``set(String, boolean)``
        when handed a bare Python ``int``, and raises an ambiguous-overload error
        rather than choosing. A caller therefore names the Java type and this
        method performs the wrapping.

        ``jpype`` is imported here, inside the call, so importing the adapter
        never imports jpype and never starts a JVM.
        """
        import jpype

        if kind == "string":
            return jpype.JString(str(value))
        if kind == "boolean":
            return jpype.JBoolean(bool(value))
        if kind == "int":
            return jpype.JInt(int(value))
        if kind == "double":
            return jpype.JDouble(float(value))
        if kind == "string_array":
            return jpype.JArray(jpype.JString)([str(item) for item in value])
        if kind == "string_matrix":
            # The nested ``[[Ljava.lang.String;`` form: a Java array of Java
            # string arrays, which is what properties declaring that type accept.
            rows = [[str(item) for item in row] for row in value]
            return jpype.JArray(jpype.JArray(jpype.JString))(rows)
        if kind == "int_array":
            return jpype.JArray(jpype.JInt)([int(item) for item in value])
        if kind == "double_array":
            return jpype.JArray(jpype.JDouble)([float(item) for item in value])
        raise AdapterError(
            "conversion_not_representable",
            f"unsupported Java write kind: {kind}",
            operation="java_typed_write",
        )

    def java_typed_write(self, request: JavaTypedWrite) -> None:
        """Write one property with the caller's declared Java type."""
        if request.kind not in JAVA_WRITE_KINDS:
            raise AdapterError(
                "conversion_not_representable",
                f"unsupported Java write kind: {request.kind}",
                operation="java_typed_write",
            )
        java = self._resolve_handle(request.node)
        if request.kind == "string_entry":
            # Alternating key/value properties are written through setEntry: the
            # ClientAPI reference directs callers to setEntry for them, and a
            # nested Java array passed to set() fails with "Unable to convert".
            if not isinstance(request.value, Mapping):
                raise AdapterError(
                    "conversion_not_representable",
                    "string_entry requires a mapping of key to value",
                    operation="java_typed_write",
                )
            try:
                for key, item in request.value.items():
                    java.setEntry(request.name, str(key), str(item))
            except Exception as exc:
                raise self._translate(exc, "java_typed_write") from exc
            return
        try:
            java.set(request.name, self._coerce_java(request.value, request.kind))
        except Exception as exc:
            raise self._translate(exc, "java_typed_write") from exc

    def java_typed_read(self, node: Any, name: str, *, form: str = "auto") -> JavaTypedRead:
        """Read one property through the typed accessors, recording which won.

        ``form`` selects which accessors may answer:

        ``auto``
            Try scalars first, then arrays, and report whichever accepts. This is
            the probing read, and the accessor name in the result is what tells a
            caller how to render the value.
        ``string_array``
            Try only ``getStringArray``. Measured need: the original
            ``bind_data_source`` called ``feature.getStringArray(name)`` directly,
            and routing that through ``auto`` changed its meaning. A
            comma-joined property such as ``col1, col2, col3`` is accepted by
            ``getString`` as one string, so ``auto`` returned that string and the
            caller split it into 16 characters, which made the S6 gate fail with
            ``data file exposes 16 columns but the schema declares 3``. A caller
            that knows it wants the array must be able to say so.
        """
        java = self._resolve_handle(node)
        accessors: tuple[str, ...]
        if form == "string_array":
            accessors = ("getStringArray",)
        elif form == "auto":
            accessors = (
                "getString",
                "getBoolean",
                "getInt",
                "getDouble",
                "getStringArray",
                "getStringMatrix",
                "getDoubleArray",
                "getDoubleMatrix",
            )
        else:
            raise AdapterError(
                "conversion_not_representable",
                f"unsupported typed read form: {form}",
                operation="property_read",
            )
        rejected: list[str] = []
        for accessor in accessors:
            try:
                raw = getattr(java, accessor)(name)
            except Exception as exc:
                # Probing tries every typed accessor, so a rejection is expected;
                # it is recorded so an unreadable property stays reportable.
                rejected.append(f"{accessor}: {type(exc).__name__}")
                continue
            if accessor in {"getString", "getBoolean", "getInt", "getDouble"}:
                return JavaTypedRead(
                    node=node,
                    name=name,
                    accessor=accessor,
                    value=unwrap_backend_value(raw),
                    rejected=tuple(rejected),
                )
            try:
                rendered: Any = [str(item) for item in list(raw)]
            except Exception as exc:
                rejected.append(f"{accessor}.render: {type(exc).__name__}")
                rendered = str(raw)
            return JavaTypedRead(
                node=node,
                name=name,
                accessor=accessor,
                value=rendered,
                rejected=tuple(rejected),
            )
        raise AdapterError(
            "property_not_found",
            f"no typed accessor accepted {name!r} on {'/'.join(node.path)} "
            f"(rejected: {', '.join(rejected)})",
            operation="property_read",
            rejected=tuple(rejected),
        )

    def java_string_array(self, node: Any, name: str) -> list[str]:
        """Read one string-array property explicitly, or refuse.

        This is the migrated form of the direct ``getStringArray`` call the DNN
        bridge used to make, and it is deliberately not the probing read: a
        scalar accessor must never be allowed to answer an array question.
        """
        read = self.java_typed_read(node, name, form="string_array")
        value = read.value
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]

    def run_feature(self, node: Any, *, method: str) -> None:
        """Invoke one declared lifecycle method on a resolved feature."""
        if method not in DNN_FEATURE_METHODS:
            raise AdapterError(
                "conversion_not_representable",
                f"unsupported feature method: {method}",
                operation="dnn_feature_run",
            )
        java = self._resolve_handle(node)
        try:
            getattr(java, method)()
        except Exception as exc:
            raise self._translate(exc, "dnn_feature_run") from exc

    def export_feature(self, node: Any, path: str) -> None:
        java = self._resolve_handle(node)
        try:
            java.export(str(path))
        except Exception as exc:
            raise self._translate(exc, "dnn_feature_export") from exc

    # -- container enumeration and node creation (plan step 6) --------------
    def attach_client(self, model: Any, *, client: Any = None) -> None:
        """Adopt an already-created client that the caller owns.

        The licensed gates create their own ``mph.Client`` and then need the
        adapter's operations on that client's models. MPh forbids a second client
        in one process, so the existing one is adopted rather than opened again.

        The client must be supplied explicitly. Measured defect this fixes: the
        first version read ``model.client``, but on MPh 1.3.1 ``mph.Model`` holds
        only ``java`` and exposes no back-reference to its owning client --
        ``Client.create`` returns ``Model(java)`` and nothing else -- so every
        licensed gate failed before training with
        ``the supplied object does not expose an MPh client``. A client that owns
        the given model is therefore looked for in ``client.models()``, which
        proves the pairing instead of assuming it.

        Ownership stays with the caller: this records the reference, and
        ``close_session`` drops it without clearing a client the caller created,
        because closing it would be a hidden resource change.
        """
        if client is None:
            raise AdapterError(
                "adapter_unavailable",
                "attach_client requires the MPh client that owns the model; "
                "MPh 1.3.1 Model exposes no client back-reference",
                operation="session_open",
            )
        if model is not None and not self._client_owns_model(client, model):
            raise AdapterError(
                "adapter_unavailable",
                "the supplied client does not own the supplied model",
                operation="session_open",
            )
        self._client = client
        self._attached = True
        self._adopted_model = model
        self._mph_version = installed_mph_version() or self._mph_version

    @staticmethod
    def _client_owns_model(client: Any, model: Any) -> bool:
        """Whether ``client`` really owns ``model``.

        Measured facts this encodes, both taken from real MPh 1.3.1 objects:

        * ``Client.models()`` returns **fresh ``Model`` wrappers** around the same
          Java model, so ``any(candidate is model ...)`` is always False for a
          model obtained from ``Client.create`` or ``Client.load``. The first
          version of this proof therefore refused every legitimate adoption, and
          the licensed preflight caught it.
        * Distinct live models do have distinct Java tags (``model1``, ``model2``),
          while every wrapper of one model reports that model's tag. The shared
          Java object or its tag is therefore the honest identity.

        A model whose Java tag is not in the client's **live** tag set is refused.
        ``client.models()`` is the authority and is re-read on every call.

        The proof deliberately does not accept a candidate by Python identity
        alone. A client that reports a wrapper it does not really own would
        otherwise be believed: ``_ForeignClient`` in the licensed preflight is
        exactly that case, and an ``is`` shortcut made the negative control pass
        when it had to fail. Only a readable live tag counts, so a model destroyed
        by ``Client.clear`` is refused because its tag no longer resolves.
        """
        try:
            owned = list(client.models())
        except Exception as exc:
            raise MphBackendBase()._translate(exc, "session_open") from exc
        target_java = getattr(model, "java", None)
        if target_java is None:
            return False
        live_tags = _live_model_tags(owned)
        if not live_tags:
            return False
        target_tag = _model_tag(target_java)
        if target_tag is not None and target_tag in live_tags:
            return True
        # Fall back to the Java object itself, which is shared between wrappers of
        # one live model even though the Python wrappers differ.
        return any(getattr(candidate, "java", None) is target_java for candidate in owned)

    def adopted_client(self) -> Any:
        """Return the adopted client, if one was adopted, else ``None``."""
        return self._client if self._attached else None

    def container_tags(self, kind: str) -> Sequence[str]:
        """List the child tags of a top-level container the adapter enumerates.

        Only containers with a well-defined tag listing are supported; anything
        else is refused rather than guessed.
        """
        accessors = {
            "study": ("study",),
            "function": ("func",),
            "component": ("component",),
        }
        names = accessors.get(kind)
        if names is None:
            raise AdapterError(
                "node_not_found",
                f"the adapter does not enumerate {kind!r} tags",
                operation="node_children",
            )
        model = self._require_model("node_children")
        for name in names:
            accessor = getattr(model.java, name, None)
            if not callable(accessor):
                continue
            try:
                # The ClientAPI container is overloaded: the zero-argument form
                # returns the tag list, while the tag form returns the feature
                # itself. Enumeration therefore calls it with no argument.
                container = accessor()
                return [str(tag) for tag in list(container.tags())]
            except Exception as exc:
                raise self._translate(exc, "node_children") from exc
        raise AdapterError(
            "node_not_found",
            f"the model exposes no {kind!r} container",
            operation="node_children",
        )

    def create_node(
        self,
        kind: str,
        tag: str,
        *,
        parent_tag: str | None = None,
        feature_type: str | None = None,
    ) -> NodeRef:
        """Create one node of a declared kind and return its reference.

        The representable creations are a closed set, so this cannot become a
        general "create anything" command channel.
        """
        model = self._require_model("node_lookup")
        java = model.java
        try:
            if kind == "study":
                java.study().create(tag)
                return NodeRef(tag=tag, path=("study", tag), node_type=None)
            if kind == "function":
                if feature_type is None:
                    raise AdapterError(
                        "conversion_not_representable",
                        "creating a function requires an explicit feature type",
                        operation="node_lookup",
                    )
                java.func().create(tag, feature_type)
                return NodeRef(tag=tag, path=("function", tag), node_type=feature_type)
            if kind == "study_step":
                if parent_tag is None or feature_type is None:
                    raise AdapterError(
                        "conversion_not_representable",
                        "creating a study step requires a parent study and a step type",
                        operation="node_lookup",
                    )
                java.study(parent_tag).feature().create(tag, feature_type)
                return NodeRef(tag=tag, path=("study", parent_tag, tag), node_type=feature_type)
        except AdapterError:
            raise
        except Exception as exc:
            raise self._translate(exc, "node_lookup") from exc
        raise AdapterError(
            "node_not_found",
            f"the adapter does not create {kind!r} nodes",
            operation="node_lookup",
        )

    def remove_node(self, kind: str, tag: str) -> None:
        """Remove one node of a declared kind."""
        model = self._require_model("node_lookup")
        java = model.java
        try:
            if kind == "study":
                java.study().remove(tag)
                return
            if kind == "function":
                java.func().remove(tag)
                return
        except Exception as exc:
            raise self._translate(exc, "node_lookup") from exc
        raise AdapterError(
            "node_not_found",
            f"the adapter does not remove {kind!r} nodes",
            operation="node_lookup",
        )

    def read_allowed_values(self, node: Any, name: str) -> list[str] | None:
        """Read the allowed values of an enumerated property, if it exposes them."""
        java = self._resolve_handle(node)
        try:
            raw = java.getAllowedPropertyValues(name)
        except Exception:
            return None
        if raw is None:
            return None
        return [str(item) for item in list(raw)]

    def java_value_type(self, node: Any, name: str) -> str | None:
        """Read a property's declared Java value type, or ``None`` if unreadable.

        Measured need: some ClientAPI properties accept only the nested
        ``[[Ljava.lang.String;`` form and reject the alternating key/value form,
        while others do the opposite. A caller that has to pick one therefore
        reads the declared type here instead of assuming, and an unreadable type
        is reported unknown rather than guessed.
        """
        java = self._resolve_handle(node)
        try:
            raw = java.getValueType(name)
        except Exception:
            return None
        if raw is None:
            return None
        return str(raw)

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
