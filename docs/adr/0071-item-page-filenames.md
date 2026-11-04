# ADR-0071 — Item page filenames

- **Status:** accepted — design-locked by grill-phase 2026-11-04 (Q1–Q5); **implemented 2026-11-04**
  as ADR-0070 sub-slice **B1b** (see "Implementation" at the end), awaiting external review.
- **Date:** 2026-11-04
- **Drivers:** ADR-0070 B1 found that two different tags whose names slugify alike share one
  `wiki/Items/<slug>.md` (`Café` / `Caf` → `caf`, any non-Latin name → `unnamed`, `Data-Mesh` /
  `Data Mesh` → `data-mesh`). B1 made every writer refuse such a write (page-ownership guard,
  `app/workers/item_pages.py`) and left the permanent filename policy to a separate design (ADR-0070
  step 1b). The 2026-11-04 B1 acceptance hit it live: **Add tag** `Data Mesh` after `Data-Mesh` →
  "Nothing was saved: The tag wasn't added: slug_collision".
- **Related:** ADR-0021 (frozen ids, renames re-slug), ADR-0059 (one flat `wiki/Items/`, `itm_` id =
  hash of the normalized creation name), ADR-0060 (other families keep id filenames), ADR-0064
  (`merge_key`, merge proposals), ADR-0066 part 3 slice 4 (Authors filenames: the precedent),
  ADR-0070 B1 (page-ownership guard).

## Context

Today `items._slug(name)` lower-cases and collapses whitespace (`_normalize_name`), then turns every
run outside `a–z0–9` into `-` (empty → `unnamed`). It is lossy, so distinct names can produce one
filename. The slug is minted when a node is created (extraction `_emit`, human-add, split spin-off)
and re-derived only on a title amendment at promote; otherwise `nodes.slug` (mirrored from the page)
is the recorded page path.

Measured on the 2026-11-04 UAT vault: 7 of 51 active tags have lossy names: 5 hand-added test tags
(`Café`, `İstanbul`, `!!!`, `Data-Mesh`, `Straße`) and 2 real ones (`Kleiner-Hoel Dilemma`,
`Regulation (EU) 575/2013`).

## Decision 1 — the filename rule (grill Q1)

- A **lossless** normalized name (only `a–z`, `0–9` and single spaces) gets its **plain slug**:
  `Data Mesh` → `data-mesh`.
- Any other name gets **`<slug>--<8 hex of its item id>`**: `Data-Mesh` → `data-mesh--<8 hex>`.
  The hex are the first 8 of the `itm_` id's 16, so for a never-renamed item they equal the first
  8 hex of sha256 of its normalized name, which is the Authors rule.
- A plain slug never contains `--`, so the two namespaces cannot meet. Allocation depends only on
  the item itself, never on other names or on arrival order.
- **Collision-resistant, not collision-free.** Two lossy names with the same base slug whose ids
  share the first 8 hex would still meet; the B1 ownership and occupancy guards stay **mandatory**
  on every writer and refuse that write explicitly. (8 hex chosen for readability and parity with
  Authors; the full 16 would remove truncation collisions at the cost of longer names.)
- Two lossless names that normalize alike already have the same id (`AI` / `ai` are one tag), so a
  plain-slug collision needs a rename onto another item's exact name; see the name-identity check
  below.
- Considered and rejected: always `<slug>--<hex>` (uniform but every filename unreadable); a suffix
  only when the slug is taken (depends on arrival order); id filenames `itm_….md` (ADR-0060's
  choice for other families; loses the readable common case).

### Name-identity check on title changes (grill Q1 qualification)

Different ids give different suffixes, so the filename guard cannot see two items renamed to the
same lossy name (`Data-Mesh` twice → two files). A title change must therefore check **name
identity** separately and send the human to a merge instead. Scope: see decision 3.

### Recorded paths are never silently renamed

`nodes.slug` (mirrored from the page) stays the recorded page path. Adopting this rule does not
re-derive or move existing pages; it applies when a slug is minted. Migrating existing pages (with
their links and review subjects) is a separate, explicit decision below.

## Decision 2 — the base slug (grill Q2)

- **Readable base:** the normalized name is decomposed (NFKD), combining marks are dropped, and a
  small fixed map folds letters NFKD leaves alone: `ß`/`ẞ` → `ss`, `æ`/`Æ` → `ae`, `ø`/`Ø` → `o`
  (the map lists both cases explicitly, independent of the lower-casing order). Then every run
  outside `a–z0–9` becomes `-`, trimmed. `Café` → `cafe--…`, `Straße` → `strasse--…`, `İstanbul`
  → `istanbul--…`; `Cafe` stays `cafe`.
