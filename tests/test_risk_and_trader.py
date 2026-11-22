from __future__ import annotations

import pytest

from ouy.models import AccountState, AgentConfig, Instrument, Position, RiskLimits, TradeAction
from ouy.risk import Rejected, size_entry

BTC = "BTC-USDT-SWAP"
ETH = "ETH-USDT-SWAP"
INST = Instrument(inst_id=BTC, inst_type="SWAP", ct_val=0.01, ct_type="linear", settle_ccy="USDT", lot_sz="0.01", min_sz="0.01", tick_sz="0.1", max_leverage=100)


def acct(**kw) -> AccountState:
    return AccountState(env="demo", equity=10_000, available=10_000, **kw)


def open_action(**kw) -> TradeAction:
    return TradeAction(**{"kind": "open", "inst_id": BTC, "side": "long", "stop_loss": 59_400, "leverage": 5, **kw})


def test_size_is_derived_from_stop_distance():
    sized = size_entry(open_action(), account=acct(), inst=INST, limits=RiskLimits(), last_px=60_000, current_leverage=3)
    # 风险 1% = 100U，止损距离 600U，每张 0.01 BTC → 100 / (600*0.01) = 16.66 张
    assert str(sized.contracts) == "16.66"
    assert sized.risk_usd == pytest.approx(99.96, abs=0.01)
    assert sized.leverage == 5


def test_notional_cap_shrinks_size():
    limits = RiskLimits(max_position_notional_x=0.5)
    sized = size_entry(open_action(), account=acct(), inst=INST, limits=limits, last_px=60_000, current_leverage=3)
    assert float(sized.contracts) * 0.01 * 60_000 <= 5_000
    assert any("名义价值" in n for n in sized.notes)


@pytest.mark.parametrize(
    "kwargs, text",
    [
        ({"stop_loss": None}, "止损"),
        ({"stop_loss": 60_500}, "低于"),
        ({"side": "short", "stop_loss": 59_000}, "高于"),
        ({"stop_loss": 59_990}, "太近"),
        ({"stop_loss": 54_000, "leverage": 10}, "爆仓"),
        ({"take_profit": 59_000}, "止盈"),
        ({"order_type": "limit", "price": 61_000}, "立刻成交"),
    ],
)
def test_bad_entries_are_rejected(kwargs, text):
    with pytest.raises(Rejected, match=text):
        size_entry(open_action(**kwargs), account=acct(), inst=INST, limits=RiskLimits(), last_px=60_000, current_leverage=3)


def test_leverage_is_clamped_to_limit():
    sized = size_entry(open_action(leverage=50), account=acct(), inst=INST, limits=RiskLimits(max_leverage=10), last_px=60_000, current_leverage=3)
    assert sized.leverage == 10


def test_daily_loss_halt_blocks_new_entries():
    with pytest.raises(Rejected, match="今日亏损"):
        size_entry(open_action(), account=acct(halted=True), inst=INST, limits=RiskLimits(), last_px=60_000, current_leverage=3)


def _pos(upl: float, avg: float = 60_000, contracts: float = 16.66) -> Position:
    return Position(inst_id=BTC, side="long", contracts=contracts, avg_px=avg, mark_px=61_000, upl=upl, upl_ratio=0.1, leverage=5, notional_usd=contracts * 0.01 * 61_000)


def test_adding_to_a_loser_is_rejected():
    with pytest.raises(Rejected, match="亏损加仓"):
        size_entry(open_action(kind="add"), account=acct(positions=[_pos(-50)]), inst=INST, limits=RiskLimits(), last_px=59_800, current_leverage=5)


def test_add_needs_stop_moved_to_free_risk():
    account = acct(positions=[_pos(166.6)])
    # 止损没动：原持仓风险已占满额度 → 拒绝
    with pytest.raises(Rejected, match="腾出额度"):
        size_entry(open_action(kind="add", stop_loss=59_400), account=account, inst=INST, limits=RiskLimits(), last_px=61_000, current_leverage=5)
    # 止损移到保本之上：原持仓锁定利润，腾出额度 → 可以加
    sized = size_entry(open_action(kind="add", stop_loss=60_300), account=account, inst=INST, limits=RiskLimits(), last_px=61_000, current_leverage=5)
    total_loss = 16.66 * 0.01 * (60_000 - 60_300) + float(sized.contracts) * 0.01 * (61_000 - 60_300)
    assert total_loss <= 100.0 + 1e-6


