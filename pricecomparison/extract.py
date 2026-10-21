"""Read a product's price out of a shop page, without any shop-specific code.

Readers run in order and the first one that finds a price wins:

  selector   the CSS selector set for the site in the panel (an override)
  jsonld     schema.org Product/Offer in <script type="application/ld+json">
             (most shops add it for Google; WooCommerce + Yoast/RankMath do)
  meta       <meta property="product:price:amount"> and similar
  microdata  itemprop="price"
  woo        the price box of a WooCommerce product page
  nextdata   price fields in a Next.js/Nuxt page's embedded JSON
  text       the first "<number> تومان" after the product title (a guess,
             flagged as such in the panel)

Prices are returned in toman.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterator

from bs4 import BeautifulSoup

from . import units
from .models import AUTO, RIAL, TOMAN

MIN_TOMAN = 1_000  # anything lower is a percentage, a count or a rating
MAX_TOMAN = 100_000_000_000

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_NUM = re.compile(r"\d[\d,٬،.\s‌٫]*\d|\d")
_OUT_WORDS = ("ناموجود", "اتمام موجودی", "موجود شد خبرم کن", "موجود شد به من اطلاع", "در انبار موجود نیست", "out of stock")
_PRICE_KEYS = (
    "selling_price", "sellingPrice", "final_price", "finalPrice", "discounted_price", "discountedPrice",
    "sale_price", "salePrice", "special_price", "specialPrice", "price_after_discount",
)
_OLD_KEYS = ("rrp_price", "rrpPrice", "original_price", "originalPrice", "regular_price", "regularPrice", "list_price")


class ExtractError(Exception):
    pass


@dataclass
class Found:
    """A price as read from the page, before converting to toman."""

    value: float | None
    method: str
    unit: str | None = None  # what the page itself says, if anything
    available: bool = True
    old: float | None = None
    near: str = ""  # the text around the price on the page, where shops write "/ کیلوگرم"
    spec: units.Qty | None = None  # unit from structured data (schema.org unitCode)
    title: str = ""


@dataclass
class Result:
    price: int | None  # toman; None when out of stock
    method: str
    available: bool = True
    old_price: int | None = None
    guess: bool = False  # found by the "text" reader: worth setting a selector
    title: str = ""  # the product's name on the shop
    qty: dict | None = None  # the quantity the price is for (units.Qty), when the page says


# ---------------------------------------------------------------- numbers
def to_digits(s: str) -> str:
    return s.translate(_DIGITS)


def parse_number(text: Any) -> float | None:
    """A number from structured data: 12500000, "12500000.00", "۱۲٬۵۰۰٬۰۰۰"."""
    if isinstance(text, bool) or text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    s = to_digits(str(text)).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        v = float(s)
        return v if v > 0 else None
    return parse_price_text(s)


def numbers_in(text: str) -> list[int]:
    """Every whole number written in shop text: "۱۲٬۵۰۰٬۰۰۰ تومان" -> [12500000]."""
    out = []
    for m in _NUM.finditer(to_digits(text)):
        digits = re.sub(r"\D", "", m.group())
        if digits and len(digits) <= 13:
            out.append(int(digits))
    return out


def parse_price_text(text: str) -> float | None:
    """The selling price in a block of text. When the block shows the old
    price next to the discounted one, the lower of the two is the price."""
    vals = [v for v in numbers_in(text) if v >= MIN_TOMAN]
    return float(min(vals)) if vals else None


def unit_in(text: str) -> str | None:
    t = text.lower()
    if "ریال" in t or "rial" in t:
        return RIAL
    if "تومان" in t or "تومن" in t or "toman" in t:
        return TOMAN
    return None


def currency_unit(code: Any) -> str | None:
    c = str(code or "").strip().upper()
    if c in ("IRR", "RIAL", "ريال", "ریال"):
        return RIAL
    if c in ("IRT", "TOMAN", "TOM", "تومان"):
        return TOMAN
    return None


def to_toman(value: float, found_unit: str | None, site_unit: str) -> int:
    unit = site_unit if site_unit != AUTO else (found_unit or TOMAN)
    return int(round(value / 10 if unit == RIAL else value))


def _out_of_stock_text(s: str) -> bool:
    s = s.lower()
    return "outofstock" in s.replace(" ", "").replace("_", "") or "soldout" in s.replace(" ", "")


# ---------------------------------------------------------------- readers
def read_selector(soup: BeautifulSoup, selector: str, out_selector: str = "") -> Found | None:
    if out_selector:
        try:
            if soup.select_one(out_selector):
                return Found(None, "selector", available=False)
        except Exception as e:  # soupsieve raises its own errors on bad selectors
            raise ExtractError(f"selector ناموجودی نامعتبر است: {e}") from None
    if not selector:
        return None
    try:
        el = soup.select_one(selector)
    except Exception as e:
        raise ExtractError(f"selector نامعتبر است: {e}") from None
    if el is None:
        return None
    text = el.get("content") or el.get("data-price") or el.get_text(" ", strip=True)
    near = (el.parent or el).get_text(" ", strip=True)[:300]
    return Found(parse_price_text(str(text)), "selector", unit_in(el.get_text(" ", strip=True)), near=near)


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    return {str(x).split("/")[-1] for x in (t if isinstance(t, list) else [t])}


def _ld_offers(offers: Any) -> list[dict]:
    if isinstance(offers, dict):
        inner = offers.get("offers")
        return [offers] + (_ld_offers(inner) if inner else [])
    if isinstance(offers, list):
        return [o for x in offers for o in _ld_offers(x)]
    return []


def _ld_unit(offer: dict, spec: dict) -> units.Qty | None:
    """unitCode / unitText / referenceQuantity of an Offer's priceSpecification."""
    ref = spec.get("referenceQuantity") or offer.get("referenceQuantity") or {}
    ref = ref if isinstance(ref, dict) else {}
    code = spec.get("unitCode") or ref.get("unitCode") or offer.get("unitCode")
    q = units.from_code(code, ref.get("value"))
    text = spec.get("unitText") or ref.get("unitText") or offer.get("unitText")
    if q is None and isinstance(text, str) and text.strip():
        q = units.parse_per("هر " + text, "", units.SPEC)
    return q


