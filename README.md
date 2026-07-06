# TradePilot — Mission Control

A live dashboard for the dual-agent A/B experiment: the **spot** bot (long-only, $1k base) racing the **futures** bot (long+short, 3×, $5k base). Both curves are shown as **% return** so the different bases compare directly.

It reads the committed SQLite DBs from both agent repos, exports a single `data.json`, and renders a self-contained dashboard on GitHub Pages — auto-refreshing twice an hour.

## What it shows
- **The Race** — both equity curves overlaid as cumulative % return
- **Side-by-side stats** — equity, % return, win rate, profit factor, max drawdown, avg R, and (futures) long vs short P&L split
- **Entry blockers** — which filter is throttling the most trades in the last 24h
- **Realized R by trend class** — the verdict on dynamic take-profit: do strong-trend entries actually run further?
- **Trade feed** — recent trades from both agents, colour-coded long/short

## Setup (5 minutes)

1. **Create a new repo** `tradepilot-dashboard` and push these files (`docs/`, `export_dashboard.py`, `.github/`).
2. **Enable Pages:** repo Settings → Pages → Source = **GitHub Actions**.
3. **If the two agent repos are private**, create a fine-grained Personal Access Token with read access to `tradepilot` and `tradepilot-futures`, add it as a repo secret named `AGENT_PAT`, and uncomment the two `token:` lines in `.github/workflows/dashboard.yml`. If they're public, skip this.
4. Run the **Build dashboard** workflow once (Actions → Run workflow). Your dashboard goes live at `https://guleaada.github.io/tradepilot-dashboard/`.

That's it — it then rebuilds twice an hour on its own.

## Local preview
Open `docs/index.html` directly in a browser — it renders with sample data so you can see the layout. To preview with real numbers:

```bash
python export_dashboard.py \
  --spot   ../tradepilot/data/tradepilot.db \
  --futures ../tradepilot-futures/data/tradepilot-futures.db \
  --spot-base 1000 --futures-base 5000 --leverage 3 \
  --start-date 2026-07-04 \
  --out docs/data.json
# then serve the folder:
python -m http.server -d docs 8080   # open http://localhost:8080
```

## Notes
- The exporter degrades gracefully: missing DBs, empty tables, or absent columns produce zeros/empties, never a crash — the dashboard always renders.
- `--start-date` drives the "day N of 30" counter; set it to your experiment start.
- The **entry blockers** panel reads `NO_ENTRY` events; if your bots log rejection reasons under a different event type, adjust the `blockers()` query in `export_dashboard.py`.
- Everything is paper/testnet — the banner says so, and it should stay that way.
