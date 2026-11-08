#!/usr/bin/env python3
"""Phase 6 slice 6-4: hand-rolled server-rendered HTML for the Human Review UI (ADR-0035 A8).

Pure string renderers — no filesystem mutation, no FastAPI coupling beyond returning strings. The
HTML layer is **never authority**: `main.py`'s thin `/ui/*` routes call the same read-model / decision
/ apply primitives the JSON API uses and pass their results here to render.

**Non-negotiable safety invariant:** review/read-model content is untrusted (CLAUDE.md rule 2), so
*every* dynamic value is HTML-escaped through :func:`_h` — there is no "trusted HTML" escape hatch for
review content. Structured values (the per-type `details` blob, lists) render through :func:`_render_value`
which recurses and escapes every leaf, so nested dict/list markup can never reach the page raw.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
from typing import Any

from app.backend import taxonomy
from app.backend.review_read import reopen_block_reason

# Corporate palette (TIMIA visual guidelines): white background, Negro text, Tierra/Gris secondary
# text, accents Rojo (brand: titles and buttons), Cielo (links), Mar (focus), Amarillo (notices),
# Verde (success notices). Arial, with metric-compatible fallbacks where it isn't installed.
# Contrast (WCAG 2.1, measured on the real backgrounds): Gris #7C878E is 3.68:1 on white, enough for
# field borders (3:1) but not for body text (4.5:1), so muted text uses the darker variant #687177
# (4.98:1 on white, 4.6:1 on the light-grey --suave).
_STYLE = (
    "<style>"
    ":root{--negro:#2D2926;--tierra:#6F5D58;--gris:#7C878E;--rojo:#A50034;--rojo-oscuro:#82002A;"
    "--mar:#00A7B5;--amarillo:#FFB81C;--verde:#257226;--cielo:#315AB5;--gris-texto:#687177;"
    "--linea:#D9DDE0;--suave:#F5F6F7}"
    "html{background:#FFFFFF}"
    "body{font-family:Arial,'Liberation Sans',Helvetica,'Helvetica Neue',sans-serif;font-size:15px;"
    "line-height:1.45;margin:2rem auto;padding:0 1.5rem;max-width:80rem;color:var(--negro);"
    "background:#FFFFFF}"
    "h1,h2,h3{font-weight:bold;line-height:1.25}"
    "h1{font-size:1.6rem;color:var(--negro);border-bottom:3px solid var(--rojo);"
    "padding-bottom:.35rem;margin:1.2rem 0 1rem}"
    "h2{font-size:1.2rem;color:var(--tierra);margin:1.6rem 0 .6rem}"
    "h3{font-size:1.05rem;color:var(--tierra)}"
    "a{color:var(--cielo);text-decoration:none}"
    "a:hover{text-decoration:underline}"
    "nav{padding:.5rem 0 .6rem;border-bottom:1px solid var(--linea);margin-bottom:.8rem}"
    "nav a{margin-right:1rem;font-weight:bold}"
    "table{border-collapse:collapse;margin:.6rem 0}"
    "th,td{border:1px solid var(--linea);padding:.35rem .65rem;text-align:left;vertical-align:top}"
    "th{background:var(--suave);font-weight:bold}"
    "table.kv th{white-space:nowrap}"
    ".banner{background:#FFF7E3;border:1px solid #F3D88F;border-left:4px solid var(--amarillo);"
    "border-radius:6px;padding:.55rem .85rem;margin:.6rem 0}"
    ".err{background:#FBEBEF;border:1px solid #E7B3C2;border-left:4px solid var(--rojo);"
    "border-radius:6px;padding:.55rem .85rem;margin:.6rem 0}"
    ".ok{background:#EDF5EC;border:1px solid #A9CDA8;border-left:4px solid var(--verde);"
    "border-radius:6px;padding:.55rem .85rem;margin:.6rem 0}"
    ".muted{color:var(--gris-texto)}"
    ".help{color:var(--tierra);font-size:.9rem}"
    ".badge{border:1px solid var(--tierra);border-radius:.6rem;padding:0 .4rem;font-size:.8rem;"
    "color:var(--tierra);white-space:nowrap}"
    "form.decide{display:inline;margin-right:.4rem}"
    # ADR-0070 B3: three button tiers. Secondary is the plain default; `primary` (solid Rojo, at
    # most one per form) and `danger` (Rojo outline, a label naming the loss, never the Enter
    # default) are explicit. WCAG 2.1 AA on every background (text 4.5:1, borders 3:1).
    "button{font:inherit;font-weight:bold;color:var(--negro);background:#FFFFFF;"
    "border:1px solid var(--gris);border-radius:6px;padding:.36rem .88rem;"
    "margin:.15rem .2rem .15rem 0;cursor:pointer}"
    "button:hover{border-color:var(--negro);background:#F4F4F4}"
    "button.primary{color:#FFFFFF;background:var(--rojo);border-color:var(--rojo)}"
    "button.primary:hover{background:var(--rojo-oscuro);border-color:var(--rojo-oscuro)}"
    "button.danger{color:var(--rojo);background:#FFFFFF;border-color:var(--rojo)}"
    "button.danger:hover{color:var(--rojo-oscuro);border-color:var(--rojo-oscuro);"
    "background:#FBEBEF}"
    "button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,"
    "a:focus-visible{outline:2px solid var(--mar);outline-offset:2px}"
    "input,select,textarea{font:inherit;color:var(--negro);border:1px solid var(--gris);"
    "border-radius:4px;padding:.22rem .4rem;background:#FFFFFF}"
    "input[type=radio],input[type=checkbox]{accent-color:var(--rojo)}"
    # UAT 2026-11-02: one decision option (radio + label) never wraps inside itself, and a row's
    # options stay on one line in the wider layout.
    "label.opt{white-space:nowrap;margin-right:.35rem}"
    "details{border:1px solid var(--linea);border-radius:6px;padding:.4rem .8rem;margin:.6rem 0}"
    "summary{cursor:pointer;font-weight:bold;color:var(--tierra)}"
    "code{background:var(--suave);border-radius:3px;padding:0 .25rem}"
    # The document screen's Enter-key default (first submit button of its form): off-screen, not
    # display:none, so every browser still uses it for implicit submission.
    ".ks-enter{position:absolute;left:-10000px;width:1px;height:1px;overflow:hidden}"
    "</style>"
)


def _h(value: Any) -> str:
    """The one mandatory escape: every dynamic value passes through here (quote=True)."""
    return html.escape(str(value), quote=True)


_ADDABLE_FOR_REUSE = frozenset({"candidate", "active", "unused", "deprecated_candidate",
                                "stale_candidate"})


def _tag_named(info: dict[str, Any], title_key: str, id_key: str) -> str:
    title = info.get(title_key)
    return f"“{title}”" if title else f"another tag ({info.get(id_key) or 'unknown'})"


def refusal_text(reason: str, info: dict[str, Any] | None = None) -> str:
    """Plain wording for a typed refusal (ADR-0071 decision 5: no raw codes on screen). `info` is the
    outcome or its `problem` (names, ids, paths); unknown reasons fall back to the code itself."""
    info = info or {}
    problem = info.get("problem") if isinstance(info.get("problem"), dict) else {}
    merged = {**problem, **{k: v for k, v in info.items() if k != "problem"}}
    page = f"Items/{merged.get('slug')}.md" if merged.get("slug") else "its page"
    if reason == "name_taken":
        status = merged.get("conflict_status")
        status_txt = f" ({status})" if status and status != "active" else ""
        return (f"a tag named {_tag_named(merged, 'conflict_title', 'conflict_node_id')}"
                f"{status_txt} already exists. Use that tag, or choose a different name"
                + (" (it is hidden or retired — restore it first, or choose a different name)"
                   if status and status not in _ADDABLE_FOR_REUSE else "") + ".")
    if reason == "title_authority_unreadable":
        return (f"the title of tag {merged.get('node_id')} ({page}) can't be read, so a duplicate "
                "name can't be ruled out. Nothing was changed; run scripts/validate_all.py and "
                "repair that page first.")
    if reason in ("slug_collision", "amended_slug_collision", "page_owned_by_other_item",
                  "slug_taken"):
        return (f"the page name {page} already belongs to "
                f"{_tag_named(merged, 'existing_title', 'existing_node_id')}"
                ". Nothing was changed; rename the tag, or merge the two tags.")
    if reason == "page_owner_unknown":
        return (f"{page} exists but doesn't say which tag owns it, so it is left alone. Nothing was "
                "changed; repair or remove that file first.")
    return {"invalid_title": "give the tag a title",
            "invalid_item_type": "choose a type",
            "unknown_item": "that tag no longer exists — reload the page",
            "node_not_addable": "that tag is hidden or retired and can't be added",
            "identity_operation_in_flight": "a merge or split on this tag is unfinished — run "
                                            "Apply first",
            "ledger_corrupt": "this document's tag ledger is damaged — restore it from backup",
            }.get(reason, reason)


def _merge_action(info: dict[str, Any] | None) -> str:
    """ADR-0071 decision 3: under a rename refusal, the merge preview link when the two tags can be
    merged (the preview writes nothing), else what the merge needs first."""
    info = info or {}
    preview = info.get("merge_preview")
    if isinstance(preview, dict) and preview.get("survivor") and preview.get("absorbed"):
        query = urllib.parse.urlencode({"survivor": preview["survivor"],
                                        "absorbed": preview["absorbed"]})
        return (f" <a href='/ui/reviews/merge/preview?{_h(query)}'>Preview merging this tag into "
                f"“{_h(preview.get('survivor_title') or preview['survivor'])}”…</a>")
    if info.get("merge_advice"):
        return f" <span class='help'>(A merge isn't possible yet: {_h(info['merge_advice'])}.)</span>"
    return ""


def _advisories(advisories: list[dict[str, Any]] | None) -> str:
    """ADR-0071 decision 3, level 2: similar existing tags reported with a recorded action — kept
    apart from refusals; the action already happened, so this is information only."""
    parts = []
    for adv in advisories or []:
        for m in adv.get("matches") or []:
            how = (f" (matched its alias “{_h(m['matched'])}”)"
                   if m.get("matched") and m.get("matched") != m.get("title") else "")
            parts.append(f"<div class='help'>Note: a similar tag already exists — "
                         f"“{_h(m.get('title'))}” ({_h(m.get('status'))}){how}. If they are the "
                         "same, merge them.</div>")
    return "".join(parts)


def _add_choice(sid: str, choice: dict[str, Any] | None) -> str:
    """ADR-0071 decision 3: the choices shown under Add tag when the new name matches an existing tag
    — Use existing (each match) and, for a similar name only, Add separately. Both submit the whole
    screen form, so the other unsaved choices are kept, and the server checks again."""
    if not choice:
        return ""
    f = "form='ks-source-form'"
    rows = []
    for m in choice.get("matches") or []:
        how = (f" (matched its alias “{_h(m['matched'])}”)"
               if m.get("matched") and m.get("matched") != m.get("title") else "")
        use = (f"<button type='submit' {f} formaction='/ui/reviews/sources/{sid}/add' "
               f"name='add_use' value='{_h(m['node_id'])}'>Use existing “{_h(m.get('title'))}”"
               "</button>" if m.get("status") in _ADDABLE_FOR_REUSE else
               "<em>(hidden or retired — can't be reused here)</em>")
        rows.append(f"<li>“{_h(m.get('title'))}” — {_h(m.get('item_type'))}, "
                    f"{_h(m.get('status'))}{how} {use}</li>")
    separately = ""
    if choice.get("kind") == "similar":
        ids = ",".join(m["node_id"] for m in choice.get("matches") or [])
        separately = (f"<p><button type='submit' {f} formaction='/ui/reviews/sources/{sid}/add' "
                      f"name='add_separately' value='{_h(ids)}'>Add “{_h(choice.get('title'))}” "
                      "separately</button> <span class='help'>(a different tag with a similar "
                      "name)</span></p>")
        intro = (f"<p><strong>A similar tag already exists.</strong> “{_h(choice.get('title'))}” "
                 "wasn't added yet. Use the existing tag, or add yours as a separate tag:</p>")
    else:
        intro = ("<p><strong>A tag with this name already exists.</strong> Use it instead:</p>")
    return (f"<div class='banner'>{intro}<ul>{''.join(rows)}</ul>{separately}</div>")


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<title>{_h(title)}</title>{_STYLE}</head><body>{body}</body></html>")


def _render_value(value: Any) -> str:
    """Recursively render a structured value, escaping every leaf (proves nested markup is safe)."""
    if isinstance(value, dict):
        if not value:
            return "<em>(empty)</em>"
        rows = "".join(
            f"<tr><th>{_h(k)}</th><td>{_render_value(v)}</td></tr>" for k, v in value.items())
        return f"<table class='kv'>{rows}</table>"
    if isinstance(value, list):
        if not value:
            return "<em>(none)</em>"
        return "<ul>" + "".join(f"<li>{_render_value(v)}</li>" for v in value) + "</ul>"
    return _h(value)


# The publication queue's state for the banner every review screen shows (ADR-0068 decision 4).
# The app sets it (`set_publication_status`); unset (a bare renderer in tests) shows no banner.
_publication_status: Any = None


def set_publication_status(provider: Any) -> None:
    """Register a zero-argument callable returning `publication_queue.status(...)`."""
    global _publication_status
    _publication_status = provider


def _publication_banner() -> str:
    if _publication_status is None:
        return ""
    try:
        st = _publication_status()
    except Exception:  # noqa: BLE001 - a banner must never break a screen
        return ""
    if st.get("state") == "corrupt":
        return ("<p class='err'><strong>The publication queue is unreadable</strong> ("
                + _h(st.get("reason")) + "). Changes are refused until it is repaired; reading "
                "and backup still work. Repair keeps the damaged file and republishes every page "
                "from the saved decisions. <form class='decide' method='post' "
                "action='/ui/publication/repair'><button type='submit'>Repair publication queue"
                "</button></form></p>")
    if st.get("state") != "pending":
        return ""
    if st.get("only_runs"):                   # a bulk job's end refresh, still to come (decision 5)
        return ("<p class='banner'><strong>Search refresh pending.</strong> A "
                + _h(", ".join(st.get("runs") or [])) + " run saved documents; the index page and "
                "search update when it finishes. If no run is in progress (it stopped), retry. "
                "<form class='decide' method='post' action='/ui/publication/retry'><button "
                "type='submit'>Retry publishing</button></form></p>")
    steps = [f"{len(st.get(k) or [])} {label} page(s)"
             for k, label in (("claims", "claim"), ("syntheses", "synthesis"), ("items", "tag"),
                              ("sources", "Source")) if st.get(k)]
    steps += ["every page"] if st.get("full") else []
    steps += ["the index page"] if st.get("index") else []
    steps += ["search"] if st.get("keyword") else []
    return ("<p class='banner'><strong>Saved; publication incomplete.</strong> Decisions are "
            "saved, but " + _h(", ".join(steps) or "some pages") + " still show the old state. "
            "Don't repeat the decisions. <form class='decide' method='post' "
            "action='/ui/publication/retry'><button type='submit'>Retry publishing</button>"
            "</form></p>")


def _nav() -> str:
    return ("<nav>"
            "<a href='/ui/reviews'>Queue</a>"
            "<a href='/ui/reviews/sources'>Sources</a>"
            "<a href='/ui/reviews?status=approved'>Approved</a>"
            "<a href='/ui/reviews/apply'>Apply…</a>"
            "<a href='/ui/reviews/tags/unused'>Unused tags</a>"
            "<a href='/ui/reviews/tags/blocked'>Blocked tags</a>"
            "</nav>" + _publication_banner())


def render_publication_result(result: dict[str, Any], *, repaired: str | None = None) -> str:
    """The Retry publishing / Repair result (ADR-0068 decision 4)."""
    done = result.get("status") == "published"
    body = [_nav(), "<h1>Publication " + ("complete" if done else "still incomplete") + "</h1>"]
    if repaired:
        body.append(f"<p>The damaged queue was kept as <code>db/{_h(repaired)}</code>.</p>")
    body.append(_render_value({"Source pages rendered": len(result.get("sources_rendered") or []),
                               "index page rebuilt": result.get("index_rebuilt"),
                               "search refreshed": result.get("keyword_reindexed")}))
    if result.get("warnings"):
        body.append(f"<p class='banner'>warnings: {_render_value(result['warnings'])}</p>")
    from app.workers import publication
    if lines := publication.describe_failures(result):
        body.append("<p>Still pending:</p><ul>" + "".join(f"<li>{_h(x)}</li>" for x in lines)
                    + "</ul>")
    body.append("<p><a href='/ui/reviews'>Back to the queue</a></p>")
    return _page("Publication", "".join(body))


# --- queue -----------------------------------------------------------------


_STATUSES = ("pending", "deferred", "approved", "rejected")


# ADR-0070 B3: a destructive button is never the Enter-key default. A form whose first submit
# button would be destructive starts with this disabled, invisible one: implicit submission (Enter
# in a field) uses the form's FIRST submit button, and a disabled default submits nothing.
_ENTER_GUARD = ("<button type='submit' class='ks-enter' disabled tabindex='-1' aria-hidden='true'>"
                "Enter does nothing here</button>")
_ACTION_TIERS = {"approve": "primary", "reject": "danger"}


def competitors_html(comps: list[dict[str, Any]] | None) -> str:
    """ADR-0070 B4: the other type changes proposed for the same tag — target type, review status
    and source document — or "" when there are none."""
    if not comps:
        return ""
    parts = []
    for c in comps:
        state = str(c.get("status"))
        if state == "approved":
            state = ("approved, applied" if c.get("applied_at") else
                     "approved, verified in effect" if c.get("verified_at") else
                     "approved, not yet applied")
        src = c.get("source_title") or c.get("source_id")
        parts.append(f"<a href='/ui/reviews/{_h(c.get('review_id'))}'>"
                     f"<code>{_h(c.get('to_item_type'))}</code></a> ({_h(state)}"
                     + (f", from “{_h(src)}”" if src else "") + ")")
    return ("<div class='help'>Also proposed for this tag: " + "; ".join(parts) + "</div>")


def _guard_for(first_action: str) -> str:
    """The Enter guard, only when the form's first submit button would be destructive (elsewhere
    Enter keeps its existing behaviour)."""
    return _ENTER_GUARD if _ACTION_TIERS.get(first_action) == "danger" else ""


def _tier(action: str) -> str:
    """The class attribute for a review-decision button (approve primary, reject destructive)."""
    tier = _ACTION_TIERS.get(action)
    return f" class='{tier}'" if tier else ""


def local_time(value: Any) -> str:
    """ADR-0072 B2: an ISO timestamp in the runtime's detected local zone, each with the zone that
    applied to IT (so the two sides of a daylight-saving change show CET / CEST). Anything that isn't
    an ISO timestamp with a zone is shown as it is."""
    from datetime import datetime
    if not isinstance(value, str) or not value:
        return "" if value is None else str(value)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        return value
    local = dt.astimezone()
    zone = local.strftime("%Z") or local.strftime("UTC%z")
    return f"{local.strftime('%Y-%m-%d %H:%M')} {zone}"


# The review types' readable names (the flat queue groups by these).
_TYPE_LABELS = {
    "promote_candidate_node": "Promote tags", "merge_items": "Merge tags",
    "change_item_type": "Change tag types", "split_item": "Split tags",
    "propose_synthesis": "Syntheses", "resolve_contradiction": "Contradictions",
    "deprecate_wiki_page": "Deprecations", "mark_semantic_duplicate": "Duplicates",
    "archive_source": "Archive documents", "hide_content": "Hide documents",
    "unhide_content": "Unhide documents", "hide_semantic_page": "Hide pages",
    "unhide_semantic_page": "Unhide pages", "hide_claim": "Hide claims",
    "unhide_claim": "Unhide claims", "hide_synthesis": "Hide syntheses",
    "unhide_synthesis": "Unhide syntheses",
}


def type_label(review_type: Any) -> str:
    return _TYPE_LABELS.get(str(review_type), str(review_type or "other"))


# What a decision leaves behind, per the repository's actual rules (ADR-0072 decision 6).
def lasting_effect(review_type: Any, action: str) -> str:
    if action == "defer":
        return ("Stays in the queue as deferred; nothing changes, and you can decide it later.")
    effect = ("This exact proposal is not filed again while the rejection stands (reopen it on its "
              "review page to change your mind).")
    if review_type == "merge_items":
        effect += (" A rejected merge also blocks filing this merge, or the reverse merge, by hand "
                   "until it is reopened.")
    if review_type in ("resolve_contradiction", "propose_synthesis"):
        effect += " The next Apply carries out the rejection for this kind of review."
    return effect


def render_queue(data: dict[str, Any], *, status: str) -> str:
    filters = " ".join(
        (f"<strong>{_h(s)}</strong>" if s == status
         else f"<a href='/ui/reviews?status={_h(s)}'>{_h(s)}</a>")
        for s in _STATUSES)
    parts = [_nav(), f"<h1>Review queue — {_h(status)} ({_h(data['count'])})</h1>",
             f"<p>Filter: {filters}</p>"]

    errs = []
    if data.get("parse_errors"):
        errs.append(f"{_h(data['parse_errors'])} unreadable file(s)")
    if data.get("schema_errors"):
        errs.append(f"{_h(data['schema_errors'])} malformed item(s)")
    if errs:
        parts.append(f"<p class='banner'>Skipped: {_h(', '.join(errs))} (excluded from the list).</p>")

    if data.get("by_type"):
        by_type = ", ".join(f"{_h(t)}: {_h(n)}" for t, n in data["by_type"].items())
        parts.append(f"<p>By type — {by_type}</p>")

    if not data["items"]:
        parts.append("<p><em>No items.</em></p>")
    else:
        applied = data.get("applied")       # approved view only: {review_id: applied_state}
        subjects = data.get("subjects") or {}
        # ADR-0070 §5 / ADR-0072 decision 6: explicit selection (none ticked), current page only,
        # for the views whose reviews can still be rejected or deferred.
        bulk = status in ("pending", "deferred")
        rows: list[str] = []
        groups: dict[str, list[dict[str, Any]]] = {}
        for it in data["items"]:
            groups.setdefault(str(it.get("type")), []).append(it)
        for gtype in sorted(groups, key=lambda t: (type_label(t), t)):
            rows.append(f"<tr><th colspan='{7 if bulk or applied is not None else 6}'>"
                        f"{_h(type_label(gtype))} ({_h(len(groups[gtype]))})</th></tr>")
            rows.append(("<tr><th></th>" if bulk else "<tr>")
                        + "<th>what</th><th>review</th><th>status</th><th>priority</th>"
                        "<th>created</th>" + ("<th>applied</th>" if applied is not None else "")
                        + "</tr>")
            for it in groups[gtype]:
                rows.append(_queue_row(it, subjects, applied, bulk, data.get("competitors")))
        if bulk:
            parts.append("<form method='post' action='/ui/reviews/bulk/confirm'>" + _ENTER_GUARD)
        parts.append("<table>" + "".join(rows) + "</table>")
        if bulk:
            parts.append("<p><button type='submit' class='danger' name='bulk_action' "
                         "value='reject'>Reject selected…</button> <button type='submit' "
                         "name='bulk_action' value='defer'>Defer selected…</button> "
                         "<span class='help'>Opens a "
                         "confirmation page; nothing is recorded until you confirm there. Approval "
                         "stays one at a time on each review's page.</span></p></form>")
        if applied is not None:
            parts.append("<p>Approved items stay listed here after Apply. <em>applied</em> shows when "
                         "a merge, split or type change was carried out; <em>already in effect</em> "
                         "means a re-approved one needs nothing more; <em>cannot verify</em> means "
                         "the saved state is incomplete (a missing page, an unfinished operation) — "
                         "Apply re-checks it; — means the type leaves no per-item record (Apply "
                         "re-checks those every run).</p>")
    parts.append(_MERGE_FORM)
    return _page(f"Reviews — {status}", "".join(parts))


def _applied_cell(state: dict[str, Any]) -> str:
    """The approved view's "applied" cell (UAT round 8; review rounds 9–10)."""
    kind = state.get("state")
    if kind == "applied":
        return f"<td>applied {_h(local_time(state.get('at')))}</td>"
    if kind == "in_effect":
        return "<td>already in effect — nothing left to apply</td>"
    if kind == "cannot_verify":
        last = (f" (last applied {_h(local_time(state['at']))}, before this approval)"
                if state.get("at") else "")
        return f"<td>cannot verify — Apply re-checks it{last}</td>"
    if kind == "not_yet":
        return "<td><strong>not yet</strong> — run Apply</td>"
    return "<td>—</td>"


