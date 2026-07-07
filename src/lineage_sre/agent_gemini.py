"""Gemini backend: runs the agent on Google's free-tier Gemini API (no cost, no card).

Get a key at https://aistudio.google.com/apikey and set GEMINI_API_KEY in .env.
Uses the same ToolBox (and optional DataHub MCP bridge) as the other backends.
"""

from __future__ import annotations

import asyncio

from google import genai
from google.genai import types
from google.genai.errors import ClientError
from rich.console import Console

from .config import Settings
from .tools import ToolBox

MAX_TURNS = 30
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_WAIT_S = 20  # free tier is RPM-limited; back off and continue


def _declarations(tool_defs: list[dict]) -> list[types.FunctionDeclaration]:
    return [
        types.FunctionDeclaration(
            name=d["name"],
            description=d["description"],
            parameters=d["input_schema"],
        )
        for d in tool_defs
    ]


async def _send_with_backoff(chat, message, console: Console):
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return await chat.send_message(message)
        except ClientError as exc:
            if exc.code == 429 and attempt < RATE_LIMIT_RETRIES:
                console.print(f"  [dim]rate-limited by free tier, waiting {RATE_LIMIT_WAIT_S}s...[/dim]")
                await asyncio.sleep(RATE_LIMIT_WAIT_S)
                continue
            raise


async def run_diagnosis_gemini(
    settings: Settings,
    toolbox: ToolBox,
    system_prompt: str,
    initial_message: str,
    use_mcp: bool = False,
    console: Console | None = None,
) -> str:
    """Run the agent loop on Gemini. Returns the final report markdown."""
    console = console or Console()

    mcp_bridge = None
    if use_mcp:
        from .mcp_bridge import DataHubMCPBridge

        mcp_bridge = DataHubMCPBridge(settings.gms_url, settings.datahub_token)

    client = genai.Client(api_key=settings.gemini_api_key)

    async def loop() -> str:
        if mcp_bridge is not None:
            tool_defs = mcp_bridge.tools + toolbox.definitions(include_read_tools=False)
            mcp_tool_names = mcp_bridge.tool_names()
        else:
            tool_defs = toolbox.definitions(include_read_tools=True)
            mcp_tool_names = set()

        chat = client.aio.chats.create(
            model=settings.gemini_model,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[types.Tool(function_declarations=_declarations(tool_defs))],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )

        response = await _send_with_backoff(chat, initial_message, console)
        final_text = ""
        for _ in range(MAX_TURNS):
            calls = response.function_calls or []
            if response.text:
                final_text = response.text
            if not calls:
                break
            result_parts = []
            for call in calls:
                args = dict(call.args or {})
                source = "mcp" if call.name in mcp_tool_names else "tool"
                console.print(f"  [dim]→ {source}:[/dim] [bold]{call.name}[/bold] [dim]{str(args)[:140]}[/dim]")
                if call.name in mcp_tool_names:
                    result = await mcp_bridge.call(call.name, args)
                else:
                    result = toolbox.dispatch(call.name, args)
                result_parts.append(
                    types.Part.from_function_response(name=call.name, response={"result": result})
                )
            response = await _send_with_backoff(chat, result_parts, console)
        return final_text

    if mcp_bridge is not None:
        async with mcp_bridge:
            return await loop()
    return await loop()
