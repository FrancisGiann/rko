# RKO

RKO is a local paper-simulation and historical-research web app using Python's standard library, SQLite, and vanilla JavaScript. This source-only snapshot includes no archived prices, exchange-order path, credentials, leverage, or real-money execution.

## Run locally

Requires Python 3.10+ and SQLite. No dependency installation is needed.

```sh
python3 server.py
```

Open `http://127.0.0.1:8765/`. Initial page load reads local state only: it does not fetch market history, start a paper cohort, or evaluate a study. The app may create local SQLite and cache folders; keep them outside version control and use device encryption as appropriate.

The forward cohort is opt-in and paper-only. It uses aggregate USD price observations, separate from Binance USDT historical research. Optional TP/SL is OFF by default. A threshold trigger records a decision and requires a later fresh quote before a simulated sell. The five-minute sampling, service interruptions, provider gaps, modeled costs, and price gaps can produce a worse outcome than the threshold. No execution or maximum-loss guarantee is made. The separate 10% account rule is a sampled peak-net-liquidation drawdown halt, not a per-position stop.

Legacy CoinGecko ML training and refresh are disabled under current API terms. Read [`PUBLIC_GUIDE.md`](PUBLIC_GUIDE.md), [`PUBLIC_DATA_GUIDE.md`](PUBLIC_DATA_GUIDE.md), and [`DATA_LICENSE_NOTICE.md`](DATA_LICENSE_NOTICE.md) before using data workflows.

## Historical panels and rights

The public source ZIP intentionally omits saved V4/V5/Jesse result JSON, fills, ledgers, metric audits, Binance archives, normalized OHLCV, and user databases. Historical panels therefore show unavailable rather than inventing results. V4 can be independently evaluated from a separately obtained checksum-matching cache. Original V5 and Jesse archived comparisons also require their matching saved results, receipts, and time audit; normalized data alone does not recreate those panels.

No project-wide open-source license is selected by this snapshot. GitHub's terms govern viewing and forking on GitHub; the repository adds no broader code-reuse license. The exact MIT notice under `third_party/jesse/LICENSE` applies only to the referenced upstream Jesse example strategies. Data terms are separate from application-code rights.

The public test report covers source-only synthetic checks. No public screenshot is included: available images contain private historical results or synthetic private QA state.
