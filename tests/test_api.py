from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from pricecomparison.app import create_app
from pricecomparison.extract import ExtractError, Result
from pricecomparison.scraper import digikala


class FakeShops:
    def __init__(self):
        self.prices: dict[str, int | None] = {}
        self.calls: list[str] = []

    async def read(self, url, site, settings):
        self.calls.append(url)
        if url not in self.prices:
            raise ExtractError("قیمتی در صفحه پیدا نشد")
        p = self.prices[url]
        return Result(p, "jsonld", available=p is not None)

    async def close(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_NO_MONITOR", "1")
    shops = FakeShops()
    app = create_app(tmp_path, fetcher=shops)
    with TestClient(app) as c:
        assert c.post("/api/setup", json={"username": "admin", "password": "secret1"}).status_code == 200
        yield c, shops, app.state.store


def test_login_required(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_NO_MONITOR", "1")
    with TestClient(create_app(tmp_path, fetcher=FakeShops())) as c:
        assert c.get("/api/state").status_code == 401
        assert c.get("/api/session").json()["configured"] is False


def test_default_sites_seeded(env):
    c, _, _ = env
    names = [s["name"] for s in c.get("/api/state").json()["sites"]]
    assert names[0] == "دیجی‌کالا" and "تکنولایف" in names


def test_product_flow_and_cheapest(env):
    c, shops, store = env
    dk = "https://www.digikala.com/product/dkp-123/x/"
    tl = "https://www.technolife.com/product-456/x"
    shop = "https://myshop.ir/product/x"
    shops.prices = {dk: 31_000_000, tl: 30_500_000, shop: 32_000_000}
    r = c.post("/api/products", json={"name": "Galaxy A56", "urls": [dk, tl, shop]}).json()
    pid = r["id"]
    assert r["skipped"] == []
    # unknown domain got its own site
    assert any(s.domains == ["myshop.ir"] for s in store.sites.values())
    for o in store.offers_of(pid):
        c.post(f"/api/offers/{o.id}/check")
    st = c.get("/api/state").json()
    prices = sorted(o["price"] for o in st["offers"])
    assert prices == [30_500_000, 31_000_000, 32_000_000]
    best = store.cheapest(pid)
    assert store.sites[best.site_id].name == "تکنولایف"

    # digikala drops below technolife: an event says who is cheapest now
    shops.prices[dk] = 29_900_000
    o_dk = next(o for o in store.offers_of(pid) if o.url == dk)
    c.post(f"/api/offers/{o_dk.id}/check")
    msgs = [e["message"] for e in c.get("/api/state").json()["events"]]
    assert any("کاهش" in m for m in msgs)
    assert any("ارزان‌ترین فروشنده" in m and "دیجی‌کالا" in m for m in msgs)
    assert [h[1] for h in o_dk.history] == [31_000_000, 29_900_000]


def test_out_of_stock_and_errors(env):
    c, shops, store = env
    url = "https://snappshop.ir/product/x"
    pid = c.post("/api/products", json={"name": "x"}).json()["id"]
    oid = c.post("/api/offers", json={"product_id": pid, "url": url}).json()["id"]
    shops.prices[url] = None
    assert c.post(f"/api/offers/{oid}/check").json()["status"] == "out"
    del shops.prices[url]
    c.post(f"/api/offers/{oid}/check")
    o = c.post(f"/api/offers/{oid}/check").json()
    assert o["status"] == "error" and o["fail_count"] == 2
    assert store.cheapest(pid) is None


def test_unknown_domain_needs_a_site(env):
    c, _, _ = env
    pid = c.post("/api/products", json={"name": "x"}).json()["id"]
    r = c.post("/api/offers", json={"product_id": pid, "url": "https://nowhere.ir/p/1"})
    assert r.status_code == 422
    assert c.post("/api/offers", json={"product_id": pid, "url": "https://nowhere.ir/p/1", "create_site": True}).status_code == 200


def test_site_crud(env):
    c, _, store = env
    r = c.post("/api/sites", json={"name": "فروشگاه", "domains": ["https://www.shop.ir/", "shop2.ir"], "selector": ".price"})
    assert r.status_code == 200 and r.json()["domains"] == ["shop.ir", "shop2.ir"]
    assert c.post("/api/sites", json={"name": "dup", "domains": ["shop.ir"]}).status_code == 409
    sid = r.json()["id"]
    pid = c.post("/api/products", json={"name": "x", "urls": ["https://shop.ir/p/1"]}).json()["id"]
    assert len(store.offers_of(pid)) == 1
    c.delete(f"/api/sites/{sid}")
    assert store.offers_of(pid) == []


def test_test_endpoint(env):
    c, shops, _ = env
    shops.prices["https://basalam.com/p/1"] = 450_000
    assert c.post("/api/test", json={"url": "https://basalam.com/p/1"}).json() == {
        "ok": True, "price": 450_000, "old_price": None, "available": True, "method": "jsonld", "guess": False,
    }
    assert c.post("/api/test", json={"url": "https://basalam.com/p/2"}).json()["ok"] is False


async def test_digikala_adapter():
    def handler(request: httpx.Request):
        assert request.url.path == "/v2/product/16210441/"
        return httpx.Response(200, json={"data": {"product": {
            "status": "marketable",
            "default_variant": {"price": {"selling_price": 315_000_000, "rrp_price": 350_000_000}},
        }}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        r = await digikala(client, "https://www.digikala.com/product/dkp-16210441/galaxy-a56/")
    assert (r.price, r.old_price, r.method) == (31_500_000, 35_000_000, "api")


async def test_digikala_out_of_stock():
    def handler(request):
        return httpx.Response(200, json={"data": {"product": {"status": "out_of_stock", "default_variant": []}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        r = await digikala(client, "https://www.digikala.com/product/dkp-1/")
    assert r.available is False