def _queue_row(it: dict[str, Any], subjects: dict[str, str], applied: dict[str, Any] | None,
               bulk: bool, competing: dict[str, Any] | None = None) -> str:
    """One flat-queue row: the selection box (bulk views), the readable subject linking to the
    review, its id, status, priority, local creation time and (approved view) the applied cell."""
    rid = _h(it.get("review_id"))
    cell = _applied_cell(applied.get(it.get("review_id")) or {}) if applied is not None else ""
    tick = (f"<td><input type='checkbox' name='rid' value='{rid}' aria-label='select {rid}'></td>"
            if bulk else "")
    what = subjects.get(str(it.get("review_id"))) or it.get("review_id")
    extra = competitors_html(competing.get(str(it.get("review_id")))) if competing else ""
    return (f"<tr>{tick}<td><a href='/ui/reviews/{rid}'>{_h(what)}</a>{extra}</td>"
            f"<td><code>{rid}</code></td><td>{_h(it.get('status'))}</td>"
            f"<td>{_h(it.get('priority'))}</td><td>{_h(local_time(it.get('created_at')))}</td>"
            f"{cell}</tr>")


_MERGE_FORM = (
        "<h2>Merge items</h2>"
        "<p>Merge one knowledge item into another: the absorbed item becomes a tombstone "
        "redirecting to the survivor and its edges move over — at apply, after the dry-run. Enter "
        "an <code>itm_</code> id or an Items slug for each side. You'll see what the merge does "
        "before confirming; confirming files it <strong>pre-approved</strong> (the filing is your "
        "approval). A rejected merge of the pair blocks (reopen it explicitly).</p>"
        "<form method='get' action='/ui/reviews/merge/preview'>"
        "<p><label>Survivor: <input type='text' name='survivor' size='30' required></label> "
        "<label>Absorbed: <input type='text' name='absorbed' size='30' required></label></p>"
        "<button type='submit'>Preview merge</button></form>")


