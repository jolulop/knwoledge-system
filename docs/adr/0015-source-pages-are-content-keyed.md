# Source pages are content-keyed: filename is the source_id

A Source page is stored at `wiki/Sources/<source_id>.md`, where `source_id` is the
content-derived `src_<first 16 hex of sha256>` used everywhere else in the system
(manifests at `raw/manifests/<source_id>.json`, normalized artifacts at
`<source_id>.md`/`.jsonl`). The wikilink target is therefore `[[Sources/<source_id>]]`.
The human-readable title lives in frontmatter as `title`, with an Obsidian `aliases`
entry so the page displays and is searchable by name.

The alternative — a human-readable slug derived from the title or original filename —
reads better in Obsidian and in raw wikilinks, but it reintroduces problems the
content-keyed model was chosen to avoid: slug collisions when two sources share a title,
non-determinism when a source has no good title, and link churn when a raw file is
renamed (every inbound wikilink would dangle). Because exact-duplicate sources already
collapse to a single `source_id` (ADR-0007), they also collapse to a single Source page
for free. Keying the page by `source_id` makes wikilink targets stable, unique, and
deterministic, and lets backlinks be computed mechanically by id rather than by fuzzy
title matching.

The cost is that a bare wikilink shows `src_01466dc7d6adf1be` rather than a friendly
name. This is mitigated by the frontmatter `title`/`aliases` (Obsidian renders and links
by alias) and is an acceptable trade for link stability across renames, re-extraction,
and the later semantic phases that will add many inbound links to Source pages.

Consequences: every layer of the system — raw, normalized, and wiki — is keyed by the
same identifier, so cross-layer navigation and validation are mechanical. Tools and
agents resolve a source's page, manifest, and normalized files from one id. The graph
and backlink work in Phase 3.5/Phase 4 can rely on stable id-based link targets rather
than defending against slug drift.
