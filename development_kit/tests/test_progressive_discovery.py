"""Progressive cold-start discovery: catalog, on-demand fetch, and paging.

The 0.7.6 plan makes cold discovery a release gate: the default fresh-client
bootstrap must stay within 32 KiB against a 40 KiB hard ceiling, the full schema
catalog must remain reachable on demand, and a client that never requests a second
page must still receive a truthful surface.

These tests are solver-free. They build the real server in-process and, for the
paging adapter, exercise it over a real stdio session with the real MCP client,
because the middleware hook only runs inside the SDK runner.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from src.schema_registry import get_schema_registry
from src.server import create_server
from src.tools.catalog import TOOL_METADATA
from src.tools.discovery import (
    MAX_CATALOG_PAGE,
    SELECTOR_NAMES,
    get_tool_catalog,
    iter_reachable_tool_names,
)
from src.tools.profiles import ProfileSelection, resolve_profile
from src.tools.tools_list_pagination import (
    CURSOR_PREFIX,
    CursorError,
    decode_cursor,
    encode_cursor,
    page_tools,
)

ROOT = Path(__file__).parents[2]

# The frozen byte gate from the 0.7.6 plan and roadmap.
BOOTSTRAP_TARGET_BYTES = 32 * 1024
BOOTSTRAP_CEILING_BYTES = 40 * 1024

_SELECTION = ProfileSelection(
    name="core",
    source="discovery-test",
    environment_variable="COMSOL_MCP_PROFILE",
    default_used=False,
)


def _size(value: object) -> int:
    return len(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    )


def _core() -> ProfileSelection:
    return _SELECTION


# --------------------------------------------------------------------------
# Bootstrap byte gate
# --------------------------------------------------------------------------


def test_capabilities_bootstrap_is_within_the_target_and_ceiling():
    """The measured cold-start payload is the gate the plan froze.

    0.7.5 measured 81,845 B, of which 60,293 B was the schema registry, so this
    assertion is the direct regression guard for the P0 fix.
    """
    server = create_server("discovery-bootstrap", profile="core")
    capabilities = _call(server, "capabilities")
    measured = _size(capabilities)

    assert measured <= BOOTSTRAP_TARGET_BYTES, measured
    assert measured <= BOOTSTRAP_CEILING_BYTES

    registry_view = capabilities["schema_registry"]
    assert "entries" not in registry_view
    assert _size(registry_view) < 2048
    # The registry identity survives the payload reduction.
    assert registry_view["registry_sha256"] == get_schema_registry()["registry_sha256"]
    assert registry_view["entry_count"] == get_schema_registry()["entry_count"]


def test_bootstrap_plus_catalog_summary_is_still_bounded():
    """A caller that also reads the compact catalog must stay bounded."""
    server = create_server("discovery-bootstrap-total", profile="core")
    total = _size(_call(server, "capabilities")) + _size(_call(server, "catalog"))
    assert total <= BOOTSTRAP_TARGET_BYTES, total


def test_bootstrap_carries_no_registry_entry_rows_in_any_profile():
    """Every profile that can report capabilities uses the compact view.

    ``comsolless_read_only`` is excluded deliberately: it is frozen at five
    stdlib-only read-only tools and reaches neither ``capabilities`` nor
    ``catalog``, so adding one there would break that frozen contract.
    """
    for profile in ("core", "basic_fem", "wave_optics", "experimental", "full"):
        server = create_server(f"discovery-{profile}", profile=profile)
        view = _call(server, "capabilities")["schema_registry"]
        assert "entries" not in view, profile
        assert view["view"] == "summary", profile


# --------------------------------------------------------------------------
# Compact catalog
# --------------------------------------------------------------------------


def _call(server, name: str, arguments: dict | None = None):
    from development_kit.tests.mcp_test_support import decode_tool_result

    return decode_tool_result(asyncio.run(server.call_tool(name, arguments or {})))


def test_catalog_summary_is_complete_over_domains_and_states_its_selectors():
    summary = get_tool_catalog(_core())

    assert summary["success"] is True
    assert summary["selector"] is None
    assert summary["tool_count"] == len(TOOL_METADATA)
    assert summary["domain_count"] == len({m.group for m in TOOL_METADATA.values()})
    assert summary["selectors"] == list(SELECTOR_NAMES)
    assert summary["registry_sha256"] == get_schema_registry()["registry_sha256"]

    domains = {row["domain"] for row in summary["domains"]}
    assert domains == {m.group for m in TOOL_METADATA.values()}
    # Domain rows account for every tool: the summary is never a subset.
    assert sum(row["tool_count"] for row in summary["domains"]) == len(TOOL_METADATA)


def test_catalog_domain_fetch_returns_full_schemas_and_states_completeness():
    server = create_server("discovery-domain", profile="full")
    page = _call(server, "catalog", {"domain": "jobs"})

    assert page["success"] is True
    assert page["selector"] == "domain"
    assert page["tool_count"] == page["returned_tool_count"]
    assert page["complete"] is True
    assert page["next_offset"] is None
    assert page["schemas_unavailable_for"] == []
    for row in page["tools"]:
        assert row["input_schema"] is not None
        assert row["input_schema"]["type"] == "object"


def test_domain_paging_covers_every_tool_exactly_once():
    """A partial page must always come with a way to reach the rest."""
    server = create_server("discovery-paging", profile="full")
    seen: list[str] = []
    offset = 0
    pages = 0
    while True:
        page = _call(server, "catalog", {"domain": "physics", "offset": offset, "limit": 7})
        pages += 1
        assert page["returned_tool_count"] <= 7
        seen.extend(row["name"] for row in page["tools"])
        if page["complete"]:
            assert page["next_offset"] is None
            break
        assert page["next_offset"] == offset + page["returned_tool_count"]
        offset = page["next_offset"]
        assert pages < 20

    expected = sorted(
        name for name, metadata in TOOL_METADATA.items() if metadata.group == "physics"
    )
    assert seen == expected
    assert len(seen) == len(set(seen))


def test_catalog_tool_fetch_returns_the_advertised_schema_exactly():
    """The on-demand schema must equal what the MCP client is served."""
    server = create_server("discovery-tool", profile="full")
    advertised = {tool.name: tool.input_schema for tool in asyncio.run(server.list_tools())}
    fetched = _call(server, "catalog", {"tool": "job_submit"})

    assert fetched["success"] is True
    assert fetched["input_schema_available"] is True
    assert fetched["input_schema_source"] == "registered_tool_manager"
    assert fetched["input_schema"] == advertised["job_submit"]
    assert fetched["tool"]["name"] == "job_submit"


def test_catalog_never_claims_a_schema_it_cannot_produce():
    """Without a live tool manager the catalog reports the gap, not a stub.

    The plan forbids claiming an unavailable schema. A domain page served without
    an advertised-schema source must therefore name every tool whose schema it
    could not attach, rather than emitting an invented placeholder.
    """
    page = get_tool_catalog(_core(), domain="jobs", surface=None)

    assert page["success"] is True
    assert page["returned_tool_count"] > 0
    assert page["schemas_unavailable_for"] == [row["name"] for row in page["tools"]]
    assert all(row["input_schema"] is None for row in page["tools"])
    assert page["input_schema_source"] == "registered_tool_manager"


def test_catalog_reports_only_the_tools_missing_an_advertised_schema():
    """A partially advertised surface names exactly the gaps, not all tools."""

    class _Surface:
        def advertised_input_schemas(self):
            return {"job_submit": {"type": "object", "properties": {}}}

    page = get_tool_catalog(_core(), domain="jobs", surface=_Surface())

    by_name = {row["name"]: row for row in page["tools"]}
    assert by_name["job_submit"]["input_schema"] == {"type": "object", "properties": {}}
    assert "job_submit" not in page["schemas_unavailable_for"]
    assert set(page["schemas_unavailable_for"]) == set(by_name) - {"job_submit"}


def test_catalog_ignores_a_surface_that_cannot_advertise_schemas():
    """A wrong-shaped surface is treated as unavailable, never as empty schemas."""

    class _Bogus:
        advertised_input_schemas = "not callable"

    page = get_tool_catalog(_core(), domain="jobs", surface=_Bogus())

    assert page["success"] is True
    assert all(row["input_schema"] is None for row in page["tools"])
    assert page["schemas_unavailable_for"] == [row["name"] for row in page["tools"]]


def test_iter_reachable_tool_names_is_sorted_and_matches_the_declared_surface():
    """The reachable-name helper is the deterministic ordering other code uses."""
    names = list(iter_reachable_tool_names(_core()))

    assert names == sorted(names)
    assert len(names) == len(set(names))
    expected = {
        name
        for name, metadata in TOOL_METADATA.items()
        if "core" in metadata.intended_profiles and metadata.feature_gate is None
    }
    assert set(names) == expected


def test_catalog_refuses_unknown_selectors_without_substituting_an_answer():
    unknown_domain = get_tool_catalog(_core(), domain="not_a_domain")
    assert unknown_domain["success"] is False
    assert unknown_domain["reason_code"] == "unknown_domain"
    assert "tools" not in unknown_domain
    assert "jobs" in unknown_domain["known_domains"]

    unknown_tool = get_tool_catalog(_core(), tool="not_a_tool")
    assert unknown_tool["success"] is False
    assert unknown_tool["reason_code"] == "unknown_tool"
    assert "tool" not in unknown_tool

    unknown_schema = get_tool_catalog(_core(), schema="comsol_mcp.not_a_schema")
    assert unknown_schema["success"] is False
    assert unknown_schema["reason_code"] == "unknown_schema_name"
    assert "entry" not in unknown_schema


def test_catalog_refuses_ambiguous_or_malformed_selectors():
    both = get_tool_catalog(_core(), domain="jobs", tool="job_submit")
    assert both["success"] is False
    assert both["reason_code"] == "multiple_selectors"
    assert both["supplied_selectors"] == ["domain", "tool"]

    blank = get_tool_catalog(_core(), domain="   ")
    assert blank["success"] is False
    assert blank["reason_code"] == "invalid_selector_value"

    bad_offset = get_tool_catalog(_core(), domain="jobs", offset=-1)
    assert bad_offset["success"] is False
    assert bad_offset["reason_code"] == "invalid_offset"

    bad_limit = get_tool_catalog(_core(), domain="jobs", limit=MAX_CATALOG_PAGE + 1)
    assert bad_limit["success"] is False
    assert bad_limit["reason_code"] == "invalid_limit"

    zero_limit = get_tool_catalog(_core(), domain="jobs", limit=0)
    assert zero_limit["success"] is False
    assert zero_limit["reason_code"] == "invalid_limit"

    past_end = get_tool_catalog(_core(), domain="jobs", offset=999)
    assert past_end["success"] is False
    assert past_end["reason_code"] == "offset_out_of_range"


def test_catalog_is_reachable_and_solver_free_in_every_working_profile():
    """Domain loading boundaries must not hide the discovery entry point.

    The offline ``comsolless_read_only`` profile is excluded because it is frozen
    at five stdlib-only read-only tools; ``catalog`` belongs to the profiles that
    can start work, and that boundary is asserted separately below.
    """
    for profile in ("core", "basic_fem", "wave_optics", "experimental", "full"):
        summary = get_tool_catalog(resolve_profile(profile, environ={}))
        assert summary["success"] is True, profile
        assert summary["profile"] == profile
        reachable = {row["domain"]: row["reachable_tool_count"] for row in summary["domains"]}
        assert reachable.get("discovery", 0) == 1, profile


def test_offline_profile_keeps_its_frozen_five_tool_surface():
    """Progressive discovery must not widen the frozen offline profile."""
    from src.tools.profiles import tool_names_for_profile

    assert tool_names_for_profile("comsolless_read_only") == {
        "mph_inspect",
        "mph_diff",
        "model_identity",
        "runtime_compatibility_status",
        "offline_export_validate",
    }


def test_catalog_import_starts_no_solver_and_no_heavy_dependency():
    """Importing the discovery surface must stay a cold, solver-free path."""
    code = (
        "import sys\n"
        "from src.tools.discovery import get_tool_catalog\n"
        "payload = get_tool_catalog()\n"
        "assert payload['success'] is True\n"
        "heavy = sorted({n.split('.')[0] for n in sys.modules}\n"
        "               & {'numpy', 'scipy', 'matplotlib', 'jpype', 'mph'})\n"
        "assert heavy == [], heavy\n"
        "print(payload['tool_count'])\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == str(len(TOOL_METADATA))


# --------------------------------------------------------------------------
# tools/list cursor adapter
# --------------------------------------------------------------------------


def test_cursor_round_trips_and_refuses_foreign_values():
    assert decode_cursor(None) == 0
    assert decode_cursor(encode_cursor(25)) == 25
    assert encode_cursor(25).startswith(CURSOR_PREFIX)

    for bad in ("garbage", "tools-v1:abc", "tools-v1:", "other:5", 5, []):
        with pytest.raises(CursorError):
            decode_cursor(bad)


def test_page_tools_accounts_for_the_whole_surface():
    tools = [type("T", (), {"name": f"tool_{index:03d}"})() for index in range(30)]

    first, cursor, accounting = page_tools(tools, None, page_size=10)
    assert [tool.name for tool in first] == [f"tool_{index:03d}" for index in range(10)]
    assert cursor == encode_cursor(10)
    assert accounting == {"totalTools": 30, "returnedTools": 10, "remainingTools": 20}

    last, end_cursor, last_accounting = page_tools(tools, encode_cursor(20), page_size=10)
    assert len(last) == 10
    assert end_cursor is None
    assert last_accounting == {"totalTools": 30, "returnedTools": 10, "remainingTools": 0}

    with pytest.raises(CursorError):
        page_tools(tools, encode_cursor(31), page_size=10)
    with pytest.raises(CursorError):
        page_tools(tools, None, page_size=0)
    with pytest.raises(CursorError):
        page_tools(tools, None, page_size=10_000)


def test_page_tools_orders_deterministically_under_serialized_entries():
    """The middleware may see wire dicts; ordering and names must still resolve."""
    tools = [{"name": name} for name in ("zeta", "alpha", "mid")]
    first, cursor, accounting = page_tools(tools, None, page_size=2)
    assert [tool["name"] for tool in first] == ["alpha", "mid"]
    assert cursor == encode_cursor(2)
    assert accounting["totalTools"] == 3

    with pytest.raises(CursorError):
        page_tools([{"noname": 1}], None, page_size=1)


def test_pagination_setting_defaults_off_and_is_a_real_boolean(tmp_path: Path):
    """The adapter is default-off, and a quoted string cannot enable it."""
    from src.settings import SETTINGS_PATH_ENV, load_settings

    defaults = load_settings({SETTINGS_PATH_ENV: str(ROOT / "settings.json")})
    assert defaults["discovery"]["pagination_enabled"] is False

    for value, expected in ((True, True), (False, False)):
        document = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
        document["discovery"] = {"pagination_enabled": value}
        path = tmp_path / f"settings-{value}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        loaded = load_settings({SETTINGS_PATH_ENV: str(path)})
        assert loaded["discovery"]["pagination_enabled"] is expected


def test_settings_document_keeps_the_pagination_switch_default_off():
    document = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    assert document["discovery"] == {"pagination_enabled": False}


@pytest.mark.parametrize("pagination", [False, True])
def test_stdio_tools_list_is_complete_and_truthful(pagination: bool, tmp_path: Path):
    """A client that never requests a second page must still see every tool.

    This is the MCP1/OpenCode contract: with the adapter off the listing is the
    ordinary complete one, and with it on the first page still declares the true
    total so the surface can never be mistaken for smaller than it is.
    """
    import anyio
    from mcp import ClientSession, types
    from mcp.client.stdio import StdioServerParameters, stdio_client

    document = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    document["discovery"] = {"pagination_enabled": pagination}
    settings_path = tmp_path / f"settings-{pagination}.json"
    settings_path.write_text(json.dumps(document), encoding="utf-8")

    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("COMSOL_MCP_")
    }
    environment.update(
        {
            "COMSOL_MCP_PROFILE": "core",
            "COMSOL_MCP_SETTINGS_PATH": str(settings_path),
        }
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.server"],
        cwd=str(ROOT),
        env=environment,
    )

    async def collect() -> tuple[list[dict], list[str], list[dict]]:
        pages: list[dict] = []
        names: list[str] = []
        raw: list[dict] = []
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=60.0) as session:
                await session.initialize()
                cursor = None
                while True:
                    params = (
                        types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
                    )
                    result = await session.list_tools(params=params)
                    raw.append(result.model_dump(by_alias=True, exclude_none=True, mode="json"))
                    pages.append(result.model_dump(by_alias=True, mode="json"))
                    names.extend(tool.name for tool in result.tools)
                    cursor = getattr(result, "next_cursor", None) or getattr(
                        result, "nextCursor", None
                    )
                    if not cursor or len(pages) > 40:
                        break
        return pages, names, raw

    pages, names, raw = anyio.run(collect)
    assert names, "no tools were listed"
    assert len(names) == len(set(names))

    if not pagination:
        assert len(pages) == 1
        assert raw[0].get("nextCursor") in (None, "")
        assert "_meta" not in raw[0]
    else:
        assert len(pages) > 1
        for page in raw:
            meta = page.get("_meta")
            assert isinstance(meta, dict)
            assert meta["adapter"] == "comsol_mcp.tools_list_pagination"
            assert meta["totalTools"] == len(names)
            assert meta["returnedTools"] == len(page["tools"])
        assert raw[-1].get("nextCursor") in (None, "")
        assert raw[-1]["_meta"]["remainingTools"] == 0
    # Either way the reachable surface is exactly the declared registry.
    expected = {
        name
        for name, metadata in TOOL_METADATA.items()
        if "core" in metadata.intended_profiles and metadata.feature_gate is None
    }
    assert set(names) == expected


class _Wire:
    """Minimal JSON-RPC stdio client, so the wire is tested without SDK paging."""

    def __init__(self, settings_path: Path, workspace: Path) -> None:
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("COMSOL_MCP_")
        }
        environment["COMSOL_MCP_PROFILE"] = "core"
        environment["COMSOL_MCP_SETTINGS_PATH"] = str(settings_path)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "src.server"],
            cwd=str(ROOT),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._id = 0

    def _send(self, payload: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload: dict = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)
        assert self.process.stdout is not None
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise AssertionError(f"server closed stdout during {method}")
            text = line.decode("utf-8").strip()
            if not text:
                continue
            message = json.loads(text)
            if message.get("id") != self._id:
                continue  # server-initiated notification
            assert "error" not in message, message
            return message["result"]

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()


@pytest.mark.parametrize("revision", ["2024-11-05", "2025-03-26", "2025-06-18"])
@pytest.mark.parametrize("pagination", [False, True])
def test_mcp1_first_page_client_receives_a_truthful_usable_surface(
    revision: str, pagination: bool, tmp_path: Path
):
    """The first page must never misrepresent the surface to a legacy client.

    Exercised on the raw wire rather than through the SDK client, whose own paging
    could hide the question. With the adapter off, one request returns everything
    and carries no cursor. With it on, the first page must declare the true total
    so a client that reads only that page knows the surface is larger.
    """
    document = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    document["discovery"] = {"pagination_enabled": pagination}
    settings_path = tmp_path / f"settings-{revision}-{pagination}.json"
    settings_path.write_text(json.dumps(document), encoding="utf-8")

    wire = _Wire(settings_path, tmp_path)
    try:
        initialized = wire.request(
            "initialize",
            {
                "protocolVersion": revision,
                "capabilities": {},
                "clientInfo": {"name": "first-page-client", "version": "1.0.0"},
            },
        )
        # The server must honour the requested revision, not silently upgrade it.
        assert initialized["protocolVersion"] == revision
        wire.notify("notifications/initialized")

        first = wire.request("tools/list", {})
        first_names = [tool["name"] for tool in first.get("tools", [])]
        assert first_names, "a first-page client received an empty surface"

        # Whatever it was shown must actually be callable.
        called = wire.request("tools/call", {"name": "capabilities", "arguments": {}})
        assert called.get("isError") is not True
        assert called.get("structuredContent") or called.get("content")

        all_names = list(first_names)
        cursor = first.get("nextCursor")
        pages = 1
        while cursor and pages < 40:
            page = wire.request("tools/list", {"cursor": cursor})
            all_names.extend(tool["name"] for tool in page.get("tools", []))
            cursor = page.get("nextCursor")
            pages += 1

        assert len(all_names) == len(set(all_names)), "a tool was listed twice"
        if pagination:
            assert first.get("nextCursor"), "the adapter served an unpaged listing"
            assert pages > 1
            assert first["_meta"]["totalTools"] == len(all_names)
        else:
            # The complete listing in one response, with no cursor to follow.
            assert not first.get("nextCursor")
            assert pages == 1
            assert first_names == all_names

        expected = {
            name
            for name, metadata in TOOL_METADATA.items()
            if "core" in metadata.intended_profiles and metadata.feature_gate is None
        }
        assert set(all_names) == expected
    finally:
        wire.close()
