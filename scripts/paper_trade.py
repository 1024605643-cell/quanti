"""Record a user-confirmed paper fill in a small JSON ledger."""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "data" / "paper_state.json"
CONFIG = json.loads((ROOT / "config" / "short_term.json").read_text(encoding="utf-8"))


def execute(state: dict, side: str, code: str, price: float, fraction: float,
            name: str = "", stage: str = "") -> dict:
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("股票代码必须是六位数字")
    if not math.isfinite(price) or price <= 0:
        raise ValueError("价格必须大于0")
    if not math.isfinite(fraction) or not 0 < fraction <= 1:
        raise ValueError("比例必须在0到1之间")
    slip = CONFIG["slippage"]
    positions = state.setdefault("positions", {})
    state.setdefault("trades", [])
    if side == "buy":
        if len(positions) >= CONFIG["max_positions"] and code not in positions:
            raise ValueError("最多同时持有3只股票")
        fill = price * (1 + slip)
        budget = state["cash"] * fraction
        qty = int(budget / fill / 100) * 100
        if qty < 100:
            raise ValueError("可用资金不足一手")
        cost = qty * fill
        state["cash"] -= cost
        old = positions.get(code)
        if old:
            total = old["quantity"] + qty
            old["avg_cost"] = (old["avg_cost"] * old["quantity"] + cost) / total
            old["quantity"] = total
        else:
            positions[code] = {"name": name, "quantity": qty, "avg_cost": fill,
                               "peak": fill, "bought_at": datetime.now().isoformat(),
                               "exit_stages": []}
    else:
        if code not in positions:
            raise ValueError("没有该股票持仓")
        pos = positions[code]
        qty = min(pos["quantity"], max(100, int(pos["quantity"] * fraction / 100) * 100))
        fill = price * (1 - slip)
        state["cash"] += qty * fill
        pos["quantity"] -= qty
        if stage:
            pos.setdefault("exit_stages", []).append(stage)
        if pos["quantity"] <= 0:
            del positions[code]
    state["trades"].append({"time": datetime.now().isoformat(timespec="seconds"),
                            "side": side, "code": code, "quantity": qty,
                            "price": round(fill, 3), "stage": stage})
    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--side", choices=["buy", "sell"], required=True)
    parser.add_argument("--code", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--price", type=float, required=True)
    parser.add_argument("--fraction", type=float, default=1.0)
    parser.add_argument("--stage", default="")
    args = parser.parse_args()
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {
        "cash": CONFIG["capital"], "positions": {}, "trades": []}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(execute(state, **vars(args)), ensure_ascii=False, indent=2),
                     encoding="utf-8")


if __name__ == "__main__":
    main()
