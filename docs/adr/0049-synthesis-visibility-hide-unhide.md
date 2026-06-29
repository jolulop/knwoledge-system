# ADR-0049 — Synthesis visibility: hide_synthesis / unhide_synthesis without graph surgery

**Status:** Accepted. Design-locked **and implemented** 2026-06-28. The three `generate_syntheses`
preservation guards (retraction loop / `apply_resolved_syntheses` / normal-regen gate);
`synthesis._apply_synthesis_visibility_transition` + `apply_hidden_syntheses`/`apply_unhidden_syntheses`
(artifact-sourced re-render via `_render_page`, page+graph partial-state typed skips); the
`render_synthesis_page` `hidden` label/banner; `review_read` `_effect_hide_synthesis`/
`_effect_unhide_synthesis` + previews + `EXECUTOR_BY_TYPE`/`_PROJECTORS`; `hide_synthesis`/
`unhide_synthesis` vocab (`reviews.REVIEW_TYPES`, `policies/review.yaml`) + `run_apply` wiring (summary
`synthesis_hidden`/`synthesis_unhidden`, the two reindex warnings, graph-required auto). **Review-round
hardening:** the untrusted-page `topic_node` binding (decision 6 — `synthesis_id(topic_node) == nid` +
artifact `node_id` match) and the hidden-claim fan-out (decision 9 — `_render_page`/`validate_projection`
filter + `rerender_synthesis_page` + the claim executor's `affected_syntheses`). **Review round 2:** the
generate-pass guards key on the authoritative **page** status (not just the graph mirror), skip-only
(decision 3), and `validate_projection` gained hidden-claim parity on the older ADR-0048 Source-page-Claims
+ Claim-contradicts surfaces (decision 9). **Review round 3:** synthesis prose is a discovery surface, so a
new node status **`evidence_hidden`** auto-suppresses an active synthesis materially derived from a hidden
claim (decision 10 — distinct from operator `hidden`, `active ⇄ evidence_hidden` by the claim fan-out,
operator hide wins, generate-pass-preserved, audited in the apply summary). **Review round 4:** `run_apply`
ordering fixed so explicit `hide_synthesis`/`unhide_synthesis` run **before** the claim → synthesis fan-out
(the final reconciliation) — so operator `hidden` wins in a mixed batch; an unreconcilable fan-out (missing
artifact) is non-clean + audited. **Review round 5:** repair-then-rerun — the claim executor recomputes
`affected_syntheses` even for fully-effected claims so a failed fan-out is retried after repair, and
`_render_page`/`rerender_synthesis_page` are change-detecting so steady-state applies don't churn. **Review
round 6:** the fan-out computes its precedence target from the **authoritative page status** (not the graph
mirror — a page-hidden/graph-active partial state isn't downgraded), and a graph-mirror-only repair still
triggers reindex (`_render_page` returns changed on a page-OR-mirror change; non-clean if that reindex
fails). Covered by `tests/test_synthesis_visibility.py` (45 tests) + `tests/test_claim_visibility.py`
projection tests. **Known follow-up (non-blocking):** the repair-then-rerun retry covers the synthesis
fan-out but not the ADR-0048 Source-page Claims / contradiction-partner re-render (a fully-effected claim
doesn't re-collect `affected_sources`/partners) — the same failure class, deferred because those surfaces
have no artifact-missing failure mode and `generate_wiki` isn't yet change-detecting (collecting them for
effected claims would churn). Completes the reversible visibility lifecycle on the
**last** generated surface — **synthesis pages** (the distinct `synthesis.py` / `render_synthesis_page`
seam): `hide_synthesis` (active → hidden) and `unhide_synthesis` (hidden → active) land **together**. Visibility-only — **no edge
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

Each guard keys on **`hidden` from the authoritative PAGE frontmatter *or* the graph mirror** (review fix):
the page is the authority (decision 2), so a **page-hidden / graph-active partial state** must also be
preserved — checking only the graph mirror would let the generator clobber a hidden page. The guards are
**skip-only**: they never *repair* the mirror (e.g. flip the active graph node to `hidden`) — drift repair
belongs to the visibility executor (typed `partial_*_state` skips) and the validators, not the generator.
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

**Untrusted-page boundary (review hardening).** The artifact is keyed by `topic_node`, read from the
**untrusted** synthesis page frontmatter (a derived on-disk file; CLAUDE.md rule 2, ADR-0009). A tampered
page could point `topic_node` at **another** topic's valid in-directory artifact and re-render *this*
synthesis (same `syn_id` + graph edges) with the wrong title/summary/prose. So the executor (and the
`rerender_synthesis_page` fan-out helper) **bind** `topic_node` to the node: `synthesis_id(topic_node)` (the
deterministic one-per-topic id hash, ADR-0021) **must equal** `nid` — else a typed
**`synthesis_topic_mismatch`** skip — and the loaded artifact's own `node_id` must equal `nid`
(defence-in-depth) — else **`synthesis_artifact_mismatch`**. Both are typed skips, never a silent mis-render.

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

**9. Hidden-claim suppression on synthesis Supporting Evidence (claim → synthesis fan-out, review fix).**
A synthesis page's **Supporting Evidence** (`[[Claims/…]]` links) + its `derived_from` frontmatter are
**default-discovery surfaces** on a browsable page — the same kind ADR-0048 filters on Source pages and
contradiction sections. So a **hidden claim** is **omitted** from them (in `_render_page`, **uniformly** —
active *and* hidden synthesis pages filter), while the `derived_from` **edge stays active** in the graph
(SoT — raw `/graph/*` still shows the syn → hidden-claim edge). Two consequences: (a) **`validate_projection`**
expects the rendered links/frontmatter to match active `derived_from` edges to **non-hidden** claim nodes;
(b) **claim hide/unhide fans out to syntheses** — the ADR-0048 claim executor now also returns
`affected_syntheses` (active `derived_from` *incoming* from a synthesis), and `run_apply` re-renders each via
`synthesis.rerender_synthesis_page` (at the synthesis's **current** status — no status change) so the
now-hidden claim drops (and a re-admitted claim restores). This closes a discovery route that would
otherwise let an active, answer-eligible synthesis page link straight to a hidden claim, and **extends
ADR-0048's claim-hide fan-out** (Source pages + contradiction partners) to syntheses.

**`validate_projection` parity on the older ADR-0048 surfaces (review fix).** The same review surfaced a
**pre-existing** gap: the ADR-0048 renderers already omit hidden claims from the **Source-page Claims**
section (`wiki.py`) and the **Claim-page Contradicting-Claims** section (`claims.py`), but
`validate_projection` still expected those surfaces to mirror **all** active edges — so a real vault with a
hidden claim and existing Source/partner pages would **validate-fail after a correct apply** (the minimal
ADR-0048 test vaults dodged it by having no such pages). `validate_projection` now expects active edges to
**non-hidden** claim endpoints on **all three** surfaces (Source Claims, Claim contradicts, Synthesis
Supporting Evidence), matching the renderers.

**10. Synthesis prose is a discovery surface — `evidence_hidden` auto-suppression (review fix).** Decision 9
removes a hidden claim's *link* from a synthesis, but the `## Synthesis` **prose** is LLM-derived *from* that
claim and the page stays `active` + answer-eligible — a residual discovery route to hidden-claim content
through aggregation. So **an active synthesis materially derived from a hidden claim is suppressed from
default discovery** via a new node status **`evidence_hidden`** (∉ `RETENTION_DEFAULT_STATUSES` → out of
nav + graph channel + answer-eligibility for free; `answer_eligible` is false because the status isn't
`active`). It is a **derived, deterministic, key-free** condition — no prose regeneration (which can't be
done deterministically); the edges, prose, and artifact stay intact for raw inspection, and the page renders
a distinct banner **"Synthesis suppressed — supporting evidence hidden"**.

- **Distinct from operator `hidden`.** `hidden` means an operator ran `hide_synthesis`; `evidence_hidden`
  means a supporting claim is hidden. Separate statuses → **clean, legible restoration rules** (no overloaded
  cause-flag for validators/projectors/search/reopen to interpret): an operator hide is reversed only by
  `unhide_synthesis`; `evidence_hidden` is reversed by claim **unhide** when all supporting evidence is
  visible again. Both are queryable (`/search?node_status=evidence_hidden`, `wiki/index.md`, raw `/graph/*`).
- **Precedence (recomputed by the claim → synthesis fan-out, `rerender_synthesis_page`):** operator
  `hidden` **wins** (stays hidden — a claim unhide never auto-restores it) > `evidence_hidden` (any active
  `derived_from` claim is hidden) > the evidence-derived status. Only **`active`** is suppressed (candidates
  / deprecated tombstones aren't default-discoverable, so there's nothing to leak) → the transition is
  **`active ⇄ evidence_hidden`**, restored to `active` only when **no** supporting claim remains hidden.
- **Operator unhide with hidden evidence.** `unhide_synthesis` clears the operator hide but restores to
  `evidence_hidden` (not `active`) if a supporting claim is still hidden — the evidence suppression outlives
  the operator hide.
- **Generate pass preserves it** (skip-only, like `hidden`): a `generate_syntheses` pass never promotes /
  retracts / regenerates an `evidence_hidden` synthesis (`_PRESERVED_GENERATE_STATUSES`).
- **Apply order (review fix).** Within one `run_apply` batch the steps run: claim hide/unhide (flip claim
  status) → **explicit `hide_synthesis`/`unhide_synthesis`** (operator intent, applied to a still-`active`
  synthesis) → **claim → synthesis fan-out as the FINAL reconciliation**. This ordering is what makes
  *operator hidden win*: if a batch hides a claim **and** operator-hides a citing synthesis, the operator
  hide lands first (synthesis `active → hidden`), then the fan-out preserves `hidden`. The earlier order
  (fan-out before the synthesis executors) wrongly left it `evidence_hidden` (the operator hide then skipped
  it as `synthesis_not_active`).
- **Reconciliation not guaranteed (review fix).** If the fan-out can't re-render an affected synthesis (page
  missing/unbindable, artifact gone — `rerender_synthesis_page` returns `None`), that synthesis may still
  surface the hidden claim's content (stale prose + link, still `active`), so the apply is **non-clean**
  (`validation_failed` + **`synthesis_evidence_suppression_not_guaranteed`**) and the count is surfaced as
  `synthesis_evidence.unreconciled` (mirrors the reindex-stale "suppression not guaranteed" posture).
- **Repair-then-rerun (review fix).** The claim executor collects `affected_syntheses` on **every** apply of
  an approved hide/unhide item — **including a fully-effected (no-op) claim** (only the `claim_not_active`
  skip is excluded) — so a fan-out that failed once is **retried** after the operator repairs the
  page/artifact (the next apply reconciles the synthesis and the warning clears). To avoid steady-state
  churn from re-reconciling effected claims every apply, `_render_page`/`rerender_synthesis_page` are
  **change-detecting**: the byte-stable page is rewritten only when its content differs and the graph
  node-status mirror is upserted only when it differs — an already-current synthesis is a true no-op.
- **Page authority in the fan-out (review fix).** `rerender_synthesis_page` computes the precedence target
  from the **authoritative PAGE status**, NOT the (possibly stale) graph mirror — so a page-hidden /
  graph-active partial state keeps `hidden` (operator wins, never downgraded to `evidence_hidden`), and
  `_render_page` then repairs the graph mirror to match the page. Because a graph-mirror-only repair changes
  no page bytes, `_render_page` returns **True when the page OR the graph mirror changed** (not just on a
  page write), and `run_apply` counts it (`synthesis_fanout_work`) so the reindex still runs; a fan-out
  change (page or mirror) whose reindex **fails** is non-clean (`synthesis_evidence_suppression_not_guaranteed`).
- **Audited:** `run_apply` reports `synthesis_evidence: {suppressed, restored, unreconciled}` in the apply
  summary (the automatic consequence of a human-approved claim hide/unhide).
- **v1 limit:** `hide_synthesis` stays active-only, so an already-`evidence_hidden` synthesis isn't directly
  operator-hideable (it's already suppressed); an operator hides it after its evidence is restored.

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
- **Preservation — page-authoritative partial state:** a **page-hidden / graph-active** synthesis survives
  all three clobber paths (rejected proposal, retraction, regen) — the page stays `hidden`, the graph mirror
  is left active (skip-only, no repair).
- **`validate_projection` parity:** a real vault passes when a hidden claim is omitted from a Source page
  **and** from a partner Claim page (the older ADR-0048 surfaces), matching the renderers.
- **`evidence_hidden` suppression:** hiding a supporting claim flips an active synthesis to `evidence_hidden`
  (suppressed banner; dropped link; edge kept) + drops it from the nav row / answer-eligibility; a claim
  unhide restores `active` only when **no** supporting claim remains hidden; an operator-`hidden` synthesis
  is **not** auto-restored; an operator unhide with hidden evidence lands on `evidence_hidden`; the generate
  pass preserves it; `validate_projection` passes; the apply summary audits `synthesis_evidence`.
- **Apply order:** a single batch with `hide_claim` + `hide_synthesis` on a citing synthesis ends `hidden`
  (operator wins, `synthesis_hidden.applied == 1`); `hide_claim` + `unhide_synthesis` on an operator-hidden
  synthesis ends `evidence_hidden`; a fan-out that can't re-render (missing artifact) is `validation_failed`
  + `synthesis_evidence_suppression_not_guaranteed` + `synthesis_evidence.unreconciled == 1`.
- **Repair-then-rerun:** apply 1 with a missing artifact → unreconciled/non-clean; restore the artifact and
  re-apply (claim hide still approved) → the synthesis becomes `evidence_hidden`, the link is removed, the
  warning clears; a third steady-state apply is clean and triggers **no** synthesis re-render / reindex.
- **Page authority + mirror repair:** a page-hidden / graph-active synthesis + a claim hide stays `hidden`
  (not `evidence_hidden`) with the mirror repaired; a page-`evidence_hidden` / graph-active drift repairs the
  mirror **and** triggers reindex, and is non-clean if that reindex fails.
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
- **Untrusted-page binding (security):** a tampered `topic_node` pointing at another existing topic/artifact
  → typed `synthesis_topic_mismatch` skip (page is **not** re-rendered with the mismatched prose); an
  artifact whose `node_id` ≠ `nid` → `synthesis_artifact_mismatch`.
- **Hidden-claim fan-out:** a hidden claim cited by a synthesis is **omitted** from that synthesis's
  Supporting Evidence links + `derived_from` frontmatter (active *and* hidden synthesis pages filter), the
  `derived_from` edge stays active (raw `/graph/*`), and a claim hide/unhide **re-renders the citing
  syntheses** (the claim executor's `affected_syntheses` → `run_apply` → `rerender_synthesis_page`); a
  visible co-cited claim is retained; `validate_projection` matches links to active non-hidden claims.
- **Graph-required:** graph absent / node missing → block + 503; dry-run blocked. **Reindex failure** →
  non-clean + `synthesis_hide_discovery_suppression_not_guaranteed` /
  `synthesis_unhide_discovery_restoration_not_guaranteed` (live + dry-run); a graph-only completion still
  reindexes; `wiki/index.md` keeps the synthesis listed annotated `hidden`.
