"""Licensed ClientAPI bridge for the typed surrogate DNN adapter.

This module is the licensed boundary: it touches the COMSOL Java API.  It must
never be imported by ordinary discovery, validation, or preview code paths, and
it exposes no generic property setter.

S4A migration (plan step 6)
---------------------------

Node resolution, Java type wrapping, and typed property reads previously lived in
this module, duplicating what the project adapter now owns, and it reached
``model.java`` directly.  Those responsibilities have moved behind
:class:`comsol_mcp.adapter.protocol.ComsolAdapter`:

* node lookup goes through ``find_node`` with an explicit kind-prefixed path;
* Java scalar/array wrapping goes through ``java_typed_write``, with the kind
  drawn from the adapter's closed ``JAVA_WRITE_KINDS`` vocabulary;
* typed reads go through ``java_typed_read``, which reports the accessor used;
* feature lifecycle calls go through ``run_feature`` / ``export_feature``, whose
  method names come from the adapter's closed ``DNN_FEATURE_METHODS`` vocabulary.

This module no longer imports ``jpype`` and no longer reads ``model.java``.  It
still accepts a raw model because existing licensed gates construct it that way;
that model is used only to build the adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from comsol_mcp.adapter import (
    DNN_FEATURE_METHODS,
    AdapterError,
    ComsolAdapter,
    JavaTypedWrite,
    NodeRef,
    ResolvedNode,
    make_backend,
)
from comsol_mcp.surrogate.dnn_adapter import DNN_FUNCTION_TYPE

# --------------------------------------------------------------------------
# ClientAPI backend
# --------------------------------------------------------------------------


def _handle(value: Any) -> Any:
    """Present a caller-supplied feature handle to the adapter.

    The licensed gates resolve features themselves and pass the raw Java object,
    so a handle that is not already an adapter reference is wrapped in
    :class:`ResolvedNode`. That keeps every existing call site working while
    still routing the operation through the adapter instead of touching Java
    here.
    """
    if isinstance(value, (NodeRef, ResolvedNode)):
        return value
    return ResolvedNode(handle=value, label=getattr(value, "__class__", type(value)).__name__)


class ClientapiSurrogateDnnBackend:
    """Adapter-backed bridge for one already loaded derived COMSOL model.

    Every Java interaction is delegated to a
    :class:`comsol_mcp.adapter.protocol.ComsolAdapter`, so this class contains no
    Java typing rules of its own and cannot drift from the rest of the project.
    Passing ``adapter`` explicitly is supported so a licensed gate or a test can
    inject a fake.

    The owning ``client`` must be supplied by the caller. MPh 1.3.1 forbids a
    second client in one process, and ``mph.Model`` exposes no back-reference to
    its owner, so the adapter cannot discover it: a caller that already holds a
    client hands that exact object over. Measured defect this fixes: the first
    migration tried to read the client off the model and every licensed gate
    failed before DNN configuration with
    ``the supplied object does not expose an MPh client``.
    """

    def __init__(
        self,
        model: Any = None,
        *,
        client: Any = None,
        adapter: ComsolAdapter | None = None,
    ):
        if adapter is None:
            if model is None:
                raise AdapterError(
                    "adapter_unavailable",
                    "a model or an adapter is required",
                    operation="session_open",
                )
            adapter = make_backend()
        if model is not None:
            # The client already exists in the licensed gates, so the adapter is
            # given that client rather than opening a second one, which MPh
            # forbids in a single process. Ownership stays with the caller.
            if client is None:
                raise AdapterError(
                    "adapter_unavailable",
                    "a client that owns the model is required; MPh 1.3.1 Model "
                    "exposes no client back-reference",
                    operation="session_open",
                )
            adapter.attach_client(model, client=client)
        self.adapter: ComsolAdapter = adapter
        self.model = model
        self.client = client

    # -- node resolution -----------------------------------------------------
    def study_tags(self) -> list[str]:
        return self._tags("study")

    def func_tags(self) -> list[str]:
        return self._tags("function")

    def _tags(self, kind: str) -> list[str]:
        """List the tags of a container the adapter can enumerate.

        The adapter enumerates only kinds with a well-defined listing, so an
        unsupported container is refused with a stable code rather than guessed.
        """
        return [str(tag) for tag in self.adapter.container_tags(kind)]

    def create_study(self, tag: str) -> NodeRef:
        return self.adapter.create_node("study", tag)

    def create_dnn_function(self, tag: str) -> NodeRef:
        return self.adapter.create_node("function", tag, feature_type=DNN_FUNCTION_TYPE)

    def create_study_step(self, study_tag: str, step_tag: str, step_type: str) -> NodeRef:
        return self.adapter.create_node(
            "study_step", step_tag, parent_tag=study_tag, feature_type=step_type
        )

    def get_study_step(self, study_tag: str, step_tag: str) -> NodeRef:
        return self.adapter.find_node(("study", study_tag, step_tag))

    def get_dnn_function(self, tag: str) -> NodeRef:
        # The DNN function feature tag inside the ``func`` container.
        return self.adapter.find_node(("function", tag))

    def remove_study(self, tag: str) -> None:
        self.adapter.remove_node("study", tag)

    def remove_function(self, tag: str) -> None:
        self.adapter.remove_node("function", tag)

    # -- typed reads and writes ---------------------------------------------
    def write_scalar(self, feature: Any, name: str, value: Any, kind: str) -> None:
        self.adapter.java_typed_write(
            JavaTypedWrite(node=_handle(feature), name=name, value=value, kind=kind)
        )

    def write_entries(self, feature: Any, name: str, entries: Mapping[str, str]) -> None:
        """Write an alternating key/value property through the adapter.

        The ClientAPI reference documents ``args`` and ``colscale`` as
        alternating arrays and explicitly directs callers to ``setEntry``, which
        is the adapter's ``string_entry`` kind.
        """
        self.adapter.java_typed_write(
            JavaTypedWrite(
                node=_handle(feature), name=name, value=dict(entries), kind="string_entry"
            )
        )

    def write_string_map(self, feature: Any, name: str, entries: Mapping[str, str]) -> None:
        """Write a keyed string map (for example ``globaldnnfunction``).

        COMSOL documents these properties as a "string array with keys 1, 2, 3,
        ...", and the accepted form is discovered from the property's own declared
        value type rather than assumed: some properties declare the nested
        ``[[Ljava.lang.String;`` form and reject the flat alternating array, while
        others do the reverse.  The declared type is measured through the adapter,
        which also performs the Java wrapping, so this module still owns no
        JPype typing rule of its own.
        """
        declared = self.adapter.java_value_type(_handle(feature), name)
        if declared is not None and "[[Ljava.lang.String;" in declared:
            keys = [str(key) for key in entries]
            values = [str(item) for item in entries.values()]
            self.adapter.java_typed_write(
                JavaTypedWrite(
                    node=_handle(feature), name=name, value=[keys, values], kind="string_matrix"
                )
            )
            return
        # Fall back to the alternating key/value form, which is what the
        # documented "array with keys 1, 2, 3, ..." describes.
        flat: list[str] = []
        for key, value in entries.items():
            flat.extend([str(key), str(value)])
        self.adapter.java_typed_write(
            JavaTypedWrite(node=_handle(feature), name=name, value=flat, kind="string_array")
        )

    def bind_data_source(self, feature: Any, path: str) -> dict[str, Any]:
        """Bind and import one data file so the column keys become known.

        Declaring ``filename`` alone does not populate the data columns; COMSOL
        refuses ``args`` until the columns exist.  This sets the source, imports
        the data, and returns the column keys COMSOL actually derived, so ``args``
        is built from observed columns rather than assumed ones.

        A repeated import raises "Unsupported function operation" even though the
        columns are already present, so an import failure is tolerated when
        COMSOL still reports column keys.
        """
        self.write_scalar(feature, "source", "file", "string")
        self.write_scalar(feature, "filename", str(path), "string")
        import_error = None
        try:
            self.adapter.run_feature(_handle(feature), method="importData")
        except AdapterError as exc:
            import_error = f"{exc.reason_code}: {exc}"
        columns: list[str] = []
        column_probe_errors: list[str] = []
        for name in ("columnKeys", "fileheaders", "columnHeaders"):
            try:
                # Explicitly the array form. The pre-migration code called
                # ``feature.getStringArray(name)`` and the probing read would
                # let ``getString`` answer instead, turning a comma-joined
                # header such as "col1, col2, col3" into 16 one-character
                # "columns"; that surfaced as
                # "data file exposes 16 columns but the schema declares 3".
                values = self.adapter.java_string_array(_handle(feature), name)
            except AdapterError as exc:
                # A missing key is expected while probing, but the failure is
                # recorded so a genuine accessor break is not silently hidden.
                column_probe_errors.append(f"{name}: {exc.reason_code}")
                continue
            if values:
                columns = values
                break
        if columns:
            import_error = None
        return {
            "import_error": import_error,
            "column_keys": columns,
            "column_probe_errors": column_probe_errors,
        }

    def read_property(self, feature: Any, name: str) -> dict[str, Any]:
        """Read one property through the adapter's typed accessors.

        The accessor the adapter reports decides the rendering, because a JPype
        Java string is not a Python ``str`` but is still iterable, so rendering by
        duck-typing would silently split a value into its characters.  The
        rejected accessors are carried through from the adapter so an unreadable
        property still reports *why*, which is what the licensed evidence records.
        """
        try:
            read = self.adapter.java_typed_read(_handle(feature), name)
        except AdapterError as exc:
            # The reason text and the rejected-accessor list are the ones the
            # licensed evidence already records, so the migration does not change
            # what an unreadable property reports.
            return {
                "readable": False,
                "reason": "no typed accessor accepted the property",
                "rejected_accessors": list(exc.rejected),
            }
        value = read.value
        if read.accessor in {"getString", "getBoolean", "getInt", "getDouble"}:
            return {"readable": True, "accessor": read.accessor, "value": str(value)}
        try:
            rendered: Any = [str(item) for item in list(value)]
        except Exception:
            rendered = str(value)
        return {"readable": True, "accessor": read.accessor, "value": rendered}

    def read_allowed_values(self, feature: Any, name: str) -> list[str] | None:
        return self.adapter.read_allowed_values(_handle(feature), name)

    # -- lifecycle -----------------------------------------------------------
    def _run(self, feature: Any, method: str) -> None:
        if method not in DNN_FEATURE_METHODS:
            raise AdapterError(
                "conversion_not_representable",
                f"unsupported feature method: {method}",
                operation="dnn_feature_run",
            )
        self.adapter.run_feature(_handle(feature), method=method)

    def train(self, feature: Any) -> None:
        self._run(feature, "run")

    def continue_training(self, feature: Any) -> None:
        self._run(feature, "continueRun")

    def run_test(self, feature: Any) -> None:
        self._run(feature, "runTest")

    def discard_data(self, feature: Any) -> None:
        self._run(feature, "discardData")

    def export_onnx(self, feature: Any, path: str) -> None:
        self.adapter.export_feature(_handle(feature), str(path))


__all__ = ["ClientapiSurrogateDnnBackend"]
