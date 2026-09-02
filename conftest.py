"""Root conftest: puts the repo root on sys.path and pins determinism for tests."""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONHASHSEED", "0")
os.environ.setdefault("TRIVENI_RAILS", "offline")
os.environ.setdefault("TRIVENI_LLM_MODE", "replay")
os.environ.setdefault("TRIVENI_SEED", "20260101")
random.seed(20260101)
