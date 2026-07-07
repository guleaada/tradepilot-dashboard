#!/usr/bin/env python3
"""
export_dashboard.py — reads the spot and futures agent SQLite DBs and writes a
single data.json the Mission Control dashboard renders. Run it in CI after both
bots commit, or locally with both DB paths available.

Usage:
    python export_dashboard.py \
        --spot   ../tradepilot/data/tradepilot.db \
        --futures ./data/tradepilot-futures.db \
        --spot-base 1000 --futures-base 5000 --leverage 3 \
        --out ./docs/data.json

Everything degrades gracefully: a missing DB, empty tables, or absent columns
just produce zeros/empties rather than crashing, so the dashboard always renders.
"""
import argparse, json, sqlite3, datetime, os

def q(db, sql, args=()):
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []

def one(db, sql, args=(), default=None):
    r = q(db, sql, args)
    return r[0][0] if r and r[0] and r[0][0] is not None else default

def has_col(db, table, col):
    try:
        return col in [c[1] for c in db.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return False

def window_snaps(db, since=None):
    """Equity snapshots inside the experiment window (all history if no since)."""
    if since:
        return q(db, "SELECT equity FROM equity_snapshots WHERE ts >= ? ORDER BY id", (f"{since}T00:00:00",))
    return q(db, "SELECT equity FROM equity_snapshots ORDER BY id")


def window_base(db, base, since=None):
    """The equity each agent BROUGHT INTO the experiment window.

    Returns are measured against this, not the nominal account base: the spot
    bot entered the window already +1.4% from its June inception, and
    normalizing to the account base credited those pre-race gains to the race
    verdict. Both curves must start at ~0% on day 1.
    """
    rows = window_snaps(db, since)
    return rows[0][0] if rows and rows[0][0] else base


def curve(db, base, since=None):
    """Equity snapshots -> cumulative % return series since the window start.

    `since` (YYYY-MM-DD) trims to the experiment window so both agents' series
    share a comparable index: the spot bot has pre-experiment history that
    would otherwise squash the futures line to the left of the shared axis.
    """
    rows = window_snaps(db, since)
    if not rows:
        return [0.0]
    wbase = window_base(db, base, since)
    return [round((r[0] / wbase - 1) * 100, 4) for r in rows]

def trade_stats(db, base, futures=False, since=None):
    closed = q(db, "SELECT pnl, initial_risk, qty, entry_price FROM trades WHERE status='closed' AND pnl IS NOT NULL")
    equity = one(db, "SELECT equity FROM equity_snapshots ORDER BY id DESC LIMIT 1", default=base)
    open_n = one(db, "SELECT COUNT(*) FROM trades WHERE status='open'", default=0)
    n = len(closed)
    wins = [t for t in closed if t[0] > 0]
    losses = [t for t in closed if t[0] <= 0]
    gross_win = sum(t[0] for t in wins)
    gross_loss = abs(sum(t[0] for t in losses))
    total_pnl = sum(t[0] for t in closed)
    # avg R: pnl / (initial_risk * qty) when available
    rs = []
    for pnl, ir, qty, ep in closed:
        risk = (ir or 0) * (qty or 0)
        if risk > 0:
            rs.append(pnl / risk)
    # % return and drawdown measured INSIDE the experiment window, against the
    # equity the agent brought into it (see window_base)
    wbase = window_base(db, base, since)
    snaps = [r[0] for r in window_snaps(db, since)]
    peak, max_dd = -1e18, 0.0
    for e in snaps:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)
    out = {
        "equity": round(equity, 2),
        "retPct": round((equity / wbase - 1) * 100, 3),
        "trades": n,
        "winRate": (len(wins) / n) if n else None,
        "profitFactor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "maxDdPct": round(max_dd * 100, 2),
        "avgR": round(sum(rs) / len(rs), 3) if rs else None,
        "openPositions": open_n,
        "expectancy": round(total_pnl / n, 2) if n else None,
    }
    if futures and has_col(db, "trades", "direction"):
        out["longPnl"] = round(one(db, "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='closed' AND direction='long'", default=0), 2)
        out["shortPnl"] = round(one(db, "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='closed' AND direction='short'", default=0), 2)
    return out

