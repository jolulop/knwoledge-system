# ADR-0043 — hide_content governance executor: source visibility via a reversible `hidden` status

**Status:** Accepted. Design-locked 2026-06-26 via a grill gate — **design only, not yet implemented**.
The second non-rekeying governance executor (after ADR-0041 `mark_semantic_duplicate`): make the
record-only `hide_content` review type executor-backed for **sources**, suppressing a source from
default retrieval + navigation via a new reversible `hidden` lifecycle status — **no id rewrite, no
deletion, content stays on disk.**
**Extends:** ADR-0041 (governance-executor risk taxonomy — stable-id rekeying is the bright line;
`hide_content` is non-rekeying), ADR-0036 (Phase 7 retention; `archive_source` / `apply_archive_sources`
is the direct precedent — a reversible status transition on the manifest, never raw bytes), ADR-0022
(page lifecycle status vs review_status), ADR-0032 (retrieval status filter `RETENTION_DEFAULT_STATUSES`),
ADR-0040 (apply dry-run preview), ADR-0030 (graph is SoT for edges). Read `app/workers/retention.py`
(`apply_archive_sources`), `app/backend/graph.py` (`NODE_STATUSES`), `app/backend/search.py`
(`RETENTION_DEFAULT_STATUSES`, `search_navigation`), `app/backend/keyword_index.py` (`answer_eligible`),
`app/backend/review_read.py` (`EXECUTOR_BY_TYPE`, `preview_archive_source`).

## Context

`hide_content` exists in `REVIEW_TYPES`/`review.yaml` (and `policies/retention.yaml`
`wiki_pages.hide_requires_review`) but is **record-only** — no executor. The governance need is to
suppress *specific* content from surfacing (sensitive / wrong / unwanted), distinct from retention
staleness — without deleting or rekeying it (reversible, auditable). The system already drives visibility
by **lifecycle status**: `RETENTION_DEFAULT_STATUSES = (active, deprecated_candidate)` filters both
`/query` retrieval evidence (via the source-status join) and the `/search` navigation channel, and
`keyword_index` sets `answer_eligible = (status == "active")`. `archive_source` proves the pattern: a
reversible `active → archive_candidate` transition on the manifest (authority) → Source-page mirror →
graph node mirror → reindex, after which the default filters exclude it. This ADR makes `hide_content` a
near-clone of that path with a distinct status and intent.

## Decisions

