from __future__ import annotations

import json

from pricecomparison.catalog import Candidate, clean_name, group_items, guess_next, parse_listing, similarity, tokens

WOO = """<html><body><h1>مصالح ساختمانی</h1>
<ul class="products columns-4">
  <li class="product type-product"><a href="https://masaleh-tehran.ir/product/cement-type2/" class="woocommerce-LoopProduct-link">
    <img alt="x"><h2 class="woocommerce-loop-product__title">سیمان پاکتی تیپ ۲ تهران</h2>
    <span class="price"><del><span class="woocommerce-Price-amount amount"><bdi>۲۰۰٬۰۰۰ تومان</bdi></span></del>
    <ins><span class="woocommerce-Price-amount amount"><bdi>۱۸۵٬۰۰۰ <span class="woocommerce-Price-currencySymbol">تومان</span></bdi></span></ins></span></a>
    <a href="?add-to-cart=12" class="button">افزودن به سبد</a></li>
  <li class="product"><a href="/product/rebar-12/" class="woocommerce-LoopProduct-link">
    <h2 class="woocommerce-loop-product__title">میلگرد ۱۲ ذوب آهن اصفهان</h2>
    <span class="price"><span class="woocommerce-Price-amount amount">۱۱٬۵۰۰٬۰۰۰ ریال</span></span></a></li>
  <li class="product"><a href="/product/gypsum/"><h2 class="woocommerce-loop-product__title">گچ سفید سمنان</h2></a>
    <span class="price">تماس بگیرید</span></li>
</ul>
<nav class="woocommerce-pagination"><a class="next page-numbers" href="/product-category/building-materials/page/2/">→</a></nav>
</body></html>"""

WOODMART = """<div class="products"><div class="product-grid-item product">
  <a href="https://shahrsakhtemani.com/product/brick/" class="product-image-link"><img alt="آجر"></a>
  <h3 class="wd-entities-title"><a href="https://shahrsakhtemani.com/product/brick/">آجر سفال ۱۰ سوراخ</a></h3>
  <span class="price"><span class="amount">۴٬۲۰۰ تومان</span></span></div></div>"""

EMALLS = """<html><body><h1>لیست قیمت مصالح ساختمانی</h1>
<div class="item"><a href="/مشخصات_سیمان-تیپ-2~id~111"><img alt="سیمان تیپ 2 تهران 50 کیلویی"></a>
  <a href="/مشخصات_سیمان-تیپ-2~id~111" title="سیمان تیپ 2 تهران 50 کیلویی">سیمان تیپ 2 تهران 50 کیلویی</a>
  <span>از ۱۸۰٬۰۰۰ تومان</span></div>
<div class="item"><a href="/مشخصات_میلگرد-14~id~222">میلگرد 14 ذوب آهن</a><span>قیمت: ۱٬۲۰۰٬۰۰۰ تومان</span></div>
<a href="/لیست-قیمت_سیمان~Category~1">سیمان</a>
</body></html>"""


def test_woocommerce_listing():
    lst = parse_listing(WOO, "https://masaleh-tehran.ir/product-category/building-materials/")
    assert lst.method == "woo" and lst.title == "مصالح ساختمانی"
    got = {i.url: (i.name, i.price) for i in lst.items}
    assert got == {
        "https://masaleh-tehran.ir/product/cement-type2/": ("سیمان پاکتی تیپ ۲ تهران", 185_000),
        "https://masaleh-tehran.ir/product/rebar-12/": ("میلگرد ۱۲ ذوب آهن اصفهان", 1_150_000),
        "https://masaleh-tehran.ir/product/gypsum/": ("گچ سفید سمنان", None),
    }
    assert lst.next_url == "https://masaleh-tehran.ir/product-category/building-materials/page/2/"


def test_woodmart_listing():
    lst = parse_listing(WOODMART, "https://shahrsakhtemani.com/shop/")
    assert [(i.name, i.price) for i in lst.items] == [("آجر سفال ۱۰ سوراخ", 4_200)]


