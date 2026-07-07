-- fct_daily_revenue: daily revenue rollup, powers the executive dashboard.
SELECT
    payment_date,
    currency,
    SUM(amount_usd) AS revenue_usd,
    COUNT(*)        AS payment_count
FROM stg_payments
GROUP BY payment_date, currency
