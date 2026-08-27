"""CI's own promise, enforced against CI's own file.

`.github/workflows/ci.yml` opens by stating that the workflow "must NEVER
call a real LLM provider or reach the live internet for anything
business-relevant". That was true of every step except the last one,
which ran:

    run: python -m app.batch

With this job's `DATABASE_URL: ""` that takes app/batch/run_daily.py's
`run_collection(None, ...)` branch -- fetch-only, nothing written, and
REAL discovery against all ~45 sources in app/sources/registry.py. The
network guard that makes the same promise true for `pytest` lives in
tests/conftest.py as a pytest fixture, and pytest is not running in that
step, so nothing stopped it. Every push and every PR update sent live
traffic from GitHub's IP ranges to 45 third-party news sites and spent
~2.5 minutes per matrix leg doing it.

The fix keeps the step doing its real job -- proving the batch CLI runs
end to end, as `python -m`, independently of the web app -- with two
independent blocks in front of it:

  * proxy env vars pointing every HTTP client at a closed local port,
    which is the only thing that reaches the `curl` SUBPROCESS that
    app/sources/_curl_sitemap.py shells out to for the sites that answer
    `requests` with 403. No Python monkeypatch can reach into that.
  * tests/helpers/network_guard.py imported directly, which turns an
    escaped socket into a named error instead of a slow timeout -- and
    keeps the step fast, since `NetworkBlockedError` is not a
    `requests.RequestException` and so skips app/sources/http.py's
    retry/backoff ladder entirely. Measured: 3.9s for the full
    45-source run.

This test asserts on the workflow file because that is where the defect
lived. It is deliberately written against "the step that runs the batch",
not against a fixed step name or index, so renaming or reordering steps
cannot make it pass vacuously.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _ci_steps() -> list[dict]:
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    steps: list[dict] = []
    for job in workflow["jobs"].values():
        steps.extend(job.get("steps") or [])
    return steps


def _batch_steps() -> list[dict]:
    """Every CI step that invokes the batch entrypoint, however it spells
    it -- `python -m app.batch`, or a heredoc that runs the module.
    """
    return [step for step in _ci_steps() if "app.batch" in (step.get("run") or "")]


@pytest.fixture(scope="module")
def batch_step() -> dict:
    steps = _batch_steps()
    assert len(steps) == 1, f"expected exactly one CI step invoking the batch entrypoint, found {len(steps)}"
    return steps[0]


def test_ci_still_runs_the_batch_entrypoint_at_all():
    """Guards the guards: deleting the step outright would "fix" the
    network problem and quietly drop DoD #1's requirement that the batch
    CLI be runnable independently of the web app. The step has to exist
    AND be offline, not one or the other.
    """
    assert _batch_steps(), "CI no longer smoke-checks the batch entrypoint at all"


def test_the_batch_step_installs_the_projects_own_network_guard(batch_step: dict):
    """THE regression. Before the fix this step's `run` was the bare
    string `python -m app.batch`, which contains none of this.
    """
    run = batch_step["run"]
    assert "network_guard" in run, "the batch smoke step does not import the network guard"
    assert "block_network()" in run, "the batch smoke step imports the guard but never installs it"


def test_the_batch_step_blocks_the_curl_subprocess_too(batch_step: dict):
    """The half a socket monkeypatch cannot cover.

    app/sources/_curl_sitemap.py runs the system `curl` binary on
    purpose, for the handful of sites that serve curl and 403 `requests`.
    A separate process does not inherit a monkeypatched
    `socket.socket.connect` -- it inherits the environment. Both
    lowercase and uppercase spellings, because `curl` reads the
    lowercase ones and much of the Python ecosystem reads either.
    """
    # Asserted against the SHELL SCRIPT, not against a workflow `env:`
    # block, and that distinction is the whole reason this test changed.
    #
    # These names were originally declared in the step's `env:`. GitHub
    # Actions reserves http_proxy / https_proxy / no_proxy for the
    # runner's own proxy configuration and rejects them there by
    # refusing to parse the ENTIRE workflow file -- every push then
    # failed in 0 seconds having scheduled no jobs, with no log and no
    # annotation, and `gh run list` showed the workflow's name as its
    # path because GitHub never read `name:`. Exporting them inside the
    # script is equivalent for the subprocess (a child inherits the
    # exported environment either way) and is not policed.
    script = batch_step.get("run") or ""
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        assert f"export {name}=" in script, (
            f"the batch smoke step does not export {name}, so curl can still reach the internet"
        )
        value = script.split(f"export {name}=", 1)[1].split("\n", 1)[0].strip()
        assert value.startswith("http://127.0.0.1:"), (
            f"{name} must point at a closed LOCAL port so an attempt is refused "
            f"instantly and no packet leaves the runner; got {value!r}"
        )
    for name in ("no_proxy", "NO_PROXY"):
        assert f"export {name}=" in script, f"{name} must be exported explicitly"
        value = script.split(f"export {name}=", 1)[1].split("\n", 1)[0].strip()
        assert value == "", f"a non-empty {name} punches a hole straight back through the proxy block"

    assert "env" not in batch_step or not (batch_step["env"].keys() & {
        "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY"
    }), (
        "proxy variables are back in the step's `env:` block. GitHub Actions "
        "reserves those names and will refuse to parse the whole workflow file."
    )


def test_no_ci_step_runs_the_batch_unguarded():
    """The exact original line, and any future re-introduction of it,
    named as a string so the failure message points straight at what to
    look for.
    """
    for step in _ci_steps():
        run = (step.get("run") or "").strip()
        assert run != "python -m app.batch", (
            "a CI step runs the batch entrypoint with no network block. "
            "That performs real discovery against ~45 live news sites on "
            "every push and PR -- see this module's docstring."
        )


def test_the_daily_batch_workflow_is_deliberately_not_touched():
    """Scope, asserted rather than assumed.

    .github/workflows/daily-batch.yml runs `python -m app.batch` for
    real, against the production database, on a cron -- fetching from
    those 45 sites IS its job. Blocking the network there would break
    the product. This test exists so that a well-meaning future sweep
    of "make the batch offline everywhere" has something to trip over.
    """
    daily = yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / "daily-batch.yml").read_text(encoding="utf-8"))
    runs = [step.get("run", "") for job in daily["jobs"].values() for step in (job.get("steps") or [])]
    assert any(run.strip() == "python -m app.batch" for run in runs), (
        "the daily batch workflow no longer runs the batch entrypoint directly -- "
        "if that was deliberate, update this test; if not, the nightly collection is dead"
    )
