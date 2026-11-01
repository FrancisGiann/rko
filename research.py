"""Past-only features, a small transparent model, and exact cash backtests."""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from typing import Any

HOUR = 3600
INITIAL = 10_000.0
ALLOCATION = 0.50
FEE = 0.001
SLIPPAGE = 0.0005
HALT = 0.10
FEATURE_NAMES = ["1-sample return", "3-sample momentum", "6-sample momentum", "12-sample volatility", "12-sample momentum"]
THRESHOLDS = [0.50, 0.53, 0.56, 0.59, 0.62, 0.65]
VERSION = "logistic-v3.1-archived-sampled-prices"


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def settings() -> dict:
    return {"version": VERSION, "features": FEATURE_NAMES, "thresholds": THRESHOLDS,
            "partition_ratios": [.6, .2, .2], "purge": "target_end < next first signal",
            "entry_delay_samples": 1, "holding_samples": 1, "initial": INITIAL,
            "allocation": ALLOCATION, "fee": FEE, "slippage": SLIPPAGE, "drawdown_halt": HALT,
            "epochs": 180, "learning_rate": .08, "regularization": .02,
            "interval_tolerance_seconds": [HOUR * .5, HOUR * 1.5]}


def experiment_key(points: list[dict]) -> str:
    return fingerprint({"settings": settings(), "observations": [{"ts":p["ts"],"price":p["price"]} for p in points]})


def feature_rows(candles: list[dict]) -> list[dict]:
    """A row at observation i sees i and earlier, never i+1.

    Features and targets are separate: the newest row needs no future sample.
    Missing hourly samples invalidate the entire 12-observation lookback.
    """
    rows = []
    for i in range(12, len(candles)):
        window = candles[i - 12:i + 1]
        if any(not HOUR * .5 <= b["ts"] - a["ts"] <= HOUR * 1.5 for a, b in zip(window, window[1:])):
            continue
        closes = [float(c["price"]) for c in window]
        returns = [closes[j] / closes[j - 1] - 1 for j in range(1, 13)]
        rows.append({"ts": int(candles[i]["ts"]), "signal_at": int(candles[i]["ts"]),
                     "x": [returns[-1], closes[-1] / closes[-4] - 1,
                           closes[-1] / closes[-7] - 1, statistics.pstdev(returns),
                           closes[-1] / closes[0] - 1]})
    return rows


def labeled_rows(candles: list[dict]) -> list[dict]:
    by_ts = {c["ts"]: i for i, c in enumerate(candles)}
    rows = []
    for f in feature_rows(candles):
        i = by_ts[f["ts"]]
        if i + 2 >= len(candles):
            continue
        nxt, exit_ = candles[i + 1], candles[i + 2]
        if any(not HOUR * .5 <= b["ts"] - a["ts"] <= HOUR * 1.5 for a, b in ((candles[i], nxt), (nxt, exit_))):
            continue
        ret = float(exit_["price"]) / float(nxt["price"]) - 1
        rows.append({**f, "exec_ts": int(nxt["ts"]), "target_end": int(exit_["ts"]),
                     "exec_open": float(nxt["price"]), "exec_close": float(exit_["price"]),
                     "label": int(ret > 0)})
    return rows


def split_rows(rows: list[dict]) -> tuple[list, list, list]:
    a, b = int(len(rows) * 0.6), int(len(rows) * 0.8)
    if a < 40 or b - a < 15 or len(rows) - b < 15:
        return [], [], []
    # A target finishes strictly before the next split's first observed price.
    # Two observations at each boundary are purged for hourly samples.
    train = [r for r in rows[:a] if r["target_end"] < rows[a]["ts"]]
    validation = [r for r in rows[a:b] if r["target_end"] < rows[b]["ts"]]
    return train, validation, rows[b:]


def sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-max(-35.0, min(35.0, z))))


def train_model(rows: list[dict]) -> dict:
    cols = list(zip(*(r["x"] for r in rows)))
    means = [statistics.mean(c) for c in cols]
    scales = [max(statistics.pstdev(c), 1e-8) for c in cols]
    xs = [[(v - m) / s for v, m, s in zip(r["x"], means, scales)] for r in rows]
    weights = [0.0] * 6
    for _ in range(180):
        grad = [0.0] * 6
        for x, r in zip(xs, rows):
            err = sigmoid(weights[0] + sum(w * v for w, v in zip(weights[1:], x))) - r["label"]
            grad[0] += err
            for j, v in enumerate(x, 1):
                grad[j] += err * v
        weights = [w - 0.08 * (g / len(rows) + (0.02 * w if j else 0))
                   for j, (w, g) in enumerate(zip(weights, grad))]
    return {"means": means, "scales": scales, "weights": weights,
            "epochs": 180, "regularization": 0.02, "version": VERSION}


def probability(model: dict, row: dict) -> float:
    standardized = [(v - m) / s for v, m, s in zip(row["x"], model["means"], model["scales"])]
    return sigmoid(model["weights"][0] + sum(w * v for w, v in zip(model["weights"][1:], standardized)))


def buy(cash: float, raw: float, equity: float | None = None) -> dict:
    """The 50% cap includes entry fees: never spend more than half equity."""
    budget = min(cash, (cash if equity is None else equity) * ALLOCATION)
    price = raw * (1 + SLIPPAGE)
    qty = budget / (price * (1 + FEE))
    notional = qty * price
    return {"cash": cash - notional * (1 + FEE), "qty": qty, "price": price,
            "fee": notional * FEE, "slippage": qty * (price - raw), "cost_basis": budget}


