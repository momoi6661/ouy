"""触发线：Worker 预先写好「条件 → 操作」，条件满足时程序直接执行，不叫醒模型思考；只执行一次，结果回报给 Worker。"""
from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from ouy.indicators import Candle
from ouy.models import TradeAction
from ouy.strategy import recorder_params
from ouy.trader import Rejected

from .test_agent_and_api import BTC, REPORT, make_rt, scripted_model


async def tick(rt, fake, px):
    fake.set_price(BTC, px)
    rt.trader._lot_touch.clear()
    cfg = rt.config()
    await rt.watcher.strategies.tick(cfg, await rt.trader.account(cfg.env), {BTC: px})


def add(rt, **p):
    side = p.pop("side", "")
    return rt.watcher.strategies.deploy(rt.config(), BTC, "trigger", side, p, note="测试")


async def test_observation_lines_are_kept_across_reviews_and_fire_once(rt, fake):
    watcher = rt.watcher
    st = watcher._state(BTC)

    def lines():
        return {r["strategy_id"]: r for r in watcher.rules("demo", BTC) if r["type"] == "watch" and r["status"] == "waiting"}

    st.report = {"ts": 1, "price": 60_000, "lines": [
        {"price": 60_100, "note": "first"}, {"price": 60_100, "note": "same line again"}, {"price": 59_500, "note": "下沿"},
    ]}
    watcher._sync_observation_rules(rt.config(), BTC)
    first = lines()
    assert sorted(r["level"] for r in first.values()) == [59_500, 60_100]  # 同价位同条件只算一条线
    assert all(r["mode"] == r["action"] == "wake" and not r["repeat"] for r in first.values())

    # 下一轮：一条线措辞变了、价位只挪了一点点（当同一条），一条没再划，另划了一条新的
    st.report = {"ts": 2, "price": 60_000, "lines": [{"price": 60_110, "note": "还是前高"}, {"price": 61_000, "note": "新线"}]}
    watcher._sync_observation_rules(rt.config(), BTC)
    second = lines()
    kept = next(i for i, r in first.items() if r["level"] == 60_100)
    assert kept in second and second[kept]["level"] == 60_100 and second[kept]["note"] == "还是前高"
    assert sorted(r["level"] for r in second.values()) == [60_100, 61_000]
    gone = next(i for i, r in first.items() if r["level"] == 59_500)
    assert rt.store.get_strategy(gone)["status"] == "cancelled"

    await tick(rt, fake, 60_100)
    notes = watcher.inbox("demo", BTC)
    assert len(notes) == 1 and notes[0].get("soft") and "还是前高" in notes[0]["text"]  # 叫醒时带的是最新的说明
    assert rt.store.get_strategy(kept)["status"] == "done"
    await tick(rt, fake, 60_000)
    await tick(rt, fake, 60_100)
    assert len(watcher.inbox("demo", BTC)) == 1  # 到过一次就结束，不反复叫

    # 到过的线在有效期内不重设；价位真的变了才是一条新线
    st.report = {"ts": 3, "price": 60_100, "lines": [{"price": 60_100, "note": "再看一次"}, {"price": 61_000, "note": "新线"}]}
    watcher._sync_observation_rules(rt.config(), BTC)
    assert sorted(r["level"] for r in lines().values()) == [61_000]
    st.report = {"ts": 4, "price": 60_100, "lines": [{"price": 60_400, "note": "换了位置"}]}
    watcher._sync_observation_rules(rt.config(), BTC)
    assert sorted(r["level"] for r in lines().values()) == [60_400]

    wake_trigger = add(rt, when="price_above", level=60_300, do="wake")
    rule = next(r for r in watcher.rules("demo", BTC) if r["strategy_id"] == wake_trigger["id"])
    assert rule["type"] == "trigger" and rule["action"] == "wake"
    with pytest.raises(Rejected, match="计划线"):
        add(rt, when="price_touch", level=60_300, observation=True, do="open_market", side="long", stop_loss=59_900)
    assert not fake.positions and not fake.orders


async def test_cancel_during_condition_evaluation_takes_precedence(rt, fake, monkeypatch):
    engine = rt.watcher.strategies
    rule = add(rt, when="close_above", level=60_100, bar="15m", do="wake")
    checking, resume = asyncio.Event(), asyncio.Event()

    async def condition(*args):
        checking.set()
        await resume.wait()
        return True

    monkeypatch.setattr(engine, "_cond", condition)
    task = asyncio.create_task(tick(rt, fake, 60_200))
    await asyncio.wait_for(checking.wait(), 2)
    engine.cancel(rule["id"])
    resume.set()
    await task
    assert rt.store.get_strategy(rule["id"])["status"] == "cancelled"
    assert not rt.watcher.inbox("demo", BTC)


async def test_worker_close_observation_wakes_once_on_future_confirmed_bar(rt, fake, monkeypatch):
    watcher = rt.watcher
    st = watcher._state(BTC)
    st.report = {"ts": 123, "price": 60_000, "lines": [
        {"price": 59_900, "note": "日内结构转弱", "when": "close_below"},
    ]}
    watcher._sync_observation_rules(rt.config(), BTC)
    rule, = rt.store.strategies("demo", BTC)
    opened = rule["ts"] - 60_000
    clock = rule["ts"] + 850_000
    bars = [Candle(opened, 60_000, 60_020, 59_700, 59_800, 1, False)]

    async def candles(*args):
        return bars

    monkeypatch.setattr(rt.market, "candles", candles)
    monkeypatch.setattr("ouy.strategy.now_ms", lambda: clock)
    await tick(rt, fake, 59_800)
    assert not watcher.inbox("demo", BTC)
    bars[0] = Candle(opened, 60_000, 60_020, 59_700, 59_800, 1, True)
    clock += 16_000
    monkeypatch.setattr("ouy.strategy.time.time", lambda: clock / 1000)
    await tick(rt, fake, 59_800)
    assert "日内结构转弱" in watcher.inbox("demo", BTC)[0]["text"]
    assert rt.store.get_strategy(rule["id"])["status"] == "done"
    assert rt.store.get_strategy(rule["id"])["state"]["runs"] == 1
    st.report["ts"] += 1
    watcher._sync_observation_rules(rt.config(), BTC)
    assert not rt.store.strategies("demo", BTC)
    assert not fake.called("swap_place_order")


