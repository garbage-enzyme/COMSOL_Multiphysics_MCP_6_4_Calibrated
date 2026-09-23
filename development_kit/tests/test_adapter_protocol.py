"""Adapter protocol contract and MPh 1.3.1/1.4 parity tests (S4A).

Every test here is solver-free: no COMSOL, no JVM, no lease, no ``mph`` import.
The fake backend stands in for a real lane so the protocol's rules, error
translation, and rollback semantics are exercised as behaviour.

The parity tests compare two backends that differ **only** in declared lane and
in the two capabilities measured in
``D:\\mcp_tests\\a75s4inv\\mph_131_140_differences.md``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from comsol_mcp.adapter import (
    ADAPTER_ERROR_CODES,
    OPERATIONS,
    REFERENCE_MPH_LANE,
    SUPPORTED_MPH_LANES,
    AdapterError,
    ComsolAdapter,
    ConvertedValue,
    EvaluationRequest,
    PropertyWritePlan,
    SessionRequest,
    available_lanes,
    convert_explicitly,
    describe_protocol,
    lane_is_supported,
    make_backend,
    normalize_evaluation_result,
    operation_is_known,
    unwrap_backend_value,
)
from comsol_mcp.adapter.fake_backend import FakeComsolBackend
from comsol_mcp.adapter.mph14_backend import (
    MPH_1_4_LANE,
    REFERENCE_DOUBLE_ROW_MATRIX_ROW_LIMIT,
    REFERENCE_MATRIX_READ_REFUSAL,
    Mph14Backend,
    lane_capabilities,
)
from comsol_mcp.adapter.mph_backend import MphBackendBase, MphReferenceBackend

ROOT = Path(__file__).parents[2]


def _opened(**kwargs) -> FakeComsolBackend:
    """A fake backend with an open session and a loaded model."""
    backend = FakeComsolBackend(**kwargs)
    backend.open_session(SessionRequest(cores=1))
    backend.load_model("fake.mph")
    return backend


# ---------------------------------------------------------------------------
# Protocol surface
# ---------------------------------------------------------------------------


def test_protocol_declares_every_operation_and_error_code() -> None:
    described = describe_protocol()
    assert described["schema_name"] == "comsol_mcp.adapter.operation"
    assert described["schema_version"] == "1.0.0"
    assert described["reference_lane"] == REFERENCE_MPH_LANE
    assert described["supported_lanes"] == list(SUPPORTED_MPH_LANES)
    # All six plan steps must be representable.
    for step in (
        "session_open",
        "session_close",
        "session_identity",
        "model_identity",
        "node_lookup",
        "convert_value",
        "property_read",
        "property_write",
        "model_load",
        "model_save",
        "model_evaluate",
        "dataset_access",
        "solution_access",
    ):
        assert operation_is_known(step), step
    assert operation_is_known("nonsense") is False
    assert len(OPERATIONS) == len(set(OPERATIONS))


def test_the_reference_lane_is_the_only_supported_lane() -> None:
    """1.4 is constructible but is not a supported compatibility claim."""
    assert REFERENCE_MPH_LANE == "1.3.1"
    assert SUPPORTED_MPH_LANES == ("1.3.1",)
    assert lane_is_supported("1.3.1") is True
    assert lane_is_supported(MPH_1_4_LANE) is False
    # The lane is still selectable explicitly, so it can be tested in isolation.
    assert MPH_1_4_LANE in available_lanes()


def test_an_unknown_lane_is_refused_with_a_stable_code() -> None:
    with pytest.raises(AdapterError) as excinfo:
        make_backend(lane="9.9.9")
    assert excinfo.value.reason_code == "adapter_unavailable"
    assert excinfo.value.operation == "session_open"


def test_unknown_reason_codes_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="unknown adapter reason code"):
        AdapterError("not_a_real_code", "x")
    assert AdapterError("node_not_found", "x").as_dict()["reason_code"] == "node_not_found"
    assert "adapter_unavailable" in ADAPTER_ERROR_CODES


def test_both_real_backends_satisfy_the_protocol() -> None:
    assert isinstance(MphReferenceBackend(), ComsolAdapter)
    assert MphReferenceBackend().lane == "1.3.1"


def test_importing_the_adapter_does_not_import_mph_or_start_a_jvm() -> None:
    """The seam must never make every consumer a potential solver starter."""
    probe = (
        "import sys;"
        f"sys.path.insert(0, {str(ROOT)!r});"
        "import comsol_mcp.adapter as a;"
        "a.available_lanes();"
        "a.describe_protocol();"
        "print('mph' in sys.modules, 'jpype' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
        env={"PYTHONPATH": "", "PATH": ""},
    )
    assert completed.stdout.strip() == "False False", completed.stdout


# ---------------------------------------------------------------------------
# Explicit conversion (step 3) — the measured matrix difference lives here
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "form", "expected"),
    [
        (3, "int", 3),
        (2.5, "float", 2.5),
        ("a", "str", "a"),
        (True, "bool", True),
        ([[1, 2], [3, 4]], "int_matrix", [[1, 2], [3, 4]]),
        ([[1.5], [2.5]], "float_matrix", [[1.5], [2.5]]),
        ([["x", "y"]], "str_matrix", [["x", "y"]]),
        ([[True, False]], "bool_matrix", [[True, False]]),
    ],
)
def test_explicit_conversion_accepts_representable_values(value, form, expected) -> None:
    converted = convert_explicitly(value, form=form)
    assert converted.value == expected
    assert converted.form == form
    assert converted.source_type == type(value).__name__


@pytest.mark.parametrize(
    ("value", "form"),
    [
        (True, "int"),  # a bool must not silently become 1
        ("3", "int"),  # no string-to-number coercion
        (None, "float"),
        (float("nan"), "float"),
        (float("inf"), "float"),
        (1, "str"),
        ("x", "bool"),
        ([[1, 2], [3]], "int_matrix"),  # ragged
        ([], "int_matrix"),  # empty
    ],
)
def test_explicit_conversion_refuses_implicit_coercion(value, form) -> None:
    with pytest.raises(AdapterError) as excinfo:
        convert_explicitly(value, form=form)
    assert excinfo.value.reason_code == "conversion_not_representable"


def test_a_jvm_string_proxy_is_unwrapped_for_the_backend() -> None:
    """Licensed defect: COMSOL's `getString` returns a JPype proxy, not a `str`.

    Measured on COMSOL 6.4.0.293: the study step's `plist` read back as
    `jpype._jstring` / `java.lang.String`, so `isinstance(value, str)` was False
    and a strict conversion reported a spurious `property_type_mismatch` (see
    `D:\\mcp_tests\\a75s4type\\java_types.json`). Only backend-produced values are
    unwrapped; caller input stays strictly checked.
    """

    class JavaString:
        """Stands in for `jpype._jstring` without importing jpype."""

        __module__ = "jpype._jstring"

        def __init__(self, text: str) -> None:
            self._text = text

        def __str__(self) -> str:
            return self._text

    # Name the class exactly as JPype does so the unwrapper's type check matches.
    JavaString.__name__ = "java.lang.String"
    JavaString.__qualname__ = "java.lang.String"

    proxy = JavaString("")
    assert not isinstance(proxy, str)
    assert unwrap_backend_value(proxy) == ""
    assert isinstance(unwrap_backend_value(proxy), str)
    # The unwrapped value then satisfies a strict conversion.
    assert convert_explicitly(unwrap_backend_value(proxy), form="str").value == ""


def test_a_jvm_numeric_proxy_is_unwrapped() -> None:
    class JavaDouble:
        __module__ = "jpype._jdouble"

        def __init__(self, value: float) -> None:
            self._value = value

        def __float__(self) -> float:
            return self._value

    JavaDouble.__name__ = "java.lang.Double"
    JavaDouble.__qualname__ = "java.lang.Double"

    proxy = JavaDouble(2.5)
    unwrapped = unwrap_backend_value(proxy)
    assert unwrapped == 2.5
    assert convert_explicitly(unwrapped, form="float").value == 2.5


def test_unwrapping_does_not_weaken_caller_input_checks() -> None:
    """A plain string must still be refused for a numeric form."""
    assert unwrap_backend_value("3") == "3"
    with pytest.raises(AdapterError) as excinfo:
        convert_explicitly(unwrap_backend_value("3"), form="int")
    assert excinfo.value.reason_code == "conversion_not_representable"
    # And a Python bool must still not become an int.
    with pytest.raises(AdapterError):
        convert_explicitly(unwrap_backend_value(True), form="int")


def test_an_unrecognized_value_passes_through_unchanged() -> None:
    sentinel = object()
    assert unwrap_backend_value(sentinel) is sentinel
    assert unwrap_backend_value(7) == 7
    assert unwrap_backend_value(None) is None


def test_an_unsupported_form_is_refused() -> None:
    with pytest.raises(AdapterError, match="unsupported conversion form"):
        convert_explicitly(1, form="complex_matrix")


def test_an_oversized_matrix_is_refused_rather_than_materialized() -> None:
    from comsol_mcp.adapter.conversion import MAX_MATRIX_ELEMENTS

    side = int(MAX_MATRIX_ELEMENTS**0.5) + 2
    with pytest.raises(AdapterError, match="bounded conversion limit"):
        convert_explicitly([[1] * side for _ in range(side)], form="int_matrix")


def test_conversion_records_the_source_type_so_parity_can_be_checked() -> None:
    """Two lanes agreeing on a number but not on a type must be distinguishable."""
    from_int = convert_explicitly(1, form="float")
    from_float = convert_explicitly(1.0, form="float")
    assert from_int.value == from_float.value
    assert from_int.source_type != from_float.source_type


# ---------------------------------------------------------------------------
# Session and model identity (step 1)
# ---------------------------------------------------------------------------


def test_session_must_be_opened_before_use_and_closed_once() -> None:
    backend = FakeComsolBackend()
    with pytest.raises(AdapterError) as excinfo:
        backend.load_model("x.mph")
    assert excinfo.value.reason_code == "session_not_open"
    backend.open_session(SessionRequest(cores=1))
    with pytest.raises(AdapterError) as excinfo:
        backend.open_session(SessionRequest(cores=1))
    assert excinfo.value.reason_code == "session_already_open"
    backend.close_session()
    assert backend.session_identity() is None
    # Closing twice is a no-op, not a failure.
    backend.close_session()


def test_caller_declared_resources_are_passed_through_unchanged() -> None:
    """The backend must not substitute its own cores/version."""
    backend = FakeComsolBackend()
    backend.open_session(SessionRequest(cores=3, version="6.4", host="h", port=2036))
    recorded = dict(backend.calls)["session_open"]
    assert recorded == {"cores": 3, "version": "6.4", "host": "h", "port": 2036}


def test_an_unprovable_model_hash_is_reported_unknown() -> None:
    backend = _opened()
    identity = backend.load_model("x.mph")
    assert identity.content_sha256 is None
    assert identity.file == "x.mph"


# ---------------------------------------------------------------------------
# Node lookup (step 2)
# ---------------------------------------------------------------------------


def test_node_lookup_resolves_and_reports_a_missing_tag() -> None:
    backend = _opened(properties={"comp1": {"L": 0.1}})
    node = backend.find_node(["component", "comp1"])
    assert node.tag == "comp1"
    assert node.path == ("component", "comp1")
    assert [child.tag for child in backend.children(node)] == ["L"]
    with pytest.raises(AdapterError) as excinfo:
        backend.find_node(["component", "absent"])
    assert excinfo.value.reason_code == "node_not_found"
    with pytest.raises(AdapterError):
        backend.find_node([])


def test_model_identity_calls_methods_rather_than_stringifying_them() -> None:
    """Licensed defect: on MPh 1.3.1 `Model.name`/`Model.file` are METHODS.

    The first implementation used `getattr(model, "name")`, so a receipt recorded
    `<bound method Model.name of Model('s4a_probe')>` instead of the model name.
    The licensed gate caught it. This pins the fix.
    """

    class MethodStyleModel:
        """Mirrors MPh's `def name(self) -> str` / `def file(self) -> Path`."""

        def name(self) -> str:
            return "s4a_probe"

        def file(self) -> str:
            return "D:/mcp_tests/a75s4lic/s4a_probe.mph"

    identity = MphBackendBase()._model_identity(MethodStyleModel())  # noqa: SLF001
    assert identity.name == "s4a_probe"
    assert "bound method" not in identity.name
    assert identity.file == "D:/mcp_tests/a75s4lic/s4a_probe.mph"
    assert identity.content_sha256 is None


