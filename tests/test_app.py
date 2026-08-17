"""Tests for the WildDuck panel.

These tests run against a throwaway SQLite database and stub out the
``api_request`` helper so no live WildDuck server is needed.
"""

import os
import tempfile

# Point the app at a throwaway database before importing it.
_TMPDIR = tempfile.mkdtemp()
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key")

import pytest  # noqa: E402

import app as app_module  # noqa: E402


app = app_module.app
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{_TMPDIR}/panel-test.db"
app.config["TESTING"] = True

CSRF = "test-csrf-token"


@pytest.fixture()
def db():
    with app.app_context():
        app_module.db.create_all()
        yield app_module.db
        app_module.db.session.remove()
        app_module.db.drop_all()


@pytest.fixture()
def client(db):
    app_module.LOGIN_ATTEMPTS.clear()
    with app.test_client() as c:
        yield c


def login(client):
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["username"] = "admin"
        sess["csrf_token"] = CSRF


def stub_api(monkeypatch, handler):
    """Replace ``api_request`` with a callable that receives (method, path)."""
    monkeypatch.setattr(app_module, "api_request", handler)


# ── Pure helpers ────────────────────────────────────────────────────────


def test_domain_of():
    assert app_module._domain_of("user@Example.com") == "example.com"
    assert app_module._domain_of("no-at-sign") is None
    assert app_module._domain_of(None) is None


def test_extract_results():
    assert app_module._extract_results([1, 2]) == [1, 2]
    assert app_module._extract_results({"results": [1, 2]}) == [1, 2]
    assert app_module._extract_results({"users": [3]}, "users") == [3]
    assert app_module._extract_results({"nothing": True}) == []


def test_gb_to_bytes():
    assert app_module.gb_to_bytes("1") == 1073741824
    assert app_module.gb_to_bytes(2) == 2147483648
    assert app_module.gb_to_bytes("abc") is None


def test_bytes_to_gb():
    assert app_module.bytes_to_gb(1073741824) == 1.0
    assert app_module.bytes_to_gb("nope") is None


# ── Auth & CSRF ─────────────────────────────────────────────────────────


def test_index_requires_login(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_csrf_rejects_missing_token(client, monkeypatch):
    login(client)
    stub_api(monkeypatch, lambda *a, **k: ({}, None))
    resp = client.post("/domain/alias", data={"domain": "a.com", "alias": "b.com"})
    assert resp.status_code == 302  # flash + redirect


def test_csrf_accepts_valid_token(client, monkeypatch):
    login(client)
    calls = []

    def handler(method, path, json_data=None, **kwargs):
        calls.append((method, path, json_data))
        return {"success": True, "id": "x"}, None

    stub_api(monkeypatch, handler)
    resp = client.post(
        "/domain/alias",
        data={"domain": "a.com", "alias": "b.com", "csrf_token": CSRF},
    )
    assert resp.status_code == 302
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/domainaliases"


def test_login_rate_limited(client, monkeypatch):
    stub_api(monkeypatch, lambda *a, **k: ({}, None))
    with client.session_transaction() as sess:
        sess["csrf_token"] = CSRF
    for _ in range(app_module.MAX_LOGIN_ATTEMPTS):
        client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "csrf_token": CSRF},
        )
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "wrong", "csrf_token": CSRF},
    )
    assert resp.status_code == 429


# ── Dashboard / pagination ─────────────────────────────────────────────


def test_dashboard_pagination(client, monkeypatch):
    login(client)

    def handler(method, path, params=None, **kwargs):
        if path == "/users":
            assert params.get("next") == "cursor-123"
            assert params.get("limit") == 50
            return {
                "results": [{"id": "u1"}],
                "total": 101,
                "nextCursor": "cursor-next",
                "previousCursor": "cursor-prev",
            }, None
        return {}, None

    stub_api(monkeypatch, handler)
    resp = client.get("/api/dashboard?tab=users&next=cursor-123")
    data = resp.get_json()
    assert data["total"] == 101
    assert data["next"] == "cursor-next"
    assert data["previous"] == "cursor-prev"


def test_domains_overview_derived(client, monkeypatch):
    login(client)

    def handler(method, path, **kwargs):
        if path == "/addresses":
            return {"results": [{"address": "a@example.com"}]}, None
        if path == "/domainaliases":
            return {"results": [{"id": "al1", "domain": "example.com", "alias": "example.org"}]}, None
        if path == "/dkim":
            return {"results": [{"id": "d1", "domain": "example.com", "selector": "default"}]}, None
        return {}, None

    stub_api(monkeypatch, handler)
    domains, err = app_module.get_domains_overview()
    assert err is None
    by_name = {d["domain"]: d for d in domains}
    assert by_name["example.com"]["dkim"]["id"] == "d1"
    assert by_name["example.org"]["is_alias"] is True


def test_cascade_delete_domain(client, monkeypatch):
    login(client)
    calls = []

    def handler(method, path, **kwargs):
        calls.append((method, path))
        if path == "/dkim/resolve/example.com":
            return {"id": "dkim1"}, None
        if path == "/domainaliases":
            return {"results": [
                {"id": "al1", "domain": "example.com", "alias": "example.org"},
                {"id": "al2", "domain": "other.com", "alias": "example.com"},
            ]}, None
        if path == "/addresses":
            return {"results": [
                {"id": "ad1", "address": "a@example.com", "forwarded": True},
                {"id": "ad2", "address": "b@example.com", "forwarded": False, "user": "u1"},
                {"id": "ad3", "address": "c@other.com", "forwarded": True},
            ]}, None
        return {"success": True}, None

    stub_api(monkeypatch, handler)
    resp = client.delete(
        "/domain/example.com/delete", headers={"X-CSRF-Token": CSRF}
    )
    assert resp.status_code == 200
    deleted = [p for m, p in calls if m == "DELETE"]
    assert "/dkim/dkim1" in deleted
    assert "/domainaliases/al1" in deleted
    assert "/domainaliases/al2" in deleted
    assert "/addresses/forwarded/a@example.com" in deleted
    assert "/users/u1/addresses/b@example.com" in deleted
    assert "/addresses/forwarded/c@other.com" not in deleted


# ── Audit log ───────────────────────────────────────────────────────────


def test_audit_log_written(client, monkeypatch):
    login(client)
    stub_api(monkeypatch, lambda *a, **k: ({"success": True}, None))
    client.post(
        "/domain/alias",
        data={"domain": "a.com", "alias": "b.com", "csrf_token": CSRF},
    )
    with app.app_context():
        entries = app_module.AuditLog.query.all()
    assert any("POST /domain/alias" in (e.action or "") for e in entries)