def test_max_adds():
    with pytest.raises(Rejected, match="加仓 2 次"):
        size_entry(open_action(kind="add", stop_loss=60_300), account=acct(positions=[_pos(100)]), inst=INST, limits=RiskLimits(), last_px=61_000, current_leverage=5, adds_done=2)


# ---------- 执行层（对着假交易所） ----------

async def test_open_attaches_stop_and_sets_leverage(rt, fake):
    result = await rt.trader.submit(open_action(take_profit=62_000), source="agent")
    assert result.status == "executed", result.message
    assert fake.leverage[BTC] == 5
    order = fake.called("swap_place_order")[0]
    assert order["sz"] == "16.66" and order["slTriggerPx"] == "59400" and order["slOrdPx"] == "-1" and order["tpTriggerPx"] == "62000"
    assert fake.positions[BTC]["pos"] == pytest.approx(16.66)
    account = await rt.trader.account()
    assert account.position(BTC).side == "long" and account.protections[0].stop_loss == 59_400


async def test_stop_can_tighten_but_not_loosen(rt, fake):
    await rt.trader.submit(open_action(), source="agent")
    loose = await rt.trader.submit(TradeAction(kind="set_stops", inst_id=BTC, stop_loss=58_000), source="agent")
    assert loose.status == "rejected" and "只能收紧" in loose.message
    fake.set_price(BTC, 60_800)
    tight = await rt.trader.submit(TradeAction(kind="set_stops", inst_id=BTC, stop_loss=60_050, take_profit=63_000), source="agent")
    assert tight.status == "executed", tight.message
    assert len(fake.algos) == 1  # 新的挂上、旧的撤掉
    assert fake.algos[0]["slTriggerPx"] == "60050" and fake.algos[0]["closeFraction"] == "1" and fake.algos[0]["ordType"] == "oco"
    # 第二次修改：已经有整仓保护单，不能再挂一张（51088），只应改原单止盈。
    original = fake.algos[0]["algoId"]
    writes = len(fake.called("swap_place_algo_order")), len(fake.called("swap_cancel_algo_orders"))
    changed = await rt.trader.submit(TradeAction(kind="set_stops", inst_id=BTC, take_profit=64_000), source="agent")
    assert changed.status == "executed" and fake.algos[0]["algoId"] == original
    assert fake.algos[0]["tpTriggerPx"] == "64000" and fake.algos[0]["slTriggerPx"] == "60050"
    assert "newSlTriggerPx" not in fake.called("swap_amend_algo_order")[-1]
    fake.fail["swap_amend_algo_order"] = "price rejected"
    failed = await rt.trader.submit(TradeAction(kind="set_stops", inst_id=BTC, take_profit=65_000), source="agent")
    assert not failed.ok and fake.algos[0]["tpTriggerPx"] == "64000"
    assert writes == (len(fake.called("swap_place_algo_order")), len(fake.called("swap_cancel_algo_orders")))


async def test_pyramid_add_then_reduce_then_close(rt, fake):
    await rt.trader.submit(open_action(), source="agent")
    fake.set_price(BTC, 61_000)
    add = await rt.trader.submit(TradeAction(kind="add", inst_id=BTC, side="long", stop_loss=60_300), source="agent")
    assert add.status == "executed", add.message
    assert fake.positions[BTC]["pos"] > 16.66
    assert {a["slTriggerPx"] for a in fake.algos} == {"60300"}  # 同风格加仓：旧的那一笔止损跟着收紧
    assert abs(sum(float(a["sz"]) for a in fake.algos) - fake.positions[BTC]["pos"]) < 1e-9  # 每笔各一张，合计正好是整个持仓
    before = fake.positions[BTC]["pos"]
    reduce = await rt.trader.submit(TradeAction(kind="reduce", inst_id=BTC, fraction=0.5), source="agent")
    assert reduce.status == "executed" and fake.positions[BTC]["pos"] == pytest.approx(before / 2, abs=0.01)
    close = await rt.trader.submit(TradeAction(kind="close", inst_id=BTC), source="agent")
    assert close.status == "executed" and BTC not in fake.positions and not fake.algos
    assert fake.cash > 10_000


