"""ADR-0074 decision 8 and 7.4, slice 5a: the roll-forward install journal of extraction.

Every durable transition of an install is crashed in turn (and the recovery crashed again); the
invariant after recovery is always one complete generation — the old one if the journal was never
written, the new one once it was — never a mix, with no journal left open and no staging left over.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import durable_write, manifests  # noqa: E402
from app.backend import extraction_install as xi  # noqa: E402
from app.workers import extract, intake, lint, publication  # noqa: E402
from app.workers.chunking import Element  # noqa: E402
from app.workers.extractors import Extraction  # noqa: E402
from tests import fixtures  # noqa: E402

GEN_A = {"text": "Generation A has one paragraph of prose text.",
         "tables": [("0.csv", "a,b\n1,2\n"), ("1.csv", "c\n3\n")]}
GEN_B = {"text": "Generation B replaces it with a different, somewhat longer paragraph.",
         "tables": [("1.csv", "c\n4\n"), ("2.csv", "d\n5\n")]}        # 0 removed, 1 changed, 2 added


class Crash(BaseException):
    """A process death at a chosen point (never caught as an ordinary error)."""


@pytest.fixture
def vault(tmp_path, monkeypatch):
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.copy_templates(tmp_path)
    fixtures.write_markdown(inbox / "doc.md")
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    gen = {"current": GEN_A, "parsed": 0}

    def dispatch(path, source_id, **kw):
        gen["parsed"] += 1
        g = gen["current"]
        return Extraction(elements=[Element("prose", g["text"])], tool="fake", tool_version="1",
                          tables=list(g["tables"]))
    monkeypatch.setattr(extract, "_parse", dispatch)
    return tmp_path, gen


def _sid(root: Path) -> str:
    return manifests.scan_manifests(root / "raw" / "manifests").valid[0]["source_id"]


def _run(root: Path, **kw) -> dict:
    return extract.extract_sources(root, jobs_db=root / "db" / "jobs.sqlite", **kw)


def _layout(root: Path) -> xi.Layout:
    return xi.Layout(root / "normalized")


def _generation(root: Path, sid: str) -> dict:
    """The installed bytes a generation is made of (the log's timestamps aside)."""
    out = _layout(root).outputs(sid)
    tables = {p.name: p.read_bytes() for p in sorted(out["tables"].iterdir())} \
        if out["tables"].is_dir() else None
    return {"markdown": out["markdown"].read_bytes() if out["markdown"].exists() else None,
            "chunks": out["chunks"].read_bytes() if out["chunks"].exists() else None,
            "tables": tables}


def _clean_generation(tmp_path, monkeypatch, gen) -> dict:
    """The generation a clean extraction of `gen` installs, in a separate vault."""
    inbox = tmp_path / "ref" / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.copy_templates(tmp_path / "ref")
    fixtures.write_markdown(inbox / "doc.md")
    intake.scan_inbox(tmp_path / "ref", jobs_db=tmp_path / "ref" / "db" / "jobs.sqlite")
    with monkeypatch.context() as m:
        m.setattr(extract, "_parse", lambda p, s, **kw: Extraction(
            elements=[Element("prose", gen["text"])], tool="fake", tool_version="1",
            tables=list(gen["tables"])))
        _run(tmp_path / "ref")
    return _generation(tmp_path / "ref", _sid(tmp_path / "ref"))


def _validate(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_normalized.py"),
                           str(root)], capture_output=True, text=True)


def _assert_settled(root: Path, sid: str, expected: list[dict]) -> dict:
    """One complete generation from `expected`, no journal, no staging or holding left."""
    layout = _layout(root)
    got = _generation(root, sid)
    assert got in expected, "a mixed generation was left installed"
    assert not layout.journal(sid).exists()
    assert not layout.staging.exists() or not list(layout.staging.iterdir())
    assert not layout.install.exists() or not list(layout.install.iterdir())
    m = manifests.get_manifest(root / "raw" / "manifests", sid)
    chunks = got["chunks"].decode().splitlines()
    assert m["ingestion_status"] == "extracted" and m["chunk_count"] == len(chunks)
    log = json.loads(layout.outputs(sid)["log"].read_text())
    assert log["table_count"] == len(got["tables"]) and log["chunk_count"] == len(chunks)
    result = _validate(root)
    assert result.returncode == 0, result.stdout
    return got


