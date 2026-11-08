#!/usr/bin/env python3
"""The supersede completion marker on a Claim page (ADR-0073 decisions 10 and 16).

Three flat frontmatter fields, written in the same page write as the loser's deprecation and kept
unchanged by every later render (re-extraction, tombstone, re-projection, deprecation, hide and
unhide):

    superseded_by_review: "rev_<16 hex>"
    superseded_by_approval: "<32 hex>"
    superseded_by_claim: "clm_<16 hex>"      (the winner)

The marker is a historical record that the operation completed; it is separate from the page's
current status. All three fields or none: a partial or malformed marker is DAMAGED — unreadable
evidence, never "no marker". A field written under any other key spelling (indented, spaced before
the colon, quoted) is detected and DAMAGED too, never read as absent. Pure; never raises.
"""
from __future__ import annotations

import re
from typing import Any

REVIEW = "superseded_by_review"
APPROVAL = "superseded_by_approval"
WINNER = "superseded_by_claim"
FIELDS = (REVIEW, APPROVAL, WINNER)

_CANON = {
    REVIEW: re.compile(r"rev_[0-9a-f]{16}"),
    APPROVAL: re.compile(r"[0-9a-f]{32}"),
    WINNER: re.compile(r"clm_[0-9a-f]{16}"),
}
# A field counts as valid only on a line in the renderer's exact key spelling: `field:` at column 0,
# then the quoted canonical value. The existing tolerance (spaces or tabs after the colon and at the
# end of the line) is unchanged; a CRLF line ending is a line-ending difference, never damage.
_CANONICAL_LINE = {f: re.compile(rf'{f}:[ \t]*"([^"]*)"[ \t]*') for f in FIELDS}
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)  # = wiki_render

NONE = "none"          # no marker field at all, in a readable page
OK = "ok"              # a complete, canonical marker
DAMAGED = "damaged"    # some field present, but partial, duplicated, malformed or non-canonically keyed
UNREADABLE = "unreadable"  # the page's frontmatter can't be parsed, or a marker field sits outside it


def is_unreadable(state: str | None) -> bool:
    """A marker state that is unreadable evidence: never "no marker", never rendered away."""
    return state in (DAMAGED, UNREADABLE) or state is None


def _reserved_key(line: str) -> str | None:
    """The marker field this line names, under ANY spelling the project's frontmatter parsers (or
    YAML) would read as that key: leading indentation, whitespace before the colon, a quoted key, a
    CRLF ending. Detection only; whether the line is canonical is a separate check."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or ":" not in stripped:
        return None
    key = stripped.split(":", 1)[0].strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1].strip()
    return key if key in FIELDS else None


def _without_line_ending(line: str) -> str:
    return line[:-1] if line.endswith("\r") else line


def parse(text: str) -> tuple[str, dict[str, str] | None]:
    """`(NONE, None)`, `(OK, {review_id, approval_id, winner})`, `(DAMAGED, None)` or
    `(UNREADABLE, None)` for an EXISTING page's text.

    Two separate steps. Detection finds every line that names a reserved field under any spelling
    another parser would accept, so a present field is never read as absent and a duplicate can't
    hide behind first- or last-value behaviour. Validation then accepts a field only on a line in
    the renderer's exact form. A page whose frontmatter block can't be parsed is UNREADABLE (its
    authority is unknown), and so is a reserved field anywhere outside the parsed block."""
    text = text or ""
    m = _FRONTMATTER.match(text)
    if not m:
        return UNREADABLE, None
    if any(_reserved_key(line) for line in text[m.end():].split("\n")):
        return UNREADABLE, None
    found: dict[str, list[str]] = {}
    for line in m.group(1).split("\n"):
        field = _reserved_key(line)
        if field is not None:
            found.setdefault(field, []).append(_without_line_ending(line))
    if not found:
        return NONE, None
    if len(found) != len(FIELDS) or any(len(v) != 1 for v in found.values()):
        return DAMAGED, None
    out: dict[str, str] = {}
    for field in FIELDS:
        canonical = _CANONICAL_LINE[field].fullmatch(found[field][0])
        if canonical is None or not _CANON[field].fullmatch(canonical.group(1)):
            return DAMAGED, None
        out[field] = canonical.group(1)
    return OK, {"review_id": out[REVIEW], "approval_id": out[APPROVAL], "winner": out[WINNER]}


def is_valid(marker: Any) -> bool:
    """A marker dict the renderer may write (all three identities canonical)."""
    return (isinstance(marker, dict)
            and isinstance(marker.get("review_id"), str)
            and bool(_CANON[REVIEW].fullmatch(marker["review_id"]))
            and isinstance(marker.get("approval_id"), str)
            and bool(_CANON[APPROVAL].fullmatch(marker["approval_id"]))
            and isinstance(marker.get("winner"), str)
            and bool(_CANON[WINNER].fullmatch(marker["winner"])))


def lines(marker: dict[str, str]) -> list[str]:
    """The three frontmatter lines for a valid marker."""
    if not is_valid(marker):
        raise ValueError("invalid supersede completion marker")
    return [f'{REVIEW}: "{marker["review_id"]}"', f'{APPROVAL}: "{marker["approval_id"]}"',
            f'{WINNER}: "{marker["winner"]}"']


def matches(marker: dict[str, str] | None, *, review_id: Any, approval_id: Any,
            winner: Any) -> bool:
    """The marker names exactly this operation: its review, approval generation AND winner."""
    return (marker is not None and marker.get("review_id") == review_id
            and marker.get("approval_id") == approval_id and marker.get("winner") == winner)
