"""The data shapes every provider method returns (`llm.py:223-233`,
p3-contract.md §1/§3). Kept as plain, small dataclasses — no behavior,
matching legacy's own `@dataclass` shapes exactly (field names and
types are part of the frozen contract per ADR §9.1: "output schema,
field names/types... frozen").
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Judgment:
    """Result of asking the LLM how relevant an article is to Grameenphone.
    `score` is documented as 1-10 in `FIXED_GUIDE`/the review UI's own
    band cutoffs (p3-contract.md §2.2); legacy's own dataclass types it
    as `float` (`llm.py:227`) even though every real value is an
    integer post-clamp (`clamp_score()`) — kept as `int` here since that
    is what the clamp logic (p3-contract.md §3) always actually
    produces; storage round-tripping int-vs-float is explicitly
    tolerated by the parity harness (tests/helpers/parity.py's
    `check_exact_match_fields` docstring).
    """

    relevant: bool
    score: int
    reason: str


@dataclass
class Summary:
    text: str
