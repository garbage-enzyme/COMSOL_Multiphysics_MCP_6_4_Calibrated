"""Licensed ClientAPI bridge for the typed surrogate DNN adapter.

This module is the licensed boundary: it imports jpype and touches the COMSOL
Java API.  It must never be imported by ordinary discovery, validation, or
preview code paths, and it exposes no generic property setter.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from comsol_mcp.surrogate.dnn_adapter import DNN_FUNCTION_TYPE


# --------------------------------------------------------------------------
# ClientAPI backend
# --------------------------------------------------------------------------


def _container_get(container: Any, tag: str) -> Any:
    """Resolve one child tag across the ClientAPI accessor variants."""
    try:
        return container.get(tag)
    except Exception:
        return container(tag)


class ClientapiSurrogateDnnBackend:
    """ClientAPI bridge for one already loaded derived COMSOL model.

    This is the licensed boundary.  It performs only the typed operations the
    adapter requests and never exposes a generic property setter to callers.
    """

    def __init__(self, model: Any):
        self.model = model

    @staticmethod
    def _tags(container: Any) -> list[str]:
        return [str(item) for item in list(container.tags())]

    def study_tags(self) -> list[str]:
        return self._tags(self.model.java.study())

    def func_tags(self) -> list[str]:
        return self._tags(self.model.java.func())

    def create_study(self, tag: str) -> Any:
        return self.model.java.study().create(tag)

    def create_study_step(self, study_tag: str, step_tag: str, step_type: str) -> Any:
        study = self.model.java.study(study_tag)
        return study.feature().create(step_tag, step_type)

    def create_dnn_function(self, tag: str) -> Any:
        return self.model.java.func().create(tag, DNN_FUNCTION_TYPE)

    def get_study_step(self, study_tag: str, step_tag: str) -> Any:
        study = self.model.java.study(study_tag)
        return study.feature(step_tag)

    def get_dnn_function(self, tag: str) -> Any:
        # The ClientAPI container is overloaded: the zero-argument form returns
        # the feature list (used by tags()), while the tag form returns the
        # feature itself.  The list object is not callable, so the tag form must
        # be used directly on the container accessor.
        return self.model.java.func(tag)

    def remove_study(self, tag: str) -> None:
        self.model.java.study().remove(tag)

    def remove_function(self, tag: str) -> None:
        self.model.java.func().remove(tag)

    @staticmethod
    def _coerce(value: Any, kind: str) -> Any:
        """Convert a JSON value to the exact Java type the property expects.

        JPype cannot disambiguate ``set(String, int)`` from
        ``set(String, boolean)`` when handed a Python ``int``; it raises an
        ambiguous-overload error rather than choosing.  Numeric and boolean
        scalars are therefore wrapped in their explicit Java types.
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
        if kind == "int_array":
            return jpype.JArray(jpype.JInt)([int(item) for item in value])
        if kind == "double_array":
            return jpype.JArray(jpype.JDouble)([float(item) for item in value])
        raise ValueError(f"unsupported write kind: {kind}")

    def write_scalar(self, feature: Any, name: str, value: Any, kind: str) -> None:
        feature.set(name, self._coerce(value, kind))

    def write_entries(self, feature: Any, name: str, entries: Mapping[str, str]) -> None:
        """Write an alternating key/value property through ``setEntry``.

        The ClientAPI reference documents ``args`` and ``colscale`` as
        alternating arrays and explicitly directs callers to ``setEntry``,
        ``getEntryKeys``, and ``getEntryKeyIndex``.  Passing a nested Java
        array to ``set`` for these properties fails with ``Unable to convert``.
        """
        for key, value in entries.items():
            feature.setEntry(name, str(key), str(value))

    def write_string_map(self, feature: Any, name: str, entries: Mapping[str, str]) -> None:
        """Write a keyed string map (for example ``globaldnnfunction``).

        COMSOL documents these properties as a "string array with keys 1, 2, 3,
        ...".  The accepted form is discovered from the property's own declared
        value type rather than assumed, because the nested-array form used by
        some other properties is rejected here with ``Unable to convert``.
        """
        import jpype

        value_type = None
        try:
            value_type = str(feature.getValueType(name))
        except Exception:
            value_type = None
        if value_type is not None and "[[Ljava.lang.String;" in value_type:
            keys = jpype.JArray(jpype.JString)([str(key) for key in entries])
            values = jpype.JArray(jpype.JString)([str(item) for item in entries.values()])
            feature.set(name, jpype.JArray(jpype.JString)([keys, values]))
            return
        # Fall back to the alternating key/value form, which is what the
        # documented "array with keys 1, 2, 3, ..." describes.
        flat: list[str] = []
        for key, value in entries.items():
            flat.extend([str(key), str(value)])
        feature.set(name, jpype.JArray(jpype.JString)(flat))

    def bind_data_source(self, feature: Any, path: str) -> dict[str, Any]:
        """Bind and import one data file so the column keys become known.

        Declaring ``filename`` alone does not populate the data columns; COMSOL
        refuses ``args`` until the columns exist.  This method therefore sets the
        source, imports the data, and returns the column keys COMSOL actually
        derived from the file, so ``args`` is built from observed columns rather
        than assumed ones.

        A repeated import raises "Unsupported function operation" even though the
        columns are already present, so an import failure is tolerated when
        COMSOL still reports column keys.
        """
        self.write_scalar(feature, "source", "file", "string")
        self.write_scalar(feature, "filename", str(path), "string")
        import_error = None
        try:
            feature.importData()
        except Exception as exc:
            import_error = f"{type(exc).__name__}: {exc}"
        columns: list[str] = []
        for name in ("columnKeys", "fileheaders", "columnHeaders"):
            try:
                raw = feature.getStringArray(name)
            except Exception:
                continue
            values = [str(item) for item in list(raw)]
            if values:
                columns = values
                break
        if columns:
            import_error = None
        return {"import_error": import_error, "column_keys": columns}

    def read_property(self, feature: Any, name: str) -> dict[str, Any]:
        """Read one property through the typed accessors, recording the one used.

        The accessor name decides the rendering: scalar accessors yield a string
        and array/matrix accessors yield a list.  A JPype Java string is not a
        Python ``str`` but is still iterable, so rendering by duck-typing would
        silently split a value into its characters.
        """
        for accessor in (
            "getString",
            "getBoolean",
            "getInt",
            "getDouble",
            "getStringArray",
            "getStringMatrix",
            "getDoubleArray",
            "getDoubleMatrix",
        ):
            try:
                value = getattr(feature, accessor)(name)
            except Exception:
                continue
            if accessor in {"getString", "getBoolean", "getInt", "getDouble"}:
                return {"readable": True, "accessor": accessor, "value": str(value)}
            try:
                rendered: Any = [str(item) for item in list(value)]
            except Exception:
                rendered = str(value)
            return {"readable": True, "accessor": accessor, "value": rendered}
        return {"readable": False, "reason": "no typed accessor accepted the property"}

    def read_allowed_values(self, feature: Any, name: str) -> list[str] | None:
        try:
            raw = feature.getAllowedPropertyValues(name)
        except Exception:
            return None
        if raw is None:
            return None
        return [str(item) for item in list(raw)]

    def train(self, feature: Any) -> None:
        feature.run()

    def continue_training(self, feature: Any) -> None:
        feature.continueRun()

    def run_test(self, feature: Any) -> None:
        feature.runTest()

    def export_onnx(self, feature: Any, path: str) -> None:
        feature.export(str(path))

    def discard_data(self, feature: Any) -> None:
        feature.discardData()




__all__ = ["ClientapiSurrogateDnnBackend"]