async def test_confirm_mode_queues_entries_but_exits_run_immediately(rt, fake):
    rt.save_config(AgentConfig(mode="confirm"))
    queued = await rt.trader.submit(open_action(), source="agent")
    assert queued.status == "pending" and BTC not in fake.positions
    approved = await rt.trader.approve(queued.pending_id)
    assert approved.status == "executed" and BTC in fake.positions
    with pytest.raises(Rejected):
        await rt.trader.approve(queued.pending_id)  # 不能重复执行
    close = await rt.trader.submit(TradeAction(kind="close", inst_id=BTC), source="agent")
    assert close.status == "executed" and BTC not in fake.positions


async def test_confirm_mode_does_not_queue_orders_the_guard_would_reject(rt, fake):
    rt.save_config(AgentConfig(mode="confirm"))
    result = await rt.trader.submit(open_action(stop_loss=61_000), source="agent")
    assert result.status == "rejected" and not rt.store.pending("demo")


async def test_advise_mode_and_watchlist(rt, fake):
    rt.save_config(AgentConfig(mode="advise"))  # 只给建议：每个动作都变成一条等用户点同意的建议，什么都不自动做
    held = await rt.trader.submit(open_action(), source="agent")
    assert held.status == "pending" and held.pending_id and BTC not in fake.positions
    rt.save_config(AgentConfig(mode="auto", permissions={"open": "off"}))  # 单独设成「不允许」的动作：不执行，也不出卡片
    assert (await rt.trader.submit(open_action(), source="agent")).status == "advised"
    rt.save_config(AgentConfig(mode="auto", instruments=["ETH-USDT-SWAP"]))
    assert (await rt.trader.submit(open_action(), source="agent")).status == "rejected"
    assert not fake.called("swap_place_order")


async def test_exchange_error_is_reported_not_swallowed(rt, fake):
    fake.fail["swap_place_order"] = "Insufficient margin"
    result = await rt.trader.submit(open_action(), source="agent")
    assert result.status == "failed" and "Insufficient margin" in result.message


async def test_panic_closes_everything(rt, fake):
    await rt.trader.submit(open_action(), source="agent")
    await rt.trader.submit(TradeAction(kind="open", inst_id="ETH-USDT-SWAP", side="short", stop_loss=3_060, order_type="limit", price=3_020), source="agent")
    assert fake.orders
    await rt.trader.panic("demo")
    assert not fake.positions and not fake.orders


async def test_per_action_permissions_override_the_mode(rt, fake):
    # 开仓自动，加仓要先问，调杠杆不允许
    rt.save_config(AgentConfig(mode="auto", permissions={"add": "confirm", "set_leverage": "off"}))
    assert (await rt.trader.submit(open_action(), source="agent")).status == "executed"
    fake.set_price(BTC, 61_000)
    add = await rt.trader.submit(TradeAction(kind="add", inst_id=BTC, side="long", stop_loss=60_300), source="agent")
    assert add.status == "pending" and fake.positions[BTC]["pos"] == pytest.approx(16.66)
    lever = await rt.trader.submit(TradeAction(kind="set_leverage", inst_id=BTC, leverage=3), source="agent")
    assert lever.status == "advised" and fake.leverage[BTC] == 5
    assert (await rt.trader.approve(add.pending_id)).status == "executed" and fake.positions[BTC]["pos"] > 16.66
    with pytest.raises(ValueError):
        AgentConfig(permissions={"withdraw": "auto"})



async def test_closed_trades_are_reported_with_realized_pnl(rt, fake):
    assert (await rt.trader.submit(open_action(), source="agent")).status == "executed"
    fake.set_price(BTC, 59_600)
    assert (await rt.trader.submit(TradeAction(kind="close", inst_id=BTC), source="agent")).status == "executed"
    trades = await rt.trader.history(BTC)
    assert len(trades) == 1 and trades[0].pnl < 0 and trades[0].side == "long" and trades[0].how == "全部平仓"
    # 亏损出场后不设冷静期：Agent 可以立刻再开仓
    assert (await rt.trader.submit(open_action(stop_loss=59_000), source="agent")).status == "executed"


