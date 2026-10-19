"""In-memory state persisted to a single JSON file (atomic writes)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import Event, Offer, Product, Settings, Site

MAX_EVENTS = 1000


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
            self.apply(json.loads(self.path.read_text()))

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
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

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

    def cheapest(self, product_id: str) -> Offer | None:
        live = [
            o for o in self.offers_of(product_id)
            if o.price and o.status == "ok" and o.enabled and self.sites.get(o.site_id, Site("", [])).enabled
        ]
        return min(live, key=lambda o: o.price) if live else None

    def snapshot(self) -> dict[str, Any]:
        return {
            "sites": [s.to_dict() for s in sorted(self.sites.values(), key=lambda s: s.created)],
            "products": [p.to_dict() for p in sorted(self.products.values(), key=lambda p: p.created)],
            "offers": [o.to_dict() for o in self.offers.values()],
        }
