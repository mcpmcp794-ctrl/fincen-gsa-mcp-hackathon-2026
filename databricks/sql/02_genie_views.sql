-- Run after 01_prepare_card_transaction_mcp. Uses existing workspace.default schema.
-- Option A: add these views (and the clean table if permitted) to Genie.
-- Never expose workspace.default.card_transaction_mcp_evaluation_labels.
-- Views use the card_transaction_mcp_ prefix so they never replace views built on
-- the original card_transaction_clean table.

CREATE OR REPLACE VIEW workspace.default.card_transaction_mcp_cardholder_summary AS
SELECT user_id, card_key,
       COUNT(*) AS transaction_count,
       COUNT(DISTINCT merchant_id) AS distinct_merchants,
       SUM(amount) AS net_spend,
       SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS positive_purchases,
       SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) AS refunds_or_credits,
       MIN(transaction_time) AS first_activity,
       MAX(transaction_time) AS last_activity
FROM workspace.default.card_transaction_mcp_clean
GROUP BY user_id, card_key;

CREATE OR REPLACE VIEW workspace.default.card_transaction_mcp_merchant_summary AS
SELECT merchant_id, mcc, merchant_state,
       COUNT(*) AS transaction_count,
       COUNT(DISTINCT card_key) AS unique_cards,
       SUM(amount) AS net_spend
FROM workspace.default.card_transaction_mcp_clean
GROUP BY merchant_id, mcc, merchant_state;

CREATE OR REPLACE VIEW workspace.default.card_transaction_mcp_daily_card_spend AS
SELECT user_id, card_key, CAST(transaction_time AS DATE) AS transaction_date,
       COUNT(*) AS transaction_count,
       SUM(amount) AS net_spend,
       COUNT(DISTINCT merchant_id) AS distinct_merchants
FROM workspace.default.card_transaction_mcp_clean
GROUP BY user_id, card_key, CAST(transaction_time AS DATE);

CREATE OR REPLACE VIEW workspace.default.card_transaction_mcp_customer_merchant_edges AS
SELECT user_id, card_key, merchant_id,
       COUNT(*) AS purchase_count, SUM(amount) AS net_spend,
       MIN(transaction_time) AS first_seen,
       MAX(transaction_time) AS last_seen
FROM workspace.default.card_transaction_mcp_clean
GROUP BY user_id, card_key, merchant_id;
