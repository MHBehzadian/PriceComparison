from __future__ import annotations

import json

import pytest

from pricecomparison.extract import ExtractError, extract, numbers_in, parse_number


def page(body: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def ld(obj) -> str:
    return f'<script type="application/ld+json">{json.dumps(obj, ensure_ascii=False)}</script>'


def test_persian_numbers():
    assert numbers_in("۱۲٬۵۰۰٬۰۰۰ تومان") == [12_500_000]
    assert numbers_in("٣٤,٩٩٠,٠٠٠ ريال") == [34_990_000]
    assert numbers_in("1.250.000") == [1_250_000]
    assert parse_number("12500000.00") == 12_500_000
    assert parse_number(0) is None


def test_jsonld_offer_in_rial():
    html = page(ld({"@context": "https://schema.org", "@type": "Product", "name": "x",
                    "offers": {"@type": "Offer", "price": "125000000", "priceCurrency": "IRR"}}))
    r = extract(html)
    assert (r.price, r.method, r.available) == (12_500_000, "jsonld", True)


def test_jsonld_graph_and_cheapest_offer():
    html = page(ld({"@graph": [
        {"@type": "WebPage"},
        {"@type": ["Product"], "offers": [
            {"@type": "Offer", "price": 9_900_000, "priceCurrency": "IRT"},
            {"@type": "Offer", "price": 9_500_000, "priceCurrency": "IRT"},
            {"@type": "Offer", "price": 9_000_000, "availability": "https://schema.org/OutOfStock"},
        ]},
    ]}))
    assert extract(html).price == 9_500_000


def test_jsonld_aggregate_offer():
    html = page(ld({"@type": "Product", "offers": {"@type": "AggregateOffer", "lowPrice": 18_700_000, "highPrice": 21_000_000, "priceCurrency": "IRT"}}))
    r = extract(html)
    assert r.price == 18_700_000 and r.old_price == 21_000_000


def test_jsonld_out_of_stock():
    html = page(ld({"@type": "Product", "offers": {"@type": "Offer", "price": 0, "availability": "http://schema.org/OutOfStock"}}))
    r = extract(html)
    assert r.available is False and r.price is None


def test_site_unit_overrides_page():
    # shops that label toman prices as IRR: the admin sets the site to toman
    html = page(ld({"@type": "Product", "offers": {"price": 12_500_000, "priceCurrency": "IRR"}}))
    assert extract(html, site_unit="toman").price == 12_500_000


def test_meta_tags():
    head = '<meta property="product:price:amount" content="4590000"><meta property="product:price:currency" content="IRT">'
    assert extract(page("", head)).price == 4_590_000


def test_microdata():
    body = '<div itemscope itemtype="https://schema.org/Product"><span itemprop="price" content="2390000">۲٬۳۹۰٬۰۰۰ تومان</span></div>'
    r = extract(page(body))
    assert (r.price, r.method) == (2_390_000, "microdata")


def test_woocommerce_discount():
    body = """
    <div class="product"><div class="summary entry-summary"><h1>گوشی</h1>
      <p class="price"><del><span class="woocommerce-Price-amount amount"><bdi>۳۵٬۰۰۰٬۰۰۰&nbsp;<span class="woocommerce-Price-currencySymbol">تومان</span></bdi></span></del>
      <ins><span class="woocommerce-Price-amount amount"><bdi>۳۱٬۵۰۰٬۰۰۰&nbsp;<span class="woocommerce-Price-currencySymbol">تومان</span></bdi></span></ins></p>
    </div></div>"""
    r = extract(page(body))
    assert (r.price, r.old_price, r.method) == (31_500_000, 35_000_000, "woo")


def test_woocommerce_rial_and_out_of_stock():
    body = '<div class="product"><div class="summary"><p class="price"><span class="amount">۴۵۰٬۰۰۰ ریال</span></p></div></div>'
    assert extract(page(body)).price == 45_000
    out = '<div class="product"><div class="summary"><p class="stock out-of-stock">ناموجود</p></div></div>'
    assert extract(page(out)).available is False


def test_next_data():
    data = {"props": {"pageProps": {"product": {"title": "x", "rrp_price": 260_000_000, "selling_price": 248_000_000}}}}
    html = page(f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>')
    r = extract(html, site_unit="rial")
    assert (r.price, r.old_price, r.method) == (24_800_000, 26_000_000, "nextdata")


def test_selector_wins_and_reports_breakage():
    body = '<h1>x</h1><div class="p-price">۱٬۲۰۰٬۰۰۰ تومان</div>' + ld({"@type": "Product", "offers": {"price": 5}})
    assert extract(page(body), selector=".p-price").price == 1_200_000
    with pytest.raises(ExtractError, match="پیدا نشد"):
        extract(page(body), selector=".gone")
    with pytest.raises(ExtractError, match="نامعتبر"):
        extract(page(body), selector="..bad[")


def test_text_fallback_is_a_guess():
    body = '<p>ارسال رایگان بالای ۵۰۰٬۰۰۰ تومان</p><h1>هدفون</h1><div><span>قیمت:</span> <b>۲۴٬۸۰۰٬۰۰۰</b> تومان</div><div>محصول مشابه ۹۹۰٬۰۰۰ تومان</div>'
    r = extract(page(body))
    assert (r.price, r.method, r.guess) == (24_800_000, "text", True)


def test_out_of_stock_words_and_nothing_found():
    assert extract(page("<main><h1>x</h1><p>ناموجود</p></main>")).available is False
    with pytest.raises(ExtractError):
        extract(page("<h1>x</h1><p>hello</p>"))
