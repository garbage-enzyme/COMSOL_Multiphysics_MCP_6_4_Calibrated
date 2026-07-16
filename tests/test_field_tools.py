from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from src.tools.field_evidence import register_field_evidence_tools


class _Node:
    def __init__(self, name, tag, kind=None, properties=(), solution=None, empty=False):
        self._name = name
        self._tag = tag
        self._kind = kind
        self._properties = properties
        self._solution = solution
        self.java = type("JavaNode", (), {"isEmpty": lambda _self: empty})()

    def name(self):
        return self._name

    def tag(self):
        return self._tag

    def type(self):
        return self._kind

    def properties(self):
        return list(self._properties)

    def property(self, name):
        if name != "solution":
            raise KeyError(name)
        return self._solution


class _Model:
    def __truediv__(self, group):
        return {
            "components": [_Node("Component 1", "comp1")],
            "solutions": [_Node("Solution 1", "sol1", empty=False)],
            "datasets": [
                _Node(
                    "研究 1//解 1",
                    "dset1",
                    kind="Solution",
                    properties=("solution",),
                    solution="sol1",
                )
            ],
        }[group]


def _tool():
    server = FastMCP("field-evidence-tools-test")
    register_field_evidence_tools(server)
    return server._tool_manager._tools["wave_optics_field_datasets"].fn


def test_public_field_dataset_discovery_is_locale_safe_and_read_only(monkeypatch):
    from src.tools import field_evidence

    model = _Model()
    monkeypatch.setattr(field_evidence.session_manager, "get_model", lambda name: model)
    monkeypatch.setattr(field_evidence.session_manager, "preflight_long_operation", lambda: {"ready": True})

    result = _tool()(model_name="fixture", max_datasets=4, max_components=2)

    assert result["success"] is True, result
    assert result["datasets"][0]["dataset_name"] == "研究 1//解 1"
    assert result["datasets"][0]["dataset_tag"] == "dset1"
    assert result["datasets"][0]["field_evaluation_eligible"] is True
    assert result["ownership_checked"] is True
    assert result["solver_started_by_tool"] is False
    assert result["study_run"] is False
    assert result["model_mutated"] is False


def test_public_field_dataset_discovery_fails_closed_on_incomplete_ownership(monkeypatch):
    from src.tools import field_evidence

    monkeypatch.setattr(field_evidence.session_manager, "get_model", lambda name: _Model())
    monkeypatch.setattr(
        field_evidence.session_manager,
        "preflight_long_operation",
        lambda: {"ready": False, "blockers": ["solver collision"]},
    )

    result = _tool()(model_name="fixture")

    assert result["success"] is False
    assert result["blockers"] == ["solver collision"]


def test_public_field_dataset_discovery_rejects_missing_model_and_limits(monkeypatch):
    from src.tools import field_evidence

    monkeypatch.setattr(field_evidence.session_manager, "get_model", lambda name: None)
    assert _tool()(model_name="missing") == {
        "success": False,
        "error": "Model not found: missing",
    }

    monkeypatch.setattr(field_evidence.session_manager, "get_model", lambda name: _Model())
    monkeypatch.setattr(field_evidence.session_manager, "preflight_long_operation", lambda: {"ready": True})
    invalid = _tool()(model_name="fixture", max_datasets=0)
    assert invalid["success"] is False
    assert "max_datasets" in invalid["error"]
