# ADR-0071 — Item page filenames

- **Status:** accepted — design-locked by grill-phase 2026-11-04 (Q1–Q5). Not implemented; built as
  ADR-0070 sub-slice **B1b**, before B2.
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