def test_model_identity_still_accepts_plain_attributes() -> None:
    """A fake or a future lane may expose attributes; both shapes must work."""

    class AttributeStyleModel:
        name = "plain"
        file = "plain.mph"

    identity = MphBackendBase()._model_identity(AttributeStyleModel())  # noqa: SLF001
    assert identity.name == "plain"
    assert identity.file == "plain.mph"


def test_model_identity_reports_unknown_instead_of_stringifying_nothing() -> None:
    class Empty:
        name = None
        file = None

    identity = MphBackendBase()._model_identity(Empty())  # noqa: SLF001
    assert identity.name == ""
    assert identity.file is None
    assert identity.content_sha256 is None


def test_an_unsupported_node_path_shape_is_refused() -> None:
    """The adapter addresses nodes by explicit kind-prefixed Java tags.

    Licensed probes showed MPh's high-level Node view is unusable on this
    localized COMSOL install: `components()` returns `组件 1` rather than `comp1`,
    `Node.exists()` is False even for a created component, and
    `Node.children()` raises `AttributeError: 'NoneType' object has no attribute
    'tags'`. The adapter therefore uses the Java accessor chain the repository's
    runtime jobs already use, and a caller states the node kind explicitly.

    A path shape the adapter does not represent is refused with a stable code
    rather than guessed at. Only the shape check is exercised, so no COMSOL is
    needed.
    """

    class StubBackend(MphBackendBase):
        def _require_model(self, operation: str) -> object:
            return object()

    backend = StubBackend()
    for path in (("nonsense", "a"), ("component",), ("component", "a", "b", "c", "d")):
        with pytest.raises(AdapterError) as excinfo:
            backend.find_node(path)
        assert excinfo.value.reason_code == "node_not_found"


