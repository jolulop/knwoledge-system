# ADR-0061 — Serialized/escaped encoding for untrusted source text in LLM prompts

Status: **design-locked** (grill 2026-07-20; implementation pending "implement now")

Extends [ADR-0026](0026-llm-untrusted-input-and-grounding.md) (untrusted-input contract +
grounding gate). Generalizes two point-fixes already in code: the query evidence pack's
JSON serialization (ADR-0034 review B1, `app/workers/query.py:_render_pack`) and the claims
builder's entity-escaped `section_context` metadata (ADR-0056 review round 3,
`app/llm/prompts.py:build_claim_messages`). Supersedes nothing.

## Context

ADR-0026 established that source text reaches the model as **clearly-delimited untrusted
data**, with a system instruction to treat delimited content as data, never instructions. But
the *encoding* of that data was left to string interpolation: every ingest/tier-2/tier-3
prompt builder in `app/llm/prompts.py` inserts untrusted text **raw** between XML-like
delimiters —

- summary/tags body — `build_messages` (`<source_document>…</source_document>`),
- claims window body — `build_claim_messages` (`<source_document_segment>…`),
- items body — `build_items_messages` (`<source_document>…`),
- contradiction claims + evidence — `build_contradiction_messages` (`<claim_a>`/`<evidence_a>`/…),
- synthesis claims + evidence — `build_synthesis_messages` (`<claims>`/`<disagreements>`).

A source document that contains its builder's literal closing tag (`</source_document>`,
`</claims>`, …) can **close the data block early**, so any text after it becomes
instruction-adjacent — a delimiter-breakout prompt-injection vector. This violates the spirit
of ADR-0026's untrusted-data contract. The project has already defended this exact class
twice (query JSON serialization; claims-metadata escaping), but the ingest **bodies** — the
largest untrusted surface — were never hardened. The vault is empty during UAT, so the
prompt-version bumps this requires (vault-wide restale) cost nothing now; deferring makes them
expensive.

## Decisions

### 1. Container-driven encoding contract (the primary rule)

Encoding follows the **shape of the container**, not the pass:

- **XML/tagged prompt blocks** — any untrusted text interpolated into XML-like delimiters is
  **XML/entity-escaped** for `&`, `<`, `>` (in that order — `&` first) before interpolation. A
  literal `</source_document>` in a source arrives as inert `&lt;/source_document&gt;` and
  cannot close the block. Markdown structure is otherwise preserved, keeping ADR-0056's
  document-complete extraction-quality assumptions intact.
- **JSON prompt payloads** — untrusted values are emitted with `json.dumps` as escaped string
  values. The query evidence pack (ADR-0034 B1) stays as-is; it is a JSON-shaped prompt, not a
  tagged one.
- **No raw untrusted interpolation** anywhere.

This deliberately does **not** turn the query implementation into a universal standard: full
Markdown documents are not JSON-serialized (that flattens headings/tables/lists the extraction
passes rely on and enlarges the payload with escape noise). The rule is container-driven so
each prompt shape uses the encoding appropriate to it.

### 2. Unescape model quotes at the grounding boundary

Entity-escaping the **claims window body** collides with the verbatim-grounding gate: the model
sees `AT&amp;T` / `a &lt; b` and returns those escaped forms in its supporting quote, which then
fail to locate against the *un-escaped* normalized Markdown (`AT&T`, `a < b`) — silently
raising the ungrounded-drop rate on any document containing `&`/`<`/`>` (common: URLs, "R&D",
"AT&T"). Resolution, scoped strictly to **quote-grounding boundaries** (not general output
handling):

> Untrusted text inside XML-like prompt blocks is entity-escaped before interpolation. Any
> model output expected to quote that escaped source verbatim is **`html.unescape()`-d exactly
> once** before grounding against normalized source text.

