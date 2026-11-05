#!/usr/bin/env python3
"""Promotion lifecycle (ADR-0018, restated by ADR-0066): candidate -> active.

ADR-0066: a knowledge item (tag) becomes active when ONE document link to it is kept — the keep is
the promotion, performed immediately by `tag_links.keep` through :func:`activate_node`. There is
**no recurrence auto-promotion** any more: an AI suggestion never becomes active without a human
keep, however many sources mention it. The ≥2-independent-sources threshold survives only as a
synthesis-eligibility input.

This pass remains the executor for **approved `promote_candidate_node` items** still in the
ledger (the flat queue / pre-ADR-0066 items): it activates each through the same
:func:`activate_node` (approve-with-amendments, ADR-0058; the ADR-0059 sentinel gate). On
promotion the page frontmatter is updated to `active` (status authority, ADR-0030) and the
`nodes.status` index is mirrored. Idempotent: a rerun skips already-active nodes.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from app.backend import db, graph, taxonomy
from app.backend.manifests import iso_now, valid_manifests
from app.workers import apply_outcomes as oc
from app.workers import item_names, item_pages, items, publication, reviews
from app.workers.wiki_render import NODE_DIR, parse_frontmatter, render_item_page

PROMOTABLE = ("item",)
_TITLE_RE = re.compile(r'(?m)^title:\s*"(.*)"\s*$')

AMENDMENT_FIELDS = frozenset({"title", "aliases", "description", "item_type"})
AMEND_MAX_TITLE, AMEND_MAX_ALIAS, AMEND_MAX_ALIASES, AMEND_MAX_DESCRIPTION = 200, 120, 16, 2000


class AmendmentError(ValueError):
    """An amendments payload violates the ADR-0058 shape/limits (the message names the field)."""


def normalize_amendments(amendments: Any) -> dict[str, Any] | None:
    """Normalize + validate an ADR-0058 amendments payload — the ONE check shared by the review
    route, the per-source form, the JSON tag-link route and `tag_links.keep`.

    Allowed fields exactly title/aliases/description/item_type (ADR-0059: item_type must be one of
    the 15 production types — the sentinel is never an amendment target); a blank title is treated
    as not-provided (a title can be corrected, never erased); a blank description explicitly clears
    the page field. Returns the normalized payload, or None when nothing effective was provided.
    Raises :class:`AmendmentError`."""
    if amendments is None:
        return None
    if not isinstance(amendments, dict):
        raise AmendmentError("amendments must be an object")
    unknown = sorted(set(amendments) - AMENDMENT_FIELDS)
    if unknown:
        raise AmendmentError(f"unknown amendment field(s): {unknown}")
    out: dict[str, Any] = {}
    title = amendments.get("title")
    if title is not None:
        if not isinstance(title, str):
            raise AmendmentError("amendment title must be a string")
        title = re.sub(r"\s+", " ", title).strip()[:AMEND_MAX_TITLE]
        if title:
            out["title"] = title
    aliases = amendments.get("aliases")
    if aliases is not None:
        if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
            raise AmendmentError("amendment aliases must be a list of strings")
        out["aliases"] = [a.strip()[:AMEND_MAX_ALIAS] for a in aliases if a.strip()][:AMEND_MAX_ALIASES]
    description = amendments.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise AmendmentError("amendment description must be a string")
        # Canonical single-line prose (review round): whitespace collapsed at the boundary; an
        # explicitly-blank description clears the page field.
        out["description"] = re.sub(r"\s+", " ", description).strip()[:AMEND_MAX_DESCRIPTION]
    item_type = amendments.get("item_type")
    if item_type is not None:
        if not taxonomy.is_production_item_type(item_type):
            raise AmendmentError("amendment item_type must be one of the production taxonomy values")
        out["item_type"] = item_type
    return out or None


def _read_meta(page_path: Path) -> dict[str, Any] | None:
    if not page_path.exists():
        return None
    text = page_path.read_text(encoding="utf-8", errors="replace")
    m = _TITLE_RE.search(text)
    if not m:
        return None
    fm = parse_frontmatter(text)
    aliases = fm.get("aliases")
    return {"title": re.sub(r"\\(.)", r"\1", m.group(1)),
            "aliases": aliases if isinstance(aliases, list) else [],
            # ADR-0059: the page owns the governed classification.
            "item_type": fm.get("item_type"),
            # ADR-0052: preserve a spin-off's split lineage when the promote pass re-renders it active.
            "split_from": fm.get("split_from"), "split_review_id": fm.get("split_review_id"),
            # ADR-0058: preserve the page-owned human description.
            "description": fm.get("description")}


def _read_amendments(reviews_dir: Path, rid: str) -> dict[str, Any] | None:
    """The approve-with-amendments payload stored on the approved item (ADR-0058), if any."""
    path = reviews_dir / "approved" / f"{rid}.json"
    if not path.exists():
        return None
    try:
        return _load_amendments(reviews_dir, rid)
    except (OSError, json.JSONDecodeError):
        return None


def _load_amendments(reviews_dir: Path, rid: str) -> dict[str, Any] | None:
    """:func:`_read_amendments` without its fallback: raises when the approved item can't be read
    (the ADR-0072 verification needs "no amendments" and "unreadable" apart)."""
    item = json.loads((Path(reviews_dir) / "approved" / f"{rid}.json").read_text(encoding="utf-8"))
    amendments = item.get("amendments") if isinstance(item, dict) else None
    return amendments if isinstance(amendments, dict) else None


def _apply_amendments(meta: dict[str, Any], amendments: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve the effective title/aliases/description/item_type (ADR-0058 approve-with-amendments,
    extended with `item_type` by ADR-0059 — retype is a metadata flip under the type-neutral id,
    and it is REQUIRED to clear the unclassified sentinel).

    Node id stays frozen regardless; only display identity moves. The old title is auto-added
    to the alias list when the title changes (page-authoritative aliases, normalized/deduped)."""
    title = meta["title"]
    aliases = list(meta["aliases"])
    description = meta.get("description")
    item_type = meta.get("item_type")
    if amendments:
        new_title = str(amendments.get("title") or "").strip()
        if isinstance(amendments.get("aliases"), list):
            aliases = [str(a).strip() for a in amendments["aliases"] if str(a).strip()]
        if new_title and new_title != title:
            aliases = items._union(aliases, [title])  # old title stays findable
            title = new_title
        if amendments.get("description") is not None:
            description = str(amendments["description"]).strip() or None
        if taxonomy.is_production_item_type(amendments.get("item_type")):
            item_type = amendments["item_type"]
    return {"title": title, "aliases": items._union(aliases, []), "description": description,
            "item_type": item_type}


