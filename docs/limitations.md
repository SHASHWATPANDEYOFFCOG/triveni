# Limitations - what Triveni does not do, and what is unverified

Kept honestly and updated every milestone. A complete system with stated limits
beats a half-finished ambitious one.

## Unverified on the build machine
- **`docker compose up` is written but not executed here.** Docker is not installed
  on this Windows development machine, so the Dockerfiles and `docker-compose.yml`
  are reviewed-but-untested. The supported, exercised path is `make setup && make demo`.

## Deliberately excluded
- **No `torch` / `sentence-transformers`.** A cold `make demo` must not download
  gigabytes. Dense blocking uses a local, deterministic hashed character n-gram
  embedding instead; the neural slot is feature-flagged off (`TRIVENI_ENABLE_NEURAL_EMBEDDINGS=0`).
- **No time-series foundation model by default** (`TRIVENI_ENABLE_TSFM=0`), same reason.
  If the boosted baseline beats it, that result gets published as-is.

## Scope
- Triveni is **read-only with respect to money**. It proposes; it never posts to a
  bank or calls a Razorpay write tool. This is enforced by an allowlist plus a test,
  not by convention.
