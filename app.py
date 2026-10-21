#!/usr/bin/env python3
"""Spy Before You Buy - free GEX heatmap web app (Telegram-gated).

Login with Telegram -> app checks live membership of Justin's private
channel on every request. In channel = full access (100 heatmaps/week);
removed from channel = locked out within minutes. Zero manual work.

Env:
  TELEGRAM_BOT_TOKEN - bot token (Space secret, never in code)
  PUBLIC_URL         - e.g. https://<you>-sbyb-gex.hf.space (for login widget)
  SESSION_SECRET     - random string for signing session cookies

Data: free CBOE delayed (~15 min) options chain - labeled on screen.
"""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from itsdangerous import URLSafeSerializer, BadData, BadSignature

import gex_heatmap
import render_mpl

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(APP_DIR, "cache")
DB_PATH = os.path.join(APP_DIR, "usage.db")
os.makedirs(CACHE_DIR, exist_ok=True)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME = "Gamma_brief_bot"
CHANNEL_ID = "-1003603174198"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-only-change-me")

WEEKLY_LIMIT = 100
CACHE_TTL = 15 * 60          # heatmap cache per ticker
MEMBER_TTL = 15 * 60         # channel-membership re-check interval
WINDOW = 7 * 24 * 3600       # rolling 7-day quota window
TICKER_RE = re.compile(r"^[A-Z]{1,6}(\.[A-Z])?$")

BUILD_LOCK = threading.Lock()
signer = URLSafeSerializer(SESSION_SECRET)
app = FastAPI(title="SBYB GEX Heatmap")


# ---------- telegram ----------
def verify_tg_auth(params: dict) -> dict | None:
    """Verify Telegram Login Widget data. Returns user dict or None."""
    if not BOT_TOKEN:
        return None
    d = dict(params)
    recv = d.pop("hash", "")
    if not recv or "id" not in d or "auth_date" not in d:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv):
        return None
    try:
        if time.time() - int(d["auth_date"]) > 86400:
            return None
    except ValueError:
        return None
    return d


def _tg_api(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data)
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


_member_cache: dict[int, tuple[bool, float]] = {}


def is_channel_member(tg_id: int) -> bool:
    """Live channel-membership check, cached briefly so removals take
    effect within minutes without hammering the API."""
    now = time.time()
    hit = _member_cache.get(tg_id)
    if hit and now - hit[1] < MEMBER_TTL:
        return hit[0]
    ok = False
    try:
        res = _tg_api("getChatMember",
                      {"chat_id": CHANNEL_ID, "user_id": tg_id})
        status = res.get("result", {}).get("status", "")
        ok = status in ("creator", "administrator", "member")
    except Exception:
        # on API failure keep the last known answer if we have one
        if hit:
            return hit[0]
        ok = False
    _member_cache[tg_id] = (ok, now)
    return ok


def session_user(request: Request) -> dict | None:
    tok = request.cookies.get("sbyb_session")
    if not tok:
        return None
    try:
        u = signer.loads(tok)
    except (BadSignature, BadData):
        return None
    if not isinstance(u, dict) or "tg_id" not in u:
        return None
    if not is_channel_member(int(u["tg_id"])):
        return None
    return u


def need_user(request: Request):
    u = session_user(request)
    if not u:
        return None, JSONResponse(
            {"detail": "Login with Telegram and join the channel to use this tool."},
            status_code=403)
    return u, None


# ---------- usage tracking (per telegram user) ----------
def _db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS builds
                   (tg_id INTEGER, username TEXT, ticker TEXT, ts REAL)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_builds ON builds (tg_id, ts)")
    return con


def uses_left(tg_id: int) -> int:
    con = _db()
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM builds WHERE tg_id=? AND ts>?",
            (tg_id, time.time() - WINDOW)).fetchone()[0]
        return max(0, WEEKLY_LIMIT - n)
    finally:
        con.close()


def record_build(tg_id: int, username: str, ticker: str):
    con = _db()
    try:
        con.execute(
            "INSERT INTO builds (tg_id, username, ticker, ts) VALUES (?,?,?,?)",
            (tg_id, username, ticker, time.time()))
        con.execute("DELETE FROM builds WHERE ts<?", (time.time() - WINDOW,))
        con.commit()
    finally:
        con.close()


# ---------- heatmap build + cache ----------
def _paths(ticker: str):
    safe = ticker.replace(".", "_")
    return (os.path.join(CACHE_DIR, f"gex-{safe}.png"),
            os.path.join(CACHE_DIR, f"gex-{safe}.txt"),
            os.path.join(CACHE_DIR, f"gex-{safe}.meta"))


