# ADR-0049 — Synthesis visibility: hide_synthesis / unhide_synthesis without graph surgery

**Status:** Accepted. Design-locked 2026-06-28 (grill-phase; **docs-only — no code yet**). Implementation
awaits a separate "implement now". Completes the reversible visibility lifecycle on the **last** generated
surface — **synthesis pages** (the distinct `synthesis.py` / `render_synthesis_page` seam): `hide_synthesis`
(active → hidden) and `unhide_synthesis` (hidden → active) land **together**. Visibility-only — **no edge
deletion** (graph is SoT for `derived_from`/`related_to` edges; ADR-0030). After this, the visibility family
is symmetric on every surface: sources (`hide_content`⇄`unhide_content`), concept/entity family
(`hide_semantic_page`⇄`unhide_semantic_page`), claims (`hide_claim`⇄`unhide_claim`), and now synthesis.
**Extends:** ADR-0046 (semantic hide, the inspection-vs-discovery split, `partial_hide_state`, the
stricter reindex posture), ADR-0047 (unhide, `from_status`, `partial_unhide_state`), ADR-0048 (the
hidden-ness-keyed projector + per-surface reindex warnings), ADR-0031 (synthesis generation/promotion/
retraction governance + the `_render_page` seam), ADR-0034 (`/query` cites source chunks, not pages),
ADR-0045 (reopen safety), ADR-0043 (the `hidden` status). Read `app/workers/synthesis.py`
(`generate_syntheses` — the **three** clobber sites; `apply_resolved_syntheses`; `_render_page`),
`app/workers/wiki_render.py` (`render_synthesis_page` — the `label` dict has no `hidden` key today),
`app/backend/main.py` (`run_apply` — already wires `synthesis_dir`/`enrichment_dir` + calls
`apply_resolved_syntheses`), `app/backend/eligibility.py` (`ANSWER_ELIGIBLE_TYPES` includes `synthesis`),
`app/backend/search.py` (`RETENTION_DEFAULT_STATUSES = ("active","deprecated_candidate")` — `candidate`
and `hidden` are already excluded).

## Context — why synthesis is neither a concept nor a claim

Synthesis differs from the two surfaces already governed:

1. **It has a promotion lifecycle, not an evidence-derived status.** A synthesis is born `candidate` (a
   `propose_synthesis` review), becomes `active` only when a human **approves** that review
   (`apply_resolved_syntheses` renders `("approved","active","approved")`), or `deprecated_candidate` on
   reject / when its topic drops below eligibility (audited retraction). So — unlike a **claim** (status
   *derived* from evidence) and unlike a **concept** (always `active`) — a normal **active synthesis carries
   `review_status: approved`, not `none`** (it was approved into existence). This is the one place the
   synthesis lifecycle changes the hide/unhide metadata.

2. **No rendered page links *to* a synthesis — so there is no partner re-render fan-out.** The claim crux
   (ADR-0048 §4: a hidden claim must be omitted from partner claims' "Contradicting Claims" sections, with a
   `contradiction_affected` re-render) has **no synthesis analog**. Nothing renders `[[Synthesis/…]]`: the
   concept→synthesis `related_to` edge is explicitly **not projected** on the concept page (ADR-0031 v1
   limit, `synthesis.py` module docstring). A synthesis page renders only **forward** links (its own
   Supporting Evidence → claims, Disagreements). Hiding a synthesis is therefore **self-contained**: the
   synthesis page + its graph node + the retrieval surfaces — no other page changes.

3. **The synthesis-specific crux is preservation across the generate pass.** Because a synthesis is produced
   and re-derived by `generate_syntheses`, **three** code paths there would silently clobber a `hidden`
   status (the analog of the claim "hidden survives `recompose_claim`" pin, but **three** sites, not one):
   the **retraction loop** (skips only `deprecated_candidate` → would retract a hidden synthesis whose topic
   lost eligibility), **`apply_resolved_syntheses`** (re-renders an approved proposal's node to `active`
   unless already active → could flip `hidden`→`active`), and the **normal-regeneration gate** (keys on
   `active`/`candidate`/rejected; `hidden` falls through all branches → it would call the LLM and reset the
   node to `candidate`).

