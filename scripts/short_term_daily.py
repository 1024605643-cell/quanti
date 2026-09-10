"""Daily A-share shortlist and paper-account review for GitHub Actions."""

from __future__ import annotations

import json
import contextlib
import io
import math
import os
import smtplib
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from urllib.request import Request, urlopen

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config" / "short_term.json").read_text(encoding="utf-8"))
OUT = ROOT / "docs" / "quant"
STATE_FILE = ROOT / "data" / "paper_state.json"
PREFER_SINA = False
BEIJING = timezone(timedelta(hours=8))


def _number(value, default=0.0) -> float:
    try:
        out = float(value)
        return default if math.isnan(out) else out
    except (TypeError, ValueError):
        return default


def _history(code: str, start: str, end: str) -> pd.DataFrame:
    symbol = ("sh" if code.startswith(("60", "68")) else "sz") + code
    if PREFER_SINA:
        frame = ak.stock_zh_a_daily(symbol=symbol, start_date=start, end_date=end,
                                    adjust="qfq").reset_index(drop=True)
    else:
        try:
            frame = ak.stock_zh_a_hist(symbol=code, start_date=start, end_date=end,
                                       period="daily", adjust="qfq")
        except Exception:
            frame = ak.stock_zh_a_daily(symbol=symbol, start_date=start, end_date=end,
                                        adjust="qfq").reset_index(drop=True)
    if frame.empty:
        return frame
    return frame.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                 "最高": "high", "最低": "low", "成交量": "volume",
                                 "成交额": "amount", "换手率": "turnover"})


def _spot() -> pd.DataFrame:
    """Eastmoney first, Tencent fallback; return one stable Chinese schema."""
    global PREFER_SINA
    try:
        return ak.stock_zh_a_spot_em()
    except Exception:
        PREFER_SINA = True
        frame = ak.stock_zh_a_spot_tx().copy()
        frame["代码"] = frame["code"].astype(str).str[-6:]
        frame["名称"] = frame["name"]
        frame["最新价"] = pd.to_numeric(frame["zxj"], errors="coerce")
        frame["涨跌幅"] = pd.to_numeric(frame["zdf"], errors="coerce")
        frame["换手率"] = pd.to_numeric(frame["hsl"], errors="coerce")
        frame["5日涨跌幅"] = pd.to_numeric(frame["zdf_d5"], errors="coerce")
        frame["10日涨跌幅"] = pd.to_numeric(frame["zdf_d10"], errors="coerce")
        # Tencent turnover is reported in 万元.
        frame["成交额"] = pd.to_numeric(frame["turnover"], errors="coerce") * 10_000
        return frame


def _score(row: pd.Series, bars: pd.DataFrame) -> tuple[float, list[str], list[str]]:
    if len(bars) < 25:
        return 0.0, [], ["历史不足25个交易日"]
    close = bars["close"].astype(float)
    volume = bars["volume"].astype(float)
    latest = bars.iloc[-1]
    reasons, rejects = [], []
    gain3 = close.iloc[-1] / close.iloc[-4] - 1
    gain5 = close.iloc[-1] / close.iloc[-6] - 1
    gain10 = close.iloc[-1] / close.iloc[-11] - 1
    gain20 = close.iloc[-1] / close.iloc[-21] - 1
    vol_ratio = volume.iloc[-1] / max(volume.iloc[-6:-1].mean(), 1)
    ma5, ma10, ma20 = close.tail(5).mean(), close.tail(10).mean(), close.tail(20).mean()
    gap = _number(latest["open"]) / max(_number(bars.iloc[-2]["close"]), 0.01) - 1
    upper_shadow = (_number(latest["high"]) - max(_number(latest["open"]),
                    _number(latest["close"]))) / max(_number(latest["close"]), 0.01)
    avg_amount20 = bars["amount"].astype(float).tail(20).mean()
    daily_change = _number(row.get("涨跌幅"))
    limit_threshold = 19.5 if str(row.get("代码", "")).startswith(("30", "68")) else 9.5

    if avg_amount20 < CONFIG["min_daily_amount"]:
        rejects.append("20日平均成交额不足5亿元")
    if gain10 > CONFIG["max_10d_gain"] or gain20 > CONFIG["max_20d_gain"]:
        rejects.append("10/20日涨幅过高，避免追高")
    if gap > CONFIG["max_gap_up"]:
        rejects.append("高开超过7%")
    if upper_shadow > CONFIG["max_upper_shadow"]:
        rejects.append("长上影线超过6%")
    if daily_change >= limit_threshold:
        rejects.append("当日接近或已涨停，无法按计划成交")
    if (close.pct_change().tail(4) > 0.095).sum() >= 2:
        rejects.append("近期多次接近涨停")
    if rejects:
        return 0.0, [], rejects

    score = 0.0
    if close.iloc[-1] > ma5 > ma10 > ma20:
        score += 28
        reasons.append("均线多头")
    score += max(0, min(20, gain3 * 180))
    score += max(0, min(18, gain5 * 100))
    score += max(0, min(18, (vol_ratio - 1) * 12))
    score += max(0, min(10, _number(row.get("涨跌幅")) / 1.2))
    score += max(0, min(6, _number(row.get("换手率")) / 2))
    if 1.2 <= vol_ratio <= 3.5:
        reasons.append(f"量比{vol_ratio:.1f}")
    if gain3 > 0.02:
        reasons.append(f"3日动量{gain3:.1%}")
    return round(score, 2), reasons, []


