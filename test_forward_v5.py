"""Fixture-only tests for the opt-in prospective V5 paper cohort."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from forward_v5 import ForwardCohort, STRATEGIES, SYMBOLS, validate_batch

DAY = 86_400


class Clock:
    def __init__(self, value: float):
        self.value = float(value)
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def set(self, value: float):
        with self.lock:
            self.value = float(value)


def floor_day(ts: float) -> int:
    return int(ts // DAY * DAY)


class FakeFeed:
    def __init__(self, clock: Clock, latest_ts: int):
        self.clock = clock
        self.latest_ts = latest_ts
        self.daily_calls = []
        self.quote_calls = 0
        self.quote_entered = threading.Event()
        self.quote_release = threading.Event()
        self.block_quote_call = None
        self.active_quotes = 0
        self.max_active_quotes = 0
        self.quote_lock = threading.Lock()

    def history(self, symbol):
        self.daily_calls.append(symbol)
        start = self.latest_ts - 119 * DAY
        rows = [{"ts": start + i * DAY, "price": 100.0 + i * 0.2 + (i % 5) * 0.03} for i in range(120)]
        return rows, {"source": "synthetic fixture", "warmup_only": True, "missing_intervals": 0}

    def quotes(self):
        with self.quote_lock:
            self.quote_calls += 1
            call_number = self.quote_calls
            self.active_quotes += 1
            self.max_active_quotes = max(self.max_active_quotes, self.active_quotes)
        requested = float(self.clock())
        try:
            if call_number == self.block_quote_call:
                self.quote_entered.set()
                if not self.quote_release.wait(timeout=5):
                    raise RuntimeError("fixture quote gate timed out")
            now = float(self.clock())
            return {symbol: {"price": 100.0 if symbol == "BTC-USD" else 10.0,
                "market_at": requested - 1, "requested_at": requested, "observed_at": now + .1,
                "source": "synthetic fixture"} for symbol in SYMBOLS}
        finally:
            with self.quote_lock:
                self.active_quotes -= 1


class ForwardCohortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = Clock(dt.datetime(2026, 1, 1, 0, 5, tzinfo=dt.timezone.utc).timestamp())
        self.feed = FakeFeed(self.now, floor_day(self.now()))
        self.service = ForwardCohort(Path(self.temp.name) / "forward.sqlite3",
            fetch_daily=self.feed.history, fetch_quotes=self.feed.quotes,
            clock=self.now, poll_seconds=300)

    def tearDown(self):
        self.service.stop()
        self.temp.cleanup()

    def wait_state(self, expected, timeout=2):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            state = self.service.state()
            if state["state"] == expected:
                return state
            time.sleep(.01)
        self.fail(f"forward cohort did not reach {expected!r}; got {self.service.state()['state']!r}")

    def start_and_wait(self):
        self.service.control("start", confirm="START", duration_minutes=259200, expiry_policy="CLOSE")
        return self.wait_state("running")

    def test_not_started_get_has_no_network_call_or_performance_claim(self):
        state = self.service.state()
        self.assertEqual(state["state"], "not_started")
        self.assertFalse(state["is_performance_observation"])
        self.assertEqual(state["forward_days"], 0)
        self.assertEqual(state["accounts"], [])
        self.assertEqual(self.feed.daily_calls, [])
        self.assertEqual(self.feed.quote_calls, 0)

    def test_totals_are_uncapped_while_state_lists_are_recent_windows(self):
        cid = self.service.state()["cohort_id"]
        now = self.now()
        with self.service.db() as db:
            for i in range(110):
                db.execute("INSERT INTO fills(cohort_id,symbol,strategy,signal_ts,request_at,receipt_at,market_at,side,qty,raw_price,fill_price,fee,slippage,realized_pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid,"BTC-USD","passive",int(now)+i,now+i,now+i,now+i,"BUY",.1,100,100,0,0,0,"fixture"))
            for i in range(61):
                db.execute("INSERT INTO decisions(cohort_id,symbol,strategy,signal_ts,created_at,due_at,expires_at,target_weight,status,reason,source_observation_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (cid,"BTC-USD","passive",int(now)+i,now+i,now+i,now+i+900,.5,"skipped","fixture",int(now)+i))
        state = self.service.state()
        self.assertEqual(state["totals"]["fills"], 110)
        self.assertEqual(state["totals"]["recorded_decisions"], 61)
        self.assertEqual(len(state["fills"]), 100)
        self.assertEqual(len(state["decisions"]), 60)

    def test_start_requires_explicit_bounded_duration_and_expiry_policy(self):
        invalid = [
            ({}, "explicit confirmation"),
            ({"confirm": "START", "duration_minutes": 0, "expiry_policy": "CLOSE"}, "integer"),
            ({"confirm": "START", "duration_minutes": 259201, "expiry_policy": "CLOSE"}, "integer"),
            ({"confirm": "START", "duration_minutes": True, "expiry_policy": "CLOSE"}, "integer"),
            ({"confirm": "START", "duration_minutes": 15.5, "expiry_policy": "CLOSE"}, "integer"),
            ({"confirm": "START", "duration_minutes": 60, "expiry_policy": "sell_everything"}, "Choose CLOSE or EXIT_ONLY"),
        ]
        for params, message in invalid:
            with self.subTest(params=params), self.assertRaisesRegex(ValueError, message):
                self.service.control("start", **params)
        self.assertEqual(self.service.state()["session_status"], "not_started")
        self.assertEqual(self.feed.daily_calls, [])
        self.assertEqual(self.feed.quote_calls, 0)
        self.service.control("start", confirm="START", duration_minutes=60, expiry_policy="EXIT_ONLY")
        state = self.service.state()
        self.assertEqual(state["session_duration_minutes"], 60)
        self.assertEqual(state["expiry_policy"], "EXIT_ONLY")
        with self.service.db() as db:
            accepted = db.execute("SELECT session_started_at,session_deadline_at FROM cohorts WHERE id=?", (state["cohort_id"],)).fetchone()
        self.assertAlmostEqual(accepted["session_deadline_at"] - accepted["session_started_at"], 60 * 60)
        self.assertTrue(state["session_id"])

    def test_pause_and_resume_keep_original_deadline_and_stop_is_terminal(self):
        state = self.start_and_wait()
        cid = state["cohort_id"]
        deadline = state["session_deadline_epoch"]
        session_id = state["session_id"]
        self.now.set(self.now() + 30)
        self.service.control("pause")
        self.assertEqual(self.service.state()["session_status"], "paused")
        worker = self.service.worker
        if worker:
            worker.join(timeout=2)
        until = time.monotonic() + 2
        while self.service.worker_lock.locked() and time.monotonic() < until:
            time.sleep(.01)
        self.now.set(self.now() + 60)
        resumed = self.service.control("resume")
        self.assertEqual(resumed["session_id"], session_id)
        self.assertEqual(resumed["session_deadline_epoch"], deadline)
        self.assertEqual(resumed["session_status"], "active")
        self.wait_state("running")
        self.service.control("stop")
        stopped = self.service.state()
        decided_at = stopped["session_decided_at_utc"] if "session_decided_at_utc" in stopped else None
        with self.service.db() as db:
            before = db.execute("SELECT session_decided_at,session_deadline_at,session_id FROM cohorts WHERE id=?", (cid,)).fetchone()
        self.service.control("stop")
        with self.service.db() as db:
            after = db.execute("SELECT session_decided_at,session_deadline_at,session_id FROM cohorts WHERE id=?", (cid,)).fetchone()
        self.assertEqual(tuple(before), tuple(after))
        self.assertEqual(after[1], deadline)
        self.assertEqual(after[2], session_id)
        with self.assertRaisesRegex(RuntimeError, "Start is available only once"):
            self.service.control("start", confirm="START", duration_minutes=60, expiry_policy="CLOSE")
        self.assertTrue(stopped["entry_disabled"])

    def test_deadline_close_rejects_same_decision_quote_then_closes_on_fresh_pair(self):
        now = self.now()
        cid = self._fixture_session(now, duration_minutes=1, policy="CLOSE", with_position=True)
        self.service.worker_lock.acquire()
        try:
            deadline = now + 60
            self.now.set(deadline - 1)
            self.service._watchdog_once()
            self.assertEqual(self.service.state()["session_status"], "active")
            self.now.set(deadline)
            self.service._watchdog_once()
            state = self.service.state()
            self.assertEqual(state["session_status"], "expired_pending_close")
            self.assertTrue(state["entry_disabled"])
            with self.service.db() as db:
                account = db.execute("SELECT qty,exit_requested,exit_decided_at FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'", (cid,)).fetchone()
                self.assertEqual(account["exit_requested"], 1)
                self.assertEqual(account["exit_decided_at"], deadline)
                self.assertGreater(account["qty"], 0)
            stale = self._quote_batch(deadline + .5, request=deadline, receipt=deadline + .5, market=deadline + .2,
                btc_price=1000, eth_price=10)
            self.service._process_quote_batch(cid, stale, deadline + .5)
            with self.service.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM fills WHERE cohort_id=? AND side='SELL'", (cid,)).fetchone()[0], 0)
            fresh = self._quote_batch(deadline + 3, request=deadline + 2, receipt=deadline + 3, market=deadline + 2.5,
                btc_price=1000, eth_price=10)
            self.service._process_quote_batch(cid, fresh, deadline + 3)
            with self.service.db() as db:
                self.assertEqual(db.execute("SELECT qty FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'", (cid,)).fetchone()[0], 0)
                fill = db.execute("SELECT request_at,receipt_at,market_at FROM fills WHERE cohort_id=? AND side='SELL'", (cid,)).fetchone()
                self.assertGreater(fill[0], deadline); self.assertGreater(fill[1], deadline); self.assertGreater(fill[2], deadline)
            self.assertEqual(self.service.state()["session_status"], "finished")
        finally:
            self.service.worker_lock.release()

    def test_expiry_replaces_stale_completed_exit_time_and_requires_new_fresh_quote(self):
        now=self.now();cid=self._fixture_session(now,duration_minutes=1,policy="CLOSE",with_position=True)
        stale_exit_time=now-500
        with self.service.db() as db:
            db.execute("UPDATE accounts SET exit_requested=0,exit_decided_at=? WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",(stale_exit_time,cid))
        self.service.worker_lock.acquire()
        try:
            deadline=now+60
            self.now.set(deadline)
            self.service._watchdog_once()
            with self.service.db() as db:
                account=db.execute("SELECT exit_requested,exit_decided_at,qty FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",(cid,)).fetchone()
            self.assertEqual(account["exit_requested"],1)
            self.assertEqual(account["exit_decided_at"],deadline)
            generation=self._generation(cid)
            old_quote=self._quote_batch(deadline-1,request=deadline-2,receipt=deadline-1,market=deadline-1.5,btc_price=1000,eth_price=10)
            self.service._process_quote_batch(cid,old_quote,deadline-1,generation=generation)
            with self.service.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM fills WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive' AND side='SELL'",(cid,)).fetchone()[0],0)
                self.assertEqual(db.execute("SELECT exit_decided_at FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",(cid,)).fetchone()[0],deadline)
            fresh=self._quote_batch(deadline+3,request=deadline+2,receipt=deadline+3,market=deadline+2.5,btc_price=1000,eth_price=10)
            self.service._process_quote_batch(cid,fresh,deadline+3,generation=generation)
            with self.service.db() as db:
                account=db.execute("SELECT exit_decided_at,qty FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",(cid,)).fetchone()
                sells=db.execute("SELECT count(*) FROM fills WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive' AND side='SELL'",(cid,)).fetchone()[0]
            self.assertEqual(sells,1)
            self.assertEqual(account["qty"],0)
        finally:
            self.service.worker_lock.release()

    def test_expiry_during_blocked_initial_quote_cannot_resurrect_startup(self):
        self.feed.block_quote_call = 1
        self.service.control("start", confirm="START", duration_minutes=1, expiry_policy="CLOSE")
        self.assertTrue(self.feed.quote_entered.wait(timeout=2))
        before = self.service.state()
        self.now.set(before["session_deadline_epoch"])
        self.service._watchdog_once()
        self.assertEqual(self.service.state()["session_status"], "finished")
        self.feed.quote_release.set()
        if self.service.worker:
            self.service.worker.join(timeout=2)
        state = self.service.state()
        self.assertEqual(state["state"], "finished")
        self.assertTrue(state["entry_disabled"])
        self.assertEqual(self.feed.quote_calls, 1)
        self.assertEqual(state["totals"]["all_forward_fills"], 0)

    def test_inflight_pre_deadline_request_cannot_buy_after_deadline(self):
        now = self.now()
        cid = self._fixture_session(now, duration_minutes=1, policy="CLOSE", with_position=False)
        generation = self._generation(cid)
        signal = int(now)
        with self.service.db() as db:
            db.execute("INSERT INTO decisions(cohort_id,symbol,strategy,signal_ts,created_at,due_at,expires_at,target_weight,status,reason,source_observation_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (cid,"BTC-USD","passive",signal,now,now,now+3600,.5,"pending","fixture",signal))
        self.now.set(now + 60)
        crossing = self._quote_batch(now + 61, request=now + 59, receipt=now + 61, market=now + 60.5)
        self.service._process_quote_batch(cid, crossing, now + 61, generation=generation)
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM fills WHERE cohort_id=? AND side='BUY'", (cid,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT qty FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'", (cid,)).fetchone()[0], 0)
            row=db.execute("SELECT session_status,entry_disabled FROM cohorts WHERE id=?", (cid,)).fetchone()
        self.assertIn(row["session_status"], {"finished", "expired_pending_close"})
        self.assertEqual(row["entry_disabled"], 1)

    def test_exit_only_blocks_increases_but_allows_normal_zero_target_sell(self):
        now = self.now()
        cid = self._fixture_session(now, duration_minutes=1, policy="EXIT_ONLY", with_position=True)
        self.service.worker_lock.acquire()
        try:
            deadline = now + 60
            self.now.set(deadline)
            self.service._watchdog_once()
            self.assertEqual(self.service.state()["session_status"], "expired_exit_only")
            with self.service.db() as db:
                for strategy, target in (("momentum_7d", .5), ("passive", 0.0)):
                    db.execute("INSERT INTO decisions(cohort_id,symbol,strategy,signal_ts,created_at,due_at,expires_at,target_weight,status,reason,source_observation_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (cid,"BTC-USD",strategy,int(deadline),deadline-60,deadline-30,deadline+870,target,"pending","fixture",int(deadline)-86400))
            quotes = self._quote_batch(deadline + 3, request=deadline + 2, receipt=deadline + 3, market=deadline + 2.5,
                btc_price=1000, eth_price=10)
            self.service._process_quote_batch(cid, quotes, deadline + 3)
            with self.service.db() as db:
                buys = db.execute("SELECT count(*) FROM fills WHERE cohort_id=? AND symbol='BTC-USD' AND side='BUY'", (cid,)).fetchone()[0]
                sells = db.execute("SELECT count(*) FROM fills WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive' AND side='SELL'", (cid,)).fetchone()[0]
                blocked = db.execute("SELECT status,reason FROM decisions WHERE cohort_id=? AND strategy='momentum_7d'", (cid,)).fetchone()
            self.assertEqual(buys, 0)
            self.assertEqual(sells, 1)
            self.assertEqual(blocked["status"], "no_trade")
            self.assertIn("Exit-only", blocked["reason"])
        finally:
            self.service.worker_lock.release()

    def test_exit_only_expiry_keeps_preexisting_reduction_and_cancels_buy(self):
        now = self.now()
        cid = self._fixture_session(now, duration_minutes=1, policy="EXIT_ONLY", with_position=True)
        self.service.worker_lock.acquire()
        try:
            with self.service.db() as db:
                for strategy, target in (("passive", 0.0), ("momentum_7d", 0.5)):
                    db.execute("INSERT INTO decisions(cohort_id,symbol,strategy,signal_ts,created_at,due_at,expires_at,target_weight,status,reason,source_observation_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (cid,"BTC-USD",strategy,int(now)-DAY,int(now)-900,int(now)-300,int(now)+900,target,"pending","pre-expiry fixture",int(now)-DAY))
            deadline = now + 60
            self.now.set(deadline)
            self.service._watchdog_once()
            state = self.service.state()
            self.assertEqual(state["session_status"], "expired_exit_only")
            generation = self._generation(cid)
            with self.service.db() as db:
                statuses = {r["strategy"]:r["status"] for r in db.execute("SELECT strategy,status FROM decisions WHERE cohort_id=?", (cid,))}
            self.assertEqual(statuses["passive"], "pending")
            self.assertEqual(statuses["momentum_7d"], "cancelled")
            quotes = self._quote_batch(deadline+3,request=deadline+2,receipt=deadline+3,market=deadline+2.5,
                btc_price=1000,eth_price=10)
            self.service._process_quote_batch(cid,quotes,deadline+3,generation=generation)
            with self.service.db() as db:
                sold = db.execute("SELECT count(*) FROM fills WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive' AND side='SELL'",(cid,)).fetchone()[0]
                remaining = db.execute("SELECT qty FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",(cid,)).fetchone()[0]
            self.assertEqual(sold,1)
            self.assertEqual(remaining,0)
        finally:
            self.service.worker_lock.release()

    def test_paused_exit_only_session_activates_management_at_deadline(self):
        now=self.now();cid=self._fixture_session(now,duration_minutes=1,policy="EXIT_ONLY",with_position=True)
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET state='paused',session_status='paused' WHERE id=?",(cid,))
        self.service.worker_lock.acquire()
        try:
            self.now.set(now+60)
            self.service._watchdog_once()
            state=self.service.state()
            self.assertEqual(state["session_status"],"expired_exit_only")
            self.assertEqual(state["state"],"running")
            self.assertTrue(state["entry_disabled"])
            self.assertTrue(any(a["qty"]>0 for a in state["accounts"]))
        finally:
            self.service.worker_lock.release()

    def test_idle_watchdog_does_not_persist_clock_high_water_each_tick(self):
        cid=self.service.state()["cohort_id"]
        with self.service.db() as db:
            initial=db.execute("SELECT clock_high_water FROM cohorts WHERE id=?",(cid,)).fetchone()[0]
        self.now.set(self.now()+120)
        self.service._watchdog_once()
        with self.service.db() as db:
            current=db.execute("SELECT clock_high_water FROM cohorts WHERE id=?",(cid,)).fetchone()[0]
        self.assertEqual(current,initial)

    def test_active_clock_high_water_is_bounded_to_thirty_second_heartbeat(self):
        now=self.now();cid=self._fixture_session(now,duration_minutes=60,policy="CLOSE",with_position=False)
        self.now.set(now+29);self.service._watchdog_once()
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT clock_high_water FROM cohorts WHERE id=?",(cid,)).fetchone()[0],now)
        self.now.set(now+30);self.service._watchdog_once()
        with self.service.db() as db:
            self.assertGreaterEqual(db.execute("SELECT clock_high_water FROM cohorts WHERE id=?",(cid,)).fetchone()[0],now+30)

    def test_reset_api_rejects_cohort_with_open_position(self):
        now=self.now();self._fixture_session(now,duration_minutes=60,policy="CLOSE",with_position=True)
        with self.assertRaisesRegex(RuntimeError,"Close all paper positions"):
            self.service.control("reset",confirm="RESET")

    def test_restart_after_deadline_expires_original_session_without_extension(self):
        state=self.service.control("start",confirm="START",duration_minutes=1,expiry_policy="CLOSE")
        self.wait_state("running")
        deadline=state["session_deadline_epoch"]
        self.service.stop()
        self.now.set(deadline+1)
        self.service=ForwardCohort(Path(self.temp.name)/"forward.sqlite3",
            fetch_daily=self.feed.history,fetch_quotes=self.feed.quotes,clock=self.now,poll_seconds=300)
        resumed=self.service.state()
        self.assertEqual(resumed["session_status"],"finished")
        self.assertTrue(resumed["entry_disabled"])
        self.assertEqual(resumed["session_deadline_epoch"],deadline)
        with self.assertRaisesRegex(RuntimeError,"terminal"):
            self.service.control("resume")

    def test_restart_clock_suspect_close_uses_only_fresh_post_highwater_quotes(self):
        now=self.now();cid=self._fixture_session(now,duration_minutes=60,policy="CLOSE",with_position=True)
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET state='exiting',session_status='clock_suspect',entry_disabled=1,session_decided_at=?,terminal_reason='clock fixture' WHERE id=?",(now,cid))
            db.execute("UPDATE accounts SET exit_requested=1,exit_decided_at=? WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",(now,cid))
        self.service.stop()
        self.feed.quote_calls=0
        self.service=ForwardCohort(Path(self.temp.name)/"forward.sqlite3",
            fetch_daily=self.feed.history,fetch_quotes=self.feed.quotes,clock=self.now,poll_seconds=300)
        limit=time.monotonic()+2
        while self.feed.quote_calls<1 and time.monotonic()<limit:time.sleep(.01)
        self.assertGreaterEqual(self.feed.quote_calls,1)
        with self.service.db() as db:
            account=db.execute("SELECT qty,exit_decided_at FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",(cid,)).fetchone()
            sells=db.execute("SELECT count(*) FROM fills WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive' AND side='SELL'",(cid,)).fetchone()[0]
        self.assertEqual(account["qty"],1)
        self.assertEqual(account["exit_decided_at"],now)
        self.assertEqual(sells,0)
        self.now.set(now+10);self.service.wake.set()
        limit=time.monotonic()+2
        while self.feed.quote_calls<2 and time.monotonic()<limit:time.sleep(.01)
        limit=time.monotonic()+2
        while time.monotonic()<limit and self.service.state()["state"]!="finished":time.sleep(.01)
        with self.service.db() as db:
            account=db.execute("SELECT qty,exit_decided_at FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",(cid,)).fetchone()
            fill=db.execute("SELECT request_at,receipt_at,market_at FROM fills WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive' AND side='SELL'",(cid,)).fetchone()
        self.assertEqual(account["qty"],0)
        self.assertEqual(account["exit_decided_at"],now)
        self.assertIsNotNone(fill)
        self.assertGreater(min(fill[0],fill[1],fill[2]),now)

    def test_legacy_untimed_migration_preserves_position_and_allows_quote_exit(self):
        now=self.now();cid=self._fixture_session(now,duration_minutes=60,policy="CLOSE",with_position=True)
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET state='paused',session_status='active',session_started_at=NULL,session_deadline_at=NULL,session_duration_minutes=NULL,expiry_policy=NULL,start_at=? WHERE id=?",(now,cid))
            db.execute("INSERT INTO fills(cohort_id,symbol,strategy,signal_ts,request_at,receipt_at,market_at,side,qty,raw_price,fill_price,fee,slippage,realized_pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid,"BTC-USD","passive",int(now)-1,now-1,now,now-1,"BUY",1,1000,1001,1,1,0,"legacy fixture"))
        self.service.stop()
        self.service=ForwardCohort(Path(self.temp.name)/"forward.sqlite3",
            fetch_daily=self.feed.history,fetch_quotes=self.feed.quotes,clock=self.now,poll_seconds=300)
        state=self.service.state()
        self.assertEqual(state["session_status"],"legacy_untimed")
        self.assertTrue(state["entry_disabled"])
        account=next(a for a in state["accounts"] if a["symbol"]=="BTC-USD" and a["strategy"]=="passive")
        self.assertEqual(account["qty"],1)
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM fills WHERE cohort_id=?",(cid,)).fetchone()[0],1)
        with self.assertRaisesRegex(RuntimeError,"migrated untimed"):
            self.service.control("resume")
        self.service.worker_lock.acquire()
        try:
            after_exit=self.service.control("exit")
            self.assertEqual(after_exit["session_status"],"stopped_pending_close")
            after_account=next(a for a in after_exit["accounts"] if a["symbol"]=="BTC-USD" and a["strategy"]=="passive")
            self.assertEqual(after_account["qty"],1)
        finally:
            self.service.worker_lock.release()

    def test_restart_of_exit_only_keeps_pending_reduction_and_cancels_pending_entry(self):
        now=self.now();cid=self._fixture_session(now,duration_minutes=60,policy="EXIT_ONLY",with_position=True)
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET state='running',session_status='expired_exit_only',entry_disabled=1 WHERE id=?",(cid,))
            for strategy,target in (("passive",0.0),("momentum_7d",0.5)):
                db.execute("INSERT INTO decisions(cohort_id,symbol,strategy,signal_ts,created_at,due_at,expires_at,target_weight,status,reason,source_observation_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (cid,"BTC-USD",strategy,int(now)-1,now-600,now-300,now+600,target,"pending","restart fixture",int(now)-DAY))
        self.service.stop()
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET state='running' WHERE id=?",(cid,))
        self.service=ForwardCohort(Path(self.temp.name)/"forward.sqlite3",
            fetch_daily=self.feed.history,fetch_quotes=self.feed.quotes,clock=self.now,poll_seconds=300)
        with self.service.db() as db:
            statuses={r["strategy"]:r["status"] for r in db.execute("SELECT strategy,status FROM decisions WHERE cohort_id=?",(cid,))}
        self.assertEqual(statuses["passive"],"pending")
        self.assertEqual(statuses["momentum_7d"],"cancelled")

    def test_ordinary_marks_do_not_add_intraday_cap_trim_or_change_active_policy(self):
        now=self.now();cid=self._fixture_session(now,duration_minutes=60,policy="CLOSE",with_position=False)
        with self.service.db() as db:
            db.execute("UPDATE accounts SET cash=4000,qty=60,basis=6000,last_price=100,peak_nlv=10000 WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",(cid,))
        generation=self._generation(cid)
        quote=self._quote_batch(now+11,request=now+10,receipt=now+11,market=now+10.5,btc_price=100,eth_price=10)
        self.service._process_quote_batch(cid,quote,now+11,generation=generation)
        with self.service.db() as db:
            pending=db.execute("SELECT count(*) FROM decisions WHERE cohort_id=? AND status='pending'",(cid,)).fetchone()[0]
            exposure=db.execute("SELECT exposure FROM marks WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive' ORDER BY id DESC LIMIT 1",(cid,)).fetchone()[0]
        self.assertEqual(pending,0)
        self.assertAlmostEqual(exposure,.6)

    def test_paused_deadline_watchdog_expires_without_user_or_browser_poll(self):
        started = self.start_and_wait()
        deadline = started["session_deadline_epoch"]
        self.service.control("pause")
        worker = self.service.worker
        if worker:
            worker.join(timeout=2)
        self.now.set(deadline)
        self.service._watchdog_once()
        state = self.service.state()
        self.assertTrue(state["entry_disabled"])
        self.assertIn(state["session_status"], {"finished", "expired_pending_close"})
        with self.assertRaisesRegex(RuntimeError, "Only a paused cohort can resume|terminal"):
            self.service.control("resume")

    def test_wall_clock_rollback_fails_closed_and_keeps_high_water(self):
        now = self.now()
        cid = self._fixture_session(now, duration_minutes=60, policy="CLOSE", with_position=False)
        self.now.set(now - 10)
        state = self.service.state()
        self.assertEqual(state["session_status"], "clock_suspect")
        self.assertTrue(state["entry_disabled"])
        with self.service.db() as db:
            row = db.execute("SELECT clock_high_water,session_decided_at FROM cohorts WHERE id=?", (cid,)).fetchone()
        self.assertGreaterEqual(row["clock_high_water"], now)
        self.assertEqual(row["session_decided_at"], row["clock_high_water"])
        with self.assertRaisesRegex(RuntimeError, "terminal"):
            self.service.control("resume")

    def test_session_configuration_and_terminal_deadline_are_in_export(self):
        state = self.service.control("start", confirm="START", duration_minutes=2880, expiry_policy="EXIT_ONLY")
        rows = list(csv.reader(io.StringIO(self.service.export_csv())))
        self.assertIn("session_deadline_utc", rows[0])
        index = {key: rows[0].index(key) for key in ("session_id", "session_duration_minutes", "expiry_policy", "session_status", "entry_disabled")}
        cohort = next(row for row in rows[1:] if row[0] == "cohort")
        self.assertEqual(cohort[index["session_id"]], state["session_id"])
        self.assertEqual(cohort[index["session_duration_minutes"]], "2880")
        self.assertEqual(cohort[index["expiry_policy"]], "EXIT_ONLY")
        self.assertEqual(cohort[index["session_status"]], "active")
        self.assertEqual(cohort[index["entry_disabled"]], "False")

    def _fixture_session(self, now, *, duration_minutes, policy, with_position):
        with self.service.db() as db:
            row = db.execute("SELECT id FROM cohorts ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
            cid = row["id"]
            db.execute("UPDATE cohorts SET state='running',start_at=?,session_id='fixture-session',session_started_at=?,session_deadline_at=?,session_duration_minutes=?,expiry_policy=?,session_status='active',entry_disabled=0,clock_high_water=?,launch_generation=1 WHERE id=?",
                (now, now, now + duration_minutes*60, duration_minutes, policy, now, cid))
            for symbol in SYMBOLS:
                for strategy in STRATEGIES:
                    db.execute("INSERT OR IGNORE INTO accounts(cohort_id,symbol,strategy,initial,cash,peak_nlv,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (cid,symbol,strategy,10_000.0,10_000.0,10_000.0,now))
            if with_position:
                db.execute("UPDATE accounts SET cash=9000,qty=1,basis=1000,last_price=1000,last_market_at=?,last_receipt_at=?,peak_nlv=10000 WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'",
                    (now,now,cid))
        return cid

    def _generation(self, cid):
        with self.service.db() as db:
            return int(db.execute("SELECT launch_generation FROM cohorts WHERE id=?", (cid,)).fetchone()[0])

    def test_start_saves_warmup_only_baseline_and_does_not_double_poll(self):
        state = self.start_and_wait()
        self.assertFalse(state["is_performance_observation"])
        self.assertEqual(self.feed.daily_calls, ["BTC-USD", "ETH-USD"])
        self.assertEqual(self.feed.quote_calls, 1)
        self.assertEqual(len(state["accounts"]), len(STRATEGIES) * len(SYMBOLS))
        with self.service.db() as db:
            cid = state["cohort_id"]
            warmup = db.execute("SELECT count(*) FROM warmup_prices WHERE cohort_id=? AND warmup_only=1", (cid,)).fetchone()[0]
            daily = db.execute("SELECT count(*) FROM daily_observations WHERE cohort_id=?", (cid,)).fetchone()[0]
        self.assertEqual(warmup, 240)
        self.assertEqual(daily, 0)
        time.sleep(.05)
        self.assertEqual(self.feed.quote_calls, 1)

    def test_atomic_quote_batch_requires_same_request_and_receipt_times(self):
        now = 1_800_000_000.0
        base = {"price": 100.0, "market_at": now - 1, "requested_at": now - 1,
            "observed_at": now, "source": "fixture"}
        quotes = {symbol: dict(base) for symbol in SYMBOLS}
        self.assertEqual(set(validate_batch(quotes, now)), set(SYMBOLS))
        for field in ("requested_at", "observed_at"):
            mismatched = {symbol: dict(base) for symbol in SYMBOLS}
            mismatched[SYMBOLS[1]][field] -= .1
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "atomic request/receipt"):
                validate_batch(mismatched, now)

    def test_start_resume_worker_race_restores_paused_and_never_overlaps(self):
        self.start_and_wait()
        self.feed.block_quote_call = 2
        self.service.wake.set()
        self.assertTrue(self.feed.quote_entered.wait(timeout=2))
        self.service.control("pause")
        self.service.worker_acquire_timeout = .01
        with self.assertRaisesRegex(RuntimeError, "state restored to paused"):
            self.service.control("resume")
        self.assertEqual(self.service.state()["state"], "paused")
        self.feed.quote_release.set()
        self.service.worker.join(timeout=2)
        until = time.monotonic() + 2
        while self.service.worker_lock.locked() and time.monotonic() < until:
            time.sleep(.01)
        self.assertFalse(self.service.worker_lock.locked())
        self.assertEqual(self.feed.max_active_quotes, 1)
        self.assertEqual(self.service.state()["state"], "paused")

    def test_delayed_daily_point_at_or_before_resume_is_warmup_only(self):
        first_day = floor_day(self.now())
        self.start_and_wait()
        self.service.control("pause")
        self.service.worker.join(timeout=2)
        next_day = first_day + DAY
        self.now.set(next_day + 5 * 60)
        self.feed.latest_ts = first_day
        self.service.control("resume")
        self.wait_state("running")
        self.now.set(next_day + 15 * 60)
        self.feed.latest_ts = next_day
        self.service._maybe_refresh_history(self.service.state()["cohort_id"], self.now())
        state = self.service.state()
        self.assertEqual(state["forward_days"], 0)
        self.assertFalse(state["is_performance_observation"])
        self.assertEqual(state["last_daily_observation_utc"]["BTC-USD"], "2026-01-02T00:00:00Z")
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM daily_observations").fetchone()[0], 0)
            skipped = db.execute("SELECT count(*) FROM decisions WHERE status='skipped' AND reason LIKE '%predates%'").fetchone()[0]
        self.assertEqual(skipped, len(STRATEGIES) * len(SYMBOLS))

    def test_history_retry_at_five_minute_bound_finds_delayed_current_point(self):
        first_day = floor_day(self.now())
        self.start_and_wait()
        cohort_id = self.service.state()["cohort_id"]
        next_day = first_day + DAY
        self.now.set(next_day + 10 * 60)
        self.feed.latest_ts = first_day
        self.service._maybe_refresh_history(cohort_id, self.now())
        self.assertEqual(self.service.state()["forward_days"], 0)
        self.now.set(next_day + 15 * 60)
        self.feed.latest_ts = next_day
        self.service._maybe_refresh_history(cohort_id, self.now())
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM daily_observations WHERE ts=?", (next_day,)).fetchone()[0], 2)
            pending = db.execute("SELECT count(*) FROM decisions WHERE signal_ts=? AND status='pending'", (next_day,)).fetchone()[0]
        self.assertEqual(pending, len(STRATEGIES) * len(SYMBOLS))
        self.assertEqual(self.service.state()["forward_days"], 1)
        self.assertTrue(self.service.state()["is_performance_observation"])

    def test_history_pair_retry_budget_stops_after_three_and_at_0400(self):
        first_day = floor_day(self.now())
        self.start_and_wait()
        cohort_id = self.service.state()["cohort_id"]
        next_day = first_day + DAY
        self.feed.latest_ts = first_day
        for minute in (10, 15, 20):
            self.now.set(next_day + minute * 60)
            self.service._maybe_refresh_history(cohort_id, self.now())
        calls_after_budget = len(self.feed.daily_calls)
        self.assertEqual(calls_after_budget, 2 + 3 * len(SYMBOLS))
        self.now.set(next_day + 25 * 60)
        self.service._maybe_refresh_history(cohort_id, self.now())
        self.assertEqual(len(self.feed.daily_calls), calls_after_budget)
        self.now.set(next_day + 4 * 3600)
        self.service._maybe_refresh_history(cohort_id, self.now())
        self.assertEqual(len(self.feed.daily_calls), calls_after_budget)
        self.assertEqual(self.service.state()["forward_days"], 0)

    def test_pending_entry_needs_fresh_quote_after_due_and_expires_at_0415(self):
        first_day = floor_day(self.now())
        state = self.start_and_wait()
        cohort_id = state["cohort_id"]
        signal_day = first_day + DAY
        self.now.set(signal_day + 15 * 60)
        self.feed.latest_ts = signal_day
        self.service._maybe_refresh_history(cohort_id, self.now())
        due = signal_day + 4 * 3600
        before = self._quote_batch(due, request=due, receipt=due + .2, market=due + .1)
        self.service._process_quote_batch(cohort_id, before, due + .2)
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM fills WHERE signal_ts=?", (signal_day,)).fetchone()[0], 0)
            self.assertGreater(db.execute("SELECT count(*) FROM decisions WHERE signal_ts=? AND status='pending'", (signal_day,)).fetchone()[0], 0)
        fresh = self._quote_batch(due + 1, request=due + 1, receipt=due + 2, market=due + 1.5)
        self.service._process_quote_batch(cohort_id, fresh, due + 2)
        with self.service.db() as db:
            buys = db.execute("SELECT count(*) FROM fills WHERE signal_ts=? AND side='BUY'", (signal_day,)).fetchone()[0]
        self.assertGreater(buys, 0)

        later_day = signal_day + DAY
        self.now.set(later_day + 15 * 60)
        self.feed.latest_ts = later_day
        self.service._maybe_refresh_history(cohort_id, self.now())
        expiry = later_day + 4 * 3600 + 900
        late = self._quote_batch(expiry, request=expiry, receipt=expiry + .1, market=expiry)
        self.service._process_quote_batch(cohort_id, late, expiry + .1)
        with self.service.db() as db:
            skipped = db.execute("SELECT count(*) FROM decisions WHERE signal_ts=? AND status='skipped' AND reason LIKE '%expiry%'", (later_day,)).fetchone()[0]
            self.assertEqual(db.execute("SELECT count(*) FROM fills WHERE signal_ts=?", (later_day,)).fetchone()[0], 0)
        self.assertEqual(skipped, len(STRATEGIES) * len(SYMBOLS))

    def _quote_batch(self, now, *, request, receipt, market, btc_price=102.0, eth_price=10.2):
        return {
            "BTC-USD": {"price": btc_price, "market_at": market, "requested_at": request, "observed_at": receipt, "source": "fixture"},
            "ETH-USD": {"price": eth_price, "market_at": market, "requested_at": request, "observed_at": receipt, "source": "fixture"},
        }

    def test_pause_holds_open_positions_and_exit_uses_later_fresh_quote_then_pauses(self):
        now = self.now()
        cid = self._make_running_with_position(now)
        self.service.control("pause")
        with self.service.db() as db:
            qty = db.execute("SELECT qty FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'", (cid,)).fetchone()[0]
        self.assertEqual(qty, 1.0)
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET state='running' WHERE id=?", (cid,))
        entered, release = threading.Event(), threading.Event()
        original_fetch = self.service.fetch_quotes
        def blocked_quotes():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("close-worker fixture quote gate timed out")
            return original_fetch()
        self.service.fetch_quotes = blocked_quotes
        try:
            self.service.control("exit")
            self.assertTrue(entered.wait(1), "serialized close worker did not enter its quote request")
            self.assertEqual(self.service.state()["state"], "exiting")
            decision = now
            same_quote = self._quote_batch(decision, request=decision, receipt=decision + .1, market=decision,
                btc_price=1000.0, eth_price=10.0)
            self.service._process_quote_batch(cid, same_quote, decision + .1)
            with self.service.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM fills WHERE side='SELL'").fetchone()[0], 0)
            fresh = self._quote_batch(decision + 2, request=decision + 2, receipt=decision + 3, market=decision + 2.5,
                btc_price=1000.0, eth_price=10.0)
            self.service._process_quote_batch(cid, fresh, decision + 3)
            self.assertEqual(self.service.state()["state"], "finished")
        finally:
            release.set()
            worker = self.service.worker
            if worker and worker.is_alive():
                worker.join(timeout=2)
        self.assertEqual(self.service.state()["state"], "finished", "a stale blocked worker must not revive a closed cohort")
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT qty FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'", (cid,)).fetchone()[0], 0)
            row = db.execute("SELECT reason FROM fills WHERE side='SELL'").fetchone()
        self.assertEqual(row[0], "manual_close")

    def test_paused_history_error_can_exit_by_fresh_quote_without_history(self):
        now = self.now()
        cid = self._make_running_with_position(now)
        self.service.control("pause")
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET state='paused' WHERE id=?", (cid,))
            db.execute("UPDATE accounts SET exit_requested=1,exit_decided_at=? WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'", (now, cid))
        self.feed.latest_ts = now - DAY * 10  # intentionally stale; quote-only exit must not consult it
        self.feed.quote_calls = 0
        self.feed.quote_release.set()
        def later_quotes():
            self.now.set(self.now() + 2)
            return self.feed.quotes()
        self.service.fetch_quotes = later_quotes
        self.service.control("exit")
        until = time.monotonic() + 2
        while time.monotonic() < until:
            with self.service.db() as db:
                qty = db.execute("SELECT qty FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'", (cid,)).fetchone()[0]
            if qty == 0:
                break
            time.sleep(.01)
        self.assertEqual(qty, 0)
        self.assertEqual(self.service.state()["state"], "finished")

    def test_paused_exit_with_bad_quotes_keeps_position_pending_without_fill(self):
        now = self.now()
        cid = self._make_running_with_position(now)
        self.service.control("pause")
        with self.service.db() as db:
            db.execute("UPDATE cohorts SET state='paused' WHERE id=?", (cid,))
            db.execute("UPDATE accounts SET exit_requested=1,exit_decided_at=? WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'", (now, cid))
        self.service.fetch_quotes = lambda: (_ for _ in ()).throw(RuntimeError("quote down"))
        self.service.control("exit")
        until = time.monotonic() + 2
        while self.service.worker and self.service.worker.is_alive() and time.monotonic() < until:
            time.sleep(.01)
        with self.service.db() as db:
            account = db.execute("SELECT qty,exit_requested FROM accounts WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'", (cid,)).fetchone()
            sells = db.execute("SELECT count(*) FROM fills WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive' AND side='SELL'", (cid,)).fetchone()[0]
        self.assertGreater(account["qty"], 0)
        self.assertEqual(account["exit_requested"], 1)
        self.assertEqual(sells, 0)

    def test_config_drift_requires_new_cohort_and_reset_preserves_archived_csv(self):
        with self.service.db() as db:
            row = db.execute("SELECT id,config_json FROM cohorts ORDER BY created_at DESC LIMIT 1").fetchone()
            changed = json.loads(row["config_json"])
            changed["annual_target_volatility"] = .11
            db.execute("UPDATE cohorts SET state='paused',session_status='paused',session_id='fixture-session',session_started_at=?,session_deadline_at=?,session_duration_minutes=60,expiry_policy='CLOSE',config_json=? WHERE id=?",
                (self.now()-100,self.now()+3600,json.dumps(changed),row["id"]))
        with self.assertRaisesRegex(RuntimeError, "code/config changed"):
            self.service.control("resume")
        with self.assertRaisesRegex(ValueError, "explicit confirmation"):
            self.service.control("reset", confirm="no")
        self.service.control("reset", confirm="RESET")
        rows = list(csv.reader(io.StringIO(self.service.export_csv())))
        self.assertGreaterEqual(len(rows), 3)
        self.assertTrue(all(len(row) == len(rows[0]) for row in rows))
        self.assertEqual(sum(row[0] == "cohort" for row in rows), 2)
        self.assertIn('"implementation_sha256"', "\n".join(row[16] for row in rows if row[0] == "cohort"))

    def _make_running_with_position(self, now):
        with self.service.db() as db:
            cohort = db.execute("SELECT id FROM cohorts ORDER BY created_at DESC LIMIT 1").fetchone()
            cid = cohort["id"]
            db.execute("UPDATE cohorts SET state='running',start_at=?,config_hash=?,session_id='fixture-session',session_started_at=?,session_deadline_at=?,session_duration_minutes=60,expiry_policy='CLOSE',session_status='active',entry_disabled=0,clock_high_water=? WHERE id=?",
                (now - 100, "fixture-config", now - 100, now + 3600, now, cid))
            for symbol in SYMBOLS:
                price = 1000.0 if symbol == "BTC-USD" else 10.0
                for strategy in STRATEGIES:
                    db.execute("INSERT OR IGNORE INTO accounts(cohort_id,symbol,strategy,initial,cash,qty,basis,peak_nlv,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (cid, symbol, strategy, 10_000.0, 10_000.0, 0.0, 0.0, 10_000.0, now))
                    db.execute("UPDATE accounts SET last_price=?,last_market_at=?,last_receipt_at=? WHERE cohort_id=? AND symbol=? AND strategy=?",
                        (price, now - 1, now - 1, cid, symbol, strategy))
            db.execute("UPDATE accounts SET cash=9000,qty=1,basis=1000,last_price=1000,peak_nlv=10000 WHERE cohort_id=? AND symbol='BTC-USD' AND strategy='passive'", (cid,))
        return cid


if __name__ == "__main__":
    unittest.main()
