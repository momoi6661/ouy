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


def test_wave_tracker_records_pivots_direction_and_amplitude():
    from ouy.agent.early_watch import WaveTracker

    wt = WaveTracker(hold_ms=0)
    assert not wt.update(1000, 100.0, atr5=0.5)
    assert not wt.update(2000, 100.1, atr5=0.5)
    assert wt.update(3000, 100.5, atr5=0.5)  # 向上拐头推进
    assert wt.dir == 1 and wt.last_trough == 100.0
    wt.update(4000, 102.0, atr5=0.5)  # 波峰拉到 102
    assert wt.update(5000, 101.6, atr5=0.5)  # 从 102 回落超过阈值：确认波峰 102，转为向下回踩
    assert wt.dir == -1 and wt.last_peak == 102.0 and wt.prev_pct == pytest.approx(2.0, abs=0.01)
    wt.update(6000, 101.0, atr5=0.5)  # 回踩到 101.0
    assert wt.update(7000, 101.35, atr5=0.5)  # 从 101.0 止跌向上拐头！
    r = wt.readings(101.4)
    assert wt.dir == 1 and wt.last_trough == 101.0 and r["wave_dir"] == 1 and r["wave_move_pct"] > 0.35
    assert "波谷 101" in wt.summary(101.4)
    # 继续突破前波峰 102.0：确认波峰波谷同步抬高（prev_trough=100 < last_trough=101 且 px > last_peak=102），触发盘中实时走向切换
    wt.update(8000, 102.3, atr5=0.5)
    flip = wt.trend_flip(102.3, atr5=0.5)
    assert flip and flip[0] == "long" and "盘中实时走向转多" in flip[1]
    assert wt.trend_flip(102.5, atr5=0.5) is None  # 同方向不重复触发

    # 盘中实时走向不改大方向（回放里它切换后继续同向不到一半）：上一小时是上涨，就仍按上涨
    from ouy.agent.plan import major_direction
    snap = {
        "last": 76.0, "live_wave": {"trend": "short"},
        "timeframes": {"5m": {"structure": "downtrend", "adx14": 35, "atr14": 0.4}},
        "windows": {"1H": {"trend": "上涨（高点低点都在抬高）"}},
    }
    assert major_direction(snap, "scalp")[0] == "long"


async def test_pullback_limit_catches_up_on_early_turn_and_position_trails_new_pivot(settings, fake):
    from ouy.models import TradeAction
    from .test_agent_and_api import REPORT
    from .test_plan import PULLBACK, reporting

    # 1. 当日亏损默认 0 = 不限：即使日初权益高，也不会设 halted
    rt = make_rt(settings, fake, reporting({**REPORT, "plan": PULLBACK}))
    rt.store.set(f"day:demo:{time.strftime('%Y-%m-%d')}", fake.cash * 3)
    acct0 = await rt.trader.account("demo")
    assert rt.config().risk.max_daily_loss_pct == 0 and not acct0.halted

    # 2. SOL 场景：现价 60000 时挂 59700 回踩多单，价格回踩到 59800（过半）后提前止跌拐头弹回 59950 → 自动撤掉 59700 改现价追进
    st = rt.watcher._state(BTC)
    st.last_px = 60_000
    await rt.watcher.review(BTC, "定时复盘")
    assert len(fake.orders) == 1 and float(fake.orders[0]["px"]) == 59_700 and BTC not in fake.positions

    t0 = int(time.time() * 1000) + 1000
    for dt, px in ((0, 60_000), (3000, 59_800), (6000, 59_950)):
        st.wave.update(t0 + dt, px, atr5=80.0)
        fake.set_price(BTC, px)
        st.last_px = px
        await rt.watcher._guard_orders(rt.config(), await rt.trader.account("demo"), {BTC: px})

    assert not fake.orders and fake.positions[BTC]["pos"] > 0
    assert any("提前止跌拐头" in n["text"] or "提前拐头" in n["text"] for n in rt.watcher.inbox("demo", BTC))

    # 3. 持仓浮盈 ≥1.2R 后走出新的更高波谷 60400 并向上推进到 60800 → 自动把止损推到新波谷 60400 外侧
    rt.trader._lot_touch.clear()
    await rt.trader.account("demo")
    t1 = int(time.time() * 1000) + 12000
    for dt, px in ((0, 60_650), (3000, 60_400), (6000, 60_800)):
        st.wave.update(t1 + dt, px, atr5=80.0)
        fake.set_price(BTC, px)
        st.last_px = px
        await rt.watcher._watch_lots(rt.config(), BTC, st, time.time())
    new_sl = rt.trader.lots("demo", BTC)[0]["stop_loss"]
    assert 60_200 < new_sl < 60_400


def test_wave_tracker_rejects_short_wick_computes_velocity_and_chart_trend_marks():
    """高频实时报价：0.5 秒毛刺假针不确认拐点，站稳 2 秒后确认；同时算出 10s/30s/60s 涨速与图表「上·延续 / 下·转折」标记。"""
    from ouy.agent.early_watch import WaveTracker
    from ouy.agent.signals import chart_trend_marks

    w = WaveTracker(hold_ms=2000)
    t = 1_700_000_000_000
    w.update(t, 100.0, 0.2)
    # 0.5 秒瞬间插一根 +0.4% 的毛刺针又缩回：不确认拐点
    assert w.update(t + 500, 100.4, 0.2) is False and w.dir == 0
    assert w.update(t + 900, 100.05, 0.2) is False and w.dir == 0
    # 真正站稳 2 秒以上：确认向上拐点
    assert w.update(t + 1500, 100.35, 0.2) is False
    assert w.update(t + 3600, 100.40, 0.2) is True and w.dir == 1

    # 连续推高 35 秒形成 +0.8% 急涨，验证 chg_10s / chg_30s 与 surge_event
    for sec in range(5, 36, 5):
        px = 100.4 + sec * 0.025
        w.update(t + 3600 + sec * 1000, px, 0.2)
    r = w.readings(100.4 + 35 * 0.025)
    assert r["chg_10s"] > 0 and r["chg_30s"] >= 0.5
    surge = w.surge_event(t + 39_600, 100.4 + 35 * 0.025, 0.2)
    assert surge is not None and surge[0] == "long"

    # 验证盘中 100%+ 反包大涨（如实盘 ETH 下跌后瞬间反包 +0.35% / 116%）：立即触发 live_trend("long") 与 surge_event("long")
    w2 = WaveTracker(hold_ms=0)
    w2.update(t, 1702.0, 5.2)
    w2.update(t + 3000, 1696.8, 5.2)  # 下跌 -0.305%
    w2.update(t + 6000, 1702.99, 5.2)  # 瞬间拉起 +0.365%（反包 119%）
    assert w2.live_trend(1702.99, 5.2) == "long"
    assert w2.surge_event(t + 6000, 1702.99, 5.2)[0] == "long"

    # 图表走势标记：连续上升台阶再下破反转，能清晰分出「上·延续」与「下·转折」
    seq = [100, 101, 100, 102, 101, 103, 102, 104, 103, 105, 104, 106, 105, 107, 106, 108]
    bars_px = [*([100, 101] * 14), *seq, 111, 112, 110, 109, 111, 114, 115, 112, 107, 104]
    marks = chart_trend_marks(_closes(bars_px))
    assert any(m["side"] == "long" and "上·" in m["label"] for m in marks)
    assert any(m["side"] == "short" and m["label"] == "下·转折" for m in marks)
