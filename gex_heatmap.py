"""Strike x expiry GEX heatmap for any ticker, from the free CBOE delayed chain.

Same data and math as the Premarket Gamma Brief's net-GEX engine
(gex_chart.strike_gex conventions: GEX = gamma * OI * 100 * spot, calls
positive, puts negative), but instead of collapsing expiries into one number
per strike, it keeps the full strike-by-expiry matrix -- the professional
"GEX heatmap" view.

The chart is 16:9, auto-highlights the important strikes (heaviest call wall,
heaviest put wall, spot), and carries a plain-English "quick read" of what
the positioning implies.

Honest limits (stated on the chart itself):
- CBOE delayed quotes (~15 min). Overnight/weekends the chain is the last close.
- Only tickers with listed options. No options -> no heatmap, never invented.

Usage:
    python3 -m whaleradar.gex_heatmap ONDS
    python3 -m whaleradar.gex_heatmap SPY --expiries 8 --dte 90
"""
import argparse
import html as html_mod
import math
import os
import sys
from collections import defaultdict
from datetime import date

from cboe_client import CboeClient
from gamma import _OPT_PAT, _last_trading_day

STRIKE_BAND = 0.25   # strikes within +-25% of spot
MAX_ROWS = 18        # fills the 16:9 frame
N_EXPIRIES = 15


def heatmap_data(ticker: str, max_dte: int = 120, n_expiries: int = N_EXPIRIES,
                 ref_date: date = None, max_rows: int = MAX_ROWS):
    """Per-(strike, expiry) net GEX matrix. Returns dict or raises RuntimeError."""
    ref_date = _last_trading_day(ref_date or date.today())
    payload = CboeClient().fetch(ticker)
    if not payload:
        raise RuntimeError(f"no CBOE options chain for {ticker} "
                           f"(no listed options or unknown ticker)")
    data = payload["data"]
    spot = float(data["current_price"])
    as_of = (data.get("last_trade_time") or "")[:10] or ref_date.isoformat()
    as_of_ts = (data.get("last_trade_time") or "").replace("T", " ")[:16] or as_of

    per_exp = defaultdict(lambda: defaultdict(float))  # exp_date -> strike -> net gex
    for o in data["options"]:
        m = _OPT_PAT.match(o["option"])
        if not m:
            continue
        exp, cp, strike = m.group(1), m.group(2), int(m.group(3)) / 1000.0
        ed = date(2000 + int(exp[:2]), int(exp[2:4]), int(exp[4:]))
        dte = (ed - ref_date).days
        if dte < 0 or dte > max_dte:
            continue
        oi = float(o.get("open_interest") or 0)
        g = float(o.get("gamma") or 0)
        if oi <= 0 or g <= 0:
            continue
        gex = g * oi * 100 * spot
        per_exp[ed][strike] += gex if cp == "C" else -gex

    if not per_exp:
        raise RuntimeError(f"no usable GEX data for {ticker}")
    expiries = sorted(per_exp)[:n_expiries]

    # All strikes with any GEX: the "important" ones are picked by size below,
    # so sparse names (few strikes) still fill the frame.
    in_band = {s for ed in expiries for s in per_exp[ed] if per_exp[ed][s] != 0}
    if not in_band:
        raise RuntimeError(f"no strikes with GEX for {ticker}")
    totals_all = {s: sum(per_exp[ed].get(s, 0.0) for ed in expiries)
                  for s in in_band}

    # The walls: the biggest positive / negative per-strike piles
    # anywhere in the band. Computed here so the wall strikes can be
    # force-included in the displayed strikes below - the standout
    # cells live inside the wall rows, so the walls must always be
    # on the map.
    call_wall = max(((s, totals_all[s]) for s in in_band
                     if totals_all[s] > 0),
                    key=lambda x: x[1], default=(None, 0.0))
    put_wall = min(((s, totals_all[s]) for s in in_band
                    if totals_all[s] < 0),
                   key=lambda x: x[1], default=(None, 0.0))

    # The "interesting" strikes: heaviest above spot (bullish, blue) and
    # heaviest below spot (bearish, yellow).
    above = [s for s in in_band if s > spot]
    below = [s for s in in_band if s < spot]
    bull_strike = max(above, key=lambda s: totals_all[s], default=None)
    bear_strike = min(below, key=lambda s: totals_all[s], default=None)

    # The "important strikes": the strike nearest spot plus the heaviest
    # GEX strikes by absolute total. Bull/bear strikes always included.
    # Sorted high strike at top.
    spot_s = min(in_band, key=lambda s: abs(s - spot))
    ranked = sorted(in_band, key=lambda s: abs(totals_all[s]), reverse=True)
    picked = [spot_s] + [s for s in ranked if s != spot_s][:max_rows - 1]
    for s in (bull_strike, bear_strike, call_wall[0], put_wall[0]):
        if s is not None and s not in picked:
            picked.append(s)
    strikes = sorted(picked, reverse=True)

    matrix = [[per_exp[ed].get(s, 0.0) for ed in expiries] for s in strikes]
    totals = [totals_all[s] for s in strikes]
    col_totals = [sum(per_exp[ed].get(s, 0.0) for s in strikes) for ed in expiries]

    tot_call = tot_put = 0.0
    for ed in expiries:
        for s, v in per_exp[ed].items():
            if v > 0:
                tot_call += v
            else:
                tot_put += v
    # biggest single strike x expiry concentration
    big = (None, None, 0.0)
    for ed in expiries:
        for s in in_band:
            v = per_exp[ed].get(s, 0.0)
            if abs(v) > abs(big[2]):
                big = (s, ed, v)

    return {
        "ticker": ticker.upper(),
        "spot": spot,
        "as_of": as_of,
        "as_of_ts": as_of_ts,
        "strikes": strikes,
        "expiries": [{"date": ed, "dte": (ed - ref_date).days,
                      "label": ed.strftime("%b %-d")} for ed in expiries],
        "matrix": matrix,
        "totals": totals,
        "col_totals": col_totals,
        "tot_call": tot_call,
        "tot_put": tot_put,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "bull_strike": bull_strike,
        "bear_strike": bear_strike,
        "biggest_cell": {"strike": big[0], "expiry": big[1], "value": big[2]},
    }


