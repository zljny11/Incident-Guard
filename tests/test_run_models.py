from __future__ import annotations

import pytest

from incident_guard.agents.provider import ProviderResponse
from incident_guard.agents.run_models import (
    AgentRun,
    RunStatus,
    StepResult,
    ToolObservation,
    TurnResult,
)


def test_run_turn_and_step_results_are_immutable_structured_values() -> None:
    response = ProviderResponse(text="resolved")
    observation = ToolObservation("call-1", "query_logs", "no errors")
    step = StepResult(1, response, [observation])
    turn = TurnResult(1, [step])
    run = AgentRun("run-1").transition(RunStatus.RUNNING).append_turn(turn)

    assert run.turns == (turn,)
    assert turn.steps == (step,)
    assert step.observations == (observation,)
    assert turn.final_response is response

    with pytest.raises(AttributeError):
        run.status = RunStatus.COMPLETED


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RunStatus.CREATED, RunStatus.RUNNING),
        (RunStatus.CREATED, RunStatus.CANCELLED),
        (RunStatus.RUNNING, RunStatus.WAITING_APPROVAL),
        (RunStatus.RUNNING, RunStatus.CANCELLING),
        (RunStatus.RUNNING, RunStatus.COMPLETED),
        (RunStatus.WAITING_APPROVAL, RunStatus.RUNNING),
        (RunStatus.CANCELLING, RunStatus.CANCELLED),
    ],
)
def test_lifecycle_allows_documented_transitions(source, target) -> None:
    kwargs = {"failure_reason": "failed"} if source.is_terminal else {}
    run = AgentRun("run-1", status=source, **kwargs)

    assert run.transition(target).status is target


@pytest.mark.parametrize("terminal", list(status for status in RunStatus if status.is_terminal))
def test_terminal_states_reject_all_transitions(terminal) -> None:
    kwargs = (
        {"failure_reason": "failed"}
        if terminal in {RunStatus.FAILED, RunStatus.FAILED_UNCERTAIN}
        else {}
    )
    run = AgentRun("run-1", status=terminal, **kwargs)

    for target in RunStatus:
        with pytest.raises(ValueError, match="Illegal AgentRun transition"):
            run.transition(target)


def test_failed_transition_requires_a_reason() -> None:
    run = AgentRun("run-1").transition(RunStatus.RUNNING)

    with pytest.raises(ValueError, match="requires failure_reason"):
        run.transition(RunStatus.FAILED)

    failed = run.transition(RunStatus.FAILED, failure_reason="provider failed")
    assert failed.failure_reason == "provider failed"


def test_turns_can_only_be_appended_in_order_while_running() -> None:
    created = AgentRun("run-1")

    with pytest.raises(ValueError, match="only be appended while.*running"):
        created.append_turn(TurnResult(1))

    running = created.transition(RunStatus.RUNNING)
    with pytest.raises(ValueError, match="Expected turn_number 1"):
        running.append_turn(TurnResult(2))
