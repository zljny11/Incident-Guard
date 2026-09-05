from __future__ import annotations

import json

from starlette.testclient import TestClient

from incident_guard.events import NewRunEvent, SQLiteEventStore
from incident_guard.web import create_console_app


def _seed_completed(data_dir):
    store = SQLiteEventStore(data_dir / "events.db")
    store.append_batch(
        "run-completed",
        (
            NewRunEvent("run.started"),
            NewRunEvent(
                "alert.received", {"role": "user", "content": "payment alert"}
            ),
            NewRunEvent("turn.started", {"turn_number": 1}),
            NewRunEvent(
                "step.started", {"turn_number": 1, "step_number": 1}
            ),
            NewRunEvent(
                "assistant.message",
                {
                    "turn_number": 1,
                    "step_number": 1,
                    "text": "resolved",
                    "stop_reason": "end_turn",
                    "tool_calls": [],
                    "input_tokens": 10,
                    "output_tokens": 3,
                },
            ),
            NewRunEvent(
                "step.completed", {"turn_number": 1, "step_number": 1}
            ),
            NewRunEvent("run.completed"),
        ),
    )
    store.close()


def _seed_pending(data_dir):
    store = SQLiteEventStore(data_dir / "events.db")
    call = {
        "id": "rollback-1",
        "name": "rollback_service",
        "arguments": {"service_id": "payment-service", "target_version": "v1"},
    }
    store.append_batch(
        "run-pending",
        (
            NewRunEvent("run.started"),
            NewRunEvent("turn.started", {"turn_number": 1}),
            NewRunEvent(
                "step.started", {"turn_number": 1, "step_number": 1}
            ),
            NewRunEvent(
                "assistant.message",
                {
                    "turn_number": 1,
                    "step_number": 1,
                    "text": "rollback proposed",
                    "stop_reason": "tool_use",
                    "tool_calls": [call],
                    "input_tokens": 10,
                    "output_tokens": 3,
                },
            ),
            NewRunEvent(
                "tool.requested",
                {
                    "call_id": "rollback-1",
                    "name": "rollback_service",
                    "arguments": call["arguments"],
                    "effect": "mutate",
                    "call_index": 0,
                },
            ),
            NewRunEvent(
                "approval.requested",
                {
                    "request_id": "approval:rollback-1",
                    "call_id": "rollback-1",
                    "reason": "operator approval required",
                },
            ),
        ),
    )
    store.close()


def test_console_lists_runs_detail_evals_and_html_pages(tmp_path):
    data_dir = tmp_path / "data"
    eval_dir = tmp_path / "evals"
    eval_dir.mkdir()
    (eval_dir / "real-model-eval.json").write_text(
        json.dumps({"aggregate": {"pass_rate": 1.0, "run_count": 15}})
    )
    _seed_completed(data_dir)
    app = create_console_app(data_dir, tmp_path / "lab", eval_dir)

    with TestClient(app) as client:
        assert client.get("/").history[0].status_code == 307
        assert "Durable Runs" in client.get("/runs").text
        assert "Evaluation Evidence" in client.get("/evals").text
        runs = client.get("/api/runs").json()
        assert [item["run_id"] for item in runs] == ["run-completed"]
        detail = client.get("/api/runs/run-completed").json()
        assert detail["run"]["status"] == "completed"
        assert detail["timeline"][-1]["event_type"] == "run.completed"
        reports = client.get("/api/evals").json()
        assert reports[0]["report"]["aggregate"]["run_count"] == 15


def test_console_approval_uses_application_service_and_sse_replays(tmp_path):
    data_dir = tmp_path / "data"
    _seed_completed(data_dir)
    _seed_pending(data_dir)
    app = create_console_app(data_dir, tmp_path / "lab", tmp_path / "evals")

    with TestClient(app) as client:
        response = client.post(
            "/api/runs/run-pending/approve/rollback-1",
            json={"reason": "reviewed in console"},
        )
        assert response.status_code == 200
        assert response.json()["approvals"] == [
            {
                "call_id": "rollback-1",
                "request_id": "approval:rollback-1",
                "status": "approved",
            }
        ]

        with client.stream("GET", "/api/runs/run-completed/events") as stream:
            body = "".join(stream.iter_text())
        assert '"event_type":"run.completed"' in body
        assert "event: done" in body