def _fmt_cell(v):
    if v == 0:
        return "$0"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e9:
        return f"{sign}${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:,.1f}K"
    return f"{sign}${a:,.0f}"


def describe(d):
    """Short point-form read: walls, price position, regime, directional lean."""
    ticker, spot = d["ticker"], d["spot"]
    cw, pw = d["call_wall"], d["put_wall"]
    net = d["tot_call"] + d["tot_put"]
    big = d["biggest_cell"]
    cw_s, cw_v = cw
    pw_s, pw_v = pw

    pts = []

    # 1. The walls, with which side is heavier.
    if pw_s is not None and cw_s is not None:
        pw_a, cw_a = abs(pw_v), abs(cw_v)
        if pw_a >= 1.5 * cw_a:
            dom = "heavier magnet below"
        elif cw_a >= 1.5 * pw_a:
            dom = "heavier magnet above"
        else:
            dom = "evenly matched"
        pts.append(f"Put wall ${pw_s:g} ({_fmt_cell(pw_v)}) vs call wall "
                   f"${cw_s:g} ({_fmt_cell(cw_v)}) - {dom}")
    elif pw_s is not None:
        pts.append(f"Put wall ${pw_s:g} ({_fmt_cell(pw_v)}) below, no real call wall above")
    elif cw_s is not None:
        pts.append(f"Call wall ${cw_s:g} ({_fmt_cell(cw_v)}) above, no real put wall below")

    # 2. Where price sits + key expiry concentration.
    pos = f"Price ${spot:.2f}"
    if pw_s is not None and cw_s is not None:
        if spot <= pw_s * 1.03:
            pos += f" pressing the ${pw_s:g} put wall"
        elif spot >= cw_s * 0.97:
            pos += f" pressing the ${cw_s:g} call wall"
        else:
            pos += " between the walls"
    bit = ""
    if (big["strike"] is not None and big["expiry"] is not None
            and big["strike"] in d["strikes"]):
        strike_total = next(
            t for s, t in zip(d["strikes"], d["totals"]) if s == big["strike"])
        if strike_total and abs(big["value"]) > 0.35 * abs(strike_total):
            exp_label = big["expiry"].strftime("%b %-d")
            side = "put" if big["value"] < 0 else "call"
            bit = (f"; most of the ${big['strike']:g} {side} gamma "
                   f"expires {exp_label}")
    pts.append(pos + bit)

    # 3. Net gamma regime, one clause.
    if net < 0:
        pts.append(f"Net gamma {_fmt_cell(net)} (negative) - put side winning, bigger swings")
    elif net > 0:
        pts.append(f"Net gamma {_fmt_cell(net)} (positive) - call side winning, choppy and pinned")
    else:
        pts.append("Net gamma flat - no side in control")

    # 4. The lean: which side's positioning dominates.
    if pw_s is not None and cw_s is not None:
        if abs(pw_v) >= 1.5 * abs(cw_v):
            lean = ("BEARISH", "puts", "put side dominates")
        elif abs(cw_v) >= 1.5 * abs(pw_v):
            lean = ("BULLISH", "calls", "call side dominates")
        else:
            lean = ("NEUTRAL", None, "walls evenly matched - expect chop")
    elif pw_s is not None:
        lean = ("BEARISH", "puts", "only real wall is below")
    elif cw_s is not None:
        lean = ("BULLISH", "calls", "only real wall is above")
    else:
        lean = ("NEUTRAL", None, "no clear walls")
    if lean[1]:
        pts.append(f"Lean: {lean[0]} (favors {lean[1]}) - {lean[2]}")
    else:
        pts.append(f"Lean: {lean[0]} - {lean[2]}")

    return "\n\n".join([f"${ticker}"] + [f"- {p}" for p in pts])


