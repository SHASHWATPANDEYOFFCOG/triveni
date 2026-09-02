"""`triveni` console entry point - a thin dispatcher onto tasks.py targets."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    sys.argv = ["tasks.py", *sys.argv[1:]]
    runpy.run_path(str(ROOT / "tasks.py"), run_name="__main__")


if __name__ == "__main__":
    main()