def _with_page(wiki_dir: Path, sources) -> list[str]:
    """The given sources that have a Source page (only those are re-rendered)."""
    return [s for s in sorted(sources) if (Path(wiki_dir) / "Sources" / f"{s}.md").exists()]


def activate_node(gconn, *, node: dict[str, Any], wiki_dir: Path, reviews_dir: Path,
                  amendments: dict[str, Any] | None = None, now: str | None = None,
                  dry_run: bool = False) -> dict[str, Any]:
    """Flip one candidate item to `active` — the single promotion writer (ADR-0018/0066).

    Applies approve-with-amendments (title/aliases/description/item_type, ADR-0058/0059): the node
    id stays frozen; only the slug (display path) may move, and this function owns the move. The
    ADR-0059 sentinel gate holds: without a real production `item_type` the node never activates.
    Returns `{outcome: "activated"|"skipped", reason?, slug, amended, affected_sources}`; a skip
    writes NOTHING. `dry_run=True` runs every gate and returns `"would_activate"` without writing
    (the keep path's preflight, so a blocked keep never half-writes)."""
    now = now or iso_now()
    nid, node_type = node["node_id"], node["node_type"]
    page_dir = Path(wiki_dir) / NODE_DIR[node_type]
    old_page = page_dir / f"{node['slug']}.md"
    meta = _read_meta(old_page)
    if meta is None:
        return {"outcome": "skipped", "reason": "page_missing"}
    eff = _apply_amendments(meta, amendments)
    if not taxonomy.is_production_item_type(eff.get("item_type")):
        return {"outcome": "skipped", "reason": "missing_required_item_type"}
    # ADR-0071: the recorded path is kept unless the title actually changes; a new title gets the
    # filename rule (frozen id suffix for a lossy name) and the exact-name check (decision 3).
    renamed = eff["title"] != meta["title"]
    slug = items.page_slug(eff["title"], nid) if renamed else node["slug"]
    new_page = page_dir / f"{slug}.md"
    if renamed and (conflict := item_names.exact_conflict(gconn, wiki_dir, eff["title"], nid)):
        return {"outcome": "skipped", "reason": conflict["reason"], "problem": conflict}
    # ADR-0070 B1 review: the current page must be this tag's own, and a moved slug's destination
    # must be free (no file, no other item recording it) or already this tag's — checked before any
    # write, so the keep that calls this (dry run first) never half-writes.
    if (problem := item_pages.page_problem(gconn, wiki_dir, nid, node["slug"])) is not None:
        return {"outcome": "skipped", "reason": problem["reason"], "problem": problem}
    if slug != node["slug"]:
        if new_page.exists() and item_pages.page_state(wiki_dir, slug, nid)[0] != "own":
            # Another page already owns the amended slug — scope-guard skip, no writes.
            return {"outcome": "skipped", "reason": "amended_slug_collision"}
        if (problem := item_pages.page_problem(gconn, wiki_dir, nid, slug)) is not None:
            return {"outcome": "skipped", "reason": "amended_slug_collision", "problem": problem}
    if dry_run:
        return {"outcome": "would_activate", "slug": slug, "amended": bool(amendments),
                "affected_sources": []}
    # ADR-0071 decision 3, level 2: a similar existing tag is reported with the outcome (after every
    # gate, before the writes, so the renamed page itself is never matched).
    advisory = (item_names.similar_advisory(gconn, wiki_dir, eff["title"], nid)
                if renamed else None)
    sources, suggested = graph.tag_projection(gconn, nid, "active")
    duplicates = graph.active_duplicates(gconn, nid)
    new_page.write_text(
        render_item_page({
            "node_id": nid, "item_type": eff["item_type"], "title": eff["title"],
            "aliases": eff["aliases"], "confidence": "low",
            "source_ids": sources, "suggested_ids": suggested, "status": "active",
            "duplicates": duplicates,
            "split_from": meta.get("split_from"),        # ADR-0052: preserve spin-off lineage
            "split_review_id": meta.get("split_review_id"),
            "description": eff["description"],           # ADR-0058: page-owned description
        }, labels=items._link_labels(wiki_dir, sources + suggested, duplicates)),
        encoding="utf-8")
    graph.upsert_node(gconn, node_id=nid, node_type=node_type, slug=slug, status="active",
                      item_type=eff["item_type"], now=now)
    # Every Source page that lists the tag changes on activation (its projection follows the status;
    # suggestion lists show the title too), not only on a slug move: the caller publishes them all.
    affected = list(sources) + list(suggested)
    if slug != node["slug"]:
        item_pages.remove_page(wiki_dir, nid, node["slug"])     # its own page (checked above)
        # Inbound links hold the old slug: the mentioning Source pages are in `affected`; duplicate
        # partners re-render here, AFTER the slug mirror update their ## Duplicates projection reads.
        for dup in graph.active_duplicates(gconn, nid):
            items._recompose_node(gconn, node_id=dup["node_id"], wiki_dir=wiki_dir,
                                  reviews_dir=reviews_dir, now=now)
    return {"outcome": "activated", "slug": slug, "amended": bool(amendments),
            "affected_sources": affected, "advisories": [advisory] if advisory else [],
            # B2a review round 2: what this activation produced, for the approval's executor record
            "title_before": meta["title"],
            "effective": {f: eff[f] for f in ("title", "aliases", "description", "item_type")}}