def verdict(d):
    """Verdict banner + suggested play for the results page.

    Returns dict with lean (BEARISH/BULLISH/NEUTRAL), headline, clause,
    and suggestion (e.g. "$25 puts expiring Dec 18") or None.
    """
    ticker = d["ticker"]
    cw, pw = d["call_wall"], d["put_wall"]
    cw_s, cw_v = cw
    pw_s, pw_v = pw
    strikes, expiries, matrix = d["strikes"], d["expiries"], d["matrix"]

    if pw_s is not None and cw_s is not None:
        if abs(pw_v) >= 1.5 * abs(cw_v):
            lean = "BEARISH"
        elif abs(cw_v) >= 1.5 * abs(pw_v):
            lean = "BULLISH"
        else:
            lean = "NEUTRAL"
    elif pw_s is not None:
        lean = "BEARISH"
    elif cw_s is not None:
        lean = "BULLISH"
    else:
        lean = "NEUTRAL"

    clause = {"BEARISH": "the put side is in control",
              "BULLISH": "the call side is in control",
              "NEUTRAL": "neither side is in control"}[lean]

    suggestion = None
    if lean == "BEARISH" and d["bear_strike"] in strikes:
        bi = strikes.index(d["bear_strike"])
        bj = min(range(len(expiries)), key=lambda j: matrix[bi][j])
        suggestion = {"side": "puts",
                      "strike": f"${d['bear_strike']:g}",
                      "expiry": expiries[bj]["label"],
                      "text": (f"${d['bear_strike']:g} puts expiring "
                               f"{expiries[bj]['label']}")}
    elif lean == "BULLISH" and d["bull_strike"] in strikes:
        bi = strikes.index(d["bull_strike"])
        bj = max(range(len(expiries)), key=lambda j: matrix[bi][j])
        suggestion = {"side": "calls",
                      "strike": f"${d['bull_strike']:g}",
                      "expiry": expiries[bj]["label"],
                      "text": (f"${d['bull_strike']:g} calls expiring "
                               f"{expiries[bj]['label']}")}

    return {"lean": lean,
            "headline": f"{ticker} looks {lean} right now",
            "clause": clause,
            "suggestion": suggestion}


