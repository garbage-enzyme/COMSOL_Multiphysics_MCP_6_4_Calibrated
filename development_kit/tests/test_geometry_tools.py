"""Solver-free geometry tool input-contract tests."""

from comsol_mcp.tools.geometry import add_difference_feature


def test_subtract_rejects_non_iterable_without_type_error():
    # None is not a valid tag sequence: the caller gets the documented
    # structured error, never a raw TypeError through the tool wrapper.
    result = add_difference_feature(None, None, None)

    assert result == {"success": False, "error": "difference inputs must be nonempty tags"}