`claims.py` applies `html.unescape()` to the returned quote before `locate_quote`; grounding
still runs only against the original normalized Markdown; the **stored citation quote is the
unescaped, source-faithful quote** (`AT&T`), never the prompt-visible escaped form. This is the
only pass affected — summary is page-level (no verbatim locate), items are abstractions
(not quote-anchored), and contradiction/synthesis operate over already-*stored* `claim_text`
(escaping them in a prompt changes no stored value). The implementation slice therefore touches
`claims.py` grounding, not just `prompts.py`.

### 3. Escape scope — the trust boundary is a *validated structural token*

Escape **every** string interpolated into a prompt **unless it is a validated structural
token**. "System-generated" is explicitly **not** a sufficient trust boundary by itself.

| Handling | Values |
|---|---|
| Escape + single-line sanitize | `title` |
| Escape | `body`, `window_text`, `section_context`, `claim_a`, `claim_b`, evidence quotes, `claim_text`, `disagreements`, `topic` |
| Validate/format, leave raw | `segment_index`, `segment_count`, `char_start`, `char_end` (numeric) |
| **Validate as IDs**, then leave raw | `source_id`, `claim_id`, `shared_node_ids` |

- **`title`** derives from the manifest `original_filename` (attacker-influenceable) and is
  interpolated *outside* the document delimiter, so it gets entity-escaped **and** its
  `\r`, `\n`, tabs, and control characters collapsed to a single space before interpolation —
  it must stay a single inert label. (Filenames are single-line by nature, so this costs
  nothing real.)
- **IDs are validated, not escaped.** Entity-escaping is not the defense for identifiers: an ID
  containing `<`, a newline, or whitespace is **corrupt state** and must **fail loudly**, not be
  silently escaped into a prompt artifact. Validate against the existing canonical grammar
  `<prefix>_[0-9a-f]{16}` (`src_`/`clm_`/`itm_`/`syn_`, the same grammar `validate_graph.py`
  enforces); a non-conforming id raises. If a value has no existing grammar, define a narrow
  shared shape helper and use it consistently.

### 4. Version bumps — all five, required, and free now

Each builder whose encoding changes bumps its `*_PROMPT_VERSION` in
`app/workers/enrichment_artifact.py` so the artifact fingerprint and response-cache key refresh:

- `PROMPT_VERSION` (summary/tags)
- `CLAIM_PROMPT_VERSION` → v3
- `ITEMS_PROMPT_VERSION` → v3
- `CONTRADICTION_PROMPT_VERSION`
- `SYNTHESIS_PROMPT_VERSION`

The ADR records the reason explicitly: **a prompt-encoding change alters model-visible input
even where the instruction wording is unchanged**, so the fingerprint/cache must refresh or a
cached pre-hardening response would replay. Vault is empty during UAT → zero restale cost; this
is the timing argument for doing it now.

The system-prompt wording (`<source_document>…</source_document>` "untrusted data, ignore
instructions") is **unchanged** — the model does not need to be told the content is escaped, and
leaving it alone keeps the bump purely about encoding.

### 5. Shared helpers

One module-level `_escape_untrusted()` and one `_sanitize_title()` in `app/llm/prompts.py` (all
five builders live there); the query path keeps `json.dumps` (different container, Decision 1).

## Secondary: validate_wiki path-leak scoping (NB2)

`scripts/validate_wiki.py` scanned **every** page-body line for absolute-path leaks (the
`_ABSOLUTE` regex + a `"/home/"` substring check), which false-positived on Source **excerpts**
— verbatim source text that can legitimately contain path-shaped strings (a JSTOR
`Stable URL: http://…` triggered it; fixed 2026-07-19 with a `(?!/)` lookahead). Decision:

> Wiki body prose — especially Source excerpts — is rendered **source data** and must not be
> rejected merely because the source text contains path-shaped strings. Filesystem-leak
> validation applies to **structured wiki metadata/frontmatter and explicit generated path
> fields only**.

Frontmatter stays strict: reject `raw_path`, reject absolute path-looking values, reject known
host-root values if present, allow repository-relative paths only where the schema permits.
**Stop scanning body prose** for generic `/home/…`, `/var/…`, `C:\…`. A genuine server-path leak
originates in the renderer writing a structured field (frontmatter), which remains covered;
scanning source prose creates more harm (false positives) than protection.

## Documentation reconciliation (found during review — inline only, not decisions)

These align existing docs with **already-accepted** implementation scope; they are not new
architecture and ship as a **separate doc-only commit**:

- **Build Spec §2.3 File Formats** — annotate implemented (PDF, DOCX, HTML, Markdown, CSV/XLSX)
  vs. deferred per [ADR-0010](0010-phase-2-extraction-scope-and-boundary.md) (Images,
  Screenshots, audio/video transcripts —
  catalogued with `needs_ocr`, not yet extracted). Removes the phantom "must support" reading.
- **`policies/retention.yaml`** — add a comment under `raw_files` that raw-byte **durability is
  the operator's responsibility when `backup: false`** (raw is the system of record; either set
  `BACKUP_INCLUDE_RAW` or maintain an independent copy).