def test_link_heuristic_listing_emalls_style():
    lst = parse_listing(EMALLS, "https://emalls.ir/لیست-قیمت_مصالح~Category~70445")
    assert lst.method == "links"
    assert [(i.name, i.price) for i in lst.items] == [("سیمان تیپ 2 تهران 50 کیلویی", 180_000), ("میلگرد 14 ذوب آهن", 1_200_000)]
    assert lst.items[0].url == "https://emalls.ir/مشخصات_سیمان-تیپ-2~id~111"


def test_itemlist_and_site_selector():
    ld = {"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "position": 1, "url": "https://a.ir/p/1", "name": "پیچ"}]}
    html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    assert [i.name for i in parse_listing(html, "https://a.ir/c").items] == ["پیچ"]
    html = '<div class="x"><a class="t" href="/goods/9">مفتول</a><b>۹۰٬۰۰۰ تومان</b></div>'
    lst = parse_listing(html, "https://a.ir/c", selector="a.t")
    assert lst.method == "selector" and lst.items[0].url == "https://a.ir/goods/9"


def test_guess_next_page():
    assert guess_next("https://emalls.ir/x~Category~70445", 2) == "https://emalls.ir/x~Category~70445~page~2"
    assert guess_next("https://emalls.ir/x~Category~70445~page~2", 3) == "https://emalls.ir/x~Category~70445~page~3"
    assert guess_next("https://shahrsakhtemani.com/shop/", 2) == "https://shahrsakhtemani.com/shop/page/2/"
    assert guess_next("https://m.ir/product-category/b/page/2/", 3) == "https://m.ir/product-category/b/page/3/"
    assert guess_next("https://m.ir/about", 2) is None


def test_names():
    assert tokens("خرید سیمان پاکتی تیپ۲ تهران") == ["سیمان", "پاکتی", "تیپ", "2", "تهران"]
    assert similarity("سیمان تیپ ۲ تهران پاکتی ۵۰ کیلویی", "قیمت سیمان پاکتی تیپ 2 تهران") >= 0.72
    assert similarity("میلگرد ۱۲ ذوب آهن", "میلگرد ۱۴ ذوب آهن") == 0  # sizes differ
    assert similarity("سیمان تیپ ۲", "سیمان سفید ساوه") < 0.72
    assert similarity("سیمان تیپ ۲ تهران پاکتی", "سیمان تیپ ۲ تهران فله") == 0  # bag vs bulk
    assert similarity("سیمان تیپ ۲ تهران", "سیمان تیپ ۲ آبیک") < 0.72  # different factory


def test_grouping_across_sites_and_existing_products():
    items = [
        Candidate("سیمان پاکتی تیپ ۲ تهران", "u1", "A", 185_000),
        Candidate("میلگرد ۱۲ ذوب آهن اصفهان", "u2", "A"),
        Candidate("خرید سیمان تیپ 2 تهران 50 کیلویی", "u3", "B", 180_000),
        Candidate("میلگرد ۱۴ ذوب آهن", "u4", "B"),
        Candidate("سیمان تیپ 2 تهران (پاکتی)", "u5", "C"),
        Candidate("گچ سفید سمنان ۴۰ کیلویی", "u6", "C"),
    ]
    products = [("p1", ["گچ سفید سمنان", "گچ سمنان ۴۰ کیلو"])]
    taken = {("p1", "A")}
    groups = group_items(items, products, taken)
    cement = next(g for g in groups if any(c.url == "u1" for c in g.items))
    assert sorted(c.url for c in cement.items) == ["u1", "u3", "u5"] and cement.product_id is None
    assert not cement.name.startswith("خرید")
    assert groups[0] is cement  # found on most shops comes first
    gypsum = next(g for g in groups if any(c.url == "u6" for c in g.items))
    assert gypsum.product_id == "p1"
    # rebar 12 and 14 stay apart
    assert len([g for g in groups if any(c.url in ("u2", "u4") for c in g.items)]) == 2


def test_clean_name():
    assert clean_name("خرید و قیمت سیمان تیپ ۲") == "سیمان تیپ ۲"
    assert clean_name("قیمت میلگرد") == "میلگرد"
    assert clean_name("خرید") == "خرید"
