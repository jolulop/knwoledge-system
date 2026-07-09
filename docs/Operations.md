# Operations — running maintenance (Phase 7)

How to run the knowledge system's maintenance passes on a schedule. **There is no built-in scheduler or
daemon** (ADR-0036): cadence is an OS `cron`/`systemd` entry you opt into, and every pass is a
deterministic, job-recorded, **detect-and-propose** operation — it surfaces health and files review items
but never acts on anything semantic/destructive on its own. Apply happens later, human-gated, via
`POST /reviews/apply`.

All commands run from the repo root (`/home/jolulop/code/knowledge-system`).

## Starting the app

The **only supported launch** is the blessed entrypoint, which binds Uvicorn to `APP_HOST` through the
`assert_safe_bind` loopback guard so the bind can't drift from the check (ADR-0009):

```bash
uv run python -m app.backend        # or: uv run python scripts/serve.py
```

**Do not** run `uvicorn app.backend.main:app --host 0.0.0.0` directly — uvicorn's `--host` overrides the
bind *without* re-checking the guard, exposing the unauthenticated API. There is no app-level auth/CSRF
(loopback-only posture); `KS_ALLOW_INSECURE_BIND=1` is a narrow internal-transport escape hatch (trusted
private network / sidecar behind a TLS/auth proxy) and is **not** a substitute for auth.

## Maintenance passes

| Pass | Endpoint | Script-equivalent | What it does |
|---|---|---|---|
| **Lint** | `POST /jobs/lint` | — | Structural validators + semantic checks (orphan/under-supported item, uncited claim, **missing raw**) + quality heuristics (**summary rot**, **stale claim citations**, **topic starvation**) → health report + governance review items. |
| **Stale / retention** | `POST /jobs/stale-check` | — | Stale sources → `archive_source` candidates; ephemeral past window → `delete_raw_file` (record-only); **LLM-cache** over TTL/size → `purge_response_cache` (record-only). Reports **live cache stats** every run. |
| **Reindex** | `POST /jobs/reindex` | `scripts/rebuild_index.py` + `scripts/reindex_keyword.py` | Rebuild `wiki/index.md` + refresh the keyword index. **Never the vector index.** |
| **Backup** | — | `scripts/backup.py` | Snapshot manifests, db (incl. graph), wiki, reviews, policies. |
| **Review reconciliation** | — | `scripts/reconcile_reviews.py` | Withdraw extraction-stale unresolved review items (ADR-0057): pending/deferred promotes for tombstoned/missing nodes, recompose-provenance deprecations for resurrected nodes. Idempotent, audited per withdrawal; never touches approved/rejected. **Preflight fail-closed** (exits non-zero, withdraws nothing) on missing/empty/schema-mismatched graph DB or graph↔wiki status drift over the reviewed nodes. |
| **Apply** (human-gated) | `POST /reviews/apply` | — | Deterministically apply *approved* decisions: promotion, contradiction, synthesis, deprecation, **archive** (`active→archive_candidate`), **duplicate annotation** (`mark_semantic_duplicate` → `## Duplicates`, ADR-0041), the **visibility family** — hide/unhide for sources (`hide_content`/`unhide_content`, ADR-0043/0047), semantic pages (`hide_semantic_page`/`unhide_semantic_page`, ADR-0046/0047), claims (ADR-0048) and synthesis (ADR-0049, `evidence_hidden`) — the **identity-surgery family** — `merge_items` (ADR-0050/0059) and `split_item` (ADR-0052/0059) — and the non-rekeying **`change_item_type`** classification flip (ADR-0059). |
| **Apply preview** (dry-run) | `POST /reviews/apply/dry-run` | — | Apply-on-a-copy preview (ADR-0040): runs the same executors against a throwaway sandbox and returns the semantic mutation diff (graph/wiki/reviews/manifests) **without touching live state**. `GET /ui/reviews/apply` renders it before enabling Apply. |

Maintenance passes are **key-free**. Only the LLM *producers* (claims/items/contradictions/synthesis)
and `POST /query` need `ANTHROPIC_API_KEY`.

### Executor-backed review types (`POST /reviews/apply`)

Every review type below has a deterministic, key-free executor (the canonical set is `_APPLY_TYPES` in
`app/backend/main.py`; a parity test keeps this table in sync). Any other approved type is reported
honestly under `unapplied` (record-only / raw-touching).

