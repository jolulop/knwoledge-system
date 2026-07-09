# ADR-0051 — Entity subtype rekey: `change_entity_subtype` as a single-node rekeying executor

> **Retired for the item era by ADR-0059** (2026-07-08): under the type-neutral `itm_` identity a
> type change is a non-rekeying metadata flip (`change_item_type`) — no id substitution, no
> tombstone, no re-point. The entity family this ADR rekeys within no longer exists post-restart.
> The subject-shape rule (`{node_id, to_type}` — one rejected retype never locks out a different
> future retype) carries over to `change_item_type`. Historical for the pre-restart vault.

**Status:** Accepted. **Design-locked 2026-06-30** (grill-phase, committed `3ab1577`); **v1 implemented
2026-07-01** (`app/workers/rekeys.py::apply_rekeys`; `rekeyed` status in graph/validate_wiki/retention.yaml;
the `render_concept_page` rekeyed-tombstone branch; `review_read` `_effect_rekey`/`preview_change_entity_subtype`
+ `EXECUTOR_BY_TYPE`; `run_apply` wiring + `rekey_discovery_reindex_not_guaranteed`; the `validate_graph`
no-active-rekeyed-endpoint + `validate_projection` `rekeyed_to` invariants; the producer alignment in
`concepts.py`). Covered by `tests/test_rekey.py`. **Implementation review round 1 (3 blocking):** the
`rekeyed_to` validator accepts **active-or-candidate** (a candidate rekey is first-class — it mints a candidate);
`_effect_rekey` verifies the tombstone's `rekeyed_to` equals the *computed* target and the target node exists +
is live (a wrong/missing target → `UNKNOWN`, not a false `EFFECTED`); the misleading "duplicate-partner fan-out"
text was corrected (an active `duplicates` edge **blocks** the rekey — there is no partner fan-out). **Round 2
(1 blocking):** `_effect_rekey` now reports a **half-mint** (crash between the target mint and the old-id
tombstone — old id still live but the target node/page already exists) as `UNKNOWN partial_rekey_state`, not
`PENDING_APPLY`, so reopen refuses (else it would orphan the target); the rekey withdrawal audit uses the
accurate `superseded_by_rekey` reason. This is
the **second rekeying governance executor** and the **prefix-changing `change_entity_subtype`** branch that
ADR-0050 and ADR-0041 explicitly **deferred** to its own follow-up. It wires an executor onto the already-
registered (but record-only) `change_entity_subtype` review type, reusing ADR-0050's identity-surgery
machinery (`graph.repoint_edge`, the dry-plan-then-apply two-pass, the `invalid_repoint_endpoint` /
approved-unapplied block gates, the tombstone render seam, the Source re-render fan-out, the
`reviews.withdraw_review_item` withdrawal, the `reviews/audit_log/` audit, the ADR-0040 dry-run). It is a
**single-node 1:1 relabel** (one logical node changes type → id-prefix → page directory), **NOT** a 2→1
collapse — so it is genuinely smaller and lower-risk than merge, and it hardens the rekey pattern before
`split_entity` (which adds human partitioning).

**Scope of v1:** entity-family subtype change **only** (`entity ↔ person ↔ organization ↔ project`, any
direction), exact within-family. **Deferred** (documented here, own follow-ups): concept↔entity-family
*type* change (a different semantic kind — a future `change_node_type`), `split_entity`, cross-type merge,
a live **un-rekey** (forward-only in v1), and **collision-as-merge** (a colliding target is blocked, never
silently merged).

**Extends/claims:** ADR-0021 (frozen content-hash ids; "a later subtype change is review-gated, like a
merge/split, because it re-keys the node's id" — this ADR makes that sketch real; the classifier defaults
uncertain entities to generic `ent_`, and this corrects them), ADR-0050 (the identity-surgery machinery +
the `merged` tombstone model this parallels with a distinct `rekeyed` status), ADR-0041 (the rekeying
bright line — `change_entity_subtype` is non-rekeying *only if* id-preserving; the prefix-changing case is
this ADR), ADR-0018 (candidate lifecycle — rekey supports candidates, where merge does not), ADR-0030
(`EDGE_ENDPOINTS` / `SAME_TYPE_EDGES` — the `invalid_repoint_endpoint` gate), ADR-0040 (apply dry-run),
ADR-0035/0036 (review ledger, executor-backed apply, graph-required posture).

