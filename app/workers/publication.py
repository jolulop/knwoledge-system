#!/usr/bin/env python3
"""The one publication function (ADR-0068 decision 4).

**Publication** makes the local pages and search reflect committed authority: claim, synthesis, tag
and Source pages, `wiki/index.md` and the keyword index. Every action, Apply, `scripts/republish.py`
and the **Retry publishing** button call :func:`publish`. It runs inside the write lock, after the
commit.

The protocol, with `app/backend/publication_queue.py`:
1. under the write lock, before changing authority, the writer records an intent entry
   (:func:`app.backend.publication_queue.record`), usually provisional (the document and tag it
   acts on);
2. it commits authority;
3. it calls :func:`publish` with the entry id and the exact work its commit produced.

:func:`publish` combines **every** pending entry with the current work and refreshes once, from
current authority, so an older entry can never restore an earlier revision. A provisional entry is
widened first (every Source page that links its tags); a full entry means every page of every kind:
each existing page plus each id the graph and the manifests name, so a page whose node or manifest
is gone is checked (and stays pending), never overlooked.
The steps run once each, in dependency order: claim pages (after copying each page's saved status into
the graph mirror), synthesis pages, tag pages, Source pages, `index.md`, the keyword index.

Publication is projection only: it renders from saved authority and never retracts a relationship,
changes a governed status, or files or reconciles a review (the one exception: a tag moves between
`active` and `unused` per its saved keep/drop decisions, `tag_links.project_page`). A page counts as
published only when it was rendered or is legitimately no longer needed (a deleted tag, a merged
redirect, a document not yet extracted). Anything else stays pending, **with everything downstream of
it, across entries**: a Source page rendered against a stale claim or tag page is not complete, nor
are `index.md` and search while any page is pending. The vector index is outside publication
(eventual; it is refreshed by `reindex_vector`).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.backend import graph, keyword_index, write_lock
from app.backend import publication_queue as queue
from app.backend.manifests import is_source_id, iso_now, manifest_path, valid_manifests

PUBLISHED = "published"
PENDING = "saved_publication_pending"
FAMILIES = ("claims", "syntheses", "items", "sources")      # the render order

_PAGE_PREFIX = {"claims": ("Claims", "clm_"), "syntheses": ("Synthesis", "syn_"),
                "sources": ("Sources", "src_")}
_CANONICAL = {"claims": re.compile(r"clm_[0-9a-f]{16}"), "syntheses": re.compile(r"syn_[0-9a-f]{16}"),
              "items": re.compile(r"itm_[0-9a-f]{16}"), "sources": re.compile(r"src_[0-9a-f]{16}")}


# Why a page stays pending, in the words a screen or script shows (the reason codes are in results).
REASON_HINTS = {
    "page_missing": "the page is missing or unreadable; restore the page from backup (its title and "
                    "description live only on the page, so publishing never recreates it), or rerun "
                    "the extraction that creates it if a run stopped before writing it",
    "node_missing": "the graph has no such tag and nothing records its deletion",
    "deletion_unfinished": "a deletion started and did not finish; run "
                           "`scripts/rebuild_tag_links.py --apply`",
    "identity_operation_in_flight": "a merge or split is unfinished; run Apply to finish it",
    "ledger_corrupt": "a document's tag decisions are unreadable; repair the ledger first",
    "tag_links_repair_needed": "the saved keep/drop decisions and the graph disagree; run "
                               "`scripts/rebuild_tag_links.py --apply`",
    "status_disagrees": "the page and the graph disagree on the tag's status; run "
                        "`scripts/rebuild_tag_links.py --apply` or finish the pending Apply",
    "evidence_gone": "the claim lost its evidence; rerun claim extraction for its documents",
    "page_or_artifact_missing": "the synthesis page or its artifact is missing; restore it from "
                                "backup or regenerate the synthesis",
    "manifest_missing": "the document has no manifest",
    "manifest_unreadable": "the document's manifest is invalid",
    "graph_missing": "there is no graph to render from",
}
_FAMILY_LABELS = {"claims": "claim", "syntheses": "synthesis", "items": "tag", "sources": "Source"}


def describe_failures(result: dict[str, Any]) -> list[str]:
    """One line per page left pending, with what to do about it."""
    lines = []
    for family in FAMILIES:
        for pid, reason in sorted((result.get("failed") or {}).get(family, {}).items()):
            hint = REASON_HINTS.get(reason) or REASON_HINTS.get(reason.split(":")[0]) or reason
            lines.append(f"{_FAMILY_LABELS[family]} page {pid}: {hint}")
    return lines


def _page_ids(wiki_dir: Path, family: str) -> set[str]:
    """The canonical ids of a family's existing pages (a stray file name is never queued)."""
    if family == "items":                 # item pages are named by slug: the id is in frontmatter
        from app.workers.wiki_render import NODE_DIR, parse_frontmatter
        d, ids = Path(wiki_dir) / NODE_DIR["item"], set()
        for page in sorted(d.glob("*.md")) if d.exists() else []:
            try:
                ids.add(str(parse_frontmatter(page.read_text(encoding="utf-8")).get("item_id")))
            except (OSError, UnicodeDecodeError):
                continue
    else:
        sub, prefix = _PAGE_PREFIX[family]
        d = Path(wiki_dir) / sub
        ids = {p.stem for p in d.glob(f"{prefix}*.md")} if d.exists() else set()
    return {i for i in ids if _CANONICAL[family].fullmatch(i)}


