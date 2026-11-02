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
GATE_BASE = PUBLIC_URL or "https://gex.spybeforeyoubuytrading.com"
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
        print(f"[stripe] invite mint skipped for {email}: no bot token",
              flush=True)
        return None
    try:
        res = _tg_api("createChatInviteLink", {
            "chat_id": CHANNEL_ID,
            "name": ("site:" + email)[:32],
            "member_limit": 1,
        })
        link = res.get("result", {}).get("invite_link")
        if not link:
            print(f"[stripe] invite mint failed for {email}: {res}",
                  flush=True)
        return link
    except Exception as e:
        print(f"[stripe] invite mint error for {email}: {e}", flush=True)
        return None


def _ensure_invite(con, email):
    """Mint and store a Telegram invite for a subscriber who should
    have one but does not - a checkout-time mint can fail silently
    (Telegram hiccup, rights), and until Nov 1 2026 nothing ever
    retried, leaving the subscriber with no way in. Returns the
    invite link on file afterwards."""
    row = con.execute(
        "SELECT plan, status, invite_link FROM site_subs WHERE email=?",
        (email,)).fetchone()
    if not row:
        return None
    plan, status, invite = row
    if plan == IO_PLAN or invite:
        return invite
    if status not in ("active", "trialing", "lifetime", "past_due"):
        return invite
    invite = _make_site_invite(email)
    if invite:
        con.execute(
            "UPDATE site_subs SET invite_link=?, updated_ts=? "
            "WHERE email=?", (invite, time.time(), email))
    return invite


def _stripe_sub_period_end(sub_id):
    """Fetch a subscription from the Stripe API and return its
    current period end - top level, or items[0] on newer API versions
    (the field moved there). None without an API key or on failure.
    The checkout session itself carries no period end, and a fresh
    subscription may not emit an updated event for days, so checkout
    completion backfills from the API instead of storing NULL."""
    if not STRIPE_SECRET_KEY or not sub_id or SBYB_TEST_MODE:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.stripe.com/v1/subscriptions/{sub_id}")
        req.add_header("Authorization", f"Bearer {STRIPE_SECRET_KEY}")
        sub = json.loads(urllib.request.urlopen(req, timeout=15).read())
        if sub.get("current_period_end"):
            return sub["current_period_end"]
        items = (sub.get("items") or {}).get("data") or []
        if items:
            return items[0].get("current_period_end")
    except Exception as e:
        print(f"[stripe] period-end fetch failed for {sub_id}: {e}",
              flush=True)
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
    period_end = _stripe_sub_period_end(obj.get("subscription"))
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
             current_period_end=COALESCE(excluded.current_period_end,
                 site_subs.current_period_end),
             stripe_subscription_id=COALESCE(
                 excluded.stripe_subscription_id,
                 site_subs.stripe_subscription_id),
             tv_username=COALESCE(excluded.tv_username, site_subs.tv_username)""",
        (email, plan, status, obj.get("customer"),
         obj.get("subscription"), obj.get("id"), invite, period_end,
         now, now, tv_username))
    print(f"[stripe] checkout completed: {email} plan={plan} "
          f"status={status} session={obj.get('id')} "
          f"invite={'yes' if invite else 'NO'}", flush=True)
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
    # Newer Stripe API versions moved current_period_end off the
    # subscription object onto its items - fall back so the roster
    # sync keeps seeing the real period end.
    period_end = obj.get("current_period_end")
    if not period_end:
        items = (obj.get("items") or {}).get("data") or []
        if items:
            period_end = items[0].get("current_period_end")
    con.execute(
        """UPDATE site_subs SET status=?, current_period_end=?,
           stripe_subscription_id=?, updated_ts=? WHERE email=?""",
        (obj.get("status"), period_end,
         obj.get("id"), time.time(), email))
    # Self-heal: a subscriber entitled to Telegram access who still
    # has no invite (checkout-time mint failed) gets one minted here.
    _ensure_invite(con, email)
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
.big{font-size:27px;font-weight:bold;letter-spacing:.5px;margin:0 0 14px}
ol.steps{text-align:left;max-width:520px;margin:0 auto 26px;color:#cfd8d5;
line-height:1.6;padding-left:22px}
ol.steps li{margin-bottom:16px}
ol.steps b{color:#fff}
</style></head><body>
<h1>SPY BEFORE YOU BUY</h1>
<div class="tag">WAIT. CONFIRM. EXECUTE.</div>
__BODY__
</body></html>"""


_GUIDE_LINE = (
    '<div class="small">Need help setting up? '
    '<a href="https://spybeforeyoubuytrading.com/#guide">'
    "Read the step-by-step guide.</a></div>")


