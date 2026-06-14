# Derived artifacts are deterministic; freshness is an input fingerprint, not a timestamp

Generated artifacts (wiki Source pages, `wiki/index.md`) are pure, reproducible
functions of their inputs: identical inputs yield byte-identical output. Two consequences
follow, replacing earlier wall-clock-based behavior.

**No wall-clock timestamps in deterministic artifacts.** `wiki/index.md` no longer
embeds a `Generated: <now>` line, and Source pages no longer carry a wall-clock
`last_compiled_at` (it superseded the field added in ADR-0022 for the deterministic
backbone). A timestamp makes every rebuild differ even when content is unchanged, which
destroys reproducibility and the ability to detect a stale artifact by rebuilding it
in memory and diffing against disk. Freshness "when was this last built" is answered by
the file's mtime, the `wiki/log.md` entry, and the `generate_wiki` job record — not by a
line baked into the artifact.

**Freshness is keyed by an input fingerprint, not the raw checksum alone.** A Source page
is a function of more than the raw bytes: the manifest fields, the source's normalized
Markdown, the page template, the renderer/schema version, and the summary-length config
all determine its output. The previous skip-regeneration test (raw `sha256` +
`ingestion_status`) silently produced stale pages whenever a template, config, or
extractor changed without the raw file changing — correct output then depended on a human
remembering `--force`. Each page now records an `input_fingerprint` in frontmatter: a
hash of a schema-version tag plus the page's own deterministic content (which transitively
covers the template, normalized text, manifest fields, and config, because all of them
change the rendered bytes). Regeneration renders the candidate page, compares its
fingerprint to the stored one, and skips only on an exact match. Bumping the schema
version is an explicit lever to force a global rebuild.

Consequences: regeneration is correct and idempotent under template/config/code changes,
not just raw-byte changes, and a deterministic index/page can be validated by
rebuild-and-compare. The cost is rendering the candidate page during the freshness check
(cheap) and a fingerprint field in frontmatter. Non-deterministic, human-or-LLM-curated
pages introduced later (Phase 3.5 concepts/claims/synthesis) are outside this rule and
may reintroduce timestamps if useful, since they are not pure projections.
