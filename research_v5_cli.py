"""Bounded V5 exploratory comparison runner (no model fitting or network access)."""
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

import walkforward_v5 as w5

ROOT = Path(__file__).resolve().parent
PROTOCOL = ROOT / "research_protocol_v5_exploratory.json"
FREEZE = ROOT / "research_protocol_v5.freeze.json"
LEDGER = ROOT / "research_data/v5/trial-ledger.jsonl"


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _append(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(_json_bytes(item))
        stream.flush()
        os.fsync(stream.fileno())


def _fixture(symbol_index: int, count: int = 2_050) -> tuple[list[dict], list[dict]]:
    start = 1_514_764_800  # Fixed synthetic epoch; no market data.
    daily = []
    price = 30_000.0 if symbol_index == 0 else 1_000.0
    for i in range(count):
        ret = 0.0002 * math.sin(i / 37 + symbol_index) + 0.008 * math.sin(i / 13 + symbol_index * .3)
        price *= max(.96, 1 + ret)
        daily.append({"ts": start + i * w5.DAY, "close": price,
            "high": price * (1 + .004 * abs(math.sin(i / 5))),
            "low": price * (1 - .004 * abs(math.cos(i / 7)))})
    schedule = []
    for i, day in enumerate(daily):
        op = day["close"] * (1 + .001 * math.sin(i / 4 + symbol_index))
        schedule.append({"ts": day["ts"], "exec_ts": day["ts"] + w5.BAR, "mark_open": op})
    return daily, schedule


def run_synthetic_benchmark() -> dict:
    """Exercise the complete candidate × symbol × fold × cost loop on synthetic fixtures."""
    started = time.perf_counter()
    cpu_started = time.process_time()
    cells = 0
    active_target_slots = 0
    fold_names = ("2022", "2023", "2024", "2025", "final_2026")
    for symbol_index, _symbol in enumerate(("BTCUSDT", "ETHUSDT")):
        daily, full_schedule = _fixture(symbol_index)
        first_index = 110
        for fold_index, fold_name in enumerate(fold_names):
            left = first_index + fold_index * 365
            right = left + 365
            schedule = full_schedule[left:right]
            start, end = daily[left]["ts"], daily[right]["ts"]
            final_exit = end - w5.DAY + w5.BAR
            predictions = [{"ts": daily[i]["ts"], "target": ((i + symbol_index) % 9 < 4)} for i in range(left, right)]
            targets = w5.candidate_targets(daily, schedule, predictions, start, end)
            active_target_slots += sum(v is not None and v > 0 for values in targets.values() for v in values.values())
            for candidate in w5.CANDIDATES:
                for cost_id, fee, slip in w5.COSTS:
                    cell = w5.backtest_common(schedule, targets[candidate], final_exit, fee, slip)
                    if cell["metrics"]["state"] != "complete":
                        raise AssertionError("Synthetic benchmark produced an incomplete cell.")
                    cells += 1
    cpu = time.process_time() - cpu_started
    wall = time.perf_counter() - started
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux ru_maxrss is KiB; the project environment is Linux.
    peak_mb = rss_kb / 1024
    result = {
        "kind": "synthetic_only_resource_benchmark",
        "fixture": "deterministic_sinusoids_no_observed_market_rows_no_v4_returns",
        "protocol_sha256": hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
        "cells_exercised": cells,
        "expected_cells": 210,
        "candidate_target_slots": active_target_slots,
        "process_cpu_seconds": round(cpu, 4),
        "wall_seconds": round(wall, 4),
        "peak_rss_mb": round(peak_mb, 3),
        "limits": {"cpu_seconds": 10, "peak_rss_mb": 200, "single_worker_nice": 19},
        "within_budget": cells == 210 and cpu <= 10 and peak_mb <= 200,
        "measured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    out = ROOT / "research_data/v5/synthetic-benchmark.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return result


def verify_freeze() -> tuple[dict, dict, str]:
    protocol_raw = PROTOCOL.read_bytes()
    digest = hashlib.sha256(protocol_raw).hexdigest()
    freeze = json.loads(FREEZE.read_text())
    protocol = json.loads(protocol_raw)
    if freeze.get("protocol_sha256") != digest or protocol.get("state") != "frozen_before_v5_candidate_scoring":
        raise ValueError("V5 protocol freeze receipt does not match the saved protocol.")
    if freeze.get("candidate_results_scored_at_freeze") != 0 or freeze.get("historical_cell_count") != 210:
        raise ValueError("V5 freeze receipt does not certify an empty 210-cell historical study.")
    return protocol, freeze, digest


def run_historical_once() -> dict:
    protocol, freeze, protocol_digest = verify_freeze()
    if LEDGER.exists() and LEDGER.stat().st_size:
        raise RuntimeError("V5 trial ledger is no longer empty; refusing any second or resumed score run.")
    ledger_sha = hashlib.sha256(LEDGER.read_bytes()).hexdigest()
    if ledger_sha != freeze["trial_ledger_initial_sha256"]:
        raise ValueError("V5 ledger bytes differ from the empty freeze receipt.")
    benchmark = json.loads((ROOT / "research_data/v5/synthetic-benchmark.json").read_text())
    if not benchmark.get("within_budget") or benchmark.get("cells_exercised") != 210:
        raise RuntimeError("The full 210-cell synthetic benchmark has not passed the frozen resource gate.")

    saved, datasets, dataset_hashes = w5.load_verified_inputs(ROOT / "research_data/v4")
    if dataset_hashes != freeze["datasets"] or dataset_hashes != {k: v["canonical_sha256"] for k, v in protocol["historical"]["datasets"].items()}:
        raise ValueError("The verified V4 datasets differ from the V5 frozen dataset identities.")
    prepared = w5.build_prepared_data(saved, datasets)
    expected = w5.result_cells()
    if len(expected) != 210:
        raise AssertionError("Frozen V5 cell grid is not 210.")

    study_id = "v5-exploratory-" + hashlib.sha256(_json_bytes({"protocol": protocol_digest, "datasets": dataset_hashes})).hexdigest()[:16]
    outdir = ROOT / "research_data/v5/studies" / study_id
    if outdir.exists():
        raise RuntimeError("V5 study output already exists; refusing an unreviewed rerun.")
    outdir.mkdir(parents=True, exist_ok=False)
    score_started = time.perf_counter()
    cpu_started = time.process_time()
    scored = []
    failures = []
    fold_lookup = {s: {f["name"]: f for f in saved["symbols"][s]["folds"]} for s in saved["symbols"]}
    for sequence, (candidate, symbol, fold_name, cost_id, fee, slip) in enumerate(expected, 1):
        attempt_time = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            result = w5.score_cell(prepared, candidate, symbol, fold_name, cost_id, fee, slip)
            row = {"sequence": sequence, "attempted_at_utc": attempt_time, "status": "complete",
                "protocol_sha256": protocol_digest, "dataset_sha256": dataset_hashes[symbol], **result}
            scored.append(row)
            _append(LEDGER, row)
        except Exception as exc:
            failure = {"sequence": sequence, "attempted_at_utc": attempt_time, "status": "failed",
                "protocol_sha256": protocol_digest, "candidate": candidate, "symbol": symbol,
                "fold": fold_name, "cost_scenario": cost_id, "error": f"{type(exc).__name__}: {str(exc)[:400]}"}
            failures.append(failure)
            _append(LEDGER, failure)
    # All comparisons are paired on the exact same symbol, fixed fold, and cost cell.
    by_key = {(x["candidate"], x["symbol"], x["fold"], x["cost_scenario"]): x for x in scored}
    paired = []
    for candidate, symbol, fold_name, cost_id, fee, slip in expected:
        if candidate in {"cash", "passive", "frozen_v4_logistic"}:
            continue
        current = by_key.get((candidate, symbol, fold_name, cost_id))
        if current is None:
            continue
        for baseline in ("cash", "passive", "frozen_v4_logistic"):
            other = by_key.get((baseline, symbol, fold_name, cost_id))
            if other is None:
                continue
            paired.append({"candidate": candidate, "baseline": baseline, "symbol": symbol,
                "fold": fold_name, "cost_scenario": cost_id,
                "return_difference_percentage_points": current["metrics"]["net_return_pct"] - other["metrics"]["net_return_pct"],
                "drawdown_difference_percentage_points": current["metrics"]["max_drawdown_pct"] - other["metrics"]["max_drawdown_pct"],
                "exposure_difference_percentage_points": current["metrics"]["mean_capital_exposure_pct"] - other["metrics"]["mean_capital_exposure_pct"]})
    cpu = time.process_time() - cpu_started
    wall = time.perf_counter() - score_started
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = {"study_id": study_id, "status": "complete" if len(scored) == 210 and not failures else "incomplete",
        "result_provenance": "newly_scored_exploratory_reuse_of_inspected_history",
        "historical_test_is_fresh": False, "protocol_sha256": protocol_digest,
        "protocol_id": protocol["protocol_id"], "v4_reference": protocol["historical"]["reference_v4"],
        "dataset_sha256": dataset_hashes, "quote_asset": "USDT",
        "candidate_order": list(w5.CANDIDATES), "cost_scenarios": [{"id": c, "fee_bps": f, "slippage_bps": s} for c, f, s in w5.COSTS],
        "cell_count_expected": 210, "cell_count_complete": len(scored), "cell_count_failed": len(failures),
        "cells": scored, "failures": failures, "paired_comparisons": paired,
        "selected_winner": None,
        "interpretation": "All cells reuse market periods and price histories already inspected in V4. No candidate is selected as a winner; lower drawdown from lower exposure is risk management, not evidence of predictive alpha. USD forward paper data is a separate source and no prospective evidence exists until explicitly started.",
        "resources": {"process_cpu_seconds": round(cpu, 4), "wall_seconds": round(wall, 4), "peak_rss_mb": round(rss_kb / 1024, 3)},
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    if failures:
        report["status"] = "incomplete"
    raw = _json_bytes(report)
    result_path = outdir / f"{study_id}.json"
    result_path.write_bytes(raw)
    (outdir / f"{study_id}.json.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n")
    score_receipt = {"study_id": study_id, "protocol_sha256": protocol_digest,
        "first_score_started_at_utc": scored[0]["attempted_at_utc"] if scored else None,
        "ledger_initial_sha256": freeze["trial_ledger_initial_sha256"],
        "ledger_final_sha256": hashlib.sha256(LEDGER.read_bytes()).hexdigest(),
        "completed_cells": len(scored), "failed_cells": len(failures), "status": report["status"],
        "all_historical_periods_exploratory_reuse": True, "selected_winner": None}
    (outdir / f"{study_id}.freeze.json").write_text(json.dumps(score_receipt, sort_keys=True, indent=2) + "\n")
    return {k: report[k] for k in ("study_id", "status", "cell_count_expected", "cell_count_complete", "cell_count_failed", "resources", "protocol_sha256", "dataset_sha256")}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"benchmark", "score"}:
        print("Usage: nice -n 19 python3 research_v5_cli.py benchmark|score", file=sys.stderr)
        return 2
    if sys.argv[1] == "benchmark":
        result = run_synthetic_benchmark()
    else:
        result = run_historical_once()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if (result.get("within_budget", True) and result.get("cell_count_failed", 0) == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
