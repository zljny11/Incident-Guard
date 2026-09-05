from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(slots=True)
class ServiceState:
    service_name: str
    version: str
    upstream_url: str | None = None
    started_at: float = field(default_factory=time.time)
    request_count: int = 0
    error_count: int = 0
    fault_mode: str | None = None
    admin_token: str = ""
    hang_seconds: float = 5.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, *, error: bool = False) -> None:
        with self.lock:
            self.request_count += 1
            if error:
                self.error_count += 1

    def counters(self) -> tuple[int, int]:
        with self.lock:
            return self.request_count, self.error_count

    def inject_fault(self, fault_mode: str) -> None:
        if fault_mode != "transient_hang":
            raise ValueError(f"unsupported fault mode: {fault_mode}")
        with self.lock:
            self.fault_mode = fault_mode

    def current_fault(self) -> str | None:
        with self.lock:
            return self.fault_mode


def query_upstream(url: str | None, timeout: float = 1.0) -> dict[str, Any] | None:
    if url is None:
        return None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if response.status != HTTPStatus.OK or payload.get("status") != "healthy":
            raise RuntimeError("upstream reported unhealthy")
        return payload
    except (OSError, ValueError, urllib.error.URLError) as error:
        return {"status": "unhealthy", "error": type(error).__name__}


def health_payload(state: ServiceState) -> tuple[int, dict[str, Any]]:
    if state.current_fault() == "transient_hang":
        time.sleep(state.hang_seconds)
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "service": state.service_name,
            "version": state.version,
            "status": "unhealthy",
            "fault": "transient_hang",
        }
    if state.service_name == "payment-service" and state.version == "v2":
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "service": state.service_name,
            "version": state.version,
            "status": "unhealthy",
            "fault": "bad_deployment",
            "error_rate": 0.42,
        }
    upstream = query_upstream(state.upstream_url)
    healthy = upstream is None or upstream.get("status") == "healthy"
    payload: dict[str, Any] = {
        "service": state.service_name,
        "version": state.version,
        "status": "healthy" if healthy else "unhealthy",
        "uptime_seconds": round(time.time() - state.started_at, 3),
    }
    if upstream is not None:
        payload["upstream"] = upstream
    return (HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE), payload


def metrics_payload(state: ServiceState) -> str:
    requests, errors = state.counters()
    labels = f'service="{state.service_name}",version="{state.version}"'
    return "\n".join(
        (
            "# TYPE incident_guard_requests_total counter",
            f"incident_guard_requests_total{{{labels}}} {requests}",
            "# TYPE incident_guard_errors_total counter",
            f"incident_guard_errors_total{{{labels}}} {errors}",
            "",
        )
    )


def build_handler(state: ServiceState) -> type[BaseHTTPRequestHandler]:
    class ServiceHandler(BaseHTTPRequestHandler):
        server_version = "IncidentGuardLab/1.0"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path == "/health":
                status, payload = health_payload(state)
                state.record(error=status != HTTPStatus.OK)
                self._json(status, payload)
                return
            if self.path == "/metrics":
                state.record()
                body = metrics_payload(state).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/":
                if state.service_name == "payment-service" and state.version == "v2":
                    state.record(error=True)
                    self._json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {
                            "service": state.service_name,
                            "version": state.version,
                            "error": "deterministic_v2_regression",
                        },
                    )
                    return
                state.record()
                self._json(
                    HTTPStatus.OK,
                    {
                        "service": state.service_name,
                        "version": state.version,
                        "message": "incident guard lab",
                    },
                )
                return
            state.record(error=True)
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/admin/inject":
                state.record(error=True)
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if (
                not state.admin_token
                or self.headers.get("Authorization")
                != f"Bearer {state.admin_token}"
            ):
                state.record(error=True)
                self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(content_length))
                fault_mode = payload.get("fault")
                state.inject_fault(fault_mode)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                state.record(error=True)
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_fault", "detail": str(error)},
                )
                return
            state.record()
            self._json(
                HTTPStatus.OK,
                {
                    "service": state.service_name,
                    "fault": fault_mode,
                    "status": "injected",
                },
            )

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, message_format: str, *args: object) -> None:
            record = {
                "timestamp": time.time(),
                "level": "info",
                "service": state.service_name,
                "version": state.version,
                "client": self.client_address[0],
                "method": self.command,
                "path": self.path,
                "message": message_format % args,
            }
            print(
                json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True
            )

    return ServiceHandler


def main() -> None:
    state = ServiceState(
        service_name=os.environ.get("SERVICE_NAME", "service"),
        version=os.environ.get("SERVICE_VERSION", "v1"),
        upstream_url=os.environ.get("UPSTREAM_URL") or None,
        admin_token=os.environ.get("LAB_ADMIN_TOKEN", ""),
        hang_seconds=float(os.environ.get("HANG_SECONDS", "5")),
    )
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), build_handler(state))
    print(
        json.dumps(
            {
                "timestamp": time.time(),
                "level": "info",
                "event": "service.started",
                "service": state.service_name,
                "version": state.version,
                "port": port,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
