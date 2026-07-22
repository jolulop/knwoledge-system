#!/usr/bin/env python3
"""Phase 4c deterministic retrieval router + ``GET /search`` orchestration (ADR-0032 §4, §8).

No LLM and no answer text — ``/search`` returns ranked, citation-anchored **evidence** plus
status-aware **navigation** and active-default **graph** hits, in three labelled groups that never
share a relevance scale (ADR-0032 decision 6). The pieces:

- :func:`extract_terms` / :func:`safe_fts_query` — turn free user text into a quoted FTS5 ``MATCH``
  expression. Stopwords and the Build Spec §8.2 routing trigger words are dropped so a routed
  natural-language prompt ("what do I know about synergy") searches the *topic* ("synergy"), not the
  question words. Raw input never reaches ``MATCH`` (which throws on ``"``/``*``/``:``/``NEAR``); the
  length and term count are bounded. This deterministic term extraction is the key-free stand-in
  until vector retrieval (Phase 4d) understands topics semantically.
- :func:`classify_shape` / :func:`route` — deterministic query-shape detection against §8.2; the
  shape→mode mapping and budgets come from ``policies/retrieval.yaml`` (``policy.py``).
- :func:`run_search` — runs the routed channels over the Phase 4a keyword index and a Phase 4b graph
  subgraph (multi-seed BFS at the policy depth budget), applies retention-aware status filters and
  per-group caps, and assembles the grouped response.

Scope: keyword/navigation/graph + vector. All chunk evidence flows through :func:`fuse_evidence`
(Reciprocal Rank Fusion) — single-channel results fuse too, so every hit carries the `channels` detail
and an RRF top-level `score`. `mode=auto` blends keyword+vector for the conceptual default and escalates
to vector for keyword-primary shapes when keyword evidence is sparse; when vector is unavailable, auto
degrades to keyword-only (a `notes` entry; never a 5xx — that is reserved for explicit `mode=vector`).
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Callable

from app.backend import graph_read
from app.backend.policy import RetrievalPolicy

# Injected by the endpoint for the vector channel: returns LanceDB rows (citation fields +
# `_distance`). Kept as a callable so search.py never imports vector_index/lancedb (the optional
# dependency stays isolated to vector_index.py + reindex_vector.py). It embeds the query lazily, so
# it is only invoked — and the embedding cost only paid — when the vector channel actually runs.
VectorSearchFn = Callable[..., list[dict[str, Any]]]


class VectorUnavailable(Exception):
    """The vector *backend* (embedder / index read) could not serve at query time. Raised by the
    injected `vector_search` callable so degradation is narrow: a row-mapping/fusion bug is NOT a
    VectorUnavailable and propagates normally instead of being hidden as a keyword-only fallback."""


class VectorChannelError(RuntimeError):
    """The vector channel was unavailable while serving an *explicit* ``mode=vector`` request (the
    endpoint maps this to 503). In ``mode=auto`` a :class:`VectorUnavailable` degrades to keyword-only."""

VALID_MODES = frozenset({"keyword", "vector", "graph", "navigation", "auto"})
# Default retention window (ADR-0032 decision 8): deprecated content stays searchable; everything
# else (archived / deleted / candidate / unknown) is excluded unless a caller explicitly asks.
RETENTION_DEFAULT_STATUSES = ("active", "deprecated_candidate")
# Wiki page types whose node_id participates in the graph (so a navigation hit can seed the graph
# group). Source/query/tag pages are not graph seeds.
GRAPH_SEED_TYPES = frozenset(
    {"item", "claim", "synthesis"}
)

_TERM_RE = re.compile(r"\w+", re.UNICODE)
# Index of the lone indexed `text` column in the evidence FTS table, for snippet().
_EVIDENCE_TEXT_COL = 12

# Function words + Build Spec §8.2 routing trigger words. Dropped from the FTS topic so the router's
# query shape is captured by classify_shape() while the search runs on the content terms only. This
# is a deliberately small, deterministic list — not a linguistic stemmer (that is a Phase 4d/5 job).
_STOPWORDS = frozenset({
    # articles / conjunctions / prepositions
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "for", "to", "with", "by", "from", "as",
    "into", "about", "over", "between", "across", "than", "vs",
    # pronouns / question words / common verbs
    "i", "we", "you", "it", "they", "me", "my", "our", "your", "their", "them", "us",
    "what", "which", "who", "whom", "whose", "how", "when", "where", "why",
    "do", "does", "did", "is", "are", "am", "was", "were", "be", "been", "being",
    "that", "this", "these", "those", "there", "here",
    # §8.2 routing trigger verbs / nouns
    "know", "knew", "tell", "show", "find", "give", "list", "get",
    "related", "relate", "relates", "relationship", "relationships", "connection", "connections",
    "connect", "connected", "disagree", "disagrees", "disagreement", "contradict", "contradicts",
    "contradiction", "contradictory", "conflict", "conflicts", "inconsistent",
    "mention", "mentions", "mentioned", "discuss", "discusses", "cover", "covers", "reference",
    "references", "source", "sources", "document", "documents", "file", "files", "paper", "papers",
    "overview", "summary", "summarize", "summaries", "have", "has", "had",
    # misc fillers
    "any", "some", "all", "more", "most", "please", "can", "could", "would", "should", "about",
})


# --------------------------------------------------------------------------- term extraction


def extract_terms(q: str, *, max_chars: int, max_terms: int) -> list[str]:
    """Lowercase content terms of a query: stopwords/trigger words removed, de-duped, bounded."""
    tokens = _TERM_RE.findall((q or "")[:max_chars].lower())
    out: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        if t in _STOPWORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_terms:
            break
    return out


def build_fts_match(terms: list[str], *, op: str = "AND") -> str | None:
    """Join content terms into a quoted FTS5 ``MATCH`` string. ``None`` if there are no terms.

    ``op="AND"`` (implicit-AND, all terms) is used for **evidence** — exact-lookup precision.
    ``op="OR"`` is used for **navigation/graph seeding** — a relationship query ("how are X and Y
    related") must find the *item* pages for X or Y separately so the graph can connect them; an
    AND would demand one page containing both. Each term is a quoted phrase (embedded quotes
    doubled), neutralizing FTS5 operators in user input.
    """
    if not terms:
        return None
    quoted = ['"' + t.replace('"', '""') + '"' for t in terms]
    return (" " if op == "AND" else " OR ").join(quoted)


def safe_fts_query(q: str, *, max_chars: int, max_terms: int) -> str | None:
    """The implicit-AND evidence match for a query's content terms (``None`` if no terms)."""
    return build_fts_match(extract_terms(q, max_chars=max_chars, max_terms=max_terms), op="AND")


# --------------------------------------------------------------------------- query classifier

_REL_RE = re.compile(r"how (are|is|do|does)\b.*\brelat|relationship between|related to|"
                     r"connection between|how .* connect", re.I)
_DISAGREE_RE = re.compile(r"disagree|contradict|conflict|inconsisten", re.I)
_MENTION_RE = re.compile(r"which (documents?|sources?|files?|papers?) (mention|discuss|cover|"
                         r"reference|talk about)", re.I)
_DISCOVERY_RE = re.compile(r"what do (i|we) know about|tell me about|overview of|"
                           r"summar(y|ize) of|what do (i|we) have on", re.I)
_FILENAME_RE = re.compile(r"\b[\w-]+\.(pdf|docx?|md|html?|csv|xlsx?|txt|pptx?|json)\b", re.I)
_QUOTED_RE = re.compile(r"[\"“”‘’]")
_NUMBER_RE = re.compile(r"\d")
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,6}s?\b")