async def test_strategy_approval_requires_current_environment_and_unexpired_rule(rt, monkeypatch):
    import httpx

    from ouy.app import create_app

    engine = rt.watcher.strategies
    demo = engine.deploy(rt.config(), BTC, "trigger", "long", {
        "when": "price_above", "level": 60_200, "do": "open_market", "stop_loss": 59_900,
    }, require_approval=True)
    live = rt.store.add_strategy("live", BTC, demo["kind"], demo["side"], demo["params"], "proposed", "")
    app = create_app(rt)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        for operation in ("approve", "cancel", "edit"):
            response = await client.post(f"/api/strategies/{live['id']}/{operation}", json={"level": 60_250})
            assert 400 <= response.status_code < 500
        assert rt.store.get_strategy(live["id"]) == live
        monkeypatch.setattr("ouy.strategy.now_ms", lambda: demo["ts"] + 600_001)
        response = await client.post(f"/api/strategies/{demo['id']}/approve")
        assert 400 <= response.status_code < 500
        assert rt.store.get_strategy(demo["id"])["status"] == "expired"
        assert rt.watcher._with_pending_status([{"strategy_id": demo["id"]}])[0]["pending_status"] == "expired"


def test_edit_that_needs_approval_leaves_the_old_rule_armed(rt):
    engine = rt.watcher.strategies
    old = add(rt, when="price_above", level=60_200, do="open_market", side="long", stop_loss=59_900)
    proposed = engine.edit(old["id"], {"level": 60_500}, require_approval=True)
    assert proposed["id"] != old["id"] and proposed["status"] == "proposed"
    assert rt.store.get_strategy(old["id"])["params"]["level"] == 60_200
    engine.cancel(proposed["id"])
    assert rt.store.get_strategy(old["id"])["status"] == "armed"
    proposed = engine.edit(old["id"], {"level": 60_500}, require_approval=True)
    assert engine.approve(proposed["id"], env="demo")["status"] == "armed"
    assert rt.store.get_strategy(old["id"])["status"] == "cancelled"


async def test_cancel_close_rule_while_preparing_position_keeps_position(rt, fake, monkeypatch):
    rule = add(rt, when="price_above", level=60_200, do="close")
    result = await rt.trader.execute(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_800, risk_pct=0.1), "demo", source="agent")
    assert result.ok
    await tick(rt, fake, 60_000)
    checking, resume = asyncio.Event(), asyncio.Event()
    original = rt.trader._context

    async def context(*args):
        checking.set()
        await resume.wait()
        return await original(*args)

    monkeypatch.setattr(rt.trader, "_context", context)
    task = asyncio.create_task(tick(rt, fake, 60_250))
    await asyncio.wait_for(checking.wait(), 2)
    rt.watcher.strategies.cancel(rule["id"])
    resume.set()
    await task
    assert fake.positions[BTC]["pos"] > 0
    assert rt.store.get_strategy(rule["id"])["status"] == "cancelled"


async def test_open_rule_does_not_become_add_after_a_position_appears(rt, fake):
    rule = add(rt, when="price_above", level=60_200, do="open_market", side="long", stop_loss=60_030)
    await tick(rt, fake, 60_000)
    result = await rt.trader.execute(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_800, risk_pct=0.1), "demo", source="user")
    assert result.status == "executed"
    initial = fake.positions[BTC]["pos"]
    await tick(rt, fake, 60_200)
    assert fake.positions[BTC]["pos"] == initial
    assert rt.store.get_strategy(rule["id"])["status"] == "cancelled"


async def test_worker_writes_a_trigger_and_code_places_the_limit_order_without_the_model(settings, fake):
    calls = {"n": 0}

    def model(messages, info):
        calls["n"] += 1
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("add_trigger", {
                "inst_id": BTC, "when": "price_below", "level": 59_950, "do": "open_limit", "side": "long", "price": 59_900, "stop_loss": 59_600,
                "take_profit": 60_500, "cancel_px": 60_400, "order_expires_minutes": 15, "risk_pct": 0.3, "reason": "回踩 59900 挂买单"})])
        assert "已启用" in str(returns[-1].content) and "当 价格 ≤ 59950 → 挂限价买单 @ 59900" in str(returns[-1].content)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "plan": None})])

    rt = make_rt(settings, fake, FunctionModel(model))
    await rt.watcher.review(BTC, "测试")
    thought = calls["n"]
    row = next(r for r in rt.watcher.strategy_rows("demo", BTC) if r["params"].get("do") == "open_limit")
    assert row["label"] == "触发线" and row["status"] == "armed"
    await tick(rt, fake, 60_000)
    await tick(rt, fake, 59_980)
    assert not fake.orders  # 条件没到
    await tick(rt, fake, 59_940)  # 条件到了：程序直接挂单
    assert [(o["side"], o["px"], o["ordType"]) for o in fake.orders] == [("buy", "59900", "limit")]
    assert calls["n"] == thought  # 没有叫模型思考
    assert rt.trader.order_guards("demo", BTC)[0]["cancel_px"] == 60_400 and rt.store.get_strategy(row["id"])["status"] == "done"
    card = rt.watcher._with_pending_status([{"strategy_id": row["id"]}])[0]
    assert card["pending_status"] == "done"
    assert card["pending_result"]["detail"]["ordId"] == fake.orders[0]["ordId"]
    assert "订单已提交，成交以交易所为准" in rt.watcher.inbox("demo", BTC)[0]["text"] and "不是新的交易指令" in rt.watcher.inbox("demo", BTC)[0]["text"]
    await tick(rt, fake, 59_930)
    assert len(fake.orders) == 1  # 只执行一次


