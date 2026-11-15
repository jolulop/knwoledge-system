#!/usr/bin/env python3
"""Phase 4c retrieval policy loader (`policies/retrieval.yaml`, ADR-0032 decision 4).

The deterministic retrieval router reads its routing taxonomy and budgets from policy rather than
hardcoding them. The project stays dependency-light (no PyYAML), so this module ships a **minimal
YAML-subset loader** — exactly the structures the project's policy files use: nested mappings
(2-space indent), sequences of scalars, and scalar values (int / float / bool / quoted or bare
string). It is intentionally not a general YAML parser; it is small, deterministic, and tested
against the real policy files.

:func:`load_retrieval_policy` layers the parsed file over hardcoded defaults, so a missing file or
absent key never crashes the router — the defaults are the source of truth for shape, the file
tunes the numbers.

ADR-0074 decision 4 (slice 3c): every policy file under `policies/` is read through
:func:`read_policy` / :func:`read_policy_text`. A **verified absence** means the code defaults
(the domain allows it — a vault without the file runs on defaults); a policy that exists but is
**unreadable or corrupt** is never taken for absence and never replaced by defaults: the read
raises :class:`PolicyUnavailable` and the operation that depends on it refuses (decision 4.4 —
a policy is a shared input).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any

from app.backend import authority_read

logger = logging.getLogger(__name__)


class PolicyUnavailable(RuntimeError):
    """A policy file exists but can't be used (unreadable, invalid UTF-8, not a mapping): the
    operation that reads it refuses instead of running on defaults. Categorical, never contents."""

    def __init__(self, path: Path, state: str, error: str | None) -> None:
        self.name = authority_read.safe_name(Path(path).name)
        self.state = state
        self.error = error or state
        super().__init__(f"policies/{self.name} is {state} ({self.error}); fix it or restore it "
                         "from backup — operations that depend on it are refused until then")


def read_policy_text(path: Path) -> str | None:
    """A policy file's text; None for a verified absence. Raises :class:`PolicyUnavailable` for an
    unreadable or non-UTF-8 file (non-blocking, links never followed)."""
    read = authority_read.read_text(Path(path))
    if read.state == authority_read.ABSENT:
        return None
    if read.state != authority_read.OK:
        raise PolicyUnavailable(path, read.state, read.error)
    return read.text


class _InvalidField(ValueError):
    """A present policy value of the wrong type or out of range (`field` is its dotted name)."""

    def __init__(self, field_name: str) -> None:
        super().__init__(field_name)
        self.field = field_name


def read_policy(path: Path, check: Callable[[dict[str, Any]], None] | None = None,
                ) -> dict[str, Any] | None:
    """A YAML policy file as a mapping (an empty file is `{}`); None for a verified absence. Raises
    :class:`PolicyUnavailable` when it exists but is unreadable, invalid UTF-8, outside the strict
    YAML subset (`invalid_yaml`), its top level is not a mapping, or — with `check`, the policy
    family's field validator — a PRESENT value has the wrong type or range
    (`invalid_field:<dotted.name>`). Missing fields keep their defaults; unknown keys are kept."""
    text = read_policy_text(path)
    if text is None:
        return None
    try:
        data = load_yaml(text, strict=True)
    except Exception as exc:  # noqa: BLE001 - any parser failure is a corrupt policy
        raise PolicyUnavailable(path, authority_read.CORRUPT, "invalid_yaml") from exc
    if not isinstance(data, dict):
        raise PolicyUnavailable(path, authority_read.CORRUPT, "not_a_mapping")
    if check is not None:
        try:
            check(data)
        except _InvalidField as exc:
            raise PolicyUnavailable(path, authority_read.CORRUPT,
                                    f"invalid_field:{exc.field}") from exc
    return data


# --------------------------------------------------------------------------- field validation
# ADR-0074 slice 3c (review round 1): each policy family validates the fields it consumes when they
# are present. A missing field keeps its default; a present one of the wrong type or range refuses
# (never replaced by the default). Booleans are never accepted where a number is required.


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _section(data: dict[str, Any], key: str, dotted: str | None = None) -> dict[str, Any]:
    """A present mapping section (`key:` with nothing under it is an empty section); {} if absent."""
    if key not in data or data[key] is None:
        return {}
    if not isinstance(data[key], dict):
        raise _InvalidField(dotted or key)
    return data[key]


def _int_field(section: dict[str, Any], key: str, dotted: str, minimum: int = 0) -> None:
    if key in section and not (_is_int(section[key]) and section[key] >= minimum):
        raise _InvalidField(dotted)


def _bool_field(section: dict[str, Any], key: str, dotted: str) -> None:
    if key in section and not isinstance(section[key], bool):
        raise _InvalidField(dotted)


def _modes_field(value: Any, dotted: str) -> None:
    if not (isinstance(value, list) and value
            and all(isinstance(m, str) and m in VALID_ROUTE_MODES for m in value)):
        raise _InvalidField(dotted)


def check_retrieval(data: dict[str, Any]) -> None:
    """`retrieval.yaml`: every `budgets`/`caps` value an integer ≥ 0 (`rrf_k` ≥ 1, a divisor),
    `weights` numbers ≥ 0, router rules and the default mode set non-empty lists of known modes."""
    for name in ("budgets", "caps"):
        block = _section(data, name)
        for key in block:
            _int_field(block, key, f"{name}.{key}", minimum=1 if key == "rrf_k" else 0)
    weights = _section(data, "weights")
    for key, value in weights.items():
        if not (_is_number(value) and value >= 0):
            raise _InvalidField(f"weights.{key}")
    router = _section(data, "router")
    if "default_mode_set" in router:
        _modes_field(router["default_mode_set"], "router.default_mode_set")
    for shape, modes in _section(router, "rules", "router.rules").items():
        _modes_field(modes, f"router.rules.{shape}")
    _int_field(router, "escalation_primary_below_k", "router.escalation_primary_below_k")


def check_retention(data: dict[str, Any]) -> None:
    """`retention.yaml`: the thresholds and switches `/jobs/stale-check` consumes."""
    raw = _section(data, "raw_files")
    _int_field(raw, "older_than_years_archive_candidate",
               "raw_files.older_than_years_archive_candidate")
    eph = _section(data, "ephemeral")
    _bool_field(eph, "enabled", "ephemeral.enabled")
    _int_field(eph, "delete_candidate_after_days", "ephemeral.delete_candidate_after_days")
    cache = _section(data, "response_cache")
    _bool_field(cache, "enabled", "response_cache.enabled")
    _int_field(cache, "cache_ttl_days", "response_cache.cache_ttl_days")
    _int_field(cache, "cache_max_mb", "response_cache.cache_max_mb")


def check_review(data: dict[str, Any]) -> None:
    """`review.yaml`: lint's merge-candidate key budget (an integer ≥ 2)."""
    _int_field(_section(data, "merge_candidates"), "max_items_per_key",
               "merge_candidates.max_items_per_key", minimum=2)