_DURABLE = ("move", "write_bytes", "write_json", "write_text", "remove_tree", "remove")


def _arm(monkeypatch, crash_at: int | None) -> list[str]:
    """Count durable writes (each one a transition); raise Crash before call number `crash_at`."""
    calls: list[str] = []
    for name in _DURABLE:
        real = getattr(durable_write, name)

        def wrapped(*a, _real=real, _name=name, **kw):
            if crash_at is not None and len(calls) == crash_at:
                raise Crash(_name)
            calls.append(f"{_name}:{Path(a[0]).name}")
            return _real(*a, **kw)
        monkeypatch.setattr(durable_write, name, wrapped)
    return calls


def _transitions(vault, monkeypatch) -> int:
    """How many durable writes a re-extraction to generation B makes, uninterrupted."""
    root, gen = vault
    with monkeypatch.context() as m:
        calls = _arm(m, None)
        gen["current"] = GEN_B
        _run(root, force=True)
    return len(calls)


# --- the crash matrix --------------------------------------------------------------------------


def test_every_transition_crashed_recovers_one_complete_generation(vault, tmp_path, monkeypatch):
    root, gen = vault
    a = _clean_generation(tmp_path / "a", monkeypatch, GEN_A)
    b = _clean_generation(tmp_path / "b", monkeypatch, GEN_B)
    _run(root)
    sid = _sid(root)
    assert _generation(root, sid) == a
    total = _transitions(vault, monkeypatch)
    assert total > 10
    for n in range(total):
        gen["current"] = GEN_A                           # back to A, cleanly
        _run(root, force=True)
        assert _assert_settled(root, sid, [a]) == a
        gen["current"] = GEN_B
        with monkeypatch.context() as m:
            _arm(m, n)
            with pytest.raises(Crash):
                _run(root, force=True)
        journaled = _layout(root).journal(sid).exists()
        _run(root)                                       # recovery at run start, then a normal run
        got = _assert_settled(root, sid, [a, b])
        if journaled:
            assert got == b, f"crash {n}: an open journal must roll forward"


def test_a_recovery_crashed_again_still_rolls_forward(vault, tmp_path, monkeypatch):
    root, gen = vault
    b = _clean_generation(tmp_path / "b", monkeypatch, GEN_B)
    _run(root)
    sid = _sid(root)
    total = _transitions(vault, monkeypatch)
    exercised = 0
    for n in range(0, total, 2):
        m_at = 0
        while True:
            gen["current"] = GEN_A                       # the state crash n leaves, recreated
            _run(root, force=True)
            gen["current"] = GEN_B
            with monkeypatch.context() as m:
                _arm(m, n)
                with pytest.raises(Crash):
                    _run(root, force=True)
            if not _layout(root).journal(sid).exists():
                break                                    # nothing for recovery to roll forward
            with monkeypatch.context() as m:             # the recovery itself dies at m_at
                _arm(m, m_at)
                try:
                    _run(root)
                    crashed = False
                except Crash:
                    crashed = True
            _run(root)                                   # the recovery of the recovery
            assert _assert_settled(root, sid, [b]) == b, f"install crash {n}, recovery crash {m_at}"
            exercised += 1
            if not crashed:
                break
            m_at += 1
    assert exercised > total


# --- table generations -------------------------------------------------------------------------


@pytest.mark.parametrize("before, after", [
    ([("0.csv", "x\n1\n"), ("1.csv", "y\n2\n")], []),             # set emptied
    ([], [("0.csv", "x\n1\n")]),                                   # tables added
    ([("0.csv", "x\n1\n")], [("0.csv", "x\n9\n"), ("3.csv", "z\n")]),  # changed and added
])
def test_table_generations_follow_the_exact_inventory(vault, tmp_path, monkeypatch, before, after):
    root, gen = vault
    gen["current"] = {"text": GEN_A["text"], "tables": before}
    _run(root)
    sid = _sid(root)
    gen["current"] = {"text": GEN_B["text"], "tables": after}
    summary = _run(root, force=True)
    assert summary["errors"] == 0
    got = _assert_settled(root, sid, [_clean_generation(tmp_path / "c", monkeypatch,
                                                         gen["current"])])
    assert sorted(got["tables"]) == sorted(n for n, _ in after)


