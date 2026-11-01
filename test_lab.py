"""Meaningful invariants and integration tests. No network or packages needed."""
from __future__ import annotations

import copy
import http.client
import json
import math
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import engine
import market
import research as r
from server import handler_for
from research_jobs import ResearchJobs


class ResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.points = market.synthetic_prices("ETH-USD")
        cls.result = r.analyze(cls.points)

    def test_features_are_prefix_invariant(self):
        prefix = self.points[:250]
        self.assertEqual(r.feature_rows(prefix), [row for row in r.feature_rows(self.points) if row["ts"] <= prefix[-1]["ts"]])
        changed = copy.deepcopy(self.points)
        for point in changed[250:]:
            point["price"] *= 4
        self.assertEqual(r.feature_rows(prefix), r.feature_rows(changed)[:len(r.feature_rows(prefix))])

    def test_raw_heldout_mutation_cannot_change_fit_or_threshold(self):
        boundary = self.result["splits"]["holdout"]["start"]
        changed = copy.deepcopy(self.points)
        for i, point in enumerate(changed):
            if point["ts"] >= boundary:
                point["price"] *= 3 + .1 * (i % 7)
        mutated = r.analyze(changed)
        self.assertEqual(self.result["model"], mutated["model"])
        self.assertEqual(self.result["threshold"], mutated["threshold"])
        self.assertEqual(self.result["fit_id"], mutated["fit_id"])
        self.assertNotEqual(self.result["id"], mutated["id"])

    def test_labels_are_purged_strictly_before_next_split(self):
        train, val, test = r.split_rows(r.labeled_rows(self.points))
        self.assertLess(max(row["target_end"] for row in train), val[0]["ts"])
        self.assertLess(max(row["target_end"] for row in val), test[0]["ts"])
        self.assertEqual(self.result["purged"], 4)
        self.assertEqual(self.result["model"], r.train_model(train))

    def test_validation_mutation_does_not_change_train_scaler(self):
        rows = r.labeled_rows(self.points)
        a = int(len(rows) * .6)
        changed = copy.deepcopy(self.points)
        for p in changed:
            if p["ts"] >= rows[a]["ts"]:
                p["price"] *= 2
        train2, _, _ = r.split_rows(r.labeled_rows(changed))
        self.assertEqual(self.result["model"], r.train_model(train2))

    def test_target_matches_next_sample_entry_and_following_exit(self):
        row = r.labeled_rows(self.points)[10]
        by_ts = {p["ts"]: p["price"] for p in self.points}
        self.assertGreater(row["exec_ts"], row["signal_at"])
        self.assertGreater(row["target_end"], row["exec_ts"])
        self.assertEqual(row["label"], int(by_ts[row["target_end"]] > by_ts[row["exec_ts"]]))
        self.assertEqual(row["exec_open"], by_ts[row["exec_ts"]])

    def test_cash_accounting_inclusive_allocation_and_costs(self):
        entry = r.buy(10_000, 100)
        self.assertAlmostEqual(entry["cash"], 5000)
        self.assertAlmostEqual(entry["qty"] * entry["price"] + entry["fee"], 5000)
        exit_ = r.sell(entry["cash"], entry["qty"], 100, entry["cost_basis"])
        self.assertAlmostEqual(exit_["cash"] - 10_000, exit_["pnl"])
        self.assertAlmostEqual(10_000 - exit_["cash"], entry["fee"] + exit_["fee"] + entry["slippage"] + exit_["slippage"])
        self.assertGreater(exit_["cash"], 0)

    def test_baselines_use_identical_dates_allocation_and_costs(self):
        curves = self.result["curve"]
        self.assertEqual(set(curves[0]), {"ts", "model", "momentum", "buy_hold"})
        self.assertEqual([curves[0][k] for k in ("model", "momentum", "buy_hold")], [r.INITIAL] * 3)
        _, _, rows = r.split_rows(r.labeled_rows(self.points))
        entry = r.buy(r.INITIAL, rows[0]["exec_open"])
        self.assertGreater(self.result["metrics"]["buy_hold"]["fees"], entry["fee"])
        self.assertEqual(curves[-1]["ts"], rows[-1]["target_end"])

    def test_drawdown_halts_further_entries(self):
        rows = [{"ts": i*3600, "signal_at": i*3600, "exec_ts": (i+1)*3600,
                 "target_end": (i+2)*3600, "exec_open":100, "exec_close":60} for i in range(3)]
        result = r.backtest(rows, [True]*3, collect=True)
        self.assertTrue(result["halted"])
        self.assertEqual(result["trades"], 1)
        self.assertGreater(result["max_drawdown"], r.HALT)

    def test_gaps_invalidate_lookbacks_and_targets(self):
        points = self.points[:100] + self.points[103:]
        rows = r.labeled_rows(points)
        self.assertFalse(any(self.points[98]["ts"] <= row["ts"] <= self.points[114]["ts"] for row in rows))

    def test_small_sample_is_not_ready(self):
        self.assertFalse(r.analyze(self.points[:60])["ready"])

    def test_both_symbols_seeded_and_zero_trades_are_honest(self):
        btc = r.analyze(market.synthetic_prices("BTC-USD"))
        self.assertTrue(btc["ready"])
        self.assertEqual(btc["metrics"]["model"]["trades"], 0)
        self.assertEqual(btc["replay_ledger"], [])
        self.assertTrue(self.result["replay_ledger"])
        self.assertEqual(market.synthetic_prices("ETH-USD"), self.points)


