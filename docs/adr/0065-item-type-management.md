# ADR-0065 — Item-type management (taxonomy beyond AI)

- **Status:** proposed — **deferred, not scheduled** (placeholder recorded 2026-10-24 at the user's
  request; not a priority). Nothing changes now: the 15-type taxonomy of ADR-0059 stays as it is.
- **Date:** 2026-10-24
- **Drivers:** UAT round 5 (fresh `/tmp/ks-uat`, 5 sources). The user defined the ADR-0059 taxonomy
  for AI material only. One of the five sources (Dennett, *Real Patterns*) is philosophy of mind,
  and its items are forced into AI types: *Folk Psychology* is filed as `ai_topic_area`. A non-AI
  document (the user's example: CRISPR) would hit the same wall. The user confirmed (2026-10-24)
  that the item-type approach itself is right. Items are the user's tags, and item types are how
  they browse them. Only *managing* the type list is open.
- **Related:** ADR-0059 (the taxonomy, type-neutral identity, the `unclassified_review_required`
  sentinel; its named deferral **"Taxonomy evolution governance"**, which this ADR takes over),
  ADR-0062 (`item_type` retrieval faceting), the `change_item_type` executor (`app/workers/retypes.py`).

## Context

`item_type` is a mutable, governed classification, not part of identity (ADR-0059): an item's id is
`sha256(normalized title)`, so changing a type never rekeys an item. The 15 values live in code
(`app/backend/taxonomy.py`, `ITEM_TYPES` plus the `THEMATIC_TYPES`/`NAMED_TYPES` split) and in the
items extraction prompt. They also appear in validators, tests and the index/navigation grouping.
ADR-0059 deferred evolution as "adding/renaming a type = ADR + prompt-version bump +
enum/validator/test updates; explicitly not a config knob".

Every value except `domain` is AI-specific (`ai_topic_area`, `model`, `model_family_architecture`, …).
That is correct for an AI-focused vault. It stops fitting as soon as the user files other kinds of
knowledge.

## Questions to settle when this is picked up

1. **Who owns the type list?** Options:
   - code-owned, changed by ADR (today's rule);
   - a user-editable taxonomy file (e.g. under `policies/`) that code, prompt and validators read;
   - a fixed core list plus user extensions.
2. **Shape:** stay one level, or add a level, e.g. `domain` as the top level with role types
   (topic, method, tool, organisation, …) under it, so non-AI fields reuse the same roles.
3. **Prompt impact:** a changed list changes the extraction prompt. Is that a vault-wide restale
   (prompt-version bump), or are existing items kept and only new extractions use the new list?
4. **Migration of existing items:** retype in bulk through `change_item_type` review items, or
   leave them until their sources are re-extracted.
5. **Rename/removal of a type:** how pages, the index grouping and pending reviews that reference
   the old value are handled.
6. **Secondary types for browsing.** In UAT r6 the user tagged one thing under several types:
   *BIRD* as a governance construct and a standard, *APM* as a method, product and standard.
   With one type per item, BIRD is browsable under only one heading, and the user said that makes
   browsing harder. ADR-0059 rejected typed facets (primary + secondary types) because nothing used
   them; browsing is now that use. Decide whether an item can also be listed under a secondary
   type, without the secondary type affecting identity or the extraction priority order.

## Decision (for now)

None beyond recording the deferral. The 15 ADR-0059 types stay fixed. Non-AI material keeps getting
the nearest type, or the sentinel where nothing fits, and the user can correct it with a retype.

## Trigger to revisit

The vault holds a meaningful share of non-AI sources, or retype volume on non-AI items becomes a
noticeable part of review work.
