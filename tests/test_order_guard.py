"""限价单的撤单线和有效期由程序盯：到了就只撤那一张并告诉 Worker；Worker 不能因为重新估了一遍就撤自己刚挂的单。"""
from __future__ import annotations

import time

from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from ouy.models import TradeAction
from ouy.store import now_ms

from .test_agent_and_api import BTC, REPORT, make_rt, scripted_model


def limit(price, stop, **extra):
    return TradeAction(kind="open", inst_id=BTC, side="long" if stop < price else "short", order_type="limit", price=price, stop_loss=stop, risk_pct=0.3, **extra)


async def tick(rt, fake, px):
    fake.set_price(BTC, px)
    cfg = rt.config()
    await rt.watcher._guard_orders(cfg, await rt.trader.account(cfg.env), {BTC: px})


async def test_cancel_line_and_expiry_cancel_only_that_order_and_tell_the_worker(settings, fake):
    rt = make_rt(settings, fake, scripted_model([]))
    wrong = await rt.trader.submit(limit(59_900, 59_600, cancel_px=59_700), source="agent")
    assert wrong.status == "rejected" and "放错了边" in wrong.message and not fake.orders
    a = await rt.trader.submit(limit(59_900, 59_600, cancel_px=60_400, expires_minutes=15), source="agent")
    b = await rt.trader.submit(limit(60_100, 60_400, cancel_px=59_500), source="agent")  # 做空挂单：撤单线在下面
    assert a.ok and b.ok and "自动撤" in a.message and len(fake.orders) == 2
    guards = rt.trader.order_guards("demo", BTC)
    assert [g["cancel_px"] for g in guards] == [60_400, 59_500] and guards[0]["expires_ts"] and not guards[1]["expires_ts"]

    await tick(rt, fake, 60_300)  # 都没到
    assert len(fake.orders) == 2 and not rt.watcher.inbox("demo", BTC)
    await tick(rt, fake, 60_450)  # 买单的撤单线到了：只撤买单
    assert [o["side"] for o in fake.orders] == ["sell"] and len(rt.trader.order_guards("demo", BTC)) == 1
    mail = rt.watcher.inbox("demo", BTC)
    assert "撤单线 60400" in mail[0]["text"] and "不要在原价重挂" in mail[0]["text"]
    assert rt.store.journal("demo")[0]["body"]["source"] == "guard"

    c = await rt.trader.submit(limit(60_300, 60_000, expires_minutes=10), source="agent")
    assert c.ok and len(fake.orders) == 2
    row = rt.trader.order_guards("demo", BTC)[-1]
    rt.store.set(f"order_guard:demo:{BTC}", [rt.trader.order_guards("demo", BTC)[0], {**row, "ts": now_ms() - 11 * 60_000, "expires_ts": now_ms() - 1000}])
    await tick(rt, fake, 60_450)  # 过了有效期
    assert [o["side"] for o in fake.orders] == ["sell"] and "过了有效期" in rt.watcher.inbox("demo", BTC)[-1]["text"]

    # 成交或被别人撤掉的单：不再盯；关掉撤单权限时程序不撤
    fake.orders.clear()
    rt.store.set(f"order_guard:demo:{BTC}", [{**g, "ts": 0} for g in rt.trader.order_guards("demo", BTC)])
    await tick(rt, fake, 59_000)
    assert rt.trader.order_guards("demo", BTC) == []
    d = await rt.trader.submit(limit(58_900, 58_600, cancel_px=59_200), source="agent")
    assert d.ok
    rt.save_config(rt.config().model_copy(update={"permissions": {"cancel_orders": "off"}}))
    await tick(rt, fake, 59_300)
    assert len(fake.orders) == 1


async def test_the_whole_tick_runs_the_guard_even_when_the_worker_is_paused(settings, fake):
    rt = make_rt(settings, fake, scripted_model([]))
    assert (await rt.trader.submit(limit(59_900, 59_600, cancel_px=60_400), source="agent")).ok
    rt.watcher._set_worker(BTC, paused=True)
    fake.set_price(BTC, 60_500)
    await rt.watcher._tick(rt.config())
    assert not fake.orders


async def test_worker_cannot_cancel_its_own_plan_just_by_recomputing(settings, fake):
    seen: list[str] = []
    calls = [
        ("cancel_pending_orders", {"inst_id": BTC, "reason": "重新算了盈亏比只有 1.1"}),
        ("cancel_strategy", {"strategy_id": 1, "reason": "成本偏高"}),
        ("cancel_pending_orders", {"inst_id": BTC, "reason": "撤单", "changed": "15m 收盘跌破 59600，结构坏了"}),
        ("cancel_strategy", {"strategy_id": 1, "reason": "撤", "changed": "BTC 急跌 1%"}),
    ]

    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        seen[:] = [str(r.content) for r in returns]
        if len(returns) < len(calls):
            return ModelResponse(parts=[ToolCallPart(*calls[len(returns)])])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "plan": None})])

    rt = make_rt(settings, fake, FunctionModel(model))
    assert (await rt.trader.submit(limit(59_900, 59_600, cancel_px=60_400), source="agent")).ok
    sid = rt.watcher.strategies.deploy(rt.config(), BTC, "pullback", "long", {"trigger": 59_000, "stop_loss": 58_700})["id"]
    assert sid == 1
    await rt.watcher.review(BTC, "测试")
    assert "没有撤" in seen[0] and "没有撤" in seen[1]
    assert "已撤销" in seen[2] and "已撤销" in seen[3]
    assert not fake.orders and rt.store.get_strategy(1)["status"] == "cancelled" and "BTC 急跌" in rt.store.get_strategy(1)["note"]
    assert rt.trader.order_guards("demo", BTC) == []
