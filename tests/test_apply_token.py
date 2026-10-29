"""ADR-0068 decision 6 (slice 4): the vault revision, consistent previews and the preview token.

Covers conservative invalidation (before writes, nested holds, a killed writer, persistence
failure), the self-healing revision file, Apply authorizations (only `ok` previews, bound inputs,
single use, restart survival, store failures), the snapshot's lock split (raw, code and revision
changes during a preview), the API/UI/CLI entry points and the restore epoch.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from fastapi.testclient import TestClient  # noqa: E402

import apply_reviews  # noqa: E402
import backup  # noqa: E402
import validate_revision  # noqa: E402
from app.backend import (  # noqa: E402
    apply_authorization, apply_gate, apply_sandbox, revision, write_lock,
)
from app.backend import main as main_module  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from tests import fixtures  # noqa: E402
from tests.test_apply_dry_run import (  # noqa: E402
    _approve_archive, _graph_source_node, _rendered_source,
)

SID = "src_0000000000000a11"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A vault whose Apply preview is clean: one approved archive of a rendered source."""
    st = get_settings(tmp_path)
    st.reviews_dir.mkdir(parents=True, exist_ok=True)
    _rendered_source(tmp_path, SID)
    _graph_source_node(tmp_path, SID)
    _approve_archive(tmp_path, SID)
    monkeypatch.setattr(main_module, "settings", st)
    return st


@pytest.fixture
def client(vault):
    return TestClient(main_module.app)


def _rev(root):
    with write_lock.hold(Path(root) / "reviews", authority=False, changes=False):
        return revision.current(root)


def _preview(st, **kw):
    return apply_gate.preview(st, evaluate=main_module._evaluate_preview, **kw)


def _apply(st, token, **kw):
    return apply_gate.apply(st, token, run_apply=main_module.run_apply, **kw)


def _records(root):
    path = apply_authorization.store_path(root)
    return json.loads(path.read_text())["records"] if path.exists() else []


def _archived(root) -> bool:
    m = json.loads((Path(root) / "raw" / "manifests" / f"{SID}.json").read_text())
    return m.get("status") == "archive_candidate"


# --- the revision: conservative invalidation (Q1) ---------------------------------------------


def test_every_mutating_hold_advances_the_revision_before_it_yields(tmp_path):
    before = _rev(tmp_path)
    seen = []
    with write_lock.hold(tmp_path / "reviews"):
        seen.append(json.loads(revision.revision_path(tmp_path).read_text())["counter"])
        with write_lock.hold(tmp_path / "reviews"):         # one increment per outermost hold
            pass
    assert seen == [before["counter"] + 1]                  # durable before the holder ran
    assert _rev(tmp_path)["counter"] == before["counter"] + 1


def test_a_read_only_hold_leaves_the_revision_alone(tmp_path):
    before = _rev(tmp_path)
    with write_lock.hold(tmp_path / "reviews", changes=False):
        pass
    assert _rev(tmp_path) == before


def test_a_mutating_hold_nested_in_a_read_only_one_invalidates(tmp_path):
    before = _rev(tmp_path)
    with write_lock.hold(tmp_path / "reviews", changes=False):
        with write_lock.hold(tmp_path / "reviews"):
            pass
        with write_lock.hold(tmp_path / "reviews"):
            pass
    assert _rev(tmp_path)["counter"] == before["counter"] + 1


def test_a_writer_killed_after_an_authority_write_leaves_the_old_token_stale(vault):
    token = _preview(vault)["preview_token"]
    assert token
    # Another process takes the lock, writes, and dies without running any `finally`.
    code = ("import os, sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path;"
            "from app.backend import write_lock\n"
            "with write_lock.hold(Path(sys.argv[2]) / 'reviews'):\n"
            "    (Path(sys.argv[2]) / 'reviews' / 'approved' / 'x.txt').write_text('w')\n"
            "    os._exit(1)\n")
    assert subprocess.run([sys.executable, "-c", code, str(ROOT), str(vault.root)]).returncode == 1
    with pytest.raises(apply_gate.ApplyRefused) as exc:
        _apply(vault, token)
    assert exc.value.reason == "stale_preview" and not _archived(vault.root)


