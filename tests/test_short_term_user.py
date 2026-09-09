from scripts.paper_trade import execute


def test_confirmed_paper_trade_and_staged_sell():
    state = {"cash": 100_000, "positions": {}, "trades": []}
    execute(state, "buy", "002384", 10.0, 1.0, "东山精密")
    assert state["positions"]["002384"]["quantity"] == 9900
    assert state["cash"] > 0

    execute(state, "sell", "002384", 9.75, 0.25, stage="stop1")
    pos = state["positions"]["002384"]
    assert pos["quantity"] == 7500
    assert pos["exit_stages"] == ["stop1"]
