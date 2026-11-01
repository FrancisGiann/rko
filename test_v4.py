"""Small deterministic checks for the frozen historical research pipeline."""
from __future__ import annotations

import csv
import copy
import hashlib
import io
import json
import math
import tempfile
import unittest
import zipfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch
import math
import time

import archive_data as ad
import research_jobs as jobs
import walkforward as wf


def archive_fixture(symbol="BTCUSDT", month="2024-01", *, count=1, start_at=None,
                    volume="2", close_delta=0, timestamp_unit=None, member=None, duplicate=False,
                    partial_duration_seconds=None):
    _, filename, month_start, _ = ad.identity(symbol, month)
    start_at = month_start if start_at is None else start_at
    unit = timestamp_unit or (1_000_000 if month >= "2025-01" else 1000)
    rows = []
    for i in range(count):
        ts = start_at + (0 if duplicate else i) * ad.BAR
        raw = ts * unit
        duration_seconds = partial_duration_seconds if i == 0 and partial_duration_seconds is not None else ad.BAR
        duration_units = round(duration_seconds * unit)
        rows.append([str(raw), "100", "102", "99", str(100 + close_delta), volume,
                     str(raw + duration_units - 1), "200" if volume != "0" else "0", "4", "1" if volume != "0" else "0", "100" if volume != "0" else "0", "0"])
    out = io.StringIO(newline="")
    csv.writer(out, lineterminator="\n").writerows(rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member or filename[:-4] + ".csv", out.getvalue().encode())
    return buf.getvalue()


class ArchiveParserTests(unittest.TestCase):
    def test_timestamp_units_on_2025_boundary(self):
        old = archive_fixture(month="2024-12")
        new = archive_fixture(month="2025-01")
        with patch.object(ad.time, "time", return_value=1_900_000_000):
            old_bars, old_info = ad.parse_archive(old, "BTCUSDT", "2024-12")
            new_bars, new_info = ad.parse_archive(new, "BTCUSDT", "2025-01")
        self.assertEqual(old_info["timestamp_unit"], "ms")
        self.assertEqual(new_info["timestamp_unit"], "us")
        self.assertEqual(old_bars[0]["ts"], ad.utc("2024-12-01T00:00:00Z"))
        self.assertEqual(new_bars[0]["ts"], ad.utc("2025-01-01T00:00:00Z"))

    def test_month_slot_gaps_are_reported_without_filling(self):
        month_start = ad.utc("2024-02-01T00:00:00Z")
        data = archive_fixture(month="2024-02", count=2, start_at=month_start + ad.BAR)
        with patch.object(ad.time, "time", return_value=1_900_000_000):
            bars, info = ad.parse_archive(data, "BTCUSDT", "2024-02")
        self.assertEqual(len(bars), 2)
        self.assertEqual(info["rows"], 2)
        self.assertEqual(len(info["missing_slots"]), 29 * 6 - 2)

    def test_short_historical_candle_is_rejected_and_reported_with_valid_neighbor(self):
        data = archive_fixture(month="2019-11", count=2, partial_duration_seconds=7200)
        with patch.object(ad.time, "time", return_value=1_900_000_000):
            bars, info = ad.parse_archive(data, "BTCUSDT", "2019-11")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["ts"], ad.utc("2019-11-01T04:00:00Z"))
        self.assertEqual(info["partial_rows"], 1)
        self.assertEqual(info["rejected_rows"], 1)
        self.assertEqual(info["partial_bar_rejections"][0]["duration_seconds"], 7200)
        self.assertIn(ad.utc("2019-11-01T00:00:00Z"), info["missing_slots"])
        self.assertEqual(len(info["missing_slots"]), 179)

    def test_short_subsecond_precision_historical_candle_is_rejected_without_rounding(self):
        data = archive_fixture(month="2020-02", count=2, partial_duration_seconds=12932.287)
        with patch.object(ad.time, "time", return_value=1_900_000_000):
            bars, info = ad.parse_archive(data, "BTCUSDT", "2020-02")
        self.assertEqual(len(bars), 1)
        rejected = info["partial_bar_rejections"][0]
        self.assertEqual(rejected["duration_units"], 12_932_287)
        self.assertEqual(rejected["timestamp_unit"], "ms")
        self.assertAlmostEqual(rejected["duration_seconds"], 12932.287)
        self.assertEqual(rejected["ts"], ad.utc("2020-02-01T00:00:00Z"))
        self.assertIn(rejected["ts"], info["missing_slots"])

    def test_wrong_unit_member_duplicates_and_partial_rows_fail_closed(self):
        with patch.object(ad.time, "time", return_value=1_900_000_000):
            with self.assertRaises(ValueError):
                ad.parse_archive(archive_fixture(month="2025-01", timestamp_unit=1000), "BTCUSDT", "2025-01")
            with self.assertRaises(ValueError):
                ad.parse_archive(archive_fixture(member="../BTCUSDT-4h-2024-01.csv"), "BTCUSDT", "2024-01")
            with self.assertRaises(ValueError):
                ad.parse_archive(archive_fixture(count=2, duplicate=True), "BTCUSDT", "2024-01")
            with self.assertRaises(ValueError):
                ad.parse_archive(archive_fixture(month="2024-01", start_at=ad.utc("2024-01-01T00:00:00Z") + 3600), "BTCUSDT", "2024-01")

    def test_zero_volume_is_reported_and_nonfinite_or_bad_ohlc_rejected(self):
        with patch.object(ad.time, "time", return_value=1_900_000_000):
            bars, info = ad.parse_archive(archive_fixture(volume="0"), "BTCUSDT", "2024-01")
            self.assertEqual(bars[0]["volume"], 0)
            self.assertEqual(info["zero_volume_bars"], 1)
            bad = archive_fixture(volume="NaN")
            with self.assertRaises(ValueError):
                ad.parse_archive(bad, "BTCUSDT", "2024-01")

    def test_checksum_binds_exact_archive_and_filename(self):
        data = archive_fixture()
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(ad.verify_checksum(data, f"{digest}  BTCUSDT-4h-2024-01.zip\n".encode(), "BTCUSDT-4h-2024-01.zip"), digest)
        with self.assertRaises(ValueError):
            ad.verify_checksum(data, f"{'0' * 64}  BTCUSDT-4h-2024-01.zip\n".encode(), "BTCUSDT-4h-2024-01.zip")


class ArchiveStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ad.ArchiveStore(Path(self.tmp.name))
        self.store.protocol["dataset"]["symbols"] = ["BTCUSDT"]
        self.store.protocol_hash = "fixture-protocol"
        self.data = archive_fixture()
        self.checksum = f"{hashlib.sha256(self.data).hexdigest()}  BTCUSDT-4h-2024-01.zip\n".encode()
        self.terms = b"Synthetic terms fixture for checksum-gated archive tests.\n"
        self.terms_hash = ad.sha(self.terms)
        self.expected_terms_patcher = patch.object(ad, "EXPECTED_TERMS_SHA", self.terms_hash)
        self.expected_terms_patcher.start()
        self.addCleanup(self.expected_terms_patcher.stop)
        self.archive_calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_fetch(self, changed=False, rate_limit=False):
        archive = archive_fixture(close_delta=1) if changed else self.data
        check = f"{hashlib.sha256(archive).hexdigest()}  BTCUSDT-4h-2024-01.zip\n".encode()
        def fetch(url, limit, **kwargs):
            if url == ad.TERMS_URL:
                return self.terms
            self.archive_calls.append(url)
            if rate_limit:
                raise RuntimeError("Official archive HTTP 429; resume later.")
            return check if url.endswith(".CHECKSUM") else archive
        return fetch

    def test_fresh_terms_are_required_before_archive_fetch(self):
        with patch("archive_data.months", return_value=["2024-01"]), patch("archive_data.https_bytes", side_effect=self.fake_fetch()):
            manifest = self.store.fetch()
        self.assertTrue(manifest["terms"]["current_source_checked"])
        self.assertEqual(manifest["terms"]["provenance"], "fresh_official_tls_check")
        self.assertEqual(manifest["completed"], 1)
        self.assertEqual(manifest["total"], 1)

    def test_corrupt_cache_is_quarantined_repaired_and_reused(self):
        with patch("archive_data.months", return_value=["2024-01"]), patch("archive_data.https_bytes", side_effect=self.fake_fetch()):
            first = self.store.fetch()
            path = self.store.root / "archives/BTCUSDT-4h-2024-01.zip"
            checksum_path = path.with_name(path.name + ".CHECKSUM")
            path.write_bytes(b"corrupt-cache")
            repaired = self.store.fetch()
            self.assertEqual(path.read_bytes(), self.data)
            self.assertEqual(checksum_path.read_bytes(), self.checksum)
            self.assertEqual(repaired["archives"]["BTCUSDT/2024-01"]["repair_refetch"], True)
            self.assertTrue(list((self.store.root / "quarantine").glob("*.corrupt")))
            calls_after_repair = len(self.archive_calls)
            reused = self.store.fetch()
            self.assertEqual(len(self.archive_calls), calls_after_repair)
            self.assertEqual(reused["state"], "verified")
            self.assertEqual(first["completed"], repaired["completed"])

    def test_repair_refuses_different_upstream_snapshot(self):
        with patch("archive_data.months", return_value=["2024-01"]), patch("archive_data.https_bytes", side_effect=self.fake_fetch()):
            self.store.fetch()
            path = self.store.root / "archives/BTCUSDT-4h-2024-01.zip"
            path.write_bytes(b"corrupt-cache")
        with patch("archive_data.months", return_value=["2024-01"]), patch("archive_data.https_bytes", side_effect=self.fake_fetch(changed=True)):
            failed = self.store.fetch()
        self.assertEqual(failed["state"], "partial")
        self.assertIn("immutable snapshot", failed["archives"]["BTCUSDT/2024-01"]["error"])
        self.assertFalse(path.exists())
        self.assertTrue(failed["archives"]["BTCUSDT/2024-01"]["archive_sha256"])

    def test_systemic_rate_limit_pauses_instead_of_hammering_all_months(self):
        self.store.protocol["dataset"]["symbols"] = ["BTCUSDT", "ETHUSDT"]
        with patch("archive_data.months", return_value=["2024-01"]), patch("archive_data.https_bytes", side_effect=self.fake_fetch(rate_limit=True)):
            paused = self.store.fetch()
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(paused["pause_reason"], "upstream_or_network")
        self.assertEqual(len(self.archive_calls), 1)

    def test_protocol_migration_fails_closed_without_private_amendment_receipt(self):
        root = Path(self.tmp.name) / "migration"
        store = ad.ArchiveStore(root)
        data = archive_fixture()
        checksum = f"{hashlib.sha256(data).hexdigest()}  BTCUSDT-4h-2024-01.zip\n".encode()
        archive_path = root / "archives/BTCUSDT-4h-2024-01.zip"
        archive_path.parent.mkdir(parents=True)
        archive_path.write_bytes(data)
        archive_path.with_name(archive_path.name + ".CHECKSUM").write_bytes(checksum)
        old_hash = "8d4cd3a600785ca069e45d2cffa1f2d65c0da5f323a69536661959c5eb7b2e13"
        original = {"protocol_sha256": old_hash, "state": "partial", "completed": 1, "total": 168,
            "archives": {"BTCUSDT/2024-01": {"state": "verified", "archive_sha256": hashlib.sha256(data).hexdigest(),
                "checksum_sha256": hashlib.sha256(checksum).hexdigest(), "fetched_at_utc": "2026-10-31T19:00:00Z"}},
            "datasets": {}, "terms": {"terms_sha256": ad.EXPECTED_TERMS_SHA}}
        ad.write_json(root / "dataset-manifest.json", original)
        with self.assertRaisesRegex(ValueError, "explicit fetch"):
            store.manifest()
        with self.assertRaisesRegex(ValueError, "amendment receipt not included"):
            store.manifest(migrate_pre_fit=True)
        self.assertEqual(archive_path.read_bytes(), data)