def read_jsonld(soup: BeautifulSoup) -> Found | None:
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            data = json.loads(tag.string or tag.get_text() or "", strict=False)
        except ValueError:
            continue
        for node in _walk(data):
            if "Product" not in _types(node) and "ProductGroup" not in _types(node):
                continue
            offers = _ld_offers(node.get("offers"))
            prices, out_seen = [], False
            for o in offers:
                spec = o.get("priceSpecification")
                spec = spec[0] if isinstance(spec, list) and spec else spec if isinstance(spec, dict) else {}
                v = parse_number(o.get("price") or spec.get("price") or o.get("lowPrice"))
                avail = str(o.get("availability", ""))
                if _out_of_stock_text(avail):
                    out_seen = True
                    continue
                if v:
                    unit = currency_unit(o.get("priceCurrency") or spec.get("priceCurrency"))
                    old = parse_number(o.get("highPrice")) if o.get("lowPrice") else None
                    prices.append((v, unit, old, _ld_unit(o, spec)))
            if prices:
                v, unit, old, qty = min(prices, key=lambda p: p[0])
                name = node.get("name") if isinstance(node.get("name"), str) else ""
                return Found(v, "jsonld", unit, old=old if old and old > v else None, spec=qty, title=name)
            if out_seen:
                return Found(None, "jsonld", available=False)
    return None


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for n in names:
        el = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if el and el.get("content"):
            return str(el["content"])
    return ""


def read_meta(soup: BeautifulSoup) -> Found | None:
    avail = _meta(soup, "product:availability", "og:availability")
    if avail and ("out" in avail.lower() or "oos" == avail.lower()):
        return Found(None, "meta", available=False)
    v = parse_number(_meta(soup, "product:price:amount", "og:price:amount", "product:sale_price:amount"))
    if not v:
        return None
    unit = currency_unit(_meta(soup, "product:price:currency", "og:price:currency"))
    return Found(v, "meta", unit)


