# Operations — running maintenance (Phase 7)

How to run the knowledge system's maintenance passes on a schedule. **There is no built-in scheduler or
daemon** (ADR-0036): cadence is an OS `cron`/`systemd` entry you opt into, and every pass is a
deterministic, job-recorded, **detect-and-propose** operation — it surfaces health and files review items
but never acts on anything semantic/destructive on its own. Apply happens later, human-gated, via
`POST /reviews/apply`.

All commands run from the repo root (`/home/jolulop/code/knowledge-system`).

## Model providers (hosted + local)

The LLM passes (enrichment, claims/items, synthesis, `POST /query`) route through per-tier **model
chains** resolved to the first available provider, with hosted fallback (ADR-0063). To run any tier on
a **local** OpenAI-compatible model (Ollama / vLLM / LM Studio) instead of a hosted one, see
`docs/Local Models.md` — set `ENRICH_LOCAL_BASE_URL` and add a `local:<model>` entry to the tier chain.

**LLM usage (ADR-0069).** Every provider attempt — from Enrich, claims, items, synthesis, `POST /query`
and the answer eval — is recorded in `db/jobs.sqlite` (`llm_usage`): one row per attempt, written
before the request is sent, with its outcome (ok, schema error, adapter error, output limit) and the
**reported** tokens. Failed and retried attempts count too; a cache replay is counted separately as
an avoided call. Each producer script ends with an "LLM usage" summary per model, `GET /jobs` and
`GET /jobs/{id}` carry the same totals (`llm_usage`, with `llm_usage_status`: `recorded`, or
`not_recorded` for jobs older than the ledger, or `unavailable` if it can't be read — unknown,
not none), and `POST /query` returns the
`invocation_id` its attempts are recorded under. Retries are the app's own (the provider SDKs'
automatic retries are off), so every request sent is a recorded attempt. Tokens a provider didn't report are shown as unknown, never 0
(local servers often omit them). For Anthropic, input excludes cache read/write tokens; on the
OpenAI wire, input includes cached tokens. No prompts, responses or prices are stored — your
provider console stays the cost authority.

## Starting the app

The **only supported launch** is the blessed entrypoint, which binds Uvicorn to `APP_HOST` through the
`assert_safe_bind` loopback guard so the bind can't drift from the check (ADR-0009):

```bash
uv run python -m app.backend        # or: uv run python scripts/serve.py
```

**Do not** run `uvicorn app.backend.main:app --host 0.0.0.0` directly — uvicorn's `--host` overrides the
bind *without* re-checking the guard, exposing the unauthenticated API. There is no app-level auth
(loopback-only posture); `KS_ALLOW_INSECURE_BIND=1` is a narrow internal-transport escape hatch (trusted
private network / sidecar behind a TLS/auth proxy) and is **not** a substitute for auth.

**Request-origin guard (ADR-0067).** Every request must use a loopback host name (`127.0.0.1`,
`localhost`, `::1`) or one listed in `KS_ALLOWED_HOSTS` (comma-separated — needed for the proxy name
when using the escape hatch); state-changing requests from a browser must come from the app's own
origin. A foreign web page or a DNS-rebinding page gets `403`. `curl` and the scripts send no browser
headers and are unaffected.

## Maintenance passes

