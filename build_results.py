#!/usr/bin/env python3
"""
build_results.py

Builds a static, read-only, public results page from Freqtrade trade databases.
Standard library only (Python 3.8+). No network access, no trackers, no secrets.

What it reads : the `trades` table of each bot's sqlite file (a private copy is
                made first, so the live bot is never touched).
What it writes: <out>/index.html, <out>/backtest.html (from backtest_data.json), <out>/results.json, <out>/trades_<bot>.csv

Usage:
    python3 build_results.py --config results_config.json --out site
"""
import argparse
import csv
import html
import json
import math
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DISCLAIMER = (
    "Paper trading only. No real money is used. Simulated fills ignore slippage, "
    "latency and liquidity limits, so live results would be worse. This is research "
    "shared for feedback, not investment advice. Do not copy these trades."
)

# Two-sided 95% t critical values by degrees of freedom (small samples need wider intervals).
T_CRIT = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
    9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074,
    23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045,
    30: 2.042,
}


def t_crit(df):
    if df in T_CRIT:
        return T_CRIT[df]
    return 2.0 if df <= 60 else 1.96


WANTED_COLUMNS = [
    "id", "pair", "is_short", "leverage", "open_date", "close_date", "close_profit",
    "close_profit_abs", "stake_amount", "exit_reason", "initial_stop_loss_pct", "is_open",
]


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------

