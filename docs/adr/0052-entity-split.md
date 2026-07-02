# ADR-0052 — Entity split: `split_entity` as the id-spawning rekeying executor

**Status:** Accepted. **Design-locked 2026-07-01** (grill-phase, docs-only); **implemented 2026-07-02**
(`app/workers/splits.py::apply_splits` + projector `review_read._effect_split`; review round 2 added the
repair-safe idempotency + malformed-artifact guards below). This is
the **last and highest-blast-radius identity-surgery executor**, the `split_entity` branch deferred by
ADR-0041 and ADR-0050. It wires an executor onto the already-registered (but record-only) `split_entity`
review type, reusing the merge/rekey machinery. Split is the **inverse of merge**: one entity-family node's
evidence is divided into a surviving **primary** and a freshly-minted **spin-off**.

**Scope of v1:** split one entity-family node (`entity`/`person`/`organization`/`project`) into the same
node kept as the **primary** (unchanged id **and** name) plus **exactly one** spin-off. **Deferred**
(documented, own follow-ups): both-halves-renamed (= `split_entity` + a future `rename` composed), a
subtype-differing spin-off (= `split_entity` + `change_entity_subtype`), an N-way split (>2), moving
non-`mentions` edges to the spin-off, a live un-split (forward-only — reconstruct via the audit, and a future
inverse is `merge_entities(spin-off → primary)`), and a `split_from` graph edge / lineage query.

**Extends/claims:** ADR-0050 (identity-surgery machinery — `graph.repoint_edge`/`find_assertion`, the
dry-plan-then-apply two-pass, virgin-target block gates, the reconstructable `reviews/audit_log/` entry, the
ADR-0040 dry-run; split is merge run backwards), ADR-0051 (the virgin-target gates + `is_canonical`/compute-
and-verify id patterns + the half-mint projector safety), ADR-0018 (candidate promotion lifecycle — the
spin-off re-earns promotion), ADR-0021/0017 (frozen content-hash ids; rename is *design-locked but
unimplemented*, which bounds this design), ADR-0009 (path-containment — the graph-boundary slug guard covers
the spin-off's page write).

## Context — why split is the hardest, and what bounds it

Merge collapses two ids into one; rekey relabels one id to one; **split spawns**. It is the inverse of merge,
and the design leans on that inversion hard. Two facts bound v1:

1. **There is no `rename_node` executor.** ADR-0017 design-locks rename as "slug/wikilink rewrites + an
   id-level redirect," but nothing implements it. So any split model that renames a surviving half leans on
   machinery that doesn't exist — which forces the "primary keeps its original name" boundary (decision 1).
2. **The extractor keys on the surface name** (`node_id = prefix + name_hash`). A split of a same-surface-name
   conflation is a human override that re-extraction can partially undo (a moved mention whose source still
   says the ambiguous name re-attracts to the primary on re-extraction). This is the general identity-surgery-
   vs-re-extraction drift (merge/rename have it too), documented, not split-specific, out of scope.

## Decisions

### 1. The original id SURVIVES as the primary; one spin-off is minted (Option A) — no tombstone

The original node **A survives as the "primary," keeping its id AND name unchanged**; the other half is a
**freshly-minted spin-off B** whose id derives from its new name; only the spin-off's human-assigned
**partition** of A's evidence re-points to it. Split is thus the exact inverse of merge:

| | merge | split (this ADR) |
|---|---|---|
| survivor A | keeps id+name, **gains** B's edges | keeps id+name, **loses** the spin-off partition |
| other node | B tombstoned (`merged`) | B **minted** fresh (`candidate`) |
| edges moved | all of B's → A | only the spin-off partition of A's → B |

Rejected: **tombstone-the-original-and-mint-two-fresh.** A split tombstone would have **two** redirect
targets, so a literal `[[…/A-slug]]` link can't resolve to one — an ambiguity merge/rekey never have (their
tombstones redirect to a single node). Option A has **no tombstone and no redirect**: links to the original
stay on the primary. It also needs **no rename** (fact 1), re-points the **least** (only the spin-off
partition), adds **no new lifecycle status** (nothing is retired), and its rollback reuses the shipped merge
executor — `merge_entities(spin-off → primary)` — **once the spin-off is `active`** (merge is active-only, and
the spin-off is born `candidate` per decision 3, so rollback is available after it promotes, not immediately).

**v1 boundary:** the primary **keeps the original name**. "Both halves renamed" (e.g. `Apple` → `Apple Inc.` +
`Apple (fruit)`, neither stays `Apple`) is `split_entity` + a future `rename`, composed. The human designates
*which meaning keeps the name/id* and *what spins off*.

### 2. The partition contract — a source-keyed mention partition + an alias partition

