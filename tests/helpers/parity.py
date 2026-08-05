"""P3 parity harness — implements the REDEFINED parity from ADR §9.1, not
the original "matches legacy output" (that definition died the moment the
owner picked `qwen3.6-27b` over the legacy `llama-3.3-70b-versatile`
baseline — see ADR §9.1's "Consequence — GATE 4 parity must be redefined").

Two independent checks, per ADR §9.1 / p3-contract.md §9:

  * `check_exact_match_fields()` — the FROZEN part of the contract:
    schema, field names/types, deterministic P1/P2-derived values, and
    every documented malformed-response fallback. Any mismatch here is a
    contract violation, full stop — model choice cannot excuse it.

  * `check_score_tolerance()` — the MODEL-DEPENDENT part: rank
    correlation + band agreement against a CONFIGURABLE threshold (never
    hardcoded — the correct value is unknown until the noise-floor
    measurement in docs/test-strategy.md is actually run). Produces a
    report listing which sample ids fell outside tolerance, since ADR
    §9.1 assigns the orchestrator to manually review those rather than
    auto-approve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tests.helpers.stats import band_agreement, spearman_rank_correlation

# Fields p3-contract.md §9.1 says MUST match exactly — pure P1/P2 output
# or deterministic Python post-processing, no model call involved.
DETERMINISTIC_FIELDS: tuple[str, ...] = (
    "sections",
    "keywords",
    "url_norm",
    "review_status",
)

# p3-contract.md §2.2 / main.py:172-176 — the band cutoffs both the review
# UI and this harness use. Duplicated here (not imported from
# legacy_reference) on purpose: this module must stay usable standalone
# for a rebuild's own scoring function, not just the legacy oracle.


def score_band(score: float) -> str:
    if score >= 8:
        return "red"
    if score >= 6:
        return "yellow"
    if score == 5:
        return "green"
    return "magenta"


@dataclass(frozen=True)
class SchemaViolation:
    record_id: Any
    field: str
    expected: Any
    actual: Any
    detail: str


def check_exact_match_fields(
    golden: dict[str, Any],
    rebuild: dict[str, Any],
    *,
    fields: tuple[str, ...] = DETERMINISTIC_FIELDS,
    record_id: Any = None,
) -> list[SchemaViolation]:
    """Compare `fields` between one golden record and one rebuild record
    for EXACT equality. Returns one SchemaViolation per mismatch (empty
    list == exact match on every requested field).
    """
    violations: list[SchemaViolation] = []
    for f in fields:
        if f not in golden:
            violations.append(SchemaViolation(record_id, f, None, None, "field absent from golden record"))
            continue
        if f not in rebuild:
            violations.append(SchemaViolation(record_id, f, golden[f], None, "field absent from rebuild output"))
            continue
        expected_val, actual_val = golden[f], rebuild[f]
        numeric = (int, float)
        both_numeric_not_bool = (
            isinstance(expected_val, numeric)
            and isinstance(actual_val, numeric)
            and not isinstance(expected_val, bool)
            and not isinstance(actual_val, bool)
        )
        same_kind = type(expected_val) is type(actual_val) or both_numeric_not_bool
        if not same_kind:
            violations.append(
                SchemaViolation(
                    record_id,
                    f,
                    expected_val,
                    actual_val,
                    f"type mismatch: {type(expected_val).__name__} vs {type(actual_val).__name__}",
                )
            )
            continue
        if expected_val != actual_val:
            violations.append(SchemaViolation(record_id, f, expected_val, actual_val, "value mismatch"))
    return violations


def check_exact_match_batch(
    golden_rows: list[dict[str, Any]],
    rebuild_rows: list[dict[str, Any]],
    *,
    key: str = "id",
    fields: tuple[str, ...] = DETERMINISTIC_FIELDS,
) -> list[SchemaViolation]:
    """Same check across a batch, aligned by `key`. Rows present in one
    side only are reported as violations too (missing/extra output is
    itself a contract break for a deterministic field).
    """
    golden_by_key = {r[key]: r for r in golden_rows}
    rebuild_by_key = {r[key]: r for r in rebuild_rows}
    violations: list[SchemaViolation] = []
    for k in sorted(set(golden_by_key) | set(rebuild_by_key), key=str):
        if k not in rebuild_by_key:
            violations.append(SchemaViolation(k, "<row>", golden_by_key[k], None, "row missing from rebuild output"))
            continue
        if k not in golden_by_key:
            violations.append(
                SchemaViolation(k, "<row>", None, rebuild_by_key[k], "unexpected extra row in rebuild output")
            )
            continue
        violations.extend(check_exact_match_fields(golden_by_key[k], rebuild_by_key[k], fields=fields, record_id=k))
    return violations


@dataclass(frozen=True)
class ToleranceOutlier:
    record_id: Any
    golden_score: float
    rebuild_score: float
    golden_band: str
    rebuild_band: str


@dataclass(frozen=True)
class ToleranceReport:
    n: int
    rank_correlation: float
    rank_correlation_threshold: float
    band_agreement: float
    band_agreement_threshold: float
    outliers: list[ToleranceOutlier] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.rank_correlation >= self.rank_correlation_threshold
            and self.band_agreement >= self.band_agreement_threshold
        )


def check_score_tolerance(
    golden_rows: list[dict[str, Any]],
    rebuild_rows: list[dict[str, Any]],
    *,
    key: str = "id",
    score_field: str = "llm_score",
    rank_correlation_threshold: float,
    band_agreement_threshold: float,
) -> ToleranceReport:
    """p3-contract.md §9.2's tolerance-tested fields, as a pass/fail report
    plus the full outlier list for manual review. Thresholds are REQUIRED
    keyword args (no default) — deliberately: this function must never be
    called with a made-up number; the caller has to have gotten them from
    somewhere real (docs/test-strategy.md's noise-floor procedure, via
    `tests.helpers.stats.compute_tolerance_from_noise_floor`).
    """
    golden_by_key = {r[key]: r for r in golden_rows}
    rebuild_by_key = {r[key]: r for r in rebuild_rows}
    common_keys = sorted(set(golden_by_key) & set(rebuild_by_key), key=str)
    if not common_keys:
        raise ValueError("no overlapping ids between golden and rebuild rows — nothing to compare")

    golden_scores = [float(golden_by_key[k][score_field]) for k in common_keys]
    rebuild_scores = [float(rebuild_by_key[k][score_field]) for k in common_keys]
    golden_bands = [score_band(s) for s in golden_scores]
    rebuild_bands = [score_band(s) for s in rebuild_scores]

    rho = spearman_rank_correlation(golden_scores, rebuild_scores)
    agreement = band_agreement(golden_bands, rebuild_bands)

    outliers = [
        ToleranceOutlier(k, gs, rs, gb, rb)
        for k, gs, rs, gb, rb in zip(common_keys, golden_scores, rebuild_scores, golden_bands, rebuild_bands)
        if gb != rb
    ]

    return ToleranceReport(
        n=len(common_keys),
        rank_correlation=rho,
        rank_correlation_threshold=rank_correlation_threshold,
        band_agreement=agreement,
        band_agreement_threshold=band_agreement_threshold,
        outliers=outliers,
    )


def format_tolerance_report(report: ToleranceReport) -> str:
    """Human-readable report for the orchestrator's manual review pass
    (ADR §9.1: "Manually reviewed by the orchestrator: every sample
    falling outside tolerance") — not machine-consumed, meant to be
    printed/pasted.
    """
    lines = [
        f"P3 parity tolerance report — n={report.n}",
        f"  rank correlation:  {report.rank_correlation:+.4f}  "
        f"(threshold {report.rank_correlation_threshold:+.4f}) "
        f"{'PASS' if report.rank_correlation >= report.rank_correlation_threshold else 'FAIL'}",
        f"  band agreement:    {report.band_agreement:.1%}  "
        f"(threshold {report.band_agreement_threshold:.1%}) "
        f"{'PASS' if report.band_agreement >= report.band_agreement_threshold else 'FAIL'}",
        f"  overall: {'PASS' if report.passed else 'FAIL'}",
    ]
    if report.outliers:
        lines.append(f"  {len(report.outliers)} sample(s) outside band agreement — for manual review:")
        for o in report.outliers:
            lines.append(
                f"    id={o.record_id!r}: golden={o.golden_score:g} ({o.golden_band}) "
                f"vs rebuild={o.rebuild_score:g} ({o.rebuild_band})"
            )
    else:
        lines.append("  no band-crossing outliers.")
    return "\n".join(lines)
