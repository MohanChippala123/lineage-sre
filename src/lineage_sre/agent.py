"""The Lineage SRE agent: a Claude tool-use loop over DataHub context + warehouse actions."""

from __future__ import annotations

import json
from datetime import datetime

from anthropic import AsyncAnthropic
from rich.console import Console

from .config import Settings
from .datahub_client import DataHubClient, dataset_urn_for
from .tools import ToolBox

MAX_TURNS = 30

SYSTEM_PROMPT = """\
You are Lineage SRE, an autonomous incident responder for data pipelines. A data asset is \
failing and you must find the root cause, fix it, and make sure the organization never has \
to re-diagnose this from scratch.

You have two sources of truth:
- DataHub (the metadata context platform): lineage graph, current schemas, ownership, \
descriptions, custom properties (e.g. vendor feed versions).
- The warehouse (DuckDB) and the model repo: what is actually running.

Investigation method:
1. Reproduce and understand the failure with run_sql.
2. Walk UPSTREAM lineage in DataHub from the failing asset. For each upstream asset, compare \
what the model's SQL expects (get_model_sql) against the asset's current schema in DataHub \
(get_dataset). A mismatch localizes the root cause.
3. Pin the root cause precisely: which asset changed, what changed, and cite evidence \
(schema fields, custom properties like feed versions, error text).
4. Establish the blast radius: walk DOWNSTREAM lineage from the root cause and list every \
affected asset, including ML assets (feature tables, model scoring outputs).
5. Look up owners of the root-cause asset and of affected assets — they are who to notify.
6. Propose a minimal fix that PRESERVES THE DOWNSTREAM CONTRACT (downstream columns must \
keep their names and types; alias renamed upstream columns rather than propagating renames). \
Validate the fixed SQL with run_sql before proposing it. Apply it with apply_fix only if permitted.
7. Write back to DataHub so the knowledge is inherited:
   - raise_incident: exactly ONE incident, on the ROOT-CAUSE asset, type DATA_SCHEMA for \
schema changes. Description must name affected downstream assets and owners.
   - write_back_docs: append a short postmortem (what broke, why, fix, date) to the \
root-cause asset and the primary failing model.
   - resolve_incident: ONLY if the fix was applied and the health check passed.

Rules:
- Be evidence-driven. Never assert a root cause you have not confirmed with tool output.
- If a tool errors, adapt; do not retry the identical call more than once.
- Naming: DataHub dataset names are `demo.<model>`; model SQL files are `<model>.sql`.

Your FINAL message must be a complete markdown RCA report with exactly these sections:
# Incident RCA: <one-line title>
## Summary  (2-3 sentences, plain language)
## Root Cause  (the precise change, with evidence)
## Blast Radius  (affected assets, including ML impact, from DataHub lineage)
## Fix  (what was changed, whether applied and verified)
## Who to Notify  (owners from DataHub, with their roles)
## Knowledge Written Back  (incident + docs written to DataHub, with URNs)
"""


async def run_diagnosis(
    settings: Settings,
    failing_model: str,
    error_text: str,
    allow_apply: bool = False,
    use_mcp: bool = False,
    console: Console | None = None,
) -> tuple[str, str]:
    """Run the agent loop. Returns (report_markdown, report_path)."""
    console = console or Console()
    client = DataHubClient(settings.gms_url, settings.datahub_token)
    toolbox = ToolBox(settings, client, allow_apply=allow_apply)

    mcp_bridge = None
    if use_mcp:
        from .mcp_bridge import DataHubMCPBridge

        mcp_bridge = DataHubMCPBridge(settings.gms_url, settings.datahub_token)

    failing_urn = dataset_urn_for(failing_model)
    initial_message = (
        f"INCIDENT: pipeline health check failed.\n"
        f"- Failing asset: {failing_model} (DataHub URN: {failing_urn})\n"
        f"- Error:\n{error_text}\n\n"
        f"Applying fixes to the warehouse is {'ALLOWED' if allow_apply else 'NOT allowed (propose only)'} in this run.\n"
        f"Diagnose the root cause, fix it, and write the knowledge back to DataHub."
    )

    anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)
    messages: list[dict] = [{"role": "user", "content": initial_message}]

    async def loop() -> str:
        if mcp_bridge is not None:
            tools = mcp_bridge.tools + toolbox.definitions(include_read_tools=False)
            mcp_tool_names = mcp_bridge.tool_names()
        else:
            tools = toolbox.definitions(include_read_tools=True)
            mcp_tool_names = set()

        final_text = ""
        for _ in range(MAX_TURNS):
            response = await anthropic.messages.create(
                model=settings.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=tools,
            )
            assistant_content = []
            tool_results = []
            for block in response.content:
                if block.type == "text":
                    final_text = block.text
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    args_preview = json.dumps(block.input)[:140]
                    source = "mcp" if block.name in mcp_tool_names else "tool"
                    console.print(f"  [dim]→ {source}:[/dim] [bold]{block.name}[/bold] [dim]{args_preview}[/dim]")
                    assistant_content.append(
                        {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                    )
                    if block.name in mcp_tool_names:
                        result = await mcp_bridge.call(block.name, block.input)
                    else:
                        result = toolbox.dispatch(block.name, block.input)
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": result}
                    )
            messages.append({"role": "assistant", "content": assistant_content})
            if response.stop_reason != "tool_use":
                break
            messages.append({"role": "user", "content": tool_results})
        return final_text

    if mcp_bridge is not None:
        async with mcp_bridge:
            report = await loop()
    else:
        report = await loop()

    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = settings.reports_dir / f"rca_{datetime.now():%Y%m%d_%H%M%S}.md"
    footer = ""
    if toolbox.actions_taken:
        footer = "\n\n---\n_Actions taken this run:_\n" + "\n".join(f"- {a}" for a in toolbox.actions_taken)
    report_path.write_text((report or "(agent produced no report)") + footer, encoding="utf-8")
    return report, str(report_path)
