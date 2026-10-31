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
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#000000">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="GEX Heatmap">
<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 440px) and (device-height: 956px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1320x2868.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 430px) and (device-height: 932px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1290x2796.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 428px) and (device-height: 926px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1284x2778.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 393px) and (device-height: 852px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1179x2556.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 390px) and (device-height: 844px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1170x2532.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 375px) and (device-height: 812px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1125x2436.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 414px) and (device-height: 896px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1242x2688.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 414px) and (device-height: 896px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-828x1792.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 414px) and (device-height: 736px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1242x2208.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 375px) and (device-height: 667px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-750x1334.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 320px) and (device-height: 568px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-640x1136.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 1024px) and (device-height: 1366px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-2048x2732.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 834px) and (device-height: 1194px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-1668x2388.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 820px) and (device-height: 1180px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-1640x2360.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 768px) and (device-height: 1024px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-1536x2048.png">
<link rel="icon" type="image/png" sizes="192x192" href="/icons/icon-192.png">
<script>if('serviceWorker' in navigator){window.addEventListener('load',function(){navigator.serviceWorker.register('/sw.js').catch(function(){})})}</script>
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
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#000000">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="GEX Heatmap">
<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 440px) and (device-height: 956px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1320x2868.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 430px) and (device-height: 932px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1290x2796.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 428px) and (device-height: 926px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1284x2778.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 393px) and (device-height: 852px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1179x2556.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 390px) and (device-height: 844px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1170x2532.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 375px) and (device-height: 812px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1125x2436.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 414px) and (device-height: 896px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1242x2688.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 414px) and (device-height: 896px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-828x1792.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 414px) and (device-height: 736px) and (-webkit-device-pixel-ratio: 3) and (orientation: portrait)" href="/splash/splash-1242x2208.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 375px) and (device-height: 667px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-750x1334.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 320px) and (device-height: 568px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-640x1136.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 1024px) and (device-height: 1366px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-2048x2732.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 834px) and (device-height: 1194px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-1668x2388.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 820px) and (device-height: 1180px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-1640x2360.png">
<link rel="apple-touch-startup-image" media="screen and (device-width: 768px) and (device-height: 1024px) and (-webkit-device-pixel-ratio: 2) and (orientation: portrait)" href="/splash/splash-1536x2048.png">
<link rel="icon" type="image/png" sizes="192x192" href="/icons/icon-192.png">
<script>if('serviceWorker' in navigator){window.addEventListener('load',function(){navigator.serviceWorker.register('/sw.js').catch(function(){})})}</script>
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
  @media (min-width:1400px){ .bleed img { width:88%; margin-left:auto; margin-right:auto; } }
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
  <div id="installTip" style="display:none;background:#0b0e13;border:1px solid #30857a;border-radius:10px;padding:10px 14px;margin:0 auto 16px;max-width:520px;font-size:13px;color:#bbb;text-align:left;">
    Get the app version - <b style="color:#fff">add this page to your home screen</b> and it opens full-screen with its own icon, no app store needed. <span id="installHow"></span>
    <button id="installBtn" type="button" style="display:none;background:#30857a;color:#fff;border:0;border-radius:8px;padding:7px 14px;font-size:13px;font-weight:700;cursor:pointer;margin-top:8px;">Install app</button>
    <a href="#" id="installX" style="color:#30857a;text-decoration:none;float:right;">dismiss</a>
  </div>
  <script>
    (function(){
      var standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone;
      if (standalone || localStorage.getItem('sbybInstallDismissed')) return;
      var ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
      var how = document.getElementById('installHow');
      how.textContent = ios ? 'On iPhone: tap Share, then Add to Home Screen.' : 'On Android: tap the browser menu, then Install app or Add to Home screen.';
      var tip = document.getElementById('installTip');
      var btn = document.getElementById('installBtn');
      tip.style.display = 'block';
      document.getElementById('installX').addEventListener('click', function(e){ e.preventDefault(); localStorage.setItem('sbybInstallDismissed','1'); tip.style.display = 'none'; });
      var deferredPrompt = null;
      window.addEventListener('beforeinstallprompt', function(e){
        e.preventDefault();
        deferredPrompt = e;
        how.textContent = 'On Android: tap Install app - one tap, no menu hunting.';
        btn.style.display = 'inline-block';
        tip.style.display = 'block';
      });
      btn.addEventListener('click', function(){
        if (!deferredPrompt) return;
        deferredPrompt.prompt();
        deferredPrompt.userChoice.then(function(choice){
          deferredPrompt = null;
          btn.style.display = 'none';
          if (choice && choice.outcome === 'accepted') { tip.style.display = 'none'; }
        });
      });
      window.addEventListener('appinstalled', function(){ tip.style.display = 'none'; });
    })();
  </script>
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
  <div class="foot">Options data may be delayed (CBOE)<br>
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
  const STEPS=['Fetching option chain...','Crunching gamma...','Loading heatmap...'];
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


# ---------- subscriber disclaimer gate ----------
# The link Justin DMs a new X subscriber is /agree/<invite-code>. The page
# shows the full terms; clicking agree logs (code, time, IP, user agent) in
# usage.db and redirects to the one-time Telegram invite https://t.me/+<code>.
DISC_PATH = os.path.join(APP_DIR, "disclaimer.md")
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def _md_to_html(md: str) -> str:
    import html as _html
    out, para, lst = [], [], None  # lst: current <li> buffer or None

    def flush_para():
        if para:
            out.append(f"<p>{_html.escape(' '.join(para))}</p>")
            para.clear()

    def flush_li():
        nonlocal lst
        if lst is not None:
            out.append(f"<li>{_html.escape(' '.join(lst))}</li>")
            lst = None

    in_list = False
    for raw in md.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if s.startswith("## "):
            flush_para(); flush_li()
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<h2>{_html.escape(s[3:])}</h2>")
        elif s.startswith("# "):
            flush_para(); flush_li()
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<h1>{_html.escape(s[2:])}</h1>")
        elif s == "---":
            flush_para(); flush_li()
            if in_list: out.append("</ul>"); in_list = False
            out.append("<hr>")
        elif s.startswith("- "):
            flush_para(); flush_li()
            if not in_list: out.append("<ul>"); in_list = True
            lst = [s[2:]]
        elif not s:
            flush_para(); flush_li()
            if in_list: out.append("</ul>"); in_list = False
        elif line[:1] in (" ", "\t") and lst is not None:
            lst.append(s)
        elif s.startswith("**") and s.endswith("**"):
            flush_para(); flush_li()
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<p class='ver'>{_html.escape(s.strip('*'))}</p>")
        else:
            if in_list and lst is not None and line[:1] in (" ", "\t"):
                lst.append(s)
            else:
                flush_li()
                if in_list: out.append("</ul>"); in_list = False
                para.append(s)
    flush_para(); flush_li()
    if in_list: out.append("</ul>")
    return "\n".join(out)


AGREE_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spy Before You Buy - Terms and Conditions</title>
<style>
body{background:#000;color:#e8ecf1;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;margin:0;padding:24px 16px 140px;}
.wrap{max-width:760px;margin:0 auto;}
.brand{color:#30857a;font-weight:800;letter-spacing:2px;font-size:13px;text-align:center;margin-bottom:4px;}
.tagline{color:#8b94a3;font-size:11px;letter-spacing:3px;text-align:center;margin-bottom:28px;}
h1{font-size:22px;line-height:1.3;}
h2{font-size:16px;margin-top:28px;color:#fff;}
p,li{font-size:14px;line-height:1.65;color:#c9d1d9;}
.ver{color:#8b94a3;font-size:12px;}
hr{border:none;border-top:1px solid #222;margin:24px 0;}
.bar{position:fixed;left:0;right:0;bottom:0;background:#0b0e13;border-top:1px solid #1c232e;padding:14px 16px;text-align:center;}
button{background:#30857a;color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:700;padding:14px 22px;width:100%;max-width:760px;cursor:pointer;}
.nope{color:#8b94a3;font-size:12px;margin-top:10px;}
</style></head><body><div class="wrap">
<div class="brand">SPY BEFORE YOU BUY</div>
<div class="tagline">WAIT. CONFIRM. EXECUTE.</div>
__BODY__
</div>
<div class="bar">
<form method="post" action="/agree/__CODE__/accept">
<button type="submit">Yes, I fully understand and agree</button>
</form>
<div class="nope">If you do not agree, close this page and do not enter the channel.</div>
</div>
</body></html>
"""


@app.get("/agree/{code}", response_class=HTMLResponse)
def agree_page(code: str):
    if not CODE_RE.match(code):
        return HTMLResponse("Invalid link.", status_code=404)
    try:
        with open(DISC_PATH) as f:
            body = _md_to_html(f.read())
    except OSError:
        return HTMLResponse("Terms unavailable - contact the owner.", status_code=500)
    return AGREE_PAGE.replace("__BODY__", body).replace("__CODE__", code)


@app.post("/agree/{code}/accept")
def agree_accept(code: str, request: Request):
    if not CODE_RE.match(code):
        return JSONResponse({"detail": "bad link"}, status_code=404)
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else ""))
    con = _db()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS agreements
                       (code TEXT, ts REAL, ip TEXT, ua TEXT)""")
        con.execute(
            "INSERT INTO agreements (code, ts, ip, ua) VALUES (?,?,?,?)",
            (code, time.time(), ip, request.headers.get("user-agent", "")[:300]))
        con.commit()
    finally:
        con.close()
    return RedirectResponse(f"https://t.me/+{code}", status_code=302)


# ---------- routes ----------
# ---------- installable app (PWA): manifest, service worker, icons ------
# Public on purpose: browsers fetch these before login and they carry no
# member data. The service worker never caches pages or API responses -
# only the icons - so login and membership checks always hit the network.
MANIFEST = {
    "name": "SPY BEFORE YOU BUY - GEX Heatmap",
    "short_name": "GEX Heatmap",
    "description": ("Members-only GEX heatmap tool - see where the gamma "
                    "walls are on any ticker."),
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#000000",
    "theme_color": "#000000",
    "icons": [
        {"src": "/icons/icon-192.png", "sizes": "192x192",
         "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-512.png", "sizes": "512x512",
         "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-maskable-512.png", "sizes": "512x512",
         "type": "image/png", "purpose": "maskable"},
    ],
}

SW_JS = """// Installable-app service worker. Network for everything except the
// icons, which are cache-first. Pages, logins and heatmaps are never
// cached, so membership checks stay live.
var CACHE = 'sbyb-icons-v3';
var ICONS = ['/icons/icon-192.png', '/icons/icon-512.png',
             '/icons/icon-maskable-512.png', '/icons/apple-touch-icon.png'];
self.addEventListener('install', function(e) {
  e.waitUntil(caches.open(CACHE).then(function(c) {
    return c.addAll(ICONS);
  }).then(function() { return self.skipWaiting(); }));
});
self.addEventListener('activate', function(e) {
  e.waitUntil(self.clients.claim());
});
self.addEventListener('fetch', function(e) {
  var url = new URL(e.request.url);
  if (url.pathname.indexOf('/icons/') === 0) {
    e.respondWith(caches.match(e.request).then(function(r) {
      return r || fetch(e.request);
    }));
  }
});
"""


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return HTMLResponse(SW_JS, media_type="application/javascript")


@app.get("/icons/{name}")
def icon(name: str):
    safe = {"icon-192.png", "icon-512.png", "icon-maskable-512.png",
            "apple-touch-icon.png"}
    if name not in safe:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(os.path.join(APP_DIR, "static", "icons", name),
                        media_type="image/png")


@app.get("/splash/{name}")
def splash(name: str):
    if not re.fullmatch(r"splash-\d+x\d+\.png", name):
        return JSONResponse({"error": "not found"}, status_code=404)
    fp = os.path.join(APP_DIR, "static", "splash", name)
    if not os.path.isfile(fp):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(fp, media_type="image/png")


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


# ---------- website subscriptions (Stripe -> Telegram access) ----------
# The Spy Before You Buy site sells subscriptions WITHOUT X (Justin's
# order, Oct 29 2026). Flow: Stripe Payment Link checkout -> webhook here
# records the subscriber and mints a personal one-time Telegram invite ->
# buyer lands on /access and gets the disclaimer-gate link -> the VM-side
# roster sync (telegram-subscribers/site_sync.py) mirrors the row, tracks
# the join, and removes the member when the subscription lapses, is
# cancelled past its paid period, or is refunded.
#
# Everything below is INERT until these env vars are set on Render:
#   STRIPE_WEBHOOK_SECRET - webhook signing secret (turns the webhook on)
#   SITE_SUBS_READ_KEY    - shared secret for the /stripe/subs read API
#   STRIPE_PLAN_MAP       - optional JSON mapping a payment-link id,
#                           price id, or amount_total (cents, as string)
#                           to a plan label; the checkout metadata "plan"
#                           wins when present.
# No new dependencies: the webhook signature is verified with stdlib HMAC.
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SITE_SUBS_READ_KEY = os.environ.get("SITE_SUBS_READ_KEY", "")
SBYB_TEST_MODE = os.environ.get("SBYB_TEST_MODE", "") == "1"
try:
    STRIPE_PLAN_MAP = json.loads(os.environ.get("STRIPE_PLAN_MAP", "{}"))
except Exception:
    STRIPE_PLAN_MAP = {}
GATE_BASE = PUBLIC_URL or "https://sbyb-gex-heatmap.onrender.com"
IO_PLAN = "Indicators Only"  # TradingView access only - never Telegram
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PORTAL_URL = os.environ.get("STRIPE_PORTAL_URL", "")


def _subs_db():
    con = _db()
    con.execute("""CREATE TABLE IF NOT EXISTS site_subs(
        email TEXT PRIMARY KEY,
        plan TEXT, status TEXT,
        stripe_customer_id TEXT, stripe_subscription_id TEXT,
        checkout_session_id TEXT, invite_link TEXT,
        current_period_end REAL, created_ts REAL, updated_ts REAL)""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(site_subs)")}
    if "tv_username" not in cols:
        con.execute("ALTER TABLE site_subs ADD COLUMN tv_username TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS stripe_events(
        event_id TEXT PRIMARY KEY, type TEXT, ts REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS tv_claims(
        email TEXT, tv_username TEXT, matched INTEGER, ts REAL)""")
    return con


def _verify_stripe_sig(payload: bytes, sig_header: str, secret: str) -> bool:
    """Standard Stripe webhook signature check (t + '.' + raw body,
    HMAC-SHA256, any matching v1 wins, 5-minute timestamp tolerance)."""
    if not sig_header:
        return False
    ts, sigs = "", []
    for part in sig_header.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == "t":
            ts = v.strip()
        elif k.strip() == "v1":
            sigs.append(v.strip())
    if not ts or not sigs:
        return False
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except ValueError:
        return False
    signed = ts.encode() + b"." + payload
    expect = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expect, s) for s in sigs)


def _make_site_invite(email: str):
    """Personal one-time channel invite for a site subscriber."""
    if SBYB_TEST_MODE:
        code = hashlib.sha256(
            f"test:{email}:{time.time()}".encode()).hexdigest()[:16]
        return f"https://t.me/+TEST{code}"
    if not BOT_TOKEN:
        return None
    try:
        res = _tg_api("createChatInviteLink", {
            "chat_id": CHANNEL_ID,
            "name": ("site:" + email)[:32],
            "member_limit": 1,
        })
        return res.get("result", {}).get("invite_link")
    except Exception:
        return None


def _sub_plan(obj, session=None):
    meta = (session or obj).get("metadata") or {}
    if meta.get("plan"):
        return meta["plan"]
    for key in (obj.get("payment_link"), str(obj.get("amount_total") or "")):
        if key and key in STRIPE_PLAN_MAP:
            return STRIPE_PLAN_MAP[key]
    return "Membership"


def _on_checkout_completed(con, obj):
    email = ((obj.get("customer_details") or {}).get("email")
             or obj.get("customer_email") or "")
    email = email.strip().lower()
    if not email:
        return
    mode = obj.get("mode")
    if mode == "payment":
        status = "lifetime"
    elif obj.get("payment_status") == "no_payment_required":
        # Free-trial start: full access now, first charge when it converts.
        status = "trialing"
    else:
        status = "active"
    tv_username = None
    for f in obj.get("custom_fields") or []:
        key = (f.get("key") or "").lower()
        label = ((f.get("label") or {}).get("custom") or "").lower()
        if "tradingview" in key or "tradingview" in label:
            tv_username = ((f.get("text") or {}).get("value") or "").strip() \
                or None
    plan = _sub_plan(obj)
    row = con.execute(
        """SELECT invite_link, status, stripe_subscription_id
           FROM site_subs WHERE email=?""", (email,)).fetchone()
    invite = row[0] if row else None
    old_sub_id = row[2] if row else None
    if plan == IO_PLAN:
        invite = None  # Indicators Only never gets a Telegram invite
    elif not invite or (row and row[1] in ("canceled", "unpaid", "refunded")):
        invite = _make_site_invite(email) or invite
    now = time.time()
    con.execute(
        """INSERT INTO site_subs(email, plan, status, stripe_customer_id,
               stripe_subscription_id, checkout_session_id, invite_link,
               current_period_end, created_ts, updated_ts, tv_username)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(email) DO UPDATE SET
             plan=excluded.plan, status=excluded.status,
             stripe_customer_id=excluded.stripe_customer_id,
             checkout_session_id=excluded.checkout_session_id,
             invite_link=excluded.invite_link, updated_ts=excluded.updated_ts,
             stripe_subscription_id=COALESCE(
                 excluded.stripe_subscription_id,
                 site_subs.stripe_subscription_id),
             tv_username=COALESCE(excluded.tv_username, site_subs.tv_username)""",
        (email, plan, status, obj.get("customer"),
         obj.get("subscription"), obj.get("id"), invite, None, now, now,
         tv_username))
    if mode == "payment" and old_sub_id:
        # Lifetime bought: stop the recurring subscription so the member
        # is never double-charged. If the cancel cannot run (no API key
        # configured), the id stays on file and the roster sync flags
        # the overlap for manual cancelling.
        if _cancel_stripe_subscription(old_sub_id):
            con.execute(
                """UPDATE site_subs SET stripe_subscription_id=NULL
                   WHERE email=?""", (email,))


def _find_sub(con, obj):
    sub_id = obj.get("id") if obj.get("object") == "subscription" \
        else obj.get("subscription")
    row = None
    if sub_id:
        row = con.execute(
            "SELECT email FROM site_subs WHERE stripe_subscription_id=?",
            (sub_id,)).fetchone()
    if not row and obj.get("customer"):
        row = con.execute(
            "SELECT email FROM site_subs WHERE stripe_customer_id=?",
            (obj["customer"],)).fetchone()
    return row[0] if row else None


def _sub_updated_plan(obj):
    """Plan label for a subscription object, from its price id via
    STRIPE_PLAN_MAP (price ids are the keys used at activation)."""
    for item in (obj.get("items") or {}).get("data") or []:
        price_id = ((item.get("price") or {}).get("id")) or ""
        if price_id in STRIPE_PLAN_MAP:
            return STRIPE_PLAN_MAP[price_id]
    return None


def _cancel_stripe_subscription(sub_id):
    """Cancel a recurring subscription via the Stripe API (used when a
    member buys Lifetime so they are never double-charged). Needs
    STRIPE_SECRET_KEY; without it the roster sync flags the overlap
    for manual cancelling instead."""
    if not STRIPE_SECRET_KEY or not sub_id or SBYB_TEST_MODE:
        return False
    try:
        req = urllib.request.Request(
            f"https://api.stripe.com/v1/subscriptions/{sub_id}",
            method="DELETE")
        req.add_header("Authorization", f"Bearer {STRIPE_SECRET_KEY}")
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception:
        return False


def _on_subscription_updated(con, obj):
    email = _find_sub(con, obj)
    if not email:
        return
    con.execute(
        """UPDATE site_subs SET status=?, current_period_end=?,
           stripe_subscription_id=?, updated_ts=? WHERE email=?""",
        (obj.get("status"), obj.get("current_period_end"),
         obj.get("id"), time.time(), email))
    # Plan switches (upgrades/downgrades via the Stripe customer portal)
    # change entitlements: Indicators Only has no Telegram access, full
    # plans do. Moving up to a full plan mints a fresh invite; the
    # roster sync enforces the Telegram side from the plan on file.
    new_plan = _sub_updated_plan(obj)
    if new_plan:
        row = con.execute(
            "SELECT plan, invite_link FROM site_subs WHERE email=?",
            (email,)).fetchone()
        old_plan, invite = row if row else (None, None)
        if new_plan != old_plan:
            if new_plan != IO_PLAN and (old_plan == IO_PLAN or not invite):
                invite = _make_site_invite(email) or invite
                con.execute(
                    "UPDATE site_subs SET invite_link=? WHERE email=?",
                    (invite, email))
            con.execute(
                "UPDATE site_subs SET plan=? WHERE email=?",
                (new_plan, email))


def _on_subscription_deleted(con, obj):
    email = _find_sub(con, obj)
    if email:
        con.execute(
            """UPDATE site_subs SET status='canceled',
               current_period_end=?, updated_ts=? WHERE email=?""",
            (obj.get("current_period_end") or time.time(),
             time.time(), email))


def _on_payment_failed(con, obj):
    email = _find_sub(con, obj)
    if email:
        con.execute(
            """UPDATE site_subs SET status='past_due', updated_ts=?
               WHERE email=? AND status IN ('active','trialing')""",
            (time.time(), email))


def _on_charge_reversed(con, obj):
    email = _find_sub(con, obj)
    if email:
        con.execute(
            "UPDATE site_subs SET status='refunded', updated_ts=? WHERE email=?",
            (time.time(), email))


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        return JSONResponse({"detail": "webhooks not configured"},
                            status_code=503)
    payload = await request.body()
    if not _verify_stripe_sig(payload,
                               request.headers.get("stripe-signature", ""),
                               STRIPE_WEBHOOK_SECRET):
        return JSONResponse({"detail": "bad signature"}, status_code=400)
    try:
        event = json.loads(payload)
    except Exception:
        return JSONResponse({"detail": "bad payload"}, status_code=400)
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    con = _subs_db()
    try:
        cur = con.execute(
            "INSERT OR IGNORE INTO stripe_events(event_id, type, ts) "
            "VALUES (?,?,?)", (event.get("id", ""), etype, time.time()))
        con.commit()
        if cur.rowcount == 0:
            return {"received": True, "duplicate": True}
        if etype == "checkout.session.completed":
            _on_checkout_completed(con, obj)
        elif etype == "customer.subscription.updated":
            _on_subscription_updated(con, obj)
        elif etype == "customer.subscription.deleted":
            _on_subscription_deleted(con, obj)
        elif etype == "invoice.payment_failed":
            _on_payment_failed(con, obj)
        elif etype in ("charge.refunded", "charge.dispute.created"):
            _on_charge_reversed(con, obj)
        con.commit()
    finally:
        con.close()
    return {"received": True}


ACCESS_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SPY BEFORE YOU BUY - Your access</title>
<style>
body{background:#000;color:#fff;font-family:Arial,sans-serif;text-align:center;
padding:64px 24px;margin:0}
h1{font-stretch:condensed;letter-spacing:2px;font-size:34px;margin:0 0 6px}
.tag{color:#5e8f86;letter-spacing:4px;font-size:12px;margin-bottom:36px}
p{color:#cfd8d5;max-width:520px;margin:0 auto 28px;line-height:1.55}
a.btn{display:inline-block;background:#5e8f86;color:#fff;text-decoration:none;
padding:14px 30px;border-radius:999px;font-weight:bold;letter-spacing:1px}
.small{color:#7d8a87;font-size:13px;margin-top:26px}
</style></head><body>
<h1>SPY BEFORE YOU BUY</h1>
<div class="tag">WAIT. CONFIRM. EXECUTE.</div>
__BODY__
</body></html>"""


@app.get("/access", response_class=HTMLResponse)
def access_page(session_id: str = ""):
    row = None
    if session_id:
        con = _subs_db()
        try:
            row = con.execute(
                """SELECT email, plan, status, invite_link, tv_username
                   FROM site_subs
                   WHERE checkout_session_id=?""", (session_id,)).fetchone()
        finally:
            con.close()
    if row and row[1] == IO_PLAN:
        body = ("<p>Payment confirmed - welcome in. Your plan is "
                "TradingView indicators only, so there is no Telegram link "
                "with it. Your indicators are being switched on for the "
                "TradingView username you gave at checkout - they appear "
                "under Invite-only scripts in your TradingView account.</p>"
                f'<div class="small">Plan: {row[1]} - {row[0]}</div>')
    elif row and row[3]:
        code = row[3].split("https://t.me/+", 1)[-1]
        portal = (f'<div class="small"><a href="{STRIPE_PORTAL_URL}">'
                  f"Manage or upgrade my subscription</a></div>"
                  if STRIPE_PORTAL_URL else "")
        body = (f"<p>Payment confirmed - welcome in. Your personal Telegram "
                f"access link is below. It is tied to your subscription and "
                f"works once, so keep it to yourself.</p>"
                f'<a class="btn" href="{GATE_BASE}/agree/{code}">'
                f"GET MY TELEGRAM ACCESS</a>"
                f'<div class="small">Plan: {row[1]} - {row[0]}</div>'
                f"{portal}")
    elif row:
        body = ("<p>Payment confirmed. Your personal access link is still "
                "being created - refresh this page in a minute.</p>")
    else:
        body = ("<p>We could not find that checkout yet. If you just paid, "
                "give it a minute and refresh. If it still does not show, "
                "contact the owner with the email you paid with.</p>")
    if row and (row[3] or row[1] == IO_PLAN) and not row[4]:
        body += ('<div class="small">No TradingView username on file yet - '
                 '<a href="/tradingview">add it here</a> so your '
                 'indicators can be switched on.</div>')
    return ACCESS_PAGE.replace("__BODY__", body)


TV_GOOD_STATUSES = ("active", "trialing", "lifetime", "past_due")

_TV_FORM = """<p>Website subscribers: enter the email you subscribed with
and your TradingView username. Access is switched on only for active
website subscriptions.</p>
<form method="post" action="/tradingview">
<p><input name="email" type="email" required placeholder="Email you subscribed with"
style="padding:12px 16px;border-radius:8px;border:1px solid #3a4542;background:#111;color:#fff;width:320px;max-width:84vw"></p>
<p><input name="tv_username" type="text" required placeholder="TradingView username (exact)"
style="padding:12px 16px;border-radius:8px;border:1px solid #3a4542;background:#111;color:#fff;width:320px;max-width:84vw"></p>
<p><button class="btn" type="submit" style="border:0;cursor:pointer">SWITCH ON MY INDICATORS</button></p>
</form>
<div class="small">No TradingView account yet? A free one is all you need -
sign up at tradingview.com, then come back with your username.</div>"""


@app.get("/tradingview", response_class=HTMLResponse)
def tv_claim_page():
    return ACCESS_PAGE.replace("__BODY__", _TV_FORM)


@app.post("/tradingview", response_class=HTMLResponse)
async def tv_claim_submit(request: Request):
    from urllib.parse import parse_qs
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    email = (form.get("email", [""])[0] or "").strip().lower()[:254]
    tv = (form.get("tv_username", [""])[0] or "").strip().lstrip("@")[:64]
    matched = 0
    if email and tv:
        con = _subs_db()
        try:
            row = con.execute(
                "SELECT status FROM site_subs WHERE email=?",
                (email,)).fetchone()
            if row and row[0] in TV_GOOD_STATUSES:
                matched = 1
                con.execute(
                    """UPDATE site_subs SET tv_username=?, updated_ts=?
                       WHERE email=?""", (tv, time.time(), email))
            con.execute(
                "INSERT INTO tv_claims VALUES(?,?,?,?)",
                (email, tv, matched, time.time()))
            con.commit()
        finally:
            con.close()
    # Same answer either way: the page never reveals whether an email
    # belongs to a subscriber.
    body = ("<p>Thanks - if that email matches an active website "
            "subscription, the indicators will be switched on for that "
            "TradingView username. They appear in TradingView under "
            "Indicators, then Invite-only scripts.</p>")
    return ACCESS_PAGE.replace("__BODY__", body)


@app.get("/stripe/subs")
def stripe_subs(key: str = ""):
    """Machine read for the VM roster sync. 404 unless the shared key
    configured on Render matches - reveals nothing when unconfigured."""
    if not SITE_SUBS_READ_KEY or not hmac.compare_digest(key,
                                                         SITE_SUBS_READ_KEY):
        return JSONResponse({"detail": "not found"}, status_code=404)
    con = _subs_db()
    try:
        rows = con.execute(
            """SELECT email, plan, status, stripe_customer_id,
               stripe_subscription_id, invite_link, current_period_end,
               updated_ts, tv_username FROM site_subs""").fetchall()
    finally:
        con.close()
    cols = ("email", "plan", "status", "stripe_customer_id",
            "stripe_subscription_id", "invite_link", "current_period_end",
            "updated_ts", "tv_username")
    return {"subs": [dict(zip(cols, r)) for r in rows]}
