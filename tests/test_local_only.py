"""The API answers only to its own page on this Mac (audit finding 1)."""

from fastapi.testclient import TestClient

import app as app_module


def _client(base="http://127.0.0.1:8377"):
    return TestClient(app_module.app, base_url=base)


def test_rebound_host_is_refused():
    tc = _client("http://attacker.example:8377")
    assert tc.get("/api/clients").status_code == 403


def test_local_hosts_are_served():
    tc = _client()
    for host in ("127.0.0.1:8377", "localhost:8377", "[::1]:8377"):
        assert tc.get("/", headers={"Host": host}).status_code == 200


def test_cross_site_post_is_refused():
    tc = _client()
    r = tc.post("/api/trash", content='{"path": "x.md"}',
                headers={"Origin": "https://evil.example", "Content-Type": "text/plain"})
    assert r.status_code == 403


def test_null_origin_post_is_refused():
    r = _client().post("/api/trash", json={"path": "x.md"}, headers={"Origin": "null"})
    assert r.status_code == 403


def test_sec_fetch_site_cross_site_is_refused():
    r = _client().post("/api/trash", json={"path": "x.md"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_same_origin_post_passes_the_guard():
    r = _client().post(
        "/api/trash", json={"path": "does/not/exist.md"},
        headers={"Origin": "http://127.0.0.1:8377", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code != 403
