"""B2a review round 1 — a failed Source-page refresh inside Apply is retried by Apply's publication.

A document status change (here `hide_content`) writes the manifest, then re-renders the Source page.
If that render fails, the outcome is `incomplete` / `source_page_refresh_failed`; Apply's own
publication now retries the page (even when the page file is missing), and the displayed outcome is
reconciled once publication rendered it. If publication fails too, the page stays queued for Retry
publishing, and after the retry the next Apply finds the review already in effect.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import publication_queue  # noqa: E402
from app.workers import publication, retention, wiki  # noqa: E402
from app.workers.wiki_render import parse_frontmatter  # noqa: E402
from tests import fixtures  # noqa: E402
from tests.test_hide_content import SID, _approve_hide, _graph_source_node, _rendered_source  # noqa: E402
from tests.test_hide_content import client  # noqa: E402,F401


def _fail_renders(monkeypatch, *, only_from_executor: bool):
    """Make generate_wiki report a page error for SID — from the retention executor only, or from
    every caller (the executor AND publication)."""
    real = wiki.generate_wiki

    def flaky(root, *a, **kw):
        caller = inspect.stack()[1].filename
        if (not only_from_executor or caller.endswith("retention.py")) and \
                SID in (kw.get("source_ids") or []):
            return {"generated": 0, "errors": 1, "error_details": [
                {"source_id": SID, "error": "OSError: disk full"}]}
        return real(root, *a, **kw)
    monkeypatch.setattr(wiki, "generate_wiki", flaky)
    return real


def _page_status(tmp_path):
    page = tmp_path / "wiki" / "Sources" / f"{SID}.md"
    return parse_frontmatter(page.read_text(encoding="utf-8")).get("status") if page.exists() else None


def _established(tmp_path):
    """A vault published once, so the tag-label snapshot exists: an unknown mapping would otherwise
    force a full Source refresh and hide a missing retry (review round 2)."""
    _rendered_source(tmp_path)
    _graph_source_node(tmp_path)
    assert publication.republish_all(tmp_path)["status"] == publication.PUBLISHED
    assert (tmp_path / "db" / "tag_labels.json").exists()
    _approve_hide(tmp_path)


def _queued_sources(tmp_path):
    return (publication_queue.outstanding(publication_queue.load(tmp_path)) or {}).get("sources", [])


def _outcome(body):
    (rec,) = [o for o in body["outcomes"] if o["review_id"] == "rev_hide"]
    return rec


def test_publication_retries_the_page_and_the_outcome_is_reconciled(client, tmp_path, monkeypatch):  # noqa: F811
    _established(tmp_path)
    (tmp_path / "wiki" / "Sources" / f"{SID}.md").unlink()     # missing page: still retried
    _fail_renders(monkeypatch, only_from_executor=True)
    body = fixtures.apply(client).json()
    rec = _outcome(body)
    assert rec["outcome"] == "applied" and rec["details"]["page_refreshed_by_publication"] is True
    assert "reason" not in rec
    assert _page_status(tmp_path) == "hidden"
    assert body["publication"]["status"] == publication.PUBLISHED


def test_a_refresh_that_fails_again_stays_queued_until_retry_publishing(client, tmp_path, monkeypatch):  # noqa: F811
    _established(tmp_path)
    real = _fail_renders(monkeypatch, only_from_executor=False)
    body = fixtures.apply(client).json()
    rec = _outcome(body)
    assert rec["outcome"] == "incomplete" and rec["reason"] == "source_page_refresh_failed"
    assert body["status"] == "saved_publication_pending"
    assert SID in _queued_sources(tmp_path)
    assert _page_status(tmp_path) == "active"                   # the page is stale, and says so
    monkeypatch.setattr(wiki, "generate_wiki", real)         # the cause is fixed
    assert publication.publish(tmp_path)["status"] == publication.PUBLISHED   # Retry publishing
    assert _page_status(tmp_path) == "hidden"
    again = client.post("/reviews/apply/dry-run").json()
    assert _outcome(again)["outcome"] == "already_in_effect"


def test_a_reporting_failure_never_drops_the_retry(client, tmp_path, monkeypatch):  # noqa: F811
    """The executor's render fails AND its reporting-only manifest read-back fails: the outcome is
    `verification_unavailable` (unchanged, honest), yet the page is still published by Apply — the
    retry comes from the executor's execution list, not from the outcome's reason."""
    _established(tmp_path)
    real = _fail_renders(monkeypatch, only_from_executor=False)
    real_load = retention.load_manifest

    def load(md, sid):
        if inspect.stack()[1].function == "guarded":           # the reporting read only
            raise OSError("read failed")
        return real_load(md, sid)
    monkeypatch.setattr(retention, "load_manifest", load)
    body = fixtures.apply(client).json()
    rec = _outcome(body)
    assert (rec["outcome"], rec["reason"]) == ("incomplete", "verification_unavailable")
    assert _page_status(tmp_path) == "active" and SID in _queued_sources(tmp_path)  # kept as work
    monkeypatch.setattr(wiki, "generate_wiki", real)
    monkeypatch.setattr(retention, "load_manifest", real_load)
    assert publication.publish(tmp_path)["status"] == publication.PUBLISHED   # Retry publishing
    assert _page_status(tmp_path) == "hidden" and _queued_sources(tmp_path) == []


def test_the_same_retry_publishes_when_only_the_executor_render_failed(client, tmp_path,  # noqa: F811
                                                                     monkeypatch):
    _established(tmp_path)
    _fail_renders(monkeypatch, only_from_executor=True)
    _real_load = retention.load_manifest

    def load(md, sid):
        if inspect.stack()[1].function == "guarded":
            raise OSError("read failed")
        return _real_load(md, sid)
    monkeypatch.setattr(retention, "load_manifest", load)
    body = fixtures.apply(client).json()
    assert body["publication"]["status"] == publication.PUBLISHED
    assert _page_status(tmp_path) == "hidden" and _queued_sources(tmp_path) == []
    assert _outcome(body)["reason"] == "verification_unavailable"   # honest: the read still failed
