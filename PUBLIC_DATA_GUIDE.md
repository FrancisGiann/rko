# Public data and local research

This repository contains code and frozen protocol descriptions, but no market dataset, saved score, fill ledger, or account database. On first run, historical summaries are unavailable. The app does not reconstruct or invent missing results. V4 can be independently evaluated from a separately obtained matching dataset. Original V5 and Jesse archived comparisons pin matching V4 result hashes and require their compatible saved score receipts and time-metric audit as well; restoring normalized OHLCV alone does not recreate those archival panels.

## Binance Vision historical data

Before using the explicit archive workflow, review the current [Binance Vision data terms](https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md) and [`DATA_LICENSE_NOTICE.md`](DATA_LICENSE_NOTICE.md). The terms cover the archive data and derived models, charts, and research results. They include CC BY-NC-SA 4.0 requirements, attribution, and personal, non-production historical-research restrictions. The terms specify same-license requirements when redistributing derivatives; this source snapshot excludes all market data and derived outputs to avoid redistribution or mixed-license assumptions. These terms are separate from the application-code rights.

Fetch and V4 evaluate are explicit, separate operations. Fetching creates local raw archives and a normalized dataset cache; V4 evaluation writes local research results and a trial ledger. Neither starts automatically when the server or page opens. For example:

```sh
python3 research_cli.py status
python3 research_cli.py fetch
python3 research_cli.py evaluate
```

Do not share or publish fetched data or derived results unless their applicable license permits it. This source snapshot does not ship those files. Restore a separately obtained, checksum-matching cache to reproduce the historical studies offline. `research_cli.py fetch` verifies the currently served terms against the reviewed hash and stops if the terms change.

## CoinGecko aggregate USD data

The forward paper cohort uses aggregate CoinGecko USD observations as simulated price proxies. They are not exchange OHLC candles or executable venue quotes. Read the current [CoinGecko API terms](https://www.coingecko.com/en/api_terms) and [license FAQ](https://support.coingecko.com/articles/1043089972-what-are-the-differences-between-commercial-and-custom-licenses). Current terms do not permit AI-model training using these data; the legacy CoinGecko ML training and refresh path is disabled.

The forward cohort is opt-in, paper-only, and separate from the Binance USDT historical studies. Its historical warmup is input only; it does not count as prospective performance. No credentials or API key are bundled.

## Local privacy

Keep SQLite databases, raw and normalized data, saved studies, trial ledgers, audit files, CSV exports, browser captures, logs, and local receipts out of source control. `.gitignore` covers common generated paths, but review every change before publishing. The app does not encrypt SQLite files; use operating-system or full-disk encryption when needed.
