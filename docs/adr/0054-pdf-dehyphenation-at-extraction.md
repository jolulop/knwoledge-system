# ADR-0054 — Deterministic PDF de-hyphenation at extraction (line-break hyphen repair)

**Status:** Accepted. **Design-locked 2026-07-04** (grill-phase); **implemented in this slice**
(`dehyphenate` in `app/workers/extractors/__init__.py`, applied in the PDF extractor before reflow;
`EXTRACT_CODE_VERSION = 1` in `app/workers/extract.py`; `tests/test_dehyphenation.py` covers the
contract matrix + an end-to-end anchor-contract run; verified against the originating UAT document —
the exact sentence extracts verbatim).

**Extends/claims:** ADR-0011 (extract/normalize layer — this is a text-quality pass inside it),
ADR-0012 (mechanical citation anchors — the reason cleanup MUST happen at extraction, never later),
ADR-0010 (`needs_ocr`/partial posture — unchanged), ADR-0037 (lint heuristics surface the
cost-bearing remediation after a repair re-extraction), ADR-0038 (retrieval-eval baseline must be
re-recorded once the committed corpus re-extracts with cleaned text).

## Context — the UAT finding

Disposable-vault UAT surfaced an exact-phrase keyword miss: `"This paper aims to make three primary
contributions"` returned zero results although the sentence is in the corpus
(`src_603f0cae8cd8ab94`, `ssrn-6372438.pdf`). The normalized Markdown contains
`"three primary con- tributions"` — the PDF hyphenates the word across a line break, pypdf preserves
`con-\ntributions`, and the paragraph reflow (`app/workers/extractors/__init__.py`
`paragraphs_from_text`, the `block.replace("\n", " ")` step) turns it into `con- tributions`,
destroying the only mechanical signal (`-` at end of line) that identifies the split. FTS5 then has
tokens `con` + `tributions`; the phrase can never match. The same artifact degrades vector-embedding
quality.

Two structural facts fix the design space:

1. **The repair must run in the PDF extractor path BEFORE the newline collapse**, while `-\n` is
   still visible. `paragraphs_from_text` is called only by the PDF extractor today, so the pass is
   PDF-scoped by construction.
2. **Cleanup at any later layer is structurally ruled out** by the citation-anchor contract
   (ADR-0012, `chunking.py`): `markdown[char_start:char_end] == chunk.text` always holds. Text must
   be born clean in the normalized Markdown; chunker- or index-side cleaning would break the
   verbatim gate.

A second artifact class observed on the same page — glued words with missing spaces
(`clescanrecogniseandrejecttamperedroadsigns`) — has **no reliable local signal**: repairing it
requires dictionary/ML word segmentation or a different PDF text extractor. That is a different
class of change (extractor quality, new dependency surface), **out of scope here**.

## Decisions

### 1. Scope — de-hyphenation + soft-hyphen strip only; glued-word repair deferred

- The slice repairs **line-break hyphenation splits** in the PDF extractor path and **strips U+00AD
  soft hyphens** anywhere in extracted PDF text.
- **Glued-word repair is explicitly out of scope** — deferred to a future **extractor-evaluation
  slice** (e.g. evaluate pymupdf/pdfplumber against the pypdf baseline). No dictionary or
  segmentation logic enters this slice.
- Non-PDF extractors (docx/html/markdown/tables) are untouched.

### 2. The repair contract (implementation contract, confirmed verbatim)

