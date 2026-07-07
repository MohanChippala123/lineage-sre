"""Claude Agent SDK backend: runs the agent on a Claude Code subscription — no API key needed.

The same ToolBox powers both backends; here its tools are exposed to Claude Code as an
in-process MCP server. With --mcp, the official DataHub MCP server is attached natively
as a stdio MCP server, replacing our built-in DataHub read tools.
"""

from __future__ import annotations

import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)
from rich.console import Console

from .config import Settings
from .tools import ToolBox

MAX_TURNS = 40

# Keep the coding-agent side of Claude Code switched off; this agent only works
# through its own tools.
DISALLOWED_BUILTINS = [
    "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch",
    "NotebookEdit", "Task", "TodoWrite",
]


def _make_sdk_tools(toolbox: ToolBox, include_read_tools: bool) -> list:
    sdk_tools = []
    for definition in toolbox.definitions(include_read_tools=include_read_tools):
        name = definition["name"]

        def make_handler(tool_name: str):
            async def handler(args: dict) -> dict:
                result = toolbox.dispatch(tool_name, args)
                return {"content": [{"type": "text", "text": result}]}

            return handler

        sdk_tools.append(
            tool(name, definition["description"], definition["input_schema"])(make_handler(name))
        )
    return sdk_tools


async def run_diagnosis_sdk(
    settings: Settings,
    toolbox: ToolBox,
    system_prompt: str,
    initial_message: str,
    use_mcp: bool = False,
    console: Console | None = None,
) -> str:
    """Run the agent loop on the Claude Agent SDK. Returns the final report markdown."""
    console = console or Console()

    sre_server = create_sdk_mcp_server(
        name="sre",
        version="1.0.0",
        tools=_make_sdk_tools(toolbox, include_read_tools=not use_mcp),
    )
    mcp_servers: dict = {"sre": sre_server}
    allowed = [
        f"mcp__sre__{d['name']}" for d in toolbox.definitions(include_read_tools=not use_mcp)
    ]
    if use_mcp:
        env = dict(os.environ)
        env["DATAHUB_GMS_URL"] = settings.gms_url
        if settings.datahub_token:
            env["DATAHUB_GMS_TOKEN"] = settings.datahub_token
        mcp_servers["datahub"] = {"type": "stdio", "command": "mcp-server-datahub", "env": env}
        allowed.append("mcp__datahub")

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers=mcp_servers,
        allowed_tools=allowed,
        disallowed_tools=DISALLOWED_BUILTINS,
        max_turns=MAX_TURNS,
        model=settings.model or None,
    )

    final_text = ""
    async for message in query(prompt=initial_message, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    final_text = block.text
                elif isinstance(block, ToolUseBlock):
                    name = block.name
                    source = "tool"
                    if name.startswith("mcp__sre__"):
                        name = name[len("mcp__sre__"):]
                    elif name.startswith("mcp__datahub__"):
                        name, source = name[len("mcp__datahub__"):], "mcp"
                    args_preview = str(block.input)[:140]
                    console.print(f"  [dim]→ {source}:[/dim] [bold]{name}[/bold] [dim]{args_preview}[/dim]")
        elif isinstance(message, ResultMessage):
            if getattr(message, "result", None):
                final_text = message.result
    return final_text