def classify_shape(q: str) -> str:
    """Deterministically map a query to a Build Spec §8.2 shape. First match wins.

    Returns one of: ``relationship``, ``disagreement``, ``mention``, ``discovery``, ``exact``,
    ``default``. Recency / archival query types stay out of the router (ADR-0032 decision 4).
    """
    text = q or ""
    if _REL_RE.search(text):
        return "relationship"
    if _DISAGREE_RE.search(text):
        return "disagreement"
    if _MENTION_RE.search(text):
        return "mention"
    if _DISCOVERY_RE.search(text):
        return "discovery"
    if _QUOTED_RE.search(text) or _FILENAME_RE.search(text) or _NUMBER_RE.search(text) \
            or _ACRONYM_RE.search(text):
        return "exact"
    return "default"


def route(mode: str, q: str, policy: RetrievalPolicy) -> tuple[list[str], str | None]:
    """Resolve the channel set to run and the classified shape (None unless mode=auto).

    No policy rule emits ``vector``; auto's vector blend is decided in :func:`run_search` by shape +
    keyword-escalation (and :func:`may_use_vector`), so any stray ``vector`` is dropped from an
    auto-derived set here.
    """
    if mode == "auto":
        shape = classify_shape(q)
        modes = [m for m in policy.modes_for_shape(shape) if m != "vector"]
        return modes, shape
    return [mode], None


