# Semantic node identity: deterministic id generation, rename, and merge

Every semantic node type gets a typed, stable identifier, generalizing the content-keyed
model (ADR-0015) and the concept/entity scheme (ADR-0017) across concepts, entities,
claims, and syntheses. Source pages keep `source_id = src_<sha256[:16]>` (content hash).
The semantic nodes, which have no canonical bytes, use an id derived from a normalized
identity string fixed **at creation**:

```text
concept_id   = cpt_<sha256(normalized_canonical_name)[:16]>
entity_id    = ent_<sha256(normalized_canonical_name)[:16]>
claim_id     = clm_<sha256(normalized_claim_text + "|" + primary_source_id)[:16]>
synthesis_id = syn_<sha256(normalized_title)[:16]>
```

Generation is **deterministic at creation**: the same canonical name / claim text yields
the same id, so re-running extraction over an unchanged corpus is idempotent and does not
mint duplicate nodes. The id is then **frozen** in the page's frontmatter — it is the
node's permanent identity even though the human-facing slug, title, or wording may later
change. (The id is derived from the creation-time string, not recomputed on every run;
recomputation is only a dedup aid when deciding whether a *new* node already exists.)

Rename and merge — both human-gated in `policies/review.yaml` — behave by id:

- **Rename / re-slug:** the `*_id` is unchanged; only the slug filename and inbound
  wikilinks are rewritten by a deterministic relink step, and the previous name is added
  to `aliases`. Graph edges, which key on the id, are untouched (ADR-0017).
- **Merge:** a human approves a surviving id; the merged-away node becomes a tombstone
  page carrying `merged_into: <surviving_id>` (kept so old links still resolve), its
  `aliases` are unioned into the survivor, and every edge is re-pointed to the surviving
  id by id, not by text.
- **Split:** new ids are minted for the parts per the reviewed decision, with
  `derived_from`/provenance recorded; the original may become a tombstone or remain as
  one of the parts.

The alternatives were rejected for the same reasons as in ADR-0017: opaque
randomly-assigned ids are non-deterministic and unreadable, while pure name/slug identity
churns every edge on rename and collides on duplicate names. Deriving the id from a
creation-time string and freezing it gives determinism *and* rename stability.

Consequences: graph edges, backlinks, and citations all reference stable typed ids, so
the expensive operations (merge/split) become id-level redirects plus a gated relink
rather than graph surgery, and re-extraction is idempotent. The cost is a tombstone/
redirect convention for merges and splits, and the rule that an id is assigned once and
never recomputed as identity. This is a deferred-phase (Phase 3.5+) contract; the Phase 3
backbone mints no semantic nodes.