- **Subject:** `{node_id: <A, the original>, spinoff_node_id: <B>}` — both ids (mirrors merge's explicit-pair
  subject), so `review_id` is distinct per `(A, spin-off)` and a rejected split-to-B never locks a split-to-C.
- **Proposal:** `{spinoff_name, spinoff_sources, spinoff_aliases}`. The executor recomputes
  `B = node_id(A.node_type, spinoff_name)` and **rejects a mismatch** with `subject.spinoff_node_id` (the
  rekey compute-and-verify pattern). The spin-off keeps **A's node_type** (a subtype-differing spin-off is
  out of scope).
- **`spinoff_sources` and `spinoff_aliases` are treated as sets — stable-deduplicated** (first-seen order
  preserved for audit/render determinism) before the guards below run. A duplicate entry is **normalized, not
  an operator-visible error**, so "proper subset" / "⊆ A.aliases" are unambiguous regardless of input
  repetition.
- **`spinoff_sources` is the required partition authority** — the mentions that **MOVE** `source→A` ⇒
  `source→B` (a true partition: each source is on exactly one side; **no copy**, so a source genuinely about
  *both* meanings is left on the primary — a documented v1 limit). Each must be a **canonical** `src_<16 hex>`
  id (typed `noncanonical_source_id` guard, reusing `manifests.is_source_id` — not left to fall through to
  `source_not_mentioned`); the set must be a **non-empty PROPER subset** of A's active mention sources → both
  halves keep ≥1 source (all-moved is a *rename*, none-moved a no-op — neither is a split); every listed source
  must have an **active** `mentions`→A edge.
- **`spinoff_aliases` is optional/advisory but validated** — must be `⊆ A.aliases`; moved aliases are removed
  from A and added to B.
- **The spin-off's name must not remain an alias on the primary (auto-move).** B's **title** *is*
  `spinoff_name`, so B owns that identity; if the normalized `spinoff_name` appears in A's aliases the executor
  **removes it from A** (in addition to `spinoff_aliases`) — else A would still claim B's name and re-conflate
  on search/extraction. Invariant: A's final aliases = `A.aliases − spinoff_aliases − {normalize(spinoff_name)}`,
  and A never retains `spinoff_name`.
- **Non-`mentions` edges stay on the primary.** In practice an entity carries `mentions` (its evidence — the
  load-bearing partition), plus rare `related_to` (synthesis→topic) and human `duplicates`; those are the
  primary's continuation and stay on A in v1 (moving them is a follow-up — no concrete need yet, and it would
  require an edge-partition UI). Separating the *evidence* is the whole point of a split; that's what v1 does.
- **Virgin spin-off:** B must pass the same three gates as a rekey target (decision 4).

### 3. The spin-off is born `candidate`; the primary keeps its status; provenance is audit + frontmatter

- **The spin-off B is ALWAYS born `candidate`, regardless of A's status.** This is the key divergence from
  rekey (which is 1:1, so it *preserves* status): **split divides the evidence**, so B's promotion must be
  *re-earned*. If B inherited `active` with <2 independent sources it would be a fabricated promotion the
  promote pass **never re-checks** (`promote_candidates` only scans `status='candidate'`). Born `candidate`,
  the existing promote pass evaluates B against *its* partition — ≥2 independent → auto-promote (recurrence),
  else stays candidate with a pending review. Promotion authority stays single-sourced (ADR-0018).
- **The primary A keeps its current status.** If A was `active` and the split leaves it with only one source,
  v1 does **not** auto-demote it (consistent with the existing active-page-preservation behavior,
  `concepts.py`) — a **documented v1 limit**.
