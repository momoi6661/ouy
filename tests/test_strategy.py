"""策略执行器：Agent 布置策略，执行器按规则自动触发和管理，不调用模型。"""
from __future__ import annotations

import time

import pytest

from ouy.models import AgentConfig, RiskLimits, TradeAction
from ouy.trader import Rejected

BTC = "BTC-USDT-SWAP"


async def tick(rt, fake, price: float) -> None:
    fake.set_price(BTC, price)
    rt.trader._lot_touch.clear()
    await rt.watcher.strategies.tick(rt.config(), await rt.trader.account("demo"), {BTC: price})


def live(rt) -> list[dict]:
    return rt.store.strategies("demo")


def one(rt, sid: int) -> dict:
    return rt.store.get_strategy(sid)


async def test_breakout_fires_the_moment_price_crosses_and_then_manages_the_trade(rt, fake):
    """突破追单：价格没到不动；一到触发价立刻市价进场；之后到 1R 移保本、到 2R 平一半，全程不调用模型。"""
    engine = rt.watcher.strategies
    row = engine.deploy(rt.config(), BTC, "breakout", "long", {
        "trigger": 60_500, "stop_loss": 60_200, "take_profit": 62_000, "risk_pct": 0.5, "leverage": 5,
        "manage": {"breakeven_r": 1, "partials": [{"r": 2, "fraction": 0.5}]},
    })
    assert row["status"] == "armed" and "≥ 60500" in engine.describe(row)

    await tick(rt, fake, 60_300)
    assert BTC not in fake.positions and one(rt, row["id"])["status"] == "armed"

    await tick(rt, fake, 60_520)
    assert fake.positions[BTC]["pos"] > 0 and one(rt, row["id"])["status"] == "active"
    size = fake.positions[BTC]["pos"]
    lot = rt.trader.lots("demo", BTC)[0]
    assert lot["source"] == "agent" and lot["stop_loss"] == 60_200

    await tick(rt, fake, 60_850)  # 入场 60520，R = 320：到 1R 移保本
    assert rt.trader.lots("demo", BTC)[0]["stop_loss"] == pytest.approx(60_520)
    await tick(rt, fake, 61_200)  # 到 2R 平一半
    assert fake.positions[BTC]["pos"] == pytest.approx(size / 2, rel=0.02)
    await tick(rt, fake, 61_250)  # 同一条规则不会做第二次
    assert fake.positions[BTC]["pos"] == pytest.approx(size / 2, rel=0.02)

    kinds = [s["kind"] for s in rt.store.activity("demo") if s["agent"] == "strategy"]
    assert kinds.count("action") == 3 and kinds[0] == "wake"

    await tick(rt, fake, 62_100)  # 止盈单触发，仓位没了，策略结束
    await tick(rt, fake, 62_100)
    assert one(rt, row["id"])["status"] == "done" and live(rt) == []


async def test_pullback_waits_for_price_to_come_back_and_dies_if_structure_breaks(rt, fake):
    engine = rt.watcher.strategies
    short = engine.deploy(rt.config(), BTC, "pullback", "short", {"trigger": 60_400, "stop_loss": 60_700, "risk_pct": 0.3})
    await tick(rt, fake, 60_100)
    assert one(rt, short["id"])["status"] == "armed" and BTC not in fake.positions
    await tick(rt, fake, 60_400)  # 刚好反弹到触发价：做空
    assert fake.positions[BTC]["pos"] < 0 and one(rt, short["id"])["status"] == "done"  # 没带管理规则，进场后交给止损单和 Worker

    fake._flatten(BTC)
    long = engine.deploy(rt.config(), BTC, "pullback", "long", {"trigger": 59_500, "stop_loss": 59_200, "expires_minutes": 30})
    await tick(rt, fake, 59_100)  # 没进场就直接跌穿止损位：作废，不接飞刀
    assert one(rt, long["id"])["status"] == "cancelled" and BTC not in fake.positions


async def test_short_breakout_opens_at_exact_trigger(rt, fake):
    row = rt.watcher.strategies.deploy(rt.config(), BTC, "breakout", "short", {"trigger": 59_800, "stop_loss": 60_100})
    await tick(rt, fake, 59_801)
    assert one(rt, row["id"])["status"] == "armed" and BTC not in fake.positions
    await tick(rt, fake, 59_800)
    assert one(rt, row["id"])["status"] == "done" and fake.positions[BTC]["pos"] < 0


