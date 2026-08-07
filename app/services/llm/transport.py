"""The ONE choke point every prompt method funnels through — mirrors
`llm.py`'s own docstring for `GroqProvider`: "Every prompt method in
this file... funnels through `_generate()`... overriding just `_ping` +
`_generate` swaps the transport and inherits all eleven prompts
unchanged." `Transport` below is that same seam, generalized to more
than one real backend.

`Transport._generate(prompt, system, fmt, num_predict, num_ctx) -> str`
is IDENTICAL in shape to `tests/helpers/fake_llm.py`'s `FakeLLMProvider`
— by construction, not by coincidence: that file's own docstring says
this is "faithful to the REAL providers... this fake mirrors that exact
signature so a future `app.services.llm` provider class can be a
near drop-in swap." `FakeLLMProvider` IS a valid `Transport` for every
test in this package; no adapter needed.

Two real implementations — "a reusable OpenAI-compatible adapter
parameterized by base_url + key + model, plus a separate Gemini
adapter" (owner requirement via the coordinator, not a bespoke class
per vendor):

  * `OpenAICompatibleTransport` — ANY OpenAI-compatible chat-completions
    API: Bearer auth, `{"model", "messages", "temperature"}` payload,
    `response_format: {"type": "json_object"}` for JSON mode. Groq,
    Mistral, and (if ever reactivated) Cerebras are all THIS class,
    differing only in `base_url`/`model`/`api_key` — a new
    OpenAI-compatible provider is therefore a CONFIG value
    (`app.services.llm.provider_registry`), never a new Python class.
    Uses raw `requests` (already a dependency) rather than any one
    vendor's SDK, so adding a provider never means adding a dependency
    either. Retry policy is p3-contract.md §7's `GroqProvider._generate()`
    ported EXACTLY — same shape the frozen contract documents (up to
    `retries` attempts, default 4 matching legacy's `GROQ_RETRIES` env
    default; exponential backoff from 2.0s honoring a `Retry-After`
    header on 429/5xx; timeout handled the same way but re-raised on
    the final attempt; any other 4xx is permanent, no retry) — now
    shared by every provider using this transport, not just Groq.
  * `GeminiTransport` — Google AI Studio via the official `google-genai`
    SDK. Not OpenAI-compatible (different auth, different request/
    response shape, no `/chat/completions` endpoint) and not in the
    legacy contract at all (Gemini didn't exist as a legacy provider)
    — ADR §9.1 rules the PROMPT frozen, not the transport, so this uses
    whatever's idiomatic for the SDK (`system_instruction` +
    `response_mime_type` for JSON mode) as long as it sends the
    IDENTICAL system+prompt text `app.services.llm.prompts` builds.

Both real transports record usage (tokens, requests) into an optional
`UsageSink` (structurally satisfied by `app.services.llm.quota.QuotaTracker`)
immediately after every attempt, success or failure — DoD #2's "track
tokens and requests consumed per provider per day" has to see EVERY
attempt, not just the ones that eventually succeeded, since a request
that gets rate-limited still typically counts against a provider's RPD.
**Both record the PROVIDER'S OWN reported total token count** (Groq/
Mistral: `response["usage"]["total_tokens"]`; Gemini:
`response.usage_metadata.total_token_count`, which INCLUDES
`thoughts_token_count` when a thinking-enabled model is used) — never a
locally computed `len(prompt)+len(completion)` sum. This is load-bearing,
not incidental: live measurement (`docs/token-budget.md` §2) found a
single `qwen/qwen3.6-27b` `judge_batch` call reporting 7,116 total
tokens where prompt+visible-completion would undercount it, because the
model's `<think>...</think>` reasoning is billed but never appears in
the parsed answer. `tests/test_p3_transport.py` pins this exact
property against both transports.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

from app.errors import ProviderPermanentError, ProviderRateLimitedError
from app.logging import get_logger

logger = get_logger(__name__)

GENERATION_TEMPERATURE = 0.1  # p3-contract.md §4 — hard-coded, identical for every call, every provider


class Transport(Protocol):
    def _generate(
        self,
        prompt: str,
        system: str = "",
        fmt: str | None = None,
        num_predict: int | None = None,
        num_ctx: int | None = None,
    ) -> str: ...


class UsageSink(Protocol):
    def record(self, provider: str, *, tokens: int, requests: int = 1) -> object: ...


# ── Generic OpenAI-compatible adapter (Groq, Mistral, Cerebras-if-ever) ──

OPENAI_COMPATIBLE_DEFAULT_RETRIES = 4  # p3-contract.md §7 — GROQ_RETRIES env default, now the shared default
OPENAI_COMPATIBLE_DEFAULT_TIMEOUT_SECONDS = 120.0  # p3-contract.md §4/§8 — flat 120s always
OPENAI_COMPATIBLE_INITIAL_BACKOFF_SECONDS = 2.0


def _retry_after_seconds(response: requests.Response | None, default: float) -> float:
    if response is None:
        return default
    header = response.headers.get("Retry-After")
    if header is None:
        return default
    try:
        return float(header)
    except (TypeError, ValueError):
        return default


@dataclass
class OpenAICompatibleTransport:
    """Any OpenAI-compatible chat-completions endpoint. `base_url` is
    the ONLY thing that varies between providers using this class
    (`app.services.llm.provider_registry.ProviderDefinition.base_url`)
    — e.g. `https://api.groq.com/openai/v1`,
    `https://api.mistral.ai/v1`. `provider_name` is what gets recorded
    into `quota` and raised in `ProviderError.provider`.
    """

    base_url: str
    api_key: str
    model: str
    provider_name: str
    quota: UsageSink | None = None
    retries: int = OPENAI_COMPATIBLE_DEFAULT_RETRIES
    timeout: float = OPENAI_COMPATIBLE_DEFAULT_TIMEOUT_SECONDS

    def _record(self, *, tokens: int, requests_count: int = 1) -> None:
        if self.quota is not None:
            self.quota.record(self.provider_name, tokens=tokens, requests=requests_count)

    def _generate(
        self,
        prompt: str,
        system: str = "",
        fmt: str | None = None,
        num_predict: int | None = None,
        num_ctx: int | None = None,
    ) -> str:
        """`num_ctx` is accepted (for `Transport` protocol conformance)
        and IGNORED — p3-contract.md §4/§8: "context size is fixed
        server-side by the model, there is no client-side knob to raise
        it," verbatim from legacy's own `GroqProvider._generate()`
        docstring, and equally true of every other OpenAI-compatible
        host this class talks to.
        """
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": GENERATION_TEMPERATURE}
        if num_predict:
            payload["max_tokens"] = num_predict
        if fmt == "json":
            # JSON mode on every provider using this class requires the
            # word "JSON" somewhere in the messages — every prompt this
            # transport ever receives already says "Respond ONLY with
            # JSON" (p3-contract.md §4).
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {self.api_key}"}
        delay = OPENAI_COMPATIBLE_INITIAL_BACKOFF_SECONDS
        for attempt in range(self.retries):
            try:
                response = requests.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers, timeout=self.timeout
                )
            except requests.exceptions.Timeout as exc:
                self._record(tokens=0)
                if attempt == self.retries - 1:
                    raise ProviderRateLimitedError(
                        f"{self.provider_name} request timed out after {self.retries} attempts",
                        provider=self.provider_name,
                        cause=exc,
                    ) from exc
                time.sleep(delay)
                delay *= 2
                continue
            except requests.exceptions.RequestException as exc:
                self._record(tokens=0)
                if attempt == self.retries - 1:
                    raise ProviderRateLimitedError(
                        f"{self.provider_name} connection error", provider=self.provider_name, cause=exc
                    ) from exc
                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code == 429 or response.status_code >= 500:
                self._record(tokens=0)
                if attempt == self.retries - 1:
                    raise ProviderRateLimitedError(
                        f"{self.provider_name} rate limited/server error (HTTP {response.status_code}) "
                        f"after {self.retries} attempts",
                        provider=self.provider_name,
                    )
                wait = _retry_after_seconds(response, default=delay)
                logger.warning(
                    "openai_compatible_retry",
                    extra={
                        "provider": self.provider_name,
                        "status_code": response.status_code,
                        "attempt": attempt + 1,
                        "wait_seconds": wait,
                    },
                )
                time.sleep(wait)
                delay *= 2
                continue
            if response.status_code >= 400:
                self._record(tokens=0)
                raise ProviderPermanentError(
                    f"{self.provider_name} rejected the request (HTTP {response.status_code}): "
                    f"{response.text[:200]}",
                    provider=self.provider_name,
                )

            data = response.json()
            usage = data.get("usage") or {}
            total_tokens = int(usage.get("total_tokens", 0))
            self._record(tokens=total_tokens)
            content = data["choices"][0]["message"]["content"] or ""
            return content.strip()
        return ""  # unreachable under normal logic (retries<=0 only) — mirrors legacy's own literal fallthrough


def openai_compatible_retries_from_env() -> int:
    """Mirrors legacy's `int(os.environ.get("GROQ_RETRIES", "4"))`
    (p3-contract.md §7) as the DEFAULT when a caller doesn't pass
    `retries` explicitly — kept as an opt-in helper rather than read
    directly inside the constructor, so tests/config-driven callers can
    set `retries=` without touching the environment. The env var name
    stays `GROQ_RETRIES` (not renamed) for continuity with the frozen
    contract's own documented name, even though it now defaults every
    OpenAI-compatible provider's retry count, not just Groq's.
    """
    return int(os.environ.get("GROQ_RETRIES", str(OPENAI_COMPATIBLE_DEFAULT_RETRIES)))


# ── Google AI Studio (Gemini) ───────────────────────────────────────────

GEMINI_DEFAULT_RETRIES = 2
GEMINI_DEFAULT_TIMEOUT_SECONDS = 120.0
GEMINI_INITIAL_BACKOFF_SECONDS = 2.0


@dataclass
class GeminiTransport:
    """Real Google AI Studio transport via `google-genai`. Not part of
    the frozen legacy contract (Gemini never existed as a legacy
    provider) — sends the SAME system+prompt text `app.services.llm.prompts`
    builds, but is free to shape the HTTP/SDK request however is
    idiomatic, per ADR §9.1: prompt/schema frozen, transport/model not.
    """

    api_key: str
    model: str
    provider_name: str = "gemini"
    quota: UsageSink | None = None
    retries: int = GEMINI_DEFAULT_RETRIES
    timeout: float = GEMINI_DEFAULT_TIMEOUT_SECONDS
    _client: Any = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        from google import genai
        from google.genai import types as genai_types

        http_options = genai_types.HttpOptions(timeout=int(self.timeout * 1000))
        self._client = genai.Client(api_key=self.api_key, http_options=http_options)

    def _record(self, *, tokens: int, requests_count: int = 1) -> None:
        if self.quota is not None:
            self.quota.record(self.provider_name, tokens=tokens, requests=requests_count)

    def _generate(
        self,
        prompt: str,
        system: str = "",
        fmt: str | None = None,
        num_predict: int | None = None,
        num_ctx: int | None = None,
    ) -> str:
        """`num_ctx` is accepted for `Transport` protocol conformance and
        ignored — Gemini's context window is a fixed model property, the
        same reasoning p3-contract.md §8 documents for Groq.
        """
        from google.genai import errors as genai_errors
        from google.genai import types as genai_types

        config_kwargs: dict[str, Any] = {"temperature": GENERATION_TEMPERATURE}
        if system:
            config_kwargs["system_instruction"] = system
        if num_predict:
            config_kwargs["max_output_tokens"] = num_predict
        if fmt == "json":
            config_kwargs["response_mime_type"] = "application/json"
        config = genai_types.GenerateContentConfig(**config_kwargs)

        delay = GEMINI_INITIAL_BACKOFF_SECONDS
        for attempt in range(self.retries):
            try:
                response = self._client.models.generate_content(model=self.model, contents=prompt, config=config)
            except genai_errors.ServerError as exc:
                self._record(tokens=0)
                if attempt == self.retries - 1:
                    raise ProviderRateLimitedError(
                        f"Gemini server error after {self.retries} attempts", provider=self.provider_name, cause=exc
                    ) from exc
                time.sleep(_gemini_retry_after_seconds(exc, default=delay))
                delay *= 2
                continue
            except genai_errors.ClientError as exc:
                self._record(tokens=0)
                if exc.code == 429:
                    if attempt == self.retries - 1:
                        raise ProviderRateLimitedError(
                            f"Gemini rate limited after {self.retries} attempts", provider=self.provider_name, cause=exc
                        ) from exc
                    time.sleep(_gemini_retry_after_seconds(exc, default=delay))
                    delay *= 2
                    continue
                raise ProviderPermanentError(
                    f"Gemini rejected the request (HTTP {exc.code}): {exc.message}",
                    provider=self.provider_name,
                    cause=exc,
                ) from exc
            else:
                text = (response.text or "").strip()
                usage = getattr(response, "usage_metadata", None)
                # `total_token_count` is the PROVIDER's own reported total
                # — it INCLUDES `thoughts_token_count` on thinking-enabled
                # models (docs/token-budget.md §2: gemini-flash-latest
                # reported thoughts=1,199 folded into total=4,109 on a
                # real judge_batch prompt). Recording anything else here
                # (e.g. prompt_token_count + candidates_token_count) would
                # silently undercount exactly the cost this lab exists to
                # surface — see tests/test_p3_transport.py.
                total_tokens = int(usage.total_token_count) if usage is not None and usage.total_token_count else 0
                self._record(tokens=total_tokens)
                return text
        return ""


def _gemini_retry_after_seconds(exc: Any, default: float) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    header = headers.get("Retry-After") if headers is not None else None
    if header is None:
        return default
    try:
        return float(header)
    except (TypeError, ValueError):
        return default