## Decisions

**1. Scope — `hide_synthesis` + `unhide_synthesis` together, synthesis pages only; active-only ⇄ active.**
Two new review types (their own seam + lifecycle), built on new `apply_hidden_syntheses` /
`apply_unhidden_syntheses` executors. **Hide is active-only**: `active → hidden`; a `candidate` or
`deprecated_candidate` synthesis is a typed **`synthesis_not_active`** skip, never a mutation. Rationale: a
`candidate` is **already non-discoverable** (`candidate ∉ RETENTION_DEFAULT_STATUSES` — absent from default
`/search`/nav/graph) and its lifecycle is governed by the `propose_synthesis` review (approve, or reject →
`deprecated_candidate`); hide must **not** become a second disposal path for an unaccepted candidate. With
hide active-only, the inverse is clean: **`unhide_synthesis: hidden → active`**.

**2. Authority + status metadata.** Synthesis **page** frontmatter is authoritative, the graph node mirrored,
both written by `_render_page`; **GRAPH-REQUIRED** (like semantic pages and claims — graph absent / node
missing → block + 503). The page is re-rendered, never hand-patched (it stays a pure projection of
graph + artifact; the trailing `input_fingerprint` stays valid).
- **Hide** → `status: hidden` + `review_status: approved` (which *matches* the prior active-synthesis
  `review_status`, so the review-status is consistent across the flip).
- **Unhide** → `status: active` + **`review_status: approved`**. **This is a deliberate, synthesis-specific
  divergence from ADR-0047/0048, not drift.** Concept/entity unhide (ADR-0047) and claim unhide (ADR-0048)
  restore `review_status: none` because a never-hidden *active* concept/claim carries `none`. A never-hidden
  *active synthesis* carries `review_status: approved` (it only reaches `active` by a human approving its
  `propose_synthesis` — `apply_resolved_syntheses` renders `("approved","active","approved")` at
  `synthesis.py:263`). So restoring `approved` makes an unhidden synthesis **identical to a normally-promoted
  one** — the same uniformity principle ADR-0047 applied with `none`, just over the synthesis convention. A
  restored `none` would be the anomaly here (an `active` synthesis that looks un-promoted). **Blind active**,
  key-free: unhide does **not** recompute eligibility at apply time. If the topic lost eligibility while
  hidden, the **next `generate_syntheses` pass** retracts it through the existing audited path
  (`deprecated_candidate`) — self-correcting, not an apply-time eligibility recompute.

**3. Preservation — hide wins; the generate pass never touches a hidden synthesis.** `hidden` is a
human-governed visibility override; automated generation must not silently replace it with `candidate`,
`active`, or `deprecated_candidate`. The **hide/unhide executors are the only paths in or out of `hidden`.**
Concretely, `generate_syntheses` gains a `hidden` guard at all three clobber sites:
- **Retraction loop:** skip `hidden` exactly as it already skips `deprecated_candidate` — a hidden synthesis
  is **never retracted**. (Retract-through-hidden is **rejected**: `deprecated_candidate ∈
  RETENTION_DEFAULT_STATUSES`, so retracting a hidden synthesis would **re-expose** material the operator
  deliberately hid — it would *un-hide* via a tombstone. Hide freezes eligibility.)
- **`apply_resolved_syntheses`:** skip if the node is `hidden` (a lingering proposal can't flip it active /
  deprecated).
