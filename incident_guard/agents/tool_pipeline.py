from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from incident_guard.agents.provider import ToolCall
from incident_guard.agents.run_models import ToolObservation


# 工具处理器的返回值被限制为：纯字符串，或者与当前调用绑定的 ToolObservation。
# 这样可以保证 Agent Loop 只接收统一的观测结果，而不会直接暴露原始 handler 的任意异常对象。
ToolHandlerResult = str | ToolObservation
ToolHandler = Callable[
    [Mapping[str, Any]], ToolHandlerResult | Awaitable[ToolHandlerResult]
]


# ToolErrorCode 是工具协议层面的标准错误集合；这些失败都被包装成结构化结果，
# 让外层 Agent 能继续做下一步判断，而不是因为单个工具失败直接崩掉。
class ToolErrorCode(StrEnum):
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    EXECUTION_FAILED = "execution_failed"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_DENIED = "approval_denied"
    TIMEOUT = "timeout"


# READ 表示只读，不产生副作用；MUTATE 表示修改系统状态，必须经过更严格的审批和串行化控制。
class ToolEffect(StrEnum):
    READ = "read"
    MUTATE = "mutate"


# PolicyAction 描述工具调用在执行前的决策：允许、拒绝，或者要求审批。
class PolicyAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    # Policy 的最终判定结果，包含动作和原因。
    action: PolicyAction
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", PolicyAction(self.action))
        if not isinstance(self.reason, str):
            raise ValueError("PolicyDecision reason must be a string")


class PolicyProvider(Protocol):
    # PolicyProvider 提供执行前策略判定，决定是否允许此工具调用。
    def evaluate(
        self, call: ToolCall, definition: "ToolDefinition"
    ) -> PolicyDecision | Awaitable[PolicyDecision]: ...


class AllowAllPolicy:
    # 默认策略是放行，但即便放行，也仍然必须通过参数校验和必要审批要求。
    def evaluate(
        self, call: ToolCall, definition: "ToolDefinition"
    ) -> PolicyDecision:
        return PolicyDecision(PolicyAction.ALLOW)


class ToolHook(Protocol):
    # Hook 允许在工具前后插入观察点，用于审计、埋点或额外检查，
    # 但不改变工具执行本身的最终语义。
    def before_tool(
        self, call: ToolCall, definition: "ToolDefinition"
    ) -> None | Awaitable[None]: ...

    def after_tool(
        self,
        call: ToolCall,
        definition: "ToolDefinition",
        observation: ToolObservation,
    ) -> None | Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    # 审批请求会记录调用上下文、影响类型和执行 lane，确保审批能够追踪到具体的副作用意图。
    request_id: str
    call: ToolCall
    effect: ToolEffect
    lane: str | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("ApprovalRequest request_id must be non-empty")
        if not isinstance(self.call, ToolCall):
            raise ValueError("ApprovalRequest call must be a ToolCall")
        object.__setattr__(self, "effect", ToolEffect(self.effect))
        if self.lane is not None and (
            not isinstance(self.lane, str) or not self.lane.strip()
        ):
            raise ValueError("ApprovalRequest lane must be non-empty or None")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("ApprovalRequest reason must be non-empty")


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    # 审批必须和原始 request_id 对应，避免审批回流时串错请求。
    request_id: str
    approved: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("ApprovalDecision request_id must be non-empty")
        if type(self.approved) is not bool:
            raise ValueError("ApprovalDecision approved must be a bool")
        if not isinstance(self.reason, str):
            raise ValueError("ApprovalDecision reason must be a string")


class ApprovalProvider(Protocol):
    # 外部审批系统会以 request/decision 对的形式做授权判定。
    def request_approval(
        self, request: ApprovalRequest
    ) -> ApprovalDecision | Awaitable[ApprovalDecision]: ...