def _fresh(png_path: str) -> bool:
    return (os.path.exists(png_path)
            and time.time() - os.path.getmtime(png_path) < CACHE_TTL)


def get_or_build(ticker: str, user: dict):
    """Returns (png_path, read, as_of_ts, extras, from_cache, left). Raises on error."""
    tg_id = int(user["tg_id"])
    username = user.get("username", "")
    png_path, txt_path, meta_path = _paths(ticker)
    extras_path = txt_path + ".extras.json"
    left = uses_left(tg_id)

    def _load_extras():
        try:
            with open(extras_path) as f:
                ex = json.load(f)
            if isinstance(ex, dict) and "cards" in ex and "tiles" in ex:
                return ex
        except (OSError, ValueError):
            pass
        return None

    if _fresh(png_path) and _load_extras() is not None:
        with open(txt_path) as f:
            read = f.read()
        as_of = open(meta_path).read() if os.path.exists(meta_path) else ""
        return png_path, read, as_of, _load_extras(), True, left
    if left <= 0:
        raise PermissionError(
            "You've used all 100 free heatmaps for this week. "
            "Your quota resets 7 days after your first one.")
    with BUILD_LOCK:
        if _fresh(png_path) and _load_extras() is not None:
            with open(txt_path) as f:
                read = f.read()
            as_of = open(meta_path).read() if os.path.exists(meta_path) else ""
            return png_path, read, as_of, _load_extras(), True, left
        d = gex_heatmap.heatmap_data(ticker)
        render_mpl.render(d, png_path)
        read = gex_heatmap.describe(d)
        ex = gex_heatmap.page_extras(d)
        with open(txt_path, "w") as f:
            f.write(read)
        with open(meta_path, "w") as f:
            f.write(d.get("as_of_ts", ""))
        with open(extras_path, "w") as f:
            json.dump(ex, f)
        record_build(tg_id, username, ticker)
        return png_path, read, d.get("as_of_ts", ""), ex, False, uses_left(tg_id)