async def test_trigger_conditions_actions_and_safety(settings, fake, monkeypatch):
    rt = make_rt(settings, fake, scripted_model([]))
    with pytest.raises(Rejected, match="止损放错边"):
        add(rt, when="price_above", level=60_500, do="open_market", side="long", stop_loss=61_000)
    with pytest.raises(Rejected, match="周期"):
        add(rt, when="close_above", level=60_500, do="wake")

    # 布置时条件已经满足：不立刻执行，等价格先离开再回来
    a = add(rt, when="price_below", level=60_100, do="open_market", side="long", stop_loss=59_500, risk_pct=0.3)["id"]
    await tick(rt, fake, 60_000)
    assert BTC not in fake.positions and "等它先不满足" in rt.store.get_strategy(a)["note"]
    await tick(rt, fake, 60_200)
    await tick(rt, fake, 60_050)
    assert fake.positions[BTC]["pos"] > 0 and rt.store.get_strategy(a)["status"] == "done"

    # 按 K 线收盘：只认布置之后才收盘的那根
    b = add(rt, when="close_below", level=59_800, bar="5m", do="close")["id"]
    w = add(rt, when="price_above", level=60_600, do="wake", wake_note="看要不要加仓")["id"]
    assert all(rt.store.get_strategy(i)["status"] == "armed" for i in (b, w))  # 几条触发线并存，互不替换
    born = rt.store.get_strategy(b)["ts"]
    bars = [Candle(born - 600_000, 60_000, 60_000, 59_700, 59_750, 1, True)]

    async def candles(inst, bar, limit, env):
        return bars
    monkeypatch.setattr(rt.market, "candles", candles)
    await tick(rt, fake, 60_060)
    assert BTC in fake.positions  # 旧的那根收在下方，不算
    bars.append(Candle(born - 60_000, 59_900, 59_900, 59_700, 59_760, 1, True))
    rt.store.update_strategy(b, state={"ready": True})  # 清掉 15 秒的 K 线缓存
    await tick(rt, fake, 60_070)
    assert BTC in fake.positions  # 即使上游标了 confirmed，未来的收盘也不能触发
    rt.store.update_strategy(b, state={"ready": True})
    with monkeypatch.context() as clock:
        clock.setattr("ouy.strategy.now_ms", lambda: born + 240_001)
        await tick(rt, fake, 60_070)
    assert BTC not in fake.positions and "已执行" in rt.store.get_strategy(b)["note"]
    assert "收盘 59760" in rt.store.get_strategy(b)["note"] and "盘中报价 60070" in rt.store.get_strategy(b)["note"]

    await tick(rt, fake, 60_650)  # 只叫醒的触发线
    assert rt.store.get_strategy(w)["status"] == "done" and "看要不要加仓" in rt.watcher.inbox("demo", BTC)[-1]["text"]

    # 执行不成不重试；关掉权限的不执行；要确认的权限下先等用户同意
    c = add(rt, when="price_below", level=60_300, do="reduce", fraction=0.5)["id"]
    await tick(rt, fake, 60_650)
    await tick(rt, fake, 60_200)
    assert rt.store.get_strategy(c)["status"] == "failed" and "不重试" in rt.store.get_strategy(c)["note"]
    d = add(rt, when="price_above", level=60_400, do="cancel_orders")["id"]
    rt.save_config(rt.config().model_copy(update={"permissions": {"cancel_orders": "off"}}))
    assert (await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", order_type="limit", price=60_100, stop_loss=59_800, risk_pct=0.3), source="agent")).ok
    await tick(rt, fake, 60_350)
    await tick(rt, fake, 60_450)
    assert len(fake.orders) == 1 and rt.store.get_strategy(d)["status"] == "cancelled"
    rt.save_config(rt.config().model_copy(update={"mode": "confirm", "permissions": {}}))
    e = add(rt, when="price_below", level=60_000, do="open_market", side="long", stop_loss=59_500)
    assert e["status"] == "proposed"
    await tick(rt, fake, 60_100)
    await tick(rt, fake, 59_990)
    assert BTC not in fake.positions


async def test_trigger_can_wait_on_anything_the_program_can_see_and_on_two_things_at_once(settings, fake):
    rt = make_rt(settings, fake, scripted_model([]))
    with pytest.raises(Rejected, match="大于 0"):
        add(rt, when="profit_above", do="close")
    # 挂单成交（有了持仓）→ 把止盈改掉；浮盈回吐 → 平仓；两条并存
    filled = add(rt, when="has_position", do="set_stops", take_profit=61_500)["id"]
    back = add(rt, when="giveback_pct", level=50, do="close")["id"]
    late = add(rt, when="has_position", and_when="after_minutes", and_level=30, do="close")["id"]
    flat = add(rt, when="no_position", do="wake", wake_note="仓位没了，重新找机会")["id"]
    await tick(rt, fake, 60_000)
    assert all(rt.store.get_strategy(i)["status"] == "armed" for i in (filled, back, late, flat))
    assert (await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_400, take_profit=62_000, risk_pct=0.3), source="agent")).ok
    await tick(rt, fake, 60_000)
    assert rt.store.get_strategy(filled)["status"] == "done" and "已执行" in rt.store.get_strategy(filled)["note"]
    assert rt.store.get_strategy(late)["status"] == "armed"  # 有持仓了，但还没过 30 分钟
    await tick(rt, fake, 60_600)  # 浮盈走高
    await tick(rt, fake, 60_400)  # 回吐 1/3：不到 50%
    assert BTC in fake.positions
    await tick(rt, fake, 60_250)  # 回吐超过一半：程序直接平
    assert BTC not in fake.positions and rt.store.get_strategy(back)["status"] == "done"
    await tick(rt, fake, 60_250)  # 仓位没了：叫醒 Worker
    assert rt.store.get_strategy(flat)["status"] == "done" and "重新找机会" in rt.watcher.inbox("demo", BTC)[-1]["text"]

    # 触发线页的清单：每条都是「等什么 → 做什么」，带谁写的、是自动执行还是只叫醒
    rules = {r["id"]: r for r in rt.watcher.rules("demo", BTC)}
    extra = ("recorder", "conds", "repeat", "cooldown_minutes", "off", "runs", "last_run")
    assert {k: v for k, v in rules[f"s{late}"].items() if k not in extra} | {"ts": 0, "updated_ts": 0, "note": ""} == {
        "id": f"s{late}", "type": "trigger", "label": "触发线", "when": "这个币有了持仓（挂单成交） 且 过了 30 分钟", "do": "市价平掉这个币的仓位", "mode": "auto",
        "status": "waiting", "by": "Worker", "ts": 0, "updated_ts": 0, "note": "", "level": None, "strategy_id": late, "expires_ts": None, "action": "close", "cond": "has_position", "bar": None}
    assert rules[f"s{flat}"]["mode"] == "wake" and rules[f"s{flat}"]["status"] == "done"


