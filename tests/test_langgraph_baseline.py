from __future__ import annotations

import json

from incident_guard.baselines import (
    run_bad_deployment_baseline,
    write_langgraph_baseline_report,
)


def test_langgraph_approval_interrupt_precedes_single_verified_mutation():
    result = run_bad_deployment_baseline()

    assert result["interrupt_count"] == 1
    assert result["mutations_before_approval"] == 0
    assert result["mutation_count"] == 1
    assert result["tool_approval_count"] == 1
    assert result["root_cause"] == "bad_deployment"
    assert result["recovery_verified"] is True
    assert result["report"]["resolution"] == "rollback_service"


def test_langgraph_rejection_has_no_mutation_or_recovery_claim():
    result = run_bad_deployment_baseline(
        approved=False, thread_id="test-rejected"
    )

    assert result["interrupt_count"] == 1
    assert result["mutations_before_approval"] == 0
    assert result["mutation_count"] == 0
    assert result["tool_approval_count"] == 0
    assert result["recovery_verified"] is False
    assert result["report"]["resolution"] == "operator_rejected"


def test_langgraph_report_is_reproducible_and_machine_readable(tmp_path):
    report = write_langgraph_baseline_report(tmp_path)

    persisted = json.loads((tmp_path / "langgraph-baseline.json").read_text())
    assert persisted == report
    assert (tmp_path / "langgraph-baseline.md").is_file()
    assert "Mutations before approval" in (
        tmp_path / "langgraph-baseline.md"
    ).read_text()
