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
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def load_yaml(text: str) -> Any:
    """Parse the YAML subset used by this project's policy files into dicts/lists/scalars."""
    rows: list[tuple[int, str]] = []
    for raw in text.splitlines():
        indent = len(raw) - len(raw.lstrip(" "))
        content = _strip_comment(raw.strip())
        if not content:
            continue
        rows.append((indent, content))

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
            if cur_indent > indent:  # defensive: malformed deeper line, skip
                pos += 1
                continue
            if is_seq:
                if not (content.startswith("- ") or content == "-"):
                    break
                value = content[1:].strip()
                pos += 1
                if value:
                    seq.append(_scalar(value))
                elif pos < len(rows) and rows[pos][0] > indent:
                    seq.append(parse_block(rows[pos][0]))
                else:
                    seq.append(None)
            else:
                if content.startswith("- "):
                    break
                key, _, rest = content.partition(":")
                key = key.strip()
                rest = rest.strip()
                pos += 1
                if rest:
                    mapping[key] = _scalar(rest)
                elif pos < len(rows) and rows[pos][0] > indent:
                    mapping[key] = parse_block(rows[pos][0])
                else:
                    mapping[key] = None
        return seq if is_seq else mapping

    if not rows:
        return {}
    return parse_block(rows[0][0])


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
# Channels a routing rule may legally emit. A typo in retrieval.yaml is filtered out rather than
# silently producing a retrieval_path that runs no channel (a rule emptied by filtering falls back
# to the default mode set, which is itself guaranteed non-empty).
VALID_ROUTE_MODES = frozenset({"keyword", "navigation", "graph", "vector"})


def _clean_modes(modes: list[str]) -> list[str]:
    return [m for m in modes if m in VALID_ROUTE_MODES]
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
}


@dataclass(frozen=True)
class RetrievalPolicy:
    router_rules: dict[str, list[str]] = field(default_factory=lambda: dict(_DEFAULT_ROUTER_RULES))
    default_mode_set: list[str] = field(default_factory=lambda: list(_DEFAULT_MODE_SET))
    caps: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_CAPS))

    def cap(self, key: str) -> int:
        return int(self.caps.get(key, _DEFAULT_CAPS[key]))

    def modes_for_shape(self, shape: str) -> list[str]:
        return list(self.router_rules.get(shape, self.default_mode_set))


def load_retrieval_policy(path: Path) -> RetrievalPolicy:
    """Load `retrieval.yaml` over the defaults. Missing file / keys fall back to defaults."""
    caps = dict(_DEFAULT_CAPS)
    router_rules = {k: list(v) for k, v in _DEFAULT_ROUTER_RULES.items()}
    default_mode_set = list(_DEFAULT_MODE_SET)

    path = Path(path)
    if path.exists():
        data = load_yaml(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # Budgets/caps live under `caps:` (new) and `budgets:` (existing) — both feed caps.
            for section in ("budgets", "caps"):
                block = data.get(section)
                if isinstance(block, dict):
                    for key, value in block.items():
                        if isinstance(value, int):
                            caps[key] = value
            router = data.get("router")
            if isinstance(router, dict):
                rules = router.get("rules")
                if isinstance(rules, dict):
                    for shape, modes in rules.items():
                        if isinstance(modes, list):
                            cleaned = _clean_modes([str(m) for m in modes])
                            if cleaned:  # drop a rule emptied by a typo → falls back to default
                                router_rules[shape] = cleaned
                default_modes = router.get("default_mode_set")
                if isinstance(default_modes, list):
                    cleaned = _clean_modes([str(m) for m in default_modes])
                    if cleaned:
                        default_mode_set = cleaned
                k = router.get("escalation_primary_below_k")
                if isinstance(k, int):
                    caps["escalation_primary_below_k"] = k

    # Guarantee a non-empty default so a routed query always runs at least one real channel.
    if not default_mode_set:
        default_mode_set = list(_DEFAULT_MODE_SET)
    # The RRF constant must be >= 1 (it is a divisor); a malformed policy value falls back to default.
    if not isinstance(caps.get("rrf_k"), int) or caps["rrf_k"] < 1:
        caps["rrf_k"] = _DEFAULT_CAPS["rrf_k"]
    return RetrievalPolicy(router_rules=router_rules, default_mode_set=default_mode_set, caps=caps)