async def test_time_triggers_and_task_actions(settings, fake, monkeypatch):
    rt = make_rt(settings, fake, scripted_model([]))
    sent = []

    async def chat(message, **kw):
        sent.append((message, kw))
    monkeypatch.setattr(rt.watcher, "chat", chat)
    with pytest.raises(Rejected, match="反复"):
        add(rt, when="every_minutes", level=10, do="close")
    with pytest.raises(Rejected, match="task"):
        add(rt, when="after_minutes", level=10, do="task")
    from ouy.store import now_ms
    clock = add(rt, when="at_time", level=now_ms() + 3_600_000, do="router_task", task="让通用 Worker 查今晚的数据公布")["id"]
    loop = add(rt, when="every_minutes", level=15, do="task", task="重新评估 4H 结构")["id"]
    await tick(rt, fake, 60_000)
    assert not sent and not rt.watcher.inbox("demo", BTC)
    row = rt.store.get_strategy(clock)
    rt.store._exec("UPDATE strategies SET params=? WHERE id=?", (__import__("json").dumps({**row["params"], "level": now_ms() - 1}, ensure_ascii=False), clock))
    rt.store._exec("UPDATE strategies SET ts=? WHERE id=?", (now_ms() - 16 * 60_000, loop))
    rt.store.update_strategy(loop, state={"ready": True})
    await tick(rt, fake, 60_000)
    import asyncio
    await asyncio.sleep(0)
    assert rt.store.get_strategy(clock)["status"] == "done" and "查今晚的数据公布" in sent[0][0] and sent[0][1]["origin"] == f"触发线 #{clock}"
    mail = [m for m in rt.watcher.inbox("demo", BTC) if m["who"] == "触发线"]
    assert len(mail) == 1 and "重新评估 4H 结构" in mail[0]["text"]
    assert rt.store.get_strategy(loop)["status"] == "armed" and "已执行 1 次" in rt.store.get_strategy(loop)["note"]  # 反复的不结束
    await tick(rt, fake, 60_000)
    assert len([m for m in rt.watcher.inbox("demo", BTC) if m["who"] == "触发线"]) == 1  # 还没到下一个 15 分钟
    rules = {r["strategy_id"]: r for r in rt.watcher.rules("demo", BTC) if r["strategy_id"]}
    assert rules[loop]["when"] == "每隔 15 分钟" and rules[loop]["do"] == "Worker 自己做一项任务：重新评估 4H 结构" and rules[loop]["mode"] == "task"


async def test_manual_order_hands_the_position_to_the_worker_right_away(settings, fake):
    import httpx

    from ouy.app import create_app

    rt = make_rt(settings, fake, scripted_model([]))
    app = create_app(rt)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        order = {"kind": "open", "inst_id": BTC, "side": "long", "order_type": "market", "stop_loss": 59_400, "margin_usdt": 20, "reason": "手动下单"}
        r = (await client.post("/api/trade", json=order)).json()
        assert r["status"] == "executed" and "盯盘没开" in r["worker_note"] and not rt.watcher.inbox("demo", BTC)
        rt.watcher.running = True
        fake.set_price(BTC, 60_300)  # 浮盈中才允许加仓
        r = (await client.post("/api/trade", json={**order, "kind": "add", "stop_loss": 59_700})).json()
        assert "已通知 BTC Worker" in r.get("worker_note", ""), r
        mail = rt.watcher.inbox("demo", BTC)
        assert mail[-1]["who"] == "用户下单" and "核对止损" in mail[-1]["text"]
        assert "worker_note" not in (await client.post("/api/trade", json={"kind": "close", "inst_id": BTC, "reason": "手动平仓"})).json()
        rt.watcher.running = False


