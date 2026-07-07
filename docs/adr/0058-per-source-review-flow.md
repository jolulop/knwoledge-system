# ADR-0058 — Per-source review flow: sequential source lens, batch decide, approve-with-amendments, human-add

- **Status:** design-locked
- **Date:** 2026-07-07
- **Drivers:** W1 grill — the user's framing requirement: candidate review processed on a
  single-source basis, sources reviewed sequentially, per-source UI showing all candidate items for
  approve/reject, with editing of proposed items and adding new ones; one external design-review
  round (4 blockers, all resolved)
- **Related:** ADR-0009 (loopback-only), ADR-0017/0021 (frozen node identity; rename = slug +
  relink + alias), ADR-0018 (promotion lifecycle; single promotion writer), ADR-0030 (page
  frontmatter is status authority), ADR-0035 + A8 (review UI contract; hand-rolled HTML),
  ADR-0040 (queue-wide apply + dry-run), ADR-0041 (rekey risk bright line), ADR-0045
  (reopen/re-decide), ADR-0051 (`change_entity_subtype`), ADR-0057 (reconciliation — runs first)

## Context

The flat review queue offers no reviewing rhythm: 1380 unactioned items in one table, each decided
by its own round-trip. Nothing promotes, so the semantic layer stalls. The natural review unit is
the **source** — the reviewer has one document's context in mind and can judge all its candidates
together — but review items carry no source id: `promote_candidate_node.subject` is `{node_id}`
only, and source linkage lives in the graph (`mentions` edges; `graph.sources_for_node` /
`mentions_for_source`). Candidate identity is name-hashed and source-agnostic (ADR-0021), so a
candidate is **inherently multi-source** — recurrence promotion depends on it.

ADR-0057's sweep runs before this slice, so the flow is designed against the post-cleanup queue.

## Decisions

### 1. A lens, not a replacement

The per-source flow is a high-volume review **lens** over extraction-caused items. The flat queue
(`/ui/reviews`) remains canonical and type-complete (ADR-0035: types are never hidden). Attribution:

- `promote_candidate_node`: shown under every source with an **active** `mentions` edge to the
  candidate.
- `change_entity_subtype`: shown under `context.source_id`.
- `deprecate_wiki_page`: a separate **"Retired by re-extraction"** section, only when attribution
  is deterministic — the exact predicate: shown under source S iff (1) unresolved
  (`pending`|`deferred`); (2) recompose-provenance (ADR-0057 decision 2 gate:
  `reason_code`/legacy constant) AND `context.node_type ∈ {concept, entity, person, organization,
  project}`; (3) the node has zero ACTIVE `mentions` edges; (4) `H` — the distinct `src_id` set
  over the node's SUPERSEDED `mentions` edges — satisfies `H == {S}`. `|H| > 1` (multi-source
  retirement) and `|H| == 0` (no superseded provenance) stay flat-queue-only. Merge-superseded
  edge rows cannot leak in: gate (2) filters them.
- Everything else (contradiction, synthesis, merge/split, archive/hide, duplicates, record-only
  types) stays global: those are governance/lifecycle/identity decisions, not source-extraction
  cleanup; forcing source ownership would create duplicate surfaces. The end of the per-source
  flow links back: "Global review items remaining", with counts by type.

### 2. Multi-source candidates: shown everywhere, decided once

A multi-source candidate appears on **every** mentioning source's screen with a badge ("also
mentioned by N other sources"). The **first decision resolves it globally**; on later sources'
screens it renders read-only as decided, showing which source's session decided it, the decision,
and when. Recurrence-eligible candidates (≥2 independent sources — would auto-promote at the next
apply, ADR-0018) are labeled as such. One-owner-source and excluded-if-multi-source were rejected
(items hidden from a screen where the source genuinely mentions them; unreviewed auto-promotes).

### 3. Sequence UX: ordered index, free jump