async def test_range_trades_both_edges_and_stops_when_the_range_breaks(rt, fake):
    engine = rt.watcher.strategies
    row = engine.deploy(rt.config(), BTC, "range", "", {"low": 59_500, "high": 60_500, "stop_low": 59_200, "stop_high": 60_800, "max_rounds": 2, "risk_pct": 0.3})
    await tick(rt, fake, 60_000)
    assert BTC not in fake.positions
    await tick(rt, fake, 59_550)  # 到下沿：做多，目标区间中轨
    assert fake.positions[BTC]["pos"] > 0 and rt.trader.lots("demo", BTC)[0]["take_profit"] == pytest.approx(60_000)
    await tick(rt, fake, 60_050)  # 止盈
    await tick(rt, fake, 60_050)
    assert BTC not in fake.positions and one(rt, row["id"])["state"]["rounds"] == 1
    await tick(rt, fake, 60_480)  # 到上沿：做空
    assert fake.positions[BTC]["pos"] < 0
    await tick(rt, fake, 60_850)  # 向上突破，止损出场；做满 2 轮，结束
    await tick(rt, fake, 60_850)
    assert BTC not in fake.positions and one(rt, row["id"])["status"] == "done"


async def test_martingale_doubles_down_in_layers_up_to_the_cap(rt, fake):
    """翻倍补仓：每逆向一段补一层、保证金翻倍；到层数上限就不再补；整组止损是硬的。"""
    cfg = rt.config()
    rt.save_config(cfg.model_copy(update={"risk": RiskLimits(max_leverage=5, risk_per_trade_pct=10, max_adds=5, allow_averaging_down=True, max_position_notional_x=5, max_total_notional_x=8)}))
    engine = rt.watcher.strategies
    row = engine.deploy(rt.config(), BTC, "martingale", "long", {
        "base_margin": 50, "step_pct": 1, "multiplier": 2, "max_layers": 3, "take_profit_pct": 0.8, "stop_loss": 55_000, "leverage": 3,
    })
    await tick(rt, fake, 60_000)  # 第一层马上开
    first = fake.positions[BTC]["pos"]
    assert first > 0 and len(one(rt, row["id"])["state"]["layers"]) == 1
    await tick(rt, fake, 59_700)  # 才跌 0.5%，不补
    assert fake.positions[BTC]["pos"] == first
    await tick(rt, fake, 59_350)  # 跌过 1%：第二层，保证金翻倍
    second = fake.positions[BTC]["pos"] - first
    assert second == pytest.approx(first * 2 * 60_000 / 59_350, rel=0.03)
    await tick(rt, fake, 58_700)  # 第三层
    await tick(rt, fake, 58_000)  # 已经 3 层，到上限不再补
    state = one(rt, row["id"])
    assert len(state["state"]["layers"]) == 3 and "补满" in state["note"]
    total = fake.positions[BTC]["pos"]
    await tick(rt, fake, 57_500)
    assert fake.positions[BTC]["pos"] == total
    avg = state["state"]["avg"]
    await tick(rt, fake, avg * 1.009)  # 回到均价上方 0.8%：整组止盈
    await tick(rt, fake, avg * 1.009)
    assert BTC not in fake.positions and one(rt, row["id"])["status"] == "done"


async def test_martingale_is_blocked_by_the_averaging_down_switch(rt, fake):
    """风控不允许亏损加仓时，程序马丁布置不上：这是用户才能改的硬开关，策略绕不过去。"""
    engine = rt.watcher.strategies
    with pytest.raises(Rejected, match="允许亏损加仓"):
        engine.deploy(rt.config(), BTC, "martingale", "long", {"stop_loss": 55_000})
    assert not live(rt)


MARTINGALE_RISK = RiskLimits(max_leverage=5, risk_per_trade_pct=2, max_adds=3, allow_averaging_down=True, max_position_notional_x=5, max_total_notional_x=8)


