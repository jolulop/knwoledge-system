#!/usr/bin/env python3
"""CLI: contradiction checks (ADR-0069).

Usage:
    uv run python scripts/detect_contradictions.py            # prints this usage; runs nothing
    uv run python scripts/detect_contradictions.py maintain   # model-free contradiction maintenance
    uv run python scripts/detect_contradictions.py estimate [--model REF] <sid> <sid> ...
    uv run python scripts/detect_contradictions.py run --token <id> [--allow-paid]
                                                       [--max-calls N] [--max-input-tokens N]

The old no-argument run judged every candidate claim pair across the whole vault with one tier-3
call per pair; under ADR-0066's broad tags that no longer bounded the work (the part-3 UAT built
10,500 pairs). It was removed in slice 0, with no legacy flag. No path of this command judges the
whole vault.

`maintain` (slice 2(a)) makes no model call: it retires **proposed** contradictions whose claims lost
their evidence, whose wording or evidence changed since they were judged, or whose documents are no
longer independent. It never changes an approved (active) contradiction or a decision awaiting
Apply, and it does not check documents for contradictions. The same maintenance already runs,
targeted, after a Details save or clear and after claim re-extraction; `maintain` is the full repair
path that lint and the Source screen point to.

`estimate` (slice 2(b)) plans a check of the documents you select — eligible and skipped pairs, the
window cross-product, expected cache hits, the model and whether it needs paid authorization,
approximate tokens and the budget — makes no model call, and saves the plan under a token when it
fits. `run --token` executes exactly that plan (see `app/workers/contradiction_check.py`); a
non-local model needs `--allow-paid`; `--max-calls`/`--max-input-tokens` may only lower the limits.

Existing contradiction review items are resolved by Apply (`/reviews/apply`,
`scripts/apply_reviews.py`). Exit status: 0 for `-h`/`--help`, a successful `maintain`, an estimate
that issued a token and a clean run (status `succeeded`); 1 when `maintain` fails, an estimate or run is
refused, or a run is not a clean success (incomplete coverage, an output-limit violation or a
failed maintenance); 2 for no or unknown arguments (so automation that still
calls the old run fails visibly); 3 when another `detect_contradictions` run holds its guard.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

USAGE = """\
usage: detect_contradictions.py [-h] {maintain,estimate,run} ...

  maintain                         retire proposed contradictions whose evidence or independence
                                   changed (no model call)
  estimate [--model REF] SID SID…  plan a check of the selected documents (no model call); prints
                                   the window plan, the budget and a token
  run --token ID [--allow-paid] [--max-calls N] [--max-input-tokens N]
                                   execute exactly that estimate (limits may only be lowered)

