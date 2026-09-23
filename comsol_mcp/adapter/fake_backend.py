"""A deterministic fake COMSOL backend for adapter contract and parity tests.

Contract and parity tests must run with no COMSOL, no JVM, and no solver lease, so
they need a backend whose behaviour is fully specified.  This fake is that
backend: it records every call, returns scripted values, and can be told to fail
at any operation so the protocol's error translation and rollback paths are
exercised for real rather than mocked away.

It deliberately implements the *same* protocol as the MPh backends by inheriting
:class:`MphBackendBase` rules where they are lane-independent, and it never
imports ``mph``.
"""

from __future__ import annotations

from typing import Any, Sequence

from comsol_mcp.adapter.conversion import convert_explicitly
from comsol_mcp.adapter.protocol import (
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


class FakeComsolBackend:
    """A scripted backend that satisfies :class:`ComsolAdapter`.

    Parameters
    ----------
    lane:
        Reported lane identity, so a parity test can present two fakes as two
        lanes while keeping everything else identical.
    properties:
        Initial ``{tag: {name: value}}`` content.
    failures:
        ``{operation: reason_code}`` making that operation raise.  Used to prove
        error translation and rollback rather than to simulate success.
    """

    def __init__(
        self,
        *,
        lane: str = "fake",
        properties: dict[str, dict[str, Any]] | None = None,
        failures: dict[str, str] | None = None,
        mph_version: str = "0.0.0-fake",
        comsol_version: str | None = "6.4.0.293-fake",
    ) -> None:
        self.lane = lane
        self.backend_name = f"fake-{lane}"
        self._properties: dict[str, dict[str, Any]] = {
            tag: dict(values) for tag, values in (properties or {}).items()
        }
        self._failures = dict(failures or {})
        self._mph_version = mph_version
        self._comsol_version = comsol_version
        self._session: SessionIdentity | None = None
        self._model: ModelIdentity | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: Every property write that was actually applied, in order, so a test
        #: can prove a failed write was rolled back rather than merely reported.
        self.applied_writes: list[tuple[str, str, Any]] = []

    # -- test helpers --------------------------------------------------------
    def _record(self, operation: str, **detail: Any) -> None:
        self.calls.append((operation, detail))

    def _maybe_fail(self, operation: str) -> None:
        reason = self._failures.get(operation)
        if reason is not None:
            raise AdapterError(reason, f"scripted failure for {operation}", operation=operation)

    def operations_called(self) -> list[str]:
        return [operation for operation, _ in self.calls]

    # -- step 1: session/client and model identity ---------------------------
    def open_session(self, request: SessionRequest) -> SessionIdentity:
        self._record(
            "session_open",
            cores=request.cores,
            version=request.version,
            host=request.host,
            port=request.port,
        )
        self._maybe_fail("session_open")
        if self._session is not None:
            raise AdapterError(
                "session_already_open",
                "a session is already open",
                operation="session_open",
            )
        self._session = SessionIdentity(
            mph_version=self._mph_version,
            comsol_version=self._comsol_version,
            host=request.host,
            port=request.port,
            standalone=request.standalone,
        )
        return self._session

    def close_session(self) -> None:
        self._record("session_close")
        self._maybe_fail("session_close")
        self._session = None

    def session_identity(self) -> SessionIdentity | None:
        return self._session

    def load_model(self, path: str) -> ModelIdentity:
        self._record("model_load", path=path)
        self._maybe_fail("model_load")
        self._require_session("model_load")
        self._model = ModelIdentity(name="fake-model", file=path, content_sha256=None)
        return self._model

    def save_model(self, path: str) -> ModelIdentity:
        self._record("model_save", path=path)
        self._maybe_fail("model_save")
        identity = self._require_model("model_save")
        return ModelIdentity(name=identity.name, file=path, content_sha256=None)

    def _require_session(self, operation: str) -> SessionIdentity:
        if self._session is None:
            raise AdapterError("session_not_open", "no session is open", operation=operation)
        return self._session

    def _require_model(self, operation: str) -> ModelIdentity:
        self._require_session(operation)
        if self._model is None:
            raise AdapterError("model_load_failed", "no model is loaded", operation=operation)
        return self._model

    # -- step 2: tag/list/node lookup ---------------------------------------
    def find_node(self, path: Sequence[str]) -> NodeRef:
        self._record("node_lookup", path=list(path))
        self._maybe_fail("node_lookup")
        self._require_model("node_lookup")
        if not path:
            raise AdapterError(
                "node_not_found", "a node path must not be empty", operation="node_lookup"
            )
        if path[0] not in self._properties:
            raise AdapterError(
                "node_not_found",
                f"no node tagged {path[0]!r}",
                operation="node_lookup",
            )
        return NodeRef(tag=path[0], path=tuple(path), node_type="component")

    def children(self, node: NodeRef) -> Sequence[NodeRef]:
        self._record("node_children", tag=node.tag)
        self._maybe_fail("node_children")
        self._require_model("node_children")
        return [node.child(name) for name in sorted(self._properties.get(node.tag, {}))]

    # -- step 3: explicit conversion ----------------------------------------
    def convert(self, value: Any, *, form: str, target: str) -> ConvertedValue:
        self._record("convert_value", form=form, target=target)
        self._maybe_fail("convert_value")
        return convert_explicitly(value, form=form)

    # -- step 4: typed property access with rollback ------------------------
    def read_property(self, node: NodeRef, name: str, *, form: str) -> ConvertedValue:
        self._record("property_read", tag=node.tag, name=name, form=form)
        self._maybe_fail("property_read")
        self._require_model("property_read")
        values = self._properties.get(node.tag)
        if values is None or name not in values:
            raise AdapterError(
                "property_not_found",
                f"no property {name!r} on {node.tag}",
                operation="property_read",
            )
        try:
            return convert_explicitly(values[name], form=form)
        except AdapterError as exc:
            raise AdapterError(
                "property_type_mismatch",
                f"property {name!r} on {node.tag} is not a {form}",
                operation="property_read",
            ) from exc

    def plan_write(self, node: NodeRef, name: str, value: ConvertedValue) -> PropertyWritePlan:
        self._record("property_write", tag=node.tag, name=name, phase="plan")
        self._require_model("property_write")
        # Capturing the previous value is what makes the write reversible; a
        # missing property is therefore not writable at all.
        previous = self.read_property(node, name, form=value.form)
        return PropertyWritePlan(
            node=node, name=name, value=value, previous=previous, rollback_required=True
        )

    def apply_write(self, plan: PropertyWritePlan) -> PropertyWriteReceipt:
        self._record("property_write", tag=plan.node.tag, name=plan.name, phase="apply")
        if "property_write" in self._failures:
            # Emulate a backend that fails mid-write: the value must be restored.
            self._properties[plan.node.tag][plan.name] = plan.previous.value
            return PropertyWriteReceipt(
                node=plan.node,
                name=plan.name,
                applied=False,
                rolled_back=True,
                reason_code=self._failures["property_write"],
            )
        self._properties[plan.node.tag][plan.name] = plan.value.value
        self.applied_writes.append((plan.node.tag, plan.name, plan.value.value))
        return PropertyWriteReceipt(node=plan.node, name=plan.name, applied=True, rolled_back=False)

    def property_value(self, tag: str, name: str) -> Any:
        """Read the fake's raw stored value, for rollback assertions."""
        return self._properties[tag][name]

    # -- step 5: evaluate and dataset/solution access -----------------------
    def evaluate(self, request: EvaluationRequest) -> ConvertedValue:
        self._record("model_evaluate", expression=request.expression)
        self._maybe_fail("model_evaluate")
        self._require_model("model_evaluate")
        return convert_explicitly(0.0, form="float")

    def dataset_names(self) -> Sequence[str]:
        self._record("dataset_access")
        self._maybe_fail("dataset_access")
        self._require_model("dataset_access")
        return ["study1//solution1"]

    def solution_names(self) -> Sequence[str]:
        self._record("solution_access")
        self._maybe_fail("solution_access")
        self._require_model("solution_access")
        return ["sol1"]


__all__ = ["FakeComsolBackend"]