def _rebuild_index_status(root: Path) -> str:
    """Rebuild `wiki/index.md`: "rebuilt" | "failed" | "missing" (no script: a degraded or test
    vault, not a failure)."""
    script = Path(root) / "scripts" / "rebuild_index.py"
    if not script.exists():
        return "missing"
    return "rebuilt" if subprocess.run(
        [sys.executable, str(script), str(root)]).returncode == 0 else "failed"


def _open_graph(root: Path) -> tuple[Any, bool]:
    """`(connection, unavailable)`: no graph file is `(None, False)` (a vault with no tags or claims
    yet); a graph that cannot be opened at this schema is `(None, True)`."""
    path = Path(root) / "db" / "graph.sqlite"
    if not path.exists():
        return None, False
    try:
        conn = graph.connect(path)
        if graph.schema_version(conn) == graph.SCHEMA_VERSION:
            return conn, False
        conn.close()
    except Exception:  # noqa: BLE001 - an unopenable graph: nothing graph-backed can publish
        pass
    return None, True


def _documents(root: Path) -> tuple[set[str], set[str]]:
    """`(extracted documents, documents with a valid manifest)`."""
    from app.workers import wiki
    manifests, _skipped = valid_manifests(Path(root) / "raw" / "manifests")
    return ({m["source_id"] for m in manifests if m.get("ingestion_status") in wiki._GENERATED_STATUSES},
            {m["source_id"] for m in manifests})


def _scope(entry: dict[str, Any], gconn, wiki_dir: Path, extracted: set[str]) -> dict[str, set[str]]:
    """The pages one entry requires, widened from current authority when it is provisional or full."""
    scope = {f: set(entry[f]) for f in FAMILIES}
    if entry["full"]:                     # every page that exists, and everything authority names
        for family in FAMILIES:
            scope[family] |= _page_ids(wiki_dir, family)
        scope["sources"] |= extracted
        if gconn is not None:
            for family, node_type in (("claims", "claim"), ("syntheses", "synthesis"),
                                      ("items", "item")):
                scope[family] |= {n["node_id"] for n in graph.nodes_of_type(gconn, node_type)}
    if entry["provisional"] and gconn is not None:
        for sid in entry["sources"]:              # the tags and claims its documents name
            scope["items"] |= {r["dst_id"] for r in gconn.execute(
                "SELECT DISTINCT dst_id FROM edges WHERE src_id = ? AND edge_type = 'mentions' "
                "AND status = 'active'", (sid,))}
            scope["claims"] |= set(graph.claims_for_source(gconn, sid))
        for nid in scope["items"]:
            scope["sources"] |= set(graph.sources_for_node(gconn, nid))
    return scope


def _dependents(gconn, failed: dict[str, dict[str, str]]) -> dict[str, dict[str, set[str]]]:
    """For each failed claim/tag page, the pages rendered from it: `{family: {failed_id: {ids}}}`.
    A claim feeds its contradiction partners, the syntheses citing it and its Source pages; a tag page
    feeds the Source pages that list it (their tag titles)."""
    out: dict[str, dict[str, set[str]]] = {f: {} for f in FAMILIES}
    if gconn is None:
        return out
    for cid in failed["claims"]:
        out["claims"][cid] = set(graph.active_contradictions_for_claim(gconn, cid))
        out["syntheses"][cid] = {
            e["src_id"] for e in graph.incoming_active(gconn, cid)
            if e["edge_type"] == "derived_from"
            and (graph.get_node(gconn, e["src_id"]) or {}).get("node_type") == "synthesis"}
        out["sources"][cid] = {e["dst_id"] for e in graph.outgoing_active(gconn, cid)
                               if e["edge_type"] == "derived_from"}
    for nid in failed["items"]:
        out["sources"].setdefault(nid, set()).update(r["src_id"] for r in gconn.execute(
            "SELECT DISTINCT src_id FROM edges WHERE dst_id = ? AND edge_type = 'mentions'", (nid,)))
    return out


