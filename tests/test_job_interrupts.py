"""UAT part 3 finding #3 (2026-11-02): a producer interrupted with Ctrl-C records its job as failed
("interrupted") instead of leaving it `running`."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import db  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.workers import enrich, extract, intake  # noqa: E402
from tests import fixtures  # noqa: E402


def _jobs(root):
    conn = db.connect(root / "db" / "jobs.sqlite")
    try:
        return db.list_jobs(conn)
    finally:
        conn.close()


def test_failure_message_names_an_interruption():
    assert db.failure_message(ValueError("bad input")) == "bad input"
    assert db.failure_message(KeyboardInterrupt()) == "interrupted (KeyboardInterrupt)"
    assert db.failure_message(SystemExit(2)) == "interrupted (SystemExit)"


def test_an_interrupted_intake_is_recorded_as_failed(tmp_path, monkeypatch):
    (tmp_path / "raw" / "inbox").mkdir(parents=True)
    (tmp_path / "raw" / "inbox" / "a.md").write_text("# A\n\nbody\n", encoding="utf-8")

    def ctrl_c(*_a, **_k):
        raise KeyboardInterrupt
    monkeypatch.setattr(intake, "_new_manifest", ctrl_c)
    with pytest.raises(KeyboardInterrupt):
        intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    (job,) = _jobs(tmp_path)
    assert job["status"] == "failed" and job["error_message"] == "interrupted (KeyboardInterrupt)"
    assert job["finished_at"]


class _Interrupting:
    name = "anthropic"
    supports_batch = False

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        raise KeyboardInterrupt


def test_an_interrupted_enrich_is_recorded_as_failed(tmp_path):
    (tmp_path / "raw" / "inbox").mkdir(parents=True)
    fixtures.copy_templates(tmp_path)
    (tmp_path / "raw" / "inbox" / "a.md").write_text("# A\n\nA body.\n", encoding="utf-8")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    with pytest.raises(KeyboardInterrupt):
        enrich.enrich_sources(tmp_path, client=LLMClient({"anthropic": _Interrupting()}),
                              model_ref="anthropic:claude-haiku-4-5",
                              jobs_db=tmp_path / "db" / "jobs.sqlite")
    statuses = {j["job_type"]: (j["status"], j["error_message"]) for j in _jobs(tmp_path)}
    assert statuses["enrich"] == ("failed", "interrupted (KeyboardInterrupt)")
    assert all(status != "running" for status, _ in statuses.values())


def test_every_producer_failure_handler_also_catches_an_interruption():
    """Structural (review 2026-11-02): any handler in app/workers that marks a job `failed` must catch
    BaseException, so Ctrl-C never leaves the job `running`."""
    import ast
    handlers = []
    for path in sorted((ROOT / "app" / "workers").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ExceptHandler):
                continue
            marks_failed = any(
                isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "update_job"
                and any(k.arg == "status" and getattr(k.value, "value", None) == "failed"
                        for k in c.keywords)
                for c in ast.walk(node))
            if marks_failed:
                handlers.append((path.name, node.lineno, getattr(node.type, "id", None)))
    assert len(handlers) >= 13, handlers                # 11 producers (retention has 2) + checks
    assert all(kind == "BaseException" for _f, _l, kind in handlers), handlers