# ---------- pages ----------
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SPY BEFORE YOU BUY - GEX Heatmap</title>
<style>
  body { background:#000; color:#fff; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; margin:0; padding:24px 16px 48px; }
  .wrap { max-width:920px; margin:0 auto; text-align:center; }
  h1 { font-size:clamp(28px,6vw,52px); font-weight:800; letter-spacing:2px; margin:24px 0 4px; }
  .tag { color:#30857a; letter-spacing:6px; font-size:12px; font-weight:600; margin-bottom:28px; }
  .card { background:#0b0e13; border:1px solid #1c232e; border-radius:14px; padding:32px 24px; max-width:520px; margin:0 auto; }
  .card p { color:#aaa; font-size:15px; line-height:1.6; }
  .foot { color:#555; font-size:12px; margin-top:36px; }
  .foot a { color:#30857a; text-decoration:none; }
</style></head><body><div class="wrap">
  <h1>SPY BEFORE YOU BUY</h1>
  <div class="tag">WAIT. CONFIRM. EXECUTE.</div>
  <div class="card">
    <h2 style="margin-top:0;">Members-only GEX Heatmaps</h2>
    <p>This tool is for Spy Before You Buy channel members.<br>Log in with Telegram to check your membership.</p>
    __WIDGET__
  </div>
  <div class="foot">More from <a href="https://x.com/spybef0rey0ubuy">@spybef0rey0ubuy</a></div>
</div></body></html>
"""

APP_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SPY BEFORE YOU BUY - GEX Heatmap</title>
<style>
  * { box-sizing: border-box; }
  body { background:#000; color:#fff; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; margin:0; padding:24px 16px 48px; overflow-x:hidden; }
  .wrap { max-width:920px; margin:0 auto; text-align:center; }
  h1 { font-size:clamp(28px,6vw,52px); font-weight:800; letter-spacing:2px; margin:24px 0 4px; }
  .tag { color:#30857a; letter-spacing:6px; font-size:12px; font-weight:600; margin-bottom:28px; }
  .who { color:#888; font-size:13px; margin-bottom:16px; }
  .who a { color:#30857a; text-decoration:none; }
  form { display:flex; gap:10px; justify-content:center; margin-bottom:10px; flex-wrap:wrap; }
  input { background:#111; border:1px solid #333; color:#fff; font-size:18px; padding:12px 16px; border-radius:10px; width:200px; text-transform:uppercase; text-align:center; }
  input:focus { outline:none; border-color:#30857a; }
  button { background:#30857a; color:#fff; border:none; font-size:16px; font-weight:700; padding:12px 28px; border-radius:10px; cursor:pointer; }
  button:disabled { opacity:.5; cursor:wait; }
  #err { color:#f87171; margin:16px 0; display:none; }
  #result { display:none; margin-top:8px; }
  #verdict { display:none; width:100vw; margin-left:calc(50% - 50vw); text-align:center; padding:14px 16px; font-size:19px; font-weight:700; border-top:1px solid; border-bottom:1px solid; }
  #verdict small { display:block; font-weight:400; font-size:13px; color:#aaa; margin-top:4px; }
  #verdict.bearish { background:rgba(239,68,68,.12); border-color:rgba(239,68,68,.5); }
  #verdict.bearish .lv { color:#f87171; }
  #verdict.bullish { background:rgba(34,197,94,.12); border-color:rgba(34,197,94,.5); }
  #verdict.bullish .lv { color:#4ade80; }
  #verdict.neutral { background:rgba(148,163,184,.10); border-color:rgba(148,163,184,.4); }
  #verdict.neutral .lv { color:#cbd5e1; }
  .bleed { width:100vw; margin-left:calc(50% - 50vw); }
  .bleed img { width:100%; display:block; border-top:1px solid #222; border-bottom:1px solid #222; }
  #tiles { display:none; grid-template-columns:repeat(3,1fr); gap:8px; margin:0 auto 18px; max-width:1100px; padding:0 16px; }
  .tile { background:#0b0e13; border:1px solid #1c232e; border-radius:10px; padding:10px 6px; }
  .tile .k { font-size:10px; letter-spacing:2px; color:#777; }
  .tile .k.long { font-size:8.5px; letter-spacing:1px; white-space:nowrap; }
  .tile .v { font-size:17px; font-weight:700; margin-top:4px; }
  .tile .v.r { color:#f87171; } .tile .v.g { color:#4ade80; } .tile .v.y { color:#facc15; }
  @media (max-width:700px){ #tiles{ grid-template-columns:repeat(3,1fr); } }
  #steps { display:none; max-width:1100px; margin:26px auto 0; padding:0 16px; grid-template-columns:repeat(3,1fr); gap:14px; text-align:left; }
  @media (max-width:700px){ #steps{ grid-template-columns:1fr; } }
  .step { background:#0b0e13; border:1px solid #1c232e; border-radius:14px; padding:20px; }
  .step.hot { border-color:rgba(250,204,21,.55); }
  .step .num { display:inline-flex; width:34px; height:34px; border-radius:50%; background:#30857a; color:#fff; font-weight:800; align-items:center; justify-content:center; margin-bottom:12px; }
  .step.hot .num { background:#facc15; color:#000; }
  .step h4 { font-size:14px; letter-spacing:1px; margin:0 0 10px; }
  .step p { font-size:14px; line-height:1.7; color:#bbb; margin:0; }
  .step p b { color:#fff; }
  .step p b.r { color:#f87171; } .step p b.g { color:#4ade80; } .step p b.y { color:#facc15; }
  .step ul { list-style:none; margin:10px 0 0; padding:0; }
  .step li { position:relative; font-size:14px; line-height:1.55; color:#bbb; padding:5px 0 5px 20px; }
  .step li:before { content:"-"; position:absolute; left:4px; top:5px; color:#30857a; font-weight:800; font-size:15px; }
  .step li b { color:#fff; }
  .step li b.r { color:#f87171; } .step li b.g { color:#4ade80; } .step li b.y { color:#facc15; }
  .play { display:none; background:linear-gradient(180deg,#10151c,#0b0e13); border:1px solid #facc15; border-radius:16px; padding:26px 28px; margin:26px auto 0; max-width:1100px; text-align:left; }
  .play .kicker { color:#facc15; font-size:12px; font-weight:800; letter-spacing:3px; margin-bottom:10px; }
  .play .big { font-size:30px; font-weight:800; margin-bottom:10px; }
  .play .big .put { color:#f87171; } .play .big .call { color:#4ade80; }
  .play p { color:#ccc; font-size:15px; line-height:1.6; margin:0; }
  .play p b { color:#fff; }
  .play .why { margin-top:12px; padding-top:12px; border-top:1px solid #222; color:#999; font-size:14px; }
  .play .facts { color:#999; font-size:14px; line-height:1.5; margin:4px 0 12px; }
  .play ul.pbul { list-style:none; margin:0; padding:0; }
  .play ul.pbul li { position:relative; color:#ccc; font-size:15px; line-height:1.6; padding:5px 0 5px 20px; }
  .play ul.pbul li:before { content:"-"; position:absolute; left:4px; top:5px; color:#facc15; font-weight:800; }
  .fine { color:#999; font-size:13px; line-height:1.6; margin:26px auto 0; max-width:1100px; text-align:center; padding:16px 16px 0; border-top:1px solid #1c232e; }
  .foot { color:#555; font-size:12px; margin-top:36px; }
  .foot a { color:#30857a; text-decoration:none; }
</style></head><body><div class="wrap">
  <h1>SPY BEFORE YOU BUY</h1>
  <div class="tag">WAIT. CONFIRM. EXECUTE.</div>
  <div class="who">Logged in as <b>__NAME__</b> &middot; <a href="/api/logout">log out</a></div>
  <h2 style="font-weight:600; font-size:20px; margin-bottom:6px;">GEX Heatmap</h2>
  <p style="color:#888; font-size:14px; margin-top:0;">Type any ticker. See where the gamma walls are.</p>
  <form id="f">
    <input id="t" placeholder="$AAPL" maxlength="8" autocomplete="off">
    <button id="b" type="submit">Generate</button>
  </form>
  <div id="err"></div>
  <div id="result">
    <div id="tiles"></div>
    <div id="verdict"></div>
    <div class="bleed"><img id="img" alt="GEX heatmap"></div>
    <div id="steps"></div>
    <div class="play" id="play"></div>
    <p class="fine" id="fine" style="display:none">Not financial advice. This tool is for educational purposes only - options trading involves substantial risk of loss. Always do your own research before making any trade.</p>
  </div>
  <div class="foot">Options data delayed ~15 min (CBOE)<br>
  More from <a href="https://x.com/spybef0rey0ubuy">@spybef0rey0ubuy</a></div>
</div>
<script>
const f=document.getElementById('f'), t=document.getElementById('t'),
      b=document.getElementById('b'), err=document.getElementById('err'),
      res=document.getElementById('result'), img=document.getElementById('img'),
      v=document.getElementById('verdict'), tiles=document.getElementById('tiles'),
      steps=document.getElementById('steps'), fine=document.getElementById('fine'),
      play=document.getElementById('play');
function esc(s){ return String(s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
f.addEventListener('submit', async e=>{
  e.preventDefault();
  const ticker=t.value.trim().toUpperCase().replace(/^\\$/,'');
  if(!ticker) return;
  err.style.display='none'; res.style.display='none';
  b.disabled=true;
  const STEPS=['Fetching option chain...','Crunching gamma...','Rendering heatmap...'];
  let si=0; b.textContent=STEPS[0];
  const stepTimer=setInterval(()=>{ si=(si+1)%STEPS.length; b.textContent=STEPS[si]; },1400);
  try{
    const r=await fetch('/api/go?ticker='+encodeURIComponent(ticker));
    const j=await r.json();
    if(!r.ok) throw new Error(j.detail||'Something went wrong');
    const ex=j.extras||{}, vd=ex.verdict||{};
    tiles.innerHTML=(ex.tiles||[]).map(tl=>'<div class="tile"><div class="k'+(tl.k.length>10?' long':'')+'">'+esc(tl.k)+'</div><div class="v '+esc(tl.c||'')+'">'+esc(tl.v)+'</div></div>').join('');
    tiles.style.display='grid';
    v.className=vd.lean==='BEARISH'?'bearish':vd.lean==='BULLISH'?'bullish':'neutral';
    v.innerHTML='<span class="lv">'+esc(vd.headline||'')+'</span> - '+esc(vd.clause||'');
    v.style.display='block';
    img.src='/i/'+encodeURIComponent(ticker)+'.png?ts='+Date.now();
    steps.innerHTML=(ex.cards||[]).map(c=>{
      const pts=(c.points||[]).map(p=>'<li>'+p+'</li>').join('');
      const body=pts?'<ul>'+pts+'</ul>':(c.html?'<p>'+c.html+'</p>':'');
      return '<div class="step'+(c.hot?' hot':'')+'"><div class="num">'+c.n+'</div><h4>'+esc(c.title)+'</h4>'+body+'</div>';
    }).join('');
    steps.style.display='grid';
    const pl=ex.play;
    if(pl){
      const ppts=(pl.points||[]).map(p=>'<li>'+p+'</li>').join('');
      play.innerHTML='<div class="kicker">MAP POINTS TO</div>'
        +'<div class="big"><span class="'+(pl.side==='puts'?'put':'call')+'">'+esc(pl.strike)+' '+esc(pl.side)+'</span> / '+esc(pl.expiry)+' expiry</div>'
        +'<div class="facts">'+esc(pl.facts||'')+'</div>'
        +'<ul class="pbul">'+ppts+'</ul>';
      play.style.display='block';
    } else { play.style.display='none'; }
    fine.style.display='block';
    res.style.display='block';
    res.scrollIntoView({behavior:'smooth', block:'nearest'});
  }catch(ex){ err.textContent=ex.message; err.style.display='block'; }
  clearInterval(stepTimer);
  b.disabled=false; b.textContent='Generate';
});
</script></body></html>
"""


def login_page() -> str:
    if PUBLIC_URL and BOT_TOKEN:
        widget = (
            f'<script async src="https://telegram.org/js/telegram-widget.js?22" '
            f'data-telegram-login="{BOT_USERNAME}" data-size="large" '
            f'data-auth-url="{PUBLIC_URL}/api/tg-auth" '
            f'data-request-access="write"></script>')
    else:
        widget = ('<p style="color:#f87171">Login not configured yet - '
                  'the site owner needs to set PUBLIC_URL and TELEGRAM_BOT_TOKEN.</p>')
    return LOGIN_PAGE.replace("__WIDGET__", widget)


# ---------- routes ----------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    u = session_user(request)
    if not u:
        return login_page()
    name = "@" + u["username"] if u.get("username") else u.get("first_name", "member")
    return APP_PAGE.replace("__NAME__", name)


@app.get("/api/tg-auth")
def tg_auth(request: Request):
    user = verify_tg_auth(dict(request.query_params))
    if not user:
        return JSONResponse(
            {"detail": "Telegram login failed verification."}, status_code=403)
    tg_id = int(user["id"])
    if not is_channel_member(tg_id):
        return HTMLResponse(
            "<body style='background:#000;color:#fff;font-family:sans-serif;text-align:center;padding:60px'>"
            "<h2>This tool is for channel members.</h2>"
            "<p>Join the Spy Before You Buy channel, then log in again.</p></body>",
            status_code=403)
    sess = signer.dumps({"tg_id": tg_id,
                         "username": user.get("username", ""),
                         "first_name": user.get("first_name", "")})
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("sbyb_session", sess, httponly=True, samesite="lax",
                    max_age=30 * 24 * 3600,
                    secure=PUBLIC_URL.startswith("https://"))
    return resp


@app.get("/api/logout")
def logout():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("sbyb_session")
    return resp


@app.get("/api/quota")
def quota(request: Request):
    u, err = need_user(request)
    if err:
        return err
    return {"left": uses_left(int(u["tg_id"])), "limit": WEEKLY_LIMIT}


@app.get("/api/go")
def go(ticker: str, request: Request):
    u, err = need_user(request)
    if err:
        return err
    ticker = ticker.strip().upper().lstrip("$")
    if not TICKER_RE.match(ticker):
        return JSONResponse(
            {"detail": "Enter a valid ticker (letters only, e.g. AAPL)."},
            status_code=400)
    try:
        _png, read, as_of, ex, _cached, left = get_or_build(ticker, u)
    except PermissionError as e:
        return JSONResponse({"detail": str(e)}, status_code=429)
    except RuntimeError:
        return JSONResponse(
            {"detail": f"No options data for {ticker} - check the ticker and try again."},
            status_code=404)
    except Exception:
        return JSONResponse(
            {"detail": "Couldn't build the heatmap right now - try again in a minute."},
            status_code=500)
    return {"read": read, "as_of": as_of, "extras": ex, "left": left}


@app.get("/i/{name}.png")
def image(name: str, request: Request):
    u, err = need_user(request)
    if err:
        return err
    ticker = name.strip().upper()
    if not TICKER_RE.match(ticker):
        return JSONResponse({"detail": "bad ticker"}, status_code=400)
    png_path, _, _ = _paths(ticker)
    if not os.path.exists(png_path):
        return JSONResponse({"detail": "not built yet"}, status_code=404)
    return FileResponse(png_path, media_type="image/png")