def test_declared_node_path_shapes_match_the_validator() -> None:
    """The declared shape table and the validator must not drift apart.

    `node_path_shapes()` is what a caller or a receipt reads;
    `_shape_is_representable` is what actually gates resolution. If they disagree,
    a documented shape would be refused or an undocumented one accepted.

    The table's keys are descriptive labels (`("component","geom","feature")`),
    while the validator dispatches on the **first element** plus the arity, so the
    comparison is over `(path[0], arity)` pairs.
    """
    declared = {(kind[0], arity) for kind, arity in MphBackendBase.node_path_shapes().items()}
    assert declared == {
        ("component", 2),
        ("component", 3),
        ("component", 4),
        ("physics", 3),
        ("study", 2),
        ("study", 3),
        ("solution", 2),
        ("dataset", 2),
    }

    kinds = {kind for kind, _ in declared} | {"nonsense"}
    for kind in sorted(kinds):
        for arity in range(1, 6):
            probe = (kind, *(f"t{index}" for index in range(arity - 1)))
            expected = (kind, arity) in declared
            assert MphBackendBase._shape_is_representable(probe) is expected, probe  # noqa: SLF001


def test_every_representable_node_path_shape_reaches_the_java_accessor() -> None:
    """Each supported shape must resolve to the documented accessor chain.

    A recording stub stands in for `model.java`, so the dispatch is exercised
    without COMSOL and each shape's accessor route is pinned.
    """
    calls: list[str] = []

    class Recording:
        def component(self, tag: str) -> "Recording":
            calls.append(f"component({tag})")
            return self

        def geom(self, tag: str) -> "Recording":
            calls.append(f"geom({tag})")
            return self

        def feature(self, tag: str) -> "Recording":
            calls.append(f"feature({tag})")
            return self

        def physics(self, tag: str) -> "Recording":
            calls.append(f"physics({tag})")
            return self

        def study(self, tag: str) -> "Recording":
            calls.append(f"study({tag})")
            return self

        def sol(self, tag: str) -> "Recording":
            calls.append(f"sol({tag})")
            return self

        def result(self) -> "Recording":
            calls.append("result()")
            return self

        def dataset(self, tag: str) -> "Recording":
            calls.append(f"dataset({tag})")
            return self

    class StubModel:
        java = Recording()

    class StubBackend(MphBackendBase):
        def _require_model(self, operation: str) -> object:
            return StubModel()

    backend = StubBackend()
    expected = {
        ("component", "comp1"): ["component(comp1)"],
        ("component", "comp1", "geom1"): ["component(comp1)", "geom(geom1)"],
        ("component", "comp1", "geom1", "i1"): [
            "component(comp1)",
            "geom(geom1)",
            "feature(i1)",
        ],
        ("physics", "comp1", "c1"): ["component(comp1)", "physics(c1)"],
        ("study", "std1"): ["study(std1)"],
        ("study", "std1", "step1"): ["study(std1)", "feature(step1)"],
        ("solution", "sol1"): ["sol(sol1)"],
        ("dataset", "dset1"): ["result()", "dataset(dset1)"],
    }
    for path, expected_calls in expected.items():
        calls.clear()
        backend._java_node(path)  # noqa: SLF001
        assert calls == expected_calls, path