def read_microdata(soup: BeautifulSoup) -> Found | None:
    scope = soup.find(attrs={"itemtype": re.compile(r"schema\.org/Product$", re.I)}) or soup
    el = scope.find(attrs={"itemprop": "price"})
    if el is None:
        return None
    av = scope.find(attrs={"itemprop": "availability"})
    if av is not None and _out_of_stock_text(str(av.get("href") or av.get("content") or "")):
        return Found(None, "microdata", available=False)
    v = parse_number(el.get("content") or el.get_text(" ", strip=True))
    if not v:
        return None
    cur = scope.find(attrs={"itemprop": "priceCurrency"})
    unit = currency_unit(cur.get("content") or cur.get_text(strip=True)) if cur else None
    near = (el.parent or el).get_text(" ", strip=True)[:300]
    return Found(v, "microdata", unit or unit_in(el.get_text(" ", strip=True)), near=near)


def read_woo(soup: BeautifulSoup) -> Found | None:
    summary = soup.select_one(".product .summary, .product .entry-summary, .single-product .summary")
    if summary is None:
        return None
    if summary.select_one(".stock.out-of-stock"):
        return Found(None, "woo", available=False)
    box = summary.select_one(".price")
    if box is None:
        return None
    amount = box.select_one("ins .amount") or box.select_one(".amount")
    old_el = box.select_one("del .amount")
    if amount is None:
        return None
    sym = amount.select_one(".woocommerce-Price-currencySymbol")
    unit = unit_in(sym.get_text() if sym else amount.get_text())
    v = parse_price_text(amount.get_text(" ", strip=True))
    old = parse_price_text(old_el.get_text(" ", strip=True)) if old_el else None
    near = " ".join([box.get_text(" ", strip=True)] + [
        sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib) for sib in list(box.next_siblings)[:3]
    ])[:300]
    return Found(v, "woo", unit, old=old, near=near) if v else None


def read_nextdata(soup: BeautifulSoup) -> Found | None:
    tag = soup.find("script", id="__NEXT_DATA__") or soup.find("script", id="__NUXT_DATA__")
    if tag is None:
        return None
    try:
        data = json.loads(tag.string or "")
    except ValueError:
        return None
    for node in _walk(data):
        for k in _PRICE_KEYS:
            v = parse_number(node.get(k)) if k in node and not isinstance(node.get(k), (dict, list)) else None
            if v and v >= MIN_TOMAN:
                old = next((parse_number(node[o]) for o in _OLD_KEYS if o in node and parse_number(node[o])), None)
                return Found(v, "nextdata", old=old if old and old > v else None)
    return None


def read_text(soup: BeautifulSoup) -> Found | None:
    """Last resort: the first price written after the product title."""
    h1 = soup.find("h1")
    pattern = re.compile(r"(\d[\d,٬،.\s]*\d)\s*(تومان|تومن|ریال)")
    texts = h1.find_all_next(string=True) if h1 else soup.find_all(string=True)
    buf = ""
    for i, s in enumerate(texts):
        if i > 400:
            break
        if s.parent and s.parent.name in ("script", "style", "del", "s", "strike"):
            continue
        buf += " " + to_digits(str(s))
        m = pattern.search(buf)
        if m:
            v = parse_price_text(m.group(1))
            if v:
                tail = " ".join(to_digits(str(x)) for x in texts[i + 1:i + 4])
                near = (buf[max(0, m.start() - 60):] + " " + tail)[:300]
                return Found(v, "text", RIAL if m.group(2) == "ریال" else TOMAN, near=near)
            buf = buf[m.end():]
    return None


def page_says_out_of_stock(soup: BeautifulSoup) -> bool:
    main = soup.find("main") or soup.body or soup
    head = main.get_text(" ", strip=True)[:4000].lower()
    return any(w in head for w in _OUT_WORDS)