| Review type | ADR | Effect on apply |
|---|---|---|
| `promote_candidate_node` | 0018/0035/0059 | Candidate knowledge item → `active` (an unclassified-sentinel candidate needs an `item_type` amendment first). |
| `resolve_contradiction` | 0031 | Acknowledge / supersede / reject a `contradicts` edge. |
| `propose_synthesis` | 0031 | Activate a `candidate` synthesis node. |
| `deprecate_wiki_page` | 0035 | Scoped page deprecation → `deprecated_candidate`. |
| `archive_source` | 0036 | Source `active` → `archive_candidate` (raw bytes untouched; manifest status flips). |
| `mark_semantic_duplicate` | 0041 | `## Duplicates` annotation edge (no id rewrite). |
| `hide_content` / `unhide_content` | 0043 / 0047 | Hide / unhide a source. |
| `hide_semantic_page` / `unhide_semantic_page` | 0046 / 0047 | Hide / unhide an item page. |
| `hide_claim` / `unhide_claim` | 0048 | Hide / unhide a claim. |
| `hide_synthesis` / `unhide_synthesis` | 0049 | Hide / unhide a synthesis (`evidence_hidden`). |
| `merge_items` | 0050/0059 | Merge two knowledge items (survivor + tombstone; survivor keeps its own item_type). |
| `change_item_type` | 0059 | Governed classification flip — page `item_type` + graph mirror; id, page path, and edges unchanged. |
| `split_item` | 0052/0059 | Split an item into a surviving primary + a minted spin-off (spin-off inherits the primary's item_type). |

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
cadence — nothing is applied automatically. For high-volume candidate review, the **per-source flow**
(`/ui/reviews/sources`, ADR-0058) walks the sources in ingest order — each screen batch-decides that
source's promotes/type changes/retirements, supports approve-with-amendments (title/aliases/
description/item_type — item_type is required to clear an unclassified-sentinel candidate) and
human-added candidates; the flat queue stays canonical for everything cross-source. Each screen
links the source's original file (`/raw/<source_id>` — passive media like PDF/text/images renders inline, sandboxed + nosniff; HTML/SVG/unknown types download as attachments, since raw sources are untrusted; lifecycle status never blocks this operator view) and offers
an explicit `?preselect=approve` link that pre-checks approve on the pending rows (deferred rows
stay parked) — you still review and submit; untouched rows always stay pending.
A human-added candidate rebuilds `wiki/index.md` immediately; its **keyword** rows follow the next
reindex pass (`/jobs/reindex` or the apply chain) like every other producer's output.

## Lint quality heuristics — remediation codes (ADR-0037, ADR-0059)

`/jobs/lint` also reports **report-only** quality findings (deterministic, key-free; they never file
review items and never turn lint `failing` on their own — they surface as maintenance debt in `by_check`).
Each finding carries a stable `data.remediation` code; act on it by re-running a producer:

| Finding (`check`) | Severity | Meaning | `data.remediation` → action |
|---|---|---|---|
| `summary_rot` | low | An enriched Source summary's artifact fingerprint no longer matches the current normalized markdown / configured summary model. | `rerun_enrich` → run enrichment for the source(s): `uv run python scripts/enrich.py` (needs the configured enrichment provider's API key — provider/model-dependent). |
| `stale_claim_citation` | medium | A claim's stored citation quote no longer grounds at its anchor in the current markdown. | `rerun_extract_claims` → re-run extraction + claim maintenance: `uv run python scripts/extract_claims.py` (needs the configured provider's key; stages the replacement claim set before superseding — a failed run preserves the existing layer and validators stay red until a run succeeds, ADR-0056). |
| `synthesis_rot` | low | An active synthesis's topic evidence (claims/citations/disagreements) drifted since approval — its artifact fingerprint no longer matches the current topic. | `rerun_synthesis` → **`uv run python scripts/generate_synthesis.py --force`** (needs the configured provider's key). `--force` is required: a normal run only *reports* the stale active synthesis (governance gate) and won't rewrite it. |
| `topic_starvation` | medium | A substantive source extracted no thematic topic layer — its items artifact has ≥5 named-type items (or ≥1 stored claim) but zero thematic-type items (ADR-0059, redefining ADR-0055's concept starvation). Artifact/claim state only, never text-shape inference. | `rerun_extract_items` → re-run the tier-2 pass: `uv run python scripts/extract_items.py` (needs the configured provider's key; a prompt/model change makes the artifact stale so a plain run re-extracts). |

