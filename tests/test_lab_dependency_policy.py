from __future__ import annotations

import subprocess

from incident_guard.lab import DockerLabController


def test_dependency_outage_injection_only_stops_dependency_service(tmp_path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    commands = []

    def runner(command, cwd, timeout):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    controller = DockerLabController(tmp_path, runner=runner)

    controller.inject_dependency_outage()

    assert commands == [
        ("docker", "compose", "stop", "dependency-service")
    ]