| Pass | Endpoint | Script-equivalent | What it does |
|---|---|---|---|
| **Lint** | `POST /jobs/lint` | — | Structural validators + semantic checks (**unused tag** — report-only, uncited claim, **missing raw**; the single-source `under_supported_item` deprecation is retired by ADR-0066 and its pending items are withdrawn as `rule_retired`) + quality heuristics (**summary rot**, **stale claim citations**, **topic starvation**) → health report + governance review items. |
| **Stale / retention** | `POST /jobs/stale-check` | — | Stale sources → `archive_source` candidates; ephemeral past window → `delete_raw_file` (record-only); **LLM-cache** over TTL/size → `purge_response_cache` (record-only). Reports **live cache stats** every run. |
| **Reindex** | `POST /jobs/reindex` | `scripts/rebuild_index.py` + `scripts/reindex_keyword.py` | Rebuild `wiki/index.md` + refresh the keyword index, as a publication (it also retries pending work; ADR-0068). **Never the vector index.** |
| **Backup** | — | `scripts/backup.py` | Snapshot manifests, db (incl. graph), wiki, reviews, policies. SQLite files are copied through SQLite's backup API, and the archive is written holding the vault writer lock (`db/.vault.lock`) exclusively — every running job, API write and write script holds it shared — so it is one consistent cut. Waits up to `--lock-timeout` (60 s) for running writers, then refuses (exit 2, nothing written); writers that start meanwhile wait. Stop the API before `--restore`. |
| **Tag-link repair** | — | `scripts/rebuild_tag_links.py [--apply]` | ADR-0066: make the graph agree with the per-document tag ledgers (the authority) — re-assert missing keeps (after a graph rebuild/restore), finish an interrupted keep (activation with the recorded amendments, or an unused tag's revival), reject links of dropped tags and human links with no keep (an interrupted drop). Dry run by default; re-renders touched pages + refreshes `wiki/index.md` and the keyword index. **Never touches** a document whose ledger is damaged (reported, never overwritten — restore it from backup) or the tags of an interrupted merge/split (`reviews/in_flight/<review_id>.json`; the next `/reviews/apply` resumes it — run that first). While such a marker exists, keep/drop/undo/human-add/block on its tags are refused (`identity_operation_in_flight`, a banner on the document screen), Delete unused skips them and extraction skips the documents linked to them (`in_flight_sources`). Apply closes the marker only after the Source pages (every affected page rendered — a per-page render failure is reported as `source_page_refresh_failed`), `wiki/index.md` and the keyword index refreshed; on a refresh failure it stays (`identity_refresh_pending`, non-clean) and the next apply finishes the job. Exits non-zero while either remains. `validate_tag_links.py` (in `validate_all`) fails while they disagree. |
| **Review reconciliation** | — | `scripts/reconcile_reviews.py` | Withdraw extraction-stale unresolved review items (ADR-0057): pending/deferred promotes for tombstoned/missing nodes, recompose-provenance deprecations for resurrected nodes. Idempotent, audited per withdrawal; never touches approved/rejected. **Preflight fail-closed** (exits non-zero, withdraws nothing) on missing/empty/schema-mismatched graph DB or graph↔wiki status drift over the reviewed nodes. |
| **Apply** (human-gated) | `POST /reviews/apply` `{"preview_token"}` | `scripts/apply_reviews.py apply --token <id>` | Needs the token of a clean preview (ADR-0068 decision 6; see *Apply tokens* below). Deterministically apply *approved* decisions: promotion, contradiction, synthesis, deprecation, **archive** (`active→archive_candidate`), **duplicate annotation** (`mark_semantic_duplicate` → `## Duplicates`, ADR-0041), the **visibility family** — hide/unhide for sources (`hide_content`/`unhide_content`, ADR-0043/0047), semantic pages (`hide_semantic_page`/`unhide_semantic_page`, ADR-0046/0047), claims (ADR-0048) and synthesis (ADR-0049, `evidence_hidden`) — the **identity-surgery family** — `merge_items` (ADR-0050/0059) and `split_item` (ADR-0052/0059) — and the non-rekeying **`change_item_type`** classification flip (ADR-0059). |
| **Apply preview** (dry-run) | `POST /reviews/apply/dry-run` | `scripts/apply_reviews.py preview [--json]` | Apply-on-a-copy preview (ADR-0040): runs the same executors against a throwaway sandbox and returns the semantic mutation diff (graph/wiki/reviews/manifests) **without touching live state**. Only a clean (`ok`) preview returns a `preview_token`. In the UI, `GET /ui/reviews/apply` is read-only; its **Preview** button (`POST /ui/reviews/apply/preview`) renders the diff and, when clean, the Apply form. |

Maintenance passes are **key-free**. Only the LLM *producers* (Enrich/claims/items/synthesis)
and `POST /query` need `ANTHROPIC_API_KEY`.

**Overlapping runs (ADR-0068).** Every commit (a screen action, a review decision, Apply, each
producer's per-document write, lint/stale-check filing, repair) takes the vault **write lock**
(`reviews/tag_links/.lock`), so writers never interleave; model calls stay outside it, so you can
keep tagging while a pipeline runs. Each producer also allows **one run at a time**
(`db/.run-<producer>.lock`): a second run of the same producer refuses at once and writes nothing —
a script exits with code **3** ("Refused: … is already running"), a `POST /jobs/*` route answers
**409** `already_running: <producer>`. Wait for the first run to finish, then start again. Different
producers may run side by side. The lock files hold no state and are never backed up.

**Publication and Retry publishing (ADR-0068).** After a change is saved, its claim, synthesis, tag
and Source pages, `wiki/index.md` and the keyword index are refreshed before the action returns. If a refresh fails,
the change stays saved and the outstanding work stays in `db/publication_pending.json`. The result
says **`saved_publication_pending`** ("Saved; publication incomplete"), and every review screen
shows a **Retry publishing** banner. Don't repeat the decision. Retry with the button,
`POST /publication/retry` or `uv run python scripts/republish.py` (`--all` republishes every page
from the saved decisions; saved Query pages are never regenerated). The next action also retries
automatically. Publishing never changes a saved decision, except that it can move a tag between
in use and unused to match its saved keeps. A page that can't be published stays pending, with the
reason shown on the result screen and by the script. For example, a missing tag page says
**Restore the page from backup**; publishing never recreates one. `validate_publication.py` fails while anything is
pending. If the queue file itself is damaged, every change refuses (HTTP 503
`publication_queue_corrupt`, script exit code 4) until you run **Repair publication queue**
(`scripts/republish.py --repair`). Repair keeps a copy of the damaged file in `db/`, replaces the
queue with one full-recovery entry, and republishes every page from the saved decisions. Reading and backups keep working meanwhile. The vector index is not part
of publication; refresh it with `scripts/reindex_vector.py`.

**Pipeline runs publish too (ADR-0068 decision 5).** Every producer (`extract_sources`, `generate_wiki`,
`enrich`, `extract_claims`, `extract_items`, `promote`,
`generate_synthesis`, the reindex job) saves each document as one commit and publishes that
document's pages before it moves to the next, then refreshes `wiki/index.md` and the keyword index
**once** at the end. There is no separate `generate_wiki` / `reindex_keyword` / `rebuild_index`
step after a run any more (they stay as manual repairs). A run reports success only after that end
refresh; if it fails, the job's status is `saved_publication_pending`, the script prints the
pending pages and exits 1, and `scripts/republish.py` finishes it. While a run is going, review
screens show **Search refresh pending** (and `validate_publication.py` fails) until it ends; if a
run stopped part-way, Retry publishing finishes its refresh. Tagging stays available during a run's
model calls, and a keep made meanwhile is published at once and never overwritten by the run.

**Details and reconciliation (ADR-0066 part 3 slice 3).** The document screen's **Details**
section confirms a document's title, authors and publisher: tick *These details are correct* and
Save. A Save that changes the Details without the tick, or from a page shown before a newer change
(another save, a new Enrich run), is refused with nothing written and the values kept; reload and
review again. **Add author rows** and **Use these** only redraw the form. **Clear confirmed
details** removes them (`report_family` and `canonical_url` stay, so independence may still rest on
them). Confirmed authors decide source independence for synthesis on its next run; until it runs, the
document screen and the lint report (`awaiting_reconciliation`, report-only) name it. The
contradiction notice says that proposed contradictions weren't re-checked: whole-vault judging was
removed (ADR-0069 slice 0), and nothing re-checks them until contradiction maintenance (ADR-0069
slice 2(a)), which also retires that notice.

**Graph traversal indexes (ADR-0068 decision 7).** Graph views and the graph part of search read
at most a fixed number of edges per node, through three indexes on `db/graph.sqlite`. The API
installs them at startup, every producer when it opens the graph, and a restore after it
finishes (a restore that could not install them prints `GRAPH INDEXES NOT INSTALLED` and exits 1).
If they are missing anyway, `GET /graph/node` and `GET /graph/neighborhood` answer 503
`graph_index_missing`, and search returns no graph results with the note `graph_index_missing`:
restart the API. A hub's node view now lists at most `edge_limit` edges per group and says which
groups were cut (`returned`, `truncated_groups`; it replaced `counts`).

**Apply tokens (ADR-0068 slice 4).** Every Apply applies exactly one clean preview. The preview
copies the vault under the write lock, runs Apply on the copy, and returns a token; Apply checks it
before writing anything and spends it. A token is refused, with nothing changed, when:
- it is missing (`preview_required`, 422) or unknown, already used or replaced (`unknown_preview`,
  409): preview again;
- anything that could change the outcome moved since the preview (`stale_preview`, 409): any write
  to the vault (a keep, a decision, a producer commit — so a preview taken during an ingest goes
  stale quickly), the code, or the settings Apply uses (including `APP_HOST`/`APP_PORT` in `.env`
  or the environment: Source pages link the raw view through them). Editing `.env` while a
  preview runs withholds its token (`settings_changed`). Preview again and read the new result: scripts never
  re-preview by themselves;
- the API runs older code than what is on disk (`restart_required`, 503): restart the API, then
  preview. **Install code (`git pull`, `uv sync`) only while no preview or Apply is running**, and
  restart the API afterwards;
- `db/apply_authorizations.json` can't be read or written (`authorization_store_unavailable`, 503).

`db/revision.json` tracks the vault's revision. Every restore starts a new epoch, so no earlier token
survives it. A damaged revision file or authorization store repairs itself: the bad bytes are kept as
`db/revision.damaged-*.json` / `db/apply_authorizations.damaged-*.json` (the preview warns
`revision_recovered` / `authorization_store_recovered`; delete the copies once inspected), and
outstanding tokens are refused. If either file can't be read or written at all (permissions, a full
disk), writes refuse with `revision_unavailable` (503). The authorization store is never backed up,
and a restore never writes `db/revision.json` or `db/apply_authorizations.json` from an archive.
`scripts/apply_reviews.py` exit codes: 0 applied (or a token issued); 1 Apply ran but is not clean
(`validation_failed`, `saved_publication_pending`) or no token; 2 usage; 3 another Apply is running;
4 queue unreadable; 5 journal unreadable; 6 `preview_required`/`unknown_preview`/`stale_preview`;
7 `restart_required` (API callers only); 8 `authorization_store_unavailable`; 9
`revision_unavailable`.

**Unfinished producer commits (ADR-0068 slice 3).** Claim, tag and synthesis extraction record
each document or topic in `db/producer_journal.json` before they change it, and remove the record
once all its writes are done. If a run is killed part-way, the record stays:
`validate_producer_journal.py` fails and names the documents or topics. The next run of that
producer redoes them even if they look up to date (from the response cache when the text is
unchanged), and retires any claim or tag the interrupted run left without evidence. Retry
publishing does not clear these records, because only the producer can finish the work. A
synthesis topic that a human decision (approval, rejection, hide) keeps from being redone is
reported as blocked. If the journal file is damaged, those three producers refuse to run (exit 5)
until `uv run python scripts/repair_producer_journal.py` sets it aside and rebuilds it. The rebuild
marks every extracted document and every possible synthesis topic, including work interrupted
before its first result was saved. The next runs redo the documents, mostly from the cache. They
check the topics and clear the ones that are already complete.

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
cadence — nothing is applied automatically. For tagging, the **per-source flow**
(`/ui/reviews/sources`, ADR-0058/0066) walks the sources in ingest order — each screen decides that
document's **tag links**: *keep* (immediate; a tag becomes active with its first keep, with optional
title/aliases/description/item_type amendments — item_type is required to keep an unclassified
suggestion), *drop* (this document only; remembered so re-extraction never re-suggests it; a tag
nobody keeps is deleted, an active tag that loses its last keep becomes **unused** —
`/ui/reviews/tags/unused`, bulk *Delete unused*), *never suggest* (the `policies/blocked_tags.txt`
list, `/ui/reviews/tags/blocked`); plus that source's type changes/retirements (recorded, applied
later) and human-added tags. The flat queue stays canonical for everything cross-source. Each screen
links the source's original file (`/raw/<source_id>` — passive media like PDF/text/images renders inline, sandboxed + nosniff; HTML/SVG/unknown types download as attachments, since raw sources are untrusted; lifecycle status never blocks this operator view) and offers
an explicit `?preselect=keep` link that pre-checks keep on the suggestions — you still review and
submit; untouched rows always record nothing. Kept tags live in the per-document ledger
`reviews/tag_links/<source_id>.json` (backed up with `reviews/`; every update — tag actions,
extraction's writes, whole merges and splits — is serialized by `reviews/tag_links/.lock`; a damaged
ledger refuses decisions instead of being overwritten). Each
immediate action refreshes the Source pages, `wiki/index.md` **and the keyword index**; a refresh
failure is shown on the result page (run `reindex_keyword.py`). Only **kept** links feed synthesis
and the ADR-0062 item-type ranking signal — an undecided suggestion is not a tag. After an unblock,
the page prints the **Re-suggest** command `scripts/extract_items.py --force --cache-only <ids>`,
which replays stored responses only (zero provider calls) and reports a cache miss per document
instead of calling the model.
**Suggestions (ADR-0066 part 2).** Extraction suggests at most `ENRICH_ITEMS_CAP` tags per document
(default 15; the tags it already keeps count toward it, and are never cut), with up to three broad
*domain* tags first. The cap is applied in code only: changing it restales and re-bills nothing —
re-cut with `scripts/extract_items.py --force --cache-only`. A suggestion whose folded name matches
the title or an alias of exactly one active/unused tag links to that tag instead of creating a new
one; when it matches several, the row offers **use *X*** per matching tag or **keep as new tag**
(*use X* keeps X here, drops the suggestion and audits `tag-resolved`). An extracted alias is kept
only when it occurs in the document's text before its reference list. On a first keep the aliases
field is prefilled, one per line, and replaces the tag's aliases; after that extraction never adds
aliases to the tag (only a merge does). Extraction never changes a status a human decided: a
suggestion naming a hidden or retired (approved deprecation, archive, delete) tag is filtered and
counted, one naming a merged tag links to its survivor. A "use X" that stopped part-way stays under
**Unfinished choices** on the document screen until you finish or cancel it — repair only reports
it; cancelling keeps whatever already happened (X stays kept if it was kept).
A human-added tag rebuilds `wiki/index.md` immediately; its **keyword** rows follow the next
reindex pass (`/jobs/reindex` or the apply chain) like every other producer's output.

