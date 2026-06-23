# Phase 7 Plan — Autonomous Maintenance

**Status:** In progress — **slice 7-1 (`/jobs/lint`) implemented**; 7-2/7-3 pending (design-locked
2026-06-23 via grill gate).
**Governing ADR:** [ADR-0036](adr/0036-phase-7-autonomous-maintenance.md). Read it first.
**Predecessors:** Phases 1–6 complete + pushed. Phase 7 builds the maintenance surface over the existing
ingest/enrich/retrieve/review machinery; it adds no new runtime and no second authority.

> [!summary]
> Phase 7 adds deterministic, job-recorded **maintenance passes** — `/jobs/lint`, `/jobs/reindex`,
> `/jobs/stale-check`, and an eval job — that **detect health problems and propose** governance review
> items, acting autonomously only on safe non-destructive ops. **No scheduler/daemon** (OS cron). The one
> new executor is reversible **`archive_source`** (status transition only); **raw bytes are never moved,
> rewritten, or deleted**; `delete_raw_file`/merge/split/cache-purge stay record-only.

---

## 1. Scope
**In:** `/jobs/lint` (structural validators + new semantic checks incl. missing-raw) · `/jobs/reindex`
(cheap deterministic default; vector explicit opt-in) · `/jobs/stale-check` (+ retention candidate
detection) · the stale/retention **producer** + reversible **`archive_source` executor** in
`/reviews/apply` · an **eval** job · **cache-purge candidate** detection · OS-**cron recipe** + a
**no-daemon contract test**. Rename `archive_raw_file → archive_source`; add `missing_raw_source`.

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

## 3. Stale / retention (slice 7-2)
- **`/jobs/stale-check`** — age-based candidate detection from manifest dates per `policies/retention.yaml`
  (`older_than_years_archive_candidate`, ephemeral `delete_candidate_after_days`): files **one
  `archive_source`** review item per stale source proposing `active → archive_candidate` (staleness/age
  evidence in the proposal — single gate, P1). Ephemeral past its window → `delete_raw_file` candidate
  (record-only). Duplicate-source detection → `mark_semantic_duplicate` (record-only).
- **`archive_source` executor** (new, reversible; wired into `/reviews/apply`): on an approved
  `archive_source`, set the lifecycle status on **manifest + Source page + graph node mirror**, then
  reindex. **No raw byte movement.** Archived sources stay indexed but are excluded from default retrieval
  (existing `RETENTION_DEFAULT_STATUSES` filter); `source_status=archived` still finds them. Idempotent /
  normalization-aware like the Phase-6 deprecation executor.
- **Rename `archive_raw_file → archive_source`** in `review.yaml` + `REVIEW_TYPES` (no producers today).
- `delete_raw_file` remains record-only **forever** (manual execution only, outside `/reviews/apply`).

---

## 4. Reindex / eval / cache (slice 7-3)
- **`/jobs/reindex`** — `rebuild_index.py` + `reindex_keyword.py` only (cheap, deterministic). Vector
  reindex is **explicit opt-in** (ADR-0033), never a default side effect. Job-recorded.
- **Eval job** — runs `evals/golden_questions.yaml`; returns pass/fail + per-case results. **Report-only:
  no review items, no mutation.**
- **Cache-purge candidates** — detect expired/oversize `db/llm_cache.sqlite` entries
  (`cache_ttl_days`/`cache_max_mb`) → review-gated `purge_response_cache` items. **No purge executor**
  (purge forfeits reproducibility, ADR-0027).
- **Operator docs + cron recipe** — a documented `cron`/`systemd` example for cadenced runs (lint weekly,
  stale monthly, eval weekly, backup daily) and the note that raw-file backup is external/opt-in. **A
  contract test asserts importing/serving the app starts no scheduler/daemon/background thread.**

---

## 5. Testing posture (key-free where possible, deterministic)
- `/jobs/lint|reindex|stale-check`: creates a job row, returns a deterministic report, records failures,
  appends `wiki/log.md`.
- Retention: an old source proposes `archive_source`; ephemeral past policy proposes `delete_raw_file`
  (record-only); **no raw mutation**. Approved `archive_source` → status flips on manifest+page+graph,
  source excluded from default retrieval but found via `source_status=archived`; idempotent.
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
| **7-2** | `/jobs/stale-check` retention producer (`archive_source` candidates, `delete_raw_file`/`mark_semantic_duplicate` record-only) + reversible **`archive_source` executor** in `/reviews/apply` (manifest+page+graph mirror, reindex, no raw move) + `archive_raw_file→archive_source` rename. Tests. |
| **7-3** | `/jobs/reindex` (deterministic surfaces) + eval job (report-only) + cache-purge candidate detection + cron/backup operator docs + **no-daemon contract test**. Tests. |

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