def test_child_nodes_extend_the_path_without_losing_the_parent() -> None:
    parent = _opened(properties={"c": {"a": 1}}).find_node(["c"])
    child = parent.child("a")
    assert child.path == ("c", "a")
    assert parent.path == ("c",)


# ---------------------------------------------------------------------------
# Typed property access and rollback (step 4)
# ---------------------------------------------------------------------------


def test_a_property_write_reports_the_applied_value() -> None:
    backend = _opened(properties={"comp1": {"L": 0.1}})
    node = backend.find_node(["comp1"])
    plan = backend.plan_write(node, "L", ConvertedValue(0.2, "float", "float"))
    assert plan.rollback_required is True
    assert plan.previous.value == 0.1
    receipt = backend.apply_write(plan)
    assert receipt.applied is True
    assert receipt.rolled_back is False
    assert backend.property_value("comp1", "L") == 0.2
    assert backend.applied_writes == [("comp1", "L", 0.2)]


def test_a_failed_write_is_rolled_back_and_never_claims_success() -> None:
    backend = _opened(
        properties={"comp1": {"L": 0.1}}, failures={"property_write": "backend_internal_error"}
    )
    node = backend.find_node(["comp1"])
    plan = backend.plan_write(node, "L", ConvertedValue(0.9, "float", "float"))
    receipt = backend.apply_write(plan)
    assert receipt.applied is False
    assert receipt.rolled_back is True
    assert receipt.reason_code == "backend_internal_error"
    # The original value survived: this is the failure-atomic guarantee.
    assert backend.property_value("comp1", "L") == 0.1
    assert backend.applied_writes == []


