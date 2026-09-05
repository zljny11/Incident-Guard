from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Any

from incident_guard.agents.provider import (
    ProviderError,
    ProviderEventType,
    ProviderResponse,
    ProviderUsage,
    StopReason,
    StreamingProvider,
    ToolCall,
)
from incident_guard.agents.react_runtime import RunLimits, ToolExecutor
from incident_guard.agents.tool_pipeline import (
    ApprovalDecision,
    PolicyAction,
    PolicyDecision,
)
from incident_guard.agents.run_models import RunStatus, ToolObservation
from incident_guard.context.artifact_store import FileToolResultStore
from incident_guard.context.context_engine import (
    ContextBudgetPolicy,
    EventContextProjector,
)
from incident_guard.events import (
    DurableRunInbox,
    EventStore,
    InboxTarget,
    LiveEvent,
    LiveEventBroker,
    NewRunEvent,
    ProjectionError,
    RunEvent,
    RunEventProjector,
    RunProjection,
    ToolState,
)


class CrashInjected(RuntimeError):
    """Test-only process-crash signal; it deliberately bypasses failure events."""


FaultInjector = Callable[[RunEvent], None]


class DurableApprovalProvider:
    """Resolve a Tool Pipeline approval from the durable run event stream."""

    def __init__(self, event_store: EventStore, run_id: str) -> None:
        self.event_store = event_store
        self.run_id = run_id

    def request_approval(self, request) -> ApprovalDecision:
        projection = RunEventProjector().project(
            self.run_id, self.event_store.replay(self.run_id)
        )
        tool = projection.tools.get(request.call.id)
        if tool is None or tool.name != request.call.name or tool.arguments != request.call.arguments:
            raise ValueError("durable approval does not match the requested tool call")
        if request.request_id != f"approval:{request.call.id}":
            raise ValueError("durable approval request id does not match the tool call")

        requested = False
        decision_payload: Mapping[str, Any] | None = None
        for event in self.event_store.replay(self.run_id):
            if (
                event.event_type == "approval.requested"
                and event.payload.get("request_id") == request.request_id
                and event.payload.get("call_id") == request.call.id
            ):
                requested = True
            elif (
                event.event_type == "approval.decided"
                and event.payload.get("request_id") == request.request_id
            ):
                decision_payload = event.payload
        if not requested or decision_payload is None:
            raise RuntimeError("tool call has no durable approval decision")
        approved = decision_payload.get("approved")
        if type(approved) is not bool:
            raise ValueError("durable approval decision must contain a bool")
        return ApprovalDecision(
            request.request_id,
            approved,
            str(decision_payload.get("reason", "")),
        )


def decide_durable_approval(
    event_store: EventStore,
    run_id: str,
    call_id: str,
    *,
    approved: bool,
    reason: str,
    projector: RunEventProjector | None = None,
) -> RunProjection:
    """Persist an operator decision bound to one pending tool call."""

    if type(approved) is not bool:
        raise ValueError("approved must be a bool")
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")
    resolved_projector = projector or RunEventProjector()
    events = event_store.replay(run_id)
    if not events:
        raise ValueError(f"run does not exist: {run_id}")
    projection = resolved_projector.project(run_id, events)
    if projection.status is not RunStatus.WAITING_APPROVAL:
        raise ValueError("run is not waiting for approval")
    request_id = f"approval:{call_id}"
    pending = any(
        event.event_type == "approval.requested"
        and event.payload.get("request_id") == request_id
        and event.payload.get("call_id") == call_id
        for event in events
    )
    if not pending:
        raise ValueError(f"no pending approval for call: {call_id}")
    event_store.append(
        run_id,
        NewRunEvent(
            "approval.decided",
            {"request_id": request_id, "approved": approved, "reason": reason},
        ),
    )
    if not approved:
        event_store.append(
            run_id,
            NewRunEvent("run.failed", {"reason": f"operator rejected {call_id}"}),
        )
    return resolved_projector.project(run_id, event_store.replay(run_id))


