from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from incident_guard.agents.tool_pipeline import (
    ToolDefinition,
    ToolEffect,
    ToolErrorCode,
    ToolExecutionFailure,
    ToolRegistry,
)
from incident_guard.tools import IncidentToolName


MUTATING_TOOLS = {
    IncidentToolName.RESTART_SERVICE.value,
    IncidentToolName.ROLLBACK_SERVICE.value,
}


class MCPToolProvider:
    """Discover and execute Incident tools through an official-SDK MCP session."""

    def __init__(
        self,
        server: StdioServerParameters,
        *,
        call_timeout: float = 10.0,
    ) -> None:
        if not isinstance(call_timeout, (int, float)) or call_timeout <= 0:
            raise ValueError("call_timeout must be positive")
        self.server = server
        self.call_timeout = float(call_timeout)
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | Any | None = None
        self._definitions: tuple[ToolDefinition, ...] = ()

    async def __aenter__(self) -> "MCPToolProvider":
        stack = AsyncExitStack()
        try:
            errlog = stack.enter_context(open(os.devnull, "w"))
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(self.server, errlog=errlog)
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            async with asyncio.timeout(self.call_timeout):
                await session.initialize()
            self._stack = stack
            self._session = session
            await self.discover()
            return self
        except BaseException:
            await stack.aclose()
            raise

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        self._definitions = ()
        if stack is not None:
            await stack.aclose()

    def definitions(self) -> tuple[ToolDefinition, ...]:
        if not self._definitions:
            raise RuntimeError("MCPToolProvider is not connected and discovered")
        return self._definitions

    def registry(self) -> ToolRegistry:
        return ToolRegistry(self.definitions())

    async def discover(self) -> tuple[ToolDefinition, ...]:
        session = self._require_session()
        try:
            async with asyncio.timeout(self.call_timeout):
                result = await session.list_tools()
        except TimeoutError as error:
            raise ToolExecutionFailure(
                ToolErrorCode.TIMEOUT, "MCP tool discovery timed out"
            ) from error
        except Exception as error:
            raise ToolExecutionFailure(
                ToolErrorCode.EXECUTION_FAILED,
                f"MCP tool discovery failed: {type(error).__name__}",
            ) from error

        expected = {name.value for name in IncidentToolName}
        discovered = {tool.name for tool in result.tools}
        if discovered != expected:
            missing = sorted(expected - discovered)
            unexpected = sorted(discovered - expected)
            raise ToolExecutionFailure(
                ToolErrorCode.EXECUTION_FAILED,
                f"MCP Incident tool set mismatch: missing={missing}, unexpected={unexpected}",
            )

        self._definitions = tuple(
            self._definition_from_remote(tool) for tool in result.tools
        )
        return self._definitions

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        session = self._require_session()
        try:
            async with asyncio.timeout(self.call_timeout):
                result = await session.call_tool(name, arguments)
        except TimeoutError as error:
            raise ToolExecutionFailure(
                ToolErrorCode.TIMEOUT, f"MCP tool call timed out: {name}"
            ) from error
        except Exception as error:
            raise ToolExecutionFailure(
                ToolErrorCode.EXECUTION_FAILED,
                f"MCP tool call failed: {type(error).__name__}",
            ) from error

        text = _result_text(result)
        if result.is_error:
            raise ToolExecutionFailure(
                ToolErrorCode.EXECUTION_FAILED,
                f"MCP tool reported an error: {text or name}",
            )
        return text

    def _definition_from_remote(self, tool: types.Tool) -> ToolDefinition:
        name = tool.name

        async def handler(arguments: dict[str, Any]) -> str:
            return await self.call_tool(name, arguments)

        is_mutation = name in MUTATING_TOOLS
        return ToolDefinition(
            name=name,
            input_schema=tool.input_schema,
            handler=handler,
            effect=ToolEffect.MUTATE if is_mutation else ToolEffect.READ,
            lane_argument="service_id" if is_mutation else None,
            description=tool.description or "",
        )

    def _require_session(self):
        if self._session is None:
            raise RuntimeError("MCPToolProvider is not connected")
        return self._session


def _result_text(result: types.CallToolResult) -> str:
    text_blocks = [
        block.text for block in result.content if isinstance(block, types.TextContent)
    ]
    if text_blocks:
        return "\n".join(text_blocks)
    if result.structured_content is not None:
        return json.dumps(
            result.structured_content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return ""
