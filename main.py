from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    # Allow `python main.py ...` to run directly from the repository root.
    src_dir = Path(__file__).resolve().parent / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    # Default to the safest runtime mode for local launches from an IDE.
    if len(sys.argv) == 1:
        sys.argv.append("shadow")

    from ibkr_micro_alpha.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