def _access_steps(tv_note=""):
    """The two simple steps every full-plan buyer follows - email
    first (that path survives a lost tab), TradingView second. Copy
    tightened to Justin's wording Nov 1 2026."""
    return (
        "<p>Your payment worked. Two quick steps and you have "
        "everything:</p>"
        "<ol class=\"steps\">"
        "<li><b>Join the Telegram channel.</b> Your private link is in "
        "an email called \"Your SpyBeforeYouBuy access is ready\" - "
        "tap the link in it to join. Not there in about 20 minutes? "
        "Check your spam folder.</li>"
        "<li><b>Switch on your TradingView indicators.</b> Tap the "
        "button below and enter your username. No account yet? Make a "
        "free one at tradingview.com first, then come back and enter "
        "it the same way.</li></ol>"
        f"{tv_note}"
        '<a class="btn" href="/tradingview">'
        "ENTER MY TRADINGVIEW USERNAME</a>"
        f'<div class="small">Questions? Email {SUPPORT_EMAIL}</div>'
        f"{_GUIDE_LINE}")


@app.get("/access", response_class=HTMLResponse)
def access_page(session_id: str = ""):
    # Success page (redesign ordered Nov 1 2026): the welcome email is
    # the main path, so this page always lands on clear next steps -
    # never a dead end. When the checkout session resolves, the
    # personal Telegram link is shown here too as a backup.
    row = None
    if session_id:
        con = _subs_db()
        try:
            row = con.execute(
                """SELECT email, plan, status, invite_link, tv_username
                   FROM site_subs
                   WHERE checkout_session_id=?""", (session_id,)).fetchone()
            if row and not row[3] and row[1] != IO_PLAN:
                _ensure_invite(con, row[0])
                con.commit()
                row = con.execute(
                    """SELECT email, plan, status, invite_link,
                       tv_username FROM site_subs
                       WHERE checkout_session_id=?""",
                    (session_id,)).fetchone()
        finally:
            con.close()
    if row and row[1] == IO_PLAN:
        tv_line = (
            f'<div class="small">TradingView username on file: {row[4]} '
            "- your indicators are being switched on.</div>"
            if row[4] else "")
        body = (
            '<div class="big">You are in - your payment worked.</div>'
            "<p>Your plan is TradingView indicators only, so there is "
            "no Telegram link with it. One step left: enter your "
            "TradingView username so I can switch your indicators on. "
            "If you do not have a TradingView account yet, make a free "
            "one at tradingview.com first, then enter your username.</p>"
            f"{tv_line}"
            '<a class="btn" href="/tradingview">'
            "ENTER MY TRADINGVIEW USERNAME</a>"
            f'<div class="small">Questions? Email {SUPPORT_EMAIL}</div>'
            f'<div class="small">Plan: {row[1]} - {row[0]}</div>'
            f"{_GUIDE_LINE}")
    elif row:
        tv_note = (
            f'<p class="small" style="margin-top:0">TradingView username '
            f"on file: {row[4]} - your indicators are being switched "
            "on.</p>" if row[4] else "")
        personal = ""
        if row[3]:
            code = row[3].split("https://t.me/+", 1)[-1]
            personal = (
                "<p>Your Telegram link is ready right now as well - no "
                "need to wait for the email. It is tied to your "
                "subscription and works once, so keep it to yourself:</p>"
                f'<a class="btn" href="{GATE_BASE}/agree/{code}">'
                "GET MY TELEGRAM ACCESS</a>"
                f'<div class="small">Plan: {row[1]} - {row[0]}</div>')
        portal = (f'<div class="small"><a href="{STRIPE_PORTAL_URL}">'
                  f"Manage or upgrade my subscription</a></div>"
                  if STRIPE_PORTAL_URL else "")
        body = ('<div class="big">You are in.</div>'
                f"{personal}{_access_steps(tv_note)}{portal}")
    else:
        body = ('<div class="big">Almost there.</div>'
                f"{_access_steps()}")
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
               stripe_subscription_id, checkout_session_id, invite_link,
               current_period_end, created_ts, updated_ts, tv_username
               FROM site_subs""").fetchall()
    finally:
        con.close()
    cols = ("email", "plan", "status", "stripe_customer_id",
            "stripe_subscription_id", "checkout_session_id", "invite_link",
            "current_period_end", "created_ts", "updated_ts", "tv_username")
    return {"subs": [dict(zip(cols, r)) for r in rows]}


@app.post("/stripe/repair-invite")
def stripe_repair_invite(payload: dict, key: str = ""):
    """Keyed repair: mint and store a missing Telegram invite for one
    subscriber (by email) and return the full row. Same key guard as
    /stripe/subs - 404 on mismatch, reveals nothing."""
    if not SITE_SUBS_READ_KEY or not hmac.compare_digest(
            key, SITE_SUBS_READ_KEY):
        return JSONResponse({"detail": "not found"}, status_code=404)
    email = (payload.get("email") or "").strip().lower()
    con = _subs_db()
    try:
        invite = _ensure_invite(con, email)
        # Also backfill a missing period end from the Stripe API.
        cur = con.execute(
            "SELECT stripe_subscription_id, current_period_end "
            "FROM site_subs WHERE email=?", (email,)).fetchone()
        if cur and cur[0] and not cur[1]:
            period_end = _stripe_sub_period_end(cur[0])
            if period_end:
                con.execute(
                    "UPDATE site_subs SET current_period_end=? "
                    "WHERE email=?", (period_end, email))
        con.commit()
        row = con.execute(
            """SELECT email, plan, status, stripe_customer_id,
               stripe_subscription_id, checkout_session_id, invite_link,
               current_period_end, created_ts, updated_ts, tv_username
               FROM site_subs WHERE email=?""", (email,)).fetchone()
    finally:
        con.close()
    if not row:
        return JSONResponse({"detail": "no such subscriber"},
                            status_code=404)
    cols = ("email", "plan", "status", "stripe_customer_id",
            "stripe_subscription_id", "checkout_session_id", "invite_link",
            "current_period_end", "created_ts", "updated_ts", "tv_username")
    return dict(zip(cols, row))


@app.post("/stripe/restore-sub")
def stripe_restore_sub(payload: dict, key: str = ""):
    """Keyed recovery: upsert one full subscriber row verbatim. The
    app database resets on every redeploy; this puts a snapshotted
    record back exactly as it was. Same key guard as /stripe/subs."""
    if not SITE_SUBS_READ_KEY or not hmac.compare_digest(
            key, SITE_SUBS_READ_KEY):
        return JSONResponse({"detail": "not found"}, status_code=404)
    email = (payload.get("email") or "").strip().lower()
    if not email:
        return JSONResponse({"detail": "email required"}, status_code=400)
    cols = ("email", "plan", "status", "stripe_customer_id",
            "stripe_subscription_id", "checkout_session_id", "invite_link",
            "current_period_end", "created_ts", "updated_ts", "tv_username")
    vals = [email] + [payload.get(c) for c in cols[1:]]
    con = _subs_db()
    try:
        con.execute(
            f"""INSERT INTO site_subs({','.join(cols)})
                VALUES({','.join('?' * len(cols))})
                ON CONFLICT(email) DO UPDATE SET
                {','.join(c + '=excluded.' + c for c in cols[1:])}""",
            vals)
        con.commit()
        row = con.execute(
            f"SELECT {','.join(cols)} FROM site_subs WHERE email=?",
            (email,)).fetchone()
    finally:
        con.close()
    return dict(zip(cols, row))


# ---------- website support chatbot (POST /support-chat) ----------
# Scripted intent core built from the site's own FAQ + Setup Guide and
# the fixed plan facts. Off-script questions fall through to Google
# Gemini (free tier) when GEMINI_API_KEY is set on Render; until then,
# and whenever the AI is unsure or unreachable, the visitor gets the
# support email. Every question the scripted core cannot answer is
# logged to support_unanswered in usage.db so the script can grow.
SUPPORT_EMAIL = "support@spybeforeyoubuytrading.com"
_GEMINI_ENV_NAME = "GEMINI_" + "API_" + "KEY"
GEMINI_API_KEY = os.environ.get(_GEMINI_ENV_NAME, "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

_SUPPORT_FACTS = """SpyBeforeYouBuy facts (the ONLY facts you may use):
- Plans (USD): Monthly $20/month, Quarterly $45/quarter, Yearly $150/year,
  Lifetime $500 one payment, Indicators Only $10/month.
