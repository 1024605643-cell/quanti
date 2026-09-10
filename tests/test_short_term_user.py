from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from scripts.paper_trade import match_order, submit, refresh
from scripts.short_term_daily import review_positions
from quanti.data.tencent_quotes import parse_snapshot

START = datetime.fromisoformat("2026-09-10T10:00:00+08:00")


def account():
    return {"cash": 100000, "positions": {}, "trades": []}


def quote(now, **changes):
    q = dict(
        code="002384",
        name="东山精密",
        price=10,
        previous_close=10,
        bid=10,
        ask=10,
        bid_lots=10000,
        ask_lots=10000,
        volume=100000,
        limit_up=11,
        limit_down=9,
        time=now.isoformat(),
        source="fixture",
    )
    return q | changes


def bought():
    s = account()
    submit(s, "buy", "002384", 1, "buy1", START)
    t = START + timedelta(seconds=5)
    match_order(s, s["orders"][0], quote(t), t)
    return s


def test_confirmed_fill_costs_and_t_plus_one():
    s = bought()
    assert s["positions"]["002384"]["quantity"] == 9900
    assert s["cash"] == pytest.approx(875.23)
    assert s["trades"][0]["price"] == 10.01
    assert s["trades"][0]["fees"] == 25.77
    submit(s, "sell", "002384", 0.25, "sell1", START + timedelta(seconds=10))
    o = s["orders"][-1]
    t = START + timedelta(seconds=20)
    match_order(s, o, quote(t, bid=9.75), t)
    assert o["status"] == "pending" and "T+1" in o["reason"]
    assert len(s["trades"]) == 1
    t = START + timedelta(days=1, minutes=-30)
    match_order(s, o, quote(t, bid=9.75), t)
    assert o["status"] == "filled"
    assert s["positions"]["002384"]["quantity"] == 7500
    assert s["trades"][-1]["fees"] == 17.77
    assert s["cash"] == pytest.approx(875.23 + 2400 * 9.74 - 17.77)


def test_rerun_never_duplicates_and_cancel_does_not_fill():
    s = bought()
    old = deepcopy(s)
    submit(s, "buy", "002384", 1, "buy1", START)
    match_order(s, s["orders"][0], quote(START), START)
    assert s == old
    submit(s, "buy", "600000", 0.5, "buy2", START)
    submit(s, "cancel", "600000", 0.5, "cancel2", START)
    assert s["orders"][1]["status"] == "cancelled"
    assert len(s["trades"]) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "before_confirmation",
        "stale",
        "suspended",
        "limit_up",
        "no_ask",
        "thin_book",
        "closed",
        "future",
    ],
)
def test_unfillable_quote_preserves_money(kind):
    s = account()
    submit(s, "buy", "002384", 1, "order", START)
    t = START + timedelta(minutes=1)
    q = quote(t)
    if kind == "before_confirmation":
        q["time"] = START.isoformat()
    if kind == "stale":
        q["time"] = (START - timedelta(minutes=3)).isoformat()
    if kind == "suspended":
        q["volume"] = 0
    if kind == "limit_up":
        q["ask"] = 11
    if kind == "no_ask":
        q["ask"] = 0
    if kind == "thin_book":
        q["ask_lots"] = 1
    if kind == "closed":
        t = START.replace(hour=12)
    if kind == "future":
        q["time"] = (t + timedelta(seconds=1)).isoformat()
    match_order(s, s["orders"][0], q, t)
    assert s["orders"][0]["status"] == "pending"
    assert s["cash"] == 100000 and not s["trades"] and not s["positions"]


def test_expiry_and_pending_position_limit():
    s = account()
    for i, code in enumerate(["002384", "600000", "300750"]):
        submit(s, "buy", code, 0.25, str(i), START)
    with pytest.raises(ValueError, match="3只"):
        submit(s, "buy", "688981", 0.25, "4", START)
    match_order(s, s["orders"][0], None, START + timedelta(hours=25))
    assert s["orders"][0]["status"] == "expired"


@pytest.mark.parametrize("code", ["510300", "900901", "920001", "002384;echo", "123456"])
def test_only_ashare_stock_codes(code):
    with pytest.raises(ValueError):
        submit(account(), "buy", code, 0.5, "bad", START)


def test_star_minimum_and_missing_marks_not_false_return():
    s = account() | {"cash": 1500}
    submit(s, "buy", "688981", 1, "star", START)
    t = START + timedelta(seconds=5)
    match_order(s, s["orders"][0], quote(t, code="688981"), t)
    assert s["orders"][0]["status"] == "rejected"
    s = bought()
    refresh(s, t, fetch=lambda code: (_ for _ in ()).throw(ConnectionError()))
    assert s["equity"] is None and s["return_pct"] is None


def test_stop_priority_holding_time_and_peak():
    s = bought()
    assert review_positions(s, {"002384": 9.4}, START)[0]["fraction"] == 1
    assert review_positions(s, {"002384": 10.1}, START + timedelta(days=14))[0]["why"].startswith(
        "持有"
    )
    review_positions(s, {"002384": 11}, START)
    assert s["positions"]["002384"]["peak"] == 11


def test_snapshot_requires_timestamp_book_and_price_limits():
    p = ["0"] * 55
    for index, value in {
        1: "东山精密",
        2: "002384",
        3: "10",
        4: "10",
        6: "1000",
        9: "9.99",
        10: "100",
        19: "10.01",
        20: "200",
        30: "20260910100100",
        47: "11",
        48: "9",
    }.items():
        p[index] = value
    q = parse_snapshot("~".join(p), "002384")
    assert q["ask_lots"] == 200 and q["time"] == "2026-09-10T10:01:00+08:00"
    p[47] = "nan"
    with pytest.raises(ValueError):
        parse_snapshot("~".join(p), "002384")


def test_wencai_uses_code_column_not_unrelated_six_digit_number(monkeypatch):
    import sys
    from types import SimpleNamespace
    import pandas as pd
    from scripts.short_term_daily import _wencai_codes

    result = pd.DataFrame({'成交额': ['987654321'], '股票代码': ['002384.SZ']})
    monkeypatch.setenv('WENCAI_COOKIE', 'test-only')
    monkeypatch.setattr('quanti.wencai_client.configure_runtime', lambda: None)
    monkeypatch.setitem(sys.modules, 'pywencai', SimpleNamespace(get=lambda **kw: result))
    assert _wencai_codes() == {'002384'}


def test_wencai_denied_is_visible_and_does_not_approve_candidates(monkeypatch):
    import sys
    from types import SimpleNamespace
    from scripts import short_term_daily as research
    monkeypatch.setenv('WENCAI_COOKIE', 'test-only')
    monkeypatch.setattr('quanti.wencai_client.configure_runtime', lambda: None)
    monkeypatch.setattr('quanti.wencai_client.last_error', '问财接口拒绝请求（HTTP 403），未参与本次加分')
    monkeypatch.setitem(sys.modules, 'pywencai', SimpleNamespace(get=lambda **kw: None))
    assert research._wencai_codes() == set()
    assert 'HTTP 403' in research.WENCAI_STATUS
