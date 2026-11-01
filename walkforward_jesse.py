"""Small, fixed replay of two source-derived 4h rules on V4's inspected data.

This module does not fetch data, fit models, or choose parameters. It implements
the mathematical rules recorded in research_protocol_jesse.json and shares the
V5 cost-aware long-only weight solver. Its output is exploratory reuse only.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

import walkforward_v5 as common
from walkforward import BAR, DAY

INITIAL = 10_000.0
MAX_WEIGHT = 0.50
HALT = 0.10
VOL_FLOOR = 0.10
VOL_TARGET = 0.10
REBAND = 0.025
TRADING_DAYS = 365
CANDIDATES = (
    "cash",
    "passive",
    "jesse_donchian20_sma200_long_cash",
    "jesse_sma50_200_long_cash",
)
COSTS = (("base", 10.0, 5.0), ("higher", 25.0, 10.0), ("stress", 50.0, 25.0))
SYMBOLS = ("BTCUSDT", "ETHUSDT")
FOLDS = ("2022", "2023", "2024", "2025", "final_2026")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def iso_utc(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_verified_inputs(root: Path = Path("research_data/v4")) -> tuple[dict, dict, dict]:
    """Read only the frozen V4 result/datasets; verify the immutable identities."""
    root = Path(root).resolve()
    result_path = root / "studies/v4-136531a33340c75d6cdf.json"
    result_sha = result_path.with_suffix(".json.sha256")
    raw_result = result_path.read_bytes()
    result_digest = sha(raw_result)
    if result_digest != result_sha.read_text().strip() or result_digest != "2c1fcbc284d32e0abd3dc649a978a016e8a25283317cd8aaabc3a549e9ddae67":
        raise ValueError("Saved V4 result differs from its frozen SHA-256.")
    saved = json.loads(raw_result)
    if saved.get("study_id") != "v4-136531a33340c75d6cdf" or saved.get("protocol_sha256") != "7f6b05b6edd89ada8dcda0fdd54e2bef702b9a0d8eeea02a8ed8e24bd31f140d":
        raise ValueError("Saved V4 identity differs from the approved Jesse comparison source.")
    datasets = {}
    dataset_hashes = {}
    for symbol in SYMBOLS:
        info = saved["symbols"][symbol]["dataset"]
        path = (root / info["file"]).resolve()
        if root not in path.parents or path.is_symlink() or not path.is_file():
            raise ValueError("Dataset path must be a regular in-root file.")
        raw = path.read_bytes()
        digest = sha(raw)
        if digest != info["canonical_sha256"]:
            raise ValueError(f"V4 normalized dataset checksum mismatch for {symbol}.")
        bars = json.loads(raw)
        validate_bars(bars)
        datasets[symbol] = bars
        dataset_hashes[symbol] = digest
    return saved, datasets, dataset_hashes


def validate_bars(bars: list[dict]) -> None:
    last = None
    for row in bars:
        ts = row.get("ts")
        values = [row.get(k) for k in ("open", "high", "low", "close", "volume")]
        if not isinstance(ts, int) or ts % BAR or (last is not None and ts <= last):
            raise ValueError("Normalized bars must have unique sorted 4h UTC timestamps.")
        if any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
            raise ValueError("Normalized bars must have finite OHLCV values.")
        op, high, low, close, volume = values
        if not (0 < low <= op <= high and low <= close <= high and volume >= 0):
            raise ValueError("Normalized bars contain invalid OHLCV ranges.")
        last = ts


def _bar_window(by_ts: dict[int, dict], end_ts: int, count: int) -> list[dict] | None:
    """A past-only contiguous 4h window, ending at `end_ts` inclusive."""
    out = []
    for offset in range(count):
        row = by_ts.get(end_ts - offset * BAR)
        if row is None:
            return None
        out.append(row)
    return out


def daily_closes(bars: list[dict]) -> dict[int, float]:
    """Return only complete UTC daily closes; gaps are never compressed/fill-forwarded."""
    by_ts = {int(row["ts"]): row for row in bars}
    if not by_ts:
        return {}
    first = min(by_ts) // DAY * DAY
    last = max(by_ts) // DAY * DAY
    result = {}
    for day in range(first, last + 1, DAY):
        six = [by_ts.get(day + slot * BAR) for slot in range(6)]
        if all(row is not None for row in six):
            result[day] = float(six[-1]["close"])
    return result


def daily_volatility(daily: dict[int, float], decision_ts: int) -> float | None:
    """Population SD of 20 returns from 21 daily closes available before decision."""
    latest_day = (decision_ts // DAY) * DAY - DAY
    closes = []
    for offset in range(21):
        day = latest_day - offset * DAY
        close = daily.get(day)
        if close is None or not math.isfinite(close) or close <= 0:
            return None
        closes.append(close)
    returns = [closes[i] / closes[i + 1] - 1 for i in range(20)]
    mean = sum(returns) / len(returns)
    return math.sqrt(sum((x - mean) ** 2 for x in returns) / len(returns))


def active_weight(volatility: float | None) -> float | None:
    if volatility is None or not math.isfinite(volatility) or volatility < 0:
        return None
    annualized = volatility * math.sqrt(TRADING_DAYS)
    return min(MAX_WEIGHT, VOL_TARGET / max(annualized, VOL_FLOOR))


def prepare_symbol(bars: list[dict], fold: dict) -> dict:
    """Make the fixed decision/execution clock for one symbol/fold."""
    dates = fold["dates_utc"]["test"]
    from walkforward import utc
    start = utc(dates[0] + "T00:00:00Z")
    end = utc(dates[1] + "T00:00:00Z")
    final_exit = end - BAR
    return prepare_schedule(bars, start, end, final_exit)


def prepare_schedule(bars: list[dict], start: int, end: int, final_exit: int) -> dict:
    """Prepare a fixed calendar window, useful for both saved folds and fixtures."""
    if start % BAR or end % BAR or final_exit != end - BAR:
        raise ValueError("Decision window must use the fixed end-minus-one-bar liquidation endpoint.")
    by_ts = {int(row["ts"]): row for row in bars}
    all_daily = daily_closes(bars)
    rows = []
    # At boundary t, execute the previous boundary's instruction. The signal
    # being decided at t uses the bar opened at t-4h, and executes at t+4h.
    for decision_ts in range(start, final_exit + 1, BAR):
        execution = by_ts.get(decision_ts)
        signal_bar = by_ts.get(decision_ts - BAR)
        signals = build_asof_signals_from_indexes(by_ts, all_daily, decision_ts)
        rows.append({
            "decision_ts": decision_ts,
            "execution_open_ts": decision_ts,
            "execution_open": float(execution["open"]) if execution and float(execution["volume"]) > 0 else None,
            "execution_volume": float(execution["volume"]) if execution else None,
            "execution_bar_present": execution is not None,
            "signal_bar_ts": decision_ts - BAR,
        "signal_bar_present": signal_bar is not None,
        "planned_fill_ts": decision_ts + BAR,
            "signals": signals,
            "is_final": decision_ts == final_exit,
        })
    if not rows or rows[-1]["execution_open_ts"] != final_exit or rows[-1]["execution_open"] is None:
        raise ValueError("Fixed calendar liquidation open is missing or has zero trade volume.")
    return {"start": start, "end": end, "final_exit_ts": final_exit, "schedule": rows}


def build_asof_signals_from_indexes(by_ts: dict[int, dict], daily: dict[int, float], decision_ts: int) -> dict:
    current_ts = decision_ts - BAR
    current = by_ts.get(current_ts)
    prior = _bar_window(by_ts, current_ts - BAR, 20) if current else None
    window = _bar_window(by_ts, current_ts, 200) if current else None
    donchian_exit = None
    donchian_entry = None
    if current is not None and prior is not None:
        close = float(current["close"])
        donchian_exit = close < min(float(row["low"]) for row in prior)
        if window is not None:
            sma200 = sum(float(row["close"]) for row in window) / 200
            donchian_entry = close > max(float(row["high"]) for row in prior) and close > sma200
        else:
            # Invalid SMA200 blocks entry without disabling a valid 20-bar exit.
            donchian_entry = False
    relation = None
    if window is not None:
        sma50 = sum(float(row["close"]) for row in window[:50]) / 50
        sma200 = sum(float(row["close"]) for row in window) / 200
        relation = 1 if sma50 > sma200 else -1 if sma50 < sma200 else 0
    volatility = daily_volatility(daily, decision_ts)
    return {"donchian_entry": donchian_entry, "donchian_exit": donchian_exit,
        "sma50_200_relation": relation, "daily_volatility": volatility,
        "active_weight": active_weight(volatility)}


def _candidate_target(candidate: str, signals: dict, held: bool) -> float | None:
    active = signals.get("active_weight")
    if candidate == "cash":
        return 0.0
    if candidate == "passive":
        return active
    if candidate == "jesse_donchian20_sma200_long_cash":
        exit_condition = signals.get("donchian_exit")
        entry_condition = signals.get("donchian_entry")
        if held:
            if exit_condition is True:
                return 0.0
            return active if exit_condition is False else None
        if entry_condition is True:
            return active
        return 0.0 if entry_condition is False else None
    if candidate == "jesse_sma50_200_long_cash":
        relation = signals.get("sma50_200_relation")
        if relation is None:
            return None
        if relation > 0:
            return active
        if relation < 0:
            return 0.0
        return active if held else 0.0
    raise ValueError(f"Unknown candidate {candidate!r}.")


def _metric_template() -> dict:
    return {"initial_usdt": INITIAL, "ending_usdt": INITIAL, "net_return_pct": 0.0,
        "max_drawdown_pct": 0.0, "fills": 0, "roundtrips": 0, "entries": 0,
        "turnover_x_initial_capital": 0.0, "fees_usdt": 0.0, "slippage_usdt": 0.0,
        "time_in_market_pct": 0.0, "mean_capital_exposure_pct": 0.0,
        "max_capital_exposure_pct": 0.0, "drawdown_halts": 0, "halted": False,
        "halt_date_utc": None, "invalid_signal_decisions": 0, "active_signals_after_halt": 0,
        "cap_trim_fills": 0, "max_pretrade_weight_pct": 0.0, "state": "complete"}


def backtest_common(schedule: list[dict], candidate: str, fee_bps: float, slip_bps: float) -> dict:
    """Shared long-only V5 risk manager on a fixed 4h observed-open clock."""
    if candidate not in CANDIDATES or fee_bps < 0 or slip_bps < 0:
        raise ValueError("Invalid candidate or cost setting.")
    if not schedule or not schedule[-1].get("is_final") or schedule[-1].get("execution_open") is None:
        raise ValueError("Exact fixed liquidation endpoint is unavailable.")
    cash, qty = INITIAL, 0.0
    peak = INITIAL
    max_dd = fees = slippage = turnover = 0.0
    fills = entries = roundtrips = halts = cap_trims = invalid = post_halt_active = 0
    halted = False
    halt_time = None
    pending_target: float | None = None
    pending_present = False
    pending_signal_ts: int | None = None
    ledger = []
    exposures = []
    elapsed = invested = exposure_time = 0.0
    previous_ts = None
    previous_exposure = 0.0
    max_pretrade_weight = 0.0
    expected = len(schedule)
    observed = sum(row.get("execution_open") is not None for row in schedule)
    zero_volume = sum(row.get("execution_bar_present") and row.get("execution_open") is None for row in schedule)

    def trade(ts: int, raw: float, target: float, reason: str,
              signal_ts: int | None = None) -> None:
        nonlocal cash, qty, fees, slippage, turnover, fills, entries, roundtrips, cap_trims
        before = qty
        order = common.solve_trade(cash, qty, raw, target, fee_bps, slip_bps)
        if order is None:
            return
        cash, qty = order["cash"], order["qty"]
        fees += order["fee"]
        slippage += order["slippage"]
        turnover += order["raw_notional"] / INITIAL
        fills += 1
        if before <= 1e-12 and qty > 1e-12:
            entries += 1
        if before > 1e-12 and qty <= 1e-12:
            roundtrips += 1
        if reason == "max_weight_cap_trim":
            cap_trims += 1
        ledger.append({"ts": ts, "side": order["side"], "reason": reason,
            "decision_ts": signal_ts, "target_weight": target, **order})

    for row in schedule:
        ts = int(row["execution_open_ts"])
        raw = row.get("execution_open")
        if raw is not None:
            raw = float(raw)
            if previous_ts is not None:
                dt = (ts - previous_ts) / DAY
                if dt > 0:
                    elapsed += dt
                    if previous_exposure > 0:
                        invested += dt
                        exposure_time += previous_exposure * dt
            liq = cash + qty * raw * (1 - slip_bps / 10_000) * (1 - fee_bps / 10_000)
            peak = max(peak, liq)
            drawdown = max(0.0, 1 - liq / peak) if peak > 0 else 0.0
            if not halted and drawdown >= HALT:
                halted = True
                halts += 1
                halt_time = ts
                pending_present = False
                if qty > 1e-12:
                    trade(ts, raw, 0.0, "drawdown_halt")
            if row.get("is_final"):
                pending_present = False
                if qty > 1e-12:
                    trade(ts, raw, 0.0, "fixed_fold_liquidation")
            elif not halted:
                marked_equity = cash + qty * raw
                weight_before = qty * raw / marked_equity if marked_equity > 0 else 0.0
                max_pretrade_weight = max(max_pretrade_weight, weight_before)
                must_trim = weight_before > MAX_WEIGHT + 1e-12
                if pending_present and pending_target is not None and pending_target <= 0 and qty > 1e-12:
                    trade(ts, raw, 0.0, "signal_exit", pending_signal_ts)
                elif pending_present and pending_target is not None:
                    if must_trim or abs(weight_before - pending_target) >= REBAND:
                        goal = min(MAX_WEIGHT, max(0.0, pending_target))
                        trade(ts, raw, goal, "max_weight_cap_trim" if must_trim else "signal_rebalance", pending_signal_ts)
                elif must_trim:
                    trade(ts, raw, MAX_WEIGHT, "max_weight_cap_trim")
            pending_present = False
            liquidation_mark = cash + qty * raw * (1 - slip_bps / 10_000) * (1 - fee_bps / 10_000)
            peak = max(peak, liquidation_mark)
            max_dd = max(max_dd, max(0.0, 1 - liquidation_mark / peak) if peak else 0.0)
            equity = cash + qty * raw
            exposure = qty * raw / equity if equity > 0 else 0.0
            exposures.append(exposure)
            previous_exposure, previous_ts = exposure, ts
            if halted:
                held = False
            else:
                held = qty > 1e-12
        else:
            # Missing/zero-volume opens create no mark or fill. A pending entry
            # expires here; the next signal is evaluated from its own timestamp.
            pending_present = False
            held = qty > 1e-12

        signals = row["signals"]
        target = _candidate_target(candidate, signals, held)
        if row.get("is_final"):
            # The final boundary is a liquidation-only observation.
            target = None
        if target is None and not row.get("is_final"):
            invalid += 1
        if halted and target is not None and target > 0:
            post_halt_active += 1
        if not halted and target is not None:
            pending_target = target
            pending_present = True
            pending_signal_ts = int(row["decision_ts"])
        else:
            pending_present = False
            pending_signal_ts = None

    if qty > 1e-9:
        raise AssertionError("Fixed endpoint failed to liquidate the account.")
    metrics = _metric_template()
    metrics.update({
        "ending_usdt": cash,
        "net_return_pct": (cash / INITIAL - 1) * 100,
        "max_drawdown_pct": max_dd * 100,
        "fills": fills,
        "roundtrips": roundtrips,
        "entries": entries,
        "turnover_x_initial_capital": turnover,
        "fees_usdt": fees,
        "slippage_usdt": slippage,
        "time_in_market_pct": invested / elapsed * 100 if elapsed else 0.0,
        "mean_capital_exposure_pct": exposure_time / elapsed * 100 if elapsed else 0.0,
        "max_capital_exposure_pct": max(exposures, default=0.0) * 100,
        "drawdown_halts": halts,
        "halted": halted,
        "halt_date_utc": datetime.fromtimestamp(halt_time, timezone.utc).isoformat().replace("+00:00", "Z") if halt_time is not None else None,
        "invalid_signal_decisions": invalid,
        "active_signals_after_halt": post_halt_active,
        "cap_trim_fills": cap_trims,
        "max_pretrade_weight_pct": max_pretrade_weight * 100,
        "expected_execution_opens": expected,
        "observed_positive_volume_opens": observed,
        "missing_or_zero_volume_opens": expected - observed,
        "zero_volume_opens": zero_volume,
        "fixed_endpoint_open_utc": iso_utc(schedule[-1]["execution_open_ts"]),
    })
    return {"metrics": metrics, "ledger": ledger}


def result_cells() -> list[tuple[str, str, str, str, float, float]]:
    return [(candidate, symbol, fold, cost_id, fee, slip)
            for candidate in CANDIDATES for symbol in SYMBOLS for fold in FOLDS
            for cost_id, fee, slip in COSTS]


def prepare_all(saved: dict, datasets: dict) -> dict:
    prepared = {}
    for symbol in SYMBOLS:
        fold_values = {}
        for fold in saved["symbols"][symbol]["folds"]:
            if fold["name"] not in FOLDS:
                continue
            fold_values[fold["name"]] = prepare_symbol(datasets[symbol], fold)
        prepared[symbol] = fold_values
    return prepared


def score_cell(prepared: dict, candidate: str, symbol: str, fold: str,
               cost_id: str, fee: float, slip: float) -> dict:
    data = prepared[symbol][fold]
    outcome = backtest_common(data["schedule"], candidate, fee, slip)
    return {"candidate": candidate, "symbol": symbol, "fold": fold,
        "cost_scenario": cost_id, "fee_bps_per_side": fee,
        "slippage_bps_per_side": slip,
        "fixed_final_exit_open_utc": iso_utc(data["final_exit_ts"]), **outcome}


def lead_screen(cells: list[dict], candidate: str) -> dict:
    """Apply the frozen descriptive screen; never labels a passing rule as alpha."""
    selected = [x for x in cells if x["candidate"] == candidate]
    actual_keys = {(x["candidate"], x["symbol"], x["fold"], x["cost_scenario"]) for x in cells}
    expected_keys = {(c, s, f, cost_id) for c, s, f, cost_id, _, _ in result_cells()}
    full_grid = len(cells) == 120 and len(actual_keys) == 120 and actual_keys == expected_keys and all(
        x.get("metrics", {}).get("state") == "complete" for x in cells)
    middle = [x for x in selected if x["cost_scenario"] == "higher"]
    stress = [x for x in selected if x["cost_scenario"] == "stress"]
    final = [x for x in middle if x["fold"] == "final_2026"]
    lookup = {(x["candidate"], x["symbol"], x["fold"], x["cost_scenario"]): x for x in cells}
    paired_medians = {}
    for symbol in SYMBOLS:
        differences = []
        for fold in FOLDS:
            left = lookup.get((candidate, symbol, fold, "higher"))
            right = lookup.get(("passive", symbol, fold, "higher"))
            if left is not None and right is not None:
                differences.append(left["metrics"]["net_return_pct"] - right["metrics"]["net_return_pct"])
        paired_medians[symbol] = statistics.median(differences) if len(differences) == 5 else None
    criteria = {
        "entire_120_cell_grid_complete_and_unique": full_grid,
        "all_30_candidate_cells_complete": len(selected) == 30 and all(x["metrics"]["state"] == "complete" for x in selected),
        "positive_35bps_periods_both_assets_at_least_3_of_5": all(
            sum(x["metrics"]["net_return_pct"] > 0 for x in middle if x["symbol"] == symbol) >= 3
            for symbol in SYMBOLS),
        "positive_75bps_independent_fold_sum_both_assets": all(
            sum(x["metrics"]["ending_usdt"] - INITIAL for x in stress if x["symbol"] == symbol) > 0
            for symbol in SYMBOLS),
        "median_paired_return_difference_vs_passive_nonnegative_at_35bps": all(
            paired_medians[symbol] is not None and paired_medians[symbol] >= 0 for symbol in SYMBOLS),
        "candidate_max_drawdown_at_most_15pct": max((x["metrics"]["max_drawdown_pct"] for x in selected), default=math.inf) <= 15,
        "at_least_20_roundtrips_each_asset_at_35bps": all(
            sum(x["metrics"]["roundtrips"] for x in middle if x["symbol"] == symbol) >= 20
            for symbol in SYMBOLS),
        "at_least_one_final_period_roundtrip_each_asset_at_35bps": all(
            any(x["metrics"]["roundtrips"] >= 1 and x["symbol"] == symbol for x in final)
            for symbol in SYMBOLS),
    }
    return {"candidate": candidate, "criteria": criteria, "median_paired_return_difference_vs_passive_percentage_points_by_symbol": paired_medians,
        "passes_all_descriptive_lead_criteria": all(criteria.values()),
        "meaning": "Passing only identifies a candidate for separately reviewed future paper study; it is not validated alpha or an automatic activation."}


def paired_comparisons(cells: list[dict]) -> list[dict]:
    by_key = {(x["candidate"], x["symbol"], x["fold"], x["cost_scenario"]): x for x in cells}
    rows = []
    for candidate in CANDIDATES[2:]:
        for symbol in SYMBOLS:
            for fold in FOLDS:
                for cost_id, _, _ in COSTS:
                    left = by_key.get((candidate, symbol, fold, cost_id))
                    if left is None:
                        continue
                    for baseline in ("cash", "passive"):
                        right = by_key.get((baseline, symbol, fold, cost_id))
                        if right is None:
                            continue
                        rows.append({"candidate": candidate, "baseline": baseline,
                            "symbol": symbol, "fold": fold, "cost_scenario": cost_id,
                            "net_return_difference_percentage_points": left["metrics"]["net_return_pct"] - right["metrics"]["net_return_pct"],
                            "drawdown_difference_percentage_points": left["metrics"]["max_drawdown_pct"] - right["metrics"]["max_drawdown_pct"],
                            "mean_exposure_difference_percentage_points": left["metrics"]["mean_capital_exposure_pct"] - right["metrics"]["mean_capital_exposure_pct"]})
    return rows