def _wencai_codes() -> set[str]:
    cookie = os.getenv("WENCAI_COOKIE", "")
    if not cookie:
        return set()
    try:
        import pywencai
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = pywencai.get(
                query="A股，非ST，上市超过180天，20日平均成交额大于5亿元，近10日涨幅小于25%，近20日涨幅小于40%，今日放量上涨",
                cookie=cookie, loop=1, retry=2)
        if not isinstance(result, pd.DataFrame) or result.empty:
            return set()
        for column in result.columns:
            if '股票代码' not in str(column) and str(column) != 'code':
                continue
            values = result[column].astype(str).str.extract(r"^(\d{6})(?:\.[A-Za-z]+)?$", expand=False).dropna()
            if not values.empty:
                return set(values)
    except Exception as exc:
        print(f"wencai unavailable: {type(exc).__name__}")
    return set()


def scan() -> tuple[list[dict], list[dict]]:
    spot = _spot()
    spot["代码"] = spot["代码"].astype(str).str.zfill(6)
    names = spot["名称"].astype(str)
    allowed = spot["代码"].str.match(r"^(00|60|30|68)")
    clean = ~names.str.contains(r"ST|退|N|C", case=False, regex=True)
    liquid = pd.to_numeric(spot["成交额"], errors="coerce").fillna(0) >= CONFIG["min_daily_amount"]
    pool = spot[allowed & clean & liquid].copy()
    change = pd.to_numeric(pool["涨跌幅"], errors="coerce").fillna(0).clip(-3, 8)
    gain5_raw = pool["5日涨跌幅"] if "5日涨跌幅" in pool else pd.Series(0, index=pool.index)
    gain5 = pd.to_numeric(gain5_raw, errors="coerce").fillna(0).clip(-5, 15)
    turnover = pd.to_numeric(pool["换手率"], errors="coerce").fillna(0).clip(0, 15)
    amount_rank = pd.to_numeric(pool["成交额"], errors="coerce").rank(pct=True)
    pool["pre_score"] = change * 2 + gain5 + turnover * 0.5 + amount_rank * 5
    # Historical requests are the slow part. A broad realtime pre-rank keeps
    # the strongest liquid names while fitting comfortably in Actions.
    pool = pool.sort_values("pre_score", ascending=False).head(80)
    today = date.today()
    start = (today - timedelta(days=130)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    ranked, rejected = [], []
    wencai = _wencai_codes()
    for _, row in pool.iterrows():
        code = row["代码"]
        try:
            bars = _history(code, start, end)
            score, reasons, rejects = _score(row, bars)
        except Exception as exc:  # one broken quote source must not stop the report
            rejected.append({"code": code, "name": row["名称"], "why": f"数据失败: {exc}"})
            continue
        item = {"code": code, "name": row["名称"], "score": score,
                "price": _number(row.get("最新价")), "change": _number(row.get("涨跌幅")),
                "reasons": reasons, "rejects": rejects}
        if code in wencai and not rejects:
            item["score"] += 8
            item["reasons"].append("同花顺问财条件共振")
        (rejected if rejects else ranked).append(item)
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:CONFIG["candidate_count"]], rejected[:10]


def ai_review(candidates: list[dict]) -> str:
    key = os.getenv("LLM_PRIMARY_API_KEY", "")
    if not key or not candidates:
        return "AI 风控复核未配置；量化排名不受影响。"
    base = os.getenv("LLM_PRIMARY_BASE_URL", "https://api.3366.ai/v1").rstrip("/")
    model = os.getenv("LLM_PRIMARY_MODEL", "gpt-6-astra")
    prompt = ("你是A股短线风控复核员。量化排名已经完成，你只解释和指出风险，"
              "不得重排或承诺收益。只解释输入的量价指标。当前未提供新闻、公告、业绩或市场环境证据，"
              "这些项目必须写未核验，禁止推测具体事件或宣称风控通过：\n" +
              json.dumps(candidates[:5], ensure_ascii=False))
    body = json.dumps({"model": model, "input": prompt, "max_output_tokens": 1200},
                      ensure_ascii=False).encode()
    try:
        req = Request(base + "/responses", data=body, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urlopen(req, timeout=60) as response:
            data = json.load(response)
        if data.get("output_text"):
            return data["output_text"]
        return "\n".join(part.get("text", "") for item in data.get("output", [])
                         for part in item.get("content", []) if part.get("type") == "output_text")
    except Exception as exc:
        return f"AI 风控复核暂时失败：{exc}"


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"cash": CONFIG["capital"], "positions": {}, "trades": []}


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


