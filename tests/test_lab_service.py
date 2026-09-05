from __future__ import annotations

from pathlib import Path

from lab.services import service


SERVICE_PATH = Path(__file__).parents[1] / "lab" / "services" / "service.py"


def test_initial_service_state_is_healthy_and_deterministic(monkeypatch) -> None:
    monkeypatch.setattr(service.time, "time", lambda: 100.0)
    state = service.ServiceState("payment-service", "v1", started_at=90.0)

    first = service.health_payload(state)
    second = service.health_payload(state)

    assert first == second
    assert first == (
        200,
        {
            "service": "payment-service",
            "version": "v1",
            "status": "healthy",
            "uptime_seconds": 10.0,
        },
    )


def test_metrics_reflect_real_request_and_error_counters() -> None:
    state = service.ServiceState("shop-api", "v1")
    state.record()
    state.record(error=True)

    metrics = service.metrics_payload(state)

    assert 'incident_guard_requests_total{service="shop-api",version="v1"} 2' in metrics
    assert 'incident_guard_errors_total{service="shop-api",version="v1"} 1' in metrics


def test_compose_declares_three_services_healthchecks_and_network() -> None:
    compose = (SERVICE_PATH.parents[1] / "docker-compose.yml").read_text()

    for service_name in ("shop-api", "payment-service", "dependency-service"):
        assert f"  {service_name}:" in compose
    assert compose.count("condition: service_healthy") == 2
    assert "healthcheck:" in compose
    assert "incident-guard-lab" in compose
