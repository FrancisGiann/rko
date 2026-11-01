"""Synthetic leakage, rule-boundary, and execution checks for the Jesse replay."""
from __future__ import annotations

import unittest
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import walkforward_jesse as jesse
import research_jesse_cli as jesse_cli
from walkforward import BAR


def make_bars(count: int, *, price_fn=None) -> list[dict]:
    rows = []
    for i in range(count):
        close = float(price_fn(i) if price_fn else 100.0)
        op = rows[-1]["close"] if rows else close
        rows.append({"ts": i * BAR, "open": op,
            "high": max(op, close) * 1.001, "low": min(op, close) * .999,
            "close": close, "volume": 1.0})
    return rows


def manual_step(ts: int, open_price, *, final=False, signals=None, present=True):
    return {"decision_ts": ts, "execution_open_ts": ts, "planned_fill_ts": ts + BAR,
        "execution_open": open_price, "execution_volume": 1 if open_price is not None else 0,
        "execution_bar_present": present, "signal_bar_ts": ts - BAR,
        "signal_bar_present": True, "signals": signals or {}, "is_final": final}


class JesseRulesTests(unittest.TestCase):
    def test_donchian_excludes_current_high_and_sma_includes_current_close(self):
        bars = make_bars(240)
        current = bars[200]
        current["close"] = 101.0
        current["high"] = 200.0
        current["low"] = 100.0
        for row in bars[180:200]:
            row["high"] = 100.0
            row["low"] = 99.0
        decision = current["ts"] + BAR
        signals = jesse.build_asof_signals_from_indexes({x["ts"]: x for x in bars},
            jesse.daily_closes(bars), decision)
        self.assertTrue(signals["donchian_entry"])
        self.assertFalse(signals["donchian_exit"])

    def test_donchian_valid_exit_survives_invalid_sma200_and_blocks_entry(self):
        bars = make_bars(260)
        bars = [row for row in bars if row["ts"] != 60 * BAR]
        current = bars[220]
        current["close"] = 90.0
        current["low"] = 90.0
        decision = current["ts"] + BAR
        signals = jesse.build_asof_signals_from_indexes({x["ts"]: x for x in bars},
            jesse.daily_closes(bars), decision)
        self.assertTrue(signals["donchian_exit"])
        self.assertFalse(signals["donchian_entry"])
        self.assertIsNone(signals["sma50_200_relation"])

    def test_sma_equality_keeps_held_position_and_leaves_flat_account_flat(self):
        bars = make_bars(300)
        decision = bars[240]["ts"] + BAR
        signals = jesse.build_asof_signals_from_indexes({x["ts"]: x for x in bars},
            jesse.daily_closes(bars), decision)
        self.assertEqual(signals["sma50_200_relation"], 0)
        self.assertEqual(jesse._candidate_target("jesse_sma50_200_long_cash", signals, False), 0.0)
        self.assertEqual(jesse._candidate_target("jesse_sma50_200_long_cash", signals, True), .5)

    def test_future_suffix_mutation_does_not_change_earlier_signals(self):
        bars = make_bars(500, price_fn=lambda i: 100 + i * .02 + (i % 9) * .001)
        decision = 260 * BAR
        before = jesse.build_asof_signals_from_indexes({x["ts"]: x for x in bars},
            jesse.daily_closes(bars), decision)
        mutated = [dict(row) for row in bars]
        for row in mutated:
            if row["ts"] >= decision:
                row["open"] *= 3
                row["high"] *= 3
                row["low"] *= 3
                row["close"] *= 3
        last = mutated[-1]
        for offset in range(1, 25):
            ts = last["ts"] + offset * BAR
            price = last["close"] * (1 + offset * .002)
            op = mutated[-1]["close"]
            mutated.append({"ts": ts, "open": op, "high": max(op, price) * 1.001,
                "low": min(op, price) * .999, "close": price, "volume": 1.0})
        after = jesse.build_asof_signals_from_indexes({x["ts"]: x for x in mutated},
            jesse.daily_closes(mutated), decision)
        self.assertEqual(before, after)

    def test_prefix_and_future_suffix_replays_preserve_real_buy_sell_fills_for_both_rules(self):
        closes = [100.0] * 200
        closes += [101.0 + .4 * i for i in range(11)]
        closes += [99.0] * 90
        closes += [99.0 + .03 * (i % 7) for i in range(30)]
        bars = []
        for i, close in enumerate(closes):
            op = bars[-1]["close"] if bars else close
            bars.append({"ts": i * BAR, "open": op,
                "high": max(op, close) * 1.001, "low": min(op, close) * .999,
                "close": close, "volume": 1.0})
        start = 201 * BAR
        final = (len(bars) - 1) * BAR
        cutoff = 280 * BAR
        baseline = jesse.prepare_schedule(bars, start, final + BAR, final)
        full_index = {row["ts"]: row for row in bars}
        full_daily = jesse.daily_closes(bars)
        for decision in range(start, cutoff + 1, BAR):
            prefix = [row for row in bars if row["ts"] <= decision - BAR]
            prefix_index = {row["ts"]: row for row in prefix}
            full_signal = jesse.build_asof_signals_from_indexes(full_index, full_daily, decision)
            prefix_signal = jesse.build_asof_signals_from_indexes(
                prefix_index, jesse.daily_closes(prefix), decision)
            self.assertEqual(full_signal, prefix_signal, f"as-of prefix differed at {decision}")

        mutated = [dict(row) for row in bars]
        for row in mutated:
            if row["ts"] > cutoff:
                row["open"] *= 1.7
                row["high"] *= 1.7
                row["low"] *= 1.7
                row["close"] *= 1.7
        last = mutated[-1]
        for n in range(1, 25):
            ts = last["ts"] + n * BAR
            price = last["close"] * (1 + .01 * n)
            op = mutated[-1]["close"]
            mutated.append({"ts": ts, "open": op, "high": max(op, price) * 1.001,
                "low": min(op, price) * .999, "close": price, "volume": 1.0})
        changed = jesse.prepare_schedule(mutated, start, final + BAR, final)
        for candidate in jesse.CANDIDATES[2:]:
            before = jesse.backtest_common(baseline["schedule"], candidate, 25, 10)
            after = jesse.backtest_common(changed["schedule"], candidate, 25, 10)
            prefix_events_before = [x for x in before["ledger"] if x["ts"] <= cutoff]
            prefix_events_after = [x for x in after["ledger"] if x["ts"] <= cutoff]
            self.assertEqual(prefix_events_before, prefix_events_after)
            sides = [x["side"] for x in prefix_events_before]
            self.assertIn("BUY", sides, candidate)
            self.assertIn("SELL", sides, candidate)

    def test_four_hour_execution_delay_and_fixed_endpoint(self):
        active = {"active_weight": .5, "donchian_entry": True, "donchian_exit": False,
            "sma50_200_relation": 1}
        exiting = {"active_weight": .5, "donchian_entry": False, "donchian_exit": True,
            "sma50_200_relation": -1}
        quiet = {"active_weight": .5, "donchian_entry": False, "donchian_exit": False,
            "sma50_200_relation": 0}
        schedule = [manual_step(i * BAR, p, signals=s, final=i == 4)
            for i, (p, s) in enumerate(((100, active), (100, quiet), (100, exiting),
                                         (100, quiet), (100, quiet)))]
        result = jesse.backtest_common(schedule, jesse.CANDIDATES[2], 10, 5)
        self.assertEqual(len(result["ledger"]), 2)
        self.assertEqual(result["ledger"][0]["ts"], BAR)
        self.assertEqual(result["ledger"][0]["decision_ts"], 0)
        self.assertEqual(result["ledger"][1]["ts"], 3 * BAR)
        self.assertEqual(result["ledger"][1]["decision_ts"], 2 * BAR)
        self.assertEqual(result["ledger"][1]["side"], "SELL")

    def test_missed_entry_expires_instead_of_carrying_stale_order(self):
        entry = {"active_weight": .5, "donchian_entry": True, "donchian_exit": False}
        no_entry = {"active_weight": .5, "donchian_entry": False, "donchian_exit": False}
        schedule = [manual_step(0, 100, signals=entry),
            manual_step(BAR, None, signals=no_entry),
            manual_step(2 * BAR, 100, signals={"active_weight": None,
                "donchian_entry": None, "donchian_exit": None}),
            manual_step(3 * BAR, 100, signals=no_entry),
            manual_step(4 * BAR, 100, final=True, signals=no_entry)]
        result = jesse.backtest_common(schedule, jesse.CANDIDATES[2], 10, 5)
        self.assertEqual(result["metrics"]["fills"], 0)
        self.assertEqual(result["metrics"]["ending_usdt"], jesse.INITIAL)

    def test_zero_volume_or_missing_final_endpoint_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "fixed liquidation endpoint"):
            jesse.backtest_common([manual_step(0, 100), manual_step(BAR, None, final=True)],
                                  "cash", 10, 5)

    def test_fixed_calendar_4h_endpoints_use_last_open_before_exclusive_end(self):
        from walkforward import utc
        for first, end, expected in (
            ("2022-01-01", "2023-01-01", "2022-12-31T20:00:00Z"),
            ("2026-01-01", "2026-10-01", "2026-09-30T20:00:00Z"),
        ):
            end_ts = utc(end + "T00:00:00Z")
            final_ts = end_ts - BAR
            fold = {"name": "fixture", "dates_utc": {"test": [first, end]},
                    "test": {"planned_final_exit_utc": "ignored V4 daily endpoint"}}
            prepared = jesse.prepare_symbol([{"ts": final_ts, "open": 100.0,
                "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0}], fold)
            self.assertEqual(jesse.iso_utc(prepared["final_exit_ts"]), expected)
            self.assertEqual(prepared["schedule"][-1]["execution_open"], 100.0)
            with self.assertRaisesRegex(ValueError, "Fixed calendar liquidation open"):
                jesse.prepare_symbol([{"ts": final_ts, "open": 100.0,
                    "high": 101.0, "low": 99.0, "close": 100.0, "volume": 0.0}], fold)

    def test_cap_trim_applies_when_target_window_is_invalid(self):
        active = {"active_weight": .5, "donchian_entry": True, "donchian_exit": False}
        missing = {"active_weight": None, "donchian_entry": None, "donchian_exit": None}
        schedule = [manual_step(0, 100, signals=active),
            manual_step(BAR, 100, signals=missing),
            manual_step(2 * BAR, 200, signals=missing),
            manual_step(3 * BAR, 200, signals=missing),
            manual_step(4 * BAR, 200, final=True, signals=missing)]
        result = jesse.backtest_common(schedule, jesse.CANDIDATES[2], 10, 5)
        cap_trims = [x for x in result["ledger"] if x["reason"] == "max_weight_cap_trim"]
        self.assertTrue(cap_trims)
        self.assertAlmostEqual(cap_trims[0]["post_trade_weight"], .5, places=9)

    def test_post_cost_weight_cash_and_fee_accounting_reconcile(self):
        active = {"active_weight": .5}
        schedule = [manual_step(0, 100, signals=active),
            manual_step(BAR, 100, signals=active),
            manual_step(2 * BAR, 100, final=True, signals=active)]
        result = jesse.backtest_common(schedule, "passive", 25, 10)
        metrics, ledger = result["metrics"], result["ledger"]
        self.assertEqual([row["side"] for row in ledger], ["BUY", "SELL"])
        self.assertAlmostEqual(ledger[0]["post_trade_weight"], .5, places=9)
        self.assertAlmostEqual(metrics["fees_usdt"], sum(row["fee"] for row in ledger), places=9)
        self.assertAlmostEqual(metrics["slippage_usdt"], sum(row["slippage"] for row in ledger), places=9)
        self.assertEqual(metrics["roundtrips"], 1)
        self.assertLess(metrics["ending_usdt"], jesse.INITIAL)
        self.assertTrue(all(row["cash"] >= 0 and row["qty"] >= 0 for row in ledger))

    def test_sampled_drawdown_halt_is_sticky_and_blocks_later_entries(self):
        active = {"active_weight": .5}
        schedule = [manual_step(0, 100, signals=active),
            manual_step(BAR, 100, signals=active),
            manual_step(2 * BAR, 80, signals=active),
            manual_step(3 * BAR, 90, signals=active),
            manual_step(4 * BAR, 90, final=True, signals=active)]
        result = jesse.backtest_common(schedule, "passive", 25, 10)
        metrics = result["metrics"]
        self.assertTrue(metrics["halted"])
        self.assertEqual(metrics["drawdown_halts"], 1)
        self.assertEqual(metrics["halt_date_utc"], jesse.iso_utc(2 * BAR))
        self.assertGreater(metrics["active_signals_after_halt"], 0)
        self.assertEqual(sum(x["side"] == "BUY" for x in result["ledger"]), 1)
        self.assertIn("drawdown_halt", [x["reason"] for x in result["ledger"]])

    def test_grid_is_exactly_120_unique_candidate_asset_fold_cost_cells(self):
        cells = jesse.result_cells()
        keys = [(a, b, c, d) for a, b, c, d, _, _ in cells]
        self.assertEqual(len(cells), 120)
        self.assertEqual(len(set(keys)), 120)
        schedule = [manual_step(0, 100, signals={}),
                    manual_step(BAR, 100, signals={}),
                    manual_step(2 * BAR, 100, final=True, signals={})]
        cash = jesse.backtest_common(schedule, "cash", 10, 5)
        self.assertEqual(cash["metrics"]["invalid_signal_decisions"], 0)

    def test_lead_screen_computes_matched_passive_medians_and_candidate_only_drawdown(self):
        candidate = jesse.CANDIDATES[2]
        cells = []
        for arm, symbol, fold, cost_id, fee, slip in jesse.result_cells():
            is_candidate = arm == candidate
            net = 1.0 if is_candidate else 0.0
            cells.append({"candidate": arm, "symbol": symbol, "fold": fold,
                "cost_scenario": cost_id, "metrics": {"state": "complete",
                    "net_return_pct": net, "ending_usdt": jesse.INITIAL + net,
                    "max_drawdown_pct": 14.0 if is_candidate else 30.0,
                    "roundtrips": 5 if is_candidate and cost_id == "higher" else 0}})
        result = jesse.lead_screen(cells, candidate)
        self.assertTrue(result["criteria"]["entire_120_cell_grid_complete_and_unique"])
        self.assertTrue(result["criteria"]["median_paired_return_difference_vs_passive_nonnegative_at_35bps"])
        self.assertTrue(result["criteria"]["candidate_max_drawdown_at_most_15pct"])
        self.assertTrue(result["passes_all_descriptive_lead_criteria"])

    def test_real_score_gate_stays_closed_until_root_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            protocol_raw = json.dumps({"state": "frozen_before_historical_candidate_scoring"},
                                      sort_keys=True).encode()
            (root / "protocol.json").write_bytes(protocol_raw)
            (root / "freeze.json").write_text(json.dumps({
                "protocol_sha256": hashlib.sha256(protocol_raw).hexdigest(),
                "parent_review_status": "pending"}))
            with patch.multiple(jesse_cli,
                PROTOCOL_PATH=root / "protocol.json",
                FREEZE_PATH=root / "freeze.json",
                LEDGER_PATH=root / "ledger.jsonl"):
                with self.assertRaisesRegex(PermissionError, "Root review has not approved"):
                    jesse_cli.verify_score_gate()


if __name__ == "__main__":
    unittest.main()
