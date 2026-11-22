from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from ouy.agent.early_watch import QuoteWatch, ShiftRetest
from ouy.agent.watcher import InstState
from .test_agent_and_api import BTC, make_rt


async def test_confirmed_turn_reaches_the_review_already_in_progress(rt, monkeypatch):
    cfg = rt.config().model_copy(update={"instruments": [BTC], "intel_minutes": 0})
    rt.save_config(cfg)
    watcher = rt.watcher
    state = watcher._state(BTC)
    messages: list[tuple[str, str]] = []
    state.reviewing = True
    state.run = SimpleNamespace(enqueue=lambda text, *, priority: messages.append((text, priority)))

    async def confirmed_turn(*args):
        return "5 分钟收盘确认转折"

    monkeypatch.setattr(watcher, "_shift_actions_on", lambda *args: True)
    monkeypatch.setattr(watcher, "_shift_watch", confirmed_turn)

    await watcher._tick(cfg)

    assert len(messages) == 1
    assert "5 分钟收盘确认转折" in messages[0][0] and messages[0][1] == "asap"
    assert state.task is None


def test_quotes_approach_without_touch_or_repeated_wake():
    watch = QuoteWatch()
    events = [watch.sample(3 * i, px, {100}, .6) for i, px in enumerate(
        [99.7, 99.82, 99.87, 99.88, 99.87, 99.88, 99.89])]
    assert [i for i, event in enumerate(events) if event] == [2]
    assert "尚未触线" in events[2]
    for t in range(21, 90, 3):
        assert not watch.sample(t, 99.88, {100}, .6)
    assert not watch.sample(90, 99.65, {100}, .6)
    assert "接近观察位" in watch.sample(93, 99.87, {100}, .6)


def test_opening_quote_rollover_dedup_and_minute_history(settings, fake):
    rt = make_rt(settings, fake, "test")
    st = InstState()
    day = 86_400_000
    ts = day * 2 - 3000
    m = {"source_env": "demo", "asset": "crypto", "session": {"anchor_ms": day, "open": 100},
         "forming_5m": {"ts": ts // 300_000 * 300_000, "open": 104, "previous_closes": []}}
    for delta, px in ((0, 104), (1000, 105), (0, 120), (2000, 103)):
        rt.watcher._opening_sample("demo", BTC, st, ts + delta, px, m, ts + 2000)
    assert st.opening_quote["session_pct"] == 3 and st.opening_quote["bar_pct"] < 0
    rt.watcher._opening_sample("demo", BTC, st, 2 * day, 110, m, 2 * day)
    assert "session_open" not in st.opening_quote and "bar_open" not in st.opening_quote
    before, after = rt.store.opening_minutes("demo", BTC)
    assert (before["samples"], before["first"], before["high"], before["low"], before["last"]) == (3, 104, 105, 103, 103)
    assert after["last"] == 110 and not rt.store.opening_minutes("live", BTC) and not fake.orders


@pytest.mark.parametrize(("side", "adverse", "back"), [("long", 99.8, 100.2), ("short", 100.2, 99.8)])
def test_shift_retest_needs_fresh_distinct_quotes_and_limits_wakes(side, adverse, back):
    watch = ShiftRetest()
    m = {"ts": 1000_000, "values": {"atr": 1}, "shift": {"ts": 700_000, "bars_ago": 0, "side": side, "level": 100}}
    assert not watch.sample(1000, 1000, adverse, m)
    assert not watch.sample(1001, 1000, adverse, m)  # 同一个报价不能重复计数
    assert not watch.sample(1002, 1002, 100.01, m)  # 靠线抖动，清掉未确认的那一次
    assert not watch.sample(1003, 1003, adverse, m)
    assert "疑似假突破" in watch.sample(1006, 1006, adverse, m) and watch.first_loss  # 同一次转折只有第一次失守叫醒 Worker
    assert not watch.sample(1009, 1009, adverse, m)
    assert not watch.sample(1012, 1012, back, m)
    assert "回到原突破方向" in watch.sample(1015, 1015, back, m) and not watch.first_loss
    assert not watch.sample(1018, 1018, adverse, m)
    assert "疑似假突破" in watch.sample(1021, 1021, adverse, m) and not watch.first_loss
    watch = ShiftRetest()
    assert not any(watch.sample(t, t, adverse, m) for t in (1040, 1043))  # 结构样本太旧


@pytest.mark.parametrize(("prices", "event"), [([100, 100.10, 100.35], "反弹"), ([100, 99.90, 99.65], "回落")])
def test_fast_move_without_level_and_no_stale_or_duplicate_samples(prices, event):
    watch = QuoteWatch()
    assert not watch.sample(0, prices[0], set(), .6)
    assert not watch.sample(3, prices[1], set(), .6)
    assert not watch.sample(3, 120, set(), .6)
    assert not watch.sample(1, 70, set(), .6)
    assert event in watch.sample(6, prices[2], set(), .6)
    assert not watch.sample(200, 110, set(), .6)


@pytest.mark.parametrize("env", ["demo", "live"])
async def test_actual_tick_only_observes_demo_without_orders(settings, fake, monkeypatch, env):
    requests = []

    def run(messages, info):
        assert not info.function_tools
        requests.append(str(messages))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,
                                                 {"reason": "尚未确认突破，先观察。", "observe_price": 99.92})])

    rt = make_rt(settings, fake, FunctionModel(run))
    cfg = rt.config().model_copy(update={"env": env, "intel_minutes": 0})
    rt.save_config(cfg)
    w = rt.watcher
    st = w._state(BTC)
    report = st.report = {"watch_levels": [{"price": 100, "note": "前高"}], "summary": "等待确认"}
    start = time.time()
    st.last_review_ts, st.next_due_ts, st.last_review_px = start, start + 300, 99.7
    stamp = [start - 9]
    base = fake._market_get_ticker
    monkeypatch.setattr(fake, "_market_get_ticker", lambda args: [{**base(args)[0], "ts": int(stamp[0] * 1000)}])
    for px in [99.7, 99.82, 99.87, 99.88]:
        fake.set_price(BTC, px)
        await w._tick(cfg)
        if st.observe_task:
            await st.observe_task
        stamp[0] += 3

    assert len(requests) == (1 if env == "demo" else 0)
    assert st.observe_price == (99.92 if env == "demo" else None)
    assert st.report is report and st.last_review_ts == start and not st.task
    assert not fake.positions and not fake.orders and not fake.algos
    if env == "demo":
        assert "尚未触线" in requests[0]