async def test_margin_amount_sets_size_unless_a_risk_budget_is_also_requested(rt, fake):
    # 投入 200 USDT × 5 倍 = 1000 USDT 名义价值；BTC 一张 0.01 个 → 60000 时一张 600 USDT
    small = await rt.trader.submit(open_action(margin_usdt=200, margin_mode="isolated"), source="user")
    assert small.status == "executed" and fake.positions[BTC]["pos"] == pytest.approx(1.66)
    assert small.detail["margin_usd"] == pytest.approx(199.2) and fake.positions[BTC]["mgnMode"] == "isolated"
    await rt.trader.submit(TradeAction(kind="close", inst_id=BTC), source="user")

    big = await rt.trader.submit(open_action(margin_usdt=5000), source="agent")
    assert big.status == "executed" and fake.positions[BTC]["pos"] == pytest.approx(41.66)
    fake.set_price(BTC, 60_010)  # 不触发独立的亏损加仓限制
    add = await rt.trader.submit(open_action(kind="add", margin_usdt=200), source="user")
    assert add.status == "executed" and fake.positions[BTC]["pos"] == pytest.approx(43.32), add.message
    await rt.trader.submit(TradeAction(kind="close", inst_id=BTC), source="user")
    fake.set_price(BTC, 60_000)
    limited = await rt.trader.submit(open_action(margin_usdt=5000, risk_pct=1), source="agent")
    assert limited.status == "executed" and "已按上限缩小" in limited.message
    assert limited.detail["margin_usd"] < big.detail["margin_usd"]


async def test_each_lot_keeps_its_own_style_and_stops(rt, fake):
    """同一个币上一笔波段、一笔超短线：各自的止盈止损各管各的，超短线那笔到目标走了，波段那笔不受影响。"""
    swing = await rt.trader.submit(open_action(style="swing", take_profit=66_000, risk_pct=0.5), source="agent")
    fake.set_price(BTC, 60_050)
    scalp = await rt.trader.submit(TradeAction(kind="add", inst_id=BTC, side="long", stop_loss=59_800, take_profit=60_400, style="scalp", risk_pct=0.2), source="agent")
    assert swing.status == scalp.status == "executed", scalp.message
    lots = rt.trader.lots("demo", BTC)
    assert [(l["style"], l["stop_loss"], l["take_profit"]) for l in lots] == [("swing", 59_400, 66_000), ("scalp", 59_800, 60_400)]
    assert sorted(a["slTriggerPx"] for a in fake.algos) == ["59400", "59800"]  # 不同风格：加仓没有去动波段那笔的止损
    scalp_size = lots[1]["contracts"]

    # 只收紧超短线那一笔的止损
    moved = await rt.trader.submit(TradeAction(kind="set_stops", inst_id=BTC, lot_id=lots[1]["id"], stop_loss=59_900), source="agent")
    assert moved.status == "executed", moved.message
    assert sorted(a["slTriggerPx"] for a in fake.algos) == ["59400", "59900"]
    loosen = await rt.trader.submit(TradeAction(kind="set_stops", inst_id=BTC, lot_id=lots[1]["id"], stop_loss=59_500), source="agent")
    assert loosen.status == "rejected" and "只能收紧" in loosen.message

    # 超短线止盈触发：只平掉它自己那几张，对账后只剩波段那一笔
    before = fake.positions[BTC]["pos"]
    fake.set_price(BTC, 60_450)
    assert fake.positions[BTC]["pos"] == pytest.approx(before - scalp_size)
    rt.trader._lot_touch.clear()
    await rt.trader.account("demo")
    left = rt.trader.lots("demo", BTC)
    assert [l["style"] for l in left] == ["swing"] and left[0]["contracts"] == pytest.approx(fake.positions[BTC]["pos"])

    # 指定某一笔平仓；只剩这一笔时就是整仓平掉
    close = await rt.trader.submit(TradeAction(kind="close", inst_id=BTC, lot_id=left[0]["id"]), source="agent")
    assert close.status == "executed" and BTC not in fake.positions and not rt.trader.lots("demo", BTC)


async def test_positions_opened_elsewhere_are_booked_with_the_coin_style(rt, fake):
    rt.save_config(AgentConfig(styles={BTC: "scalp"}))
    fake._fill(BTC, 3.0, 60_000)  # 用户在欧易 App 里自己开的仓
    await rt.trader.account("demo")
    lots = rt.trader.lots("demo", BTC)
    assert [(l["style"], l["contracts"], l["source"]) for l in lots] == [("scalp", 3.0, "external")]
    fake._fill(BTC, -3.0, 60_100)
    await rt.trader.account("demo")
    assert rt.trader.lots("demo", BTC) == []


