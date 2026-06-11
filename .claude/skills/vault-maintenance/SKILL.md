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
- Orphan concepts.
- Concepts with fewer than two sources.
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
python3 scripts/validate_frontmatter.py .
python3 scripts/validate_wikilinks.py .
python3 scripts/validate_citations.py .
python3 scripts/reindex_keyword.py .
python3 scripts/reindex_vector.py .
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
