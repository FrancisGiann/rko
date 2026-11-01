"""CoinGecko public HTTPS observations; never turn sampled prices into OHLC."""
from __future__ import annotations

import json
import math
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from research import HOUR

SYMBOLS = {"BTC-USD": "bitcoin", "ETH-USD": "ethereum"}
SOURCE = "CoinGecko public API · aggregate USD prices"
TIMEOUT, MAX_BYTES, QUOTE_MAX_AGE = 7, 1_000_000, 120
DAY_SECONDS, FORWARD_WARMUP = 86_400, 100


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z") if ts is not None else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    bundle = Path("/etc/ssl/certs/ca-certificates.crt")
    if ssl.get_default_verify_paths().cafile is None and bundle.is_file():
        context.load_verify_locations(cafile=str(bundle))
    return context


def public_json(url: str) -> object:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "api.coingecko.com" or parts.port not in {None, 443}:
        raise ValueError("Public market endpoint is not allowed.")
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=tls_context()))
    request = urllib.request.Request(url, headers={"User-Agent": "FrancisCryptoLab/3 local-simulation", "Accept": "application/json"})
    for attempt in range(2):
        try:
            with opener.open(request, timeout=TIMEOUT) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise RuntimeError("Public response exceeded the 1 MB limit.")
            try:
                return json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Public endpoint returned invalid JSON.") from exc
        except urllib.error.HTTPError as exc:
            exc.close()
            if attempt == 0 and (exc.code == 429 or 500 <= exc.code <= 599):
                time.sleep(.2)
                continue
            raise RuntimeError(f"CoinGecko HTTP {exc.code}; public availability and limits can change.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == 0 and not isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError):
                time.sleep(.2)
                continue
            raise RuntimeError(f"Public HTTPS unavailable: {getattr(exc, 'reason', exc)}") from exc
    raise RuntimeError("Public API retry limit reached.")


def validate_prices(payload: object, *, now: float) -> tuple[list[dict], dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("prices"), list):
        raise ValueError("CoinGecko history has an unexpected response shape.")
    raw = payload["prices"]
    if len(raw) < 2:
        raise ValueError("CoinGecko returned insufficient historical observations.")
    if not isinstance(raw[-1], list) or len(raw[-1]) != 2:
        raise ValueError("CoinGecko trailing partial observation is malformed.")
    # Validate ordering before dropping the provider's trailing partial point.
    try:
        times = [float(row[0]) for row in raw if isinstance(row, list) and len(row) == 2]
        if any(not math.isfinite(ts) for ts in times) or any(b < a for a, b in zip(times, times[1:])):
            raise ValueError()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("CoinGecko historical observations are not in timestamp order.") from exc
    parsed, rejected, outside = {}, 0, 0
    # The final point is the current partial sample. Keep actual timestamps on
    # all other points; do not invent candles or round observed times to hours.
    for row in raw[:-1]:
        try:
            if not isinstance(row, list) or len(row) != 2 or any(isinstance(v, bool) for v in row):
                raise ValueError()
            milliseconds, price = map(float, row)
            if milliseconds == times[-1]:
                outside += 1
                continue
            if not math.isfinite(milliseconds) or not math.isfinite(price) or price <= 0:
                raise ValueError()
            ts = int(milliseconds / 1000)
            if ts <= 0 or ts > now or ts < now - 32 * 24 * HOUR:
                outside += 1
                continue
            if ts in parsed and parsed[ts]["price"] != price:
                raise RuntimeError("Conflicting duplicate historical observations; refresh rejected.")
            parsed[ts] = {"ts": ts, "price": price}
        except (ValueError, TypeError, OverflowError):
            rejected += 1
    points = [parsed[k] for k in sorted(parsed)]
    if not points:
        raise ValueError("No valid completed hourly observations in public history.")
    gaps = sum(max(0, round((b["ts"] - a["ts"]) / HOUR) - 1) for a, b in zip(points, points[1:]))
    return points, {"rejected": rejected, "outside_range": outside, "missing_intervals": gaps,
                    "excluded_partial": 1, "source": SOURCE}


def fetch_history(symbol: str) -> tuple[list[dict], dict]:
    if symbol not in SYMBOLS:
        raise ValueError("Unsupported symbol.")
    url = f"https://api.coingecko.com/api/v3/coins/{SYMBOLS[symbol]}/market_chart?vs_currency=usd&days=30"
    return validate_prices(public_json(url), now=time.time())


def validate_daily_history(payload: object, *, now: float) -> tuple[list[dict], dict]:
    """Validate timestamped UTC daily closes for prospective indicator warm-up.

    CoinGecko's >90-day market chart is sampled daily at 00:00 UTC and has a
    final partial current-day point. That final point is validated then dropped;
    no timestamps are rounded and missing days are never filled.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("prices"), list):
        raise ValueError("CoinGecko daily history has an unexpected response shape.")
    raw = payload["prices"]
    if len(raw) < FORWARD_WARMUP + 2:
        raise ValueError("CoinGecko daily history is too short for the 100-day trend warm-up.")

    def parse_point(row: object) -> tuple[int, float]:
        if not isinstance(row, list) or len(row) != 2 or any(isinstance(v, bool) for v in row):
            raise ValueError()
        milliseconds, price = row
        ms_float, price = float(milliseconds), float(price)
        if (not math.isfinite(ms_float) or not ms_float.is_integer() or int(ms_float) % 1000 != 0
                or not math.isfinite(price) or price <= 0):
            raise ValueError()
        return int(ms_float // 1000), price

    try:
        parsed = [parse_point(row) for row in raw]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("CoinGecko daily history contains malformed or non-finite values.") from exc
    times = [ts for ts, _ in parsed]
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("CoinGecko daily history timestamps must be strictly increasing without duplicates.")
    completed, partial = parsed[:-1], parsed[-1]
    if any(ts % DAY_SECONDS for ts, _ in completed):
        raise ValueError("Completed CoinGecko daily closes must have exact 00:00 UTC timestamps.")
    if any(b[0] - a[0] != DAY_SECONDS for a, b in zip(completed, completed[1:])):
        raise ValueError("CoinGecko daily history has a missing or irregular UTC day; no gap will be filled.")
    last_complete = completed[-1][0]
    if partial[0] <= last_complete or partial[0] >= last_complete + DAY_SECONDS:
        raise ValueError("CoinGecko trailing partial point is outside the latest incomplete UTC day.")
    if partial[0] > now + 5 or completed[-1][0] > now + 5:
        raise ValueError("CoinGecko daily history contains a future timestamp.")
    points = [{"ts": ts, "price": price} for ts, price in completed]
    if len(points) < FORWARD_WARMUP + 1:
        raise ValueError("CoinGecko daily history has too few complete closes after excluding the partial point.")
    return points, {"source": "CoinGecko public daily aggregate USD prices",
        "interval_seconds": DAY_SECONDS, "points": len(points), "first_utc": iso(points[0]["ts"]),
        "last_complete_utc": iso(points[-1]["ts"]), "excluded_partial": 1,
        "warmup_only": True, "warmup_days_required": FORWARD_WARMUP,
        "missing_intervals": 0}


def fetch_daily_history(symbol: str) -> tuple[list[dict], dict]:
    if symbol not in SYMBOLS:
        raise ValueError("Unsupported symbol.")
    url = f"https://api.coingecko.com/api/v3/coins/{SYMBOLS[symbol]}/market_chart?vs_currency=usd&days=365"
    return validate_daily_history(public_json(url), now=time.time())


def validate_quote(quote: dict, now: float, *, future_tolerance: float = 5) -> dict:
    try:
        if any(isinstance(quote[k], bool) for k in ("price", "market_at", "observed_at")):
            raise ValueError()
        price, market_at, observed_at = (float(quote[k]) for k in ("price", "market_at", "observed_at"))
        if not all(math.isfinite(v) for v in (price, market_at, observed_at)) or price <= 0:
            raise ValueError()
        if market_at > observed_at + future_tolerance or observed_at > now + future_tolerance or now - observed_at > QUOTE_MAX_AGE or now - market_at > QUOTE_MAX_AGE:
            raise ValueError()
        return {**quote, "price": price, "market_at": market_at, "observed_at": observed_at}
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Public price is missing, stale, future-dated or malformed; no simulated fill.") from exc


def fetch_quote(symbol: str) -> dict:
    if symbol not in SYMBOLS:
        raise ValueError("Unsupported symbol.")
    requested_at = time.time()
    body = public_json(f"https://api.coingecko.com/api/v3/simple/price?ids={SYMBOLS[symbol]}&vs_currencies=usd&include_last_updated_at=true")
    value = body.get(SYMBOLS[symbol], {}) if isinstance(body, dict) else {}
    return validate_quote({"price": value.get("usd"), "market_at": value.get("last_updated_at"),
                           "observed_at": time.time(), "requested_at": requested_at}, time.time())


def fetch_batch_quotes() -> dict[str, dict]:
    """Fetch both aggregate USD quotes once and reject an invalid partial batch."""
    requested_at = time.time()
    body = public_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_last_updated_at=true")
    observed_at = time.time()
    if not isinstance(body, dict):
        raise ValueError("CoinGecko batch quote has an unexpected response shape.")
    output = {}
    for symbol, coin in SYMBOLS.items():
        item = body.get(coin)
        if not isinstance(item, dict):
            raise ValueError("CoinGecko batch quote omitted one required asset; batch rejected.")
        quote = validate_quote({"price": item.get("usd"), "market_at": item.get("last_updated_at"),
            "requested_at": requested_at, "observed_at": observed_at}, observed_at, future_tolerance=30)
        output[symbol] = quote
    return output


def synthetic_prices(symbol: str, count: int = 720) -> list[dict]:
    if symbol not in SYMBOLS:
        raise ValueError("Unsupported symbol.")
    rng = random.Random(3001 if symbol == "BTC-USD" else 3002)
    start, price = 1735689600, 42_000.0 if symbol == "BTC-USD" else 2_250.0
    points = []
    for i in range(count):
        points.append({"ts": start + i * HOUR, "price": price})
        ret = .0004 * math.sin(i / 9) + (.001 if (i // 90) % 2 == 0 else -.0007) + rng.gauss(0, .011)
        price = max(1.0, price * (1 + ret))
    return points
