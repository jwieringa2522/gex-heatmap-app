#!/usr/bin/env python3
"""Matplotlib renderer for the GEX heatmap - same design as the HTML version,
but fast and light (no headless browser). Produces a 16:9 PNG, 2560x1440,
table stretched to fill the frame.
"""
import math

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from gex_heatmap import _fmt_cell

BG = (0x0b / 255, 0x0e / 255, 0x13 / 255)
PANEL = (0x14 / 255, 0x1a / 255, 0x23 / 255)
ZERO = (0x12 / 255, 0x16 / 255, 0x1d / 255)
GREEN = (34 / 255, 197 / 255, 94 / 255)
RED = (239 / 255, 68 / 255, 68 / 255)
BLUE = (0x2f / 255, 0x6f / 255, 0xed / 255)
YELLOW = (0xff / 255, 0xd6 / 255, 0x0a / 255)
WHITE = (1, 1, 1)
GRAY = (0x8b / 255, 0x94 / 255, 0xa3 / 255)
DIMGRAY = (0x5b / 255, 0x64 / 255, 0x72 / 255)
SPOTBG = (0x1d / 255, 0x2b / 255, 0x1f / 255)
SPOTFG = (0x4a / 255, 0xde / 255, 0x80 / 255)


def _cell_color(v, vmax):
    if v == 0:
        return ZERO, DIMGRAY
    inten = min(1.0, 0.18 + 0.62 * math.sqrt(abs(v) / vmax))
    base = GREEN if v > 0 else RED
    return (base[0], base[1], base[2], inten), WHITE


