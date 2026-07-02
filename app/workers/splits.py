#!/usr/bin/env python3
"""Phase 6 identity-surgery executor: split_entity (ADR-0052) — the INVERSE of merge.

One entity-family node's evidence is divided into a surviving **primary** A (keeps id + name) and a
freshly-minted **spin-off** B. The human partitions A's `mentions` (`spinoff_sources`) + aliases; the listed
mentions **MOVE** `source→A` ⇒ `source→B`, B is minted `candidate`, A keeps the rest. **FORWARD-ONLY**, not a
merge: nothing is retired, no tombstone, no new lifecycle status, no pending-review withdrawal. Two-pass: a
dry plan (all guards + block gates, never partial) then apply. Graph-REQUIRED, key-free, deterministic.
Reuses ADR-0050/0051 machinery (`graph.repoint_edge`/`find_assertion`, `merges._approved_unapplied_block`,
`rekeys._is_canonical_node_id`, the virgin-target gates).

Beyond the three virgin-target gates on B it adds two identity-safety gates (ADR-0052 review round 1):
`spinoff_promote_slot_taken` (a terminal promote record for computed B would fabricate/strand its promotion —
a virgin *node* isn't a virgin *ledger slot*) and `approved_unapplied_references_primary` (split shifts A's
evidence while A survives, so an approved-but-unapplied effect on A must apply first — `promote_candidates`
promotes A on `pre_approved` alone).

**Idempotency / repair (ADR-0052 review round 2).** A crash mid-apply can leave a half-applied split (graph
autocommits per statement, then pages, then the promote item). The executor is therefore repair-safe rather
than blindly no-op'ing on the presence of B's lineage frontmatter:

- **Full-EFFECTED = true no-op.** B's page carries our lineage AND the partition fully moved (each source on
  B, none on A) AND A retains ≥1 mention AND B's promotion is accounted for → `continue`, nothing rewritten.
- **Our-lineage-but-incomplete = bounded repair.** Re-point any still-on-A partition edges, re-render A and B,
  (re-)file B's promote item, and include the whole partition in `affected_sources` for the caller's
  Source-page fan-out. Every step is idempotent. Before repairing, each `spinoff_source` must be **cleanly**
  on B already or still-movable from A; a source on *neither* or on *both* (ambiguous half-move) → typed
  `partial_split_state` skip rather than inventing state.
- **Bare B node / foreign or missing lineage = typed block** (`target_spinoff_id_exists` /
  `target_spinoff_page_exists`), never silent adoption of a slot we can't prove is ours.

Malformed approved artifacts (non-list / unhashable / non-string list fields) become typed skips, never an
uncaught exception that would abort the whole `run_apply` batch.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.backend import graph
from app.backend.manifests import is_source_id, iso_now
from app.workers import concepts, merges, rekeys, reviews
from app.workers.wiki_render import NODE_DIR, render_concept_page

_ENTITY_FAMILY = frozenset({"entity", "person", "organization", "project"})
_SPLITTABLE_STATUSES = frozenset({"active", "candidate"})


def _approved_splits(reviews_dir: Path) -> list[dict[str, Any]]:
    d = Path(reviews_dir) / "approved"
    out: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("type") == "split_entity" and item.get("status") == "approved":
            out.append(item)
    return out


def _stable_dedup(items: Any) -> list:
    """First-seen-order dedup (ADR-0052): `spinoff_sources`/`spinoff_aliases` are sets, but order is kept for
    audit/render determinism; a duplicate is normalized, never an operator error. Robust to a non-list or an
    unhashable element (a malformed artifact): a non-list yields `[]` and an unhashable item is kept rather
    than raised on — the caller's type guards normally reject such artifacts first, this is belt-and-suspenders
    so `_stable_dedup` can never be the crash site."""
    out: list = []
    seen: set = set()
    for x in items if isinstance(items, list) else []:
        try:
            if x in seen:
                continue
            seen.add(x)
        except TypeError:                                # unhashable element -> keep, never raise
            out.append(x)
            continue
        out.append(x)
    return out


def _moved_mention_edges(gconn, a: str, srcs: set[str]) -> list[dict[str, Any]]:
    """A's active `mentions` edges whose source is in the partition (source→A), deterministically ordered.
    All of a listed source's mentions of A move (a source can mention A via several evidence anchors). On a
    repair pass this naturally returns only the still-on-A remainder (already-moved edges point at B)."""
    return sorted([e for e in graph.incoming_active(gconn, a)
                   if e["edge_type"] == "mentions" and e["src_id"] in srcs],
                  key=lambda e: (e["src_id"], e["edge_id"]))


def _target_assertion_collision(gconn, moved: list[dict[str, Any]], b: str) -> bool:
    """True if any re-pointed `source→B` mention would collide with a pre-existing full-identity row (drift/
    tamper backstop — the virgin target normally has none)."""
    for e in moved:
        existing = graph.find_assertion(
            gconn, src_id=e["src_id"], dst_id=b, edge_type="mentions", asserted_by=e["asserted_by"],
            evidence_source_id=e["evidence_source_id"], evidence_char_start=e["evidence_char_start"],
            evidence_char_end=e["evidence_char_end"])
        if existing is not None and existing["edge_id"] != e["edge_id"]:
            return True
    return False


def _promote_slot_terminal(reviews_dir: Path, node_id: str) -> bool:
    """True if a `promote_candidate_node` for `node_id` already exists in a TERMINAL (approved/rejected)
    state (ADR-0052 `spinoff_promote_slot_taken`). A pending slot is fine — it is reused by the split's file."""
    rid = reviews.review_id("promote_candidate_node", {"node_id": node_id})
    return any((Path(reviews_dir) / st / f"{rid}.json").exists() for st in ("approved", "rejected"))