**Item consolidation (ADR-0064).** `/jobs/lint` files `merge_items` proposals for **merge candidates**
— two *active* items whose title/alias merge keys intersect (case, whitespace, punctuation, `&`/`and`
and a last-word plural folded). Matches are starred around one survivor (most live support wins) and
only items that directly match the survivor are filed — a transitively-linked item is proposed on a
later run, after the first merge's alias union. Finding `merge_candidate` (low; `data.remediation:
decide_merge`); a match with a *candidate* side is `merge_candidate_unpromoted` (not filed); a key on
more than 5 active items (`policies/review.yaml` `merge_candidates.max_items_per_key`) is
`merge_key_too_common` and pairs nothing. A rejection is remembered for the pair in **both**
directions, so reject false aliases freely. To merge something lint didn't detect, use the **Merge
items** form on `/ui/reviews`: it shows a preview (what moves, aliases after, any conflicting
proposals to be withdrawn) and writes nothing until you **Confirm** — confirming files the merge
pre-approved (the filing is your approval). `POST /reviews/merge` `{"survivor": …, "absorbed": …}`
(each an `itm_` id or Items slug) is the single-step script route. A rejected merge of the pair, in
either direction, blocks — to flip the survivor, reopen the rejection first, then file the other
direction. Conflicting merges (same absorbed item, or a chain) are never approved together: an
approved one blocks the filing/approval (409), pending ones are withdrawn. The merge itself still
happens only at apply, after the dry-run. Decide merges **before** deprecations of the same item: an
applied deprecation leaves the absorbed item non-active and the merge then skips it.

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
| `display_alias_rot` | low | A wikilink's display alias drifted from its target's current label (ADR-0060) — e.g. the target was retitled after the referring page rendered. Compared against the *rendered link label* (`display_link_label`, ≤78 chars), never the raw full-length `title:`, so capped aliases on long titles are current, not rot. Alias *shape* (missing/blank alias where a label resolves) is the blocking `validate_link_aliases.py` instead. | `rerun_render_chain` → the free re-render chain (ADR-0060 §6): re-run the producers that own the referring pages (`extract_claims.py --force` / `extract_items.py --force` / `generate_synthesis.py --force` / `generate_wiki.py --force` as applicable), then `reindex_keyword.py --force` + `rebuild_index.py`. Key-free for deterministic pages; LLM producers replay from cache when inputs are unchanged. |

Three coverage findings (`summary_unverifiable`, `claim_evidence_unverifiable`, `synthesis_unverifiable`,
low severity) mark lint `degraded` when an enriched page / active claim / active synthesis expects a
durable artifact that's missing or unreadable —
re-run the relevant producer to restore it. A fresh deterministic-only vault (stub summaries, no
enrichment artifacts) stays `healthy`.

## Caveats / out of scope

- **No scheduler ships.** Importing or serving the app starts no background thread (guarded by a contract
  test). You own the cron/timer.
- **Vector index** is refreshed only by `scripts/reindex_vector.py` (explicit; needs a configured embedder).
  The recommended GPU `.env` selects the **in-process FlagEmbedding + PyTorch CUDA** backend running
  BAAI/bge-m3 (`EMBEDDING_PROVIDER=flagembedding_bge_m3`, ADR-0053) — validate it with
  `uv run python scripts/check_embedding.py` before a reindex. The code default remains unconfigured
  `local_http` so non-vector roles stay Torch-free; TEI/`local_http` is a CPU-fallback option. Switching
  backends changes the `embedding_model_ref` identity, so the first reindex after a switch needs
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
