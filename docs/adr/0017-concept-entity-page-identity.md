# Concept/Entity pages: readable slug filename + stable id + aliases

Concept and Entity pages are identified differently from Source pages. A source is
content-keyed (`source_id` = `src_<sha256[:16]>`, ADR-0015) because it has canonical
bytes to hash. A concept or entity has no content to hash — its identity is a name and
meaning — and unlike a source it is subject to rename, merge, and split (all human-gated
in `policies/review.yaml`). So these pages use a three-part scheme:

- **Filename = a readable slug** of the canonical name, e.g.
  `wiki/Concepts/post-merger-integration.md`, `wiki/Entities/...`. Wikilinks
  (`[[Concepts/post-merger-integration]]`) and Obsidian browsing stay human-readable,
  which matters because concept/entity pages are the surface humans navigate most.
- **A stable `concept_id`/`entity_id` in frontmatter** (assigned once, persisted). The
  Phase 4 graph keys its edges on this id, not the slug, so a rename or merge does not
  break the graph: only the slug and the inbound wikilinks are rewritten (by a gated
  deterministic relink step), while every edge keyed on the id stays put.
- **An `aliases` list** capturing synonyms and surface variants (e.g. "PMI" ↔
  "post-merger integration"), used to dedup extraction to a single page and to drive
  Obsidian alias display/resolution.

Pure ID-keyed filenames were rejected: the id cannot be content-derived (so it is either
non-deterministic when assigned, or it churns on rename if derived from the name), and
opaque filenames gut readability for the most-browsed node type. Slug-only was rejected
because, with no stable id, graph edges and backlinks would have to be re-pointed on
every rename/merge and same-name concepts would collide. The chosen scheme keeps
presentation readable (slug) while keeping the data layer stable (id).

Consequences: renames and merges become slug/wikilink rewrites plus an id-level
redirect, not graph surgery — the expensive, error-prone part (edges) is insulated. The
cost is maintaining two keys per page (slug and id) and a relink step that runs on the
human-approved rename/merge events. This decision belongs to the deferred semantic phase
(Phase 3.5+); the Phase 3 backbone emits no concept/entity wikilinks yet (ADR-0016), so
nothing depends on it until then.
