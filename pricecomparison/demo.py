"""Demo mode: sample products and a simulated set of shops, so the panel can
be shown without reaching any real site (python -m pricecomparison demo)."""

from __future__ import annotations

import asyncio
import random
import re
import time

from .extract import ExtractError, Result
from .scraper import FetchError
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

    async def page(self, url: str, settings: Settings) -> str:
        await asyncio.sleep(self.rnd.uniform(0.3, 0.8))
        return demo_listing(url)

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------- listings
# building materials, as three shops might name them: (names per shop style, toman)
MATERIALS = [
    (("سیمان پاکتی تیپ ۲ تهران", "خرید سیمان تیپ 2 تهران 50 کیلویی", "سیمان تیپ 2 تهران (پاکتی)"), 185_000),
    (("سیمان سفید ساوه ۴۰ کیلویی", "سیمان سفید ساوه", "سیمان سفید ساوه 40 کیلوگرم"), 265_000),
    (("میلگرد ۱۲ ذوب آهن اصفهان", "میلگرد 12 ذوب آهن اصفهان شاخه 12 متری", "میلگرد ۱۲ ذوب‌آهن"), 1_150_000),
    (("میلگرد ۱۴ ذوب آهن اصفهان", "میلگرد 14 ذوب آهن اصفهان", "میلگرد ۱۴ ذوب‌آهن"), 1_560_000),
    (("میلگرد ۱۶ ذوب آهن اصفهان", "میلگرد 16 ذوب آهن", "میلگرد ۱۶ ذوب‌آهن اصفهان"), 2_040_000),
    (("گچ سفید سمنان ۴۰ کیلویی", "گچ سفید سمنان", "گچ سمنان سفید"), 98_000),
    (("گچ خاک سمنان", "گچ خاک سمنان 40 کیلویی", "گچ خاک"), 82_000),
    (("آجر سفال ۱۰ سوراخ", "آجر سفال 10 سوراخ", "آجر سفالی ۱۰ سوراخ"), 4_300),
    (("بلوک سیمانی ۲۰", "بلوک سیمانی 20 سانتی", "بلوک سیمانی ۲۰ سانت"), 21_000),
    (("ایزوگام دلیجان شرق", "ایزوگام شرق دلیجان", "عایق ایزوگام شرق"), 1_380_000),
    (("چسب کاشی پودری ۲۰ کیلویی", "چسب کاشی پودری 20 کیلویی", "چسب کاشی پودری"), 315_000),
    (("پشم سنگ ۵ سانت", "پشم سنگ 5 سانتی رول", "پشم سنگ ۵"), 890_000),
    (("سیم مفتول ۱ میل", "مفتول آرماتوربندی 1 میل", "سیم مفتول آرماتور ۱"), 61_000),
    (("ماسه شسته (تنی)", "ماسه شسته", "ماسه شسته ساختمانی"), 690_000),
    (("لوله پلی اتیلن ۶۳", "لوله پلی‌اتیلن 63", "لوله پلی اتیلن ۶۳ میلی"), 410_000),
    (("کاشی دیواری ۳۰×۹۰ سفید", "تیرچه بتنی ۴ متری", "شن نخودی"), 520_000),
]
PER_PAGE = 6


def _shop_items(host: str) -> list[tuple[str, str, int]]:
    style = 0 if "masaleh" in host else 1 if "shahr" in host else 2
    rnd = random.Random(host)
    out = []
    for i, (names, base) in enumerate(MATERIALS):
        if rnd.random() < 0.15:
            continue  # not every shop carries everything
        price = int(base * rnd.uniform(0.92, 1.1) // 1000 * 1000) or base
        out.append((names[style], f"m{i}", price))
    return out


def demo_listing(url: str) -> str:
    """A category page for any shop: WooCommerce markup with pages /page/N/,
    or emalls-style ~id~ links with ~page~N."""
    host = url.split("/")[2].removeprefix("www.")
    items = _shop_items(host)
    emalls = "~Category~" in url
    m = re.search(r"~page~(\d+)|/page/(\d+)/", url)
    page = int(next(g for g in m.groups() if g)) if m else 1
    chunk = items[(page - 1) * PER_PAGE: page * PER_PAGE]
    if not chunk:
        raise FetchError("HTTP 404")
    fa = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
    money = lambda v: f"{v:,}".replace(",", "٬").translate(fa)  # noqa: E731
    if emalls:
        cards = "".join(f'<div class="item"><a href="/مشخصات_{slug}~id~{1000 + int(slug[1:])}" title="{n}">{n}</a>'
                        f'<span>از {money(p)} تومان</span></div>' for n, slug, p in chunk)
        return f"<html><body><h1>لیست قیمت مصالح ساختمانی</h1>{cards}</body></html>"
    cards = "".join(f'<li class="product"><a href="https://{host}/product/{slug}/" class="woocommerce-LoopProduct-link">'
                    f'<h2 class="woocommerce-loop-product__title">{n}</h2><span class="price"><span class="woocommerce-Price-amount amount">'
                    f'{money(p)} <span class="woocommerce-Price-currencySymbol">تومان</span></span></span></a></li>' for n, slug, p in chunk)
    return f'<html><body><h1>مصالح ساختمانی</h1><ul class="products">{cards}</ul></body></html>'