@dataclass(frozen=True, slots=True)
class ToolError:
    # ToolError 是“工具执行层面的标准失败”，通常用于返回给 Agent，而不是直接中断运行循环。
    code: ToolErrorCode
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", ToolErrorCode(self.code))
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("ToolError message must be non-empty")

    def to_observation(self, call: ToolCall) -> ToolObservation:
        content = json.dumps(
            {"error": {"code": self.code.value, "message": self.message}},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return ToolObservation(call.id, call.name, content, is_error=True)


class ToolExecutionFailure(RuntimeError):
    """A provider-originated failure that can safely cross the tool boundary."""

    def __init__(self, code: ToolErrorCode | str, message: str) -> None:
        self.code = ToolErrorCode(code)
        if not isinstance(message, str) or not message.strip():
            raise ValueError("ToolExecutionFailure message must be non-empty")
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    # ToolDefinition 把“工具名 + 参数约束 + 执行器 + 安全元数据”统一成一个稳定契约。
    name: str
    input_schema: Mapping[str, Any]
    handler: ToolHandler
    effect: ToolEffect = ToolEffect.READ
    requires_approval: bool | None = None
    lane_argument: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("ToolDefinition name must be non-empty")
        if not isinstance(self.input_schema, Mapping):
            raise ValueError("ToolDefinition input_schema must be a mapping")
        if not callable(self.handler):
            raise ValueError("ToolDefinition handler must be callable")
        object.__setattr__(self, "effect", ToolEffect(self.effect))
        if self.requires_approval is None:
            object.__setattr__(
                self,
                "requires_approval",
                self.effect is ToolEffect.MUTATE,
            )
        elif type(self.requires_approval) is not bool:
            raise ValueError("ToolDefinition requires_approval must be a bool or None")
        if self.requires_approval and self.effect is not ToolEffect.MUTATE:
            raise ValueError("Only MUTATE tools can require approval")
        if self.effect is ToolEffect.MUTATE and not self.requires_approval:
            raise ValueError("MUTATE tools must require approval")
        if self.lane_argument is not None and (
            not isinstance(self.lane_argument, str) or not self.lane_argument.strip()
        ):
            raise ValueError("ToolDefinition lane_argument must be non-empty or None")
        if self.lane_argument is not None and self.effect is not ToolEffect.MUTATE:
            raise ValueError("Only MUTATE tools can use a named lane")
        if not isinstance(self.description, str):
            raise ValueError("ToolDefinition description must be a string")
        try:
            Draft202012Validator.check_schema(dict(self.input_schema))
        except SchemaError as error:
            raise ValueError(
                f"Invalid JSON Schema for tool {self.name}: {error.message}"
            ) from error


class ToolProvider(Protocol):
    """Source-neutral contract used by local, fake, and MCP-backed tools."""

    def definitions(self) -> tuple[ToolDefinition, ...]: ...


class ToolRegistry:
    """Process-local registry with deterministic name resolution.

    这里的注册中心负责把字符串工具名映射到可信的 ToolDefinition，
    模型不能直接拿到 handler 指针，只能通过该 registry 进入执行通道。
    """

    def __init__(self, tools: tuple[ToolDefinition, ...] = ()) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolDefinition) -> None:
        if not isinstance(tool, ToolDefinition):
            raise ValueError("ToolRegistry can only register ToolDefinition values")
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def resolve(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)