# --- detail ----------------------------------------------------------------


# Top-level item keys already shown in the detail summary table — the "Stored Proposal" section adds
# only the rest (subject/proposal/context + any extra producer fields like `winner`).
_SUMMARY_ITEM_KEYS = frozenset({
    "review_id", "type", "status", "priority", "created_at", "decided_by", "decided_at",
    "decision_note", "subject", "proposal", "context"})


def _stored_proposal(item: dict[str, Any]) -> str:
    """The full stored payload the human must see before deciding (ADR-0035 decision 6), escaped.

    subject/proposal/context plus any extra top-level item keys (e.g. a supersede `winner`) — generic,
    so no per-type HTML and future producer fields surface automatically. All via _render_value."""
    stored: dict[str, Any] = {
        "subject": item.get("subject"), "proposal": item.get("proposal"),
        "context": item.get("context"),
    }
    extra = {k: v for k, v in item.items() if k not in _SUMMARY_ITEM_KEYS}
    if extra:
        stored["other_fields"] = extra
    return "<h2>Stored proposal</h2>" + _render_value(stored)


def _reopen_section(review_id: str, effect_status: str | None) -> str:
    # ADR-0045: a terminal decision can be reopened (back to pending, re-decidable) ONLY while its effect
    # is provably not live. The button shows only when the projector permits; otherwise the inline reason.
    rid = _h(review_id)
    block = reopen_block_reason(effect_status)
    if block is not None:
        return ("<h2>Reopen</h2>"
                f"<p>This decision can’t be reopened (<code>{_h(block)}</code>). Reopen is only for a "
                "decision whose effect is not yet applied; an applied effect must be reversed separately.</p>")
    return ("<h2>Reopen</h2>"
            "<p>Move this decision back to <em>pending</em> to re-decide. Allowed only while the effect "
            "is not yet applied (it is now). A reason is required and recorded.</p>"
            f"<form method='post' action='/ui/reviews/{rid}/reopen'>"
            "<p><label>Reason: <input type='text' name='reason' size='50' required></label></p>"
            "<button type='submit'>Reopen</button></form>")


_VERSION_PROBLEMS = {
    "synthesis_artifact_missing": "The synthesis text is missing.",
    "synthesis_artifact_unreadable": "The synthesis text can't be read.",
    "synthesis_artifact_invalid": "The synthesis text is malformed",
    "synthesis_digest_mismatch": "The synthesis text doesn't match its recorded version.",
}


def _version_usable(version: dict[str, Any] | None) -> bool:
    """A version a decision can be recorded for: valid, for the review's inputs, and not in the
    middle of an interrupted regeneration (ADR-0073 decision 4)."""
    return bool(version and version.get("ok") and version.get("for_review_inputs")
                and version.get("proposal_agrees") is not False)


def _version_section(item: dict[str, Any], version: dict[str, Any] | None) -> str:
    """ADR-0073 decision 4: the version itself — title, summary, confidence and the full synthesis
    text, escaped — so the human sees exactly what a decision is bound to."""
    if version is None:
        return ""
    if not version.get("ok"):
        problem = str(version.get("problem") or "")
        text = _VERSION_PROBLEMS.get(problem, "The synthesis text can't be used.")
        if version.get("field"):
            text += f" (field: {version['field']})"
        return ("<h2>Synthesis version</h2>"
                f"<p class='banner'>{_h(text)} Nothing can be decided or applied until "
                "scripts/generate_synthesis.py writes it again.</p>")
    rows = {"title": version.get("title"), "summary": version.get("summary"),
            "confidence": version.get("confidence"),
            "version": str(version.get("version_digest"))[:12]}
    notes = []
    if not version.get("for_review_inputs"):
        notes.append("This synthesis was regenerated from other evidence than this review's; the "
                     "text below is the current one, not the one this review proposed.")
    if version.get("proposal_agrees") is False:
        notes.append("An interrupted regeneration left this proposal and the synthesis text "
                     "disagreeing. Run scripts/generate_synthesis.py to finish it, then review again.")
    bound = item.get("decided_version_digest")
    if item.get("status") in ("approved", "rejected"):
        if bound is None:
            notes.append("This decision was recorded before decisions named a version, so Apply "
                         "won't carry it out until it is decided again for the version shown.")
        elif bound != version.get("version_digest"):
            notes.append("This decision was made for an earlier version of the text; Apply won't "
                         "carry it out on this one.")
    page = (item.get("proposal") or {}).get("synthesis_id")
    link = (f"<p class='help'>Synthesis page: <code>Synthesis/{_h(page)}.md</code></p>"
            if isinstance(page, str) and re.fullmatch(r"syn_[0-9a-f]{16}", page) else "")
    return ("<h2>Synthesis version</h2>"
            + "".join(f"<p class='banner'>{_h(n)}</p>" for n in notes)
            + _render_value(rows)
            + "<h3>Full text</h3>"
            + f"<div class='synthesis-text'><p>{_h(version.get('synthesis')).replace(chr(10), '<br>')}"
              "</p></div>"
            + link)


def _decide_again_section(review_id: str, item: dict[str, Any],
                          version: dict[str, Any] | None) -> str:
    """ADR-0073 decision 7: "Decide again for the version shown" — only for a legacy decision with
    no recorded version, on the current inputs, where reopen is refused because the effect is live."""
    if (item.get("type") != "propose_synthesis" or item.get("decided_version_digest") is not None
            or not _version_usable(version)):
        return ""
    rid = _h(review_id)
    buttons = "".join(f"<button type='submit'{_tier(a)} name='action' value='{a}'>{label}</button> "
                      for a, label in (("approve", "Approve this version"),
                                       ("reject", "Reject this version")))
    return ("<h2>Decide again for the version shown</h2>"
            "<p>The decision above names no version. Record it again for the text shown above; the "
            "previous decision is kept in the audit log. Nothing changes until Apply.</p>"
            f"<form method='post' action='/ui/reviews/{rid}/decide-again'>"
            f"<input type='hidden' name='version_digest' value='{_h(version['version_digest'])}'>"
            "<p><label>Note (optional): <input type='text' name='note' size='50'></label></p>"
            f"{buttons}</form>")


def _decision_section(review_id: str, item: dict[str, Any], *, invalid_subject: bool = False,
                      effect_status: str | None = None,
                      version: dict[str, Any] | None = None) -> str:
    # Terminal items (approved/rejected) are immutable — show the recorded decision, not forms that
    # the backend would 409. Forms stay for pending/deferred (deferred is non-terminal). A terminal item
    # also gets the ADR-0045 reopen affordance (gated on effect_status).
    status = item.get("status")
    if status in ("approved", "rejected"):
        meta = {"decision": status, "decided_by": item.get("decided_by"),
                "decided_at": item.get("decided_at"), "note": item.get("decision_note")}
        if item.get("winner"):  # ADR-0044: the recorded contradiction supersede winner
            meta["winner"] = item["winner"]
        if item.get("type") == "propose_synthesis":   # ADR-0073: the version it is bound to
            bound = item.get("decided_version_digest")
            meta["version"] = str(bound)[:12] if bound else "none recorded (decided before versions)"
        again = (_decide_again_section(review_id, item, version)
                 if reopen_block_reason(effect_status) is not None else "")
        return ("<h2>Decision (recorded)</h2>" + _render_value(meta)
                + "<p>Effects are applied via <a href='/ui/reviews/apply'>Apply</a>.</p>"
                + _reopen_section(review_id, effect_status) + again)
    rid = _h(review_id)
    if item.get("type") == "propose_synthesis":
        if not _version_usable(version):
            return ("<h2>Decision</h2><p>No decision can be recorded on this version: see "
                    "<em>Synthesis version</em> above.</p>")
        digest_field = (f"<input type='hidden' name='version_digest' "
                        f"value='{_h(version['version_digest'])}'>")
    else:
        digest_field = ""
    # ADR-0044: a resolve_contradiction offers winner selection — Acknowledge (both stand) /
    # Supersede A|B / Reject / Defer. Each button is one atomic action the decide handler translates.
    if item.get("type") == "resolve_contradiction" and not invalid_subject:
        subj = item.get("subject") or {}
        a, b = _h(str(subj.get("claim_a"))), _h(str(subj.get("claim_b")))
        actions = (("acknowledge", "Acknowledge (both claims stand)"),
                   ("supersede_a", f"Supersede: A wins → deprecate B ({a} beats {b})"),
                   ("supersede_b", f"Supersede: B wins → deprecate A ({b} beats {a})"),
                   ("reject", "Reject"), ("defer", "Defer"))
        buttons = _guard_for(actions[0][0]) + "".join(
            f"<button type='submit'{_tier(act)} name='action' value='{act}'>{label}</button> "
            for act, label in actions)
        return ("<h2>Decision</h2>"
                "<p>Decisions are recorded only; effects are applied later via "
                "<a href='/ui/reviews/apply'>Apply</a>.</p>"
                "<p><strong>⚠ Supersede is terminal:</strong> the chosen winner can't be changed after "
                "approval (the loser is deprecated). Review both sides above and choose carefully.</p>"
                f"<form method='post' action='/ui/reviews/{rid}/decide'>"
                "<p><label>Note (optional): <input type='text' name='note' size='50'></label></p>"
                f"{buttons}</form>")
    # A tampered/malformed subject can never apply — disable Approve (offer only Reject/Defer) and warn.
    actions = (("reject", "Reject"), ("defer", "Defer")) if invalid_subject else \
        (("approve", "Approve"), ("reject", "Reject"), ("defer", "Defer"))
    buttons = _guard_for(actions[0][0]) + "".join(
        f"<button type='submit'{_tier(a)} name='action' value='{a}'>{_h(label)}</button> "
        for a, label in actions)
    warn = ("<p><strong>⚠ Invalid subject:</strong> this item's source_id is not canonical "
            "(possible tampering). It cannot be applied; approval is disabled.</p>"
            if invalid_subject else "")
    return ("<h2>Decision</h2>"
            "<p>Decisions are recorded only; effects are applied later via "
            "<a href='/ui/reviews/apply'>Apply</a>.</p>"
            f"{warn}"
            f"<form method='post' action='/ui/reviews/{rid}/decide'>{digest_field}"
            "<p><label>Note (optional): <input type='text' name='note' size='50'></label></p>"
            f"{buttons}</form>")


