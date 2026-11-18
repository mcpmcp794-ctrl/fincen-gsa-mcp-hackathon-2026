from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401
from card_transaction_eval.config import load_config
from card_transaction_eval.risk_suite import run_risk_suite
from card_transaction_eval.utils import run_async, write_json


async def main() -> None:
    config = load_config()
    result = await run_risk_suite(config)
    path = write_json(config.evidence_dir / "risk_scoring.json", result)
    print(f"{result.status.upper()}: {path}")


if __name__ == "__main__":
    run_async(main)