async def test_filling_one_side_of_a_bracket_cancels_the_other_side(rt, fake):
    """区间两头各挂一张限价单：一边成交后，另一边的挂单自动撤掉，记账只剩成交的那一笔。"""
    long = await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", order_type="limit", price=59_500, stop_loss=59_200, risk_pct=0.3), source="agent")
    short = await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="short", order_type="limit", price=60_500, stop_loss=60_800, risk_pct=0.3), source="agent")
    assert long.status == short.status == "executed", short.message
    assert sorted(l["status"] for l in rt.trader.lots("demo", BTC)) == ["working", "working"] and len(fake.orders) == 2

    buy = next(o for o in fake.orders if o["side"] == "buy")
    fake.orders.remove(buy)  # 下沿的多单成交
    fake.set_price(BTC, 59_500)
    fake._fill(BTC, float(buy["sz"]), 59_500)
    rt.trader._lot_touch.clear()
    state = await rt.trader.account("demo")

    assert fake.orders == [] and state.orders == []  # 上沿的空单被撤掉
    lots = rt.trader.lots("demo", BTC)
    assert [(l["side"], l["status"]) for l in lots] == [("long", "open")]
    assert lots[0]["contracts"] == pytest.approx(fake.positions[BTC]["pos"])


async def test_agent_limit_orders_too_far_from_price_are_refused(rt, fake):
    """Agent 把限价单挂在离现价很远的地方会一直成交不了：超过这种风格允许的距离就拒绝，用户自己下的单不受限。"""
    rt.save_config(rt.config().model_copy(update={"style": "scalp"}))  # 超短线：限价离现价最多 0.3%
    far = await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", order_type="limit", price=59_000, stop_loss=58_800, risk_pct=0.3), source="agent")
    assert far.status == "rejected" and "市价" in far.message and fake.orders == []
    near = await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", order_type="limit", price=59_900, stop_loss=59_700, risk_pct=0.3), source="agent")
    assert near.status == "executed", near.message
    mine = await rt.trader.submit(TradeAction(kind="open", inst_id=ETH, side="long", order_type="limit", price=fake.prices[ETH] * 0.95, stop_loss=fake.prices[ETH] * 0.93, risk_pct=0.3), source="user")
    assert mine.status == "executed", mine.message


async def test_resting_orders_report_the_margin_they_tie_up(rt, fake):
    """挂单列表要能看出每张单投入了多少钱：名义价值 = 张数 × 合约面值 × 价格，保证金 = 名义价值 ÷ 杠杆。"""
    res = await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", order_type="limit", price=59_900, stop_loss=59_600, leverage=5, risk_pct=0.3), source="user")
    assert res.status == "executed", res.message
    order = (await rt.trader.account("demo")).orders[0]
    assert order.notional_usd == pytest.approx(res.detail["notional_usd"], rel=0.01)
    assert order.leverage == 5 and order.margin_usd == pytest.approx(order.notional_usd / 5, rel=0.01)


async def test_okx_51004_leverage_tier_cap_automatically_clamps_contracts_and_retries(rt, fake):
    """欧易 51004 档位张数上限报错时，自动缩减到该档位最大可开张数重试成功，避免小品种因大资金算超张数而掉单。"""
    from ouy.okx.gateway import OkxError

    orig = fake._swap_place_order

    def capped(args):
        if float(args["sz"]) > 1.0:
            raise OkxError(
                "Order failed. For buy/sell mode, can't be more than 1(contracts) which is the maximum position amount under current leverage.",
                tool="swap_place_order", code="51004",
            )
        return orig(args)

    fake._swap_place_order = capped
    res = await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", order_type="limit", price=59_900, stop_loss=59_400, leverage=10), source="agent")
    assert res.status == "executed" and res.detail["contracts"] == "1" and "档位上限" in res.message
    assert len(fake.orders) == 1 and fake.orders[0]["sz"] == "1"


async def test_lot_protection_size_is_snapped_to_the_lot_step(rt, fake):
    """分笔张数是浮点相减得来的：挂分笔保护单前对齐到最小下单单位，不让欧易报 51121。"""
    await rt.trader.execute(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_400, leverage=3, risk_pct=0.5, reason="测试"), "demo", source="agent")
    pos = (await rt.trader.account("demo")).position(BTC)
    algo_id = await rt.trader._place_sized(pos, 0.07000000000000004, "59500", None, "demo")
    assert next(a for a in fake.algos if a["algoId"] == algo_id)["sz"] == "0.07"
    with pytest.raises(Rejected):
        await rt.trader._place_sized(pos, 0.001, "59500", None, "demo")
