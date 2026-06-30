# ADR-0050 — Identity surgery: entity/concept merge (rekeying), tombstone-redirect, forward-only

**Status:** Accepted. Design-locked 2026-06-30 (grill-phase, committed `4721c46`); **v1 merge implemented**
2026-06-30 (`merge_entities` + `merge_concepts`, exact same node_type). `merged` status (graph/validate_wiki/
retention.yaml, ∉ RETENTION_DEFAULT); `graph.{find_assertion,repoint_edge,reactivate_edge}`; the
`render_concept_page` merged-tombstone branch; `app/workers/merges.py` (`apply_merges` — two-pass plan/apply,
the three block gates, edge re-point/normalize, tombstone, alias union, withdraw, audit); `review_read`
`_effect_merge`/`preview_merge_*` + `EXECUTOR_BY_TYPE`; `run_apply` wiring + `merge_discovery_reindex_not_guaranteed`;
the `validate_graph` no-active-merged-endpoint invariant + the `validate_projection` merged_into invariant.
Covered by `tests/test_merge.py` (26). **Implementation review round (6 fixes):** the affected-surface
re-render (Source pages + duplicate partners) now covers **every** edge action (collapse/resurrect, not just
re-point); the tombstone carries `merged_at` + `merge_review_id`; the `validate_projection` invariant
(`merged_into` → active same-type non-self survivor); the subject/proposal matcher scans **both**;
`_effect_merge` distinguishes a **partial** merge as `UNKNOWN`. **Round 2:** the ADR-0040 dry-run now
surfaces the re-point itself via a new `diff.graph.edges_repointed` (`from_src/from_dst → to_src/to_dst`,
same edge_id) attributed to the merge by target id; the audit carries the absorbed `old_status` + both page
paths + `affected_pages`. (Refined per review round 1: the edge-collision key is the **full**
assertion identity incl. evidence anchors — distinct-evidence edges coexist; `supersedes` stays **directed**
— only `{contradicts,duplicates}` re-canonicalize; absorbed edges become `superseded`, **not** deleted;
re-point preserves edge provenance; pending-subject withdrawal + the merge audit reuse the existing
`reviews.withdraw_review_item` / `reviews/audit_log/`; the tombstone preserves the **full** frontmatter
schema. Review round 2: the status-agnostic `uq_edges_assertion` means a re-point can collide with an
**inactive** A row — resurrect on `proposed`/`superseded`, **block** on `rejected` (decision 3); an
**approved-but-unapplied** B-referencing item **blocks** the merge (decision 6); withdrawal covers `pending`
**and** `deferred`, matched on structured subject **+ proposal** fields (decision 2); a deterministic alias-union rule.
Review round 3: the subject matcher includes **`topic_node_id`** (so a `propose_synthesis` proposal for an
absorbed concept/entity is caught — decision 2/6); resurrecting a `proposed` target edge also **withdraws its
stale `review_id`** (decision 3); an endpoint-invalid re-point is a **pre-write block** `invalid_repoint_endpoint`
(decision 6), not a skip (else a merged endpoint survives); the reindex warning is pinned
**`merge_discovery_reindex_not_guaranteed`**. Review round 4: a **resurrected** target row's `review_id` is
**rewritten to the merge review id** — the explicit exception to "re-point preserves provenance," so an
active edge never points at a withdrawn review (decision 3); stale doc references cleaned up.) This is the dedicated **identity-surgery ADR** deferred by ADR-0041 (the
rekeying bright line) — the highest-blast-radius governance class, which *changes what ids mean*. It locks
the **risk taxonomy + invariants + the rewrite-vs-redirect decision + the reversibility posture + v1 scope**.
**v1 implements `merge_entities` + `merge_concepts` only** (the *collapse* case, exact same node_type);
**`split_entity`, prefix-changing `change_entity_subtype` (single-node rekey), and cross-type/cross-subtype
merge are documented here but deferred** to their own follow-ups within identity surgery.
**Extends/claims:** ADR-0021 (frozen content-hash ids; the rename/merge/tombstone *sketch* this ADR makes
real), ADR-0041 (rekeying = highest blast radius; `mark_semantic_duplicate` is the non-rekeying precedent —
identity surgery is the rekeying step beyond it), ADR-0017 (page identity; title/aliases are
page-authoritative), ADR-0029/0030 (graph is SoT for edges; `EDGE_TYPES`/`SAME_TYPE_EDGES`/`EDGE_ENDPOINTS`),
ADR-0035/0040 (review-ledger, decide/apply, dry-run), ADR-0045 (applied effects are not auto-undone — reversal
is a *new* governance action). Read `app/workers/concepts.py` (`node_id`/`_slug`/`_recompose_node` —
aliases are page-authoritative, additive union), `app/backend/graph.py` (`uq_edges_assertion` = the FULL
assertion identity `(src,dst,edge_type,asserted_by,evidence_source_id,evidence_char_start,evidence_char_end)`;
`SAME_TYPE_EDGES` vs the canonically-ordered symmetric set `{contradicts,duplicates}`; `EDGE_ENDPOINTS`;
`set_status`), `app/workers/duplicates.py`
(`mark_semantic_duplicate` — the closest executor precedent), `app/backend/main.py` (`run_apply`).

