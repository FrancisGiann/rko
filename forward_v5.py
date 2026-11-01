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
import threading
import time
import uuid
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
                  close_retry_after REAL);
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
                  updated_at REAL NOT NULL, PRIMARY KEY(cohort_id,symbol,strategy));
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
                  reason TEXT NOT NULL, UNIQUE(cohort_id,symbol,strategy,signal_ts,side));
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
            }
            for name, declaration in session_columns.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE cohorts ADD COLUMN {name} {declaration}")
            row = db.execute("SELECT * FROM cohorts ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
            if row is None:
                config = self._config()
                now = float(self.clock())
                db.execute("INSERT INTO cohorts(id,state,created_at,config_json,session_status,clock_high_water) VALUES(?,?,?,?,?,?)",
                    (str(uuid.uuid4()), "not_started", now, json.dumps(config, sort_keys=True), "not_started", now))
            else:
                now = float(self.clock())
                # V5 cohorts had no timer. Preserve every account and ledger row,
                # but fail closed for new entries; an explicit fresh-quote exit
                # remains available for any existing paper holding.
                if row["session_started_at"] is None and row["start_at"] is not None:
                    db.execute("UPDATE cohorts SET session_status='legacy_untimed',entry_disabled=1,terminal_reason=?,clock_high_water=max(coalesce(clock_high_water,0),?) WHERE id=?",
                        ("Migrated untimed V5 cohort; entries disabled. Existing holdings are preserved for fresh-quote closure.", now, row["id"]))
                    db.execute("UPDATE decisions SET status='cancelled',reason='Migrated untimed V5 cohort; new entries disabled.' WHERE cohort_id=? AND status='pending'", (row["id"],))
                if row["state"] in {"starting", "running", "exiting"}:
                    clock_close = row["session_status"] == "clock_suspect" and row["expiry_policy"] == "CLOSE" and db.execute(
                        "SELECT count(*) FROM accounts WHERE cohort_id=? AND (qty>1e-12 OR exit_requested=1)", (row["id"],)).fetchone()[0] > 0
                    timed_close = row["session_status"] in {"expired_pending_close", "stopped_pending_close"} or clock_close
                    if timed_close:
                        db.execute("UPDATE cohorts SET state='exiting',reason=?,error=NULL,launch_generation=launch_generation+1 WHERE id=?",
                            ("Server restarted during an authorized close-only session; waiting for a fresh post-decision quote.", row["id"]))
                    else:
                        db.execute("UPDATE cohorts SET state='paused',session_status=CASE WHEN session_status='active' THEN 'paused' ELSE session_status END,reason=?,error=NULL,launch_generation=launch_generation+1 WHERE id=?",
                            ("Server restarted; pending scheduled entries were cancelled. Resume explicitly; the original session deadline does not change.", row["id"]))
                        if row["session_status"] in {"expired_exit_only", "stopped_exit_only"}:
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
            db.execute("UPDATE accounts SET exit_requested=1,exit_decided_at=CASE WHEN exit_requested=1 THEN exit_decided_at ELSE ? END,updated_at=? WHERE cohort_id=? AND qty>1e-12",
                (when, when, cid))
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

    def _advance_session_clock(self) -> float:
        """Persist clock high-water and enforce deadline/rollback atomically."""
        launch_close = False
        launch_management = False
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
        if terminal_changed:
            self.wake.set()
        if launch_close:
            self._ensure_pending_close_worker()
        elif launch_management:
            self._ensure_exit_only_worker()
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
            fills = [dict(x) for x in db.execute("SELECT symbol,strategy,signal_ts,request_at,receipt_at,market_at,side,qty,raw_price,fill_price,fee,slippage,realized_pnl,reason FROM fills WHERE cohort_id=? ORDER BY id DESC LIMIT 100", (cid,))]
            baseline = json.loads(cohort["baseline_json"] or "{}")
            last_daily = json.loads(cohort["last_daily_ts_json"] or "{}")
            meta = json.loads(cohort["warmup_meta_json"] or "{}")
            config = json.loads(cohort["config_json"])
            latest_quotes = json.loads(cohort["latest_quote_json"] or "{}")
            start_at = cohort["start_at"]
            account_views = []
            for a in accounts:
                price = a["last_price"]
                equity = a["cash"] + (a["qty"] * price if price is not None else 0.0)
                initial = a["initial"]
                nlv = a["cash"] + (a["qty"] * price * (1 - SLIP_BPS/10000) * (1 - FEE_BPS/10000) if price is not None else 0.0)
                account_views.append({**a, "equity": equity, "net_liquidation": nlv,
                    "return_pct": (equity / initial - 1) * 100 if initial else 0.0,
                    "asset_weight_pct": (a["qty"] * price / equity * 100 if price is not None and equity > 0 else 0.0),
                    "status": "halted" if a["halted"] else "exit_pending" if a["exit_requested"] else cohort["state"]})
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
            elapsed_calendar_days = max(0.0, (now - float(start_at)) / 86400) if start_at is not None else 0.0
            forward_days = float(forward_days_count)
        due_ts = next_eligible
        return {"cohort_id": cid, "state": cohort["state"], "started_at": utc_iso(start_at),
            "resume_at": utc_iso(cohort["resume_at"]), "config": config, "config_hash": cohort["config_hash"],
            "server_now_epoch": now, "server_now_utc": utc_iso(now),
            "session_id": cohort["session_id"], "session_started_at_utc": utc_iso(cohort["session_started_at"]),
            "session_deadline_utc": utc_iso(cohort["session_deadline_at"]),
            "session_deadline_epoch": cohort["session_deadline_at"],
            "session_duration_minutes": cohort["session_duration_minutes"],
            "expiry_policy": cohort["expiry_policy"], "session_status": cohort["session_status"],
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
            "totals": {"fills": fills_count, "recorded_decisions": decisions_count,
                "forward_observations": forward_observations, "post_start_marks": marks_count,
                "all_forward_fills": fills_count}}

    def control(self, action: str, *, confirm: str | None = None,
                duration_minutes: int | None = None, expiry_policy: str | None = None) -> dict:
        if action not in {"start", "pause", "resume", "exit", "stop", "reset"}:
            raise ValueError("Choose start, pause, resume, exit, stop or reset.")
        if action == "start":
            if confirm != "START":
                raise ValueError("Start requires explicit confirmation of the displayed duration and expiry policy.")
            if isinstance(duration_minutes, bool) or not isinstance(duration_minutes, int) or not 1 <= duration_minutes <= 259200:
                raise ValueError("Session duration must be an integer from 1 to 259200 minutes.")
            if expiry_policy not in {"CLOSE", "EXIT_ONLY"}:
                raise ValueError("Choose CLOSE or EXIT_ONLY for the session expiry policy.")
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
                config_hash = hashlib.sha256(_canonical({"id": cid, "session_id": session_id,
                    "start_at": start_at, "deadline_at": deadline, "duration_minutes": duration_minutes,
                    "expiry_policy": expiry_policy, "config": config})).hexdigest()
                generation = int(row["launch_generation"]) + 1
                db.execute("UPDATE cohorts SET state='starting',start_at=?,resume_at=NULL,config_hash=?,reason=?,error=NULL,launch_generation=?,session_id=?,session_started_at=?,session_deadline_at=?,session_duration_minutes=?,expiry_policy=?,session_status='active',entry_disabled=0,clock_high_water=max(coalesce(clock_high_water,0),?),session_decided_at=NULL,terminal_reason=NULL WHERE id=?",
                    (start_at, config_hash, "Session timer started at server acceptance; warmup counts toward the deadline and is not performance.", generation, session_id, start_at, deadline, duration_minutes, expiry_policy, start_at, cid))
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
                next_session = "paused" if session == "active" else session
                db.execute("UPDATE cohorts SET state='paused',session_status=?,reason=?,error=NULL,launch_generation=launch_generation+1 WHERE id=?",
                    (next_session, "Paused by user. Existing positions remain held; the original session deadline continues without extension.", cid))
                if session in {"expired_exit_only", "stopped_exit_only"}:
                    self._cancel_pending_entries_keep_reductions(db,cid,
                        "Exit-only management paused; entries/increases are cancelled, but an already scheduled reduction remains eligible for a fresh quote before expiry.")
                else:
                    db.execute("UPDATE decisions SET status='cancelled',reason='User paused; scheduled action cancelled.' WHERE cohort_id=? AND status='pending'", (cid,))
                self.wake.set()
            elif action == "exit":
                # Explicit fresh-quote safety override. It closes the entry
                # window even if the stored policy was EXIT_ONLY or the old
                # cohort was migrated without a timer.
                if session in {"finished", "not_started"}:
                    raise RuntimeError("There is no open paper session to close.")
                generation = int(row["launch_generation"]) + 1
                db.execute("UPDATE accounts SET exit_requested=1,exit_decided_at=?,updated_at=? WHERE cohort_id=? AND qty>1e-12 AND exit_requested=0", (now, now, cid))
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
                db.execute("INSERT INTO cohorts(id,state,created_at,config_json,session_status,clock_high_water,reason) VALUES(?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), "not_started", now, json.dumps(self._config(), sort_keys=True), "not_started", now,
                     "New cohort identity. Prior cohorts, timer choices, fills and decisions remain archived."))
                self.wake.set()
        if launch_mode and launch_id:
            if launch_mode == "close_only":
                self._ensure_pending_close_worker()
            elif launch_mode == "management_only":
                self._ensure_exit_only_worker()
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
            if mode not in {"exit_only", "close_only", "management_only"}:
                if not self._initialize(cohort_id, mode, generation):
                    return
                self.wake.wait(self.poll_seconds)
                self.wake.clear()
            while not self.stop_event.is_set() and self._is_current(cohort_id, ("running", "exiting"), generation):
                if self.stop_event.is_set():
                    break
                try:
                    now = self._advance_session_clock()
                    if self._is_current(cohort_id, ("running",), generation):
                        self._maybe_refresh_history(cohort_id, now)
                    if not self._is_current(cohort_id, ("running", "exiting"), generation):
                        break
                    quotes = validate_batch(self.fetch_quotes(), self._advance_session_clock())
                    self._process_quote_batch(cohort_id, quotes, self._advance_session_clock(), generation=generation)
                except Exception as exc:
                    self._pause_error(cohort_id, f"Public feed unavailable or invalid: {str(exc)[:300]}", generation)
                    break
                if not self._is_current(cohort_id, ("running", "exiting"), generation):
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
        if not self._is_current(cohort_id, ("running", "exiting"), generation):
            return
        request_at = quotes[SYMBOLS[0]]["requested_at"]
        receipt_at = max(quotes[s]["observed_at"] for s in SYMBOLS)
        batch_hash = hashlib.sha256(_canonical(quotes)).hexdigest()
        with self.db() as db:
            cohort = db.execute("SELECT * FROM cohorts WHERE id=?", (cohort_id,)).fetchone()
            if not cohort or cohort["state"] not in {"running", "exiting"}:
                return
            if generation is not None and int(cohort["launch_generation"]) != generation:
                return
            transaction_now, rollback = self._clock_sample(cohort["clock_high_water"])
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
            db.execute("INSERT OR IGNORE INTO quote_batches VALUES(?,?,?,?)", (cohort_id, request_at, receipt_at, batch_hash))
            for symbol in SYMBOLS:
                quote = quotes[symbol]
                raw = quote["price"]
                account_rows = db.execute("SELECT * FROM accounts WHERE cohort_id=? AND symbol=?", (cohort_id, symbol)).fetchall()
                for account in account_rows:
                    decision_time = float(account["exit_decided_at"] or 0)
                    # Drawdown is measured on quote-based net liquidation. A
                    # breach creates a safety exit decision; the same quote
                    # cannot fill it. The next fresh batch may execute it.
                    equity = float(account["cash"]) + float(account["qty"]) * raw
                    nlv = float(account["cash"]) + float(account["qty"]) * raw * (1-SLIP_BPS/10000) * (1-FEE_BPS/10000)
                    peak = max(float(account["peak_nlv"]), nlv)
                    dd = max(0.0, 1 - nlv/peak) if peak else 0.0
                    just_halted = False
                    if not account["halted"] and dd >= DRAWNDOWN_LIMIT:
                        db.execute("UPDATE accounts SET halted=1,exit_requested=1,exit_decided_at=?,peak_nlv=?,max_drawdown=max(max_drawdown,?),updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                            (receipt_at, peak, dd, receipt_at, cohort_id, symbol, account["strategy"]))
                        db.execute("UPDATE decisions SET status='cancelled',reason='Risk halt; awaiting a fresh post-decision quote.' WHERE cohort_id=? AND symbol=? AND strategy=? AND status='pending'", (cohort_id, symbol, account["strategy"]))
                        account = dict(account); account["halted"] = 1; account["exit_requested"] = 1; account["exit_decided_at"] = receipt_at
                        just_halted = True
                    # A requested safety exit needs provider/request/receipt
                    # timestamps all strictly after its own decision timestamp.
                    close_after = max(float(account["exit_decided_at"] or 0), start_at)
                    if account["exit_requested"] and not just_halted and float(quote["market_at"]) > close_after and request_at > close_after and receipt_at > close_after:
                        qty = float(account["qty"])
                        if qty > 0:
                            sell = risk.solve_trade(float(account["cash"]), qty, raw, 0.0, FEE_BPS, SLIP_BPS)
                            if sell:
                                proceeds = sell["cash"] - float(account["cash"])
                                pnl = proceeds - float(account["basis"])
                                signal_key = int(float(account["exit_decided_at"]))
                                db.execute("INSERT OR IGNORE INTO fills(cohort_id,symbol,strategy,signal_ts,request_at,receipt_at,market_at,side,qty,raw_price,fill_price,fee,slippage,realized_pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (cohort_id,symbol,account["strategy"],signal_key,request_at,receipt_at,quote["market_at"],"SELL",qty,raw,raw*(1-SLIP_BPS/10000),sell["fee"],sell["slippage"],pnl,"fresh_quote_safety_exit"))
                                db.execute("UPDATE accounts SET cash=?,qty=0,basis=0,fees=fees+?,slippage=slippage+?,realized_pnl=realized_pnl+?,exit_requested=0,last_price=?,last_market_at=?,last_receipt_at=?,peak_nlv=?,max_drawdown=max(max_drawdown,?),updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                                    (sell["cash"],sell["fee"],sell["slippage"],pnl,raw,quote["market_at"],receipt_at,max(peak,sell["cash"]),dd,receipt_at,cohort_id,symbol,account["strategy"]))
                        else:
                            db.execute("UPDATE accounts SET exit_requested=0,last_price=?,last_market_at=?,last_receipt_at=?,updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                                (raw,quote["market_at"],receipt_at,receipt_at,cohort_id,symbol,account["strategy"]))
                        # No scheduled decisions are allowed after a safety exit.
                        continue
                    if account["halted"] or cohort["state"] == "exiting" or session_status in {"clock_suspect", "legacy_untimed", "finished", "not_started"}:
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
                            pnl=0.0; basis=float(account["basis"])
                            if side=="BUY": basis += order["raw_notional"]+order["fee"]+order["slippage"]
                            else:
                                closed=min(before_qty,order["quantity"])
                                portion=basis*(closed/before_qty) if before_qty else 0.0
                                proceeds=order["raw_notional"]-order["fee"]-order["slippage"]
                                pnl=proceeds-portion; basis=max(0.0,basis-portion)
                            reason="target_rebalance"
                            db.execute("INSERT OR IGNORE INTO fills(cohort_id,symbol,strategy,signal_ts,request_at,receipt_at,market_at,side,qty,raw_price,fill_price,fee,slippage,realized_pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (cohort_id,symbol,account["strategy"],decision["signal_ts"],request_at,receipt_at,quote["market_at"],side,order["quantity"],raw,fill_price,order["fee"],order["slippage"],pnl,reason))
                            db.execute("UPDATE accounts SET cash=?,qty=?,basis=?,fees=fees+?,slippage=slippage+?,realized_pnl=realized_pnl+?,last_price=?,last_market_at=?,last_receipt_at=?,peak_nlv=?,max_drawdown=max(max_drawdown,?),updated_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                                (order["cash"],after_qty,basis,order["fee"],order["slippage"],pnl,raw,quote["market_at"],receipt_at,peak,dd,receipt_at,cohort_id,symbol,account["strategy"]))
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
            else:
                active_positions = db.execute("SELECT count(*) FROM accounts WHERE cohort_id=? AND qty>1e-12", (cohort_id,)).fetchone()[0]
                if not active_positions and final_cohort["session_status"] in {"expired_exit_only", "stopped_exit_only", "clock_suspect"}:
                    db.execute("UPDATE cohorts SET state='finished',session_status='finished',entry_disabled=1,reason=?,error=NULL WHERE id=?",
                        ("All accounts are flat after the exit-only session; management is finished.", cohort_id))

    def _pause_error(self, cohort_id: str, message: str, generation: int | None = None) -> None:
        observed_now = self._advance_session_clock()
        with self.db() as db:
            row = db.execute("SELECT state,launch_generation,session_status FROM cohorts WHERE id=?", (cohort_id,)).fetchone()
            if (row and row["state"] in {"starting", "running", "exiting"} and
                    (generation is None or int(row["launch_generation"]) == generation)):
                close_pending = row["session_status"] in {"expired_pending_close", "stopped_pending_close", "clock_suspect"}
                retry_at = observed_now + self.poll_seconds if close_pending else None
                session_status = "paused" if row["session_status"] == "active" and not close_pending else row["session_status"]
                db.execute("UPDATE cohorts SET state='paused',session_status=?,reason=?,error=?,close_retry_after=? WHERE id=?",
                    (session_status, message, message, retry_at, cohort_id))
                if row["session_status"] in {"expired_exit_only", "stopped_exit_only"}:
                    self._cancel_pending_entries_keep_reductions(db, cohort_id,
                        "Public feed error; pending entries/increases were cancelled. A pending reduction remains eligible for a fresh quote.")
                else:
                    db.execute("UPDATE decisions SET status='cancelled',reason='Public feed error; no catch-up after service recovery.' WHERE cohort_id=? AND status='pending'", (cohort_id,))

    def export_csv(self) -> str:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["record_type","cohort_id","cohort_state","session_id","session_started_at_utc","session_deadline_utc","session_duration_minutes","expiry_policy","session_status","entry_disabled","clock_high_water_utc","session_decided_at_utc","terminal_reason","started_at_utc","resume_at_utc","config_sha256","config_json","symbol","strategy","signal_utc","created_or_request_utc","due_or_receipt_utc","provider_market_utc","status_or_side","target_weight","qty","raw_price","fill_price","fee_usd","slippage_usd","realized_pnl_usd","reason"])
        with self.db() as db:
            cohorts = db.execute("SELECT * FROM cohorts ORDER BY created_at,id").fetchall()
            for cohort in cohorts:
                cid = cohort["id"]
                config = cohort["config_json"]
                meta = [cid, cohort["state"], cohort["session_id"], utc_iso(cohort["session_started_at"]),
                    utc_iso(cohort["session_deadline_at"]), cohort["session_duration_minutes"], cohort["expiry_policy"],
                    cohort["session_status"], bool(cohort["entry_disabled"]), utc_iso(cohort["clock_high_water"]),
                    utc_iso(cohort["session_decided_at"]), cohort["terminal_reason"], utc_iso(cohort["start_at"]),
                    utc_iso(cohort["resume_at"]), cohort["config_hash"], config]
                writer.writerow(["cohort", *meta, "", "", "", "", "", "", "", "", "", "", "", "", "", "", cohort["reason"] or ""])
                for row in db.execute("SELECT * FROM decisions WHERE cohort_id=? ORDER BY signal_ts,symbol,strategy", (cid,)):
                    writer.writerow(["decision", *meta, row["symbol"],row["strategy"],utc_iso(row["signal_ts"]),utc_iso(row["created_at"]),utc_iso(row["due_at"]),"",row["status"],row["target_weight"],"","","","","","",row["reason"]])
                for row in db.execute("SELECT * FROM fills WHERE cohort_id=? ORDER BY id", (cid,)):
                    writer.writerow(["fill", *meta, row["symbol"],row["strategy"],utc_iso(row["signal_ts"]),utc_iso(row["request_at"]),utc_iso(row["receipt_at"]),utc_iso(row["market_at"]),row["side"],"",row["qty"],row["raw_price"],row["fill_price"],row["fee"],row["slippage"],row["realized_pnl"],row["reason"]])
        return output.getvalue()
