"""Frozen, bounded walk-forward evaluation for the personal V4 research study."""
from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

from archive_data import BAR, ArchiveStore, atomic, iso, protocol, sha, write_json

INITIAL = 10_000.0
DAY = 86_400
FEATURES = 7


def utc(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std_population(values: list[float]) -> float:
    mean = _mean(values)
    return math.sqrt(sum((x - mean) ** 2 for x in values) / len(values)) if values else 0.0


def build_daily_views(bars: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return supervised label rows and every as-of decision/observation slot.

    A decision at D 00:00 uses bars through D 00:00, enters at D 04:00,
    and exits at D+1 04:00. Invalid dates remain in the second result so a
    running account can carry its position to the next observed open.
    """
    by_ts = {}
    for bar in bars:
        ts = bar.get("ts")
        if not isinstance(ts, int) or ts % BAR or ts in by_ts:
            raise ValueError("Dataset timestamps must be unique aligned four-hour integers.")
        values = (bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close"), bar.get("volume"))
        if any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
            raise ValueError("Dataset contains non-finite bar values.")
        if not (0 < bar["low"] <= bar["open"] <= bar["high"] and bar["low"] <= bar["close"] <= bar["high"] and bar["volume"] >= 0):
            raise ValueError("Dataset contains invalid OHLCV values.")
        by_ts[ts] = bar
    if not by_ts:
        return [], []
    first = min(by_ts) // DAY * DAY
    last = max(by_ts) // DAY * DAY
    daily = {}
    for day in range(first, last + 1, DAY):
        day_bars = [by_ts.get(day + i * BAR) for i in range(6)]
        entry = by_ts.get(day + BAR)
        if all(day_bars):
            daily[day] = {"ts": day, "open": day_bars[0]["open"],
                "high": max(x["high"] for x in day_bars), "low": min(x["low"] for x in day_bars),
                "close": day_bars[-1]["close"], "volume": sum(x["volume"] for x in day_bars),
                "bars": day_bars, "entry_open": entry["open"] if entry else None,
                "mark_open": entry["open"] if entry and entry["volume"] > 0 else None,
                "exec_ts": day + BAR}
        else:
            daily[day] = {"ts": day, "bars": day_bars, "entry_open": entry["open"] if entry else None,
                "mark_open": entry["open"] if entry and entry["volume"] > 0 else None,
                "exec_ts": day + BAR, "exclusion_reason": "incomplete_daily_feature_bars"}

    schedule = []
    valid = []
    for day in sorted(daily):
        current = daily[day]
        record = {"ts": day, "exec_ts": day + BAR, "exec_open": current.get("entry_open"),
            "mark_open": current.get("mark_open"), "features": None, "decision": None, "row": None,
            "exclusion_reason": None, "label_exclusion_reason": None,
            "mark_data_reason": current.get("exclusion_reason")}
        prior = [daily.get(day - i * DAY) for i in range(1, 22)]
        if all(x and "close" in x for x in prior):
            recent_closes = [x["close"] for x in prior]
            returns = [recent_closes[i] / recent_closes[i + 1] - 1 for i in range(20)]
            latest_close = recent_closes[0]
            previous = prior[0]
            previous_volume = previous["volume"]
            preceding_volumes = [x["volume"] for x in prior[1:21]]
            latest20 = recent_closes[:20]
            feature = [latest_close / recent_closes[1] - 1,
                latest_close / recent_closes[3] - 1,
                latest_close / recent_closes[7] - 1,
                _std_population(returns),
                (previous["high"] - previous["low"]) / latest_close,
                previous_volume / _mean(preceding_volumes) - 1 if _mean(preceding_volumes) > 0 else 0.0,
                latest_close / _mean(latest20) - 1]
            if not all(math.isfinite(x) for x in feature):
                record["exclusion_reason"] = "nonfinite_feature"
            else:
                record["features"] = feature
                record["decision"] = {"ts": day, "signal_at": day, "exec_ts": day + BAR,
                    "features": feature, "exec_open": current.get("entry_open")}
        else:
            record["exclusion_reason"] = "incomplete_126_bar_lookback"

        entry_ts = day + BAR
        exit_ts = day + DAY + BAR
        execution = [by_ts.get(ts) for ts in range(entry_ts, exit_ts + BAR, BAR)]
        if record["decision"]:
            if len(execution) != 7 or any(x is None for x in execution):
                record["label_exclusion_reason"] = "missing_execution_or_holding_bar"
            elif any(x["volume"] <= 0 for x in execution):
                record["label_exclusion_reason"] = "zero_volume_execution_window"
        else:
            record["label_exclusion_reason"] = record["exclusion_reason"]
        if record["decision"] and not record["label_exclusion_reason"]:
            entry, exit_bar = execution[0], execution[-1]
            if entry["open"] <= 0 or exit_bar["open"] <= 0:
                record["label_exclusion_reason"] = "invalid_execution_open"
            else:
                row = {"ts": day, "signal_at": day, "exec_ts": entry_ts, "target_end": exit_ts,
                    "features": record["features"], "exec_open": entry["open"], "exit_open": exit_bar["open"],
                    "target_return": exit_bar["open"] / entry["open"] - 1,
                    "label": int(exit_bar["open"] / entry["open"] - 1 > 0)}
                record["row"] = row
                valid.append(row)
        schedule.append(record)
    return valid, schedule


def train_model(rows: list[dict], recipe: dict | None = None) -> dict:
    if not rows:
        raise ValueError("No eligible training rows remain after the frozen date and gap filters.")
    recipe = recipe or protocol()[0]["model"]
    matrix = [row["features"] for row in rows]
    means = [_mean([row[j] for row in matrix]) for j in range(FEATURES)]
    scales = [_std_population([row[j] for row in matrix]) for j in range(FEATURES)]
    scales = [x if x > 1e-12 else 1.0 for x in scales]
    weights = [0.0] * (FEATURES + 1)
    labels = [row["label"] for row in rows]
    n = len(rows)
    lr, reg = float(recipe["learning_rate"]), float(recipe["regularization"])
    for _ in range(int(recipe["epochs"])):
        gradients = [0.0] * len(weights)
        for feature, label in zip(matrix, labels):
            x = [1.0] + [(feature[j] - means[j]) / scales[j] for j in range(FEATURES)]
            z = sum(w * v for w, v in zip(weights, x))
            p = 1 / (1 + math.exp(-max(-35.0, min(35.0, z))))
            error = p - label
            for j, value in enumerate(x):
                gradients[j] += error * value
        weights[0] -= lr * gradients[0] / n
        for j in range(1, len(weights)):
            weights[j] -= lr * (gradients[j] / n + reg * weights[j])
    model = {"means": means, "scales": scales, "weights": weights,
        "epochs": int(recipe["epochs"]), "learning_rate": lr, "regularization": reg,
        "train_rows": n, "train_positive_rate": _mean([float(x) for x in labels]),
        "majority_label": int(sum(labels) > n / 2)}
    model["fit_id"] = sha(json.dumps({"model": model, "train_ts": [x["ts"] for x in rows]}, sort_keys=True, separators=(",", ":")).encode())
    return model


def probability(model: dict, feature: list[float]) -> float:
    x = [1.0] + [(feature[j] - model["means"][j]) / model["scales"][j] for j in range(FEATURES)]
    z = sum(w * value for w, value in zip(model["weights"], x))
    return 1 / (1 + math.exp(-max(-35.0, min(35.0, z))))


def classify(rows: list[dict], model: dict) -> list[float]:
    return [probability(model, row["features"]) for row in rows]


def _buy(cash: float, raw: float, fee_bps: float, slip_bps: float, fraction: float) -> tuple[float, float, dict]:
    fee_rate, slip_rate = fee_bps / 10_000, slip_bps / 10_000
    budget = cash * fraction
    effective = raw * (1 + slip_rate)
    qty = budget / (effective * (1 + fee_rate))
    gross, fee, slippage = qty * effective, qty * effective * fee_rate, qty * raw * slip_rate
    cash -= gross + fee
    return cash, qty, {"side": "BUY", "raw_notional": qty * raw, "fee": fee, "slippage": slippage,
        "fill_price": effective}


def _sell(cash: float, qty: float, raw: float, fee_bps: float, slip_bps: float) -> tuple[float, dict]:
    fee_rate, slip_rate = fee_bps / 10_000, slip_bps / 10_000
    effective = raw * (1 - slip_rate)
    gross, fee, slippage = qty * effective, qty * effective * fee_rate, qty * raw * slip_rate
    cash += gross - fee
    return cash, {"side": "SELL", "raw_notional": qty * raw, "fee": fee, "slippage": slippage,
        "fill_price": effective}


def backtest(schedule: list[dict], targets: dict[int, bool | None], final_exit_ts: int | None,
             *, fee_bps: float, slip_bps: float, recipe: dict, collect_curve: bool = False) -> dict:
    if final_exit_ts is None:
        return {"metrics": _empty_metrics(), "curve": [], "daily_returns": []}
    steps = [x for x in schedule if x["exec_ts"] <= final_exit_ts and x.get("mark_open") is not None]
    if not steps or steps[-1]["exec_ts"] != final_exit_ts:
        raise ValueError("The frozen final liquidation open is missing from verified data.")
    initial = float(recipe["initial_usdt_per_fold"])
    cash, qty, entry_equity = initial, 0.0, 0.0
    total_fees = total_slippage = turnover = 0.0
    fills, roundtrips, halts = 0, 0, 0
    max_dd = 0.0
    peak = initial
    halted = False
    curve = [{"ts": steps[0]["ts"], "equity": initial}]
    returns = []
    previous_equity = initial
    previous_ts = None
    previous_exposure = 0.0
    in_market_days = exposure_days = observed_days = 0.0
    exposure_samples = []
    ledger = []

    def mark(raw: float) -> float:
        return cash + qty * raw

    def liquidation(raw: float) -> float:
        if qty <= 0:
            return cash
        net = raw * (1 - slip_bps / 10_000)
        return cash + qty * net * (1 - fee_bps / 10_000)

    for step in steps:
        ts, raw = step["exec_ts"], step["mark_open"]
        if previous_ts is not None and ts > previous_ts:
            delta = (ts - previous_ts) / DAY
            observed_days += delta
            if previous_exposure > 0:
                in_market_days += delta
                exposure_days += previous_exposure * delta
        current = mark(raw)
        peak = max(peak, current)
        dd = max(0.0, 1 - current / peak) if peak else 0.0
        max_dd = max(max_dd, dd)
        liq_dd = max(0.0, 1 - liquidation(raw) / peak) if peak else 0.0
        is_final = ts == final_exit_ts
        if not halted and liq_dd >= float(recipe["drawdown_halt"]):
            if qty > 0:
                cash, fill = _sell(cash, qty, raw, fee_bps, slip_bps)
                fills += 1; roundtrips += 1
                total_fees += fill["fee"]; total_slippage += fill["slippage"]
                turnover += fill["raw_notional"] / initial
                ledger.append({"ts": ts, **fill, "reason": "drawdown_halt"})
                qty = 0.0
            halted = True; halts += 1
        if not halted and not is_final:
            desired = targets.get(step["ts"])
            if desired is True and qty == 0:
                cash, qty, fill = _buy(cash, raw, fee_bps, slip_bps,
                    float(recipe["entry_budget_fraction_including_fees"]))
                fills += 1; total_fees += fill["fee"]; total_slippage += fill["slippage"]
                turnover += fill["raw_notional"] / initial
                entry_equity = fill["raw_notional"] + fill["fee"] + fill["slippage"]
                ledger.append({"ts": ts, **fill, "reason": "target_active"})
            elif desired is False and qty > 0:
                cash, fill = _sell(cash, qty, raw, fee_bps, slip_bps)
                fills += 1; roundtrips += 1
                total_fees += fill["fee"]; total_slippage += fill["slippage"]
                turnover += fill["raw_notional"] / initial
                ledger.append({"ts": ts, **fill, "reason": "target_inactive"})
                qty = 0.0
        if is_final and qty > 0:
            cash, fill = _sell(cash, qty, raw, fee_bps, slip_bps)
            fills += 1; roundtrips += 1
            total_fees += fill["fee"]; total_slippage += fill["slippage"]
            turnover += fill["raw_notional"] / initial
            ledger.append({"ts": ts, **fill, "reason": "fold_liquidation"})
            qty = 0.0
        equity = cash + qty * raw
        peak = max(peak, equity)
        dd = max(0.0, 1 - equity / peak) if peak else 0.0
        max_dd = max(max_dd, dd)
        day_return = equity / previous_equity - 1 if previous_equity else 0.0
        returns.append({"ts": ts, "return": day_return})
        previous_equity = equity
        previous_ts = ts
        previous_exposure = (qty * raw / equity) if qty > 0 and equity > 0 else 0.0
        exposure_samples.append(previous_exposure)
        if collect_curve:
            curve.append({"ts": ts, "equity": equity})
    metrics = {"initial_usdt": initial, "ending_usdt": cash, "net_return_pct": (cash / initial - 1) * 100,
        "max_drawdown_pct": max_dd * 100, "fills": fills, "roundtrips": roundtrips,
        "turnover_x_initial_capital": turnover, "fees_usdt": total_fees, "slippage_usdt": total_slippage,
        "time_in_market_pct": (in_market_days / observed_days * 100) if observed_days else 0.0,
        "capital_exposure_pct": (exposure_days / observed_days * 100) if observed_days else 0.0,
        "mean_marked_exposure_pct": _mean(exposure_samples) * 100,
        "drawdown_halts": halts, "halted": halted,
        "interpretation": "no trades; cash result with no evidence of predictive edge" if roundtrips == 0 and fills == 0
            else "cash after risk halt" if roundtrips == 0 and halted else "historical simulation"}
    return {"metrics": metrics, "curve": curve, "daily_returns": returns, "ledger": ledger}


def _empty_metrics() -> dict:
    return {"initial_usdt": INITIAL, "ending_usdt": INITIAL, "net_return_pct": 0.0,
        "max_drawdown_pct": 0.0, "fills": 0, "roundtrips": 0, "turnover_x_initial_capital": 0.0,
        "fees_usdt": 0.0, "slippage_usdt": 0.0, "time_in_market_pct": 0.0,
        "capital_exposure_pct": 0.0, "mean_marked_exposure_pct": 0.0, "drawdown_halts": 0,
        "halted": False, "interpretation": "no eligible observations; no edge evidence"}


def _quantile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    pos = (len(values) - 1) * fraction
    lower, upper = int(math.floor(pos)), int(math.ceil(pos))
    return values[lower] if lower == upper else values[lower] * (upper - pos) + values[upper] * (pos - lower)


def _block_sample(values: list[float], block: int, rng: random.Random) -> list[float]:
    n = len(values)
    out = []
    while len(out) < n:
        start = rng.randrange(n)
        out.extend(values[(start + i) % n] for i in range(min(block, n - len(out))))
    return out[:n]


def uncertainty(model_returns: list[dict], buyhold_returns: list[dict], recipe: dict) -> dict:
    by_buy = {x["ts"]: x["return"] for x in buyhold_returns}
    paired = [(x["return"], by_buy[x["ts"]]) for x in model_returns if x["ts"] in by_buy]
    if not paired:
        return {"state": "unavailable", "reason": "no aligned observed daily opens", "replicates": 0}
    model = [x[0] for x in paired]; hold = [x[1] for x in paired]
    diffs = [a - b for a, b in paired]
    block = int(recipe["block_days"]); reps = int(recipe["replicates"]); rng = random.Random(int(recipe["seed"]))
    model_samples, hold_samples, excess_samples = [], [], []
    for _ in range(reps):
        sample = _block_sample(list(zip(model, hold, diffs)), block, rng)
        model_samples.append(math.prod(1 + x[0] for x in sample) - 1)
        hold_samples.append(math.prod(1 + x[1] for x in sample) - 1)
        excess_samples.append(_mean([x[2] for x in sample]) * 10_000)
    inactive = all(abs(x) < 1e-14 for x in model)
    return {"state": "descriptive_historical_interval", "observations": len(paired), "block_days": block,
        "replicates": reps, "seed": int(recipe["seed"]),
        "model_compounded_return_pct_ci95": [_quantile(model_samples, .025) * 100, _quantile(model_samples, .975) * 100],
        "buy_hold_compounded_return_pct_ci95": [_quantile(hold_samples, .025) * 100, _quantile(hold_samples, .975) * 100],
        "mean_paired_excess_bps": _mean(diffs) * 10_000,
        "paired_excess_bps_ci95": [_quantile(excess_samples, .025), _quantile(excess_samples, .975)],
        "inactive_zero_volatility_no_edge_evidence": inactive,
        "interpretation": "Conditional resampling of historical daily open-to-open returns; gaps can span more than one calendar day. Descriptive only, not proof of an edge or future performance."}


def classification_diagnostics(rows: list[dict], probs: list[float], model: dict) -> dict:
    if len(rows) != len(probs):
        raise ValueError("Prediction count does not match eligible rows.")
    if not rows:
        return {"eligible_rows": 0, "state": "unavailable", "reliability_bins": []}
    labels = [x["label"] for x in rows]
    majority = int(model["majority_label"])
    baseline_probability = min(1 - 1e-15, max(1e-15, float(model["train_positive_rate"])))
    clipped = [min(1 - 1e-15, max(1e-15, p)) for p in probs]
    brier = _mean([(p - y) ** 2 for p, y in zip(probs, labels)])
    logloss = _mean([-(y * math.log(p) + (1 - y) * math.log(1 - p)) for p, y in zip(clipped, labels)])
    bin_rows = []
    for i in range(10):
        indices = [j for j, p in enumerate(probs) if (i / 10 <= p < (i + 1) / 10) or (i == 9 and p == 1)]
        bin_rows.append({"bin": f"[{i/10:.1f},{(i+1)/10:.1f}{']' if i == 9 else ')'}",
            "count": len(indices), "mean_probability": _mean([probs[j] for j in indices]) if indices else None,
            "observed_positive_share": _mean([labels[j] for j in indices]) if indices else None})
    return {"eligible_rows": len(rows), "positives": sum(labels), "brier_score": brier, "log_loss": logloss,
        "accuracy_at_0_5": _mean([float((p >= .5) == bool(y)) for p, y in zip(probs, labels)]),
        "majority_accuracy": _mean([float(majority == y) for y in labels]),
        "train_majority_baseline": {"label": majority,
            "accuracy": _mean([float(majority == y) for y in labels]),
            "constant_train_positive_rate": float(model["train_positive_rate"]),
            "brier_score": _mean([(baseline_probability - y) ** 2 for y in labels]),
            "log_loss": _mean([-(y * math.log(baseline_probability) +
                (1 - y) * math.log(1 - baseline_probability)) for y in labels])},
        "reliability_bins": bin_rows}


def _quality(days: list[dict], start: int, end: int) -> dict:
    selected = [x for x in days if start <= x["ts"] < end]
    decision_exclusions, label_exclusions = {}, {}
    for row in selected:
        if row.get("decision") is None:
            reason = row.get("exclusion_reason") or "unknown"
            decision_exclusions[reason] = decision_exclusions.get(reason, 0) + 1
        if row.get("decision") is not None and row.get("row") is None:
            reason = row.get("label_exclusion_reason") or "unknown"
            label_exclusions[reason] = label_exclusions.get(reason, 0) + 1
    return {"calendar_days": len(selected), "feature_eligible_decisions": sum(x.get("decision") is not None for x in selected),
        "supervised_label_rows": sum(x.get("row") is not None for x in selected),
        "excluded_asof_decisions": len(selected) - sum(x.get("decision") is not None for x in selected),
        "excluded_asof_by_reason": decision_exclusions, "unavailable_future_labels": label_exclusions}


def _partition_rows(rows: list[dict], start: int, end: int) -> tuple[list[dict], int]:
    candidates = [x for x in rows if start <= x["ts"] < end]
    retained = [x for x in candidates if x["target_end"] < end]
    return retained, len(candidates) - len(retained)


def _targets(rows: list[dict], probs: list[float], threshold: float) -> dict[int, bool]:
    return {row["ts"]: p >= threshold for row, p in zip(rows, probs)}


def _planned_final_exit(end_exclusive: int) -> int:
    if end_exclusive % DAY:
        raise ValueError("Frozen partition end must align to midnight UTC.")
    return end_exclusive - DAY + BAR


def _require_planned_open(schedule: list[dict], final_exit: int, label: str) -> None:
    if not any(row["exec_ts"] == final_exit and row.get("mark_open") is not None for row in schedule):
        raise ValueError(f"{label} incomplete: the fixed calendar final liquidation open is missing or non-trading.")


def _threshold_search(decisions: list[dict], probs: list[float], schedule: list[dict],
                      final_exit: int, recipe: dict) -> tuple[float, list[dict]]:
    search = []
    cost = next(x for x in recipe["accounting"]["cost_scenarios_bps_per_side"] if x["id"] == "base")
    for threshold in recipe["model"]["threshold_grid"]:
        bt = backtest(schedule, _targets(decisions, probs, float(threshold)), final_exit,
            fee_bps=cost["fee"], slip_bps=cost["spread_slippage"], recipe=recipe["accounting"])
        metrics = bt["metrics"]
        search.append({"threshold": threshold, "validation_net_return_pct": metrics["net_return_pct"],
            "validation_max_drawdown_pct": metrics["max_drawdown_pct"], "validation_roundtrips": metrics["roundtrips"],
            "validation_fills": metrics["fills"]})
    chosen = sorted(search, key=lambda x: (-x["validation_net_return_pct"], x["validation_max_drawdown_pct"], x["threshold"]))[0]
    return float(chosen["threshold"]), search


def _fold_window(fold: dict) -> dict[str, int]:
    return {key: utc(fold[key]) for key in ("train_start", "train_end_exclusive", "validation_start",
        "validation_end_exclusive", "test_start", "test_end_exclusive")}


def _run_fold(name: str, fold: dict, rows: list[dict], daily: list[dict], study_id: str,
              symbol: str, recipe: dict, final: bool = False) -> dict:
    times = _fold_window(fold)
    train, purged_train = _partition_rows(rows, times["train_start"], times["train_end_exclusive"])
    validation, purged_validation = _partition_rows(rows, times["validation_start"], times["validation_end_exclusive"])
    test, purged_test = _partition_rows(rows, times["test_start"], times["test_end_exclusive"])
    if not train:
        raise ValueError(f"{symbol} {name}: frozen training window has no eligible rows.")
    validation_exit = _planned_final_exit(times["validation_end_exclusive"])
    validation_schedule = [x for x in daily if times["validation_start"] <= x["ts"] < times["validation_end_exclusive"]]
    _require_planned_open(validation_schedule, validation_exit, f"{symbol} {name} validation")
    test_schedule = [x for x in daily if times["test_start"] <= x["ts"] < times["test_end_exclusive"]]
    final_exit = _planned_final_exit(times["test_end_exclusive"])
    _require_planned_open(test_schedule, final_exit, f"{symbol} {name} test")
    validation_decisions = [x["decision"] for x in validation_schedule
        if x.get("decision") is not None and x["exec_ts"] < validation_exit]
    test_decisions = [x["decision"] for x in test_schedule
        if x.get("decision") is not None and x["exec_ts"] < final_exit]
    model = train_model(train, recipe["model"])
    val_probs = classify(validation_decisions, model)
    threshold, candidates = _threshold_search(validation_decisions, val_probs,
        validation_schedule, validation_exit, recipe)
    model_snapshot = {"fit_id": model["fit_id"], "means": model["means"], "scales": model["scales"],
        "weights": model["weights"], "epochs": model["epochs"], "learning_rate": model["learning_rate"],
        "regularization": model["regularization"], "train_rows": model["train_rows"],
        "train_positive_rate": model["train_positive_rate"], "majority_label": model["majority_label"],
        "selected_threshold": threshold}
    model_snapshot["snapshot_sha256"] = sha(json.dumps(model_snapshot, sort_keys=True, separators=(",", ":")).encode())
    final_receipt_path = Path(recipe["_study_dir"]) / f"selection-{symbol}-{name}.json"
    if final:
        frozen = {"study_id": study_id, "protocol_sha256": recipe["_protocol_sha256"],
            "dataset_sha256": recipe["_dataset_sha256"], "symbol": symbol, "fold": name,
            "model": model_snapshot, "threshold_candidates": candidates,
            "selected_at_utc": iso(), "selection_basis": "validation base-cost return, then lower drawdown, then lower threshold",
            "status": "frozen_before_final_test_scoring"}
        if final_receipt_path.exists():
            previous = json.loads(final_receipt_path.read_text())
            if previous.get("model", {}).get("snapshot_sha256") != model_snapshot["snapshot_sha256"]:
                raise ValueError("Final historical test selection receipt differs; refusing a second selected model.")
            frozen["selected_at_utc"] = previous.get("selected_at_utc")
            frozen["final_test_first_consumed_at_utc"] = previous.get("final_test_first_consumed_at_utc")
        else:
            frozen["final_test_first_consumed_at_utc"] = iso()
        write_json(final_receipt_path, frozen)

    test_decision_probs = classify(test_decisions, model)
    test_targets = _targets(test_decisions, test_decision_probs, threshold)
    momentum_targets = {row["ts"]: row["features"][2] > 0 for row in test_decisions}
    decision_prob_by_ts = {row["ts"]: p for row, p in zip(test_decisions, test_decision_probs)}
    test_probs = [decision_prob_by_ts[row["ts"]] for row in test if row["ts"] in decision_prob_by_ts]
    test_labeled_rows = [row for row in test if row["ts"] in decision_prob_by_ts]
    scenarios = {}
    bootstrap = None
    for cost in recipe["accounting"]["cost_scenarios_bps_per_side"]:
        actions = {"model": test_targets, "momentum": momentum_targets,
            "buy_hold": {x["ts"]: True for x in test_schedule if x["exec_ts"] < (final_exit or 0)},
            "cash": {x["ts"]: False for x in test_schedule}}
        results = {name_: backtest(test_schedule, target, final_exit,
            fee_bps=cost["fee"], slip_bps=cost["spread_slippage"], recipe=recipe["accounting"],
            collect_curve=(final or name == "2025" and cost["id"] == "base"))
            for name_, target in actions.items()}
        scenarios[cost["id"]] = {strategy: result["metrics"] for strategy, result in results.items()}
        if cost["id"] == "base":
            bootstrap = uncertainty(results["model"]["daily_returns"], results["buy_hold"]["daily_returns"], recipe["uncertainty"])
            scenario_series = results

    labels_by_ts = {row["ts"]: row["label"] for row in test}
    predictions = [{"ts": row["ts"], "probability": prob, "label": labels_by_ts.get(row["ts"]),
        "target": test_targets[row["ts"]]} for row, prob in zip(test_decisions, test_decision_probs)]
    return {"name": name, "dates_utc": {"train": [fold["train_start"], fold["train_end_exclusive"]],
            "validation": [fold["validation_start"], fold["validation_end_exclusive"]],
            "test": [fold["test_start"], fold["test_end_exclusive"]]},
        "counts": {"train_rows": len(train), "validation_rows": len(validation), "test_rows": len(test),
            "validation_feature_decisions": len(validation_decisions), "test_feature_decisions": len(test_decisions),
            "purged_train": purged_train, "purged_validation": purged_validation, "purged_test_tail": purged_test,
            "train_quality": _quality(daily, times["train_start"], times["train_end_exclusive"]),
            "validation_quality": _quality(daily, times["validation_start"], times["validation_end_exclusive"]),
            "test_quality": _quality(daily, times["test_start"], times["test_end_exclusive"])},
        "fit": model_snapshot, "selection": {"threshold": threshold, "criterion": "Validation base-cost net return; tie-break lower max drawdown then lower threshold.", "all_candidates": candidates},
        "test": {"label": "reserved historical test, retrospective", "planned_final_exit_utc": iso(final_exit),
            "final_test_first_consumed_at_utc":
                json.loads(final_receipt_path.read_text()).get("final_test_first_consumed_at_utc") if final else None,
            "threshold_frozen_before_test": True, "cost_scenarios": scenarios,
            "classification": classification_diagnostics(test_labeled_rows, test_probs, model), "uncertainty_base_cost": bootstrap,
            "probability_summary": {"feature_eligible_decisions": len(test_decisions), "supervised_label_rows": len(test_labeled_rows),
                "min": min(test_decision_probs) if test_decision_probs else None,
                "max": max(test_decision_probs) if test_decision_probs else None, "active_predictions": sum(test_targets.values())},
            "base_cost_curve": {strategy: result["curve"] for strategy, result in scenario_series.items()}
                if final or name == "2025" else None,
            "base_cost_daily_returns": {strategy: result["daily_returns"] for strategy, result in scenario_series.items()}
                if final else None,
            "test_predictions": predictions}}


def _ledger_entry(result: dict) -> dict:
    fits = []
    for symbol, study in result["symbols"].items():
        for fold in study["folds"]:
            fits.append({"symbol": symbol, "fold": fold["name"], "fit_id": fold["fit"]["fit_id"],
                "model_snapshot_sha256": fold["fit"]["snapshot_sha256"], "train_rows": fold["counts"]["train_rows"],
                "validation_rows": fold["counts"]["validation_rows"], "selected_threshold": fold["selection"]["threshold"],
                "threshold_candidates": fold["selection"]["all_candidates"]})
    return {"study_id": result["study_id"], "protocol_sha256": result["protocol_sha256"],
        "dataset_sha256": {key: value["dataset"]["canonical_sha256"] for key, value in result["symbols"].items()},
        "status": "complete_historical_test_consumed", "first_final_test_consumed_at_utc": result["final_test_first_consumed_at_utc"],
        "fit_and_selection_trials": fits, "created_at_utc": result["created_at_utc"]}


def _append_ledger(path: Path, entry: dict) -> None:
    existing = []
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                existing.append(json.loads(line))
    for record in existing:
        if record.get("study_id") == entry["study_id"]:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write((json.dumps(entry, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode())
        stream.flush()
        import os
        os.fsync(stream.fileno())


def run_study(store: ArchiveStore, progress=None) -> dict:
    recipe, protocol_sha = protocol()
    datasets = {}
    for symbol in recipe["dataset"]["symbols"]:
        bars, meta = store.load(symbol)
        datasets[symbol] = (bars, meta)
    digest_material = {symbol: meta["canonical_sha256"] for symbol, (_, meta) in datasets.items()}
    study_id = "v4-" + sha(json.dumps({"protocol": protocol_sha, "datasets": digest_material}, sort_keys=True).encode())[:20]
    studies_dir = store.root / "studies"
    studies_dir.mkdir(parents=True, exist_ok=True)
    result_path = studies_dir / f"{study_id}.json"
    result_sha_path = result_path.with_suffix(".json.sha256")
    ledger_path = store.root / "trial-ledger.jsonl"
    if result_path.exists():
        raw = result_path.read_bytes()
        if not result_sha_path.exists() or result_sha_path.read_text().strip() != sha(raw):
            raise ValueError("Saved study checksum is missing or invalid; refusing to replace historical results.")
        saved = json.loads(raw)
        if saved.get("study_id") != study_id or saved.get("protocol_sha256") != protocol_sha:
            raise ValueError("Saved study identity differs from the frozen protocol or dataset.")
        _append_ledger(ledger_path, _ledger_entry(saved))
        saved["result_provenance"] = "reused_saved_result"
        return saved
    if ledger_path.exists():
        for line in ledger_path.read_text().splitlines():
            if line.strip() and json.loads(line).get("study_id") == study_id:
                raise ValueError("Historical test was already consumed but the saved result is missing; refusing a silent refit.")

    study_dir = studies_dir / study_id
    study_dir.mkdir(parents=True, exist_ok=True)
    output = {"study_id": study_id, "protocol_sha256": protocol_sha, "protocol_id": recipe["id"],
        "status": "complete", "result_provenance": "newly_evaluated", "created_at_utc": iso(),
        "license": store.manifest().get("terms", {}), "source": recipe["dataset"]["source"],
        "quote_asset": recipe["dataset"]["quote_asset"], "bar_interval": recipe["dataset"]["bar_interval"],
        "decision_clock": recipe["decision"]["frequency"], "final_test_first_consumed_at_utc": None, "symbols": {}}
    for symbol_index, symbol in enumerate(recipe["dataset"]["symbols"]):
        bars, meta = datasets[symbol]
        rows, daily = build_daily_views(bars)
        fold_recipe = {**recipe, "_study_dir": str(study_dir), "_protocol_sha256": protocol_sha,
            "_dataset_sha256": meta["canonical_sha256"]}
        symbol_result = {"symbol": symbol, "dataset": {key: value for key, value in meta.items() if key != "missing_slots"},
            "dataset_gaps": {"missing_4h_slots": len(meta.get("missing_slots", [])),
                "missing_slots_utc": [iso(ts) for ts in meta.get("missing_slots", [])],
                "rejected_partial_rows": meta.get("rejected_partial_rows", 0),
                "partial_bar_rejections": meta.get("partial_bar_rejections", []),
                "zero_volume_bars": meta.get("zero_volume_bars", 0)},
            "folds": []}
        for fold in recipe["partitions"]["development_folds"]:
            name = fold["name"]
            if progress: progress({"phase": "evaluate", "symbol": symbol, "fold": name, "status": "fitting_and_scoring"})
            result = _run_fold(name, fold, rows, daily, study_id, symbol, fold_recipe, final=False)
            symbol_result["folds"].append(result)
        final_fold = recipe["partitions"]["final"]
        if progress: progress({"phase": "evaluate", "symbol": symbol, "fold": final_fold["name"], "status": "freezing_selection_then_scoring_reserved_test"})
        final_result = _run_fold(final_fold["name"], final_fold, rows, daily, study_id, symbol, fold_recipe, final=True)
        symbol_result["folds"].append(final_result)
        final_time = final_result["test"]["final_test_first_consumed_at_utc"]
        if output["final_test_first_consumed_at_utc"] is None or final_time < output["final_test_first_consumed_at_utc"]:
            output["final_test_first_consumed_at_utc"] = final_time
        output["symbols"][symbol] = symbol_result
    result = json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    atomic(result_path, result)
    atomic(result_sha_path, (sha(result) + "\n").encode())
    _append_ledger(ledger_path, _ledger_entry(output))
    if progress: progress({"phase": "complete", "study_id": study_id, "result_sha256": sha(result)})
    return output