def test_a_write_to_a_missing_property_is_not_representable() -> None:
    """No previous value means no rollback, so the write must not be planned."""
    backend = _opened(properties={"comp1": {"L": 0.1}})
    node = backend.find_node(["comp1"])
    with pytest.raises(AdapterError) as excinfo:
        backend.plan_write(node, "absent", ConvertedValue(1.0, "float", "float"))
    assert excinfo.value.reason_code == "property_not_found"


def test_a_type_mismatch_is_reported_not_coerced() -> None:
    backend = _opened(properties={"comp1": {"tag": "a-string"}})
    node = backend.find_node(["comp1"])
    with pytest.raises(AdapterError) as excinfo:
        backend.read_property(node, "tag", form="float")
    assert excinfo.value.reason_code == "property_type_mismatch"


def test_a_write_plan_carries_the_previous_value_for_rollback() -> None:
    node = _opened(properties={"c": {"a": 1}}).find_node(["c"])
    plan = PropertyWritePlan(
        node=node,
        name="a",
        value=ConvertedValue(2, "int", "int"),
        previous=ConvertedValue(1, "int", "int"),
    )
    assert plan.rollback_required is True
    assert plan.previous.value == 1


# ---------------------------------------------------------------------------
# Evaluate and dataset/solution access (step 5)
# ---------------------------------------------------------------------------


def test_evaluation_and_dataset_access_return_declared_shapes() -> None:
    backend = _opened()
    result = backend.evaluate(EvaluationRequest(expression="1+1"))
    assert result.form == "float"
    assert backend.dataset_names() == ["study1//solution1"]
    assert backend.solution_names() == ["sol1"]


def test_a_real_numpy_style_scalar_array_is_normalized() -> None:
    """MPh returns a numpy array for a scalar expression, not a Python float.

    This is measured licensed behaviour, not an assumption: `model.evaluate("a1")`
    returned `array(1.5)` and `model.evaluate("1+1")` returned `array(2.)` on
    COMSOL 6.4.0.293 (see `D:\\mcp_tests\\a75s4ev\\eval_shape.json`). The
    normalizer must therefore duck-type a numpy-like object through `tolist`.
    """

    class NumpyLike:
        """Stands in for a 0-d numpy array without importing numpy."""

        def __init__(self, value: object) -> None:
            self._value = value

        def tolist(self) -> object:
            return self._value

    for raw, expected in ((1.5, 1.5), (2.0, 2.0), (3, 3)):
        converted = normalize_evaluation_result(NumpyLike(raw))
        assert converted.value == expected
        assert converted.form == ("int" if isinstance(raw, int) else "float")

    backend = _opened()
    backend.set_evaluation("a1", NumpyLike(1.5))
    assert backend.evaluate(EvaluationRequest(expression="a1")).value == 1.5


