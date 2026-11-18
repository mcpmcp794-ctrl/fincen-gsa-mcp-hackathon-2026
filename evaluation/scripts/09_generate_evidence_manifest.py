from __future__ import annotations

import hashlib
from pathlib import Path

from _bootstrap import PROJECT_ROOT
from card_transaction_eval.config import load_config
from card_transaction_eval.utils import utc_now, write_json


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    config = load_config()
    files = sorted(
        [p for p in config.evidence_dir.rglob("*") if p.is_file() and p.name != ".gitkeep"]
        + [p for p in config.reports_dir.rglob("*") if p.is_file() and p.name != ".gitkeep"]
    )
    manifest = {
        "generated_at_utc": utc_now(),
        "files": [
            {
                "path": str(path.relative_to(PROJECT_ROOT)),
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in files
        ],
    }
    path = write_json(config.reports_dir / "evidence_manifest.json", manifest)
    print(path)


if __name__ == "__main__":
    main()
