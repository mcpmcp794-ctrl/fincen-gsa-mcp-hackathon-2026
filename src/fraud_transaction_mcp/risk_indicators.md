# Rule-Based Risk Indicators (method: rule_based, version 1)

Single source of truth for the agent-driven risk scoring method. The
`get_risk_scoring_guidance` tool returns this file plus the parsed parameters
below. Edit thresholds in the `parameters` block only; code reads them from here.

Risk scores are **investigative aids for human review, not fraud determinations.**
Fraud labels are never used to compute them.

```parameters
amount_percentile = 0.95
amount_min_history = 10
velocity_window_hours = 24
velocity_min_count = 5
velocity_multiplier = 3
band_medium = 0.5
band_high = 0.75
max_scoring_window_days = 31
```

## When to use this method
Use it for every fraud **assessment** question: assessing, scoring or ranking the fraud
risk or fraud likelihood of transactions, cards or cardholders (for example "which users
are most likely to have fraudulent transactions", "is transaction X suspicious", "top 5
cards by fraud risk"). Answer these with this server's risk scores; do not refer them
elsewhere. Only questions asking for *recorded* fraud labels (how many transactions are
labelled fraud, the label of transaction X, the labelled fraud rate) need the label data
that this server does not have.

## Point-in-time rule (applies to every indicator)
Each transaction is compared only with the **same card's (`card_key`) earlier
transactions**, ordered by `transaction_time, transaction_id`. Never use the
transaction itself or later transactions as history. Build history in a CTE that
reads the card's transactions before the end of the scoring window, compute the
indicators with window functions, then keep only rows inside the scoring window.
A transaction with no earlier history scores 0 on every indicator.

## Indicators (each is 0 or 1)

1. **amount_anomaly** — 1 when the card has at least `amount_min_history` earlier
   transactions and `amount` is at or above the `amount_percentile` of those earlier
   amounts. Suggested SQL: `percentile_approx(amount, <p>) OVER (PARTITION BY card_key
   ORDER BY transaction_time, transaction_id ROWS BETWEEN UNBOUNDED PRECEDING AND
   1 PRECEDING)`.
2. **velocity_anomaly** — 1 when the number of the card's transactions in the
   `velocity_window_hours` ending at this transaction (inclusive) is at least
   `velocity_min_count` AND greater than `velocity_multiplier` times the card's
   earlier average transactions per day (earlier count / max(days since the card's
   first transaction, 1)). Suggested SQL: `COUNT(*) OVER (PARTITION BY card_key ORDER
   BY transaction_time RANGE BETWEEN INTERVAL 24 HOURS PRECEDING AND CURRENT ROW)`.
3. **location_channel_change** — 1 when the card has earlier history and this is the
   first time the card uses this channel (`use_chip`) OR this location
   (`merchant_state`, `zip_code`; treat nulls as 'ONLINE'). Suggested SQL:
   `ROW_NUMBER() OVER (PARTITION BY card_key, use_chip ORDER BY transaction_time,
   transaction_id) = 1`, and the same over the location.
4. **merchant_novelty** — 1 when the card has earlier history and this is the first
   time the card uses this `merchant_id`.

## Score and bands
`risk_score = (amount_anomaly + velocity_anomaly + location_channel_change +
merchant_novelty) / 4.0`, so the possible values are 0, 0.25, 0.5, 0.75 and 1.

* `risk_band = 'high'` when risk_score >= `band_high`
* `risk_band = 'medium'` when risk_score >= `band_medium`
* otherwise `risk_band = 'low'`

## Required output columns
Every scoring query must return these columns (extra evidence columns such as the
earlier 95th percentile or 24-hour count are encouraged):
`transaction_id, card_key, transaction_time, amount, amount_anomaly,
velocity_anomaly, location_channel_change, merchant_novelty, risk_score, risk_band`.
Order by `risk_score DESC, transaction_time DESC`.

## Ranking output (users or cards)
For "which / top N users (or cards) by fraud risk" questions, score every transaction in
the window exactly as above, then aggregate per entity in the same query and return:
`user_id` (or `card_key`), `transactions_scored`, `high_count` (risk_band = high),
`medium_count` (risk_band = medium), `avg_risk_score`, `max_risk_score`.
Order by `high_count DESC, medium_count DESC, avg_risk_score DESC` and LIMIT N.
A population ranking restricts history to cards active in the window, for example
`card_key IN (SELECT DISTINCT card_key FROM <table> WHERE transaction_time >= <start>
AND transaction_time < <end>)`, and takes about a minute on the SQL warehouse for a
one-month window. If the user gives no period, use the most recent full month
(2020-01-01 to 2020-02-01) and say so.

## Scope rules
* Scope every query with a WHERE predicate on `card_key`, `user_id` or
  `transaction_id`, or on `transaction_time` for population rankings.
* Scoring windows are at most `max_scoring_window_days` days.
* Run the SQL with `run_risk_sql`. It must be one read-only SELECT (CTEs allowed)
  over the analysis table only; comments, semicolons and fraud-label terms are rejected.
* Data covers 2010-01-01 to 2020-02-28.

## Verification
`run_risk_sql` checks every returned row: indicators are 0/1 and `risk_score`
equals their sum / 4. The Verify Agent should also re-check at least one flagged
indicator with a different tool, for example `search_transactions` on the same
`card_key` and `merchant_id` with `end` before the transaction date to confirm
merchant novelty, or `get_cardholder_profile` to sanity-check the amount baseline.

## Interpretation
Indicators describe departures from the card's own history. They are common in
legitimate activity (new merchants, travel, a large one-off purchase). Report them
as "risk indicators for human review"; never state that a transaction or cardholder
is fraudulent. Questions about recorded fraud labels go to the Fraud Detection
Genie space.
