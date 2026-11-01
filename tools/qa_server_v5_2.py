"""Offline-only local QA server with explicitly synthetic prices and fixture rows.

This is not part of the normal launcher. It binds loopback only, refuses an
existing or in-project SQLite path, and never calls the public market adapter.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

# Running `python3 tools/qa_server_v5_2.py` sets sys.path[0] to tools/.
# Add the project root explicitly so the documented project-root command
# imports the same app modules as `python3 -m tools.qa_server_v5_2`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine import Lab
from forward_v5 import (FEE_BPS, SLIP_BPS, PROTECTION_SCHEMA, ForwardCohort,
    SYMBOLS, STRATEGIES, net_position_return_pct)
from research_jobs import ResearchJobs
from research_v5_view import HistoricalV5Summary
from server import ROOT, handler_for

DAY = 86400
FIXTURE_LABEL = "SYNTHETIC QA FIXTURE — seeded values only; no public market feed or evidence."


class SyntheticFeed:
    def __init__(self):
        self.history_calls = 0
        self.quote_calls = 0

    def history(self, symbol: str):
        self.history_calls += 1
        today = int(time.time() // DAY) * DAY
        base = 100.0 if symbol == "BTC-USD" else 10.0
        rows = [{"ts": today - (365 - i) * DAY,
                 "price": base + i * (0.025 if symbol == "BTC-USD" else 0.0025)}
                for i in range(366)]
        return rows, {"source": "synthetic QA fixture", "warmup_only": True,
            "missing_intervals": 0, "actual_provider": False}

    def quotes(self):
        self.quote_calls += 1
        request = time.time()
        receipt = request + 0.01
        return {symbol: {"price": 100.0 if symbol == "BTC-USD" else 10.0,
            "market_at": request - 0.1, "requested_at": request,
            "observed_at": receipt, "source": "synthetic QA fixture"}
            for symbol in SYMBOLS}


def seed_protection_fixture(cohort: ForwardCohort) -> None:
    """Seed one pending protective close plus one completed negative episode."""
    # Use the service's authoritative clock, which also keeps injected test
    # clocks coherent and avoids making the fixture appear clock-suspect.
    now = float(cohort.clock())
    start, deadline = now - 1800, now + 3600
    cid = cohort.state()["cohort_id"]
    episode_open = "qa-synthetic-pending-" + uuid.uuid4().hex
    episode_closed = "qa-synthetic-closed-" + uuid.uuid4().hex
    protection = {"take_profit_pct": 2.5, "stop_loss_pct": 1.5,
        "schema": "v5.2-net-liquidation-remaining-basis",
        "fee_bps_per_side": FEE_BPS, "slippage_bps_per_side": SLIP_BPS}
    session_config = {"session_id": "qa-synthetic-" + uuid.uuid4().hex,
        "started_at": start, "deadline_at": deadline, "duration_minutes": 60,
        "expiry_policy": "CLOSE", "position_protection": protection,
        "qa_fixture_label": FIXTURE_LABEL}
    config = cohort._config()
    config_hash = hashlib.sha256(json.dumps(session_config, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    quote_request, quote_receipt = now - 30.5, now - 30.4
    latest_quotes = {"BTC-USD": {"price": 104.0, "market_at": now - 31,
        "requested_at": quote_request, "observed_at": quote_receipt,
        "source": "synthetic QA fixture"},
        "ETH-USD": {"price": 10.0, "market_at": now - 31,
        "requested_at": quote_request, "observed_at": quote_receipt,
        "source": "synthetic QA fixture"}}
    pending_basis = 4000 * (1 + SLIP_BPS / 10_000) * (1 + FEE_BPS / 10_000)
    pending_return = net_position_return_pct(40, pending_basis, 104)
    closed_basis = 5000 * (1 + SLIP_BPS / 10_000) * (1 + FEE_BPS / 10_000)
    sell_fee = 4950 * FEE_BPS / 10_000
    sell_slippage = 50 * 99 * SLIP_BPS / 10_000
    closed_proceeds = 50 * 99 - sell_fee - sell_slippage
    closed_pnl = closed_proceeds - closed_basis
    with cohort.db() as db:
        db.execute("UPDATE cohorts SET state='paused',start_at=?,session_id=?,session_started_at=?,session_deadline_at=?,session_duration_minutes=60,expiry_policy='CLOSE',session_status='paused',entry_disabled=0,clock_high_water=?,last_quote_request=?,last_quote_receipt=?,latest_quote_json=?,session_config_json=?,protection_schema_version=?,config_json=?,config_hash=?,reason=? WHERE id=?",
            (start, session_config["session_id"], start, deadline, now,
             quote_request, quote_receipt, json.dumps(latest_quotes, sort_keys=True),
             json.dumps(session_config, sort_keys=True), PROTECTION_SCHEMA,
             json.dumps(config, sort_keys=True), config_hash,
             "[SYNTHETIC QA FIXTURE] Open BTC/passive episode is latched for TP; values are fabricated.", cid))
        for symbol in SYMBOLS:
            for strategy in STRATEGIES:
                px = 104.0 if symbol == "BTC-USD" else 10.0
                db.execute("INSERT INTO accounts(cohort_id,symbol,strategy,initial,cash,qty,basis,peak_nlv,last_price,last_market_at,last_receipt_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, symbol, strategy, 10_000.0, 10_000.0, 0.0, 0.0,
                     10_000.0, px, now-31, quote_receipt, now))
        db.execute("UPDATE accounts SET cash=?,qty=40,basis=?,peak_nlv=10000,exit_requested=1,exit_decided_at=?,signal_active=1,last_price=104,last_market_at=?,last_receipt_at=?,fees=10.01,slippage=4,episode_id=?,position_opened_at=?,basis_updated_at=?,basis_updated_receipt_at=?,basis_updated_market_at=?,position_tp_pct=2.5,position_sl_pct=1.5,exit_reason='take_profit',exit_priority=20,protection_trigger_reason='take_profit',protection_triggered_at=?,protection_trigger_price=104,protection_trigger_market_at=?,protection_trigger_return_pct=?,protection_trigger_basis=?,protection_trigger_threshold_pct=2.5,updated_at=? WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",
            (10_000.0-pending_basis, pending_basis, now-60, now-31, quote_receipt,
             episode_open, start, start, now-1799, now-1800, now-60, now-120,
             pending_return, pending_basis, now, cid))
        db.execute("UPDATE accounts SET cash=?,qty=0,basis=0,peak_nlv=10000,last_price=99,last_market_at=?,last_receipt_at=?,fees=24.875125,slippage=9.95,realized_pnl=?,updated_at=? WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='sma20_100'",
            (10_000.0 + closed_pnl, now-601, quote_receipt, closed_pnl, now, cid))
        db.execute("INSERT INTO position_episodes(id,cohort_id,symbol,strategy,opened_at,closed_at,opening_market_at,basis_updated_at,basis_updated_receipt_at,basis_updated_market_at,opening_basis,remaining_basis,opened_qty,current_qty,take_profit_pct,stop_loss_pct,realized_pnl,status,close_reason,pending_reason,protection_trigger_reason,close_receipt_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'exit_pending',NULL,'take_profit','take_profit',NULL)",
            (episode_open,cid,"BTC-USD","passive",start, None, start, start,
             now-1799,now-1800,pending_basis,pending_basis,40,40,2.5,1.5,0.0))
        db.execute("UPDATE position_episodes SET protection_triggered_at=?,protection_trigger_price=104,protection_trigger_return_pct=?,protection_trigger_basis=?,protection_trigger_threshold_pct=2.5 WHERE id=?",
            (now-60,pending_return,pending_basis,episode_open))
        db.execute("INSERT INTO position_episodes(id,cohort_id,symbol,strategy,opened_at,closed_at,opening_market_at,basis_updated_at,basis_updated_receipt_at,basis_updated_market_at,opening_basis,remaining_basis,opened_qty,current_qty,take_profit_pct,stop_loss_pct,realized_pnl,status,close_reason,pending_reason,protection_trigger_reason,close_receipt_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'closed','stop_loss',NULL,NULL,?)",
            (episode_closed,cid,"BTC-USD","sma20_100",start-600,start-120,start-600,
             start-600, start-599, start-600, closed_basis, 0.0, 50.0, 0.0,
             None, 1.5, closed_pnl, quote_receipt))
        entry_fee = 5005 * FEE_BPS / 10_000
        entry_slippage = 5000 * SLIP_BPS / 10_000
        db.execute("INSERT INTO fills(cohort_id,symbol,strategy,signal_ts,request_at,receipt_at,market_at,side,qty,raw_price,fill_price,fee,slippage,realized_pnl,reason,episode_id,applied_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid,"BTC-USD","sma20_100",int(start-600),start-600,start-599,start-600,
             "BUY",50.0,100.0,100.1,entry_fee,entry_slippage,0.0,"synthetic QA fixture",episode_closed,start-599))
        db.execute("INSERT INTO fills(cohort_id,symbol,strategy,signal_ts,request_at,receipt_at,market_at,side,qty,raw_price,fill_price,fee,slippage,realized_pnl,reason,episode_id,applied_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid,"BTC-USD","sma20_100",int(start-120),start-120,start-119,start-120,
             "SELL",50.0,99.0,99*(1-SLIP_BPS/10_000),sell_fee,sell_slippage,
             closed_pnl,"synthetic QA fixture",episode_closed,start-119))
        db.execute("INSERT INTO protection_events(cohort_id,episode_id,symbol,strategy,event_type,reason,priority,decided_at,market_at,request_at,receipt_at,price,net_return_pct,remaining_basis,threshold_pct,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid,episode_open,"BTC-USD","passive","TRIGGER","take_profit",20,
             now-60,now-120,now-119,now-118,104.0,pending_return,pending_basis,2.5,
             "Synthetic QA trigger; pending only; no market quote or fill is represented."))
        db.execute("INSERT INTO marks(cohort_id,symbol,strategy,market_at,request_at,receipt_at,price,equity,net_liquidation,drawdown,exposure) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cid,"BTC-USD","passive",now-31,quote_request,quote_receipt,104.0,
             10_146.0,10_131.0,0.0,40*104/10_146))


def main():
    parser = argparse.ArgumentParser(description="Loopback-only offline RKO V5.2 QA using fabricated prices.")
    parser.add_argument("--db", required=True, type=Path, help="A new disposable SQLite path outside this project.")
    parser.add_argument("--port", type=int, default=8768, help="127.0.0.1 port (0 selects an available port).")
    parser.add_argument("--scenario", choices=("blank", "protection"), default="blank",
        help="blank: empty cohort for a synthetic-only short-session test; protection: marked pending close + realized fixture history.")
    args = parser.parse_args()
    db_path = args.db.expanduser().resolve()
    if not db_path.is_absolute() or db_path.exists() or db_path.is_relative_to(ROOT):
        parser.error("--db must be a new file outside the project; existing files are never overwritten.")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be 0–65535.")
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(db_path.parent, 0o700)
    except OSError:
        pass
    lab = Lab(db_path)
    feed = SyntheticFeed()
    forward_path = db_path.parent / "forward_v5" / (db_path.stem + "_cohort.sqlite3")
    service = ForwardCohort(forward_path, fetch_daily=feed.history, fetch_quotes=feed.quotes,
        _start_watchdog=False)
    service.qa_fixture_label = FIXTURE_LABEL
    if args.scenario == "protection":
        seed_protection_fixture(service)
    service.watchdog = threading.Thread(target=service._watchdog_loop,
        name="synthetic-qa-session-watchdog", daemon=True)
    service.watchdog.start()
    jobs = ResearchJobs()
    history = HistoricalV5Summary()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port),
        handler_for(lab,jobs,service,history))
    httpd.daemon_threads = True
    print(f"RKO V5.2 synthetic QA: http://127.0.0.1:{httpd.server_address[1]}/",flush=True)
    print(f"Disposable DB: {db_path}",flush=True)
    print(f"Forward DB: {forward_path}",flush=True)
    print(FIXTURE_LABEL,flush=True)
    print("No market provider is called; Start uses only deterministic in-process synthetic data.",flush=True)
    try:
        httpd.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        print("Stopping synthetic QA server.",flush=True)
    finally:
        jobs.stop()
        service.stop()
        lab.stop.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