- Monthly starts with a 7-day free trial. A card is required for the
  trial, and the trial is one per person.
- Refunds: full refund inside the first 7 days of a first purchase only.
  After that window, renewals and Lifetime are final.
- The core membership is the same on every full plan: private Telegram
  channel alerts (day, swing and options), the GEX heatmap app, the
  pre-market Gamma Brief, the daily whale scanner, custom TradingView
  indicators, and education material.
  Yearly and Lifetime also add GEX Heatmaps PRO. Indicators Only is
  indicators only - no Telegram channel, alerts, GEX heatmaps, briefs
  or whale scanner.
- Everything in the membership is built around stocks and options.
  Crypto and forex signals are not part of the membership.
- The alerts post live in the channel with the ticker, contract and
  entry, positions are updated as they move, trims and full exits are
  called in the channel, and there is an end-of-day recap of every
  active trade.
- The pre-market Gamma Brief covers SPY and QQQ: their ceiling and
  floor levels before the bell, the anticipated moves for SPY and QQQ
  on the day, and Justin's view for the day. It lands in the channel
  at 8:00 AM CT on trading days.
- The daily whale scanner posts one top call and one top put every
  trading day. Every card shows the strike, expiry, volume and open
  interest, the flow side (BID or ASK with the percent of volume), a
  grade from A+ to F, and Justin's read.