def page_extras(d):
    """Stat tiles + beginner 1-2-3 walkthrough cards for the results page.

    Returns {"verdict": verdict(d), "tiles": [...], "cards": [...]}.
    Tiles are {"k": label, "v": value, "c": color class ("r"/"g"/"y"/"")}.
    Cards are {"n": number, "title": str, "html": str, "hot": bool}.
    """
    vd = verdict(d)
    ticker, spot = d["ticker"], d["spot"]
    cw, pw = d["call_wall"], d["put_wall"]
    cw_s, cw_v = cw
    pw_s, pw_v = pw
    net = d["tot_call"] + d["tot_put"]
    lean = vd["lean"]

    # Heaviest expiry: the expiry column holding the most gamma.
    hexp_label = "-"
    try:
        cols = d.get("col_totals") or []
        if cols and d.get("expiries"):
            hexp_label = max(zip(d["expiries"], cols),
                             key=lambda p: abs(p[1]))[0]["label"]
    except Exception:
        pass
    tiles = [
        {"k": "TICKER", "v": f"{ticker}", "c": ""},
        {"k": "NET GEX", "v": _fmt_cell(net),
         "c": "r" if net < 0 else "g" if net > 0 else ""},
        {"k": "HEAVIEST-EXPIRY", "v": hexp_label, "c": ""},
        {"k": "PUT WALL", "v": f"${pw_s:g}" if pw_s is not None else "-",
         "c": "r"},
        {"k": "SPOT", "v": f"${spot:.2f}", "c": ""},
        {"k": "CALL WALL", "v": f"${cw_s:g}" if cw_s is not None else "-",
         "c": "g"},
    ]

    cards = []

    # Content ownership (Justin's no-repeat rule, Oct 29): each box owns
    # its facts and no other box restates them. Tiles own the raw numbers,
    # the banner owns the lean. Card 1 owns the walls (strike, size, which
    # is heavier). Card 2 owns price (spot, distance to each wall, room
    # between them, and what a flipped wall - one on the wrong side of
    # price - means). Card 3 owns what to watch (break / bounce scenarios,
    # next piles, expiry concentration). The play card owns the pick
    # (what you are seeing, what would kill it, decay).

    # 1. Find the walls: strike + size + which side is the magnet.
    # Position words live in card 2 now, not here.
    if pw_s is not None and cw_s is not None:
        pw_a, cw_a = abs(pw_v), abs(cw_v)
        if pw_a >= cw_a:
            heavy, ratio = "put", pw_a / cw_a if cw_a else 99
        else:
            heavy, ratio = "call", cw_a / pw_a if pw_a else 99
        if ratio < 1.5:
            magnet_pt = ("The two walls are about even - neither one "
                         "is a real magnet")
        else:
            magnet_pt = (f"The {heavy} wall is about "
                         f"{int(round(ratio))}x heavier - that is "
                         f"the magnet")
        points = [
            f"Put wall <b class=\"r\">${pw_s:g}</b> ({_fmt_cell(pw_v)})",
            f"Call wall <b class=\"g\">${cw_s:g}</b> ({_fmt_cell(cw_v)})",
            magnet_pt,
        ]
    elif pw_s is not None:
        points = [
            f"Put wall <b class=\"r\">${pw_s:g}</b> ({_fmt_cell(pw_v)})",
            "No real call wall on this chain - the put side is the "
            "only magnet",
        ]
    elif cw_s is not None:
        points = [
            f"Call wall <b class=\"g\">${cw_s:g}</b> ({_fmt_cell(cw_v)})",
            "No real put wall on this chain - the call side is the "
            "only magnet",
        ]
    else:
        points = ["No clear gamma walls on this chain - positioning is "
                  "scattered."]
    cards.append({"n": 1, "title": "FIND THE WALLS", "points": points,
                  "hot": False})

    # 2. See where price sits: spot, the distance to each wall ($ and %),
    # and the room that leaves. Distances are measured, never assumed -
    # no other box states them. (The net-gamma regime line that used to
    # sit here is gone: the tiles own the net number and the banner owns
    # the lean, so stating it a third way was a repeat.)
    def _dist_pt(name, strike, cls):
        dd = abs(strike - spot)
        pct = dd / spot * 100 if spot else 0
        if dd <= spot * 0.005:
            where = "right at the price"
        elif strike > spot:
            where = "above the price"
        else:
            where = "below the price"
        pct_txt = f"{pct:.1f}" if pct >= 0.1 else "<0.1"
        return (f"<b class=\"{cls}\">${strike:g}</b> {name} wall is "
                f"${dd:,.2f} away ({pct_txt}%) - {where}")

    pts2 = [f"<b>{ticker}</b> trades at <b>${spot:.2f}</b>"]
    if pw_s is not None:
        pts2.append(_dist_pt("put", pw_s, "r"))
    if cw_s is not None:
        pts2.append(_dist_pt("call", cw_s, "g"))
    if pw_s is not None and cw_s is not None:
        lo, hi = min(pw_s, cw_s), max(pw_s, cw_s)
        width = hi - lo
        wpct = width / spot * 100 if spot else 0
        if lo <= spot <= hi:
            if wpct < 5:
                room = (f"The walls are only ${width:,.2f} apart "
                        f"({wpct:.1f}%) with price between them - "
                        f"boxed in tight")
            else:
                room = (f"That leaves ${width:,.2f} of room between "
                        f"the walls ({wpct:.1f}%) - price is not "
                        f"boxed in")
        elif spot < lo:
            room = ("Price sits under both walls - every big pile is "
                    "overhead, nothing underneath it")
        else:
            room = ("Price sits over both walls - every big pile is "
                    "underneath it, nothing overhead")
        pts2.append(room)
    elif pw_s is not None or cw_s is not None:
        pts2.append("Open space on the other side - no wall over "
                    "there at all")
    else:
        pts2.append("No clear walls to measure against")
    # Flipped walls: a wall on the wrong side of price does the
    # opposite job - an overhead put wall caps rallies instead of
    # flooring drops, a call wall underneath props price up instead
    # of capping it. The distances above say where the walls are;
    # this line owns what that arrangement means. No other box
    # states it. (Justin's ask, Oct 29: explain the wrong-side
    # cases further in the boxes, without repeating anything.)
    flip_put = pw_s is not None and pw_s > spot * 1.005
    flip_call = cw_s is not None and cw_s < spot * 0.995
    if flip_put and flip_call:
        pts2.append("Both walls are flipped here - the put wall "
                    "overhead caps rallies more than it floors "
                    "drops, and the call wall underneath props "
                    "price up more than it caps it")
    elif flip_put:
        pts2.append("The put wall is flipped here - sitting "
                    "overhead, it caps rallies more than it floors "
                    "drops")
    elif flip_call:
        pts2.append("The call wall is flipped here - underneath "
                    "the price, it props price up more than it "
                    "caps it")
    cards.append({"n": 2, "title": "SEE WHERE PRICE SITS", "hot": False,
                  "points": pts2})

    # 3. What to watch: scenarios, not pointers. What a break or a bounce
    # at each wall would mean, the next big pile past it, and whether one
    # expiry drives the whole read. (The old filler lines - "the expiry
    # named in the tile above", "the card below names the strike" -
    # repeated other boxes and said nothing; they are gone.)
    def _next_pile(strike, up):
        cand = [(s, t) for s, t in zip(d["strikes"], d["totals"])
                if (s > strike if up else s < strike)]
        if not cand:
            return None
        return max(cand, key=lambda p: abs(p[1]))[0]

    pts = []
    walls = [(k, s) for k, s in (("put", pw_s), ("call", cw_s))
             if s is not None]
    above = [(k, s) for k, s in walls if s >= spot * 0.995]
    below = [(k, s) for k, s in walls if s < spot * 0.995]
    at_price = any(abs(s - spot) <= spot * 0.005 for k, s in walls)
    if above:
        k, s = min(above, key=lambda w: w[1])
        nxt = _next_pile(s, True)
        tail = (f"the next big pile up is <b>${nxt:g}</b>"
                if nxt is not None else
                "there is no other big pile above it on this map - "
                "open air")
        pts.append(f"If price pushes through <b>${s:g}</b> and holds "
                   f"above it, that ceiling can flip into a floor - "
                   f"and {tail}")
    elif walls:
        pts.append("Price is already above every wall on this map - "
                   "nothing overhead caps it, so the top pile "
                   "underneath is the line it has to hold")
    if below:
        k, s = max(below, key=lambda w: w[1])
        nxt = _next_pile(s, False)
        tail = (f"if it breaks, the next big pile down is "
                f"<b>${nxt:g}</b>" if nxt is not None else
                "there is no other big pile below it on this map")
        pts.append(f"If price drops to <b>${s:g}</b>, that is the "
                   f"first real test - a bounce there keeps this "
                   f"range alive; {tail}")
    elif walls and not at_price:
        pts.append("No wall sits underneath price on this map - no "
                   "real floor until positioning builds below it")
    if not walls:
        pts.append("Positioning is scattered - no wall to break or "
                   "bounce off, so there is no clean level to watch")
    cols = d.get("col_totals") or []
    if cols:
        tot_abs = sum(abs(c) for c in cols)
        bi = max(range(len(cols)), key=lambda j: abs(cols[j]))
        share = abs(cols[bi]) / tot_abs if tot_abs else 0
        be = d["expiries"][bi]
        if share > 0.5:
            pts.append(f"One date drives this read: the {be['label']} "
                       f"expiry ({be['dte']} day"
                       f"{'s' if be['dte'] != 1 else ''} out) holds about "
                       f"{share:.0%} of all the gamma on the map - "
                       f"after that date a big chunk of this map "
                       f"disappears, so the read can change fast")
        else:
            pts.append("Gamma is spread across the expiries - no "
                       "single date drives this read, so it should "
                       "change slowly, not flip overnight")
    cards.append({"n": 3, "title": "WHAT TO WATCH", "points": pts,
                  "hot": False})

    # MAP POINTS TO play card: headline + factual read-strength line +
    # short bullets. No letter/percentage grade - one grade can't honestly
    # combine direction, timing, decay and IV without backtesting, so the
    # card shows the real facts instead and lets them speak.
    play = None
    if vd["suggestion"]:
        s = vd["suggestion"]
        noun = "put" if s["side"] == "puts" else "call"
        wall_v = pw_v if s["side"] == "puts" else cw_v
        opp_v = cw_v if s["side"] == "puts" else pw_v
        opp_s = cw_s if s["side"] == "puts" else pw_s
        snum = float(str(s["strike"]).replace("$", ""))
        wi = min(range(len(d["strikes"])),
                 key=lambda i: abs(d["strikes"][i] - snum))
        wrow = d["matrix"][wi]
        row_abs = sum(abs(v) for v in wrow)
        ei = next((j for j, e in enumerate(d["expiries"])
                   if e["label"] == s["expiry"]), None)
        share = (abs(wrow[ei]) / row_abs) if (ei is not None and row_abs) else 0
        dte = next((e["dte"] for e in d["expiries"]
                    if e["label"] == s["expiry"]), None)
        # Facts line owns this strike's weight in the whole map. (A
        # distance line here repeated card 2 word for word whenever
        # the play strike is a wall - which is most of the time.)
        map_abs = sum(abs(t) for t in d["totals"]) or 0
        share_all = row_abs / map_abs if map_abs else 0
        wall_s2 = cw_s if s["side"] == "calls" else pw_s
        facts = (f"This one strike holds about {share_all:.0%} of "
                 f"all the gamma on the map")
        if wall_s2 is not None and snum == wall_s2:
            facts += " - it is the biggest wall on the map itself"
        if dte is None:
            decay = None
        elif dte <= 7:
            decay = (f"Only {dte} day{'s' if dte != 1 else ''} to "
                     f"expiry - time decay is brutal this close. The "
                     f"option bleeds value every day, faster into "
                     f"expiry.")
        elif dte <= 21:
            decay = (f"{dte} days to expiry - decay is moderate now and "
                     f"accelerates hard in the final week.")
        elif dte <= 45:
            decay = (f"{dte} days to expiry - decay is slow for now, but "
                     f"it ramps up in the last 2-3 weeks.")
        else:
            decay = (f"{dte} days to expiry - plenty of time. Decay is "
                     f"slow at this distance.")
        # Bullets own the explanation: what you are seeing in plain
        # words, what would kill the read, and the decay clock. The
        # pile size stated is this strike's own biggest cell, measured
        # from its row - not the wall total card 1 already stated.
        cell_v = wrow[ei] if ei is not None else 0
        if s["side"] == "calls":
            seeing = (f"What you are seeing: the heaviest call pile "
                      f"above the price sits at this strike, and "
                      f"{share:.0%} of that strike's gamma lands in "
                      f"this one expiry - {_fmt_cell(cell_v)} in a "
                      f"single cell of the map. One strike, one date: "
                      f"piles like that pull price toward them and "
                      f"often stall it on the first test.")
            kill = (f"What would kill it: price getting rejected at "
                    f"{s['strike']} and falling away from it, or this "
                    f"pile shrinking on the next map - the read lives "
                    f"and dies with this pile")
        else:
            seeing = (f"What you are seeing: the heaviest put pile "
                      f"below the price sits at this strike, and "
                      f"{share:.0%} of that strike's gamma lands in "
                      f"this one expiry - {_fmt_cell(cell_v)} in a "
                      f"single cell of the map. One strike, one date: "
                      f"piles like that pull price toward them and "
                      f"often floor it on the first test.")
            kill = (f"What would kill it: price bouncing off "
                    f"{s['strike']} and climbing away from it, or this "
                    f"pile shrinking on the next map - the read lives "
                    f"and dies with this pile")
        points = [seeing, kill]
        if decay:
            points.append(decay)
        play = {
            "side": s["side"], "strike": s["strike"], "expiry": s["expiry"],
            "facts": facts, "points": points,
        }

    return {"verdict": vd, "tiles": tiles, "cards": cards, "play": play}