def blockers(db):
    """Parse no_entry reasons from events in the last 24h (best-effort)."""
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()
    rows = q(db, "SELECT detail FROM events WHERE type='NO_ENTRY' AND ts>=? ", (since,))
    counts = {}
    for (detail,) in rows:
        try:
            reason = json.loads(detail).get("reason", "other")
        except Exception:
            reason = "other"
        counts[reason] = counts.get(reason, 0) + 1
    # fallback: some builds log reasons differently — scan events broadly
    if not counts:
        for (detail,) in q(db, "SELECT detail FROM events WHERE ts>=?", (since,)):
            try:
                r = json.loads(detail).get("reason")
                if r: counts[r] = counts.get(r, 0) + 1
            except Exception:
                pass
    return [{"reason": k, "n": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])[:8]]

def trend_r(db):
    if not has_col(db, "trades", "trend_class"):
        return []
    rows = q(db, """SELECT trend_class,
                           COUNT(*) n,
                           AVG(CASE WHEN pnl>0 THEN 1.0 ELSE 0.0 END) win,
                           AVG(CASE WHEN initial_risk*qty>0 THEN pnl/(initial_risk*qty) END) avgR
                    FROM trades WHERE status='closed' AND pnl IS NOT NULL
                    GROUP BY trend_class""")
    order = {"strong": 0, "normal": 1, "weak": 2}
    out = [{"cls": r[0] or "normal", "n": r[1],
            "win": round(r[2], 3) if r[2] is not None else None,
            "avgR": round(r[3], 3) if r[3] is not None else None} for r in rows]
    return sorted(out, key=lambda x: order.get(x["cls"], 9))

def latest_regimes(db):
    """Newest regime opinion per pair: {pair: (regime, confidence)}."""
    out = {}
    for pair, regime, conf in q(db, """
        SELECT pair, regime, confidence FROM regime_calls
        WHERE id IN (SELECT MAX(id) FROM regime_calls GROUP BY pair)
    """):
        out[pair] = (regime, conf)
    return out

def pair_scores(spot, fut):
    """Per-pair scorecard across both agents: the 'where is the edge' view.

    For every pair either bot has traded or has a regime on, report each bot's
    closed-trade count + realized P&L + whether it's open now, plus the latest
    futures regime/confidence (futures covers the wider 15-pair universe).
    Sorted by combined |P&L| desc, then by open positions, then pair name — so
    the pairs actually moving the needle float to the top. Empty-but-valid
    until trades close; regime column is populated from cycle one.
    """
    def agg(db):
        if not db:
            return {}, {}
        closed = {}
        for pair, n, pnl in q(db, """
            SELECT pair, COUNT(*), COALESCE(SUM(pnl), 0)
            FROM trades WHERE status='closed' AND pnl IS NOT NULL GROUP BY pair
        """):
            closed[pair] = {"trades": n, "pnl": round(pnl, 2)}
        openp = {}
        dircol = "direction" if has_col(db, "trades", "direction") else "'long'"
        for pair, direction in q(db, f"SELECT pair, {dircol} FROM trades WHERE status='open'"):
            openp[pair] = direction or "long"
        return closed, openp

    s_closed, s_open = agg(spot)
    f_closed, f_open = agg(fut)
    regimes = latest_regimes(fut) or latest_regimes(spot)

    pairs = set(s_closed) | set(s_open) | set(f_closed) | set(f_open) | set(regimes)
    rows = []
    for p in pairs:
        s = s_closed.get(p, {"trades": 0, "pnl": 0.0})
        f = f_closed.get(p, {"trades": 0, "pnl": 0.0})
        reg, conf = regimes.get(p, (None, None))
        rows.append({
            "pair": p,
            "regime": reg, "confidence": conf,
            "spotTrades": s["trades"], "spotPnl": s["pnl"], "spotOpen": p in s_open,
            "futTrades": f["trades"], "futPnl": f["pnl"],
            "futOpen": f_open.get(p),  # direction string or None
            "totalPnl": round(s["pnl"] + f["pnl"], 2),
            "open": p in s_open or p in f_open,
        })
    rows.sort(key=lambda r: (-abs(r["totalPnl"]), not r["open"], r["pair"]))
    return rows[:15]