def render_detail(result: dict[str, Any], *, review_id: str) -> str:
    item = result["item"]
    preview = result["preview"]
    ap = preview["apply"]

    summary_rows = {
        "type": item.get("type"),
        "status": item.get("status"),
        "priority": item.get("priority"),
        "created": local_time(item.get("created_at")),
        "decided_by": item.get("decided_by"),
        "decided": local_time(item.get("decided_at")),
    }
    preview_rows = {
        "summary": preview.get("summary"),
        "proposed_action": preview.get("proposed_action"),
        "current_status": preview.get("current_status"),
        "proposed_status": preview.get("proposed_status"),
        "affected_paths": preview.get("affected_paths"),
        "node_ids": preview.get("node_ids"),
        "warnings": preview.get("warnings"),
    }
    apply_rows = {
        "supported": ap.get("supported"),
        "executor": ap.get("executor"),
        "effect_status": ap.get("effect_status"),
        "effected": ap.get("effected"),
        "warnings": ap.get("warnings"),
    }

    body = [
        _nav(),
        (f"<h1>{_h(result['subject'])} <small>— {_h(type_label(item.get('type')))}</small></h1>"
         f"<p><code>{_h(review_id)}</code></p>" if result.get("subject")
         else f"<h1>Review {_h(review_id)}</h1>"),
        "<h2>Item</h2>", _render_value(summary_rows),
        (f"<h2>Competing type changes</h2>{competitors_html(result['competitors'])}"
         if result.get("competitors") else ""),
        "<h2>Preview</h2>", _render_value(preview_rows),
        "<h2>Apply state</h2>", _render_value(apply_rows),
        "<h2>Details</h2>", _render_value(preview.get("details") or {}),
        _version_section(item, preview.get("synthesis_version")),
        _stored_proposal(item),
        _decision_section(review_id, item, invalid_subject=bool(preview.get("invalid_subject")),
                          effect_status=ap.get("effect_status"),
                          version=preview.get("synthesis_version")),
    ]
    return _page(f"Review {review_id}", "".join(body))


# --- bulk reject / defer (ADR-0072 decision 6) -----------------------------------


_BULK_VERBS = {"reject": ("Reject", "rejected"), "defer": ("Defer", "deferred")}


def _version_blocker(version: dict[str, Any]) -> str:
    """Why no decision can be recorded for this synthesis version (`_version_usable` is False)."""
    if not version.get("ok"):
        text = _VERSION_PROBLEMS.get(str(version.get("problem") or ""),
                                     "The synthesis text can't be used.")
        return text + (f" (field: {version['field']})" if version.get("field") else "")
    if not version.get("for_review_inputs"):
        return "The synthesis was regenerated from other evidence than this review's."
    return ("An interrupted regeneration left the proposal and the synthesis text disagreeing; "
            "run scripts/generate_synthesis.py.")


def _version_brief(version: dict[str, Any]) -> str:
    """The version a bulk decision binds, shown on the confirmation row (ADR-0073 slice 1 review):
    title, summary, confidence and short version, the full text behind a disclosure. Escaped."""
    rows = {"title": version.get("title"), "summary": version.get("summary"),
            "confidence": version.get("confidence"),
            "version": str(version.get("version_digest"))[:12]}
    return ("<div class='synthesis-version'>" + _render_value(rows)
            + "<details><summary>Full text</summary><div class='synthesis-text'><p>"
            + _h(version.get("synthesis")).replace(chr(10), "<br>") + "</p></div></details></div>")


def render_bulk_confirm(action: str, rows: list[dict[str, Any]],
                        missing: list[str]) -> str:
    """Step 2: what the selected reviews are and what the decision leaves behind. Writes nothing; its
    form carries each review's id AND decision fingerprint, rechecked when the decisions are recorded.
    A synthesis row SHOWS the version it binds and carries that version's digest; a synthesis whose
    version can't be decided (ADR-0073 decision 4) is listed with why and left out of the form."""
    verb = _BULK_VERBS[action][0]
    blocked = [r for r in rows if r.get("type") == "propose_synthesis"
               and not _version_usable(r.get("version"))]
    rows = [r for r in rows if r not in blocked]
    parts = [_nav(), f"<h1>{_h(verb)} {_h(len(rows))} selected review"
             f"{'s' if len(rows) != 1 else ''}?</h1>",
             "<p>Nothing has been recorded yet. Each review is checked again when you confirm: one "
             "that changed since this page was shown is skipped.</p>"]
    if missing:
        parts.append("<p class='banner'>Not found (left out): "
                     + ", ".join(f"<code>{_h(m)}</code>" for m in missing) + "</p>")
    if blocked:
        parts.append("<div class='banner'><p>Can't be decided now (left out):</p><ul>"
                     + "".join(f"<li><a href='/ui/reviews/{_h(r['review_id'])}'>{_h(r['subject'])}"
                               f"</a> <code>{_h(r['review_id'])}</code> — "
                               f"{_h(_version_blocker(r.get('version') or {}))}</li>"
                               for r in blocked) + "</ul></div>")
    if not rows:
        parts.append("<p><em>Nothing selected.</em> <a href='/ui/reviews'>Back to the queue</a></p>")
        return _page(f"{verb} selected", "".join(parts))
    items = []
    for r in rows:
        version = r.get("version") if r.get("type") == "propose_synthesis" else None
        items.append(f"<li><a href='/ui/reviews/{_h(r['review_id'])}'>{_h(r['subject'])}</a> — "
                     f"{_h(type_label(r['type']))} <code>{_h(r['review_id'])}</code><br>"
                     f"<span class='help'>{_h(lasting_effect(r['type'], action))}</span>"
                     + (_version_brief(version) if version else "")
                     + f"<input type='hidden' name='rid' value='{_h(r['review_id'])}'>"
                     f"<input type='hidden' name='fp_{_h(r['review_id'])}' "
                     f"value='{_h(r['fingerprint'])}'>"
                     + (f"<input type='hidden' name='vd_{_h(r['review_id'])}' "
                        f"value='{_h(version['version_digest'])}'>" if version else "")
                     + "</li>")
    parts.append("<form method='post' action='/ui/reviews/bulk'>" + _guard_for(action)
                 + f"<input type='hidden' name='bulk_action' value='{_h(action)}'>"
                 "<ul>" + "".join(items) + "</ul>"
                 "<p><label>Note (applies to every decision): <input type='text' name='note' "
                 "size='50'></label></p>"
                 f"<p><button type='submit' class='{'danger' if action == 'reject' else 'primary'}'>"
                 f"{_h(verb)} {_h(len(rows))} review"
                 f"{'s' if len(rows) != 1 else ''}</button> <a href='/ui/reviews'>Cancel</a></p>"
                 "</form>")
    return _page(f"{verb} selected", "".join(parts))


def render_bulk_result(action: str, recorded: list[dict[str, Any]],
                       skipped: list[dict[str, Any]]) -> str:
    """Step 3: which decisions were recorded and which were skipped (with why). Never runs Apply."""
    verb, done = _BULK_VERBS[action]
    parts = [_nav(), f"<h1>{_h(verb)} selected — result</h1>",
             f"<p>{_h(len(recorded))} {_h(done)}, {_h(len(skipped))} skipped. Nothing was applied; "
             "a mistaken rejection is undone with <strong>Reopen</strong> on its review page.</p>"]

    def row(r: dict[str, Any], why: str = "") -> str:
        return (f"<li><a href='/ui/reviews/{_h(r['review_id'])}'>{_h(r.get('subject') or r['review_id'])}"
                f"</a> <code>{_h(r['review_id'])}</code>{f' — {_h(why)}' if why else ''}</li>")
    parts.append(f"<h2>{_h(done.capitalize())}</h2>"
                 + ("<ul>" + "".join(row(r) for r in recorded) + "</ul>" if recorded
                    else "<p><em>None.</em></p>"))
    if skipped:
        parts.append("<h2>Skipped</h2><ul>" + "".join(row(r, r["reason"]) for r in skipped)
                     + "</ul>")
    parts.append("<p><a href='/ui/reviews'>Back to the queue</a></p>")
    return _page(f"{verb} selected — result", "".join(parts))


# --- apply -----------------------------------------------------------------


def render_apply_confirm(scope: dict[str, Any]) -> str:
    body = [
        _nav(),
        "<h1>Apply approved reviews</h1>",
        "<p>This processes <strong>approved</strong> items deterministically. Already-effected "
        "items may be no-ops. Exact <em>applied / normalized / skipped</em> counts and validation "
        "results are reported after apply.</p>",
        "<h2>Approved items the apply step will process</h2>",
        _render_value(scope.get("executor_backed") or {}),
        "<h2>Record-only types (will not be applied in Phase 6)</h2>",
        _render_value(scope.get("record_only") or {}),
    ]
    errs = []
    if scope.get("parse_errors"):
        errs.append(f"{scope['parse_errors']} unreadable")
    if scope.get("schema_errors"):
        errs.append(f"{scope['schema_errors']} malformed")
    if errs:
        body.append(f"<p class='banner'>Approved-queue files skipped: {_h(', '.join(errs))}.</p>")
    # ADR-0068 decision 6: this page is read-only; the preview (which records the authorization
    # Apply needs) runs on an origin-guarded POST.
    if not scope.get("executor_backed") and not scope.get("record_only") and not errs:
        # UAT part 3 #10: say why there is nothing to do instead of offering a bare Preview.
        body.append("<p class='ok'><strong>Nothing is waiting to be applied.</strong> Only "
                    "<em>approved</em> review items need Apply. Rejected and deferred decisions "
                    "need no apply, and tag decisions on a document's screen take effect "
                    "immediately.</p>"
                    "<p class='help'>A preview is still possible, for example to finish a merge "
                    "or split that stopped part-way:</p>")
        body.append(_PREVIEW_FORM.replace(">Preview<", ">Preview anyway<"))
    else:
        body.append(_PREVIEW_FORM)
    return _page("Apply reviews", "".join(body))


_PREVIEW_FORM = ("<form method='post' action='/ui/reviews/apply/preview'>"
                 "<button type='submit'>Preview</button></form>")

# ADR-0068 decision 6: why an otherwise clean preview got no token, and what to do.
_WITHHELD = {
    "revision_changed": "The vault changed while the preview ran. Preview again.",
    "code_changed": "The code changed while the preview ran. Restart the API, then preview again.",
    "settings_changed": "The settings (<code>.env</code>) changed while the preview ran. Preview again.",
    "authorization_write_failed": ("The preview could not be saved as an authorization (check "
                                   "<code>db/</code> permissions and free space). Preview again."),
}


def _validators_line(ran: bool, passed: Any, failures: Any) -> str:
    """ADR-0070 §4: validators shown as passed, failed or not run."""
    if not ran:
        return "<p>Validators: <strong>not run</strong>.</p>"
    if passed:
        return "<p class='ok'>Validators: <strong>passed</strong>.</p>"
    n = len(failures or [])
    return (f"<p class='err'>Validators: <strong>failed</strong>"
            f"{f' ({n})' if n else ''} — see the technical details.</p>")


