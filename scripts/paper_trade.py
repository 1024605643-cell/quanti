"""Owner-confirmed paper orders, filled only against subsequent fresh quotes."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import datetime, timedelta, time
from pathlib import Path

from quanti.backtest.commission import AShareCommission
from quanti.data.tencent_quotes import fetch_snapshot
from quanti.models import Direction
from quanti.utils.market import BEIJING_TZ, is_market_open

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "data/paper_state.json"
CONFIG = json.loads((ROOT / "config/short_term.json").read_text(encoding="utf-8"))
FEES = AShareCommission()
CODE = re.compile(r"(?:000|001|002|003|300|301|600|601|603|605|688)\d{3}")


def submit(state, side, code, fraction, order_id, now):
    if side not in {"buy", "sell", "cancel"} or not CODE.fullmatch(code):
        raise ValueError("请选择买入/卖出/撤单并填写六位沪深A股代码")
    if not math.isfinite(fraction) or not 0 < fraction <= 1:
        raise ValueError("比例必须大于0且不超过100%")
    orders = state.setdefault("orders", [])
    if any(o["id"] == order_id for o in orders):
        return  # GitHub re-runs must not duplicate an order.
    pending = [o for o in orders if o["status"] == "pending"]
    if side == "cancel":
        for order in pending:
            if order["code"] == code:
                order.update(status="cancelled", reason="用户撤单")
        orders.append(dict(id=order_id, side=side, code=code, status="cancelled",
                           created_at=now.isoformat(), reason="已处理撤单请求"))
        return
    if any(o["code"] == code for o in pending):
        raise ValueError("该股票已有待成交委托，请等待或先撤单")
    positions = state["positions"]
    if side == "buy":
        codes = set(positions) | {o["code"] for o in pending if o["side"] == "buy"} | {code}
        if len(codes) > CONFIG["max_positions"]:
            raise ValueError("持仓及待买入股票合计不能超过3只")
    elif code not in positions:
        raise ValueError("没有该股票持仓")
    orders.append(dict(id=order_id, side=side, code=code, fraction=fraction,
                       budget=state["cash"] * fraction,
                       quantity=int(positions.get(code, {}).get("quantity", 0) * fraction),
                       created_at=now.isoformat(), expires_at=(now + timedelta(hours=24)).isoformat(),
                       status="pending", reason="等待确认后的新行情"))


def match_order(state, order, quote, now):
    if order["status"] != "pending":
        return
    if now > datetime.fromisoformat(order["expires_at"]):
        order.update(status="expired", reason="24小时未成交，委托已失效")
        return
    if not is_market_open(now) or now.time().replace(tzinfo=None) >= time(15):
        order["reason"] = "休市，等待下次交易时段检查"
        return
    if not quote or quote["code"] != order["code"]:
        order["reason"] = "行情不可用，保留待成交"
        return
    qt = datetime.fromisoformat(quote["time"])
    if (qt <= datetime.fromisoformat(order["created_at"])
            or not 0 <= (now - qt).total_seconds() <= 90
            or qt.date() != now.date() or not is_market_open(qt) or quote["volume"] <= 0):
        order["reason"] = "行情过期、停牌或尚无确认后的新成交"
        return
    side, code = order["side"], order["code"]
    positions = state["positions"]
    if side == "buy" and re.search(r"ST|退|^[NC]", quote["name"], re.I):
        order.update(status="rejected", reason="风险警示、退市或新股禁止买入")
        return
    reference = quote["ask" if side == "buy" else "bid"]
    if reference <= 0:
        order["reason"] = "无有效对手盘，暂不成交"
        return
    fill = round(reference * (1 + CONFIG["slippage"] * (1 if side == "buy" else -1)), 2)
    if not quote["limit_down"] < fill < quote["limit_up"]:
        order["reason"] = "接近涨跌停或滑点价格越界，暂不成交"
        return
    pos = positions.get(code)
    if side == "buy":
        if len(positions) >= CONFIG["max_positions"] and pos is None:
            order.update(status="rejected", reason="已达到3只持仓上限")
            return
        budget = min(state["cash"], order["budget"])
        qty = int(budget / fill / 100) * 100
        minimum = 200 if code.startswith("688") else 100
        while qty >= minimum and qty * fill + FEES.calculate(fill, qty, Direction.BUY, now.date()) > budget:
            qty -= 100
        if qty < minimum:
            order.update(status="rejected", reason="可用资金不足最小买入数量及费用")
            return
    else:
        if pos is None:
            order.update(status="rejected", reason="已无持仓")
            return
        available = sum(lot["quantity"] for lot in pos.get("lots", [])
                        if lot["date"] < now.date().isoformat())
        target = min(pos["quantity"], order["quantity"])
        qty = target if target == pos["quantity"] else target // 100 * 100
        if qty <= 0:
            order.update(status="rejected", reason="减仓不足100股，请选择卖出全部")
            return
        if qty > available:
            order["reason"] = "T+1：所需股票尚未全部可卖"
            return
    # Do not assume liquidity beyond the displayed best-level size.
    if qty > quote["ask_lots" if side == "buy" else "bid_lots"] * 100:
        order["reason"] = "当前对手盘不足，等待足量报价"
        return
    direction = Direction.BUY if side == "buy" else Direction.SELL
    fee = round(FEES.calculate(fill, qty, direction, now.date()), 2)
    if side == "buy":
        cost = round(qty * fill + fee, 2)
        state["cash"] = round(state["cash"] - cost, 2)
        if pos is None:
            pos = positions[code] = dict(name=quote["name"], quantity=0, avg_cost=0,
                                         peak=fill, bought_at=now.isoformat(), exit_stages=[], lots=[])
        pos["avg_cost"] = (pos["avg_cost"] * pos["quantity"] + cost) / (pos["quantity"] + qty)
        pos["quantity"] += qty
        pos["lots"].append(dict(date=now.date().isoformat(), quantity=qty))
    else:
        state["cash"] = round(state["cash"] + qty * fill - fee, 2)
        remaining = qty
        for lot in pos["lots"]:
            if lot["date"] < now.date().isoformat():
                sold = min(lot["quantity"], remaining)
                lot["quantity"] -= sold
                remaining -= sold
        pos["lots"] = [lot for lot in pos["lots"] if lot["quantity"]]
        pnl = fill / pos["avg_cost"] - 1
        stages = ([(-0.025, "stop2", 0.5), (-0.0125, "stop1", 0.25)] if pnl < 0
                  else [(0.06, "take2", 0.5), (0.03, "take1", 0.25)])
        for threshold, stage, fraction in stages:
            if (pnl <= threshold if pnl < 0 else pnl >= threshold) and order["fraction"] >= fraction:
                if stage not in pos["exit_stages"]:
                    pos["exit_stages"].append(stage)
                break
        pos["quantity"] -= qty
        if pos["quantity"] == 0:
            del positions[code]
    trade = dict(order_id=order["id"], time=now.isoformat(), quote_time=quote["time"],
                 side=side, code=code, name=quote["name"], quantity=qty, price=fill,
                 reference_price=reference, fees=fee, source=quote["source"])
    state.setdefault("trades", []).append(trade)
    order.update(status="filled", reason=f"模拟成交{qty}股，价格{fill:.2f}，费用{fee:.2f}元", fill=trade)


def refresh(state, now=None, fetch=fetch_snapshot):
    codes = set(state["positions"]) | {o["code"] for o in state.get("orders", []) if o["status"] == "pending"}
    quotes = {}
    for code in codes:
        try:
            quotes[code] = fetch(code)
        except Exception:
            pass
    now = now or datetime.now(BEIJING_TZ)
    for order in state.get("orders", []):
        match_order(state, order, quotes.get(order["code"]), now)
    missing, equity = [], state["cash"]
    for code, pos in state["positions"].items():
        quote = quotes.get(code)
        if (not quote or quote['price'] <= 0
                or datetime.fromisoformat(quote["time"]).date() != now.date()
                or datetime.fromisoformat(quote["time"]) > now):
            missing.append(code)
            continue
        pos.update(mark=quote["price"], mark_time=quote["time"], peak=max(pos["peak"], quote["price"]))
        equity += pos["quantity"] * quote["price"]
    state.update(updated_at=now.isoformat(), missing_marks=missing,
                 equity=None if missing else round(equity, 2),
                 return_pct=None if missing else round((equity / CONFIG["capital"] - 1) * 100, 4))
    from scripts.short_term_daily import review_positions
    state["alerts"] = review_positions(state, {c: q["price"] for c, q in quotes.items()
        if datetime.fromisoformat(q["time"]).date() == now.date()}, now)
    if not missing:
        history = state.setdefault("equity_history", [])
        history.append(dict(time=now.isoformat(), equity=round(equity, 2)))
        peak = CONFIG["capital"]
        drawdown = 0
        for point in history:
            peak = max(peak, point["equity"])
            drawdown = max(drawdown, 1 - point["equity"] / peak)
        state["max_drawdown_pct"] = round(drawdown * 100, 4)


def save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False, indent=2)
    temp = STATE.with_suffix(".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(STATE)
    (ROOT / "docs/quant/account.json").write_text(text, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--side", choices=["buy", "sell", "cancel", "check", "monitor"], default="check")
    parser.add_argument("--code", default="")
    parser.add_argument("--fraction", type=float, default=0.5)
    args = parser.parse_args()
    state = json.loads(STATE.read_text(encoding="utf-8"))
    now = datetime.now(BEIJING_TZ)
    if args.side == "check":
        quote = fetch_snapshot("002384")
        text = f"模拟交易连接检查通过；行情时间：{quote['time']}。本次未提交任何买卖。"
    else:
        before = {o["id"]: (o["status"], o["reason"]) for o in state.get("orders", [])}
        old_alerts = state.get("alerts", [])
        if args.side != "monitor":
            submit(state, args.side, args.code, args.fraction, os.environ["GITHUB_RUN_ID"], now)
        refresh(state)
        save(state)
        changed = [o for o in state.get("orders", []) if before.get(o["id"]) != (o["status"], o["reason"])]
        text = "\n".join(f"{o['code']} {o['side']}：{o['reason']}" for o in changed)
        if state["alerts"] != old_alerts and state["alerts"]:
            text += '\n' + '\n'.join(f"{a['code']}：{a['why']}，请确认卖出{a['fraction']:.0%}当前仓位" for a in state["alerts"])
    if text:
        print(text)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as out:
                out.write("## 模拟交易回执\n\n" + text + "\n")
        # Workflow sends the receipt only AFTER the ledger commit succeeds.
        (ROOT / "paper_receipt.txt").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
