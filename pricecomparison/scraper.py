"""Fetch shop pages politely and turn them into prices.

Shops with a public JSON API that is steadier than their HTML get a small
adapter here; every other shop goes through the generic readers in
extract.py. Requests to the same shop are spaced out (Settings.host_delay_s).
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Awaitable, Callable, Protocol

import httpx

from .extract import ExtractError, Result, extract, parse_number
from .models import Settings, Site
from .store import host_of


class Fetcher(Protocol):
    async def read(self, url: str, site: Site, settings: Settings) -> Result: ...

    async def close(self) -> None: ...


class FetchError(Exception):
    pass


# ---------------------------------------------------------------- adapters
async def digikala(client: httpx.AsyncClient, url: str) -> Result:
    m = re.search(r"dkp-(\d+)", url)
    if not m:
        raise ExtractError("شناسه محصول (dkp-...) در لینک دیجی‌کالا نیست")
    r = await client.get(f"https://api.digikala.com/v2/product/{m.group(1)}/")
    if r.status_code == 404:
        raise ExtractError("دیجی‌کالا این محصول را پیدا نکرد")
    r.raise_for_status()
    product = (r.json().get("data") or {}).get("product") or {}
    if not product:
        raise ExtractError("پاسخ API دیجی‌کالا محصول نداشت")
    variant = product.get("default_variant") or {}
    if product.get("status") != "marketable" or not variant:
        return Result(None, "api", available=False)
    price = variant.get("price") or {}
    selling = parse_number(price.get("selling_price"))
    if not selling:
        return Result(None, "api", available=False)
    rrp = parse_number(price.get("rrp_price"))
    toman = int(selling // 10)  # the API answers in rial
    old = int(rrp // 10) if rrp and rrp > selling else None
    return Result(toman, "api", True, old)


ADAPTERS: dict[str, Callable[[httpx.AsyncClient, str], Awaitable[Result]]] = {
    "digikala.com": digikala,
}


def adapter_for(url: str):
    host = host_of(url)
    for d, fn in ADAPTERS.items():
        if host == d or host.endswith("." + d):
            return fn
    return None


# ---------------------------------------------------------------- fetcher
class HTTPFetcher:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._client_key: tuple = ()
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    def client(self, s: Settings) -> httpx.AsyncClient:
        key = (s.proxy, s.timeout_s, s.user_agent)
        if self._client is None or key != self._client_key:
            old = self._client
            self._client = httpx.AsyncClient(
                proxy=s.proxy or None,
                timeout=s.timeout_s,
                follow_redirects=True,
                headers={
                    "User-Agent": s.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.6",
                },
            )
            self._client_key = key
            if old is not None:
                asyncio.ensure_future(old.aclose())
        return self._client

    async def _polite(self, host: str, delay: float):
        lock = self._locks.setdefault(host, asyncio.Lock())
        await lock.acquire()
        wait = self._last.get(host, 0) + delay - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        return lock

    async def read(self, url: str, site: Site, settings: Settings) -> Result:
        host = host_of(url)
        lock = await self._polite(host, settings.host_delay_s)
        try:
            client = self.client(settings)
            fn = adapter_for(url)
            if fn and not site.selector:
                return await fn(client, url)
            r = await client.get(url)
            if r.status_code in (403, 429, 503):
                raise FetchError(f"سایت درخواست را رد کرد (HTTP {r.status_code})؛ شاید ربات‌ها را می‌بندد یا IP سرور خارج از ایران است")
            r.raise_for_status()
            return await asyncio.to_thread(extract, r.text, site.unit, site.selector, site.out_selector)
        except httpx.HTTPStatusError as e:
            raise FetchError(f"HTTP {e.response.status_code}") from None
        except httpx.TimeoutException:
            raise FetchError("سایت در زمان مقرر جواب نداد") from None
        except httpx.HTTPError as e:
            raise FetchError(f"اتصال برقرار نشد: {e.__class__.__name__}") from None
        finally:
            self._last[host] = time.monotonic()
            lock.release()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