def _render_claims(gconn, cids: list[str], *, wiki_dir: Path, root: Path, now: str,
                   failed: dict[str, str]) -> None:
    from app.workers import claims
    claims_dir = Path(wiki_dir) / "Claims"
    for cid in cids:                          # the saved visibility first: every renderer reads it
        claims.mirror_page_status(gconn, cid=cid, claims_dir=claims_dir, now=now)
    gconn.commit()
    for cid in cids:
        try:
            if not (claims_dir / f"{cid}.md").exists():
                outcome = "page_missing"
            else:
                outcome = claims.recompose_claim(
                    gconn, cid=cid, claims_dir=claims_dir, reviews_dir=root / "reviews", now=now,
                    markdown_dir=root / "normalized" / "markdown", project_only=True)
            gconn.commit()
        except Exception as exc:  # noqa: BLE001 - outstanding, reported
            gconn.rollback()
            outcome = f"render_failed:{type(exc).__name__}"
        if outcome not in ("written", "tombstoned"):
            failed[cid] = outcome


def _render_syntheses(gconn, sids: list[str], *, wiki_dir: Path, root: Path, now: str,
                      failed: dict[str, str]) -> None:
    from app.workers import synthesis
    for sid in sids:
        try:
            outcome = synthesis.rerender_synthesis_page(
                gconn, sid, synthesis_dir=Path(wiki_dir) / "Synthesis",
                enrichment_dir=root / "normalized" / "enrichment", now=now)
            gconn.commit()
        except Exception as exc:  # noqa: BLE001 - outstanding, reported
            gconn.rollback()
            failed[sid] = f"render_failed:{type(exc).__name__}"
            continue
        if outcome is None:
            failed[sid] = "page_or_artifact_missing"


def _render_items(gconn, nids: list[str], *, wiki_dir: Path, root: Path, now: str,
                  failed: dict[str, str]) -> None:
    from app.workers import tag_links
    keeps, damaged = tag_links.ledger_keeps(root / "reviews")
    for nid in nids:
        try:
            outcome = tag_links.project_page(gconn, node_id=nid, wiki_dir=wiki_dir,
                                             reviews_dir=root / "reviews", keeps=keeps,
                                             damaged=damaged, now=now)
            gconn.commit()
        except Exception as exc:  # noqa: BLE001 - outstanding, reported
            gconn.rollback()
            outcome = f"render_failed:{type(exc).__name__}"
        if outcome not in tag_links.PROJECTED:
            failed[nid] = outcome


def _render_sources(root: Path, sids: list[str], *, documents: tuple[set[str], set[str]],
                    wiki_dir: Path, templates_dir: Path | None, failed: dict[str, str]) -> list[str]:
    """Render the extracted Source pages and return the rendered ids. A document with a valid manifest
    that is not extracted yet has no page to publish (its extraction publishes it); a missing or
    invalid manifest is never taken as a removal (the system never deletes manifests)."""
    from app.workers import wiki
    extracted, valid = documents
    for sid in sids:
        if sid not in valid:
            failed[sid] = ("manifest_unreadable" if is_source_id(sid) and manifest_path(
                root / "raw" / "manifests", sid).exists() else "manifest_missing")
    render = [s for s in sids if s in extracted]
    if not render:
        return []
    try:
        gen = wiki.generate_wiki(root, source_ids=render, wiki_dir=wiki_dir, rebuild_index=False,
                                 record_job=False, templates_dir=templates_dir)
        errors = {str(e.get("source_id")) for e in gen.get("error_details") or []}
    except Exception as exc:  # noqa: BLE001 - every source stays outstanding
        errors = set(render)
        for sid in render:
            failed[sid] = f"render_failed:{type(exc).__name__}"
    for sid in errors & set(render):
        failed.setdefault(sid, "render_failed")
    return [s for s in render if s not in errors]


