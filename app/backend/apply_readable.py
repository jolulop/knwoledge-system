"""Readable Apply pages (ADR-0072 B2a; ADR-0070 §4).

Turns the per-review outcomes an Apply run reported (`app/workers/apply_outcomes.py`) into plain
sentences, in four groups kept apart: **changes**, **already in effect** (a count line),
**attention needed** (blocked / incomplete / outcome not reported, each labelled) and **advisories**.
The preview uses the sandbox run's outcomes, the result page the real run's. Pure: the names come
from the outcomes' `details` (captured by the executors before they changed anything) — this module
never reads the vault. Every dynamic value is escaped here.
"""
from __future__ import annotations

import html
from typing import Any

from app.workers import apply_outcomes as ao


def _h(value: Any) -> str:
    return html.escape(str(value), quote=True)


TYPE_LABELS = {
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
_ORDER = list(TYPE_LABELS)


def _label(review_type: Any) -> str:
    return TYPE_LABELS.get(str(review_type), str(review_type or "other"))


def _pair(value: Any) -> dict[str, str] | None:
    if isinstance(value, dict) and (value.get("id") or value.get("name")):
        return ao.named(value.get("id"), value.get("name"))
    return None


def _subjects(rec: dict[str, Any]) -> list[dict[str, str]]:
    """Every name one outcome shows (for collision detection): both ends of a merge, and a renamed
    promotion's new title (under the same id)."""
    d = rec.get("details") or {}
    out = [p for p in (_pair(d.get(k)) for k in ("subject", "survivor", "absorbed", "spinoff",
                                                 "duplicate_of", "partner")) if p]
    after = d.get("after") if isinstance(d.get("after"), dict) else {}
    subj = _pair(d.get("subject"))
    if subj and isinstance(after.get("title"), str) and after["title"].strip():
        out.append(ao.named(subj["id"], after["title"]))
    return out


def _advisory_names(adv: dict[str, Any]) -> list[dict[str, str]]:
    names = [ao.named(adv.get("node_id"), adv.get("name"))] if adv.get("node_id") else []
    return names + [ao.named(m.get("node_id"), m.get("title")) for m in adv.get("matches") or []
                    if isinstance(m, dict) and m.get("node_id")]


def collisions(records: list[dict[str, Any]],
               advisories: list[dict[str, Any]] | None = None) -> set[str]:
    """The ids whose display name another, DIFFERENT id on the page shares (case-insensitive, after
    the display sanitization of `named`), across every group and the advisories; repeated
    references to one id are not a collision."""
    by_name: dict[str, set[str]] = {}
    names = [p for rec in records for p in _subjects(rec)]
    names += [p for adv in advisories or [] for p in _advisory_names(adv)]
    for p in names:
        by_name.setdefault(p["name"].casefold(), set()).add(p["id"])
    return {i for ids in by_name.values() if len(ids) > 1 for i in ids}


def _name(p: dict[str, str] | None, clash: set[str]) -> str:
    if p is None:
        return "<em>(unnamed)</em>"
    text = f"“{_h(p['name'])}”"
    if p["id"] in clash and p["name"] != p["id"]:
        text += f" <code>{_h(p['id'])}</code>"
    return text


def sentence(rec: dict[str, Any], clash: set[str]) -> str:
    """The plain sentence for one outcome (HTML, escaped)."""
    d = rec.get("details") or {}
    t = rec.get("type")
    subj = _name(_pair(d.get("subject")) or ao.named(rec.get("review_id"), None), clash)
    before = d.get("before") if isinstance(d.get("before"), dict) else {}
    after = d.get("after") if isinstance(d.get("after"), dict) else {}
    if t == "merge_items":
        return (f"Merge {_name(_pair(d.get('absorbed')), clash)} into "
                f"{_name(_pair(d.get('survivor')) or _pair(d.get('subject')), clash)}")
    if t == "change_item_type":
        text = (f"Change type of {subj}: <code>{_h(before.get('item_type') or '?')}</code> → "
                f"<code>{_h(after.get('item_type') or '?')}</code>")
        withdraws = d.get("withdraws") if isinstance(d.get("withdraws"), list) else []
        if withdraws:                    # ADR-0070 B4: the EXACT proposals this withdraws
            text += "; withdraws the other proposal" + ("s" if len(withdraws) != 1 else "") + ": "
            text += ", ".join(f"<code>{_h(w.get('to_item_type'))}</code> "
                              f"(<a href='/ui/reviews/{_h(w.get('review_id'))}'>"
                              f"{_h(w.get('review_id'))}</a>, {_h(w.get('status'))})"
                              for w in withdraws if isinstance(w, dict))
        return text
    if t == "promote_candidate_node":
        if after.get("title") and before.get("title") and after["title"] != before["title"]:
            subj_pair = _pair(d.get("subject"))
            return (f"Promote {subj} as "
                    f"{_name(ao.named(subj_pair['id'] if subj_pair else '', after['title']), clash)}")
        return f"Promote {subj} to an active tag"
    if t == "split_item":
        docs = d.get("documents")
        moved = (f": {int(docs)} document{'s' if int(docs) != 1 else ''} move to"
                 if isinstance(docs, int) else ": documents move to")
        return f"Split {subj}{moved} {_name(_pair(d.get('spinoff')), clash)}"
    if t == "mark_semantic_duplicate":
        return f"Mark {subj} as a duplicate of {_name(_pair(d.get('duplicate_of')), clash)}"
    if t == "resolve_contradiction":
        res = d.get("resolution")
        tail = f" ({_h(res)})" if res else ""
        partner = _pair(d.get("partner"))
        return (f"Resolve the contradiction between {subj} and {_name(partner, clash)}{tail}"
                if partner else f"Resolve the contradiction on {subj}{tail}")
    if t == "propose_synthesis" and d.get("decision") == "rejected":
        return f"Discard synthesis {subj}"
    verbs = {"propose_synthesis": "Publish synthesis", "deprecate_wiki_page": "Deprecate",
             "archive_source": "Archive document", "hide_content": "Hide document",
             "unhide_content": "Unhide document", "hide_semantic_page": "Hide page",
             "unhide_semantic_page": "Unhide page", "hide_claim": "Hide claim",
             "unhide_claim": "Unhide claim", "hide_synthesis": "Hide synthesis",
             "unhide_synthesis": "Unhide synthesis"}
    return f"{_h(verbs.get(str(t), _label(t)))} {subj}"


# Plain wording for the executors' skip and incomplete codes (ADR-0072: no raw codes on screen; the
# code stays in the folded details). `review_html.refusal_text` words the tag-action refusals first.
REASONS = {
    "absorbed_merged_elsewhere": "the absorbed tag was already merged into a different tag",
    "absorbed_not_active": "the absorbed tag is not in use",
    "survivor_not_active": "the surviving tag is not in use",
    "self_merge": "both sides are the same tag",
    "merge_unverified": "the graph says merged, but the tag's page doesn't confirm it",
    "alias_not_on_primary": "a spin-off alias is not on the tag being split",
    "approved_unapplied_references_absorbed": "another approved change still refers to the absorbed "
                                              "tag; it applies first",
    "approved_unapplied_references_primary": "another approved change still refers to this tag; it "
                                             "applies first",
    "empty_partition": "no documents were chosen to move",
    "full_partition_is_rename": "every document would move, which is a rename, not a split",
    "not_independent": "the chosen documents are not independent sources",
    "source_not_mentioned": "a chosen document doesn't carry this tag",
    "spinoff_equals_primary": "the new tag would be the same tag",
    "spinoff_id_mismatch": "the new tag's id doesn't match its name",
    "target_spinoff_assertion_exists": "the new tag already has links",
    "target_spinoff_id_exists": "a tag with the new name already exists",
    "partial_split_state": "an earlier split stopped part-way and can't be resumed safely",
    "node_not_splittable": "this tag can't be split in its current status",
    "graph_item_type_mismatch": "the page has the new type but the graph doesn't",
    "item_type_missing": "no type was chosen",
    "missing_required_item_type": "the tag needs a real type before it can be kept",
    "superseded_by_merge": "a merge replaced this change",
    "superseded_by_retype": "a later type change of this tag replaced this one, so it is not "
                            "applied again",
    "competing_retypes_approved": "another approved type change of this tag is waiting too; no "
                                  "order decides between them — reopen all but one",
    "approval_not_after_latest_retype": "not provably approved after the tag's latest settled "
                                        "type choice — reopen and approve it again if it is still "
                                        "your choice",
    "execution_history_unreadable": "the tag's type-change history can't be read, so it isn't "
                                    "clear this is the latest choice — repair the audit log "
                                    "(scripts/validate_all.py), then preview again",
    "retirement_unfinished": "a retirement of this tag is unfinished",
    "node_missing": "the item no longer exists",
    "node_not_candidate": "the tag is no longer a suggestion",
    "node_not_active": "the page is not active",
    "node_not_hidden": "the page is not hidden",
    "noncanonical_node_id": "the review names an invalid id",
    "noncanonical_source_id": "the review names an invalid document id",
    "invalid_subject": "the review's subject is invalid",
    "invalid_proposal": "the review's proposal is invalid",
    "invalid_page_path": "the review names an invalid page",
    "missing_subject": "the review names no subject",
    "out_of_scope": "this review type doesn't apply to that kind of page",
    "page_missing": "its page is missing",
    "page_not_active": "its page doesn't show it as active",
    "page_node_mismatch": "the page and the graph disagree",
    "page_status_mismatch": "the page and the graph disagree on its status",
    "page_manifest_drift": "the document's page and its manifest disagree on its status",
    "partial_hide_state": "the page and the graph disagree on whether it is hidden",
    "partial_unhide_state": "the page and the graph disagree on whether it is hidden",
    "review_id_mismatch": "the approval file doesn't match the tag",
    "graph_missing": "there is no graph",
    "type_mismatch": "the two pages are of different kinds",
    "unexpected_to_status": "the review asks for an unexpected status",
    "visibility_suppressed": "it is hidden, so it is left as it is",
    "edge_missing": "the contradiction link no longer exists",
    # ADR-0073 decisions 9–17: the guarded supersede. Reason-specific; none assumes a cause.
    "contradiction_retired": "this contradiction no longer stands (it was retired), so the "
                             "decision has nothing to act on",
    "winner_not_active": "the claim chosen to win is no longer active",
    "loser_not_active": "the claim to be superseded is no longer active",
    "claim_page_unreadable": "a claim's page can't be read",
    "approval_unbound": "this approval was recorded before approvals carried an id; reopen it and "
                        "approve it again",
    "supersedes_edge_foreign": "a supersede link between these claims belongs to another decision",
    "supersede_withdrawn": "this approval was withdrawn after an incomplete supersede; reopen it "
                           "to decide again",
    "partial_supersede_withdrawn": "the supersede couldn't be finished, so the part already made "
                                   "was withdrawn; reopen the review to decide again",
    "supersede_in_flight": "an earlier run of this supersede is unfinished",
    "supersede_journal_unreadable": "the saved progress of this supersede can't be read",
    "supersede_journal_invalid": "the saved progress of this supersede doesn't match the approval",
    "supersede_state_unverified": "the claims and the supersede link don't match a known state, so "
                                  "nothing was changed",
    "loser_deprecated_unmarked": "the claim to be superseded is already deprecated, but nothing "
                                 "records that this decision did it",
    "completion_evidence_found": "the supersede is recorded as finished, so it isn't withdrawn",
    "rollback_evidence_unreadable": "a record needed to check the supersede can't be read, so "
                                    "nothing was withdrawn",
    "completion_audit_conflict": "the supersede's completion record doesn't match this decision",
    "rollback_audit_conflict": "the supersede's withdrawal record doesn't match this decision",
    "marker_unreadable": "a claim page's supersede record is damaged",
    "marker_conflict": "the claim page already records another supersede",
    "identity_operation_in_flight": "another unfinished change involves this item",
    "edge_status_unexpected": "the contradiction link is in an unexpected state",
    "evidence_changed": "the evidence changed since the review was filed",
    "evidence_gone": "the evidence no longer exists",
    "synthesis_artifact_missing": "the synthesis text is missing",
    "synthesis_artifact_invalid": "the synthesis text is malformed",
    "synthesis_digest_mismatch": "the synthesis text doesn't match its recorded version",
    "synthesis_version_replaced": "the decision was for an earlier version of the synthesis",
    "decision_unbound": "the decision was recorded before decisions named a version; open the "
                        "review and use Decide again for the version shown",
    "synthesis_artifact_unreadable": "the synthesis text can't be read",
    "synthesis_artifact_mismatch": "the synthesis text doesn't match the page",
    "synthesis_not_active": "the synthesis is not active",
    "synthesis_topic_mismatch": "the synthesis belongs to a different topic",
    "source_missing": "the document no longer exists",
    "source_not_active": "the document is not active",
    "source_not_hidden": "the document is not hidden",
    "source_page_refresh_failed": "the document's status changed but its page couldn't be "
                                  "refreshed",
    "tag_ledger_corrupt": "a document's tag decisions can't be read",
    "not_approved": "the review is not approved",
    "no_executor_in_phase_6": "this kind of review has no automatic step yet",
    "graph_unavailable": "the graph is unavailable",
    "page_conflict": "its page file belongs to another tag, or its owner can't be established; "
                     "see scripts/validate_graph.py",
    ao.HIDDEN_TAKES_PRECEDENCE: "it is hidden, and hidden wins over this change",
    ao.POSTCONDITION_NOT_MET: "the expected result is not in place",
    ao.VERIFICATION_UNAVAILABLE: "the result couldn't be checked (a page or record couldn't be read)",
}

# What to do about an `incomplete` review (B2a review round 1): Apply resumes AUTHORITY work (a saved
# identity operation); Retry publishing repairs PROJECTIONS (pages, index, search). Nothing here
# promises a recovery the system doesn't have.
_RESUMED_BY_APPLY = ("Apply again: it resumes the saved change.")
RECOVERY = {
    "source_page_refresh_failed": "The status change is saved; only the document's page is out of "
                                  "date. Retry publishing repairs the page.",
    ao.HIDDEN_TAKES_PRECEDENCE: "Nothing more happens on its own: unhide it first, or reject this "
                                "review.",
    ao.POSTCONDITION_NOT_MET: "Open the review and the details before applying again.",
    ao.VERIFICATION_UNAVAILABLE: "Repair the unreadable page or record (scripts/validate_all.py "
                                 "names it), then preview again.",
    "partial_split_state": "An earlier split stopped part-way and can't be resumed automatically; "
                           "open the review.",
    "partial_supersede_withdrawn": "Nothing more happens on its own: reopen the review to decide "
                                   "again.",
    "marker_unreadable": "Repair the damaged claim page (scripts/validate_all.py names it), then "
                         "apply again.",
}


def recovery_text(rec: dict[str, Any]) -> str:
    """The next step for one `incomplete` outcome, by its reason (and journal stage)."""
    reason = str(rec.get("reason") or "")
    if reason in RECOVERY:
        return RECOVERY[reason]
    if (rec.get("details") or {}).get("stage"):          # an identity operation's saved journal
        return _RESUMED_BY_APPLY
    return "See the details; it does not resume on its own."


def reason_text(reason: Any, info: dict[str, Any]) -> str:
    """Plain words for a skip/incomplete code; never the bare code."""
    from app.backend.review_html import refusal_text
    code = str(reason or "")
    if code == "identity_operation_in_flight" and info.get("blocking_review_id"):
        # ADR-0073 decision 14: claim governance waits on an unfinished supersede (or any
        # unreadable journal); the decision stays recorded and runs after it.
        return (f"another change involving this claim (review {info['blocking_review_id']}) is "
                "unfinished, so this one waits; it runs on a later Apply once that one finished")
    worded = refusal_text(code, info)
    if worded and worded != code:
        return worded
    return REASONS.get(code, "it couldn't be applied (the reason is in the details)")


def _caveat(rec: dict[str, Any]) -> str:
    """A note when part of the outcome couldn't be read (B2a review round 1): the sentence stands,
    but some detail or the in-flight check is unknown, never guessed."""
    d = rec.get("details") or {}
    if d.get("unavailable") or d.get("in_flight") == ao.VERIFICATION_UNAVAILABLE:
        return " <span class='help'>(some details couldn't be read — see the details)</span>"
    return ""


def _link(rid: Any) -> str:
    return f"<a href='/ui/reviews/{_h(rid)}'>review</a>"


def _tech(obj: Any) -> str:
    from app.backend.review_html import _render_value
    return f"<details><summary>details</summary>{_render_value(obj)}</details>"


def model(handled: list[dict[str, Any]], reported: list[dict[str, Any]],
          not_appliable: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Group a run's outcomes: `{changes, in_effect, information, blocked, incomplete,
    not_reported}`. A skip flagged `information` (ADR-0073: a decision on a replaced synthesis
    version) needs nothing from anyone, so it is listed once here, not under Attention needed."""
    rec = ao.reconcile(handled, reported)
    groups: dict[str, list[dict[str, Any]]] = {"changes": [], "in_effect": [], "information": [],
                                               "blocked": [], "incomplete": []}
    for r in rec["outcomes"]:
        kind = r.get("outcome")
        key = {ao.APPLIED: "changes", ao.ALREADY_IN_EFFECT: "in_effect", ao.SKIPPED: "blocked",
               ao.INCOMPLETE: "incomplete"}.get(kind)
        if key is None:                                   # a malformed report is a gap, not a guess
            rec["gaps"].append({"review_id": r.get("review_id"), "type": r.get("type"),
                                "gap": "malformed_outcome", "reports": [r]})
            continue
        if key == "blocked" and (r.get("details") or {}).get("information") is True:
            key = "information"
        groups[key].append(r)
    reported_ids = {str(r.get("review_id")) for r in reported}
    for na in not_appliable or []:                        # no executor: blocked, with its reason
        if str(na.get("review_id")) not in reported_ids:
            groups["blocked"].append(ao.outcome(str(na.get("review_id")), str(na.get("type")),
                                                ao.SKIPPED, reason=str(na.get("reason") or
                                                                       "not_appliable")))
    groups["not_reported"] = rec["gaps"]
    return groups


def render(groups: dict[str, Any], *, advisories: list[dict[str, Any]] | None = None,
           preview: bool) -> str:
    """The readable section of the preview (`preview=True`) or result page."""
    every = [r for k in ("changes", "in_effect", "information", "blocked", "incomplete")
             for r in groups.get(k, [])]
    clash = collisions(every, advisories)
    parts = []
    changes = groups["changes"]
    parts.append(f"<h2>{'Changes this Apply would make' if preview else 'Changes made'}</h2>")
    if changes:
        for t in sorted({r.get("type") for r in changes},
                        key=lambda x: (_ORDER.index(x) if x in _ORDER else len(_ORDER), str(x))):
            rows = [r for r in changes if r.get("type") == t]
            parts.append(f"<h3>{_h(_label(t))}</h3><ul>" + "".join(
                f"<li>{sentence(r, clash)}{_caveat(r)} {_link(r.get('review_id'))} {_tech(r)}</li>"
                for r in rows) + "</ul>")
    elif not any(groups.get(k) for k in ("in_effect", "information", "blocked", "incomplete",
                                         "not_reported")):
        parts.append("<p><em>Nothing " + ("would change" if preview else "was changed")
                     + ": there were no approved items to apply.</em></p>")
    else:
        parts.append("<p><em>No changes.</em></p>")
    if groups["in_effect"]:
        n = len(groups["in_effect"])
        parts.append(f"<p>{n} decided review{'s' if n != 1 else ''} already in effect — nothing "
                     f"more to do. {_tech(groups['in_effect'])}</p>")
    if groups.get("information"):
        rows = "".join(f"<li>{sentence(r, clash)}: "
                       f"{_h(reason_text(r.get('reason'), r.get('details') or {}))}. "
                       f"{_link(r.get('review_id'))} {_tech(r)}</li>"
                       for r in groups["information"])
        parts.append("<h2>Information</h2><p class='help'>Nothing to do — these decisions no "
                     "longer have anything to act on (they are for a version that has since been "
                     "replaced, or the contradiction they resolved no longer stands), so Apply "
                     "leaves them as they are. They are kept as a record.</p><ul>" + rows
                     + "</ul>")
    attention = []
    for r in groups["blocked"]:
        d = r.get("details") or {}
        info = {**d, **(d.get("problem") if isinstance(d.get("problem"), dict) else {})}
        attention.append(f"<li><strong>Blocked</strong> (nothing changed) — {sentence(r, clash)}: "
                         f"{_h(reason_text(r.get('reason'), info))}{_caveat(r)} "
                         f"{_link(r.get('review_id'))}"
                         f" {_tech(r)}</li>")
    for r in groups["incomplete"]:
        attention.append(f"<li><strong>Incomplete</strong> (some changes were made) — "
                         f"{sentence(r, clash)}: "
                         f"{_h(reason_text(r.get('reason'), r.get('details') or {}))}. "
                         f"{_h(recovery_text(r))} {_link(r.get('review_id'))} {_tech(r)}</li>")
    for g in groups["not_reported"]:
        attention.append(f"<li><strong>Outcome not reported</strong> (the result is uncertain) — "
                         f"{_h(_label(g.get('type')))}, review <code>{_h(g.get('review_id'))}</code>"
                         f" {_link(g.get('review_id'))} {_tech(g)}</li>")
    if attention:
        parts.append("<h2>Attention needed</h2><ul>" + "".join(attention) + "</ul>")
    if advisories:
        rows = []
        for a in advisories:
            new = _name(ao.named(a.get("node_id"), a.get("name")), clash)
            similar = ", ".join(_name(ao.named(m.get("node_id"), m.get("title")), clash)
                                + (f" (matched its alias “{_h(m['matched'])}”)"
                                   if m.get("matched") and m.get("matched") != m.get("title") else "")
                                for m in a.get("matches") or [] if isinstance(m, dict))
            link = f" {_link(a.get('review_id'))}" if a.get("review_id") else ""
            rows.append(f"<li>{_h(_label(a.get('type') or ''))}: {new} is similar to the existing "
                        f"tag {similar or '—'}. If they are the same, merge them.{link}</li>")
        parts.append("<h2>Advisories</h2><p class='help'>Information only — these changes were not "
                     "blocked.</p><ul>" + "".join(rows) + "</ul>")
    return "".join(parts)