def sell(cash: float, qty: float, raw: float, basis: float) -> dict:
    price = raw * (1 - SLIPPAGE)
    notional = qty * price
    proceeds = notional * (1 - FEE)
    return {"cash": cash + proceeds, "qty": qty, "price": price, "fee": notional * FEE,
            "slippage": qty * (raw - price), "pnl": proceeds - basis}


def backtest(rows: list[dict], selections: list[bool], *, hold: bool = False,
             collect: bool = False) -> dict:
    cash, qty, basis, high, max_dd = INITIAL, 0.0, 0.0, INITIAL, 0.0
    fees = slippage = 0.0
    trades = wins = active_bars = 0
    halted = False
    ledger, curve = [], []
    if rows:
        curve.append({"ts": rows[0]["exec_ts"], "equity": INITIAL})
    for i, (row, selected) in enumerate(zip(rows, selections)):
        if selected and not halted and qty == 0:
            entry = buy(cash, row["exec_open"])
            cash, qty, basis = entry["cash"], entry["qty"], entry["cost_basis"]
            fees += entry["fee"]
            slippage += entry["slippage"]
            trades += 1
            if collect:
                ledger.append({"signal_ts": row["ts"], "signal_at": row["signal_at"],
                               "fill_at": row["exec_ts"], "side": "BUY", **entry,
                               "reason": "Next actual hourly price after signal; 50% budget including fees"})
        if qty:
            active_bars += 1
        mark = cash + qty * row["exec_close"]
        high = max(high, mark)
        max_dd = max(max_dd, (high - mark) / high)
        if max_dd >= HALT:
            halted = True
        if qty and (not hold or i == len(rows) - 1 or halted):
            exit_ = sell(cash, qty, row["exec_close"], basis)
            cash, qty = exit_["cash"], 0.0
            fees += exit_["fee"]
            slippage += exit_["slippage"]
            wins += int(exit_["pnl"] > 0)
            if collect:
                ledger.append({"signal_ts": row["ts"], "signal_at": row["signal_at"],
                               "fill_at": row["target_end"], "side": "SELL", **exit_,
                               "reason": "Following actual hourly price" if not hold else "Final held-out price or drawdown halt"})
        equity = cash + qty * row["exec_close"]
        high = max(high, equity)
        max_dd = max(max_dd, (high - equity) / high)
        if max_dd >= HALT:
            halted = True
        curve.append({"ts": row["target_end"], "equity": equity})
    return {"return": (cash + qty * (rows[-1]["exec_close"] if rows else 0)) / INITIAL - 1,
            "max_drawdown": max_dd, "trades": trades, "win_rate": wins / trades if trades else None,
            "exposure": active_bars / len(rows) if rows else 0, "fees": fees, "slippage": slippage,
            "costs": fees + slippage, "halted": halted, "curve": curve, "ledger": ledger,
            "cash": cash, "high_water": high}


def analyze(candles: list[dict]) -> dict:
    rows = labeled_rows(candles)
    train, validation, test = split_rows(rows)
    if not train:
        return {"ready": False, "rows": len(rows), "message": "Need at least 90 valid feature/target pairs (about 104 contiguous hourly observations)."}
    model = train_model(train)
    scored = [(backtest(validation, [probability(model, r) >= t for r in validation]), t) for t in THRESHOLDS]
    best, threshold = max(scored, key=lambda pair: (pair[0]["return"], -pair[0]["max_drawdown"], -pair[1]))
    strategies = {
        "model": backtest(test, [probability(model, r) >= threshold for r in test], collect=True),
        "momentum": backtest(test, [r["x"][2] > 0 for r in test]),
        "buy_hold": backtest(test, [True] * len(test), hold=True),
    }
    curve = [{"ts": point["ts"], **{key: value["curve"][i]["equity"] for key, value in strategies.items()}}
             for i, point in enumerate(strategies["model"]["curve"])]
    metrics = {key: {k: v for k, v in value.items() if k not in {"curve", "ledger", "cash", "high_water"}}
               for key, value in strategies.items()}
    predicted = [probability(model, r) for r in test]
    accuracy = sum((p >= .5) == bool(r["label"]) for p, r in zip(predicted, test)) / len(test)
    brier = sum((p - r["label"]) ** 2 for p, r in zip(predicted, test)) / len(test)
    train_majority_label = int(sum(r["label"] for r in train) >= len(train) / 2)
    majority = sum(r["label"] == train_majority_label for r in test) / len(test)
    split_info = {name: {"count": len(part), "start": part[0]["ts"], "end": part[-1]["target_end"]}
                  for name, part in (("train", train), ("validation", validation), ("holdout", test))}
    fit_id = fingerprint({"version": VERSION, "train": train, "validation": validation})
    experiment_id = experiment_key(candles)
    return {"ready": True, "id": experiment_id, "fit_id": fit_id, "model": model,
            "threshold": threshold, "candidates": THRESHOLDS, "splits": split_info,
            "purged": len(rows) - len(train) - len(validation) - len(test), "rows": len(rows),
            "validation_return": best["return"], "accuracy": accuracy, "majority_accuracy": majority,
            "brier": brier, "metrics": metrics, "curve": curve,
            "replay_ledger": strategies["model"]["ledger"], "replay_cash": strategies["model"]["cash"],
            "replay_high_water": strategies["model"]["high_water"],
            "warning": "A roughly 30-day sample and a few held-out days cannot establish a durable edge. Hourly observations overlap in market regime; no significance or future return is claimed.",
            "features": FEATURE_NAMES}
