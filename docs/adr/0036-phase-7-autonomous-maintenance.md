# ADR-0036 — Phase 7: Autonomous Maintenance

**Status:** Accepted (design-locked 2026-06-23 via grill gate). No code yet.
**Supersedes/extends:** ADR-0004 (Claude Code = supervised maintenance), ADR-0002/0024 (raw immutable),
ADR-0018/0035 (review ledger + decoupled apply), ADR-0032 §8 (retention-aware retrieval),
ADR-0027 (response cache). Read `policies/retention.yaml` and Build Spec §9.2/§12.

> [!summary]
> Phase 7 adds **maintenance passes** — deterministic, job-recorded, **detect-and-propose** operations
> (`/jobs/lint`, `/jobs/reindex`, `/jobs/stale-check`, eval) that surface health problems and file
> review items for anything semantic/destructive, acting autonomously only on safe non-destructive ops.
> **No scheduler/daemon** (scheduling is an OS-cron recipe the operator opts into). Retention is made
> *actionable* via a reversible **`archive_source`** executor (status transition only); **raw bytes are
> never moved, rewritten, or deleted**, and `delete_raw_file` stays record-only forever.

---

## 1. Context

Phases 1–6 ingest, enrich, retrieve, answer, and **govern** changes through human review. The Build Spec
§9.2 envisions autonomous agents on cadences (lint weekly, stale/retention monthly, eval weekly), but the
maintenance surface is unbuilt: `/jobs/lint|reindex|stale-check` are planned-only, `policies/retention.yaml`
declares statuses + a cache-purge bound marked "not implemented", and the review types
`archive_raw_file`/`hide_content`/`mark_semantic_duplicate`/`merge_entities`/`split_entity` exist with no
producers and no executors. ADR-0004 frames the current runtime as *supervised* and defers truly
autonomous scheduling to future API workers.

## 2. The load-bearing decisions

**1. Runtime = triggerable detect-and-propose passes; no daemon.** Phase 7 ships the maintenance
*operations* as deterministic, job-recorded passes (scripts + `/jobs/*` endpoints), each writing a job
row to `db/jobs.sqlite` and appending `wiki/log.md`. **Scheduling is delegated to the OS** (a documented
`cron`/`systemd` recipe the operator opts into) — Phase 7 builds **no in-process scheduler or background
daemon**, and importing/serving the API starts none (a contract test guards this). ADR-0004 (autonomous
scheduling "eventually" via API workers, not now) + the single-user local-first posture make a daemon
pure operational cost (process lifecycle, concurrency with the live API, partial-run recovery) for no
single-user benefit. "Autonomous" here means *unattended deterministic detection that proposes work*, not
*unattended action*.

**2. The detect-and-propose invariant.** Maintenance **acts autonomously only on safe, deterministic,
non-destructive operations** (rebuild index, reindex keyword, backup, expired-cache *candidate*
detection). **Anything semantic or destructive is detected and *proposed* as a review item** (Phase 6
ledger), never executed by the maintenance pass itself. The LLM/heuristics propose; a human disposes;
`/reviews/apply` actions the approved, executor-backed subset.

