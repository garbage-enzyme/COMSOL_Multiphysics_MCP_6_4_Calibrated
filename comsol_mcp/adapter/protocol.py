"""Project-owned COMSOL adapter protocol (S4A).

Why this module exists
----------------------

MPh version changes must not leak into the DNN, research, or evidence tools.
This module defines **one** internal protocol for the version-sensitive
operations the project performs, plus typed request/result objects and a
project-owned error taxonomy, so a caller never depends on which MPh release is
installed or on which module happens to expose a conversion helper.

What the protocol owns (and MPh does not)
-----------------------------------------

The project keeps ownership of process, lease, JVM, cleanup, solver-resource,
immutable-source, and evidence contracts.  A backend may *perform* an operation,
but it may not decide resource ownership, may not silently fall back, and may not
coerce values implicitly.  Concretely:

* values are converted explicitly by the caller through
  :func:`comsol_mcp.adapter.conversion.convert_explicitly`, so a silent
  string-to-number coercion cannot make two lanes look alike;
* every failure path raises a typed :class:`AdapterError` carrying a stable
  reason code, so version-specific exception text is translated once;
* the protocol has no generic ``java`` escape hatch.  An operation that cannot be
  represented is added deliberately, not reached through a command string.

Verified version differences this design absorbs
------------------------------------------------

Reconnaissance (`D:\\mcp_tests\\a75s4inv\\mph_131_140_differences.md`) measured
that MPh 1.3.1 and 1.4.0 expose an **identical** public class/method surface, but
differ in two ways the plan predicts:

* matrix conversion moved from ``model.py`` (1.3.1) into ``node.py`` (1.4.0),
  where ``DoubleRowMatrix`` is handled by calling ``getDoubleMatrix`` and
  reshaping;
* ``CREATE_NO_WINDOW`` appears only in 1.4.0, with expanded ``creationflags``.

So matrix conversion and process startup are the two operations a backend must
not be trusted to define on its own, and both are explicit protocol members.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

PROTOCOL_SCHEMA_NAME = "comsol_mcp.adapter.operation"
PROTOCOL_SCHEMA_VERSION = "1.0.0"

# The reference lanes this project supports today. `pyproject.toml` declares
# `mph>=1.3.1,<1.4`, and **1.3.2 exists on the index**, so an eager-upgrade
# install legitimately resolves to 1.3.2. Both are reference lanes. A 1.4 lane is
# opt-in and never becomes the default implicitly.
REFERENCE_MPH_LANE = "1.3.1"
SUPPORTED_MPH_LANES = ("1.3.1", "1.3.2")

#: Behaviour that varies across the supported `<1.4` range, keyed by version.
#: MPh 1.3.1 limits `DoubleRowMatrix` **reads** to two rows and raises `TypeError`
#: beyond that; 1.3.2 generalizes the same branch to any row count, exactly as
#: 1.4.0 does (verified against both wheels). The limit is therefore a property of
#: the *installed version*, not of a backend class: a claim tied to the class
#: would be wrong on whichever lane happened to be installed.
MATRIX_READ_ROW_LIMIT_BY_VERSION: Mapping[str, int | None] = {
    "1.3.1": 2,
    "1.3.2": None,
    "1.4.0": None,
}


def matrix_read_row_limit(version: str) -> int | None:
    """Return the `DoubleRowMatrix` read row limit for an MPh version.

    ``None`` means the read is generalized. An unknown version raises rather than
    guessing, because guessing here would silently misreport a capability.
    """
    if version not in MATRIX_READ_ROW_LIMIT_BY_VERSION:
        raise AdapterError(
            "adapter_unavailable",
            f"no declared matrix read capability for MPh {version!r}",
            operation="session_identity",
        )
    return MATRIX_READ_ROW_LIMIT_BY_VERSION[version]


def installed_mph_version() -> str:
    """Return the installed MPh version, or an empty string if unavailable.

    Imported lazily and defensively so this stays usable with no MPh present.
    """
    try:
        import mph
    except Exception:
        return ""
    return str(getattr(mph, "__version__", "") or "")


# Operations the protocol represents.  Each maps to a plan step:
#   1 session/client and model identity
#   2 tag/list/node lookup
#   3 Java scalar/string/array/matrix conversion
#   4 typed property read/write with rollback checks
#   5 model load/save/evaluate and dataset/solution access
#   6 DNN and dbmodel:// adapters
OPERATIONS = (
    "session_open",
    "session_close",
    "session_identity",
    "model_identity",
    "node_lookup",
    "node_children",
    "convert_value",
    "property_read",
    "property_write",
    "java_typed_write",
    "dnn_feature_run",
    "dnn_feature_export",
    "model_load",
    "model_save",
    "model_evaluate",
    "dataset_access",
    "solution_access",
)

#: The Java write kinds the protocol represents.  This is a **closed** set: a
#: caller must name the exact Java type it intends, and there is deliberately no
#: generic "set this Java object" operation.  A narrow closed vocabulary is what
#: keeps the adapter from becoming the property escape hatch the plan forbids.
#:
#: ``string_entry`` is the odd one out: it is the alternating key/value form the
#: ClientAPI reference directs callers to for properties such as ``args``, and it
#: is applied through ``setEntry`` rather than ``set``.  ``string_matrix`` is the
#: nested ``[[Ljava.lang.String;`` form some other properties declare instead.
JAVA_WRITE_KINDS = (
    "string",
    "boolean",
    "int",
    "double",
    "string_array",
    "string_matrix",
    "int_array",
    "double_array",
    "string_entry",
)

#: The DNN feature lifecycle methods the protocol represents.  Closed for the
#: same reason as :data:`JAVA_WRITE_KINDS`: a free-form method name would be a
#: command channel, and the plan forbids that.
DNN_FEATURE_METHODS = (
    "run",
    "continueRun",
    "runTest",
    "discardData",
    "importData",
)

# Error taxonomy.  A backend translates its own exceptions into exactly one of
# these codes, so callers switch on a stable code instead of matching text.
ADAPTER_ERROR_CODES = (
    "adapter_unavailable",
    "session_already_open",
    "session_not_open",
    "node_not_found",
    "property_not_found",
    "property_type_mismatch",
    "conversion_not_representable",
    "model_load_failed",
    "model_save_failed",
    "evaluate_failed",
    "dataset_unavailable",
    "solution_unavailable",
    "backend_internal_error",
)


class AdapterError(RuntimeError):
    """One translated adapter failure with a stable reason code.

    Backends must raise this instead of letting a version-specific exception
    escape, and must never retry a mutation implicitly: a caller that wants a
    retry has to ask for one.
    """

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        operation: str = "",
        rejected: Sequence[str] = (),
    ) -> None:
        if reason_code not in ADAPTER_ERROR_CODES:
            raise ValueError(f"unknown adapter reason code: {reason_code}")
        super().__init__(message)
        self.reason_code = reason_code
        self.operation = operation
        #: Structured detail for failures that are themselves a probe result, such
        #: as every typed accessor rejecting a property read. Carried as data so a
        #: caller can report *why* something was unreadable without parsing the
        #: message; deliberately left out of ``as_dict`` so the error schema that
        #: receipts record does not change shape.
        self.rejected: tuple[str, ...] = tuple(rejected)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_name": PROTOCOL_SCHEMA_NAME,
            "schema_version": PROTOCOL_SCHEMA_VERSION,
            "operation": self.operation,
            "reason_code": self.reason_code,
            "message": str(self),
        }


@dataclass(frozen=True)
class SessionRequest:
    """Open one COMSOL session.

    Resource ownership is decided by the project, never by the backend, so the
    caller-declared cores/version/host/port are passed through unchanged and the
    backend may not substitute its own defaults.
    """

    cores: int | None = None
    version: str | None = None
    host: str | None = None
    port: int | None = None
    standalone: bool = True


@dataclass(frozen=True)
class SessionIdentity:
    """Identity of an open session, as observed rather than assumed."""

    mph_version: str
    comsol_version: str | None
    host: str | None
    port: int | None
    standalone: bool


@dataclass(frozen=True)
class ModelIdentity:
    """Identity of a loaded model.

    ``content_sha256`` is ``None`` when the backend cannot prove it, so an
    unknown identity is reported unknown instead of being inferred from a path.
    """

    name: str
    file: str | None
    content_sha256: str | None


@dataclass(frozen=True)
class NodeRef:
    """One addressable model node, resolved without reading its value."""

    tag: str
    path: Sequence[str] = ()
    node_type: str | None = None

    def child(self, tag: str) -> "NodeRef":
        return NodeRef(tag=tag, path=(*self.path, tag), node_type=None)


@dataclass(frozen=True)
class ConvertedValue:
    """An explicitly converted value and the form it was converted to.

    ``source_type`` is recorded so a parity check can tell whether two lanes
    agreed on the conversion rather than only on the final number.
    """

    value: Any
    form: str
    source_type: str


@dataclass(frozen=True)
class PropertyWritePlan:
    """One typed property write plus the rollback that must accompany it.

    The plan is validated before any write.  ``rollback`` carries the previous
    value so a failure-atomic caller can restore it; a write with no captured
    previous value is not representable, because it could not be undone.
    """

    node: NodeRef
    name: str
    value: ConvertedValue
    previous: ConvertedValue
    rollback_required: bool = True


@dataclass(frozen=True)
class PropertyWriteReceipt:
    """Outcome of one applied write, including whether rollback was needed."""

    node: NodeRef
    name: str
    applied: bool
    rolled_back: bool
    reason_code: str | None = None


@dataclass(frozen=True)
class ResolvedNode:
    """An already-resolved backend node handle.

    The licensed gates construct a model themselves and then hold raw Java
    feature objects, so the adapter must be able to operate on a handle that is
    already resolved rather than re-walking a path it cannot see.  Wrapping it in
    this type keeps that case explicit and keeps ``java_typed_write`` from
    accepting a bare ``Any``, which would be the generic escape hatch the plan
    forbids.
    """

    handle: Any
    label: str = "<resolved>"

    @property
    def path(self) -> tuple[str, ...]:
        return (self.label,)


@dataclass(frozen=True)
class JavaTypedWrite:
    """One property write with the exact Java type the caller intends.

    ``kind`` must come from :data:`JAVA_WRITE_KINDS`.  Naming the Java type
    explicitly is required because JPype cannot disambiguate ``set(String, int)``
    from ``set(String, boolean)`` and raises an ambiguous-overload error instead
    of choosing; the adapter therefore performs the wrapping rather than leaving
    it to each caller.
    """

    node: NodeRef | ResolvedNode
    name: str
    value: Any
    kind: str

    def __post_init__(self) -> None:
        if self.kind not in JAVA_WRITE_KINDS:
            raise AdapterError(
                "conversion_not_representable",
                f"unsupported Java write kind: {self.kind}",
                operation="java_typed_write",
            )


@dataclass(frozen=True)
class JavaTypedRead:
    """One property read, recording which typed accessor produced the value.

    The accessor name is kept because it decides the rendering: a JPype Java
    string is not a Python ``str`` but is still iterable, so rendering by
    duck-typing would silently split a value into its characters.
    """

    node: NodeRef | ResolvedNode
    name: str
    accessor: str
    value: Any
    #: Accessors that were tried and rejected, in the order they were tried. Kept
    #: on the result so a caller that reports "unreadable" can also report *why*,
    #: instead of the reason vanishing at the seam.
    rejected: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluationRequest:
    """One bounded model evaluation request."""

    expression: str
    unit: str | None = None
    dataset: str | None = None
    inner: Sequence[Any] = field(default_factory=tuple)
    outer: Sequence[Any] = field(default_factory=tuple)


@runtime_checkable
class ComsolAdapter(Protocol):
    """The only interface the rest of the project may use for COMSOL access.

    A backend implements this protocol.  Callers must not reach past it to
    ``mph.Client``, ``model.java``, or any version-specific helper: the whole
    point is that those stay behind this seam.
    """

    # -- identity, so a receipt can record which lane actually ran -----------
    @property
    def lane(self) -> str:
        """The MPh lane this backend targets, for example ``1.3.1``."""
        ...

    @property
    def backend_name(self) -> str:
        """A stable backend identifier recorded in receipts."""
        ...

    # -- step 1: session/client and model identity ---------------------------
    def open_session(self, request: SessionRequest) -> SessionIdentity: ...

    def close_session(self) -> None: ...

    def session_identity(self) -> SessionIdentity | None: ...

    def load_model(self, path: str) -> ModelIdentity: ...

    def save_model(self, path: str) -> ModelIdentity: ...

    # -- step 2: tag/list/node lookup ---------------------------------------
    def find_node(self, path: Sequence[str]) -> NodeRef: ...

    def children(self, node: NodeRef) -> Sequence[NodeRef]: ...

    # -- step 3: explicit conversion ----------------------------------------
    def convert(self, value: Any, *, form: str, target: str) -> ConvertedValue: ...

    # -- step 4: typed property access with rollback ------------------------
    def read_property(self, node: NodeRef, name: str, *, form: str) -> ConvertedValue: ...

    def plan_write(self, node: NodeRef, name: str, value: ConvertedValue) -> PropertyWritePlan: ...

    def apply_write(self, plan: PropertyWritePlan) -> PropertyWriteReceipt: ...

    # -- step 5: evaluate and dataset/solution access -----------------------
    def evaluate(self, request: EvaluationRequest) -> ConvertedValue: ...

    def dataset_names(self) -> Sequence[str]: ...

    def solution_names(self) -> Sequence[str]: ...

    # -- step 6: typed Java property access and DNN feature operations -------
    def java_typed_write(self, request: JavaTypedWrite) -> None:
        """Write one property with an explicitly named Java type.

        This replaces per-caller JPype coercion: the adapter owns the wrapping,
        so no caller needs to import jpype or guess an overload.
        """
        ...

    def java_typed_read(self, node: NodeRef, name: str) -> JavaTypedRead:
        """Read one property through the typed accessors, recording which one won."""
        ...

    def run_feature(self, node: NodeRef, *, method: str) -> None:
        """Invoke one named lifecycle method on a resolved feature.

        ``method`` must come from :data:`DNN_FEATURE_METHODS`; there is no
        generic "call this method" form.
        """
        ...

    def export_feature(self, node: NodeRef, path: str) -> None:
        """Export one resolved feature to a path."""
        ...

    # -- step 2 continued: container tags and node lifecycle -----------------
    # Feature-driven callers (the DNN bridge) need to enumerate and create the
    # containers they operate on. That is ordinary tag/list/node work, so it
    # belongs to step 2 and reports the step-2 operations rather than adding a
    # new operation name to the frozen protocol surface.
    def attach_client(self, model: Any) -> None:
        """Adopt an already-created client that the caller owns.

        MPh forbids a second client in one process, so a caller that already
        built one hands it over instead of having the adapter open another.
        Ownership does not transfer: the adapter must not close or clear a
        client it merely adopted.
        """
        ...

    def container_tags(self, kind: str) -> Sequence[str]:
        """List the child tags of one enumerable top-level container.

        The set of containers is closed; an unknown ``kind`` is refused rather
        than guessed at.
        """
        ...

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
        ...

    def remove_node(self, kind: str, tag: str) -> None:
        """Remove one node of a declared kind."""
        ...

    def read_allowed_values(self, node: NodeRef, name: str) -> list[str] | None:
        """Read an enumerated property's allowed values, or ``None`` if absent."""
        ...

    def java_value_type(self, node: NodeRef, name: str) -> str | None:
        """Read a property's declared Java value type, or ``None`` if unreadable.

        Some ClientAPI properties accept a nested ``[[Ljava.lang.String;`` value
        and reject the alternating key/value form, while others do the opposite.
        The declared type is therefore *measured* through this read rather than
        assumed by the caller; an unreadable type is reported as unknown.
        """
        ...


