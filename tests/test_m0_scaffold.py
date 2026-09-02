"""M0 gate: the repo runs cold, with no credentials, and exposes no secrets."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_health_endpoint_returns_200_without_credentials() -> None:
    """The DoD for M0. Note we scrub every credential env var first: a green test
    here must mean 'needs no keys', not 'the developer happened to have keys'."""
    for var in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(var, None)

    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["credentials_required"] is False
    assert body["rails"] == "offline", "the default rail must never be a live API"


def test_openapi_schema_generates() -> None:
    from api.main import app

    schema = app.openapi()
    assert schema["info"]["title"] == "Triveni"
    assert "/health" in schema["paths"]


def test_no_secrets_committed() -> None:
    from scripts.secretscan import scan

    findings = scan()
    assert findings == [], f"credential-shaped strings found: {findings}"


def test_env_example_lists_every_var_with_no_values() -> None:
    """.env.example documents every variable and holds no values (A.5)."""
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assignments = [ln for ln in lines if "=" in ln and not ln.lstrip().startswith("#")]
    assert assignments, ".env.example must list the variables"
    for line in assignments:
        key, _, value = line.partition("=")
        assert key.strip() == key, f"malformed line: {line!r}"
        if key in {"RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "ANTHROPIC_API_KEY"}:
            assert value == "", f"{key} must ship empty, got {value!r}"


@pytest.mark.parametrize(
    "target", ["setup", "run", "test", "eval", "demo", "bench", "verify", "help"]
)
def test_every_required_make_target_exists(target: str) -> None:
    """PART A.5 names these targets explicitly; the Makefile and tasks.py must agree."""
    source = (ROOT / "tasks.py").read_text(encoding="utf-8")
    assert f'@target("{target}"' in source, f"tasks.py is missing the {target} target"
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert target in makefile.split("\n")[7], f"Makefile does not expose {target}"


def test_tasks_runner_is_invokable_as_a_subprocess() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tasks.py"), "help"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "Triveni targets" in result.stdout