GENERIC = (read_jsonld, read_meta, read_microdata, read_woo, read_nextdata)


def near_price(soup: BeautifulSoup, value: float) -> str:
    """The text around where the page shows this price, for readers that
    took it from data rather than from the visible page."""
    want = {int(value), int(value) // 10, int(value) * 10}
    for s in soup.find_all(string=True):
        if s.parent is None or s.parent.name in ("script", "style", "del", "s", "strike", "title"):
            continue
        if want & set(numbers_in(str(s))):
            block = s.parent
            for _ in range(2):
                if block.parent is not None and len(block.parent.get_text(" ", strip=True)) < 160:
                    block = block.parent
            return block.get_text(" ", strip=True)[:300]
    return ""


_ATTR_LINE = re.compile(r"(واحد(?: فروش| اندازه ?گیری)?|وزن|حجم|بسته ?بندی)\s*[:：]\s*([^\n|،,؛]{1,30})")


def page_attrs(soup: BeautifulSoup) -> dict[str, str]:
    """The product's specs table: WooCommerce attributes, any two-cell
    rows, definition lists and "واحد: کیلوگرم" lines."""
    out: dict[str, str] = {}
    for tr in soup.select("table.shop_attributes tr, .woocommerce-product-attributes tr, table tr")[:80]:
        cells = tr.find_all(["th", "td"])
        if len(cells) == 2:
            k, v = cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True)
            if 0 < len(k) <= 40 and 0 < len(v) <= 80:
                out.setdefault(k, v)
    for dt in soup.find_all("dt")[:40]:
        dd = dt.find_next_sibling("dd")
        if dd:
            out.setdefault(dt.get_text(" ", strip=True)[:40], dd.get_text(" ", strip=True)[:80])
    main = soup.find("main") or soup.body or soup
    for m in _ATTR_LINE.finditer(to_digits(main.get_text("\n", strip=True))[:20000]):
        out.setdefault(m.group(1), m.group(2).strip())
    return out


def page_title(soup: BeautifulSoup, found: Found) -> str:
    h1 = soup.find("h1")
    og = soup.find("meta", attrs={"property": "og:title"})
    for t in (found.title, h1.get_text(" ", strip=True) if h1 else "", og.get("content", "") if og else ""):
        if t and t.strip():
            return re.sub(r"\s+", " ", t).strip()[:200]
    return ""


def extract(html: str, site_unit: str = AUTO, selector: str = "", out_selector: str = "") -> Result:
    soup = BeautifulSoup(html, "html.parser")
    found = read_selector(soup, selector, out_selector)
    if selector and found is None:
        # an explicit selector that matches nothing usually means the shop
        # changed its page; say so instead of quietly guessing elsewhere
        raise ExtractError(f"selector «{selector}» در صفحه پیدا نشد؛ احتمالا قالب سایت عوض شده")
    for reader in GENERIC:
        if found is not None:
            break
        found = reader(soup)
    if found is None:
        if page_says_out_of_stock(soup):
            return Result(None, "text", available=False)
        found = read_text(soup)
    if found is None:
        raise ExtractError("قیمتی در صفحه پیدا نشد؛ برای این سایت یک CSS selector تنظیم کنید")
    if not found.available or found.value is None:
        return Result(None, found.method, available=False, title=page_title(soup, found))
    price = to_toman(found.value, found.unit, site_unit)
    if not MIN_TOMAN <= price <= MAX_TOMAN:
        raise ExtractError(f"عدد خوانده‌شده ({price:,}) قیمت معقولی نیست")
    old = to_toman(found.old, found.unit, site_unit) if found.old else None
    title = page_title(soup, found)
    near = found.near or near_price(soup, found.value)
    qty = units.resolve(title, near, page_attrs(soup), found.spec)
    return Result(price, found.method, True, old if old and old > price else None, guess=found.method == "text",
                  title=title, qty=qty.to_dict() if qty else None)