# --- durability barriers -----------------------------------------------------------------------


def test_a_failed_directory_sync_leaves_the_install_to_recovery_which_repeats_the_barrier(
        vault, monkeypatch):
    root, gen = vault
    _run(root)
    sid = _sid(root)
    tables_dir = _layout(root).outputs(sid)["tables"]
    real = durable_write.sync_dir
    fired: list[Path] = []

    def failing(directory):
        # The sync after the LAST rename fails: recovery then has no rename left whose own sync
        # would cover the directories — only the repeated barrier does.
        if Path(directory) == tables_dir and (tables_dir / "2.csv").exists() and not fired:
            fired.append(Path(directory))
            raise OSError(5, "Input/output error")
        return real(directory)
    gen["current"] = GEN_B
    with monkeypatch.context() as m:
        m.setattr(durable_write, "sync_dir", failing)
        summary = _run(root, force=True)
    assert fired
    assert summary["error_details"][0]["error"].startswith(xi.INCOMPLETE)
    assert xi.read_journal(_layout(root), sid).state == xi.INSTALLING
    assert _validate(root).returncode == 1
    synced: list[Path] = []
    with monkeypatch.context() as m:
        m.setattr(durable_write, "sync_dir", lambda d: (synced.append(Path(d)), real(d))[1])
        summary = _run(root)
    out = _layout(root).outputs(sid)
    for directory in (out["markdown"].parent, out["chunks"].parent, out["tables"]):
        assert directory in synced                       # the barrier is repeated by recovery
    assert summary["install_recovery"]["completed"] == [sid]
    got = _assert_settled(root, sid, [_generation(root, sid)])
    assert got["tables"] == {n: c.encode() for n, c in GEN_B["tables"]}


def test_an_installed_journal_stays_open_until_publication_succeeds(vault, monkeypatch):
    root, gen = vault
    _run(root)
    sid = _sid(root)
    real_publish = publication.Run.publish
    gen["current"] = GEN_B
    with monkeypatch.context() as m:
        m.setattr(publication.Run, "publish",
                  lambda self, *a, **k: {**real_publish(self, *a, **k),
                                         "status": publication.PENDING})
        _run(root, force=True)
    read = xi.read_journal(_layout(root), sid)
    assert read.state == xi.INSTALLED and read.usable    # readers may use it
    assert not list(_layout(root).staging.iterdir())    # retired once installed
    with monkeypatch.context() as m:                     # recovery's publication fails too
        m.setattr(publication.Run, "publish",
                  lambda self, *a, **k: {**real_publish(self, *a, **k),
                                         "status": publication.PENDING})
        summary = _run(root)
    assert summary["install_recovery"]["closed"] == []
    assert xi.read_journal(_layout(root), sid).state == xi.INSTALLED
    summary = _run(root)                                 # skipped (usable), and the journal closes
    assert summary["skipped_unchanged"] == 1 and summary["install_recovery"]["closed"] == [sid]
    assert not _layout(root).journal(sid).exists()


# --- unrecoverable and incompatible ------------------------------------------------------------


def _crash_mid_install(vault, monkeypatch) -> str:
    """Crash a re-extraction to B right after its first output moved into place."""
    root, gen = vault
    _run(root)
    sid = _sid(root)
    gen["current"] = GEN_B
    real = durable_write.move
    moved: list[int] = []

    def move(src, dst):
        if moved:
            raise Crash("move")
        moved.append(1)
        return real(src, dst)
    with monkeypatch.context() as m:
        m.setattr(durable_write, "move", move)
        with pytest.raises(Crash):
            _run(root, force=True)
    assert xi.read_journal(_layout(root), sid).state == xi.INSTALLING
    return sid