def check_citation(data: dict[str, Any]) -> None:
    """`citation.yaml`: the abstention text (a non-empty string)."""
    text = data.get("when_no_source_found")
    if "when_no_source_found" in data and not (isinstance(text, str) and text.strip()):
        raise _InvalidField("when_no_source_found")

# --------------------------------------------------------------------------- minimal YAML subset


def _strip_comment(line: str) -> str:
    """Drop a YAML comment: a ``#`` that is outside quotes and at line start or after whitespace.

    Quote-aware so a ``#`` inside a quoted scalar (or a non-whitespace-preceded ``a#b``) is kept.
    """
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in {'"', "'"}:
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line


def _scalar(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return None
    if raw[0] in {'"', "'"} and raw[-1:] == raw[0]:
        return raw[1:-1]
    low = raw.lower()
    if low in {"true", "false"}:
        return low == "true"
    if low in {"null", "~"}:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


class YamlSubsetError(ValueError):
    """Text outside the strict policy YAML subset (`load_yaml(..., strict=True)`)."""


def load_yaml(text: str, *, strict: bool = False) -> Any:
    """Parse the YAML subset used by this project's policy files into dicts/lists/scalars.

    `strict` (every policy read, ADR-0074 slice 3c review round 1) raises :class:`YamlSubsetError`
    for anything outside the subset instead of guessing: a tab in the indentation, a line deeper
    than its block or at an indentation no open block has, a mapping line without `key:`, an empty
    or duplicate key, a sequence item inside a mapping (or the reverse), a flow value (`[...]` /
    `{...}`), an unterminated quoted scalar, or anything left over after the top-level block.
    Without it (golden eval files), the historical lenient behaviour is unchanged."""
    def fail(message: str) -> None:
        if strict:
            raise YamlSubsetError(message)

    rows: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        if strict and stripped[:1] == "\t" and _strip_comment(raw.strip()):
            fail("tab in indentation")
        content = _strip_comment(raw.strip())
        if not content:
            continue
        rows.append((indent, content))

    def value_of(raw: str) -> Any:
        raw = raw.strip()
        if strict:
            if raw[:1] in {"[", "{"}:
                fail("flow value")
            if raw[:1] in {'"', "'"} and (len(raw) < 2 or raw[-1] != raw[0]):
                fail("unterminated quoted scalar")
        return _scalar(raw)

    pos = 0

    def parse_block(indent: int) -> Any:
        nonlocal pos
        # A block is a sequence if its first line at this indent is a "- " item, else a mapping.
        is_seq = rows[pos][1].startswith("- ") or rows[pos][1] == "-"
        seq: list[Any] = []
        mapping: dict[str, Any] = {}
        while pos < len(rows):
            cur_indent, content = rows[pos]
            if cur_indent < indent:
                break
            # A deeper line — or a dedent to an indentation no open block has, which the enclosing
            # block sees as deeper — is skipped (lenient) / refused (strict).
            if cur_indent > indent:
                fail("line deeper than its block")
                pos += 1
                continue
            if is_seq:
                if not (content.startswith("- ") or content == "-"):
                    fail("mapping line inside a sequence")
                    break
                value = content[1:].strip()
                pos += 1
                if value:
                    seq.append(value_of(value))
                elif pos < len(rows) and rows[pos][0] > indent:
                    seq.append(parse_block(rows[pos][0]))
                else:
                    seq.append(None)
            else:
                if content.startswith("- ") or content == "-":
                    fail("sequence item inside a mapping")
                    break
                key, colon, rest = content.partition(":")
                key = key.strip()
                rest = rest.strip()
                if not colon or not key:
                    fail("mapping line without a key")
                if key in mapping:
                    fail(f"duplicate key {key!r}")
                pos += 1
                if rest:
                    mapping[key] = value_of(rest)
                elif pos < len(rows) and rows[pos][0] > indent:
                    mapping[key] = parse_block(rows[pos][0])
                else:
                    mapping[key] = None
        return seq if is_seq else mapping

    if not rows:
        return {}
    data = parse_block(rows[0][0])
    if pos < len(rows):
        fail("content after the top-level block")
    return data


# --------------------------------------------------------------------------- retrieval policy

# Hardcoded defaults — the contract's shape lives here; retrieval.yaml only tunes the numbers and
# the shape→mode routing. The classifier's *signal detection* is code (search.py); the policy maps
# a detected query shape to a set of retrieval modes plus the per-group budgets.
_DEFAULT_ROUTER_RULES: dict[str, list[str]] = {
    "exact": ["keyword"],
    "relationship": ["graph"],
    "disagreement": ["graph"],
    "mention": ["keyword", "graph"],
    "discovery": ["navigation", "graph"],
}
_DEFAULT_MODE_SET = ["keyword"]  # conceptual default; vector joins in Phase 4d
# Channels a routing rule may legally emit. A typo in retrieval.yaml refuses the policy (ADR-0074
# slice 3c review round 1; it was filtered out before), so a rule can never run no channel.
VALID_ROUTE_MODES = frozenset({"keyword", "navigation", "graph", "vector"})

_DEFAULT_CAPS = {
    "max_evidence_hits": 20,
    "max_navigation_hits": 20,
    "max_graph_nodes": 50,
    "max_graph_edges": 100,
    "per_channel_prefusion_limit": 50,
    "max_graph_depth_default": 2,
    "max_query_chars": 512,
    "max_query_terms": 32,
    "escalation_primary_below_k": 3,
    "rrf_k": 60,  # Reciprocal Rank Fusion constant (Phase 4e); canonical default
    # ADR-0068 decision 7: bounded graph traversal (clamped to graph_read's hard ceilings).
    "max_graph_fanout": 25,
    "max_disagreement_seeds": 50,
    "max_graph_queries": 500,
}
# Fractional retrieval weights (ADR-0062) — kept apart from the integer `caps` because they are
# compared against RRF scores (Σ 1/(k+rank) ≈ 0.016/rank at k=60). `item_type_boost` is the additive
# on-type bonus for the evidence faceting boost; 0.0 disables it. Small so it re-ranks within a few
# positions but never outranks a much-more-relevant off-type chunk.
_DEFAULT_WEIGHTS = {
    "item_type_boost": 0.005,
}


def _max_item_type_boost(rrf_k: int, prefusion: int) -> float:
    """The architectural hard cap on the ADR-0062 evidence boost (review round 1). Derived from the
    RRF scale so "bounded" is enforced, not conventional: the gap between a rank-1 single-channel hit
    (``1/(k+1)``) and a tail single-channel hit (``1/(k+prefusion)``). At the ceiling a tail on-type
    hit can rise to just *below* a rank-1 off-type hit, never above — so the facet can never lift an
    on-type chunk over a clearly-more-relevant off-type one. Also floored at the 0.005 default so the
    cap can't exceed the shipped default when k/prefusion are unusual."""
    prefusion = max(2, prefusion)  # prefusion == 1 would make the gap 0; guard the divisor
    gap = 1.0 / (rrf_k + 1) - 1.0 / (rrf_k + prefusion)
    return max(0.0, min(0.005, gap))


@dataclass(frozen=True)
class RetrievalPolicy:
    router_rules: dict[str, list[str]] = field(default_factory=lambda: dict(_DEFAULT_ROUTER_RULES))
    default_mode_set: list[str] = field(default_factory=lambda: list(_DEFAULT_MODE_SET))
    caps: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_CAPS))
    weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))

    def cap(self, key: str) -> int:
        return int(self.caps.get(key, _DEFAULT_CAPS[key]))

    def weight(self, key: str) -> float:
        return float(self.weights.get(key, _DEFAULT_WEIGHTS[key]))

    def modes_for_shape(self, shape: str) -> list[str]:
        return list(self.router_rules.get(shape, self.default_mode_set))