def render(d, png_path):
    ticker, spot = d["ticker"], d["spot"]
    strikes, expiries = d["strikes"], d["expiries"]
    matrix = d["matrix"]
    bull, bear = d["bull_strike"], d["bear_strike"]
    cw, pw = d["call_wall"], d["put_wall"]

    n_rows = len(strikes) + 2          # header + strikes + total
    n_cols = len(expiries) + 2        # strike + expiries + total
    _all = [abs(v) for row in matrix for v in row]
    _all += [abs(v) for v in d["totals"]] + [abs(v) for v in d["col_totals"]]
    _all.append(abs(sum(d["col_totals"])))
    vmax = max(_all, default=0) or 1.0   # include row/col totals: they can
                                         # exceed any single cell (SPY did)
    spot_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))

    # standout cells: biggest expiry concentration in bull/bear rows
    blue_ij = yellow_ij = None
    if bull is not None and bull in strikes:
        bi = strikes.index(bull)
        blue_ij = (bi, max(range(len(expiries)), key=lambda j: matrix[bi][j]))
    if bear is not None and bear in strikes:
        bi = strikes.index(bear)
        yellow_ij = (bi, min(range(len(expiries)), key=lambda j: matrix[bi][j]))

    fig = plt.figure(figsize=(12.8, 7.2), dpi=200)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # ---- header ----
    ax.text(0.985, 0.965, f"Chain as of {d['as_of_ts']} (CBOE delayed ~15 min)",
            color=GRAY, fontsize=8.5, va="top", ha="right")

    renderer = fig.canvas.get_renderer()
    fig_w_px = fig.dpi * fig.get_figwidth()

    def _after(t):
        bb = t.get_window_extent(renderer=renderer)
        return bb.x1 / fig_w_px  # axes spans full figure width

    # price sits right after the title
    t0 = ax.text(0.015, 0.965, f"GEX Heatmap - ${ticker}  ",
                 color=WHITE, fontsize=15, weight=800, va="top", ha="left")
    ax.text(_after(t0) + 0.002, 0.962, f"${spot:,.2f}",
            color=GRAY, fontsize=11, va="top", ha="left")

    # ---- stats strip ----
    stats = [
        ("Call GEX ", _fmt_cell(d["tot_call"]), SPOTFG),
        ("Put GEX ", _fmt_cell(d["tot_put"]), (0xf8 / 255, 0x71 / 255, 0x71 / 255)),
        ("Net ", _fmt_cell(d["tot_call"] + d["tot_put"]), WHITE),
    ]
    if cw[0] is not None:
        stats.append(("Heaviest call wall ", f"{cw[0]:g}", (0x93 / 255, 0xc5 / 255, 0xfd / 255)))
    if pw[0] is not None:
        stats.append(("Heaviest put wall ", f"{pw[0]:g}", (0xfd / 255, 0xe6 / 255, 0x8a / 255)))
    x = 0.015
    for label, val, color in stats:
        tl = ax.text(x, 0.905, label, color=GRAY, fontsize=9.5, va="top", ha="left")
        xv = _after(tl)
        tv = ax.text(xv, 0.905, val, color=color, fontsize=10.5,
                     weight=700, va="top", ha="left")
        x = _after(tv) + 0.018

    # ---- table ----
    tx0, tx1 = 0.015, 0.985
    ty0, ty1 = 0.055, 0.875
    cwdt = (tx1 - tx0) / n_cols
    chgt = (ty1 - ty0) / n_rows

    def rect(cx, cy, color, alpha=1.0):
        if isinstance(color, tuple) and len(color) == 4:
            c, alpha = color[:3], color[3] * alpha
        else:
            c = color
        ax.add_patch(Rectangle((tx0 + cx * cwdt, ty1 - (cy + 1) * chgt),
                               cwdt, chgt, facecolor=c, alpha=alpha,
                               edgecolor=(0x1c / 255, 0x23 / 255, 0x2e / 255),
                               linewidth=0.6))

    def text(cx, cy, s, color=WHITE, size=8.5, weight=400, dx=0.5, dy=0.5):
        ax.text(tx0 + (cx + dx) * cwdt, ty1 - (cy + dy) * chgt, s,
                color=color, fontsize=size, weight=weight,
                va="center", ha="center")

    # header row
    rect(0, 0, PANEL)
    text(0, 0, "Strike", color=WHITE, weight=700, dx=0.08)
    # left-align strike header: use ha left via separate call
    for j, e in enumerate(expiries):
        rect(j + 1, 0, PANEL)
        text(j + 1, 0, f"{e['label']}\n{e['dte']}d", color=GRAY, size=8)
    rect(n_cols - 1, 0, PANEL)
    text(n_cols - 1, 0, "Total", color=GRAY, weight=600)

    # strike rows
    for i, s in enumerate(strikes):
        cy = i + 1
        # strike label cell
        if i == spot_i:
            bg, fg = SPOTBG, SPOTFG
        else:
            bg, fg = PANEL, WHITE
        # wall rows: tint the whole label cell + stacked tag (no edge bar,
        # no overlap - everything stays inside the box)
        tag = None
        if cw[0] is not None and s == cw[0]:
            tag = ("WALL", SPOTFG)
            if i != spot_i:
                bg = (0x1a / 255, 0x3a / 255, 0x28 / 255)
        elif pw[0] is not None and s == pw[0]:
            tag = ("WALL", (0xf8 / 255, 0x71 / 255, 0x71 / 255))
            if i != spot_i:
                bg = (0x3d / 255, 0x1c / 255, 0x1c / 255)
        rect(0, cy, bg)
        sx = tx0 + 0.03 * cwdt
        if tag:
            # wall rows: number on top, "WALL" below - sized to fit the row
            ax.text(sx, ty1 - (cy + 0.30) * chgt, f"{s:g}", color=fg,
                    fontsize=9, weight=700, va="center", ha="left")
            ax.text(sx, ty1 - (cy + 0.74) * chgt, tag[0], color=tag[1],
                    fontsize=6.5, weight=800, va="center", ha="left")
        else:
            ax.text(sx, ty1 - (cy + 0.5) * chgt, f"{s:g}", color=fg,
                    fontsize=9, weight=700, va="center", ha="left")
        # data cells
        for j, v in enumerate(matrix[i]):
            if (i, j) == blue_ij:
                rect(j + 1, cy, BLUE)
                text(j + 1, cy, _fmt_cell(v), weight=800)
            elif (i, j) == yellow_ij:
                rect(j + 1, cy, YELLOW)
                text(j + 1, cy, _fmt_cell(v), color=(0.1, 0.1, 0.1), weight=800)
            else:
                color, fg = _cell_color(v, vmax)
                rect(j + 1, cy, color)
                text(j + 1, cy, _fmt_cell(v), color=fg)
        # row total
        color, fg = _cell_color(d["totals"][i], vmax)
        rect(n_cols - 1, cy, color)
        text(n_cols - 1, cy, _fmt_cell(d["totals"][i]), color=fg, weight=700)

    # total row
    cy = n_rows - 1
    rect(0, cy, PANEL)
    ax.text(tx0 + 0.03 * cwdt, ty1 - (cy + 0.5) * chgt, "Total", color=WHITE,
            fontsize=9, weight=700, va="center", ha="left")
    for j, v in enumerate(d["col_totals"]):
        color, fg = _cell_color(v, vmax)
        rect(j + 1, cy, color)
        text(j + 1, cy, _fmt_cell(v), color=fg, weight=700)
    grand = sum(d["col_totals"])
    color, fg = _cell_color(grand, vmax)
    rect(n_cols - 1, cy, color)
    text(n_cols - 1, cy, _fmt_cell(grand), color=fg, weight=700)

    # ---- legend ----
    ax.text(0.015, 0.028,
            "Green = positive (call) gamma exposure  \u00b7  "
            "Red = negative (put) gamma exposure  \u00b7  "
            "Blue = bullish level  \u00b7  Yellow = bearish level  \u00b7  "
            "Top 18 strikes by gamma shown",
            color=GRAY, fontsize=8.5, va="top", ha="left")

    fig.savefig(png_path, dpi=200, facecolor=BG)
    plt.close(fig)
    return png_path