def test_failing_to_persist_the_increment_refuses_the_write(tmp_path, monkeypatch):
    _rev(tmp_path)
    monkeypatch.setattr(revision, "_write_durably",
                        lambda *a: (_ for _ in ()).throw(OSError(28, "No space left")))
    ran = []
    with pytest.raises(revision.RevisionUnavailable):
        with write_lock.hold(tmp_path / "reviews"):
            ran.append(True)
    assert ran == []


def test_the_api_refuses_a_change_when_the_revision_cannot_advance(client, vault, monkeypatch):
    monkeypatch.setattr(revision, "_write_durably",
                        lambda *a: (_ for _ in ()).throw(OSError(28, "No space left")))
    resp = client.post("/ui/reviews/apply/preview")       # the snapshot only reads: still works
    assert resp.status_code == 200
    resp = client.post("/reviews/rev_arch/reopen", json={"reason": "changed my mind"})
    assert resp.status_code == 503 and "revision_unavailable" in resp.text


# --- the revision file self-heals (Q2) --------------------------------------------------------


def test_concurrent_processes_initialize_one_epoch(tmp_path):
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path;"
            "from app.backend import revision, write_lock\n"
            "with write_lock.hold(Path(sys.argv[2]) / 'reviews', changes=False):\n"
            "    print(revision.current(Path(sys.argv[2]))['epoch'])\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(tmp_path)],
                              stdout=subprocess.PIPE, text=True) for _ in range(4)]
    epochs = {p.communicate()[0].strip() for p in procs}
    assert len(epochs) == 1 and revision._EPOCH.fullmatch(epochs.pop())


def test_a_malformed_revision_recovers_to_a_new_epoch_that_refuses_old_tokens(vault):
    token = _preview(vault)["preview_token"]
    old = _rev(vault.root)
    revision.revision_path(vault.root).write_text("{not json", encoding="utf-8")
    with pytest.raises(apply_gate.ApplyRefused) as exc:
        _apply(vault, token)
    assert exc.value.reason == "stale_preview" and not _archived(vault.root)
    (copy,) = revision.damaged_copies(vault.root)
    assert copy.read_text() == "{not json"                 # the evidence is kept
    assert _rev(vault.root)["epoch"] != old["epoch"]
    out = _preview(vault)                                 # a visible warning, not a blocker
    assert out["status"] == "ok" and out["preview_token"]
    assert "revision_recovered" in out["warnings"]
    assert validate_revision.main([str(vault.root)]) == 0  # reported, never a failure


@pytest.mark.parametrize("fail_at", ["preserve", "replace"])
def test_a_crash_during_recovery_keeps_the_damaged_file_or_its_copy(tmp_path, monkeypatch,
                                                                    fail_at):
    path = revision.revision_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("garbage", encoding="utf-8")
    real = revision._write_durably
    calls = []

    def flaky(target, data):
        calls.append(target)
        if (fail_at == "preserve" and len(calls) == 1) or (fail_at == "replace" and len(calls) == 2):
            raise KeyboardInterrupt                         # killed at this step
        real(target, data)
    monkeypatch.setattr(revision, "_write_durably", flaky)
    with pytest.raises(KeyboardInterrupt):
        with write_lock.hold(tmp_path / "reviews", changes=False):
            revision.current(tmp_path)
    monkeypatch.undo()
    assert path.read_text() == "garbage"                  # the active file is untouched
    if fail_at == "replace":
        assert [p.read_text() for p in revision.damaged_copies(tmp_path)] == ["garbage"]
    rev = _rev(tmp_path)                                  # the next hold finishes the recovery
    assert rev["counter"] == 0 and revision._EPOCH.fullmatch(rev["epoch"])


