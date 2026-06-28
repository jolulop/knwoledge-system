# ADR-0047 — Unhide: governed effect-reversal of a live hide (hidden → active)

**Status:** Accepted. Design-locked **and implemented** 2026-06-28. `retention.apply_unhidden_sources`
(via `_apply_source_status_transition` generalized with a `from_status` param) + `deprecations`'
`_apply_semantic_visibility_transition` shared core with `apply_unhidden_semantic_pages`;
`review_read._effect_unhide_content`/`_effect_unhide_semantic` + previews (semantic `partial_unhide_state`;
source manifest-authority); `unhide_content`/`unhide_semantic_page` in vocab + `review.yaml` +
`EXECUTOR_BY_TYPE`; `run_apply` wiring (`_APPLY_TYPES`/`_GRAPH_REQUIRED_TYPES`, `unhidden`/`semantic_unhidden`
summary, the `unhide_discovery_restoration_not_guaranteed` non-clean posture). Covered by
`tests/test_unhide.py`.
Adds the **inverse** of the ADR-0043/0046 hides: a human-approved `unhide_content` (source) /
`unhide_semantic_page` (concept/entity-family) review item reverses an **already-live** hide
(`hidden → active`) through apply — restoring default discovery + answer-eligibility — reusing the hide
executors, the `hidden`/`active` statuses, and the projector/dry-run/reopen machinery. Non-rekeying; no
edge surgery; raw never touched.
**Extends:** ADR-0043 (source hide + `_apply_source_status_transition`), ADR-0046 (semantic hide +
`recompose_semantic_node_page` + the `partial_hide_state` reopen-safety), ADR-0045 (reopen — kept distinct
below), ADR-0040 (dry-run), ADR-0030/0022 (page-frontmatter node-status authority). Read
`app/workers/retention.py` (`_apply_source_status_transition`), `app/workers/deprecations.py`
(`apply_hidden_semantic_pages`), `app/backend/review_read.py` (`_effect_hide_semantic`, the
`effect_status` vocabulary + `reopen_block_reason`), `app/backend/main.py` (`run_apply` wiring + reindex
posture).

## Context — unhide is NOT reopen

ADR-0045 **reopen** reverts a review decision **that was never applied** (effect_status `PENDING_APPLY`):
it moves a terminal item back to `pending`, orphaning nothing because no effect is live. It **explicitly
refuses** an `EFFECTED` decision — undoing a *live* effect was deferred. **Unhide is exactly that deferred
case for hides:** the hide is live (`EFFECTED`), so we do **not** rewind the decision — we file a **new,
human-approved review item** that performs the **inverse lifecycle transition** (`hidden → active`) via
apply. Reopen rewinds an unapplied *decision*; unhide is a fresh *governance action* against a live
*status*. (They compose cleanly — see decision 6.)

## Decisions

**1. Separate `unhide_content` / `unhide_semantic_page` review types** (not bidirectional hide types).
"Hide" and "unhide" are opposite governance actions, so each gets its own single-direction type: clean
guards (hide is active-only, unhide is hidden-only — no ambiguous "hide … to_status=active"), an
unambiguous projector (hide `EFFECTED` = hidden, unhide `EFFECTED` = active, no `to_status` branching),
self-documenting audit/UI ("what was hidden" vs "what was restored"), and a producer can't propose
restoration through a suppression type. `proposal.to_status: active` stays as a **guard** value; the review
**type** encodes intent.

**2. Scope v1 = the two surfaces hide already ships:** **sources** (`unhide_content`) + the
**concept/entity-family** semantic pages `concept/entity/person/organization/project`
(`unhide_semantic_page`). **claim/synthesis unhide waits until claim/synthesis HIDE exists** (their distinct
`recompose_claim` / synthesis-executor seams), so hide+unhide land together per surface.

**3. Authority/posture mirrors each hide surface.**
- **Source unhide** = **manifest authority** (`manifests.set_status(active)` — already "un-archives",
  reversible) + best-effort graph source-node mirror; **NOT graph-required**. Reuses
  `_apply_source_status_transition`, **generalized with a `from_status` param** (`active → <to>` for
  archive/hide; `hidden → active` for unhide) so only a **hidden** source transitions — the inverse of the
  current active-only guard. A minor extension, not a rebuild.
- **Semantic unhide** = page frontmatter (authoritative) + graph node (mirror) via
  `recompose_semantic_node_page(status="active", review_status="none")`; **GRAPH-REQUIRED** (graph absent /
  node missing → block + 503; no page-only partial unhide). Joins `_GRAPH_REQUIRED_TYPES`.

