"""Explicit, version-neutral value conversion for the COMSOL adapter (S4A step 3).

The plan forbids implicit coercion: two MPh lanes must not be made to look alike
by silently converting a string to a number or by letting a backend choose a
shape.  Every conversion here is therefore explicit, total (it either returns a
converted value or raises), and records the form it produced.

Measured basis
--------------

MPh 1.3.1 keeps its matrix conversion in ``model.py``; MPh 1.4.0 moved the matrix
datatype dispatch into ``node.py``, where ``DoubleRowMatrix`` is handled by
calling ``getDoubleMatrix`` and reshaping (see
``D:\\mcp_tests\\a75s4inv\\mph_131_140_differences.md``).  Neither lane's helper
is therefore a contract.  This module owns the conversion so the project does
not inherit either lane's private decision.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from comsol_mcp.adapter.protocol import AdapterError, ConvertedValue

# The primitive forms the project represents explicitly.
SCALAR_FORMS = ("int", "float", "str", "bool")
MATRIX_FORMS = ("int_matrix", "float_matrix", "str_matrix", "bool_matrix")
CONVERSION_FORMS = (*SCALAR_FORMS, *MATRIX_FORMS)

# A matrix this project will convert.  A COMSOL matrix larger than this is
# refused rather than materialized, because the caller asked for a bounded
# conversion and an unbounded one could exhaust memory before any check ran.
MAX_MATRIX_ELEMENTS = 1_048_576


def _reject(message: str, *, operation: str = "convert_value") -> AdapterError:
    return AdapterError("conversion_not_representable", message, operation=operation)


def require_int(value: Any, *, field: str) -> int:
    """Return ``value`` as an int, refusing anything that is not exactly an int.

    ``bool`` is refused even though Python treats it as an ``int`` subclass: a
    boolean silently becoming ``1`` is precisely the implicit coercion the plan
    forbids.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise _reject(f"{field} must be an int, got {type(value).__name__}")
    return value


def require_float(value: Any, *, field: str) -> float:
    """Return ``value`` as a finite float.

    An ``int`` is accepted because widening to float is exact and intended; a
    string or ``None`` is not, and a non-finite value is refused because it
    cannot be written into a COMSOL property meaningfully.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _reject(f"{field} must be a number, got {type(value).__name__}")
    result = float(value)
    if not math.isfinite(result):
        raise _reject(f"{field} must be finite")
    return result


def require_str(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise _reject(f"{field} must be a str, got {type(value).__name__}")
    return value


def require_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise _reject(f"{field} must be a bool, got {type(value).__name__}")
    return value


def to_matrix_rows(value: Any, *, form: str) -> list[list[Any]]:
    """Normalize a matrix-like value into rows of scalars.

    Accepts a sequence of rows or a flat sequence, because COMSOL hands back
    either shape depending on the datatype and lane.  The returned shape is
    recorded by the caller rather than inferred later.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _reject(f"{form} must be a sequence of rows")
    rows: list[list[Any]] = []
    for index, row in enumerate(value):
        if isinstance(row, (str, bytes)) or not isinstance(row, Sequence):
            raise _reject(f"{form} row {index} must be a sequence")
        rows.append(list(row))
    if not rows:
        raise _reject(f"{form} must not be empty")
    width = len(rows[0])
    if width == 0:
        raise _reject(f"{form} rows must not be empty")
    for index, row in enumerate(rows):
        if len(row) != width:
            raise _reject(f"{form} row {index} is ragged")
    if len(rows) * width > MAX_MATRIX_ELEMENTS:
        raise _reject(f"{form} exceeds the bounded conversion limit")
    return rows


def convert_explicitly(value: Any, *, form: str) -> ConvertedValue:
    """Convert one value to an explicit form and report what was produced.

    The ``source_type`` field lets a parity check distinguish "both lanes agreed"
    from "both lanes coerced differently to the same number".
    """
    if form not in CONVERSION_FORMS:
        raise _reject(f"unsupported conversion form: {form}")
    source_type = type(value).__name__

    if form == "int":
        return ConvertedValue(require_int(value, field=form), form, source_type)
    if form == "float":
        return ConvertedValue(require_float(value, field=form), form, source_type)
    if form == "str":
        return ConvertedValue(require_str(value, field=form), form, source_type)
    if form == "bool":
        return ConvertedValue(require_bool(value, field=form), form, source_type)

    rows = to_matrix_rows(value, form=form)
    # Annotated explicitly: narrowing from the first branch would otherwise fix
    # the element type to int and reject the other three forms.
    converted: list[list[Any]]
    if form == "int_matrix":
        converted = [[require_int(cell, field=form) for cell in row] for row in rows]
    elif form == "float_matrix":
        converted = [[require_float(cell, field=form) for cell in row] for row in rows]
    elif form == "str_matrix":
        converted = [[require_str(cell, field=form) for cell in row] for row in rows]
    else:
        converted = [[require_bool(cell, field=form) for cell in row] for row in rows]
    return ConvertedValue(converted, form, source_type)


__all__ = [
    "CONVERSION_FORMS",
    "MATRIX_FORMS",
    "MAX_MATRIX_ELEMENTS",
    "SCALAR_FORMS",
    "convert_explicitly",
    "require_bool",
    "require_float",
    "require_int",
    "require_str",
    "to_matrix_rows",
]
