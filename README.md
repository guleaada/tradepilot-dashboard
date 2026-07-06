# TradePilot Mission Control

Static dashboard for the dual-agent TradePilot A/B experiment: **spot**
(long-only, 1x) vs **futures testnet** (long+short, 3x isolated, capped 5x).
Both agents are paper traders — no real funds anywhere in this stack.

Live at **https://guleaada.github.io/tradepilot-dashboard/**

## How it works

- The two agent repos each commit their SQLite database back to themselves
  every 15 minutes from their own GitHub Actions cycles.
- [dashboard.yml](.github/workflows/dashboard.yml) runs hourly: checks out
  both agent repos, runs [export_dashboard.py](export_dashboard.py) (stdlib
  only) to produce `docs/data.json`, and deploys `docs/` to GitHub Pages.
- [docs/index.html](docs/index.html) is a self-contained page (no build step,
  no chart libraries) that renders the head-to-head **% return** curve —
  bankrolls differ ($1,000 spot vs $5,000 futures testnet), so only
  percentage metrics are comparable — plus per-agent KPIs, direction and
  trend-class breakdowns, open positions, and recent trades.

The exporter is defensive: a missing DB/table/column yields partial data or a
"no data yet" card, never a broken page.

## One-time setup after creating the repo

1. **Enable Pages**: Settings → Pages → Source = **GitHub Actions**.
2. **Agent access**: the agent repos are private, so create a fine-grained
   PAT with **Contents: read** on `guleaada/tradepilot` and
   `guleaada/tradepilot-futures` only, add it as the **`AGENT_PAT`** repo
   secret, then uncomment the two `token:` lines in
   [.github/workflows/dashboard.yml](.github/workflows/dashboard.yml).
   Until then the dashboard deploys in its "no data yet" state.

Never write the token into any file or command.

## Local preview

```bash
python3 export_dashboard.py \
  --spot    "path/to/tradepilot/data/tradepilot.db" \
  --futures "path/to/tradepilot-futures/data/tradepilot-futures.db"
python3 -m http.server 4173 -d docs
# open http://localhost:4173
```

`docs/data.json` is generated output (gitignored); CI rebuilds it every run.
