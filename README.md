# RKO

RKO is a local research and paper-simulation dashboard. The source uses Python's standard library, SQLite, and vanilla JavaScript. It contains no exchange-order endpoint, credential entry, leverage, deployment configuration, or real-money execution.

## Run locally

Requires Python 3.10 or later and SQLite. No dependency installation is required.

```sh
python3 server.py
```

Open `http://127.0.0.1:8765/`. Page load reads local state only. It does not download market history, fit a model, or start a paper cohort. The default database is `data/crypto_lab_v5/paper.sqlite3`; those paths are retained for compatibility. Data directories, database files, caches, and research outputs are ignored by `.gitignore`.

The fixed 10% risk halt is an account-level sampled peak-net-liquidation-value drawdown rule, not a per-position stop-loss or guaranteed maximum loss. Price gaps and quote timing can overshoot it.

Run the synthetic regression tests with:

```sh
python3 -m unittest discover -v
node --check app.js
```

## Historical studies and saved results

This public source snapshot intentionally excludes raw Binance archives, normalized OHLCV data, and every saved V4/V5/Jesse score or ledger. The historical result views fail closed and show an unavailable state when compatible local artifacts are absent. V4 can be independently evaluated from a separately obtained, checksum-verified dataset. The original V5 and Jesse archived comparisons pin matching V4 result hashes and also require their compatible score receipts and time-metric audit; normalized data alone cannot recreate those archived panels. The synthetic demo is clearly separate from market evidence.

Fetching Binance data and evaluating a study are separate explicit commands. Read [`PUBLIC_DATA_GUIDE.md`](PUBLIC_DATA_GUIDE.md) and the current official terms first. The historical workflow is restricted to personal, non-production research under the dataset terms. The app does not fetch or evaluate on page load. CoinGecko aggregate USD prices are reserved for a separate simulated forward price-rule cohort; current CoinGecko terms do not permit AI-model training from its data.

## Rights and notices

No project-wide license is included, so this public repository does not grant permission to reuse the RKO application code. The MIT license in [`third_party/jesse/LICENSE`](third_party/jesse/LICENSE) applies only to the referenced upstream Jesse example strategies. See [`PROJECT_LICENSE_STATUS.md`](PROJECT_LICENSE_STATUS.md) and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
