"""In-memory state persisted to a single JSON file (atomic writes)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import units
from .models import Event, Offer, Product, Settings, Site

MAX_EVENTS = 1000


def replace(src: str | Path, dst: Path) -> None:
    """os.replace, retried: on Windows a virus scanner or a backup tool
    holding the old file open for a moment makes it fail."""
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.2)


def host_of(url: str) -> str:
    host = (urlsplit(url.strip()).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def clean_domain(d: str) -> str:
    d = d.strip().lower()
    if "://" in d:
        d = host_of(d)
    d = d.split("/", 1)[0]
    return d[4:] if d.startswith("www.") else d


class Store:
    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path else None
        self.sites: dict[str, Site] = {}
        self.products: dict[str, Product] = {}
        self.offers: dict[str, Offer] = {}
        self.settings = Settings()
        self.events: list[Event] = []
        self.meta: dict[str, Any] = {}
        if self.path and self.path.exists():
            self.apply(json.loads(self.path.read_text(encoding="utf-8")))

    # ---- persistence -------------------------------------------------
    def apply(self, raw: dict) -> None:
        self.sites = {d["id"]: Site.from_dict(d) for d in raw.get("sites", [])}
        self.products = {d["id"]: Product.from_dict(d) for d in raw.get("products", [])}
        self.offers = {d["id"]: Offer.from_dict(d) for d in raw.get("offers", [])}
        self.settings = Settings.from_dict(raw.get("settings", {}))
        self.events = [Event.from_dict(d) for d in raw.get("events", [])]
        self.meta = dict(raw.get("meta", {}))

    def raw(self) -> dict[str, Any]:
        return {
            "sites": [s.to_dict() for s in self.sites.values()],
            "products": [p.to_dict() for p in self.products.values()],
            "offers": [o.to_dict() for o in self.offers.values()],
            "settings": self.settings.to_dict(),
            "events": [e.to_dict() for e in self.events[-MAX_EVENTS:]],
            "meta": self.meta,
        }

    def save(self) -> None:
        if not self.path:
            return
        data = self.raw()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        # always UTF-8: Windows' default code page cannot hold Persian text
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        replace(tmp, self.path)

    # ---- queries -----------------------------------------------------
    def site_for_url(self, url: str) -> Site | None:
        """The site whose domain the URL is on (longest match wins, so a
        shop's own subdomain can be a separate site)."""
        host = host_of(url)
        best, best_len = None, -1
        for s in self.sites.values():
            for d in s.domains:
                if (host == d or host.endswith("." + d)) and len(d) > best_len:
                    best, best_len = s, len(d)
        return best

    def offers_of(self, product_id: str) -> list[Offer]:
        return [o for o in self.offers.values() if o.product_id == product_id]

    def live(self, o: Offer) -> bool:
        return bool(o.price) and o.status == "ok" and o.enabled and self.sites.get(o.site_id, Site("", [])).enabled

    @staticmethod
    def qty_of(o: Offer) -> units.Qty | None:
        return units.Qty.from_dict(o.qty_manual) or units.Qty.from_dict(o.qty)

    def compare(self, product_id: str, offers: list[Offer] | None = None) -> dict[str, Any]:
        """A product's prices in one unit. Returns {"ref": Qty | None,
        "auto": bool, "offers": {offer id: {"norm": toman for ref | None,
        "state": "ok" | "other" | "unknown" | "none"}}}.

        With no unit known for any shop, prices are compared as they are
        ("none"). Otherwise a shop's price counts only once it is converted
        to the reference unit: a different kind of unit ("other", a price
        per piece next to prices per kilo) or no unit at all ("unknown")
        keeps it out of the comparison until the admin sets it."""
        p = self.products.get(product_id)
        offers = self.offers_of(product_id) if offers is None else offers
        qtys = {o.id: self.qty_of(o) for o in offers}
        known = [q for q in qtys.values() if q]
        manual = units.Qty.from_dict(p.ref_unit) if p else None
        ref = manual or units.choose_ref(known)
        out: dict[str, dict[str, Any]] = {}
        for o in offers:
            q = qtys[o.id]
            if ref is None:
                out[o.id] = {"norm": o.price, "state": "none"}
            elif q is None:
                out[o.id] = {"norm": None, "state": "unknown"}
            else:
                v = units.convert(o.price, q, ref) if o.price else None
                out[o.id] = {"norm": round(v) if v is not None else None, "state": "ok" if q.dim == ref.dim else "other"}
        return {"ref": ref, "auto": manual is None, "offers": out}

    def cheapest(self, product_id: str, cmp: dict[str, Any] | None = None) -> Offer | None:
        cmp = cmp or self.compare(product_id)
        live = [o for o in self.offers_of(product_id) if self.live(o) and cmp["offers"][o.id]["norm"] is not None
                and cmp["offers"][o.id]["state"] in ("ok", "none")]
        return min(live, key=lambda o: cmp["offers"][o.id]["norm"]) if live else None

    def snapshot(self) -> dict[str, Any]:
        products, offers = [], []
        by_product: dict[str, list[Offer]] = {}
        for o in self.offers.values():
            by_product.setdefault(o.product_id, []).append(o)
        for p in sorted(self.products.values(), key=lambda p: p.created):
            mine = by_product.get(p.id, [])
            cmp = self.compare(p.id, mine)
            ref = cmp["ref"]
            seen = [q for q in (self.qty_of(o) for o in mine) if q]
            products.append({
                **p.to_dict(),
                "ref": ref.to_dict() if ref else None,
                "ref_auto": cmp["auto"],
                "ref_options": [q.to_dict() for q in units.ref_options(ref.dim, seen)] if ref else [],
            })
            for o in mine:
                q = self.qty_of(o)
                offers.append({**o.to_dict(), **cmp["offers"][o.id], "unit": q.to_dict() if q else None})
        return {
            "sites": [s.to_dict() for s in sorted(self.sites.values(), key=lambda s: s.created)],
            "products": products,
            "offers": offers,
        }