class RegistryToolExecutor:
    """Resolve, validate, authorize, schedule, and invoke one registered tool.

    这是整个工具调用链的执行边界：模型发出 ToolCall 后，实际执行都必须走这里，
    从 schema 校验到 policy、审批、最终 handler 调用，保证调用链受控。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        policy: PolicyProvider | None = None,
        hooks: tuple[ToolHook, ...] = (),
        max_read_concurrency: int = 4,
        approval_provider: ApprovalProvider | None = None,
    ) -> None:
        if type(max_read_concurrency) is not int or max_read_concurrency < 1:
            raise ValueError("max_read_concurrency must be a positive int")
        self.registry = registry
        self.policy = policy or AllowAllPolicy()
        self.hooks = hooks
        self.max_read_concurrency = max_read_concurrency
        self.approval_provider = approval_provider
        self.approval_requests: list[ApprovalRequest] = []
        self.approval_decisions: list[ApprovalDecision] = []
        self._lane_locks: dict[str, asyncio.Lock] = {}

    async def execute_batch(
        self, calls: tuple[ToolCall, ...]
    ) -> tuple[ToolObservation, ...]:
        # 批量执行时，读操作可以并发，但任何包含 MUTATE 的批次都按原始顺序串行执行，
        # 这样可以避免不同副作用互相交叉造成不确定状态。
        if not isinstance(calls, tuple) or not all(
            isinstance(call, ToolCall) for call in calls
        ):
            raise ValueError("execute_batch calls must be a tuple of ToolCall values")
        contains_mutation = any(
            (definition := self.registry.resolve(call.name)) is not None
            and definition.effect is ToolEffect.MUTATE
            for call in calls
        )
        if contains_mutation:
            observations = []
            for call in calls:
                observations.append(await self.execute(call))
            return tuple(observations)

        semaphore = asyncio.Semaphore(self.max_read_concurrency)

        async def execute_bounded(call: ToolCall) -> ToolObservation:
            async with semaphore:
                return await self.execute(call)

        return tuple(await asyncio.gather(*(execute_bounded(call) for call in calls)))

    async def preflight(
        self, call: ToolCall
    ) -> PolicyDecision | ToolObservation:
        """Validate and evaluate policy without invoking hooks, approval, or handler."""

        definition = self.registry.resolve(call.name)
        if definition is None:
            return ToolError(
                ToolErrorCode.UNKNOWN_TOOL,
                f"Unknown tool: {call.name}",
            ).to_observation(call)
        error = self._validate(definition, call)
        if error is not None:
            return error.to_observation(call)
        if (
            definition.lane_argument is not None
            and definition.lane_argument not in call.arguments
        ):
            return ToolError(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"Invalid arguments at $: missing lane field {definition.lane_argument}",
            ).to_observation(call)
        decision = await _maybe_await(self.policy.evaluate(call, definition))
        if not isinstance(decision, PolicyDecision):
            raise TypeError("Policy must return PolicyDecision")
        if decision.action is PolicyAction.DENY:
            return ToolError(
                ToolErrorCode.POLICY_DENIED,
                decision.reason or f"Policy denied tool: {call.name}",
            ).to_observation(call)
        return decision

    async def execute(self, call: ToolCall) -> ToolObservation:
        # 单个工具执行入口：先解析，再验证，再确保 lane 约束，最后才进入安全和业务判定。
        definition = self.registry.resolve(call.name)
        if definition is None:
            return ToolError(
                ToolErrorCode.UNKNOWN_TOOL,
                f"Unknown tool: {call.name}",
            ).to_observation(call)

        error = self._validate(definition, call)
        if error is not None:
            return error.to_observation(call)
        if (
            definition.lane_argument is not None
            and definition.lane_argument not in call.arguments
        ):
            return ToolError(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"Invalid arguments at $: missing lane field {definition.lane_argument}",
            ).to_observation(call)

        lane = self._lane_for(definition, call)
        if lane is not None:
            lock = self._lane_locks.setdefault(lane, asyncio.Lock())
            async with lock:
                return await self._execute_validated(call, definition, lane)
        return await self._execute_validated(call, definition, lane)

    async def _execute_validated(
        self,
        call: ToolCall,
        definition: ToolDefinition,
        lane: str | None,
    ) -> ToolObservation:
        # 这是 handler 执行前后的关键隔离边界：
        # hooks、policy、approval 和 handler 自己的异常都会在这里被收敛为结构化错误，
        # 不让异常直接打断整个 Agent Loop。

        try:
            for hook in self.hooks:
                await _maybe_await(hook.before_tool(call, definition))

            decision = await _maybe_await(self.policy.evaluate(call, definition))
            if not isinstance(decision, PolicyDecision):
                raise TypeError("Policy must return PolicyDecision")
            if decision.action is PolicyAction.DENY:
                return ToolError(
                    ToolErrorCode.POLICY_DENIED,
                    decision.reason or f"Policy denied tool: {call.name}",
                ).to_observation(call)
            if decision.action is PolicyAction.ASK or definition.requires_approval:
                approval_error = await self._request_approval(
                    call,
                    definition,
                    lane,
                    decision.reason or f"Approval required for tool: {call.name}",
                )
                if approval_error is not None:
                    return approval_error.to_observation(call)

            value = definition.handler(call.arguments)
            value = await _maybe_await(value)
            observation = self._normalize_result(call, value)
            for hook in self.hooks:
                await _maybe_await(hook.after_tool(call, definition, observation))
            return observation
        except ToolExecutionFailure as error:
            return ToolError(error.code, error.message).to_observation(call)
        except Exception as error:  # handlers are an isolation boundary
            return ToolError(
                ToolErrorCode.EXECUTION_FAILED,
                f"Tool execution failed: {type(error).__name__}",
            ).to_observation(call)

    async def _request_approval(
        self,
        call: ToolCall,
        definition: ToolDefinition,
        lane: str | None,
        reason: str,
    ) -> ToolError | None:
        # 需要审批的工具在执行前会形成 ApprovalRequest，并在外部提供器中拿到决定。
        # 若没有审批提供器，则按 fail-closed 方式拒绝执行，返回 approval_required。
        if self.approval_provider is None:
            return ToolError(ToolErrorCode.APPROVAL_REQUIRED, reason)
        request = ApprovalRequest(
            request_id=f"approval:{call.id}",
            call=call,
            effect=definition.effect,
            lane=lane,
            reason=reason,
        )
        self.approval_requests.append(request)
        decision = await _maybe_await(
            self.approval_provider.request_approval(request)
        )
        if not isinstance(decision, ApprovalDecision):
            raise TypeError("Approval provider must return ApprovalDecision")
        if decision.request_id != request.request_id:
            raise ValueError("Approval decision does not match request")
        self.approval_decisions.append(decision)
        if decision.approved:
            return None
        return ToolError(
            ToolErrorCode.APPROVAL_DENIED,
            decision.reason or f"Approval denied for tool: {call.name}",
        )

    @staticmethod
    def _lane_for(definition: ToolDefinition, call: ToolCall) -> str | None:
        # lane 用于按资源维度串行化 MUTATE 操作，例如同一个 service 的重启/回滚不能重叠执行。
        # 这样可以避免因并发副作用导致状态竞争或覆盖。
        if definition.effect is not ToolEffect.MUTATE:
            return None
        if definition.lane_argument is None:
            return f"tool:{definition.name}"
        return f"service:{call.arguments[definition.lane_argument]}"

    @staticmethod
    def _validate(
        definition: ToolDefinition, call: ToolCall
    ) -> ToolError | None:
        # 先做 schema 校验，再进入 policy/approval/execution；
        # 这是“先保证输入合法，再判断是否允许做副作用”的安全顺序。
        validator = Draft202012Validator(dict(definition.input_schema))
        errors = sorted(validator.iter_errors(call.arguments), key=_error_sort_key)
        if not errors:
            return None
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        return ToolError(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"Invalid arguments at {path}: {error.message}",
        )

    @staticmethod
    def _normalize_result(call: ToolCall, value: Any) -> ToolObservation:
        # 最终输出必须符合协议：要么是字符串结果，要么是绑定当前 call 的 ToolObservation。
        # 这样上层 Agent 可以稳定地消费执行结果，而不必关心 handler 的临时返回结构。
        if isinstance(value, ToolObservation):
            if value.call_id != call.id or value.name != call.name:
                raise ValueError("Tool observation does not match the requested call")
            return value
        if not isinstance(value, str):
            raise TypeError("Tool handler must return str or ToolObservation")
        return ToolObservation(call.id, call.name, value)


def _error_sort_key(error: ValidationError) -> tuple[str, str]:
    path = ".".join(str(part) for part in error.absolute_path)
    return path, error.message


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
