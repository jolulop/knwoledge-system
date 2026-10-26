# ADR-0067 — Request-origin guard (amends ADR-0009's CSRF deferral)

- **Status:** accepted and implemented (2026-10-27) — `app/backend/main.py`
  (`_request_origin_guard` middleware, `request_refusal`); tests `tests/test_request_guard.py`.
- **Date:** 2026-10-27
- **Drivers:** external review of ADR-0066 part 1 (review round 6, blocking issue 4). The Review UI is
  plain HTML forms over an unauthenticated, loopback-only API. ADR-0009/0035 deferred CSRF "while
  bound to loopback", but loopback binding does not protect against the operator's own browser:
  - a foreign web page can submit a form to `http://127.0.0.1:18000/…` without reading the
    response (a simple `POST` needs no CORS preflight). Bulk **Delete unused** is a parameterless
    `POST`, and keep/drop/block/apply are one form each — so the human-approval trail could record a
    decision the human never made;
  - a DNS-rebinding page (a hostile name that re-resolves to 127.0.0.1) is same-origin with itself,
    so it can also **read** responses — the vault's pages, search results and raw documents.
- **Related:** ADR-0009 (loopback-only bind, `assert_safe_bind`, `KS_ALLOW_INSECURE_BIND`),
  ADR-0035 (Review UI; CSRF deferred while loopback), ADR-0066 (immediate tag actions).

## Decision

One middleware runs before every route.

1. **Host allowlist (all methods).** The request's `Host` must name a loopback host — `127.0.0.1`,
   `localhost`, `::1`, any port — or a name listed in **`KS_ALLOWED_HOSTS`** (comma-separated; for
   the `KS_ALLOW_INSECURE_BIND` proxy/sidecar case). Anything else is refused. This defeats DNS
   rebinding: the rebound page's requests carry the attacker's host name.
2. **Same-origin state changes (`POST`/`PUT`/`PATCH`/`DELETE`).**
   - If an `Origin` header is present it must equal this server's own origin: the **request's
     scheme** (behind a TLS proxy, the forwarded scheme Uvicorn reports from trusted proxy headers)
     and its `Host`. An opaque `null` origin, a malformed one (`http://[`, a bad port) and another
     local port are refused.
   - Without `Origin`, a `Sec-Fetch-Site` other than `same-origin` or `none` is refused.
   - A request with neither header passes: it carries no browser provenance (curl, scripts,
     tests). Modern browsers send `Origin` on every cross-origin `POST`, so a foreign page cannot
     reach this branch.
3. **Refusal** is `403` with a JSON `detail` naming the reason (`host_not_allowed`,
   `cross_origin`, `cross_site`) and this ADR. Nothing runs, nothing is written.
4. **An allowed state-changing request is a vault writer:** the same middleware holds the vault
   writer lock shared for its duration (ADR-0039 amendment), so a backup never captures it
   half-done; it waits while a backup runs.

## Consequences

- The UI works unchanged from `http://127.0.0.1:<port>/` or `http://localhost:<port>/`; the
  documented scripts and `curl` calls are unaffected.
- Reaching the app under any other name (a LAN IP, a hosts-file alias, a proxy) requires listing it
  in `KS_ALLOWED_HOSTS`. That is deliberate friction on top of `KS_ALLOW_INSECURE_BIND`, which still
  provides **no authentication**.
- Tests: Starlette's `TestClient` sends `Host: testserver`; `tests/conftest.py` allows it through
  `KS_ALLOWED_HOSTS` for every test, and the guard's own tests clear it.

## Not decided here

- **Authentication** and per-form **CSRF tokens** remain Phase-8 work, required before any real
  non-loopback exposure. This guard is browser-provenance protection for the loopback posture, not
  an access control.
- A malicious **local process** is out of scope (it can already read the vault's files).

## Follow-up review (2026-10-27)

The original attack paths were verified closed. Two corrections: the Origin scheme is now compared
with the request's own scheme (it accepted either `http` or `https`), and a malformed Origin is a
`403` refusal instead of an unhandled `ValueError`.

