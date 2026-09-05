from __future__ import annotations

import json
from pathlib import Path

import pytest

from incident_guard.cli import main
from incident_guard.lab import DockerLabController


LAB_DIR = Path(__file__).parents[1] / "lab"


@pytest.mark.docker
def test_real_cli_bad_deployment_approval_and_recovery(tmp_path, capsys) -> None:
    data_dir = tmp_path / "data"
    common = ["--data-dir", str(data_dir), "--lab-dir", str(LAB_DIR)]
    alert = tmp_path / "alert.json"
    alert.write_text(
        json.dumps(
            {
                "service": "payment-service",
                "summary": "payment error rate above 30%",
            }
        )
    )
    controller = DockerLabController(LAB_DIR)
    try:
        assert main([*common, "lab", "reset"]) == 0
        assert main([*common, "inject", "bad_deployment"]) == 0
        assert main(
            [
                *common,
                "investigate",
                "--alert",
                str(alert),
                "--run-id",
                "run-real-cli",
            ]
        ) == 0
        waiting = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert waiting["status"] == "waiting_approval"

        assert main(
            [*common, "approve", "run-real-cli", "rollback_service-1"]
        ) == 0
        assert main([*common, "resume", "run-real-cli"]) == 0
        completed = json.loads(capsys.readouterr().out.splitlines()[-1])

        assert completed["status"] == "completed"
        assert controller.wait_healthy("payment-service")["version"] == "v1"
        assert controller.wait_healthy("shop-api")["status"] == "healthy"
    finally:
        controller.down()
