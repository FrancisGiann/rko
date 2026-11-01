"""Load the immutable V5 result once and expose only a compact metrics projection."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_STUDIES = ROOT / "research_data" / "v5" / "studies"
DEFAULT_PROTOCOL = ROOT / "research_protocol_v5_exploratory.json"
DEFAULT_TIME_AUDIT = ROOT / "research_data" / "jesse" / "time-in-market-metric-audit.json"
CELL_METRICS = (
    "net_return_pct", "max_drawdown_pct", "mean_capital_exposure_pct",
    "max_capital_exposure_pct", "time_in_market_pct", "turnover_x_initial_capital",
    "fills", "roundtrips", "fees_usdt", "slippage_usdt", "drawdown_halts",
    "active_signals_after_halt", "invalid_signal_days", "state",
)
PAIR_FIELDS = (
    "candidate", "baseline", "symbol", "fold", "cost_scenario",
    "return_difference_percentage_points", "drawdown_difference_percentage_points",
    "exposure_difference_percentage_points",
)


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


class HistoricalV5Summary:
    """Validate and project saved scores during server construction, never per request."""

    def __init__(self, studies: Path = DEFAULT_STUDIES, protocol_path: Path = DEFAULT_PROTOCOL,
                 time_audit_path: Path = DEFAULT_TIME_AUDIT):
        self.data = self._load(Path(studies), Path(protocol_path), Path(time_audit_path))

    @staticmethod
    def _unavailable(reason: str) -> dict:
        return {"available": False, "status": "unavailable",
            "message": reason, "historical_test_is_fresh": False,
            "cells": [], "paired_comparisons": []}

    def _load(self, studies: Path, protocol_path: Path, time_audit_path: Path) -> dict:
        try:
            protocol_raw = protocol_path.read_bytes()
            protocol = json.loads(protocol_raw)
            protocol_sha = hashlib.sha256(protocol_raw).hexdigest()
            if not isinstance(protocol, dict) or protocol.get("state") != "frozen_before_v5_candidate_scoring":
                return self._unavailable("The V5 exploratory protocol is unavailable or no longer frozen.")
            historical = protocol["historical"]
            candidates = [x["id"] for x in historical["candidates"]]
            costs = historical["cost_scenarios_bps_per_side"]
            folds = [x["name"] for x in historical["datasets"]["BTCUSDT"]["folds"]]
            result_paths = sorted(studies.glob("*/v5-exploratory-*.json"))
            result_paths = [p for p in result_paths if ".freeze." not in p.name and not p.is_symlink()]
            if not result_paths:
                return self._unavailable("No saved V5 exploratory result is bundled.")
            path = result_paths[-1]
            raw = path.read_bytes()
            result_sha = hashlib.sha256(raw).hexdigest()
            checksum_path = Path(str(path) + ".sha256")
            if not checksum_path.is_file() or checksum_path.is_symlink() or checksum_path.read_text().strip() != result_sha:
                return self._unavailable("The saved V5 result checksum is missing or invalid.")
            result = json.loads(raw)
            receipt_path = Path(str(path)[:-5] + ".freeze.json")
            if not receipt_path.is_file() or receipt_path.is_symlink():
                return self._unavailable("The V5 score receipt is missing.")
            receipt = _read_json(receipt_path)
            if (result.get("status") != "complete" or
                    result.get("historical_test_is_fresh") is not False or
                    result.get("protocol_sha256") != protocol_sha or
                    result.get("cell_count_complete") != 210 or
                    len(result.get("cells", [])) != 210 or
                    len(result.get("paired_comparisons", [])) != 360 or
                    result.get("cell_count_failed") != 0 or
                    receipt.get("study_id") != result.get("study_id") or
                    receipt.get("protocol_sha256") != protocol_sha or
                    receipt.get("status") != "complete" or
                    receipt.get("completed_cells") != 210 or
                    receipt.get("failed_cells") != 0):
                return self._unavailable("The saved result is incomplete or does not match the frozen protocol.")
            audit_raw = time_audit_path.read_bytes()
            audit_sha = hashlib.sha256(audit_raw).hexdigest()
            audit_checksum = Path(str(time_audit_path) + ".sha256")
            if (not audit_checksum.is_file() or audit_checksum.is_symlink()
                    or audit_checksum.read_text().strip() != audit_sha):
                return self._unavailable("The saved time-active metric audit is missing or has an invalid checksum.")
            audit = json.loads(audit_raw)
            audit_summary = audit.get("studies", {}).get("V5", {})
            if (audit.get("kind") != "read_only_time_in_market_metric_erratum"
                    or audit.get("source_results_edited") is not False
                    or audit.get("strategy_or_results_rerun") is not False
                    or audit_summary.get("result_sha256") != result_sha
                    or audit_summary.get("protocol_sha256") != protocol_sha
                    or audit_summary.get("audited_cells") != 210
                    or audit_summary.get("expected_cells") != 210):
                return self._unavailable("The time-active audit does not match the immutable V5 result.")
            audit_cells = {}
            for item in audit.get("cells", []):
                if item.get("study") != "V5":
                    continue
                key = (item.get("candidate"), item.get("symbol"), item.get("fold"), item.get("cost_scenario"))
                if key in audit_cells or item.get("source_result_sha256") != result_sha or item.get("source_protocol_sha256") != protocol_sha:
                    return self._unavailable("The saved time-active audit contains duplicate or unmatched cells.")
                value = item.get("corrected_time_in_market_pct")
                if not isinstance(value, (int, float)) or not 0 <= value <= 100:
                    return self._unavailable("The time-active audit contains an invalid corrected value.")
                audit_cells[key] = item
            if len(audit_cells) != 210:
                return self._unavailable("The time-active audit is missing one or more V5 cells.")
            cells = []
            for source in result["cells"]:
                metrics = source["metrics"]
                if source.get("status") != "complete" or metrics.get("state") != "complete":
                    return self._unavailable("A V5 result cell is incomplete.")
                audit_cell = audit_cells.get((source["candidate"], source["symbol"], source["fold"], source["cost_scenario"]))
                if audit_cell is None or abs(float(audit_cell["raw_time_in_market_pct"]) - float(metrics["time_in_market_pct"])) > 1e-9:
                    return self._unavailable("The time-active audit does not reconcile to the archived raw V5 metric.")
                projected_metrics = {key: metrics.get(key) for key in CELL_METRICS}
                projected_metrics["archived_raw_time_in_market_pct"] = metrics.get("time_in_market_pct")
                projected_metrics["time_in_market_pct"] = audit_cell["corrected_time_in_market_pct"]
                cells.append({
                    "sequence": source["sequence"], "candidate": source["candidate"],
                    "symbol": source["symbol"], "fold": source["fold"],
                    "cost_scenario": source["cost_scenario"],
                    "fee_bps_per_side": source["fee_bps_per_side"],
                    "slippage_bps_per_side": source["slippage_bps_per_side"],
                    "planned_final_exit_utc": source["planned_final_exit_utc"],
                    "dataset_sha256": source["dataset_sha256"],
                    "metrics": projected_metrics,
                })
            pairs = [{key: source[key] for key in PAIR_FIELDS}
                     for source in result["paired_comparisons"]]
            return {
                "available": True, "status": "complete",
                "study_id": result["study_id"], "result_sha256": result_sha,
                "created_at_utc": result["created_at_utc"],
                "first_score_started_at_utc": receipt["first_score_started_at_utc"],
                "result_provenance": result["result_provenance"],
                "historical_test_is_fresh": False,
                "all_historical_periods_exploratory_reuse": True,
                "selected_winner": None,
                "interpretation": result["interpretation"],
                "resources": result["resources"],
                "protocol_id": result["protocol_id"],
                "protocol_sha256": protocol_sha,
                "v4_reference": result["v4_reference"],
                "quote_asset": result["quote_asset"],
                "dataset_sha256": result["dataset_sha256"],
                "candidate_order": candidates,
                "cost_scenarios": costs,
                "fold_order": folds,
                "cell_count_expected": 210, "cell_count_complete": 210,
                "cell_count_failed": 0,
                "cells": cells,
                "paired_comparisons": pairs,
                "source_attribution": "Binance Vision public spot data; CC BY-NC-SA 4.0.",
                "time_in_market_audit": {
                    "status": "corrected_overlay",
                    "method": "Economic holdings are counted only while post-fill quantity exceeds 1e-12; raw result and ledger remain unchanged.",
                    "result_sha256": result_sha,
                    "audit_sha256": audit_sha,
                    "affected_cells": audit_summary["affected_time_metrics"],
                    "audited_cells": audit_summary["audited_cells"],
                },
            }
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return self._unavailable(f"Saved V5 result could not be read safely: {str(exc)[:200]}")

    def state(self) -> dict:
        return self.data
