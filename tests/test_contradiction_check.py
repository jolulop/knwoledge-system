"""ADR-0069 slice 2(b): scoped contradiction checks — estimate, token, run.

Deterministic tests, with a scripted fake judge and no live model: complete A×B window coverage;
token binding, atomic consumption and refusal on relevant change; reservations before every attempt,
retries re-reserved and counted; oversized plans refused; truncated output is a failed window; an
output-limit violation stops and marks coverage incomplete; a missing model, a chain value, a missing
`--allow-paid` and a local connection failure each make zero hosted calls; coverage records list the
completed and remaining windows.
"""
from __future__ import annotations

import dataclasses
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tests.test_contradictions as con_t  # noqa: E402
from app.backend import graph, manifests  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from app.llm.adapters import AdapterError  # noqa: E402
from app.llm.cache import ResponseCache  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.llm.usage import UsageLedger, summarize  # noqa: E402
from app.workers import claims, contradiction_check as cc, contradictions, extract, intake  # noqa: E402
from app.workers import reviews, wiki  # noqa: E402
from tests import fixtures  # noqa: E402

MD = "raw/manifests"
DOCS = {
    "a.md": ("# A\n\nThe alpha index rose in May. The beta count fell in June. "
             "The gamma rate held in July.\n"),
    "b.md": "# B\n\nThe alpha index fell in May. The delta score rose in June.\n",
    "c.md": "# C\n\nThe epsilon level rose in March.\n",
}
SENTENCES = [s.strip() + "." for text in DOCS.values()
             for s in text.split("\n\n", 1)[1].strip().rstrip(".").split(". ")]
HOSTED = "anthropic:judge-model"
LOCAL = "local:judge-model"


class SentenceClaims:
    """Fake claims adapter: one claim per sentence of the document (claim = sentence minus '.')."""
    name = "anthropic"

    def __init__(self, longer: dict[str, str] | None = None):
        self.longer = longer or {}

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        body = messages[-1]["content"]
        return {"claims": [{"claim": s[:-1], "quote": self.longer.get(s, s)}
                           for s in SENTENCES if s in body]}


_ID = re.compile(r"^\[(clm_[0-9a-f]{16})\] ", re.M)


def _ids(user: str, block: str) -> list[str]:
    return _ID.findall(user.split(f"<{block}>", 1)[1].split(f"</{block}>", 1)[0])


class Judge:
    """Scripted fake judge. `script[i]` is the i-th attempt's action: "ok", "schema" (invalid
    output), "truncate" (stopped at the output limit), "down" (connection failure) or a callable
    `(ids_a, ids_b) -> response`. `usage(i)` is the i-th attempt's reported usage."""

    def __init__(self, *, contradict=(), script=(), usage=None, available=True, on_call=None):
        self.contradict = {frozenset(p) for p in contradict}
        self.script = list(script)
        self.usage = usage
        self._available = available
        self.on_call = on_call
        self.calls: list[tuple[list[str], list[str], int]] = []

    def available(self):
        return self._available

    def parse_with_usage(self, messages, schema, model_id, *, max_tokens):
        user = messages[-1]["content"]
        a, b = _ids(user, "claims_a"), _ids(user, "claims_b")
        i = len(self.calls)
        self.calls.append((a, b, max_tokens))
        if self.on_call:
            self.on_call(i)
        action = self.script[i] if i < len(self.script) else "ok"
        usage = self.usage(i) if self.usage else None
        if action == "down":
            raise AdapterError("connection refused")
        if action == "truncate":
            raise AdapterError("truncated", outcome="output_limit", usage=usage)
        if action == "schema":
            return {"unexpected": True}, usage
        if callable(action):
            return action(a, b), usage
        return {"contradictions": [
            {"claim_a": x, "claim_b": y, "confidence": 0.8, "explanation": "opposite outcomes"}
            for x in a for y in b if frozenset((x, y)) in self.contradict]}, usage


def _vault(tmp_path, *, docs=("a.md", "b.md", "c.md")):
    jobs = tmp_path / "db" / "jobs.sqlite"
    fixtures.copy_templates(tmp_path)
    for name in docs:
        con_t._ingest(tmp_path, name, DOCS[name])
    intake.scan_inbox(tmp_path, jobs_db=jobs)
    extract.extract_sources(tmp_path, jobs_db=jobs)
    wiki.generate_wiki(tmp_path, jobs_db=jobs, templates_dir=con_t.TEMPLATES, rebuild_index=False)
    for name, author in (("a.md", "Alice"), ("b.md", "Bob"), ("c.md", "Carol")):
        if name in docs:
            manifests.set_provenance(tmp_path / MD, _sid(tmp_path, name), author=author)
    claims.extract_claims(tmp_path, client=LLMClient({"anthropic": SentenceClaims()}),
                          model_ref=con_t.CLAIM_MODEL, jobs_db=jobs, rebuild_index=False)
    return [_sid(tmp_path, n) for n in docs]


def _sid(tmp_path, name):
    return con_t._sid(tmp_path, name)


def _claim(tmp_path, sentence):
    """The claim id of a sentence's claim."""
    conn = con_t._gconn(tmp_path)
    try:
        for cid in graph.active_node_ids_of_type(conn, "claim"):
            if contradictions._read_claim_text(tmp_path / "wiki" / "Claims" / f"{cid}.md") \
                    == sentence[:-1]:
                return cid
    finally:
        conn.close()
    raise KeyError(sentence)


def _settings(tmp_path, **kw):
    base = dataclasses.replace(
        get_settings(tmp_path), contradiction_model=None, contradiction_window_chars=1,
        contradiction_max_calls=50, contradiction_max_input_tokens=400_000,
        contradiction_max_output_tokens=512, enrich_local_base_url=None)
    return dataclasses.replace(base, **kw)