def render_apply_dry_run(scope: dict[str, Any], dry: dict[str, Any]) -> str:
    """Step-1 apply page (ADR-0040): the dry-run mutation preview. Apply is offered ONLY when the
    preview is clean (`status == "ok"`); a blocked/failed/validation-failed preview withholds it.
    ADR-0072 B2a: the readable sentences (from the sandbox run's per-review outcomes) come first; the
    technical diff stays on the page, folded."""
    from app.backend import apply_readable
    status = dry.get("status")
    ran = status in ("ok", "validation_failed")
    body = [
        _nav(),
        "<h1>Apply approved reviews — preview</h1>",
        "<p>Nothing has been changed yet: this is what Apply would do.</p>",
    ]
    if dry.get("reason"):
        body.append(f"<p class='banner'>The preview couldn't run: {_h(dry.get('reason'))} "
                    f"{_h(dry.get('error') or '')}</p>")
    if ran:
        groups = apply_readable.model(dry.get("handled") or [], dry.get("outcomes") or [],
                                      dry.get("not_appliable"))
        body.append(apply_readable.render(groups, advisories=dry.get("advisories"), preview=True))
    body.append(_validators_line(ran, (dry.get("validators") or {}).get("passed"),
                                 (dry.get("validators") or {}).get("failures")))
    if dry.get("warnings"):
        body.append(f"<p class='banner'>warnings: {_render_value(dry['warnings'])}</p>")

    token = dry.get("preview_token")
    if status == "ok" and token:
        body.append(
            "<form method='post' action='/ui/reviews/apply'>"
            f"<input type='hidden' name='preview_token' value='{_h(token)}'>"
            "<button type='submit' class='primary'>Apply now</button></form>")
    elif status == "ok":
        reason = dry.get("token_withheld") or ""
        body.append(f"<p class='err'>Apply is withheld: {_WITHHELD.get(reason, 'Preview again.')}"
                    "</p>" + _PREVIEW_FORM)
    else:
        body.append("<p class='err'>Apply is withheld: the preview is not clean "
                    f"(status <code>{_h(status)}</code>). Resolve the cause before applying.</p>")

    diff = dry.get("diff") or {}
    graph_diff = diff.get("graph") or {}
    tech = [f"<p>Dry-run status: <code>{_h(status)}</code>.</p>", "<h3>Graph changes</h3>",
            _render_value({
                "edges_added": graph_diff.get("edges_added"),
                "edges_removed": graph_diff.get("edges_removed"),
                "edges_status_changed": graph_diff.get("edges_status_changed"),
                "nodes_status_changed": graph_diff.get("nodes_status_changed"),
                "nodes_added": graph_diff.get("nodes_added"),
            }),
            "<h3>Manifest status changes</h3>", _render_value(diff.get("manifests") or []),
            "<h3>Review file moves</h3>", _render_value(diff.get("reviews") or []),
            "<h3>Wiki page diffs</h3>"]
    for page in diff.get("wiki") or []:
        tech.append(f"<h4>{_h(page.get('path'))}</h4><pre>{_h(page.get('unified_diff'))}</pre>")
    tech += ["<h3>Per-review outcomes</h3>", _render_value(dry.get("outcomes") or []),
             "<h3>Per-item provenance <small>(best-effort; the diff above is authoritative)</small>"
             "</h3>", _render_value(dry.get("items") or []),
             "<h3>Not appliable</h3>", _render_value(dry.get("not_appliable") or []),
             "<h3>Validators</h3>", _render_value(dry.get("validators") or {})]
    body.append("<details><summary>Technical details (the full diff)</summary>"
                + "".join(tech) + "</details>")
    return _page("Apply preview", "".join(body))


def render_apply_refused(reason: str, action: str) -> str:
    """Apply (or a preview) was refused before any change (ADR-0068 decision 6): name the reason
    and the specific action it needs."""
    headline = {
        "stale_preview": "The vault, code or settings changed since the preview.",
        "unknown_preview": "This preview is no longer valid (already used, or replaced).",
        "preview_required": "Apply needs a preview first.",
        "restart_required": "The API is running older code than what is on disk.",
        "authorization_store_unavailable": "The preview authorization could not be read or saved.",
    }.get(reason, "Apply was refused.")
    body = [_nav(), "<h1>Apply refused — nothing was changed</h1>",
            f"<p class='banner'><strong>{_h(headline)}</strong> <code>{_h(reason)}</code></p>",
            f"<p>Next: {_h(action)}.</p>"]
    if reason != "restart_required":
        body.append(_PREVIEW_FORM)
    return _page("Apply refused", "".join(body))


def render_apply_result(result: dict[str, Any]) -> str:
    """The Apply result (ADR-0072 B2a): readable sentences from the REAL run's per-review outcomes,
    the publication state kept apart from the recorded changes, the counts folded."""
    from app.backend import apply_readable
    s = result.get("summary") or {}
    pending = result.get("status") == "saved_publication_pending" or (
        (result.get("publication") or {}).get("status") == "saved_publication_pending")
    body = [_nav(), "<h1>Apply result</h1>"]
    if pending:
        body.append("<p class='banner'><strong>The changes below were recorded; publishing them is "
                    "unfinished.</strong> Some pages, the index or search still show the old state. "
                    "Use <strong>Retry publishing</strong>, and don't apply or decide again. "
                    "<form class='decide' method='post' action='/ui/publication/retry'><button "
                    "type='submit'>Retry publishing</button></form></p>")
    groups = apply_readable.model(result.get("handled") or [], result.get("outcomes") or [])
    body.append(apply_readable.render(groups, advisories=result.get("advisories"), preview=False))
    body.append(_validators_line(True, result.get("validators_ok"), result.get("failed_validators")))
    if result.get("warnings"):
        body.append(f"<p class='banner'>warnings: {_render_value(result['warnings'])}</p>")
    summary = {
        "syntheses": s.get("syntheses"), "promotions": s.get("promotions"),
        "contradictions": s.get("contradictions"), "deprecations": s.get("deprecations"),
        "duplicates": s.get("duplicates"), "archives": s.get("archives"),
        "hidden": s.get("hidden"), "semantic_hidden": s.get("semantic_hidden"),
        "unhidden": s.get("unhidden"), "semantic_unhidden": s.get("semantic_unhidden"),
        "claims_hidden": s.get("claims_hidden"), "claims_unhidden": s.get("claims_unhidden"),
        "synthesis_hidden": s.get("synthesis_hidden"),
        "synthesis_unhidden": s.get("synthesis_unhidden"),
        "synthesis_evidence": s.get("synthesis_evidence"),
        "merged": s.get("merged"), "retyped": s.get("retyped"), "split": s.get("split"),
        "pages_changed": s.get("pages_changed"), "index_rebuilt": s.get("index_rebuilt"),
        "unapplied": s.get("unapplied"),
    }
    # UAT part 3 #10: list only what happened, not ~20 rows of zero.
    shown = {k: v for k, v in summary.items() if v not in (None, 0, False, [], {}, "")}
    tech = [f"<p>Status: <code>{_h(result.get('status'))}</code>; validators_ok: "
            f"<code>{_h(result.get('validators_ok'))}</code>.</p>", "<h3>Counts</h3>",
            _render_value(shown) if shown else "<p><em>No counts.</em></p>",
            "<h3>Per-review outcomes</h3>", _render_value(result.get("outcomes") or [])]
    if result.get("failed_validators"):
        tech += ["<h3 class='err'>Failed validators</h3>", _render_value(result["failed_validators"])]
    body.append("<details><summary>Technical details</summary>" + "".join(tech) + "</details>")
    return _page("Apply result", "".join(body))


# --- per-source review flow (ADR-0058) --------------------------------------


def undecided_text(undecided: dict[str, Any] | None) -> str:
    """ADR-0070 §6: "3 tags, 1 type change still undecided" (split by kind), or "" when none."""
    u = undecided or {}
    parts = [f"{n} {word}{'' if n == 1 else 's'}" for n, word in (
        (int(u.get("tags") or 0), "tag"), (int(u.get("type_changes") or 0), "type change"),
        (int(u.get("retirements") or 0), "retirement")) if n]
    return f"{', '.join(parts)} still undecided" if parts else ""


_TAGGING_MARKERS = {"never_tagged": "not yet tagged", "history_unknown": "tagging history unknown"}


def _tagging_marker(tagging: dict[str, Any] | None) -> str:
    label = _TAGGING_MARKERS.get(str((tagging or {}).get("state")))
    return f" <span class='badge'>{_h(label)}</span>" if label else ""


def _next_link(sid: Any, label: str, *, none_text: str = "none remaining") -> str:
    if sid:
        return f"<a href='/ui/reviews/sources/{_h(sid)}'>{_h(label)}</a>"
    return f"{_h(label)}: <em>{_h(none_text)}</em>"


def _counts_cell(counts: dict[str, Any]) -> str:
    return _h(" / ".join(f"{k}:{counts.get(k, 0)}"
                         for k in ("remaining", "kept", "dropped", "deferred", "added")))


def render_sources_index(data: dict[str, Any]) -> str:
    """The entry page: every reviewable source in manifest ingest order with counts; sources with
    zero remaining rows render greyed-out as done. 'Start review' jumps to the first source with
    remaining rows; every row stays clickable (a lens, not a locked wizard)."""
    totals = data.get("totals") or {}
    parts = [_nav(), "<h1>Review by source</h1>",
             f"<p>{_h(totals.get('sources', 0))} sources · "
             f"{_h(totals.get('remaining_overall', 0))} rows remaining overall "
             "(tag suggestions + type changes + retirements). The "
             "<a href='/ui/reviews'>flat queue</a> remains canonical for everything else.</p>"]
    first = totals.get("first_remaining_source")
    if first:
        parts.append(f"<p><a href='/ui/reviews/sources/{_h(first)}'>"
                     "<strong>Start review</strong></a> (first source with remaining rows)</p>")
    else:
        parts.append("<p><em>Nothing remaining to decide by source.</em></p>")
    # ADR-0070 §6 / ADR-0072: documents with no tag decision yet, and those whose history is unknown
    unknown = int(totals.get("history_unknown") or 0)
    parts.append(f"<p>{_h(totals.get('not_yet_tagged', 0))} not yet tagged"
                 + (f" · {_h(unknown)} with tagging history unknown" if unknown else "")
                 + " · " + _next_link(totals.get("first_untagged_source"), "Next untagged document",
                                      none_text=("none known to be untagged" if unknown
                                                 else "none remaining"))
                 + "</p>")
    rows = ["<tr><th>#</th><th>document</th><th>file</th><th>ingested</th>"
            "<th>remaining / kept / dropped / deferred / added</th></tr>"]
    for i, s in enumerate(data.get("sources") or [], start=1):
        sid = _h(s.get("source_id"))
        remaining = (s.get("counts") or {}).get("remaining", 0)
        style = "" if remaining else " class='muted'"
        rows.append(
            f"<tr{style}><td>{i}</td>"
            f"<td><a href='/ui/reviews/sources/{sid}'>{_h(s.get('title') or s.get('source_id'))}"
            f"</a>{_tagging_marker(s.get('tagging'))}</td>"
            f"<td><a href='/raw/{sid}'>{_h(s.get('filename'))}</a></td>"
            f"<td>{_h(s.get('discovered_at'))}</td>"
            f"<td>{_counts_cell(s.get('counts') or {})}{'' if remaining else ' — done'}</td></tr>")
    parts.append("<table>" + "".join(rows) + "</table>")
    if data.get("global_remaining_by_type"):
        by_type = ", ".join(f"{_h(t)}: {_h(n)}"
                            for t, n in data["global_remaining_by_type"].items())
        parts.append(f"<p>Global review items remaining (flat queue): {by_type}</p>")
    return _page("Review by source", "".join(parts))


