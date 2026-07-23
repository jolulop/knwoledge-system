---
name: vault-maintenance
description: >-
  Lint, maintain, health-check, stale-check, retention-check, backup, or evaluate
  the knowledge system. Use when the user asks to run maintenance, lint the wiki,
  find broken links, identify stale content, find contradictions, run golden questions,
  or back up the system.
---

# Vault Maintenance Skill

## Purpose

Keep the filing-cabinet/wiki/search/graph layers healthy over time.

## Checks

- Broken wikilinks.
- Missing frontmatter.
- Missing summary callouts.
- Missing citations.
- Orphan items.
- Items with fewer than two sources.
- Summary rot.
- Stale claims.
- Duplicate sources.
- Contradictions.
- Unreviewed destructive changes.
- Archive candidates older than three years.

## Procedure

Run the relevant scripts:

```bash
python3 scripts/rebuild_index.py .
python3 scripts/reindex_keyword.py .
# Vector reindex is EXPLICIT (ADR-0033) — never auto-run by the per-file hook. Run it deliberately
# after ingest batches / before retrieval evals; it needs a configured local embedding server
# (EMBEDDING_BASE_URL + EMBEDDING_MODEL_REF) and the `vector` extra (LanceDB) installed.
python3 scripts/reindex_vector.py .          # optional; skip if no embedder configured
# Runs every scripts/validate_*.py (frontmatter, wikilinks, citations, index
# consistency) and exits non-zero on any failure. Run after reindexing so the
# index-consistency check sees freshly generated indexes.
python3 scripts/validate_all.py .
python3 scripts/backup.py .
```

Create review items for any semantic or destructive decisions.

## Output format

Return:

- Checks run
- Issues found
- Review items created
- Backups created
- Suggested next actions
