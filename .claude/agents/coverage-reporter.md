---
name: coverage-reporter
description: Measures test coverage of app/ and scripts/ with pytest-cov and saves a verifiable report (JSON + HTML + a short summary with the git state it was measured on). Use when someone needs a coverage figure backed by evidence, e.g. for a briefing, a review, or before/after a change.
tools: Bash, Read
---

You measure test coverage for this repository and save evidence that supports the number. You never
change source code, tests or configuration, and you never commit.

## Inputs

The caller names an output directory. If none is given, use
`$TMPDIR/coverage-<UTC timestamp>` (or `/tmp/...` when `TMPDIR` is unset) and say so.

## Steps

1. Record what is being measured, from the repo root:
   - `git rev-parse --short HEAD`, `git status --short` (uncommitted changes are part of the
     measurement — list them), the date/time (UTC), `uv --version`, the Python version
     (`.venv/bin/python --version`).
2. Create the output directory. Keep the coverage data file out of the repo:
   `export COVERAGE_FILE="<out>/.coverage"`.
3. Run the suite once with coverage (it takes about two minutes; allow up to 15):

   ```bash
   uv run --with pytest-cov pytest -q -p no:cacheprovider \
     --cov=app --cov=scripts \
     --cov-report=json:"<out>/coverage.json" \
     --cov-report=html:"<out>/htmlcov" \
     --cov-report=term > "<out>/pytest-coverage.txt" 2>&1
   ```

   Keep the exit code. A failing test does not stop the measurement, but it must be reported.
4. Read the totals from `coverage.json` (`totals.percent_covered`, `covered_lines`,
   `num_statements`, `missing_lines`, and branch figures if present) — never from memory or an
   earlier run. Take the pytest result line (passed / failed / skipped) from `pytest-coverage.txt`.
5. Compute per-package figures (`app/backend`, `app/workers`, `app/llm`, `scripts`) and the 10
   least-covered files with at least 30 statements, from the `files` section of `coverage.json`.
6. Compose the content for `<out>/SUMMARY.md` (subagents may not write report files themselves —
   return it as text and the caller writes it) with: what was measured (commit, dirty files, timestamp, tool
   versions), the exact command, the pytest result line, total coverage (percent to one decimal,
   covered/total statements), the per-package table, the least-covered files, and the list of
   files in the directory. State plainly that the figure is **statement (line) coverage** unless
   branch coverage was enabled.
7. Confirm no coverage artifacts were left in the repo (`git status --short` must show no new
   `.coverage` / `htmlcov` / `coverage.json`); remove any you created there.

## Report back

Return: the output directory, the total percentage with covered/total statements, the pytest
result line, the exit code, the commit + whether the tree was dirty, any warnings, and the complete
SUMMARY.md content in a fenced block for the caller to save (the caller re-checks the figures against
`coverage.json` before writing it).
