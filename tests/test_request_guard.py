"""ADR-0067 request-origin guard: Host allowlist (DNS rebinding) + same-origin state changes (CSRF)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.backend import main as main_module
from app.backend.config import get_settings
from app.backend.main import request_refusal

APP_ORIGIN = "http://127.0.0.1:18000"
DELETE_UNUSED = "/ui/reviews/tags/unused/delete"      # the parameterless bulk POST the review named


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("KS_ALLOWED_HOSTS", raising=False)     # production default: loopback only
    settings = get_settings(tmp_path)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app, base_url=APP_ORIGIN)


@pytest.mark.parametrize("host", ["127.0.0.1:18000", "localhost:18000", "[::1]:18000", "127.0.0.1",
                                  "LOCALHOST:18000"])
def test_loopback_hosts_are_accepted(host, monkeypatch):
    monkeypatch.delenv("KS_ALLOWED_HOSTS", raising=False)
    assert request_refusal("GET", {"host": host}) is None


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example:18000", "", "10.0.0.5:18000"])
def test_other_hosts_are_refused_even_for_reads(host, monkeypatch):
    monkeypatch.delenv("KS_ALLOWED_HOSTS", raising=False)
    assert request_refusal("GET", {"host": host}) == "host_not_allowed"


def test_ks_allowed_hosts_extends_the_list(monkeypatch):
    monkeypatch.setenv("KS_ALLOWED_HOSTS", "ks.internal, proxy.lan")
    assert request_refusal("GET", {"host": "ks.internal:8443"}) is None
    assert request_refusal("GET", {"host": "other.lan"}) == "host_not_allowed"


@pytest.mark.parametrize("headers, expected", [
    ({"origin": APP_ORIGIN}, None),                                   # the UI's own forms
    ({"origin": "http://evil.example"}, "cross_origin"),
    ({"origin": "http://127.0.0.1:3000"}, "cross_origin"),            # another local app/port
    ({"origin": "null"}, "cross_origin"),                             # sandboxed / opaque origin
    ({"sec-fetch-site": "cross-site"}, "cross_site"),
    ({"sec-fetch-site": "same-site"}, "cross_site"),
    ({"sec-fetch-site": "same-origin"}, None),
    ({"sec-fetch-site": "none"}, None),                               # user-initiated navigation
    ({}, None),                                                       # curl / scripts
])
def test_state_changes_must_be_same_origin(headers, expected, monkeypatch):
    monkeypatch.delenv("KS_ALLOWED_HOSTS", raising=False)
    assert request_refusal("POST", {"host": "127.0.0.1:18000", **headers}) == expected


def test_reads_are_not_origin_checked(monkeypatch):
    monkeypatch.delenv("KS_ALLOWED_HOSTS", raising=False)
    assert request_refusal("GET", {"host": "127.0.0.1:18000", "origin": "http://evil.example"}) is None


def test_a_foreign_page_cannot_submit_the_bulk_delete(client):
    resp = client.post(DELETE_UNUSED, headers={"origin": "http://evil.example"})
    assert resp.status_code == 403 and "ADR-0067" in resp.json()["detail"]
    resp = client.post(DELETE_UNUSED, headers={"sec-fetch-site": "cross-site"})
    assert resp.status_code == 403


def test_the_ui_and_scripts_still_work(client):
    assert client.post(DELETE_UNUSED, headers={"origin": APP_ORIGIN}).status_code != 403
    assert client.post(DELETE_UNUSED, headers={"sec-fetch-site": "same-origin"}).status_code != 403
    assert client.post(DELETE_UNUSED).status_code != 403              # no browser headers
    assert client.get("/health").status_code == 200


def test_a_rebound_host_cannot_read_the_api(tmp_path, monkeypatch):
    monkeypatch.delenv("KS_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(main_module, "settings", get_settings(tmp_path))
    foreign = TestClient(main_module.app, base_url="http://attacker.example:18000")
    assert foreign.get("/health").status_code == 403
    assert foreign.get("/ui/reviews").status_code == 403


# --- follow-up review: exact scheme, malformed origins, API writes are vault writers -------------


def test_the_origin_scheme_must_match_the_request_scheme(monkeypatch):
    monkeypatch.delenv("KS_ALLOWED_HOSTS", raising=False)
    host = {"host": "127.0.0.1:18000"}
    assert request_refusal("POST", {**host, "origin": "https://127.0.0.1:18000"}, "http") == "cross_origin"
    assert request_refusal("POST", {**host, "origin": "http://127.0.0.1:18000"}, "https") == "cross_origin"
    assert request_refusal("POST", {**host, "origin": "https://127.0.0.1:18000"}, "https") is None


@pytest.mark.parametrize("origin", ["http://[", "http://127.0.0.1:99999999", "http://[::1", "::::"])
def test_a_malformed_origin_is_refused_not_an_error(origin, monkeypatch):
    monkeypatch.delenv("KS_ALLOWED_HOSTS", raising=False)
    assert request_refusal("POST", {"host": "127.0.0.1:18000", "origin": origin}) == "cross_origin"


def test_a_malformed_origin_gets_403_through_the_app(client):
    assert client.post(DELETE_UNUSED, headers={"origin": "http://["}).status_code == 403


def test_an_api_write_in_flight_holds_off_the_backup(client, tmp_path, monkeypatch):
    import sys
    import threading
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import backup
    from app.workers import tag_links
    inside, release = threading.Event(), threading.Event()

    def slow_delete(*args, **kwargs):
        inside.set()
        release.wait(5)
        return {"deleted": [], "skipped": [], "affected_sources": []}

    monkeypatch.setattr(tag_links, "delete_unused", slow_delete)
    monkeypatch.setattr(main_module, "_open_graph_safe", lambda: _FakeConn())
    monkeypatch.setattr(tag_links, "unused_tags", lambda *a, **k: [])
    worker = threading.Thread(target=lambda: client.post(DELETE_UNUSED))
    worker.start()
    assert inside.wait(5)
    try:
        with pytest.raises(backup.BackupRefused):
            backup.create_backup(tmp_path, lock_timeout=0.2)        # the request is mid-write
    finally:
        release.set()
        worker.join(5)
    assert backup.create_backup(tmp_path, lock_timeout=2).exists()


class _FakeConn:
    def close(self):
        pass
