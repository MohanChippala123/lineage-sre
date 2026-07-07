# Incident RCA: Upstream vendor schema change broke revenue and churn pipelines

> Sample output produced by `lineage-sre diagnose --apply` on the demo stack.

## Summary
The nightly PayFlow payments feed (v2) silently renamed the `amount_usd` column to `amount` in
`demo.raw_payments`. Every downstream model that reads payments — the staging model, the executive
revenue fact table, and the churn model's feature table — failed this morning's health check.
The fix has been applied and verified; the incident in DataHub is resolved.

## Root Cause
`raw_payments.amount_usd` no longer exists; the column now arrives as `amount`.

Evidence:
- Querying `stg_payments` fails with: `Binder Error: Referenced column "amount_usd" not found in FROM clause!`
- DataHub's current schema for `demo.raw_payments` lists `amount (DOUBLE)` and no `amount_usd`.
- The dataset's custom property `feed_version` changed from `1` to `2` (vendor: PayFlow),
  confirming a new feed version landed with last night's ingestion.
- `stg_payments.sql` in the model repo still selects `amount_usd` from `raw_payments`.

## Blast Radius
From DataHub downstream lineage of `demo.raw_payments`:

| Asset | Impact |
|---|---|
| `demo.stg_payments` | FAILING — direct reference to removed column |
| `demo.fct_daily_revenue` | FAILING — executive revenue dashboard is empty |
| `demo.churn_features` | FAILING — churn model inputs unavailable |
| `demo.churn_model_predictions` | **ML impact** — nightly churn scoring job will fail or score on stale features |

## Fix
Updated `stg_payments` to alias the renamed upstream column, preserving the downstream contract
(no downstream model needs to change):

```sql
amount AS amount_usd
```

Written to `fixes/stg_payments.sql`, validated with a live query, applied to the warehouse.
Post-fix health check: **all 4 models OK**.

## Who to Notify
- **Jane Doe** (jane.doe@example.com) — Data Platform Engineer, owner of `demo.raw_payments`.
  Should confirm the PayFlow v2 contract and update ingestion validation.
- **Mike Ops** (mike.ops@example.com) — Analytics Engineer, owner of all affected downstream models.

## Knowledge Written Back
- Raised DataHub incident (type `DATA_SCHEMA`) on `urn:li:dataset:(urn:li:dataPlatform:duckdb,demo.raw_payments,PROD)`:
  *"PayFlow feed v2 renamed amount_usd → amount; broke stg_payments, fct_daily_revenue, churn_features"*
  — resolved after the verified fix.
- Appended this postmortem to the DataHub documentation of `demo.raw_payments` and `demo.stg_payments`,
  so the next engineer or agent investigating a payments anomaly finds it immediately.