- **Losslessness is decided on the original canonical (normalized) name, never on the
  transliterated one**, so `Café` keeps its suffix and can never land on `cafe.md`.
- **Empty base:** an item whose base is empty (non-Latin or punctuation-only, e.g. `!!!`) is
  `item--<8 hex>`; `unnamed` retires for items.
- **One shared helper** in code for the base slug and the lossless test, used by items and
  Authors. **Suffix selection stays per family:** items use their frozen item id (decision 1);
  Authors keep their name-derived suffix (8 hex of sha256 of `name_identity`) and an
  author-specific empty-base fallback (`author--<8 hex>`), not `item`.
- **Changed Authors filenames are a migration, not just regeneration:** every Source page that
  links an Authors page is refreshed to the new filename and the replacement pages are published
  **before** any old generated page is removed; publication stays pending until both are done.
- **Tests:** composed and decomposed accents (NFC/NFD `Café` give one result), the uppercase map
  variants `ẞ`, `Æ`, `Ø`, Turkish `İ`, non-Latin names, punctuation-only names, and the 8-hex
  collision refusal of decision 1 (guards retained).

## Decision 3 — duplicate-name checks on new names (grill Q3)

Three paths give an item a name it did not have: a **keep with a title amendment** (document screen
or flat queue; promote moves the page and appends the old title to the aliases), a **split
spin-off name**, and **Add tag** (human-add). Ids are frozen at creation, so a renamed item's title
no longer hashes to its id, and a later rename or add of that title would mint a second item.

### Level 1 — exact name: blocks

- **Same name** = equal under the id normalization itself (`items._normalize_name`, called
  directly, never a copy), compared with every other item's **title**.
- **Excludes the item being renamed** (a self-rename or a case/spacing change never conflicts with
  itself).
- **Merged tombstones resolve to their survivor first**; the conflict, if any, is with the live
  survivor. `deleted` items do not count. Every other status counts (active, unused, candidate,
  hidden, deprecated_candidate, …).
- **Runs under the existing write/ledger lock, before any change**, like the B1 guards (the dry run
  and the real run both). A title whose authority is missing or unreadable (no page, no
  frontmatter, unreadable file) is an **explicit refusal**, never a silent pass.
- **The refusal offers an action that is actually available:**
  - **Add tag** or a **split spin-off:** show the existing tag; offer **Use existing** where the
    path supports it (Add tag does; a spin-off cannot reuse an item, so it is refused with the
    tag named). There may be no second item to merge yet, so Merge is not offered here.
  - **Rename conflict (title amendment):** link to the human-filed merge form (ADR-0064) **only
    when both items are eligible for merge**; otherwise name the other tag and the review action it
    needs first (e.g. a hidden or candidate match still blocks the duplicate, and may need keeping,
    unhiding or rejecting before a merge is possible).

### Level 2 — same `merge_key`: advisory

- **Same `merge_key`** (ADR-0064 fold: punctuation, `&`, apostrophes, last-token plural) against
  **active and unused** tags' titles **and aliases**, excluding the item itself. Extraction already
  resolves suggestions this way.
- **Never blocks:** distinct referents share keys (`C++` / `C` both fold to `c`). The screen shows
  the matching tag and **which title or alias matched**, with explicit **Use existing** and **Add
  separately** choices where the path supports them, **preserving the user's draft** (other unsaved
  choices on the form). The merge-proposal lint stays the later safety net.
- **Rechecked when the action executes:** the warning shown on the form is not trusted at Save; a
  match that appeared since is warned again, and a Level-1 conflict that appeared since blocks.

### Tests

Renamed-item matches (old title in aliases, new title on another id), self-renames, every lifecycle
status (including a merged tombstone resolving to its survivor and `deleted` ignored), alias
matches at Level 2, missing/unreadable title authority refused, the recheck at execution, and
distinct pairs such as `C++` / `C` staying addable.

## Decision 4 — no item migration; a report-only lint (grill Q4)

- **No migration tool.** The main repo's `wiki/` and `db/` are empty; the only populated vault is a
  disposable UAT clone; ADR-0066 decision 10 already plans a backup-gated clean restart from `raw/`
  with no conversion script. The rule applies when a slug is **minted** (creation, split spin-off)
  or **re-derived by a title amendment**; the restart yields a fully conforming vault.
- **Recorded paths are preserved:** ordinary publication, re-rendering and graph rebuilds keep the
  recorded `nodes.slug` (mirrored from the page) and **never recompute a path from a title**. A
  mixed vault keeps working; old shared slugs stay refused by the B1 guards.
