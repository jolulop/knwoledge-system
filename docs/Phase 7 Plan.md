# Phase 7 Plan — Autonomous Maintenance

**Status:** In progress — **slices 7-1 (`/jobs/lint`) and 7-2 (stale/retention + `archive_source`)
implemented**; 7-3 pending (design-locked 2026-06-23 via grill gate).
**Governing ADR:** [ADR-0036](adr/0036-phase-7-autonomous-maintenance.md). Read it first.
**Predecessors:** Phases 1–6 complete + pushed. Phase 7 builds the maintenance surface over the existing
ingest/enrich/retrieve/review machinery; it adds no new runtime and no second authority.

> [!summary]
> Phase 7 adds deterministic, job-recorded **maintenance passes** — `/jobs/lint`, `/jobs/reindex`,
> `/jobs/stale-check` — that **detect health problems and propose** governance review items, acting
> autonomously only on safe non-destructive ops. **No scheduler/daemon** (OS cron). The one new executor is
> reversible **`archive_source`** (status transition only); **raw bytes are never moved, rewritten, or
> deleted**; `delete_raw_file`/merge/split/cache-purge stay record-only. *(A runtime eval job is deferred —
> the golden set is a fake CI fixture; real-vault eval is future work.)*

---

## 1. Scope
**In:** `/jobs/lint` (structural validators + new semantic checks incl. missing-raw) · `/jobs/reindex`
(index + keyword only, **no vector**) · `/jobs/stale-check` (source retention + **cache-purge candidate**
detection) · the stale/retention **producer** + reversible **`archive_source` executor** in
`/reviews/apply` · `docs/Operations.md` (OS-**cron recipe** + manual eval smoke) + a **no-daemon contract
test**. Rename `archive_raw_file → archive_source`; add `missing_raw_source` + `purge_response_cache`.
**Eval job deferred** (the golden set is a fake-adapter CI fixture; real-vault eval is future work).

**Out / deferred:** any scheduler/daemon; graph-curator duplicate/merge/split detection + their executors;
`delete_raw_file` / `hide_content` / `mark_semantic_duplicate` / cache-purge **executors** (record-only or
manual); physical raw archival / `include_raw` backup; auth/CSRF / non-loopback bind (Phase 8).

**Invariants:** detect-and-propose (semantic/destructive → review item, never autonomous action);
maintenance acts autonomously only on safe deterministic non-destructive ops; **retention executors may
change lifecycle/status + retrieval defaults but never move/rewrite/delete raw bytes**; lint health is an
*outcome report*, not an aborted pass; the graph stays the single edge authority (no curator store).

---

## 2. `/jobs/lint` (slice 7-1)
A job-recorded health pass that **completes and reports**, even when health fails.
- **Structural (report-only — fixed by regen, no governance):** broken wikilinks, missing frontmatter,
  missing summary callout, missing/uncited citations. Wraps the deterministic `validate_*` checks as a
  job report (not just script exit codes), so `validators_ok`-style health is recorded.
- **Semantic (governance → review items) — shipped in 7-1:** orphan / under-supported (<2-source) active
  concept → `deprecate_wiki_page`; **missing raw** (catalogued raw file absent, path-confined under
  `root/raw` + `is_file()`; an absolute/escaping path → an explicit `invalid_raw_path` finding, no
  absolute-path leak) → **high-severity finding + `missing_raw_source`** (record-only); **uncited active
  claim** → report-only backstop. Contradictions stay owned by the existing contradiction producer.
  *(**Deferred** to a later "lint heuristics" slice: summary-rot and stale-claim drift checks — they need
  real fingerprint/drift heuristics, better as their own slice than half-done here.)*
- Returns a typed report `{status: "healthy"|"degraded"|"failing", validators[], findings:[{check,
  severity, subject, detail}], by_check, review_items_filed (newly created), review_items_existing
  (already in ledger), graph_available, job_id}`; appends `wiki/log.md`. **None of these states are a
  5xx** — health is an outcome (`degraded` = coverage incomplete, e.g. graph absent).
- Idempotent: re-running files no duplicate review items (`subject={source_id|node_id}`-keyed); reruns
  report matches under `review_items_existing`, not `review_items_filed`.

---

## 3. Stale / retention (slice 7-2) — design-locked (ADR-0036 decision 13)
- **Manifest is the durable Source lifecycle authority** (the missing piece): add `manifest["status"]`
  (default `active`) + `manifests.set_status(...)`; the Source renderer reads `manifest.get("status",
  "active")` and folds it into the Source-page `input_fingerprint`; status flows `manifest → Source page →
  nav index → retrieval`. Source pages stay a pure projection (never self-preserve status). `validate_wiki`
  gains a Source page-status == manifest-status check. `retention_class` stays distinct from `status`.
- **`/jobs/stale-check`** producer (detect-and-propose, never acts): **archive** candidate =
  `status==active` and `modified_at` age (fallback `discovered_at`) ≥ `older_than_years_archive_candidate`
  → one **`archive_source`** proposing `active → archive_candidate` (age in proposal, single gate, P1).
  **Ephemeral delete** candidate = `retention_class==ephemeral` and `discovered_at`/first-seen age >
  `delete_candidate_after_days` → **`delete_raw_file`** (record-only forever). Idempotent
  (`subject={source_id}`).
- **`apply_archive_sources` executor** (new, `app/workers/retention.py`; wired into `/reviews/apply`): on
  an approved `archive_source`, flip **`active → archive_candidate`** (the honest v1 terminal — excluded
  from default retrieval via `RETENTION_DEFAULT_STATUSES`, reversible, **no physical move**, not
  `archived`) on the **manifest**, re-render the Source page, mirror the graph source node, reindex
  (caller-owned). Scope-guarded + idempotent (only transitions an `active` source); raw untouched.
