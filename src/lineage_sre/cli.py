"""Lineage SRE command-line interface."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .config import get_settings
from .datahub_client import DataHubClient
from .demo_stack import MODELS, break_scenario, seed_datahub, seed_warehouse
from .warehouse import health_check

app = typer.Typer(
    name="lineage-sre",
    help="Autonomous incident-response agent for data pipelines, powered by DataHub.",
    no_args_is_help=True,
)
console = Console()


def _check_datahub(settings) -> bool:
    client = DataHubClient(settings.gms_url, settings.datahub_token)
    if not client.ping():
        console.print(
            f"[red]Cannot reach DataHub at {settings.gms_url}.[/red]\n"
            "Start it with: [bold]datahub docker quickstart[/bold] "
            "(see README for setup)."
        )
        return False
    return True


def _print_health(results: list[dict]) -> bool:
    table = Table(title="Pipeline health check")
    table.add_column("Model")
    table.add_column("Status")
    table.add_column("Error")
    all_ok = True
    for r in results:
        if r["ok"]:
            table.add_row(r["model"], "[green]OK[/green]", "")
        else:
            all_ok = False
            table.add_row(r["model"], "[red]FAILING[/red]", (r["error"] or "")[:120])
    console.print(table)
    return all_ok


@app.command()
def seed(no_datahub: bool = typer.Option(False, "--no-datahub", help="Build the warehouse only, skip DataHub ingestion.")):
    """Create the demo DuckDB warehouse and ingest its metadata into DataHub."""
    settings = get_settings()
    seed_warehouse(settings)
    console.print(f"[green]Warehouse created:[/green] {settings.warehouse_path}")
    if no_datahub:
        return
    if not _check_datahub(settings):
        raise typer.Exit(1)
    seed_datahub(settings)
    console.print(
        f"[green]Metadata ingested into DataHub[/green] ({settings.gms_url}): "
        "datasets, schemas, lineage, owners."
    )


@app.command("break")
def break_(no_datahub: bool = typer.Option(False, "--no-datahub", help="Skip re-emitting the new schema to DataHub.")):
    """Simulate the upstream break: PayFlow feed v2 renames amount_usd -> amount."""
    settings = get_settings()
    if not no_datahub and not _check_datahub(settings):
        raise typer.Exit(1)
    break_scenario(settings, with_datahub=not no_datahub)
    console.print(
        Panel(
            "PayFlow shipped feed v2 overnight: [bold]raw_payments.amount_usd[/bold] is now "
            "[bold]amount[/bold].\nNightly ingestion refreshed the schema in DataHub. "
            "Downstream models are about to have a bad morning.",
            title="Upstream break applied",
        )
    )


@app.command()
def check():
    """Run the pipeline health check."""
    settings = get_settings()
    if _print_health(health_check(settings.warehouse_path, MODELS)):
        console.print("[green]All models healthy.[/green]")


@app.command()
def diagnose(
    model: str = typer.Option(None, "--model", help="Failing model to diagnose (default: first failure found)."),
    apply: bool = typer.Option(False, "--apply", help="Allow the agent to apply the verified fix to the warehouse."),
    mcp: bool = typer.Option(False, "--mcp", help="Read DataHub via the official DataHub MCP Server."),
):
    """Run the Lineage SRE agent on a failing model."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        console.print("[red]ANTHROPIC_API_KEY is not set.[/red] Copy .env.example to .env and fill it in.")
        raise typer.Exit(1)
    if not _check_datahub(settings):
        raise typer.Exit(1)

    results = health_check(settings.warehouse_path, MODELS)
    failures = {r["model"]: r["error"] for r in results if not r["ok"]}
    if model is None:
        if not failures:
            console.print("[green]All models healthy — nothing to diagnose.[/green]")
            raise typer.Exit(0)
        model = next(iter(failures))
    error_text = failures.get(model, "(model queried healthy just now; diagnosing anyway)")

    console.print(
        Panel(
            f"Failing model: [bold]{model}[/bold]\nMode: "
            f"{'DataHub MCP Server' if mcp else 'direct GraphQL'} reads, "
            f"{'fix application ALLOWED' if apply else 'propose-only'}",
            title="Lineage SRE — diagnosis starting",
        )
    )

    from .agent import run_diagnosis

    report, report_path = asyncio.run(
        run_diagnosis(settings, model, error_text, allow_apply=apply, use_mcp=mcp, console=console)
    )
    console.print()
    console.print(Markdown(report or "(no report)"))
    console.print(f"\n[green]RCA report saved:[/green] {report_path}")


@app.command()
def demo(
    mcp: bool = typer.Option(False, "--mcp", help="Read DataHub via the official DataHub MCP Server."),
):
    """Full demo: seed -> break -> check -> diagnose --apply."""
    settings = get_settings()
    if not _check_datahub(settings):
        raise typer.Exit(1)
    console.rule("1/4 Seed")
    seed_warehouse(settings)
    seed_datahub(settings)
    console.print("Warehouse + DataHub metadata seeded.")
    console.rule("2/4 Break")
    break_scenario(settings, with_datahub=True)
    console.print("Upstream schema change applied (PayFlow feed v2).")
    console.rule("3/4 Health check")
    _print_health(health_check(settings.warehouse_path, MODELS))
    console.rule("4/4 Diagnose")
    diagnose(model=None, apply=True, mcp=mcp)


if __name__ == "__main__":
    app()