async def test_martingale_takes_over_a_losing_position_and_exits_at_breakeven(rt, fake):
    """保本解套：已经有一笔浮亏的多单时，Worker 只给方向和整组止损，程序接手——不另开第一层，
    逆向每走一段补一层把均价拉低，回到均价上方一点（保本位）整组平掉；原来的移保本 / 超时离场规则让位。"""
    rt.save_config(rt.config().model_copy(update={"risk": MARTINGALE_RISK.model_copy(update={"risk_per_trade_pct": 5})}))
    engine = rt.watcher.strategies
    opened = await rt.trader.execute(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_400, take_profit=61_200, leverage=3, risk_pct=0.2, reason="测试"), "demo", source="agent")
    assert opened.status == "executed"
    first = fake.positions[BTC]["pos"]
    manage = engine.deploy(rt.config(), BTC, "manage", "", {"breakeven_r": 1, "max_hold_minutes": 30})
    await tick(rt, fake, 59_700)  # 浮亏 0.5%

    row = engine.deploy(rt.config(), BTC, "martingale", "long", {"stop_loss": 57_000}, note="震荡区间下沿，结构没破，用程序马丁解套")
    assert one(rt, manage["id"])["status"] == "cancelled"  # 这笔仓改由马丁管
    assert "整组止损 57000" in engine.describe(row) and row["params"]["take_profit_pct"] == pytest.approx(0.15)

    await tick(rt, fake, 59_690)  # 接手：没有另开第一层，间距按到整组止损的距离平均分
    state = one(rt, row["id"])["state"]
    assert state["rescue"] and len(state["layers"]) == 1 and fake.positions[BTC]["pos"] == first
    assert state["max_layers"] == 4 and 1.0 < state["step_pct"] < 1.3

    await tick(rt, fake, 59_300)  # 离均价 1.17%，过了间距：补第一次，按原仓位保证金的 1.6 倍
    assert fake.positions[BTC]["pos"] == pytest.approx(first * (1 + 1.6 * 60_000 / 59_300), rel=0.03)
    await tick(rt, fake, 59_290)  # 下一拍：保本止盈挪到新均价上方 0.15%
    state = one(rt, row["id"])
    avg = fake.positions[BTC]["avgPx"]
    assert avg < 59_600 and state["state"]["tp"] == pytest.approx(avg * 1.0015) and "解套中" in state["note"]
    assert any(float(a.get("tpTriggerPx") or 0) == pytest.approx(avg * 1.0015, rel=1e-4) for a in fake.algos)

    await tick(rt, fake, avg * 1.002)  # 小反弹回到保本位：整组平掉
    await tick(rt, fake, avg * 1.002)
    done = one(rt, row["id"])
    assert BTC not in fake.positions and done["status"] == "done"


async def test_martingale_opens_its_own_first_layer_sized_to_the_risk_cap(rt, fake):
    """空仓时用：只给方向和整组止损，第一层的大小按「全补满又打到整组止损时不超过单仓风险上限」来分；风险额度用完就不再硬补。"""
    rt.save_config(rt.config().model_copy(update={"risk": MARTINGALE_RISK}))
    engine = rt.watcher.strategies
    row = engine.deploy(rt.config(), BTC, "martingale", "long", {"stop_loss": 57_000, "max_layers": 3, "leverage": 3})
    await tick(rt, fake, 60_000)
    state = one(rt, row["id"])["state"]
    assert len(state["layers"]) == 1 and state["step_pct"] == pytest.approx(5 / 3, rel=0.01)
    equity = (await rt.trader.account("demo")).equity
    risk_at_stop = fake.positions[BTC]["pos"] * 0.01 * (60_000 - 57_000)
    assert risk_at_stop == pytest.approx(equity * 0.02 / (1 + 1.6 + 2.56), rel=0.05)  # 第一层只占整组风险的一份

    for px in (59_990, 58_950, 58_940, 57_900, 57_890, 57_300):
        await tick(rt, fake, px)
    state = one(rt, row["id"])
    assert len(state["state"]["layers"]) == 3 and "补满" in state["note"]
    total_risk = fake.positions[BTC]["pos"] * 0.01 * (fake.positions[BTC]["avgPx"] - 57_000)
    assert total_risk <= equity * 0.02 * 1.02  # 整组打到止损，亏的仍在单仓风险上限以内

    await tick(rt, fake, 56_900)  # 整组止损触发
    await tick(rt, fake, 56_900)
    assert BTC not in fake.positions and one(rt, row["id"])["status"] == "done"


