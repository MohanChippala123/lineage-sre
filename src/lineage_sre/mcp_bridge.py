"""Bridge to the official DataHub MCP Server (mcp-server-datahub).

In --mcp mode the agent's DataHub *reads* (search, lineage, entity metadata, schemas,
query analysis) go through the official MCP server instead of our built-in GraphQL
tools. Write-back actions (incidents, docs) stay on our side — the MCP server is
read-focused.

Requires the optional dependency group:  uv sync --extra mcp
"""

from __future__ import annotations

import os
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class DataHubMCPBridge:
    def __init__(self, gms_url: str, token: str = ""):
        self.gms_url = gms_url
        self.token = token
        self.tools: list[dict] = []
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "DataHubMCPBridge":
        env = dict(os.environ)
        env["DATAHUB_GMS_URL"] = self.gms_url
        if self.token:
            env["DATAHUB_GMS_TOKEN"] = self.token
        params = StdioServerParameters(command="mcp-server-datahub", args=[], env=env)
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        listed = await self._session.list_tools()
        self.tools = [
            {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in listed.tools
        ]
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._stack:
            await self._stack.aclose()

    def tool_names(self) -> set[str]:
        return {t["name"] for t in self.tools}

    async def call(self, name: str, args: dict) -> str:
        assert self._session is not None
        result = await self._session.call_tool(name, args)
        parts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
        return "\n".join(parts) if parts else "(empty MCP result)"
