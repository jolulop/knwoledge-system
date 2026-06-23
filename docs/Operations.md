# Operations — running maintenance (Phase 7)

How to run the knowledge system's maintenance passes on a schedule. **There is no built-in scheduler or
daemon** (ADR-0036): cadence is an OS `cron`/`systemd` entry you opt into, and every pass is a
deterministic, job-recorded, **detect-and-propose** operation — it surfaces health and files review items
but never acts on anything semantic/destructive on its own. Apply happens later, human-gated, via
`POST /reviews/apply`.

All commands run from the repo root (`/home/jolulop/code/knowledge-system`).

## Maintenance passes

| Pass | Endpoint | Script-equivalent | What it does |
|---|---|---|---|
| **Lint** | `POST /jobs/lint` | — | Structural validators + semantic checks (orphan/under-supported concept, uncited claim, **missing raw**) → health report + governance review items. |
| **Stale / retention** | `POST /jobs/stale-check` | — | Stale sources → `archive_source` candidates; ephemeral past window → `delete_raw_file` (record-only); **LLM-cache** over TTL/size → `purge_response_cache` (record-only). Reports **live cache stats** every run. |
| **Reindex** | `POST /jobs/reindex` | `scripts/rebuild_index.py` + `scripts/reindex_keyword.py` | Rebuild `wiki/index.md` + refresh the keyword index. **Never the vector index.** |
| **Backup** | — | `scripts/backup.py` | Snapshot manifests, db (incl. graph), wiki, reviews, policies. |
| **Apply** (human-gated) | `POST /reviews/apply` | — | Deterministically apply *approved* decisions (promotion, contradiction, synthesis, deprecation, archive). |

Maintenance passes are **key-free**. Only the LLM *producers* (claims/concepts/contradictions/synthesis)
and `POST /query` need `ANTHROPIC_API_KEY`.

## Cron recipe (example)

Run the app on loopback, then drive the passes with `curl`. Adjust cadence to taste — the Build Spec
suggests lint weekly, stale/retention monthly.

```cron
KS=/home/jolulop/code/knowledge-system
# m h dom mon dow   command   (KS = repo root; app already running on 127.0.0.1:18000)
# Weekly lint — Mondays 03:00
0 3 * * 1   curl -fsS -X POST http://127.0.0.1:18000/jobs/lint            >> "$KS/wiki/log.md.cron" 2>&1
# Monthly stale/retention + cache check — 1st of month 03:30
30 3 1 * *  curl -fsS -X POST http://127.0.0.1:18000/jobs/stale-check     >> "$KS/wiki/log.md.cron" 2>&1
# Weekly reindex — Mondays 03:15
15 3 * * 1  curl -fsS -X POST http://127.0.0.1:18000/jobs/reindex         >> "$KS/wiki/log.md.cron" 2>&1
# Daily backup — 02:00
0 2 * * *   cd "$KS" && uv run python scripts/backup.py                   >> "$KS/wiki/log.md.cron" 2>&1
```

Or run the passes directly as one-shot scripts under `systemd` timers — same endpoints, same job records.
Each pass writes a job row to `db/jobs.sqlite` and appends `wiki/log.md`, so progress survives restarts
(state is on disk, never chat context). **Review the queue** (`/ui/reviews`) and **apply** on your own
cadence — nothing is applied automatically.

## Caveats / out of scope

- **No scheduler ships.** Importing or serving the app starts no background thread (guarded by a contract
  test). You own the cron/timer.
- **Vector index** is refreshed only by `scripts/reindex_vector.py` (explicit; needs a configured local
  embedding server). `/jobs/reindex` never touches it.
- **Raw-file backup is external.** `scripts/backup.py` backs up *manifests*, not the raw bytes (they can
  be large) — back up `raw/permanent/` yourself, or via your own storage policy. The lint **missing-raw**
  finding is the safety net that flags a lost raw file.
- **Cache purge is manual.** The `purge_response_cache` review item is record-only — purging
  `db/llm_cache.sqlite` forfeits LLM reproducibility (ADR-0027), so the system never does it for you.

## Manual answer-quality smoke (optional, key-required)

There is **no automated eval job** in v1 — `evals/golden_questions.yaml` is a fake-adapter CI fixture, not
a real-vault corpus (ADR-0036 decision 14). The structural regression gate is the CI suites:

```bash
uv run pytest -q tests/test_query_evals.py tests/test_retrieval_evals.py
```

For a quick *real-model* sanity check against your live vault, ask a few known questions and eyeball the
cited answers (requires `ANTHROPIC_API_KEY`):

```bash
curl -fsS -X POST http://127.0.0.1:18000/query \
  -H 'content-type: application/json' \
  -d '{"question": "what does the vault say about <topic>?", "mode": "auto"}' | python3 -m json.tool
```

A real-vault eval corpus (key-required `/evals/run`) is future work.
