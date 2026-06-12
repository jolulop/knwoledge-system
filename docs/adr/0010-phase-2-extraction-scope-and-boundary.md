# Phase 2 extraction scope and boundary

Phase 2 (Extraction and Normalization) is a deterministic, key-free stage that turns
each raw source into normalized artifacts and stops there. It extracts embedded text
from PDF, DOCX, HTML, and Markdown, and tabular data from XLSX/CSV, using the
declared `[extraction]` dependencies (pypdf, python-docx, beautifulsoup4, pandas,
openpyxl). It does **not** call an LLM, generate summaries/tags/entities/claims, write
wiki pages, or build search/graph indexes — those belong to Phase 3 and Phase 4. Like
Phase 1, it requires no API keys and produces byte-stable output for byte-stable
input.

OCR and image captioning are deferred to a later phase: the declared dependency set
contains no OCR engine or vision model, and adding them would introduce system
dependencies and non-determinism. A source that yields little or no embedded text
(e.g. a scanned, image-only PDF) is recorded with a `needs_ocr` warning and a
`partial` status — it is not an error and not a human review item, so it can be
re-processed once OCR exists.

Extraction treats every input as untrusted, potentially hostile data. It enforces a
maximum file size and a per-file timeout; a file that exceeds them, fails to parse, or
looks like a decompression bomb is marked `error` with a reason in its extraction log,
and the run continues rather than crashing. Extraction performs no network I/O (no
remote includes, relationships, or fetches). Extracted text is inert content: it is
carried forward only as quoted evidence for later phases and is never interpreted as
instructions, consistent with the untrusted-data rule in AGENTS.md and CLAUDE.md.

Consequences: Phase 2 stays fully reproducible and offline, which keeps it cheap to
re-run and test. The cost is that scanned documents and images are catalogued but not
yet readable until OCR lands, and large or malformed files are skipped rather than
partially salvaged. Both are deliberate trades favoring determinism and safety, and
both are recoverable later because the raw source and its manifest are preserved.
