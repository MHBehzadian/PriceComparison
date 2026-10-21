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
        self.pages: dict[str, str] = {}

    async def read(self, url, site, settings):
        self.calls.append(url)
        if url not in self.prices:
            raise ExtractError("قیمتی در صفحه پیدا نشد")
        p = self.prices[url]
        return Result(p, "jsonld", available=p is not None)

    async def page(self, url, settings):
        self.calls.append(url)
        if url not in self.pages:
            from pricecomparison.scraper import FetchError

            raise FetchError("HTTP 404")
        return self.pages[url]

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
        "title": "", "unit": None,
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


def woo(items, next_href=""):
    lis = "".join(
        f'<li class="product"><a href="{u}"><h2 class="woocommerce-loop-product__title">{n}</h2>'
        f'<span class="price"><span class="amount">{p} تومان</span></span></a></li>' for u, n, p in items
    )
    nxt = f'<a class="next page-numbers" href="{next_href}">بعدی</a>' if next_href else ""
    return f'<h1>مصالح ساختمانی</h1><ul class="products">{lis}</ul>{nxt}'


def test_category_import(env):
    c, shops, store = env
    a1 = "https://masaleh-tehran.ir/product-category/building-materials/"
    shops.pages[a1] = woo([("https://masaleh-tehran.ir/product/c2/", "سیمان پاکتی تیپ ۲ تهران", "185,000")], a1 + "page/2/")
    shops.pages[a1 + "page/2/"] = woo([("https://masaleh-tehran.ir/product/r12/", "میلگرد ۱۲ ذوب آهن", "1,150,000")])
    b1 = "https://shahrsakhtemani.com/shop/"
    # no next link: page 2 is guessed, page 3 does not exist
    shops.pages[b1] = woo([("https://shahrsakhtemani.com/product/cement/", "خرید سیمان تیپ 2 تهران 50 کیلویی", "180,000")])
    shops.pages[b1 + "page/2/"] = woo([("https://shahrsakhtemani.com/product/gypsum/", "گچ سفید سمنان", "95,000")])
    bad = "https://emalls.ir/x~Category~70445"

    job = c.post("/api/import", json={"urls": [a1, b1, bad], "max_pages": 10}).json()
    import time

    for _ in range(50):
        job = c.get(f"/api/import/{job['id']}").json()
        if job["status"] == "done":
            break
        time.sleep(0.05)
    src = {s["url"]: s for s in job["sources"]}
    assert (src[a1]["pages"], src[a1]["found"]) == (2, 2)
    assert (src[b1]["pages"], src[b1]["found"]) == (2, 2)
    assert src[bad]["status"] == "error"
    assert any(s.domains == ["masaleh-tehran.ir"] for s in store.sites.values())  # new shop added as a site
    groups = job["groups"]
    assert len(groups) == 3 and len(groups[0]["items"]) == 2  # the cement is on both shops
    assert c.get("/api/state").json()["import"]["status"] == "done"

    picks = [{"index": i, "name": g["name"], "urls": [x["url"] for x in g["items"]]} for i, g in enumerate(groups) if i != 2]
    picks[0]["name"] = "سیمان تیپ ۲ تهران"
    r = c.post(f"/api/import/{job['id']}/apply", json={"category": "مصالح", "groups": picks}).json()
    assert r == {"created": 2, "linked": 3}
    cement = next(p for p in store.products.values() if p.name == "سیمان تیپ ۲ تهران")
    assert cement.category == "مصالح" and len(cement.aliases) == 2
    best = store.cheapest(cement.id)
    assert best.price == 180_000 and best.method == "listing"
    assert c.post(f"/api/import/{job['id']}/apply", json={"groups": picks}).status_code == 409

    # a second import matches the existing product instead of making a new one
    shops.pages[bad] = '<a href="/m~id~5">سیمان تیپ 2 تهران پاکتی</a><span>۱۷۹٬۰۰۰ تومان</span>'
    job = c.post("/api/import", json={"urls": [bad], "max_pages": 1}).json()
    for _ in range(50):
        job = c.get(f"/api/import/{job['id']}").json()
        if job["status"] == "done":
            break
        time.sleep(0.05)
    assert job["groups"][0]["product_id"] == cement.id


def test_bulk_delete_products(env):
    c, _, store = env
    ids = [c.post("/api/products", json={"name": f"p{i}", "urls": [f"https://snappshop.ir/p/{i}"]}).json()["id"] for i in range(3)]
    r = c.post("/api/products/delete", json={"ids": ids[:2] + ["nope"]}).json()
    assert r == {"deleted": 2}
    assert list(store.products) == [ids[2]]
    assert [o.product_id for o in store.offers.values()] == [ids[2]]


def test_units_api(env):
    c, shops, store = env
    a, b = "https://masaleh-tehran.ir/product/c/", "https://shahrsakhtemani.com/product/c/"
    shops.prices = {a: 185_000, b: 3_500_000}
    pid = c.post("/api/products", json={"name": "سیمان تیپ ۲ تهران", "urls": [a, b]}).json()["id"]
    for o in store.offers_of(pid):
        c.post(f"/api/offers/{o.id}/check")
    oa = next(o for o in store.offers_of(pid) if o.url == a)
    ob = next(o for o in store.offers_of(pid) if o.url == b)
    # nothing on the pages: the name has no unit, so prices compare as they are
    assert store.compare(pid)["ref"] is None

    assert c.post("/api/units/parse", json={"text": "هر تن"}).json()["unit"]["amount"] == 1000
    assert c.put(f"/api/offers/{ob.id}/unit", json={"text": "هر تن"}).status_code == 200
    st = c.get("/api/state").json()
    offers = {o["id"]: o for o in st["offers"]}
    assert offers[oa.id]["state"] == "unknown"  # one shop has a unit now, the other must get one too
    assert c.put(f"/api/offers/{oa.id}/unit", json={"text": "کیسه ۵۰ کیلویی"}).status_code == 200
    st = c.get("/api/state").json()
    prod = next(p for p in st["products"] if p["id"] == pid)
    offers = {o["id"]: o for o in st["offers"]}
    assert prod["ref"]["dim"] == "mass"
    # 3.5M a ton is 175k a bag: the per-ton shop is the cheaper one
    assert store.cheapest(pid).id == ob.id

    assert c.put(f"/api/products/{pid}/ref", json={"dim": "mass", "amount": 1000}).status_code == 200
    offers = {o["id"]: o for o in c.get("/api/state").json()["offers"]}
    assert offers[oa.id]["norm"] == 3_700_000 and offers[ob.id]["norm"] == 3_500_000
    assert c.put(f"/api/offers/{oa.id}/unit", json={"text": "سلام"}).status_code == 422
    c.put(f"/api/offers/{oa.id}/unit", json={"text": ""})
    assert store.offers[oa.id].qty_manual is None