- **`item_filename_nonconforming`** (report-only lint, ADR-0037 family): lists each item page whose
  recorded path differs from the rule's path for its current title and id, with the **actual and
  expected paths**. It never renames, never files a review item, and never blocks validation.
  Shared slugs and ownership conflicts stay separate integrity errors (`validate_graph`,
  `validate_projection`, ADR-0070 B1).
- **Authors are not deferred:** the decision-2 Authors filename change is an immediate publication
  change. Replacement pages and Source-page links are published together (replacements and links
  first, old generated pages removed last; pending until complete), independent of the item
  migration being deferred.
- **A future item migration** is a separate governed operation with its own ADR. It would need
  recoverable publication (ADR-0068) and an inventory of **every** path-bearing reference. The
  references known today are illustrative, not that inventory: the page file, `[[Items/…]]` links
  in generated pages and `index.md`, Source `items:` frontmatter, `nodes.slug`, keyword navigation
  rows, review subjects that hold page paths, and an audit record.
- **Clean-restart precondition (added to ADR-0066 decision 10):** before the restart, verify the
  backup contains the authoritative human state that regenerating from `raw/` cannot reproduce:
  item-page edits (titles, aliases, descriptions, statuses), the tag ledgers (`reviews/tag_links/`),
  the blocked list, confirmed Details in manifests, and all review decisions and the audit log
  (`reviews/`).

## Decision 5 — one slice, B1b, before B2 (grill Q5)

These changes belong together: filename allocation, the duplicate-name rules, and the minimum UI to
choose between reuse and a separate tag. They are built, reviewed and committed as **one slice,
B1b**, inserted into ADR-0070's order after B1 and before B2, so B2 improves presentation around
settled behaviour instead of placeholders.

**Contents:**
1. The shared base-slug and lossless helper (decision 2).
2. Item slug minting by the rule (decision 1) at creation, split spin-off and title amendment.
3. Authors switched to the shared helper, with coordinated publication (decisions 2 and 4).
4. Level-1 exact-name checks on the three paths, with their refusals and offered actions
   (decision 3).
5. Level-2 `merge_key` detection with the minimum UI: **Use existing** / **Add separately** on Add
   tag, draft preserved.
6. The `item_filename_nonconforming` report-only lint (decision 4).
7. Readable wording for the remaining refusals (no raw `slug_collision`-style codes on screen).

**Boundaries:**
- Existing item paths are preserved, except on an explicit title amendment.
- The B1 ownership and occupancy guards stay mandatory, including for 8-hex suffix collisions.
- Use existing / Add separately preserve the draft and recheck matches under the write lock when
  the action executes.
- Authors replacements and the affected Source links are published before old pages are pruned.
- The filename lint stays report-only.

**Review:** one external review of the whole slice, with **three separate test groups**:
- **allocation:** the rule and the base slug (NFC/NFD accents, `ẞ`/`Æ`/`Ø`, Turkish `İ`, non-Latin
  and punctuation-only names, the `--` namespace, the 8-hex collision refusal), recorded paths
  preserved by publication and graph rebuilds, the lint;
- **duplicate decisions:** both levels on every path, renamed-item matches, self-renames, every
  lifecycle status and merged-tombstone resolution, aliases, `C++` / `C`, missing title authority,
  the execution-time recheck, draft preservation;
- **Authors publication recovery:** the filename change publishing replacements and Source links
  before pruning, interrupted at each step and completed by the retry, with links never dangling.

## Implementation (B1b, 2026-11-04)

- **`app/workers/page_names.py`** (new): `base_slug` and `is_lossless`, shared by items and Authors.
- **`items.page_slug(name, node_id)`** is the item rule; `items._slug(name)` is now the name a
  *fresh* item gets (`page_slug(name, node_id(name))`). Minting calls `page_slug` with the real id:
  extraction (`_emit`, only for a new node — an existing node of any status keeps its recorded
  slug), human-add, split spin-off (a resumed spin-off keeps its recorded slug) and promote. Promote
  re-derives the slug **only when the effective title differs from the page title**, so an
  aliases-only or type-only amendment no longer moves a page.
