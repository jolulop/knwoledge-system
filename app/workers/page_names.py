#!/usr/bin/env python3
"""Readable page-name stems shared by item pages and Authors pages (ADR-0071 decision 2).

Two pure helpers over an already-normalized name (the caller's own identity fold: the item id
normalization for items, `manifests.name_identity` for Authors):

- :func:`is_lossless` — the name is only `a–z`, `0–9` and single spaces, so its plain slug maps back
  to exactly one name. Judged on the ORIGINAL normalized name, never on the transliterated one, so
  `café` is lossy and can never take `cafe`'s plain filename.
- :func:`base_slug` — the readable part: NFKD, combining marks dropped, a small fixed map for letters
  NFKD leaves alone (`ß`/`ẞ` → `ss`, `æ`/`Æ` → `ae`, `ø`/`Ø` → `o`, both cases listed so the result
  never depends on lower-casing order), then every run outside `a–z0–9` → `-`, trimmed. May be `""`
  (a non-Latin or punctuation-only name); each family supplies its own fallback.

Suffix selection is NOT shared: items append 8 hex of their frozen item id, Authors 8 hex of a hash of
their name identity. A plain slug never contains `--`, so suffixed and plain names cannot meet.
"""
from __future__ import annotations

import re
import unicodedata

_LOSSLESS = re.compile(r"[a-z0-9]+(?: [a-z0-9]+)*")
_FOLD = str.maketrans({"ß": "ss", "ẞ": "ss", "æ": "ae", "Æ": "ae", "ø": "o", "Ø": "o"})
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def is_lossless(normalized: str) -> bool:
    """True when the plain slug of `normalized` loses nothing (only `a–z0–9` and single spaces)."""
    return isinstance(normalized, str) and _LOSSLESS.fullmatch(normalized) is not None


def base_slug(normalized: str) -> str:
    """The readable slug of `normalized`: accents stripped, `ß`/`æ`/`ø` folded, `-`-joined; may be ""."""
    text = unicodedata.normalize("NFKD", normalized if isinstance(normalized, str) else "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).translate(_FOLD).lower()
    return _NON_SLUG.sub("-", text).strip("-")
