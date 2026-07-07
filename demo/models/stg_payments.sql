-- stg_payments: cleaned payments feed.
-- Downstream contract: payment_id, customer_id, amount_usd, currency, payment_date
SELECT
    payment_id,
    customer_id,
    amount_usd,
    currency,
    CAST(created_at AS DATE) AS payment_date
FROM raw_payments