An entry page lists ALL reviewable sources — including zero-item ones, greyed out as done — in
**manifest ingest order** (creation/import time, tie-break `source_id`), each with counts
(remaining / approved / rejected / deferred / added / amended). "Start review" / "Next source"
jump to the first source with remaining attributable items; any row is directly clickable; a
deferred item never blocks the next source. Progress copy: "source k of N · M items remaining
overall". A strict wizard was rejected (one contested item stalls the pipeline);
biggest-queue-first was rejected as default (unstable order; may return as an optional sort).

### 4. Batch decide: one form per source, untouched = pending

Each source screen is **one HTML form**: per-item approve/reject/defer radios defaulting to "no
decision", inline amendment fields, one submit. The POST loops the EXISTING single-item ledger
primitives (`_record_decision` path) — **no new ledger primitive**; it is orchestration only.
Already-decided rows **skip with a per-item reason** (never 409-abort the batch — the batch
variant of the ADR-0041 scope-guard posture); results are reported per item. Partial passes are
normal; a source is done when zero attributable items remain. **Apply is unchanged**: still the
explicit, queue-wide, dry-run-previewed end step (ADR-0040); the flow records decisions only.

**Guarded sweep shortcuts are deferred, with constraints pinned now**: sweep only over currently
visible eligible `promote_candidate_node` rows; exclude rows with amendments, subtype conflicts,
warnings, duplicate/name collisions, and all deprecations; require an explicit "I reviewed all
visible eligible candidates" checkbox; specific button copy ("Approve 18 unchanged candidate
nodes", never "Approve all"); never the default action. Ship only after the batch form proves the
ergonomics.

### 5. Edit = approve-with-amendments (promote items only)

`POST /reviews/{id}/approve` (JSON and the batch form over it) accepts an optional **amendments**
payload for `promote_candidate_node` only — exactly three fields: `title`, `aliases`,
`description`. Semantics, tightly defined:

- Recorded **immutable** in the ledger at decision time; the **promote executor** applies them
  when it flips the node to `active` (single promotion writer preserved, ADR-0018).
- **Node id stays frozen** — an amended title never re-hashes the id (ADR-0021).
- **Slug re-derived** from the amended title; the page path may move, so the promote executor owns
  the move (+ the graph slug mirror), per the ADR-0017 rename contract.
- Old title **auto-added to aliases** when the title changes (unless already present); aliases
  normalized/deduped and stored as the page-authoritative alias list.
- `description` is a **new page-owned frontmatter field** that `render_concept_page` preserves and
  recompose carries through re-extraction (like `split_from`/`split_review_id`). Concept/entity
  pages have no human prose field today; the machine summary callout stays machine-composed.
- **Reject rejects the candidate**, not the amendment. **Defer preserves typed amendments** as a
  mutable `draft_amendments` block INSIDE the pending item file — excluded from identity by
  construction (`review_id` hashes only `type|subject`), never read by executors, frozen into the
  immutable `amendments` payload (validated) at approve time, discarded on reject. A separate
  UI-only draft artifact was rejected (second store → drift).
- **Subtype is NOT amendable**: the form routes to a `change_entity_subtype` item instead (rekey =
  the ADR-0041 risk bright line, own executor). Concept↔entity cross-type stays deferred.

**Named hazard — alias divergence:** after a title amendment, future extractions of the NEW name
hash to a **different** node id, creating a duplicate candidate. Accepted for v1: repaired via the
existing `merge_concepts`/`merge_entities` executors; teaching the extractor alias resolution is
explicitly out of scope.

### 6. Add-new = human-producer path

The reviewer can add a concept/entity the extractor missed (title, type, aliases, description).
The POST performs **producer-side writes immediately** — the same class of mutation as
`extract_concepts`, which was never governance-gated; promotion remains apply-gated:

- Upsert the candidate node (`status: candidate`), a `mentions` edge from the current source with
  `asserted_by: human`, and render the candidate page.
- File the `promote_candidate_node` item and record it **approved** (`decided_by: human`) in the
  same operation — the add IS the approval; "approve that I added it" double-entry was rejected.
  The next `POST /reviews/apply` promotes it through the normal executor.