def render_html(d):
    ticker, spot = d["ticker"], d["spot"]
    strikes, expiries = d["strikes"], d["expiries"]
    matrix = d["matrix"]
    bull, bear = d["bull_strike"], d["bear_strike"]
    cw, pw = d["call_wall"], d["put_wall"]

    vmax = max((abs(v) for row in matrix for v in row), default=0) or 1.0

    def cell_style(v):
        if v == 0:
            return "background:#12161d;color:#5b6472;"
        inten = 0.18 + 0.62 * math.sqrt(abs(v) / vmax)
        bg = (f"rgba(34,197,94,{inten:.2f})" if v > 0
              else f"rgba(239,68,68,{inten:.2f})")
        return f"background:{bg};color:#f2f5f9;"

    # The single standout cell per wall: biggest expiry concentration
    # inside the CALL WALL row (blue) and inside the PUT WALL row
    # (yellow), so the standout always lines up with its wall tag.
    blue_ij = yellow_ij = None
    blue_exp = bear_exp = ""
    if cw[0] is not None and cw[0] in strikes:
        bi = strikes.index(cw[0])
        bj = max(range(len(expiries)), key=lambda j: matrix[bi][j])
        blue_ij = (bi, bj)
        blue_exp = expiries[bj]["label"]
    if pw[0] is not None and pw[0] in strikes:
        bi = strikes.index(pw[0])
        bj = min(range(len(expiries)), key=lambda j: matrix[bi][j])
        yellow_ij = (bi, bj)
        bear_exp = expiries[bj]["label"]

    spot_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))

    parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8">',
        "<style>",
        "body{background:#0b0e13;color:#e8ecf1;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;margin:0;padding:0;}",
        ".window{width:1280px;height:720px;box-sizing:border-box;margin:0 auto;padding:10px 16px;overflow:hidden;display:flex;flex-direction:column;justify-content:flex-start;}",
        ".head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;}",
        ".title{font-size:20px;font-weight:800;}",
        ".sub{font-size:11px;color:#8b94a3;}",
        ".stats{display:flex;gap:16px;flex-wrap:wrap;margin:0 0 6px;font-size:11.5px;color:#8b94a3;}",
        ".stats b{color:#e8ecf1;font-size:12.5px;}",
        ".tablewrap{flex:1;min-height:0;display:flex;}",
        "table{border-collapse:collapse;width:100%;height:100%;font-size:11px;}",
        "th,td{padding:6px 7px;text-align:right;border:1px solid #1c232e;white-space:nowrap;}",
        "th{background:#141a23;color:#8b94a3;font-weight:600;}",
        "td.strike{background:#141a23;color:#e8ecf1;font-weight:700;}",
        "tr.spotrow td.strike{background:#1d2b1f;color:#4ade80;}",
        "tr.wallcall td.strike{box-shadow:inset 3px 0 0 #4ade80;}",
        "tr.wallput td.strike{box-shadow:inset 3px 0 0 #f87171;}",
        ".tag{display:inline-block;font-size:8.5px;font-weight:800;letter-spacing:1px;padding:1px 5px;border-radius:3px;margin-left:6px;vertical-align:1px;}",
        ".tag-call{background:#14532d;color:#4ade80;}",
        ".tag-put{background:#450a0a;color:#f87171;}",
        ".foot{margin-top:10px;font-size:11px;color:#8b94a3;}",
        "</style></head><body><div class='window'>",
    ]
    parts.append(
        f"<div class='head'><div class='title'>GEX Heatmap - ${html_mod.escape(ticker)} "
        f"<span style='color:#8b94a3;font-weight:400;font-size:14px;'>${spot:,.2f}</span></div>"
        f"<div class='sub'>Chain as of {html_mod.escape(d['as_of_ts'])} (CBOE - may be delayed)</div></div>"
    )
    stats = (
        "<div class='stats'>"
        f"<span>Call GEX <b style='color:#4ade80'>{_fmt_cell(d['tot_call'])}</b></span>"
        f"<span>Put GEX <b style='color:#f87171'>{_fmt_cell(d['tot_put'])}</b></span>"
        f"<span>Net <b>{_fmt_cell(d['tot_call'] + d['tot_put'])}</b></span>"
    )
    if cw[0] is not None:
        stats += f"<span>Heaviest call wall <b style='color:#4ade80'>{cw[0]:g}</b></span>"
    if pw[0] is not None:
        stats += f"<span>Heaviest put wall <b style='color:#f87171'>{pw[0]:g}</b></span>"
    stats += "</div>"
    parts.append(stats)

    parts.append("<div class='tablewrap'><table><tr><th style='text-align:left'>Strike</th>")
    for e in expiries:
        parts.append(f"<th>{e['label']}<br><span style='font-weight:400'>{e['dte']}d</span></th>")
    parts.append("<th>Total</th></tr>")

    for i, s in enumerate(strikes):
        classes = []
        tag = ""
        if cw[0] is not None and s == cw[0]:
            classes.append("wallcall")
            tag = "<span class='tag tag-call'>CALL WALL</span>"
        if pw[0] is not None and s == pw[0]:
            classes.append("wallput")
            tag = "<span class='tag tag-put'>PUT WALL</span>"
        if i == spot_i and not classes:
            classes.append("spotrow")
        cls = f" class='{' '.join(classes)}'" if classes else ""
        parts.append(f"<tr{cls}><td class='strike'>{s:g}{tag}</td>")
        for j, v in enumerate(matrix[i]):
            if (i, j) == blue_ij:
                parts.append(
                    f"<td style=\"background:#2f6fed;color:#ffffff;font-weight:800;\">{_fmt_cell(v)}</td>")
            elif (i, j) == yellow_ij:
                parts.append(
                    f"<td style=\"background:#ffd60a;color:#1a1a1a;font-weight:800;\">{_fmt_cell(v)}</td>")
            else:
                parts.append(f"<td style=\"{cell_style(v)}\">{_fmt_cell(v)}</td>")
        parts.append(f"<td style=\"{cell_style(d['totals'][i])}font-weight:700;\">{_fmt_cell(d['totals'][i])}</td></tr>")
    parts.append("<tr><td class='strike'>Total</td>")
    for v in d["col_totals"]:
        parts.append(f"<td style=\"{cell_style(v)}font-weight:700;\">{_fmt_cell(v)}</td>")
    grand = sum(d["col_totals"])
    parts.append(f"<td style=\"{cell_style(grand)}font-weight:700;\">{_fmt_cell(grand)}</td>")
    parts.append("</table></div>")
    parts.append(
        "<div class='foot'>Green = positive (call) gamma exposure &middot; "
        "Red = negative (put) gamma exposure &middot; "
        "Blue = bullish level &middot; Yellow = bearish level</div>"
    )
    parts.append("</div></body></html>")
    return "\n".join(parts)