def test_an_io_failure_reading_the_revision_is_not_malformed_contents(tmp_path):
    _rev(tmp_path)
    path = revision.revision_path(tmp_path)
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running with privileges that ignore file permissions")
        with pytest.raises(revision.RevisionUnavailable):
            with write_lock.hold(tmp_path / "reviews"):
                pass
        assert revision.damaged_copies(tmp_path) == []
    finally:
        path.chmod(0o644)


def test_restore_starts_a_new_epoch_that_refuses_earlier_tokens(vault):
    archive = backup.create_backup(vault.root)
    token = _preview(vault)["preview_token"]
    before = _rev(vault.root)
    backup.restore_backup(archive, vault.root, force=True)
    assert _rev(vault.root)["epoch"] != before["epoch"]
    with pytest.raises(apply_gate.ApplyRefused) as exc:
        _apply(vault, token)
    assert exc.value.reason == "unknown_preview"              # restore also drops every record


def test_backups_never_carry_apply_authorizations(vault):
    import zipfile
    assert _preview(vault)["preview_token"]
    before = _rev(vault.root)
    archive = backup.create_backup(vault.root)
    assert _rev(vault.root) == before                      # a backup changes nothing
    names = zipfile.ZipFile(archive).namelist()
    assert "db/revision.json" in names and "db/apply_authorizations.json" not in names


# --- authorizations: only ok previews, bound inputs, single use (Q3/Q4) ------------------------


def test_a_clean_preview_authorizes_exactly_one_apply(vault):
    out = _preview(vault)
    assert out["status"] == "ok" and apply_authorization.TOKEN.fullmatch(out["preview_token"])
    result = _apply(vault, out["preview_token"])
    assert result["status"] == "applied" and _archived(vault.root)
    assert _records(vault.root) == []                      # consumed
    with pytest.raises(apply_gate.ApplyRefused) as exc:
        _apply(vault, out["preview_token"])
    assert exc.value.reason == "unknown_preview"


def test_a_non_clean_preview_gets_no_token_and_records_nothing(vault, monkeypatch):
    monkeypatch.setattr(main_module, "run_apply", lambda st: {
        "status": "validation_failed", "validators_ok": False, "failed_validators": [],
        "warnings": [], "summary": {}})
    out = _preview(vault)
    assert out["status"] == "validation_failed" and out["preview_token"] is None
    assert _records(vault.root) == []


def test_apply_without_a_preview_is_refused_and_writes_nothing(client, vault):
    before = _rev(vault.root)
    for body, status, reason in ((None, 422, "preview_required"), ({}, 422, "preview_required"),
                                 ({"preview_token": "apa_" + "0" * 32}, 409, "unknown_preview"),
                                 ({"preview_token": "made-up"}, 409, "unknown_preview")):
        resp = client.post("/reviews/apply", json=body) if body is not None else \
            client.post("/reviews/apply")
        assert resp.status_code == status and resp.json()["detail"]["reason"] == reason
        assert resp.json()["detail"]["action"]
    assert not _archived(vault.root) and _rev(vault.root) == before


def test_changed_options_or_settings_make_the_token_stale(vault):
    token = _preview(vault)["preview_token"]
    with pytest.raises(apply_gate.ApplyRefused) as exc:
        _apply(vault, token, options={"only": ["rev_arch"]})
    assert exc.value.reason == "stale_preview"
    changed = dataclasses.replace(vault, wiki_summary_max_chars=vault.wiki_summary_max_chars + 1)
    with pytest.raises(apply_gate.ApplyRefused) as exc:
        _apply(changed, token)
    assert exc.value.reason == "stale_preview"
    assert _apply(vault, token)["status"] == "applied"     # refusals consumed nothing


def test_settings_apply_never_reads_do_not_bind_the_token(vault):
    # The API and a shell may differ in serving or model settings; Apply is key-free and doesn't serve.
    token = _preview(vault)["preview_token"]
    other = dataclasses.replace(vault, enrich_model_heavy="x:y", anthropic_api_key="sk-other")
    assert _apply(other, token)["status"] == "applied"


