"""Data model. Everything is plain dataclasses persisted as one JSON file."""

from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any

# Price units a shop can show. Everything is stored in toman.
AUTO = "auto"
TOMAN = "toman"
RIAL = "rial"
UNITS = (AUTO, TOMAN, RIAL)

# Offer status
O_PENDING = "pending"  # never checked
O_OK = "ok"  # price read
O_OUT = "out"  # page read, product not available
O_ERROR = "error"  # page or price could not be read

MAX_HISTORY = 500  # price points kept per offer


def new_id(prefix: str = "") -> str:
    return prefix + secrets.token_hex(3)


def now() -> float:
    return time.time()


class Model:
    """Mixin: tolerant (de)serialisation, so old state files keep loading."""

    @classmethod
    def from_dict(cls, d: dict[str, Any]):
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[call-overload]


@dataclass
class Site(Model):
    """A shop prices are read from. Matched to product links by domain."""

    name: str
    domains: list[str]  # "digikala.com"; subdomains match too
    id: str = field(default_factory=lambda: new_id("w"))
    enabled: bool = True
    unit: str = AUTO
    # optional overrides, for shops the automatic readers get wrong
    selector: str = ""  # CSS selector of the element holding the price
    out_selector: str = ""  # CSS selector present only when out of stock
    # category import: product links on a listing page, and its next-page link
    list_selector: str = ""
    next_selector: str = ""
    note: str = ""
    created: float = field(default_factory=now)


@dataclass
class Product(Model):
    name: str
    id: str = field(default_factory=lambda: new_id("p"))
    category: str = ""
    note: str = ""
    target_price: int | None = None  # toman; an event fires when any shop goes below it
    aliases: list[str] = field(default_factory=list)  # names the shops give it, for matching imports
    ref_unit: dict | None = None  # units.Qty the admin picked to compare in; None = automatic
    created: float = field(default_factory=now)


@dataclass
class Offer(Model):
    """One product on one shop: the page the price is read from."""

    product_id: str
    site_id: str
    url: str
    id: str = field(default_factory=lambda: new_id("o"))
    enabled: bool = True
    qty_manual: dict | None = None  # units.Qty set by the admin; wins over what is read
    # runtime
    status: str = O_PENDING
    price: int | None = None  # toman
    old_price: int | None = None  # list price before discount, if the shop shows one
    method: str = ""  # which reader found the price
    title: str = ""  # the product's name on this shop
    qty: dict | None = None  # units.Qty the price is for, as read from the shop
    error: str = ""
    last_check: float = 0
    last_ok: float = 0
    fail_count: int = 0
    # [ts, toman] per check that changed something; toman is None while unavailable
    history: list[list[Any]] = field(default_factory=list)

    def record(self, ts: float, price: int | None) -> None:
        last = self.history[-1][1] if self.history else "-"
        if price != last:
            self.history.append([round(ts), price])
            del self.history[:-MAX_HISTORY]


@dataclass
class Settings(Model):
    interval_min: int = 360  # how often every offer is checked
    timeout_s: int = 20
    host_delay_s: float = 3.0  # pause between two requests to the same shop
    concurrency: int = 4  # shops read at the same time
    proxy: str = ""  # http(s)/socks proxy for every request, if the server needs one
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    )


@dataclass
class Event(Model):
    level: str  # info | success | warning | error
    kind: str
    message: str
    refs: dict[str, str] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("e"))
    ts: float = field(default_factory=now)
