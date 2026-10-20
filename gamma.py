"""Gamma exposure levels from free CBOE delayed chain data.

Computes per-strike gamma exposure (GEX) from the CBOE delayed options
chain the whale radar already fetches, then derives the levels that matter
for the premarket brief:

- ceiling: dominant call-gamma strike above the current price
- floor:   dominant put-gamma strike below the current price
- flip:    where net gamma exposure crosses zero (regime boundary)
- mood:    'jumpy' (negative net GEX, moves snowball) or 'calm' (positive)

Approximation notes (be honest about these):
- CBOE-only chain snapshot, ~15 min delayed; OI updates overnight.
- Walls are picked with a 0.2% buffer around spot to skip the ATM
  gamma hump (gamma is always tallest at the money, which would otherwise
  just repeat the current price as both wall).
"""
import logging
import re
from collections import defaultdict
from datetime import date

from cboe_client import CboeClient

log = logging.getLogger(__name__)

_OPT_PAT = re.compile(r"^[A-Z]+(\d{6})([CP])(\d{8})$")
# Skip strikes within +-0.2% of spot: the ATM gamma hump would otherwise
# just echo the current price back as both ceiling and floor.
ATM_BUFFER = 0.002


def _last_trading_day(ref: date) -> date:
    from datetime import timedelta
    d = ref
    while d.weekday() > 4:  # Sat/Sun -> back to Friday
        d = d - timedelta(days=1)
    return d


def compute_levels(ticker: str, max_dte: int = 45, ref_date: date = None) -> dict:
    """Return gamma levels for ticker. Raises RuntimeError if no chain data."""
    ref_date = _last_trading_day(ref_date or date.today())
    payload = CboeClient().fetch(ticker)
    if not payload:
        raise RuntimeError(f"no CBOE chain data for {ticker}")
    data = payload["data"]
    spot = float(data["current_price"])

    call_gex = defaultdict(float)
    put_gex = defaultdict(float)
    for o in data["options"]:
        m = _OPT_PAT.match(o["option"])
        if not m:
            continue
        exp, cp, strike = m.group(1), m.group(2), int(m.group(3)) / 1000.0
        dte = (date(2000 + int(exp[:2]), int(exp[2:4]), int(exp[4:])) - ref_date).days
        if dte < 0 or dte > max_dte:
            continue
        oi = float(o.get("open_interest") or 0)
        g = float(o.get("gamma") or 0)
        if oi <= 0 or g <= 0:
            continue
        gex = g * oi * 100 * spot  # $ of gamma per 1pt move
        if cp == "C":
            call_gex[strike] += gex
        else:
            put_gex[strike] += gex

    if not call_gex and not put_gex:
        raise RuntimeError(f"no usable gamma data for {ticker}")

    ceiling = max(
        (s for s in call_gex if s > spot * (1 + ATM_BUFFER)),
        key=lambda s: call_gex[s],
        default=None,
    )
    floor = max(
        (s for s in put_gex if s < spot * (1 - ATM_BUFFER)),
        key=lambda s: put_gex[s],
        default=None,
    )

    strikes = sorted(set(call_gex) | set(put_gex))
    net = {s: call_gex[s] - put_gex[s] for s in strikes}
    flip = None
    for a, b in zip(strikes, strikes[1:]):
        if net[a] != 0 and (net[a] > 0) != (net[b] > 0):
            flip = (a + b) / 2
            break

    total = sum(call_gex.values()) - sum(put_gex.values())
    mood = "jumpy" if total < 0 else "calm"

    return {
        "ticker": ticker.upper(),
        "spot": spot,
        "ceiling": ceiling,
        "floor": floor,
        "flip": flip,
        "mood": mood,
        "net_gex_b": total / 1e9,
        "as_of": ref_date.isoformat(),
    }