def test_an_empty_evaluation_result_is_refused_not_zeroed() -> None:
    """An undefined operator yields an empty array; substituting 0 would fabricate."""
    with pytest.raises(AdapterError, match="produced no value"):
        normalize_evaluation_result([])

    backend = _opened()
    backend.set_evaluation("undefined_op(1)", [])
    with pytest.raises(AdapterError) as excinfo:
        backend.evaluate(EvaluationRequest(expression="undefined_op(1)"))
    assert excinfo.value.reason_code == "conversion_not_representable"


def test_a_one_element_and_multi_element_result_are_distinguished() -> None:
    """One element is a scalar; several are one matrix row."""
    assert normalize_evaluation_result([7.0]).value == 7.0
    multi = normalize_evaluation_result([1.0, 2.0, 3.0])
    assert multi.form == "float_matrix"
    assert multi.value == [[1.0, 2.0, 3.0]]
    nested = normalize_evaluation_result([[1.0], [2.0]])
    assert nested.value == [[1.0], [2.0]]


def test_an_unrepresentable_evaluation_shape_is_refused() -> None:
    with pytest.raises(AdapterError, match="does not represent"):
        normalize_evaluation_result(object())


def test_evaluation_requires_a_loaded_model() -> None:
    backend = FakeComsolBackend()
    backend.open_session(SessionRequest())
    with pytest.raises(AdapterError) as excinfo:
        backend.evaluate(EvaluationRequest(expression="1+1"))
    assert excinfo.value.reason_code == "model_load_failed"


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def test_backend_exceptions_are_translated_to_one_stable_code() -> None:
    translator = MphBackendBase()
    cases = [
        (FileNotFoundError("gone"), "model_load_failed"),
        (KeyError("tag"), "node_not_found"),
        (AttributeError("attr"), "node_not_found"),
        (ValueError("bad"), "property_type_mismatch"),
        (TypeError("bad"), "property_type_mismatch"),
        (RuntimeError("boom"), "backend_internal_error"),
        (NotImplementedError("one client"), "session_already_open"),
    ]
    for exc, expected in cases:
        translated = translator._translate(exc, "op")  # noqa: SLF001
        assert translated.reason_code == expected, exc
        assert translated.operation == "op"
        assert expected in ADAPTER_ERROR_CODES


def test_translation_preserves_an_already_translated_error() -> None:
    original = AdapterError("node_not_found", "x", operation="op")
    assert MphBackendBase()._translate(original, "other") is original  # noqa: SLF001


# ---------------------------------------------------------------------------
# 1.3.1 / 1.4.0 behaviour parity (step 5's gate)
# ---------------------------------------------------------------------------

_PARITY_PROPERTIES = {"comp1": {"L": 0.1, "rows": 2}}
_PARITY_EQUATION = "1+1"


def _pairs() -> tuple[FakeComsolBackend, FakeComsolBackend]:
    reference = _opened(lane="1.3.1", properties=_PARITY_PROPERTIES)
    candidate = _opened(lane="1.4.0", properties=_PARITY_PROPERTIES)
    return reference, candidate


def test_lanes_agree_on_the_operations_that_must_not_drift() -> None:
    """Behavioural parity for everything except the two measured differences."""
    reference, candidate = _pairs()

    for backend in (reference, candidate):
        node = backend.find_node(["comp1"])
        assert backend.read_property(node, "L", form="float").value == 0.1
        assert backend.evaluate(EvaluationRequest(expression=_PARITY_EQUATION)).value == 0.0
        assert backend.dataset_names() == ["study1//solution1"]
        assert backend.solution_names() == ["sol1"]
        assert backend.convert(3, form="int", target="t").value == 3

    assert reference.operations_called() == candidate.operations_called()


def test_lanes_agree_on_the_same_refusal_codes() -> None:
    failures = {"property_read": "property_not_found"}
    reference = _opened(lane="1.3.1", properties=_PARITY_PROPERTIES, failures=failures)
    candidate = _opened(lane="1.4.0", properties=_PARITY_PROPERTIES, failures=failures)
    codes = []
    for backend in (reference, candidate):
        node = backend.find_node(["comp1"])
        with pytest.raises(AdapterError) as excinfo:
            backend.read_property(node, "L", form="float")
        codes.append(excinfo.value.reason_code)
    assert codes[0] == codes[1] == "property_not_found"