def load_retrieval_policy(path: Path) -> RetrievalPolicy:
    """Load `retrieval.yaml` over the defaults. A missing file / absent keys fall back to defaults;
    a damaged file — or a present value of the wrong type or range (:func:`check_retrieval`: an
    unknown mode, a negative cap, `rrf_k` < 1, a negative weight) — raises
    :class:`PolicyUnavailable` (ADR-0074 slice 3c, review round 1: never filtered or defaulted)."""
    caps = dict(_DEFAULT_CAPS)
    weights = dict(_DEFAULT_WEIGHTS)
    router_rules = {k: list(v) for k, v in _DEFAULT_ROUTER_RULES.items()}
    default_mode_set = list(_DEFAULT_MODE_SET)

    data = read_policy(Path(path), check_retrieval) or {}
    # Budgets/caps live under `caps:` (new) and `budgets:` (existing) — both feed caps.
    for section in ("budgets", "caps"):
        caps.update(_section(data, section))
    weights.update({k: float(v) for k, v in _section(data, "weights").items()})
    router = _section(data, "router")
    router_rules.update({shape: list(modes)
                         for shape, modes in _section(router, "rules").items()})
    if "default_mode_set" in router:
        default_mode_set = list(router["default_mode_set"])
    if "escalation_primary_below_k" in router:
        caps["escalation_primary_below_k"] = router["escalation_primary_below_k"]

    # Enforce the architectural upper bound (ADR-0062 review round 1): a config value above the cap
    # would turn the advisory boost into a hidden evidence filter. Clamp loudly; config can only
    # lower it (or set 0), never raise it past the RRF-derived ceiling.
    cap = _max_item_type_boost(caps["rrf_k"], caps["per_channel_prefusion_limit"])
    if weights["item_type_boost"] > cap:
        logger.warning("item_type_boost %.4f exceeds the architectural cap %.4f (rrf_k=%d, "
                       "prefusion=%d); clamping to the cap", weights["item_type_boost"], cap,
                       caps["rrf_k"], caps["per_channel_prefusion_limit"])
        weights["item_type_boost"] = cap
    return RetrievalPolicy(router_rules=router_rules, default_mode_set=default_mode_set,
                           caps=caps, weights=weights)