async def test_grid_buys_a_step_down_and_sells_a_step_up(rt, fake):
    cfg = rt.config()
    rt.save_config(cfg.model_copy(update={"risk": RiskLimits(max_leverage=5, risk_per_trade_pct=10, max_adds=8, allow_averaging_down=True, max_position_notional_x=5, max_total_notional_x=8)}))
    engine = rt.watcher.strategies
    row = engine.deploy(rt.config(), BTC, "grid", "long", {"low": 59_000, "high": 61_000, "grids": 4, "margin_per_grid": 100, "stop_loss": 58_000, "leverage": 3})
    await tick(rt, fake, 60_200)  # 记下起点，不动
    assert BTC not in fake.positions
    await tick(rt, fake, 59_900)  # 跌破 60000 这条线：买一份
    one_unit = fake.positions[BTC]["pos"]
    assert one_unit > 0
    await tick(rt, fake, 59_400)  # 再跌破 59500：再买一份
    assert fake.positions[BTC]["pos"] == pytest.approx(one_unit * 2, rel=0.03)
    await tick(rt, fake, 60_100)  # 涨回 60000 上方：卖掉 59500 买的那一份
    assert fake.positions[BTC]["pos"] == pytest.approx(one_unit, rel=0.03)
    await tick(rt, fake, 60_600)  # 涨过 60500：卖掉 60000 买的那一份
    assert BTC not in fake.positions or abs(fake.positions[BTC]["pos"]) < 1e-6
    assert one(rt, row["id"])["status"] == "active"
    await tick(rt, fake, 57_900)  # 跌穿网格止损：停
    assert one(rt, row["id"])["status"] == "done"


async def test_strategies_respect_permissions_and_can_be_approved_or_cancelled(rt, fake):
    engine = rt.watcher.strategies
    rt.save_config(AgentConfig(mode="confirm"))
    row = engine.deploy(rt.config(), BTC, "breakout", "long", {"trigger": 60_500, "stop_loss": 60_200})
    assert row["status"] == "proposed"
    await tick(rt, fake, 60_520)
    assert BTC not in fake.positions  # 没同意之前不会触发
    engine.approve(row["id"])
    await tick(rt, fake, 60_520)
    assert fake.positions[BTC]["pos"] > 0

    rt.save_config(AgentConfig(mode="advise"))  # 只给建议：策略也要你点同意才启用
    assert engine.deploy(rt.config(), BTC, "pullback", "short", {"trigger": 61_000, "stop_loss": 61_300})["status"] == "proposed"
    rt.save_config(AgentConfig(mode="auto", permissions={"open": "off"}))  # 开仓设成「不允许」：不能布置
    with pytest.raises(Rejected):
        engine.deploy(rt.config(), BTC, "pullback", "long", {"trigger": 59_500, "stop_loss": 59_200})
    with pytest.raises(Rejected):  # 止损放错边
        engine.deploy(AgentConfig(mode="auto"), BTC, "breakout", "long", {"trigger": 60_500, "stop_loss": 60_900})

    rt.save_config(AgentConfig(mode="auto"))
    a = engine.deploy(rt.config(), BTC, "pullback", "short", {"trigger": 61_500, "stop_loss": 61_900})
    b = engine.deploy(rt.config(), BTC, "pullback", "short", {"trigger": 61_800, "stop_loss": 62_200})  # 同类的新策略替换旧的
    assert one(rt, a["id"])["status"] == "cancelled" and [s["id"] for s in live(rt)] == [b["id"]]
    assert engine.cancel(b["id"], "行情变了")["status"] == "cancelled" and live(rt) == []


async def test_reverse_flips_after_the_stop_is_hit_and_price_keeps_going(rt, fake):
    """止损后反手：原多单被止损、价格继续跌到触发价 → 程序自动开空；没到触发价不动。名义价值默认不设上限。"""
    assert rt.config().risk.max_position_notional_x == 0
    engine = rt.watcher.strategies
    await rt.trader.execute(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_700, take_profit=61_000, leverage=5, risk_pct=0.5, reason="测试"), "demo", source="agent")
    row = engine.deploy(rt.config(), BTC, "reverse", "short", {"trigger": 59_600, "stop_loss": 60_050, "take_profit": 58_800, "leverage": 5, "risk_pct": 0.5})
    assert "被止损后" in engine.describe(row)
    with pytest.raises(Rejected, match="止损放错边"):
        engine.deploy(rt.config(), BTC, "reverse", "short", {"trigger": 59_600, "stop_loss": 59_000})
    await tick(rt, fake, 59_900)
    assert one(rt, row["id"])["state"]["seen"] and fake.positions[BTC]["pos"] > 0
    await tick(rt, fake, 59_650)  # 止损被打，但还没到反手触发价
    assert BTC not in fake.positions and one(rt, row["id"])["status"] == "armed"
    await tick(rt, fake, 59_590)  # 继续跌到触发价：反手做空
    assert fake.positions[BTC]["pos"] < 0 and one(rt, row["id"])["status"] == "done"