def test_the_quoted_matrix_refusal_matches_the_installed_reference_lane() -> None:
    """The quoted refusal text must still exist in the installed MPh 1.3.1.

    The adapter matches on that message because MPh raises a bare ``TypeError``
    with no code. If a future MPh changes the wording, the translation would
    silently stop firing, so the quote is pinned to the installed source.
    """
    import mph

    node_source = (Path(mph.__file__).parent / "node.py").read_text(encoding="utf-8-sig")
    assert REFERENCE_MATRIX_READ_REFUSAL in node_source
    # The write path refuses more than two rows on both lanes, so its message
    # must also still be present and is deliberately NOT translated per-lane.
    assert "Will not cast object arrays with more than two rows." in node_source


def test_lanes_differ_only_on_the_two_documented_capabilities() -> None:
    """The documented differences are the *only* ones the protocol models."""
    reference_caps = lane_capabilities("1.3.1")
    candidate_caps = lane_capabilities(MPH_1_4_LANE)
    assert reference_caps["supports_dbmodel_uri"] is False
    assert candidate_caps["supports_dbmodel_uri"] is True
    assert reference_caps["double_row_matrix_row_limit"] == REFERENCE_DOUBLE_ROW_MATRIX_ROW_LIMIT
    assert candidate_caps["double_row_matrix_row_limit"] is None
    assert reference_caps.keys() == candidate_caps.keys()
    differing = {key for key in reference_caps if reference_caps[key] != candidate_caps[key]}
    assert differing == {"lane", "supports_dbmodel_uri", "double_row_matrix_row_limit"}


def test_the_14_lane_refuses_live_dbmodel_loading_with_a_stable_code() -> None:
    """1.4 can load dbmodel:// URIs; 0.7.5 still performs no live operation."""
    from comsol_mcp.adapter.mph14_backend import Mph14Backend

    backend = Mph14Backend()
    with pytest.raises(AdapterError) as excinfo:
        backend.load_model("dbmodel://library/models/cell")
    assert excinfo.value.reason_code == "model_load_failed"
    assert "deferred to alpha7.6" in str(excinfo.value)
    assert backend.supports_dbmodel_uri() is True


def test_the_14_lane_reports_no_matrix_row_limit() -> None:
    """1.4 generalizes DoubleRowMatrix conversion; the lanes are not equivalent."""
    from comsol_mcp.adapter.mph14_backend import Mph14Backend

    assert Mph14Backend().matrix_row_capacity() is None


def test_an_unknown_lane_has_no_capability_record() -> None:
    with pytest.raises(AdapterError) as excinfo:
        lane_capabilities("9.9.9")
    assert excinfo.value.reason_code == "adapter_unavailable"


def test_the_matrix_difference_maps_to_a_stable_code() -> None:
    """The reference lane's live matrix read refusal becomes one stable code.

    The real path has two steps: MPh raises a bare ``TypeError`` while reading a
    ``DoubleRowMatrix`` wider than two rows, and ``MphBackendBase._translate``
    maps it to ``property_type_mismatch`` carrying MPh's exact message. The 1.4
    lane then collapses that message to ``conversion_not_representable``.

    Both steps run for real here: the first through the shared translator, the
    second through the lane's own collapse method.
    """
    from comsol_mcp.adapter.mph_backend import MphBackendBase

    # Step 1: the raw MPh exception is translated rather than passed through.
    translated = MphBackendBase()._translate(  # noqa: SLF001
        TypeError(REFERENCE_MATRIX_READ_REFUSAL), "property_read"
    )
    assert translated.reason_code == "property_type_mismatch"
    assert REFERENCE_MATRIX_READ_REFUSAL in str(translated)

    # Step 2: the 1.4 lane collapses that message to a stable code.
    lane = Mph14Backend()
    collapsed = lane._collapse_matrix_refusal(translated)  # noqa: SLF001
    assert collapsed.reason_code == "conversion_not_representable"
    assert f"{REFERENCE_DOUBLE_ROW_MATRIX_ROW_LIMIT} rows" in str(collapsed)


def test_an_unrelated_failure_is_not_reported_as_a_matrix_difference() -> None:
    """Only the quoted refusal is collapsed; anything else passes through."""
    lane = Mph14Backend()
    original = AdapterError("node_not_found", "no node", operation="node_lookup")
    assert lane._collapse_matrix_refusal(original) is original  # noqa: SLF001
