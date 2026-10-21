/* PriceComparison dashboard — vanilla JS, live over WebSocket. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const METHOD = {
  api: "API سایت", jsonld: "داده ساختاریافته", meta: "متا تگ", microdata: "Microdata", woo: "ووکامرس",
  nextdata: "JSON صفحه", selector: "CSS selector", text: "حدس از متن صفحه", listing: "صفحه دسته‌بندی",
};
const UNIT = { auto: "خودکار", toman: "تومان", rial: "ریال" };
const RANGES = [["7", "۷ روز"], ["30", "۳۰ روز"], ["90", "۹۰ روز"], ["all", "همه"]];

let S = null;          // last state from the server
let skew = 0;          // server clock - local clock (s)
let events = [];
let evFilter = "all";
let sel = null;        // selected product id
let range = "30";
let query = "";
const hidden = new Set(); // offer ids hidden in the history chart
let ws = null, wsAlive = false;
try { sel = localStorage.getItem("pc-sel"); range = localStorage.getItem("pc-range") || range; } catch (_) { /* ignore */ }

/* ------------------------------------------------------------------ api */
async function api(method, path, body) {
  const r = await fetch(path, {
    method, credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch (_) { /* empty body */ }
  if (r.status === 401 && path !== "/api/login") { showAuth(); throw new Error("login required"); }
  if (!r.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join(" — ");
    const err = new Error(msg || `HTTP ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return data;
}

function toast(msg, kind = "info") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
}

async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg, "success"); return r; }
  catch (e) { toast(e.message, "error"); throw e; }
}

/* --------------------------------------------------------------- helpers */
const nf = new Intl.NumberFormat("fa-IR");
const nf1 = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 1 });
const toman = (v) => (v == null ? "—" : `${nf.format(v)} تومان`);
const num = (v) => (v == null ? "—" : nf.format(v));
function short(v) {
  if (v == null) return "—";
  if (Math.abs(v) >= 1e9) return `${nf1.format(v / 1e9)} میلیارد`;
  if (Math.abs(v) >= 1e6) return `${nf1.format(v / 1e6)} میلیون`;
  if (Math.abs(v) >= 1e3) return `${nf1.format(v / 1e3)} هزار`;
  return nf.format(v);
}
const pct = (v) => `${nf1.format(v)}٪`;
const site = (id) => S.sites.find((s) => s.id === id);
const product = (id) => S.products.find((p) => p.id === id);
const offersOf = (pid) => S.offers.filter((o) => o.product_id === pid);
const sname = (id) => (site(id) || {}).name || "?";
const nowS = () => Date.now() / 1000 + skew;
const busy = (o) => S.busy.includes(o.id);
// Within a product, each of its shops keeps one colour (shops in the order
// they were added); 8 colours cover any product, however many sites exist.
function siteColor(id, pid) {
  const ids = pid ? S.sites.filter((s) => offersOf(pid).some((o) => o.site_id === s.id)).map((s) => s.id) : S.sites.map((s) => s.id);
  const i = ids.indexOf(id);
  return i >= 0 && i < 8 ? `var(--s${i + 1})` : "var(--muted)";
}
const live = (o) => o.enabled && o.status === "ok" && o.price && (site(o.site_id) || {}).enabled;
function summary(pid) {
  const all = offersOf(pid);
  const ok = all.filter(live).sort((a, b) => a.price - b.price);
  const prices = ok.map((o) => o.price);
  return {
    all, ok, best: ok[0] || null,
    min: prices.length ? prices[0] : null,
    max: prices.length ? prices[prices.length - 1] : null,
    avg: prices.length ? Math.round(prices.reduce((a, b) => a + b, 0) / prices.length) : null,
    errors: all.filter((o) => o.enabled && o.status === "error").length,
  };
}
function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Math.round(nowS() - ts));
  if (s < 60) return `${nf.format(s)} ثانیه پیش`;
  if (s < 3600) return `${nf.format(Math.floor(s / 60))} دقیقه پیش`;
  if (s < 86400) return `${nf.format(Math.floor(s / 3600))} ساعت پیش`;
  return `${nf.format(Math.floor(s / 86400))} روز پیش`;
}
function clock(ts) {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString("fa-IR", { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? t : `${d.toLocaleDateString("fa-IR", { month: "short", day: "numeric" })} ${t}`;
}
const date = (ts, opt = { month: "short", day: "numeric" }) => new Date(ts * 1000).toLocaleDateString("fa-IR", opt);
const ICON = {
  check: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5"/></svg>`,
  open: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>`,
  pause: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5v14M15 5v14"/></svg>`,
  play: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M7 5l12 7-12 7z"/></svg>`,
  del: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>`,
};

/* ------------------------------------------------------------------ boot */
async function boot() {
  let sess;
  try { sess = await api("GET", "/api/session"); } catch (e) { toast(e.message, "error"); return; }
  $("#ver").textContent = `v${sess.version}`;
  if (!sess.logged_in) return showAuth(sess.configured);
  startApp();
}

function showAuth(configured = true) {
  $("#app").classList.add("hidden");
  $("#auth").classList.remove("hidden");
  const setup = !configured;
  $("#auth-sub").textContent = setup ? "اولین اجرا: یک نام کاربری و رمز عبور برای مدیر بسازید" : "ورود به پنل مقایسه قیمت";
  $("#auth-btn").textContent = setup ? "ساخت حساب و ورود" : "ورود";
  $("#auth-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("POST", setup ? "/api/setup" : "/api/login", { username: f.get("username"), password: f.get("password") });
      startApp();
    } catch (err) { $("#auth-err").textContent = err.message; }
  };
}

function startApp() {
  $("#auth").classList.add("hidden");
  $("#app").classList.remove("hidden");
  api("GET", "/api/state").then((st) => {
    events = st.events || [];
    setState(st);
    renderEvents();
  }).catch(() => {});
  connectWS();
}

function connectWS() {
  if (ws) try { ws.close(); } catch (_) { /* ignore */ }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { wsAlive = true; $("#live").classList.add("on"); };
  ws.onclose = (e) => {
    wsAlive = false; $("#live").classList.remove("on");
    if (e.code === 4401) return showAuth();
    setTimeout(connectWS, 2500);
  };
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "state") setState(msg.state);
    else if (msg.type === "event") { events.push(msg.event); events = events.slice(-300); renderEvents(msg.event.id); }
    else if (msg.type === "import") onImport(msg.job);
  };
}
setInterval(() => { if (wsAlive) try { ws.send("ping"); } catch (_) { /* ignore */ } }, 25000);

function setState(st) {
  S = st;
  skew = st.now - Date.now() / 1000;
  if (!product(sel)) sel = S.products.length ? S.products[0].id : null;
  renderAll();
}

function renderAll() {
  if (!S) return;
  renderStats();
  renderList();
  renderDetail();
  renderSites();
}

/* ----------------------------------------------------------------- stats */
function renderStats() {
  const active = S.offers.filter((o) => o.enabled && (site(o.site_id) || {}).enabled);
  const ok = active.filter((o) => o.status === "ok" || o.status === "out").length;
  const err = active.filter((o) => o.status === "error").length;
  const pill = (label, v, col) => `<span class="stat"><span class="d" style="background:${col}"></span>${label} <b>${v}</b></span>`;
  $("#stats").innerHTML =
    pill("محصولات", nf.format(S.products.length), "var(--text)") +
    pill("سایت‌ها", nf.format(S.sites.filter((s) => s.enabled).length), "var(--accent)") +
    pill("لینک‌های سالم", `${nf.format(ok)}/${nf.format(active.length)}`, !active.length ? "var(--pending)" : ok === active.length ? "var(--up)" : "var(--degraded)") +
    (err ? `<span class="stat" style="color:var(--down)">${nf.format(err)} لینک خطادار</span>` : "") +
    (S.busy.length ? `<span class="stat" style="color:var(--testing)"><span class="spin"></span> ${nf.format(S.busy.length)} در حال بررسی</span>` : "") +
    (S.demo ? `<span class="stat">حالت دمو</span>` : "");
}
setInterval(() => {
  if (!S) return;
  $("#last-check").textContent = S.last_run ? `بررسی: ${ago(S.last_run)}` : "در انتظار اولین بررسی";
}, 1000);

/* ---------------------------------------------------------- product list */
function renderList() {
  const q = query.trim().toLowerCase();
  const list = S.products.filter((p) => !q || p.name.toLowerCase().includes(q) || (p.category || "").toLowerCase().includes(q));
  $("#pitems").innerHTML = list.map((p) => {
    const sm = summary(p.id);
    const best = sm.best
      ? `<b>${short(sm.min)}</b><span class="sep">·</span><span>${esc(sname(sm.best.site_id))}</span>`
      : `<span>${sm.all.length ? "قیمتی خوانده نشده" : "بدون لینک"}</span>`;
    return `<button class="pitem ${p.id === sel ? "on" : ""}" data-p="${p.id}">
      <span class="pn">${esc(p.name)}</span>
      <span class="pp">${best}<span class="sep">·</span><span>${nf.format(sm.all.length)} سایت</span>
      ${sm.errors ? `<span class="chip bad">${nf.format(sm.errors)} خطا</span>` : ""}
      ${p.category ? `<span class="chip">${esc(p.category)}</span>` : ""}</span></button>`;
  }).join("") || `<div class="muted small" style="padding:10px">${S.products.length ? "محصولی پیدا نشد" : "هنوز محصولی اضافه نشده"}</div>`;
}
$("#psearch").oninput = (e) => { query = e.target.value; renderList(); };
$("#pitems").onclick = (e) => {
  const b = e.target.closest("[data-p]");
  if (!b) return;
  select(b.dataset.p);
};
function select(pid) {
  sel = pid;
  hidden.clear();
  try { localStorage.setItem("pc-sel", pid); } catch (_) { /* ignore */ }
  renderList();
  renderDetail();
  if (matchMedia("(max-width: 960px)").matches) $("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

/* --------------------------------------------------------------- detail */
function renderDetail() {
  const el = $("#detail");
  const p = product(sel);
  if (!p) {
    const steps = [
      ["سایت‌های منبع قیمت آماده‌اند (فروشگاه‌های معروف از قبل اضافه شده‌اند؛ هر سایت دیگری را هم می‌شود اضافه کرد)", S.sites.length > 0],
      ["یک محصول اضافه کنید و لینک صفحه‌اش در هر سایت را بدهید", S.products.length > 0],
      ["قیمت‌ها خودکار و دوره‌ای خوانده می‌شوند و این‌جا مقایسه می‌شوند", S.offers.some((o) => o.status === "ok")],
    ];
    el.innerHTML = `<div class="empty-card"><h3>شروع کار</h3><ol>${steps.map(([t, ok]) => `<li class="${ok ? "done" : ""}">${ok ? "✓ " : ""}${t}</li>`).join("")}</ol>
      <div class="btns"><button class="btn primary sm" data-add="product">＋ محصول</button><button class="btn sm" data-add="import">ورود از صفحه دسته‌بندی</button><button class="btn sm" data-add="site">＋ سایت</button></div></div>`;
    return;
  }
  const sm = summary(p.id);
  const spread = sm.min != null && sm.max != null ? sm.max - sm.min : null;
  const target = p.target_price
    ? `<span class="chip ${sm.min != null && sm.min <= p.target_price ? "good" : ""}">قیمت هدف: ${toman(p.target_price)}</span>` : "";
  const working = sm.all.some(busy);
  el.innerHTML = `
    <div class="panel">
      <div class="d-top">
        <div class="grow"><h2>${esc(p.name)}</h2>
          <div class="d-sub">${p.category ? `<span class="chip">${esc(p.category)}</span>` : ""}${target}
          <span>${nf.format(sm.all.length)} سایت</span>${p.note ? `<span>· ${esc(p.note)}</span>` : ""}</div></div>
        <div class="btns">
          <button class="btn sm" data-check-product="${p.id}" ${working ? "disabled" : ""}>${working ? `<span class="spin"></span> در حال بررسی` : "بررسی قیمت‌ها"}</button>
          <button class="btn sm" data-add="offer">＋ لینک سایت</button>
          <button class="btn sm" data-edit-product="${p.id}">ویرایش</button>
        </div>
      </div>
    </div>
    <div class="tiles">
      <div class="tile best"><span class="k">کمترین قیمت</span><span class="v">${sm.min != null ? `${num(sm.min)} <small>تومان</small>` : "—"}</span>
        <span class="s">${sm.best ? `در ${esc(sname(sm.best.site_id))}` : "—"}</span></div>
      <div class="tile"><span class="k">میانگین</span><span class="v">${sm.avg != null ? `${num(sm.avg)} <small>تومان</small>` : "—"}</span>
        <span class="s">از ${nf.format(sm.ok.length)} سایت</span></div>
      <div class="tile"><span class="k">بیشترین قیمت</span><span class="v">${sm.max != null ? `${num(sm.max)} <small>تومان</small>` : "—"}</span>
        <span class="s">${sm.ok.length > 1 ? `در ${esc(sname(sm.ok[sm.ok.length - 1].site_id))}` : "—"}</span></div>
      <div class="tile"><span class="k">اختلاف ارزان‌ترین و گران‌ترین</span><span class="v">${spread ? short(spread) : "—"}</span>
        <span class="s">${spread ? `${pct((spread / sm.min) * 100)} صرفه‌جویی با خرید از ارزان‌ترین` : "—"}</span></div>
    </div>
    <div class="panel">
      <div class="panel-head"><h3>مقایسه قیمت فعلی در سایت‌ها</h3><span class="muted small">طول میله = چقدر گران‌تر از ارزان‌ترین</span></div>
      ${compareChart(sm)}
    </div>
    <div class="panel">
      <div class="panel-head"><h3>روند قیمت</h3>
        <div class="seg" id="range">${RANGES.map(([k, t]) => `<button data-r="${k}" class="${k === range ? "on" : ""}">${t}</button>`).join("")}</div></div>
      <div class="legend2" id="legend"></div>
      <div class="chart-wrap" id="hchart"></div>
    </div>
    <div class="panel">
      <div class="panel-head"><h3>لینک‌ها</h3><button class="btn sm danger" data-del-product="${p.id}">حذف محصول</button></div>
      <div class="table-wrap"><table class="grid">${offerTable(sm)}</table></div>
    </div>`;
  renderHistory();
}

const ORDER = { ok: 0, out: 1, error: 2, pending: 3 };
function sortedOffers(sm) {
  return [...sm.all].sort((a, b) => {
    const la = live(a), lb = live(b);
    if (la && lb) return a.price - b.price;
    if (la !== lb) return la ? -1 : 1;
    return (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9);
  });
}

function compareChart(sm) {
  if (!sm.all.length) return `<div class="chart-empty">هنوز لینکی برای این محصول اضافه نشده</div>`;
  const span = sm.max - sm.min;
  const rows = sortedOffers(sm).map((o) => {
    const s = site(o.site_id) || {};
    const isBest = sm.best && o.id === sm.best.id;
    let cls = "", track = "", val = "";
    if (live(o)) {
      const w = span > 0 ? ((o.price - sm.min) / span) * 100 : 0;
      cls = isBest ? "best" : "";
      track = `${w > 0 ? `<span class="bar" style="width:${w}%"></span>` : ""}<span class="pin" style="inset-inline-start:${w}%"></span>`;
      const extra = o.price - sm.min;
      val = `<b>${toman(o.price)}</b>` + (isBest
        ? `<span><span class="badge-best">✓ ارزان‌ترین</span></span>`
        : `<span>${short(extra)} گران‌تر (${pct((extra / sm.min) * 100)})</span>`);
    } else {
      cls = o.status === "out" ? "out" : o.status === "error" ? "err" : "out";
      const word = !o.enabled || !s.enabled ? "غیرفعال" : o.status === "out" ? "ناموجود" : o.status === "error" ? "خطا در خواندن" : "در انتظار بررسی";
      val = `<b>${word}</b><span>${o.price ? `آخرین قیمت: ${short(o.price)}` : "&nbsp;"}</span>`;
    }
    return `<div class="crow ${cls}" data-tip-offer="${o.id}">
      <span class="cs"><i class="sw2" style="background:${siteColor(o.site_id, o.product_id)}"></i><span>${esc(s.name || "?")}</span></span>
      <span class="track">${track}</span><span class="cv">${val}</span></div>`;
  }).join("");
  const axis = sm.ok.length > 1
    ? `<div class="cmp-axis"><span></span><span class="ax"><span>${short(sm.min)} (کمترین)</span><span>${short(sm.max)}</span></span><span></span></div>` : "";
  return `<div class="cmp">${axis}${rows}</div>`;
}

function offerTable(sm) {
  if (!sm.all.length) return "";
  const head = `<tr><th>سایت</th><th>قیمت</th><th>قبل از تخفیف</th><th>اختلاف با کمترین</th><th>روش خواندن</th><th>آخرین بررسی</th><th>وضعیت</th><th></th></tr>`;
  return head + sortedOffers(sm).map((o) => {
    const isBest = sm.best && o.id === sm.best.id;
    const st = busy(o) ? `<span class="chip info"><span class="spin"></span> بررسی</span>`
      : !o.enabled ? `<span class="chip">غیرفعال</span>`
      : o.status === "ok" ? `<span class="chip good">سالم</span>`
      : o.status === "out" ? `<span class="chip warn">ناموجود</span>`
      : o.status === "error" ? `<span class="chip bad" title="${esc(o.error)}">خطا</span>`
      : `<span class="chip">در انتظار</span>`;
    const diff = live(o) && sm.min != null ? (o.price === sm.min ? `<span class="badge-best">ارزان‌ترین</span>` : `<span class="diff-up">+${short(o.price - sm.min)} (${pct(((o.price - sm.min) / sm.min) * 100)})</span>`) : "—";
    const method = o.method ? `<span class="chip ${o.method === "text" ? "warn" : ""}" title="${o.method === "text" ? "قیمت از متن صفحه حدس زده شده؛ بهتر است برای این سایت CSS selector تنظیم شود" : ""}">${esc(METHOD[o.method] || o.method)}</span>` : "—";
    return `<tr class="${isBest ? "best" : ""} ${o.enabled ? "" : "off"}">
      <td><span class="cs" style="display:inline-flex;gap:7px;align-items:center"><i class="sw2" style="width:10px;height:10px;border-radius:3px;background:${siteColor(o.site_id, o.product_id)}"></i>${esc(sname(o.site_id))}</span>
        <div><a class="url" href="${esc(o.url)}" target="_blank" rel="noopener noreferrer" title="${esc(o.url)}">${esc(o.url.replace(/^https?:\/\/(www\.)?/, ""))}</a></div></td>
      <td class="price">${o.price ? toman(o.price) : "—"}</td>
      <td>${o.old_price ? `<span class="old">${num(o.old_price)}</span>` : "—"}</td>
      <td>${diff}</td><td>${method}</td>
      <td class="small muted" title="${o.last_check ? esc(new Date(o.last_check * 1000).toLocaleString("fa-IR")) : ""}">${ago(o.last_check)}</td>
      <td>${st}${o.status === "error" && o.error ? `<div class="small" style="color:var(--down);max-width:260px;white-space:normal">${esc(o.error)}</div>` : ""}</td>
      <td><span class="row-acts">
        <button class="btn icon" data-check-offer="${o.id}" title="بررسی همین لینک">${ICON.check}</button>
        <a class="btn icon" href="${esc(o.url)}" target="_blank" rel="noopener noreferrer" title="باز کردن صفحه">${ICON.open}</a>
        <button class="btn icon" data-toggle-offer="${o.id}" title="${o.enabled ? "غیرفعال کردن" : "فعال کردن"}">${o.enabled ? ICON.pause : ICON.play}</button>
        <button class="btn icon" data-del-offer="${o.id}" title="حذف لینک">${ICON.del}</button></span></td></tr>`;
  }).join("");
}

/* -------------------------------------------------------- history chart */
function seriesFor(pid) {
  return offersOf(pid).filter((o) => o.history && o.history.length).map((o) => ({
    o, color: siteColor(o.site_id, pid), name: sname(o.site_id),
    pts: [...o.history].sort((a, b) => a[0] - b[0]),
  }));
}
function valueAt(pts, t) {
  let v = null;
  for (const [ts, p] of pts) { if (ts > t) break; v = p; }
  return v;
}
function niceTicks(lo, hi, n = 4) {
  const raw = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

function renderHistory() {
  const wrap = $("#hchart");
  if (!wrap) return;
  const p = product(sel);
  const series = seriesFor(p.id);
  const sm = summary(p.id);
  const bestId = sm.best ? sm.best.id : null;
  $("#legend").innerHTML = series.length > 1 ? series.map((s) => `<button class="lg ${hidden.has(s.o.id) ? "off" : ""} ${s.o.id === bestId ? "best" : ""}" data-lg="${s.o.id}">
      <i style="background:${s.color}"></i>${esc(s.name)}${s.o.id === bestId ? ` <span class="badge-best">ارزان‌ترین</span>` : ""}</button>`).join("") : "";
  const vis = series.filter((s) => !hidden.has(s.o.id));
  const t1 = nowS();
  const first = Math.min(...series.map((s) => s.pts[0][0]));
  // at least a day on screen, so a product added minutes ago still has an axis
  const t0 = range === "all" ? Math.min(first, t1 - 86400) : t1 - Number(range) * 86400;
  const vals = [];
  for (const s of vis) {
    const v0 = valueAt(s.pts, t0);
    if (v0) vals.push(v0);
    for (const [ts, v] of s.pts) if (ts >= t0 && v) vals.push(v);
  }
  if (!vals.length || !(t1 > t0)) { wrap.innerHTML = `<div class="chart-empty">برای این بازه هنوز قیمتی ثبت نشده</div>`; return; }

  const W = Math.max(300, wrap.clientWidth), H = 270;
  const L = 64, R = 14, T = 12, B = 28;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const padY = (hi - lo) * 0.12 || hi * 0.03;
  lo -= padY; hi += padY;
  const x = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);

  let g = "";
  for (const v of niceTicks(lo, hi)) {
    g += `<line class="grid-l" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="ax-t" x="${L - 8}" y="${y(v) + 3.5}" text-anchor="end">${short(v)}</text>`;
  }
  const days = (t1 - t0) / 86400;
  const xt = Math.max(2, Math.min(7, Math.floor((W - L - R) / 90)));
  for (let i = 0; i <= xt; i++) {
    const t = t0 + ((t1 - t0) * i) / xt;
    const lbl = days < 2 ? new Date(t * 1000).toLocaleTimeString("fa-IR", { hour: "2-digit", minute: "2-digit" }) : date(t);
    g += `<text class="ax-t" x="${x(t)}" y="${H - 8}" text-anchor="${i === 0 ? "start" : i === xt ? "end" : "middle"}">${lbl}</text>`;
  }
  let lines = "";
  const order = [...vis].sort((a, b) => (a.o.id === bestId) - (b.o.id === bestId)); // cheapest drawn on top
  for (const s of order) {
    // step line: a price holds until the next reading; unavailable = a gap
    let d = "", pen = false;
    const steps = [[t0, valueAt(s.pts, t0)], ...s.pts.filter(([ts]) => ts > t0), [t1, null]];
    for (let i = 0; i < steps.length - 1; i++) {
      const [ts, val] = steps[i];
      if (val) {
        d += pen ? `V${y(val).toFixed(1)}` : `M${x(ts).toFixed(1)} ${y(val).toFixed(1)}`;
        d += ` H${x(steps[i + 1][0]).toFixed(1)} `;
        pen = true;
      } else pen = false;
    }
    lines += `<path class="ln ${s.o.id === bestId ? "best" : ""}" data-ln="${s.o.id}" d="${d}" stroke="${s.color}"/>`;
    // a dot on today's price, so a shop with a single reading is still visible
    const last = valueAt(s.pts, t1);
    if (last) lines += `<circle class="mk" cx="${x(t1).toFixed(1)}" cy="${y(last).toFixed(1)}" r="${s.o.id === bestId ? 5 : 4}" fill="${s.color}"/>`;
  }
  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" dir="ltr" role="img" aria-label="روند قیمت ${esc(p.name)}">${g}${lines}
    <g id="xh" style="display:none"><line class="xh" y1="${T}" y2="${H - B}"/></g>
    <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" id="hit"/></svg>`;

  const svg = $("svg", wrap), xh = $("#xh", svg), hit = $("#hit", svg);
  const tip = $("#tip");
  hit.onmousemove = (e) => {
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const t = t0 + ((px - L) / (W - L - R)) * (t1 - t0);
    const rows = vis.map((s) => ({ s, v: valueAt(s.pts, t) })).filter((r_) => r_.v).sort((a, b) => a.v - b.v);
    xh.style.display = "";
    $("line", xh).setAttribute("x1", px); $("line", xh).setAttribute("x2", px);
    $$(".mk", xh).forEach((m) => m.remove());
    for (const { s, v } of rows) {
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("class", "mk"); c.setAttribute("cx", px); c.setAttribute("cy", y(v)); c.setAttribute("r", 4.5); c.setAttribute("fill", s.color);
      xh.appendChild(c);
    }
    if (!rows.length) { tip.classList.remove("show"); return; }
    const minV = rows[0].v;
    tip.innerHTML = `<div class="when">${new Date(t * 1000).toLocaleDateString("fa-IR", { year: "numeric", month: "long", day: "numeric" })}</div>` +
      rows.map(({ s, v }) => `<div class="row ${v === minV ? "best" : ""}"><span class="n"><i style="background:${s.color}"></i>${esc(s.name)}</span><b>${toman(v)}</b></div>`).join("");
    showTip(e);
  };
  hit.onmouseleave = () => { xh.style.display = "none"; tip.classList.remove("show"); };
}

function showTip(e) {
  const tip = $("#tip");
  tip.classList.add("show");
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let left = e.clientX + 14, top = e.clientY + 14;
  if (left + w > innerWidth - 8) left = e.clientX - w - 14;
  if (top + h > innerHeight - 8) top = e.clientY - h - 14;
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${Math.max(8, top)}px`;
}

$("#detail").addEventListener("mousemove", (e) => {
  const row = e.target.closest("[data-tip-offer]");
  if (!row) { if (!e.target.closest("#hchart")) $("#tip").classList.remove("show"); return; }
  const o = S.offers.find((x) => x.id === row.dataset.tipOffer);
  if (!o) return;
  $("#tip").innerHTML = `<b>${esc(sname(o.site_id))}</b><br>${o.price ? toman(o.price) : "—"}` +
    (o.old_price ? `<br><span class="old">${toman(o.old_price)}</span>` : "") +
    (o.method ? `<br><span class="muted">روش: ${esc(METHOD[o.method] || o.method)}</span>` : "") +
    `<br><span class="muted">آخرین بررسی: ${ago(o.last_check)}</span>` +
    (o.error ? `<br><span style="color:var(--down)">${esc(o.error)}</span>` : "");
  showTip(e);
});
$("#detail").addEventListener("mouseleave", () => $("#tip").classList.remove("show"));

/* ----------------------------------------------------------------- sites */
function renderSites() {
  const head = `<tr><th>سایت</th><th>دامنه</th><th>لینک‌ها</th><th>واحد</th><th>روش</th><th></th></tr>`;
  $("#sites-table").innerHTML = head + S.sites.map((s) => {
    const os = S.offers.filter((o) => o.site_id === s.id && o.enabled);
    const bad = os.filter((o) => o.status === "error").length;
    const methods = [...new Set(os.map((o) => o.method).filter(Boolean))].map((m) => METHOD[m] || m).join("، ");
    return `<tr data-site="${s.id}" class="${s.enabled ? "" : "off"}">
      <td><b style="font-weight:500">${esc(s.name)}</b></td>
      <td class="mono small">${esc(s.domains.join(", "))}</td>
      <td>${nf.format(os.length)}${bad ? ` <span class="chip bad">${nf.format(bad)} خطا</span>` : ""}</td>
      <td class="small">${UNIT[s.unit] || s.unit}</td>
      <td class="small muted">${s.selector ? `<span class="chip info">CSS selector</span>` : esc(methods || "خودکار")}</td>
      <td>${s.enabled ? "" : `<span class="chip">غیرفعال</span>`}</td></tr>`;
  }).join("");
}
$("#sites-table").onclick = (e) => {
  const tr = e.target.closest("[data-site]");
  if (tr) siteForm(site(tr.dataset.site));
};

/* --------------------------------------------------------------- events */
function renderEvents(newId) {
  const list = events.filter((e) => evFilter === "all" || e.level === evFilter).slice(-200).reverse();
  $("#events").innerHTML = list.map((e) => `<li class="${e.level} ${e.id === newId ? "new" : ""}" ${e.refs && e.refs.product ? `data-ev-product="${e.refs.product}" style="cursor:pointer"` : ""}>
    <span class="lv"></span><span class="msg">${esc(e.message)}</span><time>${clock(e.ts)}</time></li>`).join("")
    || `<li class="muted"><span></span><span>هنوز رویدادی ثبت نشده</span></li>`;
}
$$("#ev-filter button").forEach((b) => (b.onclick = () => {
  evFilter = b.dataset.f;
  $$("#ev-filter button").forEach((x) => x.classList.toggle("on", x === b));
  renderEvents();
}));
$("#events").onclick = (e) => {
  const li = e.target.closest("[data-ev-product]");
  if (li && product(li.dataset.evProduct)) { select(li.dataset.evProduct); window.scrollTo({ top: 0, behavior: "smooth" }); }
};

/* ----------------------------------------------------------------- modals */
function openModal(html, onSubmit) {
  const f = $("#modal-form");
  f.innerHTML = html;
  $("#modal").classList.add("open");
  f.onsubmit = async (e) => {
    e.preventDefault();
    const btn = f.querySelector("[type=submit]");
    if (btn) btn.disabled = true;
    try { await onSubmit(new FormData(f), f); closeModal(); }
    catch (err) { toast(err.message, "error"); }
    finally { if (btn) btn.disabled = false; }
  };
  const first = f.querySelector("input:not([type=checkbox]),select,textarea");
  if (first) setTimeout(() => first.focus(), 50);
}
function closeModal() { $("#modal").classList.remove("open"); $("#modal-form").classList.remove("wide"); imp = null; }
const actions = (label, extra = "") => `<div class="modal-actions">${extra}<span style="flex:1"></span><button type="button" class="btn ghost" data-close-modal>انصراف</button><button class="btn primary" type="submit">${label}</button></div>`;
const lines = (v) => v.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
const toInt = (v) => { const n = parseInt(String(v || "").replace(/[^\d۰-۹]/g, "").replace(/[۰-۹]/g, (d) => "۰۱۲۳۴۵۶۷۸۹".indexOf(d)), 10); return Number.isFinite(n) && n > 0 ? n : null; };

async function runTest(f, body) {
  const out = $(".test-out", f);
  if (!body.url) { out.className = "test-out bad"; out.textContent = "اول یک لینک وارد کنید"; return; }
  out.className = "test-out"; out.innerHTML = `<span class="spin"></span> در حال خواندن صفحه…`;
  try {
    const r = await api("POST", "/api/test", body);
    if (!r.ok) { out.className = "test-out bad"; out.textContent = r.error; return; }
    out.className = "test-out ok";
    out.innerHTML = r.available
      ? `قیمت: <b>${toman(r.price)}</b>${r.old_price ? ` · قبل از تخفیف: <span class="old">${num(r.old_price)}</span>` : ""}<br><span class="muted">روش: ${esc(METHOD[r.method] || r.method)}</span>` +
        (r.guess ? `<br><span style="color:var(--degraded)">این قیمت از متن صفحه حدس زده شده؛ اگر درست نیست برای سایت CSS selector بگذارید.</span>` : "")
      : `صفحه خوانده شد: محصول <b>ناموجود</b> است <span class="muted">(${esc(METHOD[r.method] || r.method)})</span>`;
  } catch (e) { out.className = "test-out bad"; out.textContent = e.message; }
}

function productForm(p) {
  const edit = !!p;
  p = p || { name: "", category: "", note: "", target_price: null };
  openModal(`<h2>${edit ? "ویرایش محصول" : "افزودن محصول"}</h2>
    <label>نام محصول<input name="name" required value="${esc(p.name)}" placeholder="گوشی سامسونگ Galaxy A56 ۲۵۶ گیگ"></label>
    <div class="grid2"><label>دسته‌بندی<input name="category" value="${esc(p.category)}" placeholder="موبایل"></label>
    <label>قیمت هدف (تومان، اختیاری)<input name="target_price" inputmode="numeric" value="${p.target_price || ""}" placeholder="۲۹۰۰۰۰۰۰"></label></div>
    <p class="hint">اگر قیمت در یکی از سایت‌ها به قیمت هدف یا کمتر برسد، رویداد ثبت می‌شود.</p>
    ${edit ? "" : `<label>لینک صفحه محصول در سایت‌ها (هر خط یک لینک)<textarea name="urls" rows="5" placeholder="https://www.digikala.com/product/dkp-.../\nhttps://www.technolife.com/product-.../"></textarea></label>
    <p class="hint">سایت هر لینک از روی دامنه‌اش پیدا می‌شود. اگر دامنه‌ای تعریف نشده باشد، خودکار به سایت‌ها اضافه می‌شود.</p>`}
    <label>یادداشت<input name="note" value="${esc(p.note)}"></label>
    ${actions(edit ? "ذخیره" : "افزودن و بررسی قیمت‌ها", edit ? `<button type="button" class="btn danger" data-del-product="${p.id}">حذف</button>` : "")}`,
  async (fd) => {
    const body = { name: fd.get("name"), category: fd.get("category") || "", note: fd.get("note") || "", target_price: toInt(fd.get("target_price")) };
    if (edit) return act(() => api("PUT", `/api/products/${p.id}`, body), "ذخیره شد");
    body.urls = lines(fd.get("urls") || "");
    const r = await act(() => api("POST", "/api/products", body), "محصول اضافه شد");
    for (const s of r.skipped || []) toast(s, "error");
    select(r.id);
  });
}

function offerForm(pid) {
  if (!S.products.length) return toast("اول یک محصول اضافه کنید", "error");
  pid = pid || sel;
  openModal(`<h2>افزودن لینک محصول در یک سایت</h2>
    <label>محصول<select name="product_id">${S.products.map((p) => `<option value="${p.id}" ${p.id === pid ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
    <label>لینک صفحه محصول<input name="url" required class="mono" placeholder="https://..."></label>
    <div class="btns"><button type="button" class="btn sm" id="t-btn">تست لینک</button><span class="hint" style="margin:0">قیمت را یک بار می‌خواند تا مطمئن شوید درست است</span></div>
    <div class="test-out muted">نتیجه تست این‌جا نشان داده می‌شود</div>
    ${actions("افزودن")}`,
  async (fd) => {
    const body = { product_id: fd.get("product_id"), url: fd.get("url").trim() };
    try { await api("POST", "/api/offers", body); }
    catch (e) {
      if (e.status !== 422 || !/سایتی برای دامنه/.test(e.message)) throw e;
      if (!confirm(`${e.message}\n\nسایت جدید با همین دامنه ساخته شود؟`)) throw e;
      await api("POST", "/api/offers", { ...body, create_site: true });
    }
    toast("لینک اضافه شد؛ قیمت در حال خواندن است", "success");
    if (body.product_id !== sel) select(body.product_id);
  });
  $("#t-btn").onclick = () => runTest($("#modal-form"), { url: $("#modal-form [name=url]").value.trim() });
}

function siteForm(s) {
  const edit = !!s;
  s = s || { name: "", domains: [], unit: "auto", selector: "", out_selector: "", note: "", enabled: true };
  const sample = edit ? (S.offers.find((o) => o.site_id === s.id) || {}).url || "" : "";
  openModal(`<h2>${edit ? "ویرایش سایت" : "افزودن سایت"}</h2>
    <div class="grid2"><label>نام<input name="name" required value="${esc(s.name)}" placeholder="فروشگاه نمونه"></label>
    <label>دامنه‌ها<input name="domains" required class="mono" value="${esc(s.domains.join(", "))}" placeholder="example.ir"></label></div>
    <p class="hint">با کاما جدا کنید. زیردامنه‌ها خودکار شامل می‌شوند.</p>
    <fieldset><legend>خواندن قیمت (اختیاری)</legend>
      <p class="note">پنل قیمت را خودکار از داده‌های ساختاریافته صفحه (JSON-LD، متا تگ‌ها، ووکامرس، JSON صفحه) پیدا می‌کند و برای بیشتر فروشگاه‌ها تنظیمی لازم نیست. فقط اگر قیمت اشتباه یا پیدا نشد، selector بگذارید.</p>
      <label>واحد قیمت در این سایت<select name="unit">${Object.entries(UNIT).map(([k, v]) => `<option value="${k}" ${k === s.unit ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <div class="grid2"><label>CSS selector قیمت<input name="selector" class="mono" value="${esc(s.selector)}" placeholder=".product-price .amount"></label>
      <label>CSS selector «ناموجود»<input name="out_selector" class="mono" value="${esc(s.out_selector)}" placeholder=".out-of-stock"></label></div>
      <div class="grid2"><label>selector لینک محصول در صفحه دسته‌بندی<input name="list_selector" class="mono" value="${esc(s.list_selector || "")}" placeholder=".product-card a.title"></label>
      <label>selector دکمه صفحه بعد<input name="next_selector" class="mono" value="${esc(s.next_selector || "")}" placeholder="a.next"></label></div>
      <p class="hint">فقط برای «ورود از صفحه دسته‌بندی» لازم است و فقط وقتی محصولات صفحه خودکار پیدا نشوند.</p>
      <label>لینک یک محصول برای تست<input name="test_url" class="mono" value="${esc(sample)}" placeholder="https://..."></label>
      <div class="btns"><button type="button" class="btn sm" id="t-btn">تست با همین تنظیمات</button></div>
      <div class="test-out muted">نتیجه تست این‌جا نشان داده می‌شود</div>
    </fieldset>
    <label>یادداشت<input name="note" value="${esc(s.note)}"></label>
    <label class="check"><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}> فعال (قیمت از این سایت خوانده شود)</label>
    ${actions(edit ? "ذخیره" : "افزودن", edit ? `<button type="button" class="btn danger" data-del-site="${s.id}">حذف</button>` : "")}`,
  async (fd) => {
    const body = {
      name: fd.get("name"), domains: lines(fd.get("domains")), unit: fd.get("unit"),
      selector: fd.get("selector").trim(), out_selector: fd.get("out_selector").trim(),
      list_selector: fd.get("list_selector").trim(), next_selector: fd.get("next_selector").trim(), note: fd.get("note") || "", enabled: fd.get("enabled") === "on",
    };
    await act(() => api(edit ? "PUT" : "POST", edit ? `/api/sites/${s.id}` : "/api/sites", body), "ذخیره شد");
  });
  $("#t-btn").onclick = () => {
    const f = $("#modal-form");
    runTest(f, { url: f.test_url.value.trim(), unit: f.unit.value, selector: f.selector.value.trim(), out_selector: f.out_selector.value.trim() });
  };
}

function settingsForm() {
  const s = S.settings;
  const iv = [[30, "هر ۳۰ دقیقه"], [60, "هر ساعت"], [180, "هر ۳ ساعت"], [360, "هر ۶ ساعت"], [720, "هر ۱۲ ساعت"], [1440, "روزی یک بار"]];
  if (!iv.some(([v]) => v === s.interval_min)) iv.unshift([s.interval_min, `هر ${nf.format(s.interval_min)} دقیقه`]);
  openModal(`<h2>تنظیمات</h2>
    <div class="grid2"><label>بررسی خودکار قیمت‌ها<select name="interval_min">${iv.map(([v, t]) => `<option value="${v}" ${v === s.interval_min ? "selected" : ""}>${t}</option>`).join("")}</select></label>
    <label>فاصله بین دو درخواست به یک سایت (ثانیه)<input name="host_delay_s" type="number" min="0" max="120" step="0.5" value="${s.host_delay_s}"></label></div>
    <div class="grid2"><label>حداکثر زمان انتظار (ثانیه)<input name="timeout_s" type="number" min="5" max="120" value="${s.timeout_s}"></label>
    <label>سایت‌های هم‌زمان<input name="concurrency" type="number" min="1" max="20" value="${s.concurrency}"></label></div>
    <label>پراکسی (اختیاری)<input name="proxy" class="mono" value="${esc(s.proxy)}" placeholder="http://127.0.0.1:8080 یا socks5://..."></label>
    <p class="hint">بیشتر فروشگاه‌های ایرانی درخواست از IP خارج را می‌بندند. پنل را روی سرور ایران نصب کنید یا یک پراکسی ایران بدهید.</p>
    <label>User-Agent<input name="user_agent" class="mono" value="${esc(s.user_agent)}"></label>
    <p class="hint">تغییر رمز ورود از خط فرمان سرور: <span class="mono">python -m pricecomparison passwd</span></p>
    ${actions("ذخیره")}`,
  async (fd) => {
    await act(() => api("PUT", "/api/settings", {
      interval_min: +fd.get("interval_min"), host_delay_s: +fd.get("host_delay_s"), timeout_s: +fd.get("timeout_s"),
      concurrency: +fd.get("concurrency"), proxy: fd.get("proxy").trim(), user_agent: fd.get("user_agent").trim(),
    }), "تنظیمات ذخیره شد");
  });
}

/* -------------------------------------------------------- category import */
// imp: the import shown in the modal: {job, sel: Map(index -> {on, name, urls:Set}), filter}
let imp = null;
const ISTATUS = { waiting: ["در صف", ""], reading: ["در حال خواندن", "info"], done: ["تمام شد", "good"], error: ["خطا", "bad"] };

function importForm() {
  const cur = S.import;
  if (cur && cur.status === "running") return api("GET", `/api/import/${cur.id}`).then(showImport);
  if (cur && cur.status === "done") {
    return api("GET", `/api/import/${cur.id}`).then((job) => {
      if (job.groups.length && confirm("نتیجه آخرین ورود از دسته‌بندی هنوز اعمال نشده. همان را نشان بدهم؟\n(انصراف = شروع ورود جدید)")) return showImport(job);
      importStart();
    }).catch(importStart);
  }
  importStart();
}

function importStart() {
  openModal(`<h2>ورود محصولات از صفحه دسته‌بندی</h2>
    <p class="note">لینک صفحه دسته‌بندی یا فروشگاه هر سایت را بدهید. پنل همه صفحه‌های آن را می‌خواند، محصولات را پیدا می‌کند و محصولاتی را که در چند سایت هستند از روی نامشان کنار هم می‌گذارد. قبل از افزودن، همه را می‌بینید و انتخاب می‌کنید.</p>
    <label>لینک صفحه‌های دسته‌بندی (هر خط یک لینک)<textarea name="urls" rows="4" required placeholder="https://shahrsakhtemani.com/shop/\nhttps://masaleh-tehran.ir/product-category/building-materials/"></textarea></label>
    <label>حداکثر صفحه برای هر لینک<input name="max_pages" type="number" min="1" max="100" value="20"></label>
    <p class="hint">سایتی که دامنه‌اش در پنل نیست خودکار اضافه می‌شود. بین دو درخواست به یک سایت ${nf.format(S.settings.host_delay_s)} ثانیه صبر می‌شود.</p>
    ${actions("شروع خواندن")}`,
  async (fd) => {
    const job = await api("POST", "/api/import", { urls: lines(fd.get("urls")), max_pages: +fd.get("max_pages") || 20 });
    setTimeout(() => showImport(job), 0); // after openModal's submit closes this form
  });
}

function showImport(job) {
  const same = imp && imp.job.id === job.id;
  imp = { job, sel: same ? imp.sel : new Map(), filter: same ? imp.filter : "all" };
  if (job.status === "done" && !imp.sel.size) {
    job.groups.forEach((g, i) => imp.sel.set(i, { on: true, name: g.name, urls: new Set(g.items.map((x) => x.url)) }));
  }
  const f = $("#modal-form");
  f.classList.add("wide");
  $("#modal").classList.add("open");
  f.onsubmit = (e) => { e.preventDefault(); applyImport(); };
  renderImport();
}

function onImport(job) {
  if (S) S.import = { ...job, groups: [] };
  if (!imp || imp.job.id !== job.id) {
    if (job.status === "done" && job.groups.length && !imp) toast(`خواندن دسته‌بندی تمام شد: ${nf.format(job.groups.length)} محصول پیدا شد`, "success");
    return;
  }
  if (job.status === "done" && imp.job.status === "done") return; // keep the admin's choices
  showImport(job);
}

function sourcesTable(job) {
  return `<div class="table-wrap"><table class="grid"><tr><th>صفحه</th><th>سایت</th><th>وضعیت</th><th>صفحه‌ها</th><th>محصول</th></tr>${job.sources.map((s) => {
    const [t, c] = ISTATUS[s.status] || [s.status, ""];
    return `<tr><td><span class="url" title="${esc(s.url)}">${esc(decodeURI(s.url).replace(/^https?:\/\/(www\.)?/, ""))}</span>${s.error ? `<div class="small" style="color:var(--down);white-space:normal">${esc(s.error)}</div>` : ""}</td>
      <td>${esc(s.site_name)}</td><td><span class="chip ${c}">${s.status === "reading" ? `<span class="spin"></span> ` : ""}${t}</span></td>
      <td>${nf.format(s.pages)}</td><td><b>${nf.format(s.found)}</b></td></tr>`;
  }).join("")}</table></div>`;
}

function renderImport() {
  const { job } = imp;
  const f = $("#modal-form");
  const list = $(".imp-list", f);
  const keep = [f.scrollTop, list ? list.scrollTop : 0];
  renderImportBody(job, f);
  f.scrollTop = keep[0];
  if ($(".imp-list", f)) $(".imp-list", f).scrollTop = keep[1];
}

function renderImportBody(job, f) {
  if (job.status !== "done") {
    f.innerHTML = `<h2>ورود از صفحه دسته‌بندی</h2>
      <p class="note">${job.status === "running" ? `<span class="spin"></span> در حال خواندن صفحه‌ها… می‌توانید این پنجره را ببندید؛ کار ادامه پیدا می‌کند.` : job.status === "applied" ? "این ورود اعمال شده است." : "متوقف شد."}</p>
      ${sourcesTable(job)}
      <div class="modal-actions"><span style="flex:1"></span>${job.status === "running" ? `<button type="button" class="btn danger" data-imp-cancel>توقف</button>` : ""}<button type="button" class="btn" data-close-modal>بستن</button></div>`;
    return;
  }
  const groups = job.groups;
  const multi = groups.filter((g) => g.items.length > 1).length;
  const matched = groups.filter((g) => g.product_id).length;
  const picked = [...imp.sel.values()].filter((v) => v.on && v.urls.size);
  const title = job.sources.map((s) => s.title).find(Boolean) || "";
  const shown = groups.map((g, i) => [g, i]).filter(([g]) => imp.filter === "all" || (imp.filter === "multi" ? g.items.length > 1 : g.items.length === 1));
  f.innerHTML = `<h2>نتیجه خواندن دسته‌بندی</h2>
    ${sourcesTable(job)}
    <p class="note"><b>${nf.format(groups.length)}</b> محصول پیدا شد؛ <b>${nf.format(multi)}</b> محصول در بیش از یک سایت هست و قیمتشان مقایسه می‌شود${matched ? `؛ <b>${nf.format(matched)}</b> مورد به محصولات موجود پنل وصل می‌شود` : ""}${job.known ? `؛ ${nf.format(job.known)} لینک از قبل در پنل بود و کنار گذاشته شد` : ""}.
      محصولات هم‌نام از روی اسم کنار هم گذاشته شده‌اند؛ اگر دو مورد اشتباه یکی شده‌اند، تیک یکی را بردارید.</p>
    ${groups.length ? `<div class="imp-bar">
      <label class="imp-cat">دسته‌بندی محصولات جدید<input name="category" value="${esc(imp.category ?? title.slice(0, 40))}" maxlength="40"></label>
      <div class="seg" id="imp-filter">${[["all", "همه"], ["multi", "چندسایتی"], ["single", "تک‌سایتی"]].map(([k, t]) => `<button type="button" data-imp-filter="${k}" class="${imp.filter === k ? "on" : ""}">${t}</button>`).join("")}</div>
      <span class="btns"><button type="button" class="btn sm" data-imp-all="1">انتخاب همه</button><button type="button" class="btn sm ghost" data-imp-all="0">هیچ</button></span></div>
    <div class="imp-list">${shown.map(([g, i]) => importGroup(g, i)).join("")}</div>` : ""}
    <div class="modal-actions"><span class="muted small">${nf.format(picked.length)} محصول انتخاب شده</span><span style="flex:1"></span>
      <button type="button" class="btn ghost" data-close-modal>انصراف</button>
      <button class="btn primary" type="submit" ${picked.length ? "" : "disabled"}>افزودن به پنل</button></div>`;
  const cat = $("[name=category]", f);
  if (cat) cat.oninput = () => { imp.category = cat.value; };
}

function importGroup(g, i) {
  const st = imp.sel.get(i);
  const priced = g.items.filter((x) => x.price && st.urls.has(x.url));
  const min = priced.length > 1 ? Math.min(...priced.map((x) => x.price)) : null;
  return `<div class="ig ${st.on ? "" : "off"}">
    <div class="ig-head"><input type="checkbox" data-imp-g="${i}" ${st.on ? "checked" : ""} aria-label="انتخاب">
      ${g.product_id ? `<span class="ig-name ro">${esc(g.name)}</span><span class="chip info">محصول موجود</span>`
        : `<input class="ig-name" data-imp-name="${i}" value="${esc(st.name)}" maxlength="120">`}
      <span class="chip ${g.items.length > 1 ? "good" : ""}">${nf.format(g.items.length)} سایت</span></div>
    <div class="ig-items">${g.items.map((x) => {
      const best = min != null && x.price === min;
      return `<label class="ig-item ${best ? "best" : ""}"><input type="checkbox" data-imp-i="${i}" value="${esc(x.url)}" ${st.urls.has(x.url) ? "checked" : ""}>
        <span class="ig-site">${esc(sname(x.site_id))}</span>
        <a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer" class="ig-iname">${esc(x.name)}</a>
        <span class="ig-price">${x.price ? toman(x.price) : "—"}${best ? ` <span class="badge-best">ارزان‌ترین</span>` : ""}</span></label>`;
    }).join("")}</div></div>`;
}

$("#modal-form").addEventListener("change", (e) => {
  if (!imp) return;
  const d = e.target.dataset;
  if (d.impG) { imp.sel.get(+d.impG).on = e.target.checked; renderImport(); }
  else if (d.impI) {
    const st = imp.sel.get(+d.impI);
    e.target.checked ? st.urls.add(e.target.value) : st.urls.delete(e.target.value);
    renderImport();
  }
});
$("#modal-form").addEventListener("input", (e) => {
  if (imp && e.target.dataset.impName) imp.sel.get(+e.target.dataset.impName).name = e.target.value;
});
$("#modal-form").addEventListener("click", (e) => {
  if (!imp) return;
  const t = e.target.closest("[data-imp-filter],[data-imp-all],[data-imp-cancel]");
  if (!t) return;
  if (t.dataset.impFilter) { imp.filter = t.dataset.impFilter; renderImport(); }
  else if (t.dataset.impAll) {
    const on = t.dataset.impAll === "1";
    imp.job.groups.forEach((g, i) => {
      if (imp.filter === "all" || (imp.filter === "multi" ? g.items.length > 1 : g.items.length === 1)) imp.sel.get(i).on = on;
    });
    renderImport();
  } else if ("impCancel" in t.dataset) act(() => api("POST", `/api/import/${imp.job.id}/cancel`), "متوقف شد");
});

async function applyImport() {
  const groups = [...imp.sel.entries()].filter(([, v]) => v.on && v.urls.size)
    .map(([index, v]) => ({ index, name: v.name.trim(), urls: [...v.urls] }));
  const btn = $("#modal-form [type=submit]");
  btn.disabled = true;
  try {
    const r = await act(() => api("POST", `/api/import/${imp.job.id}/apply`, { category: imp.category ?? ($("#modal-form [name=category]") || {}).value ?? "", groups }));
    toast(`${nf.format(r.created)} محصول جدید و ${nf.format(r.linked)} لینک اضافه شد؛ قیمت صفحه هر محصول در بررسی بعدی خوانده می‌شود`, "success");
    closeModal();
  } catch (_) { btn.disabled = false; }
}

/* --------------------------------------------------------------- actions */
function addThing(kind) {
  $("#add-pop").classList.remove("open");
  if (kind === "product") productForm();
  else if (kind === "offer") offerForm();
  else if (kind === "site") siteForm();
  else if (kind === "import") importForm();
}

document.addEventListener("click", async (e) => {
  if (!e.target.closest(".menu")) $("#add-pop").classList.remove("open");
  const t = e.target.closest("[data-add],[data-close-modal],[data-check-product],[data-check-offer],[data-toggle-offer],[data-del-offer],[data-edit-product],[data-del-product],[data-del-site],[data-r],[data-lg]");
  if (!t) return;
  const d = t.dataset;
  if (d.add) return addThing(d.add);
  if ("closeModal" in d) return closeModal();
  if (d.r) {
    range = d.r;
    try { localStorage.setItem("pc-range", range); } catch (_) { /* ignore */ }
    $$("#range button").forEach((b) => b.classList.toggle("on", b.dataset.r === range));
    return renderHistory();
  }
  if (d.lg) {
    hidden.has(d.lg) ? hidden.delete(d.lg) : hidden.add(d.lg);
    return renderHistory();
  }
  if (d.checkProduct) return act(() => api("POST", `/api/products/${d.checkProduct}/check`), "بررسی شروع شد");
  if (d.checkOffer) {
    t.disabled = true;
    try { await act(() => api("POST", `/api/offers/${d.checkOffer}/check`)); } finally { t.disabled = false; }
    return;
  }
  if (d.toggleOffer) {
    const o = S.offers.find((x) => x.id === d.toggleOffer);
    return act(() => api("PUT", `/api/offers/${o.id}`, { enabled: !o.enabled }));
  }
  if (d.delOffer) {
    const o = S.offers.find((x) => x.id === d.delOffer);
    if (confirm(`لینک ${sname(o.site_id)} حذف شود؟ تاریخچه قیمتش هم پاک می‌شود.`)) act(() => api("DELETE", `/api/offers/${o.id}`), "حذف شد");
    return;
  }
  if (d.editProduct) return productForm(product(d.editProduct));
  if (d.delProduct) {
    const p = product(d.delProduct);
    if (confirm(`محصول «${p.name}» و همه لینک‌ها و تاریخچه قیمتش حذف شود؟`)) { await act(() => api("DELETE", `/api/products/${p.id}`), "حذف شد"); closeModal(); }
    return;
  }
  if (d.delSite) {
    const s = site(d.delSite);
    const n = S.offers.filter((o) => o.site_id === s.id).length;
    if (confirm(`سایت «${s.name}»${n ? ` و ${nf.format(n)} لینک محصول روی آن` : ""} حذف شود؟`)) { await act(() => api("DELETE", `/api/sites/${s.id}`), "حذف شد"); closeModal(); }
  }
});

$("#btn-add").onclick = (e) => { e.stopPropagation(); $("#add-pop").classList.toggle("open"); };
$("#btn-check").onclick = () => act(() => api("POST", "/api/check"), "بررسی همه قیمت‌ها شروع شد");
$("#btn-settings").onclick = settingsForm;
$("#btn-logout").onclick = async () => { await api("POST", "/api/logout"); location.reload(); };
$("#btn-theme").onclick = () => {
  const cur = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = cur;
  try { localStorage.setItem("pc-theme", cur); } catch (_) { /* ignore */ }
};
$("#modal").addEventListener("mousedown", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
let rz;
window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => S && renderHistory(), 150); });

boot();
