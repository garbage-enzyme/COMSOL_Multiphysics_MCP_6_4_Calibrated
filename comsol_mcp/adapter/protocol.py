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

# The reference lane this project supports today.  A second lane is opt-in and
# never becomes the default implicitly.
REFERENCE_MPH_LANE = "1.3.1"
SUPPORTED_MPH_LANES = (REFERENCE_MPH_LANE,)

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
    "model_load",
    "model_save",
    "model_evaluate",
    "dataset_access",
    "solution_access",
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

    def __init__(self, reason_code: str, message: str, *, operation: str = "") -> None:
        if reason_code not in ADAPTER_ERROR_CODES:
            raise ValueError(f"unknown adapter reason code: {reason_code}")
        super().__init__(message)
        self.reason_code = reason_code
        self.operation = operation

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
    "lane_is_supported",
    "operation_is_known",
]
