"""Synthetic-only checks for prospective daily USD feed validation."""
from __future__ import annotations

import copy
import math
import unittest
from pathlib import Path
from unittest.mock import patch

import market
from research_v5_view import HistoricalV5Summary
import walkforward_v5 as w5


DAY = 86_400


def daily_payload(points: int = 101, *, start: int = 1_735_689_600) -> dict:
    completed = [[(start + i * DAY) * 1000, 100.0 + i] for i in range(points)]
    partial = [(start + (points - 1) * DAY + 52 * 60) * 1000, 200.0]
    return {"prices": completed + [partial]}


class DailyHistoryValidationTests(unittest.TestCase):
    def test_drops_one_valid_trailing_partial_and_preserves_exact_daily_timestamps(self):
        payload = daily_payload()
        now = payload["prices"][-1][0] / 1000 + 1
        points, meta = market.validate_daily_history(payload, now=now)
        self.assertEqual(len(points), 101)
        self.assertEqual(points[0]["ts"], payload["prices"][0][0] // 1000)
        self.assertEqual(points[-1]["ts"], payload["prices"][-2][0] // 1000)
        self.assertEqual(meta["excluded_partial"], 1)
        self.assertTrue(meta["warmup_only"])
        self.assertEqual(meta["missing_intervals"], 0)

    def test_rejects_gaps_duplicates_unsorted_partial_or_future_timestamps(self):
        valid = daily_payload()
        now = valid["prices"][-1][0] / 1000 + 1
        missing = copy.deepcopy(valid)
        missing["prices"][30][0] += 2 * DAY * 1000
        duplicate = copy.deepcopy(valid)
        duplicate["prices"][30][0] = duplicate["prices"][29][0]
        unsorted = copy.deepcopy(valid)
        unsorted["prices"][30], unsorted["prices"][31] = unsorted["prices"][31], unsorted["prices"][30]
        invalid_partial = copy.deepcopy(valid)
        invalid_partial["prices"][-1][0] = invalid_partial["prices"][-2][0] + DAY * 1000
        future = copy.deepcopy(valid)
        future["prices"][-1][0] = int((now + DAY) * 1000)
        for payload in (missing, duplicate, unsorted, invalid_partial, future):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    market.validate_daily_history(payload, now=now)

    def test_rejects_bad_prices_noninteger_milliseconds_and_short_history(self):
        now = daily_payload()["prices"][-1][0] / 1000 + 1
        for mutate in (
            lambda p: p["prices"][5].__setitem__(1, 0),
            lambda p: p["prices"][5].__setitem__(1, float("nan")),
            lambda p: p["prices"][5].__setitem__(0, p["prices"][5][0] + 1),
            lambda p: p["prices"].pop(-4),
        ):
            payload = daily_payload()
            mutate(payload)
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    market.validate_daily_history(payload, now=now)

    def test_fetch_adapter_requests_365_daily_points_only_for_allowed_symbols(self):
        payload = daily_payload()
        with patch.object(market, "public_json", return_value=payload) as fetch, \
             patch.object(market.time, "time", return_value=payload["prices"][-1][0] / 1000 + 1):
            points, meta = market.fetch_daily_history("BTC-USD")
        self.assertEqual(len(points), 101)
        self.assertTrue(meta["warmup_only"])
        fetch.assert_called_once_with("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=365")
        with self.assertRaises(ValueError):
            market.fetch_daily_history("BTCUSDT")


class HistoricalSummaryAuditTests(unittest.TestCase):
    def test_v5_view_is_unavailable_without_saved_result_bundle(self):
        state = HistoricalV5Summary().state()
        self.assertFalse(state["available"])
        self.assertIn("No saved V5 exploratory result is bundled", state["message"])

    def test_v5_status_copy_discloses_residual_dust_correction(self):
        app = (Path(__file__).resolve().parent / "app.js").read_text()
        self.assertIn("Time active excludes residual dust; original scores retained.", app)


class BatchQuoteTests(unittest.TestCase):
    def test_batches_two_fresh_quotes_with_provider_request_and_receive_timestamps(self):
        now = 1_800_000_000.0
        payload = {"bitcoin": {"usd": 68_000.0, "last_updated_at": now - 44},
            "ethereum": {"usd": 2_200.0, "last_updated_at": now - 43}}
        with patch.object(market, "public_json", return_value=payload) as fetch, \
             patch.object(market.time, "time", side_effect=[now, now + 1]):
            quotes = market.fetch_batch_quotes()
        fetch.assert_called_once()
        self.assertEqual(set(quotes), {"BTC-USD", "ETH-USD"})
        self.assertEqual(quotes["BTC-USD"]["requested_at"], now)
        self.assertEqual(quotes["BTC-USD"]["observed_at"], now + 1)
        self.assertEqual(quotes["BTC-USD"]["market_at"], now - 44)

    def test_invalid_or_partial_batch_fails_closed(self):
        now = 1_800_000_000.0
        for payload in (
            {"bitcoin": {"usd": 68_000.0, "last_updated_at": now - 44}},
            {"bitcoin": {"usd": 68_000.0, "last_updated_at": now - 44},
             "ethereum": {"usd": math.inf, "last_updated_at": now - 43}},
            {"bitcoin": {"usd": 68_000.0, "last_updated_at": now - 400},
             "ethereum": {"usd": 2_200.0, "last_updated_at": now - 43}},
        ):
            with patch.object(market, "public_json", return_value=payload), \
                 patch.object(market.time, "time", side_effect=[now, now + 1]):
                with self.assertRaises(ValueError):
                    market.fetch_batch_quotes()


class CommonRiskComparisonTests(unittest.TestCase):
    def test_active_weight_uses_fixed_volatility_floor_and_cap(self):
        self.assertEqual(w5.active_weight(0.0), 0.5)
        self.assertEqual(w5.active_weight(0.001), 0.5)
        self.assertAlmostEqual(w5.active_weight(0.02), .261711961, places=8)
        self.assertAlmostEqual(w5.active_weight(.04), .130855981, places=8)
        self.assertAlmostEqual(w5.active_weight(.10), .052342392, places=8)
        self.assertIsNone(w5.active_weight(None))

    def test_cost_aware_solver_hits_exact_post_cost_weight(self):
        order = w5.solve_trade(10_000, 0, 100, .5, 25, 10)
        self.assertEqual(order["side"], "BUY")
        self.assertAlmostEqual(order["post_trade_weight"], .5, places=12)
        sell = w5.solve_trade(order["cash"], order["qty"], 110, .25, 25, 10)
        equity = sell["cash"] + sell["qty"] * 110
        self.assertAlmostEqual(sell["qty"] * 110 / equity, .25, places=12)
        self.assertGreaterEqual(sell["cash"], 0)
        self.assertLess(sell["qty"], order["qty"])

    @staticmethod
    def schedule(prices):
        rows = []
        start = 1_700_000_000 // w5.DAY * w5.DAY
        for i, price in enumerate(prices):
            ts = start + i * w5.DAY
            rows.append({"ts": ts, "exec_ts": ts + w5.BAR, "mark_open": float(price)})
        return rows

    def test_band_suppresses_small_rebalance_but_zero_target_flattens(self):
        rows = self.schedule([100, 100, 100, 100])
        start = rows[0]["ts"]
        targets = {start: .5, start + w5.DAY: .49, start + 2*w5.DAY: 0.0, start + 3*w5.DAY: .5}
        result = w5.backtest_common(rows, targets, rows[-1]["exec_ts"], 25, 10)
        self.assertEqual(result["metrics"]["fills"], 2)
        self.assertEqual(result["metrics"]["roundtrips"], 1)
        self.assertEqual([x["reason"] for x in result["ledger"]], ["target_rebalance", "target_rebalance"])

    def test_drawdown_halt_is_sticky_and_counts_later_active_signals(self):
        rows = self.schedule([100, 70, 70, 70])
        start = rows[0]["ts"]
        targets = {start + i*w5.DAY: .5 for i in range(4)}
        result = w5.backtest_common(rows, targets, rows[-1]["exec_ts"], 25, 10)
        self.assertTrue(result["metrics"]["halted"])
        self.assertEqual(result["metrics"]["drawdown_halts"], 1)
        self.assertEqual(result["metrics"]["active_signals_after_halt"], 2)
        self.assertEqual([x["reason"] for x in result["ledger"]], ["target_rebalance", "drawdown_halt"])
        self.assertLess(result["metrics"]["ending_usdt"], 10_000)

    def test_fixed_endpoint_missing_or_zero_volume_fails_closed(self):
        rows = self.schedule([100, 101, 102])
        with self.assertRaisesRegex(ValueError, "fixed calendar final"):
            w5.backtest_common(rows[:-1], {}, rows[-1]["exec_ts"], 25, 10)
        rows[-1]["mark_open"] = None
        with self.assertRaisesRegex(ValueError, "fixed calendar final"):
            w5.backtest_common(rows, {}, rows[-1]["exec_ts"], 25, 10)

    def test_candidate_signals_are_invariant_to_future_close_mutation(self):
        daily = []
        start = 1_700_000_000 // w5.DAY * w5.DAY
        for i in range(150):
            close = 100 + i * .4 + (i % 7) * .1
            daily.append({"ts": start + i*w5.DAY, "close": close, "high": close + 1, "low": close - 1})
        decision_days = [row["ts"] for row in daily[101:]]
        schedule = [{"ts": ts} for ts in decision_days]
        cutoff = decision_days[-20]
        preds = [{"ts": ts, "target": (i % 3 == 0)} for i, ts in enumerate(decision_days)]
        before = w5.candidate_targets(daily, schedule, preds, decision_days[0], decision_days[-1] + w5.DAY)
        mutated = [dict(row) for row in daily]
        for row in mutated:
            if row["ts"] >= cutoff:
                row["close"] *= 1.4
                row["high"] *= 1.4
                row["low"] *= 1.4
        after = w5.candidate_targets(mutated, schedule, preds, decision_days[0], decision_days[-1] + w5.DAY)
        for candidate in w5.CANDIDATES:
            self.assertEqual({k:v for k,v in before[candidate].items() if k < cutoff},
                             {k:v for k,v in after[candidate].items() if k < cutoff})

    def test_missing_old_close_only_invalidates_long_sma(self):
        start = 1_700_000_000 // w5.DAY * w5.DAY
        daily = [{"ts": start + i*w5.DAY, "close": 100 + i*.5} for i in range(140)]
        decision = start + 130*w5.DAY
        del daily[decision//w5.DAY - start//w5.DAY - 70]
        features = w5.price_signal_inputs(daily)[decision]
        self.assertIsNotNone(features["weight"])
        self.assertIsNotNone(features["return_7d"])
        self.assertIsNotNone(features["breakout_entry"])
        self.assertIsNone(features["trend"])

    def test_prepared_realistic_daily_closes_enable_signals_and_future_gap_cannot_mask_them(self):
        from datetime import datetime, timezone
        start = 1_600_000_000 // w5.DAY * w5.DAY
        bars = []
        for day_index in range(140):
            daily_close = 100 + day_index * .5
            for slot in range(6):
                ts = start + day_index*w5.DAY + slot*w5.BAR
                close = daily_close * (1 + slot*.0001)
                bars.append({"ts": ts, "open": close, "high": close*1.001,
                    "low": close*.999, "close": close, "volume": 10.0})
        first_i, end_i = 110, 130
        test_start, test_end = start + first_i*w5.DAY, start + end_i*w5.DAY
        fmt = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        preds = [{"ts": start + i*w5.DAY, "target": True} for i in range(first_i, end_i)]
        fold = {"name": "2022", "dates_utc": {"train": [fmt(start), fmt(test_start)],
            "validation": [fmt(test_start), fmt(test_start)], "test": [fmt(test_start), fmt(test_end)]},
            "test": {"planned_final_exit_utc": datetime.fromtimestamp(test_end - w5.DAY + w5.BAR, timezone.utc).isoformat().replace("+00:00", "Z"),
                "test_predictions": preds}}
        saved = {"symbols": {"BTCUSDT": {"folds": [fold]}}}
        prepared = w5.build_prepared_data(saved, {"BTCUSDT": bars})
        targets = prepared["BTCUSDT"]["2022"]["targets"]
        first_day = test_start
        self.assertGreater(targets["passive"][first_day], 0)
        self.assertGreater(targets["momentum_7d"][first_day], 0)
        self.assertGreater(targets["sma20_100"][first_day], 0)
        # Remove the scheduled 04:00 entry and all later bars for this day. The
        # target at 00:00 must remain because no future bar gates signal creation.
        mutated = [dict(row) for row in bars if not (row["ts"] >= first_day + w5.BAR and row["ts"] < first_day + w5.DAY)]
        prepared_after = w5.build_prepared_data(saved, {"BTCUSDT": mutated})
        self.assertEqual(targets["passive"][first_day], prepared_after["BTCUSDT"]["2022"]["targets"]["passive"][first_day])

    def test_invalid_signal_still_trims_weight_cap_at_observed_open(self):
        rows = self.schedule([100, 140, 140])
        start = rows[0]["ts"]
        targets = {start: .5, start + w5.DAY: None, start + 2*w5.DAY: None}
        result = w5.backtest_common(rows, targets, rows[-1]["exec_ts"], 25, 10)
        cap_fills = [x for x in result["ledger"] if x["reason"] == "max_weight_cap_trim"]
        self.assertEqual(len(cap_fills), 1)
        self.assertAlmostEqual(cap_fills[0]["post_trade_weight"], .5, places=12)
        self.assertLessEqual(result["metrics"]["max_capital_exposure_pct"], 50.0000001)

    def test_cap_breach_never_overrides_zero_or_lower_known_target(self):
        rows = self.schedule([100, 150, 150])
        start = rows[0]["ts"]
        zero = w5.backtest_common(rows, {start: .5, start + w5.DAY: 0.0}, rows[-1]["exec_ts"], 25, 10)
        zero_rebalance = [x for x in zero["ledger"] if x["ts"] == rows[1]["exec_ts"]]
        self.assertEqual(zero_rebalance[-1]["post_trade_weight"], 0.0)
        lower = w5.backtest_common(rows, {start: .5, start + w5.DAY: .2}, rows[-1]["exec_ts"], 25, 10)
        lower_rebalance = [x for x in lower["ledger"] if x["ts"] == rows[1]["exec_ts"]]
        self.assertAlmostEqual(lower_rebalance[-1]["post_trade_weight"], .2, places=12)

    def test_neutral_grid_contains_all_210_cells_in_fixed_order(self):
        cells = w5.result_cells()
        self.assertEqual(len(cells), 210)
        self.assertEqual([x[0] for x in cells[:3]], ["cash"] * 3)
        self.assertEqual([x[3] for x in cells[:3]], ["base", "higher", "stress"])
        self.assertEqual(len({x[:4] for x in cells}), 210)


if __name__ == "__main__":
    unittest.main()