**3. Lint health is an outcome, not an abort.** A maintenance pass **completes its job and reports
health**, including a failing health verdict. **"Failing lint" is an outcome report, not an aborted
maintenance pass** — the job still records, still reports every finding, still appends `wiki/log.md`.
(This mirrors Phase 6 apply's non-transactional posture: surface the problem, don't pretend the run
didn't happen.) Distinguish this from the *deterministic validators* (`validate_*`), which may still hard-
fail on a true integrity violation (e.g. a **mutated** raw file, ADR-0024).

**4. Retention executor invariant — reversible status only, never raw bytes.**
> Phase 7 retention executors may change lifecycle/status and retrieval defaults; they may **not** move,
> rewrite, or delete raw bytes.

**Archive = a reversible lifecycle/status transition** applied to the **manifest + Source page + graph
node mirror**, then reindex — the same render-path-mirror pattern Phase 6's deprecation executor uses. It
is **not** a physical move or delete. Archived material **stays indexed** with status metadata and is
**excluded from default retrieval/navigation via the existing status filter** — an explicit
`source_status=archived` still finds it. This is already plumbed: `graph.NODE_STATUSES` already includes
`stale_candidate/archive_candidate/archived/delete_candidate/deleted`, and
`search.RETENTION_DEFAULT_STATUSES = ("active", "deprecated_candidate")` already excludes everything else
unless a caller asks (ADR-0032 §8). The executor sets the status; the existing retrieval filter does the
rest. This avoids the operational mess of moving bytes (broken manifests, stale normalized paths, backup
ambiguity, a source archived but still needed as citation evidence).

**5. Review-type taxonomy changes.**
- **`archive_raw_file` → renamed `archive_source`** — the reversible status transition (active →
  `archive_candidate`), **executor-backed in v1**. The old name is misleading (the executor never touches
  the raw file); it has no producers/consumers today, so the rename is a clean `review.yaml` +
  `REVIEW_TYPES` edit.
- **New `missing_raw_source`** — governs a broken source record (a catalogued raw file gone missing).
  **Record-only in v1** (no executor): a high-severity lint finding + a review item; remediation
  (re-locate, or archive the orphaned source via a separate `archive_source` decision) is human. The
  system never auto-deprecates a source for a missing file — it could be a transient mount/drive failure.
- **`delete_raw_file` stays record-only *forever* in v1** — proposed, logged, human-decidable, but
  **executed only manually, outside `/reviews/apply`** (the single most destructive op; CLAUDE.md rule 1,
  ADR-0002).
- **`mark_semantic_duplicate`, `merge_entities`, `split_entity`, `hide_content` stay record-only** —
  produced where cheap, but identity re-keying (merge/split) and the graph-curator duplicate detector are
  **deferred** to a later phase (too much risk for Phase 7).

**6. Retention/stale lifecycle is single-gate (collapse).** The stale-check producer files **one**
`archive_source` review item proposing `active → archive_candidate`, carrying age/staleness evidence in
the proposal. `stale_candidate` remains valid vocabulary (and review-gated/excluded-by-default if ever
used) but Phase 7 does **not** add a two-gate stale→archive flow — the operator decides "archive this old
source?" once.

**7. `/jobs/reindex` defaults to cheap deterministic surfaces only.** `rebuild_index.py` (wiki/index.md)
+ `reindex_keyword.py` (BM25). The **vector** reindex stays **explicit opt-in** (ADR-0033) — it depends on
the embedding server/config and is cost/latency-heavy, so it must never be a default maintenance side
effect.

**8. Lint findings: structural = report-only; semantic = governance review items.**
- *Report-only (structural defects, fixed by regen — no governance):* broken wikilinks, missing
  frontmatter, missing summary callout, missing/uncited citations.
- *Review-item (governance proposals):* orphan / under-supported (<2-source) concept → `deprecate_wiki_page`;
  stale/old source → `archive_source`; missing raw → `missing_raw_source`; duplicate source →
  `mark_semantic_duplicate` (record-only). **Contradictions remain owned by the existing contradiction
  producer** (not re-implemented in lint).

**9. Eval job is report-only.** Runs the golden questions (`evals/golden_questions.yaml`) and returns
pass/fail + per-case results as a regression signal. **No review items, no world mutation.**

**10. Cache purge = candidate detection only.** The retention pass detects expired/oversize
`db/llm_cache.sqlite` entries (per `policies/retention.yaml` `cache_ttl_days`/`cache_max_mb`) and files a
review-gated `purge_response_cache` (or similar) review item. **No purge executor in v1** — bulk purge
forfeits reproducibility (ADR-0027), so it stays human-gated and manual.

**11. Idempotency.** New producers key `review_id` on `subject={source_id}` where source-scoped (with
type-specific subject fields only where needed). Phase 6 `create_review_item` is already idempotent on
`(type, subject)`, so reruns over an existing queue file **no duplicates**.

**12. No change to the core graph authority model.** The graph stays the single edge authority with node
metadata mirrored from wiki/manifests (ADR-0029/0030). Phase 7 adds **no second graph authority and no
separate curator store**.

## 3. Scope (v1) and slices

**In v1:** `/jobs/lint` (structural validators + new semantic checks incl. missing-raw), `/jobs/reindex`
(cheap deterministic default), `/jobs/stale-check` (+ retention candidate detection); the stale/retention
**producer** + the reversible **`archive_source` executor** wired into `/reviews/apply`; an **eval** job;
**cache-purge candidate** detection; an OS-**cron recipe** + the **no-daemon contract test**.

**Deferred (out of v1):** graph-curator duplicate/merge/split detection + their executors; physical raw
archival / `include_raw` backup; any scheduler daemon; `hide_content` / `mark_semantic_duplicate`
executors; cache-purge executor.

**Slices (each committable + validated):**
- **7-1** — `/jobs/lint` job: structural validators wired as a job-recorded report + new semantic checks
  (orphan / <2-source concept, stale/uncited claim, summary rot, missing-raw) → structural report +
  governance review items (`deprecate_wiki_page`, `missing_raw_source`). `wiki/log.md` append.
- **7-2** — stale/retention producer (`/jobs/stale-check`): age-based `archive_source` candidates +
  duplicate detection (`mark_semantic_duplicate`, record-only) + the reversible **`archive_source`
  executor** (manifest + Source page + graph mirror + reindex) wired into `/reviews/apply`. Rename
  `archive_raw_file → archive_source`.
- **7-3** — `/jobs/reindex` (deterministic surfaces) + eval job (report-only) + cache-purge candidate
  detection; cron/backup operator docs + the no-daemon contract test.

## 4. Consequences

Phase 7 makes the Build Spec's maintenance agents *real* without inventing a second runtime or a second
authority: deterministic passes detect, propose into the existing Phase-6 ledger, and act autonomously
only where it is safe. Retention finally becomes actionable (the `archive_source` executor) while the
raw/wiki split holds absolutely — raw bytes are never touched by any executor, and the most destructive
ops (raw delete, identity merge/split, cache purge) stay human-gated or record-only. The standing trades:
no unattended *action* (only proposals) and no daemon (scheduling is the operator's cron) — both
deliberate, both revisitable when a Phase-8 API-worker runtime exists. Auth/CSRF and any non-loopback
bind remain out of scope (a Phase-8 precondition). The load-bearing commitments — detect-and-propose
passes, lint-health-as-outcome, the reversible-status-only retention executor, the taxonomy changes
(`archive_source`/`missing_raw_source`/`delete_raw_file` record-only), and no-daemon — are fixed here;
the exact lint heuristics, the staleness thresholds, and the job-report shapes are tuned per slice.