class EventDrivenAgentRuntime:
    """Recoverable structured loop backed by durable events."""

    def __init__(
        self,
        provider: StreamingProvider,
        tool_executor: ToolExecutor,
        event_store: EventStore,
        *,
        limits: RunLimits | None = None,
        projector: RunEventProjector | None = None,
        inbox: DurableRunInbox | None = None,
        live_events: LiveEventBroker | None = None,
        fault_injector: FaultInjector | None = None,
        tool_result_store: FileToolResultStore | None = None,
        context_token_budget: int | None = None,
        context_projector: EventContextProjector | None = None,
    ) -> None:
        self.provider = provider
        self.tool_executor = tool_executor
        self.event_store = event_store
        self.limits = limits or RunLimits()
        self.projector = projector or RunEventProjector()
        self.inbox = inbox or DurableRunInbox(event_store, self.projector)
        self.live_events = live_events
        self.fault_injector = fault_injector
        self.tool_result_store = tool_result_store
        if context_token_budget is not None and (
            type(context_token_budget) is not int or context_token_budget < 1
        ):
            raise ValueError("context_token_budget must be a positive int or None")
        self.context_token_budget = context_token_budget
        self.context_projector = context_projector or EventContextProjector()
        self.context_policy = ContextBudgetPolicy(self.context_projector.estimator)

    def project(self, run_id: str) -> RunProjection:
        return self.projector.project(run_id, self.event_store.replay(run_id))

    async def run(
        self, run_id: str, messages: list[dict[str, Any]]
    ) -> RunProjection:
        if self.event_store.replay(run_id):
            raise ValueError(f"run already exists: {run_id}")
        initial_events = [NewRunEvent("run.started")]
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if not isinstance(role, str) or not role.strip():
                raise ValueError("initial message role must be non-empty")
            if not isinstance(content, str):
                raise ValueError("initial message content must be a string")
            initial_events.append(
                NewRunEvent(
                    "operator.message", {"role": role, "content": content}
                )
            )
        initial_events.append(NewRunEvent("turn.started", {"turn_number": 1}))
        stored = self.event_store.append_batch(run_id, initial_events)
        for event in stored:
            self._after_durable_event(event)
        await self._emit(run_id, "runtime.status", {"status": "running"})
        return await self._drive_and_close(run_id)

    async def resume(self, run_id: str) -> RunProjection:
        if not self.event_store.replay(run_id):
            raise ValueError(f"run does not exist: {run_id}")
        projection = self.project(run_id)
        if projection.status.is_terminal:
            return projection
        await self._emit(run_id, "runtime.status", {"status": "resuming"})
        return await self._drive_and_close(run_id)

    def cancel(self, run_id: str) -> RunProjection:
        if not self.event_store.replay(run_id):
            raise ValueError(f"run does not exist: {run_id}")
        projection = self.project(run_id)
        if projection.status.is_terminal:
            return projection
        if projection.status is RunStatus.CANCELLING:
            return projection
        self._append(run_id, "run.cancelling", {})
        return self.project(run_id)

    async def _drive_and_close(self, run_id: str) -> RunProjection:
        try:
            projection = await self._drive(run_id)
        except CrashInjected:
            if self.live_events is not None:
                self.live_events.close_run(run_id)
            raise
        if projection.status.is_terminal and self.live_events is not None:
            self.live_events.close_run(run_id)
        return projection

    async def _drive(self, run_id: str) -> RunProjection:
        try:
            async with asyncio.timeout(self.limits.timeout_seconds):
                while True:
                    projection = self.project(run_id)
                    if projection.status.is_terminal:
                        return projection
                    if projection.status is RunStatus.WAITING_APPROVAL:
                        return projection
                    if projection.status is RunStatus.CANCELLING:
                        return await self._finish_cancellation(run_id, projection)

                    uncertain = self._uncertain_mutation(projection)
                    if uncertain is not None:
                        return await self._fail_uncertain(run_id, uncertain.call_id)

                    settled = await self._settle_completed_step(run_id, projection)
                    if settled is not None:
                        if settled.status.is_terminal:
                            return settled
                        continue

                    projection = self.project(run_id)
                    if projection.open_step is None:
                        if len(projection.completed_steps) >= self.limits.max_steps:
                            return await self._fail(
                                run_id,
                                "step budget exhausted after "
                                f"{self.limits.max_steps} steps",
                            )
                        self.inbox.consume(run_id, InboxTarget.NEXT_STEP)
                        projection = self.project(run_id)
                        step_number = 1 + sum(
                            turn == projection.turn_number
                            for turn, _ in projection.completed_steps
                        )
                        self._append(
                            run_id,
                            "step.started",
                            {
                                "turn_number": projection.turn_number,
                                "step_number": step_number,
                            },
                        )
                        projection = self.project(run_id)

                    assistant = projection.current_assistant
                    if assistant is None:
                        response = await self._generate_response(
                            run_id, self._provider_context(run_id, projection)
                        )
                        if self.project(run_id).status is RunStatus.CANCELLING:
                            continue
                        self._persist_response(run_id, projection, response)
                        projection = self.project(run_id)
                        assistant = projection.current_assistant
                        assert assistant is not None

                    budget_failure = self._budget_failure(projection)
                    if budget_failure is not None:
                        return await self._fail(run_id, budget_failure)

                    if assistant.stop_reason == StopReason.TOOL_USE.value:
                        self._ensure_tool_requests(run_id, projection, assistant)
                        projection = self.project(run_id)
                        await self._execute_current_tools(run_id, projection)
                        projection = self.project(run_id)
                        if projection.status.is_terminal:
                            return projection
                        if projection.status is RunStatus.WAITING_APPROVAL:
                            return projection
                        if projection.status is RunStatus.CANCELLING:
                            continue
                    self._append(
                        run_id,
                        "step.completed",
                        {
                            "turn_number": projection.turn_number,
                            "step_number": projection.current_step_number,
                        },
                    )
        except CrashInjected:
            raise
        except TimeoutError:
            projection = self.project(run_id)
            uncertain = self._uncertain_mutation(projection)
            if uncertain is not None:
                return await self._fail_uncertain(run_id, uncertain.call_id)
            return await self._fail(
                run_id,
                f"run timeout exceeded after {self.limits.timeout_seconds:g} seconds",
            )
        except Exception as error:
            projection = self.project(run_id)
            uncertain = self._uncertain_mutation(projection)
            if uncertain is not None:
                return await self._fail_uncertain(run_id, uncertain.call_id)
            return await self._fail(run_id, f"runtime failure: {error}")

    async def _settle_completed_step(
        self, run_id: str, projection: RunProjection
    ) -> RunProjection | None:
        if projection.current_step_number is None:
            return None
        key = (projection.turn_number, projection.current_step_number)
        if key not in projection.completed_steps:
            return None
        assistant = projection.current_assistant
        if assistant is None:
            raise RuntimeError("completed step has no assistant response")
        if assistant.stop_reason == StopReason.TOOL_USE.value:
            return None
        if assistant.stop_reason == StopReason.END_TURN.value:
            pending_next_turn = any(
                not item.consumed and item.target == InboxTarget.NEXT_TURN.value
                for item in projection.inbox_items
            )
            if pending_next_turn:
                self._append(
                    run_id,
                    "turn.started",
                    {"turn_number": projection.turn_number + 1},
                )
                self.inbox.consume(run_id, InboxTarget.NEXT_TURN)
                return self.project(run_id)
            self._append(run_id, "run.completed", {})
            await self._emit(run_id, "runtime.status", {"status": "completed"})
            return self.project(run_id)
        return await self._fail(
            run_id, "provider stopped because max_tokens was reached"
        )

    async def _generate_response(
        self, run_id: str, context: list[dict[str, Any]]
    ) -> ProviderResponse:
        completed_response: ProviderResponse | None = None
        async for event in self.provider.stream(context):
            if completed_response is not None:
                raise ProviderError("Provider emitted events after completed")
            if event.event_type is ProviderEventType.TEXT_DELTA:
                await self._emit(
                    run_id, "assistant.delta", {"text": event.text or ""}
                )
            elif event.event_type is ProviderEventType.TOOL_CALL:
                call = event.call
                assert call is not None
                await self._emit(
                    run_id,
                    "tool.progress",
                    {"phase": "requested", "call_id": call.id, "name": call.name},
                )
            elif event.event_type is ProviderEventType.COMPLETED:
                completed_response = event.response
        if completed_response is None:
            raise ProviderError("Provider stream ended without completed event")
        return completed_response

    def _persist_response(
        self,
        run_id: str,
        projection: RunProjection,
        response: ProviderResponse,
    ) -> None:
        usage = response.usage
        payload = {
            "turn_number": projection.turn_number,
            "step_number": projection.current_step_number,
            "text": response.text,
            "stop_reason": response.stop_reason.value,
            "tool_calls": [self._serialize_call(call) for call in response.tool_calls],
            "input_tokens": usage.input_tokens if usage is not None else None,
            "output_tokens": usage.output_tokens if usage is not None else None,
        }
        self._append(run_id, "assistant.message", payload)

    def _ensure_tool_requests(
        self,
        run_id: str,
        projection: RunProjection,
        assistant: Any,
    ) -> None:
        existing = set(projection.tools)
        for index, serialized in enumerate(assistant.tool_calls):
            call_id = serialized.get("id")
            if call_id in existing:
                continue
            call = self._deserialize_call(serialized)
            self._append(
                run_id,
                "tool.requested",
                {
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "effect": self._tool_effect(call),
                    "call_index": index,
                },
            )

    async def _execute_current_tools(
        self, run_id: str, projection: RunProjection
    ) -> None:
        key = (projection.turn_number, projection.current_step_number)
        tools = sorted(
            (
                tool
                for tool in projection.tools.values()
                if (tool.turn_number, tool.step_number) == key
            ),
            key=lambda item: item.call_index,
        )
        for tool in tools:
            if tool.state in {ToolState.COMPLETED, ToolState.FAILED}:
                continue
            if self.project(run_id).status is RunStatus.CANCELLING:
                return
            call = ToolCall(tool.call_id, tool.name, dict(tool.arguments))
            if tool.state is ToolState.REQUESTED:
                preflight = await self._preflight(call)
                if isinstance(preflight, ToolObservation):
                    self._append(
                        run_id,
                        "tool.started",
                        {
                            "call_id": call.id,
                            "name": call.name,
                            "effect": tool.effect,
                        },
                    )
                    self._append(
                        run_id,
                        "tool.failed",
                        {
                            "call_id": call.id,
                            "name": call.name,
                            "is_error": True,
                            **self._tool_result_payload(preflight),
                        },
                    )
                    continue
                approval_required = tool.effect == "mutate" or (
                    isinstance(preflight, PolicyDecision)
                    and preflight.action is PolicyAction.ASK
                )
                if approval_required:
                    approval = self._durable_approval(run_id, call.id)
                    if approval is None:
                        request_id = f"approval:{call.id}"
                        reason = (
                            preflight.reason
                            if isinstance(preflight, PolicyDecision)
                            and preflight.reason
                            else f"operator approval required for {call.name}"
                        )
                        self._append(
                            run_id,
                            "approval.requested",
                            {
                                "request_id": request_id,
                                "call_id": call.id,
                                "reason": reason,
                            },
                        )
                        await self._emit(
                            run_id,
                            "runtime.status",
                            {
                                "status": "waiting_approval",
                                "request_id": request_id,
                                "call_id": call.id,
                            },
                        )
                        return
                    if approval is False:
                        await self._fail(run_id, f"operator rejected {call.id}")
                        return
                self._append(
                    run_id,
                    "tool.started",
                    {
                        "call_id": call.id,
                        "name": call.name,
                        "effect": tool.effect,
                    },
                )
            elif tool.effect == "mutate":
                await self._fail_uncertain(run_id, call.id)
                return

            await self._emit(
                run_id,
                "tool.progress",
                {"phase": "started", "call_id": call.id, "name": call.name},
            )
            try:
                observation = await self.tool_executor.execute(call)
                if not isinstance(observation, ToolObservation):
                    raise TypeError("tool executor must return ToolObservation")
            except Exception as error:
                if tool.effect == "mutate":
                    await self._fail_uncertain(run_id, call.id)
                    return
                observation = ToolObservation(
                    call.id,
                    call.name,
                    json.dumps(
                        {"error": {"code": "execution_failed", "message": str(error)}},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    is_error=True,
                )
            event_type = "tool.failed" if observation.is_error else "tool.completed"
            if (
                tool.effect == "mutate"
                and observation.is_error
                and self._observation_error_code(observation)
                in {"execution_failed", "timeout"}
            ):
                await self._fail_uncertain(run_id, call.id)
                return
            result_payload = self._tool_result_payload(observation)
            self._append(
                run_id,
                event_type,
                {
                    "call_id": call.id,
                    "name": call.name,
                    "is_error": observation.is_error,
                    **result_payload,
                },
            )
            await self._emit(
                run_id,
                "tool.progress",
                {"phase": "completed", "call_id": call.id, "name": call.name},
            )

    def decide_approval(
        self,
        run_id: str,
        call_id: str,
        *,
        approved: bool,
        reason: str,
    ) -> RunProjection:
        return decide_durable_approval(
            self.event_store,
            run_id,
            call_id,
            approved=approved,
            reason=reason,
            projector=self.projector,
        )

    def _durable_approval(self, run_id: str, call_id: str) -> bool | None:
        request_id = f"approval:{call_id}"
        requested = False
        for event in self.event_store.replay(run_id):
            if (
                event.event_type == "approval.requested"
                and event.payload.get("request_id") == request_id
                and event.payload.get("call_id") == call_id
            ):
                requested = True
            elif (
                requested
                and event.event_type == "approval.decided"
                and event.payload.get("request_id") == request_id
            ):
                approved = event.payload.get("approved")
                if type(approved) is not bool:
                    raise ProjectionError("approval decision must contain a bool")
                return approved
        return None

    async def _preflight(
        self, call: ToolCall
    ) -> PolicyDecision | ToolObservation | None:
        preflight = getattr(self.tool_executor, "preflight", None)
        if preflight is None:
            return None
        result = preflight(call)
        if asyncio.iscoroutine(result):
            result = await result
        if result is not None and not isinstance(
            result, (PolicyDecision, ToolObservation)
        ):
            raise TypeError("tool preflight returned an unsupported value")
        return result

    @staticmethod
    def _observation_error_code(observation: ToolObservation) -> str | None:
        try:
            payload = json.loads(observation.content)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
            return None
        code = payload["error"].get("code")
        return code if isinstance(code, str) else None

    async def _finish_cancellation(
        self, run_id: str, projection: RunProjection
    ) -> RunProjection:
        uncertain = self._uncertain_mutation(projection)
        if uncertain is not None:
            return await self._fail_uncertain(run_id, uncertain.call_id)
        self._append(run_id, "run.cancelled", {})
        await self._emit(run_id, "runtime.status", {"status": "cancelled"})
        return self.project(run_id)

    async def _fail(self, run_id: str, reason: str) -> RunProjection:
        projection = self.project(run_id)
        if projection.status is RunStatus.CANCELLING:
            return await self._finish_cancellation(run_id, projection)
        self._append(run_id, "run.failed", {"reason": reason})
        await self._emit(
            run_id, "runtime.status", {"status": "failed", "reason": reason}
        )
        return self.project(run_id)

    async def _fail_uncertain(
        self, run_id: str, call_id: str
    ) -> RunProjection:
        reason = f"mutation outcome is uncertain for tool call {call_id}"
        self._append(run_id, "run.failed_uncertain", {"reason": reason})
        await self._emit(
            run_id,
            "runtime.status",
            {"status": "failed_uncertain", "reason": reason},
        )
        return self.project(run_id)

    def _append(
        self, run_id: str, event_type: str, payload: Mapping[str, Any]
    ) -> RunEvent:
        # Validate the existing stream first, then validate the appended result.
        projection = self.project(run_id)
        if projection.status.is_terminal:
            raise ProjectionError("cannot append an event to a terminal run")
        event = self.event_store.append(run_id, NewRunEvent(event_type, payload))
        self.project(run_id)
        self._after_durable_event(event)
        return event

    def _after_durable_event(self, event: RunEvent) -> None:
        if self.fault_injector is not None:
            self.fault_injector(event)

    async def _emit(
        self, run_id: str, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        if self.live_events is not None:
            await self.live_events.emit(LiveEvent(run_id, event_type, payload))

    def _tool_effect(self, call: ToolCall) -> str:
        registry = getattr(self.tool_executor, "registry", None)
        if registry is None:
            return "read"
        definition = registry.resolve(call.name)
        if definition is None:
            return "read"
        effect = getattr(definition.effect, "value", definition.effect)
        return str(effect)

    def _tool_result_payload(
        self, observation: ToolObservation
    ) -> dict[str, Any]:
        if self.tool_result_store is None:
            return {"content": observation.content}
        stored = self.tool_result_store.store(observation.content)
        payload: dict[str, Any] = {
            "content": stored.context_content,
            "content_sha256": stored.sha256,
            "content_bytes": stored.byte_size,
            "content_externalized": stored.externalized,
        }
        if stored.reference is not None:
            payload["content_ref"] = stored.reference
        return payload

    def _provider_context(
        self, run_id: str, projection: RunProjection
    ) -> list[dict[str, Any]]:
        if self.context_token_budget is None:
            return projection.provider_messages
        snapshot = self.context_projector.project(
            run_id, self.event_store.replay(run_id)
        )
        budgeted = self.context_policy.apply(
            snapshot, self.context_token_budget
        )
        return budgeted.to_provider_messages()

    def _budget_failure(self, projection: RunProjection) -> str | None:
        token_count = sum(
            (record.input_tokens or 0) + (record.output_tokens or 0)
            for record in projection.assistant_records
        )
        if token_count > self.limits.max_tokens:
            return (
                f"token budget exceeded: {token_count} > "
                f"{self.limits.max_tokens}"
            )
        tool_call_count = sum(
            len(record.tool_calls) for record in projection.assistant_records
        )
        if tool_call_count > self.limits.max_tool_calls:
            return (
                f"tool call budget exceeded: {tool_call_count} > "
                f"{self.limits.max_tool_calls}"
            )
        return None

    @staticmethod
    def _uncertain_mutation(projection: RunProjection):
        return next(
            (
                tool
                for tool in projection.tools.values()
                if tool.state is ToolState.STARTED and tool.effect == "mutate"
            ),
            None,
        )

    @staticmethod
    def _serialize_call(call: ToolCall) -> dict[str, Any]:
        return {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}

    @staticmethod
    def _deserialize_call(value: Mapping[str, Any]) -> ToolCall:
        return ToolCall(
            id=value.get("id"),
            name=value.get("name"),
            arguments=value.get("arguments"),
        )

    @staticmethod
    def response_from_projection(projection: RunProjection) -> ProviderResponse | None:
        record = projection.current_assistant
        if record is None:
            return None
        usage = None
        if record.input_tokens is not None and record.output_tokens is not None:
            usage = ProviderUsage(record.input_tokens, record.output_tokens)
        return ProviderResponse(
            text=record.text,
            stop_reason=StopReason(record.stop_reason),
            tool_calls=tuple(
                EventDrivenAgentRuntime._deserialize_call(call)
                for call in record.tool_calls
            ),
            usage=usage,
        )
