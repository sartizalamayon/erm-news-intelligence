"""Deterministic fake LLM provider for unit/integration tests.

Mission §6.4 cost discipline: "Unit and integration tests MOCK the LLM.
Only the parity harness and the budget lab spend real tokens, and both
run against the frozen sample, ON DEMAND." This module is the mock every
non-parity test should use instead of a real Groq/Gemini/Ollama call.

Faithful to the REAL providers in two respects (p3-contract.md §4, §8):

1. Shape — every prompt method in llm.py funnels through one
   `_generate(prompt, system, fmt, num_predict, num_ctx)` choke point;
   this fake mirrors that exact signature so a future
   `app.services.llm` provider class can be a near drop-in swap for
   tests that inject a fake vs. a real transport.
2. Failure shapes — malformed JSON, truncated output, HTTP 429, HTTP
   5xx, and timeout are configured via `mode`, each raising/returning
   the SAME shape the real `requests`-based transport would (an HTTPError
   with `.response.status_code`, or `requests.exceptions.Timeout`) since
   that's exactly what p3-contract.md §3's `except Exception` blocks
   have to be resilient to.

Response-body factories (`judge_batch_response`, etc.) build
schema-correct JSON for each of the 6 JSON-mode call sites documented in
p3-contract.md §3, so a test can say "the model returned this specific
well-formed thing" without hand-writing JSON.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import requests

FailureMode = Literal["ok", "malformed_json", "truncated", "rate_limited", "server_error", "timeout"]


class FakeProviderTimeout(requests.exceptions.Timeout):
    """Mirrors `requests.exceptions.Timeout` — the exact type
    `OllamaProvider._generate()`/`GroqProvider._generate()` let propagate
    when the underlying `requests.post(..., timeout=...)` deadline is
    exceeded (p3-contract.md §4 `timeout` row, §7 Timeout handling).
    """


def _http_error(status_code: int, message: str) -> requests.exceptions.HTTPError:
    """An HTTPError carrying `.response.status_code`, matching what
    `r.raise_for_status()` raises in real `_generate()` implementations
    (p3-contract.md §4) — every JSON-mode call site's blanket
    `except Exception` has to survive exactly this.
    """
    response = requests.Response()
    response.status_code = status_code
    return requests.exceptions.HTTPError(message, response=response)


@dataclass
class FakeLLMProvider:
    """Configure `mode` per test; call `_generate(...)` exactly like a
    real provider. `call_log` records every call (prompt/system/fmt/etc)
    for assertions like "translate_title was called once per non-English
    title" without needing a real model.

    `responses(prompt, system) -> str` is the success-path body. Default
    (`None`) returns `"{}"` for `fmt="json"` calls and `"ok"` otherwise —
    enough to exercise failure-mode plumbing without asserting on content;
    pass your own callable (or use the `*_response()` factories below) to
    assert on parsed values.
    """

    mode: FailureMode = "ok"
    responses: Callable[[str, str], str] | None = None
    retry_after: float | None = None
    call_log: list[dict[str, Any]] = field(default_factory=list)

    def _generate(
        self,
        prompt: str,
        system: str = "",
        fmt: str | None = None,
        num_predict: int | None = None,
        num_ctx: int | None = None,
    ) -> str:
        self.call_log.append(
            {
                "prompt": prompt,
                "system": system,
                "fmt": fmt,
                "num_predict": num_predict,
                "num_ctx": num_ctx,
            }
        )

        if self.mode == "timeout":
            raise FakeProviderTimeout("simulated timeout — no response received")
        if self.mode == "rate_limited":
            raise _http_error(429, "simulated rate limit (429)")
        if self.mode == "server_error":
            raise _http_error(503, "simulated upstream error (503)")

        body = self.responses(prompt, system) if self.responses else _default_body(fmt)

        if self.mode == "malformed_json":
            # Not JSON at all — the "model ignored the format instruction"
            # failure mode. json.loads() raises json.JSONDecodeError,
            # caught by every call site's `except Exception`.
            return "Sure! Here's the answer: " + body[:20]
        if self.mode == "truncated":
            # Well-formed prefix, cut off before the closing brace — the
            # shape a real model emits when it hits its output-token cap
            # mid-object. json.loads() raises on this too (unterminated
            # string/object), but it's a DIFFERENT failure signature than
            # malformed_json (valid JSON *prefix*, not garbage from the
            # first byte) worth testing separately.
            cut = max(1, len(body) * 2 // 3)
            return body[:cut]
        return body


def _default_body(fmt: str | None) -> str:
    return "{}" if fmt == "json" else "ok"


# ── Schema-correct response-body factories (p3-contract.md §3) ─────────


def judge_batch_response(scores: list[tuple[int, int, str]]) -> str:
    """`{"scores": [{"n": <int>, "score": <1-10>, "reason": <str>}, ...]}`
    `scores` = list of (n, score, reason) tuples, 1-indexed n.
    """
    return json.dumps({"scores": [{"n": n, "score": s, "reason": r} for n, s, r in scores]})


def cluster_topics_response(labels: list[tuple[int, str]]) -> str:
    """`{"labels": [{"n": <int>, "label": <str>}, ...]}`"""
    return json.dumps({"labels": [{"n": n, "label": lab} for n, lab in labels]})


def consolidate_labels_response(mapping: list[tuple[int, str]]) -> str:
    """`{"map": [{"n": <int>, "canon": <str>}, ...]}`"""
    return json.dumps({"map": [{"n": n, "canon": canon} for n, canon in mapping]})


def find_duplicates_response(dupes: list[tuple[int, int]]) -> str:
    """`{"dupes": [{"c": <int>, "same_as": <int>}, ...]}`"""
    return json.dumps({"dupes": [{"c": c, "same_as": s} for c, s in dupes]})


def verify_group_response(groups: list[int]) -> str:
    """`{"groups": [<int>, ...]}`, one entry per input headline, same order."""
    return json.dumps({"groups": groups})
