from __future__ import annotations

import json
import threading
import urllib.request
from contextlib import ExitStack, contextmanager
from http.server import ThreadingHTTPServer

from lab.services.service import ServiceState, build_handler


@contextmanager
def running_service(state: ServiceState):
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def read_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def test_real_http_chain_reports_same_healthy_initial_state(capsys) -> None:
    with ExitStack() as stack:
        dependency_url = stack.enter_context(
            running_service(ServiceState("dependency-service", "v1"))
        )
        payment_url = stack.enter_context(
            running_service(
                ServiceState(
                    "payment-service",
                    "v1",
                    upstream_url=f"{dependency_url}/health",
                )
            )
        )
        shop_url = stack.enter_context(
            running_service(
                ServiceState(
                    "shop-api", "v1", upstream_url=f"{payment_url}/health"
                )
            )
        )

        payloads = [
            read_json(f"{shop_url}/health"),
            read_json(f"{payment_url}/health"),
            read_json(f"{dependency_url}/health"),
        ]

    assert [(item["service"], item["version"], item["status"]) for item in payloads] == [
        ("shop-api", "v1", "healthy"),
        ("payment-service", "v1", "healthy"),
        ("dependency-service", "v1", "healthy"),
    ]
    log_lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert {line["service"] for line in log_lines} == {
        "shop-api",
        "payment-service",
        "dependency-service",
    }
    assert all(line["path"] == "/health" for line in log_lines)


def test_real_metrics_endpoint_counts_http_requests() -> None:
    state = ServiceState("payment-service", "v1")
    with running_service(state) as service_url:
        read_json(f"{service_url}/health")
        with urllib.request.urlopen(f"{service_url}/metrics", timeout=2) as response:
            metrics = response.read().decode("utf-8")

    assert (
        'incident_guard_requests_total{service="payment-service",version="v1"} 2'
        in metrics
    )
    assert (
        'incident_guard_errors_total{service="payment-service",version="v1"} 0'
        in metrics
    )
