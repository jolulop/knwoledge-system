# Phase 3.5c: graph-blocked contradiction detection and per-concept synthesis, both review-gated

Phase 3.5c is the last and highest-risk enrichment slice (ADR-0028): tier-3 reasoning that
spans multiple sources/claims and proposes **contradictions** and **syntheses** as
human-reviewed items. It builds entirely on the proven 3.5b graph — it adds no new graph
authority, only new producers over it. This ADR fixes the load-bearing decisions that block
the slice; the slicing, plan, and validators are in `docs/Phase 3.5c Plan.md`.

It ships as two ordered, independently-committable sub-slices, risk rising across them and
each tested before the next, matching the ADR-0028 discipline:

- **3.5c-1 — contradiction detection.** Lower risk: it *reuses* existing schema (the
  `contradicts` edge type and its {claim,synthesis}↔{claim,synthesis} endpoint contract,
  ADR-0030) and an existing review type (`resolve_contradiction`, `policies/review.yaml`).
  It lands first and exercises the tier-3 pairing/cost question on a contained surface.
- **3.5c-2 — cross-source synthesis.** Built on the proven pairing machinery: it adds a new
  producer and a new page-rendering surface (synthesis nodes), and a new review type.

## The load-bearing decisions

**1. Candidate-pair generation is graph-neighborhood blocking, not all-pairs.** Contradiction
detection is pairwise over claims; naive all-pairs is O(N²) tier-3 (heavy-model) calls and is
infeasible and expensive even at modest corpus size. Every pass must *block* first — cheaply
narrow to candidate pairs, then spend the heavy model only on those. The 3.5b graph already
carries a free, deterministic blocking signal: claims are connected via `active` `mentions`
edges to the concepts/entities they are about. **Two claims are a candidate pair iff they
share ≥1 `active` concept/entity node and come from two *independent* sources** (the ADR-0018
independence test — a source cannot contradict itself, and same-author/same-family is not a
real disagreement). This costs zero model calls to compute, bounds the pair set naturally,
degrades gracefully (no shared concept → not compared), and reuses logic already built and
tested. Vector/embedding similarity was considered as a second blocking channel and is
deferred: it adds an embedding dependency and a tunable threshold, and graph-neighborhood
blocking is the right v1. If recall proves too low it can be layered in later as a union
channel without changing the representation below.

**2. A detected contradiction is one sorted-pair assertion, not two rows and not a schema
change.** `contradicts` is symmetric, but the `edges` schema (ADR-0030) is directional
(`src_id`/`dst_id`) with a *single* evidence anchor, while a contradiction spans two claims
each with its own citation. The representation:

- **Canonical ordering:** write one assertion with `src_id`/`dst_id` = the two claim ids
  sorted lexically (smaller id = `src`). A-vs-B and B-vs-A therefore collapse to one row and
  one review item, and the `resolve_contradiction` item's `subject` is the same sorted pair,
  so its `review_id` is stable and idempotent across runs.
- **Evidence anchor (advisory only):** the row's single evidence triple is the **`src`
  claim's** primary `active` citation, set deterministically by the ordering, but it is an
  **advisory pointer, not the authoritative evidence**. A `contradicts` relation is inherently
  two-sided and a single edge row cannot represent both sides; the **authoritative evidence
  remains the two Claim pages' structured citations** (ADR-0019/0020). We do **not** extend the
  schema with a second evidence triple — that would be a `user_version` migration touching
  every edge consumer for provenance already reachable through the dst node.
