"""Client for the free CBOE delayed options-quotes endpoint.

Endpoint: https://cdn.cboe.com/api/global/delayed_quotes/options/{TICKER}.json
- Follows a 307 redirect to cdn-api.cboe.com (requests handles it).
- Requires a browser User-Agent.
- ~15-minute delayed chain snapshots: per-contract bid/ask/volume/OI/IV/greeks.
- NOT time & sales: no individual trade prints, no execution side.
"""
import logging
import time

import requests

from config import REQUESTS_PER_SEC, USER_AGENT

log = logging.getLogger(__name__)

BASE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker}.json"


class CboeClient:
    def __init__(self, min_interval: float = 1.0 / REQUESTS_PER_SEC):
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        self.min_interval = min_interval
        self._last = 0.0

    def _throttle(self):
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def fetch(self, ticker: str, retries: int = 3):
        """Return the parsed JSON payload, or None if the ticker has no data."""
        url = BASE_URL.format(ticker=ticker.upper().strip())
        for attempt in range(retries):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=30, allow_redirects=True)
                if resp.status_code == 404:
                    log.info("no CBOE options data for %s (404)", ticker)
                    return None
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as exc:
                log.warning("fetch %s attempt %d failed: %s", ticker, attempt + 1, exc)
                time.sleep(2 ** attempt)
        log.error("fetch %s failed after %d attempts", ticker, retries)
        return None
