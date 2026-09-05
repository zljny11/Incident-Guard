from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from incident_guard.lab import DockerLabController


LAB_DIR = Path(__file__).parents[1] / "lab"


def wait_container_unhealthy(
    controller: DockerLabController, timeout: float = 30
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if controller.container_health("payment-service") == "unhealthy":
            return
        time.sleep(0.5)
    raise AssertionError("payment-service did not become unhealthy")


@pytest.mark.docker
def test_transient_hang_times_out_and_restricted_restart_restores_chain() -> None:
    controller = DockerLabController(LAB_DIR)
    try:
        controller.reset()
        initial = controller.wait_healthy("shop-api")
        injected = controller.inject_transient_hang()

        assert initial["status"] == "healthy"
        assert injected == {
            "service": "payment-service",
            "fault": "transient_hang",
            "status": "injected",
        }
        with pytest.raises((TimeoutError, socket.timeout)):
            controller.query_health("payment-service", timeout=0.25)
        wait_container_unhealthy(controller)

        controller.restart_service("payment-service")

        assert controller.wait_healthy("payment-service")["status"] == "healthy"
        assert controller.wait_healthy("shop-api")["status"] == "healthy"
    finally:
        controller.down()