async def test_reverse_is_dropped_when_the_position_exits_in_profit(rt, fake):
    engine = rt.watcher.strategies
    await rt.trader.execute(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_700, take_profit=60_600, leverage=5, risk_pct=0.5, reason="测试"), "demo", source="agent")
    row = engine.deploy(rt.config(), BTC, "reverse", "short", {"trigger": 59_600, "stop_loss": 60_050})
    await tick(rt, fake, 60_100)
    await tick(rt, fake, 60_650)  # 止盈走的
    assert BTC not in fake.positions and one(rt, row["id"])["status"] == "cancelled"


async def test_manual_loss_then_break_does_not_reverse(rt, fake):
    engine = rt.watcher.strategies
    await rt.trader.execute(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_700, leverage=5), "demo", source="agent")
    row = engine.deploy(rt.config(), BTC, "reverse", "short", {"trigger": 59_600, "stop_loss": 60_050})
    await tick(rt, fake, 59_850)
    await rt.trader.execute(TradeAction(kind="close", inst_id=BTC), "demo", source="user")
    await tick(rt, fake, 59_590)
    state = one(rt, row["id"])["state"]
    assert BTC not in fake.positions and state["flat_seen"]
    rt.store.update_strategy(row["id"], state={**state, "flat_seen": time.time() - 31})
    await tick(rt, fake, 59_590)
    assert BTC not in fake.positions and one(rt, row["id"])["status"] == "cancelled"


async def test_thesis_review_uses_only_the_lot_timeframe_and_notifies_once(rt, fake):
    engine = rt.watcher.strategies
    opened = await rt.trader.execute(TradeAction(kind="open", inst_id=BTC, side="short", stop_loss=60_500, leverage=5), "demo", source="agent")
    row = engine.deploy(rt.config(), BTC, "manage", "", {"lot_id": opened.detail["lot_id"], "review_level": 60_200,
                         "review_bar": "15m", "breakeven_r": 1})
    await tick(rt, fake, 60_250)
    rt.store.update_strategy(row["id"], state={**one(rt, row["id"])["state"], "since": time.time() - 3600})
    end = int(time.time() * 1000)
    bars = {"5m": {"end": end, "price": 60_250}, "15m": {"end": end - 900_000, "price": 60_250}}
    engine.metrics[BTC] = {"structure_closes": bars}
    await tick(rt, fake, 60_250)
    assert "review_end" not in one(rt, row["id"])["state"]
    bars["15m"]["end"] = end
    await tick(rt, fake, 60_250)
    await tick(rt, fake, 60_100)
    assert len([a for a in rt.store.activity("demo") if "越过原计划复核线" in a["text"] and a["agent"] == "strategy"]) == 1
    assert fake.positions[BTC]["pos"] < 0 and one(rt, row["id"])["status"] == "active"


async def test_rule_bound_to_a_closed_lot_retires_quietly_and_algo_events_are_checked(rt, fake):
    """绑在某一笔仓上的触发线：那笔仓平掉以后跟着作废，不当成出错报警。算法事件带方向的，15 分钟后核对对错。"""
    engine = rt.watcher.strategies
    opened = await rt.trader.execute(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_700, leverage=5), "demo", source="agent")
    lot = opened.detail["lot_id"]
    row = engine.deploy(rt.config(), BTC, "trigger", "", {"when": "price_above", "level": 60_300, "do": "set_stops", "stop_loss": 60_000, "lot_id": lot})
    await rt.trader.execute(TradeAction(kind="close", inst_id=BTC), "demo", source="user")
    await tick(rt, fake, 60_100)
    await tick(rt, fake, 60_400)
    done = one(rt, row["id"])
    assert done["status"] == "cancelled" and "跟着作废" in done["note"] and not fake.called("swap_amend_algo_order")

    rt.watcher._algo("demo", BTC, "shift", "收盘站上前高", "叫醒 Worker", "long", 60_000, 100)
    ev = rt.store.algo_events("demo", BTC)[0]
    rt.store._exec("UPDATE algo_events SET ts=? WHERE id=?", (ev["ts"] - 16 * 60_000, ev["id"]))
    rt.watcher._algo_check("demo", {BTC: 60_200})
    report = rt.watcher.algo_report("demo", BTC)
    assert report["events"][0]["checks"]["15m"]["ok"] == "right" and report["stats"]["turn"]["right"] == 1  # 旧记录里的「结构转折」归到「转折」
    assert any(a["id"] == "turn" for a in rt.watcher.status()["instruments"][BTC]["algos"])