def test_a_record_still_present_is_stale_and_a_pruned_one_is_unknown(vault):
    first = _preview(vault)["preview_token"]
    with write_lock.hold(vault.reviews_dir):               # any authority change
        pass
    with pytest.raises(apply_gate.ApplyRefused) as exc:
        _apply(vault, first)
    assert exc.value.reason == "stale_preview"             # its record remains
    second = _preview(vault)["preview_token"]              # this write prunes the stale record
    assert [r["token"] for r in _records(vault.root)] == [second]
    with pytest.raises(apply_gate.ApplyRefused) as exc:
        _apply(vault, first)
    assert exc.value.reason == "unknown_preview"


def test_concurrent_submissions_of_one_token_let_at_most_one_apply(vault, monkeypatch):
    token = _preview(vault)["preview_token"]
    started = threading.Barrier(2)
    outcomes = []

    def submit():
        started.wait()
        try:
            outcomes.append(_apply(vault, token)["status"])
        except apply_gate.ApplyRefused as exc:
            outcomes.append(exc.reason)
    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["applied", "unknown_preview"]


def test_authorizations_survive_a_restart(vault):
    token = _preview(vault)["preview_token"]
    # A fresh process (the CLI, or the API after a restart) finds the record on disk.
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "apply_reviews.py"), "apply",
                           "--token", token, str(vault.root)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "Apply: applied" in proc.stdout and _archived(vault.root)


def test_a_revision_change_during_the_sandbox_run_withholds_the_token(vault):
    def evaluate(sandbox):
        with write_lock.hold(vault.reviews_dir):             # a keep/drop while the copy runs
            pass
        return main_module._evaluate_preview(sandbox)
    out = apply_gate.preview(vault, evaluate=evaluate)
    assert out["status"] == "ok" and out["preview_token"] is None
    assert out["token_withheld"] == "revision_changed" and _records(vault.root) == []


def test_an_authorization_write_failure_withholds_the_token(vault, monkeypatch):
    monkeypatch.setattr(apply_authorization, "_write_durably",
                        lambda *a: (_ for _ in ()).throw(OSError(28, "No space left")))
    out = _preview(vault)
    assert out["status"] == "ok" and out["preview_token"] is None
    assert out["token_withheld"] == "authorization_write_failed"


def test_a_crash_after_consuming_the_token_needs_another_preview(vault, monkeypatch):
    token = _preview(vault)["preview_token"]
    before = _rev(vault.root)

    def killed(_st):
        raise KeyboardInterrupt                            # dies before the first authority write
    with pytest.raises(KeyboardInterrupt):
        apply_gate.apply(vault, token, run_apply=killed)
    assert _rev(vault.root)["counter"] == before["counter"] + 1   # spent: the revision moved
    with pytest.raises(apply_gate.ApplyRefused) as exc:
        _apply(vault, token)
    assert exc.value.reason == "unknown_preview" and not _archived(vault.root)
    assert _apply(vault, _preview(vault)["preview_token"])["status"] == "applied"


def test_a_malformed_store_refuses_outstanding_tokens_as_unknown(vault):
    token = _preview(vault)["preview_token"]
    apply_authorization.store_path(vault.root).write_text("[", encoding="utf-8")
    with pytest.raises(apply_gate.ApplyRefused) as exc:
        _apply(vault, token)
    assert exc.value.reason == "unknown_preview"
    assert [p.read_text() for p in apply_authorization.damaged_copies(vault.root)] == ["["]
    assert _apply(vault, _preview(vault)["preview_token"])["status"] == "applied"


def test_an_unreadable_store_is_unavailable_not_unknown(client, vault, monkeypatch):
    token = _preview(vault)["preview_token"]
    real = Path.read_bytes

    def denied(self):
        if self.name == apply_authorization.STORE_NAME:
            raise PermissionError(13, "Permission denied")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", denied)
    resp = client.post("/reviews/apply", json={"preview_token": token})
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "authorization_store_unavailable"
    monkeypatch.undo()
    assert not _archived(vault.root) and _records(vault.root)   # nothing consumed


# --- the snapshot's lock split and the code fingerprint (Q5) ---------------------------------


