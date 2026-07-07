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

def curve(db, base, since=None):
    """Equity snapshots -> cumulative % return series.

    `since` (YYYY-MM-DD) trims to the experiment window so both agents' series
    share a comparable index: the spot bot has pre-experiment history that
    would otherwise squash the futures line to the left of the shared axis.
    """
    if since:
        rows = q(db, "SELECT equity FROM equity_snapshots WHERE ts >= ? ORDER BY id", (f"{since}T00:00:00",))
    else:
        rows = q(db, "SELECT equity FROM equity_snapshots ORDER BY id")
    if not rows:
        return [0.0]
    return [round((r[0] / base - 1) * 100, 4) for r in rows]

def trade_stats(db, base, futures=False):
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
    # drawdown from snapshots
    snaps = [r[0] for r in q(db, "SELECT equity FROM equity_snapshots ORDER BY id")]
    peak, max_dd = -1e18, 0.0
    for e in snaps:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)
    out = {
        "equity": round(equity, 2),
        "retPct": round((equity / base - 1) * 100, 3),
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
        "spot": trade_stats(sdb, a.spot_base) if sdb else {"equity": a.spot_base, "retPct": 0, "trades": 0, "winRate": None, "profitFactor": None, "maxDdPct": 0, "avgR": None, "openPositions": 0},
        "futures": trade_stats(fdb, a.futures_base, futures=True) if fdb else {"equity": a.futures_base, "retPct": 0, "trades": 0, "winRate": None, "profitFactor": None, "maxDdPct": 0, "avgR": None, "openPositions": 0},
        "blockers": blockers(fdb) if fdb else [],
        "trendR": trend_r(fdb) if fdb else [],
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