async def test_clear_history_only_removes_finished_records(settings, fake):
    import httpx

    from ouy.app import create_app

    rt = make_rt(settings, fake, scripted_model([]))
    s = rt.store
    done, running = s.add_work("demo", BTC, "event", "复盘", "done"), s.add_work("demo", BTC, "user", "正在做", "running")
    other = s.add_work("demo", "ETH-USDT-SWAP", "event", "别的币", "done")
    s.add_activity("demo", BTC, "worker", "result", "做完了", done)
    s.add_activity("demo", BTC, "worker", "tool", "还在做", running)
    s.add_activity("demo", "ETH-USDT-SWAP", "worker", "result", "别的币", other)
    live = add(rt, when="price_above", level=70_000, do="wake")["id"]
    ended = add(rt, when="price_below", level=50_000, do="wake")["id"]
    s.update_strategy(ended, status="cancelled")
    scored = s.add_strategy("demo", BTC, "breakout", "long", {"trigger": 1, "stop_loss": 0.5, "playbook": "突破"}, "done")["id"]
    s.update_strategy(scored, state={"since": 1})  # 策略库的单，战绩还没记：不能清
    app = create_app(rt)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        assert (await c.post("/api/history/clear", json={"inst_id": BTC, "what": "nope"})).status_code == 422
        assert (await c.post("/api/history/clear", json={"inst_id": BTC, "what": "tasks"})).json() == {"cleared": 1}
        assert {w["id"] for w in s.work_items("demo")} == {running, other}
        assert (await c.post("/api/history/clear", json={"inst_id": BTC, "what": "triggers"})).json() == {"cleared": 1}
        assert s.get_strategy(ended) is None and s.get_strategy(live)["status"] == "armed" and s.get_strategy(scored)
        assert (await c.post("/api/history/clear", json={"inst_id": BTC, "what": "talk"})).json()["cleared"] >= 1
        left = [(a["inst_id"], a["text"]) for a in s.activity("demo")]
        assert (BTC, "还在做") in left and (BTC, "做完了") not in left and ("ETH-USDT-SWAP", "别的币") in left


def _flat_bars(n=41, px=60_000.0, vol=10.0):
    import time as _t

    base = int(_t.time() * 1000) // 300_000 * 300_000
    return base, [Candle(base - (n - i) * 300_000, px, px + 40, px - 40, px + (i % 3 - 1) * 10, vol, True) for i in range(n)]


def test_scores_are_computed_by_code_from_closed_bars():
    from ouy.agent.setup import judge

    base, bars = _flat_bars()
    # 上涨 + 探到支撑后收回 + 放量 = 4 分做多；同一根 K 线逆着大方向只有 2 分
    closed = bars[:-1] + [Candle(base, 59_990, 60_020, 59_890, 60_010, 30, True)]
    up = judge(closed, {"支撑 1": 59_900.0}, "上涨（高点低点都在抬高）")
    assert up["side"] == "long" and up["score"] == 4 and up["level"] == 59_900 and all(n["ok"] for n in up["nodes"])
    down = judge(closed, {"支撑 1": 59_900.0}, "下跌（高点低点都在降低）")
    assert down["side"] == "" and down["scores"]["long"] == 2
    quiet = judge(bars, {"支撑 1": 59_000.0}, "上涨（高点低点都在抬高）")
    assert quiet["side"] == "" and not quiet["nodes"][1]["ok"] and "差" in quiet["nodes"][1]["value"]


async def test_recorder_data_is_kept_when_needed_and_old_default_recorders_are_retired(settings, fake, monkeypatch):
    rt = make_rt(settings, fake, scripted_model([]))
    w, st, eng = rt.watcher, rt.watcher._state(BTC), rt.watcher.strategies
    base, bars = _flat_bars()

    async def candles(inst, bar, limit, env):
        return bars
    monkeypatch.setattr(rt.market, "candles", candles)
    monkeypatch.setattr("ouy.indicators.read_chart", lambda c, n=30: {"range": {"upper": 60_400, "lower": 59_900}, "support": [], "resistance": []})
    st.last_px = 60_000
    await w._record(rt.config(), BTC, st, 100.0)
    assert eng.metrics[BTC]["values"]["vol_ratio"] == 1.0 and st.data_log[-1]["reading"]
    assert not w._shift_actions_on(rt.config(), BTC) and not rt.store.strategies("demo", BTC)
    # 旧版给超短线自动带的「凑够分数就叫醒」的默认记录器：换成快判之后自动撤掉；用户自己设的留着
    rt.save_config(rt.config().model_copy(update={"style": "scalp", "fast_entry": "verify"}))
    cfg = rt.config()
    for data in ("score_long", "score_short"):
        eng.deploy(cfg, BTC, "trigger", "", {**recorder_params([{"data": data, "op": "above", "level": 3}]), "do": "wake", "wake_note": "凑够了"}, by="默认")
    eng.deploy(cfg, BTC, "trigger", "", {**recorder_params([{"data": "vol_ratio", "op": "above", "level": 2.5}]), "do": "wake", "wake_note": "明显放量", "cooldown_minutes": 15}, source="user")
    rt.store.set(f"recorders:demo:{BTC}", True)
    await w._record(cfg, BTC, st, 200.0)
    rows = rt.store.strategies("demo", BTC)
    assert [r["params"]["data"] for r in rows] == ["vol_ratio"] and rows[0]["params"]["by"] == "你"  # 开着快判的币自动记数据，不再带默认记录器
    status = w.status()["instruments"][BTC]
    assert status["data"]["values"]["vol_ratio"] == 1.0 and status["data"]["levels"]["区间下沿"] == 59_900 and status["strategies"][0]["label"] == "触发线"
    assert status["data"]["values"]["atr"] > 0 and status["fast"] == {"mode": "verify", "on": True, "on_signal": "verify", "last": None, "shift": None}
    rule = next(r for r in status["rules"] if r["recorder"] and r["conds"][0]["data"] == "vol_ratio")
    assert rule["conds"] == [{"data": "vol_ratio", "op": "above", "level": 2.5}] and rule["repeat"] and rule["mode"] == "wake"

    await tick(rt, fake, 60_000)  # 量比 1：没到
    bars.append(Candle(base, 59_990, 60_020, 59_890, 60_010, 30, True))  # 新收一根放量的 K 线
    st.last_px = 60_010
    await w._record(cfg, BTC, st, 215.0)
    assert eng.metrics[BTC]["values"]["vol_ratio"] == 3.0 and len(st.data_log) == 2
    await tick(rt, fake, 60_010)
    mail = next(m["text"] for m in rt.watcher.inbox("demo", BTC) if "量比 ≥ 2.5" in m["text"])
    assert mail.startswith("触发线 #") and "的算法读数到了" in mail and "量比 ≥ 2.5" in mail and "程序记录的数据：量比 3" in mail and "现在的线" in mail and "你现在没有等着的入场触发线" in mail


