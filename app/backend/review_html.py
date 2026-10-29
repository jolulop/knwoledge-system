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
from typing import Any

from app.backend import taxonomy
from app.backend.review_read import reopen_block_reason

_STYLE = (
    "<style>"
    "body{font-family:system-ui,sans-serif;margin:2rem;max-width:60rem;color:#1a1a1a}"
    "table{border-collapse:collapse;margin:.5rem 0}"
    "th,td{border:1px solid #ccc;padding:.3rem .6rem;text-align:left;vertical-align:top}"
    "table.kv th{background:#f5f5f5;white-space:nowrap}"
    ".banner{background:#fff3cd;border:1px solid #e0c069;padding:.5rem .8rem;margin:.5rem 0}"
    ".err{background:#f8d7da;border:1px solid #d99;padding:.5rem .8rem}"
    "form.decide{display:inline;margin-right:.4rem}"
    "nav a{margin-right:.8rem}"
    "code{background:#f0f0f0;padding:0 .2rem}"
    "</style>"
)


def _h(value: Any) -> str:
    """The one mandatory escape: every dynamic value passes through here (quote=True)."""
    return html.escape(str(value), quote=True)


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
        rows = ["<tr><th>review_id</th><th>type</th><th>status</th><th>priority</th>"
                "<th>created_at</th>" + ("<th>applied</th>" if applied is not None else "") + "</tr>"]
        for it in data["items"]:
            rid = _h(it.get("review_id"))
            cell = ""
            if applied is not None:
                state = applied.get(it.get("review_id")) or {}
                if state.get("state") == "applied":
                    cell = f"<td>applied {_h(state.get('at'))}</td>"
                elif state.get("state") == "in_effect":
                    cell = "<td>already in effect — nothing left to apply</td>"
                elif state.get("state") == "cannot_verify":
                    last = (f" (last applied {_h(state['at'])}, before this approval)"
                            if state.get("at") else "")
                    cell = f"<td>cannot verify — Apply re-checks it{last}</td>"
                elif state.get("state") == "not_yet":
                    cell = "<td><strong>not yet</strong> — run Apply</td>"
                else:
                    cell = "<td>—</td>"
            rows.append(
                f"<tr><td><a href='/ui/reviews/{rid}'>{rid}</a></td>"
                f"<td>{_h(it.get('type'))}</td><td>{_h(it.get('status'))}</td>"
                f"<td>{_h(it.get('priority'))}</td><td>{_h(it.get('created_at'))}</td>{cell}</tr>")
        parts.append("<table>" + "".join(rows) + "</table>")
        if applied is not None:
            parts.append("<p>Approved items stay listed here after Apply. <em>applied</em> shows when "
                         "a merge, split or type change was carried out; <em>already in effect</em> "
                         "means a re-approved one needs nothing more; <em>cannot verify</em> means "
                         "the saved state is incomplete (a missing page, an unfinished operation) — "
                         "Apply re-checks it; — means the type leaves no per-item record (Apply "
                         "re-checks those every run).</p>")
    parts.append(
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
    return _page(f"Reviews — {status}", "".join(parts))


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


def _decision_section(review_id: str, item: dict[str, Any], *, invalid_subject: bool = False,
                      effect_status: str | None = None) -> str:
    # Terminal items (approved/rejected) are immutable — show the recorded decision, not forms that
    # the backend would 409. Forms stay for pending/deferred (deferred is non-terminal). A terminal item
    # also gets the ADR-0045 reopen affordance (gated on effect_status).
    status = item.get("status")
    if status in ("approved", "rejected"):
        meta = {"decision": status, "decided_by": item.get("decided_by"),
                "decided_at": item.get("decided_at"), "note": item.get("decision_note")}
        if item.get("winner"):  # ADR-0044: the recorded contradiction supersede winner
            meta["winner"] = item["winner"]
        return ("<h2>Decision (recorded)</h2>" + _render_value(meta)
                + "<p>Effects are applied via <a href='/ui/reviews/apply'>Apply</a>.</p>"
                + _reopen_section(review_id, effect_status))
    rid = _h(review_id)
    # ADR-0044: a resolve_contradiction offers winner selection — Acknowledge (both stand) /
    # Supersede A|B / Reject / Defer. Each button is one atomic action the decide handler translates.
    if item.get("type") == "resolve_contradiction" and not invalid_subject:
        subj = item.get("subject") or {}
        a, b = _h(str(subj.get("claim_a"))), _h(str(subj.get("claim_b")))
        actions = (("acknowledge", "Acknowledge (both claims stand)"),
                   ("supersede_a", f"Supersede: A wins → deprecate B ({a} beats {b})"),
                   ("supersede_b", f"Supersede: B wins → deprecate A ({b} beats {a})"),
                   ("reject", "Reject"), ("defer", "Defer"))
        buttons = "".join(
            f"<button type='submit' name='action' value='{act}'>{label}</button> "
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
    buttons = "".join(
        f"<button type='submit' name='action' value='{a}'>{_h(label)}</button> "
        for a, label in actions)
    warn = ("<p><strong>⚠ Invalid subject:</strong> this item's source_id is not canonical "
            "(possible tampering). It cannot be applied; approval is disabled.</p>"
            if invalid_subject else "")
    return ("<h2>Decision</h2>"
            "<p>Decisions are recorded only; effects are applied later via "
            "<a href='/ui/reviews/apply'>Apply</a>.</p>"
            f"{warn}"
            f"<form method='post' action='/ui/reviews/{rid}/decide'>"
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
        "created_at": item.get("created_at"),
        "decided_by": item.get("decided_by"),
        "decided_at": item.get("decided_at"),
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
        f"<h1>Review {_h(review_id)}</h1>",
        "<h2>Item</h2>", _render_value(summary_rows),
        "<h2>Preview</h2>", _render_value(preview_rows),
        "<h2>Apply state</h2>", _render_value(apply_rows),
        "<h2>Details</h2>", _render_value(preview.get("details") or {}),
        _stored_proposal(item),
        _decision_section(review_id, item, invalid_subject=bool(preview.get("invalid_subject")),
                          effect_status=ap.get("effect_status")),
    ]
    return _page(f"Review {review_id}", "".join(body))


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


def render_apply_dry_run(scope: dict[str, Any], dry: dict[str, Any]) -> str:
    """Step-1 apply page (ADR-0040): the dry-run mutation preview. Apply is offered ONLY when the
    preview is clean (`status == "ok"`); a blocked/failed/validation-failed preview withholds it."""
    status = dry.get("status")
    body = [
        _nav(),
        "<h1>Apply approved reviews — preview</h1>",
        f"<p>Dry-run status: <code>{_h(status)}</code> "
        "(no live state was changed by this preview).</p>",
    ]
    if dry.get("reason"):
        body.append(f"<p class='banner'>blocked/failed: {_h(dry.get('reason'))} "
                    f"{_h(dry.get('error') or '')}</p>")
    diff = dry.get("diff") or {}
    graph_diff = diff.get("graph") or {}
    body.append("<h2>Graph changes</h2>")
    body.append(_render_value({
        "edges_added": graph_diff.get("edges_added"), "edges_removed": graph_diff.get("edges_removed"),
        "edges_status_changed": graph_diff.get("edges_status_changed"),
        "nodes_status_changed": graph_diff.get("nodes_status_changed"),
        "nodes_added": graph_diff.get("nodes_added"),
    }))
    body.append("<h2>Manifest status changes</h2>")
    body.append(_render_value(diff.get("manifests") or []))
    body.append("<h2>Review file moves</h2>")
    body.append(_render_value(diff.get("reviews") or []))
    body.append("<h2>Wiki page diffs</h2>")
    for page in diff.get("wiki") or []:
        body.append(f"<h3>{_h(page.get('path'))}</h3><pre>{_h(page.get('unified_diff'))}</pre>")
    body.append("<h2>Per-item provenance <small>(best-effort; the diff above is authoritative)</small></h2>")
    body.append(_render_value(dry.get("items") or []))
    body.append("<h2>Not appliable</h2>")
    body.append(_render_value(dry.get("not_appliable") or []))
    body.append("<h2>Validators (would pass?)</h2>")
    body.append(_render_value(dry.get("validators") or {}))
    if dry.get("warnings"):
        body.append(f"<p class='banner'>warnings: {_render_value(dry['warnings'])}</p>")

    token = dry.get("preview_token")
    if status == "ok" and token:
        body.append(
            "<form method='post' action='/ui/reviews/apply'>"
            f"<input type='hidden' name='preview_token' value='{_h(token)}'>"
            "<button type='submit'>Apply now</button></form>")
    elif status == "ok":
        reason = dry.get("token_withheld") or ""
        body.append(f"<p class='err'>Apply is withheld: {_WITHHELD.get(reason, 'Preview again.')}"
                    "</p>" + _PREVIEW_FORM)
    else:
        body.append("<p class='err'>Apply is withheld: the preview is not clean "
                    f"(status <code>{_h(status)}</code>). Resolve the cause before applying.</p>")
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
    s = result.get("summary") or {}
    body = [
        _nav(),
        f"<h1>Apply result — {_h(result.get('status'))}</h1>",
    ]
    if result.get("status") == "saved_publication_pending":
        body.append("<p class='banner'><strong>Saved; publication incomplete.</strong> The approved "
                    "decisions were applied; some pages or search still need refreshing. Use "
                    "<strong>Retry publishing</strong>, and don't apply or decide again.</p>")
    body += [
        f"<p>validators_ok: <code>{_h(result.get('validators_ok'))}</code></p>",
    ]
    if result.get("warnings"):
        body.append(f"<p class='banner'>warnings: {_render_value(result['warnings'])}</p>")
    body.append("<h2>Summary</h2>")
    body.append(_render_value({
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
    }))
    if result.get("failed_validators"):
        body.append("<h2 class='err'>Failed validators</h2>")
        body.append(_render_value(result["failed_validators"]))
    return _page("Apply result", "".join(body))


# --- per-source review flow (ADR-0058) --------------------------------------


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
    rows = ["<tr><th>#</th><th>source</th><th>file</th><th>ingested</th>"
            "<th>remaining / kept / dropped / deferred / added</th></tr>"]
    for i, s in enumerate(data.get("sources") or [], start=1):
        sid = _h(s.get("source_id"))
        remaining = (s.get("counts") or {}).get("remaining", 0)
        style = "" if remaining else " style='color:#999'"
        rows.append(
            f"<tr{style}><td>{i}</td>"
            f"<td><a href='/ui/reviews/sources/{sid}'>{sid}</a></td>"
            f"<td><a href='/raw/{sid}'>{_h(s.get('filename'))}</a></td>"
            f"<td>{_h(s.get('discovered_at'))}</td>"
            f"<td>{_counts_cell(s.get('counts') or {})}{'' if remaining else ' — done'}</td></tr>")
    parts.append("<table>" + "".join(rows) + "</table>")
    if data.get("global_remaining_by_type"):
        by_type = ", ".join(f"{_h(t)}: {_h(n)}"
                            for t, n in data["global_remaining_by_type"].items())
        parts.append(f"<p>Global review items remaining (flat queue): {by_type}</p>")
    return _page("Review by source", "".join(parts))


def _decision_radios(rid: str, *, preselect: str | None = None) -> str:
    """Per-row decision radios for review items (type changes, retirements). The explicit
    no-decision option is load-bearing in this JS-free UI: a radio group cannot be DEselected once
    clicked, so it is the only way to take back a misclick before submit (and unlike defer it
    records NOTHING — the row just stays pending)."""
    rid = _h(rid)
    default = "approve" if preselect == "approve" else ""
    return ("".join(
        f"<label><input type='radio' name='decision_{rid}' value='{v}'"
        f"{' checked' if v == default else ''}> {label}</label> "
        for v, label in (("", "— leave pending"), ("approve", "approve"),
                         ("reject", "reject"), ("defer", "defer"))))


def _link_radios(nid: str, *, kept: bool, preselect: str | None = None,
                 ambiguous: list[dict[str, Any]] | None = None) -> str:
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
    default = "keep" if (preselect == "keep" and not kept and not ambiguous) else ""
    return "".join(
        f"<label><input type='radio' name='link_{nid}' value='{_h(v)}'"
        f"{' checked' if v == default else ''}> {_h(label)}</label> " for v, label in options)


def _amend_inputs(row: dict[str, Any]) -> str:
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
    return (f"<input type='text' name='amend_title_{nid}' size='18' placeholder='amended title'> "
            f"<label>aliases (one per line; delete wrong ones): <textarea name='amend_aliases_{nid}' "
            f"rows='{rows}' cols='24'>{_h(aliases)}</textarea></label> "
            f"<input type='text' name='amend_description_{nid}' size='24' "
            "placeholder='description'> "
            f"<select name='amend_item_type_{nid}'>" + "".join(options) + "</select>" + required)


def _alias_editor(row: dict[str, Any]) -> str:
    """UAT round 8: the aliases of a kept tag are the human's — edit them here (one per line; the
    list replaces the tag's aliases everywhere, not just on this document). Review round 9: the
    hidden `shown_aliases_<id>` carries the list as displayed, so an untouched box (browsers submit
    every textarea, collapsed or not) is told apart from an edit and a stale page can't undo a newer
    edit — the write is refused if the aliases changed since."""
    nid = _h(row.get("node_id"))
    shown = [str(a) for a in row.get("aliases") or []]
    rows = max(2, min(6, len(shown) + 1))
    return (f"<details><summary>edit aliases</summary><label>aliases (one per line; applies to this "
            f"tag everywhere): <textarea name='edit_aliases_{nid}' rows='{rows}' cols='24'>"
            f"{_h(chr(10).join(shown))}</textarea></label>"
            f"<input type='hidden' name='shown_aliases_{nid}' value='{_h(json.dumps(shown))}'>"
            f"</details>")


def _decided_cell(row: dict[str, Any]) -> str:
    meta = {"decision": row.get("status"), "decided_by": row.get("decided_by"),
            "decided_at": row.get("decided_at")}
    if row.get("decision_note"):
        meta["note"] = row["decision_note"]
    if row.get("amendments"):
        meta["amendments"] = row["amendments"]
    return _render_value(meta)


def render_source_screen(data: dict[str, Any]) -> str:
    """One source's screen (ADR-0066): tag suggestions to keep/drop/never-suggest, kept tags (drop to
    remove), type changes and retirements in ONE batch form (untouched rows record nothing), then
    the dropped-earlier list (undo) and the add-a-tag form."""
    sid = _h(data.get("source_id"))
    totals = data.get("totals") or {}
    counts = data.get("counts") or {}
    preselect = data.get("preselect")
    parts = [
        _nav(),
        f"<h1>Source {sid}</h1>",
        f"<p>{_h(data.get('filename'))} — <a href='/raw/{sid}'>view original</a> — source "
        f"{_h(data.get('position'))} of "
        f"{_h(totals.get('sources'))} · {_h(totals.get('remaining_overall'))} rows remaining "
        f"overall · this source: {_counts_cell(counts)}</p>",
        "<p><a href='/ui/reviews/sources'>Source index</a>"
        + (f" · <a href='/ui/reviews/sources/{_h(data['next_source'])}'>Next source</a>"
           if data.get("next_source") else "") + "</p>",
        (f"<p class='banner'><strong>This document's tag ledger is damaged</strong> "
         f"(<code>reviews/tag_links/{sid}.json</code>: {_h(data['ledger_error'])}). Tag decisions "
         "are refused until it is restored from backup, so no earlier keep or drop is lost.</p>"
         if data.get("ledger_error") else ""),
        (f"<p class='banner'><strong>A merge/split touching this document is unfinished</strong> "
         f"(<code>{_h(data['in_flight_review_id'])}</code>). Tag decisions on its tags are refused until "
         "you run <a href='/ui/reviews/apply'>Apply</a> again, which finishes it.</p>"
         if data.get("in_flight_review_id") else ""),
        "<p>Tag decisions take effect immediately and apply to <strong>this document only</strong>: "
        "<em>keep</em> tags it (a tag becomes active with its first keep), <em>drop</em> removes the "
        "link here and is remembered for re-extraction, <em>never suggest</em> blocks the name "
        "everywhere (kept links stay). Type changes and retirements are recorded and applied later "
        "via <a href='/ui/reviews/apply'>Apply</a>. Untouched rows record nothing.</p>",
        (f"<p><strong>Keep pre-selected on all suggestions</strong> — review, adjust, then submit. "
         f"<a href='/ui/reviews/sources/{sid}'>Clear pre-selection</a></p>"
         if preselect == "keep" else
         f"<form method='get' action='/ui/reviews/sources/{sid}'>"
         "<input type='hidden' name='preselect' value='keep'>"
         "<button type='submit'>Pre-select keep for all suggestions</button></form>"),
        f"<form method='post' action='/ui/reviews/sources/{sid}/decide'>",
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
            cell = _link_radios(str(row.get("node_id")), kept=kept, preselect=preselect,
                                ambiguous=choices)
            if not kept and row.get("amendable"):
                cell += "<br>" + _amend_inputs(row)
            elif kept:
                cell += "<br>" + _alias_editor(row)
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
                         f"<td>{_h(row.get('from_item_type'))} → {_h(row.get('to_item_type'))}</td>")
            else:  # retired
                info += (f"<td>{_h(row.get('title'))}</td><td>{_h(row.get('page'))}</td>"
                         f"<td>{_h(row.get('node_status'))}</td>")
            if row.get("decidable"):
                table.append(f"<tr>{info}<td>{_decision_radios(str(row.get('review_id')))}</td></tr>")
            else:
                table.append(f"<tr style='color:#777'>{info}<td>{_decided_cell(row)}</td></tr>")
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
            cell = "".join(f"<label><input type='radio' name='link_{nid}' value='{_h(v)}'> "
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
    parts.append("<p><label>Note (applies to every review-item decision this submit): "
                 "<input type='text' name='note' size='40'></label></p>"
                 "<button type='submit'>Record marked decisions</button></form>")

    dropped = data.get("dropped") or []
    parts.append(f"<h2>Dropped earlier ({len(dropped)})</h2>")
    if not dropped:
        parts.append("<p><em>None.</em></p>")
    else:
        rows = ["<tr><th>tag</th><th>dropped at</th><th></th></tr>"]
        for d in dropped:
            rows.append(
                f"<tr><td>{_h(d.get('name'))}</td><td>{_h(d.get('at'))}</td><td>"
                f"<form method='post' action='/ui/reviews/sources/{sid}/undrop' class='decide'>"
                f"<input type='hidden' name='key' value='{_h(d.get('key'))}'>"
                "<button type='submit'>Undo drop</button></form></td></tr>")
        parts.append("<p>Re-extraction of this document will not suggest these again. Undo makes "
                     "a tag suggestible here again (it is not kept).</p>")
        parts.append("<table>" + "".join(rows) + "</table>")

    parts.append(
        "<h2>Add a tag</h2>"
        "<p>Tags this document immediately (an anchorless <code>asserted_by: human</code> link, "
        "kept): a new tag is created and active; an existing one is reused.</p>"
        f"<form method='post' action='/ui/reviews/sources/{sid}/add'>"
        "<p><label>Title: <input type='text' name='title' size='30' required></label> "
        "<label>Type: <select name='item_type'>"
        + "".join(f"<option value='{t}'>{t}</option>"
                  for t in sorted(taxonomy.ITEM_TYPES) if t != taxonomy.UNCLASSIFIED)
        + "</select></label></p>"
        "<p><label>Aliases (comma-sep): <input type='text' name='aliases' size='30'></label></p>"
        "<p><label>Description: <input type='text' name='description' size='50'></label></p>"
        "<button type='submit'>Add tag</button></form>")

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


def render_source_decide_result(source_id: str, results: list[dict[str, Any]], *,
                                 warnings: list[str] | None = None) -> str:
    """Per-row batch results (recorded vs skipped-with-reason; never a batch abort)."""
    sid = _h(source_id)
    recorded = sum(1 for r in results if r.get("recorded"))
    parts = [
        _nav(),
        f"<h1>Batch decisions — source {sid}</h1>",
        f"<p>{_h(recorded)} recorded, {_h(len(results) - recorded)} skipped/no-op.</p>",
        _refresh_warnings(warnings),
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
            outcome = _h(r.get("skip_reason") or r.get("status") or "")
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
        parts.append("<form method='post' action='/ui/reviews/tags/unused/delete'>"
                     f"<button type='submit'>Delete unused ({_h(deletable)})</button></form>")
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
        "<button type='submit'>Confirm and approve merge</button> "
        "<a href='/ui/reviews'>Cancel</a></form>")
    return _page("Confirm merge", "".join(parts))


def render_error(status_code: int, message: str) -> str:
    body = [
        _nav(),
        f"<h1>Error {_h(status_code)}</h1>",
        f"<p class='err'>{_h(message)}</p>",
    ]
    return _page(f"Error {status_code}", "".join(body))
