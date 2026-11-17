"""ADR-0074 change 7c — the Apply preview benchmark: generator, runner and the preview's timings.

Smoke coverage only (7c Q3): fixture validation, report structure, the nearest-rank percentile and
threshold classification on deterministic inputs. No wall-clock performance assertion anywhere.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import bench_apply_preview as bench  # noqa: E402
import bench_fixture as fixture  # noqa: E402

GIB = 1024 ** 3


# --- percentile and classification ---------------------------------------------------------------


def test_nearest_rank_p95_is_the_19th_of_20_and_the_maximum_of_10():
    twenty = [float(v) for v in range(1, 21)]
    assert bench.nearest_rank(list(reversed(twenty))) == 19.0
    assert bench.nearest_rank([float(v) for v in range(1, 11)]) == 10.0   # n=10: p95 is the max
    assert bench.nearest_rank([7.0]) == 7.0
    assert bench.nearest_rank([]) is None
    assert bench.nearest_rank(twenty, 50.0) == 10.0
    s = bench.summarize([3.0, 1.0, 2.0])
    assert s == {"n": 3, "p95": 3.0, "median": 2.0, "min": 1.0, "max": 3.0, "values": [1.0, 2.0, 3.0]}


def _ok(n=20, v=1.0):
    return [v] * n


def _classify(*args, warmups=1, storage_runs=5, **kw):
    """`bench.classify` with a complete warm-up and storage set unless a test says otherwise."""
    return bench.classify(*args, valid_warmups=warmups, valid_storage_runs=storage_runs, **kw)


def test_an_invalid_warm_up_or_too_few_valid_storage_runs_is_incomplete_and_triggers_nothing():
    """7c-1 review round 2: the requested counts are not enough; the valid ones decide."""
    for kw, reason in (({"warmups": 0}, "warm-up: 0 valid runs, 1 required"),
                       ({"storage_runs": 1}, "storage_bytes: 1 valid runs, 5 required"),
                       ({"storage_runs": 4}, "storage_bytes: 4 valid runs, 5 required")):
        res = _classify(_ok(), _ok(), _ok(), 10, 1000 * GIB, judged=True, **kw)     # would pass
        assert res["verdict"] == "incomplete" and reason in res["incomplete_reasons"]
        res = _classify(_ok(20, 2500.0), _ok(), _ok(), 10 * GIB, 1000 * GIB, judged=True, **kw)
        assert res["verdict"] == "incomplete" and res["breach_status"] == "provisional"
        assert res["breached"] == ["lock_held_ms", "storage_bytes"]
        assert res["triggered_follow_up"] is None
    assert _classify(_ok(), _ok(), _ok(), 10, 1000 * GIB, judged=True)["verdict"] == "pass"


def _trial(kind, valid, **extra):
    t = {"set": kind, "valid": valid, "reason": None if valid else "outcome_blocked",
         "state": {"revision": [1, 1], "approved": "x"}, "free_bytes_before": 1000 * GIB}
    if valid and kind in ("warm", "storage", "warmup"):
        t["phases_ms"] = {"lock_held": 1.0, "total": 2.0}
    if valid and kind == "storage":
        t["storage"] = {"samples": 3, "peak_alloc": 10, "peak_logical": 10, "mean_walk_ms": 1.0,
                        "max_walk_ms": 1.0, "cpu_s": 0.1, "peak_phase": "after:lock_acquired"}
    if valid and kind == "contention":
        t["keep_ms"] = 1.0
    t.update(extra)
    return t


def _report(trials):
    decl = {"preset": "baseline", "scale": 600, "label": "synthetic baseline",
            "profiles": {"heavy": {}}}
    prov = {"fingerprint": "sha256:x", "fingerprint_verified": True, "verify_seconds": 0.0,
            "code": {"fixture": "a", "executing": "a", "declared": "a", "match": True}}
    settings = {"warmup": 1, "warm": 20, "contention": 20, "uncontended": 0, "storage": 5,
                "cold": 0, "interval_ms": 50}
    return bench.build_report(decl, "heavy", trials, [], logical_bytes=10, settings=settings,
                              machine_info={}, revision={}, provenance=prov)


def test_the_report_keeps_an_invalid_warm_up_and_counts_only_valid_storage_runs():
    base = [_trial("warm", True) for _ in range(20)] + [_trial("contention", True) for _ in range(20)]
    full = _report([_trial("warmup", True)] + base + [_trial("storage", True) for _ in range(5)])
    assert full["acceptance"]["verdict"] == "pass"
    # The reviewer's reproduction: one valid storage trial and four invalid ones.
    short = _report([_trial("warmup", True)] + base + [_trial("storage", True)]
                    + [_trial("storage", False) for _ in range(4)])
    acc = short["acceptance"]
    assert acc["verdict"] == "incomplete" and acc["triggered_follow_up"] is None
    assert "storage_bytes: 1 valid runs, 5 required" in acc["incomplete_reasons"]
    bad_warmup = _report([_trial("warmup", False)] + base + [_trial("storage", True) for _ in range(5)])
    acc = bad_warmup["acceptance"]
    assert acc["verdict"] == "incomplete" and acc["triggered_follow_up"] is None
    assert acc["valid_warmups"] == 0
    kept = [t for t in bad_warmup["trials"]["detail"] if t["set"] == "warmup"]
    assert len(kept) == 1 and kept[0]["valid"] is False                 # preserved in the report
    assert bad_warmup["trials"]["invalid_by_reason"] == {"warmup:outcome_blocked": 1}
    assert bad_warmup["metrics"]["lock_held_ms"]["n"] == 20            # warm-ups never measured


def test_classification_passes_fails_and_triggers_the_follow_up():
    res = _classify(_ok(), _ok(), _ok(), 10, 1000 * GIB, judged=True)
    assert res["verdict"] == "pass" and res["breached"] == [] and res["triggered_follow_up"] is None
    assert res["breach_status"] is None and res["incomplete_reasons"] == []
    # p95 decides, not the maximum: one slow run in 20 is tolerated, two are not.
    one_slow = _ok(19, 100.0) + [2500.0]
    assert _classify(one_slow, _ok(), _ok(), 10, 1000 * GIB, judged=True)["verdict"] == "pass"
    two_slow = _ok(18, 100.0) + [2500.0, 2600.0]
    res = _classify(two_slow, _ok(), _ok(), 10, 1000 * GIB, judged=True)
    assert res["verdict"] == "fail" and res["breached"] == ["lock_held_ms"]
    assert res["breach_status"] == "confirmed" and "optimization grill" in res["triggered_follow_up"]
    assert _classify(_ok(), _ok(), _ok(20, 3000.1), 10, 1000 * GIB,
                          judged=True)["breached"] == ["keep_during_copy_ms"]
    assert _classify(_ok(), _ok(20, 60000.1), _ok(), 10, 1000 * GIB,
                          judged=True)["breached"] == ["end_to_end_ms"]


def test_storage_is_judged_on_the_peak_against_the_smaller_of_5_gib_and_10_percent_free():
    big_free = 1000 * GIB                                     # 10% = 100 GiB: the 5 GiB cap applies
    r = _classify(_ok(), _ok(), _ok(), 5 * GIB, big_free, judged=True)["results"]["storage_bytes"]
    assert r["limit"] == 5 * GIB and r["pass"] is True
    assert _classify(_ok(), _ok(), _ok(), 5 * GIB + 1, big_free,
                          judged=True)["breached"] == ["storage_bytes"]
    small_free = 20 * GIB                                     # 10% = 2 GiB is the tighter limit
    r = _classify(_ok(), _ok(), _ok(), 3 * GIB, small_free, judged=True)
    assert r["results"]["storage_bytes"]["limit"] == 2 * GIB and r["breached"] == ["storage_bytes"]


def test_too_few_valid_runs_and_unjudged_profiles_never_pass():
    res = _classify(_ok(19), _ok(), _ok(), 10, 1000 * GIB, judged=True)
    assert res["verdict"] == "incomplete" and res["incomplete_reasons"]
    assert _classify(_ok(), _ok(), _ok(), None, 1000 * GIB,
                          judged=True)["verdict"] == "incomplete"
    res = _classify(_ok(), _ok(), _ok(), 10, 1000 * GIB, judged=False)
    assert res["verdict"] == "not_judged" and res["triggered_follow_up"] is None


def test_an_incomplete_measurement_shows_provisional_breaches_but_never_triggers_the_follow_up():
    """7c-1 review: one breaching sample is visible, provisional, and triggers nothing."""
    res = _classify([2500.0], _ok(), _ok(), 10, 1000 * GIB, judged=True)
    assert res["verdict"] == "incomplete" and res["breached"] == ["lock_held_ms"]
    assert res["breach_status"] == "provisional" and res["triggered_follow_up"] is None
    assert any("lock_held_ms: 1 valid runs" in r for r in res["incomplete_reasons"])
    # A complete set of breaching runs still cannot trigger anything while a blocker stands.
    res = _classify(_ok(20, 2500.0), _ok(), _ok(), 10, 1000 * GIB, judged=True,
                         blockers=["setting warmup=0 is below the required 1"])
    assert res["verdict"] == "incomplete" and res["breach_status"] == "provisional"
    assert res["triggered_follow_up"] is None
    # Unjudged runs never trigger either, whatever they observed.
    res = _classify(_ok(20, 2500.0), _ok(), _ok(), 10, 1000 * GIB, judged=False)
    assert res["breach_status"] == "provisional" and res["triggered_follow_up"] is None


def test_judged_acceptance_requires_the_baseline_settings_including_the_warm_up():
    full = {"warmup": 1, "warm": 20, "contention": 20, "uncontended": 5, "storage": 5, "cold": 3,
            "interval_ms": 50}
    assert bench.settings_shortfall(full) == []
    for key, value in (("warmup", 0), ("warm", 19), ("contention", 5), ("storage", 0),
                       ("interval_ms", 200)):
        short = bench.settings_shortfall(dict(full, **{key: value}))
        assert len(short) == 1 and key in short[0]
    assert bench.settings_shortfall(dict(full, uncontended=0, cold=0)) == []   # not judged sets


# --- validity, overlap, sanitizing, digest -------------------------------------------------------


def _result(**kw):
    base = {"status": "ok", "timings": {"phases_ms": {"lock_held": 1.0}},
            "outcomes": [{"review_id": "rev_a", "outcome": "applied", "details": {}}]}
    base.update(kw)
    return base


def test_a_trial_is_valid_only_with_every_approval_applied():
    assert bench.trial_validity(_result(), {"rev_a"}) is None
    assert bench.trial_validity(_result(status="validation_failed"), {"rev_a"}) == \
        "status_validation_failed"
    assert bench.trial_validity(_result(), {"rev_a", "rev_b"}) == "outcome_missing"
    for outcome in ("skipped", "already_in_effect", "incomplete"):
        bad = _result(outcomes=[{"review_id": "rev_a", "outcome": outcome, "details": {}}])
        assert bench.trial_validity(bad, {"rev_a"}) == f"outcome_{outcome}"
    normalized = _result(outcomes=[{"review_id": "rev_a", "outcome": "applied",
                                    "details": {"normalized": True}}])
    assert bench.trial_validity(normalized, {"rev_a"}) == "outcome_applied"
    assert bench.trial_validity(_result(timings={}), {"rev_a"}) == "no_timings"


def test_overlap_requires_the_keep_to_start_inside_the_lock_interval():
    marks = {"lock_acquired": 100, "lock_released": 200}
    assert bench.overlap(marks, 100) and bench.overlap(marks, 150) and bench.overlap(marks, 200)
    assert not bench.overlap(marks, 99) and not bench.overlap(marks, 201)
    assert not bench.overlap({}, 150)


def test_sanitize_removes_absolute_paths_from_errors_and_metadata():
    out = bench.sanitize({"e": ["OSError at /home/u/vault/db/x.sqlite: denied", "a / b",
                                "application/json", "/etc/hosts"]}, ["/home/u"])
    assert out == {"e": ["OSError at <root>/vault/db/x.sqlite: denied", "a / b", "application/json",
                         "<path>"]}


def test_the_digest_identifies_the_exact_report():
    r = {"a": 1, "b": [1, 2]}
    assert bench.digest(r) == bench.digest(json.loads(json.dumps(r)))
    assert bench.digest(r) != bench.digest({"a": 2, "b": [1, 2]})
    assert bench.digest(r).startswith("sha256:")


# --- the generator's plan and destination guard --------------------------------------------------


def test_the_plan_is_deterministic_and_declares_disjoint_targets():
    a = fixture.plan(48, 7, "smoke")
    assert a == fixture.plan(48, 7, "smoke")
    assert a["tags"] != fixture.plan(48, 8, "smoke")["tags"]
    t = a["targets"]
    item_targets = ([x for p in t["merge_pairs"] for x in p] + t["retype"] + t["split"]
                    + t["deprecate_items"] + t["hide_items"] + t["unhide_items"] + t["promote"]
                    + [x for p in t["duplicate_pairs"] for x in p] + t["synth_topics"]
                    + [c["tag"] for c in t["contention"]])
    assert len(item_targets) == len(set(item_targets))
    docs = [d for v in t["sources"].values() for d in v] + t["claim_docs"]
    assert len(docs) == len(set(docs))
    assert not set(docs) & {c["doc"] for c in t["contention"]}


def test_the_plan_refuses_a_scale_below_the_minimum():
    with pytest.raises(fixture.FixtureRefused):
        fixture.plan(fixture.MIN_SCALE - 1, 1, "smoke")


def test_the_destination_guard_refuses_the_live_vault_and_nonempty_directories(tmp_path):
    with pytest.raises(fixture.FixtureRefused):
        fixture.check_destination(ROOT)
    with pytest.raises(fixture.FixtureRefused):
        fixture.check_destination(ROOT / "bench-inside")
    (tmp_path / "x").write_text("keep me")
    with pytest.raises(fixture.FixtureRefused):
        fixture.check_destination(tmp_path)
    assert (tmp_path / "x").read_text() == "keep me"            # nothing deleted or overwritten
    assert fixture.check_destination(tmp_path / "new") == (tmp_path / "new").resolve()


def test_a_padded_pdf_has_the_declared_size_and_extractable_text():
    import io

    from pypdf import PdfReader
    raw = fixture.pdf_bytes([["Synthetic Report D0001", "Record D0001 N0000 notes that x."]],
                            200_000, "seed")
    assert abs(len(raw) - 200_000) < 400
    text = PdfReader(io.BytesIO(raw)).pages[0].extract_text()
    assert "Synthetic Report D0001" in text and "Record D0001 N0000" in text


# --- fixture verification and owned scratch space (7c-1 review) ---------------------------------


def _fake_fixture(tmp_path, profile="light"):
    """A minimal fixture: a profile vault with raw bytes, a graph db and a review payload, and a
    declaration whose fingerprint matches it. Enough for every check that runs before the runner
    creates anything."""
    fx = tmp_path / "fx"
    vault = fx / profile
    for rel, data in (("raw/inbox/a.pdf", b"%PDF-1.4 raw"), ("db/graph.sqlite", b"graph"),
                      ("reviews/approved/rev_1.json", b'{"type": "hide_content"}'),
                      ("app/x.py", b"x = 1\n")):
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_bytes(data)
    decl = {"schema_version": bench.DECLARATION_SCHEMA,
            "profiles": {profile: {"fingerprint": fixture.content_fingerprint(vault),
                                   "review_ids": {}, "contention_targets": []}}}
    (fx / "fixture.json").write_text(json.dumps(decl))
    return fx, vault


def _run(fx, profile="light", out=None):
    return bench.run(fx, profile, warmup=0, warm=0, contention=0, uncontended=0, storage=0, cold=0,
                     interval_ms=20, out_dir=out or fx.parent / "reports")


def test_an_escaping_work_link_is_refused_and_the_external_directory_is_preserved(tmp_path):
    fx, _ = _fake_fixture(tmp_path)
    outside = tmp_path / "unrelated"
    (outside / "light").mkdir(parents=True)
    (outside / "light" / "precious.txt").write_text("keep me")
    (outside / "precious.txt").write_text("keep me too")
    (fx / "_work").symlink_to(outside, target_is_directory=True)
    with pytest.raises(bench.BenchRefused, match="_work"):
        _run(fx)
    assert (outside / "light" / "precious.txt").read_text() == "keep me"
    assert (outside / "precious.txt").read_text() == "keep me too"
    assert sorted(p.name for p in outside.iterdir()) == ["light", "precious.txt"]
    assert not (fx.parent / "reports").exists()


def test_deletion_is_confined_to_the_runs_own_directory_and_never_follows_a_link(tmp_path):
    fx, _ = _fake_fixture(tmp_path)
    run_dir, run_id = bench.open_work(fx.resolve(), "light")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    (run_dir / "vault").symlink_to(outside, target_is_directory=True)    # a planted link
    with pytest.raises(bench.BenchRefused, match="link"):
        bench.remove_owned(run_dir / "vault", run_dir, run_id)
    with pytest.raises(bench.BenchRefused, match="outside"):
        bench.remove_owned(outside, run_dir, run_id)
    with pytest.raises(bench.BenchRefused, match="outside"):
        bench.remove_owned(fx / "light", run_dir, run_id)               # the pristine vault
    assert (outside / "f.txt").exists() and (fx / "light" / "db" / "graph.sqlite").exists()
    (run_dir / "vault").unlink()
    bench.close_work(run_dir, run_id)
    assert not run_dir.exists() and not (fx / "_work").exists()


def test_concurrent_runners_own_separate_directories_and_never_delete_each_others(tmp_path):
    fx, _ = _fake_fixture(tmp_path)
    fxr = fx.resolve()
    a_dir, a_id = bench.open_work(fxr, "light")
    b_dir, b_id = bench.open_work(fxr, "light")                          # same profile, same time
    assert a_dir != b_dir and a_dir.parent == b_dir.parent == fxr / "_work"
    (b_dir / "vault").mkdir()
    (b_dir / "vault" / "state.json").write_text("{}")
    with pytest.raises(bench.BenchRefused):
        bench.remove_owned(b_dir, a_dir, a_id)                          # not A's directory
    with pytest.raises(bench.BenchRefused, match="not this run's"):
        bench.remove_owned(b_dir / "vault", b_dir, a_id)                # A's id is not B's run
    (b_dir / bench.RUN_MARKER).write_text(json.dumps({"run_id": "someone-else"}))
    with pytest.raises(bench.BenchRefused, match="marker"):
        bench.remove_owned(b_dir / "vault", b_dir, b_dir.name)          # a forged marker
    bench.close_work(a_dir, a_id)                                       # A finishes first
    assert not a_dir.exists()
    assert (b_dir / "vault" / "state.json").exists() and (fxr / "_work").is_dir()
    # A leftover directory from an interrupted run is never deleted by a later run.
    leftover = fxr / "_work" / "light-interrupted"
    leftover.mkdir()
    (leftover / "partial").write_text("x")
    c_dir, c_id = bench.open_work(fxr, "light")
    bench.close_work(c_dir, c_id)
    assert (leftover / "partial").exists() and (b_dir / "vault" / "state.json").exists()


def test_a_fixture_mutated_after_the_build_is_refused_before_anything_is_created(tmp_path):
    for i, (rel, data) in enumerate((("raw/inbox/a.pdf", b"%PDF-1.4 RAW"),       # raw bytes
                                     ("db/graph.sqlite", b"graph2"),               # graph contents
                                     ("reviews/approved/rev_1.json",                # review payload
                                      b'{"type": "archive_source"}'),
                                     ("reviews/approved/rev_2.json", b"{}"))):     # an added file
        fx, vault = _fake_fixture(tmp_path / str(i))
        (vault / rel).write_bytes(data)
        with pytest.raises(bench.BenchRefused, match="fingerprint"):
            _run(fx)
        assert not (fx / "_work").exists() and not (fx.parent / "reports").exists()


def test_profile_vaults_with_escaping_links_or_an_old_declaration_are_refused(tmp_path):
    fx, vault = _fake_fixture(tmp_path / "a")
    outside = tmp_path / "secret.txt"
    outside.write_text("outside")
    (vault / "raw" / "inbox" / "link.pdf").symlink_to(outside)
    with pytest.raises(bench.BenchRefused, match="symbolic link"):
        _run(fx)
    # The fingerprint itself hashes a link as a link, never the content it points to.
    with_link = fixture.content_fingerprint(vault)
    outside.write_text("changed")
    assert fixture.content_fingerprint(vault) == with_link
    fx, vault = _fake_fixture(tmp_path / "b")
    decl = json.loads((fx / "fixture.json").read_text())
    (fx / "fixture.json").write_text(json.dumps(dict(decl, schema_version=1)))
    with pytest.raises(bench.BenchRefused, match="schema"):
        _run(fx)
    assert not (fx / "_work").exists()


def test_the_code_fingerprint_covers_only_what_the_sandbox_executes(tmp_path):
    root = tmp_path / "v"
    for rel in ("app/a.py", "scripts/s.py", "templates/t.md", "wiki/p.md", "app/__pycache__/a.pyc"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel)
    before = fixture.code_fingerprint(root)
    (root / "wiki" / "p.md").write_text("changed")                      # vault state: not code
    (root / "app" / "__pycache__" / "a.pyc").write_text("changed")     # cache: not code
    assert fixture.code_fingerprint(root) == before
    (root / "scripts" / "s.py").write_text("changed")
    assert fixture.code_fingerprint(root) != before


# --- end to end at the smoke preset --------------------------------------------------------------


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    dest = tmp_path_factory.mktemp("bench") / "fx"
    decl = fixture.build(dest, scale=fixture.MIN_SCALE, seed=3, preset="smoke")
    return dest, decl


def test_the_smoke_fixture_validates_and_declares_both_queues(built):
    dest, decl = built
    assert decl["label"] == "synthetic smoke" and decl["generator_version"] == fixture.GENERATOR_VERSION
    heavy, light = decl["profiles"]["heavy"], decl["profiles"]["light"]
    assert heavy["total"] == 50 and light["total"] == 5
    from app.backend.main import _APPLY_TYPES
    assert set(heavy["counts_by_type"]) == set(_APPLY_TYPES)             # every executor type
    assert light["counts_by_type"] == {"archive_source": 1, "change_item_type": 1, "hide_content": 1,
                                       "merge_items": 1, "promote_candidate_node": 1}
    assert {f["type"] for f in heavy["fanout"]} == {"merge_items", "change_item_type", "split_item"}
    assert heavy["fingerprint"].startswith("sha256:") and heavy["fingerprint"] != light["fingerprint"]
    assert decl["corpus_counts"]["raw"]["files"] == fixture.MIN_SCALE
    assert json.loads((dest / "fixture.json").read_text()) == decl
    assert len(heavy["contention_targets"]) == 2


def test_the_runner_reports_valid_trials_with_ordered_timings(built, tmp_path):
    dest, _ = built
    path, dg = bench.run(dest, "light", warmup=0, warm=1, contention=1, uncontended=1, storage=1,
                         cold=0, interval_ms=20, out_dir=tmp_path)
    raw = path.read_bytes()
    report = json.loads(raw)
    assert dg == bench.digest(report) and raw == bench.canonical(report)
    assert report["report_schema"] == bench.REPORT_SCHEMA and report["judged"] is False
    assert report["acceptance"]["verdict"] == "not_judged"
    declared = json.loads((dest / "fixture.json").read_text())
    assert report["fixture"]["fingerprint"] == declared["profiles"]["light"]["fingerprint"]
    assert report["fixture"]["fingerprint_verified"] is True
    assert report["code_fingerprints"]["match"] is True
    assert report["code_fingerprints"]["declared"] == declared["code_fingerprint"]
    assert report["percentile_method"].startswith("nearest-rank")
    assert report["errors"] == [], report["errors"]
    sets = {t["set"]: t for t in report["trials"]["detail"]}
    assert all(t["valid"] for t in report["trials"]["detail"]), report["trials"]["invalid_by_reason"]
    assert sets["contention"]["overlap"] is True and sets["contention"]["keep_outcome"] == "kept"
    assert sets["storage"]["storage"]["samples"] >= 1
    assert report["storage"]["logical_copied_bytes"] > 0
    for key in ("lock_wait", "lock_held", "after_lock_copy", "sandbox_run", "cleanup", "total"):
        assert sets["warm"]["phases_ms"][key] >= 0                # marks in order (no timing bound)
    assert set(sets["warm"]["sandbox_phases_ms"]) == {"snapshot_before", "run_apply",
                                                      "snapshot_after_and_diff"}
    text = raw.decode()
    assert str(dest) not in text and str(ROOT) not in text and str(Path.home()) not in text
    assert not (dest / "_work").exists()                          # scratch removed; fixture intact
    assert fixture.content_fingerprint(dest / "light") == declared["profiles"]["light"]["fingerprint"]


def test_preview_timings_derive_phases_from_the_marks():
    """The preview's additive `timings`: durations derived from the monotonic marks."""
    from app.backend import apply_gate
    marks = {"started": 0, "lock_requested": 1, "lock_acquired": 3, "lock_released": 10,
             "after_lock_copied": 12, "sandbox_evaluated": 40, "sandbox_cleaned": 41,
             "token_lock_requested": 42, "token_lock_acquired": 43, "token_lock_released": 45,
             "finished": 46}
    t = apply_gate.preview_timings({k: v * 1_000_000 for k, v in marks.items()}, {"run_apply": 5.0})
    assert t["phases_ms"]["lock_wait"] == 2.0 and t["phases_ms"]["lock_held"] == 7.0
    assert t["phases_ms"]["sandbox_run"] == 28.0 and t["phases_ms"]["total"] == 46.0
    assert t["sandbox_phases_ms"] == {"run_apply": 5.0}
    partial = apply_gate.preview_timings({"started": 0, "lock_requested": 5})
    assert partial["phases_ms"] == {}                             # only phases whose marks exist
