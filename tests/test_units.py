from __future__ import annotations

import json

import pytest

from pricecomparison import units
from pricecomparison.catalog import Candidate, group_items, parse_listing, similarity
from pricecomparison.extract import extract
from pricecomparison.models import Offer, Product, Site
from pricecomparison.store import Store


def q(dim, amount):
    return (dim, pytest.approx(amount, rel=1e-3))


def as_t(x):
    return (x.dim, x.amount) if x else None


@pytest.mark.parametrize("name, want", [
    ("خرید سیمان تیپ 2 تهران 50 کیلویی", q("mass", 50)),
    ("سیمان پاکتی تیپ ۲ تهران", q("mass", 50)),  # a cement bag is 50 kg (assumed)
    ("گچ سفید سمنان ۴۰ کیلویی", q("mass", 40)),
    ("ماسه شسته (تنی)", q("mass", 1000)),
    ("میلگرد 12 ذوب آهن اصفهان شاخه 12 متری", q("mass", 12 * 12 * 12 / 162)),
    ("رنگ پلاستیک گالن ۳٫۵ لیتری", q("volume", 3.5)),
    ("پیچ بسته ۱۰۰ عددی", q("count", 100)),
    ("ایزوگام رول ۱۰ متر مربعی", q("area", 10)),
    ("میلگرد ۱۲ ذوب آهن اصفهان", None),
    ("تن ماهی طبیعت", None),  # "تن" alone is not a unit
    ("تیرچه بتنی ۴ متری", None),  # a piece's length, not what the price is for
    ("گوشی سامسونگ Galaxy A56 حافظه ۲۵۶ گیگ", None),
])
def test_unit_from_name(name, want):
    assert as_t(units.parse_name(name)) == want


@pytest.mark.parametrize("text, name, want", [
    ("۲۵٬۰۰۰ تومان / کیلوگرم", "میلگرد ۱۲", q("mass", 1)),
    ("۶۹۰٬۰۰۰ تومان (هر تن)", "ماسه", q("mass", 1000)),
    ("قیمت هر شاخه: ۱٬۵۶۰٬۰۰۰ تومان", "میلگرد ۱۴ ذوب آهن", q("mass", 14 * 14 * 12 / 162)),
    ("۱۸۵,۰۰۰ تومان هر کیسه", "سیمان سفید ساوه ۴۰ کیلویی", q("mass", 40)),
    ("۹۸۰٬۰۰۰ تومان / متر مربع", "سرامیک", q("area", 1)),
    ("تومان به ازای هر ۱۰ کیلو", "", q("mass", 10)),
    ("قیمت: ۲۳۰٬۰۰۰ تومان", "سیمان", None),
])
def test_unit_next_to_price(text, name, want):
    assert as_t(units.parse_per(text, name)) == want


def test_assumed_sizes_are_marked():
    assert units.parse_name("سیمان پاکتی تیپ ۲").assumed
    assert not units.parse_name("سیمان تیپ ۲ کیسه ۵۰ کیلویی").assumed
    assert units.parse_per("قیمت هر شاخه", "میلگرد ۱۴").assumed  # 12 m bar assumed


def test_admin_input():
    assert as_t(units.parse_input("هر تن")) == q("mass", 1000)
    assert as_t(units.parse_input("کیسه ۵۰ کیلویی")) == q("mass", 50)
    assert as_t(units.parse_input("عدد")) == q("count", 1)
    assert as_t(units.parse_input("شاخه ۱۲ متری", "میلگرد ۱۶")) == q("mass", 16 * 16 * 12 / 162)
    assert units.parse_input("سلام") is None


def test_convert_and_reference():
    ton = units.Qty("mass", 1000, "تن")
    bag = units.Qty("mass", 50, "کیسه ۵۰ کیلویی")
    assert units.convert(4_000_000, ton, bag) == 200_000
    assert units.convert(5, units.Qty("count", 1, "عدد"), bag) is None
    ref = units.choose_ref([bag, units.Qty("mass", 50, "کیسه ۵۰ کیلویی", assumed=True), ton])
    assert (ref.dim, ref.amount) == ("mass", 50)


# ---------------------------------------------------------------- pages
def page(body, head=""):
    return f"<html><head>{head}</head><body>{body}</body></html>"


def test_page_woo_price_suffix():
    body = """<div class="product"><div class="summary"><h1>میلگرد ۱۴ ذوب آهن اصفهان</h1>
      <p class="price"><span class="woocommerce-Price-amount amount">۲۶٬۵۰۰ تومان</span>
      <small class="woocommerce-price-suffix">/ کیلوگرم</small></p></div></div>"""
    r = extract(page(body))
    assert r.price == 26_500 and r.title == "میلگرد ۱۴ ذوب آهن اصفهان"
    assert (r.qty["dim"], r.qty["amount"], r.qty["source"]) == ("mass", 1, "price")


