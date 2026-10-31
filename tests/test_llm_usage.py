"""ADR-0069 slice 1: the LLM usage ledger.

One row per provider attempt, recorded `started` before sending and finalized after; usage kept for
failed attempts; independently nullable token fields in the provider's own categories; cache hits
counted apart; attribution to the running job or a bound invocation; metadata only.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import db  # noqa: E402
from app.llm import adapters, usage  # noqa: E402
from app.llm.adapters import AdapterError  # noqa: E402
from app.llm.cache import ResponseCache  # noqa: E402
from app.llm.client import LLMClient, ParseError, build_client  # noqa: E402

MODEL = "anthropic:claude-test"
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}},
          "required": ["summary"], "additionalProperties": False}
SECRET_PROMPT = "PROMPT-TEXT-THAT-MUST-NOT-BE-STORED"
MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": SECRET_PROMPT}]
FULL = {"family": "anthropic", "input_tokens": 120, "output_tokens": 30,
        "cache_read_tokens": 0, "cache_write_tokens": None}


class UsageAdapter:
    """Fake adapter that reports usage; `script` is a list of per-attempt outcomes."""
    name = "anthropic"
    supports_batch = False

    def __init__(self, script, *, on_call=None):
        self.script = list(script)
        self.calls = 0
        self.on_call = on_call

    def available(self):
        return True

    def parse_with_usage(self, messages, schema, model_id, *, max_tokens):
        self.calls += 1
        if self.on_call:
            self.on_call()
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class PlainAdapter:
    """A test double without `parse_with_usage`: it reports no usage at all."""
    name = "anthropic"
    supports_batch = False

    def available(self):
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        return {"summary": "s"}


def _jobs(tmp_path):
    return tmp_path / "db" / "jobs.sqlite"


def _client(tmp_path, adapter, *, cache=False, retries=2):
    return LLMClient({"anthropic": adapter}, max_tokens=256, max_retries=retries,
                     cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite") if cache else None,
                     ledger=usage.UsageLedger(_jobs(tmp_path)))


def _rows(tmp_path):
    conn = sqlite3.connect(_jobs(tmp_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM llm_usage ORDER BY retry")]
    finally:
        conn.close()


# --- attempt lifecycle --------------------------------------------------------------------------


def test_an_attempt_is_recorded_started_before_it_is_sent(tmp_path):
    seen = []
    adapter = UsageAdapter([({"summary": "ok"}, FULL)],
                           on_call=lambda: seen.append([r["status"] for r in _rows(tmp_path)]))
    assert _client(tmp_path, adapter).parse(MESSAGES, SCHEMA, MODEL, operation="src_a") == {"summary": "ok"}
    assert seen == [["started"]]                       # the row existed while the call was in flight
    [row] = _rows(tmp_path)
    assert row["status"] == "ok" and row["finished_at"] and row["operation"] == "src_a"
    assert row["provider"] == "anthropic" and row["model_ref"] == MODEL and row["retry"] == 0
    assert row["usage_family"] == "anthropic"
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (120, 30, 0)
    assert row["cache_write_tokens"] is None           # not reported -> unknown, never 0
    assert row["input_chars"] == usage.input_chars(MESSAGES, SCHEMA) and row["max_output_tokens"] == 256


def test_failed_attempts_keep_their_usage_and_every_retry_is_a_row(tmp_path):
    adapter = UsageAdapter([
        ({"wrong": 1}, FULL),                                            # schema-invalid
        AdapterError("cut off", outcome="output_limit", usage=dict(FULL, output_tokens=256)),
        AdapterError("connection reset"),                                # no response, no usage
    ])
    with pytest.raises(ParseError):
        _client(tmp_path, adapter).parse(MESSAGES, SCHEMA, MODEL)
    rows = _rows(tmp_path)
    assert [(r["retry"], r["status"]) for r in rows] == [
        (0, "schema_error"), (1, "output_limit"), (2, "adapter_error")]
    assert rows[0]["input_tokens"] == 120 and rows[1]["output_tokens"] == 256
    assert rows[2]["usage_family"] is None and rows[2]["input_tokens"] is None


def test_an_adapter_that_reports_no_usage_leaves_every_token_field_unknown(tmp_path):
    _client(tmp_path, PlainAdapter()).parse(MESSAGES, SCHEMA, MODEL)
    [row] = _rows(tmp_path)
    assert row["status"] == "ok"
    assert all(row[f] is None for f in usage.TOKEN_FIELDS)


def test_a_crash_between_send_and_finish_leaves_a_visible_unfinished_attempt(tmp_path):
    ledger = usage.UsageLedger(_jobs(tmp_path))
    with db.bound_invocation("job_crash", "enrich"):
        ledger.start(model_ref=MODEL, provider="anthropic", retry=0, input_chars=10,
                     max_output_tokens=256)                          # ... and the process dies
    s = usage.summarize(_jobs(tmp_path), "job_crash")[MODEL]
    assert s["attempts"] == 1 and s["unfinished"] == 1
    assert s["unknown"] == {f: 1 for f in usage.TOKEN_FIELDS}


def test_an_interrupt_during_the_call_is_finalized_and_re_raised(tmp_path):
    adapter = UsageAdapter([KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        _client(tmp_path, adapter).parse(MESSAGES, SCHEMA, MODEL)
    [row] = _rows(tmp_path)
    assert row["status"] == "adapter_error" and row["input_tokens"] is None


def test_an_attempt_that_cannot_be_recorded_is_not_sent(tmp_path, monkeypatch):
    adapter = UsageAdapter([({"summary": "ok"}, FULL)])
    client = _client(tmp_path, adapter)

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(usage.UsageLedger, "_connect", broken)
    with pytest.raises(usage.UsageLedgerError):
        client.parse(MESSAGES, SCHEMA, MODEL)
    assert adapter.calls == 0


def test_cache_replays_are_counted_as_avoided_calls_not_attempts(tmp_path):
    first = UsageAdapter([({"summary": "ok"}, FULL)])
    with db.bound_invocation("job_c", "enrich"):
        _client(tmp_path, first, cache=True).parse(MESSAGES, SCHEMA, MODEL)
        second = UsageAdapter([])
        _client(tmp_path, second, cache=True).parse(MESSAGES, SCHEMA, MODEL)
        _client(tmp_path, second, cache=True).parse(MESSAGES, SCHEMA, MODEL)
    assert second.calls == 0
    s = usage.summarize(_jobs(tmp_path), "job_c")[MODEL]
    assert s["attempts"] == 1 and s["cache_hits"] == 2


def test_the_ledger_stores_no_prompt_or_response_content(tmp_path):
    adapter = UsageAdapter([({"summary": "RESPONSE-TEXT"}, FULL)])
    _client(tmp_path, adapter).parse(MESSAGES, SCHEMA, MODEL)
    raw = _jobs(tmp_path).read_bytes()
    assert SECRET_PROMPT.encode() not in raw and b"RESPONSE-TEXT" not in raw


# --- attribution --------------------------------------------------------------------------------


def test_attempts_belong_to_the_running_job_until_it_ends(tmp_path):
    jobs = _jobs(tmp_path)
    db.init_db(jobs)
    conn = db.connect(jobs)
    try:
        db.insert_job(conn, job_id="job_x", job_type="enrich", status="running",
                      created_at="t", started_at="t")
        _client(tmp_path, UsageAdapter([({"summary": "ok"}, FULL)])).parse(MESSAGES, SCHEMA, MODEL)
        db.update_job(conn, "job_x", status="succeeded", finished_at="t")
    finally:
        conn.close()
    assert db.current_job() is None
    _client(tmp_path, UsageAdapter([({"summary": "ok"}, FULL)])).parse(MESSAGES, SCHEMA, MODEL)
    rows = _rows(tmp_path)
    assert [(r["job_id"], r["producer"]) for r in rows] == [("job_x", "enrich"), (None, None)]


def test_a_bound_invocation_attributes_and_then_restores(tmp_path):
    with db.bound_invocation("qry_1", "query"):
        assert db.current_job() == ("qry_1", "query")
        _client(tmp_path, UsageAdapter([({"summary": "ok"}, FULL)])).parse(MESSAGES, SCHEMA, MODEL)
    assert db.current_job() is None
    assert _rows(tmp_path)[0]["job_id"] == "qry_1"


def test_build_client_always_wires_the_ledger(tmp_path):
    from app.backend.config import get_settings
    client = build_client(get_settings(tmp_path))
    assert isinstance(client._ledger, usage.UsageLedger)


# --- provider categories ------------------------------------------------------------------------


def test_anthropic_usage_keeps_its_categories_and_partial_reports():
    resp = SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                                 cache_read_input_tokens=None,
                                                 cache_creation_input_tokens=7))
    assert adapters._anthropic_usage(resp) == {
        "family": "anthropic", "input_tokens": 10, "output_tokens": 5,
        "cache_read_tokens": None, "cache_write_tokens": 7}
    assert adapters._anthropic_usage(SimpleNamespace()) is None
    # Every field is independently unknown: a missing count is None, never 0.
    only_input = SimpleNamespace(usage=SimpleNamespace(input_tokens=4))
    assert adapters._anthropic_usage(only_input) == {
        "family": "anthropic", "input_tokens": 4, "output_tokens": None,
        "cache_read_tokens": None, "cache_write_tokens": None}


def test_openai_usage_keeps_its_categories_and_local_omissions():
    resp = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=100, completion_tokens=None,
        prompt_tokens_details=SimpleNamespace(cached_tokens=40)))
    assert adapters._openai_usage(resp) == {
        "family": "openai_compatible", "input_tokens": 100, "output_tokens": None,
        "cache_read_tokens": 40, "cache_write_tokens": None}
    assert adapters._openai_usage(SimpleNamespace(usage=None)) is None   # a local server omitting it


def test_an_openai_length_stop_is_an_output_limit_with_usage(monkeypatch):
    resp = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=256, prompt_tokens_details=None),
        choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content='{"su'))])
    fake_openai = SimpleNamespace(OpenAI=lambda **k: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: resp))))
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    with pytest.raises(AdapterError) as err:
        adapters.LocalAdapter(base_url="http://x").parse_with_usage(MESSAGES, SCHEMA, "m", max_tokens=256)
    assert err.value.outcome == "output_limit" and err.value.usage["output_tokens"] == 256


def test_an_anthropic_non_json_response_still_carries_its_usage(monkeypatch):
    resp = SimpleNamespace(stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=9, output_tokens=3,
                                                 cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0),
                           content=[SimpleNamespace(type="text", text="not json")])
    fake_anthropic = SimpleNamespace(
        NOT_GIVEN=None,
        Anthropic=lambda **k: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: resp)))
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    with pytest.raises(AdapterError) as err:
        adapters.AnthropicAdapter(api_key="k").parse_with_usage(MESSAGES, SCHEMA, "m", max_tokens=64)
    assert err.value.outcome == "adapter_error" and err.value.usage["input_tokens"] == 9


# --- reading and surfacing ----------------------------------------------------------------------


def test_summary_and_report_say_reported_tokens_and_count_unknowns(tmp_path):
    with db.bound_invocation("job_r", "extract_claims"):
        _client(tmp_path, UsageAdapter([({"summary": "ok"}, FULL)])).parse(MESSAGES, SCHEMA, MODEL)
        _client(tmp_path, PlainAdapter()).parse(MESSAGES, SCHEMA, MODEL)
    s = usage.summarize(_jobs(tmp_path), "job_r")[MODEL]
    assert s["attempts"] == 2 and s["outcomes"]["ok"] == 2
    assert s["reported"]["input_tokens"] == 120 and s["unknown"]["input_tokens"] == 1
    assert s["unknown"]["cache_write_tokens"] == 2
    lines = usage.report_lines(_jobs(tmp_path), "job_r")
    assert lines[0].startswith("LLM usage (reported tokens")
    assert "2 attempt(s)" in lines[1] and "input 120 (+1 unknown)" in lines[2]
    assert usage.report_lines(_jobs(tmp_path), "job_none") == [
        "LLM usage: no model attempts and no cache hits."]


def test_jobs_endpoints_carry_per_model_usage(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.backend import main as main_module
    from app.backend.config import get_settings
    settings = get_settings(tmp_path)
    monkeypatch.setattr(main_module, "settings", settings)
    jobs = settings.jobs_db_path
    db.init_db(jobs)
    conn = db.connect(jobs)
    try:
        db.insert_job(conn, job_id="job_api", job_type="enrich", status="running",
                      created_at="t", started_at="t")
        _client(tmp_path, UsageAdapter([({"summary": "ok"}, FULL)])).parse(MESSAGES, SCHEMA, MODEL)
        db.update_job(conn, "job_api", status="succeeded", finished_at="t")
        db.insert_job(conn, job_id="job_free", job_type="lint", status="succeeded", created_at="t")
    finally:
        conn.close()
    api = TestClient(main_module.app)
    listed = {j["job_id"]: j for j in api.get("/jobs").json()["jobs"]}
    assert listed["job_api"]["llm_usage"][MODEL]["reported"]["input_tokens"] == 120
    assert listed["job_free"]["llm_usage"] == {}
    assert api.get("/jobs/job_api").json()["llm_usage"][MODEL]["attempts"] == 1


def test_query_attempts_are_recorded_under_a_returned_invocation_id(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import tests.test_api as api_t
    from app.backend import main as main_module
    from app.backend.config import get_settings
    settings = get_settings(tmp_path)
    settings.manifests_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    api_t._build_query_corpus(tmp_path)
    answer = {"claims": [{"text": "Synergy capture is central to integration.", "evidence_ids": ["e1"]}]}
    client = _client(tmp_path, UsageAdapter([(answer, FULL)]))
    monkeypatch.setattr(main_module, "_query_client", lambda: client)
    body = TestClient(main_module.app).post("/query", json={"question": "synergy capture"}).json()
    inv = body["invocation_id"]
    assert inv and inv.startswith("qry_") and body["query_id"] is None      # not saved, still durable
    [row] = _rows(tmp_path)
    assert (row["job_id"], row["producer"], row["status"]) == (inv, "query", "ok")


def test_a_producer_run_attributes_its_attempts_to_its_job_and_topic(tmp_path):
    import tests.test_synthesis as syn_t
    from app.workers import synthesis
    jobs = syn_t._build(tmp_path)
    client = LLMClient({"anthropic": syn_t.SynthAdapter()},
                       cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"),
                       ledger=usage.UsageLedger(jobs))
    out = synthesis.generate_syntheses(tmp_path, client=client, model_ref=syn_t.HEAVY,
                                       jobs_db=jobs, rebuild_index=False)
    assert out["syntheses_written"] == 1
    [row] = [r for r in _rows(tmp_path) if r["job_id"] == out["job_id"]]
    assert row["producer"] == "generate_synthesis" and row["operation"].startswith("itm_")
    assert row["status"] == "ok" and row["input_tokens"] is None     # the fake reports no usage
    assert db.current_job() is None                                   # cleared when the job ended


# --- review round: SDK retries, ledger read states, nested/concurrent attribution, finalize faults


def _mock_sdk(monkeypatch, module, ctor, responses):
    """Build the real SDK client the adapter asks for (its own kwargs, incl. `max_retries`) over a
    mocked HTTP transport serving `responses` in order; return the list of requests sent."""
    httpx = pytest.importorskip("httpx")
    sent, queue = [], list(responses)

    def handler(request):
        sent.append(request)
        status, body = queue.pop(0)
        return httpx.Response(status, json=body)

    real = getattr(module, ctor)
    monkeypatch.setattr(module, ctor, lambda **kw: real(
        **kw, http_client=httpx.Client(transport=httpx.MockTransport(handler))))
    return sent


_ANTHROPIC_OK = {"id": "msg_1", "type": "message", "role": "assistant", "model": "m",
                 "content": [{"type": "text", "text": '{"summary": "s"}'}],
                 "stop_reason": "end_turn", "stop_sequence": None,
                 "usage": {"input_tokens": 11, "output_tokens": 4}}
_OPENAI_OK = {"id": "c1", "object": "chat.completion", "created": 0, "model": "m",
              "choices": [{"index": 0, "finish_reason": "stop",
                           "message": {"role": "assistant", "content": '{"summary": "s"}'}}],
              "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}}
_SERVER_ERROR = (500, {"type": "error", "error": {"type": "api_error", "message": "boom"}})


@pytest.mark.parametrize("retries", [0, 1])
def test_every_anthropic_http_request_is_a_recorded_attempt(tmp_path, monkeypatch, retries):
    anthropic = pytest.importorskip("anthropic")     # the `enrich` extra
    sent = _mock_sdk(monkeypatch, anthropic, "Anthropic", [_SERVER_ERROR, (200, _ANTHROPIC_OK)])
    client = _client(tmp_path, adapters.AnthropicAdapter(api_key="k"), retries=retries)
    if retries == 0:                   # the SDK must not retry the 500 behind the client's back
        with pytest.raises(ParseError):
            client.parse(MESSAGES, SCHEMA, MODEL)
    else:
        assert client.parse(MESSAGES, SCHEMA, MODEL) == {"summary": "s"}
    rows = _rows(tmp_path)
    assert len(sent) == len(rows) == retries + 1
    assert rows[-1]["status"] == ("adapter_error" if retries == 0 else "ok")
    if retries:
        assert rows[-1]["input_tokens"] == 11 and rows[-1]["output_tokens"] == 4


def test_every_openai_wire_http_request_is_a_recorded_attempt(tmp_path, monkeypatch):
    openai = pytest.importorskip("openai")           # the `enrich-openai` extra
    sent = _mock_sdk(monkeypatch, openai, "OpenAI", [_SERVER_ERROR, (200, _OPENAI_OK)])
    client = LLMClient({"local": adapters.LocalAdapter(base_url="http://local.test/v1")},
                       max_tokens=256, max_retries=1, ledger=usage.UsageLedger(_jobs(tmp_path)))
    assert client.parse(MESSAGES, SCHEMA, "local:m") == {"summary": "s"}
    rows = _rows(tmp_path)
    assert len(sent) == len(rows) == 2
    assert [r["status"] for r in rows] == ["adapter_error", "ok"]
    assert rows[1]["usage_family"] == "openai_compatible" and rows[1]["input_tokens"] == 20


def test_unrecorded_usage_is_never_reported_as_no_usage(tmp_path):
    jobs = _jobs(tmp_path)
    assert usage.summarize(jobs, "job_a") is None                  # no database at all
    db.init_db(jobs)
    conn = db.connect(jobs)
    try:
        db.insert_job(conn, job_id="job_old", job_type="enrich", status="succeeded",
                      created_at="2020-01-01T00:00:00+00:00")
        for table in ("llm_usage", "llm_cache_hits", "llm_ledger_meta"):  # an older database
            conn.execute(f"DROP TABLE {table}")
        conn.commit()
    finally:
        conn.close()
    assert usage.summarize(jobs, "job_old") is None
    assert usage.report_lines(jobs, "job_old") == [
        "LLM usage: not recorded — this job predates the usage ledger."]
    db.init_db(jobs)                    # the ledger arrives; the old job stays not-recorded
    assert usage.summarize(jobs, "job_old") is None
    conn = db.connect(jobs)
    try:
        db.insert_job(conn, job_id="job_new", job_type="lint", status="succeeded",
                      created_at="2999-01-01T00:00:00+00:00")
    finally:
        conn.close()
    assert usage.summarize(jobs, "job_new") == {}                   # recorded: no model attempt


def test_an_unreadable_ledger_is_unknown_not_empty(tmp_path):
    jobs = _jobs(tmp_path)
    jobs.parent.mkdir(parents=True)
    jobs.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(usage.LedgerUnavailable):
        usage.summarize(jobs, "job_x")
    [line] = usage.report_lines(jobs, "job_x")
    assert line.startswith("LLM usage: unknown — could not read the LLM usage ledger")


def test_jobs_endpoints_say_when_usage_is_unknown(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.backend import main as main_module
    from app.backend.config import get_settings
    settings = get_settings(tmp_path)
    monkeypatch.setattr(main_module, "settings", settings)
    db.init_db(settings.jobs_db_path)
    conn = db.connect(settings.jobs_db_path)
    try:
        db.insert_job(conn, job_id="job_old", job_type="enrich", status="succeeded",
                      created_at="2020-01-01T00:00:00+00:00")
        db.insert_job(conn, job_id="job_new", job_type="lint", status="succeeded",
                      created_at="2999-01-01T00:00:00+00:00")
    finally:
        conn.close()
    api = TestClient(main_module.app)
    listed = {j["job_id"]: j for j in api.get("/jobs").json()["jobs"]}
    assert listed["job_old"]["llm_usage_status"] == "not_recorded"
    assert listed["job_new"]["llm_usage_status"] == "recorded"
    assert listed["job_new"]["llm_usage"] == {}

    def unreadable(*a, **k):
        raise usage.LedgerUnavailable("could not read the LLM usage ledger: disk I/O error")
    monkeypatch.setattr(usage, "summarize_many", unreadable)
    assert {j["llm_usage_status"] for j in api.get("/jobs").json()["jobs"]} == {"unavailable"}
    one = api.get("/jobs/job_new").json()
    assert one["llm_usage_status"] == "unavailable" and one["llm_usage"] == {}


def _start(conn, job_id, job_type):
    db.insert_job(conn, job_id=job_id, job_type=job_type, status="running",
                  created_at="t", started_at="t")


def _end(conn, job_id):
    db.update_job(conn, job_id, status="succeeded", finished_at="t")


def test_a_nested_job_restores_its_parent_whatever_order_they_end(tmp_path):
    jobs = _jobs(tmp_path)
    db.init_db(jobs)
    conn = db.connect(jobs)
    try:
        _start(conn, "job_parent", "generate_synthesis")
        _start(conn, "job_child", "enrich")
        assert db.current_job() == ("job_child", "enrich")
        _end(conn, "job_child")
        assert db.current_job() == ("job_parent", "generate_synthesis")
        _end(conn, "job_parent")
        assert db.current_job() is None
        _start(conn, "job_p2", "lint")
        _start(conn, "job_c2", "reindex")
        _end(conn, "job_p2")                                   # the parent ends first
        assert db.current_job() == ("job_c2", "reindex")
        _end(conn, "job_c2")
        assert db.current_job() is None
    finally:
        conn.close()


def test_concurrent_contexts_each_attribute_to_their_own_job(tmp_path):
    import threading
    jobs = _jobs(tmp_path)
    db.init_db(jobs)
    barrier = threading.Barrier(2)
    seen, errors = {}, []

    def run(job_id):
        try:
            conn = db.connect(jobs)
            try:
                _start(conn, job_id, "enrich")
                barrier.wait(timeout=10)         # both jobs are running at once
                _client(tmp_path, UsageAdapter([({"summary": "ok"}, FULL)])).parse(
                    MESSAGES, SCHEMA, MODEL, operation=job_id)
                seen[job_id] = db.current_job()
                barrier.wait(timeout=10)
                _end(conn, job_id)
            finally:
                conn.close()
        except BaseException as exc:             # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(j,)) for j in ("job_t1", "job_t2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert seen == {"job_t1": ("job_t1", "enrich"), "job_t2": ("job_t2", "enrich")}
    assert {(r["job_id"], r["operation"]) for r in _rows(tmp_path)} == {
        ("job_t1", "job_t1"), ("job_t2", "job_t2")}
    assert db.current_job() is None              # the main context never saw either job


def test_a_finalize_failure_keeps_the_response_and_a_visible_unfinished_row(
        tmp_path, monkeypatch, caplog):
    ledger = usage.UsageLedger(_jobs(tmp_path))
    real_connect = ledger._connect
    calls = []

    def flaky_connect():
        calls.append(1)
        if len(calls) == 2:                      # 1 = start (succeeds), 2 = finish (fails)
            raise sqlite3.OperationalError("database is locked")
        return real_connect()

    monkeypatch.setattr(ledger, "_connect", flaky_connect)
    client = LLMClient({"anthropic": UsageAdapter([({"summary": "paid"}, FULL)])},
                       max_tokens=256, max_retries=0, ledger=ledger)
    with db.bound_invocation("job_f", "enrich"), caplog.at_level("WARNING"):
        assert client.parse(MESSAGES, SCHEMA, MODEL) == {"summary": "paid"}
    [row] = _rows(tmp_path)
    assert row["status"] == "started" and row["finished_at"] is None
    assert row["input_tokens"] is None
    assert usage.summarize(_jobs(tmp_path), "job_f")[MODEL]["unfinished"] == 1
    assert "could not finalize" in caplog.text
