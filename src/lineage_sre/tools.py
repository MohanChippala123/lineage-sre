"""Agent tools: DataHub context reads, warehouse access, and write-back actions."""

from __future__ import annotations

import json
from pathlib import Path

from .config import Settings
from .datahub_client import DataHubClient
from .demo_stack import MODELS
from .warehouse import health_check, read_model_sql, recreate_view, run_readonly_sql

MAX_RESULT_CHARS = 8000

# Read tools are swapped out for the official DataHub MCP Server in --mcp mode;
# action tools are always ours (the MCP server is read-focused).
READ_TOOLS = [
    {
        "name": "get_dataset",
        "description": (
            "Fetch a dataset's metadata from DataHub: schema fields, description, owners, "
            "and custom properties (e.g. vendor feed version)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"urn": {"type": "string", "description": "DataHub dataset URN"}},
            "required": ["urn"],
        },
    },
    {
        "name": "get_lineage",
        "description": "Walk the DataHub lineage graph from a dataset, upstream or downstream, across multiple hops.",
        "input_schema": {
            "type": "object",
            "properties": {
                "urn": {"type": "string", "description": "DataHub dataset URN to start from"},
                "direction": {"type": "string", "enum": ["UPSTREAM", "DOWNSTREAM"]},
            },
            "required": ["urn", "direction"],
        },
    },
    {
        "name": "search_datasets",
        "description": "Search DataHub for datasets by name or keyword. Returns URNs.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]

ACTION_TOOLS = [
    {
        "name": "run_sql",
        "description": (
            "Run a read-only SQL query (SELECT/WITH/DESCRIBE/SHOW/EXPLAIN) against the DuckDB warehouse. "
            "Use it to reproduce failures, inspect actual columns, and validate candidate fixes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
    {
        "name": "get_model_sql",
        "description": "Read the SQL source of a pipeline model from the repo (demo/models/<model>.sql).",
        "input_schema": {
            "type": "object",
            "properties": {"model_name": {"type": "string", "description": "e.g. stg_payments"}},
            "required": ["model_name"],
        },
    },
    {
        "name": "propose_fix",
        "description": (
            "Write a corrected SQL model to the fixes/ directory as a reviewable artifact. "
            "The fix must preserve the model's downstream column contract."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model_name": {"type": "string"},
                "fixed_sql": {"type": "string", "description": "Complete replacement SQL for the model"},
                "explanation": {"type": "string", "description": "One-paragraph rationale for the change"},
            },
            "required": ["model_name", "fixed_sql", "explanation"],
        },
    },
    {
        "name": "apply_fix",
        "description": (
            "Apply a previously proposed fix: recreate the model view in the warehouse from the fix file, "
            "then re-run the pipeline health check. Only available when the operator allowed --apply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"model_name": {"type": "string"}},
            "required": ["model_name"],
        },
    },
    {
        "name": "raise_incident",
        "description": (
            "Raise an incident in DataHub on the root-cause asset so its owners and consumers see it. "
            "Raise exactly one incident per diagnosis, on the root cause (not on every affected asset)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resource_urn": {"type": "string"},
                "incident_type": {
                    "type": "string",
                    "enum": ["OPERATIONAL", "FRESHNESS", "VOLUME", "COLUMN", "SQL", "DATA_SCHEMA", "CUSTOM"],
                },
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["resource_urn", "incident_type", "title", "description"],
        },
    },
    {
        "name": "resolve_incident",
        "description": "Mark a DataHub incident RESOLVED. Only after a fix has been applied AND verified via health check.",
        "input_schema": {
            "type": "object",
            "properties": {
                "incident_urn": {"type": "string"},
                "message": {"type": "string", "description": "Resolution summary"},
            },
            "required": ["incident_urn", "message"],
        },
    },
    {
        "name": "write_back_docs",
        "description": (
            "Append a postmortem section to a dataset's documentation in DataHub, so future engineers "
            "and agents inherit what was learned. Use on the root-cause asset and the primary affected model."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "urn": {"type": "string"},
                "markdown": {"type": "string", "description": "Markdown postmortem section"},
            },
            "required": ["urn", "markdown"],
        },
    },
]


def _truncate(text: str) -> str:
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + f"\n... (truncated at {MAX_RESULT_CHARS} chars)"
    return text


class ToolBox:
    """Dispatches agent tool calls. Records actions taken for the final summary."""

    def __init__(self, settings: Settings, client: DataHubClient, allow_apply: bool):
        self.settings = settings
        self.client = client
        self.allow_apply = allow_apply
        self.actions_taken: list[str] = []
        settings.fixes_dir.mkdir(parents=True, exist_ok=True)

    def definitions(self, include_read_tools: bool = True) -> list[dict]:
        tools = list(ACTION_TOOLS)
        if include_read_tools:
            tools = READ_TOOLS + tools
        return tools

    def dispatch(self, name: str, args: dict) -> str:
        try:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return f"ERROR: unknown tool {name}"
            return _truncate(handler(**args))
        except Exception as exc:  # noqa: BLE001 - surface errors to the agent, don't crash the loop
            return f"TOOL ERROR ({name}): {exc}"

    # --- DataHub reads --------------------------------------------------------

    def _tool_get_dataset(self, urn: str) -> str:
        return json.dumps(self.client.get_dataset(urn), indent=2)

    def _tool_get_lineage(self, urn: str, direction: str) -> str:
        return json.dumps(self.client.get_lineage(urn, direction), indent=2)

    def _tool_search_datasets(self, query: str) -> str:
        return json.dumps(self.client.search_datasets(query), indent=2)

    # --- warehouse / repo -----------------------------------------------------

    def _tool_run_sql(self, sql: str) -> str:
        return run_readonly_sql(self.settings.warehouse_path, sql)

    def _tool_get_model_sql(self, model_name: str) -> str:
        return read_model_sql(self.settings.models_dir, model_name)

    # --- actions ----------------------------------------------------------------

    def _fix_path(self, model_name: str) -> Path:
        return self.settings.fixes_dir / f"{model_name}.sql"

    def _tool_propose_fix(self, model_name: str, fixed_sql: str, explanation: str) -> str:
        header = "\n".join(f"-- {line}" for line in explanation.strip().splitlines())
        path = self._fix_path(model_name)
        path.write_text(f"-- FIX proposed by Lineage SRE\n{header}\n{fixed_sql.strip()}\n", encoding="utf-8")
        self.actions_taken.append(f"Proposed fix written to {path}")
        return f"Fix written to {path}. Validate it with run_sql, then apply_fix if permitted."

    def _tool_apply_fix(self, model_name: str) -> str:
        if not self.allow_apply:
            return "DENIED: the operator did not allow applying fixes in this run (missing --apply)."
        path = self._fix_path(model_name)
        if not path.exists():
            return f"ERROR: no proposed fix found at {path}. Call propose_fix first."
        sql = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("--")
        )
        recreate_view(self.settings.warehouse_path, model_name, sql)
        self.actions_taken.append(f"Applied fix: recreated view {model_name}")
        results = health_check(self.settings.warehouse_path, MODELS)
        return "Fix applied. Health check:\n" + json.dumps(results, indent=2)

    def _tool_raise_incident(self, resource_urn: str, incident_type: str, title: str, description: str) -> str:
        incident_urn = self.client.raise_incident(resource_urn, incident_type, title, description)
        self.actions_taken.append(f"Raised {incident_type} incident {incident_urn} on {resource_urn}")
        return f"Incident raised in DataHub: {incident_urn}"

    def _tool_resolve_incident(self, incident_urn: str, message: str) -> str:
        self.client.resolve_incident(incident_urn, message)
        self.actions_taken.append(f"Resolved incident {incident_urn}")
        return f"Incident {incident_urn} marked RESOLVED."

    def _tool_write_back_docs(self, urn: str, markdown: str) -> str:
        self.client.append_editable_description(urn, markdown)
        self.actions_taken.append(f"Postmortem docs written to {urn}")
        return f"Documentation updated on {urn}."
