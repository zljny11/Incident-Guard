from __future__ import annotations

import json

from incident_guard.cli import main


class FakeDockerLabController:
    version = "v1"
    status = "healthy"

    def __init__(self, _lab_dir):
        pass

    def up(self):
        type(self).version = "v1"
        type(self).status = "healthy"

    def down(self):
        type(self).status = "stopped"

    def reset(self):
        self.up()

    def deploy_bad_deployment(self):
        type(self).version = "v2"
        type(self).status = "unhealthy"

    def inject_transient_hang(self):
        type(self).status = "unhealthy"
        return {"fault": "transient_hang"}

    def inject_dependency_outage(self):
        type(self).status = "unhealthy"

    def restart_service(self, service_id):
        assert service_id == "payment-service"
        type(self).status = "healthy"

    def rollback_service(self, service_id, target_version):
        assert (service_id, target_version) == ("payment-service", "v1")
        type(self).version = "v1"
        type(self).status = "healthy"

    def query_health(self, service_id):
        return {
            "service": service_id,
            "status": type(self).status,
            "version": type(self).version,
        }

    def wait_healthy(self, service_id):
        payload = self.query_health(service_id)
        assert payload["status"] == "healthy"
        return payload


def invoke(tmp_path, *arguments):
    return main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "--lab-dir",
            str(tmp_path / "lab"),
            *arguments,
        ]
    )


def stub_mcp(monkeypatch):
    def reads(_service, scenario):
        return {
            "query_service_health": {"status": "unhealthy", "version": "v2"},
            "query_metrics": {"error_rate": 0.42},
            "query_logs": {"error": scenario.value},
            "get_recent_deployments": {"version": "v2"},
            "read_runbook": {"recommended_action": "rollback_service"},
        }

    def recover(_service, tool, _scenario):
        controller = FakeDockerLabController(None)
        if tool.name == "rollback_service":
            controller.rollback_service("payment-service", "v1")
        else:
            controller.restart_service("payment-service")
        return {"action": tool.name, "status": "completed"}

    monkeypatch.setattr(
        "incident_guard.incident_cli.IncidentCLIService._collect_read_results", reads
    )
    monkeypatch.setattr(
        "incident_guard.incident_cli.IncidentCLIService._perform_recovery", recover
    )
    monkeypatch.setattr(
        "incident_guard.incident_cli.IncidentCLIService._verify_recovery",
        lambda _service, _scenario: {
            "verified": True,
            "health": {
                "service": "payment-service",
                "status": "healthy",
                "version": "v1",
            },
        },
    )


def test_bad_deployment_cli_recording_flow_is_repeatable(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        "incident_guard.incident_cli.DockerLabController",
        FakeDockerLabController,
    )
    stub_mcp(monkeypatch)
    alert = tmp_path / "alert.json"
    alert.write_text(json.dumps({"service": "payment-service", "error_rate": 0.42}))

    assert invoke(tmp_path, "lab", "up") == 0
    assert invoke(tmp_path, "lab", "inject", "bad_deployment") == 0
    assert invoke(
        tmp_path,
        "investigate",
        "--alert",
        str(alert),
        "--run-id",
        "run-demo",
    ) == 0
    waiting = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert waiting["status"] == "waiting_approval"

    assert invoke(tmp_path, "status", "run-demo") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["approvals"] == [
        {
            "call_id": "rollback_service-1",
            "request_id": "approval:rollback_service-1",
            "status": "pending",
        }
    ]
    assert status["evidence_count"] == 5

    assert invoke(tmp_path, "approve", "run-demo", "rollback_service-1") == 0
    assert invoke(tmp_path, "resume", "run-demo") == 0
    completed = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert completed["status"] == "completed"
    assert completed["evidence_count"] == 6
    assert FakeDockerLabController.version == "v1"
    assert completed["tools"][-1]["name"] == "verify_recovery"
    assert completed["tools"][-1]["state"] == "completed"


def test_reject_and_cancel_are_durable_terminal_decisions(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        "incident_guard.incident_cli.DockerLabController",
        FakeDockerLabController,
    )
    stub_mcp(monkeypatch)
    alert = tmp_path / "alert.json"
    alert.write_text("{}")
    invoke(tmp_path, "inject", "bad_deployment")
    invoke(
        tmp_path,
        "investigate",
        "--alert",
        str(alert),
        "--run-id",
        "run-reject",
    )
    assert invoke(tmp_path, "reject", "run-reject", "rollback_service-1") == 0
    rejected = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert rejected["status"] == "failed"

    invoke(
        tmp_path,
        "investigate",
        "--alert",
        str(alert),
        "--scenario",
        "transient_hang",
        "--run-id",
        "run-cancel",
    )
    assert invoke(tmp_path, "cancel", "run-cancel") == 0
    cancelled = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert cancelled["status"] == "cancelled"