## Context — why merge is the hardest class

Semantic node ids are **frozen content-hashes of the normalized name** (`cpt_<sha256(name)[:16]>` etc.,
ADR-0021), fixed at creation; the **slug** is name-derived (separate from the id); **graph edges key on the
id**, **wikilinks key on the slug**, **claim citations key on `source_id`**, and **review subjects key on
node_id/pair**. A merge collapses two ids into one, so — unlike every executor shipped so far — it must
**redirect or rewrite every live identity reference**. ADR-0021 sketched the contract (frozen id; merge =
tombstone the absorbed node with `merged_into`, union aliases, re-point edges by id); **none of that
machinery exists yet** (no `merged` status, no `merged_into`, no edge re-point, no tombstone-redirect). This
ADR makes the sketch real and locks the invariants.

## Decisions

**1. Merge model = hard re-point + tombstone; forward-only; auditable (not live-reversible) in v1.**
> *A merge rewrites live identity references to the survivor and leaves the absorbed id as a durable
> tombstone/redirect record. It is auditable, not live-reversible in v1.*

Rejected: a **soft redirect** (keep B's node + edges; a `merged_into` pointer every reader follows at read
time) — it imposes a runtime alias layer on **every** reader (graph traversal, search/nav, backlink render,
validators, eligibility, dry-run, future producers), risks drift + double-counting, and diverges from
"graph is SoT / edges are clean." The **hard re-point** makes the post-merge world simple: active
relationships point to survivor A; B is no longer a live identity. **Forward-only v1** is the honest safety
boundary — no live un-merge button; correcting a wrong merge is a **new** governed operation (a future
split/un-merge), reconstructable from the merge **audit** (decision 5), consistent with ADR-0045 (applied
effects aren't auto-undone). Reopen is allowed **only while the decision is PENDING_APPLY** (nothing
merged); a **cleanly applied** merge projects `EFFECTED` (absorbed node + page both `merged`, page
`merged_into` = survivor, AND no active edge still touches the absorbed id) → not reopenable; a **partial**
live state (graph XOR page merged, or a stray active edge) projects `UNKNOWN partial_merge_state` → also not
reopenable (repair the read model first), mirroring the hide/unhide reopen-safety pattern.

**2. Surface taxonomy — what is a "live identity reference" (re-pointed) vs not.**
- **Graph edges** (`derived_from`/`contradicts`/`mentions`/`duplicates`/`related_to`/`supersedes`, all key
  on node_id) → **hard re-point B→A** (decision 3). The core live-identity rewrite.