def test_raw_bytes_come_from_the_copied_manifests_and_a_deletion_meanwhile_withholds(vault,
                                                                                    monkeypatch):
    real = apply_sandbox.copy_code_and_raw

    def deleted_meanwhile(st, tmp_root):
        with write_lock.hold(st.reviews_dir):             # an approved raw deletion, say
            (st.root / "raw" / "inbox" / f"{SID}.md").unlink()
        real(st, tmp_root)
    monkeypatch.setattr(apply_sandbox, "copy_code_and_raw", deleted_meanwhile)
    out = _preview(vault)
    assert out["preview_token"] is None and out["token_withheld"] == "revision_changed"


def test_a_code_change_during_the_preview_withholds_the_token(vault, monkeypatch):
    code_file = vault.root / "app" / "x.py"
    code_file.parent.mkdir()
    code_file.write_text("A = 1\n")
    real = apply_sandbox.copy_code_and_raw

    def edited_meanwhile(st, tmp_root):
        real(st, tmp_root)
        code_file.write_text("A = 2\n")                    # a git pull mid-preview
    monkeypatch.setattr(apply_sandbox, "copy_code_and_raw", edited_meanwhile)
    out = _preview(vault)
    assert out["preview_token"] is None and out["token_withheld"] == "code_changed"


def test_added_deleted_and_generated_code_files(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("A = 1\n")
    base = apply_gate.code_fingerprint(tmp_path)
    (tmp_path / "app" / "__pycache__").mkdir()
    (tmp_path / "app" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\0")
    (tmp_path / "app" / "b.pyc").write_bytes(b"\0")
    assert apply_gate.code_fingerprint(tmp_path) == base   # generated files don't count
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "new.py").write_text("")
    added = apply_gate.code_fingerprint(tmp_path)
    assert added != base
    (tmp_path / "scripts" / "new.py").unlink()
    assert apply_gate.code_fingerprint(tmp_path) == base
    (tmp_path / "app" / "a.py").rename(tmp_path / "app" / "c.py")   # same bytes, other path
    assert apply_gate.code_fingerprint(tmp_path) != base


def test_a_code_change_between_authorization_and_apply_refuses(vault):
    token = _preview(vault)["preview_token"]
    (vault.root / "templates" / "extra.md").write_text("new\n")
    with pytest.raises(apply_gate.ApplyRefused) as exc:
        _apply(vault, token)
    assert exc.value.reason == "stale_preview" and not _archived(vault.root)


def test_the_api_refuses_while_its_loaded_code_differs_from_disk(client, monkeypatch):
    token = fixtures.authorize_apply()
    monkeypatch.setattr(main_module, "LOADED_CODE", "0" * 64)
    for resp in (client.post("/reviews/apply/dry-run"),
                 client.post("/reviews/apply", json={"preview_token": token})):
        assert resp.status_code == 503
        assert resp.json()["detail"] == {"reason": "restart_required",
                                         "action": "restart the API, then preview again"}
    resp = client.post("/ui/reviews/apply/preview")
    assert resp.status_code == 503 and "restart the API, then preview again" in resp.text


# --- entry points: CLI <-> API, UI (Q6/Q7/Q8) --------------------------------------------------


def _cli(*args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "apply_reviews.py"), *args],
                          capture_output=True, text=True)


def test_an_api_token_applies_through_the_cli_and_a_cli_token_through_the_api(client, vault):
    token = client.post("/reviews/apply/dry-run").json()["preview_token"]
    proc = _cli("apply", "--token", token, str(vault.root))
    assert proc.returncode == 0 and _archived(vault.root)

    other = "src_0000000000000a12"
    _rendered_source(vault.root, other)
    _graph_source_node(vault.root, other)
    _approve_archive(vault.root, other, rid="rev_arch2")
    out = json.loads(_cli("preview", str(vault.root), "--json").stdout)
    assert out["status"] == "ok" and out["preview_token"]
    resp = client.post("/reviews/apply", json={"preview_token": out["preview_token"]})
    assert resp.status_code == 200 and resp.json()["status"] == "applied"