- Strip `U+00AD` soft hyphens anywhere in extracted PDF text.
- Before paragraph newline collapse, repair only **same-paragraph** line breaks matching:

  ```text
  <word-char><hyphen>[ \t]*\n[ \t]*<word-char>
  ```

  where `word-char` is **Unicode alphanumeric** (use `str.islower()`-style Unicode checks, not
  `[a-z]` — Spanish is the project's secondary language, `tecno-\nlogía` must repair).

- **Rule 1 — both boundary chars are lowercase letters: drop hyphen and newline.**
  `con-\ntributions` → `contributions`; `tecno-\nlogía` → `tecnología`.
- **Rule 2 — otherwise: keep hyphen, drop newline/spacing.**
  `COVID-\n19` → `COVID-19`; `anti-\nAmerican` → `anti-American`. This branch deletes only the
  line break, never a character.
- The rule is **total** (every `-\n` word split is repaired by exactly one branch), deterministic,
  and **paragraph-bounded by construction**: the pattern's single `\n` (with only `[ \t]*` legs)
  can never match across a blank line, so the repair never crosses a paragraph break — even though
  it deliberately runs **before** paragraph splitting/reflow, while the `-\n` signal still exists.
- **Accepted error class (documented, deliberate):** a lowercase compound split at its *real*
  hyphen loses it — `best-\nknown` → `bestknown`. This has a real search cost (FTS5 tokenizes
  `best-known` as two tokens, `bestknown` as one) but the frequency asymmetry in justified print
  PDFs overwhelmingly favors typographic hyphenation, and avoiding the error requires exactly the
  dictionary/segmentation logic this slice excludes.

### 3. Rollout — opt-in with documented sequence, no automation

- **New PDF intakes get cleaned text automatically** after the extractor change.
- **Existing vault content is NOT migrated automatically.** Operator repair is explicit
  (deterministic, key-free, no LLM spend):

  ```bash
  uv run python scripts/extract_sources.py --force
  uv run python scripts/generate_wiki.py
  uv run python scripts/reindex_keyword.py
  uv run python scripts/rebuild_index.py
  uv run python scripts/validate_all.py
  ```

- **Optional/cost-bearing follow-ups stay separate** — deliberate operator acts, never automatic:

  ```bash
  uv run python scripts/reindex_vector.py . --force   # GPU re-embed
  uv run python scripts/enrich.py                     # LLM cost (cache misses: text changed)
  uv run python scripts/extract_claims.py             # LLM cost (re-anchor claims)
  ```

- No automatic LLM spend, no automatic vector re-embedding, no silent semantic refresh. The
  existing lint heuristics (ADR-0037 `summary_rot` / `stale_claim_citation` / `synthesis_rot`)
  surface stale summaries/claims after a repair re-extraction and point to the deliberate
  remediation.
- **No repair wrapper script in this slice.** A later key-free convenience script is acceptable
  only if it stops before vector/LLM work and prints the exact downstream actions still required
  (named deferred option).

### 4. `extract_code_version` — observability marker, log-only

- Add an integer constant in the extraction worker (`EXTRACT_CODE_VERSION = 1`) and write it into
  every `normalized/extraction_logs/<source_id>.json`.
- **Observability only:** do not gate, lint, auto-reextract, or mutate old logs. A missing
  `extract_code_version` means "pre-marker / older extractor."
- Future extractor behavior changes increment the integer. A future **key-free lint check**
  (deferred, not built now) can compare logs against the current constant and suggest
  `extract_sources.py --force`.
- **Not** recorded in manifests for this slice: the extraction log is the derived-layer home for
  extractor implementation provenance; keeping manifests out avoids widening the change.

## Consequences

Downstream ripple of a repair re-extraction (normalized text changes):

| Layer | Effect | Cost |
|---|---|---|
| Source pages | `input_fingerprint` covers normalized Markdown → regenerate on next `generate_wiki.py` | free |
| Keyword index | fingerprint drift → `reindex_keyword.py` | free |
| Vector index | chunk text changed → explicit `reindex_vector.py` re-embed | GPU time |
| Enrichment artifacts | `input_fingerprint` drift; changed text = new prompt = response-cache miss | real LLM cost |
| Claims | stored char anchors reference the old Markdown → `stale_claim_citation` (ADR-0037) flags; remediation `rerun_extract_claims` | real LLM cost |
| ADR-0038 retrieval eval | committed corpus re-extracts with cleaned text → **re-record the baseline** (was MRR 0.968 / recall@5 0.994 / discrimination 0.931) | one eval run |

The `needs_ocr`/partial posture (ADR-0010) is unchanged: a scanned PDF still extracts to zero
text/chunks and is honestly reported (the keyword-index zero-citable-row allowance shipped in
`5f109b8` already tolerates that state).

## Deferred (named, not in this slice)

- **Glued-word repair / extractor-evaluation slice** (pymupdf/pdfplumber vs pypdf baseline).
- **Key-free repair convenience script** (must stop before vector/LLM work and print remaining
  actions).
- **Extractor-version lint check** (compare `extract_code_version` in logs vs the current constant,
  suggest `--force` re-extract).

## Tests (design intent; written at implementation)

Extractor-level, key-free:

- lowercase join drops hyphen: `con-\ntributions` → `contributions`.
- Spanish lowercase (Unicode): `tecno-\nlogía` → `tecnología`.
- uppercase/digit continuation keeps hyphen: `COVID-\n19` → `COVID-19`.
- compound uppercase keeps hyphen: `anti-\nAmerican` → `anti-American`.
- soft hyphen U+00AD stripped anywhere.
- repair does **not** cross blank-line paragraph breaks.
- accepted error class pinned as expected behavior: `best-\nknown` → `bestknown`.
- trailing whitespace tolerated: `con- \n tributions` → `contributions` (the `[ \t]*` legs).
- `extract_code_version` written into the extraction log; absent in pre-existing logs is valid.
- anchor contract holds end-to-end: chunks from a repaired document satisfy
  `markdown[char_start:char_end] == chunk.text`.
- non-PDF extractors' output unchanged; `needs_ocr` path unchanged.