def render(report: dict) -> str:
    rows = "".join(f"<tr><td>{i+1}</td><td>{x['code']} {x['name']}</td><td>{x['score']:.1f}</td><td>{x['price']:.2f}</td><td>{x['change']:+.2f}%</td><td>{'、'.join(x['reasons'])}</td></tr>" for i, x in enumerate(report["candidates"]))
    rejected = "".join(f"<li>{x['code']} {x['name']}：{'、'.join(x.get('rejects') or [x.get('why','')])}</li>" for x in report["rejected"])
    alerts = "".join(f"<li>{x['code']}：{x['why']}（需人工确认）</li>" for x in report["alerts"]) or "<li>无</li>"
    review = str(report["ai_review"]).replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
    return f"""<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>A股短线量化</title><style>body{{font:16px system-ui;max-width:1100px;margin:auto;padding:24px;background:#f5f7fa;color:#172033}}table{{width:100%;border-collapse:collapse;background:white}}th,td{{padding:10px;border-bottom:1px solid #ddd;text-align:left}}.card{{background:white;padding:18px;margin:16px 0;border-radius:12px}}small{{color:#667}}</style><h1>A股短线量化看板</h1><small>更新时间 {report['generated_at']}｜量化排名用于研究与模拟，不保证未来收益</small><div class='card'><h2>候选排名（前5为主推）</h2><table><tr><th>#</th><th>股票</th><th>分数</th><th>价格</th><th>涨跌</th><th>量价理由</th></tr>{rows}</table></div><div class='card'><h2>GPT-6 风控复核</h2><p>{review}</p></div><div class='card'><h2>持仓风控提醒</h2><ul>{alerts}</ul></div><div class='card'><h2>为什么没入选</h2><ul>{rejected}</ul></div>"""


def notify_wecom(text: str) -> None:
    url = os.getenv("WECOM_WEBHOOK_URL", "")
    if not url:
        return
    payload = json.dumps({"msgtype": "text", "text": {"content": text}}, ensure_ascii=False).encode()
    with urlopen(Request(url, data=payload, headers={"Content-Type": "application/json"}), timeout=20) as response:
        result = json.load(response)
    if result.get("errcode") != 0:
        raise RuntimeError(f"WeCom rejected notification: {result.get('errcode')}")


def notify_email(subject: str, html: str) -> None:
    sender, password = os.getenv("EMAIL_SENDER", ""), os.getenv("EMAIL_PASSWORD", "")
    if not sender or not password:
        return
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, sender
    smtp = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30)
    try:
        smtp.login(sender, password)
        smtp.sendmail(sender, [sender], msg.as_string())
    finally:
        smtp.close()


def main() -> None:
    candidates, rejected = scan()
    state = _load_state()
    prices = {x["code"]: x["price"] for x in candidates}
    for code in state.get("positions", {}):
        if code not in prices:
            spot = _spot()
            match = spot[spot["代码"].astype(str).str.zfill(6) == code]
            if not match.empty:
                prices[code] = _number(match.iloc[0]["最新价"])
    alerts = review_positions(state, prices)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {"generated_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
              "candidates": candidates, "rejected": rejected, "alerts": alerts,
              "paper": state, "ai_review": ai_review(candidates)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html = render(report)
    html = html.replace('<h1>A股短线量化看板</h1>', '<h1>A股短线量化看板</h1><p><a href="../trade/">查看模拟账户 / 买卖操作说明</a></p>', 1)
    (OUT / "index.html").write_text(html, encoding="utf-8")
    top = "\n".join(f"{i+1}. {x['code']} {x['name']} {x['score']:.1f}分" for i, x in enumerate(candidates[:5]))
    action_url = os.getenv("CONFIRM_URL", "")
    details = '\n'.join(f"{a['code']}：{a['why']}，建议卖出{a['fraction']:.0%}当前仓位" for a in alerts) or '暂无持仓风控提醒'
    notify_wecom(f"A股短线量化候选\n{top}\n{details}\n查看账户与提交方法：{action_url}\n首次使用请在手机浏览器登录GitHub，再点Run workflow。买卖均为模拟，需你确认。公告新闻风险尚未完整核验。")
    notify_email("A股短线量化日报", html)


if __name__ == "__main__":
    main()