def publish(root: Path, *, intent: str | None = None, sources: Any = (), items: Any = (),
            claims: Any = (), syntheses: Any = (), index: bool = False, keyword: bool = False,
            full: bool = False, provisional: bool = False, own_only: bool = False,
            origin: str = "action", wiki_dir: Path | None = None,
            templates_dir: Path | None = None, now: str | None = None) -> dict[str, Any]:
    """Publish the pending entries plus the current work; return the outcome.

    `intent` is the entry this writer recorded before its commit: its scope is replaced by the exact
    work given here (`claims`, `syntheses`, `items`, `sources`, `index`, `keyword`, `full`;
    `provisional` widens it from authority like a recorded intent). Without one, the work is
    published directly, and queued only if a step fails. `own_only` publishes this writer's entry
    alone and leaves every other entry as it is: a bulk job's per-document step (:class:`Run`), whose
    end refresh then publishes everything once. The result:
    `{"status": "published"|"saved_publication_pending", "pending": <outstanding>|None, "warnings",
    "failed": {family: {id: reason}}, "index_rebuilt", "keyword_reindexed", "sources_rendered",
    "sources_failed", "items_failed"}`. Warnings keep the names screens already show
    (`source_page_refresh_failed:<id>`, `index_rebuild_failed`, `keyword_reindex_failed:<error>`), plus
    `claim_page_refresh_failed:<id>`, `synthesis_page_refresh_failed:<id>`,
    `item_page_refresh_failed:<id>`, `graph_unavailable` and `publication_pending`."""
    root = Path(root)
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    now = now or iso_now()
    with write_lock.hold(root / "reviews"):
        entries = queue.load(root)
        others: list[dict[str, Any]] = []
        if own_only:
            others = [e for e in entries if e["id"] != intent]
            entries = [e for e in entries if e["id"] == intent]
        work = queue.make_entry(origin=origin, sources=sources, items=items, claims=claims,
                                syntheses=syntheses, index=index, keyword=keyword, full=full,
                                provisional=provisional, now=now)
        for e in entries:
            if intent is not None and e["id"] == intent:
                e.update({k: work[k] for k in (*FAMILIES, "index", "keyword", "full",
                                               "provisional")})
                break
        else:
            if any(work[f] for f in FAMILIES) or work["index"] or work["keyword"] or work["full"]:
                entries.append(work)                      # direct work, or an intent already cleared

        out: dict[str, Any] = {"status": PUBLISHED, "pending": None, "warnings": [],
                               "failed": {f: {} for f in FAMILIES},
                               "index_rebuilt": False, "keyword_reindexed": False,
                               "sources_rendered": [], "sources_failed": [], "items_failed": []}
        if not entries:
            return out

        gconn, unavailable = _open_graph(root)
        if unavailable:                                   # nothing can be checked: all stays queued
            out["warnings"] += ["graph_unavailable", "publication_pending"]
            remaining = [{**e, "id": e["id"] or queue.new_entry_id(),
                          "attempts": e["attempts"] + 1} for e in entries]
            queue.save(root, others + remaining)
            out.update(status=PENDING, pending=queue.outstanding(remaining))
            return out

        failed = out["failed"]
        documents = _documents(root)
        try:
            scopes = [_scope(e, gconn, wiki_dir, documents[0]) for e in entries]
            todo = {f: sorted(set().union(*(s[f] for s in scopes))) for f in FAMILIES}
            renderers = (("claims", _render_claims), ("syntheses", _render_syntheses),
                         ("items", _render_items))
            for family, render in renderers:
                if not todo[family]:
                    continue
                if gconn is None:                         # no graph: these pages cannot render
                    failed[family].update(dict.fromkeys(todo[family], "graph_missing"))
                else:
                    render(gconn, todo[family], wiki_dir=wiki_dir, root=root, now=now,
                           failed=failed[family])
            out["sources_rendered"] = _render_sources(root, todo["sources"], documents=documents,
                                                      wiki_dir=wiki_dir, templates_dir=templates_dir,
                                                      failed=failed["sources"])
            deps = _dependents(gconn, failed)
        finally:
            if gconn is not None:
                gconn.close()

        index_failed = keyword_failed = False
        if any(e["index"] or e["full"] for e in entries):
            index_status = _rebuild_index_status(root)
            out["index_rebuilt"] = index_status == "rebuilt"
            index_failed = index_status == "failed"       # "missing": a degraded env, not a failure
        if any(e["keyword"] or e["full"] for e in entries):
            try:
                keyword_index.reindex(root)
                out["keyword_reindexed"] = True
            except Exception as exc:  # noqa: BLE001 - outstanding, reported
                keyword_failed = True
                out["warnings"].append(f"keyword_reindex_failed:{type(exc).__name__}")

        # Downstream closure across entries: a page rendered from a failed upstream page is not done.
        held = {f: set(failed[f]).union(*deps[f].values()) for f in FAMILIES}
        remaining = []
        for e, scope in zip(entries, scopes, strict=True):
            left = {f: scope[f] & held[f] for f in FAMILIES}
            owned = (scope["claims"] & failed["claims"].keys()) | (scope["items"] & failed["items"].keys())
            for uid in owned:                             # the failure's owner keeps its dependents
                for f in FAMILIES:
                    left[f] |= deps[f].get(uid, set())
            pages_left = any(left[f] for f in FAMILIES)
            left_index = pages_left or ((e["index"] or e["full"]) and index_failed)
            left_keyword = pages_left or ((e["keyword"] or e["full"]) and keyword_failed)
            if not (pages_left or left_index or left_keyword):
                continue
            remaining.append({**e, "id": e["id"] or queue.new_entry_id(),
                              **{f: sorted(left[f]) for f in FAMILIES},
                              "index": left_index, "keyword": left_keyword,
                              "full": False, "provisional": False, "attempts": e["attempts"] + 1})
        queue.save(root, others + remaining)

        for family, label in (("claims", "claim"), ("syntheses", "synthesis"), ("items", "item")):
            out["warnings"] += [f"{label}_page_refresh_failed:{i}" for i in sorted(failed[family])]
        out["warnings"] += [f"source_page_refresh_failed:{s}" for s in sorted(failed["sources"])]
        if index_failed:
            out["warnings"].append("index_rebuild_failed")
        out["sources_failed"] = sorted(failed["sources"])
        out["items_failed"] = sorted(failed["items"])
        if remaining:
            out["status"] = PENDING
            out["pending"] = queue.outstanding(remaining)
            out["warnings"].append("publication_pending")
        return out