def _decision_radios(rid: str, *, preselect: str | None = None, chosen: str | None = None) -> str:
    """Per-row decision radios for review items (type changes, retirements). The explicit
    no-decision option is load-bearing in this JS-free UI: a radio group cannot be DEselected once
    clicked, so it is the only way to take back a misclick before submit (and unlike defer it
    records NOTHING — the row just stays pending)."""
    rid = _h(rid)
    default = chosen if chosen is not None else ("approve" if preselect == "approve" else "")
    return ("".join(
        f"<label class='opt'><input type='radio' name='decision_{rid}' value='{v}'"
        f"{' checked' if v == default else ''}> {label}</label> "
        for v, label in (("", "— leave pending"), ("approve", "approve"),
                         ("reject", "reject"), ("defer", "defer"))))


def _link_radios(nid: str, *, kept: bool, preselect: str | None = None,
                 ambiguous: list[dict[str, Any]] | None = None, chosen: str | None = None) -> str:
    """ADR-0066 per-link radios: a suggestion is kept, dropped (this document only) or blocked
    ("never suggest", vault-wide); a kept link can be dropped. "— leave" records nothing.

    An ambiguous suggestion (its name matches several existing tags, P3) offers **use X** per
    matching tag instead of a plain keep, plus **keep as new tag**; pre-select never picks for it."""
    nid = _h(nid)
    if kept:
        options = (("", "— leave kept"), ("drop", "drop from this document"))
    elif ambiguous:
        options = (("", "— leave undecided"),
                   *((f"use:{m['node_id']}",
                      f"use {m.get('title')} ({m.get('item_type')}, kept on {m.get('kept_count', 0)})")
                     for m in ambiguous),
                   ("keep", "keep as new tag"), ("drop", "drop"), ("block", "never suggest"))
    else:
        options = (("", "— leave undecided"), ("keep", "keep"), ("drop", "drop"),
                   ("block", "never suggest"))
    default = (chosen if chosen is not None
               else "keep" if (preselect == "keep" and not kept and not ambiguous) else "")
    return "".join(
        f"<label class='opt'><input type='radio' name='link_{nid}' value='{_h(v)}'"
        f"{' checked' if v == default else ''}> {_h(label)}</label> " for v, label in options)


def _amend_inputs(row: dict[str, Any], draft: dict[str, Any] | None = None) -> str:
    """Inline amendment fields for a keep that activates a CANDIDATE (ADR-0058 title/aliases/
    description + ADR-0059 item_type). The item_type select is REQUIRED to keep an unclassified
    suggestion (the keep blocks without it); otherwise it is an optional reclassification."""
    nid = _h(row.get("node_id"))
    options = ["<option value=''>keep type</option>"] + [
        f"<option value='{t}'>{t}</option>" for t in sorted(taxonomy.ITEM_TYPES)
        if t != taxonomy.UNCLASSIFIED]
    required = " <strong>type required to keep</strong>" if row.get("needs_type") else ""
    # ADR-0066 P5: the aliases field REPLACES the tag's aliases, prefilled with the current ones,
    # one per line (a comma-split field would split an alias that contains a comma).
    aliases = "\n".join(str(a) for a in row.get("aliases") or [])
    rows = max(2, min(6, len(row.get("aliases") or []) + 1))
    d = draft or {}
    raw = str(row.get("node_id"))
    aliases = d.get(f"amend_aliases_{raw}", aliases)
    picked = d.get(f"amend_item_type_{raw}", "")
    options = [o.replace(f"value='{picked}'>", f"value='{picked}' selected>") if picked else o
               for o in options]
    title_v = _h(d.get(f"amend_title_{raw}", ""))
    desc_v = _h(d.get(f"amend_description_{raw}", ""))
    return (f"<input type='text' name='amend_title_{nid}' size='18' placeholder='amended title' "
            f"value='{title_v}'> "
            f"<label>aliases (one per line; delete wrong ones): <textarea name='amend_aliases_{nid}' "
            f"rows='{rows}' cols='24'>{_h(aliases)}</textarea></label> "
            f"<input type='text' name='amend_description_{nid}' size='24' "
            f"placeholder='description' value='{desc_v}'> "
            f"<select name='amend_item_type_{nid}'>" + "".join(options) + "</select>" + required)


def _alias_editor(row: dict[str, Any], draft: dict[str, Any] | None = None) -> str:
    """UAT round 8: the aliases of a kept tag are the human's — edit them here (one per line; the
    list replaces the tag's aliases everywhere, not just on this document). Review round 9: the
    hidden `shown_aliases_<id>` carries the list as displayed, so an untouched box (browsers submit
    every textarea, collapsed or not) is told apart from an edit and a stale page can't undo a newer
    edit — the write is refused if the aliases changed since."""
    nid = _h(row.get("node_id"))
    shown = [str(a) for a in row.get("aliases") or []]
    rows = max(2, min(6, len(shown) + 1))
    d = draft or {}
    raw = str(row.get("node_id"))
    text = d.get(f"edit_aliases_{raw}", chr(10).join(shown))
    shown_field = d.get(f"shown_aliases_{raw}", json.dumps(shown))   # a draft keeps what was SHOWN
    return (f"<details><summary>edit aliases</summary><label>aliases (one per line; applies to this "
            f"tag everywhere): <textarea name='edit_aliases_{nid}' rows='{rows}' cols='24'>"
            f"{_h(text)}</textarea></label>"
            f"<input type='hidden' name='shown_aliases_{nid}' value='{_h(shown_field)}'>"
            f"</details>")


def _decided_cell(row: dict[str, Any]) -> str:
    meta = {"decision": row.get("status"), "decided_by": row.get("decided_by"),
            "decided_at": row.get("decided_at")}
    if row.get("decision_note"):
        meta["note"] = row["decision_note"]
    if row.get("amendments"):
        meta["amendments"] = row["amendments"]
    return _render_value(meta)


_DETAILS_SOURCE = {"enrich": "Suggestions from Enrich", "pdf": "Title from the PDF metadata",
                   "filename": "Title from the file name"}


def _name_hints(field: str, name: Any, info: dict[str, Any], budget: Any, idx: Any) -> str:
    """ADR-0070 B5: "Already confirmed elsewhere: …" under a name field, with where each spelling
    is confirmed (role + documents) and a **Use this spelling** redraw that changes only this
    field. A suggestion for a human, never evidence; nothing is saved."""
    from app.workers import name_variants
    names = info.get("confirmed_names") or {}
    found = name_variants.hints(name, names, exclude_source=info.get("source_id"), budget=budget,
                                idx=idx) if names and str(name or "").strip() else []
    if not found:
        return ""
    rows = []
    for h in found:
        where = ", ".join(f"{_h(u['role'])} of “{_h(u['title'])}”" for u in h["uses"][:3])
        more = len(h["uses"]) - 3
        rows.append(f"<li>“{_h(h['name'])}” ({where}{f', and {more} more' if more > 0 else ''}) "
                    f"<button type='submit' name='details_action' "
                    f"value='{_h('use_spelling:' + field + ':' + h['name'])}'>Use this spelling"
                    "</button></li>")
    return ("<div class='help'>Already confirmed elsewhere — possibly the same name (a hint only):"
            "<ul>" + "".join(rows) + "</ul></div>")


def _details_section(data: dict[str, Any], draft: dict[str, Any] | None) -> str:
    """ADR-0066 part 3 slice 3: the Details fields inside the main form. Draft actions (Add author
    rows, Use these) re-render with the submitted values and write nothing; the baseline token and
    the displayed values (`details_shown`) always come from the page first shown."""
    from app.workers import details as details_mod
    info = data.get("details")
    if not info:
        return ""
    d = draft or {}
    if d and "details_title" in d:
        values = details_mod.parse_form(d)
        extra = int(d.get("_extra_rows") or 0)
    else:
        values = info["prefill"]
        extra = 0
    if d.get("_use_suggestion") and info.get("suggested"):
        values = info["suggested"]
    shown = d.get("details_shown") or json.dumps(info["prefill"], ensure_ascii=False)
    baseline = d.get("details_baseline") or info["baseline"]
    status = (f"Details confirmed on {_h(info.get('confirmed_at') or 'an earlier date')}."
              if info["confirmed"] else
              f"Not confirmed: {_h(_DETAILS_SOURCE.get(info['source'], info['source']))}.")
    parts = ["<h2>Details</h2>", f"<p>{status}</p>", reconciliation_notices(info.get("pending") or [])]
    if info.get("suggestion_differs") and info.get("suggested"):
        sug = info["suggested"]
        names = ", ".join(a["name"] for a in sug["authors"]) or "no authors"
        pub = (sug.get("publisher") or {}).get("name") or "no publisher"
        parts.append(f"<p>Enrich now suggests: <em>{_h(sug['title'])}</em> — {_h(names)} — "
                     f"{_h(pub)}. <button type='submit' name='details_action' value='use_suggestion'>"
                     "Use these</button> (fills the fields; confirms nothing)</p>")
    parts.append(f"<p><label>Title: <input type='text' name='details_title' size='60' "
                 f"value='{_h(values['title'])}'></label></p>")
    rows = list(values["authors"]) + [{"name": "", "show_page": False}] * (
        details_mod.EMPTY_AUTHOR_ROWS + extra)
    rows = rows[:details_mod.MAX_AUTHOR_ROWS]
    from app.workers import name_variants
    budget = name_variants.Budget(name_variants.FORM_BUDGET)    # one budget per form render
    idx = name_variants._index(info.get("confirmed_names") or {})
    table = ["<tr><th>order</th><th>author</th><th>show page</th></tr>"]
    for i, a in enumerate(rows):
        table.append(
            f"<tr><td><input type='hidden' name='author_row_{i}' value='1'>"
            f"<input type='number' name='author_order_{i}' value='{i}' style='width:4em'></td>"
            f"<td><input type='text' name='author_name_{i}' size='40' list='ks-known-names' "
            f"value='{_h(a['name'])}'>"
            f"{_name_hints(f'author_name_{i}', a['name'], info, budget, idx)}</td>"
            f"<td><input type='checkbox' name='author_show_{i}' value='1'"
            f"{' checked' if a.get('show_page') else ''}></td></tr>")
    parts.append("<p>Authors, in document order (clear a name to remove it):</p><table>"
                 + "".join(table) + "</table>"
                 "<p class='help'><strong>show page</strong>: list this document on the person's "
                 "(or publisher's) page in the wiki's <code>Authors/</code> folder, linked from the "
                 "document's Source page. Nothing changes on this screen: the page is written when "
                 "the details are saved as correct. A ticked box means the name already has a page "
                 "from a confirmed document.</p>")
    parts.append("<p><button type='submit' name='details_action' value='add_rows'>Add author rows"
                 "</button> (writes nothing)</p>")
    pub = values.get("publisher") or {"name": "", "show_page": False}
    parts.append(f"<p><label>Publisher: <input type='text' name='publisher_name' size='40' "
                 f"list='ks-known-names' value='{_h(pub['name'])}'></label> "
                 f"<label><input type='checkbox' name='publisher_show' value='1'"
                 f"{' checked' if pub.get('show_page') else ''}> show page</label></p>"
                 + _name_hints("publisher_name", pub["name"], info, budget, idx))
    if budget.truncated:
        parts.append("<p class='help'>Name matching was cut short (too many similar names to "
                     "compare on one page).</p>")
    parts.append("<datalist id='ks-known-names'>"
                 + "".join(f"<option value='{_h(n)}'>" for n in info.get("known_names") or [])
                 + "</datalist>")
    parts.append(f"<input type='hidden' name='details_shown' value='{_h(shown)}'>"
                 f"<input type='hidden' name='details_baseline' value='{_h(baseline)}'>"
                 f"<input type='hidden' name='_extra_rows' value='{extra}'>")
    return "".join(parts)


