#!/usr/bin/env python3
"""Possible author/publisher name variants (ADR-0070 B5): a hint on the Details form and a
report-only lint. Nothing merges and identity is unchanged (`manifests.name_identity`, one Authors
page per normalized name); a similar name is a suggestion for a human, never evidence that two
documents share an author or publisher.

**Matching rule — a provisional hint heuristic** (deterministic and bounded; it has false
positives a human dismisses). Only **confirmed** names on valid manifests, both roles (the role is
shown, never hidden). The fold is `merge_key`'s tokenization **without its plural step** (names:
"Williams" never folds to "William"), with accents removed — for the hint only. Two different
identities are a possible variant when either:

- **(a) whole-token containment:** the shorter fold's tokens occur as a contiguous run in the
  longer one's, and the shorter has a token of at least 4 letters that isn't a generic
  institutional word ("McKinsey" ⊂ "McKinsey & Company"; "University" never hints "Oxford
  University");
- **(b) initials:** the same number of tokens, the same last token, and every other token of one is
  a single-letter initial matching the other's first letter ("D. C. Dennett" ~ "Daniel C.
  Dennett").

Candidates come from a word index over qualifying tokens (and, for rule (b), the last token).
Work is bounded by a deterministic comparison budget, separate from the display limits.
"""
from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

from app.backend import manifests
from app.workers import merge_candidates

GENERIC = frozenset({
    "university", "institute", "institution", "school", "college", "company", "corporation",
    "group", "press", "consortium", "foundation", "association", "society", "department",
    "centre", "center", "council", "agency", "office", "laboratory", "lab", "research",
    "international", "national", "global"})
FORM_BUDGET = 2_000          # pairs compared per form render
LINT_BUDGET = 20_000         # pairs compared per lint run
HINTS_PER_FIELD = 3
LINT_PAIRS = 50
_MIN_LETTERS = 4


def fold(name: Any) -> tuple[str, ...]:
    """`merge_key`'s tokens without the plural step, accents removed (hint only)."""
    if not isinstance(name, str):
        return ()
    tokens = merge_candidates._tokens(name)
    out = []
    for t in tokens:
        stripped = "".join(c for c in unicodedata.normalize("NFKD", t)
                           if not unicodedata.combining(c))
        if stripped:
            out.append(stripped)
    return tuple(out)


def _qualifying(tokens: tuple[str, ...]) -> set[str]:
    return {t for t in tokens if sum(c.isalpha() for c in t) >= _MIN_LETTERS and t not in GENERIC}


def _contains(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    n = len(short)
    return any(long[i:i + n] == short for i in range(len(long) - n + 1))


def rule(a: tuple[str, ...], b: tuple[str, ...]) -> str | None:
    """`"containment"`, `"initials"` or None for two folds of DIFFERENT identities."""
    if not a or not b:
        return None
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if _contains(short, long) and _qualifying(short):
        return "containment"
    if len(a) == len(b) and len(a) >= 2 and a[-1] == b[-1]:
        initial = False
        for x, y in zip(a[:-1], b[:-1], strict=True):
            if x == y:
                continue
            if len(x) == 1 and y.startswith(x) or len(y) == 1 and x.startswith(y):
                initial = True
                continue
            return None
        if initial:
            return "initials"
    return None


def confirmed_names(manifests_dir: Path) -> dict[str, dict[str, Any]]:
    """`{identity: {name, tokens, uses: [{role, source_id, title}]}}` — every confirmed author and
    publisher on a valid manifest (the spelling first seen per identity is the shown name)."""
    out: dict[str, dict[str, Any]] = {}
    for m in sorted(manifests.valid_manifests(Path(manifests_dir))[0],
                    key=lambda m: str(m.get("source_id"))):
        details = manifests.confirmed_details(m.get("provenance") or {})
        title = details.get("title") or m.get("original_filename") or m.get("source_id")
        entries = [("author", a) for a in details["authors"]]
        if details["publisher"]:
            entries.append(("publisher", details["publisher"]))
        for role, e in entries:
            ident = manifests.name_identity(e["name"])
            if not ident:
                continue
            rec = out.setdefault(ident, {"name": e["name"], "tokens": fold(e["name"]), "uses": []})
            rec["uses"].append({"role": role, "source_id": m.get("source_id"), "title": title})
    return out


class Budget:
    """A deterministic comparison budget shared across one form render or one lint run."""

    def __init__(self, limit: int) -> None:
        self.limit, self.used, self.truncated = limit, 0, False

    def take(self) -> bool:
        if self.used >= self.limit:
            self.truncated = True
            return False
        self.used += 1
        return True


def _index(names: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
    """Word index: qualifying token → identities, plus `last:<token>` → identities (rule (b))."""
    idx: dict[str, set[str]] = {}
    for ident, rec in names.items():
        toks = rec["tokens"]
        for t in _qualifying(toks):
            idx.setdefault(t, set()).add(ident)
        if toks:
            idx.setdefault(f"last:{toks[-1]}", set()).add(ident)
    return idx


def _candidates(tokens: tuple[str, ...], idx: dict[str, set[str]]) -> list[str]:
    found: set[str] = set()
    for t in _qualifying(tokens):
        found |= idx.get(t, set())
    if tokens:
        found |= idx.get(f"last:{tokens[-1]}", set())
    return sorted(found)                                   # sorted name_identity order


def hints(name: Any, names: dict[str, dict[str, Any]], *, exclude_source: str | None,
          budget: Budget, idx: dict[str, set[str]] | None = None) -> list[dict[str, Any]]:
    """Up to :data:`HINTS_PER_FIELD` confirmed spellings that may be variants of `name` (another
    identity), each with where it is confirmed (role + documents, this document excluded); ordered
    by the number of confirming documents, then by name."""
    ident = manifests.name_identity(name) if isinstance(name, str) else ""
    tokens = fold(name)
    if not ident or not tokens:
        return []
    idx = idx if idx is not None else _index(names)
    out = []
    for other in _candidates(tokens, idx):
        if other == ident:
            continue
        if not budget.take():
            break
        rec = names[other]
        which = rule(tokens, rec["tokens"])
        uses = [u for u in rec["uses"] if u["source_id"] != exclude_source]
        if which and uses:
            out.append({"name": rec["name"], "identity": other, "rule": which, "uses": uses})
    out.sort(key=lambda h: (-len({u["source_id"] for u in h["uses"]}), h["name"].casefold()))
    return out[:HINTS_PER_FIELD]


def variant_pairs(names: dict[str, dict[str, Any]], *,
                  budget_limit: int = LINT_BUDGET) -> dict[str, Any]:
    """The lint's possible variants: `{pairs: [...≤50], more, truncated, compared}`. Each pair once,
    visited in sorted identity order; deterministic under the budget."""
    idx = _index(names)
    budget = Budget(budget_limit)
    pairs = []
    for ident in sorted(names):
        rec = names[ident]
        for other in _candidates(rec["tokens"], idx):
            if other <= ident:
                continue
            if not budget.take():
                break
            which = rule(rec["tokens"], names[other]["tokens"])
            if which:
                pairs.append({"a": {"name": rec["name"], "uses": rec["uses"]},
                              "b": {"name": names[other]["name"], "uses": names[other]["uses"]},
                              "rule": which})
        if budget.truncated:
            break
    return {"pairs": pairs[:LINT_PAIRS], "more": max(0, len(pairs) - LINT_PAIRS),
            "truncated": budget.truncated, "compared": budget.used}
