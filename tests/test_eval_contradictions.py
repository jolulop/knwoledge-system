"""ADR-0069 slice 3: the contradiction-judge evaluation gate (`scripts/eval_contradictions.py`).

Deterministic, with a scripted fake judge and no live model: the fixture's structural checks; the
gates (unreviewed labels, unset thresholds, paid authorization, a chain, the whole-evaluation budget)
stop before any call; the production estimate → run path is what runs; scoring by label with false
positives reported per label; coverage gates correctness; claim-order permutations and order
sensitivity; the relative criterion for a local model; and no vault state is written.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import eval_contradictions as ev  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from app.llm.cache import ResponseCache  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.llm.usage import UsageLedger  # noqa: E402
from app.workers import claims  # noqa: E402
from app.workers import contradiction_check as cc  # noqa: E402
from tests.test_contradiction_check import Judge  # noqa: E402

HOSTED = "anthropic:judge-model"
LOCAL = "local:judge-model"


def _fixture(*, reviewed=True, recall=1.0, fps=(0, 0, 0), permutations=2):
    fx = copy.deepcopy(ev.load_fixture(ev.FIXTURE))
    fx["review"]["labels_reviewed"] = reviewed
    fx["thresholds"] = {"min_contradiction_recall": recall,
                        "max_false_positives": dict(zip(ev.FP_LABELS, fps))}
    fx["settings"]["permutations"] = permutations
    return fx


def _cid(fx, fixture_id):
    return claims.claim_id(ev._claims(fx)[fixture_id]["text"])


def _pairs(fx, label):
    return [(_cid(fx, r[0]), _cid(fx, r[1])) for r in fx["labels"]["pairs"] if r[2] == label]


def _settings(tmp_path, **kw):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    base = dataclasses.replace(
        get_settings(vault), contradiction_model=None, contradiction_max_calls=50,
        contradiction_max_input_tokens=400_000, contradiction_max_output_tokens=512,
        enrich_local_base_url=None)
    return dataclasses.replace(base, **kw)


def _factory(hosted=None, local=None):
    hosted = hosted if hosted is not None else Judge()
    local = local if local is not None else Judge(available=False)

    def make(st):
        return LLMClient({"anthropic": hosted, "local": local, "openai": Judge(available=False)},
                         cache=ResponseCache(st.response_cache_path),
                         ledger=UsageLedger(st.jobs_db_path))
    return make


def _eval(tmp_path, fx, judge=None, *, model=HOSTED, allow_paid=True, local=None, **kw):
    kw.setdefault("real_settings", _settings(tmp_path))
    return ev.evaluate(fx, model_ref=model, allow_paid=allow_paid,
                       client_factory=_factory(judge, local), now="2026-11-01T10:00:00+00:00",
                       **kw)


# --- the fixture -----------------------------------------------------------------------------------


def test_the_committed_fixture_is_valid_and_forces_windowing(tmp_path):
    fx = ev.load_fixture(ev.FIXTURE)
    assert ev.validate_fixture(fx) == []
    result = _eval(tmp_path, fx, dry_run=True)
    assert result["dry_run"] and "verdict" not in result
    for p in result["permutations"]:
        assert p["noncorresponding_contradictions"]
        for w in p["window_plan"]:
            x, y = ev.window_index(w["id"])
            assert x >= 1 and y >= 1
        assert {ev.window_index(w["id"]) for w in p["window_plan"]} >= {(1, 2), (2, 1)}


@pytest.mark.parametrize("mutate, problem", [
    (lambda fx: fx["labels"]["pairs"].pop(0), "share the topic 'ferry-passengers'"),
    (lambda fx: fx["documents"][1]["claims"][0].update(
        text=fx["documents"][0]["claims"][0]["text"]), "have the same wording"),
    (lambda fx: fx["labels"]["pairs"].append(["A01", "A02", "unrelated"]), "pairs two claims"),
    (lambda fx: fx["labels"]["pairs"][0].__setitem__(2, "conflict"), "unknown label"),
    (lambda fx: fx["documents"][1].update(author=fx["documents"][0]["author"]),
     "authors must be distinct"),
    (lambda fx: fx["labels"]["pairs"].append(["A01", "B01", "agreement"]), "labelled twice"),
    (lambda fx: fx["settings"].update(window_chars=0), "settings.window_chars"),
    (lambda fx: fx["documents"][0].update(filename="../outside.md"), "single safe basename"),
    (lambda fx: fx["documents"][0].update(filename="/tmp/outside.md"), "single safe basename"),
    (lambda fx: fx["documents"][0].update(filename="..\\outside.md"), "single safe basename"),
])
def test_a_malformed_fixture_is_rejected(mutate, problem):
    fx = copy.deepcopy(ev.load_fixture(ev.FIXTURE))
    mutate(fx)
    assert any(problem in p for p in ev.validate_fixture(fx)), ev.validate_fixture(fx)


def test_a_window_size_that_fits_a_document_in_one_window_is_rejected(tmp_path):
    fx = _fixture()
    fx["settings"]["window_chars"] = 100_000
    judge = Judge()
    with pytest.raises(ev.FixtureError, match="fits in one window"):
        _eval(tmp_path, fx, judge)
    assert judge.calls == []


# --- the gates: nothing is sent --------------------------------------------------------------------


def test_unreviewed_labels_stop_the_evaluation_before_any_call(tmp_path):
    judge = Judge()
    with pytest.raises(ev.GateError, match="labels are a draft"):
        _eval(tmp_path, _fixture(reviewed=False), judge)
    assert judge.calls == []


@pytest.mark.parametrize("field", ["min_contradiction_recall", "agreement", "apparent_conflict",
                                   "unrelated"])
def test_an_unset_threshold_stops_the_evaluation_before_any_call(tmp_path, field):
    fx = _fixture()
    if field == "min_contradiction_recall":
        fx["thresholds"][field] = None
    else:
        fx["thresholds"]["max_false_positives"][field] = None
    judge = Judge()
    with pytest.raises(ev.GateError, match=field):
        _eval(tmp_path, fx, judge)
    assert judge.calls == []


def _no_run(monkeypatch):
    """The evaluator's own gates refuse before any run starts (not only the run's refusal)."""
    def boom(*a, **kw):
        raise AssertionError("a run was started")
    monkeypatch.setattr(ev.cc, "run", boom)


def test_a_paid_model_needs_allow_paid_and_a_chain_is_refused(tmp_path, monkeypatch):
    _no_run(monkeypatch)
    monkeypatch.setattr(ev, "materialize", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("the vault was built before the paid check")))
    judge = Judge()
    with pytest.raises(cc.Refused) as exc:
        _eval(tmp_path, _fixture(), judge, allow_paid=False)
    assert exc.value.code == "paid_authorization_required"
    with pytest.raises(cc.Refused):
        _eval(tmp_path, _fixture(), judge, model=f"{HOSTED},{LOCAL}")
    assert judge.calls == []


def test_an_unconfigured_model_is_refused_before_any_call(tmp_path, monkeypatch):
    _no_run(monkeypatch)
    judge = Judge(available=False)
    with pytest.raises(cc.Refused) as exc:
        _eval(tmp_path, _fixture(), judge)
    assert exc.value.code == "model_unavailable" and judge.calls == []


def test_the_whole_evaluation_must_fit_the_configured_budget(tmp_path):
    judge = Judge()
    with pytest.raises(cc.Refused) as exc:   # 2 permutations × 12 windows > 20 calls
        _eval(tmp_path, _fixture(), judge, real_settings=_settings(tmp_path,
                                                                    contradiction_max_calls=20))
    assert exc.value.code == "over_budget" and judge.calls == []


# --- the production path, scoring and the verdict --------------------------------------------------


def test_a_perfect_hosted_judge_passes_and_the_result_records_its_run(tmp_path):
    fx = _fixture()
    judge = Judge(contradict=_pairs(fx, "contradiction"),
                  usage=lambda i: {"family": "anthropic", "model": "judge-model-20260101",
                                   "input_tokens": 900, "output_tokens": 40})
    result = _eval(tmp_path, fx, judge)
    assert result["verdict"]["status"] == "pass", result["verdict"]
    assert result["verdict"]["worst_recall"] == 1.0
    assert result["verdict"]["worst_false_positives"] == {
        "agreement": 0, "apparent_conflict": 0, "unrelated": 0}
    assert len(judge.calls) == result["planned"]["calls"] == 24
    assert result["model_ref"] == HOSTED and result["paid"] is True
    assert result["reported_models"] == ["judge-model-20260101"]
    assert result["prompt_version"] == cc.art.CONTRADICTION_PROMPT_VERSION
    assert result["settings"]["window_chars"] == fx["settings"]["window_chars"]
    assert result["settings"]["order_seeds"] == [None, 1]
    assert result["fixture_digest"] == ev.fixture_digest(fx)
    for p in result["permutations"]:
        assert p["run"]["status"] == "succeeded" and p["run"]["coverage"]["complete"]
        assert p["cache"] == {"expected_hits": 0, "from_cache": 0}
        assert p["run"]["budget"]["calls"] == 12
        assert p["run"]["budget"]["reported_input_tokens"] == 12 * 900
        assert sorted(sorted(x["claims"]) for x in p["predictions"]) == sorted(
            sorted([r[0], r[1]]) for r in fx["labels"]["pairs"] if r[2] == "contradiction")
        ids = {c for w in p["window_plan"] for c in w["claims_a"] + w["claims_b"]}
        assert ids == set(ev._claims(fx))                  # the plan names fixture claims
    assert result["verdict"]["order_sensitive_pairs"] == []


def test_false_positives_are_counted_per_label_and_can_fail_the_verdict(tmp_path):
    fx = _fixture(fps=(0, 1, 5))
    wrong = _pairs(fx, "apparent_conflict")[:2] + _pairs(fx, "agreement")[:1]
    judge = Judge(contradict=_pairs(fx, "contradiction") + wrong)
    v = _eval(tmp_path, fx, judge)["verdict"]
    assert v["worst_false_positives"] == {"agreement": 1, "apparent_conflict": 2, "unrelated": 0}
    assert v["status"] == "fail"
    assert any("2 apparent_conflict false positive(s)" in r for r in v["reasons"])
    assert any("1 agreement false positive(s)" in r for r in v["reasons"])


def test_missed_contradictions_lower_recall_below_the_threshold(tmp_path):
    fx = _fixture(recall=0.9)
    judge = Judge(contradict=_pairs(fx, "contradiction")[1:])
    result = _eval(tmp_path, fx, judge)
    s = result["permutations"][0]["score"]
    assert s["missed"] == ["A01/B01"] and s["recall"] == pytest.approx(6 / 7)
    assert result["verdict"]["status"] == "fail"


def test_incomplete_coverage_prevents_a_pass_even_with_correct_verdicts(tmp_path):
    fx = _fixture(recall=0.0, fps=(99, 99, 999))
    judge = Judge(contradict=_pairs(fx, "contradiction"), script=["truncate"])
    v = _eval(tmp_path, fx, judge)["verdict"]
    assert v["status"] == "fail"
    assert any("permutation 0 is not a clean, complete run" in r for r in v["reasons"])


def test_a_parse_error_prevents_a_pass(tmp_path):
    fx = _fixture(recall=0.0, fps=(99, 99, 999))
    judge = Judge(contradict=_pairs(fx, "contradiction"), script=["ok"] * 12 + ["schema"] * 3)
    v = _eval(tmp_path, fx, judge)["verdict"]
    assert v["status"] == "fail"
    assert any("permutation 1 is not a clean, complete run" in r for r in v["reasons"])


def test_permutations_reorder_the_claims_and_order_sensitivity_is_reported(tmp_path):
    fx = _fixture(permutations=3)
    truth = {frozenset(p) for p in _pairs(fx, "contradiction")}

    def leaders_only(a, b):                  # "sees" a contradiction only for list A's first claim
        return {"contradictions": [{"claim_a": a[0], "claim_b": y, "confidence": 0.9,
                                    "explanation": "x"} for y in b
                                   if frozenset((a[0], y)) in truth]}
    judge = Judge(script=[leaders_only] * 36)
    result = _eval(tmp_path, fx, judge)
    plans = [p["window_plan"] for p in result["permutations"]]
    assert len({json.dumps(p) for p in plans}) == 3                # three different orders
    labelled = {frozenset(r[:2]) for r in fx["labels"]["pairs"] if r[2] == "contradiction"}
    expected = [{"/".join(sorted((w["claims_a"][0], y))) for w in plan for y in w["claims_b"]
                 if frozenset((w["claims_a"][0], y)) in labelled}
                for plan in plans]
    assert [{"/".join(sorted(x["claims"])) for x in p["predictions"]}
            for p in result["permutations"]] == expected
    sensitive = sorted(set.union(*expected) - set.intersection(*expected))
    assert sensitive, "the seeds no longer exercise order sensitivity"
    assert result["verdict"]["order_sensitive_pairs"] == sensitive


def test_order_sensitivity_lists_pairs_found_in_only_some_permutations():
    fx = _fixture()
    perms = [{"index": 0, "run": {"status": "succeeded", "coverage": {"complete": True}},
              "predictions": [{"claims": ["A01", "B01"]}],
              "score": ev.score(fx, [("A01", "B01")])},
             {"index": 1, "run": {"status": "succeeded", "coverage": {"complete": True}},
              "predictions": [], "score": ev.score(fx, [])}]
    v = ev.verdict(fx, perms, local=False, baseline=None)
    assert v["order_sensitive_pairs"] == ["A01/B01"] and v["worst_recall"] == 0.0


def test_predictions_on_unlabelled_pairs_are_unscored_without_a_default():
    fx = _fixture()
    fx["labels"]["default"] = None
    s = ev.score(fx, [("A12", "C12"), ("A01", "B01")])
    assert s["unscored"] == ["A12/C12"] and s["found"] == ["A01/B01"]
    assert s["false_positive_counts"] == {"agreement": 0, "apparent_conflict": 0, "unrelated": 0}


# --- a local model and the relative criterion ------------------------------------------------------


def test_a_local_model_needs_a_hosted_baseline(tmp_path):
    fx = _fixture()
    local = Judge(contradict=_pairs(fx, "contradiction"))
    result = _eval(tmp_path, fx, Judge(available=False), model=LOCAL, allow_paid=False,
                   local=local)
    assert result["paid"] is False and len(local.calls) == 24
    assert result["verdict"]["status"] == "incomplete"
    assert "--baseline" in result["verdict"]["relative"]["detail"]


def test_a_local_model_must_match_the_hosted_judge(tmp_path):
    fx = _fixture(fps=(0, 3, 0))
    hosted = _eval(tmp_path / "h", fx, Judge(contradict=_pairs(fx, "contradiction")))
    assert hosted["verdict"]["status"] == "pass"
    # Within the absolute thresholds, but more apparent conflicts flagged than the hosted judge.
    local = Judge(contradict=_pairs(fx, "contradiction") + _pairs(fx, "apparent_conflict")[:1])
    v = _eval(tmp_path / "l", fx, Judge(available=False), model=LOCAL, allow_paid=False,
              local=local, baseline=hosted)["verdict"]
    assert v["status"] == "fail" and v["relative"]["status"] == "fail"
    assert any("above the hosted judge's 0" in r for r in v["reasons"])
    same = Judge(contradict=_pairs(fx, "contradiction"))
    v = _eval(tmp_path / "s", fx, Judge(available=False), model=LOCAL, allow_paid=False,
              local=same, baseline=hosted)["verdict"]
    assert v["status"] == "pass" and v["relative"]["status"] == "pass"


def test_a_baseline_from_another_fixture_or_a_local_model_does_not_count():
    fx = _fixture()
    perms = [{"index": 0, "run": {"status": "succeeded", "coverage": {"complete": True}},
              "predictions": [], "score": ev.score(fx, [])}]
    other = {"fixture_digest": "x", "paid": True, "permutations": [],
             "verdict": {"worst_recall": 0.0, "worst_false_positives": {"apparent_conflict": 0}}}
    rel = ev.verdict(fx, perms, local=True, baseline=other)["relative"]
    assert "a different fixture" in " ".join(rel["reasons"])
    other |= {"fixture_digest": ev.fixture_digest(fx), "paid": False}
    rel = ev.verdict(fx, perms, local=True, baseline=other)["relative"]
    assert "not a hosted judge's result" in " ".join(rel["reasons"])


# --- no vault state --------------------------------------------------------------------------------


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file()}


def test_the_evaluation_writes_no_vault_state(tmp_path, monkeypatch, capsys):
    st = _settings(tmp_path)
    (st.root / "db").mkdir()
    (st.root / "db" / "marker.txt").write_text("untouched")
    (st.root / "reviews" / "pending").mkdir(parents=True)
    before = _snapshot(st.root)
    fx_path = tmp_path / "fixture.yaml"
    fx_path.write_text(json.dumps(_fixture()), encoding="utf-8")   # JSON is YAML
    out = tmp_path / "results"
    workdirs: list[Path] = []
    real_mkdtemp = ev.tempfile.mkdtemp
    monkeypatch.setattr(ev.tempfile, "mkdtemp",
                        lambda **kw: workdirs.append(Path(real_mkdtemp(**kw))) or workdirs[-1])
    fx = _fixture()
    code = ev.main(["--model", HOSTED, "--allow-paid", "--fixture", str(fx_path),
                    "--out-dir", str(out)], settings=st,
                   client_factory=_factory(Judge(contradict=_pairs(fx, "contradiction"))))
    assert code == 0, capsys.readouterr()
    assert _snapshot(st.root) == before                       # the vault is untouched
    assert [p.name.startswith("contradiction_judge_") for p in out.iterdir()] == [True]
    assert workdirs and not workdirs[0].exists()              # the throwaway vault is gone
    assert "Verdict: PASS" in capsys.readouterr().out


def test_the_cli_exit_codes(tmp_path, capsys):
    st = _settings(tmp_path)
    draft = tmp_path / "draft.yaml"
    draft.write_text(json.dumps(_fixture(reviewed=False)), encoding="utf-8")
    judge = Judge()
    args = ["--model", HOSTED, "--allow-paid", "--fixture", str(draft),
            "--out-dir", str(tmp_path / "r")]
    assert ev.main(args, settings=st, client_factory=_factory(judge)) == 2   # gate
    assert ev.main(args + ["--dry-run"], settings=st, client_factory=_factory(judge)) == 0
    assert judge.calls == [] and not (tmp_path / "r").exists()
    failing = tmp_path / "fail.yaml"
    failing.write_text(json.dumps(_fixture()), encoding="utf-8")
    args[args.index(str(draft))] = str(failing)
    assert ev.main(args, settings=st, client_factory=_factory(Judge())) == 1   # recall 0
    assert len(list((tmp_path / "r").iterdir())) == 1
    capsys.readouterr()


def test_a_permutation_without_a_non_corresponding_contradiction_is_rejected(tmp_path,
                                                                              monkeypatch):
    monkeypatch.setattr(ev, "noncorresponding_contradictions", lambda fx, plan, ids: [])
    judge = Judge()
    with pytest.raises(ev.FixtureError, match="non-corresponding windows"):
        _eval(tmp_path, _fixture(), judge)
    assert judge.calls == []


def test_retries_count_against_the_whole_evaluation_budget(tmp_path):
    fx = _fixture(recall=0.0, fps=(99, 99, 999))
    # Planned 2 × 12 = 24 ≤ 28; three windows of the first run fail after 3 attempts each, so
    # it spends 18 and leaves 10 — too few for the second run's 12.
    judge = Judge(contradict=_pairs(fx, "contradiction"), script=["schema"] * 9)
    result = _eval(tmp_path, fx, judge, real_settings=_settings(tmp_path,
                                                                 contradiction_max_calls=28))
    assert len(judge.calls) == 18                              # nothing past the configured cap
    assert result["permutations"][1]["refused"].startswith("over_budget")
    assert result["verdict"]["status"] == "fail"
    assert any("permutation 1 was refused" in r for r in result["verdict"]["reasons"])


def test_reported_tokens_do_not_release_the_whole_evaluation_character_allowance(tmp_path):
    fx = _fixture()
    planned = _eval(tmp_path / "plan", fx, dry_run=True)
    token_cap = planned["planned"]["estimated_input_tokens"]
    # The plan fits exactly, but two schema retries spend extra input characters. Reporting
    # few tokens never gives those characters back to the next permutation.
    judge = Judge(contradict=_pairs(fx, "contradiction"), script=["schema", "schema"],
                  usage=lambda i: {"family": "anthropic", "input_tokens": 0,
                                   "cache_read_tokens": 0, "cache_write_tokens": 0,
                                   "output_tokens": 1})
    result = _eval(tmp_path / "run", fx, judge, real_settings=_settings(
        tmp_path / "config", contradiction_max_input_tokens=token_cap))
    assert result["permutations"][0]["run"]["status"] == "succeeded"
    assert result["permutations"][1]["refused"].startswith("over_budget")
    assert len(judge.calls) == 14
    assert sum(p.get("run", {}).get("budget", {}).get("input_chars", 0)
               for p in result["permutations"]) <= token_cap * cc.CHARS_PER_TOKEN
    assert result["verdict"]["status"] == "fail"


@pytest.mark.parametrize("change", ["missing_runs", "coverage", "prompt", "schema",
                                    "output_limit", "order"])
def test_an_incompatible_or_incomplete_baseline_is_refused_before_any_call(tmp_path, change):
    fx = _fixture()
    hosted = _eval(tmp_path / "hosted", fx, Judge(contradict=_pairs(fx, "contradiction")))
    if change == "missing_runs":
        hosted["permutations"] = []
    elif change == "coverage":
        hosted["permutations"][0]["run"]["coverage"]["complete"] = False
    elif change == "prompt":
        hosted["prompt_version"] = "another-prompt"
    elif change == "schema":
        hosted["schema_version"] = "another-schema"
    elif change == "output_limit":
        hosted["settings"]["max_output_tokens"] += 1
    else:
        hosted["settings"]["order_seeds"] = [None, 999]
    local = Judge(contradict=_pairs(fx, "contradiction"))
    with pytest.raises(ev.GateError, match="invalid hosted baseline"):
        _eval(tmp_path / "local", fx, model=LOCAL, allow_paid=False, local=local,
              baseline=hosted)
    assert local.calls == []


def test_reviewed_fixture_has_fixed_thresholds_and_an_unambiguous_fleet_label():
    fx = ev.load_fixture(ev.FIXTURE)
    assert ev.gate_problems(fx) == []
    assert fx["review"]["reviewed_by"].startswith("Codex")
    assert fx["review"]["reviewed_at"] == "2026-11-01"
    assert fx["thresholds"] == {"min_contradiction_recall": 0.85,
                                "max_false_positives": {"agreement": 0,
                                                        "apparent_conflict": 1, "unrelated": 0}}
    c09 = ev._claims(fx)["C09"]
    assert "No hybrid or electric ferries entered service" in c09["text"]
    assert "in 2024" in c09["text"]
    truth = _pairs(fx, "contradiction")
    score = ev.score(fx, [tuple(r[:2]) for r in fx["labels"]["pairs"]
                          if r[2] == "contradiction"][1:])
    assert len(truth) == 7 and score["recall"] >= fx["thresholds"]["min_contradiction_recall"]
