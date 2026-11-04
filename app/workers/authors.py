#!/usr/bin/env python3
"""Authors & publishers pages (ADR-0066 decision 6; part 3 grill 2 Q2–Q3, slice 4).

`wiki/Authors/` is a code-owned **navigation projection** of the manifests' confirmed provenance, never
graph nodes or knowledge items. One page per normalized name (`manifests.name_identity`), across the
author and publisher roles; a page lists only documents whose Details are confirmed, whose name is
ticked "show page", and whose source is default-visible (search's `RETENTION_DEFAULT_STATUSES`).

- :func:`inventory` is the one read-only, deterministic builder both consumers use: publication's
  `index` step (:func:`publish`, which renders and prunes the pages) and `rebuild_index.py` (the
  "Authors & publishers" index section). An unreadable manifest raises :class:`InventoryError`
  before anything is pruned.
- **Filenames** never depend on other names: the plain slug of a lossless identity (only `a–z`,
  `0–9` and spaces), otherwise `<base>--<8 hex of sha256(identity)>`; `--` never occurs in a plain
  slug, so the two namespaces can't collide. ADR-0071 decision 2: the base and the lossless test are
  the shared `page_names` helpers (accents stripped, `ß`/`æ`/`ø` folded); an empty base is `author`.
  A page named by the previous rule (`legacy_filename_for`) is still recognised as owned, so the
  change migrates: the Source pages are refreshed to the new names first (publication widens to
  them), the replacement pages are written, and an old page is removed only once no Source page
  links it.
- **Ownership** (overwrite and prune): a file in `wiki/Authors/` with `type: author`,
  `generated_by: authors-v1`, `author_id` equal to its filename stem, and that stem equal to the
  filename derived from its `name_identity`. A file claiming the marker but failing a check is
  protected and reported invalid; a file without the marker is handwritten. Neither is ever
  overwritten or removed: a collision with either keeps publication pending.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from app.backend import manifests
from app.backend.search import RETENTION_DEFAULT_STATUSES
from app.backend.publication_queue import _sync_dir, _write_durably
from app.workers import page_names
from app.workers.wiki_render import display_link_label

DIR = "Authors"
GENERATOR = "authors-v1"
ROLES = ("author", "publisher")
_LEGACY_LOSSLESS = re.compile(r"[a-z0-9]+( [a-z0-9]+)*")
_SOURCE_AUTHOR_LINK = re.compile(r"\[\[Authors/([^\]|#]+)")
_TITLE_RE = re.compile(r'(?m)^title:\s*"(.*)"\s*$')


class InventoryError(RuntimeError):
    """The manifests can't be read reliably: nothing is rendered or pruned."""


class LinkScanError(RuntimeError):
    """`wiki/Sources/` exists but can't be listed, or a Source page in it can't be read: the link
    scan is incomplete, so nothing may be pruned on its strength (ADR-0071 review round 1)."""


def _q(value: str) -> str:
    """A frontmatter string that round-trips exactly (slice-4 review round 1): JSON encoding is a
    valid YAML double-quoted scalar (quotes, apostrophes and backslashes escaped), and brackets are
    escaped too, so a name never puts a literal `[[` in the page."""
    return json.dumps(value, ensure_ascii=False).replace("[", "\\u005b").replace("]", "\\u005d")


def read_frontmatter(path: Path) -> dict[str, Any] | None:
    """An Authors page's frontmatter as written by :func:`render_page`: quoted scalars and flow lists
    decoded exactly, bare scalars as text. None when the file has no complete frontmatter block."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return None
    out: dict[str, Any] = {}
    for line in text[4:text.index("\n---\n", 4)].splitlines():
        key, sep, raw = line.partition(":")
        if not sep:
            continue
        raw = raw.strip()
        try:
            out[key.strip()] = json.loads(raw) if raw[:1] in ('"', "[") else raw
        except ValueError:
            out[key.strip()] = raw
    return out


def _identity_hex(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]


def filename_for(identity: str) -> str:
    """The page's filename stem for a name identity (grill 2 Q3; base per ADR-0071 decision 2)."""
    base = page_names.base_slug(identity)
    if page_names.is_lossless(identity):
        return base
    return f"{base or 'author'}--{_identity_hex(identity)}"


