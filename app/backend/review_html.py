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
from typing import Any

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


def _nav() -> str:
    return ("<nav>"
            "<a href='/ui/reviews'>Queue</a>"
            "<a href='/ui/reviews?status=approved'>Approved</a>"
            "<a href='/ui/reviews/apply'>Apply…</a>"
            "</nav>")


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
        rows = ["<tr><th>review_id</th><th>type</th><th>status</th><th>priority</th>"
                "<th>created_at</th></tr>"]
        for it in data["items"]:
            rid = _h(it.get("review_id"))
            rows.append(
                f"<tr><td><a href='/ui/reviews/{rid}'>{rid}</a></td>"
                f"<td>{_h(it.get('type'))}</td><td>{_h(it.get('status'))}</td>"
                f"<td>{_h(it.get('priority'))}</td><td>{_h(it.get('created_at'))}</td></tr>")
        parts.append("<table>" + "".join(rows) + "</table>")
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


def _decision_section(review_id: str, item: dict[str, Any], *, invalid_subject: bool = False) -> str:
    # Terminal items (approved/rejected) are immutable — show the recorded decision, not forms that
    # the backend would 409. Forms stay for pending/deferred (deferred is non-terminal).
    status = item.get("status")
    if status in ("approved", "rejected"):
        meta = {"decision": status, "decided_by": item.get("decided_by"),
                "decided_at": item.get("decided_at"), "note": item.get("decision_note")}
        if item.get("winner"):  # ADR-0044: the recorded contradiction supersede winner
            meta["winner"] = item["winner"]
        return ("<h2>Decision (recorded)</h2>" + _render_value(meta)
                + "<p>Effects are applied via <a href='/ui/reviews/apply'>Apply</a>.</p>")
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
        _decision_section(review_id, item, invalid_subject=bool(preview.get("invalid_subject"))),
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
    body.append(
        "<form method='post' action='/ui/reviews/apply'>"
        "<button type='submit'>Apply now</button></form>")
    return _page("Apply reviews", "".join(body))


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

    if status == "ok":
        body.append(
            "<form method='post' action='/ui/reviews/apply'>"
            "<button type='submit'>Apply now</button></form>")
    else:
        body.append("<p class='err'>Apply is withheld: the preview is not clean "
                    f"(status <code>{_h(status)}</code>). Resolve the cause before applying.</p>")
    return _page("Apply preview", "".join(body))


def render_apply_result(result: dict[str, Any]) -> str:
    s = result.get("summary") or {}
    body = [
        _nav(),
        f"<h1>Apply result — {_h(result.get('status'))}</h1>",
        f"<p>validators_ok: <code>{_h(result.get('validators_ok'))}</code></p>",
    ]
    if result.get("warnings"):
        body.append(f"<p class='banner'>warnings: {_render_value(result['warnings'])}</p>")
    body.append("<h2>Summary</h2>")
    body.append(_render_value({
        "syntheses": s.get("syntheses"), "promotions": s.get("promotions"),
        "contradictions": s.get("contradictions"), "deprecations": s.get("deprecations"),
        "duplicates": s.get("duplicates"), "archives": s.get("archives"),
        "hidden": s.get("hidden"),
        "pages_changed": s.get("pages_changed"), "index_rebuilt": s.get("index_rebuilt"),
        "unapplied": s.get("unapplied"),
    }))
    if result.get("failed_validators"):
        body.append("<h2 class='err'>Failed validators</h2>")
        body.append(_render_value(result["failed_validators"]))
    return _page("Apply result", "".join(body))


# --- error -----------------------------------------------------------------


def render_error(status_code: int, message: str) -> str:
    body = [
        _nav(),
        f"<h1>Error {_h(status_code)}</h1>",
        f"<p class='err'>{_h(message)}</p>",
    ]
    return _page(f"Error {status_code}", "".join(body))