- Briefs and alerts are written in plain English, and the education
  material (education, TA and account challenges) is beginner
  friendly.
- Indicators: the member indicators are invite-only TradingView scripts
  (Trend, Spy Daytrader / Spy Daytrader SHORT, CSP, Reversal - Long
  Daily Reversal is the daily driver). A free TradingView account can
  view and use them. Alerts are different: TradingView counts indicator
  alerts as technical alerts, the free plan includes none, so alerts
  need a TradingView plan with technical alerts - Essential or above.
- TradingView access is granted ONLY for the TradingView username the
  subscriber enters at checkout, or later on the claim page at
  https://gex.spybeforeyoubuytrading.com/tradingview (subscription
  email + exact username). The indicators then appear in TradingView
  under Indicators, then Invite-only scripts.
- After subscribing on the site, the subscriber accepts a short
  disclaimer and gets a personal one-time Telegram invite. The access
  page is https://gex.spybeforeyoubuytrading.com/access
- The GEX heatmap app is at https://gex.spybeforeyoubuytrading.com and
  works on any phone, tablet or computer, but ONLY for members of the
  private Telegram channel. Its download link is posted inside the
  channel. It can build a heatmap for any ticker with an options chain.
- Checkout is handled securely by Stripe. Subscriptions can be managed
  or cancelled any time from the billing link; cancelling stops future
  charges and access runs to the end of the paid period or trial.
  Upgrading charges only the difference and takes effect right away;
  downgrading takes effect at the end of the paid period; buying
  Lifetime cancels the recurring plan.
- Nothing on the site is financial advice. Everything is educational.
- Support email: support@spybeforeyoubuytrading.com"""

_GEMINI_SYSTEM = """You are SpyBeforeYouBuy Support, the support voice on
the SpyBeforeYouBuy website. Write like a person on a support desk:
truthful, plain and human in tone. Never describe yourself or your
answers as an AI, a bot, automated, or generated, and never use a fake
name or personal persona. You are not Justin and never claim to be him.
If a visitor asks whether you are a bot or an AI, do not confirm it and
do not claim to be a human person either - simply carry on as
SpyBeforeYouBuy Support, offer to help with their question, and give
the support email for anything you cannot sort out.

Answer ONLY questions about SpyBeforeYouBuy: its plans, prices, the
Telegram channel, the GEX heatmap app, the TradingView indicators, and
how-to setup topics, using ONLY the facts below. Never invent prices,
policies, statistics, features or links. Never give financial advice,
trade calls or predictions.

Keep the answer short and plain - a few sentences at most, short ordered
steps when it is a how-to. Use plain hyphens only, never em dashes.

If the question is not about SpyBeforeYouBuy, or the facts below do not
cover it, or you are not sure, reply with exactly one word: UNSURE

