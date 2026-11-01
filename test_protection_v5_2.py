"""Focused synthetic tests for V5.2 optional position-level paper exits."""
from __future__ import annotations

import csv
import datetime as dt
import http.client
import json
import math
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer

import engine
from research_jobs import ResearchJobs
from server import handler_for
from forward_v5 import (EXIT_PRIORITY, FEE_BPS, SLIP_BPS, PROTECTION_SCHEMA, ForwardCohort,
    estimated_protection_price, net_position_return_pct,
    validate_protection_threshold)
from tools.qa_server_v5_2 import SyntheticFeed as OfflineQaFeed, seed_protection_fixture


class QaEntrypointTests(unittest.TestCase):
    def test_documented_project_root_qa_command_imports_without_starting_server(self):
        root = Path(__file__).resolve().parent
        result = subprocess.run([sys.executable, "tools/qa_server_v5_2.py", "--help"],
            cwd=root, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--scenario", result.stdout)


class Clock:
    def __init__(self, value: float):
        self.value = float(value)

    def __call__(self):
        return self.value

    def set(self, value: float):
        self.value = float(value)


class SyntheticProtectionFeed:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.quote_price = {"BTC-USD": 100.0, "ETH-USD": 10.0}
        self.fail_quotes = False
        self.history_calls = 0

    def history(self, symbol):
        self.history_calls += 1
        raise AssertionError("Protection-only tests must not fetch history.")

    def quotes(self):
        if self.fail_quotes:
            raise RuntimeError("synthetic quote outage")
        request = float(self.clock())
        receipt = request + .05
        return {symbol: {"price": self.quote_price[symbol], "market_at": request - .1,
            "requested_at": request, "observed_at": receipt, "source": "synthetic fixture"}
            for symbol in ("BTC-USD", "ETH-USD")}


class ProtectionV52Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = dt.datetime(2026, 1, 2, 5, tzinfo=dt.timezone.utc).timestamp()
        self.clock = Clock(self.base)
        self.feed = SyntheticProtectionFeed(self.clock)
        self.path = Path(self.temp.name) / "forward-v5.2.sqlite3"
        self.service = ForwardCohort(self.path, fetch_daily=self.feed.history,
            fetch_quotes=self.feed.quotes, clock=self.clock, poll_seconds=300,
            _start_watchdog=False)

    def tearDown(self):
        self.service.stop()
        self.temp.cleanup()

    def seed_open_account(self, *, strategy="passive", qty=10.0, basis=1000.0,
                          cash=9000.0, tp=5.0, sl=10.0):
        state = self.service.state()
        cid = state["cohort_id"]
        at = self.base
        episode = "episode-fixture-" + strategy
        config = self.service._config()
        session_config = {"session_id": "fixture-session", "started_at": at-3600,
            "deadline_at": at+86400, "duration_minutes": 1500, "expiry_policy": "CLOSE",
            "position_protection": {"take_profit_pct": tp, "stop_loss_pct": sl,
                "schema": PROTECTION_SCHEMA, "fee_bps_per_side": 25.0,
                "slippage_bps_per_side": 10.0}}
        old_quotes = {"BTC-USD": {"price": 100.0, "market_at": at-21,
                "requested_at": at-20, "observed_at": at-19},
            "ETH-USD": {"price": 10.0, "market_at": at-21,
                "requested_at": at-20, "observed_at": at-19}}
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET state='running',start_at=?,session_id=?,session_started_at=?,session_deadline_at=?,session_duration_minutes=1500,expiry_policy='CLOSE',session_status='active',entry_disabled=0,clock_high_water=?,last_quote_request=?,last_quote_receipt=?,latest_quote_json=?,session_config_json=?,protection_schema_version=?,config_json=? WHERE id=?",
                (at-3600,"fixture-session",at-3600,at+86400,at,at-20,at-19,
                 json.dumps(old_quotes,sort_keys=True),json.dumps(session_config,sort_keys=True),
                 PROTECTION_SCHEMA,json.dumps(config,sort_keys=True),cid))
            db.execute("INSERT INTO accounts(cohort_id,symbol,strategy,initial,cash,qty,basis,peak_nlv,last_price,last_market_at,last_receipt_at,updated_at,episode_id,position_opened_at,basis_updated_at,basis_updated_receipt_at,basis_updated_market_at,position_tp_pct,position_sl_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid,"BTC-USD",strategy,10000.0,cash,qty,basis,10000.0,100.0,at-21,at-19,at,
                 episode,at-110,at-100,at-101,at-102,tp,sl))
            db.execute("INSERT INTO position_episodes(id,cohort_id,symbol,strategy,opened_at,opening_market_at,basis_updated_at,basis_updated_receipt_at,basis_updated_market_at,opening_basis,remaining_basis,opened_qty,current_qty,take_profit_pct,stop_loss_pct,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')",
                (episode,cid,"BTC-USD",strategy,at-110,at-111,at-100,at-101,at-102,basis,basis,qty,qty,tp,sl))
        return cid, episode

    def seed_active_flat_account(self, *, strategy="passive", tp=5.0, sl=10.0):
        """One synthetic active account, with no prior position or fills."""
        state = self.service.state()
        cid = state["cohort_id"]
        at = self.base
        config = self.service._config()
        session_config = {"session_id": "fixture-flat-session", "started_at": at-3600,
            "deadline_at": at+86400, "duration_minutes": 1500, "expiry_policy": "CLOSE",
            "position_protection": {"take_profit_pct": tp, "stop_loss_pct": sl,
                "schema": PROTECTION_SCHEMA, "fee_bps_per_side": 25.0,
                "slippage_bps_per_side": 10.0}}
        old_quotes = {"BTC-USD": {"price": 100.0, "market_at": at-21,
                "requested_at": at-20, "observed_at": at-19},
            "ETH-USD": {"price": 10.0, "market_at": at-21,
                "requested_at": at-20, "observed_at": at-19}}
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET state='running',start_at=?,session_id=?,session_started_at=?,session_deadline_at=?,session_duration_minutes=1500,expiry_policy='CLOSE',session_status='active',entry_disabled=0,clock_high_water=?,last_quote_request=?,last_quote_receipt=?,latest_quote_json=?,session_config_json=?,protection_schema_version=?,config_json=? WHERE id=?",
                (at-3600,"fixture-flat-session",at-3600,at+86400,at,at-20,at-19,
                 json.dumps(old_quotes,sort_keys=True),json.dumps(session_config,sort_keys=True),
                 PROTECTION_SCHEMA,json.dumps(config,sort_keys=True),cid))
            db.execute("INSERT INTO accounts(cohort_id,symbol,strategy,initial,cash,qty,basis,peak_nlv,last_price,last_market_at,last_receipt_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid,"BTC-USD",strategy,10000.0,10000.0,0.0,0.0,10000.0,100.0,at-21,at-19,at))
        return cid

    def batch(self, *, request, receipt=None, market=None, btc=100.0, eth=10.0):
        receipt = request + .1 if receipt is None else receipt
        market = request - .1 if market is None else market
        return {"BTC-USD": {"price": btc, "market_at": market, "requested_at": request,
                "observed_at": receipt, "source": "synthetic fixture"},
            "ETH-USD": {"price": eth, "market_at": market, "requested_at": request,
                "observed_at": receipt, "source": "synthetic fixture"}}

    def process(self, quotes, *, now=None):
        now = max(float(self.clock()), max(q["observed_at"] for q in quotes.values()) + .01) if now is None else now
        self.service._process_quote_batch(self.service.state()["cohort_id"], quotes, now)

    def account(self, strategy="passive"):
        state = self.service.state()
        return next(a for a in state["accounts"] if a["symbol"] == "BTC-USD" and a["strategy"] == strategy)

    def add_pending_target(self, *, strategy="passive", signal_ts=None, target=.5):
        cid = self.service.state()["cohort_id"]
        signal_ts = int(self.base-1000 if signal_ts is None else signal_ts)
        with self.service.db() as db:
            db.execute("INSERT INTO decisions(cohort_id,symbol,strategy,signal_ts,created_at,due_at,expires_at,target_weight,status,reason,source_observation_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (cid,"BTC-USD",strategy,signal_ts,self.base-900,self.base-600,self.base+900,target,"pending","synthetic target",signal_ts))

    def add_target_at(self, *, cohort_id, strategy="passive", target=.5,
                      signal_ts=None, created_at=None, due_at=None, expires_at=None):
        signal_ts = int(self.base+1 if signal_ts is None else signal_ts)
        created_at = self.base-2 if created_at is None else created_at
        due_at = created_at+.1 if due_at is None else due_at
        expires_at = created_at+900 if expires_at is None else expires_at
        with self.service.db() as db:
            db.execute("INSERT INTO decisions(cohort_id,symbol,strategy,signal_ts,created_at,due_at,expires_at,target_weight,status,reason,source_observation_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (cohort_id,"BTC-USD",strategy,signal_ts,created_at,due_at,expires_at,target,"pending","synthetic target",signal_ts))

    def test_optional_threshold_validation_rejects_ambiguous_json_values(self):
        for value, maximum, expected in ((None,100.0,None),(.1,100.0,.1),(100,100.0,100.0),(99.9,99.9,99.9)):
            self.assertEqual(validate_protection_threshold(value,name="threshold",maximum=maximum),expected)
        for value, maximum in ((True,100.0),("1.0",100.0),(0,100.0),(.15,100.0),(100.1,100.0),(math.nan,100.0),(math.inf,100.0),(100,99.9)):
            with self.subTest(value=value,maximum=maximum), self.assertRaises(ValueError):
                validate_protection_threshold(value,name="threshold",maximum=maximum)

    def test_start_requires_explicit_protection_values_and_freezes_them_in_hash(self):
        bad = [True, "1.0", .15, 0, 100.1, math.inf]
        for field, value in (("take_profit_pct",x) for x in bad):
            with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                self.service.control("start",confirm="START",duration_minutes=60,expiry_policy="CLOSE",**{field:value})
        with self.assertRaisesRegex(ValueError,"99.9"):
            self.service.control("start",confirm="START",duration_minutes=60,expiry_policy="CLOSE",stop_loss_pct=100)
        state = self.service.control("start",confirm="START",duration_minutes=60,expiry_policy="CLOSE",
            take_profit_pct=8.5,stop_loss_pct=.5)
        config = state["session_config"]["position_protection"]
        self.assertEqual(config["take_profit_pct"],8.5)
        self.assertEqual(config["stop_loss_pct"],.5)
        self.assertEqual(config["schema"],PROTECTION_SCHEMA)
        with self.service.db() as db:
            row=db.execute("SELECT config_hash,session_config_json FROM cohorts WHERE id=?",(state["cohort_id"],)).fetchone()
        self.assertTrue(row["config_hash"])
        self.assertEqual(json.loads(row["session_config_json"])["position_protection"],config)

    def test_net_remaining_lot_return_includes_both_sides_of_modeled_cost(self):
        unchanged=net_position_return_pct(10,1000,100)
        self.assertAlmostEqual(unchanged,-0.34975,places=4)
        target=estimated_protection_price(10,1000,5,take_profit=True)
        self.assertAlmostEqual(net_position_return_pct(10,1000,target),5.0,places=9)
        stop=estimated_protection_price(10,1000,2,take_profit=False)
        self.assertAlmostEqual(net_position_return_pct(10,1000,stop),-2.0,places=9)

    def test_exact_take_profit_boundary_is_inclusive_but_just_below_is_not(self):
        self.seed_open_account(tp=5,sl=None)
        boundary=estimated_protection_price(10,1000,5,take_profit=True)
        self.assertAlmostEqual(net_position_return_pct(10,1000,boundary),5.0,places=9)
        exact=self.batch(request=self.base+1,btc=boundary)
        self.clock.set(self.base+1.2);self.process(exact)
        self.assertEqual(self.account()["exit_reason"],"take_profit")
        self.assertAlmostEqual(self.account()["protection_trigger_return_pct"],5.0,places=9)

        other=ForwardCohort(Path(self.temp.name)/"tp-below.sqlite3",fetch_daily=self.feed.history,
            fetch_quotes=self.feed.quotes,clock=self.clock,poll_seconds=300,_start_watchdog=False)
        previous=self.service
        self.service=other
        try:
            self.seed_open_account(tp=5,sl=None)
            below=self.batch(request=self.base+2,btc=boundary*(1-1e-6))
            self.clock.set(self.base+2.2);self.process(below)
            self.assertLess(self.account()["protection_trigger_return_pct"] or 0,5.0)
            self.assertFalse(self.account()["exit_requested"])
        finally:
            other.stop();self.service=previous

    def test_exact_stop_loss_boundary_is_inclusive_but_just_above_is_not(self):
        self.seed_open_account(tp=None,sl=5)
        boundary=estimated_protection_price(10,1000,5,take_profit=False)
        self.assertAlmostEqual(net_position_return_pct(10,1000,boundary),-5.0,places=9)
        exact=self.batch(request=self.base+1,btc=boundary)
        self.clock.set(self.base+1.2);self.process(exact)
        self.assertEqual(self.account()["exit_reason"],"stop_loss")
        self.assertAlmostEqual(self.account()["protection_trigger_return_pct"],-5.0,places=9)

        other=ForwardCohort(Path(self.temp.name)/"sl-above.sqlite3",fetch_daily=self.feed.history,
            fetch_quotes=self.feed.quotes,clock=self.clock,poll_seconds=300,_start_watchdog=False)
        previous=self.service
        self.service=other
        try:
            self.seed_open_account(tp=None,sl=5)
            above=self.batch(request=self.base+2,btc=boundary*(1+1e-6))
            self.clock.set(self.base+2.2);self.process(above)
            self.assertGreater(self.account()["protection_trigger_return_pct"] or 0,-5.0)
            self.assertFalse(self.account()["exit_requested"])
        finally:
            other.stop();self.service=previous

    def test_stop_loss_gap_fills_later_at_worse_price_and_trigger_return_is_preserved(self):
        cid,episode=self.seed_open_account(tp=None,sl=5)
        boundary=estimated_protection_price(10,1000,5,take_profit=False)
        trigger=self.batch(request=self.base+1,btc=boundary*.98)
        self.clock.set(self.base+1.2);self.process(trigger)
        self.assertEqual(self.account()["exit_reason"],"stop_loss")
        triggered_return=self.account()["protection_trigger_return_pct"]
        self.assertLess(triggered_return,-5)
        worse=self.batch(request=self.base+2,market=self.base+1.9,btc=75)
        self.clock.set(self.base+2.2);self.process(worse)
        with self.service.db() as db:
            fill=db.execute("SELECT raw_price,reason,realized_pnl FROM fills WHERE cohort_id=? AND side='SELL'",(cid,)).fetchone()
            ep=db.execute("SELECT protection_trigger_return_pct,realized_pnl,close_reason FROM position_episodes WHERE id=?",(episode,)).fetchone()
        self.assertEqual(fill["reason"],"stop_loss")
        self.assertEqual(fill["raw_price"],75)
        self.assertLess(fill["realized_pnl"],0)
        self.assertEqual(ep["protection_trigger_return_pct"],triggered_return)
        self.assertLess(ep["realized_pnl"],0)
        self.assertFalse(self.account()["halted"],"the isolated account loss stays below the separate 10% account halt")

    def test_take_profit_trigger_can_have_negative_later_realized_pnl_without_dd_override(self):
        cid,episode=self.seed_open_account(tp=5,sl=None)
        boundary=estimated_protection_price(10,1000,5,take_profit=True)
        trigger=self.batch(request=self.base+1,market=self.base+.9,btc=boundary*1.01)
        self.clock.set(self.base+1.2);self.process(trigger)
        self.assertEqual(self.account()["exit_reason"],"take_profit")
        triggered_return=self.account()["protection_trigger_return_pct"]
        self.assertGreaterEqual(triggered_return,5)
        gap=self.batch(request=self.base+2,market=self.base+1.9,btc=80)
        self.clock.set(self.base+2.2);self.process(gap)
        with self.service.db() as db:
            fill=db.execute("SELECT raw_price,reason,realized_pnl FROM fills WHERE cohort_id=? AND side='SELL'",(cid,)).fetchone()
            ep=db.execute("SELECT protection_trigger_return_pct,realized_pnl,close_reason FROM position_episodes WHERE id=?",(episode,)).fetchone()
        self.assertEqual(fill["reason"],"take_profit")
        self.assertEqual(fill["raw_price"],80)
        self.assertLess(fill["realized_pnl"],0)
        self.assertEqual(ep["protection_trigger_return_pct"],triggered_return)
        self.assertEqual(ep["close_reason"],"take_profit")
        self.assertLess(ep["realized_pnl"],0,"a profitable trigger must not falsify the later gap-loss fill")
        self.assertFalse(self.account()["halted"],"the small position does not reach the independent account drawdown halt")

    def test_actual_flat_price_buy_sell_records_both_sides_of_costs(self):
        cid=self.seed_active_flat_account(tp=None,sl=None)
        self.add_target_at(cohort_id=cid,target=.5)
        buy=self.batch(request=self.base+1,market=self.base+.9,btc=100)
        self.clock.set(self.base+1.2);self.process(buy)
        with self.service.db() as db:
            buy_fill=db.execute("SELECT * FROM fills WHERE cohort_id=? AND side='BUY'",(cid,)).fetchone()
        self.assertIsNotNone(buy_fill)
        self.assertGreater(buy_fill["fee"],0);self.assertGreater(buy_fill["slippage"],0)
        self.add_target_at(cohort_id=cid,target=0,signal_ts=int(self.base+2),
            created_at=self.base+1.5,due_at=self.base+1.6)
        sell=self.batch(request=self.base+2,market=self.base+1.9,btc=100)
        self.clock.set(self.base+2.2);self.process(sell)
        with self.service.db() as db:
            fills=db.execute("SELECT * FROM fills WHERE cohort_id=? ORDER BY id",(cid,)).fetchall()
            episode=db.execute("SELECT * FROM position_episodes WHERE cohort_id=?",(cid,)).fetchone()
            account=db.execute("SELECT * FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",(cid,)).fetchone()
        self.assertEqual([row["side"] for row in fills],["BUY","SELL"])
        self.assertTrue(all(row["fee"]>0 and row["slippage"]>0 for row in fills))
        self.assertEqual(account["qty"],0)
        self.assertAlmostEqual(episode["realized_pnl"],sum(row["realized_pnl"] for row in fills),places=8)
        self.assertAlmostEqual(account["realized_pnl"],episode["realized_pnl"],places=8)
        self.assertAlmostEqual(account["fees"],sum(row["fee"] for row in fills),places=8)
        self.assertAlmostEqual(account["slippage"],sum(row["slippage"] for row in fills),places=8)
        fee=FEE_BPS/10000;slip=SLIP_BPS/10000
        expected_roundtrip_return=((1-slip)*(1-fee)/((1+slip)*(1+fee))-1)*100
        self.assertAlmostEqual(episode["realized_pnl"]/episode["opening_basis"]*100,
            expected_roundtrip_return,places=8)
        self.assertAlmostEqual(account["cash"],10000+episode["realized_pnl"],places=8)

    def test_additional_buy_updates_weighted_basis_keeps_snapshot_and_rejects_prefetched_trigger(self):
        cid=self.seed_active_flat_account(tp=50,sl=14)
        self.add_target_at(cohort_id=cid,target=.5)
        first=self.batch(request=self.base+1,market=self.base+.9,btc=100)
        self.clock.set(self.base+1.2);self.process(first)
        first_basis=self.account()["basis"]
        with self.service.db() as db:
            first_buy_qty=db.execute("SELECT qty FROM fills WHERE cohort_id=? AND side='BUY'",(cid,)).fetchone()[0]
        episode_id=self.account()["episode_id"]
        self.add_target_at(cohort_id=cid,target=.5,signal_ts=int(self.base+2),
            created_at=self.base+1.5,due_at=self.base+1.6)
        second=self.batch(request=self.base+2,market=self.base+1.9,btc=90)
        self.clock.set(self.base+10);self.process(second,now=self.base+10)
        after_add=self.account()
        self.assertGreater(after_add["basis"],first_basis)
        self.assertEqual(after_add["position_tp_pct"],50)
        self.assertEqual(after_add["position_sl_pct"],14)
        with self.service.db() as db:
            ep=db.execute("SELECT opening_basis,remaining_basis,opened_qty,current_qty,take_profit_pct,stop_loss_pct,basis_updated_at FROM position_episodes WHERE id=?",(episode_id,)).fetchone()
            buy_fills=db.execute("SELECT qty,raw_price,fee,slippage FROM fills WHERE cohort_id=? AND side='BUY'",(cid,)).fetchall()
        self.assertAlmostEqual(ep["opening_basis"],after_add["basis"],places=8)
        self.assertAlmostEqual(ep["remaining_basis"],after_add["basis"],places=8)
        self.assertAlmostEqual(ep["current_qty"],after_add["qty"],places=10)
        self.assertGreater(after_add["qty"],first_buy_qty)
        self.assertEqual(len(buy_fills),2,"the second on-band decision is an actual additional BUY")
        self.assertAlmostEqual(ep["opened_qty"],sum(row["qty"] for row in buy_fills),places=10)
        self.assertAlmostEqual(ep["opening_basis"],sum(row["qty"]*row["raw_price"]+row["fee"]+row["slippage"] for row in buy_fills),places=8)
        self.assertEqual((ep["take_profit_pct"],ep["stop_loss_pct"]),(50,14))
        self.assertEqual(ep["basis_updated_at"],self.base+10)
        # This provider quote was fetched after the source receipt for the add,
        # but before the delayed transaction actually applied the new basis.
        prefetched=self.batch(request=self.base+3,market=self.base+2.9,btc=82)
        self.clock.set(self.base+12);self.process(prefetched,now=self.base+12)
        self.assertFalse(self.account()["exit_requested"])
        fresh=self.batch(request=self.base+11,market=self.base+10.9,btc=82)
        self.clock.set(self.base+12.2);self.process(fresh,now=self.base+12.2)
        self.assertEqual(self.account()["exit_reason"],"stop_loss")
        self.assertFalse(self.account()["halted"],"account drawdown remains a separate, unchanged 10% control")

    def test_tp_latches_on_fresh_quote_and_only_later_quote_fills(self):
        cid,_=self.seed_open_account(tp=5,sl=10)
        first=self.batch(request=self.base+1,btc=106)
        self.clock.set(self.base+1.2)
        self.process(first)
        account=self.account()
        self.assertTrue(account["exit_requested"])
        self.assertEqual(account["exit_reason"],"take_profit")
        self.assertEqual(account["protection_trigger_reason"],"take_profit")
        self.assertGreater(account["exit_decided_at"],first["BTC-USD"]["observed_at"]-.001)
        with self.service.db() as db:
            events=db.execute("SELECT event_type,reason,price,net_return_pct,remaining_basis,threshold_pct,receipt_at FROM protection_events WHERE cohort_id=? ORDER BY id",(cid,)).fetchall()
        self.assertEqual(len(events),1)
        self.assertEqual((events[0]["event_type"],events[0]["reason"]),("TRIGGER","take_profit"))
        self.assertEqual(events[0]["price"],106)
        self.assertGreater(events[0]["net_return_pct"],5)
        self.assertEqual(events[0]["remaining_basis"],1000)
        self.assertEqual(events[0]["threshold_pct"],5)
        with self.service.db() as db:
            pending=db.execute("SELECT status,close_reason,pending_reason,protection_trigger_reason FROM position_episodes WHERE cohort_id=?",(cid,)).fetchone()
        self.assertEqual((pending["status"],pending["close_reason"],pending["pending_reason"],pending["protection_trigger_reason"]),
            ("exit_pending",None,"take_profit","take_profit"))
        projected=next(e for e in self.service.state()["position_episodes"] if e["id"]==account["episode_id"])
        self.assertEqual(projected["protection_trigger_reason"],"take_profit")
        self.process(first)  # exact request replay is ignored
        self.assertEqual(len(self.service.state()["protection_events"]),1)
        self.assertGreater(self.account()["qty"],0)
        self.clock.set(self.base+2.2)
        second=self.batch(request=self.base+2,btc=108)
        self.process(second)
        account=self.account()
        self.assertFalse(account["exit_requested"])
        self.assertEqual(account["qty"],0)
        self.assertEqual(account["cooldown_after_at"],self.base+2.2)
        with self.service.db() as db:
            fill=db.execute("SELECT side,reason,request_at,receipt_at,market_at,applied_at,episode_id FROM fills WHERE cohort_id=?",(cid,)).fetchone()
            episode=db.execute("SELECT status,close_reason,pending_reason,closed_at,close_receipt_at,realized_pnl FROM position_episodes WHERE cohort_id=?",(cid,)).fetchone()
        self.assertEqual(fill["reason"],"take_profit")
        self.assertGreater(fill["request_at"],events[0]["receipt_at"] or self.base)
        self.assertEqual(fill["applied_at"],self.base+2.2)
        self.assertEqual(episode["status"],"closed")
        self.assertEqual(episode["close_reason"],"take_profit")
        self.assertIsNone(episode["pending_reason"])
        self.assertEqual(episode["closed_at"],self.base+2.2)
        self.assertEqual(episode["close_receipt_at"],self.base+2.1)

    def test_stop_can_trip_from_roundtrip_costs_even_when_raw_price_is_unchanged(self):
        self.seed_open_account(tp=None,sl=.1)
        batch=self.batch(request=self.base+1,btc=100)
        self.clock.set(self.base+1.2)
        self.process(batch)
        account=self.account()
        self.assertEqual(account["exit_reason"],"stop_loss")
        self.assertLess(account["protection_trigger_return_pct"],0)
        self.assertLessEqual(account["protection_trigger_return_pct"],-.1)
        self.assertGreater(account["qty"],0)

    def test_drawdown_halt_has_priority_over_stop_loss_on_same_quote(self):
        self.seed_open_account(qty=100,basis=10000,cash=0,tp=None,sl=.1)
        batch=self.batch(request=self.base+1,btc=89)
        self.clock.set(self.base+1.2)
        self.process(batch)
        account=self.account()
        self.assertTrue(account["halted"])
        self.assertEqual(account["exit_reason"],"account_drawdown")
        self.assertIsNone(account["protection_trigger_reason"])
        with self.service.db() as db:
            event=db.execute("SELECT reason,priority FROM protection_events WHERE event_type='TRIGGER'").fetchone()
        self.assertEqual((event["reason"],event["priority"]),("account_drawdown",EXIT_PRIORITY["account_drawdown"]))

    def test_higher_priority_manual_override_records_new_time_and_requires_fresher_quote(self):
        cid,_=self.seed_open_account(tp=5,sl=10)
        trigger=self.batch(request=self.base+1,btc=106)
        self.clock.set(self.base+1.2)
        self.process(trigger)
        original=self.account()["exit_decided_at"]
        with self.service.db() as db:
            account=db.execute("SELECT * FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",(cid,)).fetchone()
            changed=self.service._latch_exit(db,account,reason="manual_close",decided_at=self.base+5,
                basis=float(account["basis"]),detail="synthetic manual priority override")
        self.assertTrue(changed)
        account=self.account()
        self.assertEqual(account["exit_reason"],"manual_close")
        self.assertEqual(account["exit_decided_at"],self.base+5)
        self.assertGreater(account["exit_decided_at"],original)
        with self.service.db() as db:
            rows=db.execute("SELECT event_type,reason,decided_at,detail FROM protection_events ORDER BY id").fetchall()
        self.assertEqual([(x["event_type"],x["reason"]) for x in rows],[('TRIGGER','take_profit'),('OVERRIDE','manual_close')])
        self.assertIn("Superseded take_profit",rows[-1]["detail"])
        with self.service.db() as db:
            episode=db.execute("SELECT status,close_reason,pending_reason,protection_trigger_reason FROM position_episodes WHERE cohort_id=?",(cid,)).fetchone()
        self.assertEqual((episode["status"],episode["close_reason"],episode["pending_reason"],episode["protection_trigger_reason"]),
            ("exit_pending",None,"manual_close","take_profit"))
        # A prefetched response received before the new manual decision cannot fill it.
        self.clock.set(self.base+6)
        before=self.batch(request=self.base+3,receipt=self.base+3.1,market=self.base+2.9,btc=107)
        self.process(before)
        self.assertGreater(self.account()["qty"],0)
        self.clock.set(self.base+7.2)
        after=self.batch(request=self.base+7,receipt=self.base+7.1,market=self.base+6.9,btc=107)
        self.process(after)
        self.assertEqual(self.account()["qty"],0)

    def test_old_closed_override_migration_recovers_trigger_only_from_event_ledger(self):
        cid,episode_id=self.seed_open_account(tp=5,sl=10)
        trigger=self.batch(request=self.base+1,btc=106)
        self.clock.set(self.base+1.2);self.process(trigger)
        with self.service.db() as db:
            account=db.execute("SELECT * FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",(cid,)).fetchone()
            self.assertTrue(self.service._latch_exit(db,account,reason="manual_close",decided_at=self.base+2,
                basis=float(account["basis"]),detail="synthetic migration fixture override"))
        self.clock.set(self.base+3.2)
        later=self.batch(request=self.base+3,market=self.base+2.9,btc=107)
        self.process(later)
        with self.service.db() as db:
            before=db.execute("SELECT status,close_reason,realized_pnl,closed_at FROM position_episodes WHERE id=?",(episode_id,)).fetchone()
            fill_count=db.execute("SELECT count(*) FROM fills WHERE cohort_id=?",(cid,)).fetchone()[0]
            # Emulate the schema before the explicit episode trigger fields.
            db.execute("ALTER TABLE position_episodes DROP COLUMN pending_reason")
            db.execute("ALTER TABLE position_episodes DROP COLUMN protection_trigger_reason")
            db.execute("UPDATE position_episodes SET protection_triggered_at=NULL,protection_trigger_price=NULL,protection_trigger_return_pct=NULL,protection_trigger_basis=NULL,protection_trigger_threshold_pct=NULL WHERE id=?",(episode_id,))
        self.service.stop()
        self.service=ForwardCohort(self.path,fetch_daily=self.feed.history,fetch_quotes=self.feed.quotes,
            clock=self.clock,poll_seconds=300,_start_watchdog=False)
        with self.service.db() as db:
            after=db.execute("SELECT status,close_reason,pending_reason,protection_trigger_reason,protection_triggered_at,protection_trigger_price,protection_trigger_return_pct,protection_trigger_basis,protection_trigger_threshold_pct,realized_pnl,closed_at FROM position_episodes WHERE id=?",(episode_id,)).fetchone()
            after_fills=db.execute("SELECT count(*) FROM fills WHERE cohort_id=?",(cid,)).fetchone()[0]
        self.assertEqual((after["status"],after["close_reason"],after["pending_reason"],after["protection_trigger_reason"]),
            ("closed","manual_close",None,"take_profit"))
        self.assertEqual((after["protection_triggered_at"],after["protection_trigger_price"],after["protection_trigger_threshold_pct"]),
            (self.base+1.2,106,5))
        self.assertIsNotNone(after["protection_trigger_basis"])
        self.assertIsNotNone(after["protection_trigger_return_pct"])
        self.assertEqual(after["realized_pnl"],before["realized_pnl"])
        self.assertEqual(after["closed_at"],before["closed_at"])
        self.assertEqual(after_fills,fill_count)

    def test_session_close_overrides_latched_take_profit_and_requires_post_deadline_quote(self):
        cid,_=self.seed_open_account(tp=5,sl=10)
        deadline=self.base+3
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET session_deadline_at=?,expiry_policy='CLOSE' WHERE id=?",(deadline,cid))
        trigger=self.batch(request=self.base+1,market=self.base+.9,btc=106)
        self.clock.set(self.base+1.2);self.process(trigger)
        first_decision=self.account()["exit_decided_at"]
        with self.service.db() as db:
            cohort=db.execute("SELECT * FROM cohorts WHERE id=?",(cid,)).fetchone()
            self.assertTrue(self.service._terminalize_locked(db,cohort,when=deadline,
                reason="Synthetic fixed deadline.",status="expired_pending_close"))
        # Keep the service worker slot occupied so the fixture can advance
        # source timestamps explicitly without an asynchronous quote race.
        self.service.worker_lock.acquire()
        try:
            account=self.account()
            self.assertEqual(account["exit_reason"],"session_close")
            self.assertEqual(account["exit_priority"],EXIT_PRIORITY["session_close"])
            self.assertEqual(account["exit_decided_at"],deadline)
            self.assertGreater(account["exit_decided_at"],first_decision)
            with self.service.db() as db:
                events=db.execute("SELECT event_type,reason,decided_at FROM protection_events ORDER BY id").fetchall()
                episode=db.execute("SELECT status,close_reason,pending_reason,protection_trigger_reason FROM position_episodes WHERE cohort_id=?",(cid,)).fetchone()
            self.assertEqual([(row["event_type"],row["reason"]) for row in events],
                [("TRIGGER","take_profit"),("OVERRIDE","session_close")])
            self.assertEqual((episode["status"],episode["close_reason"],episode["pending_reason"],episode["protection_trigger_reason"]),
                ("exit_pending",None,"session_close","take_profit"))
            before=self.batch(request=self.base+2,market=self.base+2.1,btc=100)
            self.clock.set(self.base+3.2);self.process(before,now=self.base+3.2)
            self.assertGreater(self.account()["qty"],0,"pre-deadline-source quote cannot satisfy the session-close latch")
            after=self.batch(request=self.base+4,market=self.base+3.9,btc=100)
            self.clock.set(self.base+4.2);self.process(after,now=self.base+4.2)
            self.assertEqual(self.account()["qty"],0)
            with self.service.db() as db:
                fill=db.execute("SELECT reason,request_at FROM fills WHERE cohort_id=? AND side='SELL'",(cid,)).fetchone()
                episode=db.execute("SELECT status,close_reason,pending_reason,protection_trigger_reason,closed_at FROM position_episodes WHERE cohort_id=?",(cid,)).fetchone()
            self.assertEqual(fill["reason"],"session_close")
            self.assertGreater(fill["request_at"],deadline)
            self.assertEqual((episode["status"],episode["close_reason"],episode["pending_reason"],episode["protection_trigger_reason"]),
                ("closed","session_close",None,"take_profit"))
        finally:
            self.service.worker_lock.release()

    def test_exit_only_expiry_keeps_tp_monitoring_and_never_opens_a_new_position(self):
        cid,_=self.seed_open_account(tp=5,sl=10)
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET session_deadline_at=?,expiry_policy='EXIT_ONLY' WHERE id=?",(self.base+1,cid))
            cohort=db.execute("SELECT * FROM cohorts WHERE id=?",(cid,)).fetchone()
            self.assertFalse(self.service._terminalize_locked(db,cohort,when=self.base+1,
                reason="Synthetic EXIT_ONLY deadline.",status="expired_exit_only"))
        trigger=self.batch(request=self.base+2,market=self.base+1.9,btc=106)
        self.clock.set(self.base+2.2);self.process(trigger,now=self.base+2.2)
        self.assertEqual(self.account()["exit_reason"],"take_profit")
        self.assertEqual(self.account()["qty"],10)
        close=self.batch(request=self.base+3,market=self.base+2.9,btc=108)
        self.clock.set(self.base+3.2);self.process(close,now=self.base+3.2)
        self.assertEqual(self.account()["qty"],0)
        with self.service.db() as db:
            fills=db.execute("SELECT side,reason FROM fills WHERE cohort_id=?",(cid,)).fetchall()
        self.assertEqual([(row["side"],row["reason"]) for row in fills],[("SELL","take_profit")])

    def test_latched_pending_exit_survives_stale_quote_and_partial_batch(self):
        cid,_=self.seed_open_account(tp=5,sl=10)
        trigger=self.batch(request=self.base+1,market=self.base+.9,btc=106)
        self.clock.set(self.base+1.2);self.process(trigger)
        with self.assertRaisesRegex(ValueError,"complete BTC/ETH"):
            self.service._process_quote_batch(cid,{"BTC-USD":trigger["BTC-USD"]},self.base+2)
        self.assertEqual(self.account()["exit_reason"],"take_profit")
        self.assertGreater(self.account()["qty"],0)
        stale=self.batch(request=self.base+2,market=self.base+.9,btc=100)
        self.clock.set(self.base+2.2);self.process(stale,now=self.base+2.2)
        self.assertGreater(self.account()["qty"],0,"a quote with the old provider observation time cannot fill")
        self.assertTrue(self.account()["exit_requested"])
        fresh=self.batch(request=self.base+3,market=self.base+2.9,btc=100)
        self.clock.set(self.base+3.2);self.process(fresh,now=self.base+3.2)
        self.assertEqual(self.account()["qty"],0)
        with self.service.db() as db:
            fills=db.execute("SELECT side,reason FROM fills WHERE cohort_id=?",(cid,)).fetchall()
        self.assertEqual([(row["side"],row["reason"]) for row in fills],[("SELL","take_profit")])

    def test_delayed_processing_uses_actual_latch_time_not_earlier_receipt(self):
        self.seed_open_account(tp=5,sl=10)
        quote=self.batch(request=self.base+1,receipt=self.base+1.05,market=self.base+.95,btc=106)
        self.clock.set(self.base+10)
        self.process(quote,now=self.base+10)
        decision_at=self.account()["exit_decided_at"]
        self.assertEqual(decision_at,self.base+10)
        prefetched=self.batch(request=self.base+5,receipt=self.base+5.05,market=self.base+4.95,btc=106)
        self.clock.set(self.base+12)
        self.process(prefetched,now=self.base+12)
        self.assertGreater(self.account()["qty"],0)
        later=self.batch(request=self.base+11,receipt=self.base+11.05,market=self.base+10.9,btc=106)
        self.clock.set(self.base+12)
        self.process(later,now=self.base+12)
        self.assertEqual(self.account()["qty"],0)
        self.assertEqual(self.account()["cooldown_after_at"],self.base+12)

    def test_entry_quote_cannot_trigger_after_buy_even_when_replayed(self):
        cid=self.service.state()["cohort_id"]
        at=self.base
        with self.service.db() as db:
            config=self.service._config()
            session={"session_id":"fixture","started_at":at-3600,"deadline_at":at+3600,
                "position_protection":{"take_profit_pct":None,"stop_loss_pct":.1,"schema":PROTECTION_SCHEMA}}
            quotes={"BTC-USD":{"price":100,"market_at":at-21,"requested_at":at-20,"observed_at":at-19},
                "ETH-USD":{"price":10,"market_at":at-21,"requested_at":at-20,"observed_at":at-19}}
            db.execute("UPDATE cohorts SET state='running',start_at=?,session_started_at=?,session_deadline_at=?,session_status='active',clock_high_water=?,last_quote_request=?,last_quote_receipt=?,latest_quote_json=?,session_config_json=?,protection_schema_version=?,config_json=? WHERE id=?",
                (at-3600,at-3600,at+3600,at,at-20,at-19,json.dumps(quotes),json.dumps(session),PROTECTION_SCHEMA,json.dumps(config),cid))
            db.execute("INSERT INTO accounts(cohort_id,symbol,strategy,initial,cash,peak_nlv,last_price,last_market_at,last_receipt_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cid,"BTC-USD","passive",10000,10000,10000,100,at-21,at-19,at))
            db.execute("INSERT INTO decisions(cohort_id,symbol,strategy,signal_ts,created_at,due_at,expires_at,target_weight,status,reason,source_observation_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (cid,"BTC-USD","passive",int(at-1000),at-900,at-600,at+900,.5,"pending","synthetic entry",int(at-1000)))
        quote=self.batch(request=at+1,receipt=at+1.05,market=at+.95,btc=100)
        self.clock.set(at+1.1);self.process(quote)
        account=self.account()
        self.assertGreater(account["qty"],0)
        self.assertFalse(account["exit_requested"])
        self.assertEqual(account["position_sl_pct"],.1)
        with self.service.db() as db:
            fill=db.execute("SELECT fee,slippage,qty,raw_price,episode_id,applied_at FROM fills WHERE cohort_id=?",(cid,)).fetchone()
            expected=fill["qty"]*fill["raw_price"]+fill["fee"]+fill["slippage"]
            self.assertAlmostEqual(account["basis"],expected,places=8)
            self.assertAlmostEqual(fill["applied_at"],at+1.1)
        self.process(quote)  # exact duplicate batch
        self.assertFalse(self.account()["exit_requested"])
        later_market=self.batch(request=at+2,receipt=at+2.05,market=at+.95,btc=100)
        self.clock.set(at+2.1);self.process(later_market)
        self.assertFalse(self.account()["exit_requested"],"same provider snapshot cannot retrigger after its own BUY")
        applied=self.account()["basis_updated_at"]
        too_early=self.batch(request=applied-.05,receipt=applied-.01,market=applied-.02,btc=100)
        self.clock.set(applied+1);self.process(too_early,now=applied+1)
        self.assertFalse(self.account()["exit_requested"],"source quote received before actual fill application is ineligible")
        fresh=self.batch(request=applied+1,receipt=applied+1.05,market=applied+.9,btc=100)
        self.clock.set(applied+1.1);self.process(fresh)
        self.assertEqual(self.account()["exit_reason"],"stop_loss")

    def test_out_of_order_provider_snapshot_is_ignored(self):
        cid,_=self.seed_open_account(tp=5,sl=10)
        newer=self.batch(request=self.base+1,market=self.base+.9,btc=100)
        self.clock.set(self.base+1.2);self.process(newer)
        older=self.batch(request=self.base+2,market=self.base+.8,btc=120)
        self.clock.set(self.base+2.2);self.process(older)
        self.assertFalse(self.account()["exit_requested"])
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM quote_batches WHERE cohort_id=?",(cid,)).fetchone()[0],1)

    def test_partial_rebalance_reduces_remaining_basis_pro_rata(self):
        cid,episode=self.seed_open_account(qty=50,basis=5000,cash=5000,tp=20,sl=10)
        qty=self.account()["qty"]; basis=self.account()["basis"]
        self.add_pending_target(target=.2,signal_ts=int(self.base-800))
        batch=self.batch(request=self.base+1,btc=100)
        self.clock.set(self.base+1.2);self.process(batch)
        account=self.account()
        self.assertLess(account["qty"],qty)
        self.assertGreater(account["qty"],0)
        with self.service.db() as db:
            e=db.execute("SELECT remaining_basis,current_qty,basis_updated_at,basis_updated_receipt_at FROM position_episodes WHERE id=?",(episode,)).fetchone()
            sell=db.execute("SELECT qty,realized_pnl FROM fills WHERE cohort_id=? AND side='SELL'",(cid,)).fetchone()
        expected=basis*(account["qty"]/qty)
        self.assertAlmostEqual(account["basis"],expected,places=8)
        self.assertAlmostEqual(e["remaining_basis"],expected,places=8)
        self.assertAlmostEqual(e["current_qty"],account["qty"],places=10)
        self.assertEqual(e["basis_updated_at"],self.base+1.2)
        self.assertEqual(e["basis_updated_receipt_at"],self.base+1.1)
        self.assertGreater(sell["qty"],0)

    def test_per_account_cooldown_and_breakout_reentry_requires_fresh_channel_break(self):
        cid,episode=self.seed_open_account(strategy="close_breakout_20_10",tp=5,sl=10)
        trigger=self.batch(request=self.base+1,btc=106)
        self.clock.set(self.base+1.2);self.process(trigger)
        close=self.batch(request=self.base+2,btc=108)
        self.clock.set(self.base+2.2);self.process(close)
        account=self.account("close_breakout_20_10")
        self.assertEqual(account["qty"],0)
        self.assertEqual(account["signal_active"],0)
        cooldown=float(account["cooldown_after_at"])
        points=[{"ts":int(self.base)+i*86400,"price":100+i*.1} for i in range(101)]
        points[-2]["price"]=130;points[-1]["price"]=120  # inside prior channel; no fresh breakout
        signal=int(cooldown)
        self.service._create_decisions(cid,signal,self.base+10,signal+14400,{"BTC-USD":points,"ETH-USD":points})
        with self.service.db() as db:
            row=db.execute("SELECT status,reason FROM decisions WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='close_breakout_20_10' AND signal_ts=?",(cid,signal)).fetchone()
        self.assertEqual(row["status"],"no_trade")
        self.assertIn("cooldown",row["reason"].lower())
        signal2=int(cooldown)+86400
        self.service._create_decisions(cid,signal2,self.base+86420,signal2+14400,{"BTC-USD":points,"ETH-USD":points})
        with self.service.db() as db:
            row=db.execute("SELECT status,target_weight FROM decisions WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='close_breakout_20_10' AND signal_ts=?",(cid,signal2)).fetchone()
        self.assertEqual(row["status"],"pending")
        self.assertEqual(row["target_weight"],0.0)
        points[-1]["price"]=140  # next evaluated point breaks above the previous 20 closes
        signal3=signal2+86400
        self.service._create_decisions(cid,signal3,self.base+2*86400+20,signal3+14400,{"BTC-USD":points,"ETH-USD":points})
        with self.service.db() as db:
            row=db.execute("SELECT status,target_weight FROM decisions WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='close_breakout_20_10' AND signal_ts=?",(cid,signal3)).fetchone()
        self.assertEqual(row["status"],"pending")
        self.assertGreater(row["target_weight"],0)

    def test_legacy_started_cohort_is_preserved_but_cannot_resume_or_adopt_thresholds(self):
        cid,episode=self.seed_open_account(tp=5,sl=10)
        with self.service.db() as db:
            db.execute("INSERT INTO fills(cohort_id,symbol,strategy,signal_ts,request_at,receipt_at,market_at,side,qty,raw_price,fill_price,fee,slippage,realized_pnl,reason,episode_id,applied_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid,"BTC-USD","passive",int(self.base-100),self.base-100,self.base-99,self.base-101,"BUY",10,100,100.1,2.5,1,0,"legacy fixture",episode,self.base-99))
            db.execute("UPDATE cohorts SET protection_schema_version=NULL,session_config_json=NULL WHERE id=?",(cid,))
            db.execute("UPDATE accounts SET position_tp_pct=NULL,position_sl_pct=NULL,episode_id=NULL,position_opened_at=NULL,basis_updated_at=NULL,basis_updated_receipt_at=NULL,basis_updated_market_at=NULL WHERE cohort_id=?",(cid,))
            db.execute("DELETE FROM position_episodes WHERE cohort_id=?",(cid,))
        self.service.stop()
        migrated=ForwardCohort(self.path,fetch_daily=self.feed.history,fetch_quotes=self.feed.quotes,
            clock=self.clock,poll_seconds=300,_start_watchdog=False)
        state=migrated.state()
        self.assertTrue(state["protection_legacy_frozen"])
        self.assertEqual(state["session_status"],"paused")
        self.assertTrue(state["entry_disabled"])
        account=next(a for a in state["accounts"] if a["strategy"]=="passive")
        self.assertEqual(account["qty"],10)
        self.assertIsNone(account["position_tp_pct"])
        with migrated.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM fills WHERE cohort_id=?",(cid,)).fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM position_episodes WHERE id=?",(episode,)).fetchone()[0],0,
                "legacy positions must not receive a retroactive protection episode")
        with self.assertRaisesRegex(RuntimeError,"frozen without retroactive TP/SL"):
            migrated.control("resume")
        migrated.stop()
        self.service=migrated

    def _latch_pending_protection(self, cid, *, decided_at=None):
        decided_at = self.base + .5 if decided_at is None else decided_at
        with self.service.db() as db:
            account = db.execute("SELECT * FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'", (cid,)).fetchone()
            self.service._latch_exit(db, account, reason="take_profit", decided_at=decided_at,
                market_at=self.base, request_at=self.base, receipt_at=self.base,
                price=106, return_pct=5.1, basis=float(account["basis"]), threshold_pct=5)
            db.execute("UPDATE cohorts SET state='protection_pending',session_status='paused',entry_disabled=0,close_retry_after=NULL,reason='Synthetic latched close.' WHERE id=?", (cid,))

    def test_quote_only_worker_survives_watchdog_and_state_calls_while_fetch_is_blocked(self):
        cid,_ = self.seed_open_account(tp=5, sl=10)
        self._latch_pending_protection(cid)
        started = threading.Event()
        release = threading.Event()
        original = self.feed.quotes

        def blocked_quotes():
            started.set()
            if not release.wait(2):
                raise RuntimeError("test quote gate timed out")
            return original()

        self.service.fetch_quotes = blocked_quotes
        self.clock.set(self.base + 2)
        self.service._ensure_protection_worker()
        self.assertTrue(started.wait(1), "quote-only worker did not begin its public quote request")
        with self.service.db() as db:
            generation = db.execute("SELECT launch_generation FROM cohorts WHERE id=?", (cid,)).fetchone()[0]
        for _ in range(4):
            self.service._advance_session_clock()
            self.service.state()
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT launch_generation FROM cohorts WHERE id=?", (cid,)).fetchone()[0], generation)
            self.assertIsNone(db.execute("SELECT error FROM cohorts WHERE id=?", (cid,)).fetchone()[0])
        self.assertTrue(self.service.worker.is_alive())
        release.set()
        self.service.worker.join(timeout=2)
        self.assertFalse(self.service.worker.is_alive())
        self.assertEqual(self.account()["qty"], 0)
        with self.service.db() as db:
            fill = db.execute("SELECT side,reason FROM fills WHERE cohort_id=? AND strategy='passive' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
            state = db.execute("SELECT state FROM cohorts WHERE id=?", (cid,)).fetchone()[0]
        self.assertEqual((fill["side"], fill["reason"]), ("SELL", "take_profit"))
        self.assertEqual(state, "paused")

    def test_restart_pending_protective_close_retries_and_fills_on_fresh_quote(self):
        cid,_ = self.seed_open_account(tp=5, sl=10)
        self._latch_pending_protection(cid)
        # Simulate process loss after a trigger was durably latched but before
        # the service converted the active worker into its paused retry state.
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET state='running',session_status='active',entry_disabled=0 WHERE id=?",(cid,))
        self.clock.set(self.base + 3)
        restarted = ForwardCohort(self.path, fetch_daily=self.feed.history, fetch_quotes=self.feed.quotes,
            clock=self.clock, poll_seconds=300, _start_watchdog=False)
        try:
            worker = restarted.worker
            self.assertIsNotNone(worker)
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            with restarted.db() as db:
                account = db.execute("SELECT qty,exit_requested FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'", (cid,)).fetchone()
                fill = db.execute("SELECT side,reason FROM fills WHERE cohort_id=? AND strategy='passive' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
            self.assertEqual(account["qty"], 0)
            self.assertEqual(account["exit_requested"], 0)
            self.assertEqual((fill["side"], fill["reason"]), ("SELL", "take_profit"))
            after_close = restarted.state()
            self.assertEqual(after_close["state"], "paused")
            self.assertFalse(after_close["entry_disabled"], "a protective retry must not permanently disable other accounts")
            self.assertIsNotNone(after_close["accounts"][0]["cooldown_after_at"])
            original_session = after_close["session_id"]
            original_deadline = after_close["session_deadline_utc"]
            day = int(self.base // 86400) * 86400
            points = [{"ts": day - i * 86400, "price": 100.0 + i / 1000} for i in reversed(range(101))]
            points.sort(key=lambda x: x["ts"])
            restarted.fetch_daily = lambda symbol: (points, {"fixture": True})
            self.clock.set(self.base + 4)
            resumed = restarted.control("resume")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                resumed = restarted.state()
                if resumed["state"] == "running" or resumed.get("error"):
                    break
                time.sleep(.01)
            self.assertEqual(resumed["state"], "running", resumed.get("error"))
            self.assertEqual(resumed["session_status"], "active")
            self.assertFalse(resumed["entry_disabled"])
            self.assertEqual(resumed["session_id"], original_session)
            self.assertEqual(resumed["session_deadline_utc"], original_deadline)
            btc_passive = next(a for a in resumed["accounts"] if a["symbol"]=="BTC-USD" and a["strategy"]=="passive")
            prior_btc_passive = next(a for a in after_close["accounts"] if a["symbol"]=="BTC-USD" and a["strategy"]=="passive")
            self.assertEqual(btc_passive["cooldown_after_at"],prior_btc_passive["cooldown_after_at"])
        finally:
            restarted.stop()

    def test_paused_protection_worker_does_not_monitor_unlatched_accounts(self):
        cid,_ = self.seed_open_account(tp=5, sl=10)
        self._latch_pending_protection(cid)
        with self.service.db() as db:
            db.execute("INSERT INTO accounts(cohort_id,symbol,strategy,initial,cash,qty,basis,peak_nlv,last_price,last_market_at,last_receipt_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid,"ETH-USD","passive",10000,5000,500,5000,10000,10,self.base-21,self.base-19,self.base))
            db.execute("UPDATE cohorts SET launch_generation=launch_generation+1,close_retry_after=? WHERE id=?",(self.base+1000,cid))
            generation=db.execute("SELECT launch_generation FROM cohorts WHERE id=?",(cid,)).fetchone()[0]
        batch=self.batch(request=self.base+1,btc=108,eth=8)
        self.clock.set(self.base+1.2)
        self.service._process_quote_batch(cid,batch,self.base+1.2,generation=generation)
        with self.service.db() as db:
            eth=db.execute("SELECT qty,halted,exit_requested,last_price FROM accounts WHERE cohort_id=? AND symbol='ETH-USD' AND strategy='passive'",(cid,)).fetchone()
            eth_fills=db.execute("SELECT count(*) FROM fills WHERE cohort_id=? AND symbol='ETH-USD'",(cid,)).fetchone()[0]
            eth_events=db.execute("SELECT count(*) FROM protection_events WHERE cohort_id=? AND symbol='ETH-USD'",(cid,)).fetchone()[0]
        self.assertEqual((eth["qty"],eth["halted"],eth["exit_requested"],eth["last_price"]),(500,0,0,10))
        self.assertEqual(eth_fills,0)
        self.assertEqual(eth_events,0)
        self.assertEqual(self.account()["qty"],0,"only the already-latched BTC position should close")

    def test_export_contains_session_snapshot_episode_and_protection_audit(self):
        cid,_=self.seed_open_account(tp=6,sl=2)
        batch=self.batch(request=self.base+1,btc=107)
        self.clock.set(self.base+1.2);self.process(batch)
        rows=list(csv.DictReader(self.service.export_csv().splitlines()))
        cohort=next(x for x in rows if x["record_type"]=="cohort")
        episode=next(x for x in rows if x["record_type"]=="position_episode")
        event=next(x for x in rows if x["record_type"]=="protection_event")
        self.assertEqual(cohort["protection_schema_version"],PROTECTION_SCHEMA)
        self.assertIn('"take_profit_pct": 6',cohort["session_config_json"])
        self.assertEqual(episode["take_profit_pct"],"6.0")
        self.assertEqual(event["reason"],"take_profit")
        self.assertEqual(event["threshold_pct"],"6.0")
        self.assertEqual(event["provider_market_utc"],dt.datetime.fromtimestamp(self.base+.9,dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z"))

    def test_projection_says_protection_is_paused_and_marks_valuation_time(self):
        cid,_=self.seed_open_account(tp=5,sl=10)
        self.clock.set(self.base+200)
        account=self.account()
        self.assertIn("Awaiting fresh quote",account["protection_state"])
        self.assertFalse(account["valuation_is_live"],"fixture quote is deliberately stale")
        self.assertIsNotNone(account["valuation_quote_utc"])
        self.assertGreater(account["valuation_age_seconds"],120)
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET state='paused',session_status='paused' WHERE id=?",(cid,))
        account=self.account()
        self.assertEqual(account["protection_state"],"Configured · monitoring paused; last-quote estimate only")
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET protection_schema_version='legacy-v5.1' WHERE id=?",(cid,))
            db.execute("UPDATE accounts SET position_tp_pct=NULL,position_sl_pct=NULL WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",(cid,))
        account=self.account()
        self.assertEqual(account["protection_state"],"Legacy holding · V5.2 TP/SL not applied")

    def test_synthetic_qa_marker_is_opt_in_and_survives_projection(self):
        cid,_=self.seed_open_account(tp=5,sl=10)
        self.assertFalse(self.service.state()["qa_fixture"])
        with self.service.db() as db:
            cohort=db.execute("SELECT session_config_json FROM cohorts WHERE id=?",(cid,)).fetchone()
            config=json.loads(cohort["session_config_json"])
            config["qa_fixture_label"]="SYNTHETIC QA FIXTURE — no market data"
            db.execute("UPDATE cohorts SET session_config_json=? WHERE id=?",(json.dumps(config,sort_keys=True),cid))
        state=self.service.state()
        self.assertTrue(state["qa_fixture"])
        self.assertIn("SYNTHETIC QA FIXTURE",state["session_config"]["qa_fixture_label"])

    def test_offline_qa_fixture_has_permanent_label_pending_exit_and_realized_history(self):
        feed=OfflineQaFeed()
        self.service.fetch_daily=feed.history
        self.service.fetch_quotes=feed.quotes
        seed_protection_fixture(self.service)
        state=self.service.state()
        self.assertTrue(state["qa_fixture"])
        self.assertEqual(feed.history_calls,0)
        self.assertEqual(feed.quote_calls,0)
        pending=next(a for a in state["accounts"] if a["symbol"]=="BTC-USD" and a["strategy"]=="passive")
        self.assertTrue(pending["exit_requested"])
        self.assertIn("pending",pending["protection_state"].lower())
        pending_episode=next(e for e in state["position_episodes"] if e["id"]==pending["episode_id"])
        self.assertEqual((pending_episode["status"],pending_episode["close_reason"],pending_episode["pending_reason"],pending_episode["protection_trigger_reason"]),
            ("exit_pending",None,"take_profit","take_profit"))
        self.assertEqual(state["totals"]["all_forward_fills"],2)
        closed=next(e for e in state["position_episodes"] if e["status"]=="closed")
        self.assertLess(closed["net_realized_pnl_usd"],0)
        self.assertEqual(closed["close_reason"],"stop_loss")
        self.assertIsNone(closed["protection_trigger_reason"],"fixture intentionally lacks trigger evidence")
        self.assertTrue(all(q["source"]=="synthetic QA fixture" for q in state["latest_quotes"].values()))

    def test_episode_cost_and_fill_history_aggregate_past_recent_api_limit(self):
        cid,episode=self.seed_open_account(tp=5,sl=10)
        with self.service.db() as db:
            for i in range(101):
                db.execute("INSERT INTO fills(cohort_id,symbol,strategy,signal_ts,request_at,receipt_at,market_at,side,qty,raw_price,fill_price,fee,slippage,realized_pnl,reason,episode_id,applied_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid,"BTC-USD","passive",int(self.base)+i,self.base+i,self.base+i+.1,self.base+i-.1,
                     "SELL" if i%2 else "BUY",.01,100,99.9,.1,.2,-.05,"synthetic episode fill",episode,self.base+i+.1))
            db.execute("UPDATE position_episodes SET realized_pnl=-5.05 WHERE id=?",(episode,))
        state=self.service.state()
        self.assertEqual(len(state["fills"]),100)
        item=next(x for x in state["position_episodes"] if x["id"]==episode)
        self.assertEqual(item["all_episode_fills"],101)
        self.assertAlmostEqual(item["total_fees_usd"],10.1)
        self.assertAlmostEqual(item["total_slippage_usd"],20.2)
        self.assertAlmostEqual(item["total_entry_cost_usd"],1000)
        self.assertAlmostEqual(item["remaining_basis"],1000)
        self.assertAlmostEqual(item["net_realized_pnl_usd"],-5.05)

    def test_forward_api_validates_and_persists_optional_tp_sl_values(self):
        cid=self.service.state()["cohort_id"]
        latest=int(self.base//86400)*86400
        daily=[{"ts":latest-i*86400,"price":100+i*.01} for i in reversed(range(366))]
        daily.sort(key=lambda point:point["ts"])
        self.service.fetch_daily=lambda symbol:(daily,{"fixture":"synthetic"})
        lab=engine.Lab(Path(self.temp.name)/"api-lab.sqlite3")
        jobs=ResearchJobs(Path(self.temp.name)/"api-research")
        httpd=ThreadingHTTPServer(("127.0.0.1",0),handler_for(lab,jobs,self.service))
        thread=threading.Thread(target=httpd.serve_forever,daemon=True)
        thread.start()
        def post(payload):
            client=http.client.HTTPConnection("127.0.0.1",httpd.server_address[1],timeout=3)
            client.request("POST","/api/forward/v5",json.dumps(payload),
                {"Content-Type":"application/json","X-Crypto-Lab":"1"})
            response=client.getresponse();body=json.loads(response.read());client.close()
            return response.status,body
        try:
            invalid,body=post({"action":"start","confirm":"START","duration_minutes":60,
                "expiry_policy":"CLOSE","take_profit_pct":True,"stop_loss_pct":.1})
            self.assertEqual(invalid,400)
            self.assertIn("Take-profit",body["error"])
            self.assertEqual(self.service.state()["session_status"],"not_started")
            status,result=post({"action":"start","confirm":"START","duration_minutes":60,
                "expiry_policy":"CLOSE","take_profit_pct":.2,"stop_loss_pct":99.9})
            self.assertEqual(status,202,result)
            protection=result["state"]["session_config"]["position_protection"]
            self.assertEqual(protection["take_profit_pct"],.2)
            self.assertEqual(protection["stop_loss_pct"],99.9)
            self.assertEqual(result["state"]["cohort_id"],cid)
        finally:
            self.service.stop()
            jobs.stop()
            httpd.shutdown();httpd.server_close();thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