## Context — why subtype change is a rekey, and why it is *not* merge

A semantic node's id is `f"{prefix(node_type)}_{name_hash(name)}"` (`concepts.node_id`); the entity family
is subtyped and **the subtype selects both the id-prefix and the page directory** (`ent_`/`per_`/`org_`/
`prj_` → `wiki/Entities|People|Organizations|Projects`, ADR-0021). The classifier defaults an uncertain
entity to generic `entity` (`ent_`); correcting it later to `person`/`organization`/`project` therefore
**changes the id-prefix** → it is a **rekey**, not the id-preserving status/projection change ADR-0041
classed as non-rekeying. ADR-0041 drew exactly this bright line and deferred the prefix-changing case;
ADR-0050 listed it among its deferred follow-ups.

It is, however, the **smallest** rekey: a **1:1 relabel of one logical node**, not a collapse of two. The
edge-repoint spine, tombstone, withdrawal, audit, and dry-run all transfer from ADR-0050 unchanged; the only
structural addition is "mint the target node first." Crucially, because the target id is required to be
**empty** (decision A), the edge re-point has **none** of merge's collision matrix (no
collapse/resurrect/rejected-collision) — making rekey-apply strictly simpler than merge-apply.

## Decisions

### A. Virgin-target-only — three apply-time block gates

