"""Regression tests for render.yaml -- the DEPLOY mission's committed
Render Blueprint config (ADR §1: API on a Render free web service,
frontend on a Render Static Site).

Deliberately text-based rather than parsing with a YAML library: PyYAML
is not a declared dependency of this project (pyproject.toml has no
`yaml`/`pyyaml` entry) and this task may not add new dependencies (flag,
don't add). Plain string/line assertions are enough to pin the load-
bearing facts: the two services exist with the right runtime/plan, the
start command runs migrations before uvicorn, the health check points at
the new readiness route (not the plain liveness one), and no secret has
a real value committed.
"""

from __future__ import annotations

from pathlib import Path

RENDER_YAML = Path(__file__).resolve().parent.parent / "render.yaml"

SECRET_KEYS = [
    "DATABASE_URL",
    "SECRET_KEY",
    "RESEND_API_KEY",
    "RESEND_FROM_EMAIL",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "MISTRAL_API_KEY",
    "NVIDIA_API_KEY",
]


def _text() -> str:
    return RENDER_YAML.read_text()


def test_render_yaml_exists():
    assert RENDER_YAML.is_file(), "render.yaml must exist at the repo root (committed Render Blueprint config)"


def test_declares_both_services_with_free_plan():
    text = _text()
    assert "name: erm-backend" in text
    assert "name: erm-frontend" in text
    assert "runtime: python" in text
    assert "runtime: static" in text
    # Only the API service takes a `plan:` -- Render Static Sites have no
    # paid/free plan tiers to select, ADR §1's constraint is automatic.
    assert "plan: free" in text


def test_api_start_command_runs_migrations_before_uvicorn():
    text = _text()
    assert "startCommand:" in text
    assert "python -m app.db.migrate" in text
    assert "uvicorn app.main:app" in text
    # Migrations must run BEFORE uvicorn starts serving -- `&&` order
    # matters, not just presence of both substrings.
    migrate_pos = text.index("python -m app.db.migrate")
    uvicorn_pos = text.index("uvicorn app.main:app")
    assert migrate_pos < uvicorn_pos, "migrations must run before uvicorn in the start command"


def test_api_start_command_binds_renders_injected_port():
    assert "--port $PORT" in _text()


def test_health_check_path_is_the_new_readiness_route_not_bare_healthz():
    text = _text()
    assert "healthCheckPath: /health/ready" in text
    # Guard against silently regressing to the old liveness-only path --
    # /healthz should not appear as a *value* anywhere the healthCheckPath
    # key could plausibly be set to it.
    assert "healthCheckPath: /healthz" not in text


def test_frontend_has_no_build_step():
    text = _text()
    assert "staticPublishPath: frontend" in text


def test_trusted_proxy_hops_is_one_for_render():
    """RT-5 (docs/red-team-findings.md): Render is exactly one reverse-
    proxy hop. See render.yaml's own inline comment and
    docs/deployment.md for the full reasoning.
    """
    text = _text()
    assert 'key: TRUSTED_PROXY_HOPS' in text
    idx = text.index("key: TRUSTED_PROXY_HOPS")
    following = text[idx : idx + 200]
    assert 'value: "1"' in following


def test_no_real_secret_values_committed():
    """Every secret env var must be `sync: false` (Render prompts for a
    real value out-of-band) with no `value:` field alongside it in this
    file -- never a real key committed to the repo.
    """
    text = _text()
    for key in SECRET_KEYS:
        assert f"key: {key}" in text, f"{key} must be documented as a required env var in render.yaml"
        idx = text.index(f"key: {key}")
        # The next several lines after the key line are this entry's
        # block -- assert it declares sync: false and never a sibling
        # `value:` line before the next `- key:` entry starts.
        next_key_idx = text.find("- key:", idx + 1)
        block = text[idx : next_key_idx if next_key_idx != -1 else idx + 200]
        assert "sync: false" in block, f"{key} must be sync: false (prompted at blueprint launch), not committed"
        assert "value:" not in block, f"{key} must not carry a committed `value:` -- it is a secret"