- **`app/workers/item_names.py`** (new): `exact_conflict` (level 1; reasons `name_taken`,
  `title_authority_unreadable`) and `similar_tags` (level 2). Called under the ledger lock by
  human-add (only when it would create a tag), by `promote.activate_node` on a title change (so the
  keep's dry run refuses before any write) and by the split executor on the fresh path. A level-1
  refusal carries the conflicting tag (`conflict_node_id`, `conflict_title`, `conflict_status`,
  `via_tombstone`); `tag_links.keep` now passes the activation's `problem` through.
- **Add tag** (`POST /ui/reviews/sources/{sid}/add`): `add_use=<id>` is **Use existing** (the tag is
  kept as it is: its own title and type, no routed retype); `add_separately=<ids>` acknowledges the
  similar tags shown. A `needs_choice` outcome redraws the screen (409) with the choice box and every
  other unsaved field kept; a match not among the acknowledged ids asks again. A `name_taken`
  refusal shows Use existing for the conflicting tag when it can be reused.
- **Readable refusals:** `review_html.refusal_text` words the slug, ownership and name refusals
  (and a few others) on the document screen and its batch results.
- **Lint:** `item_filename_nonconforming` (`lint._check_item_filenames`), low, graph-gated,
  report-only; skips merged tombstones and unreadable titles.
- **Authors migration:** `authors.filename_for` uses the shared helper (empty base → `author`);
  `legacy_filename_for` recognises pages written by the previous rule as owned. Publication, when
  its index step will run, (1) adds every Source page linking an Authors page the rule no longer
  names (`authors.stale_link_sources`), (2) **writes** the Authors pages before the Source pages
  render (`_prewrite_authors`, no pruning), so a refreshed link never points at a missing page, and
  (3) in the index step prunes an old page only when no Source page links it; a still-linked one is
  kept and reported (`authors_still_linked:…`), and the publication stays pending.
- **Tests:** `tests/test_item_filenames.py` (allocation), `tests/test_duplicate_names.py` (duplicate
  decisions, including the screen), `tests/test_authors_filenames.py` (Authors publication
  recovery). Guard tests that need a shared page name now build it as an older vault would have
  one (`tests/fixtures.legacy_item_slug`).

### Review round 1 (2026-11-04)

Two blocking findings in the Authors migration (reproduced in synthetic vaults), two design gaps.

- **Source links are gated on verified Authors pages.** `_prewrite_authors` now runs whenever
  Source pages render (not only with the index step) and reports its problems instead of swallowing
  them (`authors_publication_failed:…`, `authors_conflict:…`, `authors_write_failed:…`).
  `authors.publish` reports a failed page write in `write_failed` and still writes the others.
  Before rendering, `_gate_author_links` checks **every** Authors link each Source page will carry
  (from its manifest, exactly as the renderer computes them, so links already on the page count
  too) against the page on disk (`authors.link_target_ok`: present, generated, this name's
  identity). A Source page that fails is not rendered: it keeps its previous version and old
  links, and stays pending as `authors_page_unavailable`. A page that exists with the right
  identity but whose update failed passes the gate (no dangling link), but the unfinished Authors
  work keeps the index step pending, **also for a Source-only refresh**.
- **An incomplete link scan never prunes.** `authors.source_links` treats a missing
  `wiki/Sources/` as no Source pages (a new vault), and raises `LinkScanError` when the folder
  can't be listed or any page can't be read. Pruning is then skipped
  (`authors_link_scan_failed:…`) and the publication stays pending.
- **Merge action on a rename refusal.** A `name_taken` refusal of a title amendment on the document
  screen asks `human_merge.plan_merge` (the existing tag surviving) for eligibility and direction:
  if the merge isn't blocked, it links the merge **preview** (which writes nothing); otherwise it says
  what the merge needs first. Title amendments only apply to suggestions, so today the renamed tag
  is never in use and the advice is what shows. No new rename path was added to reach the link.
- **Level 2 on all three paths.** `item_names.similar_advisory` returns structured data,
  `{kind: "similar_tags", name, node_id, matches}`. It is reported by a keep with a title amendment
  (`tag_links.keep` / `promote.activate_node` → `advisories`), by flat-queue promotions and by split
  spin-offs (Apply summary `promotions.advisories` / `split.advisories`). It is computed after every
  gate, and a failure to compute it gives no advisory, so it never changes whether the action runs.
  The batch results show it as an escaped note, apart from refusals. Use existing / Add separately
  stay on Add tag only: the other paths can't reuse a tag.

### Review round 2 (2026-11-04)

- **The gate moved to where Source pages are written.** A direct `generate_wiki` run wrote Source
  pages before its end refresh reached publication, so an interruption there left a dangling link.
  `wiki.generate_wiki` (the only writer of Source pages) now checks each page's Authors links before
  writing it (`authors.unavailable_links`, from the manifest). If a target isn't a verified page,
  it writes the Authors pages (no pruning, at most once per filename per call) and checks again.
  A page whose links still don't resolve is not written: it keeps its previous version and links,
  is reported with reason `authors_page_unavailable`, and stays queued. This covers direct runs,
  `scripts/generate_wiki.py` and callers using `rebuild_index=False`. Publication keeps its
  pre-write (for reporting and for keeping unfinished Authors work pending) and passes the typed
  reason through; its own per-Source gate from round 1 is removed as redundant.