- **The review item carries both sides.** Because the edge row alone is one-sided, the
  `resolve_contradiction` item must carry **both claim ids plus enough rendered context for
  both claims** (each side's text and its active citations) so a human can judge the
  disagreement without re-deriving it. The edge anchor is for graph traversal; the review item
  is the human-facing two-sided record.
- **Status/provenance:** `asserted_by: llm`, `status: proposed`, `confidence` = the model's
  verdict confidence, carrying the `review_id`. A `contradicts` assertion is a semantic
  judgment and is **never `active` on creation** (ADR-0030) — it is invisible until a human
  approves it.
- **Re-run/supersede:** mirroring `claims.py`, stale `contradicts` assertions are superseded
  on re-run, but the trigger differs by *what* changed, because **independence is the criterion
  for *finding* candidates, not a validity condition for a contradiction**:
  - **Endpoint gone** — a claim is no longer an `active` node (tombstoned, or its text changed
    so its content-derived id no longer stands): the relationship has lost an endpoint, so the
    assertion is superseded **whether `proposed` or `active`** — even a human-acknowledged
    contradiction goes, and the surviving claim's page drops the backlink. This is enforced in
    the **claim lifecycle itself** (the claim renderer supersedes contradicts edges touching a
    claim the moment it tombstones, via a shared graph helper — no `contradictions` import into
    `claims`), so the claim CLI leaves the repo valid without a separate contradiction pass; the
    contradiction worker keeps the same check only as a backstop. "Run detection after
    extraction" is deliberately **not** part of the validity contract.
  - **Pair left the candidate set** but both endpoints still stand (e.g. a provenance edit
    removed source independence): only a `proposed` assertion is superseded. A later provenance
    edit must **not** silently retract a human acknowledgement, since independence never bore on
    whether the two claims actually conflict.
  A superseded pair has its pending review **withdrawn** (re-fileable later), not rejected.

**3. The `resolve_contradiction` outcome is a three-value vocabulary the human chooses; the LLM
only proposes.** The Build Spec rule is *"contradictory claims must remain visible until
reviewed,"* so visibility is the pre-review default and resolution is what changes it. The
reviewer's decision is captured as a field on the review item and maps to a deterministic
effect (the LLM proposes the edge; the human's approval triggers the mutation, CLAUDE.md
rule 9):

- **`acknowledge`** → the `contradicts` edge flips `proposed → active` (now a projected
  backlink under each claim page's "Disagreements/Contradictions" section); both claims stay
  live. This is the common outcome — a real, standing disagreement.
- **`supersede`** → the reviewer names which claim wins; the worker writes an `active`
  `supersedes` edge (winner → loser) and deprecates the losing claim to
  `deprecated_candidate`. The `contradicts` edge still activates, so the historical conflict
  stays recorded. The deprecation runs through the existing **`deprecate_wiki_page` audit
  path** — the `resolve_contradiction` approval *authorizes* the deterministic deprecation
  (no second human gate), but the **`audit_log` entry must state the claim's status changed as
  part of an approved contradiction resolution**, so a status change never appears without a
  recorded cause. We do not mutate the loser's status silently.
- **`reject`** → not a real contradiction (model error); the edge → `rejected`, nothing else
  changes.

3.5c-1 builds the `contradicts` proposal plus the `acknowledge`/`reject` activation path;
`supersede` *execution* lands in **slice 1b** (now implemented) as a deterministic action in
`apply_resolved_contradictions`, reusing the existing `supersedes` edge and
`deprecated_candidate`/`deprecate_wiki_page` primitives — no new schema. The reviewer names the
`winner` on the approved item; the executor writes the `supersedes` edge, deprecates the loser
(via `recompose_claim(deprecate=True)`, which keeps the loser's evidence + contradiction
backlink rendered and flips only its lifecycle status), and files+approves a
`deprecate_wiki_page` item so the deprecation carries an `audit_log` cause. It is idempotent and
never silently records a `supersede` without effects. Because the deprecated loser **retains its
evidence**, endpoint validity is **evidence-based, not status-based** (decision 2): the
`contradicts` edge stays active rather than being re-superseded as a stale endpoint.

**4. Idempotency is per sorted-pair, cache-replayed, fingerprinted over evidence not just
text.** `claims.py` is fingerprint-idempotent per source, but contradiction detection is
per-pair. The unit is a **per-sorted-pair fingerprint** over a canonical payload — **not just
the claim texts**, because the same claim text can survive while its supporting source/span
changes, and a contradiction verdict depends on that evidence context (mirroring the claim
grounding model). The payload is:

- `claim_id_a`, `claim_text_a`, and A's **`active` citation anchors**;
- `claim_id_b`, `claim_text_b`, and B's **`active` citation anchors**;
- the **shared blocking node ids** that made the pair a candidate;
- `schema_version`, `prompt_version`, `model_ref`.

An unchanged payload replays the tier-3 verdict from the response cache (ADR-0027) with no
provider call; the pass spends model calls only on new or changed pairs. **The response cache
keys on the prompt messages, so the canonical payload is realized by embedding it in the
prompt** — both claims' texts, the *full* set of citation anchors (each `source_id` + char
range + quote), and the shared blocking node ids all appear in the message, with
`schema_version`/`prompt_version`/`model_ref` already in the cache key. So identical claim
text + quote but a changed `source_id`, char range, or shared node misses the cache and
re-evaluates, exactly as the fingerprint requires. The candidate-pair set is recomputed
deterministically from the current `active` graph each run. A **corpus-level fingerprint is
rejected** — it turns a single local pair change into a full-pass cache miss and badly worsens
the tier-3 cost profile. Job semantics follow the 3.5a/b shape: synchronous, supervised, **no
API key → a `skipped` job** — but the deterministic stale-pair supersession and human-decision
application still run without a key (as `claims.py` retracts stale evidence keyless). The
model's `confidence` is **clamped to [0,1]** before being stored (untrusted output).

**5. Synthesis is keyed per active concept/entity, gated by ≥2 independent sources.** A
`synthesis` node needs a deterministic, bounded *trigger* or the pass has no defined scope.
The unit is **one synthesis node/page per `active` concept/entity node**, with precise
eligibility:

- the target concept/entity node's `status` is `active` (candidates are never synthesized);
- **≥2 `active` claims** are connected to the target through the graph neighborhood (the same
  `active` `mentions` walk the contradiction blocker uses);
- those claims are supported by **≥2 independent sources** (ADR-0018 provenance rule).

The proposed page aggregates what those sources/claims collectively say — including the
contributing claims and their source citations, and any **`active` `contradicts` edges among
those claims** surfaced under the template's "Disagreements" section. This is a deterministic
trigger, a bounded count (one per qualifying active node, not combinatorial), and reuses the
blocking/independence/caching of 3.5c-1. The idempotency unit is a **per-target-node
fingerprint** over `{target id + status; each contributing claim's id, text, and active
citation anchors; the ids and statuses of the active contradiction edges among them; prompt /
schema / model versions}` — so a synthesis re-derives when its evidence *or* its surfaced
disagreements change, not only when claim text changes. Single-source concepts get no synthesis
(nothing to synthesize *across*), consistent with candidates being excluded from synthesis
(ADR-0018). Per-contradiction-cluster and on-demand-only triggers were rejected: the former
makes synthesis primarily about disagreement and misses the normal case (multiple sources
building a coherent picture); the latter defers the "knowledge compounds over time" goal.

**6. Synthesis grounding reuses the citation gate; it stands on grounded claims.** CLAUDE.md
rule 3 and ADR-0026 still apply, but synthesis sits one level up — raw sources are truth,
claims are grounded atomic evidence nodes, and synthesis is higher-level prose *over those
grounded claims*. The contract:

- **synthesis → claim `derived_from` edges** are written `active` when the referenced claim is
  itself `active` and citation-grounded (provenance, like a claim's own `derived_from`).
- **Optional synthesis → source `derived_from` edges** are written `active` *only* for a
  **direct source quote** in the prose that passes the existing `citations.py` verbatim-locate
  gate. A quote that cannot be located is **dropped, or the synthesis generation is marked
  `partial`** — unverifiable quote text is never written.
- The synthesis page frontmatter carries **structured, machine-checkable references**: the
  contributing claim ids and any direct-source citation anchors.
- A synthesis sentence that summarizes already-grounded claims **does not need its own raw
  span** — the citation chain through the Claim pages suffices, provided the claim references
  are explicit and checkable. This avoids re-deriving grounding the claims already did.

Crucially, **"grounded evidence pointer" is separate from "approved synthesis"**: the
`derived_from` edges are `active` because they are provenance, but the synthesis *node/page
itself stays `candidate` until reviewed/promoted* (next decision). This adds no new
verification machinery.

**7. Synthesis pages are born `candidate` and written to disk, but promotion is review-only
with no recurrence path.** Two coupled points:

- **Visibility:** a candidate synthesis page **is written to disk** under `wiki/Synthesis/` as
  `status: candidate` — a reviewer must *read the proposed prose* to judge it, so the
  "invisible until approved" model that works for an edge does not work for synthesis.
  Frontmatter: `type: synthesis`, `synthesis_id`, `status: candidate`,
  `review_status: pending`, `generation_status: enriched`, `confidence`, `input_fingerprint`.
  It is readable/reviewable; like a candidate concept it is **listed in `index.md` marked
  `candidate`** (the index is the full page listing, not a promoted-only view) but is **not
  usable as evidence for any later synthesis or query answer** until promoted to `active` — the
  load-bearing exclusion, which holds because synthesis inputs are active claims/concepts only.
  Its node status mirrors `candidate`; its `derived_from` edges are `active` (provenance, not
  approval). The page filename is the `synthesis_id` (`wiki/Synthesis/<syn_id>.md`, like
  `wiki/Claims/<clm_id>.md`), so syntheses of different topics never collide on a shared slug.
- **The review gate, and the trap it must avoid:** `promote_candidate_node` (ADR-0018) has a
  **recurrence auto-promote path** — ≥2 independent sources → auto-`active`, no human. But a
  synthesis is *born from ≥2 independent sources by construction* (that is its trigger), so
  reusing `promote_candidate_node` would make **every synthesis auto-promote and the review
  gate a no-op**, directly violating ADR-0028's "human-reviewed." Synthesis promotion is
  therefore **review-only, with no recurrence path.** This is implemented as a **distinct
  `propose_synthesis` review type** (added to `policies/review.yaml` and `reviews.py`
  `REVIEW_TYPES`), rather than overloading `promote_candidate_node` with a node-type exception
  — a `node_type == synthesis` branch in the promotion worker would be easy to break later, and
  a distinct type is structurally incapable of inheriting the auto-promote bug and keeps the
  audit trail legible. The review item's outcome maps deterministically: **approval** flips the
  page `status` `candidate → active`, mirrors the graph `nodes.status`, and records an
  `audit_log` entry; **rejection** sets `review_status: rejected` and the page `status` to
  **`deprecated_candidate`** — *not* a `rejected` node status, which the lifecycle vocabulary
  (ADR-0022: `active | candidate | deprecated_candidate | archive_candidate | archived`) does
  **not** admit. A new node status would require an explicit ADR-0022 extension; we reuse the
  existing vocabulary instead.
- **Re-generation never overrides a human decision (review-driven refinement).** The
  `propose_synthesis` review id is **fingerprint-scoped** — `subject = {topic_node_id,
  fingerprint}`, where the fingerprint covers the contributing claims + anchors + their active
  contradictions — so each distinct evidence set is a distinct, re-fileable decision. The normal
  generation pass therefore **never rewrites a reviewed synthesis**: an `active` (approved)
  synthesis whose evidence later changes **stays active** (the stale fingerprint is surfaced, not
  silently demoted), and a synthesis whose *current evidence* was rejected is left alone (no
  re-nag). Changed evidence (a new fingerprint) re-opens a topic — automatically for an
  un-reviewed/retracted one, and only under explicit **`--force`** for an approved one (which
  demotes it to `candidate` with a fresh fingerprint-scoped pending review). A topic that drops
  below the eligibility threshold is **retracted through the audited deprecation path** (a
  `deprecate_wiki_page` item is filed, the page re-rendered `deprecated_candidate` with a coherent
  `review_status`, pending proposals withdrawn) — the same governance the claim/concept tombstones
  use, never a bare status rewrite.

## Consequences

The whole slice is a pair of new producers over the 3.5b graph: no new graph authority, one
schema reuse (`contradicts`), one schema reuse with a new outcome field (`resolve_contradiction`),
one new review type (`propose_synthesis`), and a thin follow-on `supersede` executor. Cost is
bounded by deterministic graph-neighborhood blocking and made free-on-replay by the existing
response cache. Both deliverables honor the project's invariants — the LLM proposes, the human
disposes; nothing factual is written ungrounded; contradictions stay visible until reviewed;
syntheses cannot silently auto-promote. The trade is two more milestones and the standing
risk that graph-neighborhood blocking under-recalls real contradictions whose claims share no
extracted concept — accepted for v1, with vector blocking as the recorded escape hatch. The
representation, blocking strategy, resolution vocabulary, synthesis trigger, and review-only
synthesis promotion are the load-bearing commitments; the LLM verdict/synthesis output schemas
and column-level details are tuned during implementation.

## Addenda (post-ship refinements)

Recorded during the Phase 4 planning gate (2026-06-17) after re-grounding the four shipped
synthesis decisions against `app/workers/synthesis.py`. Three already matched the code
(`topic_node_id` is the canonical key; retraction already runs the audited `deprecate_wiki_page`
path; candidate syntheses are already listed in `index.md` marked `candidate`); the wording for
the index/evidence split was sharpened in `CONTEXT.md` to match decision 7 above. Two items are
**deferred to a Phase 3.5c-3 addendum slice** (no code in this gate):

1. **Quote-pattern guard (extends decision 6).** The shipped guard `_contains_verbatim_quote`
   rejects a *copied verbatim run* (≥12 consecutive words matching a contributing source). It does
   **not** catch an *invented quotation* — quotation-mark-delimited prose that matches no source and
   so reads as fabricated evidence. The addendum adds a complementary check: synthesis output whose
   `summary`/`synthesis` contains multi-word quotation-delimited spans is **rejected or marked
   `partial`** (single-word quoted terms are exempt to avoid false positives on emphasized terms);
   the prompt already says "do not quote," and the worker enforces it. Strongest-integrity boundary,
   consistent with ADR-0026 (the model produces synthesis prose, not faux evidence).
2. **Self-describing review subject (extends decision 5/7).** The `propose_synthesis` review
   subject is `{topic_node_id, fingerprint}`. The addendum additionally carries `topic_node_type`
   and `topic_slug` so review/audit records for entity-family topics (person/organization/project)
   are legible without re-resolving the node — the subject's identity contract (`{topic_node_id,
   fingerprint}`) is unchanged.

**Forward reference to Phase 4.** Decision 7's exclusion is also a **Phase 4 retrieval invariant**:
keyword, vector, and graph retrieval must treat `candidate` and `deprecated_candidate` synthesis
(and any non-`active` node) as *navigable but not citable* — discoverable in navigation/index
results, never returned as evidence or fed to an answer. This is captured here so the Phase 4 plan
inherits it rather than re-deriving it.
