"""Shared result types and helpers for the evaluation suites."""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

PASS, FAIL, ERROR, WARN, SKIPPED, MANUAL = "pass", "fail", "error", "warn", "skipped", "manual_review"
STATUSES = (PASS, FAIL, ERROR, WARN, SKIPPED, MANUAL)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CaseResult:
    case_id: str
    name: str
    status: str
    duration_ms: float | None = None
    details: dict = field(default_factory=dict)


@dataclass
class SuiteResult:
    suite: str
    status: str = "running"
    started_at_utc: str = field(default_factory=utc_now)
    finished_at_utc: str | None = None
    description: str = ""
    cases: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def add(self, case_id: str, name: str, status: str, duration_ms: float | None = None, **details) -> CaseResult:
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        case = CaseResult(case_id, name, status,
                          None if duration_ms is None else round(duration_ms, 1), details)
        self.cases.append(case)
        return case

    def counts(self) -> dict:
        c = Counter(case.status for case in self.cases)
        return {s: c.get(s, 0) for s in STATUSES}

    def finish(self, status: str | None = None) -> "SuiteResult":
        """Suite status: any fail/error -> fail; all skipped -> skipped;
        any manual_review -> manual_review; any warn -> warn; otherwise pass."""
        self.finished_at_utc = utc_now()
        counts = self.counts()
        if status:
            self.status = status
        elif counts[ERROR] or counts[FAIL]:
            self.status = FAIL
        elif self.cases and counts[SKIPPED] == len(self.cases):
            self.status = SKIPPED
        elif counts[MANUAL]:
            self.status = MANUAL
        elif counts[WARN]:
            self.status = WARN
        else:
            self.status = PASS
        self.metrics["case_counts"] = counts
        return self


def _default(o: Any):
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def write_json(path, obj) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, default=_default, indent=2) + "\n")
    return path


def read_json(path):
    return json.loads(Path(path).read_text())


def short(obj, n: int = 160) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, default=_default)
    return s if len(s) <= n else s[: n - 3] + "..."


class Timer:
    """Context manager recording elapsed wall time in milliseconds (``.ms``)."""

    def __enter__(self):
        self.ms = None
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = (time.perf_counter() - self._t0) * 1000
        return False


def percentile(values, p: float):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def latency_stats(values) -> dict:
    if not values:
        return {"n": 0}
    r = lambda v: round(v, 1)  # noqa: E731
    return {"n": len(values), "min_ms": r(min(values)), "p50_ms": r(percentile(values, 50)),
            "p95_ms": r(percentile(values, 95)), "mean_ms": r(sum(values) / len(values)),
            "max_ms": r(max(values))}


_NUM = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$")


def numbers_in(obj) -> list:
    """All numeric values anywhere in a nested result (numeric strings included)."""
    out = []
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, (int, float, Decimal)):
        out.append(float(obj))
    elif isinstance(obj, str):
        s = obj.strip().replace(",", "").lstrip("$").rstrip("%")
        if _NUM.match(s):
            out.append(float(s))
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(numbers_in(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(numbers_in(v))
    return out


def close(a, b, abs_tol: float = 0.01, rel_tol: float = 1e-9) -> bool:
    if a is None or b is None:
        return False
    return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=abs_tol)


def workspace_client():
    try:
        from databricks.sdk import WorkspaceClient
        return WorkspaceClient()
    except Exception:
        return None


def http_status(url: str, method: str = "GET", token: str | None = None, json_body=None, timeout: int = 20):
    """Return (status_code or None, short error text). Never follows redirects."""
    import requests
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.request(method, url, headers=headers, json=json_body,
                                timeout=timeout, allow_redirects=False)
        return resp.status_code, ""
    except Exception as exc:
        return None, repr(exc)[:300]


def run_async(coro_factory):
    """Run ``coro_factory()`` to completion and return its result.

    Works from a plain ``python`` process and from a notebook/IPython kernel that
    already has a running event loop (there the coroutine runs in a worker thread).
    """
    import asyncio
    import threading
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    box = {}

    def worker():
        try:
            box["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # re-raised in the caller's thread
            box["error"] = exc

    t = threading.Thread(target=worker, name="eval-async")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def quiet_logs() -> None:
    for name in ("mcp", "databricks.sql", "databricks.sdk", "httpx", "urllib3", "uvicorn"):
        logging.getLogger(name).setLevel(logging.WARNING)
