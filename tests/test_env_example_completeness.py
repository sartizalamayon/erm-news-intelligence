""".env.example claims to list every variable `Settings` reads. Make that
claim true by machine, not by memory.

The claim was already false. Five real settings had been added to
`app.config.Settings` over time and never documented:
OTP_CONSOLE_FALLBACK_ENABLED, RATE_LIMIT_REQUESTS_PER_WINDOW,
RATE_LIMIT_WINDOW_SECONDS, MAX_REQUEST_BODY_BYTES and
MAX_QUERY_PARAM_LENGTH. Nobody deploying this could have known they
existed or what they defaulted to; the rate-limit pair in particular is
the difference between a working API and one that 429s real users.

A comment asserting completeness is exactly as strong as whoever last
remembered to honour it, and this repo has already paid for that
assumption once: a `<provider>_model` field whose default nobody
re-read shipped a dead Gemini model id into a production deploy. Same
mechanism, different field. So the invariant is checked here instead.

Both directions are checked, and the second one matters as much as the
first: a variable listed here that `Settings` does NOT read is a lie in
the opposite direction -- `PORT` and `LLM_PROVIDER_PRIMARY` were both
sitting in this file long after nothing read them, telling operators
they could configure things they could not.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=")

# Variables .env.example documents on purpose that are NOT `Settings`
# fields, each with the module that reads it straight from `os.environ`.
# This is not a permanent exemption list -- it is a punch list. Every
# entry here violates app/config.py's own stated rule ("nowhere else
# should call os.environ directly for application config"), which is why
# none of them appears in render.yaml's envVars and why nothing validates
# them (LLM_BATCH="ten" is a crash partway through a nightly run, not a
# rejected config). They are documented in .env.example anyway, because
# an operator debugging a slow or expensive run has no other way to
# discover they exist. Migrating them into `Settings` is outstanding
# work; the modules that own them belong to another agent this round.
#
# `test_no_documented_non_settings_var_has_quietly_become_a_setting`
# below makes this list self-emptying: the moment one of these becomes a
# real field, that test fails and the entry has to be deleted.
_OS_ENVIRON_KNOBS = {
    # app/services/enrichment.py -- `_env_flag`, "0" = off, anything else = on
    "LLM_TRANSLATE",
    "LLM_DEDUP",
    "LLM_JUDGE",
    "LLM_TOPIC",
    # app/services/enrichment.py -- batch sizes, parsed with a bare int()/float()
    "LLM_BATCH",
    "LLM_TOPIC_CHUNK",
    "LLM_TOPIC_DEDUP_BATCH",
    "TOPIC_PREFOLD_THRESH",
    # app/services/llm/transport.py -- retry count for EVERY
    # OpenAI-compatible provider despite the legacy name
    "GROQ_RETRIES",
}


def _documented_vars() -> set[str]:
    """Every `NAME=` assignment in .env.example, ignoring comment lines
    (including the ones that deliberately explain why a variable is
    absent, e.g. "NO `PORT` -- ...").
    """
    documented = set()
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT.match(stripped)
        if match:
            documented.add(match.group(1))
    return documented


def _settings_vars() -> set[str]:
    """`Settings` reads each field from its uppercased name --
    `SettingsConfigDict(case_sensitive=False)` with no `env_prefix` and
    no per-field alias, so the field name IS the env var name.
    """
    return {name.upper() for name in Settings.model_fields}


def test_env_example_parses_into_something():
    """Guards every test below, all of which compare against this set --
    a parser that silently matched nothing would make them all pass.
    """
    documented = _documented_vars()
    assert len(documented) > 15, f"only parsed {len(documented)} assignments out of .env.example -- parser is broken"


def test_every_settings_field_is_documented_in_env_example():
    """THE regression, and the file's own headline claim. Fails today
    without the five additions; would have failed the day any of them
    was added.
    """
    missing = sorted(_settings_vars() - _documented_vars())
    assert not missing, (
        "app.config.Settings reads these, and .env.example does not mention them: "
        + ", ".join(missing)
        + ". Add each one with a comment saying what it does and what its default "
        "means -- an operator cannot configure a variable they cannot see."
    )


def test_env_example_documents_nothing_settings_does_not_read():
    """The other direction. A variable listed here that nothing reads
    tells an operator they can configure something they cannot -- which
    is exactly what `PORT` and `LLM_PROVIDER_PRIMARY` did before they
    were removed. The only permitted extras are the `os.environ` knobs
    above, which are real but bypass `Settings`.
    """
    stale = sorted(_documented_vars() - _settings_vars() - _OS_ENVIRON_KNOBS)
    assert not stale, (
        ".env.example documents variables nothing in app.config.Settings reads: "
        + ", ".join(stale)
        + ". Either wire them up, delete them, or -- if they are read directly "
        "from os.environ by a service module -- add them to _OS_ENVIRON_KNOBS "
        "in this file with the module that reads them."
    )


def test_every_os_environ_knob_is_actually_documented():
    """The punch list has to describe reality in both directions too: a
    knob named here but absent from .env.example is undiscoverable again,
    which is the whole problem.
    """
    undocumented = sorted(_OS_ENVIRON_KNOBS - _documented_vars())
    assert not undocumented, "listed as an os.environ knob but not documented in .env.example: " + ", ".join(
        undocumented
    )


def test_every_os_environ_knob_is_actually_read_somewhere():
    """Stops the exemption list from becoming a place to park anything
    inconvenient. Each entry must appear in app/ source, which is a
    weak check on purpose -- it is a staleness guard, not a proof of
    use, and a stronger one would need to import the modules that own
    these (another agent's files this round).
    """
    sources = "\n".join(path.read_text(encoding="utf-8") for path in (_REPO_ROOT / "app").rglob("*.py"))
    for knob in sorted(_OS_ENVIRON_KNOBS):
        assert knob in sources, f"{knob} is listed as an os.environ knob but appears nowhere in app/"


def test_no_documented_non_settings_var_has_quietly_become_a_setting():
    """Makes `_OS_ENVIRON_KNOBS` self-emptying. When one of these is
    migrated into `Settings` -- the outstanding work this list records --
    this fails, and the entry must be deleted so the two completeness
    tests above start governing it properly.
    """
    migrated = sorted(_OS_ENVIRON_KNOBS & _settings_vars())
    assert not migrated, (
        "these are now real Settings fields and no longer exceptions: "
        + ", ".join(migrated)
        + ". Remove them from _OS_ENVIRON_KNOBS in this file (and add them to "
        "render.yaml's envVars if they should be settable there)."
    )
