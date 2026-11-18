from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401
from card_transaction_eval.config import load_config
from card_transaction_eval.reporting import generate_reports


def main() -> None:
    config = load_config()
    outputs = generate_reports(config)
    for name, path in outputs.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
