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
from .auth import ADMIN, COOKIE, ROLES, SESSION_TTL, USER, Auth, AuthError
from . import units
from .extract import ExtractError
from .importer import Importer
from .models import UNITS, Folder, Offer, Product, Site
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


class UnitIn(BaseModel):
    text: str = Field("", max_length=80)  # "کیسه ۵۰ کیلویی", "هر تن"; empty = read it from the shop again


class RefIn(BaseModel):
    dim: str = ""  # empty = choose automatically
    amount: float = Field(1, gt=0)
    label: str = Field("", max_length=60)


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


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field("", max_length=200)  # empty: keep the current one (when editing)
    role: str | None = None  # None: a user when adding, unchanged when editing

    @field_validator("role")
    @classmethod
    def _r(cls, v):
        if v is not None and v not in ROLES:
            raise ValueError("role")
        return v


class PasswordIn(BaseModel):
    old: str
    new: str = Field(min_length=6, max_length=200)


class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class FolderItemsIn(BaseModel):
    product_ids: list[str] = Field(max_length=5000)
    add: bool = True  # False: take them out


# ------------------------------------------------------------------ app
class Hub:
    """Live WebSocket clients, each with its session cookie: a message can be
    shaped per person, and a session that ended (account deleted, password
    or role changed) is closed instead of fed."""

    def __init__(self):
        self.clients: dict[WebSocket, str] = {}

    async def send(self, msg: dict, verify, shape=None) -> None:
        dead = []
        for ws, token in list(self.clients.items()):
            user = verify(token)
            try:
                if user is None:
                    await ws.close(code=4401)
                    dead.append(ws)
                    continue
                await ws.send_json(shape(user) if shape else msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.pop(ws, None)


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

    def need_auth(request: Request) -> dict:
        user = auth.verify(request.cookies.get(COOKIE))
        if user is None:
            raise HTTPException(401, "login required")
        return user

    def need_admin(user: dict = Depends(need_auth)) -> dict:
        if user["role"] != ADMIN:
            raise HTTPException(403, "این کار فقط برای مدیر مجاز است")
        return user

    A = [Depends(need_auth)]  # anyone logged in: look, check prices, own folders
    AD = [Depends(need_admin)]  # change products, links, sites, settings, users

    def get(coll: dict, key: str):
        obj = coll.get(key)
        if obj is None:
            raise HTTPException(404, "not found")
        return obj

    # ---- auth --------------------------------------------------------
    @app.get("/api/session")
    async def session(request: Request):
        user = auth.verify(request.cookies.get(COOKIE))
        return {
            "configured": auth.configured,
            "logged_in": user is not None,
            "user": user,
            "version": __version__,
        }

    def set_cookie(resp: Response, request: Request, username: str) -> None:
        secure = request.url.scheme == "https"
        resp.set_cookie(COOKIE, auth.issue(username), max_age=SESSION_TTL, httponly=True, samesite="strict", secure=secure)

    @app.post("/api/setup")
    async def setup(body: SetupIn, request: Request, response: Response):
        if auth.configured:
            raise HTTPException(409, "already configured")
        auth.set_password(body.username, body.password, ADMIN)
        set_cookie(response, request, body.username.strip())
        return {"ok": True}

    @app.post("/api/login")
    async def login(body: LoginIn, request: Request, response: Response):
        name = body.username.strip()
        if not auth.check(name, body.password):
            await asyncio.sleep(1)
            raise HTTPException(401, "نام کاربری یا رمز عبور اشتباه است")
        set_cookie(response, request, name)
        return {"ok": True}

    @app.put("/api/me/password")
    async def change_own_password(body: PasswordIn, request: Request, response: Response, user: dict = Depends(need_auth)):
        if not auth.check(user["username"], body.old):
            await asyncio.sleep(1)
            raise HTTPException(403, "رمز فعلی درست نیست")
        auth.set_password(user["username"], body.new)
        set_cookie(response, request, user["username"])  # this browser stays logged in, others are logged out
        return {"ok": True}

    # ---- users (admin) ---------------------------------------------------
    @app.get("/api/users", dependencies=AD)
    async def list_users():
        return auth.list()

    @app.post("/api/users", dependencies=AD)
    async def add_user(body: UserIn):
        name = body.username.strip()
        if name in auth.users:
            raise HTTPException(409, "این نام کاربری وجود دارد")
        try:
            auth.set_password(name, body.password, body.role or USER)
        except AuthError as e:
            raise HTTPException(422, str(e)) from None
        await mon.emit("info", "user", f"کاربر «{name}» ({'مدیر' if body.role == ADMIN else 'کاربر'}) ساخته شد")
        return {"ok": True}

    @app.put("/api/users/{name}", dependencies=AD)
    async def edit_user(name: str, body: UserIn):
        if name not in auth.users:
            raise HTTPException(404, "not found")
        try:
            if body.password:
                auth.set_password(name, body.password)
            if body.role is not None and body.role != auth.role(name):
                auth.set_role(name, body.role)
        except AuthError as e:
            raise HTTPException(422, str(e)) from None
        await broadcast({"type": "state"})  # ends that person's sessions right away
        return {"ok": True}

    @app.delete("/api/users/{name}")
    async def del_user(name: str, me: dict = Depends(need_admin)):
        if name == me["username"]:
            raise HTTPException(422, "حساب خودتان را نمی‌توانید حذف کنید")
        try:
            auth.delete(name)
        except AuthError as e:
            raise HTTPException(422, str(e)) from None
        for f in [f for f in store.folders.values() if f.owner == name]:
            store.folders.pop(f.id, None)
        await mon.emit("warning", "user", f"کاربر «{name}» حذف شد")
        await mon.changed()
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    # ---- state -------------------------------------------------------
    def shared_state() -> dict[str, Any]:
        """What everyone sees; full_state() adds what is theirs."""
        return {
            **store.snapshot(),
            "busy": sorted(mon.busy),
            "last_run": mon.last_run,
            "version": __version__,
            "demo": demo,
            "now": time.time(),
        }

    def full_state(user: dict, shared: dict[str, Any] | None = None) -> dict[str, Any]:
        admin = user["role"] == ADMIN
        st = {
            **(shared or shared_state()),
            "me": user,
            "folders": [f.to_dict() for f in store.folders_of(user["username"])],
            # a user only needs to know how often prices are read, not the proxy and the rest
            "settings": store.settings.to_dict() if admin else {"interval_min": store.settings.interval_min},
            "import": None,
        }
        if admin:
            job = max(imp.jobs.values(), key=lambda j: j.started, default=None)
            st["import"] = job.to_dict(groups=False) if job else None
        return st

    @app.get("/api/state")
    async def state(user: dict = Depends(need_auth)):
        return {**full_state(user), "events": [e.to_dict() for e in store.events[-200:]]}

    async def broadcast(msg: dict) -> None:
        if msg.get("type") == "state":
            shared = shared_state()
            await hub.send(msg, auth.verify, lambda user: {"type": "state", "state": full_state(user, shared)})
        elif msg.get("type") == "import":
            await hub.send(msg, auth.verify, lambda user: msg if user["role"] == ADMIN else {"type": "noop"})
        else:
            await hub.send(msg, auth.verify)

    mon.broadcast = broadcast
    imp.broadcast = broadcast

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        token = websocket.cookies.get(COOKIE) or ""
        user = auth.verify(token)
        if user is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        hub.clients[websocket] = token
        try:
            await websocket.send_json({"type": "state", "state": full_state(user)})
            while True:
                await websocket.receive_text()  # keepalive pings from the page
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.pop(websocket, None)

    # ---- folders (each person's own) -------------------------------------
    def my_folder(fid: str, user: dict) -> Folder:
        f = store.folders.get(fid)
        if f is None or f.owner != user["username"]:
            raise HTTPException(404, "not found")
        return f

    @app.post("/api/folders")
    async def add_folder(body: FolderIn, user: dict = Depends(need_auth)):
        name = body.name.strip()
        if any(f.name == name for f in store.folders_of(user["username"])):
            raise HTTPException(409, "پوشه‌ای با این نام دارید")
        f = Folder(owner=user["username"], name=name)
        store.folders[f.id] = f
        await mon.changed()
        return f.to_dict()

    @app.put("/api/folders/{fid}")
    async def rename_folder(fid: str, body: FolderIn, user: dict = Depends(need_auth)):
        f = my_folder(fid, user)
        f.name = body.name.strip()
        await mon.changed()
        return f.to_dict()

    @app.delete("/api/folders/{fid}")
    async def del_folder(fid: str, user: dict = Depends(need_auth)):
        store.folders.pop(my_folder(fid, user).id, None)
        await mon.changed()
        return {"ok": True}

    @app.post("/api/folders/{fid}/products")
    async def folder_products(fid: str, body: FolderItemsIn, user: dict = Depends(need_auth)):
        f = my_folder(fid, user)
        ids = [i for i in body.product_ids if i in store.products]
        if body.add:
            f.product_ids += [i for i in dict.fromkeys(ids) if i not in f.product_ids]
        else:
            f.product_ids = [i for i in f.product_ids if i not in set(ids)]
        await mon.changed()
        return f.to_dict()

    # ---- sites -------------------------------------------------------
    def domain_taken(domains: list[str], skip: str = "") -> None:
        for s in store.sites.values():
            if s.id != skip and set(s.domains) & set(domains):
                raise HTTPException(409, f"دامنه {', '.join(set(s.domains) & set(domains))} قبلا برای «{s.name}» ثبت شده")

    @app.post("/api/sites", dependencies=AD)
    async def add_site(body: SiteIn):
        domain_taken(body.domains)
        s = Site(**body.model_dump())
        store.sites[s.id] = s
        await mon.emit("info", "site", f"سایت «{s.name}» اضافه شد")
        await mon.changed()
        return s.to_dict()

    @app.put("/api/sites/{sid}", dependencies=AD)
    async def edit_site(sid: str, body: SiteIn):
        s = get(store.sites, sid)
        domain_taken(body.domains, skip=sid)
        for k, v in body.model_dump().items():
            setattr(s, k, v)
        await mon.changed()
        return s.to_dict()

    @app.delete("/api/sites/{sid}", dependencies=AD)
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

    @app.post("/api/test", dependencies=AD)
    async def test_url(body: TestIn):
        """Read a link once with the given settings, without saving anything."""
        site = store.site_for_url(body.url) or Site(name="?", domains=[host_of(body.url)])
        probe = Site(name=site.name, domains=site.domains, unit=body.unit, selector=body.selector, out_selector=body.out_selector)
        try:
            r = await fetcher.read(body.url, probe, store.settings)
        except (ExtractError, FetchError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "price": r.price, "old_price": r.old_price, "available": r.available, "method": r.method, "guess": r.guess,
                "title": r.title, "unit": r.qty}

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

    @app.post("/api/products", dependencies=AD)
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

    @app.put("/api/products/{pid}", dependencies=AD)
    async def edit_product(pid: str, body: ProductIn):
        p = get(store.products, pid)
        p.name, p.category, p.note, p.target_price = body.name, body.category, body.note, body.target_price or None
        await mon.changed()
        return p.to_dict()

    @app.delete("/api/products/{pid}", dependencies=AD)
    async def del_product(pid: str):
        p = store.products.pop(pid, None)
        if p is None:
            raise HTTPException(404, "not found")
        for o in store.offers_of(pid):
            store.offers.pop(o.id, None)
        store.meta.get("cheapest", {}).pop(pid, None)
        store.forget_products({pid})
        await mon.emit("warning", "product", f"محصول «{p.name}» حذف شد")
        await mon.changed()
        return {"ok": True}

    @app.post("/api/products/delete", dependencies=AD)
    async def del_products(body: IdsIn):
        gone = [store.products.pop(pid) for pid in body.ids if pid in store.products]
        ids = {p.id for p in gone}
        for o in [o for o in store.offers.values() if o.product_id in ids]:
            store.offers.pop(o.id, None)
        for pid in ids:
            store.meta.get("cheapest", {}).pop(pid, None)
        store.forget_products(ids)
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
    @app.post("/api/offers", dependencies=AD)
    async def new_offer(body: OfferIn):
        p = get(store.products, body.product_id)
        o = await add_offer(p, body.url, body.site_id, body.create_site)
        await mon.changed()
        background(mon.check_offer(o))
        return o.to_dict()

    @app.put("/api/offers/{oid}", dependencies=AD)
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

    @app.delete("/api/offers/{oid}", dependencies=AD)
    async def del_offer(oid: str):
        if store.offers.pop(oid, None) is None:
            raise HTTPException(404, "not found")
        await mon.changed()
        return {"ok": True}

    @app.post("/api/units/parse", dependencies=AD)
    async def parse_unit(body: UnitIn, offer_id: str = ""):
        o = store.offers.get(offer_id)
        p = store.products.get(o.product_id) if o else None
        q = units.parse_input(body.text, (o.title if o else "") or (p.name if p else ""))
        return {"unit": q.to_dict() if q else None}

    @app.put("/api/offers/{oid}/unit", dependencies=AD)
    async def set_offer_unit(oid: str, body: UnitIn):
        o = get(store.offers, oid)
        if not body.text.strip():
            o.qty_manual = None
        else:
            p = store.products.get(o.product_id)
            q = units.parse_input(body.text, o.title or (p.name if p else ""))
            if q is None:
                raise HTTPException(422, "واحد را نشناختم؛ مثلا بنویسید «کیسه ۵۰ کیلویی»، «هر تن»، «شاخه ۱۲ متری» یا «عدد»")
            o.qty_manual = q.to_dict()
        await mon.changed()
        return {"unit": o.qty_manual or o.qty}

    @app.put("/api/products/{pid}/ref", dependencies=AD)
    async def set_product_ref(pid: str, body: RefIn):
        p = get(store.products, pid)
        if not body.dim:
            p.ref_unit = None
        else:
            if body.dim not in units.BASE_FA:
                raise HTTPException(422, "unknown unit")
            p.ref_unit = units.Qty(body.dim, body.amount, body.label or units.label_of(body.dim, body.amount), units.MANUAL).to_dict()
        await mon.changed()
        return {"ok": True}

    @app.post("/api/offers/{oid}/check", dependencies=A)
    async def check_offer(oid: str):
        o = get(store.offers, oid)
        await mon.check_offer(o)
        return o.to_dict()

    @app.post("/api/check", dependencies=AD)
    async def check_all():
        offers = [
            o for o in store.offers.values()
            if o.enabled and o.product_id in store.products and store.sites.get(o.site_id, Site("", [])).enabled
        ]
        background(mon.check_many(offers))
        return {"ok": True, "count": len(offers)}

    # ---- category import ---------------------------------------------
    @app.post("/api/import", dependencies=AD)
    async def start_import(body: ImportIn):
        if any(j.status == "running" for j in imp.jobs.values()):
            raise HTTPException(409, "یک ورود از دسته‌بندی در جریان است؛ تا تمام شدنش صبر کنید")
        job = imp.start(body.urls, body.max_pages)
        await mon.emit("info", "import", f"خواندن {len(job.sources)} صفحه دسته‌بندی شروع شد")
        await mon.changed()
        return job.to_dict()

    @app.get("/api/import/{jid}", dependencies=AD)
    async def get_import(jid: str):
        return get(imp.jobs, jid).to_dict()

    @app.post("/api/import/{jid}/cancel", dependencies=AD)
    async def cancel_import(jid: str):
        imp.cancel(get(imp.jobs, jid))
        return {"ok": True}

    @app.post("/api/import/{jid}/apply", dependencies=AD)
    async def apply_import(jid: str, body: ApplyIn):
        job = get(imp.jobs, jid)
        if job.status != "done":
            raise HTTPException(409, "این ورود هنوز تمام نشده یا قبلا اعمال شده")
        created, linked, _ = imp.apply(job, [g.model_dump() for g in body.groups], body.category.strip())
        await mon.emit("success", "import", f"از صفحه‌های دسته‌بندی {created} محصول جدید و {linked} لینک اضافه شد")
        await mon.changed()
        return {"created": created, "linked": linked}

    # ---- settings ----------------------------------------------------
    @app.get("/api/settings", dependencies=AD)
    async def get_settings():
        return store.settings.to_dict()

    @app.put("/api/settings", dependencies=AD)
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
