"""FastAPI application: REST API, live WebSocket feed and the dashboard."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .auth import COOKIE, SESSION_TTL, Auth
from .extract import ExtractError
from .importer import Importer
from .models import UNITS, Offer, Product, Site
from .monitor import Monitor
from .scraper import FetchError, Fetcher, HTTPFetcher
from .sites import seed_sites
from .store import Store, clean_domain, host_of

log = logging.getLogger("pricecomparison")
STATIC = Path(__file__).parent / "static"


def _url(v: str) -> str:
    v = v.strip()
    if not v.startswith(("http://", "https://")) or not host_of(v):
        raise ValueError("لینک باید با http:// یا https:// شروع شود")
    return v


# ---------------------------------------------------------------- inputs
class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    domains: list[str] = Field(min_length=1)
    enabled: bool = True
    unit: str = "auto"
    selector: str = Field("", max_length=300)
    out_selector: str = Field("", max_length=300)
    list_selector: str = Field("", max_length=300)
    next_selector: str = Field("", max_length=300)
    note: str = Field("", max_length=200)

    @field_validator("domains")
    @classmethod
    def _d(cls, v):
        out = []
        for item in v:
            for d in str(item).replace("\n", ",").split(","):
                d = clean_domain(d)
                if d and d not in out:
                    if "." not in d:
                        raise ValueError(f"دامنه نامعتبر: {d}")
                    out.append(d)
        if not out:
            raise ValueError("حداقل یک دامنه لازم است")
        return out

    @field_validator("unit")
    @classmethod
    def _u(cls, v):
        if v not in UNITS:
            raise ValueError("unit")
        return v


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field("", max_length=40)
    note: str = Field("", max_length=300)
    target_price: int | None = Field(None, ge=0)
    urls: list[str] = []  # links to add with a new product

    @field_validator("urls")
    @classmethod
    def _u(cls, v):
        return [_url(x) for x in v if x.strip()]


class OfferIn(BaseModel):
    product_id: str
    url: str
    site_id: str = ""  # empty: found from the link's domain
    create_site: bool = False  # no site has the domain: add one for it

    @field_validator("url")
    @classmethod
    def _u(cls, v):
        return _url(v)


class OfferEdit(BaseModel):
    url: str | None = None
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def _u(cls, v):
        return None if v is None else _url(v)


class TestIn(BaseModel):
    url: str
    unit: str = "auto"
    selector: str = ""
    out_selector: str = ""

    @field_validator("url")
    @classmethod
    def _u(cls, v):
        return _url(v)


class ImportIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=20)
    max_pages: int = Field(20, ge=1, le=100)

    @field_validator("urls")
    @classmethod
    def _u(cls, v):
        out = [_url(x) for x in v if x.strip()]
        if not out:
            raise ValueError("حداقل یک لینک لازم است")
        return out


class PickIn(BaseModel):
    index: int
    name: str = Field("", max_length=120)
    urls: list[str] = []


class ApplyIn(BaseModel):
    category: str = Field("", max_length=40)
    groups: list[PickIn]


class SettingsIn(BaseModel):
    interval_min: int = Field(360, ge=5, le=7 * 24 * 60)
    timeout_s: int = Field(20, ge=5, le=120)
    host_delay_s: float = Field(3.0, ge=0, le=120)
    concurrency: int = Field(4, ge=1, le=20)
    proxy: str = Field("", max_length=200)
    user_agent: str = Field(min_length=10, max_length=400)


class IdsIn(BaseModel):
    ids: list[str] = Field(max_length=5000)


class LoginIn(BaseModel):
    username: str
    password: str


class SetupIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=6)


# ------------------------------------------------------------------ app
class Hub:
    """Live WebSocket clients."""

    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def send(self, msg: dict) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


def create_app(data_dir: str | Path | None = None, fetcher: Fetcher | None = None, demo: bool = False) -> FastAPI:
    data = Path(data_dir or os.environ.get("PC_DATA_DIR", "data")).resolve()
    data.mkdir(parents=True, exist_ok=True)
    store = Store(data / "state.json")
    auth = Auth(data / "auth.json")
    hub = Hub()
    if demo:
        from .demo import SimShops, seed

        seed(store)
        fetcher = SimShops(store)
    else:
        seed_sites(store)
        fetcher = fetcher or HTTPFetcher()
    mon = Monitor(store, fetcher)
    imp = Importer(store, fetcher)
    monitor_enabled = os.environ.get("PC_NO_MONITOR") != "1"
    tasks: set[asyncio.Task] = set()

    def background(coro) -> None:
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.create_task(mon.run()) if monitor_enabled else None
        await mon.emit("info", "panel", f"پنل مقایسه قیمت {__version__} راه‌اندازی شد" + (" (حالت دمو)" if demo else ""))
        yield
        if loop:
            loop.cancel()
        for t in [*tasks, *imp.tasks.values()]:
            t.cancel()
        await fetcher.close()
        store.save()

    app = FastAPI(title="PriceComparison", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.monitor, app.state.auth = store, mon, auth

    def need_auth(request: Request) -> None:
        if not auth.verify(request.cookies.get(COOKIE)):
            raise HTTPException(401, "login required")

    A = [Depends(need_auth)]

    def get(coll: dict, key: str):
        obj = coll.get(key)
        if obj is None:
            raise HTTPException(404, "not found")
        return obj

    # ---- auth --------------------------------------------------------
    @app.get("/api/session")
    async def session(request: Request):
        return {
            "configured": auth.configured,
            "logged_in": auth.verify(request.cookies.get(COOKIE)),
            "username": auth.username if auth.configured else "",
            "version": __version__,
        }

    def set_cookie(resp: Response, request: Request) -> None:
        secure = request.url.scheme == "https"
        resp.set_cookie(COOKIE, auth.issue(), max_age=SESSION_TTL, httponly=True, samesite="strict", secure=secure)

    @app.post("/api/setup")
    async def setup(body: SetupIn, request: Request, response: Response):
        if auth.configured:
            raise HTTPException(409, "already configured")
        auth.set_password(body.username, body.password)
        set_cookie(response, request)
        return {"ok": True}

    @app.post("/api/login")
    async def login(body: LoginIn, request: Request, response: Response):
        if not auth.check(body.username, body.password):
            await asyncio.sleep(1)
            raise HTTPException(401, "نام کاربری یا رمز عبور اشتباه است")
        set_cookie(response, request)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    # ---- state -------------------------------------------------------
    def full_state() -> dict[str, Any]:
        return {
            **store.snapshot(),
            "settings": store.settings.to_dict(),
            "busy": sorted(mon.busy),
            "last_run": mon.last_run,
            "import": (lambda j: j.to_dict(groups=False) if j else None)(max(imp.jobs.values(), key=lambda j: j.started, default=None)),
            "version": __version__,
            "demo": demo,
            "now": time.time(),
        }

    @app.get("/api/state", dependencies=A)
    async def state():
        return {**full_state(), "events": [e.to_dict() for e in store.events[-200:]]}

    async def broadcast(msg: dict) -> None:
        if msg.get("type") == "state":
            msg = {"type": "state", "state": full_state()}
        await hub.send(msg)

    mon.broadcast = broadcast
    imp.broadcast = broadcast

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        if not auth.verify(websocket.cookies.get(COOKIE)):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        hub.clients.add(websocket)
        try:
            await websocket.send_json({"type": "state", "state": full_state()})
            while True:
                await websocket.receive_text()  # keepalive pings from the page
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(websocket)

    # ---- sites -------------------------------------------------------
    def domain_taken(domains: list[str], skip: str = "") -> None:
        for s in store.sites.values():
            if s.id != skip and set(s.domains) & set(domains):
                raise HTTPException(409, f"دامنه {', '.join(set(s.domains) & set(domains))} قبلا برای «{s.name}» ثبت شده")

    @app.post("/api/sites", dependencies=A)
    async def add_site(body: SiteIn):
        domain_taken(body.domains)
        s = Site(**body.model_dump())
        store.sites[s.id] = s
        await mon.emit("info", "site", f"سایت «{s.name}» اضافه شد")
        await mon.changed()
        return s.to_dict()

    @app.put("/api/sites/{sid}", dependencies=A)
    async def edit_site(sid: str, body: SiteIn):
        s = get(store.sites, sid)
        domain_taken(body.domains, skip=sid)
        for k, v in body.model_dump().items():
            setattr(s, k, v)
        await mon.changed()
        return s.to_dict()

    @app.delete("/api/sites/{sid}", dependencies=A)
    async def del_site(sid: str):
        s = store.sites.pop(sid, None)
        if s is None:
            raise HTTPException(404, "not found")
        gone = [o for o in store.offers.values() if o.site_id == sid]
        for o in gone:
            store.offers.pop(o.id, None)
        await mon.emit("warning", "site", f"سایت «{s.name}» و {len(gone)} لینک آن حذف شد")
        await mon.changed()
        return {"ok": True}

    @app.post("/api/test", dependencies=A)
    async def test_url(body: TestIn):
        """Read a link once with the given settings, without saving anything."""
        site = store.site_for_url(body.url) or Site(name="?", domains=[host_of(body.url)])
        probe = Site(name=site.name, domains=site.domains, unit=body.unit, selector=body.selector, out_selector=body.out_selector)
        try:
            r = await fetcher.read(body.url, probe, store.settings)
        except (ExtractError, FetchError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "price": r.price, "old_price": r.old_price, "available": r.available, "method": r.method, "guess": r.guess}

    # ---- products ----------------------------------------------------
    async def add_offer(product: Product, url: str, site_id: str = "", create_site: bool = False) -> Offer:
        site = store.sites.get(site_id) if site_id else store.site_for_url(url)
        if site is None:
            if not create_site:
                raise HTTPException(422, f"سایتی برای دامنه {host_of(url)} تعریف نشده؛ اول آن را در بخش سایت‌ها اضافه کنید")
            site = Site(name=host_of(url), domains=[host_of(url)])
            store.sites[site.id] = site
            await mon.emit("info", "site", f"سایت «{site.name}» برای لینک جدید اضافه شد")
        for o in store.offers_of(product.id):
            if o.site_id == site.id and o.url == url:
                raise HTTPException(409, "این لینک قبلا اضافه شده")
        o = Offer(product_id=product.id, site_id=site.id, url=url)
        store.offers[o.id] = o
        return o

    @app.post("/api/products", dependencies=A)
    async def add_product(body: ProductIn):
        p = Product(name=body.name, category=body.category, note=body.note, target_price=body.target_price or None)
        new, skipped = [], []
        for url in body.urls:
            try:
                new.append(await add_offer(p, url, create_site=True))
            except HTTPException as e:
                skipped.append(f"{url}: {e.detail}")
        store.products[p.id] = p
        await mon.emit("info", "product", f"محصول «{p.name}» با {len(new)} لینک اضافه شد", product=p.id)
        await mon.changed()
        if new:
            background(mon.check_many(new))
        return {**p.to_dict(), "skipped": skipped}

    @app.put("/api/products/{pid}", dependencies=A)
    async def edit_product(pid: str, body: ProductIn):
        p = get(store.products, pid)
        p.name, p.category, p.note, p.target_price = body.name, body.category, body.note, body.target_price or None
        await mon.changed()
        return p.to_dict()

    @app.delete("/api/products/{pid}", dependencies=A)
    async def del_product(pid: str):
        p = store.products.pop(pid, None)
        if p is None:
            raise HTTPException(404, "not found")
        for o in store.offers_of(pid):
            store.offers.pop(o.id, None)
        store.meta.get("cheapest", {}).pop(pid, None)
        await mon.emit("warning", "product", f"محصول «{p.name}» حذف شد")
        await mon.changed()
        return {"ok": True}

    @app.post("/api/products/delete", dependencies=A)
    async def del_products(body: IdsIn):
        gone = [store.products.pop(pid) for pid in body.ids if pid in store.products]
        ids = {p.id for p in gone}
        for o in [o for o in store.offers.values() if o.product_id in ids]:
            store.offers.pop(o.id, None)
        for pid in ids:
            store.meta.get("cheapest", {}).pop(pid, None)
        if gone:
            await mon.emit("warning", "product", f"{len(gone)} محصول حذف شد")
        await mon.changed()
        return {"deleted": len(gone)}

    @app.post("/api/products/{pid}/check", dependencies=A)
    async def check_product(pid: str):
        get(store.products, pid)
        offers = [o for o in store.offers_of(pid) if o.enabled]
        background(mon.check_many(offers))
        return {"ok": True, "count": len(offers)}

    # ---- offers (one product on one site) ------------------------------
    @app.post("/api/offers", dependencies=A)
    async def new_offer(body: OfferIn):
        p = get(store.products, body.product_id)
        o = await add_offer(p, body.url, body.site_id, body.create_site)
        await mon.changed()
        background(mon.check_offer(o))
        return o.to_dict()

    @app.put("/api/offers/{oid}", dependencies=A)
    async def edit_offer(oid: str, body: OfferEdit):
        o = get(store.offers, oid)
        if body.url is not None and body.url != o.url:
            site = store.site_for_url(body.url)
            if site is None or site.id != o.site_id:
                raise HTTPException(422, "لینک جدید باید روی همان سایت باشد؛ برای سایت دیگر لینک تازه اضافه کنید")
            o.url, o.history, o.price, o.status, o.error = body.url, [], None, "pending", ""
            background(mon.check_offer(o))
        if body.enabled is not None:
            o.enabled = body.enabled
        await mon.changed()
        return o.to_dict()

    @app.delete("/api/offers/{oid}", dependencies=A)
    async def del_offer(oid: str):
        if store.offers.pop(oid, None) is None:
            raise HTTPException(404, "not found")
        await mon.changed()
        return {"ok": True}

    @app.post("/api/offers/{oid}/check", dependencies=A)
    async def check_offer(oid: str):
        o = get(store.offers, oid)
        await mon.check_offer(o)
        return o.to_dict()

    @app.post("/api/check", dependencies=A)
    async def check_all():
        offers = [
            o for o in store.offers.values()
            if o.enabled and o.product_id in store.products and store.sites.get(o.site_id, Site("", [])).enabled
        ]
        background(mon.check_many(offers))
        return {"ok": True, "count": len(offers)}

    # ---- category import ---------------------------------------------
    @app.post("/api/import", dependencies=A)
    async def start_import(body: ImportIn):
        if any(j.status == "running" for j in imp.jobs.values()):
            raise HTTPException(409, "یک ورود از دسته‌بندی در جریان است؛ تا تمام شدنش صبر کنید")
        job = imp.start(body.urls, body.max_pages)
        await mon.emit("info", "import", f"خواندن {len(job.sources)} صفحه دسته‌بندی شروع شد")
        await mon.changed()
        return job.to_dict()

    @app.get("/api/import/{jid}", dependencies=A)
    async def get_import(jid: str):
        return get(imp.jobs, jid).to_dict()

    @app.post("/api/import/{jid}/cancel", dependencies=A)
    async def cancel_import(jid: str):
        imp.cancel(get(imp.jobs, jid))
        return {"ok": True}

    @app.post("/api/import/{jid}/apply", dependencies=A)
    async def apply_import(jid: str, body: ApplyIn):
        job = get(imp.jobs, jid)
        if job.status != "done":
            raise HTTPException(409, "این ورود هنوز تمام نشده یا قبلا اعمال شده")
        created, linked, _ = imp.apply(job, [g.model_dump() for g in body.groups], body.category.strip())
        await mon.emit("success", "import", f"از صفحه‌های دسته‌بندی {created} محصول جدید و {linked} لینک اضافه شد")
        await mon.changed()
        return {"created": created, "linked": linked}

    # ---- settings ----------------------------------------------------
    @app.get("/api/settings", dependencies=A)
    async def get_settings():
        return store.settings.to_dict()

    @app.put("/api/settings", dependencies=A)
    async def put_settings(body: SettingsIn):
        for k, v in body.model_dump().items():
            setattr(store.settings, k, v.strip() if isinstance(v, str) else v)
        await mon.changed()
        return store.settings.to_dict()

    # ---- static UI -----------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
