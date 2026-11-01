# RKO public source guide

This public source snapshot contains the RKO local web app and synthetic tests. It does not contain saved V4/V5/Jesse studies, market archives, normalized candles, account databases, trading history, screenshots, or QA logs. The archived result screens therefore report unavailable until compatible local research artifacts are supplied; fetching data alone does not recreate the original V5 or Jesse result bundle.

## Run locally

Requires Python 3.10+ and SQLite. No third-party Python or JavaScript packages are required.

```sh
python3 server.py
```

Open `http://127.0.0.1:8765/`. The initial page only reads local state; it does not fetch market data, start a paper session, or evaluate a historical study. Default databases and caches are created locally and ignored by Git. SQLite is not encrypted by the app; use encrypted device storage as appropriate.

The forward cohort is opt-in and paper-only. It uses aggregate USD price observations, separate from Binance USDT historical research. TP and SL are optional and OFF by default. If enabled, each applies to the remaining position using modeled entry costs plus estimated exit costs; a trigger waits for a later fresh quote before a simulated sell. Sampled prices, fees, delays, and gaps do not provide a guaranteed fill, stop, maximum loss, or return. The separate 10% account drawdown halt is a sampled peak net-liquidation rule, not a per-position stop.

The app makes no exchange orders and has no API credential input or leverage. Legacy CoinGecko ML training and refresh are disabled under current API terms. Review [`PUBLIC_DATA_GUIDE.md`](PUBLIC_DATA_GUIDE.md), [`DATA_LICENSE_NOTICE.md`](DATA_LICENSE_NOTICE.md), and [`PROJECT_LICENSE_STATUS.md`](PROJECT_LICENSE_STATUS.md) before using a data workflow.

## Archived comparisons

No historical result JSON, fills, trial ledgers, metric audit, or dataset is bundled. V4 may be independently evaluated from a separately obtained checksum-matching dataset. Original V5 and Jesse archived comparisons pin compatible V4 result hashes and also need their corresponding saved result, receipt, and audit artifacts; normalized OHLCV by itself does not reproduce those panels. All such historical work remains subject to the current applicable data terms.

## Rights and evidence

No project-wide open-source license has been selected by this snapshot. GitHub's terms govern viewing and forking on GitHub, but this repository adds no broader code-reuse license. The MIT license notice under `third_party/jesse/LICENSE` applies only to the specifically attributed upstream example strategy references. Data licenses are separate from application-code rights.

The public test report records source-only synthetic verification and contains no saved market-price or historical-strategy P&L examples; regression fixtures may show simulated amounts. No V5.2 public screenshot is included because the available screenshots show either private historical results or private QA fixtures; those files are not part of this public packet.
