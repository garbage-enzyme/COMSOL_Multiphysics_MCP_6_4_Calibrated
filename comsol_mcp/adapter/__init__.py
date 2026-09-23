"""Project-owned COMSOL adapter seam (S4A).

Import from here rather than from the submodules so the public shape of the
adapter is one reviewable list.

Typical use::

    from comsol_mcp.adapter import describe_protocol, make_backend

    backend = make_backend()                 # the MPh 1.3.1 reference lane
    identity = backend.open_session(SessionRequest(cores=1))

A backend is the only object allowed to touch MPh or the Java bridge. Nothing
else in the project may import ``mph`` for operational work.
"""

from __future__ import annotations

from typing import Any

from comsol_mcp.adapter.conversion import (
    CONVERSION_FORMS,
    MAX_MATRIX_ELEMENTS,
    convert_explicitly,
    normalize_evaluation_result,
    unwrap_backend_value,
)
from comsol_mcp.adapter.protocol import (
    ADAPTER_ERROR_CODES,
    OPERATIONS,
    PROTOCOL_SCHEMA_NAME,
    PROTOCOL_SCHEMA_VERSION,
    REFERENCE_MPH_LANE,
    SUPPORTED_MPH_LANES,
    AdapterError,
    ComsolAdapter,
    ConvertedValue,
    EvaluationRequest,
    ModelIdentity,
    NodeRef,
    PropertyWritePlan,
    PropertyWriteReceipt,
    SessionIdentity,
    SessionRequest,
    describe_protocol,
    lane_is_supported,
    operation_is_known,
)

# Backends are resolved lazily so importing this package never imports MPh, never
# starts a JVM, and never acquires a solver lease. A module-level `import mph`
# here would make every consumer of the adapter a potential solver starter.
_BACKEND_FACTORIES: dict[str, str] = {
    REFERENCE_MPH_LANE: "comsol_mcp.adapter.mph_backend:MphReferenceBackend",
    "1.4.0": "comsol_mcp.adapter.mph14_backend:Mph14Backend",
}


def available_lanes() -> tuple[str, ...]:
    """Return the lanes this build can construct."""
    return tuple(sorted(_BACKEND_FACTORIES))


def make_backend(lane: str | None = None, **kwargs: Any) -> ComsolAdapter:
    """Construct the backend for ``lane``, defaulting to the reference lane.

    The default is the reference lane on purpose: a second lane must be selected
    explicitly so it can never become the implicit production path.
    """
    import importlib

    selected = lane or REFERENCE_MPH_LANE
    target = _BACKEND_FACTORIES.get(selected)
    if target is None:
        raise AdapterError(
            "adapter_unavailable",
            f"no adapter backend is registered for MPh lane {selected!r}",
            operation="session_open",
        )
    module_name, attribute = target.split(":")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute)
    backend: ComsolAdapter = factory(**kwargs)
    return backend


__all__ = [
    "ADAPTER_ERROR_CODES",
    "CONVERSION_FORMS",
    "MAX_MATRIX_ELEMENTS",
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
    "available_lanes",
    "convert_explicitly",
    "describe_protocol",
    "lane_is_supported",
    "make_backend",
    "normalize_evaluation_result",
    "operation_is_known",
    "unwrap_backend_value",
]