def synthetic_bars(days=360, start="2020-01-01T00:00:00Z"):
    first = wf.utc(start)
    bars = []
    for i in range(days * 6):
        price = 100 * math.exp(.00008 * i + .012 * math.sin(i / 43))
        next_price = 100 * math.exp(.00008 * (i + 1) + .012 * math.sin((i + 1) / 43))
        bars.append({"ts": first + i * ad.BAR, "open": price, "high": max(price, next_price) * 1.001,
            "low": min(price, next_price) * .999, "close": next_price,
            "volume": 10 + (i % 9), "quote_volume": price * 10, "trades": 20,
            "taker_base": 4, "taker_quote": price * 4})
    return bars


class WalkForwardTests(unittest.TestCase):
    def setUp(self):
        self.bars = synthetic_bars()
        self.rows, self.daily = wf.build_daily_views(self.bars)

    def test_features_are_prefix_invariant_and_fixed_lookback(self):
        prefix_bars = self.bars[:300 * 6]
        prefix_rows, _ = wf.build_daily_views(prefix_bars)
        changed = [dict(x) for x in self.bars]
        for i in range(300 * 6, len(changed)):
            changed[i]["close"] *= 3
            changed[i]["open"] *= 3
            changed[i]["high"] *= 3
            changed[i]["low"] *= 3
        full_rows, _ = wf.build_daily_views(changed)
        expected = [x for x in full_rows if x["ts"] < prefix_bars[-1]["ts"] // 86400 * 86400]
        self.assertEqual(prefix_rows, expected)
        self.assertEqual(len(self.rows[0]["features"]), 7)
        self.assertEqual(self.rows[0]["ts"], self.daily[0]["ts"] + 21 * 86400)

    def test_zero_volume_execution_window_invalidates_decision_only(self):
        changed = [dict(x) for x in self.bars]
        target_day = self.daily[35]["ts"]
        # The 12:00 bar lies inside the 24-hour holding interval.
        by_ts = {x["ts"]: i for i, x in enumerate(changed)}
        changed[by_ts[target_day + 3 * ad.BAR]]["volume"] = 0
        rows, days = wf.build_daily_views(changed)
        day = next(x for x in days if x["ts"] == target_day)
        self.assertIsNone(day["row"])
        self.assertEqual(day["label_exclusion_reason"], "zero_volume_execution_window")
        original = next(x for x in self.daily if x["ts"] == target_day)
        self.assertEqual(day["decision"], original["decision"])
        self.assertTrue(day["decision"])
        self.assertTrue(rows)

    def test_removing_future_exit_bar_cannot_remove_earlier_asof_target(self):
        target_day = self.daily[35]["ts"]
        exit_ts = target_day + 86400 + ad.BAR
        changed = [x for x in self.bars if x["ts"] != exit_ts]
        rows, days = wf.build_daily_views(changed)
        after = next(x for x in days if x["ts"] == target_day)
        before = next(x for x in self.daily if x["ts"] == target_day)
        self.assertIsNotNone(before["decision"])
        self.assertIsNotNone(after["decision"])
        self.assertEqual(before["decision"], after["decision"])
        self.assertIsNone(after["row"])
        self.assertEqual(after["label_exclusion_reason"], "missing_execution_or_holding_bar")
        model = {"means": [0.0] * 7, "scales": [1.0] * 7, "weights": [2.0] + [0.0] * 7}
        self.assertEqual(wf.probability(model, before["decision"]["features"]),
            wf.probability(model, after["decision"]["features"]))
        target_probability = wf.probability(model, after["decision"]["features"])
        target_map = wf._targets([after["decision"]], [target_probability], 0.5)
        fixed_end=next(x["exec_ts"] for x in days if x["ts"]==self.daily[70]["ts"])
        result=wf.backtest(days,target_map,fixed_end,fee_bps=10,slip_bps=5,
            recipe=wf.protocol()[0]["accounting"])
        self.assertEqual(result["ledger"][0]["side"],"BUY")
        self.assertEqual(result["ledger"][0]["ts"],target_day+ad.BAR)

    def test_split_purges_actual_target_ends_strictly(self):
        start = wf.utc("2020-01-01T00:00:00Z")
        end = wf.utc("2020-06-01T00:00:00Z")
        rows = [{"ts": end - 2 * 86400, "target_end": end - 1},
                {"ts": end - 86400, "target_end": end},
                {"ts": end - 3600, "target_end": end + 7200}]
        kept, purged = wf._partition_rows(rows, start, end)
        self.assertEqual(len(kept), 1)
        self.assertEqual(purged, 2)
        self.assertLess(kept[0]["target_end"], end)

    def test_model_training_is_deterministic_and_train_only(self):
        train = self.rows[:220]
        one = wf.train_model(train)
        two = wf.train_model(train)
        self.assertEqual(one, two)
        self.assertEqual(one, wf.train_model(train))

    def test_backtest_holds_target_and_costs_reduce_matched_result(self):
        start = wf.utc("2020-01-01T00:00:00Z")
        schedule = [{"ts": start + i*86400, "exec_ts": start + i*86400 + 14400,
            "mark_open": 100.0, "exec_open": 100.0} for i in range(5)]
        final = schedule[-1]["exec_ts"]
        targets = {x["ts"]: i < 3 for i, x in enumerate(schedule)}
        recipe = wf.protocol()[0]["accounting"]
        base = wf.backtest(schedule, targets, final, fee_bps=10, slip_bps=5, recipe=recipe)
        stress = wf.backtest(schedule, targets, final, fee_bps=50, slip_bps=25, recipe=recipe)
        self.assertEqual(base["metrics"]["fills"], 2)
        self.assertEqual(base["metrics"]["roundtrips"], 1)
        self.assertGreater(base["metrics"]["time_in_market_pct"], 0)
        self.assertGreater(base["metrics"]["capital_exposure_pct"], 0)
        self.assertGreater(base["metrics"]["fees_usdt"], 0)
        self.assertGreater(base["metrics"]["slippage_usdt"], 0)
        self.assertGreater(base["metrics"]["net_return_pct"], stress["metrics"]["net_return_pct"])

    def test_risk_halt_exits_on_observed_open_and_blocks_reentry(self):
        start = wf.utc("2020-01-01T00:00:00Z")
        values = [100, 100, 80, 70, 80]
        schedule = [{"ts": start + i*86400, "exec_ts": start + i*86400 + 14400, "mark_open": price}
            for i, price in enumerate(values)]
        targets = {x["ts"]: True for x in schedule}
        result = wf.backtest(schedule, targets, schedule[-1]["exec_ts"], fee_bps=10, slip_bps=5,
            recipe=wf.protocol()[0]["accounting"])
        self.assertTrue(result["metrics"]["halted"])
        self.assertEqual(result["metrics"]["drawdown_halts"], 1)
        self.assertEqual(result["metrics"]["roundtrips"], 1)
        self.assertEqual(result["ledger"][1]["ts"], schedule[2]["exec_ts"])

    def test_cash_result_and_bootstrap_are_honest_and_deterministic(self):
        schedule = self.daily[:30]
        final = schedule[-1]["exec_ts"]
        targets = {x["ts"]: False for x in schedule}
        result = wf.backtest(schedule, targets, final, fee_bps=10, slip_bps=5,
            recipe=wf.protocol()[0]["accounting"])
        self.assertEqual(result["metrics"]["net_return_pct"], 0)
        self.assertEqual(result["metrics"]["fills"], 0)
        self.assertIn("no trades", result["metrics"]["interpretation"])
        interval1 = wf.uncertainty(result["daily_returns"], result["daily_returns"], wf.protocol()[0]["uncertainty"])
        interval2 = wf.uncertainty(result["daily_returns"], result["daily_returns"], wf.protocol()[0]["uncertainty"])
        self.assertEqual(interval1, interval2)
        self.assertTrue(interval1["inactive_zero_volatility_no_edge_evidence"])

    def test_empty_classification_reports_unavailable(self):
        self.assertEqual(wf.classification_diagnostics([], [], {"majority_label": 0})["state"], "unavailable")


class SyntheticFitBenchmark(unittest.TestCase):
    def test_frozen_sized_fit_resource_sample(self):
        bars = synthetic_bars(days=850)
        rows, _ = wf.build_daily_views(bars)
        train = rows[:740]
        started = time.perf_counter()
        model = wf.train_model(train)
        elapsed = time.perf_counter() - started
        self.assertEqual(model["train_rows"], len(train))
        self.assertLess(elapsed, 8.0)


class ResearchJobStateTests(unittest.TestCase):
    def test_fit_progress_reports_completed_folds_and_fixed_total(self):
        recipe, _ = ad.protocol()
        progress = jobs._fit_progress({"phase": "evaluate", "symbol": "ETHUSDT", "fold": "2022"}, recipe)
        self.assertEqual(progress["completed_fits"], 5)
        self.assertEqual(progress["total_fits"], 10)
        self.assertEqual(progress["progress_unit"], "fits")

    def test_status_distinguishes_newly_evaluated_from_reused_saved_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ad.ArchiveStore(root)
            result = {"study_id": "v4-fixture", "protocol_sha256": store.protocol_hash, "status": "complete"}
            raw = (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode()
            result_path = root / "studies/v4-fixture.json"
            result_path.parent.mkdir(parents=True)
            result_path.write_bytes(raw)
            result_path.with_suffix(".json.sha256").write_text(ad.sha(raw) + "\n")

            just_evaluated = jobs.ResearchJobs(root)
            just_evaluated._job.update({"state": "complete", "action": "evaluate", "progress": {
                "phase": "complete", "study_id": "v4-fixture", "result_provenance": "newly_evaluated",
                "completed_fits": 10, "total_fits": 10, "progress_unit": "fits"}})
            fresh = jobs.research_state(root, just_evaluated)
            self.assertEqual(fresh["protocol_status"], "historical_test_consumed")
            self.assertEqual(fresh["result"]["result_provenance"], "newly_evaluated")

            reloaded = jobs.research_state(root, jobs.ResearchJobs(root))
            self.assertEqual(reloaded["result"]["result_provenance"], "older_saved_snapshot")

    def test_saved_result_selection_matches_verified_dataset_before_newer_unmatched_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ad.ArchiveStore(root)
            current_hashes = {"BTCUSDT": "a" * 64, "ETHUSDT": "b" * 64}
            datasets = {symbol: {"canonical_sha256": digest} for symbol, digest in current_hashes.items()}
            ad.write_json(root / "dataset-manifest.json", {"protocol_sha256": store.protocol_hash,
                "state": "verified", "completed": 168, "total": 168, "archives": {}, "datasets": datasets})

            def save(study_id, created, hashes):
                result = {"study_id": study_id, "protocol_sha256": store.protocol_hash, "status": "complete",
                    "created_at_utc": created, "symbols": {symbol: {"dataset": {"canonical_sha256": digest}}
                        for symbol, digest in hashes.items()}}
                raw = (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode()
                path = root / "studies" / f"{study_id}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                path.with_suffix(".json.sha256").write_text(ad.sha(raw) + "\n")

            save("v4-older-match", "2026-01-01T00:00:00Z", current_hashes)
            save("v4-newer-other-data", "2026-10-01T00:00:00Z", {"BTCUSDT": "c" * 64, "ETHUSDT": "d" * 64})
            state = jobs.research_state(root, jobs.ResearchJobs(root))
            self.assertEqual(state["result"]["study_id"], "v4-older-match")
            self.assertEqual(state["result"]["dataset_match_status"], "matches_current_verified_datasets")

            active = jobs.load_saved_result(store, current_datasets=datasets, active_study_id="v4-newer-other-data")
            self.assertEqual(active["study_id"], "v4-newer-other-data")
            self.assertEqual(active["dataset_match_status"], "saved_snapshot_differs_or_current_data_unavailable")

    def test_consumed_selection_with_missing_or_corrupt_result_stays_consumed_and_blocks_evaluate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ad.ArchiveStore(root)
            study_id = "v4-consumed-fixture"
            study_dir = root / "studies" / study_id
            study_dir.mkdir(parents=True)
            result_path = root / "studies" / f"{study_id}.json"
            result_path.write_text('{"study_id":"damaged"}')
            result_path.with_suffix(".json.sha256").write_text("0" * 64 + "\n")
            ad.write_json(study_dir / "selection-BTCUSDT-final.json", {
                "study_id": study_id, "protocol_sha256": store.protocol_hash,
                "final_test_first_consumed_at_utc": "2026-10-31T19:45:00Z"})

            state = jobs.research_state(root, jobs.ResearchJobs(root))
            self.assertEqual(state["protocol_status"], "historical_test_consumed")
            self.assertEqual(state["result_status"], "missing_or_invalid_after_consumption")
            self.assertIsNone(state["result"])
            with self.assertRaisesRegex(RuntimeError, "consumed"):
                jobs.ResearchJobs(root).start("evaluate")


class SmallStudyFixture:
    def __init__(self, root: Path, recipe: dict, protocol_sha: str, *, days=310,
                 mutate_from=None, mutate_until=None):
        self.root, self.protocol_hash, self.protocol = root, protocol_sha, recipe
        self.bars = {"BTCUSDT": synthetic_bars(days=days), "ETHUSDT": synthetic_bars(days=days)}
        for i, bar in enumerate(self.bars["ETHUSDT"]):
            for key in ("open", "high", "low", "close"):
                bar[key] *= 1.7
            bar["quote_volume"] *= 1.7
            bar["taker_quote"] *= 1.7
        if mutate_from is not None:
            for bars in self.bars.values():
                for bar in bars:
                    if bar["ts"] >= mutate_from and (mutate_until is None or bar["ts"] < mutate_until):
                        for key in ("open", "high", "low", "close"):
                            bar[key] *= 1.4
                        bar["volume"] *= 1.2
                        bar["quote_volume"] *= 1.4
                        bar["taker_base"] *= 1.2
                        bar["taker_quote"] *= 1.4

    def load(self, symbol):
        bars = self.bars[symbol]
        raw = json.dumps(bars, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        return bars, {"symbol": symbol, "quote_asset": "USDT", "bar_interval": "4h",
            "canonical_sha256": hashlib.sha256(raw).hexdigest(), "file": f"fixture/{symbol}.json",
            "rows": len(bars), "first_open_utc": wf.iso(bars[0]["ts"]),
            "last_close_exclusive_utc": wf.iso(bars[-1]["ts"] + ad.BAR), "missing_slots": [], "zero_volume_bars": 0}

    def manifest(self):
        return {"terms": {"license": "CC BY-NC-SA 4.0", "terms_sha256": ad.EXPECTED_TERMS_SHA,
            "provenance": "synthetic test fixture", "current_source_checked": False}}


class StudyIntegrationTests(unittest.TestCase):
    def test_complete_small_study_and_saved_reuse(self):
        original, _ = ad.protocol()
        recipe = copy.deepcopy(original)
        base = wf.utc("2020-01-01T00:00:00Z")
        def day(value):
            return datetime.fromtimestamp(base + value * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        def fold(name, train, validation, test):
            return {"name": name, "train_start": day(train[0]), "train_end_exclusive": day(train[1]),
                "validation_start": day(validation[0]), "validation_end_exclusive": day(validation[1]),
                "test_start": day(test[0]), "test_end_exclusive": day(test[1])}
        recipe["partitions"]["development_folds"] = [
            fold("a", (0, 90), (90, 120), (120, 150)),
            fold("b", (30, 120), (120, 150), (150, 180)),
            fold("c", (60, 150), (150, 180), (180, 210)),
            fold("d", (90, 180), (180, 210), (210, 240))]
        recipe["partitions"]["final"] = fold("final_fixture", (120, 210), (210, 240), (240, 270))
        protocol_sha = hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            store = SmallStudyFixture(Path(directory), recipe, protocol_sha)
            with patch.object(wf, "protocol", return_value=(recipe, protocol_sha)):
                result = wf.run_study(store)
                self.assertEqual(result["result_provenance"], "newly_evaluated")
                self.assertEqual(len(result["symbols"]), 2)
                for symbol in ("BTCUSDT", "ETHUSDT"):
                    folds = result["symbols"][symbol]["folds"]
                    self.assertEqual(len(folds), 5)
                    self.assertEqual(len(folds[0]["selection"]["all_candidates"]), 6)
                    final = folds[-1]
                    self.assertTrue(final["test"]["threshold_frozen_before_test"])
                    labeled_predictions = sum(item["label"] is not None for item in final["test"]["test_predictions"])
                    self.assertEqual(final["test"]["probability_summary"]["feature_eligible_decisions"], labeled_predictions)
                    self.assertEqual(set(final["test"]["cost_scenarios"]), {"base", "higher", "stress"})
                ledger = Path(directory) / "trial-ledger.jsonl"
                self.assertEqual(len(ledger.read_text().splitlines()), 1)
                with patch.object(wf, "train_model", side_effect=AssertionError("cached study refit")):
                    cached = wf.run_study(store)
                self.assertEqual(cached["result_provenance"], "reused_saved_result")
                self.assertEqual(len(ledger.read_text().splitlines()), 1)

    def test_final_period_raw_mutation_cannot_change_fits_or_selections(self):
        original, _ = ad.protocol()
        recipe = copy.deepcopy(original)
        base = wf.utc("2020-01-01T00:00:00Z")
        def day(value):
            return datetime.fromtimestamp(base + value * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        def fold(name, train, validation, test):
            return {"name": name, "train_start": day(train[0]), "train_end_exclusive": day(train[1]),
                "validation_start": day(validation[0]), "validation_end_exclusive": day(validation[1]),
                "test_start": day(test[0]), "test_end_exclusive": day(test[1])}
        recipe["partitions"]["development_folds"] = [
            fold("a", (0, 90), (90, 120), (120, 150)), fold("b", (30, 120), (120, 150), (150, 180)),
            fold("c", (60, 150), (150, 180), (180, 210)), fold("d", (90, 180), (180, 210), (210, 240))]
        recipe["partitions"]["final"] = fold("final_fixture", (120, 210), (210, 240), (240, 270))
        protocol_sha = hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        mutation_start, mutation_end = base + 260 * 86400, base + 270 * 86400
        with tempfile.TemporaryDirectory() as baseline_dir, tempfile.TemporaryDirectory() as changed_dir:
            baseline_store = SmallStudyFixture(Path(baseline_dir), recipe, protocol_sha, days=310)
            changed_store = SmallStudyFixture(Path(changed_dir), recipe, protocol_sha, days=310,
                mutate_from=mutation_start, mutate_until=mutation_end)
            with patch.object(wf, "protocol", return_value=(recipe, protocol_sha)):
                baseline = wf.run_study(baseline_store)
                changed = wf.run_study(changed_store)
            for symbol in ("BTCUSDT", "ETHUSDT"):
                before_folds = baseline["symbols"][symbol]["folds"]
                after_folds = changed["symbols"][symbol]["folds"]
                self.assertEqual(len(before_folds), 5)
                for before, after in zip(before_folds, after_folds):
                    self.assertEqual(before["fit"]["snapshot_sha256"], after["fit"]["snapshot_sha256"])
                    self.assertEqual(before["fit"]["selected_threshold"], after["fit"]["selected_threshold"])
                    self.assertEqual(before["selection"], after["selection"])
                for before, after in zip(before_folds[:-1], after_folds[:-1]):
                    self.assertEqual(before["test"]["test_predictions"], after["test"]["test_predictions"])
                final_before = before_folds[-1]["test"]["test_predictions"]
                final_after = after_folds[-1]["test"]["test_predictions"]
                early_before = {x["ts"]: (x["probability"], x["target"]) for x in final_before if x["ts"] < mutation_start}
                early_after = {x["ts"]: (x["probability"], x["target"]) for x in final_after if x["ts"] < mutation_start}
                self.assertEqual(early_before,early_after)
                self.assertEqual(len(early_before), 20)
                # Mutation is confined to the final reserved test period.
                for before, after in zip(baseline_store.bars[symbol], changed_store.bars[symbol]):
                    if not mutation_start <= before["ts"] < mutation_end:
                        self.assertEqual(before, after)
            self.assertNotEqual(baseline["study_id"], changed["study_id"])

    def test_append_after_fixed_windows_cannot_change_earlier_predictions(self):
        original, _ = ad.protocol()
        recipe = copy.deepcopy(original)
        base = wf.utc("2020-01-01T00:00:00Z")

        def day(value):
            return datetime.fromtimestamp(base + value * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def fold(name, train, validation, test):
            return {"name": name, "train_start": day(train[0]), "train_end_exclusive": day(train[1]),
                "validation_start": day(validation[0]), "validation_end_exclusive": day(validation[1]),
                "test_start": day(test[0]), "test_end_exclusive": day(test[1])}

        recipe["partitions"]["development_folds"] = [
            fold("a", (0, 90), (90, 120), (120, 150)), fold("b", (30, 120), (120, 150), (150, 180)),
            fold("c", (60, 150), (150, 180), (180, 210)), fold("d", (90, 180), (180, 210), (210, 240))]
        recipe["partitions"]["final"] = fold("final_fixture", (120, 210), (210, 240), (240, 270))
        protocol_sha = hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as baseline_dir, tempfile.TemporaryDirectory() as appended_dir:
            baseline_store = SmallStudyFixture(Path(baseline_dir), recipe, protocol_sha, days=310)
            appended_store = SmallStudyFixture(Path(appended_dir), recipe, protocol_sha, days=330)
            with patch.object(wf, "protocol", return_value=(recipe, protocol_sha)):
                baseline = wf.run_study(baseline_store)
                appended = wf.run_study(appended_store)
            for symbol in ("BTCUSDT", "ETHUSDT"):
                before_folds = baseline["symbols"][symbol]["folds"]
                after_folds = appended["symbols"][symbol]["folds"]
                for before, after in zip(before_folds, after_folds):
                    self.assertEqual(before["fit"]["snapshot_sha256"], after["fit"]["snapshot_sha256"])
                    self.assertEqual(before["selection"], after["selection"])
                    self.assertEqual(before["test"]["test_predictions"], after["test"]["test_predictions"])
                self.assertGreater(appended_store.bars[symbol][-1]["ts"], baseline_store.bars[symbol][-1]["ts"])
            self.assertNotEqual(baseline["study_id"], appended["study_id"])


if __name__ == "__main__":
    unittest.main()
