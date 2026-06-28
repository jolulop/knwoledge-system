# ADR-0046 — Semantic-page hiding: hide_semantic_page governance executor

**Status:** Accepted. Design-locked **and implemented** 2026-06-27. `deprecations.apply_hidden_semantic_pages`
(the `recompose_semantic_node_page(status="hidden", review_status="approved")` executor, graph-required,
active-only, concept/entity family); `review_read._effect_hide_semantic` + `preview_hide_semantic_page` +
`HIDE_SEMANTIC_SCOPE_DIRS`; `hide_semantic_page` in `REVIEW_TYPES` + `review.yaml` + `EXECUTOR_BY_TYPE`;
`main.run_apply` wiring (`_APPLY_TYPES`/`_GRAPH_REQUIRED_TYPES`, summary `semantic_hidden`, stricter reindex
posture). Covered by `tests/test_hide_semantic_page.py`.
Extends the ADR-0043 source-hide model to **generated semantic node pages**: an approved
`hide_semantic_page` flips a concept/entity-family node `active → hidden`, suppressing it from default
retrieval + navigation + the `/search` graph channel and revoking its answer-eligibility — still
**non-rekeying** (the ADR-0041 bright line), no identity surgery, no edge changes.
**Extends:** ADR-0043 (source `hide_content` + the `hidden` lifecycle status), ADR-0035 A5 (the
`deprecate_wiki_page` executor — subject `{page, node_id}`, canonical-page guard, scope-dir gating, the
`recompose_*` render seam), ADR-0030/0022 (the page frontmatter is the node-status authority; graph node
is a mirror), ADR-0040 (apply dry-run), ADR-0045 (reopen). Read `app/workers/concepts.py`
(`recompose_semantic_node_page` — the page+graph status writer), `app/workers/deprecations.py`
(`apply_approved_deprecations` — the subject/guard/scope machinery to mirror), `app/backend/review_read.py`
(the `_effect_*` projectors + `effect_status`), `app/backend/eligibility.py` (`ANSWER_ELIGIBLE_TYPES`),
`app/backend/search.py` (`RETENTION_DEFAULT_STATUSES`, the navigation + graph-channel node-status filters).

## Context