def render_source_screen(data: dict[str, Any], *, draft: dict[str, Any] | None = None,
                         errors: list[str] | None = None, notices: list[str] | None = None,
                         warnings: list[str] | None = None,
                         add_choice: dict[str, Any] | None = None) -> str:
    """One source's screen (ADR-0066): the Details section (part 3 slice 3), tag suggestions to
    keep/drop/never-suggest, kept tags (drop to remove), type changes and retirements in ONE batch
    form (untouched rows record nothing), then the dropped-earlier list (undo) and the add-a-tag form.

    `draft` re-renders a submission (a draft action, a form-level refusal, or an Add tag / Undo drop
    pressed with other choices still unsaved) with every submitted value preserved; `errors` are the
    form-level refusal reasons, `notices` what such an action did, `warnings` a failed refresh.

    UAT part 3 #6/#7: every button on the screen except Clear confirmed details submits the ONE
    batch form (`form='ks-source-form'`, the HTML `form` attribute, since forms can't nest), so
    pressing it never discards what was chosen elsewhere on the page."""
    sid = _h(data.get("source_id"))
    draft = draft or {}
    totals = data.get("totals") or {}
    counts = data.get("counts") or {}
    preselect = data.get("preselect")
    parts = [
        _nav(),
        # Enter in any field presses this first: it redraws the page and saves nothing.
        "<button type='submit' form='ks-source-form' name='screen_action' value='redraw' "
        "class='ks-enter' tabindex='-1' aria-hidden='true'>Redraw</button>",
        f"<h1>{_h(data.get('title')) if data.get('title') else f'Source {sid}'}</h1>",
        f"<p>{sid} · {_h(data.get('filename'))} — <a href='/raw/{sid}'>view original</a> — source "
        f"{_h(data.get('position'))} of "
        f"{_h(totals.get('sources'))} · {_h(totals.get('remaining_overall'))} rows remaining "
        f"overall · this source: {_counts_cell(counts)}</p>",
        "<p><a href='/ui/reviews/sources'>Source index</a> · "
        + _next_link(data.get("next_source"), "Next document with undecided rows") + " · "
        + _next_link(data.get("next_untagged"), "Next untagged document",
                     none_text=("none known to be untagged"
                                if (totals.get("history_unknown") or 0) else "none remaining"))
        + _tagging_marker(data.get("tagging")) + "</p>",
        (f"<p class='banner'><strong>This document's tag ledger is damaged</strong> "
         f"(<code>reviews/tag_links/{sid}.json</code>: {_h(data['ledger_error'])}). Tag decisions "
         "are refused until it is restored from backup, so no earlier keep or drop is lost.</p>"
         if data.get("ledger_error") else ""),
        (f"<p class='banner'><strong>A merge/split touching this document is unfinished</strong> "
         f"(<code>{_h(data['in_flight_review_id'])}</code>). Tag decisions on its tags are refused until "
         "you run <a href='/ui/reviews/apply'>Apply</a> again, which finishes it.</p>"
         if data.get("in_flight_review_id") else ""),
        "<p>Choices on this page are saved only when you press <strong>Save</strong> (Enter never "
        "saves). Tag decisions then take effect at once and apply to <strong>this document only"
        "</strong>: "
        "<em>keep</em> tags it (a tag becomes active with its first keep), <em>drop</em> removes the "
        "link here and is remembered for re-extraction, <em>never suggest</em> blocks the name "
        "everywhere (kept links stay). Type changes and retirements are recorded and applied later "
        "via <a href='/ui/reviews/apply'>Apply</a>. Untouched rows record nothing.</p>",
        (f"<p><strong>Keep pre-selected on all suggestions</strong> — review, adjust, then Save. "
         f"<a href='/ui/reviews/sources/{sid}'>Clear pre-selection</a> (reloads the page and "
         "discards unsaved choices)</p>"
         if preselect == "keep" else
         "<p><button type='submit' form='ks-source-form' name='screen_action' "
         "value='preselect_keep'>Pre-select keep for all suggestions</button> "
         "<span class='help'>(keeps your other choices; saves nothing)</span></p>"),
        "".join(f"<p class='err'><strong>Nothing was saved:</strong> {_h(e)}</p>"
                for e in errors or []),
        "".join(f"<p class='ok'>{_h(n)}</p>" for n in notices or []),
        _refresh_warnings(warnings),
        f"<form method='post' action='/ui/reviews/sources/{sid}/decide' id='ks-source-form'>"
        "<input type='hidden' name='_screen' value='1'>",
        _details_section(data, draft),
    ]

    def _tag_section(title_txt: str, rows: list[dict[str, Any]], *, kept: bool) -> None:
        parts.append(f"<h2>{_h(title_txt)} ({len(rows)})</h2>")
        if not rows:
            parts.append("<p><em>None.</em></p>")
            return
        table = ["<tr><th>tag</th><th>type</th><th>aliases</th><th>status</th><th>notes</th>"
                 "<th>decision</th></tr>"]
        for row in rows:
            notes = []
            if row.get("kept_elsewhere"):
                notes.append(f"kept on {row['kept_elsewhere']} other document(s)")
            if row.get("node_status") == "unused":
                notes.append("unused tag — keeping revives it")
            if row.get("ambiguous_with"):
                notes.append("matches several existing tags — choose one, or keep as a new tag")
            for c in row.get("collisions") or []:          # UAT round 8: shared names → merge proposals
                where = "suggested, not kept yet" if c.get("other_status") == "candidate" \
                    else str(c.get("other_status"))
                notes.append(f"“{c.get('name')}” is also the {c.get('other_field')} of "
                             f"“{c.get('other_title')}” ({where})")
            choices = list(row.get("ambiguous_with") or [])
            if (pending := row.get("pending_use")) and pending.get("chosen"):
                notes.append(f"unfinished: “use {pending.get('chosen_title')}” started "
                             f"{pending.get('at')} — choose it again to finish, or another option "
                             "to cancel it")
                if all(m.get("node_id") != pending["chosen"] for m in choices):
                    choices.append({"node_id": pending["chosen"], "title": pending.get("chosen_title"),
                                    "item_type": "unfinished", "kept_count": "?"})
            raw = str(row.get("node_id"))
            cell = _link_radios(raw, kept=kept, preselect=preselect, ambiguous=choices,
                                chosen=draft.get(f"link_{raw}") if draft else None)
            if not kept and row.get("amendable"):
                cell += "<br>" + _amend_inputs(row, draft)
            elif kept:
                cell += "<br>" + _alias_editor(row, draft)
            table.append(
                f"<tr><td>{_h(row.get('title'))}</td><td>{_h(row.get('item_type'))}</td>"
                f"<td>{_h(', '.join(row.get('aliases') or []))}</td>"
                f"<td>{_h(row.get('node_status'))}</td><td>{_h('; '.join(notes))}</td>"
                f"<td>{cell}</td></tr>")
        parts.append("<table>" + "".join(table) + "</table>")

    def _review_section(title_txt: str, rows: list[dict[str, Any]], *, columns: str) -> None:
        parts.append(f"<h2>{_h(title_txt)} ({len(rows)})</h2>")
        if not rows:
            parts.append("<p><em>None.</em></p>")
            return
        table = [f"<tr>{columns}<th>decision</th></tr>"]
        for row in rows:
            rid = _h(row.get("review_id"))
            info = f"<td><a href='/ui/reviews/{rid}'>{rid}</a></td>"
            if row["kind"] == "retype":
                info += (f"<td>{_h(row.get('name'))}</td>"
                         f"<td>{_h(row.get('from_item_type'))} → {_h(row.get('to_item_type'))}"
                         f"{competitors_html(row.get('competitors'))}</td>")
            else:  # retired
                info += (f"<td>{_h(row.get('title'))}</td><td>{_h(row.get('page'))}</td>"
                         f"<td>{_h(row.get('node_status'))}</td>")
            if row.get("decidable"):
                rid_raw = str(row.get("review_id"))
                radios = _decision_radios(rid_raw, chosen=draft.get(f"decision_{rid_raw}")
                                          if draft else None)
                table.append(f"<tr>{info}<td>{radios}</td></tr>")
            else:
                table.append(f"<tr class='muted'>{info}<td>{_decided_cell(row)}</td></tr>")
        parts.append("<table>" + "".join(table) + "</table>")

    unfinished = data.get("unfinished") or []
    if unfinished:                                        # part-2 review: interrupted "use X"
        parts.append(f"<h2>Unfinished choices ({len(unfinished)})</h2>"
                     "<p>A “use X” choice stopped part-way. Finish it, or cancel it — what already "
                     "happened stays (if X was kept here, it stays kept until you drop it).</p>")
        table = ["<tr><th>suggestion</th><th>use</th><th>started</th><th>decision</th></tr>"]
        for u in unfinished:
            nid = _h(u.get("node_id"))
            options = [("", "— leave unfinished")]
            if u.get("chosen"):
                options.append((f"use:{u['chosen']}", f"finish: use {u.get('chosen_title')}"))
            options.append(("cancel", "cancel"))
            cell = "".join(f"<label class='opt'><input type='radio' name='link_{nid}' value='{_h(v)}'> "
                           f"{_h(label)}</label> " for v, label in options)
            table.append(f"<tr><td>{_h(u.get('name'))}</td><td>{_h(u.get('chosen_title'))}</td>"
                         f"<td>{_h(u.get('at'))}</td><td>{cell}</td></tr>")
        parts.append("<table>" + "".join(table) + "</table>")
    _tag_section("Suggested tags", data.get("suggestions") or [], kept=False)
    _tag_section("Kept tags", data.get("kept") or [], kept=True)
    _review_section("Type changes from this source", data.get("retype_items") or [],
                    columns="<th>review_id</th><th>name</th><th>retype</th>")
    _review_section("Retired by re-extraction", data.get("retired") or [],
                    columns="<th>review_id</th><th>title</th><th>page</th><th>status</th>")
    details_info = data.get("details")
    tick = ("<p><label><input type='checkbox' name='details_confirm' value='1'"
            f"{' checked' if draft.get('details_confirm') and not draft.get('_use_suggestion') else ''}"
            "> <strong>These details are correct</strong> (confirms the Details above)</label></p>"
            if details_info else "")
    if details_info and details_info.get("confirmed"):
        # The other Details choice, next to the tick (UAT 2026-11-02: it was easy to miss at the top
        # of the section). Pending until Save, like every other change on the screen.
        tick += ("<p><label><input type='checkbox' name='details_clear' value='1'"
                 f"{' checked' if draft.get('details_clear') else ''}> <strong>Clear the confirmed "
                 "details</strong> instead</label> <span class='help'>(removes the confirmed title, "
                 "authors and publisher; independence may still rest on other provenance)</span></p>")
    parts.append("<p><label>Note (applies to every review-item decision this submit): "
                 f"<input type='text' name='note' size='40' value='{_h(draft.get('note', ''))}'>"
                 "</label></p>" + tick
                 + "<p><button type='submit' class='primary' name='screen_action' value='save'>"
                 "Save all changes on "
                 "this page</button> "
                 + (f"<strong>{_h(undecided_text(data.get('undecided')))}</strong> "
                    if undecided_text(data.get("undecided")) else "")
                 + "<span class='help'>Saves every choice above at once: tag "
                 "decisions, type changes and retirements, the Details (or a Clear). Untouched rows "
                 "record nothing.</span></p></form>")

    dropped = data.get("dropped") or []
    parts.append(f"<h2>Dropped earlier ({len(dropped)})</h2>")
    if not dropped:
        parts.append("<p><em>None.</em></p>")
    else:
        rows = ["<tr><th>tag</th><th>dropped at</th><th></th></tr>"]
        for d in dropped:
            rows.append(
                f"<tr><td>{_h(d.get('name'))}</td><td>{_h(d.get('at'))}</td><td>"
                f"<button type='submit' form='ks-source-form' "
                f"formaction='/ui/reviews/sources/{sid}/undrop' name='key' "
                f"value='{_h(d.get('key'))}'>Undo drop</button></td></tr>")
        parts.append("<p>Re-extraction of this document will not suggest these again. "
                     "<strong>Undo drop</strong> saves at once, separately from Save: it makes a tag "
                     "suggestible here again (it is not kept). Your other choices stay unsaved.</p>")
        parts.append("<table>" + "".join(rows) + "</table>")

    add_type = str(draft.get("add_item_type") or "")
    f = "form='ks-source-form'"
    parts.append(
        "<h2>Add a tag</h2>"
        "<p>The <strong>Add tag</strong> button saves this one tag at once, separately from Save "
        "(an anchorless <code>asserted_by: human</code> link, kept): a new tag is created and "
        "active; an existing one is reused. Your other choices on this page stay as they are, "
        "unsaved, until you press Save.</p>"
        f"<p><label>Title: <input type='text' name='add_title' size='30' {f} "
        f"value='{_h(draft.get('add_title', ''))}'></label> "
        f"<label>Type: <select name='add_item_type' {f}>"
        + "".join(f"<option value='{t}'{' selected' if t == add_type else ''}>{t}</option>"
                  for t in sorted(taxonomy.ITEM_TYPES) if t != taxonomy.UNCLASSIFIED)
        + "</select></label></p>"
        f"<p><label>Aliases (comma-sep): <input type='text' name='add_aliases' size='30' {f} "
        f"value='{_h(draft.get('add_aliases', ''))}'></label></p>"
        f"<p><label>Description: <input type='text' name='add_description' size='50' {f} "
        f"value='{_h(draft.get('add_description', ''))}'></label></p>"
        f"<button type='submit' {f} formaction='/ui/reviews/sources/{sid}/add'>Add tag</button>"
        + _add_choice(sid, add_choice))

    if data.get("global_remaining_by_type"):
        by_type = ", ".join(f"{_h(t)}: {_h(n)}"
                            for t, n in data["global_remaining_by_type"].items())
        parts.append(f"<p>Global review items remaining (<a href='/ui/reviews'>flat queue</a>): "
                     f"{by_type}</p>")
    return _page(f"Source {data.get('source_id')}", "".join(parts))