@pytest.mark.parametrize("tamper, reason", [
    ("config", "incompatible_configuration"),
    ("staged", "staged_table:2.csv_mismatch"),
    ("missing", "table:2.csv_missing"),
])
def test_an_install_that_cant_complete_is_unrecoverable_and_preserved(vault, monkeypatch, tamper,
                                                                      reason):
    root, gen = vault
    sid = _crash_mid_install(vault, monkeypatch)
    layout = _layout(root)
    entry = xi.read_journal(layout, sid).entry
    stage = layout.stage(sid, entry["attempt_id"])
    kw = {}
    if tamper == "config":
        kw["target_chars"] = 999
    elif tamper == "staged":
        (stage / "tables" / "2.csv").write_text("tampered\n")
    else:
        (stage / "tables" / "2.csv").unlink()
    before = {p: p.read_bytes() for p in (root / "normalized").rglob("*")
              if p.is_file() and ".install" not in p.parts}
    summary = _run(root, source_ids=[], **kw)            # recovery only: no fresh attempt
    assert summary["install_recovery"]["unrecoverable"] == {sid: reason}
    read = xi.read_journal(layout, sid)
    assert read.state == xi.UNRECOVERABLE and not read.usable and read.entry["reason"] == reason
    after = {p: p.read_bytes() for p in (root / "normalized").rglob("*")
             if p.is_file() and ".install" not in p.parts}
    assert after == before                              # every artifact preserved, staging kept
    assert stage.exists() and layout.holding(sid, entry["attempt_id"]).exists()
    assert "extraction install unrecoverable" in _validate(root).stdout
    findings = [f for f in lint.run_lint(root, record_job=False, file_review_items=False)["findings"]
                if f["check"] == xi.INCOMPLETE]
    assert findings and findings[0]["subject"] == sid and findings[0]["severity"] == "high"


def test_a_fresh_attempt_supersedes_an_unrecoverable_install(vault, tmp_path, monkeypatch):
    root, gen = vault
    sid = _crash_mid_install(vault, monkeypatch)
    layout = _layout(root)
    old = xi.read_journal(layout, sid).entry["attempt_id"]
    _run(root, source_ids=[], target_chars=999)          # → unrecoverable (incompatible)
    assert xi.read_journal(layout, sid).state == xi.UNRECOVERABLE
    summary = _run(root, target_chars=999)              # not skipped: a fresh attempt
    assert summary["extracted"] == 1
    assert not layout.stage(sid, old).exists() and not layout.holding(sid, old).exists()
    expected = _generation(root, sid)
    _assert_settled(root, sid, [expected])
    assert expected["tables"] == {n: c.encode() for n, c in GEN_B["tables"]}


# --- damaged journals and staging cleanup ------------------------------------------------------


@pytest.mark.parametrize("damage", ["garbage", "not_object", "bad_table_name", "bad_supersedes",
                                    "other_document", "bad_log_hash"])
def test_a_damaged_journal_blocks_the_document_and_all_staging_cleanup(vault, monkeypatch, damage):
    root, gen = vault
    sid = _crash_mid_install(vault, monkeypatch)
    layout = _layout(root)
    path = layout.journal(sid)
    entry = json.loads(path.read_text())
    if damage == "garbage":
        path.write_bytes(b"\xff{")
    elif damage == "not_object":
        path.write_text("[]")
    else:
        if damage == "bad_table_name":
            entry["outputs"]["tables"]["../escape.csv"] = entry["outputs"]["tables"]["2.csv"]
        elif damage == "bad_supersedes":
            entry["supersedes"] = ["../x"]
        elif damage == "other_document":
            entry["source_id"] = "src_" + "0" * 16
        else:
            entry["log"]["status"] = "tampered"
        path.write_text(json.dumps(entry))
    orphan = layout.staging / f"src_{'1' * 16}-att_{'0' * 12}"
    orphan.mkdir(parents=True)
    before = {p: p.read_bytes() for p in (root / "normalized").rglob("*") if p.is_file()}
    parsed = gen["parsed"]
    summary = _run(root, force=True)
    assert gen["parsed"] == parsed                       # not even parsed again
    assert summary["install_recovery"]["blocked"] == {sid: "install_journal_corrupt"}
    assert summary["staging_cleanup"]["removed"] == [] and summary["staging_cleanup"]["blocked"]
    assert orphan.exists()                               # an incomplete inventory proves nothing
    assert summary["error_details"] == [{"source_id": sid, "error": "install_journal_corrupt",
                                         "skip_reason": "install_journal_corrupt",
                                         "preserved_prior": True}]
    after = {p: p.read_bytes() for p in (root / "normalized").rglob("*") if p.is_file()}
    assert after == before                               # never extracted over
    assert _validate(root).returncode == 1


