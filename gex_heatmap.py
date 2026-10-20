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
    for s in (bull_strike, bear_strike):
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
    call_wall = max(((s, totals_all[s]) for s in in_band if totals_all[s] > 0),
                    key=lambda x: x[1], default=(None, 0.0))
    put_wall = min(((s, totals_all[s]) for s in in_band if totals_all[s] < 0),
                   key=lambda x: x[1], default=(None, 0.0))

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
        {"k": "TICKER", "v": f"${ticker}", "c": ""},
        {"k": "NET GEX", "v": _fmt_cell(net),
         "c": "r" if net < 0 else "g" if net > 0 else ""},
        {"k": "HEAVIEST EXPIRY", "v": hexp_label, "c": ""},
        {"k": "PUT WALL", "v": f"${pw_s:g}" if pw_s is not None else "-",
         "c": "r"},
        {"k": "SPOT", "v": f"${spot:.2f}", "c": ""},
        {"k": "CALL WALL", "v": f"${cw_s:g}" if cw_s is not None else "-",
         "c": "g"},
    ]

    cards = []
    # 1. Find the walls.
    if pw_s is not None and cw_s is not None:
        pw_a, cw_a = abs(pw_v), abs(cw_v)
        if pw_a >= cw_a:
            heavy, ratio = "put", pw_a / cw_a if cw_a else 99
        else:
            heavy, ratio = "call", cw_a / pw_a if pw_a else 99
        html = (f"Big money clusters at two strikes: the "
                f"<b class=\"r\">${pw_s:g} put wall</b> ({_fmt_cell(pw_v)}) "
                f"below and the <b class=\"g\">${cw_s:g} call wall</b> "
                f"({_fmt_cell(cw_v)}) above. The {heavy} wall is about "
                f"{int(round(ratio))}x heavier - that is the magnet.")
    elif pw_s is not None:
        html = (f"Big money clusters below: the "
                f"<b class=\"r\">${pw_s:g} put wall</b> ({_fmt_cell(pw_v)}). "
                f"No real call wall above - the magnet points down.")
    elif cw_s is not None:
        html = (f"Big money clusters above: the "
                f"<b class=\"g\">${cw_s:g} call wall</b> ({_fmt_cell(cw_v)}). "
                f"No real put wall below - the magnet points up.")
    else:
        html = "No clear gamma walls on this chain - positioning is scattered."
    cards.append({"n": 1, "title": "FIND THE WALLS", "html": html,
                  "hot": False})

    # 2. See where price sits.
    if pw_s is not None and cw_s is not None:
        if spot <= pw_s * 1.03:
            pos = f"pressing the <b class=\"r\">${pw_s:g} put wall</b>"
        elif spot >= cw_s * 0.97:
            pos = f"pressing the <b class=\"g\">${cw_s:g} call wall</b>"
        else:
            near = "put" if abs(spot - pw_s) < abs(spot - cw_s) else "call"
            pos = f"between the walls, closer to the {near} wall"
    else:
        pos = "on the map"
    regime = ("the put side is winning and swings get bigger" if net < 0
              else "the call side is winning and price gets choppy" if net > 0
              else "neither side is in control")
    cards.append({"n": 2, "title": "SEE WHERE PRICE SITS", "hot": False,
                  "html": (f"<b>${ticker}</b> trades at <b>${spot:.2f}</b>, "
                           f"{pos}. Net gamma is <b>{_fmt_cell(net)}</b>, "
                           f"so {regime}.")})

    # 3. What to watch: walls move over time (unique beginner insight -
    # the banner owns the verdict, card 1 owns the magnet, card 2 owns the
    # price position, the play card owns the trade).
    _wmove = ("Walls are not permanent - they are built from open options "
              "and shift as expiry dates pass. The heaviest action right "
              "now sits in the expiry named in the tile above.")
    if lean == "NEUTRAL":
        w = (_wmove + " No single strike stands out today, so there is no "
             "MAP POINTS TO card.")
    else:
        w = (_wmove + " The MAP POINTS TO card below names the exact strike "
             "and date.")
    cards.append({"n": 3, "title": "WHAT TO WATCH", "html": w, "hot": False})

    # MAP POINTS TO play card (the recommended option display).
    play = None
    if vd["suggestion"]:
        s = vd["suggestion"]
        noun = "put" if s["side"] == "puts" else "call"
        wall_v = pw_v if s["side"] == "puts" else cw_v
        play = {
            "side": s["side"], "strike": s["strike"], "expiry": s["expiry"],
            "body": (f"The {s['strike']} strike holds {_fmt_cell(wall_v)} of "
                     f"{noun} gamma - the single biggest {noun} pile on the "
                     f"map - and the biggest {noun} concentration expires "
                     f"{s['expiry']}."),
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

    # The single standout cell per level: biggest expiry concentration in the
    # bullish strike's row (blue) and in the bearish strike's row (yellow).
    blue_ij = yellow_ij = None
    blue_exp = bear_exp = ""
    if bull is not None and bull in strikes:
        bi = strikes.index(bull)
        bj = max(range(len(expiries)), key=lambda j: matrix[bi][j])
        blue_ij = (bi, bj)
        blue_exp = expiries[bj]["label"]
    if bear is not None and bear in strikes:
        bi = strikes.index(bear)
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
        f"<div class='sub'>Chain as of {html_mod.escape(d['as_of_ts'])} (CBOE delayed ~15 min)</div></div>"
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
