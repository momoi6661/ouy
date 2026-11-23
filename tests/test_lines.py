"""计划线：Worker 每轮必交的一组「价位 → 到了做什么」。空仓时现价上下都要有；进场、撤单、叫醒由程序到了直接执行。"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from ouy.models import CycleReport, PlanLine

from .test_agent_and_api import BTC, REPORT, make_rt
from .test_plan import reporting

FLAT = {**REPORT, "plan": None, "no_plan_reason": "no_edge"}  # 假交易所的现价是 60000
LONG_LOW = {"price": 59_500, "do": "enter", "side": "long", "stop_loss": 59_200, "take_profit": 60_600, "note": "区间下沿接多"}
SHORT_HIGH = {"price": 60_500, "do": "enter", "side": "short", "stop_loss": 60_800, "take_profit": 59_400, "note": "区间上沿接空"}


async def tick(rt, fake, px):
    fake.set_price(BTC, px)
    rt.trader._lot_touch.clear()
    cfg = rt.config()
    rt.watcher._state(BTC).last_px = px
    await rt.watcher.strategies.tick(cfg, await rt.trader.account(cfg.env), {BTC: px})
    for _ in range(3):  # 带动作的线到了之后由盯盘程序另起一步去做
        if jobs := list(rt.watcher._trigger_jobs):
            await asyncio.gather(*jobs)


def lines(rt, live=True):
    return [r for r in rt.store.strategies("demo", BTC, live=live) if r["params"].get("observation")]


def test_an_entry_line_needs_a_side_and_stops_on_the_right_side():
    assert PlanLine(price=60_500, note="前高").do == "wake"
    assert PlanLine(**LONG_LOW).side == "long"
    with pytest.raises(ValidationError, match="side、stop_loss、take_profit"):
        PlanLine(price=59_500, do="enter", side="long", note="缺止损")
    with pytest.raises(ValidationError, match="放错边"):
        PlanLine(**{**LONG_LOW, "stop_loss": 59_800})
    with pytest.raises(ValidationError):
        CycleReport(**{**FLAT, "lines": [{"price": 60_000 + i, "note": "太多"} for i in range(9)]})


async def test_flat_worker_can_omit_or_draw_lines_without_forced_two_sided_retry(settings, fake):
    """空仓不再强制上下各划一条线；Worker 按需画的撤单线和叫醒线照常同步为观察规则。"""
    seen = []
    chosen = {**FLAT, "no_plan_reason": "limit", "lines": [{"price": 60_500, "note": "前高"}, {"price": 59_500, "do": "cancel", "note": "跌破原计划失效"}]}
    rt = make_rt(settings, fake, reporting(chosen, seen=seen))
    rt.watcher._state(BTC).last_px = 60_000
    result = await rt.watcher.review(BTC, "测试")
    assert len(seen) == 1
    assert [l["price"] for l in result["lines"]] == [60_500, 59_500]
    rows = {r["params"]["level"]: r for r in lines(rt)}
    assert set(rows) == {60_500, 59_500} and rows[59_500]["params"]["line"] == {"do": "cancel"} and "line" not in rows[60_500]["params"]
    shown = {r["level"]: r for r in rt.watcher.rules("demo", BTC) if r["type"] == "watch"}
    assert shown[59_500]["action"] == "cancel_orders" and shown[59_500]["mode"] == "auto" and shown[60_500]["mode"] == "wake"

    # 只画一侧或空列表也不会被退回
    one_sided = {**FLAT, "no_plan_reason": "limit", "lines": [{"price": 60_500, "note": "只看上方"}]}
    seen2 = []
    rt2 = make_rt(settings, fake, reporting(one_sided, seen=seen2))
    rt2.watcher._state(BTC).last_px = 60_000
    await rt2.watcher.review(BTC, "测试")
    assert len(seen2) == 1


async def test_lines_gate_only_applies_when_flat_and_downgrades_bad_entries_after_one_retry(rt, fake):
    from types import SimpleNamespace

    w, cfg = rt.watcher, rt.config()
    w._state(BTC).last_px = 60_000
    deps = SimpleNamespace(nudged=False)
    tight = {**LONG_LOW, "price": 59_990, "stop_loss": 59_980, "take_profit": 60_000, "note": "止损太近"}  # 扣费后不够盈亏比
    report = CycleReport(**{**FLAT, "lines": [SHORT_HIGH, tight]})
    problem = await w._lines_gate(cfg, BTC, report, deps)
    assert problem and "59990 这条进场线没通过核对" in problem and "60500" not in problem
    deps.nudged = True
    assert await w._lines_gate(cfg, BTC, report, deps) is None
    assert [l.do for l in report.lines] == ["enter", "wake"] and "没通过核对" in report.lines[1].note

    fake._fill(BTC, 1, 60_000)
    rt.trader._lot_touch.clear()
    assert (await rt.trader.account("demo")).position(BTC)
    one_sided = CycleReport(**{**FLAT, "lines": [{"price": 60_500, "note": "只有上方"}]})
    assert await w._lines_gate(cfg, BTC, one_sided, SimpleNamespace(nudged=False)) is None  # 有仓时不要求上下都有
    holding = CycleReport(**{**REPORT, "lines": []})
    assert await w._lines_gate(cfg, BTC, holding, SimpleNamespace(nudged=False)) is None  # 有仓时止损止盈就是上下两条线


async def test_entry_line_enters_once_and_voids_the_other_side(rt, fake):
    w, st = rt.watcher, rt.watcher._state(BTC)
    st.last_px = 60_000
    st.report = {"ts": 1, "price": 60_000, "lines": [LONG_LOW, SHORT_HIGH, {"price": 61_500, "note": "更高的前高"}]}
    w._sync_observation_rules(rt.config(), BTC)
    assert len(lines(rt)) == 3
    await tick(rt, fake, 59_500)
    assert fake.positions and fake.algos  # 进场了，止损止盈挂在交易所
    left = lines(rt)
    assert [r["params"]["level"] for r in left] == [61_500]  # 另一边的进场线作废，只叫醒的线还在
    short = next(r for r in lines(rt, live=False) if r["params"]["level"] == 60_500)
    assert short["status"] == "cancelled" and "已经进场" in short["note"]
    mail = w.inbox("demo", BTC)
    assert len(mail) == 1 and mail[0]["who"] == "计划线" and not mail[0].get("soft")  # 程序下了单：马上告诉 Worker
    assert any(r["kind"] == "manage" for r in rt.store.strategies("demo", BTC))  # 进场后照样布置保本和分批止盈
    await tick(rt, fake, 59_600)
    await tick(rt, fake, 59_500)
    assert len(w.inbox("demo", BTC)) == 1  # 到过一次就结束

    # 下一轮 Worker 原样再写同一条进场线：有效期内不重设
    st.report = {"ts": 2, "price": 59_500, "lines": [LONG_LOW, {"price": 61_500, "note": "更高的前高"}]}
    w._sync_observation_rules(rt.config(), BTC)
    assert [r["params"]["level"] for r in lines(rt)] == [61_500]


async def test_entry_line_that_no_longer_passes_only_wakes_and_cancel_line_drops_resting_entries(rt, fake):
    w, st = rt.watcher, rt.watcher._state(BTC)
    st.last_px = 60_000
    far = {"price": 59_990, "do": "enter", "side": "long", "stop_loss": 59_980, "take_profit": 60_000, "note": "止损太近"}
    st.report = {"ts": 1, "price": 60_000, "lines": [far, {"price": 60_300, "do": "cancel", "note": "站上这里不追"}]}
    w._sync_observation_rules(rt.config(), BTC)
    await tick(rt, fake, 59_990)
    assert not fake.positions and not fake.orders
    mail = w.inbox("demo", BTC)
    assert len(mail) == 1 and mail[0].get("soft") and "没有进场" in mail[0]["text"]

    await tick(rt, fake, 60_300)
    mail = w.inbox("demo", BTC)
    assert len(mail) == 2 and "没有在等的入场需要撤" in mail[1]["text"] and mail[1].get("soft")
    assert [e["algo"] for e in rt.store.algo_events("demo", BTC)].count("rule") == 2


async def test_changing_a_lines_action_or_stop_redraws_it_but_rewording_does_not(rt, fake):
    w, st = rt.watcher, rt.watcher._state(BTC)
    st.last_px = 60_000
    st.report = {"ts": 1, "price": 60_000, "lines": [LONG_LOW, {"price": 60_500, "note": "前高"}]}
    w._sync_observation_rules(rt.config(), BTC)
    first = {r["params"]["level"]: r["id"] for r in lines(rt)}
    st.report = {"ts": 2, "price": 60_000, "lines": [{**LONG_LOW, "note": "换个说法"}, {"price": 60_500, "do": "cancel", "note": "前高改成撤单线"}]}
    w._sync_observation_rules(rt.config(), BTC)
    second = {r["params"]["level"]: r["id"] for r in lines(rt)}
    assert second[59_500] == first[59_500] and second[60_500] != first[60_500]
    st.report = {"ts": 3, "price": 60_000, "lines": [{**LONG_LOW, "stop_loss": 59_000}, {"price": 60_500, "do": "cancel", "note": "前高改成撤单线"}]}
    w._sync_observation_rules(rt.config(), BTC)
    third = {r["params"]["level"]: r for r in lines(rt)}
    assert third[59_500]["id"] != second[59_500] and third[59_500]["params"]["line"]["stop_loss"] == 59_000 and third[60_500]["id"] == second[60_500]
