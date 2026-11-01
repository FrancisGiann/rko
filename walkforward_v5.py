"""Fixed, exploratory common-risk comparison over already-inspected V4 data.

This module never fits a model.  Its only ML inputs are the checksum-verified
saved V4 out-of-sample target maps.  All new rules use past completed daily
closes and share the same explicit cost-aware position manager.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from pathlib import Path

from walkforward import BAR, DAY, build_daily_views

INITIAL = 10_000.0
MAX_WEIGHT = 0.50
HALT = 0.10
VOL_FLOOR = 0.10
VOL_TARGET = 0.10
REBAND = 0.025
TRADING_DAYS = 365
CANDIDATES = (
    "cash", "passive", "momentum_7d", "frozen_v4_logistic", "sma20_100",
    "close_breakout_20_10", "trend_gated_v4",
)
COSTS = (("base", 10.0, 5.0), ("higher", 25.0, 10.0), ("stress", 50.0, 25.0))


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_verified_inputs(root: Path = Path("research_data/v4")) -> tuple[dict, dict, dict]:
    """Load the immutable V4 result and canonical datasets, verifying every hash."""
    result_path = root / "studies/v4-136531a33340c75d6cdf.json"
    result_sha = result_path.with_suffix(".json.sha256")
    raw_result = result_path.read_bytes()
    expected_result = result_sha.read_text().strip()
    if sha(raw_result) != expected_result or expected_result != "2c1fcbc284d32e0abd3dc649a978a016e8a25283317cd8aaabc3a549e9ddae67":
        raise ValueError("Saved V4 result does not match its frozen checksum; refusing V5 replay.")
    saved = json.loads(raw_result)
    if saved.get("study_id") != "v4-136531a33340c75d6cdf" or saved.get("protocol_sha256") != "7f6b05b6edd89ada8dcda0fdd54e2bef702b9a0d8eeea02a8ed8e24bd31f140d":
        raise ValueError("Saved V4 study identity changed; refusing V5 replay.")
    datasets = {}
    for symbol, info in saved["symbols"].items():
        rel = Path(info["dataset"]["file"])
        path = (root / rel).resolve()
        if root.resolve() not in path.parents or path.is_symlink():
            raise ValueError("Unsafe or symlinked V4 dataset path.")
        raw = path.read_bytes()
        if sha(raw) != info["dataset"]["canonical_sha256"]:
            raise ValueError(f"V4 dataset checksum mismatch for {symbol}.")
        datasets[symbol] = json.loads(raw)
    return saved, datasets, {s: x["dataset"]["canonical_sha256"] for s, x in saved["symbols"].items()}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std_population(values: list[float]) -> float:
    mean = _mean(values)
    return math.sqrt(sum((x - mean) ** 2 for x in values) / len(values)) if values else 0.0


def complete_daily_closes(bars: list[dict]) -> list[dict]:
    """Return calendar slots with a close only when all six 4h bars exist.

    Incomplete days remain as timestamp-only slots.  This makes a missing UTC
    close invalidate future indicators instead of silently compressing time.
    """
    if not bars:
        return []
    by_ts = {int(x["ts"]): x for x in bars}
    first = min(by_ts) // DAY * DAY
    last = max(by_ts) // DAY * DAY
    output = []
    for day in range(first, last + 1, DAY):
        records = [by_ts.get(day + i * BAR) for i in range(6)]
        if all(records):
            output.append({"ts": day, "close": float(records[-1]["close"])})
        else:
            output.append({"ts": day})
    return output


def active_weight(daily_volatility: float | None) -> float | None:
    """Return frozen target weight, or None when the as-of volatility window is incomplete."""
    if daily_volatility is None or not math.isfinite(daily_volatility) or daily_volatility < 0:
        return None
    annualized = daily_volatility * math.sqrt(TRADING_DAYS)
    return min(MAX_WEIGHT, VOL_TARGET / max(annualized, VOL_FLOOR))


def price_signal_inputs(daily: list[dict]) -> dict[int, dict]:
    """Build past-only indicators with their own frozen lookback requirements.

    Decisions at D 00:00 use complete daily records for D-1 and earlier. A
    missing old close invalidates a 100-day trend value without disabling a
    still-valid 20-day risk estimate, 7-day momentum, or 20/10 breakout.
    Missing UTC days are never filled.
    """
    by_ts = {row["ts"]: row for row in daily}

    def close_window(day: int, count: int) -> list[float] | None:
        closes = []
        for offset in range(1, count + 1):
            row = by_ts.get(day - offset * DAY)
            if row is None or "close" not in row:
                return None
            close = float(row["close"])
            if not math.isfinite(close) or close <= 0:
                return None
            closes.append(close)
        return closes

    output = {}
    for decision_day in sorted(by_ts):
        vol_closes = close_window(decision_day, 21)
        vol = (_std_population([vol_closes[i] / vol_closes[i + 1] - 1 for i in range(20)])
               if vol_closes else None)
        momentum_closes = close_window(decision_day, 8)
        breakout_closes = close_window(decision_day, 21)
        trend_closes = close_window(decision_day, 100)
        latest = vol_closes[0] if vol_closes else None
        output[decision_day] = {
            "weight": active_weight(vol),
            "return_7d": latest / momentum_closes[7] - 1 if latest is not None and momentum_closes else None,
            "trend": (latest > _mean(trend_closes) and _mean(vol_closes[:20]) > _mean(trend_closes))
                if latest is not None and trend_closes and vol_closes else None,
            "breakout_entry": latest > max(breakout_closes[1:21]) if latest is not None and breakout_closes else None,
            "breakout_exit": latest < min(breakout_closes[1:11]) if latest is not None and breakout_closes else None,
        }
    return output


def candidate_targets(daily: list[dict], schedule: list[dict],
                      saved_predictions: list[dict], start: int, end: int) -> dict[str, dict[int, float | None]]:
    """Create fixed desired-weight maps without consulting returns or labels."""
    inputs = price_signal_inputs(daily)
    v4 = {int(row["ts"]): bool(row["target"]) for row in saved_predictions}
    targets: dict[str, dict[int, float | None]] = {key: {} for key in CANDIDATES}
    breakout_active = False
    for row in schedule:
        day = int(row["ts"])
        if not start <= day < end:
            continue
        feat = inputs.get(day, {})
        weight = feat.get("weight")
        targets["cash"][day] = 0.0
        targets["passive"][day] = weight
        momentum = feat.get("return_7d")
        targets["momentum_7d"][day] = (0.0 if momentum <= 0 else weight) if momentum is not None and (momentum <= 0 or weight is not None) else None
        trend = feat.get("trend")
        targets["sma20_100"][day] = (0.0 if trend is False else weight) if trend is not None and (trend is False or weight is not None) else None
        breakout_entry, breakout_exit = feat.get("breakout_entry"), feat.get("breakout_exit")
        if breakout_entry is True:
            breakout_active = True
        elif breakout_exit is True:
            breakout_active = False
        targets["close_breakout_20_10"][day] = (0.0 if not breakout_active else weight) if breakout_entry is not None and breakout_exit is not None and (not breakout_active or weight is not None) else None
        v4_target = v4.get(day)
        targets["frozen_v4_logistic"][day] = (weight if v4_target else 0.0) if v4_target is not None and (not v4_target or weight is not None) else None
        if v4_target is False or trend is False:
            targets["trend_gated_v4"][day] = 0.0
        elif v4_target is True and trend is True and weight is not None:
            targets["trend_gated_v4"][day] = weight
        else:
            targets["trend_gated_v4"][day] = None
    return targets


def solve_trade(current_cash: float, current_qty: float, raw_price: float,
                desired_weight: float, fee_bps: float, slip_bps: float) -> dict | None:
    """Return exact quantity change needed for desired post-cost marked weight."""
    if not (all(math.isfinite(x) for x in (current_cash, current_qty, raw_price, desired_weight, fee_bps, slip_bps))
            and current_cash >= -1e-8 and current_qty >= 0 and raw_price > 0
            and 0 <= desired_weight <= MAX_WEIGHT and fee_bps >= 0 and slip_bps >= 0):
        raise ValueError("Invalid inputs to the common risk trade solver.")
    equity = current_cash + current_qty * raw_price
    asset = current_qty * raw_price
    current_weight = asset / equity if equity > 0 else 0.0
    if desired_weight <= 0:
        delta_notional = -asset
    elif desired_weight > current_weight:
        cost_rate = (1 + slip_bps / 10_000) * (1 + fee_bps / 10_000) - 1
        delta_notional = (desired_weight * equity - asset) / (1 + desired_weight * cost_rate)
    else:
        cost_rate = 1 - (1 - slip_bps / 10_000) * (1 - fee_bps / 10_000)
        delta_notional = -(asset - desired_weight * equity) / (1 - desired_weight * cost_rate)
    if abs(delta_notional) < 1e-8 and desired_weight > 0:
        return None
    if delta_notional > 0:
        quantity = delta_notional / raw_price
        executed = delta_notional * (1 + slip_bps / 10_000)
        fee = executed * fee_bps / 10_000
        slippage = delta_notional * slip_bps / 10_000
        cash_after = current_cash - executed - fee
        qty_after = current_qty + quantity
        side = "BUY"
    else:
        delta_notional = min(-delta_notional, asset)
        quantity = delta_notional / raw_price
        executed = delta_notional * (1 - slip_bps / 10_000)
        fee = executed * fee_bps / 10_000
        slippage = delta_notional * slip_bps / 10_000
        cash_after = current_cash + executed - fee
        qty_after = current_qty - quantity
        side = "SELL"
    if cash_after < -1e-6 or qty_after < -1e-12:
        raise AssertionError("Common risk manager produced negative cash or position.")
    cash_after, qty_after = max(0.0, cash_after), max(0.0, qty_after)
    marked_equity = cash_after + qty_after * raw_price
    actual_weight = qty_after * raw_price / marked_equity if marked_equity else 0.0
    return {"side": side, "quantity": quantity, "raw_notional": abs(delta_notional),
        "fee": fee, "slippage": slippage, "cash": cash_after, "qty": qty_after,
        "post_trade_weight": actual_weight}


def _metrics_empty() -> dict:
    return {"initial_usdt": INITIAL, "ending_usdt": INITIAL, "net_return_pct": 0.0,
        "max_drawdown_pct": 0.0, "fills": 0, "roundtrips": 0, "turnover_x_initial_capital": 0.0,
        "fees_usdt": 0.0, "slippage_usdt": 0.0, "time_in_market_pct": 0.0,
        "mean_capital_exposure_pct": 0.0, "max_capital_exposure_pct": 0.0,
        "drawdown_halts": 0, "halted": False, "invalid_signal_days": 0,
        "active_signals_after_halt": 0, "state": "complete"}


def backtest_common(schedule: list[dict], targets: dict[int, float | None], final_exit_ts: int,
                    fee_bps: float, slip_bps: float) -> dict:
    """Simulate one candidate/fold/cost cell with shared risk/execution rules."""
    all_steps = [x for x in schedule if int(x["exec_ts"]) <= final_exit_ts]
    if not all_steps or int(all_steps[-1]["exec_ts"]) != final_exit_ts:
        raise ValueError("The fixed calendar final liquidation open is missing from the dataset.")
    if all_steps[-1].get("mark_open") is None:
        raise ValueError("The fixed calendar final liquidation open has zero volume or is unavailable.")
    steps = [x for x in all_steps if x.get("mark_open") is not None]
    if steps[-1] is not all_steps[-1]:
        raise ValueError("No observed open exists at the fixed fold endpoint.")
    cash, qty = INITIAL, 0.0
    fees = slippage = turnover = 0.0
    fills = episodes = completed_episodes = halts = 0
    peak = INITIAL
    max_dd = 0.0
    halted = False
    invalid_signal_days = post_halt_active = 0
    previous_ts = None
    previous_exposure = 0.0
    elapsed = invested_days = exposure_days = 0.0
    exposures = []
    ledger = []

    for index, step in enumerate(steps):
        ts, day = int(step["exec_ts"]), int(step["ts"])
        raw = float(step["mark_open"])
        if previous_ts is not None:
            dt = (ts - previous_ts) / DAY
            if dt > 0:
                elapsed += dt
                if previous_exposure > 0:
                    invested_days += dt
                    exposure_days += previous_exposure * dt
        liquidation_equity = cash + qty * raw * (1 - slip_bps / 10_000) * (1 - fee_bps / 10_000)
        peak = max(peak, liquidation_equity)
        max_dd = max(max_dd, max(0.0, 1 - liquidation_equity / peak))
        liquidation_dd = max(0.0, 1 - liquidation_equity / peak) if peak else 0.0
        is_final = ts == final_exit_ts
        was_halted = halted
        if not halted and liquidation_dd >= HALT:
            halts += 1
            halted = True
            if qty > 0:
                order = solve_trade(cash, qty, raw, 0.0, fee_bps, slip_bps)
                assert order and order["side"] == "SELL"
                cash, qty = order["cash"], order["qty"]
                fees += order["fee"]; slippage += order["slippage"]
                turnover += order["raw_notional"] / INITIAL; fills += 1; completed_episodes += 1
                ledger.append({"ts": ts, "side": "SELL", "reason": "drawdown_halt", **order})
        desired = targets.get(day)
        if was_halted and desired is not None and desired > 0:
            post_halt_active += 1
        if not halted and not is_final:
            marked_equity = cash + qty * raw
            current_weight = qty * raw / marked_equity if marked_equity > 0 else 0.0
            must_trim = current_weight > MAX_WEIGHT + 1e-12
            if must_trim or (desired is not None and (
                    (desired <= 0 and qty > 0) or abs(current_weight - float(desired)) >= REBAND)):
                before_qty = qty
                target_weight = min(MAX_WEIGHT, max(0.0, float(desired))) if desired is not None else MAX_WEIGHT
                order = solve_trade(cash, qty, raw, target_weight, fee_bps, slip_bps)
                if order:
                    cash, qty = order["cash"], order["qty"]
                    fees += order["fee"]; slippage += order["slippage"]
                    turnover += order["raw_notional"] / INITIAL; fills += 1
                    if before_qty <= 1e-12 and qty > 1e-12:
                        episodes += 1
                    if before_qty > 1e-12 and qty <= 1e-12:
                        completed_episodes += 1
                    reason = "max_weight_cap_trim" if must_trim else "target_rebalance"
                    ledger.append({"ts": ts, "side": order["side"], "reason": reason, **order})
        if not is_final and desired is None:
            invalid_signal_days += 1
        # The final daily signal is intentionally ignored; close at the exact
        # predeclared endpoint without choosing a later convenient open.
        if is_final and qty > 0:
            order = solve_trade(cash, qty, raw, 0.0, fee_bps, slip_bps)
            assert order and order["side"] == "SELL"
            cash, qty = order["cash"], order["qty"]
            fees += order["fee"]; slippage += order["slippage"]
            turnover += order["raw_notional"] / INITIAL; fills += 1; completed_episodes += 1
            ledger.append({"ts": ts, "side": "SELL", "reason": "fixed_fold_liquidation", **order})
        marked = cash + qty * raw * (1 - slip_bps / 10_000) * (1 - fee_bps / 10_000)
        peak = max(peak, marked)
        max_dd = max(max_dd, max(0.0, 1 - marked / peak))
        marked_equity = cash + qty * raw
        exposure = qty * raw / marked_equity if qty > 0 and marked_equity > 0 else 0.0
        exposures.append(exposure)
        previous_exposure, previous_ts = exposure, ts
    if qty > 1e-10:
        raise AssertionError("Candidate remained exposed after the fixed fold endpoint.")
    metrics = _metrics_empty()
    metrics.update({"ending_usdt": cash, "net_return_pct": (cash / INITIAL - 1) * 100,
        "max_drawdown_pct": max_dd * 100, "fills": fills, "roundtrips": completed_episodes,
        "entries": episodes, "turnover_x_initial_capital": turnover, "fees_usdt": fees,
        "slippage_usdt": slippage, "time_in_market_pct": invested_days / elapsed * 100 if elapsed else 0.0,
        "mean_capital_exposure_pct": exposure_days / elapsed * 100 if elapsed else 0.0,
        "max_capital_exposure_pct": max(exposures, default=0.0) * 100, "drawdown_halts": halts,
        "halted": halted, "invalid_signal_days": invalid_signal_days,
        "active_signals_after_halt": post_halt_active})
    return {"metrics": metrics, "ledger": ledger}


def fold_bounds(fold: dict) -> tuple[int, int, int]:
    from walkforward import _planned_final_exit, utc
    start = utc(fold["dates_utc"]["test"][0] + "T00:00:00Z")
    end = utc(fold["dates_utc"]["test"][1] + "T00:00:00Z")
    return start, end, _planned_final_exit(end)


def result_cells() -> list[tuple[str, str, str, str, float, float]]:
    """Neutral fixed-order 210-cell grid."""
    return [(candidate, symbol, fold, cost_id, fee, slip)
        for candidate in CANDIDATES for symbol in ("BTCUSDT", "ETHUSDT")
        for fold in ("2022", "2023", "2024", "2025", "final_2026")
        for cost_id, fee, slip in COSTS]


def build_prepared_data(saved: dict, datasets: dict) -> dict:
    """Build all fold schedules and target maps before inspecting any V5 returns."""
    prepared = {}
    for symbol, bars in datasets.items():
        _, schedule_all = build_daily_views(bars)
        daily = complete_daily_closes(bars)
        by_fold = {}
        for fold in saved["symbols"][symbol]["folds"]:
            start, end, final_exit = fold_bounds(fold)
            # Keep every calendar schedule row, including dates whose execution
            # bar is absent; target eligibility depends on past closes only.
            schedule = [row for row in schedule_all if start <= int(row["ts"]) < end]
            targets = candidate_targets(daily, schedule, fold["test"]["test_predictions"], start, end)
            by_fold[fold["name"]] = {"start": start, "end": end, "final_exit_ts": final_exit,
                "schedule": schedule, "targets": targets, "fixed_exit_utc": fold["test"]["planned_final_exit_utc"]}
        prepared[symbol] = by_fold
    return prepared


def score_cell(prepared: dict, candidate: str, symbol: str, fold_name: str,
               cost_id: str, fee: float, slip: float) -> dict:
    data = prepared[symbol][fold_name]
    outcome = backtest_common(data["schedule"], data["targets"][candidate], data["final_exit_ts"], fee, slip)
    return {"candidate": candidate, "symbol": symbol, "fold": fold_name,
        "cost_scenario": cost_id, "fee_bps_per_side": fee, "slippage_bps_per_side": slip,
        "planned_final_exit_utc": data["fixed_exit_utc"], **outcome}