def operation_is_known(operation: str) -> bool:
    return operation in OPERATIONS


def lane_is_supported(lane: str) -> bool:
    """Whether a lane may be selected as the *reference* lane.

    A non-reference lane is reachable only through an explicit isolated backend
    selection; this function deliberately answers about the supported set rather
    than about what happens to be importable.
    """
    return lane in SUPPORTED_MPH_LANES


def describe_protocol() -> Mapping[str, Any]:
    """Return the protocol's declared surface for receipts and tests."""
    return {
        "schema_name": PROTOCOL_SCHEMA_NAME,
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "reference_lane": REFERENCE_MPH_LANE,
        "supported_lanes": list(SUPPORTED_MPH_LANES),
        "operations": list(OPERATIONS),
        "error_codes": list(ADAPTER_ERROR_CODES),
    }


__all__ = [
    "ADAPTER_ERROR_CODES",
    "OPERATIONS",
    "PROTOCOL_SCHEMA_NAME",
    "PROTOCOL_SCHEMA_VERSION",
    "REFERENCE_MPH_LANE",
    "SUPPORTED_MPH_LANES",
    "AdapterError",
    "ComsolAdapter",
    "ConvertedValue",
    "EvaluationRequest",
    "ModelIdentity",
    "NodeRef",
    "PropertyWritePlan",
    "PropertyWriteReceipt",
    "SessionIdentity",
    "SessionRequest",
    "describe_protocol",
    "installed_mph_version",
    "lane_is_supported",
    "matrix_read_row_limit",
    "operation_is_known",
]
