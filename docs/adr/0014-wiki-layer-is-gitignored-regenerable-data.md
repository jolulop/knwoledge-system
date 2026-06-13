# The generated wiki layer is local runtime data, not code: gitignored, durable via backups

Mutable, generated data is treated as distinct from code and is not committed to git.
This already holds for the content-keyed manifests, the normalized layer, and the
search indexes (untracked in commit ab81737, ADR-0008/0011). Phase 3 extends the same
rule to the **entire `wiki/` layer**: generated Source pages, `wiki/index.md`, and
`wiki/log.md` are gitignored regenerable/runtime data. Only the empty directory
scaffolding (via `.gitkeep`) stays in git. Durability of the wiki — including the
append-only `log.md` history — is provided by the backup mechanism (`scripts/backup.py`
into `backups/`), never by git.

This applies the "data is not code" principle uniformly: a live system's data should not
live in version control. `index.md` is trivially regenerable from the pages, and Source
pages are deterministic projections of the manifest + normalized layer, so committing
them would only add churn. `log.md` is the one artifact that is *not* regenerable from
inputs — it is an accumulating record of ingests, extractions, generations, queries, and
reviews — so the decision deliberately makes the backup worker responsible for its
durability rather than keeping it in git.

Note that `wiki/index.md` and `wiki/log.md` are tracked in git today; honoring this
decision means untracking them and adding the wiki layer to `.gitignore` during the
Phase 3 build (keeping the `.gitkeep` files), mirroring how the stale generated indexes
were untracked previously.

Consequences: git holds only code, docs, ADRs, policies, and templates — a clean,
data-free history — and a fresh clone has an empty wiki until the pipeline re-runs.
The backup mechanism becomes load-bearing: it is now the sole durability guarantee for
`log.md` and any future human-curated wiki content, so its coverage and cadence (a
Phase 7 concern) matter more, and a gap in backups is a gap in history. This is an
accepted trade in favor of keeping mutable data out of source control.
