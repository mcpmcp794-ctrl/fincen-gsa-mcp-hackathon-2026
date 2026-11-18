"""Make the evaluation package importable when scripts are run from the repository."""
from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parents[1]
# The card_transaction_eval package lives in evaluation/src/.
EVAL_SRC = PROJECT_ROOT / "evaluation" / "src"
# PROJECT_ROOT/src makes the fraud_transaction_mcp package importable.
for path in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src"), str(EVAL_SRC), str(SCRIPTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)
