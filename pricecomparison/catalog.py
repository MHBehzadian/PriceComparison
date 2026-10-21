"""Read a shop's category (listing) pages and group the same product across
shops by name, so a whole category can be added to the panel at once.

Listing readers, first match wins:
  selector  the site's own "product link" CSS selector, if set in the panel
  woo       WooCommerce loops (ul.products li.product and common themes)
  itemlist  schema.org ItemList in JSON-LD
  links     links whose address looks like a product page (/product/,
            ~id~123 on emalls, dkp-123, ...), with the price from the card
            around each link

Pages are followed through rel=next / WooCommerce pagination, or by
guessing the next page address when the page has no such link.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from . import units
from .extract import parse_number, parse_price_text, to_toman, unit_in

PRODUCT_PATH = re.compile(r"/product/|/products?/[^/]+|~id~\d+|/dkp-\d+|/p/\d+|/item/|/shop/[^/]+/[^/]+", re.I)
NOT_PRODUCT = re.compile(r"/product-category/|/product-tag/|/category/|/tag/|/cart|/checkout|/my-account|add-to-cart=|[?&](orderby|filter_|min_price)", re.I)
WOO_ITEMS = (
    "ul.products li.product", ".products .product", ".product-grid-item", ".product-small", ".wd-product",
)
WOO_LINK = (
    ".woocommerce-loop-product__title a", ".wd-entities-title a", ".product-title a", ".name a",
    "h2 a", "h3 a", "a.woocommerce-LoopProduct-link", "a.woocommerce-loop-product__link", "a[href]",
)
WOO_TITLE = (".woocommerce-loop-product__title", ".wd-entities-title", ".product-title", ".name", "h2", "h3")
MAX_NAME = 200


@dataclass
class Item:
    name: str
    url: str
    price: int | None = None  # toman, as shown on the listing
    near: str = ""  # the card's text around the price, where "/ کیلوگرم" would be


@dataclass
class Listing:
    items: list[Item] = field(default_factory=list)
    next_url: str | None = None
    method: str = ""
    title: str = ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:MAX_NAME]


def _abs(base: str, href: str) -> str:
    return urljoin(base, href.strip()).split("#", 1)[0]


def _same_site(a: str, b: str) -> bool:
    ha = (urlsplit(a).hostname or "").removeprefix("www.")
    hb = (urlsplit(b).hostname or "").removeprefix("www.")
    return ha == hb


def _text_without_old(el: Tag) -> str:
    return " ".join(t for t in el.find_all(string=True) if not t.find_parent(["del", "s", "strike"]))


def _card_price(card: Tag, site_unit: str) -> int | None:
    box = card.select_one(".price")
    if box is not None:
        el = box.select_one("ins .amount") or box.select_one(".amount") or box
        text = _text_without_old(el)
    else:
        text = _text_without_old(card)
        if not re.search("تومان|تومن|ریال", text):
            return None
    v = parse_price_text(text)
    return to_toman(v, unit_in(text), site_unit) if v else None


def _add(out: dict[str, Item], base: str, href: str, name: str, price: int | None, near: str = "") -> None:
    if not href:
        return
    url = _abs(base, href)
    if not url.startswith("http") or not _same_site(url, base) or NOT_PRODUCT.search(url):
        return
    name = _clean(name)
    cur = out.get(url)
    if cur is None:
        if name:
            out[url] = Item(name, url, price, near[:300])
    else:
        if len(name) > len(cur.name):
            cur.name = name
        cur.price = cur.price or price
        cur.near = cur.near or near[:300]


def read_selector(soup: BeautifulSoup, base: str, selector: str, site_unit: str) -> dict[str, Item]:
    out: dict[str, Item] = {}
    for a in soup.select(selector):
        link = a if a.name == "a" else a.find("a", href=True)
        if link is None:
            continue
        card = a.find_parent(["li", "article"]) or a.parent
        _add(out, base, link.get("href", ""), link.get("title") or link.get_text(" ", strip=True), _card_price(card, site_unit) if card else None,
             _text_without_old(card) if card else "")
    return out


def read_woo(soup: BeautifulSoup, base: str, site_unit: str) -> dict[str, Item]:
    out: dict[str, Item] = {}
    for sel in WOO_ITEMS:
        cards = soup.select(sel)
        if not cards:
            continue
        for card in cards:
            link = next((card.select_one(s) for s in WOO_LINK if card.select_one(s)), None)
            if link is None:
                continue
            title = next((card.select_one(s) for s in WOO_TITLE if card.select_one(s)), None)
            name = title.get_text(" ", strip=True) if title else link.get("title") or link.get_text(" ", strip=True)
            _add(out, base, link.get("href", ""), name, _card_price(card, site_unit), _text_without_old(card))
        if out:
            return out
    return out


def read_itemlist(soup: BeautifulSoup, base: str) -> dict[str, Item]:
    out: dict[str, Item] = {}
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            data = json.loads(tag.string or "", strict=False)
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if node.get("@type") == "ListItem":
                    inner = node.get("item") if isinstance(node.get("item"), dict) else {}
                    url = node.get("url") or inner.get("url") or (node.get("item") if isinstance(node.get("item"), str) else "")
                    name = node.get("name") or inner.get("name") or ""
                    offers = inner.get("offers") or {}
                    price = parse_number(offers.get("price") or offers.get("lowPrice")) if isinstance(offers, dict) else None
                    _add(out, base, url, name, int(price) if price else None)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return out


def read_links(soup: BeautifulSoup, base: str, site_unit: str) -> dict[str, Item]:
    out: dict[str, Item] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not PRODUCT_PATH.search(unquote(href)):
            continue
        img = a.find("img")
        name = a.get("title") or a.get_text(" ", strip=True) or (img.get("alt", "") if img else "")
        # the product card: the nearest ancestor that shows a price and
        # does not also hold other products
        price, node, near = None, a, ""
        for _ in range(5):
            node = node.parent
            if node is None or node.name in ("body", "html"):
                break
            others = {_abs(base, x["href"]) for x in node.find_all("a", href=True) if PRODUCT_PATH.search(unquote(x["href"]))}
            if len(others) > 1:
                break
            text = node.get_text(" ", strip=True)
            if "تومان" in text or "ریال" in text or "تومن" in text:
                v = parse_price_text(text)
                price = to_toman(v, unit_in(text), site_unit) if v else None
                near = text
                break
        _add(out, base, href, name if len(_clean(name)) >= 3 else "", price, near)
    return out


def next_page(soup: BeautifulSoup, base: str, selector: str = "") -> str | None:
    el = None
    if selector:
        el = soup.select_one(selector)
    el = el or soup.find("link", rel="next") or soup.select_one("a.next.page-numbers, a.next, a[rel=next], .pagination-next a, li.next a")
    href = el.get("href") if el else None
    return _abs(base, href) if href else None


def guess_next(url: str, page_no: int) -> str | None:
    """The address of page `page_no` for shops whose pages carry no next
    link: WooCommerce /page/N/ and emalls ~page~N."""
    if "~Category~" in url or "~category~" in url:
        base = re.sub(r"~page~\d+", "", url)
        return f"{base}~page~{page_no}"
    parts = urlsplit(url)
    path = re.sub(r"/page/\d+/?$", "/", parts.path if parts.path.endswith("/") else parts.path + "/")
    if "/product-category/" in path or path.rstrip("/").endswith("/shop") or "/shop/" in path:
        return parts._replace(path=f"{path}page/{page_no}/").geturl()
    return None


def parse_listing(html: str, base: str, site_unit: str = "auto", selector: str = "", next_selector: str = "") -> Listing:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    title = _clean(h1.get_text(" ", strip=True)) if h1 else _clean(soup.title.get_text() if soup.title else "")
    for method, fn in (
        ("selector", lambda: read_selector(soup, base, selector, site_unit) if selector else {}),
        ("woo", lambda: read_woo(soup, base, site_unit)),
        ("itemlist", lambda: read_itemlist(soup, base)),
        ("links", lambda: read_links(soup, base, site_unit)),
    ):
        found = fn()
        if found:
            return Listing(list(found.values()), next_page(soup, base, next_selector), method, title)
    return Listing([], next_page(soup, base, next_selector), "", title)


# ------------------------------------------------------------- name matching
_AR = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "أ": "ا", "إ": "ا", "آ": "ا", "ؤ": "و", "‌": " ", "ـ": ""})
_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
STOP = {
    "خرید", "قیمت", "فروش", "مشخصات", "و", "با", "از", "در", "به", "برای", "مدل", "برند", "عمده", "ارزان", "اصل", "اصلی",
    "کیفیت", "عالی", "نوع", "انواع", "محصول", "جدید", "امروز", "روز", "ارسال", "فوری",
}
# units say how a product is sold, not which product it is: left out of the
# score, so "50 کیلویی" on one shop and nothing on another still match
UNITS = {"کیلویی", "کیلو", "کیلوگرم", "kg", "عدد", "عددی", "شاخه", "شاخه ای", "متری", "متر", "تنی", "تن", "بسته", "کارتن", "گرم", "لیتری", "لیتر"}
# packaging that is a different product with a different price
PACKS = {"پاکتی": "bag", "پاکت": "bag", "کیسه": "bag", "کیسه ای": "bag", "فله": "bulk", "فله ای": "bulk", "جامبو": "jumbo"}


def tokens(name: str) -> list[str]:
    s = name.translate(_AR).translate(_FA_DIGITS).lower()
    s = re.sub(r"[ً-ٰٟ]", "", s)  # harakat
    s = re.sub(r"(\d)([^\d\s.])", r"\1 \2", s)
    s = re.sub(r"([^\d\s.])(\d)", r"\1 \2", s)
    s = re.sub(r"[^\w.\s]|_", " ", s)
    return [t.strip(".") for t in s.split() if t.strip(".") and t not in STOP]


def similarity(a: str, b: str) -> float:
    # package sizes do not make a different product: "گچ ۴۰ کیلویی" and
    # "گچ ۲۵ کیلویی" are compared per kilo once grouped
    ta, tb = set(tokens(units.strip_sizes(a))) - UNITS, set(tokens(units.strip_sizes(b))) - UNITS
    pa, pb = {PACKS[t] for t in ta if t in PACKS}, {PACKS[t] for t in tb if t in PACKS}
    if pa and pb and pa != pb:
        return 0.0  # bagged and bulk cement are different products
    ta, tb = ta - PACKS.keys(), tb - PACKS.keys()
    if not ta or not tb:
        return 0.0
    na, nb = {t for t in ta if t.replace(".", "").isdigit()}, {t for t in tb if t.replace(".", "").isdigit()}
    # sizes and grades decide: rebar 12 is not rebar 14, cement type 2 is not type 5
    if na and nb and not (na <= nb or nb <= na):
        return 0.0
    common = ta & tb
    if len(common) < min(2, len(ta), len(tb)):
        return 0.0
    jaccard = len(common) / len(ta | tb)
    overlap = len(common) / min(len(ta), len(tb))
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return round(0.4 * jaccard + 0.4 * overlap + 0.2 * seq, 3)


MATCH = 0.72
_SALES = re.compile(r"^\s*((خرید|قیمت|فروش|مشخصات|لیست قیمت)\s*(و\s*)?)+", re.U)


def clean_name(name: str) -> str:
    """ "خرید و قیمت سیمان ..." -> "سیمان ..." """
    return _clean(_SALES.sub("", name)) or _clean(name)


def best_name(names: list[str]) -> str:
    """The name for a new product: the one closest to all the others, the
    shorter one on a tie."""
    names = [clean_name(n) for n in names]
    if len(names) == 1:
        return names[0]
    return max(names, key=lambda n: (sum(similarity(n, o) for o in names if o is not n), -len(n)))


@dataclass
class Candidate:
    """One product found on a listing page."""

    name: str
    url: str
    site_id: str
    price: int | None = None
    qty: dict | None = None  # units.Qty the listed price is for
    norm: int | None = None  # the price in the group's unit


@dataclass
class Group:
    """Listing items that look like one product, one per site at most."""

    name: str
    items: list[Candidate] = field(default_factory=list)
    product_id: str | None = None  # an existing product they match
    ref: dict | None = None  # units.Qty the group's prices are compared in
    score: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


def group_items(items: list[Candidate], products: list[tuple[str, list[str]]], taken: set[tuple[str, str]]) -> list[Group]:
    """Group `items` by name. `products` is [(id, [name, aliases...])] of the
    products already in the panel; `taken` holds (product_id, site_id) pairs
    that already have a link, so a match never adds a second link from the
    same site to a product."""
    groups: list[Group] = []
    by_product: dict[str, Group] = {}
    names_of = dict(products)
    for it in sorted(items, key=lambda x: (x.site_id, x.name)):
        best, best_score = None, 0.0
        for pid, names in products:
            if (pid, it.site_id) in taken:
                continue
            sc = max(similarity(it.name, n) for n in names)
            if sc > best_score:
                best, best_score = pid, sc
        if best and best_score >= MATCH:
            g = by_product.get(best)
            if g is None:
                g = by_product[best] = Group(name=names_of[best][0], product_id=best, score=best_score)
                groups.append(g)
            if all(c.site_id != it.site_id for c in g.items):
                g.items.append(it)
                taken.add((best, it.site_id))
                continue
        best_g, best_score = None, 0.0
        for g in groups:
            if g.product_id or any(c.site_id == it.site_id for c in g.items):
                continue
            sc = max(similarity(it.name, c.name) for c in g.items)
            if sc > best_score:
                best_g, best_score = g, sc
        if best_g and best_score >= MATCH:
            best_g.items.append(it)
            best_g.score = min(best_g.score, best_score)
        else:
            groups.append(Group(name=it.name, items=[it]))
    for g in groups:
        if not g.product_id:
            g.name = best_name([c.name for c in g.items])
    # products found on more shops first: those are the comparisons
    for g in groups:
        qs = [units.Qty.from_dict(c.qty) for c in g.items]
        ref = units.choose_ref([q for q in qs if q])
        g.ref = ref.to_dict() if ref else None
        for c, q in zip(g.items, qs):
            if ref is None:
                c.norm = c.price
            elif q is not None and c.price:
                v = units.convert(c.price, q, ref)
                c.norm = round(v) if v is not None else None
    groups.sort(key=lambda g: (-len(g.items), g.product_id is None, g.name))
    return groups
