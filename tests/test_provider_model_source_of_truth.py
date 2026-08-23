"""`app.services.llm.provider_registry` is the ONLY source of model ids.

This suite exists because that invariant broke once, silently, in a way
no other test could see — and the failure was production-only.

The mechanism: `app.services.llm.registry._env_credentials` reads the
bootstrap model generically as `getattr(settings, f"{provider}_model")`,
and `load_provider_account` prefers that value over
`provider_registry.DEFAULT_PROVIDER_DEFINITIONS[provider].default_model`,
consulting the registry only when the settings value is falsy. A
non-empty DEFAULT on a `Settings.<provider>_model` field therefore
shadows the registry for every deployment that does not explicitly set
the corresponding env var.

What that cost in practice: `groq_model` defaulted to `qwen/qwen3.6-27b`
and `gemini_model` to `gemini-2.0-flash`. `provider_registry` had
already moved off both — to `llama-3.1-8b-instant` (14.4x the daily
request ceiling, no reasoning-token waste) and
`gemini-flash-lite-latest` — and its comments record `gemini-2.0-flash`
as verified DEAD, HTTP 404 "no longer available" on every key held by
this project. `render.yaml` declares neither `GROQ_MODEL` nor
`GEMINI_MODEL`, so production would have resolved both stale defaults
while every developer machine resolved the corrected values out of its
own `.env`. Gemini is the secondary for all five features, so the whole
failover path would have 404'd the first time a primary went down.

`tests/conftest.py` now blanks `<PROVIDER>_MODEL` alongside
`<PROVIDER>_API_KEY` so this suite (and every other) sees exactly what
production sees rather than whatever the developer's `.env` holds.
"""

from __future__ import annotations

import pytest
from pydantic_core import PydanticUndefined

from app.config import Settings
from app.services.llm.provider_registry import DEFAULT_PROVIDER_DEFINITIONS
from app.services.llm.registry import _env_credentials

# A model id that provider_registry has explicitly retired. Pinned here
# so the test names the actual regression, not an abstract property.
DEAD_GEMINI_MODEL = "gemini-2.0-flash"
SUPERSEDED_GROQ_MODEL = "qwen/qwen3.6-27b"


def _declared_default(field: str) -> str:
    """The default DECLARED in app/config.py for `field`.

    Read off the model field rather than by instantiating `Settings`:
    instantiating resolves the real environment, and `conftest.py`
    blanks `<PROVIDER>_MODEL` there — so an instance would report `""`
    for a field whose source default is a hardcoded model id, and this
    suite would pass against the exact regression it exists to catch.
    (It did, on first writing. The declared default is the thing that
    reaches production; assert on that.)
    """
    model_field = Settings.model_fields.get(field)
    if model_field is None:
        return ""
    default = model_field.default
    return "" if default is None or default is PydanticUndefined else str(default)


@pytest.mark.parametrize("provider", sorted(DEFAULT_PROVIDER_DEFINITIONS))
def test_no_provider_model_id_is_hardcoded_in_settings(provider: str) -> None:
    """Every `Settings.<provider>_model` field must default to blank.

    Blank is what makes `load_provider_account` fall through to the
    registry. Any non-empty default here silently becomes the production
    model id for that provider, whether or not anyone remembers to
    declare the env var in render.yaml.
    """
    default = _declared_default(f"{provider}_model")
    assert default == "", (
        f"Settings.{provider}_model defaults to {default!r}, which shadows "
        f"provider_registry's {DEFAULT_PROVIDER_DEFINITIONS[provider].default_model!r}. "
        "Model ids belong in provider_registry.DEFAULT_PROVIDER_DEFINITIONS — "
        "the only place they are ever live-verified. Leave this field blank."
    )


@pytest.mark.parametrize("provider", sorted(DEFAULT_PROVIDER_DEFINITIONS))
def test_registry_default_is_what_an_unconfigured_deployment_resolves(provider: str) -> None:
    """The end-to-end property the field-level test protects: with no
    env override and no `provider_configs` row, the model that actually
    gets used is the registry's.
    """
    _api_key, env_model = _env_credentials(provider)
    resolved = env_model or DEFAULT_PROVIDER_DEFINITIONS[provider].default_model
    assert resolved == DEFAULT_PROVIDER_DEFINITIONS[provider].default_model


def test_the_two_retired_model_ids_are_gone_from_settings_defaults() -> None:
    """Names the specific regression, so a future edit that reinstates
    either id fails with an explanation rather than a bare inequality.
    """
    assert _declared_default("gemini_model") != DEAD_GEMINI_MODEL, (
        f"{DEAD_GEMINI_MODEL} is retired — Google returns HTTP 404 "
        "'no longer available' for it on every key this project holds. "
        "provider_registry pins the `-latest` alias precisely so this "
        "cannot rot again."
    )
    assert _declared_default("groq_model") != SUPERSEDED_GROQ_MODEL, (
        f"{SUPERSEDED_GROQ_MODEL} is superseded — it is a reasoning model "
        "(thinking tokens are pure waste for this pipeline's short JSON "
        "verdicts) and caps at 1,000 RPD against a ~1,100 req/day workload. "
        "provider_registry uses llama-3.1-8b-instant: 14,400 RPD."
    )


def test_conftest_blanks_model_env_so_a_developers_dotenv_cannot_mask_the_default() -> None:
    """The harness hole that let the regression live.

    `conftest.py` blanked `<PROVIDER>_API_KEY` but not
    `<PROVIDER>_MODEL`, so a developer whose `.env` carried a corrected
    `GROQ_MODEL` ran the entire suite against that value while CI ran
    against the code default. Assert the blanking is in place, by the
    same registry-derived list, so adding a provider cannot reopen it.
    """
    import os

    for provider in DEFAULT_PROVIDER_DEFINITIONS:
        var = f"{provider.upper()}_MODEL"
        assert os.environ.get(var) == "", (
            f"{var} is {os.environ.get(var)!r} during the test run. "
            "tests/conftest.py must blank it, or model-resolution "
            "assertions become machine-dependent."
        )
