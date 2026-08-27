"""What app/ imports, pyproject.toml declares. Both directions had
drifted.

  * `starlette` was imported directly and by name in three middleware
    modules (BaseHTTPMiddleware, Request, Response, JSONResponse,
    Headers, and the ASGI type aliases) and declared nowhere. It arrived
    only because FastAPI happens to depend on it. The day FastAPI
    changes or loosens that pin, three of this app's middleware modules
    break at import time -- on Render, at startup -- with nothing in the
    manifest to explain why.

  * `groq` was declared and imported by nothing, ever. It was
    provisioned ahead of P3-LLM, which then reached Groq over its
    OpenAI-compatible REST endpoint via
    `app.services.llm.transport.OpenAICompatibleTransport` (plain
    `requests`), the same transport Mistral and NVIDIA use. Left in, it
    was a wheel download and an install on every Render deploy and every
    CI matrix leg, plus its own transitive tree, for a module no line of
    this codebase imports.

The first test below is the general mechanism rather than a check on
those two names: every third-party top-level module app/ imports must
resolve to a distribution this project declares. The second is specific
to `groq`, because "declared but unused" cannot be detected generally --
`uvicorn` and `psycopg[binary]` are both legitimately never imported by
app/ -- and because a name that was once required is exactly the kind of
thing that gets pasted back in.
"""

from __future__ import annotations

import ast
import re
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

# `tomllib` is stdlib from 3.11 only, and pyproject.toml declares
# `requires-python = ">=3.10"`, so the 3.10 leg of the CI matrix has no
# such module. Importing it unconditionally made this file fail at
# COLLECTION on that leg, which aborts the whole run with exit code 2
# before a single test executes -- pytest reported "1589 items / 1
# error" and stopped. That is how the first CI run this project ever
# performed failed: not on an assertion, on an import.
#
# `tomli` is the same parser under its pre-stdlib name and is a common
# transitive dependency, so it is tried next. If neither is present the
# module skips rather than errors: this test reads pyproject.toml to
# check the manifest against real imports, and that check genuinely
# runs on the 3.12 leg. A skip on 3.10 loses nothing; a collection
# error there loses the entire suite.
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        import pytest

        pytest.skip(
            "needs tomllib (Python 3.11+) or tomli; the manifest check runs on the 3.12 matrix leg",
            allow_module_level=True,
        )

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# "fastapi>=0.115", "uvicorn[standard]>=0.32", "psycopg[binary]>=3.2" ->
# fastapi / uvicorn / psycopg
_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9._-]+)")


def _normalize(name: str) -> str:
    """PEP 503 normalization -- `google-genai`, `google_genai` and
    `Google.GenAI` are one distribution.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_distributions() -> set[str]:
    manifest = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    project = manifest["project"]
    requirements = list(project.get("dependencies") or [])
    for extra in (project.get("optional-dependencies") or {}).values():
        requirements.extend(extra)
    declared = set()
    for requirement in requirements:
        match = _REQUIREMENT_NAME.match(requirement.strip())
        assert match, f"could not parse requirement {requirement!r}"
        declared.add(_normalize(match.group(1)))
    return declared


def _imported_top_level_modules() -> dict[str, list[str]]:
    """`{top-level module: [files that import it]}` for every absolute
    import under app/, first-party and stdlib included (filtered by the
    caller, so the filtering is visible where it is reasoned about).
    """
    found: dict[str, list[str]] = {}
    for path in sorted((_REPO_ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                found.setdefault(name, []).append(str(path.relative_to(_REPO_ROOT)))
    return found


def test_the_import_scan_finds_something():
    """Guards the test below, which iterates whatever this returns."""
    modules = _imported_top_level_modules()
    assert "fastapi" in modules, "the AST scan of app/ found no fastapi import -- the scan is broken"


def test_every_third_party_module_app_imports_is_declared():
    """THE regression: fails on `starlette` before it was added to
    pyproject.toml.

    Resolved through `importlib.metadata.packages_distributions()`
    rather than a hand-written module->package table, so
    `docx` -> python-docx, `google.genai` -> google-genai and
    `argon2` -> argon2-cffi come out right without this test having to
    know about any of them.
    """
    installed = packages_distributions()
    declared = _declared_distributions()
    undeclared: dict[str, tuple[tuple[str, ...], str]] = {}

    for module, importers in _imported_top_level_modules().items():
        if module == "app" or module in sys.stdlib_module_names:
            continue
        # One top-level name can come from several distributions --
        # `google` is a namespace package shared by `google-genai` and
        # its own transitive `google-auth`. Satisfied by ANY declared
        # candidate, not all of them; requiring all would demand that
        # this manifest list other packages' dependencies.
        candidates = tuple(d for d in installed.get(module, []) if _normalize(d) != "erm-news-intel")
        if not candidates:
            # Not installed, or not distribution-backed (a local module
            # shadowing, a namespace with no metadata). Nothing to
            # assert -- an import that does not resolve at all is
            # already a hard failure everywhere else in the suite.
            continue
        if not any(_normalize(d) in declared for d in candidates):
            undeclared[module] = (candidates, importers[0])

    assert not undeclared, "app/ imports these, and pyproject.toml declares none of them: " + ", ".join(
        f"{module} (from {'/'.join(dists)}, first seen in {where})"
        for module, (dists, where) in sorted(undeclared.items())
    )


def test_the_groq_sdk_is_not_declared():
    """`groq` is reached over HTTP by `OpenAICompatibleTransport`, not
    through its SDK -- verified by the assertion below, which is the part
    that actually keeps this honest. If someone genuinely starts using
    the SDK, this test fails at the same moment they add the import, and
    the fix is to add the dependency back and delete this test.
    """
    imported = _imported_top_level_modules()
    assert "groq" not in imported, (
        "app/ now imports the groq SDK: " + ", ".join(imported.get("groq", [])) + ". Re-declare it in pyproject.toml "
        "and remove this test."
    )
    assert "groq" not in _declared_distributions(), (
        "pyproject.toml declares the groq SDK again, and nothing imports it. Groq is "
        "reached over its OpenAI-compatible REST endpoint via app/services/llm/transport.py "
        "(plain requests), the same way Mistral and NVIDIA are -- the SDK is install time "
        "and wheel weight on every Render deploy for nothing."
    )
