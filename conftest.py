"""Root conftest: puts the repo root on sys.path and pins determinism for tests."""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

from hypothesis import HealthCheck, settings

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Hypothesis' 200ms per-example deadline is a *latency* assertion, and none of the
# property tests in this repo are about latency - they assert logical properties like
# "canonical_json returns the same bytes twice" and "allocate conserves every paise".
#
# It bit us exactly as you would expect: a full-suite run that had just fitted several
# hundred gradient-boosted models stalled long enough for one `canonical_json` call to
# exceed 200ms, and the suite reported a failure in the module every Merkle proof
# depends on. The call takes 0.16ms on an idle machine and 200,000 randomised values
# show zero instability - the code was never the problem, and a test that fails on a
# loaded laptop is worse than no test because of what it looks like.
#
# Timing belongs in `make bench`, which measures it deliberately and reports it.
settings.register_profile(
    "triveni",
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("triveni")

os.environ.setdefault("PYTHONHASHSEED", "0")
os.environ.setdefault("TRIVENI_RAILS", "offline")
os.environ.setdefault("TRIVENI_LLM_MODE", "replay")
os.environ.setdefault("TRIVENI_SEED", "20260101")
random.seed(20260101)