def _refresh_warnings(warnings: list[str] | None) -> str:
    """The banner for a failed post-action refresh (ADR-0066 review round 2: never swallowed).
    ADR-0068: the work stays queued, so the fix is Retry publishing, never repeating a decision."""
    if not warnings:
        return ""
    return ("<p class='banner'><strong>Pages or search index not refreshed</strong> ("
            + _h(", ".join(warnings)) + "): the decisions are saved, but Source pages or search may "
            "show old titles or statuses until publishing succeeds. Use <strong>Retry publishing"
            "</strong> (or <code>uv run python scripts/republish.py</code>).</p>")


def reconciliation_notices(pending: list[str]) -> str:
    """ADR-0066 part 3 slice 3: one line per producer that hasn't reconciled this document's changed
    independence inputs, and (ADR-0069 slice 2(a)) the contradiction-maintenance notice when a
    proposed contradiction of this document no longer matches its evidence (report-only)."""
    from app.workers import contradictions, reconciliation
    text = {**reconciliation.SCREEN_NOTICES, contradictions.NOTICE_NAME: contradictions.SCREEN_NOTICE}
    return "".join(f"<p class='banner'>{_h(text.get(name, name))}</p>" for name in pending)


def render_source_decide_result(source_id: str, results: list[dict[str, Any]], *,
                                 warnings: list[str] | None = None,
                                 pending: list[str] | None = None,
                                 progress: dict[str, Any] | None = None) -> str:
    """Per-row batch results (recorded vs skipped-with-reason; never a batch abort). `progress` (the
    document's view recomputed AFTER the save, ADR-0070 §6): the still-undecided warning — never a
    block; untouched rows stay unchanged — and "Next document with undecided rows"."""
    sid = _h(source_id)
    recorded = sum(1 for r in results if r.get("recorded"))
    left = ""
    if progress is not None:
        text = undecided_text(progress.get("undecided"))
        total = int((progress.get("undecided") or {}).get("total") or 0)
        left = ((f"<p class='banner'>{_h(total)} row{'s' if total != 1 else ''} on this document "
                 f"{'are' if total != 1 else 'is'} still undecided ({_h(text)}).</p>")
                if total else "<p class='ok'>Nothing is left undecided on this document.</p>")
        left += ("<p>" + _next_link(progress.get("next_source"),
                                    "Next document with undecided rows") + "</p>")
    parts = [
        _nav(),
        f"<h1>Batch decisions — source {sid}</h1>",
        f"<p>{_h(recorded)} recorded, {_h(len(results) - recorded)} skipped/no-op.</p>",
        left,
        _refresh_warnings(warnings),
        reconciliation_notices(pending or []),
        f"<p><a href='/ui/reviews/sources/{sid}'>Back to source</a> · "
        "<a href='/ui/reviews/sources'>Source index</a> · "
        "<a href='/ui/reviews/apply'>Apply…</a></p>",
    ]
    if not results:
        parts.append("<p><em>No decisions were marked.</em></p>")
    else:
        rows = ["<tr><th>row</th><th>action</th><th>recorded</th>"
                "<th>status / skip reason</th></tr>"]
        for r in results:
            if r.get("review_id"):
                rid = _h(r.get("review_id"))
                label = f"<a href='/ui/reviews/{rid}'>{rid}</a>"
            else:
                label = _h(r.get("title") or r.get("node_id"))
            outcome = _h(refusal_text(r["skip_reason"], r.get("skip_info"))
                         if r.get("skip_reason") else r.get("status") or "")
            outcome += _merge_action(r.get("skip_info")) + _advisories(r.get("advisories"))
            amended = " (amended)" if r.get("amended") else ""
            rows.append(f"<tr><td>{label}</td>"
                        f"<td>{_h(r.get('action'))}</td><td>{_h(r.get('recorded'))}</td>"
                        f"<td>{outcome}{_h(amended)}</td></tr>")
        parts.append("<table>" + "".join(rows) + "</table>")
    return _page(f"Batch decisions — {source_id}", "".join(parts))


# --- tag lists (ADR-0066) ----------------------------------------------------


def render_unused_tags(rows: list[dict[str, Any]], *, result: dict[str, Any] | None = None) -> str:
    """The Unused tags list (tags no document keeps) with the one bulk Delete unused action."""
    parts = [_nav(), "<h1>Unused tags</h1>",
             "<p>Tags that lost their last kept link. They are hidden from browsing and keep their "
             "edits; keeping one on any document revives it. <strong>Delete unused</strong> removes "
             "every unprotected one permanently (merge-redirect targets and tags with a synthesis "
             "page are kept).</p>"]
    if result is not None:
        parts.append(f"<p class='banner'>Deleted {_h(len(result.get('deleted') or []))}; "
                     f"skipped {_h(len(result.get('skipped') or []))} protected.</p>")
        parts.append(_refresh_warnings(result.get("warnings")))
    if not rows:
        parts.append("<p><em>No unused tags.</em></p>")
        return _page("Unused tags", "".join(parts))
    table = ["<tr><th>tag</th><th>type</th><th>protected</th></tr>"]
    for r in rows:
        table.append(f"<tr><td>{_h(r.get('title'))} <code>{_h(r.get('node_id'))}</code></td>"
                     f"<td>{_h(r.get('item_type'))}</td>"
                     f"<td>{_h(r.get('protected') or '')}</td></tr>")
    parts.append("<table>" + "".join(table) + "</table>")
    deletable = sum(1 for r in rows if not r.get("protected"))
    if deletable:
        parts.append("<form method='post' action='/ui/reviews/tags/unused/delete'>" + _ENTER_GUARD
                     + f"<button type='submit' class='danger'>Delete {_h(deletable)} unused "
                     f"tag{'s' if deletable != 1 else ''}</button></form>")
    return _page("Unused tags", "".join(parts))


def render_blocked_tags(blocked: dict[str, str], *, result: dict[str, Any] | None = None) -> str:
    """The Blocked tags list (policies/blocked_tags.txt) with per-name unblock + the Re-suggest hint."""
    parts = [_nav(), "<h1>Blocked tags</h1>",
             "<p>Names extraction never suggests (<code>policies/blocked_tags.txt</code>, editable by "
             "hand). Kept links are never affected.</p>"]
    if result is not None and result.get("outcome") == "unblocked":
        sources = result.get("resuggest_sources") or []
        parts.append(f"<div class='banner'>Unblocked <strong>{_h(result.get('name'))}</strong>. "
                     "Future extractions may suggest it again.")
        if sources:
            parts.append(" To re-suggest it now on the documents that mentioned it (replayed from "
                         "the response cache only — never a provider call; a document with no "
                         "cached response is reported and left as it is), run:<br><code>uv run "
                         "python scripts/extract_items.py --force --cache-only "
                         + _h(" ".join(sources)) + "</code>")
        parts.append("</div>")
    if not blocked:
        parts.append("<p><em>No blocked tags.</em></p>")
        return _page("Blocked tags", "".join(parts))
    table = ["<tr><th>name</th><th></th></tr>"]
    for key, name in sorted(blocked.items(), key=lambda kv: kv[1].lower()):
        table.append(
            f"<tr><td>{_h(name)}</td><td>"
            "<form method='post' action='/ui/reviews/tags/blocked/unblock' class='decide'>"
            f"<input type='hidden' name='key' value='{_h(key)}'>"
            "<button type='submit'>Unblock</button></form></td></tr>")
    parts.append("<table>" + "".join(table) + "</table>")
    return _page("Blocked tags", "".join(parts))


# --- error -----------------------------------------------------------------


_MERGE_OUTCOME_TEXT = {
    "filed": "A new <code>merge_items</code> item will be filed <strong>approved</strong> by you.",
    "approved_existing": "An existing pending proposal for this exact merge will be "
                         "<strong>approved</strong> in place.",
}


def render_merge_confirm(plan: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Step 1 of the human-filed merge (ADR-0064 review round): what the merge WOULD do — nothing has
    been written yet. Confirm POSTs the same survivor/absorbed to /ui/reviews/merge."""
    sv, ab = ctx["survivor"], ctx["absorbed"]
    parts = [_nav(), "<h1>Confirm merge</h1>"]
    if plan["outcome"] == "already_approved":
        rid = _h(plan["review_id"])
        parts.append(f"<p>This merge is already approved: <a href='/ui/reviews/{rid}'>{rid}</a>. "
                     "Nothing to file — it takes effect at the next apply.</p>")
        return _page("Confirm merge", "".join(parts))
    rows = "".join(
        f"<tr><th>{label}</th><td>{_h(n['title'])} <code>{_h(n['slug'])}</code> "
        f"<code>{_h(n['node_id'])}</code></td><td>{_h(len(n['sources']))} source(s)</td></tr>"
        for label, n in (("Survivor (stays active)", sv), ("Absorbed (becomes a tombstone)", ab)))
    parts.append(f"<table>{rows}</table>")
    parts.append(
        f"<p>At apply: <strong>{_h(ctx['absorbed_active_edges'])}</strong> active edge(s) on the "
        "absorbed item re-point to the survivor; its unresolved review items are withdrawn; the "
        "merge is <strong>forward-only</strong> (auditable, not live-reversible).</p>")
    parts.append("<p>Survivor aliases after the merge:</p>" + _render_value(ctx["aliases_after"]))
    parts.append(f"<p>{_MERGE_OUTCOME_TEXT[plan['outcome']]}</p>")
    if plan["withdraw"]:
        links = ", ".join(f"<a href='/ui/reviews/{_h(w)}'>{_h(w)}</a>" for w in plan["withdraw"])
        parts.append(f"<p class='banner'>Conflicting pending merge proposal(s) will be withdrawn "
                     f"(audited, <code>superseded_by_human_merge</code>): {links}</p>")
    parts.append(
        "<form method='post' action='/ui/reviews/merge'>"
        f"<input type='hidden' name='survivor' value='{_h(sv['node_id'])}'>"
        f"<input type='hidden' name='absorbed' value='{_h(ab['node_id'])}'>"
        "<button type='submit' class='primary'>Confirm and approve merge</button> "
        "<a href='/ui/reviews'>Cancel</a></form>")
    return _page("Confirm merge", "".join(parts))


def render_error(status_code: int, message: str, *, link: str | None = None) -> str:
    body = [
        _nav(),
        f"<h1>Error {_h(status_code)}</h1>",
        f"<p class='err'>{_h(message)}</p>",
    ]
    if link:                                     # ADR-0070 B4: the review to reopen first
        body.append(f"<p><a href='/ui/reviews/{_h(link)}'>Open {_h(link)} to reopen it</a></p>")
    return _page(f"Error {status_code}", "".join(body))
