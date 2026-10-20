# SPY BEFORE YOU BUY - Free GEX Heatmap App

Type a ticker, get the strike x expiry GEX heatmap + point-form read.
Brand: black / white / teal `#30857a`.

## How it works

- `GET /` - the page (Telegram login for members, ticker box when authed)
- `GET /api/tg-auth` - Telegram Login Widget callback: verifies signature,
  checks live membership of the private channel, sets a session cookie
- `GET /api/go?ticker=AAPL` - builds (or serves cached) heatmap, returns
  `{read, as_of, left}` JSON. Counts against quota only on fresh builds.
- `GET /i/AAPL.png` - the heatmap PNG (16:9, matplotlib-rendered, no browser needed)
- `GET /api/quota` - `{left, limit}` for this visitor

Gating: Telegram login + live channel-membership check (re-checked every
15 min, so removing someone from the channel locks them out automatically).
Quota: 100 fresh heatmaps per rolling 7 days per Telegram user (SQLite).
Every build is logged (tg id, username, ticker, time) in `usage.db`.
Chain cache: 15 min per ticker (`cache/`). Data: free CBOE delayed ~15 min.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --port 8000
```

## Deploy free - Render

1. Free account at render.com, new **Web Service**, connect the GitHub repo
2. Runtime: Python. Build command: `pip install -r requirements.txt`
   Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1`
3. Environment variables:
   - `TELEGRAM_BOT_TOKEN` = @Gamma_brief_bot token (secret)
   - `PUBLIC_URL` = `https://<service-name>.onrender.com`
   - `SESSION_SECRET` = any long random string (secret)
4. Free tier sleeps after 15 min idle - keep it warm with a ping every
   10 min (a cron hitting the homepage is enough; 750 free hours/month
   covers 24/7 for one service).

Then in Telegram: @BotFather -> `/setdomain` -> @Gamma_brief_bot ->
send the onrender.com domain (no https://).