def may_use_vector(mode: str, q: str, policy: RetrievalPolicy) -> bool:
    """Whether the vector channel could possibly run for this request — so the endpoint can skip the
    vector-capability / index-status check entirely for shapes that never use vector (graph-only auto
    queries). ``auto`` can use vector only when the routed set has a keyword evidence channel
    (conceptual default always, keyword-primary shapes on escalation)."""
    if mode == "vector":
        return True
    if mode != "auto":
        return False
    modes, _ = route(mode, q, policy)
    return "keyword" in modes


# --------------------------------------------------------------------------- status filters


def parse_statuses(raw: str | None, valid: frozenset[str], default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    items = [s.strip() for s in raw.split(",") if s.strip()]
    if not items:
        return default
    bad = sorted(set(items) - valid)
    if bad:
        raise ValueError(f"unknown status(es) {bad}; allowed: {sorted(valid)}")
    return tuple(items)


def _status_allowed(status: str | None, allowed: tuple[str, ...]) -> bool:
    # Unknown status (missing Source page / nav row) is excluded by default (ADR-0032; Q3): a missing
    # status means index/source drift, exactly when not to surface content. Repair the index/page to
    # restore it. A caller may still ask for a specific status set explicitly.
    return status in allowed


# --------------------------------------------------------------------------- channel: evidence


def _source_status_map(conn: sqlite3.Connection, source_ids: list[str]) -> dict[str, str]:
    if not source_ids:
        return {}
    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"SELECT node_id, status FROM navigation WHERE page_type = 'source' "
        f"AND node_id IN ({placeholders})",
        source_ids,
    ).fetchall()
    return {r["node_id"]: r["status"] for r in rows}


