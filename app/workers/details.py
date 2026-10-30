#!/usr/bin/env python3
"""The document screen's Details section (ADR-0066 part 3 slice 3).

A person confirms or corrects a document's title, authors and publisher. This module builds what the
form displays, the **baseline token** that proves a Save still matches it, and parses, compares and
validates a submission. The commit itself (the write lock, the tag rows, publication) is the screen
action's; the write is `manifests.confirm_details` / `clear_details`.

- **Prefill:** the confirmed Details when there are any; otherwise Enrich's fresh suggestions;
  otherwise the fallback title (filtered PDF title or filename) with no authors or publisher. A name
  that already has an Authors page is pre-ticked "show page" here, at prefill only.
- **Baseline token** (slice-3 grill Q1): a digest of what the form displayed — the lifecycle status,
  this document's tag-ledger state, its confirmed provenance and the displayed Details values and
  suggestions (not only Enrich's input fingerprint: a forced Enrich run can return other values
  under the same fingerprint).
- **Touched** is judged against the displayed values the form carries (`details_shown`), never
  values loaded at POST time; untouched Details are never validated, so they can't obstruct tagging.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.backend import manifests
from app.workers import titles

EMPTY_AUTHOR_ROWS = 3
MAX_AUTHOR_ROWS = 60


def _author_rows(names: list[str], paged: set[str]) -> list[dict[str, Any]]:
    return [{"name": n, "show_page": manifests.name_identity(n) in paged} for n in names]


def vault_names(manifests_dir: Path) -> tuple[list[str], set[str]]:
    """The confirmed names in the vault (for the form's name suggestions) and the identities with an
    Authors page (a confirmed `show_page` anywhere)."""
    names: dict[str, str] = {}
    paged: set[str] = set()
    for m in manifests.valid_manifests(manifests_dir)[0]:
        details = manifests.confirmed_details(m.get("provenance") or {})
        entries = list(details["authors"]) + ([details["publisher"]] if details["publisher"] else [])
        for e in entries:
            ident = manifests.name_identity(e["name"])
            names.setdefault(ident, e["name"])
            if e.get("show_page"):
                paged.add(ident)
    return sorted(names.values(), key=str.casefold), paged


def view(manifest: dict[str, Any], *, markdown: str, enrichment_dir: Path, logs_dir: Path,
         paged: set[str]) -> dict[str, Any]:
    """What the Details section shows for one document."""
    prov = manifest.get("provenance") or {}
    confirmed = manifests.confirmed_details(prov)
    is_confirmed = bool(prov.get("confirmed_at") or confirmed["title"] or confirmed["authors"]
                        or confirmed["publisher"])
    enrichment = titles.fresh_enrichment(manifest, markdown, enrichment_dir=enrichment_dir,
                                         logs_dir=logs_dir)
    hint = titles.read_metadata_hint(Path(logs_dir), manifest["source_id"])
    fallback_title = titles.prompt_title(manifest, enrichment=None, hint=hint)
    filename = manifest.get("original_filename") or manifest["source_id"]
    fallback = "pdf" if titles.filter_pdf_title(hint.get("title"), filename) else "filename"
    suggested = None
    if enrichment:
        pub = enrichment.get("publisher")
        suggested = {"title": enrichment.get("title") or fallback_title,
                     "authors": _author_rows(list(enrichment.get("authors") or []), paged),
                     "publisher": ({"name": pub, "show_page": manifests.name_identity(pub) in paged}
                                   if pub else None)}
    if is_confirmed:
        prefill = {"title": confirmed["title"] or (suggested or {}).get("title") or fallback_title,
                   "authors": confirmed["authors"], "publisher": confirmed["publisher"]}
    elif suggested is not None:
        prefill = suggested
    else:
        prefill = {"title": fallback_title, "authors": [], "publisher": None}
    differs = bool(is_confirmed and suggested and _comparable(suggested) != _comparable(prefill))
    return {"confirmed": is_confirmed, "confirmed_at": prov.get("confirmed_at"),
            "prefill": normalize(prefill), "suggested": normalize(suggested) if suggested else None,
            "suggestion_differs": differs,
            "source": "confirmed" if is_confirmed else ("enrich" if suggested else fallback)}


def _comparable(values: dict[str, Any]) -> tuple:
    return (manifests.name_identity(values.get("title")),
            tuple(manifests.name_identity(a["name"]) for a in values.get("authors") or []),
            manifests.name_identity((values.get("publisher") or {}).get("name")))


def normalize(values: dict[str, Any]) -> dict[str, Any]:
    """Cleaned Details values, comparable between what was shown and what was submitted."""
    clean = manifests._clean
    pub = values.get("publisher")
    return {"title": clean(values.get("title"), 300),
            "authors": [{"name": clean(a.get("name")), "show_page": bool(a.get("show_page"))}
                        for a in values.get("authors") or [] if clean(a.get("name"))],
            "publisher": ({"name": clean(pub.get("name")), "show_page": bool(pub.get("show_page"))}
                          if isinstance(pub, dict) and clean(pub.get("name")) else None)}


def baseline_token(manifest: dict[str, Any], ledger: dict[str, Any], details: dict[str, Any]) -> str:
    """The digest of what the form displayed (slice-3 grill Q1)."""
    data = {"status": manifest.get("status") or "active", "ledger": ledger,
            "provenance": manifest.get("provenance") or {},
            "shown": details["prefill"], "suggested": details["suggested"]}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
                          .encode("utf-8")).hexdigest()[:24]


def parse_form(form: dict[str, Any]) -> dict[str, Any]:
    """The Details fields of a submission: blank author rows dropped, the rest ordered by their order
    field with row order as the tie-breaker. A row's hidden marker makes an absent checkbox read as
    unchecked."""
    rows = []
    for key in form:
        if str(key).startswith("author_row_"):
            idx = str(key)[len("author_row_"):]
            if not idx.isdigit():
                continue
            i = int(idx)
            order = str(form.get(f"author_order_{i}") or "").strip()
            rows.append((int(order) if order.lstrip("-").isdigit() else i, i,
                         str(form.get(f"author_name_{i}") or ""),
                         bool(form.get(f"author_show_{i}"))))
    rows.sort(key=lambda r: (r[0], r[1]))
    publisher = None
    if str(form.get("publisher_name") or "").strip():
        publisher = {"name": str(form.get("publisher_name")),
                     "show_page": bool(form.get("publisher_show"))}
    return {"title": str(form.get("details_title") or ""),
            "authors": [{"name": n, "show_page": s} for _o, _i, n, s in rows if n.strip()],
            "publisher": publisher}


def shown_values(form: dict[str, Any]) -> dict[str, Any] | None:
    """The displayed baseline the form carried, or None when absent or malformed."""
    try:
        shown = json.loads(str(form.get("details_shown") or ""))
    except ValueError:
        return None
    return normalize(shown) if isinstance(shown, dict) else None


def touched(form: dict[str, Any]) -> bool:
    """Whether a submission touches Details: the tick is set, or a field differs from the displayed
    baseline. A form with no Details fields at all (an older page, a JSON client) never does."""
    if form.get("details_confirm"):
        return True
    if "details_title" not in form:
        return False
    shown = shown_values(form)
    return shown is None or normalize(parse_form(form)) != shown


def validate(submitted: dict[str, Any]) -> list[str]:
    """Form-level errors for touched Details: an empty title, a duplicate normalized author, and a
    nonblank author or publisher value that normalizes to nothing (control characters only). Only a
    blank row is an intentional removal; it is dropped before this check (`parse_form`)."""
    errors = []
    for a in submitted.get("authors") or []:
        if not manifests._clean(a.get("name")):
            errors.append("An author name isn't usable text (only control characters).")
    pub = submitted.get("publisher")
    if pub and not manifests._clean(pub.get("name")):
        errors.append("The publisher name isn't usable text (only control characters).")
    values = normalize(submitted)
    if not values["title"]:
        errors.append("The title can't be empty.")
    seen: set[str] = set()
    for a in values["authors"]:
        ident = manifests.name_identity(a["name"])
        if ident in seen:
            errors.append(f"“{a['name']}” is listed twice.")
        seen.add(ident)
    return errors