def _promote_item_present(reviews_dir: Path, node_id: str) -> bool:
    """True if a `promote_candidate_node` for `node_id` exists in ANY state (pending/approved/rejected) — i.e.
    B's promotion ledger slot is filled and therefore *accounted for* (ADR-0052). A terminal `rejected` promote
    is a deliberate human accounting ("split done, chose not to promote B"), not a partial split — it counts."""
    rid = reviews.review_id("promote_candidate_node", {"node_id": node_id})
    return any((Path(reviews_dir) / st / f"{rid}.json").exists()
               for st in ("pending", "approved", "rejected"))


def _write_audit(reviews_dir: Path, rid: str, record: dict[str, Any], now: str) -> None:
    audit = Path(reviews_dir) / "audit_log"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / f"{rid}-split-{uuid.uuid4().hex[:8]}.json").write_text(
        json.dumps({"review_id": rid, "decision": "split", "decided_by": "system",
                    "decided_at": now, **record}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def apply_splits(gconn, reviews_dir: Path, *, wiki_dir: Path, now: str | None = None) -> dict[str, Any]:
    """Apply approved `split_entity` decisions (ADR-0052). Returns
    `{applied, skipped:[{review_id, reason}], changed_pages, graph_changed, affected_sources}`. Graph-
    REQUIRED; the caller owns the final commit + the Source-page re-render of `affected_sources`. Repair-safe:
    a half-applied split is completed idempotently, a fully-applied one is a true no-op."""
    now = now or iso_now()
    applied = 0
    skipped: list[dict[str, str]] = []
    changed_pages: list[str] = []
    affected_sources: set[str] = set()

    for item in _approved_splits(reviews_dir):
        rid = str(item.get("review_id", ""))
        subj = item.get("subject") or {}
        proposal = item.get("proposal") or {}
        a = subj.get("node_id")
        b_declared = subj.get("spinoff_node_id")
        spinoff_name = proposal.get("spinoff_name")

        def _skip(reason: str) -> None:
            skipped.append({"review_id": rid, "reason": reason})

        # --- malformed-proposal / derivation guards (typed skips; a bad artifact never raises) ---
        if not isinstance(spinoff_name, str) or not spinoff_name.strip():
            _skip("invalid_proposal")
            continue
        if not rekeys._is_canonical_node_id(a):
            _skip("noncanonical_node_id")
            continue
        n_a = graph.get_node(gconn, a)
        if n_a is None:
            _skip("node_missing")
            continue
        a_type = n_a["node_type"]
        if a_type not in _ENTITY_FAMILY:
            _skip("out_of_scope")
            continue
        if n_a["status"] not in _SPLITTABLE_STATUSES:    # A's status gates both fresh + repair (split
            _skip("node_not_splittable")                 # preserves A's status, so a repair sees it too)
            continue
        b = concepts.node_id(a_type, spinoff_name)
        if b != b_declared:
            _skip("spinoff_id_mismatch")
            continue
        if b == a:                                       # spin-off name hashes to the primary
            _skip("spinoff_equals_primary")
            continue
        raw_srcs = proposal.get("spinoff_sources")
        if not isinstance(raw_srcs, list):
            _skip("invalid_proposal")
            continue
        if not all(is_source_id(s) for s in raw_srcs):   # is_source_id is False (not raise) on non-strings
            _skip("noncanonical_source_id")
            continue
        srcs = _stable_dedup(raw_srcs)
        if not srcs:
            _skip("empty_partition")
            continue
        raw_aliases = proposal.get("spinoff_aliases") or []
        if not isinstance(raw_aliases, list) or not all(isinstance(x, str) for x in raw_aliases):
            _skip("invalid_proposal")
            continue
        aliases_move = _stable_dedup(raw_aliases)
        srcs_set = set(srcs)

        # A's page (needed to render on both the fresh and repair paths)
        b_slug = concepts._slug(spinoff_name)
        b_page_rel = f"{NODE_DIR[a_type]}/{b_slug}.md"
        a_page = f"{NODE_DIR[a_type]}/{n_a['slug']}.md"
        meta_a = concepts._read_node_meta(wiki_dir / a_page)
        if meta_a is None:
            _skip("page_missing")
            continue

        # --- B-slot state branch: fresh apply | no-op-or-repair | typed block ---
        n_b = graph.get_node(gconn, b)
        b_page_abs = wiki_dir / b_page_rel
        b_meta = concepts._read_node_meta(b_page_abs) if b_page_abs.exists() else None
        our_lineage = bool(b_meta and b_meta.get("split_from") == a
                           and b_meta.get("split_review_id") == rid)

        if n_b is not None or b_page_abs.exists():
            # B's id-slot is already touched — never silently adopt a slot we can't prove is ours.
            if not our_lineage:
                _skip("target_spinoff_id_exists" if n_b is not None else "target_spinoff_page_exists")
                continue
            # our lineage: the node must be present + splittable-status, else it is a broken partial
            if n_b is None or n_b["status"] not in _SPLITTABLE_STATUSES:
                _skip("partial_split_state")
                continue
            a_mentioners = set(graph.sources_for_node(gconn, a))
            b_mentioners = set(graph.sources_for_node(gconn, b))
            # every listed source must be CLEANLY on B (already moved) XOR still on A (movable); a source on
            # neither (lost/invented) or on both (half-moved) is ambiguous -> don't repair, flag the partial
            if any((s in a_mentioners) == (s in b_mentioners) for s in srcs_set):
                _skip("partial_split_state")
                continue
            fully_effected = (srcs_set <= b_mentioners and not (srcs_set & a_mentioners)
                              and bool(a_mentioners)
                              and (n_b["status"] == "active" or _promote_item_present(reviews_dir, b)))
            if fully_effected:
                continue                                 # true idempotent no-op (matches _effect_split EFFECTED)
            repaired = True                              # bounded repair: fall through to the apply block
        else:
            repaired = False
            # --- fresh-path partition + virgin-target block gates (never partial-applies) ---
            a_sources = set(graph.sources_for_node(gconn, a))   # A's active mention sources
            if not srcs_set <= a_sources:
                _skip("source_not_mentioned")
                continue
            if srcs_set == a_sources:                     # all moved -> a rename, not a split; A keeps >=1
                _skip("full_partition_is_rename")
                continue
            a_aliases_norm = {concepts._normalize_name(x) for x in meta_a["aliases"]}
            if not all(concepts._normalize_name(x) in a_aliases_norm for x in aliases_move):
                _skip("alias_not_on_primary")
                continue
            if _target_assertion_collision(gconn, _moved_mention_edges(gconn, a, srcs_set), b):
                _skip("target_spinoff_assertion_exists")
                continue
            if _promote_slot_terminal(reviews_dir, b):
                _skip("spinoff_promote_slot_taken")
                continue
            if merges._approved_unapplied_block(gconn, reviews_dir, wiki_dir, a, a_page, rid):
                _skip("approved_unapplied_references_primary")
                continue

        # --- APPLY / REPAIR (re-point before render; idempotent; never partial from here) ---
        moved = _moved_mention_edges(gconn, a, srcs_set)   # all on-A edges (fresh) / the remainder (repair)
        graph.upsert_node(gconn, node_id=b, node_type=a_type, slug=b_slug, status="candidate", now=now)
        for e in moved:
            graph.repoint_edge(gconn, e["edge_id"], new_dst=b, now=now)   # source→A ⇒ source→B
        # render the spin-off B (candidate; its moved mentions; split lineage)
        b_page_abs.parent.mkdir(parents=True, exist_ok=True)
        b_page_abs.write_text(render_concept_page({
            "node_type": a_type, "node_id": b, "id_field": concepts.ID_FIELD[a_type],
            "title": spinoff_name, "aliases": aliases_move, "confidence": meta_a.get("confidence", "low"),
            "source_ids": graph.sources_for_node(gconn, b), "status": "candidate",
            "duplicates": graph.active_duplicates(gconn, b), "split_from": a, "split_review_id": rid,
        }), encoding="utf-8")
        # re-render primary A: aliases minus spinoff_aliases AND the auto-moved spin-off name; status unchanged
        drop = {concepts._normalize_name(x) for x in aliases_move} | {concepts._normalize_name(spinoff_name)}
        a_aliases_final = [x for x in meta_a["aliases"] if concepts._normalize_name(x) not in drop]
        (wiki_dir / a_page).write_text(render_concept_page({
            "node_type": a_type, "node_id": a, "id_field": concepts.ID_FIELD[a_type],
            "title": meta_a["title"], "aliases": a_aliases_final, "confidence": meta_a.get("confidence", "low"),
            "source_ids": graph.sources_for_node(gconn, a), "status": n_a["status"],
            "duplicates": graph.active_duplicates(gconn, a),
            "split_from": meta_a.get("split_from"), "split_review_id": meta_a.get("split_review_id"),
        }), encoding="utf-8")
        graph.upsert_node(gconn, node_id=a, node_type=a_type, slug=n_a["slug"], status=n_a["status"], now=now)
        # file B's promote_candidate_node so the new candidate enters the promotion ledger (the promote pass
        # won't file a pending item for a not-yet-independent candidate). Idempotent: reuses a pending slot,
        # and is a no-op when a terminal (approved/rejected) slot already accounts for B.
        reviews.create_review_item(
            reviews_dir, review_type="promote_candidate_node", subject={"node_id": b},
            proposal={"to_status": "active", "node_type": a_type},
            context={"created_by": "split", "split_from": a}, now=now)
        item_pages = [b_page_rel, a_page]
        changed_pages.extend(item_pages)
        affected_sources |= srcs_set                     # whole partition (incl. already-moved) for the fan-out
        _write_audit(reviews_dir, rid, {
            "repaired": repaired,
            "primary": {"node_id": a, "type": a_type, "slug": n_a["slug"], "page": a_page,
                        "aliases_after": a_aliases_final},
            "spinoff": {"node_id": b, "type": a_type, "slug": b_slug, "page": b_page_rel,
                        "title": spinoff_name, "aliases": aliases_move},
            "moved_sources": sorted(srcs_set), "moved_edges": [e["edge_id"] for e in moved],
            "affected_pages": item_pages + [f"Sources/{s}.md" for s in sorted(srcs_set)],
        }, now)
        applied += 1

    return {"applied": applied, "skipped": skipped, "changed_pages": sorted(set(changed_pages)),
            "graph_changed": applied > 0, "affected_sources": sorted(affected_sources)}
