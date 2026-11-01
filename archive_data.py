"""Bounded, resumable official Binance spot archive ingestion. No order API."""
from __future__ import annotations

import calendar
import csv
import hashlib
import io
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from market import NoRedirect, tls_context

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "research_data" / "v4"
BAR = 14400
MAX_ZIP, MAX_CSV = 1_000_000, 2_000_000
TERMS_URL = "https://raw.githubusercontent.com/binance/binance-public-data/master/TERMS_AND_CONDITIONS.md"
TERMS_PAGE = "https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md"
EXPECTED_TERMS_SHA = "99c4921dbe023cd68c1a447775707d621453cc78aec6ac0bfe2e6c06a5114ee0"


def iso(ts: int | float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).isoformat().replace("+00:00", "Z")


def utc(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".part")
    temp.write_bytes(data)
    temp.replace(path)


def write_json(path: Path, value: object) -> None:
    atomic(path, (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode())


def protocol() -> tuple[dict, str]:
    data = (ROOT / "research_protocol_v4.json").read_bytes()
    receipt_path = ROOT / "research_protocol_v4.freeze.json"
    if receipt_path.is_file():
        expected_hash = json.loads(receipt_path.read_text())["sha256"]
    else:
        # The source-only distribution omits private selection/freeze receipts
        # but retains the protocol's content checksum.
        expected_hash = (ROOT / "research_protocol_v4.sha256").read_text().strip().split()[0]
    if sha(data) != expected_hash:
        raise ValueError("Frozen research protocol hash differs; no download or evaluation permitted.")
    return json.loads(data), sha(data)


def months(first: str = "2019-10", last: str = "2026-09") -> list[str]:
    year, month = map(int, first.split("-")); output = []
    while f"{year:04}-{month:02}" <= last:
        output.append(f"{year:04}-{month:02}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return output


def identity(symbol: str, month: str) -> tuple[str, str, int, int]:
    if symbol not in {"BTCUSDT", "ETHUSDT"} or month not in months():
        raise ValueError("Archive symbol/month is outside the frozen protocol.")
    year, m = map(int, month.split("-"))
    start = utc(month + "-01")
    end = start + calendar.monthrange(year, m)[1] * 86400
    filename = f"{symbol}-4h-{month}.zip"
    return f"https://data.binance.vision/data/spot/monthly/klines/{symbol}/4h/{filename}", filename, start, end


def https_bytes(url: str, limit: int, *, timeout: int = 10) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    archive_ok = parsed.hostname == "data.binance.vision" and re.fullmatch(r"/data/spot/monthly/klines/(BTCUSDT|ETHUSDT)/4h/\1-4h-\d{4}-\d{2}\.zip(?:\.CHECKSUM)?", parsed.path)
    if (parsed.scheme != "https" or parsed.port not in {None, 443} or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not (archive_ok or url == TERMS_URL)):
        raise ValueError("Only official allowlisted historical research URLs are permitted.")
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=tls_context()))
    request = urllib.request.Request(url, headers={"User-Agent": "FrancisCryptoLab/4-personal-nonproduction-research"})
    for attempt in range(2):
        try:
            with opener.open(request, timeout=timeout) as response:
                data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError("Official archive response exceeded the bounded size limit.")
            return data
        except urllib.error.HTTPError as exc:
            code = exc.code; exc.close()
            if attempt == 0 and (code == 429 or 500 <= code <= 599):
                time.sleep(.5); continue
            raise RuntimeError(f"Official archive HTTP {code}; resume later. No substitute data were used.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == 0:
                time.sleep(.3); continue
            raise RuntimeError(f"Official HTTPS request failed: {getattr(exc, 'reason', exc)}") from exc
    raise RuntimeError("Official archive retry limit reached.")


def verify_checksum(data: bytes, checksum: bytes, filename: str) -> str:
    try:
        fields = checksum.decode("ascii").strip().split()
        if len(fields) != 2 or fields[1].lstrip("*") != filename or not re.fullmatch("[0-9a-fA-F]{64}", fields[0]):
            raise ValueError()
        digest = sha(data)
        if digest != fields[0].lower():
            raise ValueError()
        return digest
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Published SHA256 or archive identity mismatch; archive refused.") from exc


def parse_archive(data: bytes, symbol: str, month: str, *, now: float | None = None) -> tuple[list[dict], dict]:
    _, filename, start, end = identity(symbol, month)
    if len(data) > MAX_ZIP:
        raise ValueError("Compressed archive exceeds 1 MB.")
    current = time.time() if now is None else now
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            expected = filename[:-4] + ".csv"
            if len(entries) != 1 or entries[0].filename != expected:
                raise ValueError("Archive must contain exactly its expected flat CSV member.")
            info = entries[0]
            if info.flag_bits & 1 or info.file_size > MAX_CSV or info.compress_size > MAX_ZIP or info.file_size > max(1, info.compress_size) * 100:
                raise ValueError("Encrypted or oversized archive member refused.")
            csv_bytes = archive.read(info)
    except (zipfile.BadZipFile, RuntimeError, UnicodeError) as exc:
        raise ValueError("Invalid official archive structure or CRC.") from exc
    if len(csv_bytes) > MAX_CSV:
        raise ValueError("Expanded CSV exceeds the safe limit.")
    expected_unit = "us" if month >= "2025-01" else "ms"
    divisor = 1_000_000 if expected_unit == "us" else 1000
    parsed = {}; seen = set(); partial_rows = []
    try:
        reader = csv.reader(io.StringIO(csv_bytes.decode("utf-8")))
        for line, row in enumerate(reader, 1):
            if line > 186 or len(row) != 12:
                raise ValueError("Unexpected monthly CSV row count or 12-column schema.")
            if not row[0].isdigit() or not row[6].isdigit() or not row[8].isdigit():
                raise ValueError("Archive timestamps and trade counts must be integers.")
            raw_open, raw_close, trades = int(row[0]), int(row[6]), int(row[8])
            if raw_open % divisor:
                raise ValueError("Wrong open timestamp unit.")
            ts = raw_open // divisor
            if not start <= ts < end or ts % BAR or raw_close // divisor > current:
                raise ValueError("Future, misaligned or out-of-month bar refused.")
            if ts in seen:
                raise ValueError("Duplicate or conflicting bar timestamp refused.")
            seen.add(ts)
            op, high, low, close, volume = map(float, row[1:6])
            quote, taker_base, taker_quote = float(row[7]), float(row[9]), float(row[10])
            if not all(math.isfinite(x) for x in (op, high, low, close, volume, quote, taker_base, taker_quote)):
                raise ValueError("Non-finite OHLCV data refused.")
            if not (0 < low <= op <= high and low <= close <= high) or min(volume, quote, taker_base, taker_quote) < 0:
                raise ValueError("Invalid OHLC range or negative volume.")
            if taker_base > volume + max(1e-8, volume * 1e-8) or taker_quote > quote + max(1e-6, quote * 1e-8):
                raise ValueError("Taker volume exceeds total volume.")
            duration_units = raw_close - raw_open + 1
            if duration_units <= 0:
                raise ValueError("Open/close duration must be positive.")
            full_bar_units = BAR * divisor
            if duration_units > full_bar_units:
                raise ValueError("Overlong candle close boundary refused.")
            if duration_units < full_bar_units:
                partial_rows.append({"ts": ts, "duration_units": duration_units,
                    "timestamp_unit": expected_unit, "duration_seconds": duration_units / divisor,
                    "reason": "historical candle declared close before the full four-hour boundary; row rejected"})
                continue
            parsed[ts] = {"ts": ts, "open": op, "high": high, "low": low, "close": close,
                          "volume": volume, "quote_volume": quote, "trades": trades,
                          "taker_base": taker_base, "taker_quote": taker_quote}
    except (ValueError, UnicodeError, csv.Error, OverflowError) as exc:
        raise ValueError(f"{symbol} {month} validation failed: {exc}") from exc
    if not parsed:
        raise ValueError("Archive contains no valid bars.")
    expected_slots = set(range(start, end, BAR)); missing = sorted(expected_slots - parsed.keys())
    return [parsed[ts] for ts in sorted(parsed)], {"timestamp_unit": expected_unit, "rows": len(parsed),
        "expected_rows": len(expected_slots), "missing_slots": missing, "zero_volume_bars": sum(x["volume"] == 0 for x in parsed.values()),
        "rejected_rows": len(partial_rows), "duplicate_rows": 0, "partial_rows": len(partial_rows),
        "partial_bar_rejections": partial_rows}


class ArchiveStore:
    def __init__(self, root: Path = DATA_ROOT):
        self.root = Path(root)
        self.protocol, self.protocol_hash = protocol()

    def manifest(self, *, migrate_pre_fit: bool = False) -> dict:
        path = self.root / "dataset-manifest.json"
        if not path.exists():
            total = len(self.protocol["dataset"]["symbols"]) * len(months())
            return {"protocol_sha256": self.protocol_hash, "state": "empty", "archives": {}, "datasets": {}, "completed": 0, "total": total}
        value = json.loads(path.read_text())
        if value.get("protocol_sha256") != self.protocol_hash:
            if not migrate_pre_fit:
                raise ValueError("Cached dataset uses an earlier pre-fit protocol; start an explicit fetch to revalidate and migrate it.")
            self._migrate_pre_fit_manifest(value)
        return value

    def _migrate_pre_fit_manifest(self, manifest: dict) -> None:
        prior_hash = manifest.get("protocol_sha256")
        receipt_path = ROOT / "research_protocol_v4.freeze.json"
        if not receipt_path.is_file():
            raise ValueError("Pre-fit cache migration requires an amendment receipt not included in this source-only package.")
        receipt = json.loads(receipt_path.read_text())
        links = {item.get("prior_sha256"): item.get("new_sha256") for item in receipt.get("pre_fit_amendments", [])
            if item.get("prior_sha256") and item.get("new_sha256")}
        cursor, chain, seen = prior_hash, [], set()
        while cursor != self.protocol_hash and cursor in links and cursor not in seen:
            seen.add(cursor); next_hash = links[cursor]
            chain.append({"prior_sha256": cursor, "new_sha256": next_hash})
            cursor = next_hash
        if cursor != self.protocol_hash or not chain:
            raise ValueError("Cached dataset protocol change is not an approved pre-fit amendment chain.")
        ledger = self.root / "trial-ledger.jsonl"
        study_results = list((self.root / "studies").glob("v4-*.json")) if (self.root / "studies").exists() else []
        if ledger.exists() and ledger.stat().st_size or study_results:
            raise ValueError("Protocol migration is allowed only before any historical outcome study was recorded.")
        verified = 0
        for key, entry in manifest.get("archives", {}).items():
            if entry.get("state") != "verified":
                continue
            try:
                symbol, month = key.split("/", 1)
                url, filename, _, _ = identity(symbol, month)
                archive_path = self.root / "archives" / filename
                checksum_path = archive_path.with_name(filename + ".CHECKSUM")
                if archive_path.is_symlink() or checksum_path.is_symlink() or not archive_path.is_file() or not checksum_path.is_file():
                    raise ValueError("verified archive/checksum pair is missing or symlinked")
                data, checksum = archive_path.read_bytes(), checksum_path.read_bytes()
                if sha(checksum) != entry.get("checksum_sha256"):
                    raise ValueError("cached published checksum bytes differ from the verified manifest")
                digest = verify_checksum(data, checksum, filename)
                if digest != entry.get("archive_sha256"):
                    raise ValueError("cached archive bytes differ from the verified manifest")
                _, info = parse_archive(data, symbol, month)
                entry.update(info)
                entry["pre_fit_migrated_at_utc"] = iso()
                verified += 1
            except Exception as exc:
                raise ValueError(f"Pre-fit manifest migration failed for {key}; cached bytes were preserved: {str(exc)[:250]}") from exc
        manifest.update({"protocol_sha256": self.protocol_hash, "state": "partial", "datasets": {},
            "completed": verified, "total": len(self.protocol["dataset"]["symbols"]) * len(months()),
            "error": "Pre-fit clarification applied; archive hashes were revalidated. Resume fetch to complete normalized datasets.",
            "pause_reason": None, "failed_archives": [key for key, value in manifest.get("archives", {}).items()
                if value.get("state") != "verified"],
            "protocol_migrations": manifest.get("protocol_migrations", []) + [{
                "from_sha256": prior_hash, "to_sha256": self.protocol_hash, "approved_pre_fit_chain": chain,
                "at_utc": iso(), "archives_revalidated": verified, "outcome_studies_present": False}]})
        self.save_manifest(manifest)

    def save_manifest(self, manifest: dict):
        manifest["updated_at_utc"] = iso()
        write_json(self.root / "dataset-manifest.json", manifest)

    def terms(self, *, refresh: bool = False) -> dict:
        path = self.root / "source-terms.md"
        probe_copy = ROOT / "research_data" / "v4" / "probe" / "TERMS_AND_CONDITIONS.md"
        provenance = "fresh_official_tls_check"
        verified_at = iso()
        if refresh:
            raw = https_bytes(TERMS_URL, 64000)
        else:
            provenance = "cached_reviewed_terms_not_rechecked"
            raw = path.read_bytes() if path.exists() else probe_copy.read_bytes() if probe_copy.exists() else b""
            verified_at = None
            receipt_path = ROOT / "research_data" / "v4" / "probe" / "probe-receipt.json"
            if receipt_path.exists():
                for receipt in json.loads(receipt_path.read_text()):
                    if receipt.get("url") == TERMS_URL and receipt.get("sha256") == EXPECTED_TERMS_SHA:
                        verified_at = receipt.get("fetched_at_utc")
                        break
        if sha(raw) != EXPECTED_TERMS_SHA:
            raise ValueError("Dataset terms are unavailable or changed from the reviewed version; research download is blocked pending review.")
        atomic(path, raw)
        return {"attribution": "Binance Vision official spot datasets", "license": "CC BY-NC-SA 4.0",
                "terms_url": TERMS_PAGE, "raw_terms_url": TERMS_URL, "terms_sha256": sha(raw),
                "terms_last_updated": "2026-08-26", "verified_at_utc": verified_at,
                "provenance": provenance, "current_source_checked": bool(refresh),
                "scope": "Personal non-production historical research only. Derived charts/models/results retain attribution and CC BY-NC-SA 4.0. Not commercial/live proprietary execution; code is separately scoped."}

    def fetch(self, progress=None, stop=None) -> dict:
        manifest = self.manifest(migrate_pre_fit=True)
        total = len(self.protocol["dataset"]["symbols"]) * len(months())
        manifest["total"] = total
        try:
            terms_record = self.terms(refresh=True)
        except Exception as exc:
            manifest.update({"state": "paused", "error": f"Current official terms could not be verified: {str(exc)[:400]}", "pause_reason": "terms_check"})
            self.save_manifest(manifest)
            return manifest
        manifest.update({"state": "downloading", "error": None, "pause_reason": None, "terms": terms_record})
        self.save_manifest(manifest)
        errors = []; network_blocked = False; paused_error = None
        for symbol in self.protocol["dataset"]["symbols"]:
            for month in months():
                if network_blocked:
                    break
                if stop and stop.is_set():
                    manifest["state"] = "interrupted"; self.save_manifest(manifest); return manifest
                url, filename, _, _ = identity(symbol, month)
                path = self.root / "archives" / filename; check_path = path.with_name(filename + ".CHECKSUM")
                key = f"{symbol}/{month}"
                prior = manifest["archives"].get(key, {})
                repair = False
                try:
                    cached = path.exists() and check_path.exists() and prior.get("state") == "verified"
                    if cached:
                        checksum, data = check_path.read_bytes(), path.read_bytes()
                        try:
                            digest = verify_checksum(data, checksum, filename)
                            if digest != prior.get("archive_sha256") or sha(checksum) != prior.get("checksum_sha256"):
                                raise ValueError("Cached archive changed since its prior verified manifest entry.")
                        except ValueError:
                            repair = True
                    if not cached or repair:
                        if repair:
                            quarantine = self.root / "quarantine"
                            quarantine.mkdir(parents=True, exist_ok=True)
                            stamp = str(time.time_ns())
                            if path.exists(): os.replace(path, quarantine / f"{filename}.{stamp}.corrupt")
                            if check_path.exists(): os.replace(check_path, quarantine / f"{filename}.{stamp}.CHECKSUM.corrupt")
                        elif path.exists() or check_path.exists():
                            quarantine = self.root / "quarantine"
                            quarantine.mkdir(parents=True, exist_ok=True)
                            stamp = str(time.time_ns())
                            if path.exists(): os.replace(path, quarantine / f"{filename}.{stamp}.unverified")
                            if check_path.exists(): os.replace(check_path, quarantine / f"{filename}.{stamp}.CHECKSUM.unverified")
                        checksum = https_bytes(url + ".CHECKSUM", 4096)
                        data = https_bytes(url, MAX_ZIP)
                        digest = verify_checksum(data, checksum, filename)
                        previous_digest = prior.get("archive_sha256")
                        if prior.get("state") in {"verified", "error"} and previous_digest and digest != previous_digest:
                            raise ValueError("Official archive changed from the previously verified immutable snapshot; refusing replacement.")
                    bars, info = parse_archive(data, symbol, month)
                    if not cached or repair:
                        atomic(path, data); atomic(check_path, checksum)
                        time.sleep(.12)
                    previous = manifest["archives"].get(key, {})
                    manifest["archives"][key] = {"state": "verified", "url": url, "checksum_url": url + ".CHECKSUM",
                        "archive_sha256": digest, "checksum_sha256": sha(checksum), "bytes": len(data),
                        "fetched_at_utc": previous.get("fetched_at_utc") or iso(),
                        "last_verified_at_utc": iso(), "repair_refetch": repair, **info}
                except Exception as exc:
                    errors.append(key)
                    manifest["archives"][key] = {**prior, "state": "error", "url": url,
                        "error": str(exc)[:400], "checked_at_utc": iso()}
                    message = str(exc)
                    network_blocked = isinstance(exc, RuntimeError) and (
                        "Official HTTPS request failed:" in message or "retry limit" in message
                        or any(f"HTTP {code}" in message for code in (403, 429, 500, 502, 503, 504)))
                    if network_blocked:
                        paused_error = f"Batch paused at {key}: {message[:300]}"
                manifest["completed"] = sum(x["state"] == "verified" for x in manifest["archives"].values())
                self.save_manifest(manifest)
                if progress:
                    progress({"phase": "download", "completed": manifest["completed"], "total": total, "current": key,
                              "error": manifest["archives"][key].get("error")})
        if errors:
            manifest.update({"state": "paused" if network_blocked else "partial",
                "error": paused_error or "Missing/unverified monthly archives block evaluation.",
                "pause_reason": "upstream_or_network" if network_blocked else None, "failed_archives": errors})
        elif manifest.get("completed") != total:
            manifest.update({"state": "partial", "error": "Archive set is incomplete; evaluation remains blocked.",
                "pause_reason": None, "failed_archives": []})
        else:
            for symbol in self.protocol["dataset"]["symbols"]:
                bars = []
                for month in months():
                    _, filename, _, _ = identity(symbol, month)
                    data = (self.root / "archives" / filename).read_bytes()
                    verify_checksum(data, (self.root / "archives" / (filename + ".CHECKSUM")).read_bytes(), filename)
                    part, _ = parse_archive(data, symbol, month); bars.extend(part)
                if len({x["ts"] for x in bars}) != len(bars):
                    raise ValueError("Duplicate timestamp across verified monthly archives.")
                normalized = (json.dumps(bars, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
                digest = sha(normalized); output = self.root / "datasets" / f"{symbol}-{digest}.json"
                if not output.exists(): atomic(output, normalized)
                slots = set(range(utc("2019-10-01"), utc("2026-10-01"), BAR))
                missing = sorted(slots - {b["ts"] for b in bars})
                partials = [row for key, archive in manifest["archives"].items()
                    if key.startswith(symbol + "/") for row in archive.get("partial_bar_rejections", [])]
                manifest["datasets"][symbol] = {"symbol": symbol, "quote_asset": "USDT", "bar_interval": "4h",
                    "canonical_sha256": digest, "file": str(output.relative_to(self.root)), "rows": len(bars),
                    "expected_rows": len(slots), "rejected_partial_rows": len(partials),
                    "first_open_utc": iso(bars[0]["ts"]), "last_close_exclusive_utc": iso(bars[-1]["ts"] + BAR),
                    "missing_slots": missing, "partial_bar_rejections": partials,
                    "zero_volume_bars": sum(b["volume"] == 0 for b in bars)}
            manifest.update({"state": "verified", "error": None, "failed_archives": [], "verified_at_utc": iso()})
        self.save_manifest(manifest)
        return manifest

    def load(self, symbol: str) -> tuple[list[dict], dict]:
        if symbol not in {"BTCUSDT", "ETHUSDT"}: raise ValueError("Unsupported historical symbol.")
        manifest = self.manifest()
        total = len(self.protocol["dataset"]["symbols"]) * len(months())
        if manifest["state"] != "verified" or manifest["completed"] != total or manifest.get("total") != total:
            raise ValueError(f"All {total} archives must verify before historical evaluation.")
        terms = manifest.get("terms", {})
        if terms.get("terms_sha256") != EXPECTED_TERMS_SHA:
            raise ValueError("Verified archive manifest lacks the reviewed dataset license record.")
        meta = manifest["datasets"][symbol]
        path = (self.root / meta["file"]).resolve()
        if self.root.resolve() not in path.parents: raise ValueError("Unsafe dataset cache path.")
        raw = path.read_bytes()
        if sha(raw) != meta["canonical_sha256"]: raise ValueError("Normalized dataset checksum mismatch.")
        return json.loads(raw), meta
