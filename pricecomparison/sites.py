"""Well-known Iranian online shops, added on first run. The admin can edit,
disable or delete any of them and add others; nothing depends on this list."""

from __future__ import annotations

from .models import Site

DEFAULT_SITES: list[dict] = [
    {"name": "دیجی‌کالا", "domains": ["digikala.com"], "note": "از API خود دیجی‌کالا خوانده می‌شود"},
    {"name": "ترب", "domains": ["torob.com"], "note": "مقایسه‌گر؛ کمترین قیمت فروشنده‌ها"},
    {"name": "تکنولایف", "domains": ["technolife.com", "technolife.ir"]},
    {"name": "اسنپ‌شاپ", "domains": ["snappshop.ir"]},
    {"name": "باسلام", "domains": ["basalam.com"]},
    {"name": "ایمالز", "domains": ["emalls.ir"], "note": "مقایسه‌گر"},
    {"name": "مقداد آی‌تی", "domains": ["meghdadit.com"]},
    {"name": "لیون کامپیوتر", "domains": ["lioncomputer.com"]},
    {"name": "دیجی‌استایل", "domains": ["digistyle.com"]},
    {"name": "خانومی", "domains": ["khanoumi.com"]},
]


def seed_sites(store) -> None:
    if store.sites or store.meta.get("seeded"):
        return
    for i, d in enumerate(DEFAULT_SITES):
        s = Site(**d)
        s.created += i / 1000  # keep the list's order
        store.sites[s.id] = s
    store.meta["seeded"] = True
    store.save()