- **Wikilinks** → **the tombstone resolves literal links; NO global relink.** Graph-**projected** backlink
  sections (Source "Concepts", synthesis links, `## Duplicates`, etc.) update *for free*: re-pointing the
  edge B→A + re-rendering the pages that projected it makes their projection render `[[…/A-slug]]`. Any
  remaining **literal** `[[…/B-slug]]` reference resolves to B's tombstone page (which links to A) — not
  rewritten. Literal links are **historical references, not live identity assertions**; rewriting them
  globally would touch prose/notes/imported context and change meaning (a separate opt-in "relink literal
  links" tool is a possible *later* cleanup, **not** part of merge v1).
- **Claim citations** (`derived_from` → `source_id`) → **N/A**: claims key on `source_id`; an entity/concept
  merge never touches them.
- **Search/nav rows** → **auto-derived via reindex** (page-derived; follow the re-rendered pages).
- **Unresolved review subjects referencing B** (every item in `pending/` — status **`pending` OR
  `deferred`**, since `deferred` lives in `pending/`; otherwise a deferred item could later be approved
  against a tombstoned id) → **withdrawn via the existing `reviews.withdraw_review_item`** (the mechanism the
  synthesis pass uses for superseded proposals): it removes the `pending/<id>.json` file and writes a
  `reviews/audit_log/<id>-withdrawn-<hex>.json` entry (`decision: "withdrawn"`, `decided_by: "system"`,
  `note: "superseded_by_merge"`). A **real, audited ledger operation**, not an item *status*
  (`REVIEW_STATUSES` stays `pending|approved|rejected|deferred`; a withdrawn item is gone from `pending/`,
  recorded in `audit_log/`; a human re-files against A if still wanted). **Matcher:** exact node-id equality in
  structured fields of the **subject OR proposal** — `node_id`, `node_ids[]`, `survivor_node_id`/
  `absorbed_node_id`, **`topic_node_id`** (a `propose_synthesis` subject keys on the topic node — a
  pending/deferred/approved synthesis proposal whose `topic_node_id == B` must be matched), and the canonical
  **page-path** form — **never** by scanning rendered prose. (The single matcher used by both the
  unresolved-withdrawal and the approved-unapplied gate; scanning the proposal too, not just the subject,
  catches an item that references B in its proposal payload.) **Approved** (decided) items referencing B are **not** just immutable audit —
  they are executor inputs and are a **pre-merge BLOCK gate** (decision 6); **rejected** items are immutable
  audit, left as-is. (Re-keying a subject is rejected: the subject-hashed `review_id` would change → a
  different item.)

**3. Edge re-point algorithm + invariants (the hardest invariant).**
> *The live graph after merge ≡ "replace B with A, then normalize: canonicalize symmetric edges, collapse
> duplicates, remove self-edges."*

**Active edges only** are re-pointed; historical/proposed/rejected/superseded rows **stay unchanged** on B
as audit/provenance (re-pointing old decisions would hide what was originally asserted about B). The re-point
is an in-place **endpoint update** of the existing edge row (`UPDATE … SET src_id|dst_id = A …`) — it
**preserves the `edge_id` + all provenance** (`asserted_by`, `review_id`, `job_id`, **evidence anchors**); it
does **not** overwrite provenance with the merge's ids (that would weaken the graph audit — the merge is
recorded separately, decision 5). For each **active** edge with `src==B` or `dst==B`:
- compute the re-pointed endpoint (B→A);
- **endpoint validity:** the result must remain valid under `graph.EDGE_ENDPOINTS` (same-type merge
  preserves endpoint types, so this holds in practice); a transform that would violate it is a **pre-write
  BLOCK** (`invalid_repoint_endpoint`, decision 6), **not** a skip — *skipping* would leave the active B edge
  untouched, and since B becomes `merged` that active edge would then have a merged endpoint, violating the
  decision-5 invariant. Blocking keeps the graph consistent (no partial-apply);
- **canonical re-order — only the canonically-ordered symmetric set `{contradicts, duplicates}`** (`src<dst`,
  per `validate_graph`). **`supersedes` is DIRECTED — preserve its direction** (`supersedes(B→C)`→`(A→C)`,
  `supersedes(C→B)`→`(C→A)`); never reverse/canonicalize it (that would invert winner/loser);