def test_the_cli_reports_refusals_with_numeric_exit_codes(vault):
    assert _cli().returncode == 2
    proc = _cli("apply", str(vault.root))
    assert proc.returncode == 6 and "preview_required" in proc.stderr
    proc = _cli("apply", "--token", "apa_" + "1" * 32, str(vault.root))
    assert proc.returncode == 6 and "unknown_preview" in proc.stderr
    proc = _cli("preview", str(vault.root))
    assert proc.returncode == 0 and "Token: apa_" in proc.stdout
    token = proc.stdout.split("Token: ")[1].split()[0]
    with write_lock.hold(vault.reviews_dir):
        pass
    proc = _cli("apply", "--token", token, str(vault.root))
    assert proc.returncode == 6 and "stale_preview" in proc.stderr and not _archived(vault.root)


def test_the_cli_exits_non_zero_when_apply_is_not_clean(vault, monkeypatch):
    token = _preview(vault)["preview_token"]
    monkeypatch.setattr(main_module, "run_apply", lambda st: {"status": "saved_publication_pending",
                                                              "warnings": [],
                                                              "failed_validators": []})
    assert apply_reviews._apply(vault, token, main_module) == 1


def test_the_ui_get_is_read_only(client, vault, monkeypatch):
    monkeypatch.setattr(apply_gate, "preview",
                        lambda *a, **k: pytest.fail("GET must not run the preview"))
    for p in (revision.revision_path(vault.root), apply_authorization.store_path(vault.root)):
        p.unlink(missing_ok=True)
    resp = client.get("/ui/reviews/apply")
    assert resp.status_code == 200 and "action='/ui/reviews/apply/preview'" in resp.text
    assert not revision.revision_path(vault.root).exists()
    assert not apply_authorization.store_path(vault.root).exists()