Three coverage findings (`summary_unverifiable`, `claim_evidence_unverifiable`, `synthesis_unverifiable`,
low severity) mark lint `degraded` when an enriched page / active claim / active synthesis expects a
durable artifact that's missing or unreadable —
re-run the relevant producer to restore it. A fresh deterministic-only vault (stub summaries, no
enrichment artifacts) stays `healthy`.

## Caveats / out of scope

- **No scheduler ships.** Importing or serving the app starts no background thread (guarded by a contract
  test). You own the cron/timer.
- **Vector index** is refreshed only by `scripts/reindex_vector.py` (explicit; needs a configured embedder).
  The default embedder is the **in-process FlagEmbedding + PyTorch CUDA** backend running BAAI/bge-m3
  (`EMBEDDING_PROVIDER=flagembedding_bge_m3`, ADR-0053) — validate it with `python scripts/check_embedding.py`
  before a reindex. The old TEI (`local_http`) HTTP server is a CPU-fallback option only. Switching backends
  changes the `embedding_model_ref` identity, so the first reindex after a switch needs
  `scripts/reindex_vector.py --force`. `/jobs/reindex` never touches the vector index.
- **Raw-byte backup is opt-in.** By default `scripts/backup.py` backs up *manifests*, not the raw bytes
  (size + the raw-privacy posture). Set `BACKUP_INCLUDE_RAW=1` to include the **manifest-catalogued** raw
  bytes (`relative_raw_path` + every `occurrences[].relative_path`, wherever they live under `raw/`,
  including `raw/inbox/`); each is sha256-verified against its manifest and a missing/mismatched catalogued
  file aborts the backup (ADR-0039). Un-manifested staging is not backed up. Restore is `backup.py
  --restore <archive>` (guarded in-place; `--force`/`--dry-run`). The lint **missing-raw** finding remains
  the safety net that flags a lost raw file.
- **Cache purge is manual.** The `purge_response_cache` review item is record-only — purging
  `db/llm_cache.sqlite` forfeits LLM reproducibility (ADR-0027), so the system never does it for you.

## Manual answer-quality smoke (optional, key-required)

There is **no automated eval job** in v1 — `evals/golden_questions.yaml` is a fake-adapter CI fixture, not
a real-vault corpus (ADR-0036 decision 14). The structural regression **pytest suites** are the local
gate, run by the working rhythm — there is **no in-repo CI runner yet** (see Build Spec §16):

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

### Real-vault answer-quality eval (ADR-0042)

`POST /evals/run` scores the cited-answer pipeline against a curated **real-vault golden Q&A corpus**
deterministically (no LLM judge): per question it checks expected/forbidden sources cited, abstention
match, zero unsourced/security-rejected claims, and citation recall/precision. It is **key-required,
loopback-only, and cost-bearing**, so it is **read-only over vault SoT** (it may write its own reports +
populate the LLM cache; it always queries with `save:false`) and **gated**: `confirm_cost:true` is required
(else `400`), `limit` is clamped to `EVAL_MAX_QUESTIONS_HARD_CAP`, and an unconfigured LLM is a `503`.

```bash
# curate your corpus (gitignored — operator data):
cp evals/golden_answers.example.yaml evals/golden_answers.local.yaml   # then edit with real src_ ids

# validate the corpus with NO LLM call (key-free):
curl -fsS -X POST http://127.0.0.1:18000/evals/run \
  -H 'content-type: application/json' -d '{"dry_run": true}' | python3 -m json.tool

# run it (real LLM, cost-bearing; uses the response cache unless fresh:true):
curl -fsS -X POST http://127.0.0.1:18000/evals/run \
  -H 'content-type: application/json' -d '{"confirm_cost": true, "limit": 20}' | python3 -m json.tool

# browse stored runs (key-free):
curl -fsS http://127.0.0.1:18000/evals/results | python3 -m json.tool
```

Durable results live under `evals/reports/answers/` (gitignored) and store **only ids/flags/scores/
metadata** — never the prompt, evidence, answer prose, or any absolute path. The headline is a
reproducibility-stamped snapshot, **not** a CI gate (answer generation is nondeterministic).
