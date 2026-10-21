"""Category import: read every page of one or more shop category listings,
group what was found into products, and add the ones the admin keeps."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from . import units
from .catalog import Candidate, Group, group_items, guess_next, parse_listing
from .extract import ExtractError
from .models import O_OK, Offer, Product, Site, new_id
from .scraper import FetchError, Fetcher
from .store import Store, host_of

log = logging.getLogger("pricecomparison")
MAX_PAGES = 100


@dataclass
class Source:
    url: str
    site_id: str
    site_name: str
    status: str = "waiting"  # waiting | reading | done | error
    pages: int = 0
    found: int = 0
    method: str = ""
    title: str = ""
    error: str = ""


@dataclass
class Job:
    sources: list[Source]
    max_pages: int
    id: str = field(default_factory=lambda: new_id("j"))
    status: str = "running"  # running | done | cancelled | applied
    started: float = field(default_factory=time.time)
    finished: float = 0
    groups: list[Group] = field(default_factory=list)
    known: int = 0  # items skipped because their link is already in the panel

    def to_dict(self, groups: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if not groups:
            d["groups"] = []
        d["group_count"] = len(self.groups)
        return d


class Importer:
    def __init__(self, store: Store, fetcher: Fetcher, broadcast: Callable[[dict], Awaitable[None]] | None = None):
        self.store = store
        self.fetcher = fetcher
        self.broadcast = broadcast
        self.jobs: dict[str, Job] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    async def _send(self, job: Job, groups: bool = False) -> None:
        if self.broadcast:
            await self.broadcast({"type": "import", "job": job.to_dict(groups)})

    def start(self, urls: list[str], max_pages: int) -> Job:
        sources = []
        for url in urls:
            site = self.store.site_for_url(url)
            if site is None:
                site = Site(name=host_of(url), domains=[host_of(url)])
                self.store.sites[site.id] = site
            sources.append(Source(url=url, site_id=site.id, site_name=site.name))
        job = Job(sources=sources, max_pages=max(1, min(max_pages, MAX_PAGES)))
        for old in [j for j in self.jobs.values() if j.status != "running"][:-4]:
            self.jobs.pop(old.id, None)  # keep a few finished jobs only
        self.jobs[job.id] = job
        self.tasks[job.id] = asyncio.create_task(self.run(job))
        return job

    def cancel(self, job: Job) -> None:
        t = self.tasks.get(job.id)
        if t and not t.done():
            t.cancel()
        job.status = "cancelled"

    async def crawl(self, job: Job, src: Source, out: list[Candidate]) -> None:
        site = self.store.sites.get(src.site_id)
        if site is None:
            return
        src.status = "reading"
        seen_pages: set[str] = set()
        seen_items: set[str] = set()
        url: str | None = src.url
        while url and src.pages < job.max_pages and url not in seen_pages:
            seen_pages.add(url)
            try:
                html = await self.fetcher.page(url, self.store.settings)
            except (FetchError, ExtractError) as e:
                if src.pages == 0:
                    src.status, src.error = "error", str(e)
                    await self._send(job)
                    return
                break  # a guessed page that does not exist ends the listing
            listing = await asyncio.to_thread(parse_listing, html, url, site.unit, site.list_selector, site.next_selector)
            src.pages += 1
            src.method = src.method or listing.method
            src.title = src.title or listing.title
            new = [it for it in listing.items if it.url not in seen_items]
            for it in new:
                seen_items.add(it.url)
                q = units.resolve(it.name, it.near)
                out.append(Candidate(name=it.name, url=it.url, site_id=site.id, price=it.price, qty=q.to_dict() if q else None))
            src.found = len(seen_items)
            await self._send(job)
            if not new:
                break
            url = listing.next_url or guess_next(src.url, src.pages + 1)
        src.status = "done" if src.found else "error"
        if not src.found and not src.error:
            src.error = "محصولی در این صفحه پیدا نشد؛ برای این سایت «selector لینک محصول در دسته‌بندی» بگذارید"

    async def run(self, job: Job) -> None:
        found: list[Candidate] = []
        try:
            await asyncio.gather(*(self.crawl(job, s, found) for s in job.sources))
            existing = {o.url for o in self.store.offers.values()}
            fresh = [c for c in found if c.url not in existing]
            job.known = len(found) - len(fresh)
            products = [(p.id, [p.name, *p.aliases]) for p in self.store.products.values()]
            taken = {(o.product_id, o.site_id) for o in self.store.offers.values()}
            job.groups = await asyncio.to_thread(group_items, fresh, products, taken)
            job.status = "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as e:
            log.exception("import failed")
            job.status = "done"
            for s in job.sources:
                if s.status == "reading":
                    s.status, s.error = "error", f"خطای داخلی: {e}"
        finally:
            job.finished = time.time()
            self.store.save()  # sites added for new domains
            await self._send(job, groups=True)

    def apply(self, job: Job, picks: list[dict], category: str) -> tuple[int, int, list[Offer]]:
        """picks: [{"index": group index, "name": str, "urls": [kept item urls]}].
        Returns (new products, links added, the new offers)."""
        created, offers = 0, []
        existing = {o.url for o in self.store.offers.values()}
        for pick in picks:
            i = pick.get("index")
            if not isinstance(i, int) or not 0 <= i < len(job.groups):
                continue
            g = job.groups[i]
            keep = [c for c in g.items if c.url in set(pick.get("urls") or []) and c.url not in existing]
            if not keep:
                continue
            p = self.store.products.get(g.product_id or "")
            if p is None:
                name = str(pick.get("name") or g.name).strip()[:120] or g.name
                p = Product(name=name, category=category[:40])
                self.store.products[p.id] = p
                created += 1
            for c in keep:
                if c.name not in p.aliases and c.name != p.name:
                    p.aliases.append(c.name)
                o = Offer(product_id=p.id, site_id=c.site_id, url=c.url, title=c.name, qty=c.qty)
                if c.price:
                    # the listing already shows a price: chart it right away;
                    # the next scheduled check reads the product page itself
                    o.price, o.status, o.method = c.price, O_OK, "listing"
                    o.record(time.time(), c.price)
                self.store.offers[o.id] = o
                existing.add(c.url)
                offers.append(o)
        job.status = "applied"
        return created, len(offers), offers
