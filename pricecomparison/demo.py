"""Demo mode: sample products and a simulated set of shops, so the panel can
be shown without reaching any real site (python -m pricecomparison demo)."""

from __future__ import annotations

import asyncio
import random
import time

from .extract import ExtractError, Result
from .models import O_OK, O_OUT, Offer, Product, Settings, Site
from .sites import seed_sites

PRODUCTS = [
    ("گوشی سامسونگ Galaxy A56 حافظه ۲۵۶ گیگ", "موبایل", 31_500_000, "samsung-galaxy-a56-256gb"),
    ("هدفون سونی WH-1000XM5", "صوتی", 24_800_000, "sony-wh-1000xm5"),
    ("لپ‌تاپ ایسوس Vivobook 15 X1504VA", "لپ‌تاپ", 42_900_000, "asus-vivobook-15-x1504va"),
    ("اسپرسوساز دلونگی EC685", "لوازم خانگی", 18_700_000, "delonghi-ec685"),
    ("کنسول بازی PS5 Slim دیجیتال", "گیمینگ", 38_400_000, "sony-ps5-slim-digital"),
]
SHOPS = ["digikala.com", "torob.com", "technolife.com", "snappshop.ir", "basalam.com", "meghdadit.com"]


def _url(domain: str, slug: str, n: int) -> str:
    if domain == "digikala.com":
        return f"https://www.digikala.com/product/dkp-{17000000 + n}/{slug}/"
    return f"https://{domain}/product/{slug}"


def seed(store) -> None:
    if store.products:
        return
    seed_sites(store)
    by_domain = {d: s for s in store.sites.values() for d in s.domains}
    rnd = random.Random(7)
    t0 = time.time() - 30 * 86400
    store.settings.interval_min = 5
    for i, (name, cat, base, slug) in enumerate(PRODUCTS):
        p = Product(name=name, category=cat, target_price=int(base * 0.93 // 100_000 * 100_000))
        p.created = t0 + i
        store.products[p.id] = p
        shops = rnd.sample(SHOPS, k=rnd.randint(4, 6))
        for j, dom in enumerate(shops):
            o = Offer(product_id=p.id, site_id=by_domain[dom].id, url=_url(dom, slug, i * 10 + j))
            price = base * rnd.uniform(0.94, 1.1)
            ts = t0
            while ts < time.time():
                if rnd.random() < 0.04:
                    o.record(ts, None)
                else:
                    price *= rnd.uniform(0.985, 1.022)
                    o.record(ts, int(price // 10_000 * 10_000))
                ts += rnd.uniform(0.5, 2.5) * 86400
            o.price = o.history[-1][1] if o.history and o.history[-1][1] else int(price // 10_000 * 10_000)
            o.record(time.time(), o.price)
            o.status, o.method = O_OK, "api" if dom == "digikala.com" else rnd.choice(["jsonld", "jsonld", "nextdata", "meta"])
            o.last_check = o.last_ok = time.time() - rnd.uniform(0, 240)
            store.offers[o.id] = o
    store.save()


class SimShops:
    """Moves each price a little on every read; now and then a shop is out of
    stock or does not answer."""

    def __init__(self, store):
        self.store = store
        self.rnd = random.Random()

    async def read(self, url: str, site: Site, settings: Settings) -> Result:
        await asyncio.sleep(self.rnd.uniform(0.2, 1.2))
        o: Offer | None = next((x for x in self.store.offers.values() if x.url == url), None)
        if self.rnd.random() < 0.05:
            raise ExtractError("قیمتی در صفحه پیدا نشد؛ برای این سایت یک CSS selector تنظیم کنید")
        if self.rnd.random() < 0.04:
            return Result(None, "jsonld", available=False)
        known = [h[1] for h in o.history if h[1]] if o else []
        last = (o.price if o and o.price else None) or (known[-1] if known else 20_000_000)
        move = self.rnd.choice([1, 1, 1, 1, self.rnd.uniform(0.97, 1.03)])
        method = o.method if o and o.status in (O_OK, O_OUT) and o.method else "jsonld"
        return Result(int(last * move // 10_000 * 10_000), method, True)

    async def close(self) -> None:
        pass
