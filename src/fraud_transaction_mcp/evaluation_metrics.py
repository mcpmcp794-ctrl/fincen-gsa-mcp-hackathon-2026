"""EVALUATION ONLY: score the deterministic review cues against fraud ground truth.

This is the only module that reads the restricted label table. It returns
aggregate confusion-matrix metrics, never per-transaction labels, and its tool is
registered only when ENABLE_EVALUATION_TOOLS=true. Analysis tools never import it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import risk_scoring
from .analytics import _date_where, _dates, _table

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2}")


def _safe_div(a, b):
    return round(a / b, 6) if b else None


def evaluate_review_signals(backend, label_table, start, end, amount_threshold=500.0):
    if not start or not end:
        raise ValueError("start and end (YYYY-MM-DD) are required")
    if not isinstance(label_table, str) or not _IDENT.fullmatch(label_table):
        raise ValueError("Unsafe label table identifier")
    if isinstance(amount_threshold, bool) or not isinstance(amount_threshold, (int, float)) \
            or not 0 < amount_threshold <= 1e9:
        raise ValueError("amount_threshold must be a positive number")
    clauses, params = _date_where(start, end)
    where = " AND ".join("c." + c for c in clauses)
    flagged = ("CASE WHEN c.amount >= ? OR LOWER(c.use_chip) LIKE '%online%' "
               "OR (c.Errors IS NOT NULL AND TRIM(c.Errors) <> '' "
               "AND LOWER(TRIM(c.Errors)) NOT IN ('none','nan')) THEN 1 ELSE 0 END")
    sql = (
        "SELECT SUM(CASE WHEN flagged=1 AND label=1 THEN 1 ELSE 0 END) AS tp, "
        "SUM(CASE WHEN flagged=1 AND label=0 THEN 1 ELSE 0 END) AS fp, "
        "SUM(CASE WHEN flagged=0 AND label=1 THEN 1 ELSE 0 END) AS fn, "
        "SUM(CASE WHEN flagged=0 AND label=0 THEN 1 ELSE 0 END) AS tn "
        f"FROM (SELECT {flagged} AS flagged, CAST(l.Is_Fraud AS INT) AS label "
        f"FROM {_table(backend)} c JOIN {label_table} l ON c.transaction_id = l.transaction_id "
        f"WHERE {where}) s")
    row = backend.query(sql, [float(amount_threshold)] + params)[0]
    tp, fp, fn, tn = (int(row.get(k) or 0) for k in ("tp", "fp", "fn", "tn"))
    total = tp + fp + fn + tn
    precision, recall = _safe_div(tp, tp + fp), _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * precision * recall, precision + recall) if precision and recall else None
    return {
        "rule": "HIGH_AMOUNT or ONLINE_PURCHASE or ERROR_RECORDED (review_signals)",
        "amount_threshold": float(amount_threshold), "start": start, "end": end,
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "transactions": total, "fraud_rate": _safe_div(tp + fn, total),
        "precision": precision, "recall": recall, "f1": f1,
        "accuracy": _safe_div(tp + tn, total),
        "note": "Evaluation only. Aggregate metrics; no per-transaction labels are returned.",
    }


def reference_risk_sql(table, label_table, max_cards, params=None):
    """Reference implementation of risk_indicators.md (evaluation only), aggregated by
    risk_score with fraud-label counts. Placeholders: start, end, end, start."""
    p = params or risk_scoring.parameters()
    hours = int(p["velocity_window_hours"])
    return (
        f"WITH cards AS (SELECT card_key FROM {table} WHERE transaction_time >= ? "
        f"AND transaction_time < ? GROUP BY card_key ORDER BY xxhash64(card_key) LIMIT {int(max_cards)}), "
        f"hist AS (SELECT c.transaction_id, c.card_key, c.transaction_time, c.amount, c.use_chip, "
        f"c.merchant_id, c.merchant_state, c.zip_code FROM {table} c JOIN cards k ON c.card_key = k.card_key "
        f"WHERE c.transaction_time < ?), "
        "f AS (SELECT *, "
        "COUNT(*) OVER (PARTITION BY card_key ORDER BY transaction_time, transaction_id "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_n, "
        f"percentile_approx(amount, {float(p['amount_percentile'])}) OVER (PARTITION BY card_key "
        "ORDER BY transaction_time, transaction_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_p, "
        "COUNT(*) OVER (PARTITION BY card_key ORDER BY transaction_time "
        f"RANGE BETWEEN INTERVAL {hours} HOURS PRECEDING AND CURRENT ROW) AS n_win, "
        "MIN(transaction_time) OVER (PARTITION BY card_key) AS first_time, "
        "ROW_NUMBER() OVER (PARTITION BY card_key, merchant_id ORDER BY transaction_time, transaction_id) AS m_seq, "
        "ROW_NUMBER() OVER (PARTITION BY card_key, use_chip ORDER BY transaction_time, transaction_id) AS c_seq, "
        "ROW_NUMBER() OVER (PARTITION BY card_key, coalesce(merchant_state, 'ONLINE'), "
        "coalesce(zip_code, 'ONLINE') ORDER BY transaction_time, transaction_id) AS l_seq FROM hist), "
        "s AS (SELECT transaction_id, "
        f"CASE WHEN prior_n >= {int(p['amount_min_history'])} AND amount >= prior_p THEN 1 ELSE 0 END AS a, "
        f"CASE WHEN n_win >= {int(p['velocity_min_count'])} AND n_win > {float(p['velocity_multiplier'])} * "
        "(prior_n / greatest(datediff(transaction_time, first_time), 1)) THEN 1 ELSE 0 END AS v, "
        "CASE WHEN prior_n > 0 AND (c_seq = 1 OR l_seq = 1) THEN 1 ELSE 0 END AS l, "
        "CASE WHEN prior_n > 0 AND m_seq = 1 THEN 1 ELSE 0 END AS m "
        "FROM f WHERE transaction_time >= ?) "
        "SELECT (s.a + s.v + s.l + s.m) / 4.0 AS risk_score, COUNT(*) AS n, "
        "SUM(y.label) AS fraud, SUM(s.a) AS a, SUM(s.v) AS v, SUM(s.l) AS l, SUM(s.m) AS m, "
        "SUM(s.a * y.label) AS a_fraud, SUM(s.v * y.label) AS v_fraud, "
        "SUM(s.l * y.label) AS l_fraud, SUM(s.m * y.label) AS m_fraud "
        f"FROM s JOIN (SELECT transaction_id, CAST(Is_Fraud AS INT) AS label FROM {label_table}) y "
        "ON s.transaction_id = y.transaction_id GROUP BY 1 ORDER BY 1")


def _confusion(tp, fp, fn, tn):
    precision, recall = _safe_div(tp, tp + fp), _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * precision * recall, precision + recall) if precision and recall else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": precision,
            "recall": recall, "f1": f1}


def evaluate_risk_indicators(backend, label_table, start, end, max_cards=500):
    """Score the rule-based risk indicators against fraud ground truth on a deterministic
    sample of cards active in [start, end]. Aggregate metrics only."""
    if not start or not end:
        raise ValueError("start and end (YYYY-MM-DD) are required")
    _dates(start, end)
    if not isinstance(label_table, str) or not _IDENT.fullmatch(label_table):
        raise ValueError("Unsafe label table identifier")
    if isinstance(max_cards, bool) or not isinstance(max_cards, int) or not 1 <= max_cards <= 2000:
        raise ValueError("max_cards must be an integer between 1 and 2000")
    p = risk_scoring.parameters()
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
    if (e - s).days > p["max_scoring_window_days"]:
        raise ValueError(f"Window must be at most {p['max_scoring_window_days']} days")
    s_ts, e_ts = s.strftime("%Y-%m-%d %H:%M:%S"), e.strftime("%Y-%m-%d %H:%M:%S")
    rows = backend.query(reference_risk_sql(_table(backend), label_table, max_cards, p),
                         [s_ts, e_ts, e_ts, s_ts])
    total = sum(int(r.get("n") or 0) for r in rows)
    fraud = sum(int(r.get("fraud") or 0) for r in rows)
    base = _safe_div(fraud, total)
    by_score, bands = [], {}
    for r in rows:
        score, n, f = float(r["risk_score"]), int(r.get("n") or 0), int(r.get("fraud") or 0)
        by_score.append({"risk_score": score, "transactions": n, "fraud": f,
                         "fraud_rate": _safe_div(f, n)})
        b = bands.setdefault(risk_scoring.band(score, p), {"transactions": 0, "fraud": 0})
        b["transactions"] += n
        b["fraud"] += f
    for b in bands.values():
        b["fraud_rate"] = _safe_div(b["fraud"], b["transactions"])
        b["lift"] = _safe_div(b["fraud_rate"], base) if b["fraud_rate"] is not None and base else None
    indicators = {}
    for key, name in zip("avlm", risk_scoring.INDICATORS):
        flagged = sum(int(r.get(key) or 0) for r in rows)
        tp = sum(int(r.get(f"{key}_fraud") or 0) for r in rows)
        m = _confusion(tp, flagged - tp, fraud - tp, total - flagged - (fraud - tp))
        m["flag_rate"] = _safe_div(flagged, total)
        m["lift"] = _safe_div(m["precision"], base) if m["precision"] is not None and base else None
        indicators[name] = m
    thresholds = {}
    for name, cut in (("medium_or_high", p["band_medium"]), ("any_indicator", 0.25)):
        tp = sum(x["fraud"] for x in by_score if x["risk_score"] >= cut)
        flagged = sum(x["transactions"] for x in by_score if x["risk_score"] >= cut)
        thresholds[name] = _confusion(tp, flagged - tp, fraud - tp, total - flagged - (fraud - tp))
    return {
        "method": "rule_based", "parameters": p, "start": start, "end": end,
        "cards_sampled": max_cards, "transactions": total, "fraud": fraud, "base_fraud_rate": base,
        "by_score": by_score, "bands": bands, "indicators": indicators, "thresholds": thresholds,
        "note": ("Evaluation only. Reference implementation of risk_indicators.md on a "
                 "deterministic card sample; aggregate metrics, no per-transaction labels."),
    }
