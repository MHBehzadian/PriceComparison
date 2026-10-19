"""Checks every product link on a schedule and reports what changed."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from .extract import ExtractError
from .models import O_ERROR, O_OK, O_OUT, Event, Offer
from .scraper import FetchError, Fetcher
from .store import Store

log = logging.getLogger("pricecomparison")
TICK_S = 30
FAIL_REPORT = 2  # consecutive failures before an error event
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fmt(v: int | None) -> str:
    return "—" if v is None else f"{v:,}".replace(",", "٬").translate(_FA) + " تومان"


class Monitor:
    def __init__(self, store: Store, fetcher: Fetcher, broadcast: Callable[[dict], Awaitable[None]] | None = None):
        self.store = store
        self.fetcher = fetcher
        self.broadcast = broadcast
        self.busy: set[str] = set()
        self.last_run = 0.0

    # ---- events --------------------------------------------------------
    async def emit(self, level: str, kind: str, message: str, **refs: str) -> None:
        ev = Event(level=level, kind=kind, message=message, refs=refs)
        self.store.events.append(ev)
        del self.store.events[:-1000]
        if self.broadcast:
            await self.broadcast({"type": "event", "event": ev.to_dict()})

    async def changed(self) -> None:
        self.store.save()
        if self.broadcast:
            await self.broadcast({"type": "state"})

    # ---- checks --------------------------------------------------------
    def names(self, o: Offer) -> tuple[str, str]:
        p = self.store.products.get(o.product_id)
        s = self.store.sites.get(o.site_id)
        return (p.name if p else "?"), (s.name if s else "?")

    async def check_offer(self, o: Offer) -> None:
        site = self.store.sites.get(o.site_id)
        if site is None or o.id in self.busy:
            return
        self.busy.add(o.id)
        pname, sname = self.names(o)
        before_price, before_status = o.price, o.status
        try:
            res = await self.fetcher.read(o.url, site, self.store.settings)
        except (ExtractError, FetchError) as e:
            o.status, o.error, o.fail_count = O_ERROR, str(e), o.fail_count + 1
            if o.fail_count == FAIL_REPORT:
                await self.emit("error", "offer", f"قیمت «{pname}» از {sname} خوانده نشد: {e}", offer=o.id, product=o.product_id)
        except Exception as e:  # a bug in a reader must not stop the loop
            log.exception("reading %s failed", o.url)
            o.status, o.error, o.fail_count = O_ERROR, f"خطای داخلی: {e}", o.fail_count + 1
        else:
            if before_status == O_ERROR and o.fail_count >= FAIL_REPORT:
                await self.emit("success", "offer", f"قیمت «{pname}» از {sname} دوباره خوانده شد", offer=o.id, product=o.product_id)
            o.error, o.fail_count, o.method, o.last_ok = "", 0, res.method, time.time()
            o.old_price = res.old_price
            if res.available and res.price:
                o.status, o.price = O_OK, res.price
            else:
                o.status, o.price = O_OUT, None
            o.record(time.time(), o.price)
            await self.report_change(o, before_price, before_status, pname, sname)
        finally:
            o.last_check = time.time()
            self.busy.discard(o.id)
        await self.changed()

    async def report_change(self, o: Offer, before: int | None, before_status: str, pname: str, sname: str) -> None:
        refs = {"offer": o.id, "product": o.product_id}
        if before_status in ("pending", O_ERROR) and before is None:
            pass  # first reading: nothing to compare with
        elif o.price and before and o.price != before:
            pct = (o.price - before) / before * 100
            level = "success" if pct < 0 else "warning"
            word = "کاهش" if pct < 0 else "افزایش"
            await self.emit(level, "price", f"«{pname}» در {sname}: {fmt(before)} ← {fmt(o.price)} ({word} {abs(pct):.1f}٪)", **refs)
        elif o.status == O_OUT and before_status == O_OK:
            await self.emit("warning", "stock", f"«{pname}» در {sname} ناموجود شد", **refs)
        elif o.status == O_OK and before_status == O_OUT:
            await self.emit("success", "stock", f"«{pname}» در {sname} دوباره موجود شد: {fmt(o.price)}", **refs)

        p = self.store.products.get(o.product_id)
        best = self.store.cheapest(o.product_id)
        cheapest: dict[str, Any] = self.store.meta.setdefault("cheapest", {})
        prev = cheapest.get(o.product_id)
        if best and prev and prev != best.site_id:
            bname = self.store.sites[best.site_id].name
            await self.emit("info", "cheapest", f"ارزان‌ترین فروشنده «{pname}» حالا {bname} است: {fmt(best.price)}", **refs)
        cheapest[o.product_id] = best.site_id if best else None
        if p and p.target_price and o.price and o.price <= p.target_price and not (before and before <= p.target_price):
            await self.emit("success", "target", f"«{pname}» در {sname} به قیمت هدف رسید: {fmt(o.price)}", **refs)

    async def check_many(self, offers: list[Offer]) -> None:
        sem = asyncio.Semaphore(max(1, self.store.settings.concurrency))

        async def one(o: Offer) -> None:
            async with sem:
                await self.check_offer(o)

        await asyncio.gather(*(one(o) for o in offers))
        self.last_run = time.time()
        await self.changed()

    def due(self) -> list[Offer]:
        every = max(5, self.store.settings.interval_min) * 60
        t = time.time()
        out = []
        for o in self.store.offers.values():
            site = self.store.sites.get(o.site_id)
            if o.enabled and site and site.enabled and o.product_id in self.store.products and t - o.last_check >= every:
                out.append(o)
        return out

    async def run(self) -> None:
        while True:
            try:
                due = self.due()
                if due:
                    await self.check_many(due)
            except Exception:
                log.exception("check round failed")
            await asyncio.sleep(TICK_S)