async def test_worker_sets_a_recorder_that_opens_first_then_wakes_it(settings, fake):
    from ouy.store import now_ms

    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("add_trigger", {  # 条件是算法读数的触发线（以前要用单独的 set_recorder）
                "inst_id": BTC, "reason": "分数够、贴近下沿就先挂单", "cooldown_minutes": 1, "do": "open_limit", "side": "long", "risk_pct": 0.3,
                "when": "data_above", "data": "score_long", "level": 3, "and_when": "data_below", "and_data": "dist:区间下沿", "and_level": 0.3,
                "price": 59_830, "stop_loss": 59_600})])
        assert "已启用" in str(returns[-1].content) and "当 做多分数 ≥ 3 且 现价高出「区间下沿」的幅度% ≤ 0.3" in str(returns[-1].content)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "plan": None})])

    rt = make_rt(settings, fake, FunctionModel(model))
    eng = rt.watcher.strategies
    await rt.watcher.review(BTC, "测试")
    rec = next(r for r in rt.store.strategies("demo", BTC) if r["params"].get("recorder"))
    assert rec["params"]["recorder"] and rec["params"]["repeat"] and len(rec["params"]["conds"]) == 2 and rec["side"] == "long"
    assert rec["params"]["wake_note"] == "分数够、贴近下沿就先挂单" and "and_when" not in rec["params"] and "挂限价买单 @ 59830，止损 59600" in eng.describe(rec)
    # 库里旧的记录器有「价位跟着线走」的写法（工具里已经不给了）：照常执行
    moving = {k: v for k, v in rec["params"].items() if k not in ("price", "stop_loss")} | {"price_line": "区间下沿", "price_offset_pct": 0.05, "stop_line": "区间下沿", "stop_offset_pct": -0.3}
    rt.store._exec("UPDATE strategies SET params=? WHERE id=?", (__import__("json").dumps(moving, ensure_ascii=False), rec["id"]))
    rec = rt.store.get_strategy(rec["id"])
    assert "挂限价买单 @ 「区间下沿」+0.05%，止损 「区间下沿」-0.3%" in eng.describe(rec)

    def record(levels=None, **values):
        eng.metrics[BTC] = {"ts": now_ms(), "source_env": "demo", "values": {"px": 60_000, "vol_ratio": 1.0, "score_long": 0, **values}, "levels": levels or {"区间下沿": 59_900.0, "区间上沿": 60_400.0}}

    eng.metrics.pop(BTC, None)  # 复盘已经采样过；模拟订阅暂时没有可用读数
    await tick(rt, fake, 60_000)  # 还没有数据：不执行
    record(score_long=3)
    await tick(rt, fake, 59_950)  # 第一次有效采样就达标：之前缺数据不算观察到了低分
    assert not fake.orders and not rt.store.get_strategy(rec["id"])["state"].get("ready")
    record(score_long=3)
    await tick(rt, fake, 60_300)  # 分数够了，但离下沿还远：两项要同时满足
    assert not fake.orders
    record({"区间下沿": 59_800.0, "区间上沿": 60_400.0}, score_long=3)  # 线已经挪了：按新位置挂
    await tick(rt, fake, 59_950)
    assert [(o["side"], o["px"], o["ordType"]) for o in fake.orders] == [("buy", "59829.9", "limit")]  # 程序先挂单
    row = rt.store.get_strategy(rec["id"])
    assert row["status"] == "armed" and row["state"]["runs"] == 1
    mail = rt.watcher.inbox("demo", BTC)[-1]["text"]
    assert mail.startswith(f"触发线 #{rec['id']} 的算法读数到了") and "订单已提交" in mail and "不是新的交易指令" in mail
    assert "做多分数实测 3" in mail and "盘中报价 59950" in mail
    rt.store.update_strategy(rec["id"], state={**row["state"], "cool_until": 0})
    record(score_long=1)
    await tick(rt, fake, 59_950)
    record({"区间下沿": 59_800.0, "区间上沿": 60_400.0}, score_long=4)
    await tick(rt, fake, 59_950)
    assert len(fake.orders) == 1 and "已经有持仓或挂单" in rt.store.get_strategy(rec["id"])["note"]  # 已有挂单：这次跳过
    rule = next(r for r in rt.watcher.rules("demo", BTC) if r["strategy_id"] == rec["id"])
    assert rule["recorder"] and rule["runs"] == 1 and rule["mode"] == "auto" and rule["label"] == "触发线"
    eng.metrics[BTC]["ts"] -= 600_000  # 数据太久没更新：不拿来判断
    assert eng.data(BTC, "score_long") is None and eng.line(BTC, "区间下沿") is None
    assert not eng.fresh_metrics("demo", BTC) and "现在的线" not in eng.snapshot(BTC, "demo")
    record(sig_long=4, score_long=2)
    eng.metrics[BTC]["fast_ts"] = now_ms() - 120_000
    assert eng.data(BTC, "score_long") == 2 and eng.data(BTC, "sig_long") is None
    assert "快判做多分数" not in eng.snapshot(BTC, "demo")
    assert eng.fresh_metrics("demo", BTC) and not eng.fresh_metrics("live", BTC)

    # Worker 把它改回只叫醒、换一个条件；改价的 edit_trigger 也照常能用
    eng.edit(rec["id"], {"conds": [{"data": "vol_ratio", "op": "above", "level": 2}], "do": "wake"}, "改", side="", replace=True)
    row = rt.store.get_strategy(rec["id"])
    assert row["params"]["do"] == "wake" and "price_line" not in row["params"] and row["side"] == "" and row["state"]["runs"] == 1
    old = add(rt, when="price_above", level=60_900, do="open_market", side="short", stop_loss=61_100, take_profit=60_100)["id"]
    assert eng.edit(old, {"level": 60_300, "stop_loss": 60_500, "take_profit": 59_950})["params"]["level"] == 60_300
    with pytest.raises(Rejected, match="止损放错边"):
        eng.edit(old, {"stop_loss": 59_000})


