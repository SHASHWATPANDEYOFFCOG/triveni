# ADR 0003 - Dependencies were verified before being written down

**Status:** accepted - 2026-09-02

**Context.** Rule A.6 forbids writing code against an API that has not been confirmed.
This machine runs Python 3.14.3, new enough that wheels are not guaranteed.

**Decision.** Before any pin entered `pyproject.toml`, each package was installed into
`.venv` and its exact API *executed*: `scipy.optimize.linear_sum_assignment` and
`nnls`, `ortools.sat.python.cp_model` solving a toy CP-SAT to OPTIMAL,
`cryptography` Ed25519 sign/verify plus raw-seed export, `sklearn`
`HistGradientBoostingRegressor(loss="quantile")`, `statsmodels`
`ExponentialSmoothing(trend="add", seasonal="add", seasonal_periods=7)`, pydantic v2
frozen models, duckdb, and FastAPI `StreamingResponse` (which is how SSE is done -
no extra dependency). Starlette 1.6 needs `httpx2`, not `httpx`, for its TestClient;
that was discovered by running it, and is pinned in the dev extra.

**Consequence.** No fallbacks were needed: the global assignment solver, CP-SAT
many-to-one disambiguation and Ed25519 tree-head signing are all real. Two heavy
components are still deliberately excluded - `torch`/`sentence-transformers` and any
time-series foundation model - because an offline cold start must not download
gigabytes. Their slots are feature-flagged (`TRIVENI_ENABLE_*=0`) and the default
implementations are local; see ADR 0004 when that embedding work lands at M7.
