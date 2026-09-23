"""Isolated MPh 1.4.0 COMSOL adapter backend (S4A step 3).

Why a separate class rather than a flag
---------------------------------------

MPh 1.4.0 changes behaviour inside method bodies while keeping the public method
surface identical to 1.3.1, so a version flag would hide the difference exactly
where it matters.  A separate backend makes the lane explicit, records it in
every receipt, and lets parity tests compare two objects rather than one object
under two settings.

The measured behavioural differences this backend exists for
-----------------------------------------------------------

See ``D:\\mcp_tests\\a75s4inv\\mph_131_140_differences.md``.

1. ``Client.load()`` accepts ``dbmodel://`` URIs on 1.4.0; on 1.3.1 the argument
   is unconditionally resolved as a filesystem path.  The 1.4 backend therefore
   routes a ``dbmodel://`` string to ``load`` unchanged, and the reference
   backend refuses it rather than passing it to ``Path``.
2. ``DoubleRowMatrix`` **read** conversion is general on 1.4.0 but raises
   ``TypeError`` for more than two rows on 1.3.1.  This backend reports that
   difference through a stable reason code instead of padding or truncating rows
   to force equality.  The **write** path refuses more than two rows on both
   lanes, so it needs no lane-specific handling.
3. Windows subprocess creation flags are centralized in 1.4.0.  The project still
   owns its process contract, so this backend inherits the same process
   discipline and never relies on MPh's flags.

Lane isolation
--------------

Selecting this backend is explicit (``make_backend(lane="1.4.0")``).  It must not
become the default, and constructing it does not import MPh: the import stays
inside ``open_session``.
"""

from __future__ import annotations

from typing import Any

from comsol_mcp.adapter.mph_backend import MphBackendBase
from comsol_mcp.adapter.protocol import (
    MATRIX_READ_ROW_LIMIT_BY_VERSION,
    AdapterError,
    ConvertedValue,
    ModelIdentity,
    NodeRef,
    matrix_read_row_limit,
)

#: The lane this backend targets.  Declared as a module constant so a receipt can
#: record it without constructing the backend.
MPH_1_4_LANE = "1.4.0"

#: A ``DoubleRowMatrix`` read wider than this cannot be converted on the 1.3.1
#: reference lane.  Recorded here so the difference is one named constant rather
#: than a repeated literal.
REFERENCE_DOUBLE_ROW_MATRIX_ROW_LIMIT = 2

#: The exact refusal text MPh 1.3.1 raises when reading a wider matrix.  Matching
#: the message is unavoidable because MPh raises a bare ``TypeError`` with no
#: code, and the message is quoted from the measured source so a change to it
#: cannot pass silently.
REFERENCE_MATRIX_READ_REFUSAL = "Cannot convert double-row matrix with more than two rows."


class Mph14Backend(MphBackendBase):
    """The isolated MPh 1.4.0 lane.

    Inherits conversion, rollback, and error translation from
    :class:`MphBackendBase` so the two lanes cannot diverge on the rules the plan
    requires the project to own.
    """

    lane = MPH_1_4_LANE
    backend_name = "mph-1.4.0"

    def load_model(self, path: str) -> ModelIdentity:
        """Load a model, permitting a ``dbmodel://`` URI on this lane.

        On 1.4.0 ``Client.load`` recognises the URI and passes it to the COMSOL
        Model Manager.  This project still performs no live Model Manager
        operation in 0.7.5, so a URI is refused with a stable code before it
        reaches COMSOL; the branch exists to make the capability and its
        deliberate restriction both explicit.
        """
        if isinstance(path, str) and path.startswith("dbmodel://"):
            raise AdapterError(
                "model_load_failed",
                "live dbmodel:// loading is deferred to alpha7.6; "
                "0.7.5 validates the URI as syntax and evidence only",
                operation="model_load",
            )
        return super().load_model(path)

    def _collapse_matrix_refusal(self, exc: AdapterError) -> AdapterError:
        """Collapse the reference lane's wide-matrix refusal to a stable code.

        Extracted so the translation is one testable step rather than a branch
        buried inside ``read_property``. The write path is unaffected: it refuses
        more than two rows on both lanes.
        """
        if REFERENCE_MATRIX_READ_REFUSAL in str(exc):
            return AdapterError(
                "conversion_not_representable",
                "this lane restricts DoubleRowMatrix reads to "
                f"{REFERENCE_DOUBLE_ROW_MATRIX_ROW_LIMIT} rows",
                operation="property_read",
            )
        return exc

    def read_property(self, node: NodeRef, name: str, *, form: str) -> ConvertedValue:
        """Read a property, translating this lane's matrix read limitation.

        Reading a ``DoubleRowMatrix`` with more than two rows succeeds on 1.4.0 but
        raises ``TypeError`` on 1.3.1, whose message is quoted in
        :data:`REFERENCE_MATRIX_READ_REFUSAL` (see module docstring). The write
        path refuses more than two rows on **both** lanes. Rather than hiding the
        read difference, this lane reports it as a stable code, so a parity
        comparison reports a documented difference instead of an unexplained
        failure.
        """
        try:
            return super().read_property(node, name, form=form)
        except AdapterError as exc:
            raise self._collapse_matrix_refusal(exc) from exc

    def matrix_row_capacity(self) -> int | None:
        """Return this lane's ``DoubleRowMatrix`` read limit, which is none.

        1.4.0 generalizes the read conversion. Note that 1.3.2 generalizes it too,
        so this is not by itself what distinguishes 1.4 from every 1.3.x lane; the
        `dbmodel://` load capability does. The value comes from the shared
        version-keyed table so a lane cannot invent its own answer.
        """
        return matrix_read_row_limit(self.lane)

    def supports_dbmodel_uri(self) -> bool:
        """Whether the underlying lane can load a ``dbmodel://`` URI at all."""
        return True


def lane_capabilities(lane: str) -> dict[str, Any]:
    """Return the capability difference between lanes as data.

    Kept beside the backend so a receipt or parity report can record why two lanes
    are not interchangeable, without constructing either one. The matrix limit is
    read from the shared version-keyed table, so 1.3.2 is reported as generalized
    (it is) instead of being described as 1.3.1 was.
    """
    supported = set(MATRIX_READ_ROW_LIMIT_BY_VERSION)
    if lane not in supported:
        raise AdapterError(
            "adapter_unavailable",
            f"no capability record for MPh lane {lane!r}",
            operation="session_identity",
        )
    return {
        "lane": lane,
        # Only 1.4.0 recognizes a dbmodel:// URI in Client.load; 1.3.x resolves
        # the argument as a filesystem path in every release examined.
        "supports_dbmodel_uri": lane == MPH_1_4_LANE,
        "double_row_matrix_row_limit": matrix_read_row_limit(lane),
    }


__all__ = [
    "MPH_1_4_LANE",
    "REFERENCE_DOUBLE_ROW_MATRIX_ROW_LIMIT",
    "REFERENCE_MATRIX_READ_REFUSAL",
    "Mph14Backend",
    "lane_capabilities",
]
