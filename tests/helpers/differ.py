"""GATE 3's "diff against legacy output = empty" — the actual mechanism.

A naive set-difference on matched-article ids would never be empty: the
legacy semantic-recall stage promoted exactly 4 of 51,886 rows
(legacy-behavior.md §5.5), and that stage is being removed entirely per
the ADR. Those 4 rows will ALWAYS be legacy-only once semantic search is
gone — that's a known, already-explained, permanent delta, not a bug in
the rebuild. A differ that fails on it is wrong; a differ that ignores
every legacy-only row unconditionally is equally wrong (it would hide a
REAL regression behind the same blind spot). `diff_matched_sets()`
resolves this: every legacy-only / rebuild-only row must be classified
by an explicit, testable predicate as EXPECTED (with a documented reason)
or it counts as a failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExpectedDelta:
    """One documented, predicate-driven exemption. `predicate` receives
    the LEGACY-ONLY row (the row the rebuild is missing) and returns True
    if that specific absence is expected, not a regression.
    """

    reason: str
    predicate: Callable[[dict[str, Any]], bool]


# The one delta this repo currently knows about — legacy-behavior.md §5:
# "semantic" match_type rows exist ONLY because of the semantic-recall
# stage being removed per the ADR. Predicate-based (not an id allowlist)
# so it stays correct if the frozen sample or corpus changes: any row
# whose LEGACY match_type is 'semantic' is expected-missing from a rebuild
# that never runs that stage, unconditionally, not just for 4 specific ids.
SEMANTIC_RECALL_REMOVED = ExpectedDelta(
    reason=(
        "Legacy semantic-recall stage promoted this row (match_type='semantic'); "
        "legacy-behavior.md §5 confirms it promoted exactly 4 of 51,886 rows "
        "historically and is being removed per the ADR (§6: 'Stripping it per §1 "
        "is overdue'). The rebuild has no semantic stage, so this row is "
        "expected-absent, not a regression."
    ),
    predicate=lambda row: row.get("match_type") == "semantic",
)

DEFAULT_EXPECTED_DELTAS: tuple[ExpectedDelta, ...] = (SEMANTIC_RECALL_REMOVED,)


@dataclass
class DiffResult:
    key: str
    legacy_only: list[dict[str, Any]]
    rebuild_only: list[dict[str, Any]]
    expected: list[tuple[dict[str, Any], str]]
    unexpected_legacy_only: list[dict[str, Any]]
    unexpected_rebuild_only: list[dict[str, Any]]

    @property
    def is_empty(self) -> bool:
        """GATE 3's actual pass condition: every row present on only one
        side has been accounted for by a documented ExpectedDelta. Any
        UNEXPECTED leftover — either side — fails the gate.
        """
        return not self.unexpected_legacy_only and not self.unexpected_rebuild_only

    def format_report(self) -> str:
        lines = [
            f"P1/P2 equivalence diff — key={self.key!r}",
            f"  legacy_only={len(self.legacy_only)} rebuild_only={len(self.rebuild_only)} "
            f"expected={len(self.expected)}",
            f"  result: {'EMPTY (pass)' if self.is_empty else 'NOT EMPTY (fail)'}",
        ]
        if self.expected:
            lines.append("  expected deltas:")
            for row, reason in self.expected:
                lines.append(f"    {self.key}={row.get(self.key)!r}: {reason}")
        if self.unexpected_legacy_only:
            lines.append(f"  UNEXPECTED legacy-only rows ({len(self.unexpected_legacy_only)}):")
            for row in self.unexpected_legacy_only:
                lines.append(f"    {self.key}={row.get(self.key)!r}: {row}")
        if self.unexpected_rebuild_only:
            lines.append(f"  UNEXPECTED rebuild-only rows ({len(self.unexpected_rebuild_only)}):")
            for row in self.unexpected_rebuild_only:
                lines.append(f"    {self.key}={row.get(self.key)!r}: {row}")
        return "\n".join(lines)


def diff_matched_sets(
    legacy_rows: list[dict[str, Any]],
    rebuild_rows: list[dict[str, Any]],
    *,
    key: str = "id",
    expected_deltas: tuple[ExpectedDelta, ...] = DEFAULT_EXPECTED_DELTAS,
) -> DiffResult:
    """Diff two row sets by `key`, classifying every legacy-only row
    against `expected_deltas` in order (first match wins). Rebuild-only
    rows (the rebuild found/matched something legacy didn't) have no
    exemption list today — nothing in the current scope produces those on
    purpose, so any occurrence is always UNEXPECTED. If a future stage
    legitimately does this, add a symmetric `rebuild_only` predicate list
    rather than silently swallowing it here.
    """
    legacy_by_key = {r[key]: r for r in legacy_rows}
    rebuild_by_key = {r[key]: r for r in rebuild_rows}
    legacy_only_keys = sorted(set(legacy_by_key) - set(rebuild_by_key), key=str)
    rebuild_only_keys = sorted(set(rebuild_by_key) - set(legacy_by_key), key=str)

    expected: list[tuple[dict[str, Any], str]] = []
    unexpected_legacy_only: list[dict[str, Any]] = []
    for k in legacy_only_keys:
        row = legacy_by_key[k]
        matched = next((d for d in expected_deltas if d.predicate(row)), None)
        if matched:
            expected.append((row, matched.reason))
        else:
            unexpected_legacy_only.append(row)

    rebuild_only_rows = [rebuild_by_key[k] for k in rebuild_only_keys]

    return DiffResult(
        key=key,
        legacy_only=[legacy_by_key[k] for k in legacy_only_keys],
        rebuild_only=rebuild_only_rows,
        expected=expected,
        unexpected_legacy_only=unexpected_legacy_only,
        unexpected_rebuild_only=rebuild_only_rows,
    )
