"""Transactional SQLite paper accounts, model snapshots and observed-price fills."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import market
import research as r

LIVE = "live:coingecko"
DEMO = "demo:seed3001-3002"
MAX_DELAY = 600  # Wait at most ten minutes beyond the planned hourly entry.
LEGACY_PUBLIC_NOTICE = ("Legacy public CoinGecko history is read-only: current API terms do not permit using these data for AI model training. "
                        "Use the separate Binance personal-research view or the synthetic demo. Existing positions may only be exited at a fresh quote.")


class Lab:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock, self.compute_lock, self.job_lock = threading.RLock(), threading.Lock(), threading.Lock()
        self.stop = threading.Event()
        self.feed = {s: {"state": "waiting", "error": None, "checked_at": None, "quote": None} for s in market.SYMBOLS}
        self.job = {"running": False, "symbol": None}
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS prices_v3 (
                  namespace TEXT, symbol TEXT, ts INTEGER, price REAL NOT NULL, fetched_at REAL NOT NULL,
                  PRIMARY KEY(namespace,symbol,ts));
                CREATE TABLE IF NOT EXISTS experiments_v3 (
                  id TEXT PRIMARY KEY, namespace TEXT NOT NULL, symbol TEXT NOT NULL,
                  created_at REAL NOT NULL, result_json TEXT NOT NULL,
                  observations_json TEXT, settings_json TEXT);
                CREATE TABLE IF NOT EXISTS paper_v3 (
                  namespace TEXT, symbol TEXT, cash REAL NOT NULL, qty REAL NOT NULL DEFAULT 0,
                  basis REAL NOT NULL DEFAULT 0, mark REAL, high_water REAL NOT NULL,
                  status TEXT NOT NULL, reason TEXT, last_signal INTEGER, exit_due REAL,
                  exit_requested INTEGER NOT NULL DEFAULT 0, exit_decided_at REAL, model_json TEXT, experiment_id TEXT,
                  updated_at REAL NOT NULL, PRIMARY KEY(namespace,symbol));
                CREATE TABLE IF NOT EXISTS decisions_v3 (
                  namespace TEXT, symbol TEXT, signal_ts INTEGER, created_at REAL NOT NULL,
                  due_at REAL NOT NULL, expires_at REAL NOT NULL, probability REAL,
                  threshold REAL, status TEXT NOT NULL, reason TEXT NOT NULL,
                  PRIMARY KEY(namespace,symbol,signal_ts));
                CREATE TABLE IF NOT EXISTS fills_v3 (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT, symbol TEXT,
                  signal_ts INTEGER NOT NULL, fill_at REAL NOT NULL, market_at REAL NOT NULL,
                  side TEXT NOT NULL, qty REAL NOT NULL, raw_price REAL NOT NULL, price REAL NOT NULL,
                  fee REAL NOT NULL, slippage REAL NOT NULL, pnl REAL NOT NULL, reason TEXT NOT NULL,
                  UNIQUE(namespace,symbol,signal_ts,side));
            """)
            if "exit_decided_at" not in {row[1] for row in db.execute("PRAGMA table_info(paper_v3)")}:
                db.execute("ALTER TABLE paper_v3 ADD COLUMN exit_decided_at REAL")
            experiment_columns = {row[1] for row in db.execute("PRAGMA table_info(experiments_v3)")}
            for column in ("observations_json", "settings_json"):
                if column not in experiment_columns:
                    db.execute(f"ALTER TABLE experiments_v3 ADD COLUMN {column} TEXT")
            # A restart never silently resumes an account or fills a historical
            # pending order. An existing position requests a fresh-price exit.
            db.execute("UPDATE paper_v3 SET status='paused', reason='Server restarted. Review feed and resume explicitly.', exit_requested=(qty>0), exit_decided_at=? WHERE namespace=? AND status='running'", (time.time(), LIVE))
            db.execute("UPDATE decisions_v3 SET status='cancelled', reason='Restart cancelled pending entry.' WHERE namespace=? AND status='pending'", (LIVE,))

    @contextmanager
    def db(self):
        with self.lock:
            con = sqlite3.connect(self.path, timeout=10)
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

    @staticmethod
    def namespace(mode: str, symbol: str) -> str:
        if mode not in {"live", "demo"} or symbol not in market.SYMBOLS:
            raise ValueError("Choose live or demo and BTC-USD or ETH-USD.")
        return LIVE if mode == "live" else DEMO

    def prices(self, mode: str, symbol: str) -> list[dict]:
        ns = self.namespace(mode, symbol)
        with self.db() as db:
            rows = db.execute("SELECT ts,price,fetched_at FROM prices_v3 WHERE namespace=? AND symbol=? ORDER BY ts", (ns, symbol)).fetchall()
        return [dict(row) for row in rows]

    def put_prices(self, mode: str, symbol: str, points: list[dict]) -> None:
        ns, now = self.namespace(mode, symbol), time.time()
        with self.db() as db:
            db.execute("DELETE FROM prices_v3 WHERE namespace=? AND symbol=?", (ns, symbol))
            db.executemany("INSERT INTO prices_v3 VALUES(?,?,?,?,?)", [(ns, symbol, p["ts"], p["price"], now) for p in points])

    def demo_prices(self, symbol: str) -> list[dict]:
        points = self.prices("demo", symbol)
        if not points:
            self.put_prices("demo", symbol, market.synthetic_prices(symbol))
            points = self.prices("demo", symbol)
        return points

    def experiment(self, mode: str, symbol: str, points: list[dict] | None = None) -> dict:
        points = points if points is not None else (self.demo_prices(symbol) if mode == "demo" else self.prices(mode, symbol))
        data = [{"ts": p["ts"], "price": p["price"]} for p in points]
        key = r.experiment_key(data)
        if mode == "live":
            # The public CoinGecko data path is retired for ML. Keep an exact
            # historical cache view available, but never fit or persist a new
            # result from these observations.
            with self.db() as db:
                row = db.execute("SELECT result_json FROM experiments_v3 WHERE id=? AND namespace=?", (key, LIVE)).fetchone()
            return json.loads(row[0]) if row else {"ready": False, "message": LEGACY_PUBLIC_NOTICE}
        with self.compute_lock:
            with self.db() as db:
                row = db.execute("SELECT result_json FROM experiments_v3 WHERE id=?", (key,)).fetchone()
            if row:
                return json.loads(row[0])
            result = r.analyze(data)
            if result["ready"]:
                with self.db() as db:
                    db.execute("INSERT OR IGNORE INTO experiments_v3(id,namespace,symbol,created_at,result_json,observations_json,settings_json) VALUES(?,?,?,?,?,?,?)",
                               (key, self.namespace(mode, symbol), symbol, time.time(), json.dumps(result, allow_nan=False), json.dumps(data, allow_nan=False), json.dumps(r.settings(), allow_nan=False)))
            return result

    def archived_experiment(self, experiment_id: str) -> dict | None:
        """Local audit helper; raw snapshots are not sent in the dashboard API."""
        with self.db() as db:
            row = db.execute("SELECT * FROM experiments_v3 WHERE id=?", (experiment_id,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "namespace": row["namespace"], "symbol": row["symbol"], "created_at": row["created_at"],
                "observations": json.loads(row["observations_json"]) if row["observations_json"] else None,
                "settings": json.loads(row["settings_json"]) if row["settings_json"] else None,
                "result": json.loads(row["result_json"])}

    def ensure_account(self, mode: str, symbol: str, latest: int | None = None) -> dict:
        ns = self.namespace(mode, symbol)
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO paper_v3(namespace,symbol,cash,high_water,status,reason,last_signal,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (ns, symbol, r.INITIAL, r.INITIAL, "paused", "Ready. Start explicitly; no past orders are executed.", latest, time.time()))
            return dict(db.execute("SELECT * FROM paper_v3 WHERE namespace=? AND symbol=?", (ns, symbol)).fetchone())

    def account(self, mode: str, symbol: str) -> dict:
        return self.ensure_account(mode, symbol)

    def pause(self, symbol: str, reason: str, *, halted: bool = False) -> None:
        with self.db() as db:
            now = time.time()
            db.execute("UPDATE paper_v3 SET status=CASE WHEN status='halted' THEN status ELSE ? END,reason=CASE WHEN status='halted' THEN reason ELSE ? END,exit_requested=(qty>0),exit_decided_at=CASE WHEN exit_requested=1 THEN exit_decided_at ELSE ? END,updated_at=? WHERE namespace=? AND symbol=?",
                       ("halted" if halted else "paused", reason, now, now, LIVE, symbol))
            db.execute("UPDATE decisions_v3 SET status='cancelled',reason=? WHERE namespace=? AND symbol=? AND status='pending'", (reason, LIVE, symbol))

    def process_signal(self, symbol: str, points: list[dict], *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        features = r.feature_rows(points)
        account = self.ensure_account("live", symbol, points[-1]["ts"] if points else None)
        if account["status"] != "running":
            return
        if points and points[-1]["ts"] > (account["last_signal"] or 0) and (not features or features[-1]["ts"] != points[-1]["ts"]):
            self.pause(symbol, "Missing hourly interval invalidated the latest feature window. No catch-up fills.")
            return
        if not features:
            return
        latest = features[-1]
        if latest["ts"] <= (account["last_signal"] or 0):
            return
        interval = latest["ts"] - account["last_signal"] if account["last_signal"] else r.HOUR
        if interval > 1.5 * r.HOUR or now >= latest["ts"] + r.HOUR:
            self.pause(symbol, "Missed or delayed hourly signal. No catch-up fills; refresh and resume.")
            return
        pinned = json.loads(account["model_json"])
        prob, threshold = r.probability(pinned["model"], latest), pinned["threshold"]
        selected = prob >= threshold
        # An hourly delay aligns live orders with the next-observation historical
        # experiment. Fills still require a newly requested price after due_at.
        with self.db() as db:
            current = db.execute("SELECT status,last_signal,model_json FROM paper_v3 WHERE namespace=? AND symbol=?", (LIVE, symbol)).fetchone()
            if current is None or current["status"] != "running" or latest["ts"] <= (current["last_signal"] or 0) or current["model_json"] != account["model_json"]:
                return
            db.execute("INSERT OR IGNORE INTO decisions_v3 VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (LIVE, symbol, latest["ts"], now, latest["ts"] + r.HOUR,
                        latest["ts"] + r.HOUR + MAX_DELAY, prob, threshold,
                        "pending" if selected else "skipped",
                        f"p={prob:.3f} {'≥' if selected else '<'} validation threshold {threshold:.2f}; one-sample entry delay"))
            db.execute("UPDATE paper_v3 SET last_signal=?,updated_at=? WHERE namespace=? AND symbol=?", (latest["ts"], now, LIVE, symbol))

    def apply_quote(self, symbol: str, quote: dict, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        q = market.validate_quote(quote, now)
        with self.db() as db:
            a = db.execute("SELECT * FROM paper_v3 WHERE namespace=? AND symbol=?", (LIVE, symbol)).fetchone()
            if a is None:
                return
            a = dict(a)
            equity = a["cash"] + a["qty"] * q["price"]
            high = max(a["high_water"], equity)
            dd = (high - equity) / high
            if dd >= r.HALT:
                if not a["exit_requested"]:
                    a["exit_decided_at"] = now
                a["status"], a["reason"], a["exit_requested"] = "halted", "10% drawdown halt. Reset after review to start a new account.", 1
                db.execute("UPDATE decisions_v3 SET status='cancelled',reason='Drawdown halt' WHERE namespace=? AND symbol=? AND status='pending'", (LIVE, symbol))
            # Never sell at an old historical price. A pause or risk halt requests
            # liquidation at the next valid, newly requested public observation.
            if a["qty"] and (a["exit_requested"] or (a["exit_due"] is not None and q["market_at"] >= a["exit_due"])):
                after = a["exit_decided_at"] if a["exit_requested"] else a["exit_due"]
                if after is not None and q["market_at"] > after and q.get("requested_at", q["observed_at"]) > after and q["observed_at"] > after:
                    exit_ = r.sell(a["cash"], a["qty"], q["price"], a["basis"])
                    signal_ts = int(a["exit_due"] - 2 * r.HOUR)
                    db.execute("INSERT INTO fills_v3(namespace,symbol,signal_ts,fill_at,market_at,side,qty,raw_price,price,fee,slippage,pnl,reason) VALUES(?,?,?,?,?,'SELL',?,?,?,?,?,?,?)",
                               (LIVE, symbol, signal_ts, q["observed_at"], q["market_at"], a["qty"], q["price"], exit_["price"], exit_["fee"], exit_["slippage"], exit_["pnl"], "Fresh observed-price exit; " + (a["reason"] or "one-sample hold elapsed")))
                    a["cash"], a["qty"], a["basis"], a["exit_due"], a["exit_requested"], a["exit_decided_at"] = exit_["cash"], 0.0, 0.0, None, 0, None
            expired = db.execute("SELECT signal_ts FROM decisions_v3 WHERE namespace=? AND symbol=? AND status='pending' AND expires_at<?", (LIVE, symbol, now)).fetchall()
            if expired:
                db.execute("UPDATE decisions_v3 SET status='expired',reason='Entry observation arrived too late; no historical catch-up.' WHERE namespace=? AND symbol=? AND status='pending' AND expires_at<?", (LIVE, symbol, now))
                a["status"], a["reason"], a["exit_requested"] = "paused", "Missed entry window; skipped and paused safely.", int(a["qty"] > 0)
                a["exit_decided_at"] = now if a["qty"] else None
                db.execute("UPDATE decisions_v3 SET status='cancelled' WHERE namespace=? AND symbol=? AND status='pending'", (LIVE, symbol))
            pending = db.execute("SELECT * FROM decisions_v3 WHERE namespace=? AND symbol=? AND status='pending' AND due_at<=? ORDER BY signal_ts LIMIT 1", (LIVE, symbol, now)).fetchone()
            if pending and a["status"] == "running" and a["qty"] == 0:
                after = max(pending["created_at"], pending["due_at"])
                if q["market_at"] > after and q.get("requested_at", q["observed_at"]) > after and q["observed_at"] > after:
                    entry = r.buy(a["cash"], q["price"])
                    db.execute("INSERT INTO fills_v3(namespace,symbol,signal_ts,fill_at,market_at,side,qty,raw_price,price,fee,slippage,pnl,reason) VALUES(?,?,?,?,?,'BUY',?,?,?,?,?,0,?)",
                               (LIVE, symbol, pending["signal_ts"], q["observed_at"], q["market_at"], entry["qty"], q["price"], entry["price"], entry["fee"], entry["slippage"], pending["reason"] + "; newly requested post-due public aggregate price"))
                    a["cash"], a["qty"], a["basis"], a["exit_due"] = entry["cash"], entry["qty"], entry["cost_basis"], pending["signal_ts"] + 2 * r.HOUR
                    db.execute("UPDATE decisions_v3 SET status='filled' WHERE namespace=? AND symbol=? AND signal_ts=?", (LIVE, symbol, pending["signal_ts"]))
            equity = a["cash"] + a["qty"] * q["price"]
            high = max(high, equity)
            if (high - equity) / high >= r.HALT:
                if not a["exit_requested"]:
                    a["exit_decided_at"] = now
                a["status"], a["reason"], a["exit_requested"] = "halted", "10% drawdown halt; reset after review.", int(a["qty"] > 0)
                db.execute("UPDATE decisions_v3 SET status='cancelled' WHERE namespace=? AND symbol=? AND status='pending'", (LIVE, symbol))
            db.execute("UPDATE paper_v3 SET cash=?,qty=?,basis=?,mark=?,high_water=?,status=?,reason=?,exit_due=?,exit_requested=?,exit_decided_at=?,updated_at=? WHERE namespace=? AND symbol=?",
                       (a["cash"], a["qty"], a["basis"], q["price"], high, a["status"], a["reason"], a["exit_due"], a["exit_requested"], a["exit_decided_at"], now, LIVE, symbol))

    def _legacy_account(self, symbol: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT * FROM paper_v3 WHERE namespace=? AND symbol=?", (LIVE, symbol)).fetchone()
        return dict(row) if row else None

    def attempt_legacy_exit(self, symbol: str) -> bool:
        """Sell an explicitly requested legacy position using only a fresh quote."""
        self.namespace("live", symbol)
        account = self._legacy_account(symbol)
        if not account or not account["qty"] or not account["exit_requested"]:
            return False
        try:
            quote = market.fetch_quote(symbol)
            self.apply_quote(symbol, quote)
            self.feed[symbol] = {"state": "retired", "error": LEGACY_PUBLIC_NOTICE,
                "checked_at": time.time(), "quote": quote, "source": market.SOURCE}
            return True
        except Exception as exc:
            self.feed[symbol] = {"state": "error", "error": f"Fresh quote exit is pending: {str(exc)[:240]}",
                "checked_at": time.time(), "quote": None, "source": market.SOURCE}
            return False

    def refresh(self, symbol: str, *, force: bool = False) -> None:
        """Legacy refresh is quote-only and only services an explicit exit."""
        self.namespace("live", symbol)
        self.feed[symbol] = {**self.feed[symbol], "state": "retired", "error": LEGACY_PUBLIC_NOTICE}
        self.attempt_legacy_exit(symbol)

    def launch_refresh(self, symbol: str) -> bool:
        self.namespace("live", symbol)
        raise ValueError("Public CoinGecko history refresh and legacy ML retraining are disabled under the current API terms. Cached results remain read-only.")

    def poll(self):
        while not self.stop.is_set():
            if self.job_lock.acquire(blocking=False):
                try:
                    # Opt-in background polling is restricted to explicitly
                    # requested exits; it never fetches history or creates signals.
                    with self.db() as db:
                        symbols = [row[0] for row in db.execute(
                            "SELECT symbol FROM paper_v3 WHERE namespace=? AND qty>0 AND exit_requested=1", (LIVE,))]
                    for symbol in symbols:
                        self.attempt_legacy_exit(symbol)
                finally:
                    self.job_lock.release()
            self.stop.wait(60)

    def control(self, mode: str, symbol: str, action: str) -> str:
        ns = self.namespace(mode, symbol)
        if action == "refresh":
            if mode != "live":
                raise ValueError("Seeded history is fixed. Use Run replay.")
            raise ValueError("Public CoinGecko history refresh and legacy ML retraining are disabled under the current API terms. Review the cached result or use the synthetic demo.")
        if mode == "live" and action in {"start", "resume", "reset"}:
            raise ValueError("Legacy public accounts and cached CoinGecko ML results are read-only. Use the synthetic demo or the separate V5 prospective price-rule cohort.")
        if mode == "live" and action == "pause":
            account = self._legacy_account(symbol)
            if not account:
                return "No legacy account exists. Public ML accounts are read-only and disabled."
            self.pause(symbol, "Paused by you. Existing position requests a fresh-quote exit.")
            if account["qty"]:
                self.attempt_legacy_exit(symbol)
                current = self._legacy_account(symbol) or account
                if current["qty"]:
                    return "Entries paused. Exit remains pending until a fresh public quote arrives."
                return "Entries paused and the position exited using a fresh public quote."
            return "Legacy account is paused. No position required an exit."
        points = self.demo_prices(symbol) if mode == "demo" else self.prices(mode, symbol)
        a = self.ensure_account(mode, symbol, points[-1]["ts"] if points else None)
        if action == "reset":
            with self.db() as db:
                db.execute("DELETE FROM fills_v3 WHERE namespace=? AND symbol=?", (ns, symbol))
                db.execute("DELETE FROM decisions_v3 WHERE namespace=? AND symbol=?", (ns, symbol))
                db.execute("DELETE FROM paper_v3 WHERE namespace=? AND symbol=?", (ns, symbol))
                db.execute("INSERT INTO paper_v3(namespace,symbol,cash,high_water,status,reason,last_signal,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                           (ns, symbol, r.INITIAL, r.INITIAL, "paused", "Reset complete. Start explicitly; historical signals will be skipped.", points[-1]["ts"] if points else None, time.time()))
            return "Selected simulated account reset to $10,000 and paused."
        if mode == "demo":
            if action != "replay":
                raise ValueError("Demo supports Run replay and Reset; it does not run on a clock.")
            result = self.experiment(mode, symbol, points)
            if not result["ready"]:
                raise ValueError(result["message"])
            if a["status"] == "complete":
                return "Replay already complete. Reset to repeat; no duplicate fills were created."
            with self.db() as db:
                for fill in result["replay_ledger"]:
                    raw = fill["price"] / (1 + r.SLIPPAGE if fill["side"] == "BUY" else 1 - r.SLIPPAGE)
                    db.execute("INSERT OR IGNORE INTO fills_v3(namespace,symbol,signal_ts,fill_at,market_at,side,qty,raw_price,price,fee,slippage,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (ns, symbol, fill["signal_ts"], fill["fill_at"], fill["fill_at"], fill["side"], fill["qty"], raw, fill["price"], fill["fee"], fill["slippage"], fill.get("pnl", 0), "SYNTHETIC REPLAY · " + fill["reason"]))
                db.execute("UPDATE paper_v3 SET cash=?,high_water=?,status='complete',reason='Seeded synthetic held-out replay complete.',experiment_id=?,updated_at=? WHERE namespace=? AND symbol=?",
                           (result["replay_cash"], result["replay_high_water"], result["id"], time.time(), ns, symbol))
            return "Synthetic held-out replay completed. Ledger contains simulated fills only."
        if action == "pause":
            self.pause(symbol, "Paused by you. Any open position exits at the next fresh public observation.")
            return "New entries paused. Open positions request an observed-price exit."
        if action not in {"start", "resume"}:
            raise ValueError("Unsupported action.")
        if a["status"] == "running":
            return "Simulated account is already running."
        feed = self.feed[symbol]
        if feed["state"] != "ok" or time.time() - (feed["checked_at"] or 0) > 120:
            raise ValueError("Refresh the public feed successfully before starting simulated execution.")
        try:
            market.validate_quote(feed.get("quote"), time.time())
        except ValueError as exc:
            raise ValueError("Cached public price is stale or unavailable. Refresh before starting simulated execution.") from exc
        if not points or time.time() - points[-1]["ts"] > 75 * 60:
            raise ValueError("Historical feed is stale. Refresh before starting.")
        equity = a["cash"] + a["qty"] * (a["mark"] or 0)
        if a["status"] == "halted" or (a["high_water"] - equity) / a["high_water"] >= r.HALT:
            raise ValueError("Drawdown halt requires review and Reset; resume is blocked.")
        if a["qty"]:
            raise ValueError("An open position is waiting for its pause exit. Refresh before resuming.")
        result = self.experiment(mode, symbol, points)
        if not result["ready"]:
            raise ValueError(result["message"])
        # Resume preserves the exact previously pinned model, threshold and ID.
        with self.db() as db:
            current = db.execute("SELECT * FROM paper_v3 WHERE namespace=? AND symbol=?", (ns, symbol)).fetchone()
            if current is None or current["status"] == "halted" or current["qty"]:
                raise ValueError("Account changed while starting. Review its current state and retry.")
            pinned = current["model_json"] or json.dumps({"model": result["model"], "threshold": result["threshold"]})
            db.execute("UPDATE paper_v3 SET status='running',reason='Waiting for the next new hourly signal, then one-sample entry delay.',last_signal=?,model_json=?,experiment_id=?,updated_at=? WHERE namespace=? AND symbol=?",
                       (points[-1]["ts"], pinned, current["experiment_id"] or result["id"], time.time(), ns, symbol))
        return "Simulated execution started. Historical signals are skipped; first entry follows the next new signal by one hour."

    def ledger(self, mode: str, symbol: str, limit: int = 100) -> list[dict]:
        ns = self.namespace(mode, symbol)
        with self.db() as db:
            rows = db.execute("SELECT * FROM fills_v3 WHERE namespace=? AND symbol=? ORDER BY fill_at DESC,id DESC LIMIT ?", (ns, symbol, limit)).fetchall()
        return [dict(row) for row in rows]

    def state(self, mode: str, symbol: str) -> dict:
        if mode == "live":
            return self._legacy_public_state(symbol)
        ns = self.namespace(mode, symbol)
        points = self.demo_prices(symbol) if mode == "demo" else self.prices(mode, symbol)
        result = self.experiment(mode, symbol, points)
        a = self.ensure_account(mode, symbol, points[-1]["ts"] if points else None)
        with self.db() as db:
            costs = dict(db.execute("SELECT COALESCE(SUM(fee),0) fees, COALESCE(SUM(slippage),0) slippage,COUNT(*) fills FROM fills_v3 WHERE namespace=? AND symbol=?", (ns, symbol)).fetchone())
            decisions = [dict(row) for row in db.execute("SELECT signal_ts,created_at,due_at,probability,threshold,status,reason FROM decisions_v3 WHERE namespace=? AND symbol=? ORDER BY signal_ts DESC LIMIT 8", (ns, symbol))]
        quote = self.feed[symbol].get("quote") if mode == "live" else None
        mark = a["mark"] or (points[-1]["price"] if points else 0)
        equity = a["cash"] + a["qty"] * mark
        feed = ({"state": "synthetic", "source": "Seeded synthetic prices · seeds 3001 / 3002", "error": None, "checked_at": None, "quote": None}
                if mode == "demo" else {**self.feed[symbol], "source": market.SOURCE})
        quote_valid = mode == "demo"
        if mode == "live":
            try:
                market.validate_quote(quote, time.time())
                quote_valid = True
            except ValueError:
                if feed["state"] == "ok":
                    feed.update({"state": "stale", "error": "Cached public price is stale or unavailable. Refresh to obtain a current provider timestamp."})
        feed.update({"latest_sample": points[-1]["ts"] if points else None,
                     "sample_age": max(0, time.time() - points[-1]["ts"]) if points else None,
                     "fresh": mode == "demo" or bool(points and time.time() - points[-1]["ts"] <= 75 * 60 and feed["state"] == "ok" and time.time() - (feed["checked_at"] or 0) <= 120 and quote_valid)})
        last_features = r.feature_rows(points)
        pinned = json.loads(a["model_json"]) if a["model_json"] else None
        model = pinned["model"] if pinned else result.get("model")
        latest_probability = r.probability(model, last_features[-1]) if model and last_features else None
        public_result = {k: v for k, v in result.items() if k not in {"replay_ledger", "replay_cash", "replay_high_water", "model"}}
        if result.get("ready"):
            public_result["coefficients"] = list(zip(result["features"], result["model"]["weights"][1:]))
        return {"mode": mode, "symbol": symbol, "simulated": True, "feed": feed,
                "job": self.job, "market": {"price": quote["price"] if quote else (points[-1]["price"] if points else None), "samples": len(points)},
                "research": public_result, "latest_probability": latest_probability,
                "latest_threshold": pinned["threshold"] if pinned else result.get("threshold"),
                "probability_model_id": a["experiment_id"] if pinned else result.get("id"),
                "account": {k: a[k] for k in ("cash", "qty", "status", "reason", "exit_due", "exit_requested", "experiment_id")}
                           | {"equity": equity, "return": equity / r.INITIAL - 1,
                              "drawdown": (a["high_water"] - equity) / a["high_water"],
                              "exposure": a["qty"] * mark / equity if equity else 0, **costs},
                "ledger": self.ledger(mode, symbol), "decisions": decisions,
                "method": {"fee_bps": r.FEE * 10000, "slippage_bps": r.SLIPPAGE * 10000,
                           "allocation": r.ALLOCATION, "drawdown_halt": r.HALT, "initial": r.INITIAL, "poll_seconds": 60}}

    def _legacy_public_state(self, symbol: str) -> dict:
        """Read-only projection of saved public data; never predicts or trains."""
        self.namespace("live", symbol)
        points = self.prices("live", symbol)
        account = self._legacy_account(symbol)
        if account is None:
            account = {"cash": r.INITIAL, "qty": 0.0, "status": "paused",
                "reason": "Legacy public ML entries are disabled. Use the synthetic demo or V5 forward price rules.",
                "exit_due": None, "exit_requested": 0, "experiment_id": None, "mark": None,
                "high_water": r.INITIAL, "fees": 0.0, "slippage": 0.0}
        selected_result = None
        with self.db() as db:
            if account.get("experiment_id"):
                row = db.execute("SELECT result_json FROM experiments_v3 WHERE id=? AND namespace=?",
                    (account["experiment_id"], LIVE)).fetchone()
                if row:
                    selected_result = json.loads(row[0])
            if selected_result is None and points:
                data = [{"ts": p["ts"], "price": p["price"]} for p in points]
                row = db.execute("SELECT result_json FROM experiments_v3 WHERE id=? AND namespace=?",
                    (r.experiment_key(data), LIVE)).fetchone()
                if row:
                    selected_result = json.loads(row[0])
            costs = dict(db.execute("SELECT COALESCE(SUM(fee),0) fees, COALESCE(SUM(slippage),0) slippage,COUNT(*) fills FROM fills_v3 WHERE namespace=? AND symbol=?", (LIVE, symbol)).fetchone())
            decisions = [dict(row) for row in db.execute("SELECT signal_ts,created_at,due_at,probability,threshold,status,reason FROM decisions_v3 WHERE namespace=? AND symbol=? ORDER BY signal_ts DESC LIMIT 8", (LIVE, symbol))]
        mark = account.get("mark") or (points[-1]["price"] if points else 0.0)
        equity = account["cash"] + account["qty"] * mark
        high_water = max(float(account.get("high_water") or r.INITIAL), equity)
        result = selected_result or {"ready": False, "message": LEGACY_PUBLIC_NOTICE}
        public_result = {k: v for k, v in result.items() if k not in {"replay_ledger", "replay_cash", "replay_high_water", "model"}}
        if result.get("ready"):
            public_result["message"] = "Saved historical snapshot only. New CoinGecko ML training and public refresh are disabled under current API terms."
            public_result["coefficients"] = list(zip(result.get("features", []), result.get("model", {}).get("weights", [])[1:]))
        feed = {"state": "retired", "source": market.SOURCE, "error": LEGACY_PUBLIC_NOTICE,
            "checked_at": None, "quote": None, "latest_sample": points[-1]["ts"] if points else None,
            "sample_age": max(0, time.time() - points[-1]["ts"]) if points else None,
            "fresh": False}
        return {"mode": "live", "symbol": symbol, "simulated": True, "feed": feed,
            "job": {"running": False, "symbol": None},
            "market": {"price": mark or None, "samples": len(points)}, "research": public_result,
            "latest_probability": None, "latest_threshold": result.get("threshold"),
            "probability_model_id": account.get("experiment_id"),
            "account": {k: account[k] for k in ("cash", "qty", "status", "reason", "exit_due", "exit_requested", "experiment_id")}
                | {"equity": equity, "return": equity / r.INITIAL - 1,
                   "drawdown": (high_water - equity) / high_water if high_water else 0.0,
                   "exposure": account["qty"] * mark / equity if equity else 0.0, **costs},
            "ledger": self.ledger("live", symbol), "decisions": decisions,
            "method": {"fee_bps": r.FEE * 10000, "slippage_bps": r.SLIPPAGE * 10000,
                "allocation": r.ALLOCATION, "drawdown_halt": r.HALT, "initial": r.INITIAL,
                "poll_seconds": None, "legacy_public_ml_enabled": False}}

    def export(self, mode: str, symbol: str) -> str:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["data_mode", "source", "symbol", "signal_utc", "observed_fill_utc", "provider_price_utc", "side", "quantity", "raw_usd", "simulated_fill_usd", "fee_usd", "slippage_usd", "realized_pnl_usd", "reason"])
        for fill in reversed(self.ledger(mode, symbol, -1)):
            writer.writerow(["SYNTHETIC_DEMO" if mode == "demo" else "PUBLIC_DATA_SIMULATED", "Seeded synthetic" if mode == "demo" else market.SOURCE, symbol,
                             market.iso(fill["signal_ts"]), market.iso(fill["fill_at"]), market.iso(fill["market_at"]), fill["side"],
                             *[f"{fill[k]:.10f}" for k in ("qty", "raw_price", "price", "fee", "slippage", "pnl")], fill["reason"]])
        return output.getvalue()
