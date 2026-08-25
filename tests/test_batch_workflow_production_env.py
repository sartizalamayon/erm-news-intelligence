"""The nightly batch runs `APP_ENV=production`, so it is subject to the
same startup fail-fast as the API — and its env block did not satisfy it.

`.github/workflows/daily-batch.yml` sets `APP_ENV: production` and runs
`python -m app.batch`, whose `main()` calls
`app.config.validate_production_settings`. That check now also validates
the credentials of the email transport `email_provider` selects.
`email_provider` defaults to `brevo`; the workflow declared
`RESEND_API_KEY` and no Brevo pair. Reproduced directly against those
exact env vars:

    ConfigurationError: Missing required production configuration:
    BREVO_API_KEY (required because EMAIL_PROVIDER=brevo),
    BREVO_FROM_EMAIL (required because EMAIL_PROVIDER=brevo)

Every scheduled run would have died there, before fetching a single
article — a red X every morning and an empty product, from a workflow
that had never once executed because the repo has no remote yet. The
batch sends no email; it needs those values only because the production
check is deliberately one whole-process definition of "is this
deployment configured" rather than two that can disagree.

These assertions are derived from `app.config` rather than written out,
so the workflow cannot drift from the check again: switch
`EMAIL_PROVIDER` in the workflow and the required secrets switch with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import _EMAIL_PROVIDER_REQUIRED_ENV, Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DAILY_BATCH = _REPO_ROOT / ".github" / "workflows" / "daily-batch.yml"

# The four bootstrap provider keys `validate_production_settings` accepts
# — any ONE is enough, because routing degrades per feature rather than
# failing.
_PROVIDER_KEYS = ("GROQ_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY", "NVIDIA_API_KEY")


@pytest.fixture(scope="module")
def batch_job_env() -> dict[str, str]:
    workflow = yaml.safe_load(_DAILY_BATCH.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert len(jobs) == 1, "daily-batch.yml grew a second job; this test assumes one"
    return dict(next(iter(jobs.values()))["env"])


def test_the_batch_job_really_does_run_as_production(batch_job_env: dict[str, str]):
    """Guards everything below: if APP_ENV were not production,
    `validate_production_settings` would return early and none of these
    requirements would apply.
    """
    assert batch_job_env.get("APP_ENV") == "production"


@pytest.mark.parametrize("name", ("DATABASE_URL", "SECRET_KEY"))
def test_unconditionally_required_production_vars_are_declared(batch_job_env: dict[str, str], name: str):
    assert name in batch_job_env, f"{name} is required in production and the batch job does not declare it"


def test_at_least_one_llm_provider_key_is_declared(batch_job_env: dict[str, str]):
    """`validate_production_settings` accepts any one of the four. The
    batch is the only thing that spends LLM quota, so "at least one" is
    not a formality here.
    """
    assert any(key in batch_job_env for key in _PROVIDER_KEYS), (
        "the batch job declares none of "
        + ", ".join(_PROVIDER_KEYS)
        + " -- production startup refuses to run without one"
    )


def test_the_selected_email_providers_credentials_are_declared(batch_job_env: dict[str, str]):
    """THE regression. Derived from `_EMAIL_PROVIDER_REQUIRED_ENV`, so it
    follows `EMAIL_PROVIDER` wherever the workflow sets it — including
    the case that caused the bug, where the workflow set it nowhere and
    silently inherited `brevo` from the field default.
    """
    provider = batch_job_env.get("EMAIL_PROVIDER") or Settings.model_fields["email_provider"].default
    required = _EMAIL_PROVIDER_REQUIRED_ENV[provider]
    missing = [env_name for env_name, _field in required if env_name not in batch_job_env]
    assert not missing, (
        f"daily-batch.yml runs with EMAIL_PROVIDER={provider} and does not declare "
        + ", ".join(missing)
        + ". app.batch.run_daily.main() calls validate_production_settings, which "
        "refuses to start without them -- every scheduled run would fail before "
        "fetching anything."
    )


def test_the_workflow_states_its_email_provider_rather_than_inheriting_it(batch_job_env: dict[str, str]):
    """Inheriting the default is what made this invisible: the workflow
    named Resend's secret, so it READ as a Resend deployment, while the
    default quietly made it a Brevo one. Naming the provider makes the
    two secrets below it obviously the matching pair.
    """
    assert "EMAIL_PROVIDER" in batch_job_env, (
        "daily-batch.yml does not state which email transport it runs with, so which "
        "secrets it needs depends on a default declared in app/config.py"
    )
