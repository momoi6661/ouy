"""快判：程序每几秒用 1 分钟 K 线判断盘是不是朝一个方向动了，算好入场 / 止损 / 目标；授权过的方向直接下单，没授权的叫醒 Worker 核实。"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from ouy.agent.signals import FastParams, detect, key_lines
from ouy.backtest import simulate, summarize, to_5m
from ouy.indicators import Candle
from ouy.models import WorkerOverride

from .test_agent_and_api import BTC, REPORT, make_rt
from .test_plan import reporting

MIN = 60_000


def quiet(n: int = 60, px: float = 60_000.0, start: int | None = None) -> list[Candle]:
    """一段窄幅来回的 1 分钟 K 线（旧 → 新），最后一根收盘在 start + n 分钟。"""
    start = start if start is not None else (int(time.time() * 1000) // MIN - n - 1) * MIN
    return [Candle(start + i * MIN, px + (-4 if i % 2 else 4), px + 12, px - 12, px + (4 if i % 2 else -4), 10.0, True) for i in range(n)]


def test_fast_judge_reads_breakout_impulse_and_rejection_from_closed_bars_only():
    bars = quiet()
    levels = {"区间上沿": 60_050.0, "区间下沿": 59_800.0, "上一个 1H 高点": 60_400.0}
    assert detect(bars, 60_000, levels, trend=1) is None  # 没动：没有信号
    up = [*bars, Candle(bars[-1].ts + MIN, 60_000, 60_095, 59_995, 60_090, 40.0, True)]  # 放量收在上沿上方
    sig = detect(up, 60_092, levels, atr5=60.0, trend=1)
    assert sig and (sig.side, sig.kind, sig.strength) == ("long", "breakout", 3) and sig.level == 60_050 and "区间上沿" in sig.why
    assert sig.stop < 60_050 and sig.targets[0] == 60_400 and sig.plan(20).entry_type == "now"  # 止损在线的另一边，目标是前面那条线
    assert detect(up, 60_092, levels, atr5=60.0, trend=-1) is None  # 逆大方向又没有别的加分：不够分
    forming = [*bars, Candle(bars[-1].ts + MIN, 60_000, 60_095, 59_995, 60_090, 40.0, False)]
    assert detect(forming, 60_092, levels, trend=1) is None  # 这根还没收盘：不算
    assert detect(up, 60_030, levels, atr5=60.0, trend=1) is None  # 现价已经掉回线下：不追
    wick = [*bars, Candle(bars[-1].ts + MIN, 60_010, 60_110, 60_000, 60_005, 40.0, True)]  # 刺穿上沿又收回
    rej = detect(wick, 60_004, levels, atr5=60.0, trend=-1)
    assert rej and (rej.side, rej.kind) == ("short", "reject") and rej.stop > 60_110
    run = [*bars, *(Candle(bars[-1].ts + (i + 1) * MIN, 60_000 + i * 70, 60_075 + i * 70, 59_995 + i * 70, 60_070 + i * 70, 45.0, True) for i in range(3))]
    imp = detect(run, 60_212, {}, trend=1)
    assert imp and (imp.side, imp.kind) == ("long", "impulse") and imp.stop < 60_212 and "量是平时的" in imp.why


def test_backtest_replays_bars_through_the_same_judge_and_charges_costs():
    bars = quiet(1700, start=1_700_000_000_000 // 300_000 * 300_000)
    assert len(to_5m(bars)) == 340 and simulate(bars, []) == []  # 没有行情就没有信号
    levels, trend, atr5 = key_lines(to_5m(bars), [])
    assert "区间上沿" in levels and atr5 and summarize([]) == {"n": 0}


async def _armed(settings, fake, mode: str, on_signal: str = "verify", calls: list[int] | None = None):
    """一个超短线的 BTC Worker，关键线已经记好，1 分钟 K 线刚放量收在区间上沿上方。"""
    rt = make_rt(settings, fake, reporting({**REPORT, "plan": None, "no_plan_reason": "cost", "on_signal": on_signal}, calls=calls))
    rt.save_config(rt.config().model_copy(update={"style": "scalp", "fast_entry": "verify", "intel_minutes": 0, "fast_kinds": ["retest", "breakout", "reject", "impulse"], "workers": {BTC: WorkerOverride(fast_entry=mode)}}))  # type: ignore[arg-type]
    w, st = rt.watcher, rt.watcher._state(BTC)
    await w.review(BTC, "定时复盘")  # Worker 先交一轮结论（里面有它的 on_signal）
    bars = quiet()
    bars.append(Candle(bars[-1].ts + MIN, 60_000, 60_095, 59_995, 60_090, 40.0, True))

    async def candles(inst, bar, limit, env):
        return bars
    rt.market.candles = candles  # type: ignore[method-assign]
    w.strategies.metrics[BTC] = {"ts": int(time.time() * 1000), "values": {"px": 60_092, "trend": 1, "atr": 60.0}, "levels": {"区间上沿": 60_050.0, "上一个 1H 高点": 60_500.0}, "why": {}}
    fake.prices[BTC] = 60_092
    st.last_px = 60_092
    return rt, w, st


async def test_an_unauthorized_signal_wakes_the_worker_with_the_numbers_already_worked_out(settings, fake):
    rt, w, st = await _armed(settings, fake, "agent", on_signal="verify")
    ask = await w._fast(rt.config(), BTC, st, time.time())
    assert ask and "程序快判出了信号（你没有授权这个方向）" in ask and "收盘突破·做多（3 分）" in ask and "已通过程序核对" in ask and "entry_type=now" in ask
    assert BTC not in fake.positions and st.fast_last["status"] == "verify"
    events = [event for event in rt.store.algo_events("demo", BTC) if event["algo"] == "fast"]
    assert len(events) == 1 and "你没有授权这个方向" in events[0]["effect"]
    values = w.strategies.metrics[BTC]["values"]
    assert values["sig_long"] == 3 and values["sig_short"] == 0 and values["vol_1m"] == 4.0  # 快判的分数也记进数据，触发线的条件能用
    assert await w._fast(rt.config(), BTC, st, time.time() + 10) is None  # 同一根 K 线只处理一次
    assert w.status()["instruments"][BTC]["fast"]["last"]["kind"] == "breakout"


async def test_an_authorized_signal_is_executed_at_once_and_the_worker_is_told_afterwards(settings, fake):
    calls: list[int] = []
    rt, w, st = await _armed(settings, fake, "agent", on_signal="long", calls=calls)
    used = len(calls)
    assert await w._fast(rt.config(), BTC, st, time.time()) is None  # 不用叫醒：已经做了
    assert fake.positions[BTC]["pos"] > 0 and len(calls) == used  # 直接下单，模型一次都没被调用
    assert st.fast_last["status"] == "executed" and "Worker 授权过这个方向" in st.fast_last["result"]
    manage = next(r for r in rt.store.strategies("demo", BTC) if r["kind"] == "manage")
    assert manage["params"]["by"] == "快判" and manage["params"]["breakeven_r"] == 1.0
    assert any(m["who"] == "快判" and "入场单已提交" in m["text"] for m in w.inbox("demo", BTC))
    assert any(a["agent"] == "strategy" and a["kind"] == "action" and "快判" in a["text"] for a in rt.store.activity("demo", inst_id=BTC))


async def test_a_signal_kind_that_loses_in_the_backtest_is_only_recorded(settings, fake, tmp_path, monkeypatch):
    """快判和算法候选位同一条规矩：存盘回测里机械照做扣费后亏的那类信号，授权了也不下单、不叫醒 Worker，只记一条。"""
    from ouy import backtest_plans as bp
    from ouy.backtest import Trade, save

    calls: list[int] = []
    rt, w, st = await _armed(settings, fake, "agent", on_signal="long", calls=calls)
    used = len(calls)
    monkeypatch.setattr(bp, "REPORT_DIR", tmp_path)
    save("scalp", {BTC: [Trade(i * 60_000, "breakout", "long", 3, 100.0, 99.7, -0.4, -0.12, "止损", i * 60_000 + 1) for i in range(120)]}, {"kind": "fast"}, tmp_path)
    assert bp.losing("scalp") == {"快判·收盘突破"}  # 「全部」「做多」这些汇总行不会被当成打法
    assert await w._fast(rt.config(), BTC, st, time.time()) is None
    assert BTC not in fake.positions and len(calls) == used and st.fast_last["status"] == "skipped" and "回测是亏的" in st.fast_last["result"]
    event = w.algo_report("demo", BTC)["events"][0]
    assert event["algo"] == "fast" and "只记录" in event["effect"]
    assert "收盘突破" in next(a["does"] for a in w.status()["instruments"][BTC]["algos"] if a["id"] == "fast")


@pytest.mark.parametrize("status", ["pending", "rejected", "failed", "advised"])
async def test_fast_submission_does_not_retry_via_worker_or_claim_a_fill(settings, fake, monkeypatch, status):
    rt, w, st = await _armed(settings, fake, "agent", on_signal="long")

    async def submit(*args):
        return SimpleNamespace(status=status, message=status, pending_id=123 if status == "pending" else None)

    monkeypatch.setattr(w, "_enter_now", submit)
    assert await w._fast(rt.config(), BTC, st, time.time()) is None
    assert st.fast_last["status"] == status
    assert not w.inbox("demo", BTC) and BTC not in fake.positions
    asks = [a for a in rt.store.activity("demo", inst_id=BTC) if a["kind"] == "ask" and a["data"].get("pending_id") == 123]
    assert len(asks) == int(status == "pending")


async def test_legacy_auto_still_requires_worker_authorization_and_off_disables_signals(settings, fake):
    rt, w, st = await _armed(settings, fake, "auto")
    ask = await w._fast(rt.config(), BTC, st, time.time())
    assert ask and "没有授权这个方向" in ask and not fake.positions and not fake.orders
    rt2, w2, st2 = await _armed(settings, type(fake)(), "off")
    assert await w2._fast(rt2.config(), BTC, st2, time.time()) is None and st2.fast_last is None and not w2.status()["instruments"][BTC]["fast"]["on"]


async def test_worker_switches_quant_mode_and_describe_tape_explains_consecutive_drops_then_bounce(settings, fake):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel
    from ouy.agent.signals import describe_tape

    # 3 连阴下跌后刚收一根阳线：没破前高 = 下跌喘气反弹不追涨；破了前高 = 等回踩接多不硬追
    base = quiet(10, 60_000)
    drops = [Candle(base[-1].ts + (i + 1) * MIN, 60_000 - i * 40, 60_010 - i * 40, 59_950 - i * 40, 59_960 - i * 40, 20.0, True) for i in range(3)]
    bounce = Candle(drops[-1].ts + MIN, 59_880, 59_930, 59_870, 59_920, 25.0, True)
    plain = describe_tape([*base, *drops, bounce], {"区间上沿": 60_100.0, "区间下沿": 59_700.0}, None, "下跌", "trend", "short", 59_920)
    assert "连续 3 根阴线后刚收阳反弹" in plain and "不能当反转追涨" in plain and "只盯做空" in plain
    shifted = describe_tape(
        [*base, *drops, bounce], {"区间上沿": 60_100.0, "区间下沿": 59_700.0},
        {"side": "long", "kind": "反转", "level": 59_900.0, "bars_ago": 1, "origin": 59_800.0, "extreme": 59_930.0, "why": "破前高"},
        "震荡", "trend", "both", 59_920,
    )
    assert "连跌 3 根后转阳" in shifted and "等回踩 59900 附近挂单接多" in shifted

    def run(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("set_quant_mode", {"inst_id": BTC, "mode": "range", "bias": "short", "vol_ratio": 2.0, "reason": "横盘箱体上沿受压"})])
        assert "震荡箱体" in str(returns[-1].content)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "plan": None, "no_plan_reason": "cost"})])

    rt = make_rt(settings, fake, FunctionModel(run))
    await rt.watcher.review(BTC, "定时复盘")
    qt = rt.watcher.status()["instruments"][BTC]["quant"]
    assert qt["mode"] == "range" and qt["bias"] == "short" and qt["vol_ratio"] == 2.0 and qt["by"] == "Worker" and "箱体" in qt["reason"]
