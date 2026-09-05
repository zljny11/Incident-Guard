from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from incident_guard.agents.provider import (
    ProviderError,
    ProviderEventType,
    StopReason,
    StreamingProvider,
    ToolCall,
)
from incident_guard.agents.run_models import (
    AgentRun,
    RunStatus,
    StepResult,
    ToolObservation,
    TurnResult,
)
from incident_guard.agents.goal_gate import GoalGate, GoalGateDecision


class ToolExecutor(Protocol):
    async def execute(self, call: ToolCall) -> ToolObservation:
        """执行一个结构化工具调用并返回可回灌的观察。"""


FakeToolValue = (
    str
    | ToolObservation
    | Exception
    | Callable[[ToolCall], str | ToolObservation | Awaitable[str | ToolObservation]]
)


class FakeToolExecutor:
    """按 Tool Call ID（其次按工具名）返回预设结果的测试执行器。"""

    def __init__(self, responses: Mapping[str, FakeToolValue]) -> None:
        self._responses = dict(responses)
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall) -> ToolObservation:
        self.calls.append(call)
        if call.id in self._responses:
            value = self._responses[call.id]
        elif call.name in self._responses:
            value = self._responses[call.name]
        else:
            raise ProviderError(f"No fake tool response configured for {call.id}")

        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(call)
            if inspect.isawaitable(value):
                value = await value
        if isinstance(value, ToolObservation):
            if value.call_id != call.id or value.name != call.name:
                raise ProviderError(
                    "Fake tool observation does not match the requested ToolCall"
                )
            return value
        if not isinstance(value, str):
            raise ProviderError("Fake tool response must resolve to a string or observation")
        return ToolObservation(call.id, call.name, value)


@dataclass(frozen=True, slots=True)
class RunLimits:
    max_steps: int = 10
    timeout_seconds: float = 30.0
    max_tool_calls: int = 32
    max_tokens: int = 100_000

    def __post_init__(self) -> None:
        if type(self.max_steps) is not int or self.max_steps < 1:
            raise ValueError("max_steps must be a positive int")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be greater than zero")
        for name, value in (
            ("max_tool_calls", self.max_tool_calls),
            ("max_tokens", self.max_tokens),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative int")


class StructuredAgentRuntime:
    """执行异步 model -> tools -> observations -> model ReAct Loop。"""

    def __init__(
        self,
        provider: StreamingProvider,
        tool_executor: ToolExecutor,
        *,
        limits: RunLimits | None = None,
        goal_gate: GoalGate | None = None,
    ) -> None:
        self.provider = provider
        self.tool_executor = tool_executor
        self.limits = limits or RunLimits()
        self.goal_gate = goal_gate

    async def run(self, run_id: str, messages: list[dict]) -> AgentRun:
        run = AgentRun(run_id).transition(RunStatus.RUNNING)
        context = [dict(message) for message in messages]
        steps: list[StepResult] = []
        tool_call_count = 0
        token_count = 0

        try:
            async with asyncio.timeout(self.limits.timeout_seconds):
                for _ in range(self.limits.max_steps):
                    step_number = len(steps) + 1
                    response = await self._generate_response(context)
                    if response.usage is not None:
                        token_count += response.usage.total_tokens
                    if token_count > self.limits.max_tokens:
                        steps.append(StepResult(step_number, response))
                        return self._fail(
                            run,
                            steps,
                            f"token budget exceeded: {token_count} > {self.limits.max_tokens}",
                        )

                    if response.stop_reason is StopReason.TOOL_USE:
                        requested_count = len(response.tool_calls)
                        if tool_call_count + requested_count > self.limits.max_tool_calls:
                            steps.append(StepResult(step_number, response))
                            return self._fail(
                                run,
                                steps,
                                "tool call budget exceeded: "
                                f"{tool_call_count + requested_count} > "
                                f"{self.limits.max_tool_calls}",
                            )

                        execute_batch = getattr(self.tool_executor, "execute_batch", None)
                        if execute_batch is not None:
                            observations = await execute_batch(response.tool_calls)
                        else:
                            observations = []
                            for call in response.tool_calls:
                                observations.append(await self.tool_executor.execute(call))
                        tool_call_count += requested_count
                        step = StepResult(step_number, response, observations)
                        steps.append(step)
                        self._append_step_context(context, step)
                        continue

                    steps.append(StepResult(step_number, response))
                    if response.stop_reason is StopReason.END_TURN:
                        if self.goal_gate is not None:
                            decision = self.goal_gate.evaluate(run_id, tuple(steps))
                            if inspect.isawaitable(decision):
                                decision = await decision
                            if not isinstance(decision, GoalGateDecision):
                                raise TypeError("Goal gate must return GoalGateDecision")
                            if not decision.allowed:
                                run = run.append_turn(
                                    TurnResult(len(run.turns) + 1, steps)
                                )
                                self._append_goal_gate_context(
                                    context, response.text, decision.feedback
                                )
                                steps = []
                                continue
                        run = run.append_turn(
                            TurnResult(len(run.turns) + 1, steps)
                        )
                        return run.transition(RunStatus.COMPLETED)
                    run = run.append_turn(TurnResult(len(run.turns) + 1, steps))
                    return run.transition(
                        RunStatus.FAILED,
                        failure_reason="provider stopped because max_tokens was reached",
                    )

                return self._fail(
                    run,
                    steps,
                    f"step budget exhausted after {self.limits.max_steps} steps",
                )
        except TimeoutError:
            return self._fail(
                run,
                steps,
                f"run timeout exceeded after {self.limits.timeout_seconds:g} seconds",
            )
        except Exception as error:
            return self._fail(run, steps, f"runtime failure: {error}")

    async def _generate_response(self, context: list[dict]):
        completed_response = None
        async for event in self.provider.stream(context):
            if completed_response is not None:
                raise ProviderError("Provider emitted events after completed")
            if event.event_type is ProviderEventType.COMPLETED:
                completed_response = event.response
        if completed_response is None:
            raise ProviderError("Provider stream ended without completed event")
        return completed_response

    @staticmethod
    def _append_goal_gate_context(
        context: list[dict], response_text: str, feedback: str
    ) -> None:
        context.append({"role": "assistant", "content": response_text})
        context.append({"role": "system", "content": feedback})

    @staticmethod
    def _append_step_context(context: list[dict], step: StepResult) -> None:
        response = step.response
        context.append(
            {
                "role": "assistant",
                "content": response.text,
                "tool_calls": [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": dict(call.arguments),
                    }
                    for call in response.tool_calls
                ],
            }
        )
        for observation in step.observations:
            context.append(
                {
                    "role": "tool",
                    "tool_call_id": observation.call_id,
                    "name": observation.name,
                    "content": observation.content,
                    "is_error": observation.is_error,
                }
            )

    @staticmethod
    def _fail(
        run: AgentRun, steps: list[StepResult], failure_reason: str
    ) -> AgentRun:
        if steps:
            run = run.append_turn(TurnResult(len(run.turns) + 1, steps))
        return run.transition(RunStatus.FAILED, failure_reason=failure_reason)
