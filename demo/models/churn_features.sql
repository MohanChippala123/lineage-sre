-- churn_features: per-customer features for the churn model's nightly scoring job.
SELECT
    c.customer_id,
    c.country,
    COUNT(p.payment_id)             AS payment_count,
    COALESCE(SUM(p.amount_usd), 0)  AS total_revenue_usd,
    MAX(p.payment_date)             AS last_payment_date
FROM stg_customers c
LEFT JOIN stg_payments p ON p.customer_id = c.customer_id
GROUP BY c.customer_id, c.country
