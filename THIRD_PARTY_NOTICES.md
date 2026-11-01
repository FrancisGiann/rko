# Third-party notices

## Jesse example strategy references

The project independently implements the mathematical rules of two public Jesse example strategies. It does not bundle, install, import, or execute the Jesse framework or upstream strategy modules.

- Jesse `Donchian` example, commit [`7c91e0a37bf62165790120d730442e4f6eb00364`](https://github.com/jesse-ai/example-strategies/commit/7c91e0a37bf62165790120d730442e4f6eb00364), file SHA-256 `fc635b257ad1e12951dc140dae46a63bd37e9abfa5f2d681ef1e754d8ce393fe`; source credits gabrielweich/jesse-strategies.
- Jesse `SMACrossover` example, same commit, file SHA-256 `453440d7b934c494934a1c56b3826d94638594f79ad4e4c7faaff36b96d33fae`; source credits Fengkie J.
- The explicit Donchian period 20 is consistent with the Jesse indicator default at commit [`aded4b9d51d432d411a8f7fbf45e47572dff6629`](https://github.com/jesse-ai/jesse/commit/aded4b9d51d432d411a8f7fbf45e47572dff6629), file SHA-256 `b7e96ebe3ba476c771a65b353c269a84d02e04587f0d76166c5f322bbbb3a401`.

The referenced examples are MIT licensed. Their full license notice is preserved in [`third_party/jesse/LICENSE`](third_party/jesse/LICENSE). Copyright (c) 2020 jesse-ai. This notice does not apply to the RKO application code.

## Binance historical market data

Binance Vision archive data and derived results are governed by separate terms, including CC BY-NC-SA 4.0 and personal, non-production historical-research restrictions. No such data or results are distributed in this source snapshot. See [`DATA_LICENSE_NOTICE.md`](DATA_LICENSE_NOTICE.md) and the [official terms](https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md).

## CoinGecko

The separate forward paper workflow may request public aggregate USD observations from CoinGecko. Read its current [API terms](https://www.coingecko.com/en/api_terms) and [license FAQ](https://support.coingecko.com/articles/1043089972-what-are-the-differences-between-commercial-and-custom-licenses). No data or API credentials are bundled, and the legacy CoinGecko model-training and refresh path is disabled.
