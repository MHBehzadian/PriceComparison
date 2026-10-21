"""Sale units: which quantity a shop's price is for, so prices quoted per
kilo, per ton, per 50 kg bag or per 12 m rebar can be compared.

A quantity is a dimension plus an amount in that dimension's base unit:

  mass    kg        "کیسه ۵۰ کیلویی" = 50, "تن" = 1000
  length  m         "متر طول"
  area    m²        "متر مربع"
  volume  litre     "لیتر", "متر مکعب" = 1000
  count   piece     "عدد", "بسته ۱۰ عددی" = 10

Containers whose size is not stated (a bag of unknown weight, a roll, a
truck load) get a dimension of their own and only compare with the same
container. Everything here is plain text matching, no AI.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

MASS, LENGTH, AREA, VOLUME, COUNT = "mass", "length", "area", "volume", "count"
BAG, BAR, ROLL, PACK, TRUCK = "bag", "bar", "roll", "pack", "truck"
BASE_FA = {MASS: "کیلوگرم", LENGTH: "متر", AREA: "متر مربع", VOLUME: "لیتر", COUNT: "عدد",
           BAG: "کیسه", BAR: "شاخه", ROLL: "رول", PACK: "بسته", TRUCK: "سرویس"}

# source of a unit, strongest first
MANUAL, PRICE, SPEC, ATTR, NAME, DEFAULT = "manual", "price", "spec", "attr", "name", "default"


@dataclass
class Qty:
    dim: str
    amount: float  # in the dimension's base unit
    label: str  # as a buyer would say it: "کیسه ۵۰ کیلویی"
    source: str = ""
    assumed: bool = False  # filled in from a typical size, not read from the shop

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Qty | None:
        if not d or not d.get("dim") or not d.get("amount"):
            return None
        return cls(str(d["dim"]), float(d["amount"]), str(d.get("label") or ""), str(d.get("source") or ""), bool(d.get("assumed")))


# ------------------------------------------------------------------- text
_AR = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "أ": "ا", "إ": "ا", "‌": " ", "ـ": "", "٫": ".", "²": "2", "³": "3"})
_DIG = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def norm(s: str) -> str:
    s = (s or "").translate(_AR).translate(_DIG).lower()
    s = re.sub(r"(?<=\d)[/،,](?=\d)", ".", s)  # ۲/۵ and 2,5 as decimals
    s = re.sub(r"(\d)([^\d\s.])", r"\1 \2", s)
    s = re.sub(r"([^\d\s.])(\d)", r"\1 \2", s)
    return re.sub(r"\s+", " ", s).strip()


def fa_num(v: float) -> str:
    s = f"{v:,.2f}".rstrip("0").rstrip(".") if v % 1 else f"{int(v):,}"
    return s.replace(",", "٬").replace(".", "٫").translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def _w(alts: str) -> str:
    return rf"(?<!\w)(?:{alts})(?!\w)"


# (pattern, dimension, base units per one of it)
UNIT_WORDS: list[tuple[str, str, float]] = [
    (_w(r"کیلو ?گرمی|کیلو ?گرم|کیلویی|کیلو|kg|kilo"), MASS, 1),
    (_w(r"تنی|تن|ton"), MASS, 1000),
    (_w(r"گرمی|گرم|gr"), MASS, 0.001),
    (_w(r"متر ?مربعی|متر ?مربع|مترمربع|m 2|m2|sqm"), AREA, 1),
    (_w(r"متر ?مکعبی|متر ?مکعب|m 3|m3|کوب"), VOLUME, 1000),
    (_w(r"میلی ?لیتری|میلی ?لیتر|ml|سی ?سی|cc"), VOLUME, 0.001),
    (_w(r"لیتری|لیتر|lit|liter|litre"), VOLUME, 1),
    (_w(r"متر ?طولی|متر ?طول|متری|متر"), LENGTH, 1),
    (_w(r"عددی|عدد|دانه|قطعه|قالب|pcs|piece"), COUNT, 1),
]
PER_ADJ: list[tuple[str, str, float]] = [
    (_w(r"تنی"), MASS, 1000), (_w(r"کیلویی|کیلو ?گرمی"), MASS, 1), (_w(r"متر ?مربعی"), AREA, 1),
    (_w(r"متر ?مکعبی"), VOLUME, 1000), (_w(r"لیتری"), VOLUME, 1), (_w(r"متر ?طولی"), LENGTH, 1),
]
CONTAINERS: list[tuple[str, str]] = [
    (_w(r"کیسه ای|کیسه|پاکتی|پاکت|ساک|جامبو ?بگ"), BAG),
    (_w(r"شاخه ای|شاخه"), BAR),
    (_w(r"رولی|رول"), ROLL),
    (_w(r"بسته|کارتن|پالت|جعبه|گالن|سطل|حلب|بشکه"), PACK),
    (_w(r"سرویس|خاور|کامیون|تریلی|بار"), TRUCK),
]
_N = r"(\d+(?:\.\d+)?)"

# typical sizes when a shop names the container but not its size
BAG_KG = [(r"سیمان", 50), (r"گچ", 40), (r"چسب کاشی|پودر کاشی", 20), (r"آهک", 25), (r"پودر سنگ|میکرونیزه", 25), (r"پرلیت", 25)]
REBAR_LEN_M = 12


def _container(text: str) -> str | None:
    for pat, kind in CONTAINERS:
        if re.search(pat, text):
            return kind
    return None


def _sizes(text: str) -> list[tuple[str, float, str]]:
    """Every "<number> <unit>" in the text: [(dim, base amount, matched text)]."""
    out = []
    for pat, dim, mult in UNIT_WORDS:
        for m in re.finditer(_N + r" ?" + pat, text):
            out.append((dim, float(m.group(1)) * mult, m.group(0), m.start()))
    out.sort(key=lambda x: x[3])
    return [(d, a, t) for d, a, t, _ in out]


def strip_sizes(text: str) -> str:
    """The name without its package size, for matching one product sold in
    different packages ("گچ ۴۰ کیلویی" and "گچ ۲۵ کیلویی")."""
    t = norm(text)
    for pat, dim, _ in UNIT_WORDS:
        if dim in (MASS, VOLUME, AREA, COUNT):
            t = re.sub(_N + r" ?" + pat, " ", t)
    return re.sub(r"\s+", " ", t).strip()


def label_of(dim: str, amount: float) -> str:
    if dim == MASS:
        if amount >= 1000 and amount % 1000 == 0:
            return "تن" if amount == 1000 else f"{fa_num(amount / 1000)} تن"
        if amount < 1:
            return f"{fa_num(amount * 1000)} گرم"
        return "کیلوگرم" if amount == 1 else f"{fa_num(amount)} کیلوگرم"
    if dim == VOLUME and amount >= 1000 and amount % 1000 == 0:
        return "متر مکعب" if amount == 1000 else f"{fa_num(amount / 1000)} متر مکعب"
    base = BASE_FA.get(dim, dim)
    return base if amount == 1 else f"{fa_num(amount)} {base}"


def _bar(name: str, length: float | None, source: str) -> Qty:
    """A bar: rebar is weighed (d²/162 kg per metre), other bars by length."""
    m = re.search(r"میلگرد(?: \w+)? " + _N, name)
    d = float(m.group(1)) if m else None
    assumed = length is None
    length = length or REBAR_LEN_M
    if d and 6 <= d <= 40:
        kg = round(d * d / 162 * length, 2)
        return Qty(MASS, kg, f"شاخه {fa_num(length)} متری (≈{fa_num(round(kg, 1))} کیلوگرم)", source, assumed)
    if not assumed:
        return Qty(LENGTH, length, f"شاخه {fa_num(length)} متری", source)
    return Qty(BAR, 1, "شاخه", source)


def _fill(kind: str, name: str, source: str) -> Qty:
    """A container: its size from the name, else a typical size."""
    sizes = _sizes(name)
    by_dim = {d: (a, t) for d, a, t in reversed(sizes)}
    if kind == BAR:
        return _bar(name, by_dim.get(LENGTH, (None,))[0], source)
    if kind == BAG:
        if MASS in by_dim:
            return Qty(MASS, by_dim[MASS][0], f"کیسه {fa_num(by_dim[MASS][0])} کیلویی", source)
        for pat, kg in BAG_KG:
            if re.search(pat, name):
                return Qty(MASS, kg, f"کیسه {fa_num(kg)} کیلویی", source or DEFAULT, assumed=True)
        return Qty(BAG, 1, "کیسه", source)
    if kind == ROLL:
        if AREA in by_dim:
            return Qty(AREA, by_dim[AREA][0], f"رول {label_of(AREA, by_dim[AREA][0])}", source)
        if LENGTH in by_dim:
            return Qty(LENGTH, by_dim[LENGTH][0], f"رول {label_of(LENGTH, by_dim[LENGTH][0])}", source)
        return Qty(ROLL, 1, "رول", source)
    if kind == PACK:
        for dim in (COUNT, MASS, VOLUME, AREA):
            if dim in by_dim:
                return Qty(dim, by_dim[dim][0], f"بسته {label_of(dim, by_dim[dim][0])}", source)
        return Qty(PACK, 1, "بسته", source)
    return Qty(TRUCK, 1, "سرویس (بار کامل)", source)


# --------------------------------------------------------------- readers
_PER = r"(?:هر|/|به ازای(?: هر)?|برای هر|فی|قیمت هر|بر)"


def parse_per(text: str, name: str = "", source: str = PRICE) -> Qty | None:
    """The unit a price is quoted per, from the words around the price:
    "۲۵٬۰۰۰ تومان / کیلوگرم", "قیمت هر شاخه", "تومان (هر تن)"."""
    t = norm(text)
    n = norm(name)
    for prefix in (_PER + r" ?", r"(?:تومان|ریال|تومن) ?[(]?"):
        for pat, dim, mult in UNIT_WORDS:
            m = re.search(prefix + r"(?:" + _N + r" ?)?" + pat, t)
            if m and not (dim == LENGTH and re.search(prefix + r"متر ?(?:مربع|مکعب)", t)):
                amount = float(m.group(1)) * mult if m.group(1) else mult
                return Qty(dim, amount, label_of(dim, amount), source)
        for pat, kind in CONTAINERS:
            if re.search(prefix + pat, t):
                return _fill(kind, n, source)
    return None


def parse_name(name: str, source: str = NAME) -> Qty | None:
    """The package a product name describes: "سیمان تیپ ۲ پاکتی ۵۰ کیلویی",
    "ماسه شسته (تنی)", "میلگرد ۱۲ شاخه ۱۲ متری"."""
    t = norm(name)
    kind = _container(t)
    if kind:
        return _fill(kind, t, source)
    for dim in (MASS, VOLUME, AREA, COUNT):
        for d, amount, matched in _sizes(t):
            if d == dim:
                return Qty(dim, amount, label_of(dim, amount), source)
    # "(تنی)", "کیلویی" with no number: sold per one of it. Only these
    # adjective forms count; a bare "تن" may be part of a name ("تن ماهی")
    for pat, dim, mult in PER_ADJ:
        if re.search(pat, t):
            return Qty(dim, mult, label_of(dim, mult), source)
    return None


UNIT_CODES: dict[str, tuple[str, float]] = {
    "KGM": (MASS, 1), "TNE": (MASS, 1000), "GRM": (MASS, 0.001), "MTR": (LENGTH, 1), "MTK": (AREA, 1),
    "MTQ": (VOLUME, 1000), "LTR": (VOLUME, 1), "MLT": (VOLUME, 0.001), "C62": (COUNT, 1), "H87": (COUNT, 1),
    "EA": (COUNT, 1), "PCE": (COUNT, 1), "NAR": (COUNT, 1),
}


def from_code(code: Any, value: Any = None) -> Qty | None:
    """schema.org unitCode (UN/CEFACT) with an optional referenceQuantity."""
    c = str(code or "").strip().upper()
    if c not in UNIT_CODES:
        return None
    dim, mult = UNIT_CODES[c]
    try:
        v = float(value) if value not in (None, "") else 1.0
    except (TypeError, ValueError):
        v = 1.0
    amount = v * mult
    return Qty(dim, amount, label_of(dim, amount), SPEC)


ATTR_UNIT = re.compile(r"واحد|بسته ?بندی|نوع فروش|فروش به صورت|unit")
ATTR_SIZE = re.compile(r"وزن|حجم|مقدار|متراژ|تعداد در|weight|size")


def from_attrs(attrs: dict[str, str], name: str) -> Qty | None:
    """A specs table: "واحد: کیلوگرم", "وزن: ۵۰ کیلوگرم"."""
    # "واحد: کیسه" takes the bag's weight from the "وزن" row when there is one
    sizes = " ".join(v for k, v in attrs.items() if ATTR_SIZE.search(norm(k)))
    for k, v in attrs.items():
        if ATTR_UNIT.search(norm(k)):
            q = parse_per("هر " + v, f"{name} {sizes}", ATTR) or parse_name(v, ATTR)
            if q:
                return q
    for k, v in attrs.items():
        if ATTR_SIZE.search(norm(k)):
            q = parse_name(v, ATTR)
            if q and q.dim in (MASS, VOLUME, AREA, COUNT):
                kind = _container(norm(name))
                if kind == BAG and q.dim == MASS:
                    q.label = f"کیسه {fa_num(q.amount)} کیلویی"
                return q
    return None


def resolve(name: str, near_price: str = "", attrs: dict[str, str] | None = None, spec: Qty | None = None) -> Qty | None:
    """The quantity a price is for. What the shop writes next to its price
    wins, then structured data, then the specs table, then the name."""
    return spec or parse_per(near_price, name) or from_attrs(attrs or {}, name) or parse_name(name)


def parse_input(text: str, name: str = "") -> Qty | None:
    """What the admin types as a unit in the panel: "کیسه ۵۰ کیلویی",
    "هر تن", "شاخه ۱۲ متری"."""
    q = parse_per("هر " + text, (text + " " + name).strip(), MANUAL) or parse_name(text + " " + name if _container(norm(text)) else text, MANUAL)
    if q:
        q.source, q.assumed = MANUAL, False
        q.label = q.label or text.strip()
    return q


# ------------------------------------------------------------- comparing
def choose_ref(qtys: list[Qty]) -> Qty | None:
    """The unit to compare a product's prices in: the package most shops
    sell, read rather than assumed when possible."""
    if not qtys:
        return None
    dims = Counter(q.dim for q in qtys)
    dim = max(dims, key=lambda d: (dims[d], d in (MASS, VOLUME, AREA, LENGTH, COUNT)))
    same = [q for q in qtys if q.dim == dim]
    sizes = Counter(round(q.amount, 3) for q in same)

    def rank(a: float):
        mine = [q for q in same if round(q.amount, 3) == a]
        package = any(_container(norm(q.label)) for q in mine)  # "کیسه ۵۰ کیلویی" reads better than "۵۰ کیلوگرم"
        return (sizes[a], sum(1 for q in mine if not q.assumed), package, -a)

    best = max(sizes, key=rank)
    q = next(q for q in same if round(q.amount, 3) == best)
    return Qty(q.dim, q.amount, q.label, "", q.assumed)


def convert(price: float, q: Qty, ref: Qty) -> float | None:
    """The price of `ref` at the rate of `price` per `q`, or None when the
    two cannot be compared."""
    if q.dim != ref.dim or not q.amount:
        return None
    return price / q.amount * ref.amount


def ref_options(dim: str, seen: list[Qty]) -> list[Qty]:
    """Units the admin can pick to compare a product in."""
    out: dict[float, Qty] = {}
    for q in seen:
        if q.dim == dim:
            out.setdefault(round(q.amount, 3), Qty(q.dim, q.amount, q.label))
    std = {MASS: [1, 1000], LENGTH: [1], AREA: [1], VOLUME: [1, 1000], COUNT: [1]}.get(dim, [1])
    for a in std:
        out.setdefault(float(a), Qty(dim, float(a), label_of(dim, a)))
    return sorted(out.values(), key=lambda q: q.amount)
