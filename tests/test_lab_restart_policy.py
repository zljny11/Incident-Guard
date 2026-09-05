from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from incident_guard.lab import DockerLabController


def test_restart_is_restricted_to_payment_service(tmp_path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    commands = []

    def runner(command, cwd: Path, timeout: float):
        commands.append((tuple(command), cwd, timeout))
        return subprocess.CompletedProcess(command, 0, "", "")

    controller = DockerLabController(tmp_path, runner=runner)

    controller.restart_service("payment-service")

    assert commands == [
        (("docker", "compose", "restart", "payment-service"), tmp_path, 60)
    ]


@pytest.mark.parametrize(
    "service_id", ["shop-api", "dependency-service", "../payment-service", ""]
)
def test_restart_rejects_non_allowlisted_service_without_running_command(
    tmp_path, service_id: str
) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    commands = []

    def runner(command, cwd, timeout):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    controller = DockerLabController(tmp_path, runner=runner)

    with pytest.raises(ValueError, match="not restartable"):
        controller.restart_service(service_id)

    assert commands == []