- **self-edge** (`src==dst` after re-point — e.g. a `duplicates(A,B)`/`contradicts(A,B)`/`related_to(A,B)`
  becomes A↔A) → set the absorbed B edge row to **`status: superseded`** **keeping its B endpoint** (a node
  can't relate to itself; the absorbed row is **status-changed only, not endpoint-updated, not deleted** — it
  stays queryable as history), recorded in the merge audit;
- **collision** — `uq_edges_assertion` is **status-AGNOSTIC** (unique across *all* rows), so the re-pointed
  **FULL assertion identity** `(src,dst,edge_type,asserted_by,evidence_source_id,evidence_char_start,
  evidence_char_end)` may already exist on A in **any** status. The in-place endpoint `UPDATE` would violate
  the unique index, so the outcome depends on the existing A row's status (the absorbed B edge is
  status-changed **keeping its B endpoint**, never endpoint-updated, in every collision case):
  - existing A row **`active`** → **collapse**: leave the surviving A edge active; absorbed B → `superseded`;
    audit the absorbed edge id.
  - existing A row **`proposed`/`superseded`** (lifecycle-inactive, no human "no") → **resurrect**: update the
    target A row to `active` (it's the same assertion, B≡A); absorbed B → `superseded`; audit
    `resurrected_target_collision` (target_edge_id + previous target status + absorbed edge id) — preserves
    the live relationship without a duplicate row. **Review-id authority (the explicit exception to "a
    normal re-point preserves all provenance"):** the activation was authorized by the **merge**, not by the
    target row's old proposal, so the resurrected row's **`review_id` is rewritten to the merge review id**;
    `asserted_by` / evidence anchors / `job_id` stay as the original assertion's provenance. **If the target
    row's old `review_id`** (a `proposed` row can carry one — e.g. a contradiction proposal awaiting a human)
    has an item still in `pending/` (status `pending` or `deferred`), that review is **withdrawn** via
    `reviews.withdraw_review_item` (`note: superseded_by_merge`) — else a human could later decide a review
    for an assertion the merge already activated (this review is on **A**, so the decision-2/6 B-matcher
    doesn't reach it — the resurrect path withdraws it explicitly). The merge audit records
    `resurrected_target_collision` with the `target_edge_id`, previous target status, the
    `previous_target_review_id` (= the withdrawn review), and the absorbed edge id — so the active status's
    authority (merge) and history (the superseded proposal) are both legible.
  - existing A row **`rejected`** (an explicit human terminal "no" to that exact assertion) → **BLOCK the
    whole merge before any write**, typed `rejected_target_collision` (decision 6) — never auto-resurrect a
    human rejection during a merge; the dry-run surfaces the conflict + edge ids for the operator.
  - **Distinct evidence coexists:** edges differing only in evidence anchors are DISTINCT assertions — two
    active `mentions(Src→A)` and `mentions(Src→B)` with *different* spans become **two distinct active
    `mentions(Src→A)`** edges (no collapse; collapsing on `(src,dst,edge_type,asserted_by)` alone would erase
    distinct evidence — relationship-level dedup is a separate graph-model change, out of scope).
- otherwise (no collision) → the in-place endpoint `UPDATE` lands on the B edge (`edge_id` + provenance
  preserved).

Deterministic + idempotent (a re-apply after a completed merge is a no-op). Every page that projected a
re-pointed edge is **re-rendered** so its backlink section shows A.

**4. v1 scope = merge only, exact same node_type.**
- **`merge_entities`** — exact same entity-family node_type (`per_`+`per_`, `org_`+`org_`, `prj_`+`prj_`,
  `ent_`+`ent_`) — **no cross-subtype** (person↔organization etc. changes the id prefix/path → that is
  identity surgery **plus** type migration).
- **`merge_concepts`** — `cpt_`+`cpt_` only.
- (Same-type is consistent with `mark_semantic_duplicate`'s `SAME_TYPE` constraint — a merge often follows a
  reviewed `duplicates(a,b)` annotation.)
- **Deferred (documented, own follow-up each):** **`split_entity`** (spawns ids; needs a human **partition**
  of aliases/mentions/edges/backlinks/evidence + `split_from` provenance + a bespoke preview — a separate
  design); **prefix-changing `change_entity_subtype`** (single-node rekey: mint a new-prefix id, re-point
  the old node's edges, tombstone the old id `merged_into` the new — reuses the merge machinery but with a
  *freshly-minted* survivor); **cross-type/cross-subtype merge**. This ADR **claims the ADR-0041 deferral**
  of these.

**5. Tombstone representation + subject + reconstructable audit.**
- **New lifecycle status `merged`** for the absorbed node — added to `graph.NODE_STATUSES`,
  `validate_wiki._VALID_STATUS`, `policies/retention.yaml`, and **kept OUT of
  `search.RETENTION_DEFAULT_STATUSES`** (so B drops from default `/search` nav + graph channel +
  answer-eligibility for free, like `hidden`/`evidence_hidden`; still queryable via
  `/search?node_status=merged` + raw `/graph/*`). **Not** `deprecated_candidate` (that is **in**
  `RETENTION_DEFAULT_STATUSES` → it would leave the absorbed identity discoverable, and conflates merge with
  evidence-loss deprecation).
- **Tombstone page** B stays at its **old path**; the executor re-renders it to a short tombstone that
  **preserves the full required frontmatter schema** (`validate_frontmatter`): `type`, the typed id
  (`concept_id`/`entity_id`/`person_id`/…), `title`, `confidence`, `review_status: approved`, plus
  `status: merged`, `merged_into: <A id>`, `merged_at` / `merge_review_id`, and the preserved `aliases`
  (audit). Only the **body** collapses to a brief "Merged into `[[…/A]]`" note (not the old full semantic
  page). Graph node B stays with `status: merged`; **survivor A stays `active`**.
- **Survivor A** is re-rendered with B's title + aliases unioned in (so A is findable by B's name; aliases
  are page-authoritative, ADR-0017/0021 — the merge executor is the explicit place that writes them).
  **Deterministic union rule:** A's `title` is **unchanged**; `aliases(A) := stable-dedup(A.aliases ++ [B.title]
  ++ B.aliases)` — i.e. preserve A's existing aliases in order, then append B's title, then B's aliases,
  de-duplicating case-insensitively while dropping any entry equal to A's title (stable first-occurrence
  order; byte-stable output).
- **Subject (explicit winner — unlike the unordered `duplicates` pair):**
  `subject = {survivor_node_id: A, absorbed_node_id: B}`, `proposal = {to_status: merged}`. **Scope guards**
  (skip with a typed reason, BEFORE any write, never partial-apply): malformed subject, invalid/missing
  node id, `survivor == absorbed`, **type mismatch** (not exact same node_type), survivor/absorbed not
  `active`, survivor itself already `merged`. **Reject = ledger no-op.** **Graph-REQUIRED.** Previewable via
  the ADR-0040 **dry-run** (graph edge re-point/collapse/drop deltas + B's tombstone diff + A's re-render +
  withdrawn pending subjects) + an A1 per-item projector.
- **Reconstructable audit** (the basis of the auditable, not-live-reversible posture) — **lives in the
  existing `reviews/audit_log/`** (the established governance audit store, reused by decide/withdraw/reopen),
  as one structured merge entry (e.g. `audit_log/<review_id>-merged-<hex>.json`) carrying: the absorbed
  id/path/title/type/old-status, survivor id/path, the **re-pointed / collapsed / dropped** active edge ids
  (with their original provenance), the affected pages re-rendered, the unioned aliases, the withdrawn
  pending-subject review_ids, and the review id/actor/time — enough to **reconstruct a future inverse**
  (un-merge / re-split). No separate artifact store (keeps all governance audit in one place); the edge-row
  detail is carried *in* the audit_log entry.
- **Validator invariant:** a `merged` node is a **tombstone** whose `merged_into` points to an **active,
  same-type** survivor, and **no active edge may have a `merged` node as an endpoint** (the merge re-points
  them all away) — a clean post-merge invariant for `validate_graph` / `validate_wiki` / `validate_projection`.
- **Reindex/non-clean posture (mirrors the hide slices):** a merge changes discoverability (B drops, A's
  page + projected pages change) → reindex; an applied merge whose keyword/nav reindex failed → non-clean
  (`validation_failed` + the pinned warning **`merge_discovery_reindex_not_guaranteed`**).

**6. Pre-merge BLOCK gates — never partial-apply.** Like every executor (`apply_archive_sources` precedent),
the merge runs its guards **before any write**; a blocked merge writes **nothing** and the dry-run surfaces
the blockers (review ids/types/edge ids) so the operator resolves them first. Beyond the basic subject
guards (decision 5), three state-dependent gates (the merge computes the full re-point plan in a dry pass,
detects any blocker, and refuses **before** any write):
- **`rejected_target_collision`** (decision 3): a re-pointed edge whose full assertion identity already
  exists on A as a **`rejected`** row — never auto-resurrect a human rejection during a merge.
- **`invalid_repoint_endpoint`** (decision 3): a re-pointed (canonicalized) edge would violate
  `graph.EDGE_ENDPOINTS` — block rather than skip, so no active edge is left pointing at the soon-to-be
  `merged` B (consistency over partial-apply).
- **`approved_unapplied_references_absorbed`:** the merge scans `reviews/approved/` for items whose
  **structured subject/proposal references absorbed B** (same exact-id matcher as decision 2) and projects
  each via the existing `review_read` effect-status projector. **Block** if any projects `PENDING_APPLY`,
  `UNKNOWN`, **or `APPLY_DEFERRED`** (manual-effect state is unknown — block unless a future allowlist of
  inert types says otherwise). Items that project `EFFECTED` or `NO_EFFECT_REQUIRED` are **not** blockers
  (their effect is already realized / owes nothing). The operator applies / reopens / rejects the blocking
  items, then re-merges — no silent override of a human approval, no coupling of merge to other executors.

## Consequences

The project gets a designed path for its highest-risk governance class on an honest safety boundary:
**forward-only, auditable merge** that rewrites only the **live** identity graph (active edges → survivor,
normalized) and leaves the absorbed id as a **durable `merged` tombstone** that resolves old links and
records a reconstructable audit — reusing the review-ledger + dry-run + reopen + reindex machinery, touching
**no** claim citation (source-keyed) and **no** literal wikilink (tombstone-resolved). The cost is the new
`merged` status + tombstone renderer, the edge re-point/normalize algorithm + its `EDGE_ENDPOINTS`/symmetric/
collision/self-edge invariants, the survivor alias-union, the pending-subject withdrawal, the audit record,
and the validator invariant. **Deferred (own follow-ups):** `split_entity` (partition design), prefix-changing
`change_entity_subtype` (single-node rekey reusing merge), cross-type merge, an opt-in literal-link relink
tool, and any live un-merge (only auditable in v1).

## Tests (design intent; written at implementation)

- **Merge collapses ids:** approve `{survivor: A, absorbed: B}` (same type) → B node + page `status: merged`,
  `merged_into: A`, short tombstone body, title/aliases preserved; A stays `active` with B's aliases unioned
  in; B drops from default `/search` nav + graph channel + answer-eligibility; raw `/graph/*` + an explicit
  `node_status=merged` still return B.
- **Edge re-point normalize:** an active `mentions(Src→B)` becomes `mentions(Src→A)` (endpoint update,
  `edge_id` + provenance preserved) and Src's page re-renders `[[…/A]]`; an inactive/superseded edge on B is
  **untouched** (history).
- **Distinct evidence coexists:** two active `mentions(Src→A)` and `mentions(Src→B)` with **different
  evidence anchors** become **two distinct active `mentions(Src→A)`** edges after merge (no collapse — the
  collision key is the full assertion identity incl. evidence anchors).
- **Collision outcomes (status-dependent, full identity incl. evidence anchors):** existing **active** A row
  → collapse (absorbed B → `superseded`, kept on its B endpoint, audited; A stays active); existing
  **`proposed`/`superseded`** A row → **resurrect** the target to `active` + absorbed B → `superseded` +
  audit `resurrected_target_collision`; existing **`rejected`** A row → the merge **blocks pre-write**
  (`rejected_target_collision`, no writes, dry-run shows the conflict).
- **`supersedes` direction preserved:** `supersedes(B→C)` → `supersedes(A→C)`; `supersedes(C→B)` →
  `supersedes(C→A)` — never reversed/canonicalized. Only `{contradicts, duplicates}` are re-canonicalized
  (`src<dst`).
- **Self-edge → superseded:** a `duplicates(A,B)`/`contradicts(A,B)`/`related_to(A,B)` that becomes A↔A after
  re-point is set to `status: superseded` (not deleted), recorded in the audit.
- **Wikilinks:** a literal `[[…/B-slug]]` still resolves (B's tombstone exists) — `validate_wikilinks`
  passes; a graph-projected backlink shows A after the edge re-point + re-render.
- **Unresolved subjects:** a `pending` **and** a `deferred` item referencing B are both withdrawn via
  `reviews.withdraw_review_item` (removed from `pending/`, an `audit_log/<id>-withdrawn-<hex>.json` entry,
  `note: superseded_by_merge`); a `rejected` item is left as immutable audit.
- **Subject matcher:** items with `subject.node_id == B`, `B ∈ subject.node_ids[]`,
  `subject.absorbed_node_id == B`, **`subject.topic_node_id == B`** (a `propose_synthesis` proposal), and the
  page-path subject form are matched; an item that only mentions B in prose/context is **not** matched.
- **Synthesis proposal by topic:** a pending/deferred `propose_synthesis` whose `subject.topic_node_id == B`
  (absorbed concept/entity) is withdrawn; an **approved** such proposal projecting `PENDING_APPLY` **blocks**
  the merge.
- **Approved-unapplied gate:** an approved item referencing B that projects `PENDING_APPLY` (or `UNKNOWN` /
  `APPLY_DEFERRED`) **blocks** the merge (`approved_unapplied_references_absorbed`, no writes, dry-run lists
  it); one projecting `EFFECTED` / `NO_EFFECT_REQUIRED` does **not** block.
- **Resurrected target review:** a `proposed` target-edge collision whose row carries a `review_id` with a
  pending/deferred item → the target row becomes `active`, the absorbed row `superseded`, the old proposal's
  review is **withdrawn** (`superseded_by_merge`), the target row's final **`review_id` is the merge review
  id** (asserted_by/evidence/job_id preserved), and the audit records `previous_target_review_id`.
- **Endpoint-invalid blocks:** a re-point that would violate `EDGE_ENDPOINTS` **blocks** pre-write
  (`invalid_repoint_endpoint`) and leaves the graph + wiki **unchanged** (no active edge left pointing at the
  merged B).
- **Tombstone validates:** the merged tombstone page passes `validate_frontmatter` (full schema preserved:
  `type`/typed-id/`title`/`confidence`/`review_status` + `status: merged`/`merged_into`),
  `validate_wikilinks`, and `validate_projection`, and survives a graph-node reindex.
- **Subject guards:** type-mismatch / self-merge / missing node / non-active endpoint / already-merged
  survivor each skip with a typed reason, never partial-apply.
- **Forward-only / reopen:** a not-yet-applied merge is reopenable (PENDING); an applied merge projects
  `EFFECTED` → reopen blocked (409). Reject = ledger no-op.
- **Validator invariant:** `validate_graph`/`validate_wiki` fail if an **active** edge has a `merged`
  endpoint, or a `merged` page's `merged_into` doesn't point to an active same-type survivor.
- **Idempotent + non-clean:** a re-apply of a completed merge is a true no-op (no writes); an applied merge
  whose reindex failed is `validation_failed` + the `merge_discovery_reindex_not_guaranteed` warning
  (mutation still written).
- **Dry-run:** shows the edge re-point/collapse/drop deltas + B's tombstone diff + A's re-render + the
  withdrawn subjects; graph-down apply refuses identically (live 503 / dry-run blocked).