- **Purpose-named audit entry** in addition to the approval audit:
  `audit_log/<review_id>-human-added-<hex>.json` (actor, source_id, node_id/type,
  title/aliases/description, created-vs-reused node, edge identity, promote-item resolution
  reference) — precedent: the `-withdrawn-`/`-merged-` entries.
- **Anchorless mention, stated posture:** the human mention carries NO evidence anchor in v1. This
  is not a schema exception — evidence fields are nullable (`graph.py` edges schema),
  `validate_graph` rejects only half-specified ranges, the LLM path already writes anchorless
  mentions on `locate_quote` miss, and `"human"` is already in `ASSERTED_BY`. No quote/span is
  claimed anywhere: Source/Concept renderers project mentions as wikilink lists only and never
  pretend evidence text exists (claims remain the only quote-rendering surface, untouched).
  Free-text quote-to-locate is deferred.
- **Duplicate/identity routing:** same node id already `candidate` → add the human mention and
  approve/reuse its existing promote item; already `active` → add the mention only, no promote
  item; entity-family subtype collision → route to `change_entity_subtype`; concept↔entity
  cross-type conflicts stay deferred — never guessed.
- **Terminal rejected slot (review round):** if the candidate's `promote_candidate_node` item is
  already **rejected**, the add is **blocked with a message** naming the prior rejection (who,
  when) and pointing at the explicit ADR-0045 reopen path. A rejected promotion is a human
  governance record: it is never silently reused, reopened as a side effect, or bypassed via a
  parallel review subject (which would break `(type, subject)` idempotence).

### 7. Surfaces

New UI routes as thin orchestration over shared read-model/service functions (ADR-0035 A8 posture:
hand-rolled HTML via `review_html.py` renderers, `_h()` escaping everywhere, POST-only + PRG,
`assert_safe_bind`, no JS, errors as HTML):

- `GET /ui/reviews/sources` — the source index (decision 3).
- `GET /ui/reviews/sources/{source_id}` — the per-source screen (decisions 1, 2, 4, 5).
- `POST /ui/reviews/sources/{source_id}/decide` — batch decide (decision 4).
- `POST /ui/reviews/sources/{source_id}/add` — human-add (decision 6).

JSON surface change: the existing `POST /reviews/{id}/approve` gains the optional `amendments`
payload (promote only). Attribution/projection logic lives in `review_read.py` beside the
projector registry, sharing its primitives; nothing in the HTML layer becomes authority.

## Tests (implementation slice)

- Human-add matrix: new candidate / existing candidate (mention + reuse) / already-active
  (mention only, no promote item) / subtype collision routes to `change_entity_subtype` /
  cross-type conflict deferred (no write) / terminal REJECTED promote slot blocks with the
  prior-rejection message (no reuse, no reopen side effect, no parallel subject). Audit entry
  contents pinned.
- Validators green over anchorless human mentions (`validate_graph`, projection, wiki).
- Batch submit: partial decisions recorded, untouched items stay pending, already-decided rows
  skip-with-reason, no 409 abort; per-item results rendered.
- Amendments: title change keeps the frozen id; slug/page move owned by the promote executor;
  old title appended to aliases; description survives `_recompose_node` re-extraction; defer
  round-trips `draft_amendments`; reject discards them; executors never read drafts.
- Attribution: multi-source candidate appears under each mentioning source and renders
  read-only-decided after the first decision; retired-section predicate — single-source
  deterministic shown, `|H| > 1` and `|H| == 0` stay flat-only.
- XSS/untrusted-text fixtures for source titles, candidate names, aliases, descriptions, and
  review payloads across all new pages (A8 invariant).

## Deferred (named)

- Guarded sweep shortcut (constraints pinned in decision 4).
- Biggest-queue-first as an optional index sort.
- Rename of ACTIVE nodes (amendments operate pre-promotion only; the ADR-0017 rename executor
  remains design-locked-but-unimplemented).
- Concept↔entity cross-type identity moves (identity-surgery family).
- Free-text quote-to-locate evidence for human-added mentions.
- Extractor alias resolution (the decision-5 alias-divergence hazard's root fix).