- **Normal-regeneration gate:** `if status == "hidden": continue` — **no LLM call**, no node reset (no model
  spend on deliberately-hidden material; mirrors ADR-0048's claim detection-exclusion principle).

Source-material drift while hidden is reconciled **after unhide** (or by a future explicit regeneration
action), never by auto-generation piercing the hide.

**4. Effect.** A hidden synthesis drops from default `/search` **navigation** + the `/search` **graph
channel** (the `RETENTION_DEFAULT_STATUSES` node-status filter — `hidden` is excluded for free, like every
prior hide) and **loses answer-eligibility** (`answer_eligible` needs `status == active`; synthesis **is**
answer-eligible — stronger than a source). It is **not** in the `/search` **evidence** channel and is **not**
a `/query` citation — both are **source-chunk** evidence keyed by `source_id` (ADR-0034), never synthesis
pages. **Preserved:** the graph node + its `derived_from` (→ claims) and `related_to` (→ topic) edges stay
(graph is SoT — no surgery); raw `/graph/*` still returns the synthesis and its edges with `status: hidden`.
**No cross-page fan-out** (nothing renders `[[Synthesis/…]]`; §Context point 2).

**5. Rendered page — hidden banner, sections kept.** `render_synthesis_page` gains a `hidden` branch: the
`> [!summary]` callout label becomes **"Synthesis hidden — suppressed from default discovery"**, and the
graph-derived **Supporting Evidence** (`[[Claims/…]]`) + **Disagreements** sections **stay rendered** (the
page is the durable inspection record; an operator opening a hidden synthesis still wants its evidence and
disagreements — mirrors the claim hidden render which keeps its evidence table). Hide suppresses *discovery*,
not the *page record*. (A bare suppression notice — dropping the sections — is the redaction/deletion shape,
which is **not** what `hidden` means here.)

**6. Executor home + render-source.** The executors live in `synthesis.py` (reusing `_render_page` + the
synthesis **artifact** `normalized/enrichment/<topic_node_id>.synthesis.json` — title/summary/prose), and are
called from **`run_apply`'s graph block, alongside `apply_resolved_syntheses`** (the governance-executor
family — `run_apply` **already** wires `synthesis_dir` + `enrichment_dir`, so no new plumbing). Render-source
is the **artifact**, exactly like `apply_resolved_syntheses`; a missing artifact is a typed
**`synthesis_artifact_missing`** skip (can't deterministically re-render). Key-free, deterministic,
idempotent; never touches `raw/`; no index rebuild (caller-owned).

**7. Projector / reopen — partial-state safety keyed on hidden-ness** (mirrors ADR-0046/0047/0048):
- **hide** (`_effect_hide_synthesis`): `EFFECTED` = page **and** graph `hidden` (+ `review_status: approved`);
  `PENDING_APPLY` = neither hidden; **partial** (page XOR graph hidden) = `UNKNOWN partial_hide_state`;
  `synthesis_not_active` warning on a non-active target.
- **unhide** (`_effect_unhide_synthesis`): `EFFECTED` = neither page nor graph `hidden`; `PENDING_APPLY` =
  both `hidden`; **partial** = `UNKNOWN partial_unhide_state`. (No `synthesis_not_hidden` warning — a
  non-hidden target *is* the unhide goal → `EFFECTED`/idempotent.)

Partial live states are **not reopenable** (ADR-0045 — part of the change is live; reopening would orphan
it). The **executor** mirrors this: it reads **both** the page frontmatter and the graph node; `page_hidden
!= graph_hidden` is a typed `partial_hide_state` / `partial_unhide_state` skip (**never silent**); both at
target with a lagging `review_status` → `normalized`. `hide_synthesis`/`unhide_synthesis` join
`REVIEW_TYPES`, `policies/review.yaml` `requires_human_approval`, `EXECUTOR_BY_TYPE`, the dry-run previews,
and `_GRAPH_REQUIRED_TYPES` (graph-required is automatic).

**8. Reindex-failure is non-clean, with synthesis-specific warnings.** An applied hide/unhide whose
keyword/nav reindex failed → apply `validation_failed` (live + dry-run) +
**`synthesis_hide_discovery_suppression_not_guaranteed`** / **`synthesis_unhide_discovery_restoration_not_guaranteed`**
(distinct from the source/semantic/claim warnings — per-surface naming so an operator knows *which*
visibility executor caused the non-clean apply; synthesis is an answer-eligible discovery surface). The
mutation **remains written**; only the cleanliness signal flips. A **graph-only completion** still triggers
reindex (`applied + normalized`, the ADR-0046 hardening). `wiki/index.md` keeps the synthesis listed,
annotated `hidden` (or `active` on unhide).

## Consequences

The synthesis lifecycle gains reversible, audited, reopen-safe visibility governance reusing the
review-ledger + hide/unhide + projector/dry-run/reopen + reindex machinery, with **three** synthesis-specific
pieces: (a) the `hidden` guard at the three `generate_syntheses` clobber sites (hide wins; no model spend on
hidden material); (b) the artifact-sourced re-render via `_render_page` from `run_apply` (vs the concept
`recompose_semantic_node_page` seam); (c) the unhide-restores-`review_status: approved` synthesis convention.
Notably **simpler** than claims: **no partner backlink fan-out** (nothing links to a synthesis). After this
slice the visibility family is **complete on every surface**. Deferred: identity surgery (merge/split
rekeying) — the next major design target — and Phase 8 auth/CSRF (until a concrete non-loopback need).

## Tests (design intent; written at implementation)

- **Hide** an active synthesis → page + graph `hidden` + `review_status: approved`; renders the hidden
  banner with Supporting Evidence + Disagreements **kept**; drops from default `/search` **navigation** +
  the **graph channel** + **answer-eligibility** (`answer_eligible` false). Raw `/graph/*` still returns it +
  its `derived_from`/`related_to` edges (`status: hidden`). **No test expects it to leave source-chunk
  evidence** — synthesis is not chunk evidence.
- **Active-only:** hiding a `candidate` or `deprecated_candidate` synthesis → typed `synthesis_not_active`
  skip (no mutation); the candidate is disposed of by rejecting its `propose_synthesis`, not by hide.
- **Preservation — the three clobber sites:** with a synthesis `hidden`, a `generate_syntheses` pass
  (a) does **not** retract it even when its topic is no longer eligible; (b) does **not** promote/deprecate
  it via `apply_resolved_syntheses` from a lingering proposal; (c) does **not** regenerate it (no LLM call,
  status stays `hidden`). The hidden status survives the full pass byte-stable.
- **Unhide** a hidden synthesis → `status: active` + `review_status: approved` (the synthesis convention,
  **not** `none`); restores default `/search` nav + graph-channel discovery + `answer_eligible`. Unhide is
  **blind active** — it does not recompute eligibility; a subsequent generate pass retracts it to
  `deprecated_candidate` **only if** the topic is genuinely ineligible (self-correcting).
- **No fan-out:** hiding a synthesis re-renders **only** the synthesis page (no partner page changes — assert
  no `[[Synthesis/…]]` exists to update).
- **Projector / reopen:** hide partial (page XOR graph hidden) / unhide partial → `UNKNOWN`
  `partial_hide_state` / `partial_unhide_state` → reopen **409**; fully effected → reopen blocked; from-state
  → reopen allowed; `synthesis_not_active` skip on a non-active hide target (a non-hidden unhide target is
  `EFFECTED`/idempotent — no `synthesis_not_hidden` warning).
- **Executor partial-state** is a typed skip, never silent: page-active/graph-hidden (or the inverse) →
  `partial_hide_state`; page-hidden/graph-active → `partial_unhide_state`. Both hidden + `review_status:
  pending` → projector `UNKNOWN`; the executor normalizes it (`normalized` count).
- **Render-source:** a missing synthesis artifact → typed `synthesis_artifact_missing` skip (no partial
  re-render).
- **Graph-required:** graph absent / node missing → block + 503; dry-run blocked. **Reindex failure** →
  non-clean + `synthesis_hide_discovery_suppression_not_guaranteed` /
  `synthesis_unhide_discovery_restoration_not_guaranteed` (live + dry-run); a graph-only completion still
  reindexes; `wiki/index.md` keeps the synthesis listed annotated `hidden`.