def build(ticker, out_dir=None, n_expiries=N_EXPIRIES, max_dte=60, make_png=True):
    d = heatmap_data(ticker, max_dte=max_dte, n_expiries=n_expiries)
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", "files")
    os.makedirs(out_dir, exist_ok=True)
    html_path = os.path.join(out_dir, f"gex-heatmap-{d['ticker']}.html")
    with open(html_path, "w") as f:
        f.write(render_html(d))
    png_path = None
    if make_png:
        import subprocess
        renderer = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "render_card_png.py")
        png_path = os.path.join(out_dir, f"gex-heatmap-{d['ticker']}.png")
        subprocess.run([sys.executable, renderer, html_path, png_path,
                        "--viewport", "1280x720"], check=True,
                       capture_output=True)
    return d, html_path, png_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--expiries", type=int, default=N_EXPIRIES)
    ap.add_argument("--dte", type=int, default=60)
    ap.add_argument("--no-png", action="store_true")
    ap.add_argument("--read", action="store_true",
                    help="print the plain-English quick read to stdout")
    args = ap.parse_args()
    try:
        d, html_path, png_path = build(args.ticker, n_expiries=args.expiries,
                                       max_dte=args.dte,
                                       make_png=not args.no_png)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 1
    print(f"spot {d['spot']:.2f} as of {d['as_of_ts']}")
    if args.read:
        print("READ:", describe(d))
    print(f"html: {html_path}")
    if png_path:
        print(f"png:  {png_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