ADR-0043 shipped **source** hiding: `active → hidden` on the **manifest** (the source's status authority),
excluded from default retrieval + nav, raw bytes untouched, graph mirror best-effort. Generated **semantic
node pages** (concept/entity/person/organization/project/claim/synthesis) are the natural fast-follow, and
still non-rekeying. But their authority differs: a semantic node's status authority is its **page
frontmatter** with the **graph node as a synchronous mirror** (ADR-0030/0022) — there is no manifest. The
existing `recompose_semantic_node_page(gconn, node_id, wiki_dir, status, review_status)` already writes
**both** (re-renders the page at an explicit status, mirrors the graph node) and is the deprecation
executor's render seam for the concept/entity family. And `ANSWER_ELIGIBLE_TYPES` includes **every**
semantic type with `answer_eligible = (status == "active" and …)`, so for semantic pages — unlike sources,
which are never answer-eligible — hiding **revokes answer-eligibility**, the primary new effect.

## Decisions

**1. Scope v1 = the concept/entity node-page family** (`concept`, `entity`, `person`, `organization`,
`project`) — the single `recompose_semantic_node_page` seam. **`claim`** (the distinct `recompose_claim`
seam, with its own evidence/citation/contradiction-supersede semantics) and **`synthesis`** (a separate
executor path) are **fast-follows**, each its own slice — exactly how ADR-0043 scoped sources-first and how
`deprecate_wiki_page` grew its executor branches.

**2. Authority = page frontmatter (authoritative) + graph node (mirror), via `recompose_semantic_node_page`
— and therefore GRAPH-REQUIRED.** The executor renders through that one seam at **`status="hidden"` AND
`review_status="approved"`** (the human approval — mirroring the deprecation contract, which requires both;
a hide must never leave `review_status: pending`), which reads the node from the graph (display metadata +
active `mentions`) and writes page + graph-node mirror together. It
is **graph-required**: if the graph is absent or the node is missing, the executor **skips/blocks** (never a
page-only hide) and the apply gate returns **503** — because a page-only hide would leave the graph node
`active`, so it would still surface as a `/search` graph adjacent: an **incomplete, misleading** hide.
(Sources could hide graph-best-effort only because the manifest is their authority; semantic nodes have no
such manifest fallback.) The new type therefore joins `_GRAPH_REQUIRED_TYPES`, and the ADR-0040 dry-run on a
graph-unavailable vault blocks/503s like the other graph-required apply items.

**3. Effect = one status flip; all suppression falls out of existing filters; edges + inspection
preserved.** `active → hidden` cascades:
- **excluded from default `/search` navigation** (the `RETENTION_DEFAULT_STATUSES` node-status filter —
  `hidden` is not in it), so the page drops out of the discovery channel;
- **excluded from the `/search` graph channel** as an adjacent/waypoint (`search_subgraph` `node_statuses`
  filter), so default-search paths *through* a hidden node are cut;
- its **`answer_eligible` metadata flips false** (`answer_eligible` requires `status == "active"`) —
  surfaced on `/search` navigation rows + `/graph/*` node metadata, marking the page non-anchoring for
  synthesis/discovery.

*(`/query` answer-citations are **source-chunk evidence** keyed by `source_id` (ADR-0034) — semantic pages
are never `/query` citations regardless of status — so hiding a concept changes default `/search`
discovery, not `/query` source citations. This is the accurate surface; the discovery suppression + graph-
channel cut are the real effect vs a source hide.)*

**Preserved** (hiding is reversible visibility, **not** graph surgery): the graph node and **all its edges
stay** (graph is SoT for edges — no edge deletion, no backlink rewrite in v1), and the **raw `/graph/*`
inspection APIs still return the node with `status: hidden`** (the edge-status-only inspection layer —
exactly as ADR-0043 kept hidden sources inspectable). Suppressing edges/traversal would be edge surgery
(drifting toward the deferred identity-surgery class) and would break the inspection-vs-discovery split.

**3a. Stricter reindex posture (mirrors ADR-0043) + `wiki/index.md` stays a catalog.** The suppression in
decision 3 is delivered by the **keyword/navigation index**, so — exactly as source `hide_content` — a
semantic hide that **applies/normalizes while `reindex_keyword` fails** makes apply **non-clean**: status
`validation_failed` + warning **`semantic_hide_retrieval_suppression_not_guaranteed`** (the page + graph are
hidden, but a stale index can still surface it until reindex succeeds), and the ADR-0040 dry-run previews the
same non-clean posture in its sandbox. The mutation stays written; the warning tells the operator suppression
may lag. Separately, the static **`wiki/index.md`** browse-all/audit catalog **keeps the hidden page listed,
annotated `hidden`** (like hidden sources) — default `/query`/`/search`/nav/graph discovery already excludes
it via the status filters, and an `index.md` **rebuild** failure stays **warning-only** (it is the catalog,
not the keyword/nav suppression lever).

**4. Reuse the `hidden` status; a NEW `hide_semantic_page` review type modeled on `deprecate_wiki_page`.**
No new status — `hidden` is already valid for graph nodes (`graph.NODE_STATUSES`) and page frontmatter
(`validate_wiki`). The new type is **distinct** from source `hide_content` (different subject shape,
authority, and graph posture):
- `hide_content` — **source**-only, `subject.source_id`, manifest authority, graph-optional (unchanged).
- `hide_semantic_page` — semantic node/page, `subject {page, node_id}`, `proposal.to_status: hidden`,
  graph-required; reuses `deprecate_wiki_page`'s safety machinery verbatim: the **canonical-page guard**
  (`subject.page` must equal the node's canonical `NODE_DIR[type]/slug.md` — no traversal), **scope-dir
  gating** (v1: `Concepts`/`Entities`/`People`/`Organizations`/`Projects`), and the `recompose_semantic_node_page`
  render seam (with `status="hidden"` instead of `"deprecated_candidate"`). Overloading `hide_content` to
  branch on subject shape would fork one type across two authority models — rejected.

**5. Active-only, hide-only v1; reject is a no-op; unhide deferred.** v1 transitions **`active → hidden`
only** (mirroring ADR-0043): a node that is not `active` (candidate / deprecated_candidate / already hidden)
is a **skip/no-op**, not an error. A rejected `hide_semantic_page` applies nothing. **Unhide (`hidden →
active`) is deferred** to a separate future slice — it is a new executor *direction* (re-deriving/restoring
status), unrelated to ADR-0045 reopen (which reverts a *review decision*, not a *status*).

**6. Reopen + dry-run integration is free — and reopen-safe.** A `hide_semantic_page` projector
(`_effect_hide_semantic`) classifies by how much of the hide is **live**, which the ADR-0045 reopen gate
consumes directly (reopen is allowed only for `PENDING_APPLY`/`NO_EFFECT_REQUIRED`, blocked for
`EFFECTED`/`UNKNOWN`):
- **`EFFECTED`** — page `status: hidden` **+** `review_status: approved` **+** graph node `hidden` (fully
  live; reopen blocked — that's unhide territory).
- **`UNKNOWN` (`partial_hide_state`)** — a **partial live hide**: page XOR graph already `hidden`, or both
  `hidden` but `review_status` not yet approved. Crucially this is **NOT** `PENDING_APPLY`: reopen treats
  `PENDING_APPLY` as "no live effect to orphan", so a partial hide returning it would let a reopen clear the
  decision while leaving a hidden page/node behind. `UNKNOWN` blocks reopen ("repair the read model first").
- **`PENDING_APPLY`** — **neither** page nor graph is `hidden` (cleanly unapplied; reopenable). A non-active
  node still gets a `node_not_active` warning (the executor will skip it), but reopen stays safe because no
  hide effect is live.
- **`UNKNOWN`** — graph absent / node missing (existing) / `rejected` → `NO_EFFECT_REQUIRED`.

The same projector feeds the ADR-0040 dry-run preview. No new gate logic — the reopen-safety follows from
classifying partial live state as non-`PENDING_APPLY`.

**7. Policy posture (note for future producers).** v1 has **no auto-producer** — `hide_semantic_page` items
are human-initiated governance proposals (CLAUDE.md rule 9), the same as source `hide_content`.
**`review.yaml::hide_semantic_page`** is the immediate review-type gate; a future
**`policies/retention.yaml::wiki_pages.hide_requires_review`** key is the intended policy posture if/when an
automated producer proposes semantic hides.

## Consequences

Curators can suppress a noisy/incorrect generated concept/entity page from default discovery + answer
synthesis with a reversible, audited, **non-rekeying** status flip that reuses the deprecation executor's
render seam and guards, the `hidden` status, and the dry-run/reopen/projector machinery wholesale — the
node and its provenance/edges stay intact and inspectable. Costs: the `hide_semantic_page` type +
`recompose_semantic_node_page`-backed executor (graph-required), its projector + preview, the apply
orchestration wiring (`_APPLY_TYPES`, `_GRAPH_REQUIRED_TYPES`, summary), and tests. Deferred: claim +
synthesis hiding (distinct seams), **unhide** (`hidden → active`), any edge/backlink change, and all
identity surgery.

## Tests (design intent; written at implementation)

- An approved `hide_semantic_page` for an **active** concept flips the page to `status: hidden` +
  `review_status: approved` **and** the graph node to `hidden`; the page drops from default `/search`
  navigation and the `/search` graph channel (not an adjacent), its `answer_eligible` flips false, and an
  explicit `node_status=hidden` surfaces it again; raw `/graph/*` still returns it with `status: hidden`;
  its edges remain. (`/query` cites source chunks, not semantic pages — out of scope for this effect.)
- **Partial/active-only states:** graph `active` + page hidden → apply (completes the transition); graph
  `hidden` + page `active` → `node_not_active` skip (no page mutation); page+graph `hidden` +
  `review_status` not approved → normalize.
- **Projector `EFFECTED` requires all three** — page `status: hidden` **+** page `review_status: approved`
  **+** graph node `hidden`; a **partial live hide** (page XOR graph `hidden`, or both `hidden` but
  `review_status` not approved) projects **`UNKNOWN` (`partial_hide_state`)** — NOT `PENDING_APPLY` — so
  reopen blocks it; only when **neither** is `hidden` is it `PENDING_APPLY` (reopenable).
- **Reindex-failure non-clean:** an applied semantic hide + a failed `reindex_keyword` → apply status
  `validation_failed` + warning `semantic_hide_retrieval_suppression_not_guaranteed` (live + dry-run); the
  mutation stays written. A failed `index.md` *rebuild* stays warning-only.
- **`wiki/index.md`** still lists the hidden page annotated `hidden` (catalog), while default discovery
  excludes it.
- **Graph-required:** graph absent / node missing → the executor skips/blocks and the apply gate 503s (no
  page-only hide); the dry-run on a graph-unavailable vault previews non-clean/blocked.
- **Active-only:** a candidate / deprecated_candidate / already-hidden node is a skip/no-op (honest reason),
  and the projector adds a `node_not_active` warning on an approved hide of a non-active node; a **rejected**
  item applies nothing.
- **Canonical-page + scope guards** (reused from `deprecate_wiki_page`): a `subject.page` ≠ the node's
  canonical page, a traversal path, or an out-of-scope dir → skipped, never read/written.
- **Reopen-safe (ADR-0045):** a partial live hide (page hidden + graph active, or vice versa, or both
  hidden + `review_status` pending) → **409** (UNKNOWN `partial_hide_state`), no ledger mutation; a cleanly
  unapplied hide (neither hidden) reopens; a graph-only completion (page already hidden) still triggers
  reindex (else a stale nav index could surface the hidden page).
- **Reopen (ADR-0045):** a `PENDING_APPLY` approved hide reopens (→ pending); an `EFFECTED` one is blocked
  (`already_applied`).
- Idempotent re-apply of an already-hidden node is a no-op; `hide_content` (source) behavior is unchanged.