async def test_no_observation_after_stale_quote_or_stale_busy_signal(rt):
    w, cfg = rt.watcher, rt.config()
    st = w._state(BTC)
    st.report = {"watch_levels": [{"price": 100}]}
    now = time.time()
    st.last_px = 99.65
    w._sample_observation(cfg, BTC, st, now - 60, now, False)
    assert not st.quote_watch.samples
    w._sample_observation(cfg, BTC, st, now, now, True)
    st.last_px = 99.88
    w._sample_observation(cfg, BTC, st, now + 3, now + 3, True)
    assert st.pending_observation and not st.observe_task
    w._sample_observation(cfg, BTC, st, now + 24, now + 24, False)
    assert not st.pending_observation and not st.observe_task


async def test_one_turn_wakes_the_worker_once_and_logs_the_rest_under_one_algorithm(rt, fake, monkeypatch):
    """确认之后盘中失守 → 收回 → 再失守：三次都记成「转折」这一个算法的事件，只有第一次失守叫醒 Worker。"""
    w, cfg = rt.watcher, rt.config().model_copy(update={"intel_minutes": 0})
    rt.save_config(cfg)
    st = w._state(BTC)
    start = time.time()
    st.last_review_ts, st.next_due_ts, st.last_review_px = start, start + 600, 100.2
    shift = {"side": "long", "kind": "反转", "level": 100.0, "origin": 99.0, "ts": int((start - 400) * 1000), "bars_ago": 1, "why": "收盘站上前一个摆动高点 100"}
    woken: list[str] = []

    async def review(inst_id, reason, gate):
        woken.append(reason)
        st.reviewing = False

    async def no_rules(*args, **kwargs):
        return None

    monkeypatch.setattr(w, "_review_task", review)
    monkeypatch.setattr(w.strategies, "tick", no_rules)
    stamp = [start - 9]
    base = fake._market_get_ticker
    monkeypatch.setattr(fake, "_market_get_ticker", lambda args: [{**base(args)[0], "ts": int(stamp[0] * 1000)}])
    for px in [99.9, 99.9, 100.1, 100.1, 99.9, 99.9]:
        w.strategies.metrics[BTC] = {"ts": int(time.time() * 1000), "source_env": cfg.env, "values": {"px": px, "atr": 0.2}, "levels": {}, "why": {}, "shift": shift, "asset": "crypto"}
        fake.set_price(BTC, px)
        await w._tick(cfg)
        if st.task:
            await st.task
        stamp[0] += 1

    events = [e for e in w.algo_report(cfg.env, BTC)["events"] if e["algo"] == "turn"][::-1]
    assert [e["text"][:4] for e in events] == ["盘中失守", "盘中收回", "盘中失守"]
    assert [e["effect"].startswith("叫醒") for e in events] == [True, False, False]
    assert [e["side"] for e in events] == ["short", "long", "short"]
    assert len([r for r in woken if "疑似假突破" in r]) == 1 and not any("回到原突破方向" in r for r in woken)
    turn = w.status()["instruments"][BTC]["turn"]
    assert turn["stage"] == "lost" and turn["side"] == "long" and turn["level"] == 100.0
