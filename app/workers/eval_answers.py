#!/usr/bin/env python3
"""ADR-0042 real-vault answer-quality eval: corpus + deterministic scorer + runner.

Key-free core. It loads a curated golden Q&A corpus, scores the cited-answer signals
**deterministically** against per-question predicates, aggregates, and renders a **privacy-safe**
report. The actual `/query` invocation is INJECTED as `query_fn` (a fake in CI, the real pipeline in
`POST /evals/run`), so the scorer + runner are tested without an LLM key. The durable result stores
**only ids/flags/scores/metadata** — never the prompt, evidence pack, answer prose, or any absolute
path (ADR-0042 decision 5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.backend.manifests import is_source_id
from app.backend.policy import load_yaml

SCORING_VERSION = 1

# The evidence-producing /query modes a corpus case may ask for (graph/navigation can't cite, ADR-0034).
QUERY_MODES = frozenset({"auto", "keyword", "vector"})

# Per-question deterministic predicates (ADR-0042 decision 1). All must pass for a case to `pass`.
PREDICATES = (
    "expected_cited", "forbidden_not_cited", "abstain_match",
    "no_unsourced_claims", "no_security_rejections", "answer_when_expected",
)


@dataclass
class Case:
    id: str
    category: str
    question: str
    mode: str
    expected_source_ids: list[str] = field(default_factory=list)
    forbidden_source_ids: list[str] = field(default_factory=list)
    should_abstain: bool = False
    expect_answer: bool = True


def load_corpus(text: str) -> list[Case]:
    """Parse the golden Q&A corpus (the project YAML subset) into Cases. Tolerant of missing optionals;
    structural/existence validation is `validate_case`."""
    data = load_yaml(text)
    raw = data.get("cases", []) if isinstance(data, dict) else []
    cases: list[Case] = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        should_abstain = bool(c.get("should_abstain", False))
        cases.append(Case(
            id=str(c.get("id", "")),
            category=str(c.get("category", "")),
            question=str(c.get("question", "")),
            mode=str(c.get("mode", "auto")),
            expected_source_ids=[str(s) for s in (c.get("expected_source_ids") or [])],
            forbidden_source_ids=[str(s) for s in (c.get("forbidden_source_ids") or [])],
            should_abstain=should_abstain,
            expect_answer=bool(c.get("expect_answer", not should_abstain)),
        ))
    return cases


def validate_case(case: Case, known_source_ids: set[str] | None = None) -> list[str]:
    """Curation errors for one case (empty list = valid). Shape is always checked; **both**
    expected and forbidden source ids must be canonical, and (when `known_source_ids` is provided)
    must resolve against the vault — else the case is a curation skip, never a silent pass."""
    errs: list[str] = []
    if not case.id:
        errs.append("missing id")
    if not case.question.strip():
        errs.append("empty question")
    if case.mode not in QUERY_MODES:
        # Don't echo the raw mode (untrusted) — report the allowed set only.
        errs.append(f"unsupported mode (allowed: {sorted(QUERY_MODES)})")
    for label, ids in (("expected", case.expected_source_ids), ("forbidden", case.forbidden_source_ids)):
        for i, sid in enumerate(ids):
            # A non-canonical id is untrusted (could be a path like /home/...): report field+index,
            # NEVER the raw value, so it can't leak into the durable artifact. A canonical (src_<16 hex>)
            # id that's simply absent is safe to name (it's a content hash, not a path).
            if not is_source_id(sid):
                errs.append(f"{label}_source_ids[{i}] not canonical (src_<16 hex>)")
            elif known_source_ids is not None and sid not in known_source_ids:
                errs.append(f"{label}_source_id {sid} not in vault")
    if case.should_abstain and case.expected_source_ids:
        errs.append("should_abstain is contradictory with expected_source_ids")
    return errs


def score_case(case: Case, signals: dict[str, Any]) -> dict[str, Any]:
    """Deterministic per-question score from the /query signals (ADR-0042 decision 1). `signals` carries
    only ids/flags/counts — no prose."""
    cited = {s for s in signals.get("cited_source_ids", []) if isinstance(s, str)}
    expected = set(case.expected_source_ids)
    forbidden = set(case.forbidden_source_ids)
    abstained = bool(signals.get("abstained", False))
    unsourced = int(signals.get("unsourced_count", 0))
    security = int(signals.get("security_rejected_count", 0))
    answer_produced = not abstained
    predicates = {
        "expected_cited": expected <= cited,
        "forbidden_not_cited": not (forbidden & cited),
        "abstain_match": abstained == case.should_abstain,
        "no_unsourced_claims": unsourced == 0,
        "no_security_rejections": security == 0,
        "answer_when_expected": (not case.expect_answer) or answer_produced,
    }
    return {
        "id": case.id, "category": case.category,
        "expected_source_ids": sorted(expected), "forbidden_source_ids": sorted(forbidden),
        "cited_source_ids": sorted(cited),
        "abstained": abstained, "unsourced_count": unsourced, "security_rejected_count": security,
        "citation_recall": (len(expected & cited) / len(expected)) if expected else None,
        "citation_precision": (len(expected & cited) / len(cited)) if cited else None,
        "predicates": predicates,
        "pass": all(predicates.values()),
        "fail_reasons": sorted(k for k, v in predicates.items() if not v),
    }


def _predicate_pass_rates(results: list[dict[str, Any]]) -> dict[str, float]:
    if not results:
        return {p: 0.0 for p in PREDICATES}
    return {p: sum(1 for r in results if r["predicates"][p]) / len(results) for p in PREDICATES}


def run_eval(cases: list[Case], query_fn: Callable[[Case], dict[str, Any]], *, limit: int,
             known_source_ids: set[str] | None = None,
             cache_mode: str = "cached") -> dict[str, Any]:
    """Validate -> select up to `limit` valid cases -> score each via the injected `query_fn`.

    `query_fn(case)` returns scoreable signals only: `abstained`, `cited_source_ids`,
    `unsourced_count`, `security_rejected_count`, and optionally `cache_hit` (bool). No prose/prompt.
    """
    valid: list[Case] = []
    skipped: list[dict[str, Any]] = []
    for case in cases:
        errs = validate_case(case, known_source_ids)
        (skipped.append({"id": case.id, "reasons": errs}) if errs else valid.append(case))

    to_run = valid[: max(0, limit)]
    results: list[dict[str, Any]] = []
    cache_hits = cache_misses = 0
    for case in to_run:
        signals = query_fn(case)
        hit = signals.get("cache_hit")
        if hit is True:
            cache_hits += 1
        elif hit is False:
            cache_misses += 1
        results.append(score_case(case, signals))

    passed = sum(1 for r in results if r["pass"])
    return {
        "scoring_version": SCORING_VERSION,
        "n_corpus": len(cases), "n_valid": len(valid), "n_run": len(to_run),
        "n_skipped": len(skipped), "n_passed": passed, "n_failed": len(results) - passed,
        "predicate_pass_rates": _predicate_pass_rates(results),
        "cache_mode": cache_mode, "cache_hits": cache_hits, "cache_misses": cache_misses,
        "results": results, "skipped": skipped,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Human summary of a stamped report (no prose/prompt/evidence — ids/flags/scores only)."""
    m = report.get("meta", {})
    lines = [
        "# Answer-quality eval report (ADR-0042)", "",
        f"- run_id: {m.get('run_id')}",
        f"- created_at: {m.get('created_at')}",
        f"- scoring_version: {report.get('scoring_version')}",
        f"- model_ref: {m.get('model_ref')}  (provider: {m.get('model_provider')})",
        f"- cache_mode: {report.get('cache_mode')}  "
        f"(hits {report.get('cache_hits')}, misses {report.get('cache_misses')})",
        f"- vault: {m.get('vault_fingerprint')}  (graph_schema {m.get('graph_schema_version')})",
        f"- corpus {report['n_corpus']} | valid {report['n_valid']} | run {report['n_run']} | "
        f"skipped {report['n_skipped']}",
        f"- **passed {report['n_passed']} / {report['n_run']}**", "",
        "## Predicate pass rates", "", "| predicate | rate |", "|---|---|",
    ]
    for p, rate in report["predicate_pass_rates"].items():
        lines.append(f"| {p} | {rate:.2f} |")
    lines += ["", "## Per question", "", "| id | category | pass | fail_reasons | recall | precision |",
              "|---|---|---|---|---|---|"]
    for r in report["results"]:
        rec = "-" if r["citation_recall"] is None else f"{r['citation_recall']:.2f}"
        prec = "-" if r["citation_precision"] is None else f"{r['citation_precision']:.2f}"
        lines.append(f"| {r['id']} | {r['category']} | {'PASS' if r['pass'] else 'FAIL'} | "
                     f"{', '.join(r['fail_reasons'])} | {rec} | {prec} |")
    if report["skipped"]:
        lines += ["", "## Skipped (curation errors)", ""]
        lines += [f"- {s['id']}: {'; '.join(s['reasons'])}" for s in report["skipped"]]
    return "\n".join(lines) + "\n"