async def test_user_adds_edits_and_switches_off_a_recorder(settings, fake):
    import httpx

    from ouy.app import create_app

    rt = make_rt(settings, fake, scripted_model([]))
    app = create_app(rt)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        conds = [{"data": "dist:区间下沿", "op": "below", "level": 0.1}, {"data": "book_bias", "op": "above", "level": 20}]
        r = await client.post("/api/recorders", json={"inst_id": BTC, "conds": conds, "note": "贴近下沿且买盘多"})
        row = r.json()
        assert r.status_code == 200 and row["status"] == "armed" and row["params"]["by"] == "你" and row["params"]["repeat"] and row["params"]["recorder"]
        assert "现价高出「区间下沿」的幅度% ≤ 0.1 且 盘口买卖偏向 ≥ 20" in rt.watcher.strategies.describe(row)
        conds[0]["level"] = 0.2
        r = (await client.post(f"/api/strategies/{row['id']}/edit", json={"conds": conds, "off": True, "cooldown_minutes": 5})).json()
        assert r["params"]["conds"][0]["level"] == 0.2 and r["params"]["level"] == 0.2 and r["params"]["off"] and r["params"]["cooldown_minutes"] == 5
        assert (await client.post("/api/recorders", json={"inst_id": BTC, "conds": [{"data": "没有这项", "op": "above", "level": 1}]})).status_code == 400
        rule = next(x for x in (await client.get("/api/agent/status")).json()["instruments"][BTC]["rules"] if x["strategy_id"] == row["id"])
        assert rule["recorder"] and rule["off"] and rule["conds"][0]["level"] == 0.2 and rule["by"] == "你"


async def test_user_can_draw_a_line_that_wakes_cancels_closes_or_opens(rt, fake):
    """用户自己划的触发线：一句「到了什么 → 做什么」，不带观察线标记（Worker 重新划线时不会把它撤掉），只执行一次。"""
    import httpx

    from ouy.app import create_app

    watcher, app = rt.watcher, create_app(rt)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        async def line(**body):
            return await client.post("/api/lines", json={"inst_id": BTC, **body})

        wake = (await line(when="price_touch", level=60_200, do="wake", note="前高")).json()
        assert wake["status"] == "armed" and wake["params"]["by"] == "你" and not wake["params"].get("observation")
        opened = (await line(when="price_touch", level=60_300, do="open", side="long", stop_loss=59_900, take_profit=61_000)).json()
        assert opened["params"]["do"] == "open_market" and opened["side"] == "long"
        assert (await line(when="close_below", level=59_000, bar="15m", do="cancel_orders")).json()["params"]["bar"] == "15m"
        bad = await line(when="price_touch", level=60_300, do="open", side="long", stop_loss=60_900)
        assert bad.status_code == 400 and "止损放错边" in bad.json()["detail"]
        assert (await line(when="price_touch", level=60_300, do="open", stop_loss=59_900)).status_code == 400  # 开仓要给方向
        assert (await line(when="price_touch", level=60_300, do="close", stop_loss=59_900)).status_code == 400
        # 条件也可以是一项算法读数（以前的记录器）：反复生效，只能叫醒、撤单、平仓
        turn = (await line(when="data_above", data="turn_stage", level=3, do="wake", note="转折确认")).json()
        assert turn["params"]["recorder"] and turn["params"]["repeat"] and turn["params"]["conds"] == [{"data": "turn_stage", "op": "above", "level": 3.0}]
        assert "转折" in watcher.strategies.describe(turn)
        assert (await line(when="data_below", data="dist:区间下沿", level=-0.2, do="cancel_orders")).status_code == 200  # 读数可以是负数
        assert (await line(when="data_above", level=3, do="wake")).status_code == 400  # 没选读数
        assert (await line(when="data_above", data="score_long", level=3, do="open", side="long", stop_loss=59_900)).status_code == 400  # 读数条件不让直接开仓
        assert (await line(when="data_above", data="没有这项", level=3, do="wake")).status_code == 400
        assert (await line(when="price_touch", level=0, do="wake")).status_code == 400
        r = (await client.post(f"/api/strategies/{turn['id']}/edit", json={"level": 2})).json()  # 只看一项读数的：改数值
        assert r["params"]["conds"][0]["level"] == 2 and r["params"]["level"] == 2

        watcher._state(BTC).report = {"ts": 9, "price": 60_000, "lines": []}
        watcher._sync_observation_rules(rt.config(), BTC)  # Worker 这一轮一条线都没划：用户的线不受影响
        assert rt.store.get_strategy(wake["id"])["status"] == "armed"
        await tick(rt, fake, 60_000)
        await tick(rt, fake, 60_200)
        assert rt.store.get_strategy(wake["id"])["status"] == "done" and not fake.positions
        assert any(n.get("soft") and "前高" in n["text"] for n in watcher.inbox("demo", BTC))
        await tick(rt, fake, 60_300)
        assert rt.store.get_strategy(opened["id"])["status"] == "done" and fake.positions