- **Rename `archive_raw_file → archive_source`** in `review.yaml` + `REVIEW_TYPES` (no producers today).
- `delete_raw_file` remains record-only **forever** (manual execution only, outside `/reviews/apply`).
- **Duplicate detection deferred** (exact SHA dupes already collapse to one `source_id`; semantic/near
  duplicates need a separate similarity design).

---

## 4. Reindex / cache / cron (slice 7-3) — design-locked (ADR-0036 decision 14)
- **`/jobs/reindex`** — job-recorded; runs **`rebuild_index.py` + `reindex_keyword.py` only** (cheap,
  deterministic, key-free). **No `vector` parameter** — the vector index stays the explicit
  `reindex_vector.py` (ADR-0033), so maintenance never triggers an embedding-server side effect.
- **Eval job — deferred (over-scoped).** `golden_questions.yaml` is a fake-adapter CI fixture, so it can't
  run as a real-vault eval; the regression stays gated by the CI suites (`test_query_evals`,
  `test_retrieval_evals`), with a **manual opt-in real-model smoke recipe in `docs/Operations.md`**. A
  real-vault eval corpus is future work.
- **Cache-purge candidates** — **one aggregate, record-only `purge_response_cache`** review item (stable
  subject `{"scope": "response_cache"}`), folded into **`/jobs/stale-check`**. Filed when
  `db/llm_cache.sqlite` exceeds `cache_ttl_days`/`cache_max_mb`. **No executor, never in `_APPLY_TYPES`**
  (purge forfeits reproducibility, ADR-0027 — manual only). Proposal/log/API carry **only** counts / total
  size / cap / oldest age / candidate counts — **never** `response_json`/prompts/keys. Detection never
  deletes or mutates the cache. `/jobs/stale-check` reports **live cache stats every run** (even when the
  item exists): missing cache DB → `cache_present: false`, no finding; corrupt/unreadable → a
  **warning/degraded cache report**, never a reason to skip source-retention checks. The pass appends
  `wiki/log.md` + records metadata for **both** source and cache retention.
- **Cron + no-daemon** — operator cadence recipe (lint weekly, stale monthly, reindex/backup as desired) +
  the manual eval-smoke recipe + the external/opt-in raw-backup note go in **`docs/Operations.md`** (the
  Plan stays design accounting). A **contract test** asserts importing/serving the app starts no
  scheduler/daemon/background thread.

---

## 5. Testing posture (key-free where possible, deterministic)
- `/jobs/lint|reindex|stale-check`: creates a job row, returns a deterministic report, records failures,
  appends `wiki/log.md`.
- Retention: an old source proposes `archive_source`; ephemeral past policy proposes `delete_raw_file`
  (record-only); **no raw mutation**. Approved `archive_source` → status flips on manifest+page+graph,
  source excluded from default retrieval but found via an explicit status filter including
  `archive_candidate`; idempotent.
- Raw-missing: a missing manifest occurrence → high-severity finding + `missing_raw_source` (not a clean
  pass, not an aborted run).
- Idempotency: producers over the existing large pending queue file no duplicates on rerun.
- Cache-retention: expired/oversize cache → review-gated purge candidate; **no automatic purge**.
- Eval job: report-only (no review items, no mutation).
- **No-daemon contract test:** importing/serving the API starts no scheduler/background thread.
- Lint LLM-free where possible (the semantic checks reuse the graph, not new LLM calls); any LLM-touching
  check is fake-adapter-gated like prior phases.

---

## 6. Sub-slices (each committable + validated)
| Slice | Deliverable |
|---|---|
| **7-1** ✅ | `/jobs/lint`: structural validators as a job report + semantic checks (orphan / <2-source concept, uncited claim, **missing-raw** with path confinement + `invalid_raw_path`) → report + governance review items (`deprecate_wiki_page`, `missing_raw_source`); 3-state health (healthy/degraded/failing); filed-vs-existing item counts; `wiki/log.md` append. Tests (12+). *(summary-rot / stale-claim drift deferred to a later heuristics slice.)* |
| **7-2** ✅ | `manifest["status"]` authority (set_status, Source render, validate_wiki check) + `/jobs/stale-check` producer (`archive_source` + ephemeral `delete_raw_file` record-only candidates; detect-always) + reversible **`apply_archive_sources` executor** in `/reviews/apply` (manifest→page→graph mirror, scope-guarded, schema-safe, keyword reindex, no raw move) + `archive_raw_file→archive_source` rename. Tests (14+). *(duplicate detection deferred.)* |
| **7-3** | `/jobs/reindex` (index+keyword only, no vector) + cache-purge candidate detection folded into `/jobs/stale-check` (aggregate record-only `purge_response_cache`, live stats, missing/corrupt-safe) + `docs/Operations.md` (cron recipe + manual eval smoke + raw-backup note) + **no-daemon contract test**. Eval job deferred (golden set is a fake fixture). Tests. |

---

## 7. Success criteria (Phase 7 done when)
- `/jobs/lint`, `/jobs/reindex`, `/jobs/stale-check` exist as deterministic job-recorded passes that
  report health, append `wiki/log.md`, and file governance review items for semantic findings — **lint
  may report failing health while still completing**.
- Retention is actionable: approved `archive_source` deterministically flips status on manifest + Source
  page + graph node and excludes the source from default retrieval, **without touching raw bytes**;
  `delete_raw_file` and identity/merge types stay record-only; cache purge stays candidate-only.
- No scheduler/daemon ships (cron recipe documented; contract test green); the graph authority model is
  unchanged; raw remains immutable.
- Full suite + ruff + validators green. → Phase 8 (the API-worker / multi-surface runtime).
