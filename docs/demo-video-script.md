# Demo video script (target: 2:45)

**Setup before recording:** DataHub quickstart running, `.env` filled in, terminal + browser
(DataHub UI on localhost:9002) side by side. Run `uv run lineage-sre seed` beforehand so
recording starts clean.

## 0:00–0:25 — The problem
- Voiceover: "Every data team knows this morning: a vendor silently renamed a column overnight,
  and now the revenue dashboard is empty and the churn model is scoring on broken features.
  A human spends an hour walking lineage, diffing schemas, and pinging owners.
  Lineage SRE is an agent that does it in a minute — using DataHub as its brain."
- Screen: DataHub UI showing the demo lineage graph (raw_payments → … → churn_model_predictions).

## 0:25–0:45 — Break it
- Terminal: `uv run lineage-sre break` — show the "PayFlow feed v2" panel.
- Terminal: `uv run lineage-sre check` — health-check table with 3 models FAILING in red.

## 0:45–2:00 — The agent works
- Terminal: `uv run lineage-sre diagnose --apply` (or `--mcp` and say "reads go through the
  official DataHub MCP Server").
- Let the streaming tool calls speak: point at them as they scroll —
  "reproduces the failure… walks upstream lineage in DataHub… compares the model SQL against
  the live schema… there's the root cause: feed_version bumped to 2, amount_usd is now amount…
  checks downstream blast radius — note it flags the ML scoring job… looks up the owners…
  writes a contract-preserving fix, validates it, applies it… health check green…
  raises a DataHub incident and writes the postmortem back."
- Show the final RCA report rendered in the terminal, scroll it briefly.

## 2:00–2:30 — The payoff in DataHub
- Browser: open `demo.raw_payments` in DataHub → **Incidents** tab: the DATA_SCHEMA incident,
  raised and resolved by the agent.
- **Documentation** tab: the postmortem the agent appended.
- Voiceover: "The graph is smarter after the incident than before it. The next engineer — or the
  next agent — inherits this knowledge instead of re-diagnosing from scratch."

## 2:30–2:45 — Close
- "Lineage SRE: detect, diagnose, fix, verify, and write it back. Built on DataHub's lineage,
  ownership, incidents, and the official MCP Server. Apache 2.0, repo linked below."