def search_evidence(
    conn: sqlite3.Connection,
    match: str,
    *,
    source_id: str | None,
    source_statuses: tuple[str, ...],
    prefusion_limit: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Keyword (BM25) evidence over the Phase 4a chunk index, retention-filtered by source status."""
    sql = (
        "SELECT source_id, chunk_id, ordinal, kind, heading_path, section, char_start, char_end, "
        "page, page_end, table_reference, sheet_reference, bm25(evidence) AS score, "
        f"snippet(evidence, {_EVIDENCE_TEXT_COL}, '', '', '…', 16) AS snippet "
        "FROM evidence WHERE evidence MATCH ?"
    )
    params: list[Any] = [match]
    if source_id:
        sql += " AND source_id = ?"
        params.append(source_id)
    sql += " ORDER BY score, source_id, ordinal LIMIT ?"
    params.append(prefusion_limit)
    rows = conn.execute(sql, params).fetchall()

    status_map = _source_status_map(conn, sorted({r["source_id"] for r in rows}))
    hits: list[dict[str, Any]] = []
    for r in rows:
        sstatus = status_map.get(r["source_id"])
        if not _status_allowed(sstatus, source_statuses):
            continue
        hits.append({
            "source_id": r["source_id"],
            "chunk_id": r["chunk_id"],
            "ordinal": r["ordinal"],
            "kind": r["kind"],
            "section": r["section"],
            "heading_path": json.loads(r["heading_path"]) if r["heading_path"] else [],
            "char_start": r["char_start"],
            "char_end": r["char_end"],
            "page": r["page"],
            "page_end": r["page_end"],
            "table_reference": r["table_reference"],
            "sheet_reference": r["sheet_reference"],
            "source_status": sstatus,
            "snippet": r["snippet"],
            "score": r["score"],
            "retrieval_path": ["keyword"],
        })
        if len(hits) >= limit:
            break
    return hits


# --------------------------------------------------------------------------- channel: navigation


def search_navigation(
    conn: sqlite3.Connection,
    match: str,
    *,
    page_type: str | None,
    node_statuses: tuple[str, ...],
    language: str | None,
    item_types: frozenset[str] | None = None,
    prefusion_limit: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Status-aware page discovery over the Phase 4a navigation index.

    ``item_types`` (ADR-0062) is a type predicate, not a layer filter: it narrows rows that *have*
    an item_type (Item pages) to the requested set, while non-item pages (item_type stored as '')
    pass through untouched.
    """
    sql = (
        "SELECT path, page_type, node_id, item_type, status, review_status, language, "
        "answer_eligible, title, summary, bm25(navigation) AS score FROM navigation "
        "WHERE navigation MATCH ?"
    )
    params: list[Any] = [match]
    if page_type:
        sql += " AND page_type = ?"
        params.append(page_type)
    if language:
        sql += " AND language = ?"
        params.append(language)
    if item_types:
        placeholders = ",".join("?" for _ in item_types)
        sql += f" AND (item_type = '' OR item_type IN ({placeholders}))"
        params.extend(sorted(item_types))
    sql += " ORDER BY score, path LIMIT ?"
    params.append(prefusion_limit)
    rows = conn.execute(sql, params).fetchall()

    hits: list[dict[str, Any]] = []
    for r in rows:
        if not _status_allowed(r["status"], node_statuses):
            continue
        hits.append({
            "path": r["path"],
            "page_type": r["page_type"],
            "node_id": r["node_id"] or None,
            "item_type": r["item_type"] or None,
            "title": r["title"],
            "summary": r["summary"],
            "status": r["status"],
            "review_status": r["review_status"],
            "language": r["language"],
            "answer_eligible": r["answer_eligible"] == "1",
            "score": r["score"],
        })
        if len(hits) >= limit:
            break
    return hits


# --------------------------------------------------------------------------- channel: graph


def _vector_snippet(text: str, *, limit: int = 240) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _vector_hit(row: dict[str, Any], source_status: str | None) -> dict[str, Any]:
    """Map a LanceDB row (full citation fields + `_distance`) to the keyword-identical evidence hit."""
    return {
        "source_id": row["source_id"],
        "chunk_id": row.get("chunk_id"),
        "ordinal": row.get("ordinal"),
        "kind": row.get("kind", ""),
        "section": row.get("section"),
        "heading_path": json.loads(row["heading_path"]) if row.get("heading_path") else [],
        "char_start": row["char_start"],
        "char_end": row["char_end"],
        "page": row.get("page"),
        "page_end": row.get("page_end"),
        "table_reference": row.get("table_reference"),
        "sheet_reference": row.get("sheet_reference"),
        "source_status": source_status,
        "snippet": _vector_snippet(row.get("text", "")),
        "score": float(row["_distance"]),
        "retrieval_path": ["vector"],
    }


def search_vector(
    vector_search: VectorSearchFn,
    keyword_conn: sqlite3.Connection | None,
    *,
    source_id: str | None,
    source_statuses: tuple[str, ...],
    prefusion_limit: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Vector evidence: ANN rows → keyword-identical hits, retention-filtered (one fusion channel).

    Honors the same ``source_id`` filter and retention window as keyword evidence (ADR-0033): a hit
    whose source is the wrong source, or archived/deleted/unknown, is excluded unless asked for.
    Deterministic order: distance ascending, tie-break ``(source_id, ordinal, chunk_id)``.
    """
    rows = vector_search(limit=prefusion_limit)
    status_map = (
        _source_status_map(keyword_conn, sorted({r["source_id"] for r in rows}))
        if keyword_conn is not None else {}
    )
    hits: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: (r["_distance"], r["source_id"], r.get("ordinal") or 0,
                                           r.get("chunk_id") or "")):
        if source_id and row["source_id"] != source_id:  # defensive: honor the source_id filter
            continue
        sstatus = status_map.get(row["source_id"])
        if not _status_allowed(sstatus, source_statuses):
            continue
        hits.append(_vector_hit(row, sstatus))
        if len(hits) >= limit:
            break
    return hits


# Channel precedence for fusion: citation fields (snippet etc.) come from the first channel to find a
# chunk in this order (keyword's match-centered snippet wins over vector's), and retrieval_path is
# emitted in this order.
_CHANNEL_ORDER = ("keyword", "vector")


def fuse_evidence(
    channel_hits: dict[str, list[dict[str, Any]]], *, k: int, limit: int
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion over the chunk-evidence channels (ADR-0032 addendum 7).

    Each chunk scores ``Σ 1/(k + rank_c)`` over the channels that returned it; deduped by the citation
    key ``(source_id, char_start, char_end)``. A fused hit's top-level ``score`` is the RRF value,
    ``retrieval_path`` lists the contributing channels, and ``channels`` carries each channel's 1-based
    rank + native score. Single-channel input fuses too (one entry, order-preserving). Deterministic:
    RRF descending, tie-break ``(source_id, ordinal, char_start, char_end)``.

    Each channel contributes **at most once** per citation key (best/first rank wins) — a same-channel
    duplicate (only possible from a corrupt index; the index validators flag that) is ignored rather
    than double-counted. ``k`` is clamped to ``>= 1`` so a malformed ``rrf_k`` can never divide by zero.
    """
    k = max(1, int(k))
    fused: dict[tuple, dict[str, Any]] = {}
    for channel in _CHANNEL_ORDER:
        for rank, hit in enumerate(channel_hits.get(channel) or [], start=1):
            key = (hit["source_id"], hit["char_start"], hit["char_end"])
            entry = fused.get(key)
            if entry is None:
                entry = {kk: vv for kk, vv in hit.items() if kk not in ("score", "retrieval_path")}
                entry["channels"] = {}
                entry["_rrf"] = 0.0
                fused[key] = entry
            if channel in entry["channels"]:
                continue  # count each channel once (best rank, already recorded)
            entry["channels"][channel] = {"rank": rank, "score": float(hit["score"])}
            entry["_rrf"] += 1.0 / (k + rank)

    # Deterministic: RRF desc, then (source_id, ordinal, char_start, char_end) — the last completes
    # the citation key so order is total even across equal-RRF same-source chunks.
    ordered = sorted(
        fused.values(),
        key=lambda e: (-e["_rrf"], e["source_id"], e.get("ordinal") or 0, e["char_start"], e["char_end"]),
    )
    out: list[dict[str, Any]] = []
    for entry in ordered[:limit]:
        entry["score"] = entry.pop("_rrf")
        entry["retrieval_path"] = [c for c in _CHANNEL_ORDER if c in entry["channels"]]
        out.append(entry)
    return out


def apply_item_type_boost(
    pool: list[dict[str, Any]],
    *,
    source_types: dict[str, frozenset[str]],
    requested: frozenset[str],
    boost: float,
) -> list[dict[str, Any]]:
    """ADR-0062 advisory evidence boost: add ``boost`` to the RRF ``score`` of on-type chunks (their
    source bridges to a requested item_type), then re-sort. In place; returns the same list.

    Advisory, never a filter: the boost is bounded (weaker than primary relevance), so it breaks
    ties and nudges a few positions but a much-more-relevant off-type chunk still outranks a weak
    on-type one. Deterministic tie-break matches :func:`fuse_evidence`."""
    for e in pool:
        if requested & source_types.get(e["source_id"], frozenset()):
            e["score"] += boost
            e["item_type_boosted"] = True  # debug metadata: this chunk got the advisory boost
    pool.sort(key=lambda e: (-e["score"], e["source_id"], e.get("ordinal") or 0,
                             e["char_start"], e["char_end"]))
    return pool


def _empty_graph(depth: int) -> dict[str, Any]:
    return {"seeds": [], "nodes": [], "edges": [], "depth": depth, "truncated": False}


def graph_group(
    conn: sqlite3.Connection,
    nav_hits: list[dict[str, Any]],
    *,
    shape: str | None,
    depth: int,
    edge_statuses: tuple[str, ...],
    node_statuses: tuple[str, ...],
    node_types: frozenset[str] | None,
    item_types: frozenset[str] | None = None,
    node_cap: int,
    edge_cap: int,
) -> dict[str, Any]:
    """Flat graph subgraph for /search: multi-seed BFS at ``depth`` over the Phase 4b projection.

    Seeds are the navigation hits that resolve to graph node pages (ranked by their BM25). For the
    ``disagreement`` shape the traversal is graph-native — it falls back to the endpoints of active
    ``contradicts`` edges when the query carries no topic, and restricts edges to ``contradicts`` —
    so "which sources disagree" surfaces contradictions without relying on trigger-word text matches.
    Retention (``node_statuses``) is applied to every node, so archived/deleted adjacents do not leak.
    """
    topic_seeds = [h["node_id"] for h in nav_hits if h["page_type"] in GRAPH_SEED_TYPES and h["node_id"]]
    edge_types: tuple[str, ...] | None = None
    if shape == "disagreement":
        edge_types = ("contradicts",)
        seeds = topic_seeds or graph_read.active_contradiction_endpoints(conn)
    else:
        seeds = topic_seeds
    if not seeds:
        return _empty_graph(depth)
    return graph_read.search_subgraph(
        conn, seeds, depth=depth, edge_statuses=edge_statuses, node_statuses=node_statuses,
        node_types=node_types, item_types=item_types, edge_types=edge_types,
        node_cap=node_cap, edge_cap=edge_cap,
    )


# --------------------------------------------------------------------------- orchestrator


def run_search(
    *,
    q: str,
    mode: str,
    keyword_conn: sqlite3.Connection | None,
    graph_conn: sqlite3.Connection | None,
    policy: RetrievalPolicy,
    source_id: str | None = None,
    page_type: str | None = None,
    node_type: str | None = None,
    item_types: frozenset[str] | None = None,
    language: str | None = None,
    source_statuses: tuple[str, ...] = RETENTION_DEFAULT_STATUSES,
    node_statuses: tuple[str, ...] = RETENTION_DEFAULT_STATUSES,
    edge_statuses: tuple[str, ...] = graph_read.DEFAULT_EDGE_STATUSES,
    evidence_limit: int | None = None,
    navigation_limit: int | None = None,
    graph_limit: int | None = None,
    vector_search: VectorSearchFn | None = None,
    vector_unavailable_reason: str | None = None,
) -> dict[str, Any]:
    """Run the routed channels and assemble the grouped ``/search`` response."""
    modes, shape = route(mode, q, policy)
    prefusion = policy.cap("per_channel_prefusion_limit")
    ev_limit = evidence_limit or policy.cap("max_evidence_hits")
    nav_limit = navigation_limit or policy.cap("max_navigation_hits")
    node_cap = graph_limit or policy.cap("max_graph_nodes")
    edge_cap = policy.cap("max_graph_edges")
    depth = policy.cap("max_graph_depth_default")
    node_types = frozenset({node_type}) if node_type else None

    terms = extract_terms(q, max_chars=policy.cap("max_query_chars"),
                          max_terms=policy.cap("max_query_terms"))
    evidence_match = build_fts_match(terms, op="AND")   # exact-lookup precision
    nav_match = build_fts_match(terms, op="OR")          # item recall for seeding

    navigation: list[dict[str, Any]] = []
    graph_result = _empty_graph(min(depth, graph_read.MAX_DEPTH))
    notes: list[str] = []

    # Per-channel chunk-evidence hit lists, fused at the end via RRF (one channel fuses too).
    channel_hits: dict[str, list[dict[str, Any]]] = {}
    nav_hits: list[dict[str, Any]] = []
    if keyword_conn is not None:
        if "keyword" in modes and evidence_match is not None:
            channel_hits["keyword"] = search_evidence(
                keyword_conn, evidence_match, source_id=source_id, source_statuses=source_statuses,
                prefusion_limit=prefusion, limit=prefusion,
            )
        if ("navigation" in modes or "graph" in modes) and nav_match is not None:
            nav_hits = search_navigation(
                keyword_conn, nav_match, page_type=page_type, node_statuses=node_statuses,
                language=language, item_types=item_types, prefusion_limit=prefusion, limit=nav_limit,
            )
            if "navigation" in modes:
                navigation = nav_hits

    # The graph channel runs even without an FTS topic (disagreement is graph-native).
    if "graph" in modes and graph_conn is not None:
        graph_result = graph_group(
            graph_conn, nav_hits, shape=shape, depth=depth, edge_statuses=edge_statuses,
            node_statuses=node_statuses, node_types=node_types, item_types=item_types,
            node_cap=node_cap, edge_cap=edge_cap,
        )

    # Vector-channel decision (ADR-0032 addenda 5–6). Explicit mode=vector always runs vector. In
    # auto, the conceptual `default` shape always blends vector, and the keyword-primary shapes
    # (exact/mention) escalate to vector only when keyword evidence is sparse
    # (< escalation_primary_below_k); graph-only shapes (no keyword evidence channel) defer vector.
    # vector_search embeds the query lazily, so the cost is paid only here, when vector actually runs.
    keyword_count = len(channel_hits.get("keyword", []))
    want_vector = "vector" in modes or (
        mode == "auto" and "keyword" in modes
        and (shape == "default" or keyword_count < policy.cap("escalation_primary_below_k"))
    )
    if want_vector and vector_search is not None:
        try:
            channel_hits["vector"] = search_vector(
                vector_search, keyword_conn, source_id=source_id, source_statuses=source_statuses,
                prefusion_limit=prefusion, limit=prefusion,
            )
        except VectorUnavailable as exc:  # backend failed: explicit -> 503; auto -> degrade
            if mode != "auto":
                raise VectorChannelError(str(exc)) from exc
            notes.append(f"vector channel unavailable — degraded to keyword-only: {exc}")
        else:
            if "vector" not in modes:
                modes = [*modes, "vector"]
    elif want_vector and mode == "auto" and vector_unavailable_reason is not None:
        # Auto wanted vector but it is unavailable — note only genuine degradations (a keyword-only
        # deployment passes reason=None and degrades silently).
        notes.append(f"vector channel unavailable — degraded to keyword-only: {vector_unavailable_reason}")

    # RRF-fuse the chunk-evidence channels into one ranked evidence[] (ADR-0032 addendum 7).
    # ADR-0062 evidence faceting: an item_type facet applies a bounded, ADVISORY boost — never a
    # filter. Fuse the full candidate pool, add item_type_boost to on-type chunks (bridged via
    # active items + active mentions), re-sort, THEN cap. The boost is weaker than primary
    # relevance, so it breaks ties / nudges a few positions but cannot exclude off-type evidence.
    boost = policy.weight("item_type_boost")
    boost_ran = bool(item_types) and boost > 0 and graph_conn is not None and bool(channel_hits)
    if boost_ran:
        pool = fuse_evidence(channel_hits, k=policy.cap("rrf_k"), limit=max(ev_limit, prefusion))
        bridge = graph_read.source_item_types(graph_conn, {e["source_id"] for e in pool})
        apply_item_type_boost(pool, source_types=bridge, requested=item_types, boost=boost)
        evidence = pool[:ev_limit]
    else:
        evidence = fuse_evidence(channel_hits, k=policy.cap("rrf_k"), limit=ev_limit)

    if item_types:
        # ADR-0062 review round 1 (NB1): the boost clause must reflect what actually happened —
        # a disabled (item_type_boost=0) or graph-unavailable boost must not claim it applied.
        if boost_ran:
            boost_clause = "evidence received an advisory boost only"
        elif boost <= 0:
            boost_clause = "evidence boost disabled (item_type_boost=0)"
        elif graph_conn is None:
            boost_clause = "evidence boost unavailable (graph index unavailable)"
        else:
            boost_clause = "no evidence to boost"
        prefix = ("item_type facet applied to item page/graph results; non-item results retained; "
                  if ("navigation" in modes or "graph" in modes)
                  else "item_type facet: ")
        notes.append(f"{prefix}{boost_clause}; off-type evidence retained")

    counts = {
        "evidence": len(evidence),
        "navigation": len(navigation),
        "graph": len(graph_result["nodes"]),
    }
    return {
        "query": q,
        "mode": mode,
        "shape": shape,
        "retrieval_path": modes,
        "evidence": evidence,
        "navigation": navigation,
        "graph": graph_result,
        "counts": counts,
        "truncated": graph_result["truncated"],
        "no_results": sum(counts.values()) == 0,
        "notes": notes,
    }
