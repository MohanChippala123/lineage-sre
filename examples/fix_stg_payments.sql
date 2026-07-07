-- FIX proposed by Lineage SRE
-- Root cause: PayFlow feed v2 renamed raw_payments.amount_usd to amount (confirmed via
-- DataHub schema for demo.raw_payments, feed_version custom property 1 -> 2).
-- This fix aliases the new upstream column back to amount_usd, preserving the downstream
-- contract of stg_payments (fct_daily_revenue and churn_features keep working unchanged).
SELECT
    payment_id,
    customer_id,
    amount AS amount_usd,
    currency,
    CAST(created_at AS DATE) AS payment_date
FROM raw_payments
