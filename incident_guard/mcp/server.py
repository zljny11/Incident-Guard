from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
import anyio
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from incident_guard.agents.tool_pipeline import ToolDefinition, ToolEffect, ToolProvider
from incident_guard.tools import (
    DockerIncidentToolProvider,
    FakeIncidentToolProvider,
    IncidentScenario,
)


def create_incident_mcp_server(provider: ToolProvider) -> Server:
    """Expose one ToolProvider over MCP without moving policy into the server."""

    definitions = provider.definitions()
    by_name = {str(definition.name): definition for definition in definitions}

    async def list_tools(_context, _params) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=str(definition.name),
                    description=definition.description,
                    inputSchema=dict(definition.input_schema),
                    annotations=types.ToolAnnotations(
                        readOnlyHint=definition.effect is ToolEffect.READ,
                        destructiveHint=definition.effect is ToolEffect.MUTATE,
                        idempotentHint=False,
                        openWorldHint=False,
                    ),
                )
                for definition in definitions
            ]
        )

    async def call_tool(_context, params) -> types.CallToolResult:
        definition = by_name.get(params.name)
        if definition is None:
            return _error_result(f"unknown Incident tool: {params.name}")
        arguments = params.arguments or {}
        validation_errors = sorted(
            Draft202012Validator(dict(definition.input_schema)).iter_errors(arguments),
            key=lambda error: (list(error.absolute_path), error.message),
        )
        if validation_errors:
            return _error_result(f"invalid arguments: {validation_errors[0].message}")
        try:
            value = definition.handler(arguments)
            if inspect.isawaitable(value):
                value = await value
            if not isinstance(value, str):
                return _error_result("Incident tool returned a non-text result")
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=value)]
            )
        except Exception as error:
            return _error_result(f"Incident tool failed: {type(error).__name__}")

    return Server(
        "incident-guard",
        version="0.1.0",
        description="Restricted incident investigation and recovery tools",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], isError=True
    )


async def serve_stdio(provider: ToolProvider) -> None:
    server = create_incident_mcp_server(provider)
    stdin, stdout, transport = await _async_stdio_streams()
    try:
        async with stdio_server(stdin, stdout) as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        transport.close()


class _AsyncTextReader:
    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader

    def __aiter__(self) -> "_AsyncTextReader":
        return self

    async def __anext__(self) -> str:
        line = await self.reader.readline()
        if not line:
            raise StopAsyncIteration
        return line.decode("utf-8", errors="replace")


class _AsyncTextWriter:
    async def write(self, value: str) -> None:
        sys.stdout.write(value)

    async def flush(self) -> None:
        sys.stdout.flush()


async def _async_stdio_streams():
    """Adapt stdio without a worker thread, which also supports Python 3.14."""

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    return _AsyncTextReader(reader), _AsyncTextWriter(), transport


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Incident Guard stdio MCP server")
    parser.add_argument(
        "--scenario",
        choices=[scenario.value for scenario in IncidentScenario],
        default=IncidentScenario.BAD_DEPLOYMENT.value,
    )
    parser.add_argument("--backend", choices=("fake", "docker"), default="fake")
    parser.add_argument("--lab-dir", default="lab")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    provider = (
        DockerIncidentToolProvider(args.lab_dir, args.scenario)
        if args.backend == "docker"
        else FakeIncidentToolProvider(args.scenario)
    )
    anyio.run(serve_stdio, provider)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