def job_status(status: str, publication: dict[str, Any] | None) -> str:
    """A job's final status: never "succeeded" while its publication is incomplete (decision 5)."""
    if status == "succeeded" and publication and publication.get("status") == PENDING:
        return PENDING
    return status


def summary(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """What a job's result reports about its publication: `{status, pending, still_pending}`."""
    if result is None:
        return None
    return {"status": result["status"], "pending": result["pending"],
            "still_pending": describe_failures(result)}


def report(pub: dict[str, Any] | None) -> list[str]:
    """A job script's lines about its publication (`summary` output; None prints nothing)."""
    if not pub:
        return []
    if pub["status"] == PUBLISHED:
        return ["Publication: published (pages, index.md and search are current)."]
    return (["Publication: saved, publication incomplete. Retry with "
             "`uv run python scripts/republish.py`."]
            + [f"  pending: {line}" for line in pub.get("still_pending") or []])


class Run:
    """One bulk job's publication (ADR-0068 decision 5).

    For each document, under the write lock: :meth:`intent` before its first write, then its commit,
    then :meth:`publish`, which publishes **that document's** pages from its own entry only. The model
    calls for the next document happen outside the lock. :meth:`finish` is the end refresh, once per
    run: everything pending, the tags the run touched (their Source pages), `index.md` and the keyword
    index. The job reports success only after it.

    The run's own entry (origin `run_<producer>`, :func:`app.backend.publication_queue.extend`) is
    queued before the first commit and kept queued before each later one, so a crash at any point
    leaves the end refresh to retry. While the run is going, that entry is what screens show as
    "search refresh pending". `search=False` is a producer called as a step of a caller that owns
    the refresh (Apply, a test): it records and publishes each document's pages but no run entry,
    and :meth:`finish` does nothing."""

    def __init__(self, root: Path, producer: str, *, search: bool = True,
                 wiki_dir: Path | None = None, templates_dir: Path | None = None) -> None:
        self.root = Path(root)
        self.origin = queue.RUN_PREFIX + producer
        self.search = search
        self.wiki_dir, self.templates_dir = wiki_dir, templates_dir
        self.entry_id: str | None = None
        self.items: set[str] = set()                  # tags whose Source pages the end refresh renders
        self.committed = False
        self.failed: dict[str, dict[str, str]] = {f: {} for f in FAMILIES}

    def intent(self, *, sources: Any = (), items: Any = (), claims: Any = (),
               syntheses: Any = (), full: bool = False, refresh: bool = True) -> str | None:
        """Record intent before a commit (under the write lock): the run's end refresh, plus the
        document's own provisional entry when it names pages (`full` when its scope is unknown).
        `refresh=False` is a commit that may change no page: the end refresh is then needed only if
        its :meth:`publish` says so."""
        self.committed = self.committed or refresh
        if self.search:
            self.entry_id = queue.extend(self.root, self.entry_id, origin=self.origin,
                                         items=self.items)
        if not (full or any((sources, items, claims, syntheses))):
            return None
        return queue.record(self.root, origin=self.origin, sources=sources, items=items,
                            claims=claims, syntheses=syntheses, full=full)

    def publish(self, entry_id: str | None, *, sources: Any = (), items: Any = (), claims: Any = (),
                syntheses: Any = (), later_items: Any = (),
                refresh: bool = False) -> dict[str, Any] | None:
        """After the commit, still under the lock: publish the document's own pages (`sources`,
        `items`, `claims`, `syntheses`), and add `later_items` (tags whose other Source pages may
        list them) to the end refresh. `refresh` says the commit changed pages after all."""
        self.committed = self.committed or refresh
        if later_items:
            self.items |= set(later_items)
            if self.search:
                self.entry_id = queue.extend(self.root, self.entry_id, origin=self.origin,
                                             items=self.items)
        if entry_id is None:
            return None
        out = publish(self.root, intent=entry_id, sources=sources, items=items, claims=claims,
                      syntheses=syntheses, own_only=True, origin=self.origin,
                      wiki_dir=self.wiki_dir, templates_dir=self.templates_dir)
        for family in FAMILIES:
            self.failed[family].update(out["failed"][family])
        return out

    def done(self, entry_id: str | None) -> None:
        """Acknowledge a document's entry whose pages the producer itself wrote (generate_wiki: the
        Source page it just rendered), so they are not rendered twice. Under the write lock."""
        if entry_id is not None:
            queue.discard(self.root, entry_id)

    def finish(self) -> dict[str, Any] | None:
        """The end refresh. Returns the publication result, or None for a step."""
        if not self.search:
            return None
        with write_lock.hold(self.root / "reviews"):
            return publish(self.root, intent=self.entry_id, items=self.items,
                           provisional=bool(self.items), index=self.committed,
                           keyword=self.committed, origin=self.origin, wiki_dir=self.wiki_dir,
                           templates_dir=self.templates_dir)


def republish_all(root: Path, *, origin: str = "republish_all", wiki_dir: Path | None = None,
                  now: str | None = None) -> dict[str, Any]:
    """Publish everything from current authority: every claim, synthesis, tag and Source page,
    `index.md` and the keyword index. Saved Query pages are answers, never regenerated."""
    with write_lock.hold(Path(root) / "reviews"):
        rid = queue.record(root, origin=origin, full=True, provisional=False, now=now)
        return publish(root, intent=rid, index=True, keyword=True, full=True, origin=origin,
                       wiki_dir=wiki_dir, now=now)


def repair(root: Path, *, wiki_dir: Path | None = None, now: str | None = None) -> dict[str, Any]:
    """Repair an unreadable queue without ever leaving the vault unguarded:
    1. copy the damaged bytes beside it, flushed (the damaged file stays, so writes stay refused);
    2. atomically and durably replace the queue with one full-recovery entry;
    3. publish it — everything, from current authority.
    A crash after step 1 leaves writes refused (rerun repair); after step 2, a full entry pending.
    On a readable queue this is a full republish."""
    root = Path(root)
    with write_lock.hold(root / "reviews", authority=False):
        try:
            queue.load(root)
        except queue.QueueCorrupt:
            damaged = queue.preserve_damaged(root, now=now)
            rid = queue.replace_with_recovery(root, origin="queue_repair", now=now)
            result = publish(root, intent=rid, index=True, keyword=True, full=True,
                             origin="queue_repair", wiki_dir=wiki_dir, now=now)
            return {**result, "set_aside": damaged.name if damaged else None}
        result = republish_all(root, origin="queue_repair", wiki_dir=wiki_dir, now=now)
    return {**result, "set_aside": None}