def parse_dt(value):
    """Freqtrade stores naive UTC datetimes as text. Return a naive UTC datetime or None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(v, tz=timezone.utc).replace(tzinfo=None)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def load_trades(db_path):
    """Return (closed_trades, open_count). Reads from a private snapshot copy."""
    src = Path(db_path).expanduser()
    if not src.is_file():
        raise FileNotFoundError("database file not found")
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "snap.sqlite"
        shutil.copy2(src, dst)
        for suffix in ("-wal", "-shm"):  # include uncheckpointed writes if the bot uses WAL
            extra = Path(str(src) + suffix)
            if extra.is_file():
                shutil.copy2(extra, Path(str(dst) + suffix))
        con = sqlite3.connect(str(dst))
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
            if not cols:
                raise RuntimeError("no 'trades' table in this database")
            use = [c for c in WANTED_COLUMNS if c in cols]
            rows = con.execute("SELECT " + ", ".join(use) + " FROM trades").fetchall()
        finally:
            con.close()

    closed, open_count = [], 0
    for row in rows:
        d = dict(zip(use, row))
        if d.get("is_open"):
            open_count += 1
            continue
        close = parse_dt(d.get("close_date"))
        if close is None or d.get("close_profit") is None:
            continue
        profit = float(d["close_profit"])
        stake = float(d.get("stake_amount") or 0.0)
        pabs = d.get("close_profit_abs")
        pabs = float(pabs) if pabs is not None else profit * stake
        sp = d.get("initial_stop_loss_pct")
        closed.append({
            "id": d.get("id") or 0,
            "pair": str(d.get("pair") or "?"),
            "short": bool(d.get("is_short")),
            "lev": float(d.get("leverage") or 1.0),
            "open": parse_dt(d.get("open_date")),
            "close": close,
            "profit": profit,
            "pabs": pabs,
            "reason": str(d.get("exit_reason") or "unknown"),
            "stop": float(sp) if sp not in (None, 0, 0.0) else None,
        })
    closed.sort(key=lambda t: (t["close"], t["id"]))
    return closed, open_count


# ----------------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------------

def compute_stats(trades, start_balance, min_trades):
    n = len(trades)
    s = {"n": n, "min_trades": min_trades}
    if n == 0:
        s["verdict"] = ("none", "No closed trades yet")
        return s

    rets = [t["profit"] for t in trades]
    wins = [t for t in trades if t["profit"] > 0]
    losses = [t for t in trades if t["profit"] <= 0]
    s["wins"] = len(wins)
    s["win_rate"] = len(wins) / n
    s["avg_win"] = statistics.fmean(t["profit"] for t in wins) if wins else None
    s["avg_loss"] = statistics.fmean(t["profit"] for t in losses) if losses else None

    gross_win = sum(t["pabs"] for t in trades if t["pabs"] > 0)
    gross_loss = -sum(t["pabs"] for t in trades if t["pabs"] < 0)
    s["profit_factor"] = (gross_win / gross_loss) if gross_loss > 0 else None
    s["total_pnl"] = sum(t["pabs"] for t in trades)
    s["start_balance"] = start_balance
    s["total_pct"] = (s["total_pnl"] / start_balance) if start_balance > 0 else None

    mean_ret = statistics.fmean(rets)
    s["mean_ret"] = mean_ret
    if n >= 2:
        sd = statistics.stdev(rets)
        margin = t_crit(n - 1) * sd / math.sqrt(n)
        s["ci_lo"], s["ci_hi"] = mean_ret - margin, mean_ret + margin
    else:
        s["ci_lo"] = s["ci_hi"] = None

    # R multiples: profit divided by the initial stop distance (both are ratios on stake).
    r_vals = [t["profit"] / abs(t["stop"]) for t in trades if t["stop"]]
    s["r_coverage"] = len(r_vals) / n
    s["mean_r"] = statistics.fmean(r_vals) if r_vals and len(r_vals) / n >= 0.8 else None

    # Equity curve and drawdown (closed trades only, in close order).
    balance = start_balance if start_balance > 0 else 0.0
    peak = balance
    first = trades[0]["open"] or trades[0]["close"]
    equity = [(first, balance)]
    dd_series = [(first, 0.0)]
    max_dd = 0.0
    for t in trades:
        balance += t["pabs"]
        equity.append((t["close"], balance))
        peak = max(peak, balance)
        dd = (balance / peak - 1.0) if peak > 0 else 0.0
        dd_series.append((t["close"], dd))
        max_dd = min(max_dd, dd)
    s["equity"], s["drawdown"], s["max_dd"] = equity, dd_series, max_dd
    s["end_balance"] = balance

    # Longest losing streak.
    longest = run = 0
    for t in trades:
        run = run + 1 if t["profit"] <= 0 else 0
        longest = max(longest, run)
    s["max_loss_streak"] = longest

    holds = [(t["close"] - t["open"]).total_seconds() / 3600.0 for t in trades if t["open"]]
    s["avg_hold_h"] = statistics.fmean(holds) if holds else None
    s["best"] = max(rets)
    s["worst"] = min(rets)
    s["first_close"], s["last_close"] = trades[0]["close"], trades[-1]["close"]

    months = OrderedDict()
    for t in trades:
        m = t["close"].strftime("%Y-%m")
        row = months.setdefault(m, {"n": 0, "wins": 0, "pnl": 0.0})
        row["n"] += 1
        row["wins"] += 1 if t["profit"] > 0 else 0
        row["pnl"] += t["pabs"]
    s["months"] = months

    reasons = {}
    for t in trades:
        row = reasons.setdefault(t["reason"], {"n": 0, "wins": 0, "pnl": 0.0})
        row["n"] += 1
        row["wins"] += 1 if t["profit"] > 0 else 0
        row["pnl"] += t["pabs"]
    s["reasons"] = sorted(reasons.items(), key=lambda kv: -kv[1]["n"])

    s["clusters"] = find_clusters(trades, s["total_pnl"])
    s["verdict"] = verdict(s)
    return s


def find_clusters(trades, total_pnl, min_size=4, top=2):
    """Groups of trades that closed within the same clock hour. One market move or one
    manual action can close many trades at once, so these are not independent results."""
    groups = {}
    for t in trades:
        key = t["close"].strftime("%Y-%m-%d %H:00")
        groups.setdefault(key, []).append(t)
    found = []
    for key, g in groups.items():
        if len(g) >= min_size:
            pnl = sum(t["pabs"] for t in g)
            reasons = Counter(t["reason"] for t in g)
            found.append({"hour": key, "n": len(g), "pnl": pnl,
                          "reason": reasons.most_common(1)[0][0],
                          "rest_n": len(trades) - len(g),
                          "rest_pnl": total_pnl - pnl})
    found.sort(key=lambda c: -abs(c["pnl"]))
    return found[:top]


def verdict(s):
    """Plain-language label. Never calls a result good unless the statistics support it."""
    n, need = s["n"], s["min_trades"]
    if n < need:
        return ("early", "Too early to judge (%d of %d trades)" % (n, need))
    lo, hi = s.get("ci_lo"), s.get("ci_hi")
    if lo is None:
        return ("early", "Too early to judge")
    if hi < 0:
        return ("bad", "Losing, and not just bad luck")
    if lo > 0:
        return ("good", "Positive so far (still paper money)")
    return ("mixed", "Inconclusive: cannot separate skill from luck yet")


# ----------------------------------------------------------------------------
# Formatting and charts
# ----------------------------------------------------------------------------

def esc(x):
    return html.escape(str(x), quote=True)


def pct(x, d=2, sign=True):
    if x is None:
        return "n/a"
    return ("%+.*f%%" if sign else "%.*f%%") % (d, x * 100)


def num(x, d=2, sign=False):
    if x is None:
        return "n/a"
    return ("%+.*f" if sign else "%.*f") % (d, x)


def fmt_hours(h):
    if h is None:
        return "n/a"
    if h < 1:
        return "%d min" % round(h * 60)
    if h < 48:
        return "%.1f h" % h
    return "%.1f days" % (h / 24)


def day(d):
    return d.strftime("%d %b %Y") if d else "n/a"


def svg_chart(points, kind, baseline=None, width=420, height=210):
    """Inline SVG line/area chart. kind is 'equity' or 'drawdown'."""
    if len(points) < 2:
        return '<p class="muted">Not enough trades to draw this chart yet.</p>'
    if len(points) > 400:  # keep file small; always keep the last point
        step = math.ceil(len(points) / 400)
        points = points[::step] + ([points[-1]] if (len(points) - 1) % step else [])
    pl, pr, pt, pb = 50, 10, 10, 26
    w, h = width - pl - pr, height - pt - pb
    vals = [p[1] for p in points]
    lo, hi = min(vals), max(vals)
    if baseline is not None:
        lo, hi = min(lo, baseline), max(hi, baseline)
    if kind == "drawdown":
        hi = 0.0
        lo = min(lo, -0.01)
    if hi - lo < 1e-9:
        hi, lo = hi + 1, lo - 1
    pad = (hi - lo) * 0.06
    lo_p, hi_p = lo - (pad if kind == "equity" else pad), hi + (pad if kind == "equity" else 0)
    t0, t1 = points[0][0], points[-1][0]
    span = (t1 - t0).total_seconds()

    def X(i, t):
        frac = ((t - t0).total_seconds() / span) if span > 0 else i / (len(points) - 1)
        return pl + frac * w

    def Y(v):
        return pt + (hi_p - v) / (hi_p - lo_p) * h

    path = " ".join(
        ("M" if i == 0 else "L") + "%.1f,%.1f" % (X(i, t), Y(v)) for i, (t, v) in enumerate(points)
    )
    out = ['<svg viewBox="0 0 %d %d" role="img" class="chart" xmlns="http://www.w3.org/2000/svg">' % (width, height)]
    for v in (lo_p + (hi_p - lo_p) * f for f in (0.0, 0.5, 1.0)):
        y = Y(v)
        label = pct(v, 1, sign=False) if kind == "drawdown" else "%.0f" % v
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (pl, pl + w, y, y))
        out.append('<text x="%d" y="%.1f" class="axis" text-anchor="end">%s</text>' % (pl - 6, y + 3, esc(label)))
    if baseline is not None:
        yb = Y(baseline)
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="base"/>' % (pl, pl + w, yb, yb))
    if kind == "drawdown":
        area = path + " L%.1f,%.1f L%.1f,%.1f Z" % (X(len(points) - 1, t1), Y(0), X(0, t0), Y(0))
        out.append('<path d="%s" class="area-bad"/>' % area)
    out.append('<path d="%s" class="%s"/>' % (path, "line-bad" if kind == "drawdown" else "line"))
    out.append('<text x="%d" y="%d" class="axis" text-anchor="start">%s</text>' % (pl, height - 6, esc(day(t0))))
    out.append('<text x="%d" y="%d" class="axis" text-anchor="end">%s</text>' % (pl + w, height - 6, esc(day(t1))))
    out.append("</svg>")
    return "".join(out)


CSS = """
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#6b6560;--card:#fff;--line:#e7e5e4;--accent:#2563eb;
--good:#15803d;--bad:#b91c1c;--warn:#a16207;--soft:#f5f5f4}
@media(prefers-color-scheme:dark){:root{--bg:#0c0a09;--fg:#e7e5e4;--muted:#a8a29e;--card:#1c1917;
--line:#292524;--accent:#60a5fa;--good:#4ade80;--bad:#f87171;--warn:#fbbf24;--soft:#231f1d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:1.7rem;margin:.2em 0}h2{font-size:1.3rem;margin:1.6em 0 .5em}h3{font-size:1.05rem;margin:1.2em 0 .4em}
.muted{color:var(--muted)}.small{font-size:.85rem}
.banner{border:1px solid var(--line);border-left:4px solid var(--warn);background:var(--card);padding:10px 14px;border-radius:8px;margin:12px 0}
.banner.demo{border-left-color:var(--bad);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:14px 0}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.92rem}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:.8rem;text-transform:uppercase;letter-spacing:.03em}
td.r,th.r{text-align:right}td.wrap{white-space:normal;min-width:170px}
.badge{display:inline-block;padding:1px 9px;border-radius:99px;font-size:.78rem;border:1px solid var(--line);background:var(--soft)}
.v-good{color:var(--good)}.v-bad{color:var(--bad)}.v-early,.v-mixed,.v-none{color:var(--warn)}
.grid-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:10px 0}
.stat{background:var(--soft);border-radius:8px;padding:8px 10px}
.stat .l{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.stat .v{font-size:1.15rem;font-weight:650}.stat .h{font-size:.75rem;color:var(--muted)}
svg.chart{width:100%;height:auto;display:block}
.grid{stroke:var(--line);stroke-width:1}.base{stroke:var(--muted);stroke-width:1;stroke-dasharray:4 3}
.axis{fill:var(--muted);font-size:13px}.line{fill:none;stroke:var(--accent);stroke-width:2}
.line-bad{fill:none;stroke:var(--bad);stroke-width:1.5}.area-bad{fill:var(--bad);opacity:.15}
details{margin:8px 0}summary{cursor:pointer;font-weight:600}
a{color:var(--accent)}footer{margin-top:40px;font-size:.85rem;color:var(--muted)}
"""

GLOSSARY = [
    ("Closed trades", "Only finished trades are counted. Open positions are left out until they close."),
    ("Win rate", "Share of trades that ended in profit. A high win rate alone means little: many small wins can be wiped out by a few big losses."),
    ("Expectancy", "Average result per trade. The range next to it is a 95% interval. If the range includes zero, the data cannot yet separate skill from luck."),
    ("R multiple", "Profit measured in units of the risk taken. +2R means the trade made twice what it risked. Here R is derived from each trade's initial stop distance and is shown only when most trades have one."),
    ("Profit factor", "Total money won divided by total money lost. Above 1 means profit, below 1 means loss."),
    ("Max drawdown", "Biggest fall from a previous high point of the account. It shows how bad the worst stretch felt."),
    ("Exit reasons", "Shown as the bot logs them. stop_loss, target and time_stop are the strategy's own rules. force_exit means the trading desk's own safety rule closed the position (for example when Bitcoin falls below a set level), not the strategy's stop or target. It acts on every open trade at once."),
    ("Clustering", "Trades that close in the same hour usually come from one market move or one action. They are shown as a group, because ten trades closing together are closer to one bet than ten."),
    ("Sample size", "With few trades, luck dominates. Bots below the trade threshold are flagged as too early to judge."),
]


def stat_card(label, value, hint=""):
    h = '<div class="h">%s</div>' % esc(hint) if hint else ""
    return '<div class="stat"><div class="l">%s</div><div class="v">%s</div>%s</div>' % (esc(label), esc(value), h)


def bot_section(bot, s, open_count, trades, cfg):
    bid = bot["_id"]
    out = ['<section id="%s" class="card">' % esc(bid)]
    out.append("<h2>%s <span class='badge'>%s</span></h2>" % (esc(bot.get("name", bid)), esc(bot.get("status", "active"))))
    if bot.get("description"):
        out.append("<p>%s</p>" % esc(bot["description"]))
    if bot.get("since"):
        out.append('<p class="small muted">Counting only trades opened on or after %s, when the current rules started.</p>' % esc(bot["since"]))
    if not s or s.get("n", 0) == 0:
        out.append('<p class="muted">No closed trades yet.</p></section>')
        return "".join(out)

    kind, text = s["verdict"]
    out.append('<p class="v-%s"><strong>%s</strong></p>' % (esc(kind), esc(text)))
    show_abs = cfg.get("show_absolute_usdt", True)
    ci = ""
    if s.get("ci_lo") is not None:
        ci = "95%% range %s to %s" % (pct(s["ci_lo"]), pct(s["ci_hi"]))
    cards = [
        stat_card("Closed trades", str(s["n"]), "%d still open" % open_count),
        stat_card("Win rate", pct(s["win_rate"], 1, sign=False), "%d wins" % s["wins"]),
        stat_card("Avg win / avg loss", "%s / %s" % (pct(s["avg_win"]), pct(s["avg_loss"])), "on stake"),
        stat_card("Expectancy per trade", pct(s["mean_ret"]), ci),
        stat_card("Expectancy in R", num(s["mean_r"], 2, sign=True) + " R" if s["mean_r"] is not None else "n/a",
                  "derived from stops" if s["mean_r"] is not None else "stop data incomplete"),
        stat_card("Profit factor", num(s["profit_factor"], 2) if s["profit_factor"] is not None else "n/a"),
        stat_card("Total result", pct(s["total_pct"]) if s["total_pct"] is not None else "n/a",
                  ("%s USDT on %.0f start" % (num(s["total_pnl"], 2, sign=True), s["start_balance"])) if show_abs else "of start balance"),
        stat_card("Max drawdown", pct(s["max_dd"], 1, sign=False), "closed-trade basis"),
        stat_card("Longest losing run", "%d trades" % s["max_loss_streak"]),
        stat_card("Avg hold time", fmt_hours(s["avg_hold_h"])),
        stat_card("Best / worst trade", "%s / %s" % (pct(s["best"], 1), pct(s["worst"], 1))),
        stat_card("Period", "%s to %s" % (day(s["first_close"]), day(s["last_close"]))),
    ]
    out.append('<div class="grid-stats">%s</div>' % "".join(cards))
    for c in s.get("clusters") or []:
        out.append('<p class="banner"><strong>Clustering check.</strong> %d trades closed within the same hour (%s UTC, exit: %s) '
                   'and together made %s USDT. Without this group the bot has %d trades and %s USDT in total. '
                   'Trades that close together come from one market move or one action, so they count as closer to one bet than %d.</p>' % (
                       c["n"], esc(c["hour"]), esc(c["reason"]), num(c["pnl"], 2, sign=True), c["rest_n"],
                       num(c["rest_pnl"], 2, sign=True), c["n"]))

    out.append("<h3>Account value (closed trades)</h3>")
    out.append(svg_chart(s["equity"], "equity", baseline=s["start_balance"] if s["start_balance"] > 0 else None))
    out.append("<h3>Drawdown from peak</h3>")
    out.append(svg_chart(s["drawdown"], "drawdown"))

    rows = "".join(
        "<tr><td>%s</td><td class='r'>%d</td><td class='r'>%s</td><td class='r'>%s</td></tr>" % (
            esc(m), r["n"], pct(r["wins"] / r["n"], 0, sign=False),
            num(r["pnl"], 2, sign=True) if show_abs else "")
        for m, r in s["months"].items())
    out.append("<h3>By month</h3><div class='scroll'><table><tr><th>Month</th><th class='r'>Trades</th>"
               "<th class='r'>Win rate</th><th class='r'>Result (USDT)</th></tr>%s</table></div>" % rows)

    rows = "".join(
        "<tr><td>%s</td><td class='r'>%d</td><td class='r'>%s</td><td class='r'>%s</td></tr>" % (
            esc(k), r["n"], pct(r["wins"] / r["n"], 0, sign=False),
            num(r["pnl"], 2, sign=True) if show_abs else "")
        for k, r in s["reasons"])
    out.append("<h3>How trades ended</h3><div class='scroll'><table><tr><th>Exit reason</th><th class='r'>Trades</th>"
               "<th class='r'>Win rate</th><th class='r'>Result (USDT)</th></tr>%s</table></div>" % rows)

    recent = trades[-25:][::-1]
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class='r %s'>%s</td><td>%s</td></tr>" % (
            esc(t["pair"]), "short" if t["short"] else "long", esc(day(t["close"])),
            esc(fmt_hours((t["close"] - t["open"]).total_seconds() / 3600.0) if t["open"] else "n/a"),
            "v-good" if t["profit"] > 0 else "v-bad", esc(pct(t["profit"])), esc(t["reason"]))
        for t in recent)
    out.append("<h3>Latest %d closed trades</h3><div class='scroll'><table><tr><th>Pair</th><th>Side</th><th>Closed</th>"
               "<th>Held</th><th class='r'>Result</th><th>Exit</th></tr>%s</table></div>" % (len(recent), rows))
    if cfg.get("export_csv", True):
        out.append('<p class="small"><a href="trades_%s.csv">Download all closed trades (CSV)</a></p>' % esc(bid))
    out.append("</section>")
    return "".join(out)


def build_page(cfg, bots_data, generated):
    title = cfg.get("site_title", "Bot results")
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             '<title>%s</title><meta name="robots" content="index,follow"><style>%s</style></head><body><main>' % (esc(title), CSS)]
    parts.append("<h1>%s</h1>" % esc(title))
    if cfg.get("tagline"):
        parts.append('<p class="muted">%s</p>' % esc(cfg["tagline"]))
    if cfg.get("_has_backtest"):
        parts.append('<p><a href="backtest.html"><strong>Backtest research: 3 years, 124 coins</strong></a> '
                     '<span class="muted small">(what the rules did on past data, and everything that failed)</span></p>')
    if cfg.get("demo"):
        parts.append('<div class="banner demo">DEMO DATA: randomly generated test numbers. These are NOT real results. '
                     'This preview only shows the page layout.</div>')
    parts.append('<div class="banner">%s</div>' % esc(cfg.get("disclaimer", DEFAULT_DISCLAIMER)))

    parts.append("<details><summary>How to read these numbers</summary><dl>%s</dl></details>" % "".join(
        "<dt><strong>%s</strong></dt><dd>%s</dd>" % (esc(a), esc(b)) for a, b in GLOSSARY))

    # Summary table: listed in config order, never sorted by performance, failures included.
    rows = []
    for bot, s, open_count, trades, err in bots_data:
        bid = bot["_id"]
        name = "<a href='#%s'>%s</a> <span class='badge'>%s</span>" % (
            esc(bid), esc(bot.get("name", bid)), esc(bot.get("status", "active")))
        if err or not s or s.get("n", 0) == 0:
            rows.append("<tr><td class='wrap'>%s<div class='small muted'>%s</div></td><td class='r'>0</td>"
                        "<td class='r'>-</td><td class='r'>-</td><td class='r'>-</td></tr>" % (name, esc(err or "No closed trades yet")))
            continue
        kind, text = s["verdict"]
        rows.append(
            "<tr><td class='wrap'>%s<div class='small v-%s'>%s</div></td><td class='r'>%d</td><td class='r'>%s</td>"
            "<td class='r'>%s</td><td class='r'>%s</td></tr>" % (
                name, esc(kind), esc(text), s["n"], esc(pct(s["win_rate"], 0, sign=False)),
                esc(pct(s["mean_ret"])), esc(pct(s["max_dd"], 1, sign=False))))
    parts.append("<h2>All bots</h2><p class='muted small'>Every bot is listed, including the ones that failed. "
                 "Order follows the config file, not performance.</p><div class='card scroll'><table><tr><th>Bot and verdict</th>"
                 "<th class='r'>Trades</th><th class='r'>Win rate</th><th class='r'>Per trade</th><th class='r'>Max DD</th>"
                 "</tr>%s</table></div>" % "".join(rows))

    for bot, s, open_count, trades, err in bots_data:
        if err:
            parts.append('<section id="%s" class="card"><h2>%s</h2><p class="muted">%s</p></section>' % (
                esc(bot["_id"]), esc(bot.get("name", bot["_id"])), esc(err)))
        else:
            parts.append(bot_section(bot, s, open_count, trades, cfg))

    notes = cfg.get("notes") or []
    if notes:
        parts.append("<h2>Notes and known limits</h2><ul>%s</ul>" % "".join("<li>%s</li>" % esc(n) for n in notes))

    links = cfg.get("links") or {}
    link_html = " | ".join('<a href="%s" rel="noopener">%s</a>' % (esc(v), esc(k)) for k, v in links.items() if v)
    parts.append("<footer><p>%s</p>%s<p>Generated %s UTC. Closed trades only. "
                 "This page is static: it contains no login and no connection to any trading account.</p></footer>" % (
                     esc(cfg.get("credit", "Built on Freqtrade (GPL-3.0). Strategies and research are the author's own.")),
                     ("<p>%s</p>" % link_html) if link_html else "", esc(generated)))
    parts.append("</main></body></html>")
    return "".join(parts)


# ----------------------------------------------------------------------------
# Backtest research page (curated content kept in backtest_data.json)
# ----------------------------------------------------------------------------

def _table(head, rows, text_cols=()):
    th = "".join("<th%s>%s</th>" % ("" if i == 0 or i in text_cols else " class='r'", esc(h)) for i, h in enumerate(head))
    body = []
    for row in rows:
        tds = "".join("<td%s>%s</td>" % ("" if i == 0 or i in text_cols else " class='r'", esc(c)) for i, c in enumerate(row))
        body.append("<tr>%s</tr>" % tds)
    return "<div class='scroll'><table><tr>%s</tr>%s</table></div>" % (th, "".join(body))


def _live_line(bot_id, bots_data):
    for bot, s, open_count, trades, err in bots_data:
        if bot["_id"] != bot_id:
            continue
        if err or not s or s.get("n", 0) == 0:
            return "Live paper so far: no closed trades yet."
        r = ("%s R per trade" % num(s["mean_r"], 2, sign=True)) if s.get("mean_r") is not None else "R not available"
        return ("Live paper so far (since %s): %d closed trades, win rate %s, %s on the stake, %s. %s" % (
            bot.get("since", "start"), s["n"], pct(s["win_rate"], 0, sign=False), pct(s["mean_ret"]), r,
            "Too few trades to judge." if s["n"] < s["min_trades"] else "See the live page for the range."))
    return None


def build_backtest_page(data, cfg, bots_data, generated):
    title = data.get("title", "Backtest research")
    site = cfg.get("site_title", "Bot results")
    p = ['<!doctype html><html lang="en"><head><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width,initial-scale=1">'
         '<title>%s | %s</title><meta name="robots" content="index,follow"><style>%s</style></head><body><main>' % (
             esc(title), esc(site), CSS)]
    p.append('<p class="small"><a href="index.html">&larr; Live paper results</a></p>')
    p.append("<h1>%s</h1>" % esc(title))
    p.append("<p>%s</p>" % esc(data.get("intro", "")))
    p.append('<div class="banner">%s</div>' % esc(cfg.get("disclaimer", DEFAULT_DISCLAIMER)))
    if data.get("why_two"):
        p.append('<div class="card"><strong>Backtest and live, side by side</strong><p>%s</p></div>' % esc(data["why_two"]))

    nav = " | ".join('<a href="#%s">%s</a>' % (esc(sec["id"]), esc(sec["title"])) for sec in data.get("sections", []))
    nav += " | <a href='#failed'>What failed</a> | <a href='#limits'>Limits</a>"
    p.append('<p class="small">Jump to: %s</p>' % nav)

    p.append("<h2>How the tests were run</h2><div class='card scroll'><table>%s</table></div>" % "".join(
        "<tr><td class='wrap'><strong>%s</strong></td><td class='wrap'>%s</td></tr>" % (esc(a), esc(b))
        for a, b in data.get("setup", [])))
    p.append("<details><summary>Terms used on this page</summary><dl>%s</dl></details>" % "".join(
        "<dt><strong>%s</strong></dt><dd>%s</dd>" % (esc(a), esc(b)) for a, b in data.get("concepts", [])))

    for sec in data.get("sections", []):
        p.append('<section id="%s" class="card"><h2>%s <span class="badge">%s</span></h2><p>%s</p>' % (
            esc(sec["id"]), esc(sec["title"]), esc(sec.get("status", "")), esc(sec.get("summary", ""))))
        live = _live_line(sec["bot_id"], bots_data) if sec.get("bot_id") else None
        if live:
            p.append('<p class="banner">%s <a href="index.html#%s">Open live results</a></p>' % (esc(live), esc(sec["bot_id"])))
        for t in sec.get("tables", []):
            p.append("<h3>%s</h3>%s" % (esc(t["caption"]), _table(t["head"], t["rows"], t.get("text_cols", ()))))
            if t.get("note"):
                p.append('<p class="small muted">%s</p>' % esc(t["note"]))
        if sec.get("caveats"):
            p.append("<h3>Caveats for this bot</h3><ul>%s</ul>" % "".join("<li>%s</li>" % esc(c) for c in sec["caveats"]))
        p.append("</section>")

    ft = data.get("failed_table")
    p.append('<h2 id="failed">What failed</h2><p>%s</p>' % esc(data.get("failed_intro", "")))
    if ft:
        p.append("<div class='card'>%s</div>" % _table(ft["head"], ft["rows"], text_cols=(len(ft["head"]) - 1,)))
    if data.get("failed_bullets"):
        p.append("<ul>%s</ul>" % "".join("<li>%s</li>" % esc(b) for b in data["failed_bullets"]))

    p.append('<h2 id="limits">Limits of this research</h2><ul>%s</ul>' % "".join("<li>%s</li>" % esc(x) for x in data.get("limits", [])))
    if data.get("questions"):
        p.append('<div class="card"><strong>Where feedback helps most</strong><ul>%s</ul></div>' % "".join(
            "<li>%s</li>" % esc(q) for q in data["questions"]))
    if data.get("files"):
        p.append('<p class="small muted">%s</p>' % esc(data["files"]))

    links = cfg.get("links") or {}
    link_html = " | ".join('<a href="%s" rel="noopener">%s</a>' % (esc(v), esc(k)) for k, v in links.items() if v)
    p.append("<footer>%s<p>Generated %s UTC. This page is static: it contains no login and no connection to any trading account.</p></footer>" % (
        ("<p>%s</p>" % link_html) if link_html else "", esc(generated)))
    p.append("</main></body></html>")
    return "".join(p)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def safe_id(raw):
    return re.sub(r"[^a-z0-9_-]+", "-", str(raw).lower()).strip("-") or "bot"


def jsonable(s):
    keep = {}
    for k, v in s.items():
        if k in ("equity", "drawdown", "months", "reasons"):
            continue
        if isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, tuple):
            v = list(v)
        keep[k] = v
    return keep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="results_config.json")
    ap.add_argument("--out", default="site")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    min_trades = int(cfg.get("min_trades_for_judgement", 50))

    bots_data, results, ok = [], {}, 0
    seen = set()
    for bot in cfg.get("bots", []):
        bid = safe_id(bot.get("id") or bot.get("name"))
        while bid in seen:
            bid += "-x"
        seen.add(bid)
        bot["_id"] = bid
        try:
            trades, open_count = load_trades(bot["db"])
            if bot.get("since"):  # count only trades opened under the current rules
                since = parse_dt(bot["since"])
                if since is None:
                    raise ValueError("bad 'since' date, use YYYY-MM-DD")
                trades = [t for t in trades if (t["open"] or t["close"]) >= since]
            s = compute_stats(trades, float(bot.get("start_balance", 100)), min_trades)
            bots_data.append((bot, s, open_count, trades, None))
            results[bid] = dict(jsonable(s), open_trades=open_count, name=bot.get("name", bid), status=bot.get("status"))
            ok += 1
            if cfg.get("export_csv", True):
                with open(out_dir / ("trades_%s.csv" % bid), "w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    w.writerow(["bot", "pair", "side", "leverage", "opened_utc", "closed_utc", "profit_pct", "profit_usdt", "exit_reason"])
                    for t in trades:
                        w.writerow([bid, t["pair"], "short" if t["short"] else "long", t["lev"],
                                    t["open"].isoformat(sep=" ") if t["open"] else "", t["close"].isoformat(sep=" "),
                                    "%.4f" % (t["profit"] * 100), "%.4f" % t["pabs"], t["reason"]])
        except Exception as exc:  # one broken bot must not break the whole page
            print("WARNING: bot %r skipped: %s" % (bid, exc), file=sys.stderr)
            bots_data.append((bot, None, 0, [], "Data not available for this bot right now."))

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    bt_file = Path(__file__).resolve().parent / "backtest_data.json"
    if bt_file.exists():
        bt = json.loads(bt_file.read_text(encoding="utf-8"))
        cfg["_has_backtest"] = True
        (out_dir / "backtest.html").write_text(build_backtest_page(bt, cfg, bots_data, generated), encoding="utf-8")
    (out_dir / "index.html").write_text(build_page(cfg, bots_data, generated), encoding="utf-8")
    (out_dir / "results.json").write_text(json.dumps(
        {"generated_utc": generated, "demo": bool(cfg.get("demo")), "bots": results}, indent=2, default=str), encoding="utf-8")
    print("Wrote %s (%d of %d bots read)" % (out_dir / "index.html", ok, len(cfg.get("bots", []))))
    return 0 if ok or not cfg.get("bots") else 1


if __name__ == "__main__":
    sys.exit(main())
