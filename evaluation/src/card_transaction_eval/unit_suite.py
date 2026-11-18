"""Offline unit/contract tests: runs the project's pytest suites and records JUnit results."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .utils import ERROR, FAIL, PASS, SKIPPED, SuiteResult, Timer

DESCRIPTION = ("pytest suites that need no workspace: tests/ (analytics logic, backend helpers, column "
               "contract, data-preparation and Genie-view static checks) and client/tests/ (MCP client "
               "against the real server over streamable HTTP with an in-memory SQLite backend, LLM stubbed).")

SUITES = [("UT-01", "tests", "server analytics, backend, contract and static checks"),
          ("UT-02", "client/tests", "MCP client and server over streamable HTTP (SQLite, stubbed LLM)")]


async def run_unit_tests(config) -> SuiteResult:
    r = SuiteResult("unit_tests", description=DESCRIPTION)
    totals = {"tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for case_id, rel, name in SUITES:
        if not (config.project_root / rel).exists():
            r.add(case_id, f"pytest {rel}", SKIPPED, reason="folder not found")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            junit = Path(tmp) / "junit.xml"
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", rel, f"--junitxml={junit}"]
            with Timer() as t:
                proc = subprocess.run(cmd, cwd=config.project_root, env=env, capture_output=True, text=True, timeout=900)
            if not junit.exists():
                r.add(case_id, f"pytest {rel}: {name}", ERROR, t.ms, exit_code=proc.returncode,
                      output=(proc.stdout[-1500:] + proc.stderr[-800:]))
                continue
            root = ET.parse(junit).getroot()
            suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
            n = {k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")}
            failed = [f"{tc.get('classname')}::{tc.get('name')}" for tc in root.iter("testcase")
                      if tc.find("failure") is not None or tc.find("error") is not None]
            passed = n["tests"] - n["failures"] - n["errors"] - n["skipped"]
            status = PASS if proc.returncode == 0 and n["tests"] > 0 else FAIL
            r.add(case_id, f"pytest {rel}: {name}", status, t.ms, tests=n["tests"], passed=passed,
                  failed=n["failures"], errors=n["errors"], skipped=n["skipped"], failed_tests=failed[:20])
            for k, v in (("tests", n["tests"]), ("passed", passed), ("failed", n["failures"]),
                         ("errors", n["errors"]), ("skipped", n["skipped"])):
                totals[k] += v
    r.metrics["totals"] = totals
    return r.finish()