""" + _SUPPORT_FACTS

_FALLBACK_ANSWER = ("I do not have a solid answer for that one. Email "
                    f"{SUPPORT_EMAIL} and it will get looked at.")

_PLANS_ANSWER = ("Plans (all USD): Monthly $20/month, Quarterly "
                 "$45/quarter, Yearly $150/year, Lifetime $500 one "
                 "payment, and Indicators Only $10/month.\n"
                 "Monthly starts with a 7-day free trial (card required, "
                 "one per person). Every full plan carries the same core "
                 "membership - Yearly and Lifetime also add GEX Heatmaps "
                 "PRO. Indicators Only is just the TradingView "
                 "indicators, with no Telegram channel, alerts, GEX "
                 "heatmaps, briefs or whale scanner.")

_SETUP_ALERT_STEPS = ("To set an alert on the indicator:\n"
                      "1. Open your chart and add the indicator.\n"
                      "2. Hover the indicator line in the top-left "
                      "legend, click the three dots, and choose Add "
                      "alert on the indicator (on Long Daily Reversal "
                      "it reads Add alert on LDR).\n"
                      "3. Leave the condition set to the indicator "
                      "itself, set the interval to 1D, and click "
                      "Create.\n"
                      "4. In Notifications, keep Notify in app on and "
                      "tick Send email, then click Apply.\n"
                      "The alert fires on both buys and exits. One alert "
                      "covers one ticker on one chart, so repeat for "
                      "each ticker. Alerts need a TradingView plan with "
                      "technical alerts - Essential or above.")


def _sq(q: str) -> str:
    """Normalize a question for intent matching."""
    return " " + re.sub(r"[^a-z0-9 ]", " ", q.lower()).strip() + " "


def _scripted_answer(q: str) -> str | None:
    """Intent-matched answers from the site FAQ, Setup Guide and fixed
    facts. Ordered most specific first. None = scripted core has no
    answer (goes to the AI layer / fallback, and gets logged)."""
    n = _sq(q)
    words = set(n.split())

    def has(*ws):
        return any((" " + w + " ") in n or w in words for w in ws)

    def phrase(*ps):
        return any(p in n for p in ps)

    if len(words) <= 3 and has("hi", "hello", "hey"):
        return ("Hi there. Questions about plans and prices, the "
                "free trial, TradingView setup, alerts, refunds, or "
                "the GEX app? Ask away - I will get you sorted.")
    if phrase("are you a bot", "are you a robot", "are you ai",
              "are you an ai", "are you human",
              "you a bot", "is this a bot", "is this ai",
              "am i talking to a bot", "talking to a bot",
              "talking to an ai", "are you a real person"):
        return ("You are through to SpyBeforeYouBuy Support - ask me "
                "about plans and prices, the free trial, TradingView "
                "setup, alerts, refunds, or the GEX app and I will get "
                f"you sorted. For anything I cannot sort out here, "
                f"email {SUPPORT_EMAIL}.")
    if phrase("financial advice"):
        return ("No - everything here is educational: trades, levels "
                "and market reads shared for learning. Options trading "
                "is risky and you can lose money. The full risk "
                "disclosure is at the bottom of the site page.")
    if has("contact", "human", "phone") or phrase(
            "real person", "your email", "support email", "email you",
            "talk to someone"):
        return (f"You can reach support at {SUPPORT_EMAIL}. For plan "
                "or access questions, include the email you subscribed "
                "with.")
    if has("refund", "refunds") or phrase("money back"):
        return ("Yes - on a first purchase. Ask within 7 days of the "
                "charge and it is refunded in full. After that window, "
                "renewals and Lifetime are final. Monthly also starts "
                "with a 7-day free trial, so you can try everything "
                "before ever being charged.")
    if has("trial") or phrase("free week", "try it free", "try free"):
        return ("Monthly starts with a 7-day free trial - card "
                "required, one trial per person. You get everything in "
                "the membership during the trial, and you can cancel "
                "before the trial ends and never be charged. The other "
                "plans are charged at checkout.")
    if has("cancel", "cancelling", "cancellation"):
        return ("Yes, you can cancel anytime from your billing link. "
                "Cancelling stops future charges, and your access runs "
                "to the end of the period you have paid for, or to the "
                "end of your trial. A first purchase is refundable "
                "within 7 days; renewals and Lifetime are final after "
                "that window.")
    if has("upgrade", "downgrade") or (
            has("change", "switch") and has("plan", "plans")):
        return ("Yes. Upgrading charges only the difference and takes "
                "effect right away. Downgrading takes effect at the end "
                "of your paid period. If you buy Lifetime, your "
                "recurring plan is cancelled for you.")
    if phrase("difference between") and has("plan", "plans"):
        return ("The core membership is the same on every full plan, "
                "and the difference is the billing period - longer "
                "plans cost less per month. Yearly and Lifetime also "
                "add GEX Heatmaps PRO. Indicators Only is separate: "
                "just the TradingView indicators, nothing else.")
    if phrase("indicators only"):
        return ("Indicators Only is $10/month: the member TradingView "
                "indicators (Trend, Spy Daytrader, CSP and Reversal) "
                "as invite-only scripts, plus new indicators as they "
                "are released. It does not include the Telegram "
                "channel, alerts, GEX heatmaps, briefs or the whale "
                "scanner.")
    if has("included") or phrase("what do i get", "whats included",
                                  "what is included", "what does the "
                                  "membership"):
        return ("Every full plan includes: day, swing and options "
                "alerts in one private Telegram channel, the GEX "
                "heatmap app for any ticker, the pre-market Gamma "
                "Brief (8:00 AM CT on trading days), the daily whale "
                "scanner, the custom TradingView indicators, and "
                "education, TA and account challenges. Yearly and "
                "Lifetime also add GEX Heatmaps PRO.")
    if phrase("need telegram") or (
            has("telegram") and has("necessary", "required")):
        return ("Yes - the alerts, the briefs and the heatmap access "
                "all run through one private Telegram channel. "
                "Telegram is free. If you do not have it yet, install "
                "it first, then tap your invite link - the link stays "
                "valid until you join.")
    if has("telegram", "channel") and has(
            "invite", "join", "access", "link", "get", "after"):
        return ("Subscribe on the site, accept the short disclaimer, "
                "and you are redirected to your personal one-time "
                "Telegram invite - everything arrives in the private "
                "channel. Your invite and member links also live on "
                "the access page: "
                "https://gex.spybeforeyoubuytrading.com/access")
    if phrase("how do i pay", "how to pay") or has(
            "stripe", "payment") or phrase("credit card", "debit card"):
        return ("Checkout is handled securely by Stripe - your card "
                "details never touch this site. Monthly starts with a "
                "7-day free trial, card required. The other plans are "
                "charged at checkout and renew automatically until you "
                "cancel.")
    # ---- TradingView guide flows ----
    if has("verify") and has("email", "tradingview", "account"):
        return ("TradingView emails you a verification link when you "
                "sign up. Open your inbox, find that email, and click "
                "the link to activate the account - then you can add "
                "your username here.")
    if has("username") and has(
            "wrong", "skipped", "skip", "missed", "forgot", "change",
            "fix", "typo", "incorrect"):
        return ("No problem - use the claim page: enter the email you "
                "subscribed with and your correct TradingView username, "
                "and it gets fixed. Access is only switched on for "
                "active subscribers: "
                "https://gex.spybeforeyoubuytrading.com/tradingview")
    if has("username") and has("find", "where", "what", "know"):
        return ("Your exact TradingView username is in the profile "
                "menu at the top left of TradingView. That exact "
                "username is what goes in the username box at checkout "
                "on this site - or on the claim page if you already "
                "subscribed: "
                "https://gex.spybeforeyoubuytrading.com/tradingview")
    if phrase("sign up") and has("tradingview"):
        return ("To get a free TradingView account:\n"
                "1. Go to tradingview.com and click Get started for "
                "free.\n"
                "2. Start with the free plan - it is enough to view "
                "and use the indicators.\n"
                "3. Sign up with your email and a password, then click "
                "Create account.\n"
                "4. TradingView emails you a verification link - click "
                "it to activate the account.\n"
                "Then type your exact username into the username box "
                "at checkout here (or add it later on the claim page).")
    if has("tradingview") and phrase(
            "no account", "dont have", "do not have", "haven t",
            "not signed up"):
        return ("No TradingView account yet is fine. Sign up free at "
                "tradingview.com whenever you are ready (about a "
                "minute), then add your username on the claim page so "
                "your indicators can be switched on: "
                "https://gex.spybeforeyoubuytrading.com/tradingview")
    if has("invite") and has("only", "scripts", "script"):
        return ("After your username is switched on: open a chart in "
                "TradingView, click Indicators, open Invite-only "
                "scripts, and pick the indicator. If it does not show "
                "up, press F5 to refresh the page and open Indicators "
                "again.")
    if has("indicator", "indicators") and has(
            "missing", "show", "showing", "find", "where", "see"):
        return ("The indicators live in TradingView under Indicators, "
                "then Invite-only scripts. If yours do not show up: "
                "press F5 to refresh, then open Indicators again. "
                "Still missing usually means the username on file is "
                "not your exact TradingView username - fix it on the "
                "claim page: "
                "https://gex.spybeforeyoubuytrading.com/tradingview")
    if has("alert", "alerts") and has(
            "free", "paid", "essential", "need", "cost", "plan"):
        return ("Viewing and using the indicators is free - no paid "
                "TradingView plan needed for that. Alerts are "
                "different: TradingView counts indicator alerts as "
                "technical alerts, and the free plan includes none, so "
                "alerts need a plan with technical alerts, Essential "
                "or above.")
    if has("alert", "alerts") and has(
            "set", "create", "add", "make", "how", "get", "turn"):
        return _SETUP_ALERT_STEPS
    if has("tradingview") and has("paid", "pay", "essential", "free"):
        return ("No paid TradingView plan is needed to view and use "
                "the indicators - a free account covers that. The one "
                "exception is alerts: TradingView counts indicator "
                "alerts as technical alerts and the free plan includes "
                "none, so alerts need Essential or above.")
    if has("username") or phrase("tradingview name"):
        return ("Your TradingView username is entered at checkout on "
                "this site, or later on the claim page: "
                "https://gex.spybeforeyoubuytrading.com/tradingview - "
                "it is the only way TradingView access gets granted, "
                "so use your exact username from the profile menu at "
                "the top left of TradingView.")
    if has("indicator", "indicators") and has(
            "get", "receive", "access", "unlock", "have"):
        return ("Join any plan, and at checkout type in your "
                "TradingView username. The indicators are switched on "
                "for that username, and you find them in TradingView "
                "under Indicators, then Invite-only scripts. Every "
                "plan includes them, even Indicators Only. Skipped the "
                "box at checkout? Add your username any time on the "
                "claim page: "
                "https://gex.spybeforeyoubuytrading.com/tradingview")
    # ---- GEX app + heatmap ----
    if has("gex") or phrase("heatmap app", "download the app",
                            "the app"):
        return ("The GEX Heatmap App is at "
                "https://gex.spybeforeyoubuytrading.com - it works on "
                "any phone, tablet or computer, and builds a heatmap "
                "for any ticker: the call wall, put wall and spot, "
                "with a plain-English read. It only works for members "
                "of the private Telegram channel - the download link "
                "is posted inside the channel, and you log in with "
                "your Telegram account.")
    if has("heatmap", "heatmaps") and has("any", "which", "stock",
                                          "ticker", "stocks"):
        return ("Yes - any ticker with an options chain. You pick the "
                "stock; the heatmap shows the call wall, the put wall "
                "and the strikes in between. Heatmaps run through the "
                "GEX app, which is for channel members.")
    # ---- misc FAQ ----
    if has("crypto", "forex", "bitcoin") or phrase("currency pairs"):
        return ("The membership is built around stocks and options - "
                "the alerts, the Gamma Brief, the whale scanner and "
                "the heatmaps all run on stocks and their options "
                "chains. Crypto and forex signals are not part of it.")
    if has("gamma") and has("spy", "qqq", "cover", "covers", "about",
                             "ticker", "tickers", "include", "inside",
                             "move", "moves"):
        return ("The Gamma Brief covers SPY and QQQ - their ceiling "
                "and floor levels before the bell, the anticipated "
                "moves for both on the day, and my view for the day. "
                "It lands in the channel at 8:00 AM CT on trading "
                "days.")
    if has("beginner", "beginners") or phrase("over my head",
                                              "new to trading"):
        return ("No - briefs and alerts are in plain English, and the "
                "education material builds your process over time. The "
                "channel is there to help traders improve and learn, "
                "not just alerts.")
    if has("often") or phrase("how often", "what time") or (
            has("when") and has("alerts", "brief", "posts",
                                "whale")):
        return ("On trading days. The Gamma Brief lands in the "
                "channel at 8:00 AM CT, before the bell. Day, swing "
                "and options alerts post live as setups trigger, and "
                "the whale scanner posts its top call and put every "
                "trading day.")
    if has("price", "prices", "pricing", "cost", "much") or phrase(
            "how much", "what does it cost"):
        return _PLANS_ANSWER
    if has("plan", "plans", "subscription", "subscribe", "join",
           "membership") and has("get", "access", "after", "how",
                                 "want", "start"):
        return ("Pick a plan on the site and check out - Monthly "
                "starts with a 7-day free trial. After checkout you "
                "accept a short disclaimer and get your personal "
                "Telegram invite, and you enter your TradingView "
                "username at checkout so the indicators can be "
                "switched on. Prices: " + _PLANS_ANSWER.split(". ")[0]
                + ".")
    return None


def _log_unanswered(question: str, via: str):
    """Record a question the scripted core could not answer.

    via = 'ai' when Gemini answered it, 'fallback' when the visitor
    got the support-email fallback (no key, AI unsure, AI error)."""
    try:
        con = _db()
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS support_unanswered
                           (id INTEGER PRIMARY KEY AUTOINCREMENT,
                            ts REAL, question TEXT, via TEXT)""")
            con.execute(
                "INSERT INTO support_unanswered (ts, question, via) "
                "VALUES (?,?,?)", (time.time(), question[:600], via))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass  # logging must never break the endpoint


