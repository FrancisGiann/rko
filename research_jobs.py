"""Serialized background jobs and read-only status for V4 archive research."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from archive_data import ArchiveStore, DATA_ROOT, protocol, sha


class ResearchJobs:
    def __init__(self, data_root: Path = DATA_ROOT):
        self.data_root = Path(data_root)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._job = {"running": False, "state": "idle", "action": None, "started_at_utc": None,
            "finished_at_utc": None, "progress": None, "error": None}

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._job))

    def start(self, action: str) -> dict:
        if not isinstance(action, str) or action not in {"fetch", "evaluate"}:
            raise ValueError("V4 research action must be fetch or evaluate.")
        if action == "evaluate":
            state = research_state(self.data_root, self)
            if state.get("protocol_status") == "historical_test_consumed":
                if state.get("result"):
                    message = "The frozen historical test is already consumed; load its saved result instead of evaluating again."
                else:
                    message = "The frozen historical test was consumed, but its saved result is missing or invalid; evaluation is blocked."
                raise RuntimeError(message)
        with self._lock:
            if self._job["running"]:
                raise RuntimeError("A V4 historical research job is already running.")
            self._stop.clear()
            self._job = {"running": True, "state": "running", "action": action,
                "started_at_utc": _now(), "finished_at_utc": None, "progress": {"phase": "starting"}, "error": None}
            self._thread = threading.Thread(target=self._run, args=(action,), name="crypto-v4-research", daemon=True)
            self._thread.start()
            result = json.loads(json.dumps(self._job))
        return result

    def _run(self, action: str) -> None:
        try:
            os.nice(19)
        except (AttributeError, OSError):
            pass

        def update(value: dict):
            if value.get("phase") == "evaluate" and "symbol" in value and "fold" in value:
                value = _fit_progress(value, protocol()[0])
            with self._lock:
                self._job["progress"] = value
        try:
            store = ArchiveStore(self.data_root)
            if action == "fetch":
                result = store.fetch(progress=update, stop=self._stop)
                state = result["state"]
                error = result.get("error")
                if state == "verified":
                    final_state = "complete"
                elif state == "interrupted":
                    final_state = "stopped"
                else:
                    final_state = "paused" if state == "paused" else "failed"
                progress = {"phase": "download", "completed": result.get("completed", 0),
                    "total": result.get("total", 168), "current": None}
            else:
                from walkforward import run_study
                result = run_study(store, progress=update)
                final_state = "complete"
                error = None
                recipe, _ = protocol()
                total_fits = len(recipe["dataset"]["symbols"]) * (len(recipe["partitions"]["development_folds"]) + 1)
                progress = {"phase": "complete", "study_id": result["study_id"],
                    "result_provenance": result.get("result_provenance", "newly_evaluated"),
                    "completed_fits": total_fits, "total_fits": total_fits, "progress_unit": "fits"}
            with self._lock:
                self._job.update({"running": False, "state": final_state, "finished_at_utc": _now(),
                    "progress": progress, "error": error})
        except Exception as exc:
            with self._lock:
                self._job.update({"running": False, "state": "failed", "finished_at_utc": _now(),
                    "error": str(exc)[:500]})

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fit_progress(value: dict, recipe: dict) -> dict:
    """Attach real, deterministic progress counts to one model-fit event."""
    folds = recipe["partitions"]["development_folds"] + [recipe["partitions"]["final"]]
    symbols = recipe["dataset"]["symbols"]
    symbol_index = symbols.index(value["symbol"])
    fold_index = next(i for i, fold in enumerate(folds) if fold["name"] == value["fold"])
    return {**value, "completed_fits": symbol_index * len(folds) + fold_index,
        "total_fits": len(symbols) * len(folds), "progress_unit": "fits"}


def _result_dataset_hashes(result: dict) -> dict[str, str]:
    symbols = result.get("symbols")
    if not isinstance(symbols, dict) or not symbols:
        return {}
    hashes = {}
    for symbol, item in symbols.items():
        digest = item.get("dataset", {}).get("canonical_sha256") if isinstance(item, dict) else None
        if not isinstance(digest, str) or len(digest) != 64:
            return {}
        hashes[symbol] = digest
    return hashes


def _consumed_test_recorded(store: ArchiveStore) -> bool:
    """Keep the frozen protocol consumed if its saved result is missing or damaged."""
    ledger = store.root / "trial-ledger.jsonl"
    if ledger.is_file() and not ledger.is_symlink():
        try:
            for line in ledger.read_text().splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if (entry.get("protocol_sha256") == store.protocol_hash and
                        (entry.get("status") == "complete_historical_test_consumed" or
                         entry.get("first_final_test_consumed_at_utc"))):
                    return True
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    studies = store.root / "studies"
    if not studies.is_dir() or studies.is_symlink():
        return False
    for study_dir in studies.iterdir():
        if study_dir.is_symlink() or not study_dir.is_dir() or not study_dir.name.startswith("v4-"):
            continue
        for receipt_path in study_dir.glob("selection-*.json"):
            if receipt_path.is_symlink() or not receipt_path.is_file():
                continue
            try:
                receipt = json.loads(receipt_path.read_text())
                if (receipt.get("protocol_sha256") == store.protocol_hash and
                        receipt.get("final_test_first_consumed_at_utc")):
                    return True
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    return False


def load_saved_result(store: ArchiveStore, *, current_datasets: dict | None = None,
                      active_study_id: str | None = None) -> dict | None:
    studies = store.root / "studies"
    if not studies.exists() or studies.is_symlink():
        return None
    candidates = []
    current_hashes = {}
    if isinstance(current_datasets, dict):
        current_hashes = {symbol: item.get("canonical_sha256") for symbol, item in current_datasets.items()
            if isinstance(item, dict) and isinstance(item.get("canonical_sha256"), str)}
    for path in studies.glob("v4-*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        digest_path = path.with_suffix(".json.sha256")
        if digest_path.is_symlink() or not digest_path.is_file():
            continue
        try:
            raw = path.read_bytes()
            if sha(raw) != digest_path.read_text().strip():
                continue
            result = json.loads(raw)
            if result.get("protocol_sha256") != store.protocol_hash or result.get("status") != "complete":
                continue
            result["result_sha256"] = sha(raw)
            result_hashes = _result_dataset_hashes(result)
            result["dataset_match_status"] = ("matches_current_verified_datasets"
                if current_datasets and result_hashes == current_hashes else "saved_snapshot_differs_or_current_data_unavailable")
            candidates.append((path.name, result))
        except (OSError, ValueError, json.JSONDecodeError):
            continue

    if not candidates:
        return None
    if active_study_id:
        active = next((result for _, result in candidates if result.get("study_id") == active_study_id), None)
        if active:
            active["result_provenance"] = "reused_saved_result"
            return active
    if current_datasets and current_hashes:
        matching = [result for _, result in candidates
            if result.get("dataset_match_status") == "matches_current_verified_datasets"]
        if matching:
            matching.sort(key=lambda item: (item.get("created_at_utc", ""), item.get("study_id", "")), reverse=True)
            matching[0]["result_provenance"] = "reused_saved_result"
            return matching[0]
    candidates.sort(key=lambda pair: (pair[1].get("created_at_utc", ""), pair[1].get("study_id", "")), reverse=True)
    saved = candidates[0][1]
    saved["result_provenance"] = "older_saved_snapshot"
    return saved


def research_state(data_root: Path = DATA_ROOT, jobs: ResearchJobs | None = None) -> dict:
    store = ArchiveStore(data_root)
    try:
        manifest = store.manifest()
        datasets = {symbol: {"quote_asset": item.get("quote_asset"), "bar_interval": item.get("bar_interval"),
            "canonical_sha256": item.get("canonical_sha256"), "rows": item.get("rows"),
            "first_open_utc": item.get("first_open_utc"), "last_close_exclusive_utc": item.get("last_close_exclusive_utc"),
            "missing_4h_slots": len(item.get("missing_slots", [])), "zero_volume_bars": item.get("zero_volume_bars", 0)}
            for symbol, item in manifest.get("datasets", {}).items()}
        errors = [{"archive": key, "error": value.get("error", "archive validation failed")}
            for key, value in manifest.get("archives", {}).items() if value.get("state") != "verified"]
        terms = manifest.get("terms", {})
        data_state = {"state": manifest.get("state", "empty"), "completed": manifest.get("completed", 0),
            "total": manifest.get("total", 168), "error": manifest.get("error"),
            "pause_reason": manifest.get("pause_reason"), "updated_at_utc": manifest.get("updated_at_utc"),
            "failed_archives": errors[:8], "terms": {"license": terms.get("license"),
                "terms_url": terms.get("terms_url"), "terms_sha256": terms.get("terms_sha256"),
                "verified_at_utc": terms.get("verified_at_utc"), "provenance": terms.get("provenance"),
                "current_source_checked": terms.get("current_source_checked")}, "datasets": datasets}
    except Exception as exc:
        data_state = {"state": "error", "completed": 0, "total": 168, "error": str(exc)[:400],
            "failed_archives": [], "datasets": {}}
    job_state = jobs.snapshot() if jobs else {"running": False, "state": "idle"}
    active_study_id = None
    if job_state.get("state") == "complete" and job_state.get("action") == "evaluate":
        active_study_id = (job_state.get("progress") or {}).get("study_id")
    current_datasets = manifest.get("datasets") if data_state.get("state") == "verified" else None
    result = load_saved_result(store, current_datasets=current_datasets, active_study_id=active_study_id)
    if result and job_state.get("state") == "complete" and job_state.get("action") == "evaluate":
        progress = job_state.get("progress") or {}
        if progress.get("study_id") == result.get("study_id"):
            result["result_provenance"] = progress.get("result_provenance", "reused_saved_result")
    consumed = bool(result) or _consumed_test_recorded(store)
    return {"protocol_sha256": store.protocol_hash, "protocol_id": store.protocol.get("id"),
        "protocol_status": "historical_test_consumed" if consumed else "frozen_before_outcome_analysis",
        "result_status": "available" if result else ("missing_or_invalid_after_consumption" if consumed else "not_evaluated"),
        "data": data_state, "job": job_state, "result": result}
