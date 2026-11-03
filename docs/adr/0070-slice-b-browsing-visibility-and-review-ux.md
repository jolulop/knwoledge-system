# ADR-0070 — Slice B: browsing visibility, kept tags in Obsidian, and review-screen UX

- **Status:** accepted — design-locked by grill-phase 2026-11-03 (Q1–Q12), amended by **review
  round 1** (2026-11-03, section at the end). Not implemented. Build order B1 → B5.
- **Date:** 2026-11-03
- **Drivers:** ADR-0066 "Deferred / out of scope (Slice B and later)", plus the part-3 UAT
  (2026-11-02/03) comments: **comment 4** (Obsidian's Tags pane empty while tag pages exist),
  author-name variants ("McKinsey" vs "McKinsey & Company" make two Authors pages), and competing
  `change_item_type` proposals for one tag (applying one withdrew the other). Unkept suggestions such
  as *Software Development* show as loose graph nodes; the committed graph filter
  `-[status:candidate]` was a stopgap.
- **Related:** ADR-0030 (page-authoritative status), ADR-0059 (one flat `wiki/Items/`, `item_type`),
  ADR-0060 (display aliases), ADR-0064 (`merge_key`), ADR-0066 (tagging; decisions 3–4, part 3 Q3
  `keywords:`, slice 4 Authors pages and source visibility), ADR-0068 (publication).

## Scope and order (grill Q1)

Slice B is built as **sub-slices**, each implemented, reviewed and committed on its own, in this
order:

1. **B1 — Obsidian and `index.md` visibility**, plus comment 4 (kept tags in `tags:`).
2. **B2 — review-screen UX:** real titles on the remaining screens, a readable Apply preview, an
   untouched-row warning plus "next document", a "not yet tagged" marker, queue UX.
3. **B3 — button styling:** quieter secondary and destructive buttons on the document screen.
4. **B4 — competing `change_item_type` proposals** on one tag.
5. **B5 — author-name variants.**

The whole of Slice B is grilled first; implementation starts only on "implement now".

## B1 — Visibility of tag pages in Obsidian and the index

### 1. Hide by filtering, never by moving pages (grill Q2)

Item pages stay where they are: one flat `wiki/Items/` folder (ADR-0059), status on the page
(ADR-0030). No page moves, no dot-folder, no "page only once kept". Hiding is done by:
- the committed Obsidian graph preset (`wiki/.obsidian/graph.json`), and
- `index.md`'s Items listing.

Accepted consequence: Obsidian's file explorer, search and quick switcher still show every item
page. Most of that clutter is unfinished review work. A suggestion page goes away once it is
dropped on every document (ADR-0066 decision 3).

Rejected: moving non-active pages into a hidden folder (status-dependent paths would touch review
records, links, validators and the flat folder), and not writing a page until the first keep
(reverses page authority for candidates; unused tags would still need hiding).

### 2. Show only active tags: an explicit expansion of ADR-0066 (grill Q3)

**This expands B1 beyond ADR-0066's "candidate and unused".** The graph preset and the index show
only item pages with `status: active`. Left out: `candidate`, `unused`, `merged` (tombstones),
`deprecated_candidate` and `hidden`.
- **Scoped strictly to item pages** (`Items/`). Sources, Authors, Synthesis (including candidate
  synthesis), Queries and every other family are unchanged. A global `status: active` filter is
  wrong: it would hide other families.
- **The pages stay on disk**, so merge redirects, audit history and existing links keep working
  (an old link to a merged tag still opens its redirect page).
- **Index counts.** The Items section lists active pages only (still grouped by `item_type`), with
  one count line per hidden status. A count links only to a screen that really lists that status:
  - `candidate` → the per-document screens (`/ui/reviews/sources`), where suggestions are rows;
  - `unused` → the Unused tags list (`/ui/reviews/tags/unused`);
  - `merged`, `deprecated_candidate`, `hidden` → a plain count, no link (no screen lists them;
    the index must not imply they have a review queue).

  Links use the same API base as the Source page's "open original" link.
- **Side effect:** an unclassified (sentinel) tag can only be a candidate, so the index's
  "Unclassified (review required)" group disappears from the index. Those tags stay on the review
  screens.
- **Acceptance check (implementation):** the exact Obsidian search syntax ("hide pages in
  `Items/` that are not active") must be verified in Obsidian itself against fixture pages of
  every family and status. Sources, Authors and synthesis pages, including candidate synthesis,
  must stay visible.

**Obsidian acceptance requirements for B1** (verified in Obsidian on a UAT clone; each failure is a
design issue to bring back, not something to patch silently):
1. The graph preset hides non-active `Items/` pages only. Every other family, including candidate
   synthesis, stays visible.
2. A digits-only child label (`domain/2024`) is accepted as a nested tag.
3. Unicode tag behaviour: accented labels (`Café`), case-only differences (`ai` / `AI`), `ß` /
   `ss`, a Turkish dotted/dotless `i`, and NFC vs NFD input. Record which forms Obsidian treats
   as equal, compared with the `casefold()` policy.
4. The id fallback label (`domain/itm_…`) and suffixed labels (`domain/C_3fa9c1`) appear as
   nested tags.
5. The Tags pane shows the `<item_type>/` tree with counts of documents.

### 3. Kept tags in the Source page `tags:`, nested by type (grill Q4, comment 4)

The Source page carries `tags:` again, as a **generated projection of the keep ledger**. It is
not another place where tags are decided: editing it in Obsidian never changes what is kept, and
the next publication overwrites it.
- **Contents:** every tag **kept on that document**, human-added tags included, **whether or not
  its name is on the blocked-tag list** (review round 1). Blocking filters suggestions and never
  touches kept links (ADR-0066 decision 5). Suggestions and drops never appear. Only on
  **default-visible** documents (the same visibility rule as the Authors pages, ADR-0066 slice 4).
- **Form:** `<item_type>/<label>`, using the governed `item_type` key and the item's canonical
  name, so the Tags pane becomes a browse-by-type tree. Example: `ai_topic_area/Neuroscience`.
- **Identity stays the item id.** The rendered tag string is only a browsing label.
- **`keywords:` is kept independently** (search terms: kept tags plus eligible suggestions,
  unchanged). `items:` is unchanged too.
- **Refresh:** keep/drop, add, title amendment, retype, merge and split re-render the affected
  Source pages through the existing publication flow (the same path that already keeps
  `keywords:`/`items:` current). When a write changes the **derived tag mapping** of any tag other
  than those it acts on, it records a **full Source-page publication intent** (see "Collisions").
- **Validation:** `tags:` must equal its projection, the same way `items:` must equal the body
  links (`validate_projection`).
- **Documented:** the Tags pane counts **document uses** of a tag. It is not the number of item
  pages.

#### Encoding rule (one deterministic rule; amended by review round 1)

`label` from the item's canonical name, in this order:
1. NFKC.
2. **Apostrophes deleted:** `'` U+0027, `’` U+2019, `‘` U+2018, `ʼ` U+02BC, `` ` `` U+0060,
   `´` U+00B4. This is the encoding's own list; `merge_key` is unchanged.
3. **`&` → ` and `**, with spaces on both sides, so `Research&Development` and
   `Research & Development` both give `Research_and_Development`.
4. Every run of characters that are not Unicode letters or digits → one `_`.
5. Leading and trailing `_` trimmed.
6. Case and accents kept.
7. **Digits-only labels get no prefix.** The complete tag (e.g. `domain/2024`) contains letters,
   which meets Obsidian's documented rule. To be confirmed in Obsidian (acceptance check).
8. **Empty label** (a punctuation-only name such as `++` or `…`): the label is the **item id**
   itself (e.g. `domain/itm_3fa9c1d2e4b5a6f7`). This fallback label takes part in collision
   checking like any other, because another item could literally be named that id.

The tag is `<item_type>/<label>`.

#### Collisions: suffix only the clashing tags (grill Q5; algorithm amended by review round 1)

- **Universe:** the tags that actually render, i.e. **active** items. A candidate's encoding can
  never force a suffix onto a kept tag.
- **Comparison policy:** Python `str.casefold()` on the complete `<item_type>/<label>`. This is the
  chosen policy, not a claim about Obsidian's equality rules. Whether Obsidian treats further
  Unicode forms as equal is an acceptance check.
- **Suffix:** `_` plus a prefix of the item id's 16-hex hash. Each tag has a suffix length from the
  sequence **0 → 6 → 8 → 10 → 12 → 14 → 16** (0 = no suffix).
- **Algorithm (a fixed point over the set of active items):**
  1. Start with every suffix length at 0.
  2. Group the final tags by casefolded value.
  3. In every group with two or more members, move **every** member to the next length,
     including members that had no suffix yet.
  4. Repeat from step 2 until no group has two or more members.
- **Termination:** lengths only grow and stop at 16. Two tags that both carry the full 16-hex
  suffix end in different ids, so they can't be equal. Every group therefore eventually splits.
  The result depends only on the set of active items, not on creation order.
- **Explicit failure:** if a member already at length 16 is still in a group (only possible after
  an invariant violation, e.g. duplicate item ids), the mapping raises a typed error. Publication
  stays pending with that reason. Nothing is published with ambiguous tags, and nothing falls back
  silently.
- **Derived and rebuildable.** The mapping is recomputed from the active item set every time, with
  no identity registry.
- **Publication (review round 1): a full Source-page intent.** A write compares the derived mapping
  before and after its commit. When the mapping changed for any tag other than the ones it acts on
  (a collision appeared, disappeared or lengthened), it records a **full Source-page publication
  intent**. Every Source page is then re-rendered from current authority, so an interruption is
  recovered by the normal publication retry. Prefix-based widening was rejected: it would need its
  own correctness proof, including transitive collisions. The `tags:` projection check in
  `validate_projection` detects drift, but it is a backstop, not the recovery path. Narrow this
  later only if a full refresh proves expensive.

Rejected: always adding the id suffix (every tag carries a code), and leaving clashing tags out
with a flag (a tag would go missing from the pane until fixed).

### B1 tests the implementation must include

- Encoding: punctuation, spaces, compact and spaced `&` (`R&D`, `R & D`), every listed apostrophe,
  accents, digits-only names, punctuation-only names (id fallback), names that normalize alike.
- Collisions: creation order does not change the result; case-only clashes; suffix-prefix
  clashes (6 → 8 …); a suffixed label equal to another item's plain label; suffixes extended to
  the full 16 hex; an item literally named another item's id fallback; duplicate item ids raise
  the typed error and leave publication pending; a collision appearing and disappearing.
- Publication: a mapping change records a full Source-page intent; publication interrupted after
  the commit and before other tags' documents are re-rendered is completed by the retry.
- Status transitions: keep, drop, last keep dropped (active → unused: tag leaves `tags:`),
  revive, human add, **a blocked but kept tag stays in `tags:`**, retype (prefix changes), title
  amendment, merge, split; a hidden document carries no `tags:`.
- Hand-edited `tags:` changes no keep, and the next publication overwrites it; the validator
  flags drift.
- Index: active-only Items listing, per-status counts, links only where a screen exists, other
  families unchanged.

## B2 — Review-screen UX

### 4. Readable Apply preview: plain list, technical detail folded (grill Q6)

- **One prediction path.** The readable sentences are built from the **same sandbox run** that
  produces the technical diff and the preview token (`_evaluate_preview` → `run_apply` on the
  copy, its result and the approved review files). There is no second prediction path.
- **Three outcome groups, kept distinct:**
  - **changes:** one plain sentence per approved review that changes something, grouped by kind,
    e.g. "Merge *X* into *Y*" or "Change type of *X*: `ai_topic_area` → `domain`";
  - **already in effect:** approvals that need nothing more, as one count line (not listed as
    changes);
  - **blocked / not appliable:** each with its reason and a link to its review page (where Reopen
    lives).
- **Names:** readable names (tag titles, document titles). Ids appear in the folded details when
  needed to tell duplicates apart.
- **Document counts** ("links on 3 documents move") appear only when the preview establishes
  them. Nothing is estimated.
- **Validators** are shown as passed, failed or not run.
- **The technical diff stays on the page** inside a closed `<details>` section. No JavaScript.
- **Unchanged:** the preview token, freshness and authorization rules (ADR-0040, ADR-0068
  decision 6), and when Apply is offered or withheld.
- **Tests:** mixed outcomes on one preview, duplicate names, titles that need escaping, and a
  stale preview refused on Apply.

### 5. The flat queue

- **Readable subject.** A "What" column with the readable subject: a tag name, "*X* into *Y*" for
  a merge, a document title. The review detail page heads with the same subject, plus the id.
- **Grouping:** rows are grouped by review type.
- **Local time:** times are shown in the machine's local time zone (Europe/Madrid on the user's
  machine), with the zone named.
- **Bulk reject / defer only (grill Q7).** Approval stays one at a time on the detail page.
  - It uses the existing decision functions and ledger, and records **one decision per review**.
  - Selection is explicit: no row is selected by default, and selection is limited to the
    current page.
  - Status and eligibility are rechecked under the existing write lock. Stale, retiring or
    otherwise ineligible reviews are skipped with a reason.
  - The result page lists which decisions were recorded and which were skipped.
  - A bulk action never runs Apply.
  - The batch is not atomic. Each recorded decision stays recorded and auditable if the batch is
    interrupted. A retry skips reviews that already carry that decision and never duplicates
    them; duplicate ids in one submission count once.
  - **Tests:** mixed eligibility, duplicate submitted ids, stale selections, interrupted batches
    and their retry.

### 6. Untagged documents, undecided rows and "next document" (grill Q8–Q9)

Two states, kept distinct:
- **Not yet tagged:** no **tag** decision has ever been saved for the document. A Details-only save
  does not count. Dropping every suggestion does count.
  - **Durable evidence (review round 1):** the document's tag-decision ledger
    (`reviews/tag_links/<source_id>.json`) records `first_tag_decision_at`.
    - **What sets it:** the first **successfully recorded** keep, drop, add, "use X" or **"never
      suggest"** chosen on that document's screen. A block counts here even though its effect is
      vault-wide: it is an explicit choice made on the document.
    - **Never cleared:** undoing a drop or dropping a keep leaves it in place. A refused or failed
      action never sets it.
    - **Lifetime:** it travels with the ledger through backup and restore. A changed file (new
      `source_id`) starts without it.
  - **Migration, evidence-based:** existing ledgers are filled once (repair path) from existing
    decisions (kept links, drop records) and **attributable** audit entries, i.e. tag audit events
    that carry that `source_id`. The earliest attributable audit timestamp is used.
    - **Decisions but no timestamp:** recorded as tagged, time unknown.
    - **No evidence at all:** recorded as **tagging history unknown**. It is neither "not yet
      tagged" nor tagged, and no timestamp is invented.
    - **Display:** the document list shows a separate "tagging history unknown" marker. The first
      decision recorded afterwards replaces it with a real timestamp.
- **Undecided rows:** rows on the document screen still awaiting a choice: **tag suggestions,
  type changes and retirements**. Counted with a split by kind (e.g. "3 tags, 1 type change still
  undecided").
  - **Deferred** reviews have already received a choice and are excluded unless reopened.
  - Each review is counted **once**, even when it reaches the document through several
    associations.
  - New extraction can add undecided rows to a document that was already reviewed.
  - **One eligibility rule** serves the screen count, the Save-result warning and Next
    navigation.

Surfaces:
- **Document list:** a "not yet tagged" marker, and a **"Next untagged document"** link.
- **Document screen:** the undecided count beside Save.
- **Save result page:** "N rows on this document are still undecided" (split by kind), with Back
  and **"Next document with undecided rows"**. Counts are recomputed after the Save. The warning
  never blocks saving, and untouched rows stay unchanged.
- **Next navigation:** forward in the list's deterministic order from the current document,
  wrapping once and excluding the current document. When nothing qualifies it shows "None
  remaining". (Today's "Next source" jumps to the first document in the list with rows; this
  replaces it.)
- **Tests:** all-drop decisions, a Details-only save, newly extracted suggestions on a reviewed
  document, partial saves, wraparound, no remaining documents, deferred rows, a review reached
  through two associations; undoing the only drop keeps "tagged"; "never suggest" as the only
  decision clears "not yet tagged"; a refused action sets nothing; backup/restore keeps the
  marker; migration with full evidence, with decisions but no timestamp, and with no evidence
  ("history unknown").

## B3 — Button styling (grill Q10)

Three tiers, with red kept as the primary (TIMIA guidelines: Rojo for buttons). CSS classes only:
forms, routes and behaviour are unchanged, apart from the destructive wording.
- **Primary:** solid Rojo, **at most one per form**. Examples: *Save all changes on this page*,
  *Apply now*, *Confirm and approve merge*.
- **Secondary:** white, grey border, dark text. Examples: *Add tag*, *Undo drop*, *Pre-select
  keep*, *Use these*, *Add author rows*, *Use this spelling*, *Preview*, *Preview merge*, *Reopen*,
  *Unblock*, *Retry publishing*.
- **Destructive:** white, Rojo border and Rojo text, with a label that names the loss (e.g.
  "Delete 4 unused tags"). Never the Enter-key default. Examples: *Delete unused*, the new bulk
  *Reject*.
- Contrast stays WCAG 2.1 AA on the real backgrounds (text 4.5:1, borders 3:1), as for the
  palette.

## B4 — Competing `change_item_type` proposals (grill Q11)

Reviews stay **one per target type** (subject `{node_id, to_item_type}`), which keeps per-target
review identity and rejection history.
- **Visible:** every surface that shows a retype (document-screen row, review page, queue) names
  its competitors for the same tag. For each competitor it shows the target type, the review
  status and the source document, where available.
- **Approval guard:** approving a retype is **refused while another approved, not-yet-applied
  retype of the same tag exists**, with a link to reopen that one. The check runs under the
  existing write lock and covers the UI form, a stale form and the JSON/API decision path.
- **Apply recheck:** if conflicting approvals already exist (e.g. from a restored database, or
  approvals made before this guard), Apply **blocks** that tag's retypes with a reason. They are
  never resolved by processing order. The blocked tag appears under "blocked" in the readable
  preview (decision 4).
- **Withdrawal stays as today**, and only an applied retype withdraws its pending or deferred
  competitors (`superseded_by_retype`). The preview lists the **exact** proposals the chosen type
  will withdraw. Rejecting or deferring a retype never withdraws its competitors.
- **Tests:** concurrent approvals, stale forms, reopening, an already-applied proposal, and a
  restored database holding two conflicting approvals.

Rejected: folding proposals into one review (changes review identity and per-target rejection
keying), and letting the newest proposal replace the older one (a later document would silently
override an earlier suggestion).

## B5 — Author/publisher name variants (grill Q12)

A **hint on the Details form** plus a **report-only lint**. Nothing merges, and identity rules
(`manifests.name_identity`, one Authors page per normalized name) are unchanged. Similar names are
a suggestion for human review, **never evidence** that two sources share an author or publisher.
**Independence changes only after corrected Details are explicitly saved.**

- **Form hint.** A name in the Details form (an Enrich suggestion or the saved value) that matches
  an already-confirmed name gets a note: "Already confirmed elsewhere: *McKinsey & Company*
  (publisher of *<document>*, …)".
  - The note shows where the alternative spelling is confirmed (role and documents), so the user
    can judge it.
  - A **"Use this spelling"** button changes only that field, keeps every other unsaved choice,
    and saves nothing (a redraw, like *Use these*).
  - **Server-rendered:** hints for a newly typed name appear only after a redraw (Enter or any
    redraw button). There are no live suggestions. The existing `datalist` of known names stays.
- **Lint (report-only):** possible variants among the confirmed names, with links to the affected
  documents' screens. It never merges Authors pages and never files a review or semantic decision.
- **Matching rule: a provisional hint heuristic** (deterministic and bounded, but not
  "conservative": it produces false positives a human must dismiss; review round 1).
  - Compared only against **confirmed** names on valid manifests, across both roles. The role is
    shown, and author/publisher differences are reported, never hidden.
  - **Fold:** `merge_key`'s tokenization (NFKC, case, apostrophes, `&`/and, punctuation)
    **without its plural step**, which `manifests.name_identity` also omits for names ("Williams"
    must not fold to "William"). Accents are removed, **for the hint only**.
  - Two different identities are a **possible variant** when either:
    - **(a) whole-token containment:** the shorter fold's tokens occur as a contiguous run in the
      longer one's, and the shorter has at least one token of ≥4 letters that is **not a generic
      institutional word** ("McKinsey" ⊂ "McKinsey & Company"). The generic list is fixed in code:
      *university, institute, institution, school, college, company, corporation, group, press,
      consortium, foundation, association, society, department, centre, center, council, agency,
      office, laboratory, lab, research, international, national, global*. So "University" never
      hints "Oxford University"; or
    - **(b) initials:** the same number of tokens, the same last token, and every other token of
      one is a single-letter initial matching the other's first letter ("D. C. Dennett" ~
      "Daniel C. Dennett").
  - Nothing else counts: no edit distance and no embeddings.
  - **Candidate generation:** a word index over the confirmed names' qualifying tokens (≥4 letters,
    not generic). Only names sharing such a token, or for rule (b) the last token, are compared.
  - **Computation is bounded separately from display.** A common surname bucket could still make
    the work quadratic, so each run has a **deterministic comparison budget**: candidates are
    visited in sorted `name_identity` order, and comparison stops at the budget, initially
    **2,000 pairs per form render** and **20,000 per lint run** (tunable constants). When the
    budget is hit, the form says "name matching was cut short" and the lint reports
    `truncated: true` with the number of pairs compared.
  - **Display bounds:** at most **3 hints per field**, ordered by the number of confirming
    documents, then by name. Lint output: at most **50 pairs**, plus an "N more" count.
- **Tests:** false positives (short tokens, e.g. "AI" never matches "AI Lab" through rule (a);
  generic words, e.g. "University" vs "Oxford University", "Press" vs "MIT Press"; different
  people who share a surname, e.g. "J. Smith" vs "Adam Smith" are not an initials match, "A.
  Smith" vs "Adam Smith" is a hint, not evidence), abbreviations and initials, punctuation,
  `&`/and, accents, no plural folding ("Williams" vs "William"), author vs publisher role, the
  comparison budget reached (truncation reported, deterministic result), "Use this spelling"
  keeping the other unsaved choices, the lint filing nothing, and independence unchanged until
  Save.

## Review round 1 (2026-11-03)

One external design review of the grilled ADR: three blocking issues, four non-blocking. The user
accepted the proposed resolutions with six adjustments (marked "user" below). Documentation only.

| # | Issue | Resolution |
|---|---|---|
| R1 | Blocked names conflicted with kept-link authority | `tags:` includes every kept tag, blocked or not; blocking filters suggestions only |
| R2 | Encoding underspecified | Exact apostrophe list; spaced `&`; no prefix for digits-only labels (Obsidian check); punctuation-only → item id label, **collision-checked too (user)** |
| R3 | Collision resolution might not terminate; recovery | Fixed-point suffix algorithm, 0 → 6 → 8 → … → 16, every group member extended, **typed failure on an invariant violation (user)**; **full Source-page publication intent when the mapping changes (user, replaces prefix widening)** |
| R3b | `casefold()` claim | **Stated as the chosen policy, not as Obsidian's rule; Unicode cases added to the Obsidian acceptance check (user)** |
| N1 | B5 bounds | **A deterministic comparison budget with truncation reported, separate from the display caps (user)**; 3 hints per field; 50 lint pairs |
| N2 | "Conservative" overstated | Reworded as a provisional hint heuristic; generic institutional words excluded from containment; false-positive tests |
| N3 | Plural folding | Omitted for names, as in `name_identity` |
| N4 | "Never saved" needs durable evidence | `first_tag_decision_at` in the ledger, never cleared by undo; **"never suggest" counts (user)**; **evidence-based migration, "tagging history unknown" when evidence is missing (user)** |

## Implementation — B1 (2026-11-03)

- **`app/workers/tag_projection.py` (new):**
  - `encode_label` and `build_mapping` implement the encoding rule and the fixed-point collision
    rule.
  - `active_mapping` builds the vault's mapping: every `active` item, the title from **its own**
    page (the authority) and its graph `item_type`. Review round 2: there is no fallback. A
    missing or unreadable page, a page whose `item_id` names another item, an empty title or a
    missing type raises `TagMappingError`, so publication stays pending rather than publish a
    borrowed or invented name.
  - `source_tags` returns a document's `tags:`.
  - `load_published`, `save_published` and `changed_ids` handle the last published mapping.
- **Apostrophes are deleted before *and* after NFKC.** NFKC turns `´` U+00B4 into a space plus a
  combining accent, so deleting only after NFKC never saw it. Found by the encoding tests.
- **Source page:** the template gains `tags: {{tags_fm}}` after `keywords:`.
  - `generate_wiki` passes the curated (kept) active tags through the mapping, on default-visible
    documents only.
  - Within one locked call the mapping is derived once. A run derives it again inside each per-page
    hold.
  - Legacy tags (active, linked before the keep ledger) are curated rows on the page, so they appear
    too.
- **Publication: how "a write records a full Source-page intent" is realized.** Publication compares
  the current mapping with the **last published mapping**, `db/tag_labels.json` (`_widen_for_tag_labels`).
  - A change to a tag is *covered* when the entries acted on it, or when it is still active and
    every document keeping it is already in scope.
  - Any uncovered change, or an unknown published mapping (missing or malformed file), adds
    **every** extracted Source page to the scope of every entry, so a failure stays queued.
  - The snapshot is saved only when every Source page in that publication rendered.
  - **Crash safety:** the comparison runs at publication from durable state, so a write that
    commits and dies before publishing is covered by the retry. The pending entry is widened and
    the old snapshot still differs.
  - The snapshot holds labels, never identity. Deleting it costs one full Source-page refresh.
  - A `TagMappingError` adds every Source page too. Each fails to render and stays pending.
- **Hand edits to `tags:`:** the Source-page fingerprint skip leaves a hand-edited page alone until
  its inputs change, as for every generated page. `validate_projection` reports the drift, and
  `scripts/generate_wiki.py --force` restores it. Keeps are never read from the page.
- **Index** (`scripts/rebuild_index.py`):
  - Items lists active pages only. "Not listed here" count lines link to `/ui/reviews/sources`
    (candidate) and `/ui/reviews/tags/unused` (unused), and give plain counts for merged,
    deprecated_candidate, hidden and any other status.
  - The base URL comes from the "open original" link (`raw_view_base`).
  - Navigation Counts shows the listed (active) Items.
- **Graph preset:** `-path:Claims -file:index -file:log -(path:Items -[status:active])`. **Not yet
  verified in Obsidian:** this is Obsidian acceptance requirement 1.
- **Validator:** `validate_projection` checks each Source page's `tags:` against the projection
  (archived documents: empty) and reports a mapping that cannot be built.
- **Tests:**
  - `tests/test_tag_projection.py` (new): encoding, collisions (order, case-only, 6 → 8, the full
    16, a suffixed label equal to a plain one, the id fallback), and integration through real
    keep, drop, undo and block actions. The integration covers a collision rewriting another
    document's page and disappearing again, an interrupted publication completed by the retry, a
    lost snapshot, a keep that refreshes only its own documents, an unbuildable mapping, a
    blocked-but-kept tag, a title amendment, an archived document, and hand-edit drift.
  - `tests/test_wiki_render.py`: the active-only index and its count lines.
  - Retype, merge and split publish through Apply's full publication. Real Apply tests were
    added in review round 2.
- **Found while testing, pre-existing:** two different tags whose names slugify the same share
  one page file. Examples: `Café` and `Caf` → `wiki/Items/caf.md`; any all-non-Latin name →
  `unnamed`; `C++` / `C` when neither is kept, since an active tag absorbs the other by
  `merge_key`. Writing or deleting that file for one tag destroyed the other's page-held title,
  aliases, description and status. Review round 2 makes B1 fail safe. **The permanent
  filename policy is a separate design.**

## Review round 2 on B1 (2026-11-03)

One external review of the B1 implementation: three blocking issues (P1 shared page files, P2
resolver, P3 mapper), plus requested tests. The user accepted the fixes with tightened safeguards.

- **P2, resolver:** in each clashing group only the members below 16 hex extend; saturated
  members stay. `TagMappingError` is raised only when no member of a group can extend (duplicate
  ids). The reviewer's three-item case (two `X` needing full suffixes, and a third literally named
  one of the results) now resolves.
- **P3, mapper:** strict ownership and authority, as above.
- **P1, page ownership** (`app/workers/item_pages.py`, new). One rule decides who may write or
  delete `Items/<slug>.md`:
  - **the item's own page** (`item_id` equals the item): writable;
  - **absent:** writable only when no other item node, **in any status** (candidate, active,
    unused, hidden, merged, deprecated_candidate, …), records the same slug;
  - **another item's page, or a page whose owner can't be established** (unreadable, no
    frontmatter, missing or malformed `item_id`): refused. It fails closed.
  - Deleting is allowed only for the item's own page or an absent one.
- **Checks run before authority changes, under the existing write/ledger lock.** Multi-item
  operations check every destination before their first write:
  - **Extraction** (`items._emit`): a suggestion whose page name is taken or unowned is skipped
    before its node or mention exists. Human decisions are untouched. The run is `partial`, with
    `suggestions_skipped_slug_taken` and `slug_taken_details` (source, proposed name, slug,
    reason, owner, holders).
  - **Tag actions:** keep, drop, edit aliases, undo drop and "use X" check the tag and its
    `duplicates` partners. A block checks every matching tag and partner before writing the
    list entry. `delete_tag` checks before its deletion intent, and Delete unused reports a
    refused tag under `skipped`.
  - **Human-add** checks graph holders as well as the file.
  - **Promote** (`activate_node`) checks the current page and a moved slug's destination,
    including in its dry run, which is the keep preflight.
  - **Apply executors:** retype (in `retype_check`, so the queue's applied column agrees), merge
    (survivor, absorbed tombstone, duplicate partners) and split (primary, spin-off) refuse as
    typed skips before the journal.
  - The page renderers (`_recompose_node`, `recompose_semantic_node_page`) return
    `page_conflict` without writing. Publication keeps such a page pending with a hint.
  - `write_page` / `remove_page` enforce the same rule at the point of IO, as a backstop.
- **Validation:** `validate_projection` builds the tag mapping (strictly) only when a Source page
  has an active kept tag to project, so a vault with no such page is not failed for unrelated
  item pages. Test fixtures with non-canonical ids (`itm_x`) now use canonical ones: the
  ownership rule rejects a malformed `item_id`.
  `validate_graph` reports item nodes that share a slug (any status).
  `validate_projection` reports an item page whose `item_id` is not the graph item recording that
  page name.
- **A block cannot see a tag through a borrowed page.** A tag's names are read from its page, so
  a tag whose recorded page belongs to another item matches no block. The block only records the
  name and neither withdraws nor deletes anything for that tag.
- **Tests:**
  - `tests/test_item_page_ownership.py` (new, 20): the rule, plus fail-closed ownership (no
    frontmatter, no `item_id`, malformed, non-string, unreadable).
  - Extraction: skip-and-report, a page name held in every status with the file gone, an unowned
    hand-made file.
  - Human-add refusal; keep, drop and block on an existing collision, with no ledger or graph
    change; `delete_tag` and Delete unused.
  - Apply: retype, merge and split skips, and a split spin-off onto a page name held by a file or
    only in the graph (journal never opened).
  - Both validators.
  - `tests/test_tag_projection.py` (57): the saturated-member resolver case; the mapper refusing a
    missing, unreadable, wrong-owner or untitled page (publication pending, then recovered);
    repeated recovery failures staying queued until fixed.
  - Interruption: a crash after the authority commit (intent recorded, keep committed, nothing
    published), recovered by the next publication; a partial Source refresh that keeps the
    snapshot, then the retry finishing.
  - **Real Apply** (`POST /reviews/apply` with a preview token): a retype ending and then
    re-creating a collision, a merge ending one, and a split creating one, each rewriting the
    untouched document's `tags:`.

## Review round 3 on B1 (2026-11-04)

One remaining blocker: "use X" checked only the suggestion and the chosen tag. The inner `drop()`
then checked the suggestion's duplicate partners after the chosen tag had already been kept, and
left a resolving record behind. `use_existing` now checks every affected page first: the chosen
tag, the suggestion, and both sides' duplicate partners. The check runs before cancelling an
earlier resolution or writing the resolving record. Regression tests (`tests/test_item_page_ownership.py`)
cover a foreign-owned partner on either side, with and without an earlier pending resolution.
Each asserts the ledger, the graph and the pending resolution are unchanged. All four fail
against the old preflight.

## Implementation notes

- Each sub-slice follows the usual rhythm: implement on "implement now" → tests → external review
  → fixes → commit → push. B1's Obsidian acceptance check needs a UAT clone and Obsidian.
- `CONTEXT.md` gains: *Tag projection* (`tags:`), *Not yet tagged*, *Undecided row*.