def test_a_cross_origin_preview_post_is_refused_before_any_work(client, monkeypatch):
    monkeypatch.setattr(apply_gate, "preview",
                        lambda *a, **k: pytest.fail("refused requests must not run the preview"))
    resp = client.post("/ui/reviews/apply/preview", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403


def test_a_clean_ui_preview_renders_the_token_bound_apply_form(client, vault):
    resp = client.post("/ui/reviews/apply/preview")
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    (rec,) = _records(vault.root)
    assert f"name='preview_token' value='{rec['token']}'" in resp.text
    assert client.post("/reviews/apply/dry-run").headers["cache-control"] == "no-store"
    resp = client.post("/ui/reviews/apply", data={"preview_token": rec["token"]})
    assert resp.status_code == 200 and "Apply result" in resp.text and _archived(vault.root)
    again = client.post("/ui/reviews/apply", data={"preview_token": rec["token"]})
    assert again.status_code == 409 and "unknown_preview" in again.text
    assert "action='/ui/reviews/apply/preview'" in again.text          # offers a new preview


def test_raw_bytes_follow_the_copied_manifests_not_the_live_ones(vault):
    assert _preview(vault)["preview_token"]                # the store exists, and is never copied
    with write_lock.hold(vault.reviews_dir, changes=False):
        tmp_root = apply_sandbox.copy_state(vault)
    try:
        # A source catalogued after the state copy is not part of this snapshot.
        _rendered_source(vault.root, "src_0000000000000b01")
        apply_sandbox.copy_code_and_raw(vault, tmp_root)
        assert (tmp_root / "raw" / "inbox" / f"{SID}.md").exists()
        assert not (tmp_root / "raw" / "inbox" / "src_0000000000000b01.md").exists()
        assert not (tmp_root / "db" / apply_authorization.STORE_NAME).exists()
        assert not list((tmp_root / "db").glob("*.lock"))
    finally:
        apply_sandbox.cleanup_sandbox(tmp_root)


# --- review round 1 on slice 4 ------------------------------------------------------------------


def test_a_restore_killed_after_the_archived_revision_never_revives_a_token(vault):
    (vault.root / "db" / "zz_after_revision.json").write_text("{}", encoding="utf-8")
    archive = backup.create_backup(vault.root)                 # carries db/revision.json at R
    archived = _rev(vault.root)
    token = _preview(vault)["preview_token"]                   # bound to R (neither moved it)
    assert token and _rev(vault.root) == archived
    page = vault.root / "wiki" / "Sources" / f"{SID}.md"       # restored before db/ (archive order)
    restored_bytes = page.read_bytes()
    page.write_bytes(restored_bytes + b"\nedited after the backup\n")
    # Kill the restore during extraction (after its CRC check, which also opens every member) at the
    # first archive member after db/revision.json.
    code = ("import os, sys, zipfile; sys.path.insert(0, sys.argv[1]);"
            "sys.path.insert(0, sys.argv[1] + '/scripts'); import backup\n"
            "real_open, real_test = zipfile.ZipFile.open, zipfile.ZipFile.testzip\n"
            "state = {'extracting': False}\n"
            "def checked(self):\n"
            "    bad = real_test(self)\n"
            "    state['extracting'] = True\n"
            "    return bad\n"
            "def killing(self, name, *a, **k):\n"
            "    names = self.namelist()\n"
            "    n = name if isinstance(name, str) else name.filename\n"
            "    if (state['extracting'] and n != 'BACKUP_MANIFEST.json'\n"
            "            and names.index(n) > names.index('db/revision.json')):\n"
            "        os._exit(1)\n"
            "    return real_open(self, name, *a, **k)\n"
            "zipfile.ZipFile.testzip, zipfile.ZipFile.open = checked, killing\n"
            "backup.restore_backup(sys.argv[3], sys.argv[2], force=True)\n")
    proc = subprocess.run([sys.executable, "-c", code, str(ROOT), str(vault.root), str(archive)])
    assert proc.returncode == 1                                # killed mid-restore
    assert page.read_bytes() == restored_bytes                 # extraction had begun: page restored
    assert _rev(vault.root)["epoch"] != archived["epoch"]      # the archived revision never landed
    with pytest.raises(apply_gate.ApplyRefused):
        _apply(vault, token)
    assert not _archived(vault.root)


def test_a_restore_never_writes_the_archived_revision_or_authorizations(vault):
    import zipfile
    assert _preview(vault)["preview_token"]
    archive = backup.create_backup(vault.root)
    with zipfile.ZipFile(archive, "a") as zf:                  # an archive that carries both files
        zf.writestr("db/apply_authorizations.json", "{}")
    report = backup.restore_backup(archive, vault.root, force=True)
    assert "db/revision.json" not in report.overwritten + report.written
    assert "db/apply_authorizations.json" not in report.overwritten + report.written
    assert _records(vault.root) == []                          # cleared by the restore


def _port_vault(vault, monkeypatch, port):
    (vault.root / ".env").write_text(f"APP_PORT={port}\nANTHROPIC_API_KEY=sk-live\n"
                                     f"EMBEDDING_CACHE_DIR={vault.root}/cache\n", encoding="utf-8")
    st = get_settings(vault.root)
    monkeypatch.setattr(main_module, "settings", st)
    return st


def test_the_preview_executes_with_the_live_env_and_matches_the_applied_pages(vault, monkeypatch):
    st = _port_vault(vault, monkeypatch, 19001)
    out = _preview(st)
    assert out["status"] == "ok" and out["preview_token"]
    page = f"wiki/Sources/{SID}.md"
    (diff,) = [d for d in out["diff"]["wiki"] if d["path"] == page]
    added = [ln for ln in diff["unified_diff"].splitlines() if ln.startswith("+ ")
             or ln.startswith("+-")]
    assert any(":19001/raw/" in ln for ln in added) and not any(":18000/" in ln for ln in added)
    assert _apply(st, out["preview_token"])["status"] == "applied"
    text = (vault.root / page).read_text(encoding="utf-8")
    assert f"http://127.0.0.1:19001/raw/{SID}" in text and ":18000/" not in text


def test_the_sandbox_env_carries_no_secrets_or_absolute_paths(vault, tmp_path):
    _port_vault_env = vault.root / ".env"
    _port_vault_env.write_text("APP_PORT=19001\nANTHROPIC_API_KEY=sk-live\n# c\n"
                               "EMBEDDING_CACHE_DIR=/abs/cache\nWIKI_SUMMARY_MAX_CHARS=300\n",
                               encoding="utf-8")
    box = tmp_path / "box"
    box.mkdir()
    apply_gate.carry_env(vault, box)
    assert (box / ".env").read_text().split() == ["APP_PORT=19001", "WIKI_SUMMARY_MAX_CHARS=300"]


def test_changing_the_effective_raw_view_url_before_apply_refuses_the_token(vault, monkeypatch):
    st = _port_vault(vault, monkeypatch, 19001)
    token = _preview(st)["preview_token"]
    # The API's settings are unchanged, but its workers read the vault's .env afresh.
    (vault.root / ".env").write_text("APP_PORT=19002\n", encoding="utf-8")
    with pytest.raises(apply_gate.ApplyRefused) as exc:
        _apply(st, token)
    assert exc.value.reason == "stale_preview" and not _archived(vault.root)
    (vault.root / ".env").write_text("APP_PORT=19001\nANTHROPIC_API_KEY=sk-other\n",
                                     encoding="utf-8")
    assert _apply(st, token)["status"] == "applied"           # a key change alone doesn't bind


def test_a_sandbox_that_could_not_reproduce_the_live_settings_is_a_failed_preview(vault,
                                                                                   monkeypatch):
    st = _port_vault(vault, monkeypatch, 19001)
    monkeypatch.setattr(apply_gate, "carry_env", lambda *a: None)
    out = _preview(st)
    assert out["status"] == "failed" and out["reason"] == "settings_not_carried"
    assert out["preview_token"] is None and _records(vault.root) == []



def test_a_settings_change_during_the_preview_withholds_the_token(vault, monkeypatch):
    st = _port_vault(vault, monkeypatch, 19001)

    def edited_meanwhile(sandbox):
        out = main_module._evaluate_preview(sandbox)
        (vault.root / ".env").write_text("APP_PORT=19002\n", encoding="utf-8")
        return out
    out = apply_gate.preview(st, evaluate=edited_meanwhile)
    assert out["status"] == "ok" and out["preview_token"] is None
    assert out["token_withheld"] == "settings_changed" and _records(vault.root) == []


def test_a_secret_change_during_the_preview_still_authorizes(vault, monkeypatch):
    st = _port_vault(vault, monkeypatch, 19001)

    def rotated_meanwhile(sandbox):
        out = main_module._evaluate_preview(sandbox)
        (vault.root / ".env").write_text(f"APP_PORT=19001\nANTHROPIC_API_KEY=sk-rotated\n"
                                         f"EMBEDDING_CACHE_DIR={vault.root}/cache\n",
                                         encoding="utf-8")
        return out
    out = apply_gate.preview(st, evaluate=rotated_meanwhile)
    assert out["preview_token"] and _apply(st, out["preview_token"])["status"] == "applied"


def test_the_authorization_binds_the_settings_the_preview_captured(vault, monkeypatch):
    # .env changes after the capture and changes back before the authorization check (A-B-A): the
    # copy ran with B, so the sandbox check refuses it.
    st = _port_vault(vault, monkeypatch, 19001)
    real = apply_sandbox.copy_code_and_raw

    def flipped(st_, tmp_root):
        (vault.root / ".env").write_text("APP_PORT=19002\n", encoding="utf-8")
        real(st_, tmp_root)
    monkeypatch.setattr(apply_sandbox, "copy_code_and_raw", flipped)

    def restored(sandbox):
        out = main_module._evaluate_preview(sandbox)
        (vault.root / ".env").write_text("APP_PORT=19001\n", encoding="utf-8")
        return out
    out = apply_gate.preview(st, evaluate=restored)
    assert out["status"] == "failed" and out["reason"] == "settings_not_carried"
    assert out["preview_token"] is None
