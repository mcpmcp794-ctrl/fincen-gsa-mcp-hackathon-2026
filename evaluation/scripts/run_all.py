from __future__ import annotations

import argparse
import runpy
import sys
import traceback

from _bootstrap import PROJECT_ROOT, SCRIPTS_DIR  # noqa: F401
from card_transaction_eval.config import check_auth, load_config
from card_transaction_eval.custom_suite import run_custom_functional
from card_transaction_eval.genie_suite import run_genie_benchmark
from card_transaction_eval.hybrid_suite import create_hybrid_worksheet
from card_transaction_eval.judge_suite import run_llm_judge
from card_transaction_eval.performance_suite import run_performance_suite
from card_transaction_eval.preflight import run_preflight
from card_transaction_eval.reconciliation import run_reconciliation
from card_transaction_eval.reporting import generate_reports
from card_transaction_eval.risk_suite import run_risk_suite
from card_transaction_eval.security_suite import run_security_suite
from card_transaction_eval.unit_suite import run_unit_tests
from card_transaction_eval.utils import ERROR, FAIL, SuiteResult, run_async, write_json


async def run(args: argparse.Namespace) -> int:
    """Run the selected suites; returns 0 (pass / pass with findings), 1 (failures) or 2 (no auth)."""
    config = load_config(args.config)
    ok, message = check_auth(config)
    print(f"Auth: {message}")
    if not ok:
        return 2
    suites = [("preflight.json", run_preflight)]
    if not args.skip_unit:
        suites.append(("unit_tests.json", run_unit_tests))
    if not args.skip_custom:
        suites.extend(
            [
                ("custom_functional.json", run_custom_functional),
                ("sql_reconciliation.json", run_reconciliation),
                ("security_negative.json", run_security_suite),
            ]
        )
        if not args.skip_performance:
            suites.append(("performance.json", run_performance_suite))
    if not args.skip_genie and config.enable_genie:
        suites.append(("genie_benchmark.json", run_genie_benchmark))
    if not args.skip_judge and config.enable_judge:
        suites.append(("llm_judge.json", run_llm_judge))
    if not args.skip_risk and config.enable_risk_suite:
        suites.append(("risk_scoring.json", run_risk_suite))

    for filename, runner in suites:
        print(f"Running {filename} ...")
        try:
            result = await runner(config)
        except Exception as exc:  # a crashed suite is recorded as evidence; the run continues
            result = SuiteResult(filename.removesuffix(".json"))
            result.add("RUN-ERR", "Suite crashed before completing", ERROR, error=repr(exc)[:500],
                       traceback=traceback.format_exc()[-2000:])
            result.finish()
        write_json(config.evidence_dir / filename, result)
        counts = result.metrics.get("case_counts", {})
        print(f"  {result.status.upper()}  " + ", ".join(f"{k}={v}" for k, v in counts.items() if v))

    routing_result, worksheet = create_hybrid_worksheet(config)
    write_json(config.evidence_dir / "hybrid_routing.json", routing_result)
    print(f"Hybrid worksheet: {worksheet}")

    runpy.run_path(str(SCRIPTS_DIR / "08_capture_metadata.py"), run_name="__main__")
    outputs = generate_reports(config)
    print("Reports:")
    for name, path in outputs.items():
        print(f"  {name}: {path}")
    runpy.run_path(str(SCRIPTS_DIR / "09_generate_evidence_manifest.py"), run_name="__main__")
    overall = __import__("json").loads(outputs["json"].read_text())["overall_status"]
    print(f"Overall: {overall}")
    return 1 if overall in (FAIL, "no_evidence") else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run post-deployment MCP evaluation")
    parser.add_argument("--config", default=None, help="Path to evaluation YAML")
    parser.add_argument("--skip-unit", action="store_true")
    parser.add_argument("--skip-custom", action="store_true")
    parser.add_argument("--skip-genie", action="store_true")
    parser.add_argument("--skip-performance", action="store_true")
    parser.add_argument("--skip-judge", action="store_true", help="Skip the LLM-as-judge agent critique suite")
    parser.add_argument("--skip-risk", action="store_true", help="Skip the rule-based risk scoring suite")
    args = parser.parse_args(argv)
    return run_async(lambda: run(args))


if __name__ == "__main__":
    sys.exit(main())