def test_page_jsonld_unit_code():
    ld = {"@type": "Product", "name": "ماسه شسته", "offers": {"@type": "Offer", "price": 690000, "priceCurrency": "IRT",
          "priceSpecification": {"@type": "UnitPriceSpecification", "price": 690000, "unitCode": "TNE"}}}
    r = extract(page(f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>'))
    assert (r.qty["dim"], r.qty["amount"], r.qty["source"]) == ("mass", 1000, "spec")


def test_page_specs_table_and_name():
    body = """<h1>سیمان سفید ساوه</h1><span class="p">۲۶۵٬۰۰۰ تومان</span>
      <table class="shop_attributes"><tr><th>واحد فروش</th><td>کیسه</td></tr><tr><th>وزن</th><td>۴۰ کیلوگرم</td></tr></table>"""
    r = extract(page(body), selector=".p")
    assert (r.qty["dim"], r.qty["amount"], r.qty["source"]) == ("mass", 40, "attr")
    # nothing on the page but the name
    r = extract(page('<h1>گچ خاک سمنان ۴۰ کیلویی</h1><span class="p">۸۲٬۰۰۰ تومان</span>'), selector=".p")
    assert (r.qty["amount"], r.qty["source"]) == (40, "name")


def test_listing_card_unit():
    html = """<ul class="products"><li class="product"><a href="/product/r14/"><h2 class="woocommerce-loop-product__title">میلگرد ۱۴ ذوب آهن</h2>
      <span class="price"><span class="amount">۲۶٬۵۰۰ تومان</span> <span class="woocommerce-price-suffix">هر کیلو</span></span></a></li></ul>"""
    it = parse_listing(html, "https://a.ir/shop/").items[0]
    assert as_t(units.resolve(it.name, it.near)) == q("mass", 1)


# ------------------------------------------------------------ comparing
def store_with(*offers):
    st = Store(None)
    p = Product(name="سیمان تیپ ۲")
    st.products[p.id] = p
    for i, (price, qty) in enumerate(offers):
        s = Site(name=f"s{i}", domains=[f"s{i}.ir"])
        st.sites[s.id] = s
        o = Offer(product_id=p.id, site_id=s.id, url=f"https://s{i}.ir/p", price=price, status="ok",
                  qty=units.parse_input(qty).to_dict() if qty else None)
        st.offers[o.id] = o
    return st, p


def test_compare_converts_to_the_common_unit():
    # per bag 185k, per ton 3.5M (= 175k a bag), per kilo 3.8k (= 190k a bag)
    st, p = store_with((185_000, "کیسه ۵۰ کیلویی"), (3_500_000, "هر تن"), (3_800, "کیلوگرم"))
    cmp = st.compare(p.id)
    assert cmp["ref"].amount == 50
    norms = sorted(v["norm"] for v in cmp["offers"].values())
    assert norms == [175_000, 185_000, 190_000]
    best = st.cheapest(p.id)
    assert st.sites[best.site_id].name == "s1"  # the per-ton shop, cheapest only once converted


def test_compare_keeps_out_what_cannot_be_compared():
    st, p = store_with((185_000, "کیسه ۵۰ کیلویی"), (190_000, "کیسه ۵۰ کیلویی"), (9_000, "عدد"), (100_000, None))
    states = sorted(v["state"] for v in st.compare(p.id)["offers"].values())
    assert states == ["ok", "ok", "other", "unknown"]
    assert st.cheapest(p.id).price == 185_000


def test_no_units_anywhere_compares_as_is():
    st, p = store_with((31_000_000, None), (30_500_000, None))
    cmp = st.compare(p.id)
    assert cmp["ref"] is None and st.cheapest(p.id).price == 30_500_000


def test_manual_unit_and_reference_win():
    st, p = store_with((185_000, None), (3_500_000, "هر تن"))
    o = next(o for o in st.offers.values() if o.price == 185_000)
    o.qty_manual = units.parse_input("کیسه ۵۰ کیلویی").to_dict()
    p.ref_unit = units.Qty("mass", 1000, "تن").to_dict()
    cmp = st.compare(p.id)
    assert cmp["offers"][o.id]["norm"] == 3_700_000 and not cmp["auto"]


def test_grouping_ignores_package_size_and_compares_per_unit():
    assert similarity("گچ سفید سمنان ۴۰ کیلویی", "گچ سفید سمنان ۲۵ کیلویی") >= 0.72
    bag40 = units.parse_input("کیسه ۴۰ کیلویی").to_dict()
    kilo = units.parse_input("کیلوگرم").to_dict()
    groups = group_items([
        Candidate("گچ سفید سمنان ۴۰ کیلویی", "u1", "A", 98_000, qty=bag40),
        Candidate("گچ سفید سمنان", "u2", "B", 2_300, qty=kilo),
    ], [], set())
    assert len(groups) == 1
    g = groups[0]
    assert g.ref["amount"] == 40 or g.ref["amount"] == 1
    norms = {c.url: c.norm for c in g.items}
    assert norms["u2"] / norms["u1"] == pytest.approx(2_300 * 40 / 98_000, rel=1e-3)