def promote_candidates(
    root: Path,
    *,
    manifests_dir: Path | None = None,
    graph_db: Path | None = None,
    wiki_dir: Path | None = None,
    reviews_dir: Path | None = None,
    jobs_db: Path | None = None,
    rebuild_index: bool = True,
    record_job: bool = True,
) -> dict[str, Any]:
    """Activate candidates whose `promote_candidate_node` item is approved (see the module docstring).
    ADR-0066 review round 10: it rewrites item pages, so the whole run is serialized with tag actions
    (the ledger lock — re-entrant, so a no-op inside run_apply, which already holds it)."""
    from app.workers import tag_links                    # lazy: tag_links imports this module
    lock_dir = Path(reviews_dir) if reviews_dir else Path(root).resolve() / "reviews"
    with tag_links.ledger_lock(lock_dir):
        return _promote_candidates(root, manifests_dir=manifests_dir, graph_db=graph_db,
                                   wiki_dir=wiki_dir, reviews_dir=reviews_dir, jobs_db=jobs_db,
                                   rebuild_index=rebuild_index, record_job=record_job)


def _promote_candidates(
    root: Path,
    *,
    manifests_dir: Path | None,
    graph_db: Path | None,
    wiki_dir: Path | None,
    reviews_dir: Path | None,
    jobs_db: Path | None,
    rebuild_index: bool,
    record_job: bool,
) -> dict[str, Any]:
    root = Path(root).resolve()
    manifests_dir = Path(manifests_dir) if manifests_dir else root / "raw" / "manifests"
    graph_db = Path(graph_db) if graph_db else root / "db" / "graph.sqlite"
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    reviews_dir = Path(reviews_dir) if reviews_dir else root / "reviews"
    jobs_db = Path(jobs_db) if jobs_db else root / "db" / "jobs.sqlite"

    now = iso_now()
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(jobs_db)
        conn = db.connect(jobs_db)
        db.insert_job(conn, job_id=job_id, job_type="promote", status="running",
                      created_at=now, started_at=now)

    # ADR-0068 decision 5: a standalone run publishes each activation (its tag's Source pages) in
    # its commit and ends with one refresh; inside Apply (`rebuild_index=False`) it is a step, and
    # Apply's own intent and fan-out cover it.
    run = publication.Run(root, "promote", wiki_dir=wiki_dir) if rebuild_index else None
    try:
        _valid, _skipped_invalid = valid_manifests(manifests_dir)
        considered = promoted_review = amended = 0
        skipped: list[dict[str, Any]] = []
        advisories: list[dict[str, Any]] = []
        affected_sources: set[str] = set()
        # ADR-0072: ONE outcome per approved `promote_candidate_node` review — `promoted` = the applied
        # outcomes; every `skipped` entry is a `skipped` outcome, plus the `page_missing` refusals the
        # legacy list never carried and the approvals the candidate loop doesn't visit (see
        # `_unvisited_outcomes`).
        outcomes: list[dict[str, Any]] = []
        handled: set[str] = set()
        if graph_db.exists():
            graph.init_db(graph_db)
            gconn = graph.connect(graph_db)
            try:
                candidates = [
                    dict(r) for r in gconn.execute(
                        "SELECT node_id, node_type, slug FROM nodes WHERE status = 'candidate'")
                ]
                for node in candidates:
                    if node["node_type"] not in PROMOTABLE:
                        continue
                    considered += 1
                    nid = node["node_id"]
                    rid = reviews.review_id("promote_candidate_node", {"node_id": nid})
                    if not (reviews_dir / "approved" / f"{rid}.json").exists():
                        continue  # stays candidate (ADR-0066: only a keep or an approval promotes)
                    # ADR-0058 approve-with-amendments: the human's title/aliases/description ride the
                    # approved item; the executor applies them at flip-to-active.
                    intent = run.intent(items=[nid], refresh=False) if run else None
                    amendments = _read_amendments(reviews_dir, rid)
                    details = _promotion_details(wiki_dir, node, amendments)   # names BEFORE the write
                    handled.add(rid)
                    out = activate_node(gconn, node=node, wiki_dir=wiki_dir, reviews_dir=reviews_dir,
                                        amendments=amendments, now=now)
                    if out["outcome"] == "skipped":
                        if run:
                            run.publish(intent)           # nothing changed: clears the intent
                        if out["reason"] != "page_missing":
                            skipped.append({"review_id": rid, "reason": out["reason"],
                                            **({"problem": out["problem"]}
                                               if out.get("problem") else {})})
                        outcomes.append(oc.outcome(
                            rid, "promote_candidate_node", oc.SKIPPED, reason=out["reason"],
                            subject=details["subject"],
                            **({"problem": out["problem"]} if out.get("problem") else {})))
                        continue
                    amended += bool(out["amended"])
                    advisories += [{"review_id": rid, **a} for a in out.get("advisories") or []]
                    affected_sources.update(out["affected_sources"])
                    if run:                       # every Source page that lists the tag
                        run.publish(intent, sources=_with_page(wiki_dir, out["affected_sources"]),
                                    refresh=True)
                    promoted_review += 1  # human-approved promotion; the ledger loop already closed
                    _write_executor_record(reviews_dir, rid, nid, out, now, amendments=amendments)
                    outcomes.append(oc.outcome(rid, "promote_candidate_node", oc.APPLIED, **details))
                outcomes += _unvisited_outcomes(gconn, reviews_dir, wiki_dir, handled)
            finally:
                gconn.close()
        else:
            outcomes += _unvisited_outcomes(None, reviews_dir, wiki_dir, handled)

        promoted = promoted_review
        # A run published each activation's Source pages above; as Apply's step it renders none and
        # reports them (`affected_sources`, existing pages only) for Apply's one publication.
        pub = run.finish() if run is not None else None  # index.md + keyword, once (decision 5)
        affected_sources = set(_with_page(wiki_dir, affected_sources))
        summary = {
            "job_id": job_id, "candidates_considered": considered, "promoted": promoted,
            "promoted_by_review": promoted_review,
            "amended": amended, "skipped": skipped, "advisories": advisories, "outcomes": outcomes,
            "affected_sources": sorted(affected_sources),
            "manifests_skipped_invalid": len(_skipped_invalid),
            "index_rebuilt": bool(pub and pub["index_rebuilt"]),
            "keyword_reindexed": bool(pub and pub["keyword_reindexed"]),
            "publication": publication.summary(pub), "promoted_at": now,
        }
        status = publication.job_status("succeeded", pub)
        summary["status"] = status
        if conn is not None:
            db.update_job(conn, job_id, status=status, finished_at=iso_now(), metadata=summary)
        return summary
    except BaseException as exc:  # incl. Ctrl-C (UAT part 3 #3)
        if conn is not None:
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(), error_message=db.failure_message(exc))
        raise
    finally:
        if conn is not None:
            conn.close()


