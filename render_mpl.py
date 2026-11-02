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
TEAL = (0x30 / 255, 0x85 / 255, 0x7a / 255)          # brand accent #30857a
TEALLT = (0x4f / 255, 0xc3 / 255, 0xb0 / 255)        # light tint for text


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
    cw, pw = d["call_wall"], d["put_wall"]

    n_rows = len(strikes) + 2          # header + strikes + total
    n_cols = len(expiries) + 2        # strike + expiries + total
    _all = [abs(v) for row in matrix for v in row]
    _all += [abs(v) for v in d["totals"]] + [abs(v) for v in d["col_totals"]]
    _all.append(abs(sum(d["col_totals"])))
    vmax = max(_all, default=0) or 1.0   # include row/col totals: they can
                                         # exceed any single cell (SPY did)
    spot_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))

    # standout cells: biggest expiry concentration inside each WALL
    # row, so the blue cell always sits in the CALL WALL row and the
    # yellow cell always sits in the PUT WALL row, on every ticker.
    blue_ij = yellow_ij = None
    if cw[0] is not None and cw[0] in strikes:
        bi = strikes.index(cw[0])
        blue_ij = (bi, max(range(len(expiries)), key=lambda j: matrix[bi][j]))
    if pw[0] is not None and pw[0] in strikes:
        bi = strikes.index(pw[0])
        yellow_ij = (bi, min(range(len(expiries)), key=lambda j: matrix[bi][j]))

    # biggest single strike x expiry cell, for the white outline -
    # only when that strike is actually on the displayed map
    big_ij = None
    _big = d.get("biggest_cell") or {}
    if _big.get("strike") in strikes:
        _bj = next((j for j, e in enumerate(expiries)
                    if e["date"] == _big.get("expiry")), None)
        if _bj is not None:
            big_ij = (strikes.index(_big["strike"]), _bj)

    fig = plt.figure(figsize=(12.8, 7.2), dpi=200)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # ---- header ----
    ax.text(0.985, 0.965, f"Chain as of {d['as_of_ts']} (CBOE - may be delayed)",
            color=GRAY, fontsize=8.5, va="top", ha="right")

    renderer = fig.canvas.get_renderer()
    fig_w_px = fig.dpi * fig.get_figwidth()
    fig_h_px = fig.dpi * fig.get_figheight()

    def _after(t):
        bb = t.get_window_extent(renderer=renderer)
        return bb.x1 / fig_w_px  # axes spans full figure width

    # price sits right after the title
    t0 = ax.text(0.015, 0.965, f"GEX Heatmap - {ticker}  ",
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
    if d.get("gamma_flip") is not None:
        stats.append(("Gamma flip ", f"${d['gamma_flip']:,.2f}", TEALLT))
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
    # strike-label column: original width, widened only as much as the
    # "CALL WALL"/"PUT WALL" tag beside the number strictly needs (0.082
    # axes ~ 210px fits number + gap + tag with margin)
    lab_w = max(cwdt, 0.082)
    data_w = (tx1 - tx0 - lab_w) / (n_cols - 1)

    def colx(cx):
        if cx <= 0:
            return tx0
        return tx0 + lab_w + (cx - 1) * data_w

    def colw(cx):
        return lab_w if cx == 0 else data_w

    def rect(cx, cy, color, alpha=1.0):
        if isinstance(color, tuple) and len(color) == 4:
            c, alpha = color[:3], color[3] * alpha
        else:
            c = color
        ax.add_patch(Rectangle((colx(cx), ty1 - (cy + 1) * chgt),
                               colw(cx), chgt, facecolor=c, alpha=alpha,
                               edgecolor=(0x1c / 255, 0x23 / 255, 0x2e / 255),
                               linewidth=0.6))

    def text(cx, cy, s, color=WHITE, size=8.5, weight=400, dx=0.5, dy=0.5):
        ax.text(colx(cx) + dx * colw(cx), ty1 - (cy + dy) * chgt, s,
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

    # measured label-column boxes the flip tag must never cover:
    # every strike number and wall tag, plus the header and total
    # row bands of the label column itself
    label_obstacles = [(tx0, ty1 - chgt, tx0 + lab_w, ty1),
                       (tx0, ty0, tx0 + lab_w, ty0 + chgt)]
    num_right = {}         # strike row index -> number's right edge

    # strike rows
    for i, s in enumerate(strikes):
        cy = i + 1
        # strike label cell: numbers always white; spot cell is white w/ dark
        # number so it pops; wall cells get a standout tint
        tag = None
        if cw[0] is not None and s == cw[0]:
            tag = "CALL WALL"
            bg, fg = (0x16 / 255, 0x5c / 255, 0x33 / 255), WHITE
        elif pw[0] is not None and s == pw[0]:
            tag = "PUT WALL"
            bg, fg = (0x6b / 255, 0x22 / 255, 0x22 / 255), WHITE
        elif i == spot_i:
            bg, fg = (0xf2 / 255, 0xf2 / 255, 0xf2 / 255), (0x11 / 255,) * 3
        else:
            bg, fg = PANEL, WHITE
        rect(0, cy, bg)
        sx = tx0 + 0.04 * lab_w
        tnum = ax.text(sx, ty1 - (cy + 0.5) * chgt, f"{s:g}", color=fg,
                       fontsize=9, weight=700, va="center", ha="left")
        _nb = tnum.get_window_extent(renderer=renderer)
        num_right[i] = _nb.x1 / fig_w_px
        label_obstacles.append((_nb.x0 / fig_w_px, _nb.y0 / fig_h_px,
                                _nb.x1 / fig_w_px, _nb.y1 / fig_h_px))
        if tag:
            # tag sits right of the number in white; measured so they never
            # overlap and always stay inside the label cell
            twall = ax.text(num_right[i] + 0.006, ty1 - (cy + 0.5) * chgt,
                            tag, color=WHITE, fontsize=7, weight=800,
                            va="center", ha="left")
            _tb = twall.get_window_extent(renderer=renderer)
            label_obstacles.append((_tb.x0 / fig_w_px, _tb.y0 / fig_h_px,
                                    _tb.x1 / fig_w_px, _tb.y1 / fig_h_px))
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
    ax.text(tx0 + 0.04 * lab_w, ty1 - (cy + 0.5) * chgt, "Total", color=WHITE,
            fontsize=9, weight=700, va="center", ha="left")
    for j, v in enumerate(d["col_totals"]):
        color, fg = _cell_color(v, vmax)
        rect(j + 1, cy, color)
        text(j + 1, cy, _fmt_cell(v), color=fg, weight=700)
    grand = sum(d["col_totals"])
    color, fg = _cell_color(grand, vmax)
    rect(n_cols - 1, cy, color)
    text(n_cols - 1, cy, _fmt_cell(grand), color=fg, weight=700)

    # Gamma flip tag: no line is drawn across the table (Justin's
    # order, Nov 2). When a flip exists inside the displayed strike
    # range, a small "FLIP" tag marks it in the strike-label column
    # of the nearest strike row, beside the strike number - the same
    # open area the CALL WALL / PUT WALL tags use. The tag is kept
    # only if its measured box stays inside the label column and
    # clears every strike number and wall tag; otherwise no tag is
    # drawn and the stats strip value still discloses the level.
    flip_tag_drawn = False
    flip = d.get("gamma_flip")
    if flip is not None and strikes and strikes[-1] <= flip <= strikes[0]:
        _fi = min(range(len(strikes)),
                  key=lambda i: abs(strikes[i] - flip))
        tflip = ax.text(num_right[_fi] + 0.006,
                        ty1 - (_fi + 1.5) * chgt, "FLIP",
                        color=TEALLT, fontsize=7, weight=800,
                        va="center", ha="left")
        _fb = tflip.get_window_extent(renderer=renderer)
        _fx0, _fx1 = _fb.x0 / fig_w_px, _fb.x1 / fig_w_px
        _fy0, _fy1 = _fb.y0 / fig_h_px, _fb.y1 / fig_h_px
        _clear = _fx1 <= tx0 + lab_w and all(
            _fx1 <= ox0 or ox1 <= _fx0 or
            _fy1 <= oy0 or oy1 <= _fy0
            for ox0, oy0, ox1, oy1 in label_obstacles)
        if _clear:
            flip_tag_drawn = True
        else:
            tflip.remove()

    # White outline on the biggest single strike x expiry cell -
    # drawn on top of any standout fill it may coincide with.
    if big_ij is not None:
        _bi, _bj = big_ij
        ax.add_patch(Rectangle((colx(_bj + 1), ty1 - (_bi + 2) * chgt),
                               data_w, chgt, facecolor="none",
                               edgecolor=WHITE, linewidth=2.0, zorder=6))

    # ---- legend ----
    _leg2 = []
    if big_ij is not None:
        _bl = next(e["label"] for e in expiries
                   if e["date"] == _big["expiry"])
        # dollars escaped: two bare $ in one matplotlib string would
        # be parsed as a mathtext pair and eat the text between them
        _val = _fmt_cell(_big["value"]).replace("$", r"\$")
        _leg2.append(f"White outline: biggest single pile, "
                     f"{_val} at the "
                     f"\\${_big['strike']:g} strike / {_bl} expiry")
    if flip_tag_drawn:
        _leg2.append("Teal FLIP tag: gamma flip")
    ax.text(0.015, 0.048 if _leg2 else 0.028,
            "Green = positive (call) gamma exposure  \u00b7  "
            "Red = negative (put) gamma exposure  \u00b7  "
            "Blue = bullish level  \u00b7  Yellow = bearish level  \u00b7  "
            "Top 18 strikes by gamma shown",
            color=GRAY, fontsize=8.5, va="top", ha="left")
    if _leg2:
        ax.text(0.015, 0.0245, "  \u00b7  ".join(_leg2),
                color=GRAY, fontsize=8.5, va="top", ha="left")

    fig.savefig(png_path, dpi=200, facecolor=BG)
    plt.close(fig)
    return png_path