def test_staging_cleanup_removes_only_what_no_journal_names(vault, monkeypatch):
    root, gen = vault
    sid = _crash_mid_install(vault, monkeypatch)
    layout = _layout(root)
    _run(root, source_ids=[], target_chars=999)          # unrecoverable: artifacts preserved
    attempt = xi.read_journal(layout, sid).entry["attempt_id"]
    legacy = layout.staging / sid                        # the pre-journal staging layout
    legacy.mkdir()
    orphan = layout.staging / f"{sid}-att_{'f' * 12}"
    orphan.mkdir()
    stray_holding = layout.install / f"{sid}-att_{'e' * 12}{xi.HOLDING_SUFFIX}"
    stray_holding.mkdir()
    summary = _run(root, source_ids=[], target_chars=999)
    assert sorted(summary["staging_cleanup"]["removed"]) == sorted(
        [legacy.name, orphan.name, stray_holding.name])
    assert layout.stage(sid, attempt).exists() and layout.holding(sid, attempt).exists()


def test_install_writes_go_through_the_durable_helper(vault):
    """The runtime write guard covers the extraction family: a clean install raises no violation
    (conftest fails the test otherwise), and a raw write to an output is flagged."""
    from tests import write_guard
    root, gen = vault
    _run(root)
    sid = _sid(root)
    assert any(fam == "extraction" for fam, _ in write_guard.helper_writes)
    assert write_guard.family(str(_layout(root).outputs(sid)["markdown"])) == "extraction"


def test_recovery_keeps_governance_and_provenance_and_applies_only_extraction_fields(
        vault, monkeypatch):
    root, gen = vault
    sid = _crash_mid_install(vault, monkeypatch)
    mdir = root / "raw" / "manifests"
    record = manifests.get_manifest(mdir, sid)
    record["status"] = "hidden"                          # a governance change after the crash
    manifests.save_manifest(mdir, record)
    entry = xi.read_journal(_layout(root), sid).entry
    summary = _run(root, source_ids=[])
    assert summary["install_recovery"]["completed"] == [sid]
    after = manifests.get_manifest(mdir, sid)
    assert after["status"] == "hidden"
    assert {k: after.get(k) for k in manifests.EXTRACTION_FIELDS} == entry["manifest_fields"]


def test_a_damaged_manifest_blocks_recovery_and_leaves_the_journal(vault, monkeypatch):
    root, gen = vault
    sid = _crash_mid_install(vault, monkeypatch)
    path = manifests.manifest_path(root / "raw" / "manifests", sid)
    good = path.read_bytes()
    path.write_bytes(b"{")
    journal = _layout(root).journal(sid).read_bytes()
    summary = _run(root, source_ids=[])
    assert summary["install_recovery"]["blocked"] == {sid: "manifest_corrupt"}
    assert _layout(root).journal(sid).read_bytes() == journal
    path.write_bytes(good)                               # repaired (e.g. scripts/repair_manifest.py)
    assert _run(root, source_ids=[])["install_recovery"]["completed"] == [sid]


def test_a_generation_that_fails_its_final_verification_is_unrecoverable(vault, monkeypatch):
    root, gen = vault
    _run(root)
    sid = _sid(root)
    real = durable_write.move

    def corrupting(src, dst):                            # the bytes change under the rename
        real(src, dst)
        if Path(dst).name == "2.csv":
            Path(dst).write_text("bit rot\n")
    gen["current"] = GEN_B
    with monkeypatch.context() as m:
        m.setattr(durable_write, "move", corrupting)
        summary = _run(root, force=True)
    assert summary["error_details"][0]["error"] == f"{xi.INCOMPLETE}:table:2.csv_not_installed"
    read = xi.read_journal(_layout(root), sid)
    assert read.state == xi.UNRECOVERABLE and read.entry["reason"] == "table:2.csv_not_installed"
