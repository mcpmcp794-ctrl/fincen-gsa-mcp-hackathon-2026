from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401
from card_transaction_eval.config import load_config
from card_transaction_eval.genie_suite import run_genie_benchmark
from card_transaction_eval.utils import run_async, write_json


async def main() -> None:
    config = load_config()
    result = await run_genie_benchmark(config)
    path = write_json(config.evidence_dir / "genie_benchmark.json", result)
    print(f"{result.status.upper()}: {path}")
    if result.status == "manual_review":
        print("Review each Genie answer against the captured reference SQL rows.")


if __name__ == "__main__":
    run_async(main)
