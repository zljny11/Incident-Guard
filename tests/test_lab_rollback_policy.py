from __future__ import annotations

import subprocess

import pytest

from incident_guard.lab import DockerLabController


def controller_with_commands(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "payment-v1.env").write_text("PAYMENT_VERSION=v1\n")
    commands = []

    def runner(command, cwd, timeout):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    return DockerLabController(tmp_path, runner=runner), commands


def test_rollback_is_restricted_to_payment_v1(tmp_path) -> None:
    controller, commands = controller_with_commands(tmp_path)

    controller.rollback_service("payment-service", "v1")

    assert commands == [
        (
            "docker",
            "compose",
            "--env-file",
            str(tmp_path / "env" / "payment-v1.env"),
            "up",
            "--build",
            "--detach",
            "--force-recreate",
            "payment-service",
        )
    ]


def test_relative_lab_path_is_resolved_before_compose_changes_cwd(
    tmp_path, monkeypatch
) -> None:
    lab = tmp_path / "lab"
    lab.mkdir()
    (lab / "docker-compose.yml").write_text("services: {}\n")
    (lab / "env").mkdir()
    (lab / "env" / "payment-v1.env").write_text("PAYMENT_VERSION=v1\n")
    captured = {}

    def runner(command, cwd, timeout):
        captured.update(command=tuple(command), cwd=cwd, timeout=timeout)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.chdir(tmp_path)
    controller = DockerLabController("lab", runner=runner)
    controller.up(build=False)

    assert captured["cwd"] == lab
    assert captured["command"][3] == str(lab / "env" / "payment-v1.env")


@pytest.mark.parametrize(
    ("service_id", "target"),
    [
        ("payment-service", "v2"),
        ("payment-service", "latest"),
        ("shop-api", "v1"),
        ("../payment-service", "v1"),
    ],
)
def test_rollback_rejects_non_allowlisted_targets_without_command(
    tmp_path, service_id: str, target: str
) -> None:
    controller, commands = controller_with_commands(tmp_path)

    with pytest.raises(ValueError, match="not allowed"):
        controller.rollback_service(service_id, target)

    assert commands == []