def test_a_wake_line_always_fires_on_touch_even_if_the_worker_asked_for_a_close():
    """叫醒线碰到就叫是设计：Worker 写成「等 5 分钟收盘站上」也按碰到算；进场线、撤单线的收盘确认照旧。"""
    from ouy.models import PlanLine

    wake = PlanLine(price=71.54, when="close_above", bar="5m", do="wake", note="站上前高")
    assert (wake.when, wake.bar) == ("price_touch", None)
    cancel = PlanLine(price=71.1, when="close_below", bar="5m", do="cancel", note="跌破前低，原计划失效")
    assert (cancel.when, cancel.bar) == ("close_below", "5m")
    enter = PlanLine(price=71.54, when="close_above", bar="5m", do="enter", side="long", stop_loss=71.2, take_profit=72.2, note="站上前高跟多")
    assert enter.when == "close_above"


async def test_reverse_trigger_line_and_after_stop_rule(rt, fake):
    """用户可提前设置两类自动反手触发器：① 到点位自动先平仓核实归零再开反向单（/api/lines do=reverse）；② 止损后自动反手（/api/strategies/reverse）。"""
    import httpx
    from ouy.app import create_app
    from ouy.models import TradeAction
    from ouy.strategy import rule_parts

    app = create_app(rt)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8899") as client:
        await rt.trader.execute(TradeAction(kind="open", inst_id=BTC, side="long", leverage=5, stop_loss=59_400, reason="测试"), "demo", source="user")
        assert fake.positions[BTC]["pos"] > 0

        r_after = await client.post("/api/strategies/reverse", json={"inst_id": BTC, "side": "short", "trigger": 59_380, "stop_loss": 59_800, "take_profit": 58_500, "leverage": 20, "margin_usdt": 15.0, "window_minutes": 60})
        assert r_after.status_code == 200
        row_after = r_after.json()
        when_s, do_s = rule_parts(row_after)
        assert row_after["kind"] == "reverse" and row_after["params"]["margin_usdt"] == 15.0 and row_after["params"]["leverage"] == 20 and "原仓止损" in when_s and "自动反手做空" in do_s
        await client.post(f"/api/strategies/{row_after['id']}/cancel")

        r_line = await client.post("/api/lines", json={"inst_id": BTC, "when": "price_touch", "level": 59_500, "do": "reverse", "side": "short", "stop_loss": 59_900, "take_profit": 58_600, "leverage": 5, "margin_usdt": 50.0})
        assert r_line.status_code == 200
        row_line = r_line.json()
        assert row_line["params"]["do"] == "reverse" and row_line["params"]["margin_usdt"] == 50.0 and "平掉原仓并市价反手做空" in rt.watcher.strategies.describe(row_line)

        await tick(rt, fake, 59_800)
        assert fake.positions[BTC]["pos"] > 0 and rt.store.get_strategy(row_line["id"])["status"] == "armed"

        await tick(rt, fake, 59_500)
        assert rt.store.get_strategy(row_line["id"])["status"] == "done"
        assert fake.positions[BTC]["pos"] < 0
        assert fake.algos[0]["slTriggerPx"] == "59900" and fake.algos[0]["tpTriggerPx"] == "58600"


async def test_live_price_feed_wakes_immediately_on_trigger_and_skips_redundant_http_poll(rt, fake):
    """实时 WebSocket 报价：平时不打断 3 秒周期，一旦触及触发线立刻置位 _wake_tick 毫秒级执行；且 _tick 直接复用新鲜 WS 报价不发多余 HTTP ticker。"""
    import time
    from ouy.okx.feed import PriceFeed
    from ouy.store import now_ms

    rt.save_config(rt.config().model_copy(update={"instruments": [BTC]}))
    w = rt.watcher
    w.running = True
    w.feed = PriceFeed(on_tick=w.on_live_price)
    add(rt, when="price_touch", level=59_500, do="wake", wake_note="触及反手线")

    st = w._state(BTC)
    st.last_px, st.last_review_ts, st.next_due_ts = 60_000.0, time.time(), time.time() + 3600
    w._wake_tick.clear()
    # 普通波动未碰线：更新最新价但不提前打断休眠
    w.on_live_price("demo", BTC, 59_900.0, now_ms())
    assert st.last_px == 59_900.0 and not w._wake_tick.is_set()

    # 价格瞬间触及 59500 触发线：毫秒级置位 _wake_tick 立刻唤醒执行！
    w.on_live_price("demo", BTC, 59_500.0, now_ms())
    assert w._wake_tick.is_set()

    # 首次 _tick 后算法时钟就绪，之后 _tick 直接复用新鲜 WS 报价，不再发 HTTP market_get_ticker
    w.feed.prices[("demo", BTC)] = (59_500.0, now_ms(), time.monotonic())
    await w._tick(rt.config())
    before = len(fake.called("market_get_ticker"))
    w.feed.prices[("demo", BTC)] = (59_510.0, now_ms(), time.monotonic())
    await w._tick(rt.config())
    assert len(fake.called("market_get_ticker")) == before
    w.running = False
