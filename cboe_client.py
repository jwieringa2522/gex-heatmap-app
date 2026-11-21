"""Client for the free CBOE delayed options-quotes endpoint.

Endpoint: https://cdn.cboe.com/api/global/delayed_quotes/options/{TICKER}.json
- Follows a 307 redirect to cdn-api.cboe.com (requests handles it).
- Requires a browser User-Agent.
- ~15-minute delayed chain snapshots: per-contract bid/ask/volume/OI/IV/greeks.
- NOT time & sales: no individual trade prints, no execution side.

2026-11-20 changes (Cloudflare block fix, ported from the Options
Whale Radar client the same day):
- The redirect hop started returning a Cloudflare "Attention
  Required!" 403 when hit with the old minimal header set
  (User-Agent + Accept only) - members saw "No options data" for
  tickers that plainly have chains. Fetching the redirect target host
  (cdn-api.cboe.com) DIRECTLY with a full browser header set
  (Referer, Accept-Language, Sec-Fetch-*) returns 200 with the
  identical payload. fetch() now sends the full header set and tries
  the direct host first, then the classic redirect URL.
"""
import logging
import time

import requests

from config import REQUESTS_PER_SEC, USER_AGENT

log = logging.getLogger(__name__)

BASE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker}.json"
DIRECT_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/options/{ticker}.json"

_BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.cboe.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}


class CboeClient:
    def __init__(self, min_interval: float = 1.0 / REQUESTS_PER_SEC):
        self.session = requests.Session()
        self.session.headers.update(_BROWSER_HEADERS)
        self.min_interval = min_interval
        self._last = 0.0

    def _throttle(self):
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def fetch(self, ticker: str, retries: int = 3):
        """Return the parsed JSON payload, or None if the ticker has no data."""
        ticker = ticker.upper().strip()
        urls = [DIRECT_URL.format(ticker=ticker), BASE_URL.format(ticker=ticker)]
        for attempt in range(retries):
            for url in urls:
                self._throttle()
                try:
                    resp = self.session.get(url, timeout=30, allow_redirects=True)
                    if resp.status_code == 404:
                        log.info("no CBOE options data for %s (404)", ticker)
                        return None
                    resp.raise_for_status()
                    return resp.json()
                except (requests.RequestException, ValueError) as exc:
                    log.warning("fetch %s attempt %d (%s) failed: %s",
                                ticker, attempt + 1, url.split("/")[2], exc)
            time.sleep(2 ** attempt)
        log.error("fetch %s failed after %d attempts", ticker, retries)
        return None
