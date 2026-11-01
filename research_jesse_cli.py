"""Frozen Jesse-rule exploration CLI. Synthetic benchmark is the only pre-review run."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import resource
import sys
import time
from pathlib import Path

import walkforward_jesse as jesse
from walkforward import BAR, utc

ROOT = Path(__file__).resolve().parent
PROTOCOL_PATH = ROOT / "research_protocol_jesse.json"
FREEZE_PATH = ROOT / "research_protocol_jesse.freeze.json"
LEDGER_PATH = ROOT / "research_data/jesse/trial-ledger.jsonl"
BENCHMARK_PATH = ROOT / "research_data/jesse/synthetic-benchmark.json"
CODE_FILES = ("walkforward_jesse.py", "walkforward_v5.py", "walkforward.py", "research_jesse_cli.py")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _append(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(_json_bytes(item))
        stream.flush()
        os.fsync(stream.fileno())


def _source_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in CODE_FILES}


def _protocol() -> tuple[dict, str]:
    raw = PROTOCOL_PATH.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _synthetic_bars(symbol_index: int) -> list[dict]:
    """Deterministic small synthetic bars only; this fixture contains no market rows."""
    start = utc("2019-10-01T00:00:00Z")
    end = utc("2026-10-01T00:00:00Z")
    count = (end - start) // BAR
    price = 8_000.0 if symbol_index == 0 else 180.0
    rows = []
    for i in range(count):
        op = price
        change = (0.00008 * math.sin(i / 31 + symbol_index)
                  + 0.0022 * math.sin(i / 9 + symbol_index * .4)
                  + 0.0004 * math.sin(i / 173 + .2 * symbol_index))
        price = max(1e-6, op * (1 + change))
        rows.append({"ts": start + i * BAR, "open": op,
            "high": max(op, price) * 1.0015, "low": min(op, price) * .9985,
            "close": price, "volume": 1.0})
    return rows


def _fold_from_protocol(item: dict) -> dict:
    return {"name": item["name"],
        "dates_utc": {"test": [item["test_start_utc"][:10], item["end_exclusive_utc"][:10]]},
        "test": {"fixed_final_liquidation_open_utc": item["fixed_final_liquidation_open_utc"]}}


def run_synthetic_benchmark() -> dict:
    protocol, protocol_digest = _protocol()
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    cells = completed = nonzero_fill_cells = 0
    target_condition_counts = {c: 0 for c in jesse.CANDIDATES}
    prepared = {}
    for symbol_index, symbol in enumerate(jesse.SYMBOLS):
        bars = _synthetic_bars(symbol_index)
        prepared[symbol] = {}
        for fold_item in protocol["historical"]["folds"]:
            fold = _fold_from_protocol(fold_item)
            prepared[symbol][fold["name"]] = jesse.prepare_symbol(bars, fold)
            for row in prepared[symbol][fold["name"]]["schedule"]:
                signals = row["signals"]
                target_condition_counts["jesse_donchian20_sma200_long_cash"] += int(signals.get("donchian_entry") is True)
                target_condition_counts["jesse_sma50_200_long_cash"] += int(signals.get("sma50_200_relation") == 1)
    expected_cells = jesse.result_cells()
    for candidate, symbol, fold, cost_id, fee, slip in expected_cells:
        result = jesse.score_cell(prepared, candidate, symbol, fold, cost_id, fee, slip)
        cells += 1
        if result["metrics"]["state"] != "complete":
            raise AssertionError("Synthetic fixture produced an incomplete cell.")
        completed += 1
        nonzero_fill_cells += int(result["metrics"]["fills"] > 0)
    cpu_seconds = time.process_time() - started_cpu
    wall_seconds = time.perf_counter() - started_wall
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    report = {
        "kind": "synthetic_only_jesse_grid_benchmark",
        "fixture": "deterministic_synthetic_4h_ohlcv_no_observed_market_rows",
        "protocol_sha256": protocol_digest,
        "source_sha256": _source_hashes(),
        "expected_cells": 120,
        "cells_exercised": cells,
        "cells_completed": completed,
        "cells_with_synthetic_fills": nonzero_fill_cells,
        "synthetic_entry_condition_counts": target_condition_counts,
        "process_cpu_seconds": round(cpu_seconds, 4),
        "wall_seconds": round(wall_seconds, 4),
        "peak_rss_mb": round(peak_mb, 3),
        "resource_limits": {"process_cpu_seconds_max": 10, "peak_rss_mb_max": 200,
            "worker": "single nice 19"},
        "within_budget": cells == 120 and completed == 120 and cpu_seconds <= 10 and peak_mb <= 200,
        "measured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    BENCHMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
    BENCHMARK_PATH.write_text(json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n")
    return report


def write_freeze_receipt() -> dict:
    protocol, protocol_digest = _protocol()
    if protocol.get("state") != "frozen_before_historical_candidate_scoring":
        raise ValueError("Protocol is not frozen for pre-score review.")
    if not LEDGER_PATH.exists() or LEDGER_PATH.stat().st_size:
        raise RuntimeError("The new append-only trial ledger must still be empty before freeze receipt creation.")
    benchmark = json.loads(BENCHMARK_PATH.read_text())
    if not benchmark.get("within_budget") or benchmark.get("cells_exercised") != 120:
        raise RuntimeError("The complete synthetic 120-cell benchmark has not passed.")
    if benchmark.get("protocol_sha256") != protocol_digest or benchmark.get("source_sha256") != _source_hashes():
        raise ValueError("Benchmark protocol/code identities are stale; rerun the synthetic gate after source changes.")
    receipt = {
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": protocol_digest,
        "freeze_created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "candidate_historical_scores_at_freeze": 0,
        "historical_grid_cells": 120,
        "all_periods_exploratory_reuse": True,
        "dataset_sha256": {s: protocol["historical"]["datasets"][s]["canonical_sha256"] for s in jesse.SYMBOLS},
        "code_sha256": _source_hashes(),
        "synthetic_benchmark_sha256": hashlib.sha256(BENCHMARK_PATH.read_bytes()).hexdigest(),
        "synthetic_benchmark_cells": 120,
        "synthetic_benchmark_cpu_seconds": benchmark["process_cpu_seconds"],
        "synthetic_benchmark_peak_rss_mb": benchmark["peak_rss_mb"],
        "trial_ledger_initial_sha256": hashlib.sha256(LEDGER_PATH.read_bytes()).hexdigest(),
        "trial_ledger_initial_bytes": 0,
        "parent_review_status": "pending",
        "parent_approval_recorded_at_utc": None,
    }
    FREEZE_PATH.write_text(json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False) + "\n")
    return receipt


def verify_score_gate() -> tuple[dict, dict, str]:
    protocol, protocol_digest = _protocol()
    receipt = json.loads(FREEZE_PATH.read_text())
    if receipt.get("protocol_sha256") != protocol_digest:
        raise ValueError("Frozen protocol bytes differ from reviewed receipt.")
    if protocol.get("state") != "frozen_before_historical_candidate_scoring":
        raise ValueError("Only the frozen pre-scoring protocol can be used.")
    if receipt.get("parent_review_status") != "approved" or not receipt.get("parent_approval_recorded_at_utc"):
        raise PermissionError("Root review has not approved this frozen protocol for scoring.")
    if receipt.get("code_sha256") != _source_hashes():
        raise ValueError("Study runner or common engine code changed after freeze review.")
    if not LEDGER_PATH.exists() or LEDGER_PATH.stat().st_size:
        raise RuntimeError("Trial ledger is no longer empty; a score run already consumed the protocol.")
    if hashlib.sha256(LEDGER_PATH.read_bytes()).hexdigest() != receipt.get("trial_ledger_initial_sha256"):
        raise ValueError("Empty-ledger freeze receipt mismatch.")
    benchmark_raw = BENCHMARK_PATH.read_bytes()
    benchmark = json.loads(benchmark_raw)
    if hashlib.sha256(benchmark_raw).hexdigest() != receipt.get("synthetic_benchmark_sha256"):
        raise ValueError("Synthetic benchmark changed after review.")
    if not benchmark.get("within_budget") or benchmark.get("cells_exercised") != 120:
        raise ValueError("Synthetic benchmark resource gate is not satisfied.")
    if benchmark.get("protocol_sha256") != protocol_digest or benchmark.get("source_sha256") != _source_hashes():
        raise ValueError("Synthetic benchmark protocol/code identities do not match the frozen code.")
    if receipt.get("code_sha256") != benchmark.get("source_sha256"):
        raise ValueError("Freeze receipt code identities differ from the synthetic benchmark.")
    return protocol, receipt, protocol_digest


def run_historical_once() -> dict:
    protocol, freeze, protocol_digest = verify_score_gate()
    saved, datasets, dataset_hashes = jesse.load_verified_inputs(ROOT / "research_data/v4")
    expected_hashes = {s: protocol["historical"]["datasets"][s]["canonical_sha256"] for s in jesse.SYMBOLS}
    if dataset_hashes != expected_hashes or dataset_hashes != freeze["dataset_sha256"]:
        raise ValueError("Cached historical datasets do not match the frozen protocol.")
    study_id = "jesse-exploratory-" + hashlib.sha256(_json_bytes({
        "protocol": protocol_digest, "datasets": dataset_hashes})).hexdigest()[:16]
    prepared = jesse.prepare_all(saved, datasets)
    expected = jesse.result_cells()
    if len(expected) != 120 or len({(x[0], x[1], x[2], x[3]) for x in expected}) != 120:
        raise AssertionError("Frozen candidate grid is not exactly 120 unique cells.")
    outdir = ROOT / "research_data/jesse/studies" / study_id
    outdir.mkdir(parents=True, exist_ok=False)
    _append(LEDGER_PATH, {"event": "study_started", "study_id": study_id,
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "protocol_sha256": protocol_digest, "expected_cell_attempts": 120,
        "result_provenance": "exploratory_reuse_of_already_inspected_history"})
    cells, failures = [], []
    for sequence, (candidate, symbol, fold, cost_id, fee, slip) in enumerate(expected, 1):
        attempted_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            result = jesse.score_cell(prepared, candidate, symbol, fold, cost_id, fee, slip)
            row = {"sequence": sequence, "attempted_at_utc": attempted_at,
                "status": "complete", "protocol_sha256": protocol_digest,
                "dataset_sha256": dataset_hashes[symbol], **result}
            cells.append(row)
            _append(LEDGER_PATH, row)
        except Exception as exc:
            failure = {"sequence": sequence, "attempted_at_utc": attempted_at,
                "status": "failed", "protocol_sha256": protocol_digest,
                "candidate": candidate, "symbol": symbol, "fold": fold,
                "cost_scenario": cost_id,
                "error": f"{type(exc).__name__}: {str(exc)[:400]}"}
            failures.append(failure)
            _append(LEDGER_PATH, failure)
    cell_keys = {(x["candidate"], x["symbol"], x["fold"], x["cost_scenario"]) for x in cells}
    expected_keys = {(x[0], x[1], x[2], x[3]) for x in expected}
    if len(cells) + len(failures) != 120 or len(cell_keys) != len(cells) or cell_keys | {
            (x["candidate"], x["symbol"], x["fold"], x["cost_scenario"]) for x in failures} != expected_keys:
        raise AssertionError("Run ledger does not reconcile to all 120 unique expected attempts.")
    report = {
        "study_id": study_id,
        "status": "complete" if len(cells) == 120 and not failures else "incomplete",
        "result_provenance": "newly_scored_exploratory_reuse_of_inspected_history",
        "historical_test_is_fresh": False,
        "historical_periods_previously_inspected_in_v4": True,
        "protocol_sha256": protocol_digest,
        "v4_reference": protocol["historical"]["v4_reference"],
        "dataset_sha256": dataset_hashes,
        "quote_asset": "USDT",
        "candidate_order": list(jesse.CANDIDATES),
        "cost_scenarios": protocol["grid"]["cost_scenarios_bps_per_side"],
        "expected_cells": 120,
        "complete_cells": len(cells),
        "failed_cells": failures,
        "cells": cells,
        "paired_comparisons_vs_cash_and_same_clock_passive": jesse.paired_comparisons(cells),
        "predeclared_descriptive_lead_screen": {
            candidate: jesse.lead_screen(cells, candidate) for candidate in jesse.CANDIDATES[2:]},
        "selected_winner": None,
        "interpretation": "Every historical window and underlying price series was already inspected in V4. All scores are exploratory replays; a lower drawdown from lower realized exposure is risk management, not predictive alpha. A screen pass, if any, is only a lead for separate review and cannot activate the USD paper cohort.",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    raw = _json_bytes(report)
    result_path = outdir / f"{study_id}.json"
    result_path.write_bytes(raw)
    (outdir / f"{study_id}.json.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n")
    (outdir / f"{study_id}.freeze.json").write_text(json.dumps({
        "study_id": study_id, "protocol_sha256": protocol_digest,
        "trial_ledger_sha256": hashlib.sha256(LEDGER_PATH.read_bytes()).hexdigest(),
        "attempted_cells": len(cells) + len(failures), "complete_cells": len(cells),
        "failed_cells": len(failures), "historical_periods_exploratory_reuse": True,
        "selected_winner": None}, sort_keys=True, indent=2) + "\n")
    return {k: report[k] for k in ("study_id", "status", "expected_cells", "complete_cells", "protocol_sha256", "dataset_sha256")}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"benchmark", "freeze", "score"}:
        print("Usage: nice -n 19 python3 research_jesse_cli.py benchmark|freeze|score", file=sys.stderr)
        return 2
    command = sys.argv[1]
    result = (run_synthetic_benchmark() if command == "benchmark" else
              write_freeze_receipt() if command == "freeze" else run_historical_once())
    print(json.dumps(result, sort_keys=True, indent=2))
    if command == "benchmark":
        return 0 if result.get("within_budget") else 1
    if command == "score":
        return 0 if result.get("status") == "complete" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
