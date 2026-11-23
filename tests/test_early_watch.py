from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from ouy.agent.early_watch import ShiftRetest
from ouy.agent.signals import trend_run
from ouy.indicators import Candle
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


async def test_one_turn_wakes_the_worker_once_and_logs_the_rest_under_one_algorithm(rt, fake, monkeypatch):
    """确认之后盘中失守 → 收回 → 再失守：三次都记成「转折」这一个算法的事件，只有第一次失守叫醒 Worker。"""
    w, cfg = rt.watcher, rt.config().model_copy(update={"intel_minutes": 0})
    rt.save_config(cfg)
    st = w._state(BTC)
    start = time.time()
    st.last_review_ts, st.next_due_ts, st.last_review_px = start, start + 600, 100.2
    shift = {"side": "long", "kind": "反转", "level": 100.0, "origin": 99.0, "ts": int((start - 400) * 1000), "bars_ago": 1, "why": "收盘站上前一个摆动高点 100"}
    woken: list[str] = []

    async def review(inst_id, reason, source):
        woken.append(reason)
        st.reviewing = False

    async def no_rules(*args, **kwargs):
        return None

    monkeypatch.setattr(w, "review", review)
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


def _closes(closes: list[float], step_ms: int = 900_000) -> list[Candle]:
    out, prev = [], closes[0]
    for i, close in enumerate(closes):
        out.append(Candle(i * step_ms, prev, max(prev, close) + 1, min(prev, close) - 1, close, 100.0))
        prev = close
    return out


def test_trend_run_needs_consecutive_closes_and_enough_distance():
    flat = [100 + (i % 2) for i in range(31)]  # 一上一下，最后一根收低：平均波幅约 3
    assert trend_run(_closes(flat)) is None
    assert trend_run(_closes([*flat, 103, 105])) is None  # 只连了两根
    assert trend_run(_closes([*flat, 102, 103, 104])) is None  # 连了三根但没走出几倍波幅
    run = trend_run(_closes([*flat, 104, 108, 112]))
    assert run and run["side"] == "long" and run["bars"] == 3 and run["origin"] == 100 and run["to"] == 112 and run["step"] == 1
    assert run["ts"] == 30 * 900_000 and run["bar_ts"] == 33 * 900_000
    longer = trend_run(_closes([*flat, 104, 108, 112, 113]))
    assert longer["ts"] == run["ts"] and longer["step"] == 1  # 同一段走线、没多走出一档：不算新事件
    assert trend_run(_closes([*flat, 104, 108, 112, 124]))["step"] == 2
    down = trend_run(_closes([*flat, 97, 93, 89]))
    assert down["side"] == "short" and down["pct"] < 0
    assert trend_run(_closes([*flat, 104, 108, 112, 111])) is None  # 最后一根反着收：这段走线结束


async def test_a_run_wakes_the_worker_once_per_step_and_is_recorded(rt, fake, monkeypatch):
    cfg, w = rt.config(), rt.watcher
    st = w._state(BTC)
    st.last_px = 112.0
    snap = await rt.market.snapshot(BTC, cfg.env, ("5m", "15m", "1H", "4H"))
    flat = [100 + (i % 2) for i in range(31)]
    bars = _closes([*flat, 104, 108, 112])
    clock = [bars[-1].ts + 900_000 + 1]
    snap["timeframes"]["15m"]["last_closed_open_ts_ms"] = bars[-1].ts

    async def snapshot(*args):
        return snap

    async def candles(inst, bar, limit, env):
        return bars

    monkeypatch.setattr(rt.market, "snapshot", snapshot)
    monkeypatch.setattr(rt.market, "candles", candles)
    monkeypatch.setattr("ouy.agent.watcher.now_ms", lambda: clock[0])
    await w._algo_tick(cfg, BTC, st)
    runs = [e for e in rt.store.algo_events("demo", BTC) if e["algo"] == "run"]
    assert len(runs) == 1 and runs[0]["side"] == "long" and "连续 3 根收高" in runs[0]["text"] and runs[0]["effect"].startswith("叫醒")
    mail = [n for n in w.inbox("demo", BTC) if n.get("market_key") == "run"]
    assert len(mail) == 1 and "不要因此追单" in mail[0]["text"]

    bars = _closes([*flat, 104, 108, 112, 113])  # 又收高一根，但没多走出一档
    snap["timeframes"]["15m"]["last_closed_open_ts_ms"] = bars[-1].ts
    clock[0] += 900_000
    await w._algo_tick(cfg, BTC, st)
    assert len([e for e in rt.store.algo_events("demo", BTC) if e["algo"] == "run"]) == 1
    bars = _closes([*flat, 104, 108, 112, 113, 125])  # 走出第二档：再报一次，信箱里还是只有最新的一条
    snap["timeframes"]["15m"]["last_closed_open_ts_ms"] = bars[-1].ts
    clock[0] += 900_000
    await w._algo_tick(cfg, BTC, st)
    assert len([e for e in rt.store.algo_events("demo", BTC) if e["algo"] == "run"]) == 2
    assert len([n for n in w.inbox("demo", BTC) if n.get("market_key") == "run"]) == 1
    assert w.ALGOS["run"] == "连续走线" and w.ALGO_LAYER["run"] == "read"
