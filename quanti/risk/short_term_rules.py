"""Pure exit alerts shared by cloud reports and desktop paper trading."""
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))


def review_positions(state: dict, prices: dict[str, float], now=None) -> list[dict]:
    now = now or datetime.now(BEIJING)
    alerts = []
    for code, pos in state.get("positions", {}).items():
        price = prices.get(code)
        if not price:
            continue
        pnl = price / pos["avg_cost"] - 1
        pos["peak"] = max(pos.get("peak", pos["avg_cost"]), price)
        done = set(pos.get("exit_stages", []))
        if pnl <= -0.05:
            alerts.append({"code": code, "action": "sell", "fraction": 1.0, "why": "-5%硬止损"})
        elif (now.date() - datetime.fromisoformat(pos["bought_at"]).date()).days >= 14:
            alerts.append({"code": code, "action": "sell", "fraction": 1.0, "why": "持有已满两周，退出提醒"})
        elif pnl <= -0.025 and "stop2" not in done:
            alerts.append({"code": code, "action": "sell", "fraction": 0.5, "why": "-2.5%减当前仓位50%", "stage": "stop2"})
        elif pnl <= -0.0125 and "stop1" not in done:
            alerts.append({"code": code, "action": "sell", "fraction": 0.25, "why": "-1.25%减仓25%", "stage": "stop1"})
        elif pnl >= 0.06 and "take2" not in done:
            alerts.append({"code": code, "action": "sell", "fraction": 0.5, "why": "+6%减当前仓位50%", "stage": "take2"})
        elif pnl >= 0.03 and "take1" not in done:
            alerts.append({"code": code, "action": "sell", "fraction": 0.25, "why": "+3%止盈25%", "stage": "take1"})
        elif "take2" in done and price / pos["peak"] - 1 <= -0.03:
            alerts.append({"code": code, "action": "sell", "fraction": 1.0, "why": "峰值回撤3%清仓"})
    return alerts