Whole-vault contradiction judging was removed (ADR-0069 slice 0). Existing contradiction review
items are resolved by Apply (/reviews/apply or scripts/apply_reviews.py).
"""


class _Usage(ValueError):
    pass


def _parse(argv: list[str], *, values: tuple[str, ...], flags: tuple[str, ...]
           ) -> tuple[dict[str, object], list[str]]:
    opts: dict[str, object] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in values:
            if i + 1 >= len(argv) or argv[i + 1].startswith("--") or arg in opts:
                raise _Usage(arg)
            opts[arg] = argv[i + 1]
            i += 2
        elif arg in flags:
            opts[arg] = True
            i += 1
        elif arg.startswith("--"):
            raise _Usage(arg)
        else:
            rest.append(arg)
            i += 1
    return opts, rest


def _n(value: int) -> str:
    return f"{value:,}"


def _maintain() -> int:
    from app.backend.config import get_settings
    from app.workers import contradictions
    settings = get_settings()
    try:
        summary = contradictions.maintain(settings.root, manifests_dir=settings.manifests_dir,
                                          jobs_db=settings.jobs_db_path,
                                          graph_db=settings.graph_db_path,
                                          wiki_dir=settings.wiki_dir,
                                          reviews_dir=settings.reviews_dir)
    except Exception as exc:
        print(f"Contradiction maintenance failed: {exc}", file=sys.stderr)
        return 1
    print(f"Job: {summary['job_id']} ({summary['status']})")
    print(f"Proposed contradictions checked: {summary['assertions_checked']}")
    print(f"Retired: {len(summary['retired'])}")
    for e in summary["retired"]:
        reason = contradictions.RETIRE_REASONS[e["reason"]]
        print(f"  {e['claim_a']} <-> {e['claim_b']}: {reason} ({e['review_id']} withdrawn)")
    if summary["awaiting_apply"]:
        print(f"Decided, awaiting Apply (left as they are): {summary['awaiting_apply']}")
    if summary["unverifiable"]:
        print(f"Couldn't be checked (left as they are): {len(summary['unverifiable'])}")
        for e in summary["unverifiable"]:
            reason = contradictions.UNVERIFIABLE_REASONS[e["reason"]]
            print(f"  {e['claim_a']} <-> {e['claim_b']}: {reason}")
    print("Maintenance is current. This does not check documents for contradictions.")
    return 0


def _estimate(argv: list[str]) -> int:
    from app.backend.config import get_settings
    from app.workers import contradiction_check as cc
    opts, sids = _parse(argv, values=("--model",), flags=())
    settings = get_settings()
    try:
        result = cc.estimate(settings, sids, model=opts.get("--model"))
    except cc.Refused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    plan, check, names = result["plan"], result["budget"], result["titles"]
    print("Contradiction check estimate (no model call was made)")
    print(f"Documents ({len(plan['documents'])}):")
    for sid in plan["documents"]:
        print(f"  {sid}  {names[sid]}")
    eligible = [p for p in plan["pairs"] if p["eligible"]]
    print(f"Document pairs: {len(plan['pairs'])} ({len(eligible)} eligible, "
          f"{len(plan['pairs']) - len(eligible)} skipped)")
    for p in plan["pairs"]:
        if p["eligible"]:
            print(f"  {p['a']} <-> {p['b']}: {p['claims_a']} x {p['claims_b']} claims -> "
                  f"{p['windows_a']} x {p['windows_b']} windows = "
                  f"{p['windows_a'] * p['windows_b']} judge call(s)")
        else:
            print(f"  {p['a']} <-> {p['b']}: skipped — {p['reason']}")
    print("Every window of A is compared with every window of B, so the calls grow with the "
          "product of the windows, not linearly.")
    print(f"Windows: {check['windows']} total; expected cache hits: {check['cache_hits']}; "
          f"calls to send: {check['calls']} (retries, if any, also count against the call cap)")
    model = plan["model_ref"]
    if plan["paid"]:
        print(f"Model: {model} at {plan['endpoint']} — requires paid authorization "
              "(pass --allow-paid on the run; actual billing can vary)")
    else:
        print(f"Model: {model} at {plan['endpoint']} — a configured endpoint (not proof that "
              "it runs on this device or for free)")
    if not result["available"]:
        print(f"  {model} is not configured (no credential or base URL): the run will refuse; "
              "no other model is substituted")
    lim = check["limits"]
    print(f"Per-call output limit: {_n(plan['max_output_tokens'])} tokens")
    print(f"Input: {_n(check['input_chars'])} characters ≈ {_n(check['estimated_input_tokens'])} "
          f"tokens (estimated at {cc.CHARS_PER_TOKEN} characters per token, not reported)")
    print(f"Output: at most {_n(check['max_output_tokens'])} tokens ({check['calls']} call(s) x "
          f"{_n(plan['max_output_tokens'])}; bounded only where the endpoint enforces the limit)")
    print(f"Budget: calls {check['calls']} / {lim['max_calls']} and input characters "
          f"{_n(check['input_chars'])} / {_n(lim['max_input_chars'])} (hard caps); estimated "
          f"input tokens {_n(check['estimated_input_tokens'])} / {_n(lim['max_input_tokens'])} "
          f"(approximate) — {'fits' if check['fits'] else 'OVER: ' + ', '.join(check['over'])}")
    if result["refused"] == "nothing_to_check":
        print("Nothing to check: no eligible pair has claims with evidence on both sides. No "
              "token was issued.")
        return 1
    if result["refused"] == "over_budget":
        print("Over budget: no token was issued. Select fewer documents, or raise "
              "CONTRADICTION_MAX_CALLS / CONTRADICTION_MAX_INPUT_TOKENS and estimate again.")
        return 1
    flag = " --allow-paid" if plan["paid"] else ""
    print(f"Token: {result['token']}")
    print(f"Run it: {cc.RUN_COMMAND} {result['token']}{flag}")
    return 0


def _int(opts: dict[str, object], name: str) -> int | None:
    if name not in opts:
        return None
    try:
        return int(str(opts[name]))
    except ValueError:
        raise _Usage(name) from None


def _run(argv: list[str]) -> int:
    from app.backend.config import get_settings
    from app.llm import usage
    from app.workers import contradiction_check as cc
    opts, rest = _parse(argv, values=("--token", "--max-calls", "--max-input-tokens"),
                        flags=("--allow-paid",))
    if rest or "--token" not in opts:
        raise _Usage("run")
    max_calls, max_input = _int(opts, "--max-calls"), _int(opts, "--max-input-tokens")
    settings = get_settings()
    try:
        s = cc.run(settings, str(opts["--token"]), allow_paid=bool(opts.get("--allow-paid")),
                   max_calls=max_calls, max_input_tokens=max_input)
    except cc.Refused as exc:
        print(f"Refused (nothing was sent; the token is still usable unless it is unknown): {exc}",
              file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Contradiction check failed: {exc}", file=sys.stderr)
        return 1
    cov, b = s["coverage"], s["budget"]
    print(f"Job: {s['job_id']} ({s['status']})")
    print(f"Model: {s['model_ref']} at {s['endpoint']} (prompt {s['prompt_version']})")
    print(f"Windows: {cov['windows']} — {len(cov['completed'])} completed "
          f"({cov['from_cache']} from cache), {len(cov['failed'])} failed, "
          f"{len(cov['remaining'])} remaining")
    if cov["complete"]:
        print("Coverage: complete — every window of each eligible pair was compared.")
    else:
        print("Coverage: INCOMPLETE — this is not a complete comparison of the selected documents.")
        for wid, why in cov["failed"].items():
            print(f"  failed: {wid}: {why}")
        for wid in cov["remaining"]:
            print(f"  remaining: {wid}")
    if s["stopped"]:
        print(s["stopped"])
    print(f"Contradictions proposed: {s['contradictions_proposed']} (review them in /reviews)")
    for key, label in (("already_proposed", "already proposed (left as they are)"),
                       ("skipped_human_decided", "skipped, decided by a human"),
                       ("out_of_scope_pairs_dropped", "out-of-scope pairs dropped")):
        if s[key]:
            print(f"  {label}: {s[key]}")
    if s["maintenance"]["retired"] or s["maintenance"]["error"]:
        print(f"Maintenance before judging: {s['maintenance']['retired']} retired"
              + (f"; failed: {s['maintenance']['error']}" if s["maintenance"]["error"] else ""))
    lim = b["limits"]
    print(f"Calls sent: {b['calls']} / {lim['max_calls']}; input characters "
          f"{_n(b['input_chars'])} / {_n(lim['max_input_chars'])}")
    print(f"Input tokens: {_n(b['reported_input_tokens'])} reported; "
          f"{_n(b['estimated_input_tokens_not_reported'])} estimated, not reported")
    print(f"Output tokens: {_n(b['reported_output_tokens'])} reported; "
          f"{_n(b['reserved_output_tokens_not_reported'])} reserved, not reported")
    for line in usage.report_lines(settings.jobs_db_path, s["job_id"]):
        print(line)
    if cov["complete"] and s["status"] != "succeeded":
        print("Not a clean run: see the output-limit violation or the maintenance failure above.")
    return 0 if s["status"] == "succeeded" else 1


def main(argv: list[str]) -> int:
    if any(a in ("-h", "--help") for a in argv):
        print(USAGE, end="")
        return 0
    try:
        if argv == ["maintain"]:
            return _maintain()
        if argv[:1] == ["estimate"]:
            return _estimate(argv[1:])
        if argv[:1] == ["run"]:
            return _run(argv[1:])
    except _Usage:
        pass
    print(USAGE, end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    if sys.argv[1:] == ["maintain"] or sys.argv[1:2] == ["run"]:
        from app.backend import run_guard  # noqa: E402 - one run at a time (ADR-0068)
        from app.backend.config import get_settings  # noqa: E402
        raise SystemExit(run_guard.run_cli(get_settings().db_dir, "detect_contradictions",
                                           lambda: main(sys.argv[1:])))
    raise SystemExit(main(sys.argv[1:]))