def _client(tmp_path, hosted=None, local=None):
    return LLMClient({"anthropic": hosted or Judge(), "local": local or Judge(available=False),
                      "openai": Judge(available=False)},
                     cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"),
                     ledger=UsageLedger(tmp_path / "db" / "jobs.sqlite"))


def _estimate(st, sids, client, model=HOSTED):
    return cc.estimate(st, sids, model=model, client=client)


def _run(st, token, client, **kw):
    kw.setdefault("allow_paid", True)
    return cc.run(st, token, client=client, **kw)


def _pending(tmp_path):
    out = []
    for p in sorted((tmp_path / "reviews" / "pending").glob("*.json")):
        item = json.loads(p.read_text())
        if item["type"] == "resolve_contradiction":
            out.append(item)
    return out


# --- the plan: scope, pairs and windows ---------------------------------------------------------


def test_every_window_of_a_is_compared_with_every_window_of_b(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    judge = Judge()
    client = _client(tmp_path, judge)
    est = _estimate(st, [sa, sb], client)
    [pair] = est["plan"]["pairs"]
    assert sorted((pair["windows_a"], pair["windows_b"])) == [2, 3]
    assert est["budget"]["windows"] == est["budget"]["calls"] == 6 and judge.calls == []
    summary = _run(st, est["token"], client)
    seen = {frozenset((x, y)) for a, b, _ in judge.calls for x in a for y in b}
    ca = [_claim(tmp_path, s) for s in SENTENCES[:3]]
    cb = [_claim(tmp_path, s) for s in SENTENCES[3:5]]
    assert seen == {frozenset((x, y)) for x in ca for y in cb}   # the full cross-product
    assert len(judge.calls) == 6 and summary["coverage"]["complete"] is True
    assert summary["coverage"]["remaining"] == [] and summary["status"] == "succeeded"


def test_a_contradiction_in_non_corresponding_windows_is_found(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    def index(wid, side):
        return re.search(side + r"(\d+)/", wid.split("#", 1)[1]).group(1)
    off = next(w for w in est["plan"]["windows"] if index(w["id"], "a") != index(w["id"], "xb"))
    x, y = off["claims_a"][0], off["claims_b"][0]
    judge = Judge(contradict=[(x, y)])
    summary = _run(st, est["token"], _client(tmp_path, judge))
    assert summary["contradictions_proposed"] == 1
    [item] = _pending(tmp_path)
    assert {item["subject"]["claim_a"], item["subject"]["claim_b"]} == {x, y}


def test_claims_are_packed_into_windows_up_to_the_size(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    est = _estimate(_settings(tmp_path, contradiction_window_chars=100_000), [sa, sb],
                    _client(tmp_path))
    assert [(p["windows_a"], p["windows_b"]) for p in est["plan"]["pairs"]] == [(1, 1)]
    [w] = est["plan"]["windows"]
    assert sorted((len(w["claims_a"]), len(w["claims_b"]))) == [2, 3]


def test_skipped_pairs_say_their_actual_reason(tmp_path):
    sa, sb, sc = _vault(tmp_path)
    manifests.set_provenance(tmp_path / MD, sc, author="Alice")          # shares a's author
    st = _settings(tmp_path)
    reasons = {(p["a"], p["b"]): p["reason"]
               for p in _estimate(st, [sa, sb, sc], _client(tmp_path))["plan"]["pairs"]}
    assert reasons[tuple(sorted((sa, sc)))] == "shared author Alice"
    assert reasons[tuple(sorted((sa, sb)))] is None
    manifests.set_provenance(tmp_path / MD, sc, author="Carol", publisher="Acme")
    manifests.set_provenance(tmp_path / MD, sb, author="Bob", publisher="Acme")
    reasons = {(p["a"], p["b"]): p["reason"]
               for p in _estimate(st, [sb, sc], _client(tmp_path))["plan"]["pairs"]}
    assert list(reasons.values()) == ["same publisher Acme"]


def test_unconfirmed_details_skip_the_pair_as_provenance_unknown(tmp_path):
    sa, sb, sc = _vault(tmp_path)
    manifests.clear_details(tmp_path / MD, sc)
    est = _estimate(_settings(tmp_path), [sa, sc], _client(tmp_path))
    assert [p["reason"] for p in est["plan"]["pairs"]] == [
        "provenance unknown (details not confirmed)"]
    assert est["token"] is None and est["refused"] == "nothing_to_check"


@pytest.mark.parametrize("sids, code", [([], "too_few_documents"),
                                        (["SAME", "SAME"], "too_few_documents"),
                                        (["SA", "src_" + "0" * 16], "unknown_document")])
def test_the_selection_needs_two_valid_distinct_documents(tmp_path, sids, code):
    sa, _sb, _sc = _vault(tmp_path)
    sids = [sa if s in ("SAME", "SA") else s for s in sids]
    with pytest.raises(cc.Refused) as exc:
        _estimate(_settings(tmp_path), sids, _client(tmp_path))
    assert exc.value.code == code


def test_the_estimate_makes_no_model_call_and_writes_only_its_saved_estimate(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    judge = Judge()
    client = _client(tmp_path, judge)
    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    est = _estimate(_settings(tmp_path), [sa, sb], client)
    after = {p for p in tmp_path.rglob("*") if p.is_file()}
    assert judge.calls == [] and est["token"]
    assert {p.relative_to(tmp_path).as_posix() for p in after - before
            if not p.name.endswith(".lock")} == {"db/contradiction_estimates.json"}


# --- the judge model ---------------------------------------------------------------------------


def test_a_missing_model_or_a_chain_is_refused_with_zero_calls(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    judge = Judge()
    for model, code in ((None, "no_model"), (f"{HOSTED},{LOCAL}", "chain"),
                        ("nope:x", "bad_model"), ("anthropic", "bad_model")):
        with pytest.raises(cc.Refused) as exc:
            _estimate(_settings(tmp_path), [sa, sb], _client(tmp_path, judge), model=model)
        assert exc.value.code == code
    assert judge.calls == []
    st = _settings(tmp_path, contradiction_model=HOSTED)                 # the setting is used
    assert cc.estimate(st, [sa, sb], client=_client(tmp_path, judge))["plan"]["model_ref"] == HOSTED


def test_a_paid_model_needs_allow_paid_and_the_refusal_keeps_the_token(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    judge = Judge()
    est = _estimate(st, [sa, sb], _client(tmp_path, judge))
    assert est["plan"]["paid"] is True
    with pytest.raises(cc.Refused) as exc:
        cc.run(st, est["token"], client=_client(tmp_path, judge))
    assert exc.value.code == "paid_authorization_required" and judge.calls == []
    assert _run(st, est["token"], _client(tmp_path, judge))["coverage"]["complete"]


def test_a_local_model_needs_no_paid_authorization(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path, enrich_local_base_url="http://127.0.0.1:9")
    hosted, local = Judge(), Judge()
    est = _estimate(st, [sa, sb], _client(tmp_path, hosted, local), model=LOCAL)
    assert est["plan"]["paid"] is False and est["plan"]["endpoint"] == "http://127.0.0.1:9"
    summary = cc.run(st, est["token"], client=_client(tmp_path, hosted, local))
    assert summary["coverage"]["complete"] and len(local.calls) == 6 and hosted.calls == []


def test_a_local_connection_failure_stops_with_its_error_and_zero_hosted_calls(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path, enrich_local_base_url="http://127.0.0.1:9")
    hosted, local = Judge(), Judge(script=["down"] * 10)
    est = _estimate(st, [sa, sb], _client(tmp_path, hosted, local), model=LOCAL)
    summary = cc.run(st, est["token"], client=_client(tmp_path, hosted, local))
    assert hosted.calls == [] and len(local.calls) == 3              # one window's attempts
    assert summary["status"] == "partial" and not summary["coverage"]["complete"]
    assert "connection refused" in next(iter(summary["coverage"]["failed"].values()))
    assert len(summary["coverage"]["remaining"]) == 5
    assert "no other model is substituted" in summary["stopped"]


def test_an_unconfigured_model_is_refused_and_the_token_kept(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    hosted = Judge()
    est = _estimate(st, [sa, sb], _client(tmp_path, hosted), model=LOCAL)
    assert est["available"] is False and est["token"]
    with pytest.raises(cc.Refused) as exc:
        cc.run(st, est["token"], client=_client(tmp_path, hosted))
    assert exc.value.code == "model_unavailable" and hosted.calls == []
    assert cc.find_estimate(tmp_path, est["token"]) is not None


# --- the token -----------------------------------------------------------------------------------


def test_the_token_is_consumed_when_the_run_is_accepted(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    _run(st, est["token"], _client(tmp_path))
    with pytest.raises(cc.Refused) as exc:
        _run(st, est["token"], _client(tmp_path))
    assert exc.value.code == "unknown_token"


def test_the_token_is_consumed_before_the_first_call(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    seen = []
    judge = Judge(on_call=lambda i: seen.append(cc.find_estimate(tmp_path, est["token"])))
    _run(st, est["token"], _client(tmp_path, judge))
    assert seen and all(r is None for r in seen)


def _re_extract(tmp_path, sid, longer):
    claims.extract_claims(tmp_path, client=LLMClient({"anthropic": SentenceClaims(longer)}),
                          model_ref=con_t.CLAIM_MODEL, source_ids=[sid], force=True,
                          jobs_db=tmp_path / "db" / "jobs.sqlite", rebuild_index=False)


@pytest.mark.parametrize("change", ["evidence", "details", "window", "output_limit"])
def test_a_relevant_change_refuses_the_token(tmp_path, change):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    judge = Judge()
    est = _estimate(st, [sa, sb], _client(tmp_path, judge))
    if change == "evidence":                      # same wording (same claim id), new quote
        _re_extract(tmp_path, sa, {SENTENCES[0]: SENTENCES[0] + " " + SENTENCES[1]})
    elif change == "details":
        manifests.set_provenance(tmp_path / MD, sb, author="Bob", publisher="Other Press")
    elif change == "window":
        st = dataclasses.replace(st, contradiction_window_chars=100_000)
    else:
        st = dataclasses.replace(st, contradiction_max_output_tokens=1024)
    with pytest.raises(cc.Refused) as exc:
        _run(st, est["token"], _client(tmp_path, judge))
    assert exc.value.code == "estimate_stale" and judge.calls == []
    assert cc.find_estimate(tmp_path, est["token"]) is not None      # still there (refused)


def test_an_unrelated_change_does_not_refuse_the_token(tmp_path):
    sa, sb, sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    manifests.set_provenance(tmp_path / MD, sc, author="Dora")          # not selected
    assert _run(st, est["token"], _client(tmp_path))["coverage"]["complete"]


# --- limits and reservations ---------------------------------------------------------------------


def test_an_oversized_plan_gets_no_token(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    est = _estimate(_settings(tmp_path, contradiction_max_calls=5), [sa, sb], _client(tmp_path))
    assert est["refused"] == "over_budget" and est["token"] is None
    assert est["budget"]["over"] == ["calls"]
    assert not cc.store_path(tmp_path).exists()


def test_a_run_may_lower_the_limits_but_not_raise_them(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    judge = Judge()
    est = _estimate(st, [sa, sb], _client(tmp_path, judge))
    with pytest.raises(cc.Refused) as exc:
        _run(st, est["token"], _client(tmp_path, judge), max_calls=51)
    assert exc.value.code == "limit_raised"
    with pytest.raises(cc.Refused) as exc:                             # lowered below the plan
        _run(st, est["token"], _client(tmp_path, judge), max_calls=5)
    assert exc.value.code == "over_budget"
    with pytest.raises(cc.Refused) as exc:
        _run(st, est["token"], _client(tmp_path, judge), max_input_tokens=10)
    assert exc.value.code == "over_budget" and judge.calls == []
    summary = _run(st, est["token"], _client(tmp_path, judge), max_calls=6)
    assert summary["coverage"]["complete"] and summary["budget"]["limits"]["max_calls"] == 6


def test_a_tighter_configured_cap_applies_to_an_existing_token(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    judge = Judge()
    est = _estimate(_settings(tmp_path), [sa, sb], _client(tmp_path, judge))
    tight = _settings(tmp_path, contradiction_max_calls=5)          # lowered after the estimate
    with pytest.raises(cc.Refused) as exc:
        _run(tight, est["token"], _client(tmp_path, judge))
    assert exc.value.code == "over_budget" and judge.calls == []
    with pytest.raises(cc.Refused) as exc:                           # nor raised back by the run
        _run(_settings(tmp_path, contradiction_max_calls=6), est["token"],
             _client(tmp_path, judge), max_calls=7)
    assert exc.value.code == "limit_raised"
    summary = _run(_settings(tmp_path, contradiction_max_calls=6), est["token"],
                   _client(tmp_path, judge))
    assert summary["limits"]["max_calls"] == 6 and summary["coverage"]["complete"]
    assert len(judge.calls) == 6


def test_every_attempt_is_reserved_and_retries_count_against_the_cap(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    judge = Judge(script=["schema"])                       # the first window needs a retry
    summary = _run(st, est["token"], _client(tmp_path, judge), max_calls=6)
    assert len(judge.calls) == 6                           # never more than the cap
    assert summary["budget"]["calls"] == 6
    cov = summary["coverage"]
    assert len(cov["completed"]) == 5 and len(cov["remaining"]) == 1 and not cov["complete"]
    assert summary["stopped"].startswith("stopped by the budget")
    usage = summarize(tmp_path / "db" / "jobs.sqlite", summary["job_id"])[HOSTED]
    assert usage["attempts"] == 6                          # one ledger row per attempt


def test_a_retry_the_budget_cannot_cover_fails_its_window(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    judge = Judge(script=["ok"] * 5 + ["schema"])          # the last window's retry
    summary = _run(st, est["token"], _client(tmp_path, judge), max_calls=6)
    cov = summary["coverage"]
    assert len(judge.calls) == 6 and len(cov["completed"]) == 5 and len(cov["failed"]) == 1
    assert "stopped by the budget after 1 attempt(s)" in next(iter(cov["failed"].values()))


def test_a_truncated_response_is_a_failed_window_and_is_not_retried(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    first = est["plan"]["windows"][0]
    x, y = first["claims_a"][0], first["claims_b"][0]
    judge = Judge(script=["truncate"], contradict=[(x, y)])
    summary = _run(st, est["token"], _client(tmp_path, judge))
    assert len(judge.calls) == 6                           # no retry of the truncated window
    assert summary["coverage"]["failed"] == {first["id"]: (
        "the response stopped at the output limit (truncated)")}
    assert not summary["coverage"]["complete"] and summary["status"] == "partial"
    assert summary["contradictions_proposed"] == 0         # never read as "no contradictions"


def _violating(on_call):
    return lambda i: {"family": "anthropic", "input_tokens": 100,
                      "output_tokens": 9999 if i == on_call else 10}


def test_an_output_limit_violation_fails_its_window_and_stops_further_calls(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    judge = Judge(usage=_violating(1))
    summary = _run(st, est["token"], _client(tmp_path, judge))
    assert len(judge.calls) == 2
    cov = summary["coverage"]
    second = est["plan"]["windows"][1]["id"]
    assert len(cov["completed"]) == 1 and list(cov["failed"]) == [second]
    assert len(cov["remaining"]) == 4 and not cov["complete"] and summary["status"] == "partial"
    assert "above the 512-token limit" in summary["stopped"]
    assert summary["budget"]["output_limit_violation"]


def test_a_violation_in_the_final_window_is_not_a_success_and_is_not_cached(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    last = est["plan"]["windows"][-1]
    x, y = last["claims_a"][0], last["claims_b"][0]
    judge = Judge(usage=_violating(5), contradict=[(x, y)])
    summary = _run(st, est["token"], _client(tmp_path, judge))
    assert summary["status"] == "partial" and summary["coverage"]["complete"] is False
    assert list(summary["coverage"]["failed"]) == [last["id"]]
    assert summary["contradictions_proposed"] == 0                 # the violating verdict unfiled
    again = _estimate(st, [sa, sb], _client(tmp_path))
    assert again["budget"]["cache_hits"] == 5                      # its response wasn't cached


def test_a_failed_maintenance_keeps_the_run_from_a_clean_success(tmp_path, monkeypatch):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    monkeypatch.setattr(contradictions, "targeted_maintenance", lambda *a, **k: {
        "checked": 0, "retired": [], "unverifiable": [], "awaiting_apply": 0,
        "error": "OSError: disk full"})
    summary = _run(st, est["token"], _client(tmp_path))
    assert summary["coverage"]["complete"] and summary["status"] == "partial"
    assert summary["maintenance"] == {"retired": 0, "error": "OSError: disk full"}


def test_the_budget_charges_reported_usage_and_keeps_estimates_apart():
    budget = cc.Budget({"max_calls": 3, "max_input_tokens": 100}, 10)
    budget.reserve(retry=0, input_chars=40, max_tokens=10)             # est. 20 tokens
    budget.settle(outcome="ok", usage={"family": "anthropic", "input_tokens": 5,
                                        "cache_read_tokens": 7, "output_tokens": 3})
    budget.reserve(retry=0, input_chars=40, max_tokens=10)
    budget.settle(outcome="schema_error", usage=None)                 # nothing reported
    r = budget.report()
    assert (r["reported_input_tokens"], r["estimated_input_tokens_not_reported"]) == (12, 20)
    assert (r["reported_output_tokens"], r["reserved_output_tokens_not_reported"]) == (3, 10)
    assert budget.input_tokens == 32 and budget.output_tokens == 13 and budget.calls == 2
    with pytest.raises(cc.BudgetExhausted):                            # 81 chars > 200? no: tokens
        budget.reserve(retry=1, input_chars=150, max_tokens=10)        # est. 75 + 32 > 100
    assert budget.calls == 2 and budget.input_chars == 80              # nothing reserved
    with pytest.raises(cc.WindowTruncated):
        budget.reserve(retry=0, input_chars=2, max_tokens=10)
        budget.settle(outcome="output_limit", usage=None)
    with pytest.raises(cc.BudgetExhausted):                            # the call cap
        budget.reserve(retry=0, input_chars=2, max_tokens=10)


# --- results ---------------------------------------------------------------------------------------


def test_proposals_record_their_judged_evidence_and_maintenance_keeps_them(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    x, y = _claim(tmp_path, SENTENCES[0]), _claim(tmp_path, SENTENCES[3])
    est = _estimate(st, [sa, sb], _client(tmp_path))
    summary = _run(st, est["token"], _client(tmp_path, Judge(contradict=[(x, y)])))
    assert summary["contradictions_proposed"] == 1
    [item] = _pending(tmp_path)
    lo, hi = sorted((x, y))
    assert item["subject"] == {"claim_a": lo, "claim_b": hi}
    assert set(item["context"]["judged_evidence"]) == {lo, hi}
    assert item["context"]["judged_by"]["job_id"] == summary["job_id"]
    assert [s["claim_id"] for s in item["proposal"]["sides"]] == [lo, hi]
    from app.backend import review_read
    conn = con_t._gconn(tmp_path)
    try:
        preview = review_read.preview_resolve_contradiction(item, gconn=conn,
                                                            wiki_dir=tmp_path / "wiki")
    finally:
        conn.close()
    assert preview["details"]["judged_by"]["window"] == item["context"]["judged_by"]["window"]
    conn = con_t._gconn(tmp_path)
    try:
        assert [r["status"] for r in graph.contradiction_between(conn, lo, hi)] == ["proposed"]
        found = contradictions.check_assertions(
            conn, reviews_dir=tmp_path / "reviews", claims_dir=tmp_path / "wiki" / "Claims",
            markdown_dir=tmp_path / "normalized" / "markdown", manifests_dir=tmp_path / MD)
    finally:
        conn.close()
    assert found["stale"] == [] and found["unverifiable"] == [] and found["checked"] == 1
    # Changed evidence later retires it (maintenance reads the recorded fingerprints).
    _re_extract(tmp_path, sa, {SENTENCES[0]: SENTENCES[0] + " " + SENTENCES[1]})
    assert _pending(tmp_path) == []


def test_out_of_scope_pairs_are_dropped_and_counted(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path, contradiction_window_chars=100_000)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    [w] = est["plan"]["windows"]
    a1, a2 = w["claims_a"][:2]
    stray = "clm_" + "f" * 16

    def answer(ids_a, ids_b):
        return {"contradictions": [
            {"claim_a": a1, "claim_b": a2, "confidence": 1, "explanation": "same list"},
            {"claim_a": a1, "claim_b": stray, "confidence": 1, "explanation": "not supplied"},
            {"claim_a": ids_b[0], "claim_b": a1, "confidence": 2, "explanation": "swapped"}]}
    summary = _run(st, est["token"], _client(tmp_path, Judge(script=[answer])))
    assert summary["out_of_scope_pairs_dropped"] == 2 and summary["contradictions_proposed"] == 1
    [item] = _pending(tmp_path)
    assert item["proposal"]["confidence"] == 1.0                        # clamped


def test_a_pair_a_human_decided_is_not_re_proposed(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    x, y = _claim(tmp_path, SENTENCES[0]), _claim(tmp_path, SENTENCES[3])
    est = _estimate(st, [sa, sb], _client(tmp_path))
    _run(st, est["token"], _client(tmp_path, Judge(contradict=[(x, y)])))
    [item] = _pending(tmp_path)
    reviews.resolve_review_item(tmp_path / "reviews", item["review_id"], decision="rejected",
                                decided_by="tester")
    est = _estimate(st, [sa, sb], _client(tmp_path))
    assert est["budget"]["cache_hits"] == 6
    summary = _run(st, est["token"], _client(tmp_path, Judge(contradict=[(x, y)])))
    assert summary["skipped_human_decided"] == 1 and summary["contradictions_proposed"] == 0


def _window_with(est, cid):
    return next(i for i, w in enumerate(est["plan"]["windows"])
                if cid in w["claims_a"] + w["claims_b"])


def test_a_claim_edited_during_a_call_is_not_filed_and_stops_the_run(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    w0 = est["plan"]["windows"][0]
    x, y = w0["claims_a"][0], w0["claims_b"][0]
    page = tmp_path / "wiki" / "Claims" / f"{x}.md"
    text = contradictions._read_claim_text(page)

    def edit(i):
        if i == 0:
            page.write_text(page.read_text().replace(text, "Edited meanwhile"))
    judge = Judge(contradict=[(x, y)], on_call=edit)
    summary = _run(st, est["token"], _client(tmp_path, judge))
    assert summary["contradictions_proposed"] == 0 and len(judge.calls) == 1
    assert summary["coverage"]["failed"] == {w0["id"]: (
        f"not filed: claim {x}'s wording or evidence changed during the call")}
    assert "estimate again" in summary["stopped"] and summary["status"] == "partial"


def test_details_changed_during_a_call_leave_the_verdict_unfiled(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    w0 = est["plan"]["windows"][0]
    x, y = w0["claims_a"][0], w0["claims_b"][0]

    def same_author(i):                               # the two documents stop being independent
        if i == 0:
            manifests.set_provenance(tmp_path / MD, sb, author="Alice")
    judge = Judge(contradict=[(x, y)], on_call=same_author)
    summary = _run(st, est["token"], _client(tmp_path, judge))
    assert summary["contradictions_proposed"] == 0 and _pending(tmp_path) == []
    assert len(judge.calls) == 1 and not summary["coverage"]["complete"]
    assert "Details of" in summary["coverage"]["failed"][w0["id"]]


def test_a_claim_hidden_during_its_own_call_is_not_filed_and_stops_the_run(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    w0 = est["plan"]["windows"][0]
    x, y = w0["claims_a"][0], w0["claims_b"][0]

    def hide(i):                                      # hidden while its window is being judged
        if i == 0:
            conn = con_t._gconn(tmp_path)
            conn.execute("UPDATE nodes SET status = 'hidden' WHERE node_id = ?", (x,))
            conn.commit()
            conn.close()
    judge = Judge(contradict=[(x, y)], on_call=hide)
    summary = _run(st, est["token"], _client(tmp_path, judge))
    assert summary["contradictions_proposed"] == 0 and _pending(tmp_path) == []
    assert len(judge.calls) == 1 and not summary["coverage"]["complete"]
    assert summary["coverage"]["failed"] == {w0["id"]: (
        f"not filed: claim {x} is no longer active (hidden or retracted) during the call")}
    assert "estimate again" in summary["stopped"] and summary["status"] == "partial"


def test_a_claim_hidden_during_the_run_stops_it_before_its_window_is_sent(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    windows = est["plan"]["windows"]
    later = next(c for c in windows[-1]["claims_a"] if c not in windows[0]["claims_a"])
    first_with = _window_with(est, later)

    def hide(i):
        if i == 0:
            conn = con_t._gconn(tmp_path)
            conn.execute("UPDATE nodes SET status = 'hidden' WHERE node_id = ?", (later,))
            conn.commit()
            conn.close()
    judge = Judge(on_call=hide)
    summary = _run(st, est["token"], _client(tmp_path, judge))
    assert len(judge.calls) == first_with                 # nothing sent from its window on
    assert f"claim {later} is no longer active" in summary["stopped"]
    assert len(summary["coverage"]["remaining"]) == len(windows) - first_with


def test_an_interrupted_run_resumes_completed_windows_from_the_cache(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    first = _run(st, est["token"], _client(tmp_path), max_calls=6)
    assert first["coverage"]["complete"]
    est = _estimate(st, [sa, sb], _client(tmp_path))
    assert est["budget"]["cache_hits"] == 6 and est["budget"]["calls"] == 0
    judge = Judge()
    again = _run(st, est["token"], _client(tmp_path, judge))
    assert judge.calls == [] and again["coverage"]["from_cache"] == 6
    usage = summarize(tmp_path / "db" / "jobs.sqlite", again["job_id"])[HOSTED]
    assert usage["cache_hits"] == 6 and usage["attempts"] == 0


def test_a_stopped_run_resumes_only_the_remaining_windows(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    judge = Judge(script=["ok", "ok", "down", "down", "down"])
    first = _run(st, est["token"], _client(tmp_path, judge))
    assert len(first["coverage"]["completed"]) == 2 and not first["coverage"]["complete"]
    est = _estimate(st, [sa, sb], _client(tmp_path))
    assert est["budget"]["cache_hits"] == 2 and est["budget"]["calls"] == 4
    judge = Judge()
    again = _run(st, est["token"], _client(tmp_path, judge))
    assert len(judge.calls) == 4 and again["coverage"]["complete"]


def test_the_coverage_record_is_kept_on_the_job(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    summary = _run(st, est["token"], _client(tmp_path, Judge(script=["truncate"])))
    import sqlite3
    conn = sqlite3.connect(tmp_path / "db" / "jobs.sqlite")
    try:
        status, meta = conn.execute("SELECT status, metadata_json FROM jobs WHERE job_id = ?",
                                    (summary["job_id"],)).fetchone()
    finally:
        conn.close()
    meta = json.loads(meta)
    assert status == "partial" and meta["mode"] == "run"
    assert meta["documents"] == sorted([sa, sb]) and meta["model_ref"] == HOSTED
    assert len(meta["coverage"]["completed"]) == 5 and len(meta["coverage"]["failed"]) == 1
    assert meta["prompt_version"] == "contradiction-batch-prompt-v1" and meta["checked_at"]


# --- CLI -----------------------------------------------------------------------------------------


def _cli(tmp_path, monkeypatch, judge):
    import detect_contradictions as cli
    monkeypatch.setenv("KNOWLEDGE_SYSTEM_HOME", str(tmp_path))
    monkeypatch.setenv("CONTRADICTION_WINDOW_CHARS", "1")
    monkeypatch.setenv("CONTRADICTION_MAX_OUTPUT_TOKENS", "512")
    monkeypatch.delenv("CONTRADICTION_MODEL", raising=False)
    monkeypatch.setattr(cc, "build_client", lambda *a, **k: _client(tmp_path, judge))
    return cli


def test_the_cli_estimates_then_runs_the_token(tmp_path, monkeypatch, capsys):
    sa, sb, sc = _vault(tmp_path)
    manifests.set_provenance(tmp_path / MD, sc, author="Alice")
    judge = Judge()
    cli = _cli(tmp_path, monkeypatch, judge)
    assert cli.main(["estimate", "--model", HOSTED, sa, sb, sc]) == 0
    out = capsys.readouterr().out
    assert "no model call was made" in out and "skipped — shared author Alice" in out
    assert re.search(r"(3 x 2|2 x 3) claims -> \1 windows = 6 judge call\(s\)", out)
    assert "requires paid authorization" in out
    assert "expected cache hits: 0; calls to send: " in out and judge.calls == []
    token = re.search(r"Token: (cce_[0-9a-f]{32})", out).group(1)
    assert cli.main(["run", "--token", token]) == 1                     # no --allow-paid
    assert "requires paid authorization" in capsys.readouterr().err
    assert cli.main(["run", "--token", token, "--allow-paid"]) == 0
    out = capsys.readouterr().out
    assert "Coverage: complete" in out and "Calls sent: " in out and "reported" in out


def test_the_cli_reports_incomplete_coverage_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    sa, sb, _sc = _vault(tmp_path)
    cli = _cli(tmp_path, monkeypatch, Judge(script=["truncate"]))
    assert cli.main(["estimate", "--model", HOSTED, sa, sb]) == 0
    token = re.search(r"Token: (cce_\w+)", capsys.readouterr().out).group(1)
    assert cli.main(["run", "--token", token, "--allow-paid"]) == 1
    out = capsys.readouterr().out
    assert "Coverage: INCOMPLETE" in out and "failed: " in out


def test_the_cli_exits_nonzero_when_maintenance_failed(tmp_path, monkeypatch, capsys):
    sa, sb, _sc = _vault(tmp_path)
    cli = _cli(tmp_path, monkeypatch, Judge())
    monkeypatch.setattr(contradictions, "targeted_maintenance", lambda *a, **k: {
        "checked": 0, "retired": [], "unverifiable": [], "awaiting_apply": 0, "error": "boom"})
    assert cli.main(["estimate", "--model", HOSTED, sa, sb]) == 0
    token = re.search(r"Token: (cce_\w+)", capsys.readouterr().out).group(1)
    assert cli.main(["run", "--token", token, "--allow-paid"]) == 1
    out = capsys.readouterr().out
    assert "Coverage: complete" in out and "Not a clean run" in out and "failed: boom" in out


def test_the_cli_refuses_an_over_budget_plan(tmp_path, monkeypatch, capsys):
    sa, sb, _sc = _vault(tmp_path)
    cli = _cli(tmp_path, monkeypatch, Judge())
    monkeypatch.setenv("CONTRADICTION_MAX_CALLS", "2")
    assert cli.main(["estimate", "--model", HOSTED, sa, sb]) == 1
    out = capsys.readouterr().out
    assert "OVER: calls" in out and "no token was issued" in out


def test_saved_estimates_are_never_backed_up_or_restored():
    import backup
    assert backup._ESTIMATES_FILENAME == cc.STORE_NAME
    assert f"db/{cc.STORE_NAME}" in backup._NEVER_RESTORED


# --- interruption, endpoint identity, scoped evidence ---------------------------------------------


class Killed(BaseException):
    """Stands in for the process being interrupted mid-call."""


def _job(tmp_path, job_id=None):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "db" / "jobs.sqlite")
    try:
        if job_id is None:
            row = conn.execute("SELECT job_id, status, metadata_json, error_message FROM jobs "
                               "WHERE job_type = 'detect_contradictions' ORDER BY rowid DESC "
                               "LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT job_id, status, metadata_json, error_message FROM jobs "
                               "WHERE job_id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    return {"job_id": row[0], "status": row[1], "metadata": json.loads(row[2] or "{}"),
            "error": row[3]}


def test_progress_is_checkpointed_on_the_job_while_it_runs(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    seen = []
    judge = Judge(on_call=lambda i: seen.append(_job(tmp_path)))
    _run(st, est["token"], _client(tmp_path, judge))
    first, third = seen[0], seen[2]
    assert first["status"] == "running" and first["metadata"]["phase"] == "judging"
    meta = third["metadata"]
    windows = [w["id"] for w in est["plan"]["windows"]]
    assert meta["documents"] == sorted([sa, sb]) and meta["coverage"]["windows"] == 6
    assert meta["coverage"]["completed"] == windows[:2]
    assert meta["coverage"]["in_progress"] == windows[2]
    assert meta["coverage"]["remaining"] == windows[2:]


def test_the_accepted_scope_is_recorded_before_the_token_is_spent(tmp_path, monkeypatch):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    seen = []
    real = cc._consume
    monkeypatch.setattr(cc, "_consume", lambda root, token: (seen.append(_job(tmp_path)),
                                                              real(root, token)))
    _run(st, est["token"], _client(tmp_path))
    [job] = seen
    assert job["metadata"]["phase"] == "accepted" and job["metadata"]["coverage"]["windows"] == 6
    assert job["metadata"]["estimate_token"] == est["token"]


def test_an_interrupted_run_leaves_its_coverage_on_the_job(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))

    def kill(i):
        if i == 2:
            raise Killed()
    with pytest.raises(Killed):
        _run(st, est["token"], _client(tmp_path, Judge(on_call=kill)))
    job = _job(tmp_path)
    windows = [w["id"] for w in est["plan"]["windows"]]
    assert job["status"] == "failed" and job["error"].startswith("interrupted: Killed")
    assert job["metadata"]["coverage"]["completed"] == windows[:2]
    assert job["metadata"]["coverage"]["remaining"] == windows[2:]
    assert cc.find_estimate(tmp_path, est["token"]) is None          # spent: estimate again
    again = _estimate(st, [sa, sb], _client(tmp_path))
    assert again["budget"]["cache_hits"] == 2


def test_a_refused_run_is_recorded_as_skipped(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    with pytest.raises(cc.Refused):
        cc.run(st, est["token"], client=_client(tmp_path))         # no --allow-paid
    job = _job(tmp_path)
    assert job["status"] == "skipped" and job["metadata"]["refused"] == "paid_authorization_required"


def test_another_endpoint_under_the_same_model_ref_replays_nothing(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    one = _settings(tmp_path, enrich_local_base_url="http://127.0.0.1:9001")
    two = _settings(tmp_path, enrich_local_base_url="http://127.0.0.1:9002")
    est = _estimate(one, [sa, sb], _client(tmp_path, local=Judge()), model=LOCAL)
    cc.run(one, est["token"], client=_client(tmp_path, local=Judge()))
    old = _estimate(one, [sa, sb], _client(tmp_path, local=Judge()), model=LOCAL)
    assert old["budget"]["cache_hits"] == 6
    with pytest.raises(cc.Refused) as exc:                           # bound to the first server
        cc.run(two, old["token"], client=_client(tmp_path, local=Judge()))
    assert exc.value.code == "estimate_stale"
    new = _estimate(two, [sa, sb], _client(tmp_path, local=Judge()), model=LOCAL)
    assert new["budget"]["cache_hits"] == 0 and new["budget"]["calls"] == 6
    judge = Judge()
    cc.run(two, new["token"], client=_client(tmp_path, local=judge))
    assert len(judge.calls) == 6


SHARED = "The beta count fell in June."


def test_a_claim_shared_with_an_unselected_document_carries_only_the_selected_evidence(tmp_path):
    DOCS["d.md"] = f"# D\n\n{SHARED} The omega mark rose in April.\n"
    SENTENCES.append("The omega mark rose in April.")
    try:
        sa, sb, _sc, sd = _vault(tmp_path, docs=("a.md", "b.md", "c.md", "d.md"))
        manifests.set_provenance(tmp_path / MD, sd, author="Dora")
        shared = _claim(tmp_path, SHARED)
        conn = con_t._gconn(tmp_path)
        try:
            assert set(graph.sources_for_claim(conn, shared)) == {sa, sd}
        finally:
            conn.close()
        st = _settings(tmp_path)
        y = _claim(tmp_path, SENTENCES[3])
        est = _estimate(st, [sa, sb], _client(tmp_path))
        judge = Judge(contradict=[(shared, y)])
        _run(st, est["token"], _client(tmp_path, judge))
        conn = con_t._gconn(tmp_path)
        try:
            work = cc.build_plan(st, conn, [sa, sb], HOSTED)["work"]
        finally:
            conn.close()
        assert work and judge.calls
        for entry in work.values():
            assert sd not in entry["messages"][-1]["content"]      # D's evidence never sent
        [item] = _pending(tmp_path)
        side = next(x for x in item["proposal"]["sides"] if x["claim_id"] == shared)
        assert {c["source_id"] for c in side["citations"]} == {sa}
        assert item["context"]["judged_scope"][shared] == sa
        found = contradictions.check_assertions(
            con_t._gconn(tmp_path), reviews_dir=tmp_path / "reviews",
            claims_dir=tmp_path / "wiki" / "Claims",
            markdown_dir=tmp_path / "normalized" / "markdown", manifests_dir=tmp_path / MD)
        assert found["stale"] == [] and found["unverifiable"] == []   # D's evidence is ignored
        # Maintenance checks independence on the judged documents: D sharing B's author is
        # irrelevant, A sharing it retires the proposal.
        manifests.set_provenance(tmp_path / MD, sd, author="Bob")
        assert contradictions.maintain(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")[
            "retired"] == []
        manifests.set_provenance(tmp_path / MD, sa, author="Bob")
        assert [e["reason"] for e in contradictions.maintain(
            tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")["retired"]] == ["not_independent"]
    finally:
        DOCS.pop("d.md")
        SENTENCES.pop()


# --- slice 3 hooks: the evaluator's claim-order seed and the reported model -----------------------


def test_an_order_seed_reorders_the_claims_and_its_token_binds_it(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    plain = _estimate(st, [sa, sb], _client(tmp_path))
    assert "order_seed" not in plain["plan"]            # production digests are unchanged
    seeded = cc.estimate(st, [sa, sb], model=HOSTED, client=_client(tmp_path), order_seed=1)
    assert seeded["plan"]["order_seed"] == 1
    order = [w["claims_a"] + w["claims_b"] for w in seeded["plan"]["windows"]]
    assert order != [w["claims_a"] + w["claims_b"] for w in plain["plan"]["windows"]]
    assert sorted(map(sorted, order)) == sorted(
        sorted(w["claims_a"] + w["claims_b"]) for w in plain["plan"]["windows"])
    judge = Judge()
    summary = _run(st, seeded["token"], _client(tmp_path, judge))
    assert summary["status"] == "succeeded"
    assert [a + b for a, b, _ in judge.calls] == order  # the run judged the seeded plan


def test_the_budget_reports_the_models_the_provider_names(tmp_path):
    sa, sb, _sc = _vault(tmp_path)
    st = _settings(tmp_path)
    est = _estimate(st, [sa, sb], _client(tmp_path))
    judge = Judge(usage=lambda i: {"family": "anthropic", "model": "judge-model-20260101",
                                   "input_tokens": 10, "output_tokens": 2})
    summary = _run(st, est["token"], _client(tmp_path, judge))
    assert summary["budget"]["reported_models"] == ["judge-model-20260101"]
