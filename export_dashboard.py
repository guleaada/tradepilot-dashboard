#!/usr/bin/env python3
"""Export TradePilot dual-agent stats into docs/data.json.

Reads the SQLite databases committed by the two trading agents (spot
TradePilot and TradePilot-Futures) and produces one small JSON file the
static dashboard in docs/index.html renders. Stdlib only — no pip installs.

Defensive by design: a missing database, table, or column produces partial
data (or null for that agent), never a crash. CI runs this even before the
AGENT_PAT secret is configured, in which case both agents export as null and
the dashboard shows its "no data yet" state.

Usage:
  python3 export_dashboard.py \
    --spot    agents/spot/data/tradepilot.db \
    --futures agents/futures/data/tradepilot-futures.db \
    --out     docs/data.json
"""
import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

MAX_CURVE_POINTS = 600  # keep data.json small; ~30 days of hourly snapshots fits anyway


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def columns(cx, table):
    try:
        return {r[1] for r in cx.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def q(cx, sql, params=()):
    try:
        return cx.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def downsample(points, cap=MAX_CURVE_POINTS):
    if len(points) <= cap:
        return points
    step = len(points) / cap
    sampled = [points[int(i * step)] for i in range(cap)]
    sampled[-1] = points[-1]  # always keep the latest reading
    return sampled


def round2(v):
    return None if v is None else round(v, 2)


def export_agent(path, kind):
    """kind: 'spot' | 'futures'. Returns a dict or None when the DB is absent."""
    if not path or not os.path.exists(path):
        return None
    cx = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cx.row_factory = sqlite3.Row
    try:
        trade_cols = columns(cx, "trades")
        dir_col = "direction" if "direction" in trade_cols else "side"

        # --- equity curve (hourly snapshots committed by the agent) ---
        snaps = q(cx, "SELECT ts, equity FROM equity_snapshots ORDER BY id")
        curve = downsample([[r["ts"], round(r["equity"], 2)] for r in snaps])
        baseline = snaps[0]["equity"] if snaps else None
        equity_now = snaps[-1]["equity"] if snaps else None

        peak, max_dd = float("-inf"), 0.0
        for r in snaps:
            peak = max(peak, r["equity"])
            if peak > 0:
                max_dd = max(max_dd, (peak - r["equity"]) / peak)

        # --- closed-trade stats ---
        closed = q(cx, "SELECT * FROM trades WHERE status = 'closed' ORDER BY id")
        pnls = [t["pnl"] or 0 for t in closed]
        wins = [p for p in pnls if p > 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(p for p in pnls if p <= 0))
        win_rate = len(wins) / len(closed) if closed else None
        profit_factor = (
            None if not closed
            else (gross_win / gross_loss) if gross_loss > 0
            else ("inf" if gross_win > 0 else None)
        )
        expectancy = (sum(pnls) / len(closed)) if closed else None
        today_pnl = sum(
            t["pnl"] or 0 for t in closed
            if (t["exit_time"] or "").startswith(today_utc())
        )

        # --- AI spend ---
        spend_total = q(cx, "SELECT COALESCE(SUM(spend), 0) AS s FROM ai_budget")
        spend_today = q(cx, "SELECT COALESCE(SUM(spend), 0) AS s FROM ai_budget WHERE date = ?", (today_utc(),))

        # --- open positions ---
        open_rows = q(cx, "SELECT * FROM trades WHERE status = 'open' ORDER BY id")
        open_positions = [
            {
                "pair": t["pair"],
                "direction": t[dir_col] if dir_col in trade_cols else "long",
                "qty": t["qty"],
                "entry": round2(t["entry_price"]),
                "stop": round2(t["stop_price"]),
                "tp": round2(t["tp_price"]),
                "opened": t["entry_time"],
                **({"leverage": t["leverage"]} if "leverage" in trade_cols else {}),
                **({"trend": t["trend_class"]} if "trend_class" in trade_cols else {}),
                **({"funding": round(t["funding_paid"] or 0, 4)} if "funding_paid" in trade_cols else {}),
            }
            for t in open_rows
        ]

        recent_trades = [
            {
                "pair": t["pair"],
                "direction": t[dir_col] if dir_col in trade_cols else "long",
                "pnl": round2(t["pnl"]),
                "reason": t["exit_reason"],
                "closed": t["exit_time"],
                **({"trend": t["trend_class"]} if "trend_class" in trade_cols else {}),
            }
            for t in q(cx, "SELECT * FROM trades WHERE status = 'closed' ORDER BY id DESC LIMIT 10")
        ]

        # --- latest regime opinions (one per pair, newest first) ---
        regimes = [
            {"pair": r["pair"], "regime": r["regime"], "confidence": r["confidence"], "ts": r["ts"]}
            for r in q(cx, """
                SELECT pair, regime, confidence, ts FROM regime_calls
                WHERE id IN (SELECT MAX(id) FROM regime_calls GROUP BY pair)
                ORDER BY ts DESC LIMIT 15
            """)
        ]

        last_event = q(cx, "SELECT ts FROM events ORDER BY id DESC LIMIT 1")

        agent = {
            "kind": kind,
            "baseline": round2(baseline),
            "equity": round2(equity_now),
            "return_pct": round2((equity_now / baseline - 1) * 100) if baseline and equity_now else None,
            "curve": curve,
            "stats": {
                "closed": len(closed),
                "win_rate": round(win_rate, 4) if win_rate is not None else None,
                "profit_factor": round(profit_factor, 2) if isinstance(profit_factor, float) else profit_factor,
                "total_pnl": round2(sum(pnls)),
                "expectancy": round2(expectancy),
                "max_drawdown_pct": round(max_dd * 100, 2),
                "today_pnl": round2(today_pnl),
            },
            "ai_spend": {
                "total": round(spend_total[0]["s"], 4) if spend_total else 0,
                "today": round(spend_today[0]["s"], 4) if spend_today else 0,
            },
            "open_positions": open_positions,
            "recent_trades": recent_trades,
            "regimes": regimes,
            "last_activity": last_event[0]["ts"] if last_event else None,
        }

        # --- futures-only extras ---
        if "direction" in trade_cols:
            agent["by_direction"] = [
                dict(r) for r in q(cx, """
                    SELECT direction, COUNT(*) AS trades,
                           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                           ROUND(COALESCE(SUM(pnl), 0), 2) AS total_pnl
                    FROM trades WHERE status = 'closed' GROUP BY direction ORDER BY direction
                """)
            ]
        if "trend_class" in trade_cols:
            agent["by_trend"] = [
                dict(r) for r in q(cx, """
                    SELECT COALESCE(trend_class, '(fixed tp)') AS trend, COUNT(*) AS trades,
                           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                           ROUND(AVG(pnl / (initial_risk * entry_qty)), 2) AS avg_r,
                           ROUND(COALESCE(SUM(pnl), 0), 2) AS total_pnl
                    FROM trades
                    WHERE status = 'closed' AND initial_risk > 0 AND entry_qty > 0
                    GROUP BY COALESCE(trend_class, '(fixed tp)')
                """)
            ]
        if "funding_paid" in trade_cols:
            funding = q(cx, "SELECT COALESCE(SUM(funding_paid), 0) AS f FROM trades")
            agent["funding_total"] = round(funding[0]["f"], 4) if funding else 0

        return agent
    finally:
        cx.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--spot", default="agents/spot/data/tradepilot.db")
    ap.add_argument("--futures", default="agents/futures/data/tradepilot-futures.db")
    ap.add_argument("--out", default="docs/data.json")
    args = ap.parse_args()

    data = {
        "generated_at": utcnow_iso(),
        "agents": {
            "spot": export_agent(args.spot, "spot"),
            "futures": export_agent(args.futures, "futures"),
        },
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(data, f, separators=(",", ":"))

    for name, agent in data["agents"].items():
        status = f"equity ${agent['equity']} ({agent['stats']['closed']} closed trades)" if agent else "NO DATA (db missing)"
        print(f"  {name:8s} {status}")
    print(f"wrote {args.out} ({os.path.getsize(args.out):,} bytes)")


if __name__ == "__main__":
    main()