The new id is **computed**, not human-chosen (unlike merge's explicit survivor): `new_id = prefix(to_type) +
"_" + old_id.split("_", 1)[1]`. v1 mints **only into a fully empty target slot**, enforced by three
**apply-time block gates, surfaced in the dry-run** (not decision-time-only — colliding state can appear
between approval and apply), checked in the dry plan **before any write** (never partial-apply):

- **`target_subtype_id_exists`** — `graph.get_node(new_id) is not None` for **any** lifecycle status (active,
  candidate, deprecated_candidate, hidden, archived, or a `merged`/`rekeyed`/`deleted` tombstone). A
  status-agnostic node check: a real node already owns the id.
- **`target_subtype_page_exists`** — the target page `NODE_DIR[to_type]/<slug>.md` already exists on disk.
  This is **not** redundant with the id check: `wiki/` is regenerable/gitignored and the `nodes` table is a
  *derived* index, so the two can **drift** — an orphan page with no indexed node would otherwise be **silently
  overwritten** by the mint. A distinct typed reason because its root cause (wiki/graph drift) and remediation
  (rebuild the index / remove the orphan) differ from a genuinely occupied id.
- **`target_assertion_exists`** — `find_assertion` (status-agnostic, the `uq_edges_assertion` identity) finds a
  pre-existing edge row at any re-pointed full identity (decision E). In a consistent graph the virgin target
  has no edges, but a drifted/tampered graph can carry a **dangling** row that `repoint_edge` would hit *after*
  the node+page are minted — so it is detected in the dry plan and **blocks first**.

These keep the invariant simple — *subtype rekey may only mint into an empty slot* — and avoid accidental
merge, tombstone resurrection, hidden/candidate collapse, and audit overwrite. **Collision-as-merge is
deferred**, never silently performed: turning a subtype correction into an identity collapse is exactly what
these gates prevent.

### B. Distinct `rekeyed` tombstone status + `rekeyed_to` pointer — do not generalize `merged`

A rekey tombstone points its successor at a **different-type** id, which would violate the `merged_into →
active, **same-type**, non-self survivor` invariant `validate_projection` enforces for `merged`. We do **not**
relax that invariant (it is a real safety check for *actual* merges). Instead a **new lifecycle status**:

- `status: rekeyed`, `rekeyed_to: <new_id>`, `rekeyed_at`, `rekey_review_id` (full required frontmatter
  schema preserved; only the **body** collapses to a redirect note that **keeps the standard `> [!summary]`
  callout** — e.g. `> [!summary] Retyped <node_type>` + "Retyped into [[…]]" — mirroring the `merged`
  tombstone's `> [!summary] Merged <node_type>` branch in `render_concept_page`, per the CLAUDE.md/AGENTS.md
  page standard that every major page keeps a summary callout).
- `rekeyed` added to `graph.NODE_STATUSES`, `validate_wiki`'s vocab, and `retention.yaml`; **kept out of
  `RETENTION_DEFAULT_STATUSES`** → the old id drops from default `/search` nav + graph channel +
  answer-eligibility **for free** (like `merged`).
- `validate_graph`: **no active edge may have a `rekeyed` endpoint** (verbatim mirror of the `merged`
  invariant — edges must have been re-pointed to the new id).
- `validate_projection`: a `rekeyed` tombstone's `rekeyed_to` resolves to an **active-or-candidate**
  (the mint preserves the old status — a candidate rekey is first-class, decision D), ***different*-type,
  same-family, indexed, non-self** node — **and** carries the **same name-hash, differing only by prefix**.

`merged` (absorbed into a *different existing node*, lossy) and `rekeyed` (the *same logical node, relabeled*,
1:1) are distinct governance events and stay semantically separate — consistent with the project's instinct to
keep `hidden`/`evidence_hidden`/`archive_candidate` distinct rather than overload.

### C. Identity contract — scope, subject/proposal, new-id derivation

- **Scope:** entity-family only — `entity | person | organization | project`, **any direction**. **Concept and
  any concept↔entity-family move are excluded** (a cross-family *type* change, not a *subtype* change; the
  review type is named `change_entity_subtype`). Deferred as a hypothetical future `change_node_type`.
- **Subject:** `subject: {node_id, to_type}` — the **proposed identity change** (the node *and* its target
  subtype) is the unit of human judgment, so `review_id = hash(type, subject)` is keyed to it. This
  deliberately mirrors merge's full-change-identity subject `{survivor, absorbed}` rather than a bare
  `{node_id}`: because `create_review_item` is idempotent across pending/**approved**/**rejected**, a bare
  `{node_id}` subject would reuse one `review_id` for *every* target and so **permanently lock a node out of
  any future retype once one target is rejected**. With `{node_id, to_type}`, rejecting `ent→org` blocks only
  that exact change, not `ent→per`.
- **Proposal:** `proposal: {to_type: <entity|person|organization|project>}`, retained for readability and
  **required to equal `subject.to_type`** (else a typed `to_type_mismatch` skip). The existing
  `_subject_references` matcher still keys on `subject.node_id`, so ADR-0050's withdrawal + approved-gate
  machinery covers a pending rekey unchanged — and **applying one retype withdraws any competing pending/
  deferred retypes of the same old `node_id`** (they all match on `node_id`). **No proposal-update/supersede
  semantics are added** (v1 keeps it simple — re-proposing the *same* `(node_id, to_type)` is an idempotent
  no-op via the existing filer).
- **Old id must be canonical (security):** before deriving the new id the executor requires `old_id` to
  **fullmatch** `^(cpt|ent|per|org|prj)_[0-9a-f]{16}$` — a tighter check than merge's `_is_safe_id`, mirroring
  `claims.CLAIM_ID_RE` / `citations._SOURCE_ID` + the `f5ba86a` fullmatch hardening; a malformed/historical/
  tampered id → typed skip `noncanonical_node_id`, never minting a malformed target. The check validates id
  **shape**, not scope: a well-formed *concept* id (`cpt_…`) passes here and is then caught by the
  entity-family scope guard as `out_of_scope` — the honest reason (it is a valid id, just not retypable),
  distinct from a genuinely malformed id.
- **New-id derivation — prefix substitution on the frozen hash, never re-hashing title/name:**
  `new_id = prefix(to_type) + "_" + old_id.split("_", 1)[1]` (`ent_abc123… → org_abc123…`). This is forced by
  decision B's invariant **and** by ADR-0021's frozen-id model: a node's id-hash is frozen at creation while
  its title/slug may have drifted since, so re-hashing the *current* title would produce a *different* hash and
  break "same hash, differ only by prefix." Prefix-substitution carries the frozen hash verbatim → the
  invariant holds **unconditionally, even for renamed nodes** — and it is **tamper-proof** (no untrusted
  name/title feeds the id; pure string surgery on the canonical-checked id). The new node copies
  `title`/`aliases`/`confidence` from the old node's authoritative **page** and **derives its source links from
  the re-pointed active `mentions` edges** (`graph.sources_for_node` — the projection authority;
  concept/entity pages do not own `source_ids` in frontmatter); only the **id-prefix + directory** change (the
  name-derived slug is unchanged → it lands at `NODE_DIR[to_type]/<same-slug>.md`).

### D. Retypable iff `status ∈ {active, candidate}` — diverge from merge's active-only

Merge is active-only because collapsing candidates muddies **promotion accounting**. Rekey is **1:1** — it
carries the *same* `mentions`/`source_ids` to the new id, so promotion-eligibility is identical before and
after; there is no accounting hazard. Since the dominant trigger (correcting a classifier-default generic
entity, ADR-0021) happens while the node is still a **candidate**, locking rekey to active-only would gut the
feature. So:

- `active → active`, `candidate → candidate` — the **new node preserves the old lifecycle status**.
- A retyped candidate keeps being a candidate and gets its own promotion proposal on the next `promote` pass
  (same sources) — self-correcting. A pending `promote_candidate_node` for the old id is **withdrawn** via the
  reused matcher.
- All other statuses skip with `node_not_retypable` (or `unexpected_from_status`):
  `deprecated_candidate`/`hidden`/`stale_candidate`/`archive_candidate`/`archived`/`merged`/`rekeyed`/
  `deleted` require their own prior governance path, not a subtype rekey.

### E. Apply mechanics (mirrors ADR-0050; simpler because the target is virgin)

New executor `app/workers/rekeys.py::apply_rekeys`, **graph-REQUIRED**, called in `run_apply`'s graph block
right after `apply_merges`; `change_entity_subtype` added to `EXECUTOR_BY_TYPE` + the graph-required type set
(`main.py`). Two-pass: a dry plan (detect block gates, never partial-apply), then apply.

- **Subject/derivation guards** (skip-typed, never partial-apply): `invalid_to_type` (`to_type` ∉
  entity-family) · `invalid_subject` · `to_type_mismatch` (`proposal.to_type != subject.to_type`) ·
  `noncanonical_node_id` (old id ∤ `^(cpt|ent|per|org|prj)_[0-9a-f]{16}$`, decision C) · `node_missing` ·
  `out_of_scope` (old node type ∉ entity-family) · **`noop_same_type`** (`to_type == old_type`) — a **typed
  no-op/skip, not an error; it never mutates or blocks other items** · `node_not_retypable`
  (status ∉ {active,candidate}) · idempotent true no-op if the old node is already `rekeyed` · `page_missing`.
- **Three virgin-target block gates** (decision A, dry-plan, before any write): **`target_subtype_id_exists`**
  (`graph.get_node(new_id)`) · **`target_subtype_page_exists`** (target page on disk) ·
  **`target_assertion_exists`** (`find_assertion` on any re-pointed full identity).
- **Edge re-point** — pure substitution (`graph.repoint_edge`, provenance preserved) for every **active** edge
  touching the old id; re-canonicalize the symmetric `{contradicts, duplicates}` pair (src < dst). The dry plan
  still runs `find_assertion` on each re-pointed full identity: in a **consistent** graph the virgin target has
  no pre-existing edges so it never fires, but a **drifted/tampered** graph can carry a dangling row →
  **`target_assertion_exists` BLOCK** (rekey **never** collapses/resurrects into the target — that is the
  identity-collapse decision A forbids; it blocks, unlike merge's status-keyed collapse/resurrect matrix).
  **Keep the `invalid_repoint_endpoint` BLOCK gate**: a `duplicates`/type-constrained edge whose
  `EDGE_ENDPOINTS` / `SAME_TYPE_EDGES` contract breaks under the new type (e.g. an `entity` marked `duplicates`
  of a same-type `entity`, now type-mismatched) → **block; the human resolves the duplicate first**. Self-edges
  are impossible on a virgin target (defensive check only).
- **Ordering** (re-point *before* render, so the new page's source links populate — decision C; mirrors
  merge's repoint-then-`_render_survivor`): (1) dry-plan all edges + the three block gates; (2) **upsert the
  bare new graph node** at `new_id`, status = old status; (3) `repoint_edge` old→new for every active edge;
  (4) **render the new node's page** — now `graph.sources_for_node(new_id)` is populated; title/aliases/
  confidence copied from the old page; (5) tombstone the old node (`rekeyed` + `rekeyed_to`/`rekeyed_at`/
  `rekey_review_id`, body collapses, graph mirror → `rekeyed`); (6) **fan-out re-render** — affected **Source**
  pages (`mentions`) only (their *projected* backlinks regenerate pointing at the new id); (7) withdraw
  pending/deferred old-id subjects (`_withdraw_b_subjects`, incl. a pending promotion + competing retypes);
  (8) audit `<rid>-rekeyed-<hex>.json` (old/new ids+types+paths, re-pointed edge ids w/ provenance, withdrawn
  subjects).
- **The only projected fan-out is Source `mentions` pages — there is NO `duplicates` partner fan-out in v1.**
  A `duplicates` edge is `SAME_TYPE_EDGES`, so a subtype change always makes its endpoints different types →
  the rekey **blocks** (`invalid_repoint_endpoint`) before any write; there is therefore never a surviving
  `duplicates` edge whose partner page could re-render. `related_to` re-points freely but is **unprojected**
  (concept→synthesis, ADR-0031): no rendered surface changes, so it is **audited but invents no projection
  obligation**. (The general rule: re-render a partner only when its page type projects the re-pointed
  relation — which, for a rekey, is `mentions` alone.)
- **Approved-unapplied gate (reused):** an approved-but-unapplied item referencing the old id (about to be
  tombstoned) **blocks** with `approved_unapplied_references_rekeyed` — exactly as merge blocks on its absorbed
  id.
- **Link behavior (mirrors merge):** literal `[[Entities/<slug>]]` resolves to the **tombstone** at the old
  path (which redirects "Retyped into [[Organizations/<slug>]]"); *projected* backlinks update via edge
  re-point + re-render. **No global literal-link rewrite.**

### F. Projector, dry-run, reindex, reversibility

- **Projector** `_effect_rekey` + `preview_change_entity_subtype`: `EFFECTED` iff the old node is `rekeyed` +
  its page tombstone's `rekeyed_to` equals the **computed** target id + that target node **exists** and is
  live (active/candidate) + no active edge touches the old id + the item approved (a wrong/missing/inactive
  target is never a false EFFECTED). `PENDING_APPLY` only when **nothing** is applied — the old id is fully
  live AND the target does not exist. Every **partial** live state → **`UNKNOWN partial_rekey_state`** (NOT
  reopenable — else reopen would orphan a half-applied rekey), covering both directions: (a) old tombstoned but
  the target is missing/wrong/inactive, and (b) the **half-mint** — a crash between the target mint and the
  old-id tombstone (`upsert_node` commits immediately), so the old id is still live but the target node **or**
  page already exists. v1 limit: unlike merge, a re-apply does **not** resume a half-mint (it would block on
  `target_subtype_id_exists`) — the operator reconciles the orphan target manually.
- **Dry-run** (ADR-0040) surfaces the node mint + `edges_repointed` + the tombstone diff (extends the existing
  `apply_sandbox` merge attribution).
- **Reindex** runs after apply; failure → **non-clean** `rekey_discovery_reindex_not_guaranteed` (old id may
  still surface from a stale index though it is tombstoned on disk).
- **Forward-only, auditable (not live-reversible) in v1**, mirroring ADR-0050. The `<rid>-rekeyed-<hex>.json`
  audit is the reconstruction basis; a live **un-rekey** is a separate future governed op.

## Consequences

- Closes the prefix-changing `change_entity_subtype` deferral from ADR-0041/0050 with the smallest rekey,
  hardening the rekey pattern (virgin-target mint + re-point + tombstone) before `split_entity`.
- One new lifecycle status (`rekeyed`) and two validator branches; the `RETENTION_DEFAULT_STATUSES` exclusion
  gives discovery suppression for free, so no new retrieval lever is introduced.
- A sharper validator than merge can express: `rekeyed_to` is provably a lawful subtype relabel (same name-
  hash, prefix-only delta).
- Candidate support (the divergence from merge) keeps the primary use case (classifier-default correction)
  alive without expanding into unsafe lifecycle repair.
- Forward-only keeps v1 bounded; un-rekey, collision-as-merge, concept↔entity moves, and `split_entity` remain
  explicit, documented follow-ups.
- **Producer (extractor) alignment:** the concept/entity extractor already detects subtype conflicts and files
  a `change_entity_subtype` review; it now writes the ADR contract `subject {node_id, to_type}` +
  `proposal {to_type}` (`from`/name → `context`), and **withholds** a *concept↔entity-family* conflict (a
  cross-family type change, not a subtype rekey — a future `change_node_type`), since filing an
  always-`out_of_scope`-skipped review would be misleading. Only entity-family↔entity-family conflicts file a
  review.

## Tests (design intent; written at implementation)

- Subtype rekey `ent_→org_` (and the reverse): new node minted at the prefix-substituted id with the **same
  hash**, old node tombstoned `rekeyed`/`rekeyed_to`, page moved `Entities/→Organizations/`, status preserved
  (active and candidate cases).
- The **three virgin-target block gates**, each before any write, surfaced in the dry-run:
  `target_subtype_id_exists` on an occupant of **any** `NODE_STATUSES` value (the gate is status-agnostic —
  active, candidate, stale_candidate, deprecated_candidate, archive_candidate, archived, delete_candidate,
  deleted, hidden, evidence_hidden, and the `merged`/`rekeyed` tombstones); a target **graph node absent but the
  target page present on disk** → `target_subtype_page_exists`; a pre-existing inactive/dangling target
  **assertion identity** → `target_assertion_exists`. None partial-applies (no node minted, no page written).
- A **noncanonical/malformed old id** → `noncanonical_node_id` *before* new-id derivation; `proposal.to_type !=
  subject.to_type` → `to_type_mismatch`.
- Re-proposal: a **rejected `ent→org` does not block a later `ent→per`** (distinct `review_id` via
  `subject.to_type`); applying one retype **withdraws a competing pending/deferred retype of the same node**.
- `noop_same_type` is a typed no-op that mutates nothing and does not block sibling items.
- `node_not_retypable` for each excluded status; `out_of_scope` for a concept/claim subject; `invalid_to_type`
  for a non-family / concept target.
- Candidate `ent_→org_`: the new node **preserves `candidate`**, a pending `promote_candidate_node` for the
  old id is **withdrawn**, and the **old id does not promote**.
- Edge re-point: `mentions` (Source-page fan-out) and `related_to` audited-but-not-re-rendered (unprojected);
  an active `duplicates`-to-same-old-type edge **blocks** the rekey (`invalid_repoint_endpoint`) — asserting
  there is NO surviving-duplicates partner fan-out in v1.
- Withdrawal of a pending/deferred old-id subject (incl. `promote_candidate_node`); `approved_unapplied_
  references_rekeyed` blocks an approved-but-unapplied old-id reference.
- Renamed-node rekey: the new id derives from the **frozen hash**, not the current title.
- The `rekeyed` tombstone body keeps the required **`> [!summary]`** callout (mirrors the `merged` tombstone),
  per the CLAUDE.md/AGENTS.md page standard.
- Projector `EFFECTED`/`PENDING_APPLY`/`partial_rekey_state`; reopen excluded on partial; dry-run mint +
  `edges_repointed` + tombstone diff; reindex-failure → non-clean `rekey_discovery_reindex_not_guaranteed`.
- Validators: `validate_graph` rejects an active edge with a `rekeyed` endpoint; `validate_projection` rejects
  a `rekeyed_to` that is same-type, neither-active-nor-candidate, cross-family, self, or a different name-hash (a malformed
  same-hash case, not only wrong lifecycle/type).
