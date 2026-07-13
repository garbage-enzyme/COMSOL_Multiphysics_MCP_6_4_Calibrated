"""Isolated H4 semantic worker protocol with a dependency-free fake backend."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socketserver
import sys
import threading
import time
from typing import Any

from .semantic_contracts import PUBLIC_LIMITS, WORKER_PROTOCOL_SCHEMA_VERSION


MAXIMUM_REQUEST_BYTES = 16_384
TOKEN_ENVIRONMENT_VARIABLE = "COMSOL_SEMANTIC_SESSION_TOKEN"


def _response(request_id: str | None, *, success: bool, **fields: Any) -> dict[str, Any]:
    return {
        "schema_version": WORKER_PROTOCOL_SCHEMA_VERSION,
        "request_id": request_id,
        "success": success,
        **fields,
    }


class _WorkerState:
    def __init__(self, token: str, fault: str | None, query_delay: float):
        self.token = token
        self.fault = fault
        self.query_delay = query_delay
        self.started_at = time.time()
        self.query_count = 0
        self.load_count = 0
        self.last_error: str | None = None
        self.active = threading.Lock()
        self.capacity = threading.BoundedSemaphore(PUBLIC_LIMITS["maximum_queue_depth"] + 1)

    def status(self) -> dict[str, Any]:
        return {
            "backend": "fake",
            "pid": os.getpid(),
            "load_count": self.load_count,
            "query_count": self.query_count,
            "started_at_epoch": self.started_at,
            "last_error": self.last_error,
        }

    def query(self, query: str, limit: int) -> list[dict[str, Any]]:
        if self.fault == "query_hang":
            time.sleep(3600)
        if self.fault == "crash_before_response":
            os._exit(73)
        if self.query_delay:
            time.sleep(self.query_delay)
        self.query_count += 1
        return [
            {
                "source": "fake/COMSOL_ReferenceManual.pdf",
                "page": index + 1,
                "snippet": query[: PUBLIC_LIMITS["maximum_snippet_characters"]],
                "distance": float(index),
            }
            for index in range(limit)
        ]


class _RequestHandler(socketserver.StreamRequestHandler):
    server: "_WorkerServer"

    def handle(self) -> None:
        self.connection.settimeout(PUBLIC_LIMITS["query_deadline_seconds"])
        raw = self.rfile.readline(MAXIMUM_REQUEST_BYTES + 1)
        if len(raw) > MAXIMUM_REQUEST_BYTES or not raw.endswith(b"\n"):
            self._write(_response(None, success=False, error={"code": "invalid_request", "message": "request is oversized or unterminated"}))
            return
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(_response(None, success=False, error={"code": "invalid_json", "message": "request must be UTF-8 JSON"}))
            return
        request_id = request.get("request_id") if isinstance(request, dict) else None
        if not isinstance(request, dict) or request.get("schema_version") != WORKER_PROTOCOL_SCHEMA_VERSION:
            self._write(_response(request_id, success=False, error={"code": "invalid_schema", "message": "unsupported protocol schema"}))
            return
        token = request.get("token")
        if not isinstance(token, str) or not secrets.compare_digest(token, self.server.state.token):
            self._write(_response(request_id, success=False, error={"code": "unauthorized", "message": "invalid session token"}))
            return
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            self._write(_response(None, success=False, error={"code": "invalid_request_id", "message": "request_id is required"}))
            return
        if not self.server.state.capacity.acquire(blocking=False):
            self._write(_response(request_id, success=False, error={"code": "busy", "message": "worker queue is full"}))
            return
        try:
            with self.server.state.active:
                self._dispatch(request_id, request)
        finally:
            self.server.state.capacity.release()

    def _dispatch(self, request_id: str, request: dict[str, Any]) -> None:
        operation = request.get("operation")
        if operation in {"health", "status"}:
            response = _response(request_id, success=True, status=self.server.state.status())
        elif operation == "query":
            query = request.get("query")
            limit = request.get("limit", 5)
            if not isinstance(query, str) or not query.strip() or len(query) > PUBLIC_LIMITS["maximum_query_characters"]:
                response = _response(request_id, success=False, error={"code": "invalid_query", "message": "query violates public limits"})
            elif not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= PUBLIC_LIMITS["maximum_results"]:
                response = _response(request_id, success=False, error={"code": "invalid_limit", "message": "limit violates public limits"})
            else:
                results = self.server.state.query(query.strip(), limit)
                response = _response(request_id, success=True, results=results, status=self.server.state.status())
        else:
            response = _response(request_id, success=False, error={"code": "unknown_operation", "message": "operation is unsupported"})

        fault = self.server.state.fault
        if fault == "invalid_json":
            self.wfile.write(b"{invalid\n")
            self.wfile.flush()
            return
        if fault == "oversized_json":
            self.wfile.write(b'{"padding":"' + b"x" * (PUBLIC_LIMITS["maximum_response_bytes"] + 1) + b'"}\n')
            self.wfile.flush()
            return
        if fault == "wrong_request_id":
            response["request_id"] = "wrong-request-id"
        self._write(response)
        if fault == "crash_after_response":
            os._exit(74)

    def _write(self, response: dict[str, Any]) -> None:
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > PUBLIC_LIMITS["maximum_response_bytes"]:
            encoded = json.dumps(_response(response.get("request_id"), success=False, error={"code": "response_too_large", "message": "response exceeds public limit"}), separators=(",", ":")).encode("utf-8") + b"\n"
        self.wfile.write(encoded)
        self.wfile.flush()


class _WorkerServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: _WorkerState):
        self.state = state
        super().__init__(address, _RequestHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--fault", choices=[
        "startup_hang", "query_hang", "invalid_json", "oversized_json",
        "wrong_request_id", "crash_before_response", "crash_after_response",
    ])
    parser.add_argument("--query-delay", type=float, default=0.0)
    args = parser.parse_args()
    token = os.environ.get(TOKEN_ENVIRONMENT_VARIABLE, "")
    if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        raise SystemExit("missing or invalid semantic worker session token")
    if args.fault == "startup_hang":
        time.sleep(3600)
    state = _WorkerState(token, args.fault, max(0.0, args.query_delay))
    with _WorkerServer(("127.0.0.1", args.port), state) as server:
        handshake = {
            "schema_version": WORKER_PROTOCOL_SCHEMA_VERSION,
            "event": "ready",
            "pid": os.getpid(),
            "host": "127.0.0.1",
            "port": int(server.server_address[1]),
        }
        print(json.dumps(handshake, separators=(",", ":")), flush=True)
        server.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    main()
