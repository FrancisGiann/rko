"""Isolated, opt-in, prospective aggregate-USD paper cohort (no exchange orders)."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import sqlite3
import sys
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
from pathlib import Path

import market
import walkforward_v5 as risk

STRATEGIES = ("cash", "passive", "momentum_7d", "sma20_100", "close_breakout_20_10")
SYMBOLS = ("BTC-USD", "ETH-USD")
FEE_BPS, SLIP_BPS = 25.0, 10.0
DRAWNDOWN_LIMIT = 0.10
POLL_SECONDS = 300
DAILY_REFRESH_AFTER_UTC = 600
ENTRY_EXPIRY_SECONDS = 900
MAX_QUOTE_AGE = 120
FUTURE_TOLERANCE = 30
SOURCE = "CoinGecko public aggregate USD prices"
PROTECTION_SCHEMA = "v5.2-net-liquidation-remaining-basis"


def validate_protection_threshold(value: object, *, name: str, maximum: float) -> float | None:
    """Validate one optional percentage without accepting JSON booleans/strings."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be blank/OFF or a JSON number from 0.1 to {maximum:g} in 0.1-point increments.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number.") from exc
    if not number.is_finite():
        raise ValueError(f"{name} must be a finite number.")
    if number < Decimal("0.1") or number > Decimal(str(maximum)):
        raise ValueError(f"{name} must be from 0.1 to {maximum:g} percent.")
    if (number * 10) != (number * 10).to_integral_value():
        raise ValueError(f"{name} must use 0.1 percentage-point increments.")
    return float(number)


def net_position_return_pct(qty: float, basis: float, raw_price: float,
                            fee_bps: float = FEE_BPS, slip_bps: float = SLIP_BPS) -> float | None:
    """Mark the remaining long lot after estimated sell slippage and commission."""
    values = (qty, basis, raw_price, fee_bps, slip_bps)
    if not all(math.isfinite(float(x)) for x in values) or qty <= 0 or basis <= 0 or raw_price <= 0:
        return None
    sell_value = qty * raw_price * (1 - slip_bps / 10_000) * (1 - fee_bps / 10_000)
    return (sell_value / basis - 1) * 100


def estimated_protection_price(qty: float, basis: float, threshold_pct: float,
                               *, take_profit: bool) -> float | None:
    """Return the raw quote that reaches the selected net liquidation threshold."""
    if not all(math.isfinite(float(x)) for x in (qty, basis, threshold_pct)) or qty <= 0 or basis <= 0:
        return None
    multiplier = 1 + threshold_pct / 100 if take_profit else 1 - threshold_pct / 100
    if multiplier <= 0:
        return None
    sell_factor = (1 - SLIP_BPS / 10_000) * (1 - FEE_BPS / 10_000)
    return basis * multiplier / (qty * sell_factor)


def _reaches_protection_threshold(value: float, threshold: float, *, take_profit: bool) -> bool:
    boundary = threshold if take_profit else -threshold
    crossed = value >= boundary if take_profit else value <= boundary
    # Binary floats may represent a quote derived from the exact threshold
    # boundary a few ulps to the wrong side. This tolerance is 1e-10 percentage
    # points; it cannot materially advance a trigger but makes equality inclusive.
    return crossed or math.isclose(value, boundary, rel_tol=0.0, abs_tol=1e-10)


EXIT_PRIORITY = {"take_profit": 20, "stop_loss": 21, "account_drawdown": 30,
    "manual_close": 40, "session_close": 50}


def utc_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def validate_batch(quotes: dict, now: float) -> dict:
    """Validate both assets before allowing any account in the cohort to mutate."""
    if not isinstance(quotes, dict) or set(quotes) != set(SYMBOLS):
        raise ValueError("A complete BTC/ETH quote batch is required; no partial comparison round is allowed.")
    output = {}
    common_request = None
    common_receipt = None
    for symbol in SYMBOLS:
        quote = quotes[symbol]
        try:
            price = float(quote["price"])
            market_at = float(quote["market_at"])
            requested = float(quote["requested_at"])
            received = float(quote["observed_at"])
            vals = (price, market_at, requested, received)
            if any(isinstance(quote[k], bool) for k in ("price", "market_at", "requested_at", "observed_at")):
                raise ValueError()
            if not all(math.isfinite(x) for x in vals) or price <= 0 or requested > received:
                raise ValueError()
            if now - received > MAX_QUOTE_AGE or now - market_at > MAX_QUOTE_AGE:
                raise ValueError()
            if market_at > received + FUTURE_TOLERANCE or received > now + FUTURE_TOLERANCE:
                raise ValueError()
            output[symbol] = {**quote, "price": price, "market_at": market_at,
                "requested_at": requested, "observed_at": received}
            if common_request is None:
                common_request, common_receipt = requested, received
            elif requested != common_request or received != common_receipt:
                raise ValueError("BTC/ETH quote timestamps do not belong to one atomic request/receipt batch.")
        except ValueError as exc:
            if "atomic request/receipt" in str(exc):
                raise
            raise ValueError(f"{symbol} quote is stale, future-dated, or malformed; the entire batch was rejected.") from exc
        except (KeyError, TypeError, OverflowError) as exc:
            raise ValueError(f"{symbol} quote is stale, future-dated, or malformed; the entire batch was rejected.") from exc
    return output


def daily_signal(points: list[dict], breakout_was_active: bool) -> tuple[dict[str, float | None], bool | None]:
    """Create targets from the new completed 00:00 UTC price observation and past points."""
    prices = [float(x["price"]) for x in points]
    if len(prices) < 101:
        return {}, None
    if any(not math.isfinite(x) or x <= 0 for x in prices[-101:]):
        return {}, None
    returns = [prices[-i] / prices[-i - 1] - 1 for i in range(1, 21)]
    vol = risk._std_population(returns)
    weight = risk.active_weight(vol)
    current = prices[-1]
    momentum = current / prices[-8] - 1
    previous20 = prices[-21:-1]
    previous10 = prices[-11:-1]
    fast = sum(prices[-20:]) / 20
    slow = sum(prices[-100:]) / 100
    trend = current > slow and fast > slow
    enter = current > max(previous20)
    exit_ = current < min(previous10)
    breakout_active = breakout_was_active
    if enter:
        breakout_active = True
    elif exit_:
        breakout_active = False
    return {
        "cash": 0.0,
        "passive": weight,
        "momentum_7d": weight if momentum > 0 else 0.0,
        "sma20_100": weight if trend else 0.0,
        "close_breakout_20_10": weight if breakout_active else 0.0,
    }, breakout_active