def legacy_filename_for(identity: str) -> str:
    """The stem the pre-ADR-0071 rule gave (no transliteration, `unnamed` fallback). Used only to
    recognise and retire a page written before the change, never to write one."""
    slug = re.sub(r"[^a-z0-9]+", "-", identity).strip("-") or "unnamed"
    if _LEGACY_LOSSLESS.fullmatch(identity):
        return slug
    return f"{slug}--{_identity_hex(identity)}"


def source_links(wiki_dir: Path) -> dict[str, set[str]]:
    """`{Authors filename stem: {source ids whose Source page links it}}`, read from the pages.

    A missing `wiki/Sources/` is a vault with no Source pages yet (`{}`). A folder that exists but
    can't be listed, or any page in it that can't be read, raises :class:`LinkScanError`: an
    incomplete scan must never look like "nothing links this page"."""
    folder = Path(wiki_dir) / "Sources"
    try:
        paths = sorted(p for p in folder.iterdir() if p.suffix == ".md")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise LinkScanError(f"wiki/Sources can't be listed ({type(exc).__name__})") from exc
    out: dict[str, set[str]] = {}
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LinkScanError(f"Sources/{path.name} can't be read ({type(exc).__name__})") from exc
        for stem in _SOURCE_AUTHOR_LINK.findall(text):
            out.setdefault(stem.strip(), set()).add(path.stem)
    return out


def stale_link_sources(root: Path, *, wiki_dir: Path | None = None) -> set[str]:
    """Source ids whose page links an Authors page the current rule no longer names for it — the
    pages a filename-rule change must refresh before the old Authors pages may go. Raises
    :class:`LinkScanError` when the Source pages can't all be read."""
    root = Path(root)
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    valid, _skipped = manifests.valid_manifests(root / "raw" / "manifests")
    wanted: dict[str, set[str]] = {}
    for m in valid:
        try:
            names = linked_names(m)
        except (KeyError, TypeError, ValueError):
            continue
        wanted[m["source_id"]] = {f for role in names.values() for _n, f in role if f}
    stale = set()
    for stem, sids in source_links(wiki_dir).items():
        stale |= {s for s in sids if stem not in wanted.get(s, set())}
    return stale


def linked_names(manifest: dict[str, Any]) -> dict[str, list[tuple[str, str | None]]]:
    """A document's confirmed names per role as `(display name, Authors filename or None)`: a
    filename only for a ticked name on a confirmed, default-visible document (slice-4 grill Q2), so
    its Source page links exactly the pages that list it back."""
    prov = manifest.get("provenance") or {}
    details = manifests.confirmed_details(prov)
    visible = (manifest.get("status") or "active") in RETENTION_DEFAULT_STATUSES

    def entry(e: dict[str, Any]) -> tuple[str, str | None]:
        ident = manifests.name_identity(e["name"])
        return (e["name"], filename_for(ident) if visible and e.get("show_page") and ident
                else None)
    return {"author": [entry(a) for a in details["authors"]],
            "publisher": [entry(details["publisher"])] if details["publisher"] else []}


def _source_title(wiki_dir: Path, sid: str) -> str:
    try:
        match = _TITLE_RE.search((Path(wiki_dir) / "Sources" / f"{sid}.md")
                                 .read_text(encoding="utf-8", errors="replace"))
    except OSError:
        match = None
    return re.sub(r"\\(.)", r"\1", match.group(1)) if match else sid