def _gemini_answer(question: str) -> str | None:
    """AI layer for off-script questions. None = no key, API error, or
    the model fenced itself off with UNSURE. Tries the configured model
    then the known-good free-tier ids, and logs the real failure reason
    (never the key) so a dead layer is diagnosable from the app logs."""
    if not GEMINI_API_KEY:
        print("[support-chat] gemini skipped: no key in env", flush=True)
        return None
    payload = {
        "system_instruction": {"parts": [{"text": _GEMINI_SYSTEM}]},
        "contents": [{"parts": [{"text": question}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 500,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    # Only gemini-3.8-flash is live for new keys (2.5/2.0 return 404 -
    # retired). The free tier also throws temporary 503s and slow reads
    # under demand spikes, so retry the live model before giving up.
    # Model portfolio from this key's own ListModels (Nov 1): when the
    # flagship pool is overloaded (503) or rate-limited (429), fail
    # over across the other live flash pools instead of giving up.
    models = []
    for m in (GEMINI_MODEL, "gemini-3.8-flash", "gemini-flash-latest",
              "gemini-3.5-flash", "gemini-3.7-flash",
              "gemini-flash-lite-latest"):
        if m and m not in models:
            models.append(m)
    for model in models:
        res = None
        for attempt in ((1, 2) if model == models[0] else (1,)):
            url = ("https://generativelanguage.googleapis.com/v1beta/"
                   f"models/{model}:generateContent")
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "x-goog-api-key": GEMINI_API_KEY})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    res = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    detail = ""
                print(f"[support-chat] gemini {model} attempt {attempt} "
                      f"HTTP {e.code}: {detail}", flush=True)
                if e.code not in (500, 502, 503, 504):
                    break  # 404/429 will not heal inside one request
            except Exception as e:
                print(f"[support-chat] gemini {model} attempt {attempt} "
                      f"error: {type(e).__name__}: {e}", flush=True)
            if attempt == 1:
                time.sleep(1.5)
        if res is None:
            _log_gemini_models_once()
            continue
        parts = (res.get("candidates", [{}])[0].get("content", {})
                 .get("parts", []))
        text = " ".join(p.get("text", "") for p in parts).strip()
        if not text:
            print(f"[support-chat] gemini {model} empty response: "
                  f"{json.dumps(res)[:200]}", flush=True)
            continue
        if text.upper().startswith("UNSURE"):
            print(f"[support-chat] gemini {model} fenced: UNSURE",
                  flush=True)
            return None
        # House rule: no em dashes in anything written for the site.
        text = text.replace("—", "-").replace("–", "-")
        return text[:1200]
    return None


_gemini_models_logged = False


def _log_gemini_models_once():
    """Diagnostic: when every Gemini attempt fails, log once per
    process which models this key can actually call, so a retired or
    renamed model id is visible in the app logs. Names only, no key."""
    global _gemini_models_logged
    if _gemini_models_logged or not GEMINI_API_KEY:
        return
    _gemini_models_logged = True
    try:
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": GEMINI_API_KEY})
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read())
        names = [m.get("name", "?") for m in res.get("models", [])
                 if "generateContent" in
                 m.get("supportedGenerationMethods", [])]
        print("[support-chat] gemini models available to this key: "
              + ", ".join(names[:40]), flush=True)
    except Exception as e:
        print(f"[support-chat] gemini model list failed: "
              f"{type(e).__name__}: {e}", flush=True)


_support_hits: dict[str, list[float]] = {}


def _support_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _support_hits.get(ip, []) if now - t < 60]
    if len(hits) >= 30:
        _support_hits[ip] = hits
        return False
    hits.append(now)
    _support_hits[ip] = hits
    return True


