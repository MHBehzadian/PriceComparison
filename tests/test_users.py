from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from pricecomparison.app import create_app
from pricecomparison.auth import Auth

from .test_api import FakeShops


@pytest.fixture
def two(tmp_path, monkeypatch):
    """An admin and a plain user, each with their own browser."""
    monkeypatch.setenv("PC_NO_MONITOR", "1")
    app = create_app(tmp_path, fetcher=FakeShops())
    with TestClient(app) as admin, TestClient(app) as user:
        assert admin.post("/api/setup", json={"username": "boss", "password": "secret1"}).status_code == 200
        assert admin.post("/api/users", json={"username": "ali", "password": "secret2", "role": "user"}).status_code == 200
        assert user.post("/api/login", json={"username": "ali", "password": "secret2"}).status_code == 200
        yield admin, user, app.state.store


def test_session_says_who_and_what(two):
    admin, user, _ = two
    assert admin.get("/api/session").json()["user"] == {"username": "boss", "role": "admin"}
    assert user.get("/api/session").json()["user"] == {"username": "ali", "role": "user"}
    assert user.get("/api/state").json()["me"]["role"] == "user"


def test_only_admin_changes_things(two):
    admin, user, store = two
    pid = admin.post("/api/products", json={"name": "سیمان", "urls": ["https://masaleh-tehran.ir/p/1"]}).json()["id"]
    oid = next(iter(store.offers))
    sid = next(iter(store.sites))
    forbidden = [
        ("post", "/api/products", {"name": "x"}),
        ("put", f"/api/products/{pid}", {"name": "y"}),
        ("delete", f"/api/products/{pid}", None),
        ("post", "/api/products/delete", {"ids": [pid]}),
        ("post", "/api/offers", {"product_id": pid, "url": "https://snappshop.ir/p"}),
        ("put", f"/api/offers/{oid}", {"enabled": False}),
        ("delete", f"/api/offers/{oid}", None),
        ("put", f"/api/offers/{oid}/unit", {"text": "تن"}),
        ("put", f"/api/products/{pid}/ref", {"dim": "mass", "amount": 1000}),
        ("post", "/api/sites", {"name": "x", "domains": ["x.ir"]}),
        ("put", f"/api/sites/{sid}", {"name": "x", "domains": ["x.ir"]}),
        ("delete", f"/api/sites/{sid}", None),
        ("post", "/api/import", {"urls": ["https://x.ir/shop/"]}),
        ("post", "/api/check", None),
        ("get", "/api/settings", None),
        ("get", "/api/users", None),
        ("post", "/api/users", {"username": "x", "password": "secret3"}),
        ("delete", "/api/users/boss", None),
    ]
    for method, path, body in forbidden:
        r = getattr(user, method)(path, json=body) if body is not None else getattr(user, method)(path)
        assert r.status_code == 403, (method, path, r.status_code)
    assert pid in store.products and store.offers[oid].enabled
    # looking and reading prices again is fine
    assert user.post(f"/api/products/{pid}/check").status_code == 200
    assert user.post(f"/api/offers/{oid}/check").status_code == 200
    st = user.get("/api/state").json()
    assert "proxy" not in st["settings"] and st["import"] is None


def test_folders_are_private(two):
    admin, user, store = two
    p1 = admin.post("/api/products", json={"name": "سیمان"}).json()["id"]
    p2 = admin.post("/api/products", json={"name": "گچ"}).json()["id"]
    fid = user.post("/api/folders", json={"name": "علاقه‌مندی‌ها"}).json()["id"]
    assert user.post("/api/folders", json={"name": "علاقه‌مندی‌ها"}).status_code == 409
    r = user.post(f"/api/folders/{fid}/products", json={"product_ids": [p1, p2, p1, "nope"]}).json()
    assert r["product_ids"] == [p1, p2]
    r = user.post(f"/api/folders/{fid}/products", json={"product_ids": [p1], "add": False}).json()
    assert r["product_ids"] == [p2]
    assert [f["name"] for f in user.get("/api/state").json()["folders"]] == ["علاقه‌مندی‌ها"]
    # nobody else sees or touches it, not even an admin
    assert admin.get("/api/state").json()["folders"] == []
    assert admin.put(f"/api/folders/{fid}", json={"name": "x"}).status_code == 404
    assert admin.delete(f"/api/folders/{fid}").status_code == 404
    # a deleted product leaves the folder
    admin.delete(f"/api/products/{p2}")
    assert store.folders[fid].product_ids == []
    assert user.put(f"/api/folders/{fid}", json={"name": "مصالح"}).json()["name"] == "مصالح"
    assert user.delete(f"/api/folders/{fid}").status_code == 200 and not store.folders


def test_user_admin(two):
    admin, user, store = two
    user.post("/api/folders", json={"name": "a"})
    assert [u["username"] for u in admin.get("/api/users").json()] == ["ali", "boss"]
    assert admin.post("/api/users", json={"username": "ali", "password": "secret9"}).status_code == 409
    assert admin.post("/api/users", json={"username": "x", "password": "123"}).status_code == 422
    # the last admin stays
    assert admin.delete("/api/users/boss").status_code == 422
    assert admin.put("/api/users/boss", json={"username": "boss", "role": "user"}).status_code == 422
    # a new password ends that person's sessions
    assert admin.put("/api/users/ali", json={"username": "ali", "password": "newpass1", "role": "user"}).status_code == 200
    assert user.get("/api/state").status_code == 401
    assert user.post("/api/login", json={"username": "ali", "password": "newpass1"}).status_code == 200
    # a password reset without a role keeps the role
    assert admin.put("/api/users/boss", json={"username": "boss", "password": "secret1"}).status_code == 200
    admin.post("/api/login", json={"username": "boss", "password": "secret1"})
    assert admin.get("/api/settings").status_code == 200
    # promoted: admin rights at once (and a fresh login, since the role changed)
    admin.put("/api/users/ali", json={"username": "ali", "role": "admin"})
    user.post("/api/login", json={"username": "ali", "password": "newpass1"})
    assert user.get("/api/settings").status_code == 200
    # deleting someone removes their folders
    assert admin.delete("/api/users/ali").status_code == 200
    assert not store.folders and user.get("/api/state").status_code == 401


def test_change_own_password(two):
    _, user, _ = two
    assert user.put("/api/me/password", json={"old": "wrong1", "new": "another1"}).status_code == 403
    assert user.put("/api/me/password", json={"old": "secret2", "new": "another1"}).status_code == 200
    assert user.get("/api/state").status_code == 200  # this browser stays in
    user.post("/api/logout")
    assert user.post("/api/login", json={"username": "ali", "password": "another1"}).status_code == 200


def test_old_single_admin_file_is_migrated(tmp_path):
    old = Auth(tmp_path / "old.json")
    # the v0.1 layout: one account at the top level
    import hashlib
    salt = b"s" * 16
    old.data.update(username="admin", salt=salt.hex(), hash=hashlib.pbkdf2_hmac("sha256", b"secret1", salt, 200_000).hex())
    old.data.pop("users", None)
    (tmp_path / "auth.json").write_text(json.dumps(old.data), encoding="utf-8")
    a = Auth(tmp_path / "auth.json")
    assert a.check("admin", "secret1") and a.role("admin") == "admin" and a.configured
    assert "hash" not in json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    assert a.verify(a.issue("admin")) == {"username": "admin", "role": "admin"}
