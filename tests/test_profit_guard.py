from __future__ import annotations

import time

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from ouy.models import TradeAction
from ouy.profit_guard import quote, signal
from ouy.trader import Rejected
from .test_agent_and_api import BTC, REPORT, make_rt


@pytest.fixture
def clock(fake, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    orig = fake._market_get_ticker
    monkeypatch.setattr(fake, "_market_get_ticker", lambda args: [
        {**orig(args)[0], "bidPx": str(fake.prices[args["instId"]]), "askPx": str(fake.prices[args["instId"]]), "ts": str(int(now[0] * 1000))}
    ])
    return now


async def position(rt, side="long", *, source="agent", kind="open", sl=None):
    sign = 1 if side == "long" else -1
    a = TradeAction(kind=kind, inst_id=BTC, side=side, stop_loss=sl or 60000-sign*600, take_profit=60000+sign*1000,
                    margin_usdt=20, leverage=5)
    r = await rt.trader.submit(a, source=source)
    assert r.ok, r.message
    return r.detail["lot_id"]


def attach(rt, lot_id, **options):
    return rt.watcher.strategies.deploy(rt.config(), BTC, "profit_guard", "", {"lot_id": lot_id, "on_trigger": "close", **options})["id"]


async def tick(rt, fake, clock, px, dt=3):
    clock[0] += dt
    fake.set_price(BTC, px)
    rt.trader._lot_touch.clear()
    cfg = rt.config()
    await rt.watcher.strategies.tick(cfg, await rt.trader.account(cfg.env), {BTC: px}, {BTC: fake._market_get_ticker({"instId": BTC})[0]})


def fok(fake):
    return [args for args in fake.called("swap_place_order") if args["ordType"] == "fok"]


@pytest.mark.parametrize("side", ["long", "short"])
async def test_worker_attaches_and_exit_is_scoped_to_one_lot(settings, fake, clock, side):
    lid = None

    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("deploy_strategy", {"inst_id": BTC, "lot_id": lid, "kind": "profit_guard", "on_trigger": "close", "reason": "这一笔按短线锁盈"})])
        assert "已布置" in str(returns[-1].content)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, REPORT)])

    rt = make_rt(settings, fake, FunctionModel(model))
    lid = await position(rt, side)
    fake.set_price(BTC, 60050 if side == "long" else 59950)
    other = await position(rt, side, kind="add", sl=59500 if side == "long" else 60500)
    await rt.watcher.review(BTC, "为这笔模拟仓添加提前止盈")
    sid = next(r["id"] for r in rt.store.strategies("demo") if r["kind"] == "profit_guard")
    sign = 1 if side == "long" else -1
    for gain in (500, 800, 750):
        await tick(rt, fake, clock, 60000 + gain*sign)
    assert not fok(fake)
    other_size = rt.store.lot(other)["contracts"]
    await tick(rt, fake, clock, 60000 + 560*sign)
    await tick(rt, fake, clock, 60000 + 550*sign)
    assert len(fok(fake)) == 1 and fok(fake)[0]["reduceOnly"] is True
    assert abs(fake.positions[BTC]["pos"]) == pytest.approx(other_size)
    assert rt.store.lot(lid)["status"] == "closed" and rt.store.lot(other)["status"] == "open"
    assert str(fake.algos[0]["algoId"]) == rt.store.lot(other)["algo_id"] and len(fake.algos) == 1
    assert any("已核对" in n["text"] for n in rt.watcher.inbox("demo", BTC)) and rt.store.get_strategy(sid)["status"] == "done"


async def test_stall_and_data_gap(rt, fake, clock):
    lid = await position(rt)
    attach(rt, lid, stall_minutes=0.5)
    await tick(rt, fake, clock, 60720)
    await tick(rt, fake, clock, 60723, dt=65)
    assert not fok(fake)
    for _ in range(2):
        await tick(rt, fake, clock, 60725, dt=10)
    assert not fok(fake)
    await tick(rt, fake, clock, 60724, dt=11)
    assert len(fok(fake)) == 1 and "没有明显推进" in rt.watcher.inbox("demo", BTC)[0]["text"]


@pytest.mark.parametrize("changed", ["manual-confirmed", "confirm-declined", "target-changed", "no-longer-profitable", "timeout", "off"])
async def test_approval_and_revalidation(rt, fake, clock, changed):
    lid = await position(rt, source="user" if changed == "manual-confirmed" else "agent")
    rt.save_config(rt.config().model_copy(update={"hands_off_manual": True}))
    sid = attach(rt, lid)
    if changed != "manual-confirmed":
        rt.save_config(rt.config().model_copy(update={"permissions": {"close": "off" if changed == "off" else "confirm"}}))
    await tick(rt, fake, clock, 60800)
    await tick(rt, fake, clock, 60550)
    assert not fok(fake)
    if changed == "off":
        assert not rt.store.pending("demo")
        return
    pid = rt.store.get_strategy(sid)["state"]["pending_id"]
    await tick(rt, fake, clock, 60549)
    assert len(rt.store.pending("demo")) == 1
    if changed == "confirm-declined":
        rt.trader.reject(pid)
    else:
        if changed == "target-changed":
            rt.store.update_lot(lid, take_profit=63000)
        if changed == "no-longer-profitable":
            fake.set_price(BTC, 60030)
        if changed == "timeout":
            clock[0] += 46
        result = await rt.trader.approve(pid)
        assert result.ok == (changed == "manual-confirmed"), result.message
    await tick(rt, fake, clock, 60551)
    assert len(fok(fake)) == int(changed == "manual-confirmed")
    assert ("已核对" in rt.watcher.inbox("demo", BTC)[-1]["text"]) == (changed == "manual-confirmed")


