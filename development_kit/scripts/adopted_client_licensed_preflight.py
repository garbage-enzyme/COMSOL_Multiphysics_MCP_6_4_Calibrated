"""Licensed preflight: prove the adopted-client path against real MPh 1.3.1.

This is the regression probe for the 2026-09-25 acceptance finding. The S4A
migration made the DNN bridge call ``attach_client(model)`` and the adapter read
``model.client``; MPh 1.3.1 exposes no such back-reference, so both licensed
training runs failed before DNN configuration with

    AdapterError: the supplied object does not expose an MPh client

This probe answers one question against the real library rather than a stub:
given a real ``mph.Client`` and one of its real ``Model`` objects, does the
adapter adopt the client, prove the pairing, and resolve a node through it?

It is deliberately small. It creates one model with a component, enumerates the
study and function containers, resolves a node, and releases the client. It does
not configure or train a DNN: that is the S4/S6 gate's job.

Ownership: the probe creates the client, so the probe disconnects it. Nothing is
adopted across a process boundary and no lease is taken by the adapter.

Usage::

    python development_kit/scripts/adopted_client_licensed_preflight.py \
        --output D:\\mcp_tests\\a75adopt\\preflight.json --cores 1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

SCHEMA_NAME = "comsol_mcp.adopted_client_licensed_preflight"
SCHEMA_VERSION = "1.0.0"

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cores", type=int, default=1)
    parser.add_argument("--version", default="6.4")
    return parser


class _ForeignClient:
    """A client whose ``models()`` reports a model it does not own.

    MPh allows only one client per Python session ("Only one client can be
    instantiated per Python session"), so this stand-in cannot be a *second real*
    client. It is constructed so the model it reports is provably absent from the
    real client's live set: the caller clears the real client first, which
    destroys that model, while the stale wrapper keeps reporting its old tag. The
    ownership proof compares against the client's live tag set, so this stand-in
    must be refused.
    """

    def __init__(self, *, models: list[Any]) -> None:
        self._models = models
        self.cleared = False

    def models(self) -> list[Any]:
        return list(self._models)

    def clear(self) -> None:
        self.cleared = True


def _probe(output: Path, cores: int, version: str) -> int:
    started = time.monotonic()
    result: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "cores": cores,
        "requested_version": version,
    }
    client: Any = None
    try:
        import jpype  # noqa: PLC0415
        import mph  # noqa: PLC0415

        from comsol_mcp.adapter import make_backend  # noqa: PLC0415
        from comsol_mcp.surrogate.dnn_clientapi_backend import (  # noqa: PLC0415
            ClientapiSurrogateDnnBackend,
        )

        result["python"] = sys.version.split()[0]
        result["mph_version"] = str(mph.__version__)

        client = mph.Client(cores=cores, version=version)
        result["client"] = {
            "version": str(client.version),
            "standalone": bool(client.standalone),
            "jvm_started": bool(jpype.isJVMStarted()),
        }

        model = client.create("AdoptedClientPreflight")
        model.java.component().create("comp1", True)

        # The measured fact this whole repair rests on. It is recorded rather
        # than asserted from memory, so a future MPh that does add the
        # back-reference changes this receipt instead of silently invalidating it.
        result["model_exposes_client_attribute"] = hasattr(model, "client")
        result["model_public_names"] = sorted(
            name for name in dir(model) if not name.startswith("_")
        )

        # The regression itself: the bridge must accept (model, client) and the
        # adapter must prove the client owns the model.
        bridge = ClientapiSurrogateDnnBackend(model, client=client)
        result["bridge_constructed"] = True

        adapter = bridge.adapter
        adopted = adapter.adopted_client()
        result["adapter_adopted_the_supplied_client"] = adopted is client
        result["real_client_accepted_its_model"] = adopted is client

        # Node resolution through the adopted client, which is what the DNN
        # configuration path does first.
        result["study_tags"] = [str(tag) for tag in bridge.study_tags()]
        result["func_tags"] = [str(tag) for tag in bridge.func_tags()]
        node = adapter.find_node(("component", "comp1"))
        result["resolved_node_path"] = list(node.path)
        result["component_tag"] = "comp1"

        result["success"] = all(
            [
                result["bridge_constructed"],
                result["adapter_adopted_the_supplied_client"],
                result["resolved_node_path"] == ["component", "comp1"],
            ]
        )
    except Exception as exc:  # noqa: BLE001 - the receipt records the failure
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()[-3000:]
    finally:
        # The probe created this client, so the probe releases it. The adapter
        # never owned it and must not have cleared it. ``clear`` is the cleanup
        # the accepted gates use; ``disconnect`` only applies to a remote server
        # client and raises on a standalone one.
        if client is not None:
            # Negative control, run *after* cleanup so the wrapper is genuinely
            # stale: a client must not be able to adopt a model that is no longer
            # in its live set. MPh allows only one client per session, so the
            # "foreign" client is a stand-in that reports this dead model.
            try:
                client.clear()
                result["client_cleared"] = True
                stale_refused = False
                stale_reason = None
                try:
                    make_backend().attach_client(model, client=_ForeignClient(models=[model]))
                except Exception as exc:  # noqa: BLE001 - the refusal is the assertion
                    stale_refused = True
                    stale_reason = f"{type(exc).__name__}: {exc}"
                result["stale_model_refused"] = stale_refused
                result["stale_model_reason"] = stale_reason
                if not stale_refused and not result.get("error"):
                    result["success"] = False
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                result["client_clear_error"] = f"{type(exc).__name__}: {exc}"
                result["success"] = False
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return 0 if result["success"] else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return _probe(args.output, args.cores, args.version)


if __name__ == "__main__":
    raise SystemExit(main())
