from __future__ import annotations

import time
from pathlib import Path

import pytest

from incident_guard.lab import DockerLabController


LAB_DIR = Path(__file__).parents[1] / "lab"


def wait_for_version(
    controller: DockerLabController,
    version: str,
    status: str,
    timeout: float = 45,
) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = controller.query_health("payment-service")
            if last.get("version") == version and last.get("status") == status:
                return last
        except OSError:
            pass
        time.sleep(0.25)
    raise AssertionError(f"payment did not reach {version}/{status}: {last}")


@pytest.mark.docker
def test_bad_deployment_is_real_and_rollback_restores_payment_v1() -> None:
    controller = DockerLabController(LAB_DIR)
    try:
        controller.reset()
        assert controller.wait_healthy("payment-service")["version"] == "v1"

        controller.deploy_bad_deployment()
        failed = wait_for_version(controller, "v2", "unhealthy")

        assert failed["fault"] == "bad_deployment"
        assert failed["error_rate"] == 0.42
        assert controller.image_exists("incident-guard/payment-service:v1")
        assert controller.image_exists("incident-guard/payment-service:v2")

        controller.rollback_service("payment-service", "v1")

        recovered = controller.wait_healthy("payment-service")
        assert recovered["version"] == "v1"
        assert recovered["status"] == "healthy"
        assert controller.wait_healthy("shop-api")["status"] == "healthy"
    finally:
        controller.down()