- **Provenance — audit is authority; frontmatter is a convenience; no graph edge.**
  - **Durable record:** a reconstructable `reviews/audit_log/<rid>-split-<hex>.json` (source A, spin-off B,
    `spinoff_name`, moved sources, moved aliases, review id, timestamp) — the source of truth, since `wiki/`
    is gitignored/regenerable (mirrors merge's audit).
  - **Page convenience on B:** `split_from: <A>` + `split_review_id: <rid>` frontmatter — **page-preserved**
    (added to the preserved-frontmatter set alongside title/aliases, else a later `recompose` of B drops it).
    Advisory lineage, **not** a validator-enforced redirect (unlike `merged_into`/`rekeyed_to` — B is a live
    node, nothing follows `split_from`).
  - **No graph edge.** A `split_from` edge would need a new `edge_type` (EDGE_TYPES/EDGE_ENDPOINTS/validators/
    projection) for zero current query use — disproportionate; a lineage query is a clean follow-up.

### 4. Apply mechanics + guards (composes from merge/rekey; never partial-apply)

New executor `app/workers/splits.py::apply_splits`, **graph-REQUIRED**, called in `run_apply`'s graph block
after `apply_rekeys`; `split_entity` → `EXECUTOR_BY_TYPE` + `_APPLY_TYPES` (graph-required auto). Two-pass:
a dry plan (all guards + block gates, never partial), then apply. **No new lifecycle status.**

- **Subject/derivation guards** (skip-typed): `invalid_proposal` (missing `spinoff_name`) ·
  `noncanonical_node_id` (A; reuse the rekey canonical regex) · `node_missing` · `out_of_scope`
  (A ∉ entity family) · `spinoff_id_mismatch` (`node_id(A.type, spinoff_name) != subject.spinoff_node_id`) ·
  `spinoff_equals_primary` (name hashes to A) · `node_not_splittable` (A.status ∉ {active,candidate}) ·
  `page_missing`.
- **Partition guards:** `empty_partition` · `noncanonical_source_id` (a `spinoff_source` ∤ `src_<16 hex>`) ·
  `source_not_mentioned` (a listed source lacks an active `mentions`→A) · `full_partition_is_rename`
  (`spinoff_sources` not a proper subset — A must keep ≥1) · `alias_not_on_primary` (`spinoff_aliases ⊄ A.aliases`).
- **Block gates on B (reuse rekey's three virgin-target gates) + the ledger-slot gate:**
  `target_spinoff_id_exists` (`get_node(B)`) · `target_spinoff_page_exists` (B page on disk) ·
  `target_spinoff_assertion_exists` (`find_assertion` per would-be `source→B` mention — drift/tamper backstop) ·
  **`spinoff_promote_slot_taken`** — a `promote_candidate_node` for the computed B already exists in a
  **terminal** state (`approved/` or `rejected/`). Because `create_review_item` is idempotent across
  pending/approved/rejected (`reviews.py`), a stale *approved* slot would fabricate B's promotion
  (`pre_approved`) and a stale *rejected* slot would strand B (can't re-file); a **pending** slot is fine
  (reused). A virgin *node* B does not imply a virgin *ledger slot*.
- **Approved-unapplied gate on the PRIMARY A** — `approved_unapplied_references_primary`: an approved but
  not-yet-effected item referencing A (effect-status ∈ {`pending_apply`, `unknown`, `apply_deferred`}, the
  same gate merge/rekey use via `merges._approved_unapplied_block`) **blocks** the split. Split changes A's
  evidence partition while A survives, and `promote_candidates` promotes A on a `pre_approved` flag alone
  (`promote.py`), so an approved-unapplied `promote_candidate_node` for A + split-moves-sources-first would
  promote A under stale evidence. (This is the split analog of merge/rekey's absorbed/old-id gate — here it
  guards the *surviving* node whose inputs change.)
- **Ordering** (re-point before render; after the gates there are no more blocks): (1) upsert the bare **B**
  node (`candidate`, type = A's); (2) `repoint_edge` each moved mention `source→A` ⇒ `source→B`; (3) render
  **B**'s page (title=`spinoff_name`, aliases=`spinoff_aliases`, status `candidate`, sources from the graph,
  + `split_from`/`split_review_id`); (4) re-render **A**'s page (`aliases = A.aliases − spinoff_aliases −
  {normalize(spinoff_name)}`, remaining mentions, **status unchanged**); (5) **file B's
  `promote_candidate_node` review** (so the new
  candidate enters the promotion ledger — the promote pass won't file a pending item for a not-yet-independent
  candidate); (6) fan-out re-render the **moved sources' Source pages** (`affected_sources = spinoff_sources`
  — their mentions projection now shows B); (7) audit `reviews/audit_log/<rid>-split-<hex>.json`.
- **No pending-review withdrawal.** Split **retires no id** (A survives, sources survive, B is new), so no
  review references a tombstoned node — nothing to withdraw (the clean contrast with merge/rekey). A's own
  pending items are *not* withdrawn (A persists); its approved-but-unapplied items are instead handled by the
  `approved_unapplied_references_primary` **block gate** above. The only review *written* is B's promote item.
- **Projector `_effect_split`** — `EFFECTED` iff **all** hold: B graph node exists (`candidate` or already
  `active`); B page exists with `split_from == A` and `split_review_id == rid`; **each** `spinoff_source` now
  actively mentions B and **no longer** actively mentions A; A retains ≥1 active mention; **and B's promotion
  is accounted for** — a pending/approved/**rejected** promote_candidate_node for B exists, or B is already
  `active` (a terminal *rejected* promote is a deliberate human accounting — "split done, chose not to promote
  B" — a filled ledger slot, **not** a partial split, so it counts as accounted).
  `PENDING_APPLY` iff nothing is applied (B absent, A retains all sources + aliases). **Any** partial —
  including a half-mint (B rendered + mentions moved but the promote item not yet filed, since `upsert_node`
  commits immediately) — → **`UNKNOWN partial_split_state`** (not reopenable; reopen would strand a
  half-applied split, same safety as rekey's half-mint). A missing promotion ledger while B is `candidate`
  is a partial state, not EFFECTED.
- **Dry-run** (ADR-0040) surfaces it via the existing `apply_sandbox` diff (`nodes_added` B, `edges_repointed`
  the moved mentions, A/B/source page diffs). **Reindex-failure → non-clean**
  `split_discovery_reindex_not_guaranteed`.
- **Forward-only, auditable (not live-reversible) in v1**, mirroring merge/rekey; the audit entry is the
  reconstruction basis, and the natural inverse is `merge_entities(spin-off → primary)` **once the spin-off is
  `active`** (merge is active-only).

## Consequences

- Closes the last rekeying deferral (ADR-0041/0050); the identity-surgery family (merge, subtype-rekey,
  split) is complete. All three are forward-only, auditable, virgin-target-gated, and dry-run-previewable.
- Adds **no** new lifecycle status and **no** new edge type — split is expressed entirely as mint + edge
  re-point + page re-render, so its surface is smaller than merge's despite being the "hardest" op.
- The evidence partition (mentions) is the load-bearing thing; leaving non-`mentions` edges + the both-renamed
  and N-way cases as documented follow-ups keeps v1 bounded without inventing an edge-partition UI.
- Rollback reuses the shipped merge executor (`merge spin-off → primary`) **after the spin-off is promoted to
  `active`** (merge is active-only) — so forward-only does not *permanently* strand a mistaken split, but it is
  not an immediate undo: a still-`candidate` (e.g. one-source) spin-off is not mergeable until it promotes.
- Two pre-write gates protect the surviving/spawned identities beyond the virgin-target set: the
  `approved_unapplied_references_primary` gate (A's evidence must not shift under a stale approved effect) and
  the `spinoff_promote_slot_taken` gate (B's promotion ledger slot must be free of stale terminal records).
- Two documented v1 limits: no auto-demotion of a primary left with one source; and re-extraction drift on a
  same-surface-name conflation (both general, not split-specific).

## Tests (design intent; written at implementation)

- Split `A` (active, sources `{S1,S2,S3}`) with `spinoff_sources={S3}`: B minted `candidate` at
  `node_id(A.type, spinoff_name)`, S3's mention re-pointed A→B, A keeps `{S1,S2}` + active, B page carries
  `split_from`/`split_review_id`, B's `promote_candidate_node` filed, S3's Source page re-renders to mention B.
- Candidate A split preserves A `candidate`; both halves then evaluated by the promote pass on their
  partitions (≥2-independent B auto-promotes; else stays candidate + pending review).
- `full_partition_is_rename` (all sources moved), `empty_partition`, `noncanonical_source_id` (a malformed
  `spinoff_source`), `source_not_mentioned`, `alias_not_on_primary`, `spinoff_id_mismatch`,
  `spinoff_equals_primary`, `node_not_splittable` (each excluded status), `out_of_scope` (concept subject).
- Three virgin-target gates on B (id occupant any status / orphan page / dangling assertion) — each blocks
  before any write; plus `spinoff_promote_slot_taken` — a **terminal** (approved/rejected) promote record for
  computed B blocks, while a **pending** one does not.
- `approved_unapplied_references_primary`: an approved-but-unapplied `promote_candidate_node` for A blocks the
  split before any write (guards A's evidence from a stale approved promotion); an EFFECTED item for A does not.
- Alias partition: moved aliases removed from A, present on B; a non-A alias rejected; **`spinoff_name` present
  in A's aliases is auto-moved off A** (A's final aliases exclude it; B owns it as its title).
- Duplicate `spinoff_sources`/`spinoff_aliases` entries are **stable-deduplicated** (first-seen order kept),
  not rejected; the partition guards apply to the deduped set.
- Rollback timing: a one-source `candidate` spin-off is **not** immediately mergeable back into A (merge is
  active-only) — rollback waits on the spin-off's promotion.
- Projector: `EFFECTED` on a clean split; `PENDING_APPLY` before apply; half-mint (B minted, promote item not
  filed / mentions partially moved) → `UNKNOWN partial_split_state`; reopen 409 for a half-mint.
- No review withdrawn by a split; B's promote item is the only review written.
- Dry-run shows `nodes_added` B + `edges_repointed` + A/B/source diffs, live unchanged; reindex-failure →
  non-clean `split_discovery_reindex_not_guaranteed`.
- Rollback smoke: `merge_entities(B → A)` after a split restores A's mentions (audit-reconstruction basis).