**4. Render `review_status: "none"` — restore the clean default active state.** A normal, never-hidden
active page is `status: active, review_status: none`; unhide restores exactly that, so "active" stays
**uniform** (no second "approved-active" flavor that would diverge from never-hidden pages and confuse
validators/consumers). The human-approval trail lives in the **review ledger + audit** (the approved
`unhide_*` item), not a frontmatter marker. Accordingly the unhide projector's `EFFECTED` keys on
**`status == active`** (page + graph, + manifest for source) — `review_status` is **not** in the condition
(active's default is `none`; no governance marker is needed on the default state).

**5. Effect = restore discovery; reindex-failure is non-clean (inverse warning); `index.md` re-annotated.**
`hidden → active` restores the page/source to default `/search` navigation + the `/search` graph channel
(the `RETENTION_DEFAULT_STATUSES` filter admits `active`) and flips `answer_eligible` back true. Because
that restoration is delivered by the keyword/nav index, an unhide that **applies while `reindex_keyword`
fails** is **non-clean**: status `validation_failed` + warning **`unhide_discovery_restoration_not_guaranteed`**
(live + dry-run). The risk is the **inverse** of a hide's: not a leak — the page/source **is** active on
disk (authority correct) — but a **stale index keeps it hidden from discovery** until reindex succeeds, so
the operator shouldn't read "applied" and assume it's discoverable again. The static `wiki/index.md`
catalog keeps the page listed (it always did) but now **drops the `hidden` annotation** (it is `active`);
an `index.md` *rebuild* failure stays warning-only.

**6. Projector/reopen-safety = the exact inverse of `partial_hide_state` (ADR-0046).** The unhide projector
(`_effect_unhide_*`) classifies by how much of the un-hide is **live**:
- **`EFFECTED`** — fully active (page **and** graph active, + manifest active for source): the unhide is
  live (re-hide territory — reopen blocked).
- **`PENDING_APPLY`** — still fully **hidden** (the unhide has not been applied). It is **reopenable under
  the ADR-0045 gate precisely because no restoration effect is live** — and reopening *withdraws the pending
  unhide decision* (the item returns to `pending`); it does **not** undo or re-apply the prior hide. The
  original hide was already applied and **stays applied**: the subject simply remains `hidden`, its current
  state, with one fewer pending decision against it. (Reopen never touches a live effect; that is the whole
  ADR-0045 invariant.)
- **`UNKNOWN` (`partial_unhide_state`)** — **semantic only:** a **partial live unhide** where page XOR
  graph is `active` (the genuine two-authority split — page frontmatter and graph node are co-authorities).
  Part of the restoration is live, so it is **NOT** plain `PENDING_APPLY` (reopen would clear the unhide
  decision while leaving a partially-active node). `UNKNOWN` blocks reopen ("repair the read model first").
- **`UNKNOWN`** — graph absent / node missing; **`NO_EFFECT_REQUIRED`** — a rejected unhide.

**Source correction (mirrors `_effect_hide_content`):** for **sources** the **manifest is the single
authority** (the Source page is a *projection*, not a co-authority), so there is **no** source
`partial_unhide_state` — `_effect_unhide_content` is `EFFECTED` iff the **manifest** is `active`,
`PENDING_APPLY` iff hidden, and a stale page is a **`page_manifest_drift` warning**, not `UNKNOWN`. (The
ADR's earlier "source manifest active but page hidden = partial" framing was over-generalized; sources
follow the shipped manifest-authority model.) This is why decision 1's separate types matter: each unhide
projector is the clean mirror image of its hide counterpart, with no `to_status` branching.

**7. Guards + producer.** **Hidden-only:** unhide applies only to a currently-**hidden** subject; a
non-hidden subject is a typed **`source_not_hidden`** (source) / **`node_not_hidden`** (semantic) skip/no-op
(the inverse of hide's `node_not_active`). A **rejected** unhide applies nothing. Semantic unhide reuses the
hide canonical-page guard + scope-dir gating verbatim. Dry-run graph-unavailable posture: semantic
**blocked/503** (graph-required), source not graph-required. v1 has **no auto-producer** — unhide items are
human-initiated governance proposals (CLAUDE.md rule 9), like hide.

## Consequences

Hiding becomes a **reversible** lifecycle on both surfaces it ships for, closing the "hidden is a one-way
trip except by hand" gap, with a governed, audited, reopen-safe `hidden → active` that reuses the source
status-transition helper (one `from_status` param), the semantic recompose seam, the `hidden`/`active`
statuses, and the projector/dry-run/reopen machinery wholesale. Costs: the two `unhide_*` types + their
projectors/previews, the `from_status` generalization, an `apply_unhidden_semantic_pages` parameterization,
the `run_apply` wiring (`_APPLY_TYPES`, `_GRAPH_REQUIRED_TYPES`, summary, the new reindex warning), and
tests. Deferred: claim + synthesis unhide (await their hide), and all identity surgery.

## Tests (design intent; written at implementation)

- Source: approved `unhide_content` for a **hidden** source → manifest `active`, Source page mirror active,
  excluded-no-more from default `/search`; a non-hidden source → `source_not_hidden` skip; not
  graph-required (no graph → still applies).
- Semantic: approved `unhide_semantic_page` for a **hidden** concept → page `status: active` +
  `review_status: none` **and** graph node `active`; re-enters default `/search` navigation + graph channel;
  `answer_eligible` back true. Graph absent / node missing → block + 503. A non-hidden node →
  `node_not_hidden` skip; rejected → no-op.
- Projector/reopen: fully active → `EFFECTED` (reopen blocked); fully hidden → `PENDING_APPLY` (reopen
  allowed); a **semantic** partial (page XOR graph active) → `UNKNOWN partial_unhide_state` → reopen
  **409**, no mutation. **Source:** `EFFECTED` iff manifest active (page drift is a `page_manifest_drift`
  warning, never `UNKNOWN`); a non-hidden, non-active source → `source_not_hidden` skip.
- Reindex: an applied unhide + failed `reindex_keyword` → `validation_failed` +
  `unhide_discovery_restoration_not_guaranteed` (live + dry-run); a graph-only completion still triggers
  reindex. `index.md` lists the page annotated `active` (no `hidden`).
- Idempotent re-apply of an already-active subject is a no-op; hide behavior is unchanged.