def feed(spot, fut):
    items = []
    def pull(db, bot, has_dir):
        cols = "pair, entry_price, exit_price, pnl, status"
        dircol = "direction" if has_dir else "'long'"
        rows = q(db, f"SELECT {dircol}, {cols} FROM trades ORDER BY id DESC LIMIT 6")
        for r in rows:
            direction, pair, ep, xp, pnl, status = r
            items.append({
                "bot": bot, "dir": direction or "long", "pair": pair,
                "entry": round(ep, 2) if ep else 0,
                "exit": round(xp, 2) if xp else None,
                "pnl": round(pnl, 2) if pnl is not None else None,
                "note": status, "closed": status == "closed",
                "_id": None,
            })
    if fut:   pull(fut, "fut", has_col(fut, "trades", "direction"))
    if spot:  pull(spot, "spot", has_col(spot, "trades", "direction"))
    return items[:8]

def verdict(spot_ret, fut_ret, day):
    """The race headline: who leads, by how much (in % return points)."""
    gap = abs(fut_ret - spot_ret)
    if gap < 0.05:
        leader, text = "tie", "Dead heat"
    elif fut_ret > spot_ret:
        leader, text = "futures", f"Futures leading spot by +{gap:.2f}%"
    else:
        leader, text = "spot", f"Spot leading futures by +{gap:.2f}%"
    return {
        "leader": leader,
        "gapPct": round(gap, 2),
        "text": text,
        "day": day,
        "spotRetPct": round(spot_ret, 3),
        "futRetPct": round(fut_ret, 3),
    }

def connect(path):
    if path and os.path.exists(path):
        try:
            return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spot"); ap.add_argument("--futures")
    ap.add_argument("--spot-base", type=float, default=1000)
    ap.add_argument("--futures-base", type=float, default=5000)
    ap.add_argument("--leverage", type=int, default=3)
    ap.add_argument("--start-date", default=None, help="YYYY-MM-DD experiment start, for day counter")
    ap.add_argument("--out", default="data.json")
    a = ap.parse_args()

    sdb, fdb = connect(a.spot), connect(a.futures)

    day = 1
    if a.start_date:
        try:
            d0 = datetime.date.fromisoformat(a.start_date)
            day = (datetime.date.today() - d0).days + 1
        except ValueError:
            pass

    data = {
        "meta": {
            "day": max(1, day),
            "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "spot": {"base": a.spot_base},
            "futures": {"base": a.futures_base, "leverage": a.leverage},
        },
        "curves": {
            "spot": curve(sdb, a.spot_base, a.start_date) if sdb else [0.0],
            "fut": curve(fdb, a.futures_base, a.start_date) if fdb else [0.0],
        },
        "spot": trade_stats(sdb, a.spot_base, since=a.start_date) if sdb else {"equity": a.spot_base, "retPct": 0, "trades": 0, "winRate": None, "profitFactor": None, "maxDdPct": 0, "avgR": None, "openPositions": 0},
        "futures": trade_stats(fdb, a.futures_base, futures=True, since=a.start_date) if fdb else {"equity": a.futures_base, "retPct": 0, "trades": 0, "winRate": None, "profitFactor": None, "maxDdPct": 0, "avgR": None, "openPositions": 0},
        "blockers": blockers(fdb) if fdb else [],
        "trendR": trend_r(fdb) if fdb else [],
        "pairScores": pair_scores(sdb, fdb),
        "feed": feed(sdb, fdb),
    }
    data["verdict"] = verdict(data["spot"]["retPct"], data["futures"]["retPct"], data["meta"]["day"])

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(data, f, indent=2)
    print(f"wrote {a.out}: day {data['meta']['day']}, "
          f"spot {data['spot']['trades']} trades, futures {data['futures']['trades']} trades")

if __name__ == "__main__":
    main()