def inventory(root: Path, *, wiki_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every Authors page the confirmed provenance calls for, in a deterministic order:
    `{identity, filename, title, aliases, roles: {role: [{source_id, title}]}}`."""
    root = Path(root)
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    manifests_dir = root / "raw" / "manifests"
    # `valid_manifests` skips an unparseable file silently; here that must stop the step, or a
    # manifest that can't be read would look like a removed reference and prune its pages.
    for path in sorted(manifests_dir.glob("*.json")) if manifests_dir.exists() else []:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InventoryError(f"manifest {path.name} can't be read ({type(exc).__name__}); "
                                 "Authors pages left as they are until it is fixed") from exc
    valid, skipped = manifests.valid_manifests(manifests_dir)
    if skipped:
        raise InventoryError(f"{len(skipped)} manifest(s) can't be read; Authors pages left as they "
                             "are until they are fixed")
    for m in valid:
        # Parseable JSON with a malformed provenance shape (e.g. `authors: "Ada"`) would silently
        # yield no names and prune their pages: refuse instead (slice-4 review round 1).
        prov = m.get("provenance")
        problem = manifests.provenance_problem(prov if isinstance(prov, dict) else {}) \
            if prov is None or isinstance(prov, dict) else "provenance is not an object"
        if problem:
            raise InventoryError(f"manifest {m['source_id']}: {problem}; Authors pages left as they "
                                 "are until it is fixed")
    pages: dict[str, dict[str, Any]] = {}
    voted: set[tuple[str, str]] = set()             # (identity, source): one spelling vote each
    for m in valid:
        sid = m["source_id"]
        for role, names in linked_names(m).items():
            for name, filename in names:
                if filename is None:
                    continue
                ident = manifests.name_identity(name)
                page = pages.setdefault(ident, {"identity": ident, "filename": filename,
                                                "spellings": Counter(), "aliases": set(),
                                                "roles": {r: [] for r in ROLES}})
                page["aliases"].add(name)               # every spelling is an alias ...
                if (ident, sid) not in voted:           # ... but a document votes once
                    voted.add((ident, sid))
                    page["spellings"][name] += 1
                if all(d["source_id"] != sid for d in page["roles"][role]):
                    page["roles"][role].append({"source_id": sid,
                                                "title": _source_title(wiki_dir, sid)})
    out = []
    for page in pages.values():
        spellings = page.pop("spellings")
        top = max(spellings.values())
        page["title"] = min(n for n, c in spellings.items() if c == top)
        page["aliases"] = sorted(page["aliases"], key=lambda n: (n.casefold(), n))
        for docs in page["roles"].values():
            docs.sort(key=lambda d: (d["title"].casefold(), d["source_id"]))
        out.append(page)
    return sorted(out, key=lambda p: (p["title"].casefold(), p["filename"]))


def render_page(page: dict[str, Any]) -> str:
    """The generated page (slice-4 grill Q1): byte-stable, every name through safe quoting."""
    roles = [r for r in ROLES if page["roles"][r]]
    authored, published = len(page["roles"]["author"]), len(page["roles"]["publisher"])
    parts = [f"author of {authored} document{'s' if authored != 1 else ''}"] if authored else []
    if published:
        parts.append(f"publisher of {published} document{'s' if published != 1 else ''}")
    title = display_link_label(page["title"]) or page["filename"]
    lines = ["---", "type: author", f"author_id: {_q(page['filename'])}",
             f"title: {_q(page['title'])}",
             f"aliases: [{', '.join(_q(a) for a in page['aliases'])}]",
             f"name_identity: {_q(page['identity'])}",
             f"roles: [{', '.join(roles)}]", "status: active", f"generated_by: {GENERATOR}", "---",
             "", f"# {title}", "",
             f"> [!summary] {title}: {' and '.join(parts)} in this vault, from confirmed details.",
             ""]
    for role, heading in (("author", "Authored"), ("publisher", "Published")):
        docs = page["roles"][role]
        if not docs:
            continue
        lines += [f"## {heading}", ""]
        lines += [f"- [[Sources/{d['source_id']}|{display_link_label(d['title']) or d['source_id']}]]"
                  for d in docs]
        lines.append("")
    return "\n".join(lines)


def ownership(path: Path) -> str:
    """"owned" (generated and consistent), "invalid" (claims the marker but fails a check) or
    "handwritten" (no marker)."""
    fm = read_frontmatter(path)
    if fm is None or fm.get("generated_by") != GENERATOR:
        return "handwritten" if fm is not None or GENERATOR not in _head(path) else "invalid"
    ident = fm.get("name_identity")
    if (fm.get("type") == "author" and fm.get("author_id") == path.stem
            and isinstance(ident, str) and ident
            and path.stem in (filename_for(ident), legacy_filename_for(ident))):
        return "owned"
    return "invalid"


def _head(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return ""


def _write_atomically(path: Path, content: str) -> None:
    """A complete temporary file, flushed, then an atomic replace (slice-4 review round 1): a crash or
    a failed write never leaves a truncated page that a retry would mistake for a handwritten one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        _write_durably(tmp, content.encode("utf-8"))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    _sync_dir(path.parent)


UNAVAILABLE = "authors_page_unavailable"


def unavailable_links(wiki_dir: Path, manifest: dict[str, Any]) -> list[str]:
    """The Authors filenames a document's Source page would link (every one, new or already on the
    page — :func:`linked_names`, exactly what the renderer writes) that are not verified pages
    (:func:`link_target_ok`). Empty means the Source page may be written (ADR-0071 review round 2)."""
    missing = {filename for role in linked_names(manifest).values() for name, filename in role
               if filename is not None
               and not link_target_ok(wiki_dir, filename, manifests.name_identity(name))}
    return sorted(missing)


def link_target_ok(wiki_dir: Path, filename: str, identity: str) -> bool:
    """Whether `Authors/<filename>.md` is a page a Source link may point at: present, generated and
    consistent (:func:`ownership`), and written for this name identity — read from disk, so it holds
    whatever the inventory or the last write did (ADR-0071 review round 1)."""
    path = Path(wiki_dir) / DIR / f"{filename}.md"
    if not path.is_file() or ownership(path) != "owned":
        return False
    return (read_frontmatter(path) or {}).get("name_identity") == identity


def publish(root: Path, *, wiki_dir: Path | None = None, prune: bool = True) -> dict[str, Any]:
    """Render and prune `wiki/Authors/` from the inventory (the caller holds the write lock).

    Writes only changed pages; removes only owned pages the inventory no longer has. A page whose
    file is handwritten, invalid, or owned by another identity (a hash collision) is a conflict:
    left untouched and reported. A page whose write fails is reported in `write_failed` and the
    others still go ahead. ADR-0071: an owned page the inventory no longer has is removed only when
    no Source page links it any more; a still-linked one is kept and reported in `still_linked`, and
    when the Source pages can't all be read (`link_scan_failed`, the reason) nothing is removed.
    The caller keeps the publication pending on any of these. `prune=False` only writes
    (publication runs that first, before the Source pages render, so a new link never points at a
    page not yet written). Raises :class:`InventoryError` (before any write) when the manifests
    can't be read. Returns `{pages, written, removed, conflicts, write_failed, still_linked,
    link_scan_failed}`."""
    root = Path(root)
    wiki_dir = Path(wiki_dir) if wiki_dir else root / "wiki"
    pages = inventory(root, wiki_dir=wiki_dir)
    folder = wiki_dir / DIR
    written, removed, conflicts, write_failed = [], [], [], []
    wanted = {p["filename"] for p in pages}
    for page in pages:
        path = folder / f"{page['filename']}.md"
        if path.exists():
            state = ownership(path)
            same = (read_frontmatter(path) or {}).get("name_identity") == page["identity"]
            if state != "owned" or not same:
                conflicts.append({"file": f"{DIR}/{path.name}", "state": state if state != "owned"
                                  else "another_identity", "name": page["title"]})
                continue
        content = render_page(page)
        try:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                _write_atomically(path, content)
                written.append(page["filename"])
        except (OSError, UnicodeDecodeError) as exc:
            write_failed.append({"file": f"{DIR}/{path.name}", "error": type(exc).__name__})
    still_linked: list[dict[str, Any]] = []
    link_scan_failed = None
    if prune:
        try:
            linked = source_links(wiki_dir)
        except LinkScanError as exc:
            link_scan_failed = str(exc)
    if prune and link_scan_failed is None:
        for path in sorted(folder.glob("*.md")) if folder.exists() else []:
            if path.stem not in wanted and ownership(path) == "owned":
                if linked.get(path.stem):
                    still_linked.append({"file": f"{DIR}/{path.name}",
                                         "sources": sorted(linked[path.stem])})
                    continue
                path.unlink()
                removed.append(path.stem)
    return {"pages": len(pages), "written": written, "removed": removed, "conflicts": conflicts,
            "write_failed": write_failed, "still_linked": still_linked,
            "link_scan_failed": link_scan_failed}