def _promotion_details(wiki_dir: Path, node: dict[str, Any],
                       amendments: dict[str, Any] | None) -> dict[str, Any]:
    """ADR-0072 decision 3: the tag's name and its effective title/type, read BEFORE activation
    (read-only; the same resolution `activate_node` applies). `before`/`after.title` only when the
    title amendment renames."""
    subject = oc.item_name(wiki_dir, node, node.get("node_id"))
    page = Path(wiki_dir) / NODE_DIR[node["node_type"]] / f"{node['slug']}.md"
    meta = oc.guarded(_read_meta, page)                  # reporting-only: never raises
    eff = oc.guarded(_apply_amendments, meta, amendments) if isinstance(meta, dict) else None
    if not isinstance(eff, dict):
        return {"subject": subject}
    details: dict[str, Any] = {"subject": subject, "after": {"item_type": eff.get("item_type")}}
    if eff["title"] != meta["title"]:
        details["before"] = {"title": meta["title"]}
        details["after"]["title"] = eff["title"]
    return details


def _approved_promotions(reviews_dir: Path) -> list[dict[str, Any]]:
    d = Path(reviews_dir) / "approved"
    out: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (isinstance(item, dict) and item.get("type") == "promote_candidate_node"
                and item.get("status") == "approved"):
            out.append({**item, "review_id": str(item.get("review_id") or path.stem),
                        "_file_id": path.stem})
    return out


