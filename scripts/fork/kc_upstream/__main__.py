"""``python3 scripts/fork/kc_upstream <command>``: run the CLI from a checkout."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kc_upstream.cli import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
