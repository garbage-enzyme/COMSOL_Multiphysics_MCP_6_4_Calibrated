import json
from pathlib import Path

from src.jobs import native_cancel_probe as probe


def test_discover_environment_records_build_and_hashes(monkeypatch, tmp_path):
    root = tmp_path / "comsol"
    api = root / "apiplugins" / "com.comsol.api_1.0.0.jar"
    model = root / "plugins" / "com.comsol.model_1.0.0.jar"
    client = root / "plugins" / "com.comsol.clientapi_1.0.0.jar"
    for path in (api, model, client):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode("ascii"))

    monkeypatch.setattr(
        probe.mph.discovery,
        "backend",
        lambda version=None: {
            "name": "6.4",
            "major": 6,
            "minor": 4,
            "patch": 0,
            "build": 293,
            "root": root,
            "jvm": root / "java" / "jvm.dll",
        },
    )

    manifest = probe.discover_environment()

    assert manifest["backend"]["build"] == 293
    assert manifest["jars"]["api"]["exists"] is True
    assert len(manifest["jars"]["clientapi"]["sha256"]) == 64
    assert manifest["candidates"]["connection_internal"]["required_methods"] == [
        "cancelRunnable()",
        "stopRunnable(int)",
    ]


def test_reflection_refuses_to_start_a_jvm(monkeypatch):
    monkeypatch.setattr(probe.jpype, "isJVMStarted", lambda: False, raising=False)
    try:
        probe.reflect_candidate_signatures()
    except RuntimeError as exc:
        assert "probe-only" in str(exc)
    else:
        raise AssertionError("reflection unexpectedly proceeded without a JVM")


def test_native_cancel_profile_is_data_only_and_pins_all_required_jars():
    path = Path(probe.__file__).with_name("native_cancel_profiles.json")
    profile = json.loads(path.read_text(encoding="utf-8"))["profiles"][0]

    assert profile["backend"] == {"major": 6, "minor": 4, "patch": 0, "build": 293}
    assert set(profile["jars"]) == {"api", "model", "clientapi"}
    assert all(len(item["sha256"]) == 64 for item in profile["jars"].values())
    assert profile["candidate"]["methods"] == ["cancel()", "stop(int)"]
    assert profile["native_cancel_gate"]["fresh_subprocess_runs"] == 3


def test_unknown_environment_never_selects_a_native_profile(monkeypatch):
    monkeypatch.setattr(probe, "discover_environment", lambda: {"backend": {"major": 6, "minor": 4, "patch": 9, "build": 1}, "jars": {}})
    assert probe.select_progress_context_profile() is None