class FeedTests(unittest.TestCase):
    def history(self):
        self.now = 1_800_000_000.0
        return {"prices": [[(self.now - (4-i)*3600)*1000, 100+i] for i in range(5)]}

    def test_partial_future_and_malformed_history(self):
        body = self.history()
        body["prices"].insert(1,[self.now*1000-3.5*3600*1000,float('nan')])
        points, info = market.validate_prices(body, now=self.now)
        self.assertEqual(len(points),4)
        self.assertEqual(info["rejected"],1)
        self.assertLess(points[-1]["ts"], self.now)
        future = self.history()
        future["prices"][-2][0] = (self.now+100)*1000
        future["prices"][-1][0] = (self.now+200)*1000
        points, info = market.validate_prices(future, now=self.now)
        self.assertEqual(info["outside_range"],1)
        self.assertTrue(all(p["ts"]<=self.now for p in points))

    def test_unsorted_or_bad_shape_fails_closed(self):
        body = self.history(); body["prices"].reverse()
        with self.assertRaises(ValueError): market.validate_prices(body, now=self.now)
        for body in ({}, [], {"prices":[[1,2],None]}):
            with self.assertRaises(ValueError): market.validate_prices(body, now=1_800_000_000)

    def test_conflicting_duplicate_history_rejected(self):
        body = self.history(); body["prices"].insert(1,[body["prices"][0][0],999])
        with self.assertRaises(RuntimeError): market.validate_prices(body,now=self.now)

    def test_bad_quotes_never_validate(self):
        now = 1_800_000_000
        for quote in ({"price":1,"market_at":now-121,"observed_at":now},
                      {"price":1,"market_at":now+20,"observed_at":now},
                      {"price":float('nan'),"market_at":now,"observed_at":now},
                      {"price":True,"market_at":now,"observed_at":now}, {}):
            with self.assertRaises(ValueError): market.validate_quote(quote,now)

    def test_endpoint_allowlist_and_ssl_verification(self):
        for url in ("http://api.coingecko.com/api/v3/ping","https://evil.example/ping","https://api.coingecko.com:444/ping"):
            with self.assertRaises(ValueError): market.public_json(url)
        self.assertEqual(market.tls_context().verify_mode,2)
        self.assertTrue(market.tls_context().check_hostname)

    def test_http_errors_invalid_json_and_bounds(self):
        class Response:
            def __init__(self,data): self.data=data
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def read(self,n): return self.data[:n]
        class Opener:
            def open(self,*args,**kwargs): return Response(self.data)
        opener=Opener()
        for data in (b'not-json',b'x'*(market.MAX_BYTES+1)):
            opener.data=data
            with patch('market.urllib.request.build_opener',return_value=opener):
                with self.assertRaises(RuntimeError): market.public_json('https://api.coingecko.com/api/v3/ping')
        with patch('market.urllib.request.OpenerDirector.open',side_effect=urllib.error.HTTPError('x',429,'rate',{},None)) as call, patch('market.time.sleep'):
            with self.assertRaises(RuntimeError): market.public_json('https://api.coingecko.com/api/v3/ping')
            self.assertEqual(call.call_count,2)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'lab.sqlite3'
        self.base=int(time.time())//3600*3600
        self.lab=engine.Lab(self.path)
        fixture=market.synthetic_prices('ETH-USD',200)
        offset=self.base-3600-fixture[-1]['ts']
        self.points=[{'ts':p['ts']+offset,'price':p['price']} for p in fixture]
        self.lab.put_prices('live','ETH-USD',self.points)
        self.lab.ensure_account('live','ETH-USD',self.points[-1]['ts'])

    def tearDown(self): self.tmp.cleanup()

    def running(self):
        model={'means':[0]*5,'scales':[1]*5,'weights':[10,0,0,0,0,0]}
        with self.lab.db() as db:
            db.execute("UPDATE paper_v3 SET status='running',model_json=?,experiment_id='test-frozen',updated_at=? WHERE namespace=? AND symbol='ETH-USD'",(json.dumps({'model':model,'threshold':.5}),self.base-10,engine.LIVE))

    def signal(self):
        self.running()
        points=self.points+[{'ts':self.base,'price':self.points[-1]['price']*1.01}]
        self.lab.process_signal('ETH-USD',points,now=self.base+2)
        return points

    def quote(self,at,price=100): return {'price':price,'market_at':at-1,'observed_at':at,'requested_at':at-.5}

    def buy_position(self):
        self.signal(); at=self.base+3600+2
        self.lab.apply_quote('ETH-USD',self.quote(at),now=at)
        return at

    def test_new_account_and_startup_never_execute_history(self):
        with patch('market.fetch_history',side_effect=AssertionError('retired public history must not be requested')), patch('research.analyze',side_effect=AssertionError('public data must not be fitted')):
            self.lab.refresh('ETH-USD',force=True)
        self.assertEqual(self.lab.account('live','ETH-USD')['status'],'paused')
        self.assertEqual(self.lab.ledger('live','ETH-USD'),[])
        with self.assertRaisesRegex(ValueError,'CoinGecko'):
            self.lab.control('live','ETH-USD','refresh')
        with self.assertRaisesRegex(ValueError,'read-only'):
            self.lab.control('live','ETH-USD','start')
    def test_legacy_public_state_is_cached_read_only_and_never_reports_fresh_quotes(self):
        now=self.base+10
        self.lab.feed['ETH-USD']={'state':'ok','checked_at':now,'error':None,'quote':{'price':100,'market_at':now-119,'observed_at':now-1,'requested_at':now-2}}
        with patch('research.analyze',side_effect=AssertionError('state must not train or infer')):
            state=self.lab.state('live','ETH-USD')
        self.assertFalse(state['feed']['fresh']); self.assertEqual(state['feed']['state'],'retired')
        self.assertIsNone(state['latest_probability'])
        self.assertFalse(state['research']['ready'])
        self.assertIn('AI model training',state['feed']['error'])
    def test_signal_waits_one_hour_and_fills_only_new_observation(self):
        self.signal()
        self.lab.apply_quote('ETH-USD',self.quote(self.base+100),now=self.base+100)
        self.assertEqual(self.lab.ledger('live','ETH-USD'),[])
        due=self.base+3600
        cached={'price':90,'market_at':due-1,'observed_at':due+2,'requested_at':due+1}
        self.lab.apply_quote('ETH-USD',cached,now=due+2)
        self.assertEqual(self.lab.ledger('live','ETH-USD'),[])
        self.lab.apply_quote('ETH-USD',self.quote(due+3,123),now=due+3)
        fill=self.lab.ledger('live','ETH-USD')[0]
        self.assertEqual(fill['raw_price'],123)
        self.assertGreater(fill['market_at'],due)
        self.assertAlmostEqual(self.lab.account('live','ETH-USD')['cash'],5000)

    def test_idempotent_quote_and_signal(self):
        points=self.signal()
        self.lab.process_signal('ETH-USD',points,now=self.base+4)
        at=self.base+3600+2; quote=self.quote(at)
        self.lab.apply_quote('ETH-USD',quote,now=at)
        before=self.lab.account('live','ETH-USD')
        self.lab.apply_quote('ETH-USD',quote,now=at)
        self.assertEqual(len(self.lab.ledger('live','ETH-USD')),1)
        self.assertEqual(self.lab.account('live','ETH-USD')['qty'],before['qty'])

    def test_pause_during_signal_computation_cannot_create_pending_entry(self):
        self.running()
        points=self.points+[{'ts':self.base,'price':100}]
        def interrupted_probability(*args):
            self.lab.pause('ETH-USD','Concurrent pause')
            return .99
        with patch('research.probability',side_effect=interrupted_probability):
            self.lab.process_signal('ETH-USD',points,now=self.base+2)
        with self.lab.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM decisions_v3 WHERE status='pending'").fetchone()[0],0)
        self.assertEqual(self.lab.account('live','ETH-USD')['status'],'paused')

    def test_cached_late_signal_can_schedule_a_future_entry(self):
        self.running(); points=self.points+[{'ts':self.base,'price':100}]
        self.lab.process_signal('ETH-USD',points,now=self.base+1200)
        with self.lab.db() as db:
            decision=db.execute("SELECT * FROM decisions_v3").fetchone()
        self.assertEqual(decision['status'],'pending')
        self.assertGreater(decision['due_at'],decision['created_at'])

    def test_public_refresh_job_is_disabled_under_current_api_terms(self):
        with patch('market.fetch_history',side_effect=AssertionError('must not fetch public history')):
            with self.assertRaisesRegex(ValueError,'disabled under the current API terms'):
                self.lab.launch_refresh('ETH-USD')
            self.lab.refresh('ETH-USD',force=True)
        self.assertFalse(self.lab.job['running'])
    def test_exit_uses_fresh_future_price_not_historical_sample(self):
        self.buy_position(); at=self.base+7200+2
        self.lab.apply_quote('ETH-USD',self.quote(at,110),now=at)
        ledger=self.lab.ledger('live','ETH-USD'); self.assertEqual(len(ledger),2)
        self.assertEqual(ledger[0]['side'],'SELL'); self.assertEqual(ledger[0]['raw_price'],110)
        self.assertEqual(self.lab.account('live','ETH-USD')['qty'],0)
        self.assertAlmostEqual(sum(f['pnl'] for f in ledger),self.lab.account('live','ETH-USD')['cash']-r.INITIAL)

    def test_pause_requires_price_after_pause_decision(self):
        at=self.buy_position(); pause_at=at+10
        with patch('engine.time.time',return_value=pause_at): self.lab.pause('ETH-USD','User pause')
        self.lab.apply_quote('ETH-USD',self.quote(pause_at,101),now=pause_at)
        self.assertGreater(self.lab.account('live','ETH-USD')['qty'],0)
        self.lab.apply_quote('ETH-USD',self.quote(pause_at+3,102),now=pause_at+3)
        self.assertEqual(self.lab.account('live','ETH-USD')['qty'],0)

    def test_risk_halt_is_sticky_and_requires_post_decision_exit(self):
        at=self.buy_position()+20
        self.lab.apply_quote('ETH-USD',self.quote(at,60),now=at)
        a=self.lab.account('live','ETH-USD'); self.assertEqual(a['status'],'halted'); self.assertGreater(a['qty'],0)
        with patch('engine.time.time',return_value=at+1): self.lab.pause('ETH-USD','Feed failed')
        self.lab.apply_quote('ETH-USD',self.quote(at+4,120),now=at+4)
        a=self.lab.account('live','ETH-USD'); self.assertEqual(a['status'],'halted'); self.assertEqual(a['qty'],0)
        with self.assertRaisesRegex(ValueError,'read-only'):
            self.lab.control('live','ETH-USD','reset')
        self.assertEqual(self.lab.account('live','ETH-USD')['status'],'halted')

    def test_missed_signal_and_entry_windows_pause_without_fills(self):
        self.running()
        points=self.points+[{'ts':self.base+3600,'price':100}]
        self.lab.process_signal('ETH-USD',points,now=self.base+3600+3)
        self.assertEqual(self.lab.account('live','ETH-USD')['status'],'paused')
        self.signal(); at=self.base+3600+engine.MAX_DELAY+1
        self.lab.apply_quote('ETH-USD',self.quote(at),now=at)
        self.assertEqual(self.lab.ledger('live','ETH-USD'),[])
        self.assertEqual(self.lab.account('live','ETH-USD')['status'],'paused')

    def test_restart_cancels_pending_and_pauses_without_duplicate_fills(self):
        at=self.buy_position()
        with patch('engine.time.time',return_value=at+10): restarted=engine.Lab(self.path)
        a=restarted.account('live','ETH-USD'); self.assertEqual(a['status'],'paused'); self.assertEqual(a['exit_requested'],1)
        self.assertEqual(len(restarted.ledger('live','ETH-USD')),1)
        restarted.apply_quote('ETH-USD',self.quote(at+14,101),now=at+14)
        self.assertEqual(len(restarted.ledger('live','ETH-USD')),2)

    def test_sql_transaction_rolls_back_partial_fill(self):
        self.signal(); at=self.base+3600+2
        with self.lab.db() as db:
            db.execute("CREATE TRIGGER fail_fill BEFORE INSERT ON fills_v3 BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.lab.apply_quote('ETH-USD',self.quote(at),now=at)
        a=self.lab.account('live','ETH-USD'); self.assertEqual(a['cash'],10000); self.assertEqual(a['qty'],0)
        with self.lab.db() as db:
            self.assertEqual(db.execute("SELECT status FROM decisions_v3").fetchone()[0],'pending')

    def test_history_failure_does_not_block_independent_price_exit(self):
        at=self.buy_position()+10
        with patch('engine.time.time',return_value=at),patch('market.fetch_history',side_effect=AssertionError('public history disabled')),patch('market.fetch_quote',return_value=self.quote(at+3,105)):
            self.lab.control('live','ETH-USD','pause')
        self.assertEqual(self.lab.account('live','ETH-USD')['qty'],0)
        self.assertEqual(self.lab.feed['ETH-USD']['state'],'retired')
    def test_demo_live_separation_reset_export_and_replay_idempotence(self):
        self.lab.control('demo','ETH-USD','replay'); before=self.lab.state('demo','ETH-USD')
        self.assertEqual(before['account']['fills'],24)
        self.lab.control('demo','ETH-USD','replay')
        self.assertEqual(self.lab.state('demo','ETH-USD')['account']['fills'],24)
        with self.assertRaisesRegex(ValueError,'read-only'):
            self.lab.control('live','ETH-USD','reset')
        self.assertEqual(self.lab.state('demo','ETH-USD')['account']['fills'],24)
        export=self.lab.export('demo','ETH-USD'); self.assertIn('SYNTHETIC_DEMO',export); self.assertIn('SYNTHETIC REPLAY',export)
        self.assertEqual(self.lab.account('live','ETH-USD')['cash'],10000)
        self.lab.control('demo','ETH-USD','reset'); self.assertEqual(self.lab.ledger('demo','ETH-USD'),[])

    def test_csv_exports_more_than_ten_thousand_fills_without_truncation(self):
        with self.lab.db() as db:
            db.executemany("INSERT INTO fills_v3(namespace,symbol,signal_ts,fill_at,market_at,side,qty,raw_price,price,fee,slippage,pnl,reason) VALUES(?,?,?,?,?,'BUY',1,100,100,0,0,0,'SYNTHETIC TEST')",
                           [(engine.DEMO,'ETH-USD',self.base+i,self.base+i,self.base+i) for i in range(10001)])
        self.assertEqual(len(self.lab.export('demo','ETH-USD').strip().splitlines()),10002)

    def test_cache_survives_restart_without_public_refit_or_inference(self):
        result=r.analyze(self.points)
        with self.lab.db() as db:
            db.execute("INSERT INTO experiments_v3(id,namespace,symbol,created_at,result_json,observations_json,settings_json) VALUES(?,?,?,?,?,?,?)",
                (result['id'],engine.LIVE,'ETH-USD',time.time(),json.dumps(result),json.dumps(self.points),json.dumps(r.settings())))
            db.execute("UPDATE paper_v3 SET experiment_id=?,model_json=? WHERE namespace=? AND symbol=?",
                (result['id'],json.dumps({'model':result['model'],'threshold':result['threshold']}),engine.LIVE,'ETH-USD'))
        restarted=engine.Lab(self.path)
        with patch('research.analyze',side_effect=AssertionError('cached public result must never be refit')):
            state=restarted.state('live','ETH-USD')
            cached=restarted.experiment('live','ETH-USD',self.points)
        self.assertTrue(state['research']['ready'])
        self.assertIsNone(state['latest_probability'])
        self.assertEqual(cached['id'],result['id'])
    def test_original_observations_and_settings_survive_rolling_history_and_restart(self):
        original=self.lab.experiment('demo','ETH-USD',self.points)
        replacement=[{'ts':p['ts']+3600,'price':p['price']*1.1} for p in self.points]
        self.lab.put_prices('demo','ETH-USD',replacement)
        self.lab.experiment('demo','ETH-USD',replacement)
        archive=engine.Lab(self.path).archived_experiment(original['id'])
        self.assertEqual(archive['observations'],self.points)
        self.assertEqual(archive['settings'],r.settings())
        self.assertEqual(r.experiment_key(archive['observations']),original['id'])
        self.assertEqual(archive['result']['model'],original['model'])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(); cls.lab=engine.Lab(Path(cls.tmp.name)/'api.sqlite3')
        cls.research_jobs=ResearchJobs(Path(cls.tmp.name)/'v4')
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(cls.lab,cls.research_jobs)); cls.port=cls.server.server_address[1]
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls): cls.research_jobs.stop(); cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.tmp.cleanup()

    def request(self,path='/',method='GET',body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        c.request(method,path,body=body,headers=headers or {}); response=c.getresponse(); data=response.read(); result=(response.status,dict(response.getheaders()),data); c.close(); return result

    def control(self,body,headers=None):
        h={'Content-Type':'application/json','X-Crypto-Lab':'1',**(headers or {})}
        return self.request('/api/control','POST',json.dumps(body),h)

    def test_assets_health_and_real_api_state_both_symbols(self):
        for path in ('/','/app.js','/time_utils.js','/styles.css','/favicon.svg','/api/health','/api/research/v4','/api/state?mode=demo&symbol=BTC-USD','/api/state?mode=demo&symbol=ETH-USD'):
            code,headers,data=self.request(path); self.assertEqual(code,200,path); self.assertTrue(data); self.assertIn('Content-Security-Policy',headers)

    def test_legacy_public_get_is_cached_only_and_never_trains_or_fetches_history(self):
        with (patch('research.analyze',side_effect=AssertionError('public state must not train')),
              patch('market.fetch_history',side_effect=AssertionError('public state must not fetch history'))):
            code,_,body=self.request('/api/state?mode=live&symbol=BTC-USD')
        self.assertEqual(code,200)
        state=json.loads(body)
        self.assertEqual(state['feed']['state'],'retired')
        self.assertFalse(state['feed']['fresh'])
        self.assertIsNone(state['latest_probability'])
        self.assertFalse(state['research']['ready'])

    def test_v4_status_is_read_only_and_jobs_are_explicit_and_serialized(self):
        _,_,before=self.request('/api/research/v4')
        self.assertEqual(json.loads(before)['job']['state'],'idle')
        self.assertEqual(self.request('/api/research/v4','POST','{}',{'Content-Type':'application/json','X-Crypto-Lab':'1'})[0],400)
        started=threading.Event(); finish=threading.Event()
        def fake_run(instance,action):
            started.set(); finish.wait(2)
            with instance._lock:
                instance._job.update({'running':False,'state':'complete','finished_at_utc':'fixture','error':None})
        with patch.object(ResearchJobs,'_run',fake_run):
            code,_,body=self.request('/api/research/v4','POST',json.dumps({'action':'fetch'}),
                {'Content-Type':'application/json','X-Crypto-Lab':'1'})
            self.assertEqual(code,202); started.wait(1)
            self.assertEqual(self.request('/api/research/v4','POST',json.dumps({'action':'evaluate'}),
                {'Content-Type':'application/json','X-Crypto-Lab':'1'})[0],409)
            finish.set()
        deadline=time.time()+2
        while self.research_jobs.snapshot()['running'] and time.time()<deadline: time.sleep(.01)
        self.assertFalse(self.research_jobs.snapshot()['running'])
        self.assertEqual(json.loads(body)['job']['action'],'fetch')

    def test_bad_inputs_origins_hosts_and_body_shape(self):
        for path in ('/api/state?mode=real&symbol=BTC-USD','/api/state?mode=live&symbol=INVALID','/../../etc/passwd'):
            self.assertIn(self.request(path)[0],(400,404))
        body={'mode':'demo','symbol':'BTC-USD','action':'replay'}
        self.assertEqual(self.control(body,{'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('/api/health',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('/api/control','POST','{}',{'Content-Type':'application/json'})[0],403)
        self.assertEqual(self.control([])[0],400)
        self.assertEqual(self.control({'mode':'demo','symbol':'BTC-USD','action':7})[0],400)
        self.assertEqual(self.request('/api/control','POST','{}',{'Content-Type':'application/json','X-Crypto-Lab':'1','Content-Length':'bad'})[0],400)

    def test_reset_requires_confirmation_and_export_is_labeled(self):
        self.assertEqual(self.control({'mode':'demo','symbol':'ETH-USD','action':'replay'})[0],200)
        body={'mode':'demo','symbol':'ETH-USD','action':'reset'}
        self.assertEqual(self.control(body)[0],400)
        self.assertEqual(self.request('/api/export.csv?mode=demo&symbol=ETH-USD')[0],200)
        self.assertIn(b'SYNTHETIC_DEMO',self.request('/api/export.csv?mode=demo&symbol=ETH-USD')[2])
        body['confirm']='RESET'; self.assertEqual(self.control(body)[0],200)

    def test_no_real_money_endpoint(self):
        self.assertEqual(self.request('/api/order','POST','{}',{'Content-Type':'application/json','X-Crypto-Lab':'1'})[0],404)


if __name__=='__main__': unittest.main(verbosity=2)
