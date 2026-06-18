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

Scope: keyword/navigation/graph only. **Vector is Phase 4d** (rejected at the endpoint), and **RRF
fusion is Phase 4e** — evidence is keyword-only here, each hit carrying ``retrieval_path:
["keyword"]`` so 4e can extend it.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from app.backend import graph_read
from app.backend.policy import RetrievalPolicy

VALID_MODES = frozenset({"keyword", "vector", "graph", "navigation", "auto"})
# Default retention window (ADR-0032 decision 8): deprecated content stays searchable; everything
# else (archived / deleted / candidate / unknown) is excluded unless a caller explicitly asks.
RETENTION_DEFAULT_STATUSES = ("active", "deprecated_candidate")
# Wiki page types whose node_id participates in the graph (so a navigation hit can seed the graph
# group). Source/query/tag pages are not graph seeds.
GRAPH_SEED_TYPES = frozenset(
    {"concept", "entity", "person", "organization", "project", "claim", "synthesis"}
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
    related") must find the *entity* pages for X or Y separately so the graph can connect them; an
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

    Vector is dropped from any auto-derived set (Phase 4d); an explicit ``mode=vector`` is handled
    (rejected) at the endpoint, not here.
    """
    if mode == "auto":
        shape = classify_shape(q)
        modes = [m for m in policy.modes_for_shape(shape) if m != "vector"]
        return modes, shape
    return [mode], None


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
    prefusion_limit: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Status-aware page discovery over the Phase 4a navigation index."""
    sql = (
        "SELECT path, page_type, node_id, status, review_status, language, answer_eligible, "
        "title, summary, bm25(navigation) AS score FROM navigation WHERE navigation MATCH ?"
    )
    params: list[Any] = [match]
    if page_type:
        sql += " AND page_type = ?"
        params.append(page_type)
    if language:
        sql += " AND language = ?"
        params.append(language)
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
        node_types=node_types, edge_types=edge_types, node_cap=node_cap, edge_cap=edge_cap,
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
    language: str | None = None,
    source_statuses: tuple[str, ...] = RETENTION_DEFAULT_STATUSES,
    node_statuses: tuple[str, ...] = RETENTION_DEFAULT_STATUSES,
    edge_statuses: tuple[str, ...] = graph_read.DEFAULT_EDGE_STATUSES,
    evidence_limit: int | None = None,
    navigation_limit: int | None = None,
    graph_limit: int | None = None,
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
    nav_match = build_fts_match(terms, op="OR")          # entity recall for seeding

    evidence: list[dict[str, Any]] = []
    navigation: list[dict[str, Any]] = []
    graph_result = _empty_graph(min(depth, graph_read.MAX_DEPTH))

    # Keyword-backed channels need both an FTS match and the index.
    nav_hits: list[dict[str, Any]] = []
    if keyword_conn is not None:
        if "keyword" in modes and evidence_match is not None:
            evidence = search_evidence(
                keyword_conn, evidence_match, source_id=source_id, source_statuses=source_statuses,
                prefusion_limit=prefusion, limit=ev_limit,
            )
        if ("navigation" in modes or "graph" in modes) and nav_match is not None:
            nav_hits = search_navigation(
                keyword_conn, nav_match, page_type=page_type, node_statuses=node_statuses,
                language=language, prefusion_limit=prefusion, limit=nav_limit,
            )
            if "navigation" in modes:
                navigation = nav_hits

    # The graph channel runs even without an FTS topic (disagreement is graph-native).
    if "graph" in modes and graph_conn is not None:
        graph_result = graph_group(
            graph_conn, nav_hits, shape=shape, depth=depth, edge_statuses=edge_statuses,
            node_statuses=node_statuses, node_types=node_types, node_cap=node_cap, edge_cap=edge_cap,
        )

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
    }