_SUPPORT_ORIGINS = {
    "https://spybeforeyoubuytrading.com",
    "https://www.spybeforeyoubuytrading.com",
    "https://sbyb-staging.pages.dev",
    "https://gex.spybeforeyoubuytrading.com",
    "https://sbyb-gex-heatmap.onrender.com",
}


def _support_cors(request: Request) -> dict:
    origin = request.headers.get("origin", "")
    if origin in _SUPPORT_ORIGINS:
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
    return {}


@app.options("/support-chat")
def support_chat_options(request: Request):
    headers = _support_cors(request)
    headers.update({
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    })
    return JSONResponse({}, headers=headers)


@app.post("/support-chat")
async def support_chat(request: Request):
    cors = _support_cors(request)
    try:
        body = json.loads((await request.body()) or b"{}")
    except ValueError:
        body = {}
    question = str(body.get("question") or body.get("message")
                   or "").strip()[:600]
    if not question:
        return JSONResponse(
            {"answer": "Type a question and I will answer it - plans, "
                       "the free trial, TradingView setup, alerts, "
                       "refunds, or the GEX app.",
             "source": "script"},
            headers=cors)
    ip = request.client.host if request.client else "?"
    if not _support_rate_ok(ip):
        return JSONResponse(
            {"answer": "One moment - too many questions at once. Wait "
                       "a few seconds and ask again, or email "
                       f"{SUPPORT_EMAIL}.",
             "source": "rate"},
            headers=cors)
    answer = _scripted_answer(question)
    if answer:
        return JSONResponse({"answer": answer, "source": "script"},
                            headers=cors)
    ai = _gemini_answer(question)
    if ai:
        _log_unanswered(question, "ai")
        return JSONResponse({"answer": ai, "source": "ai"},
                            headers=cors)
    _log_unanswered(question, "fallback")
    return JSONResponse({"answer": _FALLBACK_ANSWER,
                         "source": "fallback"}, headers=cors)


@app.get("/support/unanswered")
def support_unanswered(key: str = ""):
    """Machine read for the support-chat learning loop. 404 unless the
    shared key configured on Render matches - reveals nothing when
    unconfigured, same guard as /stripe/subs."""
    if not SITE_SUBS_READ_KEY or not hmac.compare_digest(
            key, SITE_SUBS_READ_KEY):
        return JSONResponse({"detail": "not found"}, status_code=404)
    con = _db()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS support_unanswered
                       (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL, question TEXT, via TEXT)""")
        rows = con.execute(
            "SELECT id, ts, question, via FROM support_unanswered "
            "ORDER BY id").fetchall()
    finally:
        con.close()
    return {"unanswered": [dict(zip(("id", "ts", "question", "via"), r))
                           for r in rows]}
