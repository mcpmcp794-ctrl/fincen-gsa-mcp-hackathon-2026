from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401
from card_transaction_eval.config import load_config
from card_transaction_eval.hybrid_suite import create_hybrid_worksheet
from card_transaction_eval.utils import write_json


def main() -> None:
    config = load_config()
    result, worksheet = create_hybrid_worksheet(config)
    result_path = write_json(config.evidence_dir / "hybrid_routing.json", result)
    print(f"{result.status.upper()}: {result_path}")
    print(f"Complete the manual worksheet: {worksheet}")


if __name__ == "__main__":
    main()