def _unvisited_outcomes(gconn, reviews_dir: Path, wiki_dir: Path,
                        handled: set[str]) -> list[dict[str, Any]]:
    """Read-only pass (ADR-0072 decision 2): an outcome for every approved `promote_candidate_node`
    review the candidate loop didn't act on — it only visits CANDIDATE nodes, so an approval whose
    node is already active (an earlier run, or a keep) or gone is otherwise never reported. Active
    node with an active page carrying the approval's amendments → `already_in_effect` (postcondition
    verified, :func:`_amendments_in_effect`); an amended field the page doesn't carry → `skipped`,
    `postcondition_not_met`; a read that fails → `verification_unavailable`; anything else →
    `skipped` with a typed reason. Every read is guarded; writes nothing."""
    out: list[dict[str, Any]] = []
    for item in _approved_promotions(reviews_dir):
        rid = item["review_id"]
        if rid in handled or item["_file_id"] in handled:   # the loop keys by the file's name
            continue
        handled.add(rid)                                  # one outcome even for a duplicate file
        out.append(_unvisited_outcome(gconn, reviews_dir, wiki_dir, item))
    return out


_RECORD_DECISION = "promoted"


def approval_fingerprint(item: dict[str, Any], amendments: dict[str, Any] | None) -> str:
    """B2a review round 3: the identity of ONE approval — its review id, type, subject, the
    amendments exactly as executed (values and list order preserved) and its `decided_at` — as a
    sha256 of deterministic canonical JSON (sorted keys, fixed separators). A reopened and re-approved
    review with other amendments gets a different fingerprint, even within the same second."""
    import hashlib
    payload = {"review_id": item.get("review_id"), "type": item.get("type"),
               "subject": item.get("subject"), "amendments": amendments,
               "decided_at": item.get("decided_at")}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _approval_item(reviews_dir: Path, rid: str) -> dict[str, Any] | None:
    """The approved review file as a dict, or None when it can't be read."""
    try:
        item = json.loads((Path(reviews_dir) / "approved" / f"{rid}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return item if isinstance(item, dict) else None


def _write_executor_record(reviews_dir: Path, rid: str, nid: str, out: dict[str, Any], now: str,
                           *, amendments: dict[str, Any] | None) -> None:
    """B2a review rounds 2–3: the minimal executor record of an approved promotion — the title it had
    just before this approval executed and the effective state it produced — bound to THAT approval
    by :func:`approval_fingerprint`, computed from the approval file with the amendments exactly as
    executed (`audit_log/<review_id>-promoted-<hex>.json`, the merges/retypes/splits convention,
    written under Apply's lock). A record whose approval file couldn't be read carries no
    fingerprint, so it can never verify anything."""
    item = _approval_item(reviews_dir, rid)
    audit = Path(reviews_dir) / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{rid}-{_RECORD_DECISION}-{uuid.uuid4().hex[:8]}.json").write_text(
        json.dumps({"review_id": rid, "decision": _RECORD_DECISION, "decided_by": "system",
                    "decided_at": now, "node_id": nid,
                    "approval_fingerprint": approval_fingerprint(item, amendments) if item else None,
                    "title_before": out.get("title_before"),
                    "effective": out.get("effective")}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def _complete_effective(eff: Any) -> bool:
    """B2a review round 4: a matching record verifies only with ALL four recorded fields, well typed
    — `title` a string that isn't blank (`strip()`; the recorded value itself is compared
    unchanged), `aliases` a list of strings, `description` a string or None, `item_type` a string.
    Anything less is insufficient evidence (`verification_unavailable`), never completed from the
    page."""
    if not isinstance(eff, dict) or any(f not in eff for f in _EFFECT_FIELDS):
        return False
    return (isinstance(eff["title"], str) and bool(eff["title"].strip())
            and isinstance(eff["aliases"], list) and all(isinstance(a, str) for a in eff["aliases"])
            and (eff["description"] is None or isinstance(eff["description"], str))
            and isinstance(eff["item_type"], str))


def _executor_record(reviews_dir: Path, item: dict[str, Any],
                     amendments: dict[str, Any] | None) -> dict[str, Any] | None:
    """The executor record of THIS approval: the latest record whose `approval_fingerprint` equals
    the approval's (the same approval data execution uses), or None. A readable record with a
    different or missing fingerprint belongs to another approval and is ignored; a record that can't
    be read well enough to check its fingerprint, or a matching one without its effective state,
    raises (the caller reports `verification_unavailable`)."""
    rid = str(item.get("review_id") or "")
    expected = approval_fingerprint(item, amendments)
    found = []
    for path in sorted((Path(reviews_dir) / "audit_log").glob(f"{rid}-{_RECORD_DECISION}-*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rec, dict):
            raise ValueError(f"unreadable executor record {path.name}")
        if rec.get("approval_fingerprint") != expected:
            continue
        if not _complete_effective(rec.get("effective")):
            raise ValueError(f"executor record {path.name} has an incomplete effective state")
        found.append(rec)
    return max(found, key=lambda r: str(r.get("decided_at"))) if found else None


def _unvisited_outcome(gconn, reviews_dir: Path, wiki_dir: Path,
                       item: dict[str, Any]) -> dict[str, Any]:
    """The outcome of ONE approval the candidate loop didn't act on (see :func:`_unvisited_outcomes`)."""
    rid = item["review_id"]
    subj = item.get("subject") if isinstance(item.get("subject"), dict) else {}
    nid = subj.get("node_id")

    def _skip(reason: str, subject: dict[str, str], **extra: Any) -> dict[str, Any]:
        return oc.outcome(rid, "promote_candidate_node", oc.SKIPPED, reason=reason,
                          subject=subject, **extra)

    def _unverified(subject: dict[str, str]) -> dict[str, Any]:
        return oc.unverified(rid, "promote_candidate_node", changed=False, subject=subject)

    if gconn is None:
        return _skip("graph_missing", oc.named(nid, None))
    node = oc.guarded(graph.get_node, gconn, nid) if isinstance(nid, str) and nid else None
    if node is oc.UNAVAILABLE:
        return _unverified(oc.named(nid, None))
    if node is None:
        return _skip("node_missing", oc.named(nid, None))
    if node["node_type"] not in PROMOTABLE:
        return _skip("out_of_scope", oc.named(nid, None))
    subject = oc.item_name(wiki_dir, node, nid)
    if node["status"] == "candidate":
        # a candidate the loop didn't act on: this file isn't the node's canonical approval
        return _skip("review_id_mismatch", subject)
    if node["status"] != "active":
        return _skip("node_not_candidate", subject, status=node["status"])
    page = Path(wiki_dir) / NODE_DIR[node["node_type"]] / f"{node['slug']}.md"
    is_page = oc.guarded(page.is_file)
    if is_page is oc.UNAVAILABLE:
        return _unverified(subject)
    if not is_page:
        return _skip("page_missing", subject)
    page_status = oc.guarded(lambda: parse_frontmatter(
        page.read_text(encoding="utf-8", errors="replace")).get("status"))
    if page_status is oc.UNAVAILABLE:
        return _unverified(subject)
    if page_status != "active":
        return _skip("page_not_active", subject, page_status=page_status)
    check = oc.guarded(_amendments_in_effect, page, reviews_dir, item["_file_id"], item)
    if not isinstance(check, dict):
        return _unverified(subject)
    if check.get("ambiguous"):           # no evidence of the title before this approval executed
        return oc.unverified(rid, "promote_candidate_node", changed=False, subject=subject,
                             problem={"evidence": "no_executor_record",
                                      "fields": check["fields"]})
    if check["fields"]:
        return _skip(oc.POSTCONDITION_NOT_MET, subject, problem={"fields": check["fields"]},
                     before=check["before"], after=check["after"])
    return oc.outcome(rid, "promote_candidate_node", oc.ALREADY_IN_EFFECT, subject=subject,
                      after={"item_type": node.get("item_type")})


_EFFECT_FIELDS = ("title", "aliases", "description", "item_type")


def _amendments_in_effect(page: Path, reviews_dir: Path, file_id: str,
                          item: dict[str, Any]) -> dict[str, Any]:
    """Whether an active tag's page carries its approval's amendments (B2a review round 2). Raises
    when the page, the approved item or an executor record can't be read (the caller's `guarded`
    makes that `verification_unavailable`).

    - **With this approval's executor record** (:func:`_executor_record`): in effect iff the page
      equals the effective state recorded when it executed; the old title it appended is whatever
      that execution produced, never inferred.
    - **Without one:** the payload `activate_node` gets (:func:`_load_amendments`) resolved by the
      same :func:`_apply_amendments`. A fixed point (resolving changes nothing) is in effect. An
      approval that amends no title can't have appended anything, so any difference is not in
      effect; likewise a page whose title, description or type isn't the approved one. The one
      **ambiguous** case: the approved title IS the page's title and the page has exactly the
      approved aliases plus one more — a self-title amendment with an extra alias and an executed
      rename (which appends the previous title) end the same way; it is never verified as in
      effect (no alias is guessed to be the old title).

    Returns `{fields, before, after}` (empty `fields` = in effect), plus `ambiguous: True`."""
    meta = _read_meta(page)
    if meta is None:
        raise ValueError("the page has no title")
    current = _apply_amendments(meta, None)              # the page, normalized the executor's way
    amendments = _load_amendments(reviews_dir, file_id)  # the payload execution uses
    record = _executor_record(reviews_dir, item, amendments)
    # The recorded state is compared as recorded: no field is ever filled in from the page.
    expected = ({f: record["effective"][f] for f in _EFFECT_FIELDS} if record is not None
                else _apply_amendments(meta, amendments))
    fields = [f for f in _EFFECT_FIELDS if expected[f] != current[f]]
    out: dict[str, Any] = {"fields": fields, "before": {f: current[f] for f in fields},
                           "after": {f: expected[f] for f in fields}}
    if record is None and fields == ["aliases"]:
        # Rename history can only ever add ONE alias (the title just before this approval). The only
        # unresolvable case is exactly that: the approved title is the page's, and the page has the
        # approved aliases plus one more — an executed rename and a self-title amendment with an
        # extra alias look the same. Any other difference is certainly not in effect.
        title_amended = bool(str((amendments or {}).get("title") or "").strip())
        extra = [a for a in current["aliases"] if a not in expected["aliases"]]
        if (title_amended and expected["title"] == current["title"] and len(extra) == 1
                and [a for a in current["aliases"] if a != extra[0]] == expected["aliases"]):
            out["ambiguous"] = True
    return out