async def test_unfilled_keeps_protection_and_never_resubmits(rt, fake, clock):
    lid = await position(rt)
    sid = attach(rt, lid)
    fake.stuck.add(BTC)
    await tick(rt, fake, clock, 60800)
    await tick(rt, fake, clock, 60550)
    await tick(rt, fake, clock, 60500)
    assert len(fok(fake)) == 1 and fake.algos and rt.store.lot(lid)["status"] == "open"
    assert rt.store.get_strategy(sid)["status"] == "failed" and "未成交" in rt.watcher.inbox("demo", BTC)[0]["text"]


async def test_no_cross_env_wrong_id_duplicates_or_target_exit(rt, fake, clock):
    lid = await position(rt)
    with pytest.raises(Rejected, match="编号"):
        attach(rt, lid+1)
    attach(rt, lid)
    with pytest.raises(Rejected, match="已经有"):
        attach(rt, lid)
    await tick(rt, fake, clock, 60800)
    await tick(rt, fake, clock, 61001)
    assert not fok(fake) and "已不在" in rt.watcher.inbox("demo", BTC)[0]["text"]
    rt.save_config(rt.config().model_copy(update={"env": "live"}))
    with pytest.raises(Rejected, match="模拟盘"):
        attach(rt, lid)


def test_uses_exit_side_and_ignores_invalid_quotes_and_insufficient_profit():
    q = {"last": 110, "bidPx": 101, "askPx": 105, "ts": 100000}
    assert quote(q, True, 100) == (101, 100) and quote(q, False, 100) == (105, 100)
    assert not quote(q, True, 130) and not quote({**q, "askPx": 99}, False, 100)
    p = dict(target=110, entry=100, long=True, arm_pct=70, giveback_pct=25, stall_minutes=3, cost_pct=.15)
    state = {}
    assert not signal(p, state, 108, 100) and not signal(p, state, 100.10, 103)


async def test_notify_only_and_exchange_stop(rt, fake, clock):
    lid = await position(rt)
    engine = rt.watcher.strategies
    engine.deploy(rt.config(), BTC, "profit_guard", "", {"lot_id": lid})
    await tick(rt, fake, clock, 60800)
    await tick(rt, fake, clock, 60550)
    await tick(rt, fake, clock, 60500)
    assert len(rt.watcher.inbox("demo", BTC)) == 1 and "仅提醒" in rt.watcher.inbox("demo", BTC)[0]["text"]
    attach(rt, lid)
    await tick(rt, fake, clock, 60800)
    await tick(rt, fake, clock, 59399)
    assert not fok(fake) and rt.store.lot(lid)["status"] == "closed"


async def test_lot_rule_api_does_not_start_watcher_or_switch_environment(rt, fake):
    import httpx
    from ouy.app import create_app

    lid = await position(rt)
    url = f"/api/lots/{lid}/profit-guard"
    app = create_app(rt)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        bad = await c.post(url, json={"giveback_pct": 1})
        assert bad.status_code == 422
        result = await c.post(url, json={})
        assert result.status_code == 200 and rt.store.get_strategy(result.json()["id"])["params"]["on_trigger"] == "notify"
        rt.save_config(rt.config().model_copy(update={"env": "live"}))
        assert (await c.post(url, json={"on_trigger": "close"})).status_code == 400
    assert rt.config().env == "live" and not rt.watcher.running and not fok(fake)


async def test_profit_guard_after_partial_reduce(rt, fake, clock):
    lid = await position(rt)
    attach(rt, lid)
    await tick(rt, fake, clock, 60800)
    r = await rt.trader.submit(TradeAction(kind="reduce", inst_id=BTC, lot_id=lid, fraction=0.5), source="user")
    assert r.ok, r.message
    remaining = rt.store.lot(lid)["contracts"]
    await tick(rt, fake, clock, 60550)
    assert float(fok(fake)[0]["sz"]) == pytest.approx(remaining)


@pytest.mark.parametrize("kind,side,change,enters", [
    ("breakout", "long", "", True), ("pullback", "short", "", True),
    ("breakout", "long", "reversal", False), ("breakout", "long", "stale", False),
    ("pullback", "short", "wide", False), ("breakout", "long", "expired", False),
    ("breakout", "long", "permission", False), ("breakout", "long", "unapproved", False),
])
async def test_demo_scalp_preplanned_entry(rt, fake, clock, monkeypatch, kind, side, change, enters):
    cfg = rt.config().model_copy(update={"style": "scalp", "mode": "confirm" if change == "unapproved" else "auto"})
    rt.save_config(cfg)
    long = side == "long"
    sid = rt.watcher.strategies.deploy(cfg, BTC, kind, side, {
        "trigger": 60500, "take_profit": 62000 if long else 59500, "stop_loss": 60000 if long else 61000,
        "margin_usdt": 20, "expires_minutes": 1,
    })["id"]
    if change == "permission":
        rt.save_config(cfg.model_copy(update={"mode": "confirm"}))
    real = rt.market.ticker
    async def fresh(i, env):
        t = await real(i, env)
        return {**t, **{"wide": {"askPx": 60620}, "reversal": {"bidPx": 60499, "askPx": 60499, "last": 60499},
                        "stale": {"ts": (clock[0]-12)*1000}}.get(change, {})}
    monkeypatch.setattr(rt.market, "ticker", fresh)
    await tick(rt, fake, clock, 60520, dt=61 if change == "expired" else 3)
    await tick(rt, fake, clock, 60521)
    assert len(fake.called("swap_place_order")) == int(enters), rt.store.get_strategy(sid)["note"]
    if enters:
        assert fake.algos[0]["slTriggerPx"] and fake.algos[0]["tpTriggerPx"]
        assert "已提交" in rt.watcher.inbox("demo", BTC)[0]["text"]