**1. What "hidden" suppresses — retrieval + navigation, NOT graph traversal.**
Hidden content is excluded from `/query` answer evidence and `/search` results — **including the
`/search` graph channel**, which is retention-status-filtered (`node_statuses` default
`RETENTION_DEFAULT_STATUSES`) and so drops hidden by default — and from default navigation, while the page
file + raw bytes stay on disk and the **graph (nodes/edges as SoT) stays intact**. The **raw `/graph/*`
APIs** (`graph_read` neighborhood) still surface a hidden node *with its status visible*
(provenance/diagnostics/reversibility preserved) — i.e. the *discovery* surfaces (search/nav, incl.
`/search`'s graph group) exclude hidden, but the *inspection* API does not. A backlink to a hidden node may
still render on another page (graph SoT unchanged) — a documented v1 limit. "Hidden from discovery," not
graph surgery. Explicit status filters (`source_status=hidden`) can still surface it on the dynamic APIs.

**2. Mechanism — a new reversible `hidden` lifecycle status (the archive pattern), active-only.**
Add `hidden` to the **full** status vocabulary — **not** a parallel boolean flag (which every renderer/
validator/indexer/filter would have to remember to AND with status). The complete add-list (a `hidden`
left out of any of these is a hard failure *before* visibility behavior matters):
- **`app/backend/manifests.py::SOURCE_STATUSES`** — the manifest is the source-status authority and
  `manifests.set_status` validates against it; **without this, `set_status(sid, "hidden")` raises** and the
  executor can't run. (First thing to add + test.)
- **`app/backend/graph.py::NODE_STATUSES`** — the graph node-status set; also makes `source_status=hidden`
  parseable (`search.parse_statuses` validates against `NODE_STATUSES`).
- **`policies/retention.yaml` `statuses:`** list + the validator status allow-sets
  (`validate_frontmatter`/`validate_wiki`/`validate_graph`). `RETENTION_DEFAULT_STATUSES` is **unchanged**, so
`hidden` (absent from it) is excluded from retrieval evidence + navigation **for free** — exactly like
`archive_candidate`. (Precision: for a **source**, the levers are the **evidence-retrieval source-status
filter** — chunk evidence is filtered by its source's status — and the **navigation channel**, *not*
`answer_eligible`: Source pages are already `answer_eligible: false` regardless of status, because they are
citation/nav artifacts, never answer pages. `answer_eligible` only becomes a hiding lever for the deferred
**semantic-page** slice.) The executor transitions only content
currently **`active`** (mirroring `apply_archive_sources`), so `hidden ↔ active` is clean with **no
prior-status memory** needed. `hidden` is kept **distinct from `archive_candidate`** despite the identical
visibility mechanism, because the *intent + audit trail* differ (governance suppression vs retention
staleness) and they must be filterable separately. No `hidden_candidate` — the review item is the approval
gate (one-step `active → hidden` on approve).

**3. Type scope — sources only (v1).**
`hide_content` targets `subject.source_id` only. The **manifest `status: hidden` is authority**; the
Source page is re-rendered with `status: hidden`; the graph source-node mirror is updated best-effort (if
the graph exists); keyword/navigation is reindexed after apply. Raw bytes untouched. Sources are the only
type carrying citable retrieval evidence, so source-hiding delivers the full decision-1 retrieval+nav
contract with maximal reuse of the proven source-status pipeline. **Semantic-node-page hiding**
(concept/entity/person/org/project/claim/synthesis — page/graph status authority, nav/projection-only,
trickier graph read-model questions) is an explicit **fast-follow slice**, not v1 (avoids mixing manifest
authority and page-frontmatter authority in the first slice).

**4. Lifecycle — hide-only v1 (`active → hidden`); unhide deferred.**
The executor (`apply_hidden_sources`, mirroring `apply_archive_sources`) handles only `active → hidden`,
one-directional like archive. Reversibility is **preserved** (status can be flipped back manually via
`manifests.set_status`, or by a later unhide slice/review) — there is just no unhide executor in v1.
- **Subject/proposal:** `subject = {source_id}` (canonical); `proposal = {to_status: hidden}`.
- **Scope guards (skip with a reason, never partial-apply — mirror `apply_archive_sources`):**
  `invalid_source_id` (untrusted subject, rejected before any path use), `source_missing`,
  `unexpected_to_status` (not `hidden`), and an **idempotent no-op** when the source is already not
  `active` (already hidden / archived / deprecated → skip, no flip). **Explicit intentional limit:** because
  the transition is `active → hidden` only, a source that is **already `archive_candidate`/`archived`/
  `deprecated_candidate` cannot be hidden in v1** (it is a no-op skip, not an error). Hiding non-active
  content needs the deferred unhide/bidirectional-transition work; a still-sensitive archived source is, in
  practice, already excluded from default retrieval/nav by its current status.
- **Reject = no-op** (the source stays active); `decision_apply_required(hide_content, approved) == True`,
  `(…, rejected) == False`; **not** in `_REJECT_HAS_EFFECT`.
- **Governing switch = the review type, no new retention knob.** Source hiding is executable *only* via an
  **approved `hide_content` review item** (human approval mandatory, CLAUDE.md rule 9); `policies/
  review.yaml::hide_content` is the governing contract. v1 adds **no** `retention.yaml` field — the
  existing `wiki_pages.hide_requires_review` is **reserved for the future semantic-page hiding slice**, not
  source hiding (adding a `raw_files.hide_requires_review` now would be an unenforced second source of
  truth). If a future producer ever *auto-proposes* source hides, that producer slice adds the knob.
- **Classification:** executor-backed but **not graph-required** (like `archive_source` — the load-bearing
  effect is manifest + Source page; the graph source-node mirror is best-effort, skipped when the graph is
  absent). Add to `_APPLY_TYPES`; `EXECUTOR_BY_TYPE["hide_content"] = "apply_hidden_sources"`; run inside
  the caller-owned `run_apply` (own graph conn, like archive).
- **A1 preview projector** `preview_hide_content` mirrors `preview_archive_source`: `node_ids=[source_id]`,
  `affected_paths=["Sources/<sid>.md"]`, `proposed_status=hidden`, `current_status` from the manifest
  (the authority), `invalid_subject` on a non-canonical `source_id`, `effected` when already `hidden`.
- **Previewable** via the ADR-0040 dry-run: a `diff.manifests` field-level `status: active → hidden` + the
  Source-page wiki diff; live byte-identical until apply.

**5. `index.md` — hidden Source pages stay listed, annotated `hidden` (same as archive).**
The dynamic surfaces (`/query` retrieval, `/search` navigation, `answer_eligible`) exclude hidden by
default via the status filter. The **static `wiki/index.md` browse-all catalog continues to list** hidden
Source pages, annotated `status: hidden` — exactly as `archive_candidate` appears today. `index.md` is an
audit/catalog surface, not the primary retrieval/navigation filter; since the content remains on disk,
omitting it from the catalog would be misleading and would create a second hiding mechanism that
complicates auditing. No new `rebuild_index.py` filtering.

## Consequences

A second non-rekeying governance executor ships on the safest footing: a deterministic, idempotent,
reversible `active → hidden` status transition that suppresses a source from default retrieval +
navigation by **reusing the proven `archive_source` status→retrieval→nav→answer-eligibility pipeline**,
touching no stable id, raw byte, citation, or backlink target, and previewable via the ADR-0040 dry-run.
Costs: one new status value threaded through the status vocabulary + validator allow-sets, the executor +
its guards, the A1 projector, and the classification wiring. Deferred: **unhide** (`hidden → active`
executor), **semantic-node-page hiding**, and the documented v1 limit that a hidden node's backlinks may
still render on other pages (graph traversal stays visible by design, decision 1).

## Tests (design intent; written at implementation)

- Approve `hide_content{source_id}` → manifest `status: hidden`, Source page re-rendered `status: hidden`,
  graph source-node mirror `hidden` (when graph present); idempotent re-apply is a no-op (already hidden).
- A hidden source is **excluded from `/query` evidence and `/search` (keyword + navigation)** by default,
  and **`answer_eligible` is false**; an explicit `source_status=hidden` still finds it.
- `wiki/index.md` still lists the hidden Source page, annotated `hidden`; raw bytes byte-identical.
- Scope guards skip with their reason (`invalid_source_id`, `source_missing`, `unexpected_to_status`,
  already-not-active no-op); reject is a no-op (source stays active).
- Graph-absent apply still hides (manifest + page authority; graph mirror skipped) — not a 503
  (`hide_content` is not graph-required).
- Dry-run preview shows the manifest `active → hidden` diff + Source-page diff; live unchanged until apply.
- `hidden` is an accepted status everywhere: **`manifests.set_status(sid, "hidden")` succeeds** (in
  `SOURCE_STATUSES`), `search.parse_statuses("hidden", NODE_STATUSES, …)` parses,
  `validate_wiki`/`validate_frontmatter`/`validate_graph` accept it; validators pass on a hidden source.
- The `/search` **graph** channel excludes a hidden source by default, while raw `/graph/node` returns it
  with `status: hidden`.