class ForwardCohort:
    """A single-writer service with its own SQLite file and an explicit lifecycle."""

    def __init__(self, path: Path, *, fetch_daily=None, fetch_quotes=None,
                 clock=time.time, poll_seconds: int = POLL_SECONDS,
                 _monotonic=time.monotonic, _start_watchdog: bool = True):
        self.path = Path(path)
        if not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
        self._new_file = not self.path.exists()
        self.fetch_daily = fetch_daily or market.fetch_daily_history
        self.fetch_quotes = fetch_quotes or market.fetch_batch_quotes
        self.clock = clock
        self._monotonic = _monotonic
        self._clock_lock = threading.Lock()
        self._clock_anchor_wall = float(self.clock())
        self._clock_anchor_mono = float(self._monotonic())
        self._last_wall_observed = self._clock_anchor_wall
        self.poll_seconds = poll_seconds
        # Private QA launcher may set this to tag all locally fabricated
        # prices/states; production server never sets the attribute.
        self.qa_fixture_label = None
        self.lock = threading.RLock()
        self.worker_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.watchdog_stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.watchdog: threading.Thread | None = None
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cohorts(
                  id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at REAL NOT NULL,
                  start_at REAL, resume_at REAL, config_json TEXT NOT NULL, config_hash TEXT,
                  baseline_json TEXT, last_daily_ts_json TEXT, last_history_check_day TEXT,
                  last_quote_request REAL, last_quote_receipt REAL, latest_quote_json TEXT,
                  warmup_meta_json TEXT, reason TEXT, error TEXT, forward_days REAL NOT NULL DEFAULT 0,
                  reset_at REAL, launch_generation INTEGER NOT NULL DEFAULT 0,
                  last_history_check_at REAL, history_check_count INTEGER NOT NULL DEFAULT 0,
                  session_id TEXT, session_started_at REAL, session_deadline_at REAL,
                  session_duration_minutes INTEGER, expiry_policy TEXT,
                  session_status TEXT NOT NULL DEFAULT 'not_started', entry_disabled INTEGER NOT NULL DEFAULT 0,
                  clock_high_water REAL, session_decided_at REAL, terminal_reason TEXT,
                  close_retry_after REAL, session_config_json TEXT, protection_schema_version TEXT);
                CREATE TABLE IF NOT EXISTS warmup_prices(
                  cohort_id TEXT NOT NULL, symbol TEXT NOT NULL, ts INTEGER NOT NULL,
                  price REAL NOT NULL, fetched_at REAL NOT NULL, warmup_only INTEGER NOT NULL CHECK(warmup_only=1),
                  PRIMARY KEY(cohort_id,symbol,ts));
                CREATE TABLE IF NOT EXISTS daily_observations(
                  cohort_id TEXT NOT NULL, symbol TEXT NOT NULL, ts INTEGER NOT NULL,
                  price REAL NOT NULL, requested_at REAL NOT NULL, received_at REAL NOT NULL,
                  PRIMARY KEY(cohort_id,symbol,ts));
                CREATE TABLE IF NOT EXISTS accounts(
                  cohort_id TEXT NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
                  initial REAL NOT NULL, cash REAL NOT NULL, qty REAL NOT NULL DEFAULT 0,
                  basis REAL NOT NULL DEFAULT 0, peak_nlv REAL NOT NULL, halted INTEGER NOT NULL DEFAULT 0,
                  exit_requested INTEGER NOT NULL DEFAULT 0, exit_decided_at REAL,
                  signal_active INTEGER NOT NULL DEFAULT 0, last_price REAL, last_market_at REAL,
                  last_receipt_at REAL, fees REAL NOT NULL DEFAULT 0, slippage REAL NOT NULL DEFAULT 0,
                  realized_pnl REAL NOT NULL DEFAULT 0, max_drawdown REAL NOT NULL DEFAULT 0,
                  updated_at REAL NOT NULL, episode_id TEXT, position_opened_at REAL,
                  basis_updated_at REAL, basis_updated_receipt_at REAL, basis_updated_market_at REAL,
                  position_tp_pct REAL, position_sl_pct REAL, cooldown_after_at REAL, cooldown_after_receipt_at REAL,
                  exit_reason TEXT, exit_priority INTEGER NOT NULL DEFAULT 0,
                  protection_trigger_reason TEXT, protection_triggered_at REAL,
                  protection_trigger_price REAL, protection_trigger_market_at REAL,
                  protection_trigger_return_pct REAL, protection_trigger_basis REAL,
                  protection_trigger_threshold_pct REAL,
                  PRIMARY KEY(cohort_id,symbol,strategy));
                CREATE TABLE IF NOT EXISTS decisions(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, cohort_id TEXT NOT NULL, symbol TEXT NOT NULL,
                  strategy TEXT NOT NULL, signal_ts INTEGER NOT NULL, created_at REAL NOT NULL,
                  due_at REAL NOT NULL, expires_at REAL NOT NULL, target_weight REAL,
                  status TEXT NOT NULL, reason TEXT NOT NULL, source_observation_ts INTEGER,
                  UNIQUE(cohort_id,symbol,strategy,signal_ts));
                CREATE TABLE IF NOT EXISTS fills(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, cohort_id TEXT NOT NULL, symbol TEXT NOT NULL,
                  strategy TEXT NOT NULL, signal_ts INTEGER NOT NULL, request_at REAL NOT NULL,
                  receipt_at REAL NOT NULL, market_at REAL NOT NULL, side TEXT NOT NULL,
                  qty REAL NOT NULL, raw_price REAL NOT NULL, fill_price REAL NOT NULL,
                  fee REAL NOT NULL, slippage REAL NOT NULL, realized_pnl REAL NOT NULL,
                  reason TEXT NOT NULL, episode_id TEXT, applied_at REAL,
                  UNIQUE(cohort_id,symbol,strategy,signal_ts,side));
                CREATE TABLE IF NOT EXISTS position_episodes(
                  id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
                  opened_at REAL NOT NULL, closed_at REAL, opening_market_at REAL NOT NULL,
                  basis_updated_at REAL, basis_updated_receipt_at REAL NOT NULL, basis_updated_market_at REAL NOT NULL,
                  opening_basis REAL NOT NULL, remaining_basis REAL NOT NULL,
                  opened_qty REAL NOT NULL, current_qty REAL NOT NULL,
                  take_profit_pct REAL, stop_loss_pct REAL, realized_pnl REAL NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'open', close_reason TEXT, pending_reason TEXT,
                  protection_trigger_reason TEXT, close_receipt_at REAL,
                  protection_triggered_at REAL, protection_trigger_price REAL,
                  protection_trigger_return_pct REAL, protection_trigger_basis REAL,
                  protection_trigger_threshold_pct REAL);
                CREATE INDEX IF NOT EXISTS episode_history_idx ON position_episodes(cohort_id,symbol,strategy,opened_at);
                CREATE TABLE IF NOT EXISTS protection_events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, cohort_id TEXT NOT NULL, episode_id TEXT,
                  symbol TEXT NOT NULL, strategy TEXT NOT NULL, event_type TEXT NOT NULL,
                  reason TEXT NOT NULL, priority INTEGER NOT NULL, decided_at REAL NOT NULL,
                  market_at REAL, request_at REAL, receipt_at REAL, price REAL,
                  net_return_pct REAL, remaining_basis REAL, threshold_pct REAL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS marks(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, cohort_id TEXT NOT NULL, symbol TEXT NOT NULL,
                  strategy TEXT NOT NULL, market_at REAL NOT NULL, request_at REAL NOT NULL,
                  receipt_at REAL NOT NULL, price REAL NOT NULL, equity REAL NOT NULL,
                  net_liquidation REAL NOT NULL, drawdown REAL NOT NULL, exposure REAL NOT NULL,
                  UNIQUE(cohort_id,symbol,strategy,receipt_at));
                CREATE TABLE IF NOT EXISTS quote_batches(
                  cohort_id TEXT NOT NULL, request_at REAL NOT NULL, receipt_at REAL NOT NULL,
                  payload_hash TEXT NOT NULL, PRIMARY KEY(cohort_id,request_at));
            """)
            # Small additive migration for a persisted V5 cohort created by an
            # earlier local build. Generation tokens prevent a blocked fetch
            # from an old worker from initializing or pausing a resumed cohort.
            columns = {x[1] for x in db.execute("PRAGMA table_info(cohorts)")}
            if "launch_generation" not in columns:
                db.execute("ALTER TABLE cohorts ADD COLUMN launch_generation INTEGER NOT NULL DEFAULT 0")
            if "last_history_check_at" not in columns:
                db.execute("ALTER TABLE cohorts ADD COLUMN last_history_check_at REAL")
            if "history_check_count" not in columns:
                db.execute("ALTER TABLE cohorts ADD COLUMN history_check_count INTEGER NOT NULL DEFAULT 0")
            session_columns = {
                "session_id": "TEXT", "session_started_at": "REAL", "session_deadline_at": "REAL",
                "session_duration_minutes": "INTEGER", "expiry_policy": "TEXT",
                "session_status": "TEXT NOT NULL DEFAULT 'not_started'",
                "entry_disabled": "INTEGER NOT NULL DEFAULT 0", "clock_high_water": "REAL",
                "session_decided_at": "REAL", "terminal_reason": "TEXT", "close_retry_after": "REAL",
                "session_config_json": "TEXT", "protection_schema_version": "TEXT",
            }
            for name, declaration in session_columns.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE cohorts ADD COLUMN {name} {declaration}")
            account_columns = {x[1] for x in db.execute("PRAGMA table_info(accounts)")}
            account_migrations = {
                "episode_id": "TEXT", "position_opened_at": "REAL",
                "basis_updated_at": "REAL", "basis_updated_receipt_at": "REAL", "basis_updated_market_at": "REAL",
                "position_tp_pct": "REAL", "position_sl_pct": "REAL",
                "cooldown_after_at": "REAL", "cooldown_after_receipt_at": "REAL", "exit_reason": "TEXT",
                "exit_priority": "INTEGER NOT NULL DEFAULT 0",
                "protection_trigger_reason": "TEXT", "protection_triggered_at": "REAL",
                "protection_trigger_price": "REAL", "protection_trigger_market_at": "REAL",
                "protection_trigger_return_pct": "REAL", "protection_trigger_basis": "REAL",
                "protection_trigger_threshold_pct": "REAL",
            }
            for name, declaration in account_migrations.items():
                if name not in account_columns:
                    db.execute(f"ALTER TABLE accounts ADD COLUMN {name} {declaration}")
            fill_columns = {x[1] for x in db.execute("PRAGMA table_info(fills)")}
            if "episode_id" not in fill_columns:
                db.execute("ALTER TABLE fills ADD COLUMN episode_id TEXT")
            if "applied_at" not in fill_columns:
                db.execute("ALTER TABLE fills ADD COLUMN applied_at REAL")
            episode_columns = {x[1] for x in db.execute("PRAGMA table_info(position_episodes)")}
            for name, declaration in {
                "basis_updated_at": "REAL", "close_receipt_at": "REAL",
                "protection_triggered_at": "REAL", "protection_trigger_price": "REAL",
                "protection_trigger_return_pct": "REAL", "protection_trigger_basis": "REAL",
                "protection_trigger_threshold_pct": "REAL",
                "pending_reason": "TEXT", "protection_trigger_reason": "TEXT",
            }.items():
                if name not in episode_columns:
                    db.execute(f"ALTER TABLE position_episodes ADD COLUMN {name} {declaration}")
            # Earlier V5.2 builds used close_reason to mean both a pending latch
            # and a completed SELL. Migrate that ambiguity without changing any
            # fill or realized amount: final reason remains empty until a fill.
            db.execute("UPDATE position_episodes SET pending_reason=coalesce(pending_reason,(SELECT a.exit_reason FROM accounts a WHERE a.cohort_id=position_episodes.cohort_id AND a.symbol=position_episodes.symbol AND a.strategy=position_episodes.strategy AND a.episode_id=position_episodes.id),close_reason),close_reason=NULL WHERE status='exit_pending'")
            # The first recorded protective decision is authoritative for
            # older episodes whose later final close reason overwrote the
            # original trigger. Never infer a trigger from realized P&L.
            for episode in db.execute("SELECT id,protection_trigger_reason,protection_triggered_at,protection_trigger_price,protection_trigger_return_pct,protection_trigger_basis,protection_trigger_threshold_pct,close_reason FROM position_episodes").fetchall():
                if episode["protection_trigger_reason"] is None:
                    event = db.execute("SELECT reason,decided_at,price,net_return_pct,remaining_basis,threshold_pct FROM protection_events WHERE episode_id=? AND event_type IN ('TRIGGER','OVERRIDE') AND reason IN ('take_profit','stop_loss') ORDER BY id LIMIT 1", (episode["id"],)).fetchone()
                    if event:
                        db.execute("UPDATE position_episodes SET protection_trigger_reason=?,protection_triggered_at=coalesce(protection_triggered_at,?),protection_trigger_price=coalesce(protection_trigger_price,?),protection_trigger_return_pct=coalesce(protection_trigger_return_pct,?),protection_trigger_basis=coalesce(protection_trigger_basis,?),protection_trigger_threshold_pct=coalesce(protection_trigger_threshold_pct,?) WHERE id=?",
                            (event["reason"],event["decided_at"],event["price"],event["net_return_pct"],event["remaining_basis"],event["threshold_pct"],episode["id"]))
            row = db.execute("SELECT * FROM cohorts ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
            if row is None:
                config = self._config()
                now = float(self.clock())
                db.execute("INSERT INTO cohorts(id,state,created_at,config_json,session_status,clock_high_water,protection_schema_version) VALUES(?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), "not_started", now, json.dumps(config, sort_keys=True), "not_started", now, PROTECTION_SCHEMA))
            else:
                now = float(self.clock())
                current = db.execute("SELECT * FROM cohorts WHERE id=?", (row["id"],)).fetchone()
                if current["protection_schema_version"] is None:
                    history_count = sum(db.execute(f"SELECT count(*) FROM {table} WHERE cohort_id=?", (row["id"],)).fetchone()[0]
                        for table in ("accounts", "decisions", "fills", "marks", "daily_observations", "warmup_prices"))
                    if current["session_started_at"] is None and current["start_at"] is None and history_count == 0:
                        # An empty V5.1 cohort can safely adopt the explicit V5.2
                        # protection schema because it has no session or history.
                        db.execute("UPDATE cohorts SET config_json=?,protection_schema_version=?,session_config_json=NULL WHERE id=?",
                            (json.dumps(self._config(), sort_keys=True), PROTECTION_SCHEMA, row["id"]))
                    elif current["session_started_at"] is not None or current["start_at"] is not None or history_count:
                        # Preserve any started V5.1 session, timer, fills and
                        # holdings, but never retrofit new protection settings.
                        # Users may close existing holdings from fresh quotes;
                        # an explicit archive/reset creates the next V5.2 cohort.
                        next_state = "paused" if current["state"] in {"starting", "running"} else current["state"]
                        next_session = "paused" if current["session_status"] == "active" else current["session_status"]
                        db.execute("UPDATE cohorts SET protection_schema_version='legacy-v5.1',entry_disabled=1,state=?,session_status=?,reason=?,terminal_reason=coalesce(terminal_reason,?),error=NULL,launch_generation=launch_generation+1 WHERE id=?",
                            (next_state, next_session,
                             "Legacy V5.1 settings are frozen. New entries and resume are disabled; existing holdings and history are preserved for fresh-quote closure. Archive/reset after closing to choose V5.2 protection settings.",
                             "Legacy V5.1 protection settings were not inferred or applied.", row["id"]))
                        db.execute("UPDATE decisions SET status='cancelled',reason='Legacy V5.1 migration; pending actions were cancelled. Existing holdings are preserved for fresh-quote closure.' WHERE cohort_id=? AND status='pending'", (row["id"],))
                # V5 cohorts had no timer. Preserve every account and ledger row,
                # but fail closed for new entries; an explicit fresh-quote exit
                # remains available for any existing paper holding.
                if row["session_started_at"] is None and row["start_at"] is not None:
                    db.execute("UPDATE cohorts SET session_status='legacy_untimed',entry_disabled=1,terminal_reason=?,clock_high_water=max(coalesce(clock_high_water,0),?) WHERE id=?",
                        ("Migrated untimed V5 cohort; entries disabled. Existing holdings are preserved for fresh-quote closure.", now, row["id"]))
                    db.execute("UPDATE decisions SET status='cancelled',reason='Migrated untimed V5 cohort; new entries disabled.' WHERE cohort_id=? AND status='pending'", (row["id"],))
                current = db.execute("SELECT * FROM cohorts WHERE id=?", (row["id"],)).fetchone()
                if current["state"] in {"starting", "running", "exiting"}:
                    clock_close = current["session_status"] == "clock_suspect" and current["expiry_policy"] == "CLOSE" and db.execute(
                        "SELECT count(*) FROM accounts WHERE cohort_id=? AND (qty>1e-12 OR exit_requested=1)", (row["id"],)).fetchone()[0] > 0
                    timed_close = current["session_status"] in {"expired_pending_close", "stopped_pending_close"} or clock_close
                    pending_safety = db.execute("SELECT count(*) FROM accounts WHERE cohort_id=? AND exit_requested=1 AND exit_priority BETWEEN ? AND ?",
                        (row["id"], EXIT_PRIORITY["take_profit"], EXIT_PRIORITY["account_drawdown"])).fetchone()[0] > 0
                    if timed_close:
                        db.execute("UPDATE cohorts SET state='exiting',reason=?,error=NULL,launch_generation=launch_generation+1 WHERE id=?",
                            ("Server restarted during an authorized close-only session; waiting for a fresh post-decision quote.", row["id"]))
                    elif pending_safety:
                        db.execute("UPDATE cohorts SET state='protection_pending',session_status=CASE WHEN session_status='active' THEN 'paused' ELSE session_status END,close_retry_after=?,reason=?,error=NULL,launch_generation=launch_generation+1 WHERE id=?",
                            (now,"A V5.2 protection close was already latched before restart. It remains pending; retrying with fresh quotes only and no signal processing.",row["id"]))
                        db.execute("UPDATE decisions SET status='cancelled',reason='Server restarted with a protective close pending; no signal catch-up.' WHERE cohort_id=? AND status='pending'", (row["id"],))
                    else:
                        db.execute("UPDATE cohorts SET state='paused',session_status=CASE WHEN session_status='active' THEN 'paused' ELSE session_status END,reason=?,error=NULL,launch_generation=launch_generation+1 WHERE id=?",
                            ("Server restarted; pending scheduled entries were cancelled. Resume explicitly; the original session deadline does not change.", row["id"]))
                        if current["session_status"] in {"expired_exit_only", "stopped_exit_only"}:
                            self._cancel_pending_entries_keep_reductions(db,row["id"],
                                "Server restarted; entries/increases will not catch up. A pending reduction remains eligible for a fresh quote before expiry.")
                        else:
                            db.execute("UPDATE decisions SET status='cancelled',reason='Server restarted; no catch-up.' WHERE cohort_id=? AND status='pending'", (row["id"],))
        self._status_error = None
        if self._new_file and self.path.exists():
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self._advance_session_clock()
        if _start_watchdog:
            self.watchdog = threading.Thread(target=self._watchdog_loop, name="forward-session-watchdog", daemon=True)
            self.watchdog.start()
            self._watchdog_once()

    @staticmethod
    def _config() -> dict:
        if getattr(sys, "frozen", False):
            try:
                from desktop_build_info import SOURCE_SHA256
                implementation = dict(SOURCE_SHA256)
            except (ImportError, TypeError, ValueError) as exc:
                raise RuntimeError("Frozen build is missing its verified trading-source fingerprint.") from exc
            if not implementation or any(not isinstance(name, str) or not isinstance(digest, str) or len(digest) != 64
                                          for name, digest in implementation.items()):
                raise RuntimeError("Frozen build trading-source fingerprint is invalid.")
        else:
            implementation = {}
            for path in (Path(__file__), Path(market.__file__), Path(risk.__file__)):
                implementation[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        policy_path = Path(__file__).with_name("forward_policy_v5_addendum.json")
        policy_sha = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        return {"source": SOURCE, "symbols": list(SYMBOLS), "strategies": list(STRATEGIES),
            "starting_usd_per_strategy_symbol": 10_000.0, "fee_bps_per_side": FEE_BPS,
            "slippage_bps_per_side": SLIP_BPS, "volatility_window_days": 20,
            "annualization_days": 365, "annual_target_volatility": 0.10,
            "annual_volatility_floor": 0.10, "post_trade_max_weight": 0.50,
            "rebalance_band": 0.025, "drawdown_halt": DRAWNDOWN_LIMIT,
            "scheduled_decision_utc": "00:00", "scheduled_fill_utc": "04:00",
            "entry_expiry_utc": "04:15", "quote_poll_seconds": POLL_SECONDS,
            "daily_history_refresh_earliest_utc": "00:10",
            "daily_history_refresh_deadline_utc_exclusive": "04:00",
            "daily_history_max_pairs_per_utc_day": 3,
            "daily_history_min_interval_seconds": 300,
            "quote_age_max_seconds": MAX_QUOTE_AGE, "provider_future_tolerance_seconds": FUTURE_TOLERANCE,
            "history_warmup_points_max": 366, "warmup_is_never_pnl": True,
            "no_historical_catchup": True,
            "position_protection_schema": PROTECTION_SCHEMA,
            "position_protection_basis": "net liquidation on remaining weighted entry-cost basis; entry fees/slippage and estimated sell fees/slippage included",
            "position_protection_costs_bps_per_side": {"fee": FEE_BPS, "slippage": SLIP_BPS},
            "position_protection_trigger_clock": "atomic BTC/ETH quote; provider/request/receipt strictly newer than latest basis-changing fill application time",
            "position_protection_exit_clock": "fresh atomic quote with provider/request/receipt strictly later than latched decision",
            "position_protection_defaults": {"take_profit_pct": None, "stop_loss_pct": None},
            "position_protection_max_pct": {"take_profit_pct": 100.0, "stop_loss_pct": 99.9},
            "position_protection_step_pct": 0.1,
            "position_protection_cooldown": "post-close daily source observation strictly later than protective close application time",
            "forward_policy_addendum_sha256": policy_sha,
            "implementation_sha256": implementation,
            "disclaimer": "CoinGecko daily aggregate USD prices are sampled-price proxies, not executable venue fills."}

    @contextmanager
    def db(self):
        with self.lock:
            con = sqlite3.connect(self.path, timeout=8)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            try:
                con.execute("BEGIN IMMEDIATE")
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise
            finally:
                con.close()

    def _latest(self, db):
        return db.execute("SELECT * FROM cohorts ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()

    def _clock_sample(self, high_water: float | None = None) -> tuple[float, bool]:
        """Return a monotonic-backed wall estimate and a rollback flag.

        The process monotonic clock prevents a frozen/backward wall clock from
        extending a running timer. Persisted wall high-water catches rollback
        across restarts. This is fail-closed protection, not tamper-proof time.
        """
        wall = float(self.clock())
        mono = float(self._monotonic())
        expected = self._clock_anchor_wall + max(0.0, mono - self._clock_anchor_mono)
        with self._clock_lock:
            prior = max(self._last_wall_observed, float(high_water or 0.0))
            rollback = wall < prior - 1.0 or wall < expected - 5.0
            safe = max(wall, expected, prior)
            self._last_wall_observed = max(self._last_wall_observed, wall)
        return safe, rollback

    def _terminalize_locked(self, db, cohort, *, when: float, reason: str,
                             status: str, clock_suspect: bool = False) -> bool:
        """Close the entry window once and set the selected post-session policy."""
        current = str(cohort["session_status"] or "not_started")
        if current not in {"active", "paused"}:
            return False
        policy = cohort["expiry_policy"] or "CLOSE"
        cid = cohort["id"]
        if policy == "EXIT_ONLY":
            self._cancel_pending_entries_keep_reductions(db, cid, reason + " Pending entries/increases were cancelled.")
        else:
            db.execute("UPDATE decisions SET status='cancelled',reason=? WHERE cohort_id=? AND status='pending'",
                (reason + " Pending scheduled actions were cancelled.", cid))
        db.execute("UPDATE cohorts SET entry_disabled=1,session_status=?,session_decided_at=?,terminal_reason=?,reason=?,error=CASE WHEN ? THEN ? ELSE error END,launch_generation=launch_generation+1 WHERE id=?",
            ("clock_suspect" if clock_suspect else status, when, reason, reason,
             int(clock_suspect), "System clock moved backward or fell behind its monotonic/high-water estimate; entries are permanently disabled for this cohort.", cid))
        open_positions = db.execute("SELECT count(*) FROM accounts WHERE cohort_id=? AND qty>1e-12", (cid,)).fetchone()[0]
        if policy == "CLOSE":
            close_rows = db.execute("SELECT * FROM accounts WHERE cohort_id=? AND qty>1e-12", (cid,)).fetchall()
            for account in close_rows:
                if not account["exit_requested"] or int(account["exit_priority"] or 0) < EXIT_PRIORITY["session_close"]:
                    self._latch_exit(db, account, reason="session_close", decided_at=when,
                        basis=float(account["basis"]), detail=reason)
            if open_positions:
                db.execute("UPDATE cohorts SET state='exiting' WHERE id=?", (cid,))
                return True
            if not clock_suspect:
                db.execute("UPDATE cohorts SET session_status='finished',state='finished' WHERE id=?", (cid,))
            else:
                db.execute("UPDATE cohorts SET state='finished' WHERE id=?", (cid,))
            return False
        if open_positions:
            # Deadline applies while paused too. EXIT_ONLY continues with fresh
            # quotes; a user can pause that management explicitly afterward.
            next_state = "paused" if clock_suspect else "running"
            db.execute("UPDATE cohorts SET state=? WHERE id=?", (next_state, cid))
            return False
        if not clock_suspect:
            db.execute("UPDATE cohorts SET session_status='finished',state='finished' WHERE id=?", (cid,))
        else:
            db.execute("UPDATE cohorts SET state='finished' WHERE id=?", (cid,))
        return False

    @staticmethod
    def _cancel_pending_entries_keep_reductions(db, cohort_id: str, reason: str) -> None:
        """Cancel pending increases while preserving already scheduled reductions."""
        pending = db.execute("SELECT id,symbol,strategy,target_weight FROM decisions WHERE cohort_id=? AND status='pending'", (cohort_id,)).fetchall()
        for decision in pending:
            account = db.execute("SELECT cash,qty,last_price FROM accounts WHERE cohort_id=? AND symbol=? AND strategy=?",
                (cohort_id, decision["symbol"], decision["strategy"])).fetchone()
            keep_reduction = False
            if account and account["qty"] > 1e-12 and account["last_price"] and decision["target_weight"] is not None:
                equity = float(account["cash"]) + float(account["qty"]) * float(account["last_price"])
                weight = float(account["qty"]) * float(account["last_price"]) / equity if equity > 0 else 0.0
                keep_reduction = float(decision["target_weight"]) <= weight + 1e-12
            if not keep_reduction:
                db.execute("UPDATE decisions SET status='cancelled',reason=? WHERE id=?", (reason, decision["id"]))

    @staticmethod
    def _record_protection_event(db, account, *, event_type: str, reason: str,
                                 priority: int, decided_at: float, market_at: float | None = None,
                                 request_at: float | None = None, receipt_at: float | None = None,
                                 price: float | None = None, return_pct: float | None = None,
                                 basis: float | None = None, threshold_pct: float | None = None,
                                 detail: str = "") -> None:
        db.execute("INSERT INTO protection_events(cohort_id,episode_id,symbol,strategy,event_type,reason,priority,decided_at,market_at,request_at,receipt_at,price,net_return_pct,remaining_basis,threshold_pct,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account["cohort_id"], account["episode_id"], account["symbol"], account["strategy"],
             event_type, reason, priority, decided_at, market_at, request_at, receipt_at, price,
             return_pct, basis, threshold_pct, detail))

    def _latch_exit(self, db, account, *, reason: str, decided_at: float,
                    market_at: float | None = None, request_at: float | None = None,
                    receipt_at: float | None = None, price: float | None = None,
                    return_pct: float | None = None, basis: float | None = None,
                    threshold_pct: float | None = None, detail: str = "") -> bool:
        """Durably request a later-quote close, allowing only higher-priority overrides."""
        priority = EXIT_PRIORITY[reason]
        already = bool(account["exit_requested"])
        prior_priority = int(account["exit_priority"] or 0)
        if already and priority <= prior_priority:
            return False
        self._record_protection_event(db, account,
            event_type="OVERRIDE" if already else "TRIGGER", reason=reason,
            priority=priority, decided_at=decided_at, market_at=market_at,
            request_at=request_at, receipt_at=receipt_at, price=price,
            return_pct=return_pct, basis=basis, threshold_pct=threshold_pct,
            detail=(f"Superseded {account['exit_reason']} priority {prior_priority}. " if already else "") + detail)
        db.execute("UPDATE accounts SET exit_requested=1,exit_decided_at=?,exit_reason=?,exit_priority=?,updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
            (decided_at, reason, priority, decided_at, account["cohort_id"], account["symbol"], account["strategy"]))
        if account["episode_id"]:
            db.execute("UPDATE position_episodes SET status='exit_pending',pending_reason=? WHERE id=?",
                (reason, account["episode_id"]))
        if reason in {"take_profit", "stop_loss"} and account["episode_id"]:
            db.execute("UPDATE accounts SET protection_trigger_reason=coalesce(protection_trigger_reason,?),protection_triggered_at=coalesce(protection_triggered_at,?),protection_trigger_price=coalesce(protection_trigger_price,?),protection_trigger_market_at=coalesce(protection_trigger_market_at,?),protection_trigger_return_pct=coalesce(protection_trigger_return_pct,?),protection_trigger_basis=coalesce(protection_trigger_basis,?),protection_trigger_threshold_pct=coalesce(protection_trigger_threshold_pct,?) WHERE cohort_id=? AND symbol=? AND strategy=?",
                (reason, decided_at, price, market_at, return_pct, basis, threshold_pct,
                 account["cohort_id"], account["symbol"], account["strategy"]))
            db.execute("UPDATE position_episodes SET protection_trigger_reason=coalesce(protection_trigger_reason,?),protection_triggered_at=coalesce(protection_triggered_at,?),protection_trigger_price=coalesce(protection_trigger_price,?),protection_trigger_return_pct=coalesce(protection_trigger_return_pct,?),protection_trigger_basis=coalesce(protection_trigger_basis,?),protection_trigger_threshold_pct=coalesce(protection_trigger_threshold_pct,?) WHERE id=?",
                (reason, decided_at, price, return_pct, basis, threshold_pct, account["episode_id"]))
        db.execute("UPDATE decisions SET status='cancelled',reason=? WHERE cohort_id=? AND symbol=? AND strategy=? AND status='pending'",
            (f"{reason.replace('_', ' ').title()} exit is latched; no entry or rebalance may cancel it.", account["cohort_id"], account["symbol"], account["strategy"]))
        return True

    def _advance_session_clock(self) -> float:
        """Persist clock high-water and enforce deadline/rollback atomically."""
        launch_close = False
        launch_management = False
        launch_protection = False
        terminal_changed = False
        with self.db() as db:
            row = self._latest(db)
            if row is None:
                return float(self.clock())
            status = row["session_status"] or "not_started"
            if status in {"not_started", "finished", "legacy_untimed", "reset"}:
                # No active timer needs a two-second persistent heartbeat.
                return float(self.clock())
            safe_now, rollback = self._clock_sample(row["clock_high_water"])
            high_water = float(row["clock_high_water"] or 0)
            if status in {"active", "paused"} and row["session_started_at"] is not None:
                if rollback:
                    terminal_changed = True
                    db.execute("UPDATE cohorts SET clock_high_water=max(coalesce(clock_high_water,0),?) WHERE id=?", (safe_now, row["id"]))
                    launch_close = self._terminalize_locked(db, row, when=safe_now,
                        reason="Clock integrity check failed; entry permission is closed. A fresh quote is required for any safety exit.",
                        status="clock_suspect", clock_suspect=True) or launch_close
                elif row["session_deadline_at"] is not None and safe_now >= float(row["session_deadline_at"]):
                    terminal_changed = True
                    db.execute("UPDATE cohorts SET clock_high_water=max(coalesce(clock_high_water,0),?) WHERE id=?", (safe_now, row["id"]))
                    expiry_status = "expired_pending_close" if row["expiry_policy"] == "CLOSE" else "expired_exit_only"
                    launch_close = self._terminalize_locked(db, row, when=safe_now,
                        reason=f"Session deadline reached at {utc_iso(float(row['session_deadline_at']))}; new entries are permanently disabled.",
                        status=expiry_status) or launch_close
                    refreshed = self._latest(db)
                    launch_management = bool(refreshed and refreshed["state"] == "running" and
                        refreshed["session_status"] == "expired_exit_only")
                elif safe_now - high_water >= 30:
                    # Keep a recent durable high-water without fsyncing every
                    # two-second watchdog tick.
                    db.execute("UPDATE cohorts SET clock_high_water=max(coalesce(clock_high_water,0),?) WHERE id=?",
                        (safe_now, row["id"]))
            newest = self._latest(db)
            if newest and newest["state"] == "paused" and newest["session_status"] in {
                    "expired_pending_close", "stopped_pending_close", "clock_suspect"} and \
                    newest["close_retry_after"] is not None and safe_now >= float(newest["close_retry_after"]):
                db.execute("UPDATE cohorts SET state='exiting',close_retry_after=NULL,launch_generation=launch_generation+1 WHERE id=? AND state='paused'",
                    (newest["id"],))
                newest = self._latest(db)
            launch_close = launch_close or bool(newest and newest["state"] == "exiting" and
                newest["session_status"] in {"expired_pending_close", "stopped_pending_close", "clock_suspect"})
            launch_management = launch_management or bool(newest and newest["state"] == "running" and
                newest["session_status"] in {"expired_exit_only", "stopped_exit_only"})
            if newest and newest["state"] == "protection_pending":
                retry_at = float(newest["close_retry_after"] or 0)
                if safe_now >= retry_at:
                    # Do not invalidate a worker which may already own the
                    # serialized quote slot. The worker itself advances the
                    # clock before each request; mutating its generation here
                    # could make it stale and prevent every protective fill.
                    launch_protection = True
        if terminal_changed:
            self.wake.set()
        if launch_close:
            self._ensure_pending_close_worker()
        elif launch_management:
            self._ensure_exit_only_worker()
        elif launch_protection:
            self._ensure_protection_worker()
        return safe_now

    def _ensure_pending_close_worker(self) -> None:
        with self.db() as db:
            row = self._latest(db)
            if not row or row["state"] != "exiting" or row["session_status"] not in {
                    "expired_pending_close", "stopped_pending_close", "clock_suspect"}:
                return
            cohort_id = row["id"]
            generation = int(row["launch_generation"])
        if not self.worker_lock.acquire(blocking=False):
            return
        self.stop_event.clear()
        self.wake.clear()
        self.worker = threading.Thread(target=self._run, args=(cohort_id, "close_only", generation),
            name="forward-session-close", daemon=True)
        try:
            self.worker.start()
        except BaseException:
            self.worker_lock.release()
            with self.db() as db:
                db.execute("UPDATE cohorts SET error=?,reason=? WHERE id=? AND state='exiting' AND launch_generation=?",
                    ("Close-only worker could not start; restart the local service or retry Close positions now.",
                     "The session remains terminal and pending-close; no new entries are possible.", cohort_id, generation))

    def _ensure_protection_worker(self) -> None:
        if not self.worker_lock.acquire(blocking=False):
            return
        cohort_id = None
        generation = None
        started = False
        try:
            with self.db() as db:
                row = self._latest(db)
                if not row or row["state"] != "protection_pending":
                    return
                pending = db.execute("SELECT count(*) FROM accounts WHERE cohort_id=? AND exit_requested=1 AND exit_priority BETWEEN ? AND ?",
                    (row["id"], EXIT_PRIORITY["take_profit"], EXIT_PRIORITY["account_drawdown"])).fetchone()[0]
                if not pending:
                    db.execute("UPDATE cohorts SET state='paused',session_status=CASE WHEN session_status='active' THEN 'paused' ELSE session_status END,close_retry_after=NULL,reason='No pending protection closes remain; monitoring stays paused until explicitly resumed.' WHERE id=?",
                        (row["id"],))
                    return
                cohort_id = row["id"]
                # Reserve a new generation only after the exclusive worker slot
                # is acquired and the pending close is revalidated.
                generation = int(row["launch_generation"]) + 1
                db.execute("UPDATE cohorts SET launch_generation=?,close_retry_after=NULL,error=NULL WHERE id=? AND state='protection_pending'",
                    (generation, cohort_id))
            self.stop_event.clear()
            self.wake.clear()
            self.worker = threading.Thread(target=self._run, args=(cohort_id, "protective_only", generation),
                name="forward-protective-close", daemon=True)
            started = True
            try:
                self.worker.start()
            except BaseException:
                started = False
                raise
        except BaseException:
            if cohort_id is not None and generation is not None:
                with self.db() as db:
                    db.execute("UPDATE cohorts SET close_retry_after=?,reason=?,error=? WHERE id=? AND state='protection_pending' AND launch_generation=?",
                        (float(self.clock()) + self.poll_seconds,
                         "The protective exit remains latched; no new entries are possible. A fresh quote retry is scheduled.",
                         "Quote-only close worker could not start; retry after the bounded interval.", cohort_id, generation))
            raise
        finally:
            if not started and self.worker_lock.locked():
                self.worker_lock.release()

    def _ensure_exit_only_worker(self) -> None:
        with self.db() as db:
            row = self._latest(db)
            if not row or row["state"] != "running" or row["session_status"] not in {"expired_exit_only", "stopped_exit_only"}:
                return
            cohort_id, generation = row["id"], int(row["launch_generation"])
        if not self.worker_lock.acquire(blocking=False):
            return
        self.stop_event.clear()
        self.wake.clear()
        self.worker = threading.Thread(target=self._run, args=(cohort_id, "management_only", generation),
            name="forward-exit-only-management", daemon=True)
        try:
            self.worker.start()
        except BaseException:
            self.worker_lock.release()
            with self.db() as db:
                db.execute("UPDATE cohorts SET error=?,reason=? WHERE id=? AND state='running' AND launch_generation=?",
                    ("Exit-only management worker could not start; retry Resume or Close positions now.",
                     "The timer is terminal and entries remain disabled.", cohort_id, generation))

    def _watchdog_once(self) -> None:
        self._advance_session_clock()

    def _watchdog_loop(self) -> None:
        while not self.watchdog_stop.is_set():
            try:
                self._watchdog_once()
            except Exception:
                # The API path exposes the underlying database error. The
                # watchdog remains alive so a temporary lock/error can recover.
                pass
            self.watchdog_stop.wait(2.0)

    def _is_current(self, cohort_id: str, states=("starting", "running"), generation: int | None = None) -> bool:
        with self.db() as db:
            row = self._latest(db)
            return bool(row and row["id"] == cohort_id and row["state"] in states and
                        (generation is None or int(row["launch_generation"]) == generation))

    def state(self) -> dict:
        now = self._advance_session_clock()
        with self.db() as db:
            cohort = self._latest(db)
            if not cohort:
                raise RuntimeError("Forward cohort state is unavailable.")
            cid = cohort["id"]
            accounts = [dict(x) for x in db.execute("SELECT * FROM accounts WHERE cohort_id=? ORDER BY symbol,strategy", (cid,))]
            decisions = [dict(x) for x in db.execute("SELECT symbol,strategy,signal_ts,created_at,due_at,expires_at,target_weight,status,reason FROM decisions WHERE cohort_id=? ORDER BY signal_ts DESC,id DESC LIMIT 60", (cid,))]
            fills = [dict(x) for x in db.execute("SELECT symbol,strategy,signal_ts,request_at,receipt_at,market_at,side,qty,raw_price,fill_price,fee,slippage,realized_pnl,reason,episode_id,applied_at FROM fills WHERE cohort_id=? ORDER BY id DESC LIMIT 100", (cid,))]
            episodes = [dict(x) for x in db.execute("SELECT * FROM position_episodes WHERE cohort_id=? ORDER BY opened_at DESC LIMIT 100", (cid,))]
            episode_costs = {row["episode_id"]: dict(row) for row in db.execute(
                "SELECT episode_id,count(*) AS fill_count,coalesce(sum(fee),0) AS total_fees,"
                "coalesce(sum(slippage),0) AS total_slippage FROM fills "
                "WHERE cohort_id=? AND episode_id IS NOT NULL GROUP BY episode_id", (cid,))}
            protection_events = [dict(x) for x in db.execute("SELECT * FROM protection_events WHERE cohort_id=? ORDER BY id DESC LIMIT 100", (cid,))]
            baseline = json.loads(cohort["baseline_json"] or "{}")
            last_daily = json.loads(cohort["last_daily_ts_json"] or "{}")
            meta = json.loads(cohort["warmup_meta_json"] or "{}")
            config = json.loads(cohort["config_json"])
            latest_quotes = json.loads(cohort["latest_quote_json"] or "{}")
            start_at = cohort["start_at"]
            account_views = []
            for a in accounts:
                price = a["last_price"]
                quote_receipt = float(a["last_receipt_at"]) if a["last_receipt_at"] is not None else None
                quote_age = max(0.0, now - quote_receipt) if quote_receipt is not None else None
                equity = a["cash"] + (a["qty"] * price if price is not None else 0.0)
                initial = a["initial"]
                nlv = a["cash"] + (a["qty"] * price * (1 - SLIP_BPS/10000) * (1 - FEE_BPS/10000) if price is not None else 0.0)
                position_return = net_position_return_pct(float(a["qty"]), float(a["basis"]), float(price)) if price is not None else None
                tp_price = estimated_protection_price(float(a["qty"]), float(a["basis"]), float(a["position_tp_pct"]), take_profit=True) if a["position_tp_pct"] is not None else None
                sl_price = estimated_protection_price(float(a["qty"]), float(a["basis"]), float(a["position_sl_pct"]), take_profit=False) if a["position_sl_pct"] is not None else None
                is_legacy = cohort["protection_schema_version"] not in {None, PROTECTION_SCHEMA}
                monitoring = cohort["state"] == "running" and cohort["session_status"] in {
                    "active", "expired_exit_only", "stopped_exit_only"}
                if a["exit_requested"]:
                    protection_state = (a["exit_reason"] or "close").replace("_", " ").title() + " pending · fresh quote required"
                elif is_legacy and a["qty"] > 1e-12:
                    protection_state = "Legacy holding · V5.2 TP/SL not applied"
                elif a["qty"] > 1e-12 and (a["position_tp_pct"] is not None or a["position_sl_pct"] is not None):
                    if monitoring and quote_age is not None and quote_age <= MAX_QUOTE_AGE:
                        protection_state = "Monitoring on valid quotes · thresholds estimated at last quote"
                    elif monitoring:
                        protection_state = "Awaiting fresh quote · last-quote estimate only"
                    else:
                        protection_state = "Configured · monitoring paused; last-quote estimate only"
                elif a["cooldown_after_at"] is not None:
                    protection_state = "Flat reentry cooldown · waiting for newer daily observation"
                elif a["qty"] > 1e-12:
                    protection_state = "Holding · protection OFF for this position"
                else:
                    protection_state = "Flat"
                account_views.append({**a, "equity": equity, "net_liquidation": nlv,
                    "return_pct": (equity / initial - 1) * 100 if initial else 0.0,
                    "asset_weight_pct": (a["qty"] * price / equity * 100 if price is not None and equity > 0 else 0.0),
                    "net_liquidation_return_pct": (nlv / initial - 1) * 100 if initial else 0.0,
                    "position_net_return_pct": position_return,
                    "estimated_take_profit_price": tp_price,
                    "estimated_stop_loss_price": sl_price,
                    "valuation_quote_utc": utc_iso(quote_receipt),
                    "valuation_age_seconds": quote_age,
                    "valuation_is_live": bool(monitoring and quote_age is not None and quote_age <= MAX_QUOTE_AGE),
                    "protection_enabled": bool(a["position_tp_pct"] is not None or a["position_sl_pct"] is not None),
                    "protection_state": protection_state,
                    "status": "exit_pending" if a["exit_requested"] else "halted" if a["halted"] else cohort["state"]})
            for episode in episodes:
                costs = episode_costs.get(episode["id"], {})
                # opening_basis includes all modeled entry costs on every add;
                # remaining_basis tracks only the unsold lot. Fees/slippage and
                # realized P&L aggregate every fill for the episode, not the
                # API's capped recent-fill projection.
                episode["total_entry_cost_usd"] = float(episode["opening_basis"])
                episode["total_fees_usd"] = float(costs.get("total_fees", 0.0))
                episode["total_slippage_usd"] = float(costs.get("total_slippage", 0.0))
                episode["all_episode_fills"] = int(costs.get("fill_count", 0))
                episode["net_realized_pnl_usd"] = float(episode["realized_pnl"])
                # Trigger cause and final close cause are distinct. A later
                # manual/session/DD override must not erase the original TP/SL.
                episode["protection_trigger_reason"] = episode.get("protection_trigger_reason")
            newest_daily = max(last_daily.values()) if last_daily else None
            boundary = max(float(cohort["start_at"] or 0), float(cohort["resume_at"] or 0))
            boundary_next_daily = (int(boundary) // 86400 + 1) * 86400 if boundary else None
            next_eligible = max(newest_daily + 86400 if newest_daily else 0,
                boundary_next_daily or 0) or None
            forward_observations = sum(db.execute("SELECT count(*) FROM daily_observations WHERE cohort_id=? AND symbol=?", (cid,s)).fetchone()[0] for s in SYMBOLS)
            forward_days_count = db.execute("SELECT count(DISTINCT ts) FROM daily_observations WHERE cohort_id=?", (cid,)).fetchone()[0]
            marks_count = db.execute("SELECT count(*) FROM marks WHERE cohort_id=?", (cid,)).fetchone()[0]
            fills_count = db.execute("SELECT count(*) FROM fills WHERE cohort_id=?", (cid,)).fetchone()[0]
            decisions_count = db.execute("SELECT count(*) FROM decisions WHERE cohort_id=?", (cid,)).fetchone()[0]
            episodes_count = db.execute("SELECT count(*) FROM position_episodes WHERE cohort_id=?", (cid,)).fetchone()[0]
            protection_events_count = db.execute("SELECT count(*) FROM protection_events WHERE cohort_id=?", (cid,)).fetchone()[0]
            elapsed_calendar_days = max(0.0, (now - float(start_at)) / 86400) if start_at is not None else 0.0
            forward_days = float(forward_days_count)
            session_config = json.loads(cohort["session_config_json"] or "{}")
            qa_fixture = bool(session_config.get("qa_fixture_label") or self.qa_fixture_label)
        due_ts = next_eligible
        return {"cohort_id": cid, "state": cohort["state"], "started_at": utc_iso(start_at),
            "resume_at": utc_iso(cohort["resume_at"]), "config": config, "config_hash": cohort["config_hash"],
            "server_now_epoch": now, "server_now_utc": utc_iso(now),
            "session_id": cohort["session_id"], "session_started_at_utc": utc_iso(cohort["session_started_at"]),
            "session_deadline_utc": utc_iso(cohort["session_deadline_at"]),
            "session_deadline_epoch": cohort["session_deadline_at"],
            "session_duration_minutes": cohort["session_duration_minutes"],
            "expiry_policy": cohort["expiry_policy"], "session_status": cohort["session_status"],
            "session_config": session_config, "qa_fixture": qa_fixture,
            "protection_schema_version": cohort["protection_schema_version"],
            "protection_legacy_frozen": bool(cohort["protection_schema_version"] not in {None, PROTECTION_SCHEMA}),
            "protection_basis": config.get("position_protection_basis"),
            "session_decided_at_utc": utc_iso(cohort["session_decided_at"]),
            "entry_disabled": bool(cohort["entry_disabled"]),
            "clock_integrity": "rollback_detected" if cohort["session_status"] == "clock_suspect" else "high_water_checked",
            "terminal_reason": cohort["terminal_reason"], "close_retry_after_utc": utc_iso(cohort["close_retry_after"]),
            "source": SOURCE, "source_attribution": "Data provided by CoinGecko. https://www.coingecko.com/",
            "terms_url": "https://www.coingecko.com/en/api_terms",
            "forward_days": forward_days, "elapsed_calendar_days": elapsed_calendar_days,
            "is_performance_observation": bool(forward_days_count or marks_count or fills_count),
            "baseline_utc": {s: utc_iso(ts) for s, ts in baseline.items()},
            "last_daily_observation_utc": {s: utc_iso(ts) for s, ts in last_daily.items()},
            "next_eligible_observation_utc": utc_iso(next_eligible),
            "next_due_utc": utc_iso(due_ts + 4*3600 if due_ts else None),
            "next_expiry_utc": utc_iso(due_ts + 4*3600 + ENTRY_EXPIRY_SECONDS if due_ts else None),
            "warmup_meta": meta, "last_quote_request": utc_iso(cohort["last_quote_request"]),
            "last_quote_receipt": utc_iso(cohort["last_quote_receipt"]),
            "latest_quotes": latest_quotes, "reason": cohort["reason"], "error": cohort["error"],
            "accounts": account_views, "decisions": decisions, "fills": fills,
            "position_episodes": episodes, "protection_events": protection_events,
            "totals": {"fills": fills_count, "recorded_decisions": decisions_count,
                "forward_observations": forward_observations, "post_start_marks": marks_count,
                "all_forward_fills": fills_count, "position_episodes": episodes_count,
                "protection_events": protection_events_count}}

    def control(self, action: str, *, confirm: str | None = None,
                duration_minutes: int | None = None, expiry_policy: str | None = None,
                take_profit_pct: object = None, stop_loss_pct: object = None) -> dict:
        if action not in {"start", "pause", "resume", "exit", "stop", "reset"}:
            raise ValueError("Choose start, pause, resume, exit, stop or reset.")
        if action == "start":
            if confirm != "START":
                raise ValueError("Start requires explicit confirmation of the displayed duration and expiry policy.")
            if isinstance(duration_minutes, bool) or not isinstance(duration_minutes, int) or not 1 <= duration_minutes <= 259200:
                raise ValueError("Session duration must be an integer from 1 to 259200 minutes.")
            if expiry_policy not in {"CLOSE", "EXIT_ONLY"}:
                raise ValueError("Choose CLOSE or EXIT_ONLY for the session expiry policy.")
            take_profit_pct = validate_protection_threshold(take_profit_pct,
                name="Take-profit threshold", maximum=100.0)
            stop_loss_pct = validate_protection_threshold(stop_loss_pct,
                name="Stop-loss threshold", maximum=99.9)
        if action == "reset" and confirm != "RESET":
            raise ValueError("Reset requires explicit confirmation.")
        now = self._advance_session_clock()
        launch_mode = None
        generation = None
        launch_id = None
        idle_stop = False
        with self.db() as db:
            row = self._latest(db)
            cid, state = row["id"], row["state"]
            session = row["session_status"] or "not_started"
            if action == "start":
                if state != "not_started" or session != "not_started" or row["entry_disabled"]:
                    raise RuntimeError("Start is available only once for a new cohort. Export and explicitly reset to create a new session.")
                safe_now, rollback = self._clock_sample(row["clock_high_water"])
                if rollback:
                    raise RuntimeError("The system clock moved backward; Start is blocked until a new explicitly reset cohort can establish a safe clock baseline.")
                start_at = max(now, safe_now)
                deadline = start_at + duration_minutes * 60
                session_id = str(uuid.uuid4())
                config = json.loads(row["config_json"])
                session_config = {"session_id": session_id, "started_at": start_at,
                    "deadline_at": deadline, "duration_minutes": duration_minutes,
                    "expiry_policy": expiry_policy,
                    "position_protection": {"take_profit_pct": take_profit_pct,
                        "stop_loss_pct": stop_loss_pct, "schema": PROTECTION_SCHEMA,
                        "basis": config["position_protection_basis"],
                        "fee_bps_per_side": FEE_BPS, "slippage_bps_per_side": SLIP_BPS,
                        "trigger_requires_quote_after_latest_basis_fill_application": True,
                        "exit_requires_quote_after_decision": True,
                        "cooldown_requires_source_daily_after_exit_fill_application": True}}
                config_hash = hashlib.sha256(_canonical({"id": cid, "session_id": session_id,
                    "start_at": start_at, "deadline_at": deadline, "duration_minutes": duration_minutes,
                    "expiry_policy": expiry_policy, "config": config,
                    "session_config": session_config})).hexdigest()
                generation = int(row["launch_generation"]) + 1
                db.execute("UPDATE cohorts SET state='starting',start_at=?,resume_at=NULL,config_hash=?,reason=?,error=NULL,launch_generation=?,session_id=?,session_started_at=?,session_deadline_at=?,session_duration_minutes=?,expiry_policy=?,session_status='active',entry_disabled=0,clock_high_water=max(coalesce(clock_high_water,0),?),session_decided_at=NULL,terminal_reason=NULL,session_config_json=?,protection_schema_version=? WHERE id=?",
                    (start_at, config_hash, "Session timer started at server acceptance; warmup counts toward the deadline and is not performance.", generation, session_id, start_at, deadline, duration_minutes, expiry_policy, start_at, json.dumps(session_config, sort_keys=True), PROTECTION_SCHEMA, cid))
                for symbol in SYMBOLS:
                    for strategy in STRATEGIES:
                        db.execute("INSERT OR IGNORE INTO accounts(cohort_id,symbol,strategy,initial,cash,peak_nlv,updated_at) VALUES(?,?,?,?,?,?,?)",
                            (cid, symbol, strategy, 10_000.0, 10_000.0, 10_000.0, start_at))
                launch_mode, launch_id = "start", cid
            elif action == "resume":
                if session in {"expired_pending_close", "stopped_pending_close", "clock_suspect"}:
                    raise RuntimeError("This session is terminal and cannot resume signals. A pending close requires a fresh quote.")
                if session in {"expired_exit_only", "stopped_exit_only", "finished"}:
                    if state != "paused":
                        raise RuntimeError("This session is terminal; only exit-only management or a fresh-quote close can continue.")
                if state != "paused":
                    raise RuntimeError("Only a paused cohort can resume.")
                if session == "legacy_untimed":
                    raise RuntimeError("This migrated untimed cohort has new entries disabled. Use Close positions now or export and explicitly reset.")
                if row["protection_schema_version"] != PROTECTION_SCHEMA and row["session_started_at"] is not None:
                    raise RuntimeError("This started V5.1 cohort is frozen without retroactive TP/SL settings. Preserve or close its holdings, then explicitly archive/reset for a new V5.2 session.")
                if session not in {"paused", "expired_exit_only", "stopped_exit_only"}:
                    raise RuntimeError("This cohort has no resumable authorized session. Export and explicitly reset to begin a new one.")
                pending_exit = db.execute("SELECT count(*) FROM accounts WHERE cohort_id=? AND exit_requested=1", (cid,)).fetchone()[0]
                if pending_exit:
                    raise RuntimeError("A requested close is still pending. Retry Close positions now with a fresh quote.")
                frozen = json.loads(row["config_json"])
                if frozen != self._config():
                    raise RuntimeError("Forward strategy or data-source code/config changed since this cohort was frozen. Export it, then explicitly reset for a new session.")
                generation = int(row["launch_generation"]) + 1
                continuing_exit_only = session in {"expired_exit_only", "stopped_exit_only"} or bool(row["entry_disabled"])
                next_session = session if continuing_exit_only else "active"
                db.execute("UPDATE cohorts SET state='starting',resume_at=?,session_status=?,reason=?,error=NULL,launch_generation=? WHERE id=?",
                    (now, next_session, "Resuming with the original immutable deadline and a new observation baseline; no catch-up trades." if not continuing_exit_only else "Resuming exit-only management; original deadline is terminal and new/increased positions remain blocked.", generation, cid))
                if continuing_exit_only:
                    self._cancel_pending_entries_keep_reductions(db,cid,
                        "Exit-only resume; entries/increases are cancelled, while a pending reduction may use a fresh quote before its original expiry.")
                else:
                    db.execute("UPDATE decisions SET status='cancelled',reason='Resume baseline reset; no catch-up.' WHERE cohort_id=? AND status='pending'", (cid,))
                launch_mode, launch_id = "resume", cid
            elif action == "pause":
                if state not in {"running", "exiting", "starting"}:
                    raise RuntimeError("Only a running cohort can pause.")
                if session in {"expired_pending_close", "stopped_pending_close", "clock_suspect"}:
                    raise RuntimeError("The session has a pending close; pause cannot cancel or defer its fresh-quote closure.")
                latched_closes = db.execute("SELECT count(*) FROM accounts WHERE cohort_id=? AND exit_requested=1 AND exit_priority BETWEEN ? AND ?",
                    (cid, EXIT_PRIORITY["take_profit"], EXIT_PRIORITY["account_drawdown"])).fetchone()[0]
                next_session = "paused" if session == "active" else session
                if latched_closes:
                    db.execute("UPDATE cohorts SET state='protection_pending',session_status=?,reason=?,error=NULL,close_retry_after=NULL,launch_generation=launch_generation+1 WHERE id=?",
                        (next_session, "Signal monitoring is paused. A previously latched protective close continues quote-only; the timer is unchanged.", cid))
                    launch_mode, launch_id = "protection_only", cid
                else:
                    db.execute("UPDATE cohorts SET state='paused',session_status=?,reason=?,error=NULL,launch_generation=launch_generation+1 WHERE id=?",
                        (next_session, "Paused by user. Existing positions remain held and unmonitored; the original session deadline continues without extension.", cid))
                if session in {"expired_exit_only", "stopped_exit_only"}:
                    self._cancel_pending_entries_keep_reductions(db,cid,
                        "Exit-only management paused; entries/increases are cancelled, but an already scheduled reduction remains eligible for a fresh quote before expiry.")
                elif not latched_closes:
                    db.execute("UPDATE decisions SET status='cancelled',reason='User paused; scheduled action cancelled.' WHERE cohort_id=? AND status='pending'", (cid,))
                self.wake.set()
            elif action == "exit":
                # Explicit fresh-quote safety override. It closes the entry
                # window even if the stored policy was EXIT_ONLY or the old
                # cohort was migrated without a timer.
                if session in {"finished", "not_started"}:
                    raise RuntimeError("There is no open paper session to close.")
                generation = int(row["launch_generation"]) + 1
                for account in db.execute("SELECT * FROM accounts WHERE cohort_id=? AND qty>1e-12", (cid,)).fetchall():
                    self._latch_exit(db, account, reason="manual_close", decided_at=now,
                        basis=float(account["basis"]), detail="User selected Close positions now.")
                db.execute("UPDATE decisions SET status='cancelled',reason='Close positions now; waiting for a fresh post-decision quote.' WHERE cohort_id=? AND status='pending'", (cid,))
                remaining = db.execute("SELECT count(*) FROM accounts WHERE cohort_id=? AND (qty>1e-12 OR exit_requested=1)", (cid,)).fetchone()[0]
                new_status = "stopped_pending_close" if remaining else "finished"
                next_op_state = "exiting" if remaining else "finished"
                db.execute("UPDATE cohorts SET state=?,session_status=?,entry_disabled=1,session_decided_at=coalesce(session_decided_at,?),terminal_reason=?,reason=?,error=NULL,launch_generation=? WHERE id=?",
                    (next_op_state, new_status, now, "Close positions now was selected; no new entries can occur.",
                     "Closing paper positions from a fresh quote; existing holdings remain until the quote is valid." if remaining else "No paper positions were open; session finished.", generation, cid))
                if remaining:
                    launch_mode, launch_id = "close_only", cid
                self.wake.set()
            elif action == "stop":
                if session in {"not_started", "finished", "expired_pending_close", "expired_exit_only", "stopped_pending_close", "stopped_exit_only", "clock_suspect", "legacy_untimed"}:
                    if session in {"expired_pending_close", "stopped_pending_close", "clock_suspect", "expired_exit_only", "stopped_exit_only", "finished"}:
                        # Idempotent: repeated Stop cannot move the decision time.
                        idle_stop = True
                    else:
                        raise RuntimeError("Start a confirmed session before using Stop now.")
                if not idle_stop:
                    generation = int(row["launch_generation"]) + 1
                    terminal = "stopped_pending_close" if row["expiry_policy"] == "CLOSE" else "stopped_exit_only"
                    launch_close = self._terminalize_locked(db, row, when=now,
                        reason="Stopped by user; the selected expiry policy now governs the remaining paper holdings.", status=terminal)
                    refreshed = self._latest(db)
                    if launch_close or (refreshed and refreshed["state"] == "exiting"):
                        launch_mode, launch_id = "close_only", cid
                    elif refreshed and refreshed["state"] == "running" and refreshed["session_status"] == "stopped_exit_only":
                        launch_mode, launch_id = "management_only", cid
                    self.wake.set()
            elif action == "reset":
                open_positions = db.execute("SELECT count(*) FROM accounts WHERE cohort_id=? AND (qty>1e-12 OR exit_requested=1)", (cid,)).fetchone()[0]
                if open_positions:
                    raise RuntimeError("Close all paper positions with a fresh quote before archiving this cohort. Existing holdings are preserved.")
                generation = int(row["launch_generation"]) + 1
                db.execute("UPDATE cohorts SET state='reset',reset_at=?,reason=?,error=NULL,entry_disabled=1,launch_generation=? WHERE id=?",
                    (now, "Confirmed archive/reset; prior session and all cohort records remain available in export.", generation, cid))
                db.execute("UPDATE decisions SET status='cancelled',reason='Confirmed cohort archive/reset.' WHERE cohort_id=? AND status='pending'", (cid,))
                db.execute("INSERT INTO cohorts(id,state,created_at,config_json,session_status,clock_high_water,reason,protection_schema_version) VALUES(?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), "not_started", now, json.dumps(self._config(), sort_keys=True), "not_started", now,
                     "New V5.2 cohort; optional per-position protection remains OFF until explicit Start review.", PROTECTION_SCHEMA))
                self.wake.set()
        if launch_mode and launch_id:
            if launch_mode == "close_only":
                self._ensure_pending_close_worker()
            elif launch_mode == "management_only":
                self._ensure_exit_only_worker()
            elif launch_mode == "protection_only":
                self._ensure_protection_worker()
            else:
                self._launch(launch_id, launch_mode, generation)
        return self.state()

    def _launch(self, cohort_id: str, mode: str, generation: int) -> None:
        if not self.worker_lock.acquire(timeout=getattr(self, "worker_acquire_timeout", 5)):
            with self.db() as db:
                pending_state = "exiting" if mode == "exit_only" else "starting"
                db.execute("UPDATE cohorts SET state='paused',session_status=CASE WHEN session_status='active' THEN 'paused' ELSE session_status END,reason=?,error=? WHERE id=? AND state=? AND launch_generation=?",
                    ("The serialized feed worker is still finishing. Retry the requested operation after it stops.",
                     "Another feed worker is still active.", cohort_id, pending_state, generation))
            raise RuntimeError("A forward-cohort worker is still finishing a feed request; state restored to paused. Retry explicitly.")
        self.stop_event.clear()
        self.wake.clear()
        self.worker = threading.Thread(target=self._run, args=(cohort_id, mode, generation), name="forward-v5-public-feed", daemon=True)
        try:
            self.worker.start()
        except BaseException:
            self.worker_lock.release()
            with self.db() as db:
                pending_state = "exiting" if mode == "exit_only" else "starting"
                db.execute("UPDATE cohorts SET state='paused',session_status=CASE WHEN session_status='active' THEN 'paused' ELSE session_status END,reason=?,error=? WHERE id=? AND state=? AND launch_generation=?",
                    ("Worker startup failed; retry explicitly.", "Background worker could not start.", cohort_id, pending_state, generation))
            raise

    def stop(self) -> None:
        self.stop_event.set()
        self.watchdog_stop.set()
        self.wake.set()
        with self.db() as db:
            row = self._latest(db)
            if row and row["state"] in {"starting", "running", "exiting"}:
                if row["session_status"] in {"expired_pending_close", "stopped_pending_close", "clock_suspect"}:
                    # Persist the pending close so service startup can continue
                    # quote-only management after a fresh request.
                    pass
                elif db.execute("SELECT count(*) FROM accounts WHERE cohort_id=? AND exit_requested=1 AND exit_priority BETWEEN ? AND ?",
                        (row["id"],EXIT_PRIORITY["take_profit"],EXIT_PRIORITY["account_drawdown"])).fetchone()[0] > 0:
                    session_status = "paused" if row["session_status"] == "active" else row["session_status"]
                    db.execute("UPDATE cohorts SET state='protection_pending',session_status=?,close_retry_after=?,reason=?,error=NULL,launch_generation=launch_generation+1 WHERE id=?",
                        (session_status,float(self.clock()),"Server stopped with a protective close latched. On restart, fresh quote-only closure resumes; signals remain paused.",row["id"]))
                    db.execute("UPDATE decisions SET status='cancelled',reason='Server stopped with a protective close pending; no catch-up.' WHERE cohort_id=? AND status='pending'",(row["id"],))
                else:
                    session_status = "paused" if row["session_status"] == "active" else row["session_status"]
                    db.execute("UPDATE cohorts SET state='paused',session_status=?,reason=?,error=NULL,launch_generation=launch_generation+1 WHERE id=?",
                        (session_status, "Server stopped; no feed polling occurs offline. The original timer continues; restart resumes its existing deadline.", row["id"]))
                    if row["session_status"] in {"expired_exit_only", "stopped_exit_only"}:
                        self._cancel_pending_entries_keep_reductions(db,row["id"],
                            "Server stopped; no entries/increases will catch up. A pending reduction remains eligible for a fresh quote before expiry.")
                    else:
                        db.execute("UPDATE decisions SET status='cancelled',reason='Server stopped; no catch-up after restart.' WHERE cohort_id=? AND status='pending'", (row["id"],))
        worker = self.worker
        if worker and worker.is_alive():
            worker.join(timeout=3)
        watchdog = self.watchdog
        if watchdog and watchdog.is_alive() and watchdog is not threading.current_thread():
            watchdog.join(timeout=2)

    def _daily_pair(self) -> tuple[dict[str, list[dict]], dict, float, float]:
        points = {}
        metadata = {}
        request_at = self.clock()
        for symbol in SYMBOLS:
            rows, meta = self.fetch_daily(symbol)
            points[symbol], metadata[symbol] = rows, meta
        received_at = self.clock()
        if points[SYMBOLS[0]][-1]["ts"] != points[SYMBOLS[1]][-1]["ts"]:
            raise ValueError("BTC and ETH daily history baselines differ; the atomic comparison cohort is paused.")
        latest = points[SYMBOLS[0]][-1]["ts"]
        if received_at - latest > 36 * 3600 or latest > received_at + FUTURE_TOLERANCE:
            raise ValueError("CoinGecko daily history is stale or future-dated; no decision was created.")
        return points, metadata, request_at, received_at

    def _run(self, cohort_id: str, mode: str, generation: int) -> None:
        try:
            if mode not in {"exit_only", "close_only", "management_only", "protective_only"}:
                if not self._initialize(cohort_id, mode, generation):
                    return
                self.wake.wait(self.poll_seconds)
                self.wake.clear()
            while not self.stop_event.is_set() and self._is_current(cohort_id, ("running", "exiting", "protection_pending"), generation):
                if self.stop_event.is_set():
                    break
                try:
                    now = self._advance_session_clock()
                    if self._is_current(cohort_id, ("running",), generation):
                        self._maybe_refresh_history(cohort_id, now)
                    if not self._is_current(cohort_id, ("running", "exiting", "protection_pending"), generation):
                        break
                    quotes = validate_batch(self.fetch_quotes(), self._advance_session_clock())
                    self._process_quote_batch(cohort_id, quotes, self._advance_session_clock(), generation=generation)
                except Exception as exc:
                    self._pause_error(cohort_id, f"Public feed unavailable or invalid: {str(exc)[:300]}", generation)
                    break
                if not self._is_current(cohort_id, ("running", "exiting", "protection_pending"), generation):
                    break
                self.wake.wait(self.poll_seconds)
                self.wake.clear()
        finally:
            if self.worker_lock.locked():
                self.worker_lock.release()

    def _initialize(self, cohort_id: str, mode: str, generation: int) -> bool:
        try:
            daily, meta, requested, received = self._daily_pair()
            quotes = validate_batch(self.fetch_quotes(), self._advance_session_clock())
            self._advance_session_clock()
            if not self._is_current(cohort_id, ("starting",), generation):
                return False
            baselines = {s: int(daily[s][-1]["ts"]) for s in SYMBOLS}
            day = dt.datetime.fromtimestamp(received, dt.timezone.utc).strftime("%Y-%m-%d")
            with self.db() as db:
                row = self._latest(db)
                if not row or row["id"] != cohort_id or row["state"] != "starting" or int(row["launch_generation"]) != generation:
                    return False
                prior_checks = int(row["history_check_count"] or 0) if row["last_history_check_day"] == day else 0
                for symbol in SYMBOLS:
                    db.executemany("INSERT OR REPLACE INTO warmup_prices VALUES(?,?,?,?,?,1)",
                        [(cohort_id, symbol, int(p["ts"]), float(p["price"]), received) for p in daily[symbol]])
                    for strategy in STRATEGIES:
                        db.execute("UPDATE accounts SET last_price=?,last_market_at=?,last_receipt_at=?,updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                            (quotes[symbol]["price"], quotes[symbol]["market_at"], received, received, cohort_id, symbol, strategy))
                db.execute("INSERT OR IGNORE INTO quote_batches VALUES(?,?,?,?)",
                    (cohort_id, quotes[SYMBOLS[0]]["requested_at"], received, hashlib.sha256(_canonical(quotes)).hexdigest()))
                db.execute("UPDATE cohorts SET state='running',baseline_json=?,last_daily_ts_json=?,last_history_check_day=?,last_history_check_at=?,history_check_count=?,last_quote_request=?,last_quote_receipt=?,latest_quote_json=?,warmup_meta_json=?,reason=?,error=NULL WHERE id=?",
                    (json.dumps(baselines, sort_keys=True), json.dumps(baselines, sort_keys=True), day, received, prior_checks,
                     quotes[SYMBOLS[0]]["requested_at"], received, json.dumps(quotes, sort_keys=True),
                     json.dumps(meta, sort_keys=True),
                     "Baseline saved. Historical warmup is input-only; waiting for a strictly newer completed UTC daily observation.", cohort_id))
            return True
        except Exception as exc:
            self._pause_error(cohort_id, f"Startup baseline unavailable: {str(exc)[:300]}", generation)
            return False

    def _maybe_refresh_history(self, cohort_id: str, now: float) -> None:
        current_day = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d")
        current_dt = dt.datetime.fromtimestamp(now, dt.timezone.utc)
        seconds_into_day = current_dt.hour * 3600 + current_dt.minute * 60 + current_dt.second
        if seconds_into_day < DAILY_REFRESH_AFTER_UTC:
            return
        skip_reason = None
        cohort_reason = None
        with self.db() as db:
            cohort = db.execute("SELECT * FROM cohorts WHERE id=?", (cohort_id,)).fetchone()
            if not cohort or cohort["state"] != "running":
                return
            if cohort["session_status"] == "clock_suspect":
                return
            last_daily = json.loads(cohort["last_daily_ts_json"] or "{}")
            latest_seen = max(last_daily.values() or [0])
            today_start = int(dt.datetime.fromisoformat(current_day).replace(tzinfo=dt.timezone.utc).timestamp())
            same_day = cohort["last_history_check_day"] == current_day
            history_checks = int(cohort["history_check_count"] or 0) if same_day else 0
            if latest_seen >= today_start:
                return
            if seconds_into_day >= 4 * 3600:
                skip_reason = "Today's completed daily point was not available before 04:00 UTC; no decision or backfill."
                cohort_reason = "No completed daily point arrived before 04:00 UTC. The scheduled signal was recorded as missed; waiting for the next UTC day."
            # Forward-only addendum: at most three BTC/ETH history pairs per
            # UTC day, starting 00:10, separated by at least five minutes;
            # no scheduled refresh happens after 04:00. Explicit start/resume
            # baselines are one separate history request.
            if skip_reason is None:
                last_check = float(cohort["last_history_check_at"] or 0)
                if same_day and now - last_check < 300:
                    return
                if history_checks >= 3:
                    skip_reason = "Three bounded daily-history checks were exhausted before 04:00 UTC; no catch-up."
                    cohort_reason = "Three bounded daily-history checks completed before today's 00:00 point was available. The scheduled observation was missed; waiting for the next UTC day."
                else:
                    # Reserve a request before I/O so repeated calls cannot
                    # exceed the daily budget if the source stalls.
                    db.execute("UPDATE cohorts SET last_history_check_day=?,last_history_check_at=?,history_check_count=? WHERE id=? AND state='running'",
                        (current_day, now, history_checks + 1, cohort_id))
        if skip_reason:
            self._skip_daily(cohort_id, today_start, now, skip_reason)
            with self.db() as db:
                db.execute("UPDATE cohorts SET reason=? WHERE id=? AND state='running'",
                    (cohort_reason, cohort_id))
            return
        daily, meta, requested, received = self._daily_pair()
        latest_ts = int(daily[SYMBOLS[0]][-1]["ts"])
        with self.db() as db:
            cohort = db.execute("SELECT * FROM cohorts WHERE id=?", (cohort_id,)).fetchone()
            if not cohort or cohort["state"] != "running":
                return
            last = json.loads(cohort["last_daily_ts_json"] or "{}")
            baseline = json.loads(cohort["baseline_json"] or "{}")
            cutoff = max(float(cohort["start_at"] or 0), float(cohort["resume_at"] or 0))
            newest_consumed = min(int(last.get(s, 0)) for s in SYMBOLS)
            for symbol in SYMBOLS:
                db.executemany("INSERT OR IGNORE INTO warmup_prices VALUES(?,?,?,?,?,1)",
                    [(cohort_id, symbol, int(p["ts"]), float(p["price"]), received)
                     for p in daily[symbol] if int(p["ts"]) <= max(int(baseline.get(symbol, 0)), cutoff)])
                for point in daily[symbol]:
                    if int(point["ts"]) > newest_consumed and int(point["ts"]) > cutoff:
                        db.execute("INSERT OR IGNORE INTO daily_observations VALUES(?,?,?,?,?,?)",
                            (cohort_id, symbol, int(point["ts"]), float(point["price"]), requested, received))
            db.execute("UPDATE cohorts SET warmup_meta_json=? WHERE id=?",
                (json.dumps(meta, sort_keys=True), cohort_id))
        # With an outage, never advance a decision to a stale or already-expired 04:00.
        if latest_ts <= newest_consumed:
            return
        new_points = [p for p in daily[SYMBOLS[0]] if int(p["ts"]) > newest_consumed]
        if not new_points:
            return
        # A point whose UTC timestamp is before or equal to the latest explicit
        # start/resume wall clock is warmup only, even if it arrived late from
        # the provider after that boundary.
        post_boundary = []
        for missed in new_points:
            if int(missed["ts"]) <= cutoff:
                self._skip_daily(cohort_id, int(missed["ts"]), received,
                    "Daily point timestamp predates the cohort/resume start; warmup only, never backfilled.")
            else:
                post_boundary.append(missed)
        if not post_boundary:
            self._advance_baseline(cohort_id, latest_ts)
            return
        # Only the newest unseen observation can create a decision. Any older
        # post-start points found after a delay are recorded as missed.
        for missed in post_boundary[:-1]:
            self._skip_daily(cohort_id, int(missed["ts"]), received, "Missed daily observation; no historical catch-up or backfill.")
        point = post_boundary[-1]
        signal_ts = int(point["ts"])
        due = signal_ts + 4 * 3600
        if received >= due:
            self._skip_daily(cohort_id, signal_ts, received, "Completed daily observation arrived at or after its 04:00 UTC execution deadline; no backdated decision or fill.")
            self._advance_baseline(cohort_id, latest_ts)
            return
        points_for_signal = {s: [p for p in daily[s] if int(p["ts"]) <= signal_ts] for s in SYMBOLS}
        for symbol in SYMBOLS:
            if not points_for_signal[symbol] or int(points_for_signal[symbol][-1]["ts"]) != signal_ts:
                self._skip_daily(cohort_id, signal_ts, received, "BTC/ETH daily observation pair was incomplete.")
                self._advance_baseline(cohort_id, latest_ts)
                return
        self._create_decisions(cohort_id, signal_ts, received, due, points_for_signal)
        self._advance_baseline(cohort_id, latest_ts)

    def _skip_daily(self, cohort_id: str, signal_ts: int, created: float, reason: str) -> None:
        due = signal_ts + 4 * 3600
        with self.db() as db:
            for symbol in SYMBOLS:
                for strategy in STRATEGIES:
                    db.execute("INSERT OR IGNORE INTO decisions(cohort_id,symbol,strategy,signal_ts,created_at,due_at,expires_at,target_weight,status,reason,source_observation_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (cohort_id, symbol, strategy, signal_ts, created, due, due+ENTRY_EXPIRY_SECONDS, None, "skipped", reason, signal_ts))

    def _create_decisions(self, cohort_id: str, signal_ts: int, created: float, due: float,
                          points: dict[str, list[dict]]) -> None:
        if created >= due:
            self._skip_daily(cohort_id, signal_ts, created, "Decision was not created before the scheduled execution deadline.")
            return
        with self.db() as db:
            for symbol in SYMBOLS:
                rows = db.execute("SELECT strategy,signal_active FROM accounts WHERE cohort_id=? AND symbol=?", (cohort_id, symbol)).fetchall()
                old_state = {x["strategy"]: bool(x["signal_active"]) for x in rows}
                targets, breakout_state = daily_signal(points[symbol], old_state.get("close_breakout_20_10", False))
                if not targets:
                    for strategy in STRATEGIES:
                        db.execute("INSERT OR IGNORE INTO decisions(cohort_id,symbol,strategy,signal_ts,created_at,due_at,expires_at,target_weight,status,reason,source_observation_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (cohort_id, symbol, strategy, signal_ts, created, due, due+ENTRY_EXPIRY_SECONDS, None, "skipped", "Insufficient contiguous completed closes for this signal.", signal_ts))
                    continue
                for strategy in STRATEGIES:
                    target = targets.get(strategy)
                    active = bool(target is not None and target > 0)
                    account = db.execute("SELECT cooldown_after_at FROM accounts WHERE cohort_id=? AND symbol=? AND strategy=?",
                        (cohort_id, symbol, strategy)).fetchone()
                    cooldown = float(account["cooldown_after_at"]) if account and account["cooldown_after_at"] is not None else None
                    if cooldown is not None and signal_ts <= cooldown:
                        db.execute("INSERT OR IGNORE INTO decisions(cohort_id,symbol,strategy,signal_ts,created_at,due_at,expires_at,target_weight,status,reason,source_observation_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (cohort_id,symbol,strategy,signal_ts,created,due,due+ENTRY_EXPIRY_SECONDS,target,"no_trade",
                             "Protective-exit cooldown: this account requires a daily source timestamp strictly after the protective close was applied.",signal_ts))
                        db.execute("UPDATE accounts SET signal_active=0,updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                            (created,cohort_id,symbol,strategy))
                        continue
                    if cooldown is not None:
                        db.execute("UPDATE accounts SET cooldown_after_at=NULL,cooldown_after_receipt_at=NULL WHERE cohort_id=? AND symbol=? AND strategy=?",
                            (cohort_id,symbol,strategy))
                    db.execute("INSERT OR IGNORE INTO decisions(cohort_id,symbol,strategy,signal_ts,created_at,due_at,expires_at,target_weight,status,reason,source_observation_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (cohort_id, symbol, strategy, signal_ts, created, due, due+ENTRY_EXPIRY_SECONDS, target, "pending" if target is not None else "skipped",
                         "New post-start daily signal; scheduled 04:00 UTC; expires 04:15 UTC." if target is not None else "Invalid feature window; no signal catch-up.", signal_ts))
                    db.execute("UPDATE accounts SET signal_active=?,updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                        (int(breakout_state if strategy == "close_breakout_20_10" and breakout_state is not None else active), created, cohort_id, symbol, strategy))

    def _advance_baseline(self, cohort_id: str, signal_ts: int) -> None:
        with self.db() as db:
            row = db.execute("SELECT last_daily_ts_json FROM cohorts WHERE id=?", (cohort_id,)).fetchone()
            current = json.loads(row[0] or "{}") if row else {}
            for symbol in SYMBOLS:
                current[symbol] = max(int(current.get(symbol, 0)), signal_ts)
            db.execute("UPDATE cohorts SET last_daily_ts_json=? WHERE id=?", (json.dumps(current, sort_keys=True), cohort_id))

    def _process_quote_batch(self, cohort_id: str, quotes: dict, now: float, *, generation: int | None = None) -> None:
        # Validate both assets before the transaction; no partial mark/fill round.
        safe_now = self._advance_session_clock()
        quotes = validate_batch(quotes, max(float(now), safe_now))
        if not self._is_current(cohort_id, ("running", "exiting", "protection_pending"), generation):
            return
        request_at = quotes[SYMBOLS[0]]["requested_at"]
        receipt_at = max(quotes[s]["observed_at"] for s in SYMBOLS)
        batch_hash = hashlib.sha256(_canonical(quotes)).hexdigest()
        with self.db() as db:
            cohort = db.execute("SELECT * FROM cohorts WHERE id=?", (cohort_id,)).fetchone()
            if not cohort or cohort["state"] not in {"running", "exiting", "protection_pending"}:
                return
            if generation is not None and int(cohort["launch_generation"]) != generation:
                return
            prior_batch = db.execute("SELECT payload_hash FROM quote_batches WHERE cohort_id=? AND request_at=?", (cohort_id, request_at)).fetchone()
            if prior_batch:
                if prior_batch["payload_hash"] == batch_hash:
                    return
                raise ValueError("A quote request timestamp was already consumed with different payload; the batch was rejected.")
            if ((cohort["last_quote_request"] is not None and request_at <= float(cohort["last_quote_request"])) or
                    (cohort["last_quote_receipt"] is not None and receipt_at <= float(cohort["last_quote_receipt"]))):
                return
            prior_quotes = json.loads(cohort["latest_quote_json"] or "{}")
            if any(s in prior_quotes and float(quotes[s]["market_at"]) < float(prior_quotes[s]["market_at"]) for s in SYMBOLS):
                return
            transaction_now, rollback = self._clock_sample(cohort["clock_high_water"])
            decision_now = max(transaction_now, receipt_at)
            db.execute("UPDATE cohorts SET clock_high_water=max(coalesce(clock_high_water,0),?) WHERE id=?",
                (transaction_now, cohort_id))
            terminalized = False
            if (cohort["session_status"] in {"active", "paused"} and
                    (rollback or (cohort["session_deadline_at"] is not None and transaction_now >= float(cohort["session_deadline_at"])) )):
                terminal_status = "clock_suspect" if rollback else ("expired_pending_close" if cohort["expiry_policy"] == "CLOSE" else "expired_exit_only")
                terminalized = self._terminalize_locked(db, cohort, when=transaction_now,
                    reason="Clock integrity failed; session entry permission is closed." if rollback else
                        f"Session deadline reached at {utc_iso(float(cohort['session_deadline_at']))}; new entries are permanently disabled.",
                    status=terminal_status, clock_suspect=rollback)
                cohort = self._latest(db)
            if generation is not None and int(cohort["launch_generation"]) != generation:
                return
            start_at = float(cohort["session_started_at"] or cohort["start_at"] or 0)
            session_status = cohort["session_status"] or "not_started"
            deadline = float(cohort["session_deadline_at"] or 0)
            entry_window = (session_status == "active" and not bool(cohort["entry_disabled"]) and
                deadline > 0 and transaction_now < deadline and request_at < deadline and receipt_at < deadline and
                request_at > start_at and receipt_at < deadline)
            exit_only = session_status in {"expired_exit_only", "stopped_exit_only"}
            session_config = json.loads(cohort["session_config_json"] or "{}")
            protection_config = session_config.get("position_protection", {})
            protective_only = cohort["state"] == "protection_pending"
            db.execute("INSERT INTO quote_batches VALUES(?,?,?,?)", (cohort_id, request_at, receipt_at, batch_hash))
            for symbol in SYMBOLS:
                quote = quotes[symbol]
                raw = quote["price"]
                account_rows = db.execute("SELECT * FROM accounts WHERE cohort_id=? AND symbol=?", (cohort_id, symbol)).fetchall()
                for account in account_rows:
                    # A quote-only protection worker manages only exits that
                    # were already latched before monitoring paused. Other
                    # accounts remain untouched until explicit resume.
                    if protective_only and not account["exit_requested"]:
                        continue
                    equity = float(account["cash"]) + float(account["qty"]) * raw
                    nlv = float(account["cash"]) + float(account["qty"]) * raw * (1-SLIP_BPS/10000) * (1-FEE_BPS/10000)
                    peak = max(float(account["peak_nlv"]), nlv)
                    dd = max(0.0, 1 - nlv/peak) if peak else 0.0
                    original_priority = int(account["exit_priority"] or 0)
                    # Manual/session CLOSE and existing account-drawdown behavior
                    # outrank per-position protection. A newly crossed DD is a
                    # decision on this quote, never a same-quote fill.
                    if not account["halted"] and dd >= DRAWNDOWN_LIMIT:
                        db.execute("UPDATE accounts SET halted=1,peak_nlv=?,max_drawdown=max(max_drawdown,?),updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                            (peak, dd, receipt_at, cohort_id, symbol, account["strategy"]))
                        account = dict(account); account["halted"] = 1
                        if not account["exit_requested"] or original_priority < EXIT_PRIORITY["account_drawdown"]:
                            self._latch_exit(db, account, reason="account_drawdown", decided_at=decision_now,
                                market_at=quote["market_at"], request_at=request_at, receipt_at=receipt_at,
                                price=raw, basis=float(account["basis"]), detail="Existing 10% account net-liquidation drawdown halt.")
                            account = db.execute("SELECT * FROM accounts WHERE cohort_id=? AND symbol=? AND strategy=?",
                                (cohort_id, symbol, account["strategy"])).fetchone()
                            # The current quote establishes the halt; it cannot fill it.
                            db.execute("UPDATE accounts SET last_price=?,last_market_at=?,last_receipt_at=?,peak_nlv=?,max_drawdown=max(max_drawdown,?),updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                                (raw, quote["market_at"], receipt_at, peak, dd, receipt_at, cohort_id, symbol, account["strategy"]))
                            db.execute("INSERT OR IGNORE INTO marks(cohort_id,symbol,strategy,market_at,request_at,receipt_at,price,equity,net_liquidation,drawdown,exposure) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (cohort_id, symbol, account["strategy"], quote["market_at"], request_at, receipt_at, raw, equity, nlv, dd,
                                 float(account["qty"])*raw/equity if equity > 0 else 0.0))
                            continue
                    # Any close latch is sticky. A higher-priority event records
                    # a new decision timestamp; only a subsequent fresh batch
                    # with all timestamps strictly later may execute it.
                    if account["exit_requested"]:
                        close_after = max(float(account["exit_decided_at"] or 0), start_at)
                        fresh_after_decision = (float(quote["market_at"]) > close_after and
                            request_at > close_after and receipt_at > close_after)
                        if fresh_after_decision:
                            qty = float(account["qty"])
                            if qty > 0:
                                sell = risk.solve_trade(float(account["cash"]), qty, raw, 0.0, FEE_BPS, SLIP_BPS)
                                if sell:
                                    proceeds = sell["cash"] - float(account["cash"])
                                    basis = float(account["basis"])
                                    pnl = proceeds - basis
                                    reason = account["exit_reason"] or "manual_close"
                                    signal_key = int(float(account["exit_decided_at"]))
                                    episode_id = account["episode_id"]
                                    self._record_protection_event(db, account, event_type="FILL", reason=reason,
                                        priority=int(account["exit_priority"] or EXIT_PRIORITY.get(reason, 0)),
                                        decided_at=decision_now, market_at=quote["market_at"],
                                        request_at=request_at, receipt_at=receipt_at, price=raw,
                                        return_pct=net_position_return_pct(qty, basis, raw), basis=basis,
                                        threshold_pct=float(account["protection_trigger_threshold_pct"]) if account["protection_trigger_threshold_pct"] is not None else None,
                                        detail="Paper SELL filled from a fresh quote after the latched decision; modeled execution costs apply.")
                                    db.execute("INSERT OR IGNORE INTO fills(cohort_id,symbol,strategy,signal_ts,request_at,receipt_at,market_at,side,qty,raw_price,fill_price,fee,slippage,realized_pnl,reason,episode_id,applied_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                        (cohort_id,symbol,account["strategy"],signal_key,request_at,receipt_at,quote["market_at"],"SELL",qty,raw,raw*(1-SLIP_BPS/10000),sell["fee"],sell["slippage"],pnl,reason,episode_id,decision_now))
                                    if episode_id:
                                        db.execute("UPDATE position_episodes SET closed_at=?,close_receipt_at=?,remaining_basis=0,current_qty=0,realized_pnl=realized_pnl+?,status='closed',close_reason=?,pending_reason=NULL WHERE id=?",
                                            (decision_now,receipt_at,pnl,reason,episode_id))
                                    protective_fill = reason in {"take_profit", "stop_loss"}
                                    cooldown = decision_now if protective_fill else account["cooldown_after_at"]
                                    cooldown_receipt = receipt_at if protective_fill else account["cooldown_after_receipt_at"]
                                    db.execute("UPDATE accounts SET cash=?,qty=0,basis=0,fees=fees+?,slippage=slippage+?,realized_pnl=realized_pnl+?,exit_requested=0,exit_reason=NULL,exit_priority=0,episode_id=NULL,position_opened_at=NULL,basis_updated_at=NULL,basis_updated_receipt_at=NULL,basis_updated_market_at=NULL,position_tp_pct=NULL,position_sl_pct=NULL,cooldown_after_at=?,cooldown_after_receipt_at=?,signal_active=0,protection_trigger_reason=NULL,protection_triggered_at=NULL,protection_trigger_price=NULL,protection_trigger_market_at=NULL,protection_trigger_return_pct=NULL,protection_trigger_basis=NULL,protection_trigger_threshold_pct=NULL,last_price=?,last_market_at=?,last_receipt_at=?,peak_nlv=?,max_drawdown=max(max_drawdown,?),updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                                        (sell["cash"],sell["fee"],sell["slippage"],pnl,cooldown,cooldown_receipt,raw,quote["market_at"],receipt_at,max(peak,sell["cash"]),dd,decision_now,cohort_id,symbol,account["strategy"]))
                            else:
                                db.execute("UPDATE accounts SET exit_requested=0,exit_reason=NULL,exit_priority=0,last_price=?,last_market_at=?,last_receipt_at=?,updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                                    (raw,quote["market_at"],receipt_at,receipt_at,cohort_id,symbol,account["strategy"]))
                        else:
                            db.execute("UPDATE accounts SET last_price=?,last_market_at=?,last_receipt_at=?,peak_nlv=?,max_drawdown=max(max_drawdown,?),updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                                (raw,quote["market_at"],receipt_at,peak,dd,receipt_at,cohort_id,symbol,account["strategy"]))
                        continue
                    # Protections are evaluated only while the cohort is actively
                    # monitoring. A quote must be a new provider observation after
                    # the latest basis-changing fill and prior accepted quote.
                    if (not protective_only and cohort["state"] == "running" and
                            session_status in {"active", "expired_exit_only", "stopped_exit_only"} and
                            float(account["qty"]) > 1e-12 and not account["halted"] and
                            account["position_opened_at"] is not None and
                            account["basis_updated_receipt_at"] is not None and
                            account["basis_updated_market_at"] is not None and
                            float(quote["market_at"]) > max(float(account["basis_updated_market_at"]), float(account["basis_updated_at"] or 0), float(account["last_market_at"] or 0)) and
                            request_at > max(float(account["basis_updated_receipt_at"]), float(account["basis_updated_at"] or 0)) and
                            receipt_at > max(float(account["basis_updated_receipt_at"]), float(account["basis_updated_at"] or 0))):
                        position_return = net_position_return_pct(float(account["qty"]), float(account["basis"]), raw)
                        hit_reason = None
                        hit_threshold = None
                        if account["position_sl_pct"] is not None and position_return is not None and _reaches_protection_threshold(position_return,float(account["position_sl_pct"]),take_profit=False):
                            hit_reason, hit_threshold = "stop_loss", float(account["position_sl_pct"])
                        elif account["position_tp_pct"] is not None and position_return is not None and _reaches_protection_threshold(position_return,float(account["position_tp_pct"]),take_profit=True):
                            hit_reason, hit_threshold = "take_profit", float(account["position_tp_pct"])
                        if hit_reason:
                            self._latch_exit(db, account, reason=hit_reason, decided_at=decision_now,
                                market_at=quote["market_at"], request_at=request_at, receipt_at=receipt_at,
                                price=raw, return_pct=position_return, basis=float(account["basis"]),
                                threshold_pct=hit_threshold,
                                detail="Threshold crossed on remaining-lot net liquidation after modeled entry and estimated exit costs.")
                            account = db.execute("SELECT * FROM accounts WHERE cohort_id=? AND symbol=? AND strategy=?",
                                (cohort_id, symbol, account["strategy"])).fetchone()
                            db.execute("UPDATE accounts SET last_price=?,last_market_at=?,last_receipt_at=?,peak_nlv=?,max_drawdown=max(max_drawdown,?),updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                                (raw,quote["market_at"],receipt_at,peak,dd,receipt_at,cohort_id,symbol,account["strategy"]))
                            continue
                    if (account["halted"] or cohort["state"] in {"exiting", "protection_pending"} or
                            session_status in {"clock_suspect", "legacy_untimed", "finished", "not_started"}):
                        db.execute("UPDATE accounts SET peak_nlv=?,max_drawdown=max(max_drawdown,?),last_price=?,last_market_at=?,last_receipt_at=?,updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                            (peak,dd,raw,quote["market_at"],receipt_at,receipt_at,cohort_id,symbol,account["strategy"]))
                        continue
                    # Resolve expired or fresh-on-time scheduled actions.
                    pending = db.execute("SELECT * FROM decisions WHERE cohort_id=? AND symbol=? AND strategy=? AND status='pending' ORDER BY signal_ts", (cohort_id,symbol,account["strategy"])).fetchall()
                    for decision in pending:
                        if max(float(quote["market_at"]), request_at, receipt_at) >= float(decision["expires_at"]):
                            db.execute("UPDATE decisions SET status='skipped',reason='04:15 UTC expiry passed; late quote cannot revive the decision.' WHERE id=?", (decision["id"],))
                            continue
                        strict_after = max(float(decision["created_at"]), float(decision["due_at"]), start_at)
                        if min(float(quote["market_at"]), request_at, receipt_at) <= strict_after:
                            continue
                        desired = decision["target_weight"]
                        if desired is None:
                            db.execute("UPDATE decisions SET status='skipped',reason='No valid target weight; no fill.' WHERE id=?", (decision["id"],))
                            continue
                        current_weight = float(account["qty"])*raw/equity if equity > 0 else 0.0
                        desired = min(.5, max(0.0, float(desired)))
                        if session_status == "active" and not entry_window:
                            db.execute("UPDATE decisions SET status='skipped',reason='Session entry deadline or accepted-start boundary has passed; no late entry is allowed.' WHERE id=?", (decision["id"],))
                            continue
                        if exit_only and desired > current_weight + 1e-12:
                            db.execute("UPDATE decisions SET status='no_trade',reason='Exit-only session: new or increased exposure is blocked; reductions remain allowed.' WHERE id=?", (decision["id"],))
                            continue
                        should_trade = (desired == 0 and float(account["qty"]) > 0) or current_weight > .5 + 1e-12 or abs(current_weight-desired) >= risk.REBAND
                        if should_trade:
                            target = desired if decision["target_weight"] is not None else .5
                            order = risk.solve_trade(float(account["cash"]), float(account["qty"]), raw, target, FEE_BPS, SLIP_BPS)
                        else:
                            order = None
                        if exit_only and order and order["side"] == "BUY":
                            order = None
                        if order:
                            before_qty=float(account["qty"]); after_qty=order["qty"]
                            side=order["side"]; fill_price=raw*(1+SLIP_BPS/10000) if side=="BUY" else raw*(1-SLIP_BPS/10000)
                            pnl=0.0; basis=float(account["basis"]); episode_id=account["episode_id"]
                            if side=="BUY":
                                basis_added=order["raw_notional"]+order["fee"]+order["slippage"]
                                basis += basis_added
                                if before_qty <= 1e-12:
                                    episode_id=str(uuid.uuid4())
                                    position_tp_pct=protection_config.get("take_profit_pct")
                                    position_sl_pct=protection_config.get("stop_loss_pct")
                                    position_opened_at=decision_now
                                    db.execute("INSERT INTO position_episodes(id,cohort_id,symbol,strategy,opened_at,opening_market_at,basis_updated_at,basis_updated_receipt_at,basis_updated_market_at,opening_basis,remaining_basis,opened_qty,current_qty,take_profit_pct,stop_loss_pct,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')",
                                        (episode_id,cohort_id,symbol,account["strategy"],decision_now,quote["market_at"],decision_now,receipt_at,quote["market_at"],basis_added,basis_added,order["quantity"],after_qty,position_tp_pct,position_sl_pct))
                                else:
                                    position_tp_pct=account["position_tp_pct"]
                                    position_sl_pct=account["position_sl_pct"]
                                    position_opened_at=account["position_opened_at"]
                                    if episode_id:
                                        db.execute("UPDATE position_episodes SET opening_basis=opening_basis+?,remaining_basis=remaining_basis+?,opened_qty=opened_qty+?,current_qty=?,basis_updated_at=?,basis_updated_receipt_at=?,basis_updated_market_at=? WHERE id=?",
                                            (basis_added,basis_added,order["quantity"],after_qty,decision_now,receipt_at,quote["market_at"],episode_id))
                            else:
                                closed=min(before_qty,order["quantity"])
                                portion=basis*(closed/before_qty) if before_qty else 0.0
                                proceeds=order["raw_notional"]-order["fee"]-order["slippage"]
                                pnl=proceeds-portion; basis=max(0.0,basis-portion)
                                position_tp_pct=account["position_tp_pct"]
                                position_sl_pct=account["position_sl_pct"]
                                position_opened_at=account["position_opened_at"]
                                if episode_id:
                                    if after_qty <= 1e-12:
                                        db.execute("UPDATE position_episodes SET closed_at=?,close_receipt_at=?,remaining_basis=0,current_qty=0,realized_pnl=realized_pnl+?,status='closed',close_reason='strategy_exit',pending_reason=NULL,basis_updated_at=?,basis_updated_receipt_at=?,basis_updated_market_at=? WHERE id=?",
                                            (decision_now,receipt_at,pnl,decision_now,receipt_at,quote["market_at"],episode_id))
                                    else:
                                        db.execute("UPDATE position_episodes SET remaining_basis=?,current_qty=?,realized_pnl=realized_pnl+?,basis_updated_at=?,basis_updated_receipt_at=?,basis_updated_market_at=? WHERE id=?",
                                            (basis,after_qty,pnl,decision_now,receipt_at,quote["market_at"],episode_id))
                            reason="target_rebalance"
                            db.execute("INSERT OR IGNORE INTO fills(cohort_id,symbol,strategy,signal_ts,request_at,receipt_at,market_at,side,qty,raw_price,fill_price,fee,slippage,realized_pnl,reason,episode_id,applied_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (cohort_id,symbol,account["strategy"],decision["signal_ts"],request_at,receipt_at,quote["market_at"],side,order["quantity"],raw,fill_price,order["fee"],order["slippage"],pnl,reason,episode_id,decision_now))
                            if side=="SELL" and after_qty <= 1e-12:
                                basis=0.0
                                episode_id=None; position_tp_pct=None; position_sl_pct=None; position_opened_at=None
                            basis_receipt=receipt_at if side=="BUY" or after_qty > 1e-12 else None
                            basis_market=quote["market_at"] if side=="BUY" or after_qty > 1e-12 else None
                            basis_applied=decision_now if side=="BUY" or after_qty > 1e-12 else None
                            db.execute("UPDATE accounts SET cash=?,qty=?,basis=?,episode_id=?,position_opened_at=?,basis_updated_at=?,basis_updated_receipt_at=?,basis_updated_market_at=?,position_tp_pct=?,position_sl_pct=?,fees=fees+?,slippage=slippage+?,realized_pnl=realized_pnl+?,last_price=?,last_market_at=?,last_receipt_at=?,peak_nlv=?,max_drawdown=max(max_drawdown,?),updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                                (order["cash"],after_qty,basis,episode_id,position_opened_at,basis_applied,basis_receipt,basis_market,position_tp_pct,position_sl_pct,order["fee"],order["slippage"],pnl,raw,quote["market_at"],receipt_at,peak,dd,decision_now,cohort_id,symbol,account["strategy"]))
                        db.execute("UPDATE decisions SET status=?,reason=? WHERE id=?", ("filled" if order else "no_trade", "Filled from a new post-decision quote." if order else ("Exit-only session blocked an increase; no fill." if exit_only else "Target already within the 2.5 percentage-point no-trade band; no fill."), decision["id"]))
                        account = db.execute("SELECT * FROM accounts WHERE cohort_id=? AND symbol=? AND strategy=?", (cohort_id,symbol,account["strategy"])).fetchone()
                        equity = float(account["cash"]) + float(account["qty"])*raw
                    # Post-trade sampled exposure; NLV remains the drawdown basis.
                    equity = float(account["cash"]) + float(account["qty"])*raw
                    nlv = float(account["cash"]) + float(account["qty"])*raw*(1-SLIP_BPS/10000)*(1-FEE_BPS/10000)
                    peak=max(peak,nlv); dd=max(dd,1-nlv/peak if peak else 0.0)
                    exposure=float(account["qty"])*raw/equity if equity>0 else 0.0
                    db.execute("UPDATE accounts SET last_price=?,last_market_at=?,last_receipt_at=?,peak_nlv=?,max_drawdown=max(max_drawdown,?),updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                        (raw,quote["market_at"],receipt_at,peak,dd,receipt_at,cohort_id,symbol,account["strategy"]))
                    db.execute("INSERT OR IGNORE INTO marks(cohort_id,symbol,strategy,market_at,request_at,receipt_at,price,equity,net_liquidation,drawdown,exposure) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (cohort_id,symbol,account["strategy"],quote["market_at"],request_at,receipt_at,raw,equity,nlv,dd,exposure))
            db.execute("UPDATE cohorts SET last_quote_request=?,last_quote_receipt=?,latest_quote_json=?,forward_days=max(forward_days,?) WHERE id=?",
                (request_at,receipt_at,json.dumps(quotes,sort_keys=True),max(0.0,(receipt_at-float((db.execute("SELECT start_at FROM cohorts WHERE id=?",(cohort_id,)).fetchone() or [0])[0] or receipt_at))/86400),cohort_id))
            final_cohort = db.execute("SELECT state,session_status FROM cohorts WHERE id=?", (cohort_id,)).fetchone()
            if final_cohort["state"] == "exiting":
                remaining = db.execute("SELECT count(*) FROM accounts WHERE cohort_id=? AND (qty>1e-12 OR exit_requested=1)", (cohort_id,)).fetchone()[0]
                if not remaining:
                    db.execute("UPDATE cohorts SET state='finished',session_status='finished',entry_disabled=1,reason=?,error=NULL WHERE id=?",
                        ("All requested positions exited on fresh quotes; the session is finished.", cohort_id))
            elif final_cohort["state"] == "protection_pending":
                remaining = db.execute("SELECT count(*) FROM accounts WHERE cohort_id=? AND exit_requested=1 AND exit_priority BETWEEN ? AND ?",
                    (cohort_id, EXIT_PRIORITY["take_profit"], EXIT_PRIORITY["account_drawdown"])).fetchone()[0]
                if not remaining:
                    db.execute("UPDATE cohorts SET state='paused',close_retry_after=NULL,reason=?,error=NULL WHERE id=? AND state='protection_pending'",
                        ("The latched protective close filled from a later fresh quote. Signal monitoring remains paused; resume explicitly after reviewing the cooldown.", cohort_id))
            else:
                active_positions = db.execute("SELECT count(*) FROM accounts WHERE cohort_id=? AND qty>1e-12", (cohort_id,)).fetchone()[0]
                if not active_positions and final_cohort["session_status"] in {"expired_exit_only", "stopped_exit_only", "clock_suspect"}:
                    db.execute("UPDATE cohorts SET state='finished',session_status='finished',entry_disabled=1,reason=?,error=NULL WHERE id=?",
                        ("All accounts are flat after the exit-only session; management is finished.", cohort_id))

    def _pause_error(self, cohort_id: str, message: str, generation: int | None = None) -> None:
        observed_now = self._advance_session_clock()
        with self.db() as db:
            row = db.execute("SELECT * FROM cohorts WHERE id=?", (cohort_id,)).fetchone()
            if (row and row["state"] in {"starting", "running", "exiting", "protection_pending"} and
                    (generation is None or int(row["launch_generation"]) == generation)):
                close_pending = row["session_status"] in {"expired_pending_close", "stopped_pending_close", "clock_suspect"}
                protection_pending = db.execute("SELECT count(*) FROM accounts WHERE cohort_id=? AND exit_requested=1 AND exit_priority BETWEEN ? AND ?",
                    (cohort_id, EXIT_PRIORITY["take_profit"], EXIT_PRIORITY["account_drawdown"])).fetchone()[0] > 0
                retry_at = observed_now + self.poll_seconds if close_pending or protection_pending else None
                session_status = "paused" if row["session_status"] == "active" and not close_pending else row["session_status"]
                if protection_pending and not close_pending and row["state"] != "exiting":
                    db.execute("UPDATE cohorts SET state='protection_pending',session_status=?,reason=?,error=?,close_retry_after=? WHERE id=?",
                        (session_status, message + " Latched closes retry using quotes only; signals stay paused.", message, retry_at, cohort_id))
                    db.execute("UPDATE decisions SET status='cancelled',reason='Feed unavailable; the latched close remains active and scheduled entries are cancelled.' WHERE cohort_id=? AND status='pending'", (cohort_id,))
                else:
                    db.execute("UPDATE cohorts SET state='paused',session_status=?,reason=?,error=?,close_retry_after=? WHERE id=?",
                        (session_status, message, message, retry_at, cohort_id))
                if row["session_status"] in {"expired_exit_only", "stopped_exit_only"}:
                    self._cancel_pending_entries_keep_reductions(db, cohort_id,
                        "Public feed error; pending entries/increases were cancelled. A pending reduction remains eligible for a fresh quote.")
                elif not protection_pending:
                    db.execute("UPDATE decisions SET status='cancelled',reason='Public feed error; no catch-up after service recovery.' WHERE cohort_id=? AND status='pending'", (cohort_id,))

    def export_csv(self) -> str:
        output = io.StringIO()
        fields = ["record_type","cohort_id","cohort_state","session_id","session_started_at_utc","session_deadline_utc",
            "session_duration_minutes","expiry_policy","session_status","entry_disabled","clock_high_water_utc",
            "session_decided_at_utc","terminal_reason","started_at_utc","resume_at_utc","config_sha256","config_json",
            "session_config_json","protection_schema_version","symbol","strategy","signal_utc","created_or_request_utc",
            "due_or_receipt_utc","provider_market_utc","status_or_side","target_weight","qty","raw_price","fill_price",
            "fee_usd","slippage_usd","realized_pnl_usd","reason","episode_id","opened_at_utc","closed_at_utc",
            "take_profit_pct","stop_loss_pct","opening_basis_usd","remaining_basis_usd","net_return_pct",
            "threshold_pct","exit_priority","protection_decided_at_utc","applied_at_utc","basis_applied_at_utc",
            "close_receipt_at_utc","pending_reason","protection_trigger_reason","detail"]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        with self.db() as db:
            cohorts = db.execute("SELECT * FROM cohorts ORDER BY created_at,id").fetchall()
            for cohort in cohorts:
                cid = cohort["id"]
                meta = {"cohort_id": cid, "cohort_state": cohort["state"], "session_id": cohort["session_id"],
                    "session_started_at_utc": utc_iso(cohort["session_started_at"]),
                    "session_deadline_utc": utc_iso(cohort["session_deadline_at"]),
                    "session_duration_minutes": cohort["session_duration_minutes"], "expiry_policy": cohort["expiry_policy"],
                    "session_status": cohort["session_status"], "entry_disabled": bool(cohort["entry_disabled"]),
                    "clock_high_water_utc": utc_iso(cohort["clock_high_water"]),
                    "session_decided_at_utc": utc_iso(cohort["session_decided_at"]), "terminal_reason": cohort["terminal_reason"],
                    "started_at_utc": utc_iso(cohort["start_at"]), "resume_at_utc": utc_iso(cohort["resume_at"]),
                    "config_sha256": cohort["config_hash"], "config_json": cohort["config_json"],
                    "session_config_json": cohort["session_config_json"],
                    "protection_schema_version": cohort["protection_schema_version"]}
                writer.writerow({**meta, "record_type":"cohort", "reason":cohort["reason"] or ""})
                for row in db.execute("SELECT * FROM decisions WHERE cohort_id=? ORDER BY signal_ts,symbol,strategy", (cid,)):
                    writer.writerow({**meta, "record_type":"decision", "symbol":row["symbol"],"strategy":row["strategy"],
                        "signal_utc":utc_iso(row["signal_ts"]),"created_or_request_utc":utc_iso(row["created_at"]),
                        "due_or_receipt_utc":utc_iso(row["due_at"]),"status_or_side":row["status"],
                        "target_weight":row["target_weight"],"reason":row["reason"]})
                for row in db.execute("SELECT * FROM fills WHERE cohort_id=? ORDER BY id", (cid,)):
                    writer.writerow({**meta, "record_type":"fill", "symbol":row["symbol"],"strategy":row["strategy"],
                        "signal_utc":utc_iso(row["signal_ts"]),"created_or_request_utc":utc_iso(row["request_at"]),
                        "due_or_receipt_utc":utc_iso(row["receipt_at"]),"provider_market_utc":utc_iso(row["market_at"]),
                        "status_or_side":row["side"],"qty":row["qty"],"raw_price":row["raw_price"],
                        "fill_price":row["fill_price"],"fee_usd":row["fee"],"slippage_usd":row["slippage"],
                        "realized_pnl_usd":row["realized_pnl"],"reason":row["reason"],"episode_id":row["episode_id"],
                        "applied_at_utc":utc_iso(row["applied_at"])})
                for row in db.execute("SELECT * FROM position_episodes WHERE cohort_id=? ORDER BY opened_at,id", (cid,)):
                    writer.writerow({**meta, "record_type":"position_episode", "symbol":row["symbol"],"strategy":row["strategy"],
                        "episode_id":row["id"],"opened_at_utc":utc_iso(row["opened_at"]),"closed_at_utc":utc_iso(row["closed_at"]),
                        "take_profit_pct":row["take_profit_pct"],"stop_loss_pct":row["stop_loss_pct"],
                        "opening_basis_usd":row["opening_basis"],"remaining_basis_usd":row["remaining_basis"],
                        "realized_pnl_usd":row["realized_pnl"],"status_or_side":row["status"],
                        "reason":row["close_reason"] or row["pending_reason"] or "",
                        "pending_reason":row["pending_reason"] or "",
                        "protection_trigger_reason":row["protection_trigger_reason"] or "",
                        "protection_decided_at_utc":utc_iso(row["protection_triggered_at"]),
                        "applied_at_utc":utc_iso(row["opened_at"]),"basis_applied_at_utc":utc_iso(row["basis_updated_at"]),
                        "close_receipt_at_utc":utc_iso(row["close_receipt_at"])})
                for row in db.execute("SELECT * FROM protection_events WHERE cohort_id=? ORDER BY id", (cid,)):
                    writer.writerow({**meta, "record_type":"protection_event", "symbol":row["symbol"],"strategy":row["strategy"],
                        "episode_id":row["episode_id"],"status_or_side":row["event_type"],"reason":row["reason"],
                        "signal_utc":utc_iso(row["decided_at"]),"created_or_request_utc":utc_iso(row["request_at"]),
                        "due_or_receipt_utc":utc_iso(row["receipt_at"]),"provider_market_utc":utc_iso(row["market_at"]),
                        "raw_price":row["price"],"net_return_pct":row["net_return_pct"],
                        "remaining_basis_usd":row["remaining_basis"],"threshold_pct":row["threshold_pct"],
                        "exit_priority":row["priority"],"protection_decided_at_utc":utc_iso(row["decided_at"]),
                        "detail":row["detail"]})
        return output.getvalue()