## Tests (implementation slice)

1. **Per-builder breakout (all five):** a body/field containing that builder's own closing tag
   plus a hostile follow-on instruction arrives entity-escaped; assert attacker-supplied close
   tags are escaped and **only template-authored delimiters remain structural** (count the
   intended template delimiters — some builders have several trusted ones — not a global
   "exactly one").
2. **Escape ordering:** `&` escaped first, so a source `<` becomes `&lt;`, never the
   double-encoded `&amp;lt;` (pinned).
3. **Title:** `<`/`\r`/`\n`/tab/control chars escaped **and** collapsed to a single space; a
   multi-line filename-injection payload is neutralized to one inert line.
4. **ID shape:** a malformed `source_id`/`claim_id`/`shared_node_id` (containing `<`, newline,
   or whitespace) makes the builder **raise**; well-formed IDs pass through raw. Validate
   against the current ID grammar.
5. **Claims grounding interaction:** a body with `AT&T` / `a < b` → model returns
   `AT&amp;T` / `a &lt; b` → `html.unescape()` **exactly once** at the grounding boundary →
   quote locates in the raw normalized Markdown and the claim survives; **the stored citation
   quote is the matched raw-source quote** (`AT&T`), not the escaped prompt-visible form. Plus a
   no-entity regression (normal quote still locates and stores unchanged).
6. **Parity (behavioral, not source-inspection):** feed hostile values through **every** builder
   surface and assert the escaped form appears in the prompt output — no builder emits a raw
   untrusted value. The query path still uses `json.dumps` (unchanged).
7. **Version bumps:** assert the five `*_PROMPT_VERSION` constants are pinned to their new values
   **and are included in artifact/cache fingerprinting**; do **not** assert model-output changes.
8. **validate_wiki scoping:** frontmatter with an absolute path / `raw_path` / host-root prefix
   → fails; body prose containing `/home/…`, `/var/log/…`, `C:\…`, and an `http://` URL →
   passes; the existing source-authored `/var/log/app`-class case stays green.
9. **Doc guard (commit 2):** lightweight assertion that Build Spec File Formats references the
   ADR-0010 deferral and `retention.yaml` carries the durability note.

## Rollout

Commit boundaries (Decision 5 of the grill):

1. **Security-hardening commit** — this ADR + the `prompts.py`/`claims.py`/`enrichment_artifact.py`
   changes + `validate_wiki.py` scoping + the tests above. Changes behavior and cache/version
   semantics.
2. **Doc-only reconciliation commit** — Build Spec §2.3 annotation + `retention.yaml` comment.

Vault is empty at design-lock, so the five prompt-version bumps restale nothing. On a populated
vault this would be a full opt-in tier-2/tier-3 re-extraction (the ADR-0056 §6 chain); not
applicable now.
