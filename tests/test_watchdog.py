from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from pydantic_ai.models.function import FunctionModel

from .test_agent_and_api import BTC, make_rt


@pytest.mark.parametrize("wake", ["always", "event"])
async def test_failure_backoff_blocks_automatic_wakes_but_keeps_rules_running(rt, fake, monkeypatch, wake):
    w = rt.watcher
    cfg = rt.config().model_copy(update={"instruments": [BTC], "intel_minutes": 0, "wake": wake})
    rt.save_config(cfg)
    st = w._state(BTC)
    clock = time.time()
    st.last_review_ts, st.last_review_px = clock - 60, fake.prices[BTC]
    st.next_due_ts, st.retry_after_ts = clock - 1, clock + 120
    st.fails = 1
    rule = w.strategies.deploy(cfg, BTC, "trigger", "", {"when": "price_above", "level": 60_100, "do": "wake"})
    calls = []

    async def review(*args):
        calls.append(args)
        st.reviewing = False

    monkeypatch.setattr(w, "_review_task", review)
    monkeypatch.setattr("ouy.agent.watcher.time.time", lambda: clock)
    await w._tick(cfg)
    fake.set_price(BTC, 60_200)
    await w._tick(cfg)
    assert rt.store.get_strategy(rule["id"])["status"] == "done"
    assert w.inbox("demo", BTC) and st.pending_wakes
    assert calls == [] and st.task is None
    clock += 121
    await w._tick(cfg)
    await st.task
    assert len(calls) == 1


async def test_continuous_mode_respects_the_workers_next_check(rt, fake, monkeypatch):
    w = rt.watcher
    cfg = rt.config().model_copy(update={"instruments": [BTC], "wake": "always", "intel_minutes": 0})
    rt.save_config(cfg)
    st = w._state(BTC)
    clock = time.time()
    st.last_review_ts, st.last_review_px = clock - 60, fake.prices[BTC]
    st.next_due_ts = clock + 120
    calls = []

    async def review(*args):
        calls.append(args)
        st.reviewing = False

    async def algo(*args):
        pass

    monkeypatch.setattr(w, "_review_task", review)
    monkeypatch.setattr(w, "_algo_tick", algo)
    monkeypatch.setattr("ouy.agent.watcher.time.time", lambda: clock)
    await w._tick(cfg)
    assert st.task is None and calls == []
    clock += 121
    await w._tick(cfg)
    await st.task
    assert len(calls) == 1 and calls[0][2] is False


async def test_model_failure_restores_unread_events(settings, fake):
    def fail(messages, info):
        raise RuntimeError("upstream 503")

    rt = make_rt(settings, fake, FunctionModel(fail))
    w, st = rt.watcher, rt.watcher._state(BTC)
    note = {"who": "recorder", "text": "review the crossed level", "ts": 1000}
    rt.store.set(f"inbox:demo:{BTC}", [note])
    await w._review_task(BTC, "event")
    assert w.inbox("demo", BTC) == [note]
    assert st.fails == 1 and round(st.retry_after_ts - st.last_review_ts) == 120
    assert st.next_due_ts == st.retry_after_ts


async def test_saved_hard_event_is_not_filtered_as_a_timer(rt, fake, monkeypatch):
    w = rt.watcher
    cfg = rt.config().model_copy(update={"instruments": [BTC], "intel_minutes": 0})
    rt.save_config(cfg)
    st = w._state(BTC)
    st.last_review_ts, st.last_review_px = time.time() - 600, fake.prices[BTC]
    st.pending_wakes = ["position changed during backoff"]
    calls = []

    async def record(*args):
        calls.append(args)
        st.reviewing = False

    async def algo(*args):
        pass

    monkeypatch.setattr(w, "_review_task", record)
    monkeypatch.setattr(w, "_algo_tick", algo)
    await w._tick(cfg)
    await st.task
    assert calls == [(BTC, "position changed during backoff", False)]


async def test_watchdog_recovery_waits_for_the_previous_loop_to_exit(rt, monkeypatch):
    w = rt.watcher
    started, cleaning, finish, replacement = [asyncio.Event() for _ in range(4)]

    async def old_loop():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()

    async def new_loop():
        assert finish.is_set()
        replacement.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(w, "_loop", new_loop)
    w.running, w._beat = True, 0
    previous = w._task = asyncio.create_task(old_loop())
    await started.wait()
    recovery = asyncio.create_task(w._recover(rt.config()))
    try:
        await asyncio.wait_for(cleaning.wait(), 2)
        assert w._task is previous and not replacement.is_set()
        finish.set()
        await asyncio.wait_for(recovery, 2)
        await asyncio.wait_for(replacement.wait(), 2)
        assert previous.done() and w._task is not previous
    finally:
        finish.set()
        await recovery
        await w.stop()


async def test_watchdog_timeout_backoff_and_queue_wait_are_distinct(rt):
    w, st = rt.watcher, rt.watcher._state(BTC)
    started = asyncio.Event()

    async def stuck():
        started.set()
        await asyncio.Event().wait()

    st.task = asyncio.create_task(stuck())
    await started.wait()
    st.reviewing, st.review_started = True, 0
    await w._recover(rt.config())
    assert not st.task.done()  # Waiting for the Worker lock has no execution timer.
    st.review_started = time.time() - 400
    await w._recover(rt.config())
    assert st.task.done() and not st.reviewing
    assert st.retry_after_ts > time.time() + 119


async def test_slow_maintenance_does_not_block_watchdog_or_duplicate_jobs(rt, monkeypatch):
    w = rt.watcher
    pulses, checks = asyncio.Queue(), asyncio.Queue()
    patrol_started, maintenance_started = asyncio.Event(), asyncio.Event()
    jobs = []

    async def tick_sleep(seconds):
        assert seconds == 15
        await pulses.get()

    async def recover(cfg):
        checks.put_nowait(True)

    async def patrol(cfg):
        jobs.append("patrol")
        patrol_started.set()
        await asyncio.Event().wait()

    async def maintenance(cfg):
        jobs.append("maintenance")
        maintenance_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("ouy.agent.watcher.asyncio.sleep", tick_sleep)
    monkeypatch.setattr(w, "_recover", recover)
    monkeypatch.setattr(w, "_patrol", patrol)
    monkeypatch.setattr(w, "_maintenance", maintenance)
    w.running = True
    w._watchdog_task = asyncio.create_task(w._watch_loop())
    try:
        pulses.put_nowait(True)
        await asyncio.wait_for(checks.get(), 2)
        await asyncio.wait_for(patrol_started.wait(), 2)
        await asyncio.wait_for(maintenance_started.wait(), 2)
        for _ in range(4):
            pulses.put_nowait(True)
            await asyncio.wait_for(checks.get(), 2)
        assert jobs == ["patrol", "maintenance"]
    finally:
        await w.stop()
    assert w._patrol_task.done() and w._maintenance_task.done()


async def test_failed_closed_trade_reviews_still_count_towards_batch_limit(rt, monkeypatch):
    w = rt.watcher
    trades = [SimpleNamespace(inst_id=BTC, closed_ts=int(time.time() * 1000) - i, how="closed") for i in range(6)]
    attempts = []

    async def history(*args):
        return trades

    async def fail(cfg, trade):
        attempts.append(trade)
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(rt.trader, "history", history)
    monkeypatch.setattr(w, "_review_trade", fail)
    rt.store.set("reviewed:demo", [])
    assert await w._review_trades(rt.config()) == 0
    assert len(attempts) == 3


async def test_acknowledging_a_review_preserves_events_that_arrive_during_it(rt, monkeypatch):
    w, st = rt.watcher, rt.watcher._state(BTC)
    st.pending_wakes = ["order filled", "level crossed"]

    async def review(*args, **kwargs):
        st.pending_wakes.append("position reduced")

    monkeypatch.setattr(w, "review", review)
    await w._review_task(BTC, "order filled; level crossed")
    assert st.pending_wakes == ["position reduced"]


async def test_recovery_exception_does_not_kill_the_watchdog(rt, monkeypatch):
    w = rt.watcher
    pulses, attempts = asyncio.Queue(), asyncio.Queue()

    async def sleep(seconds):
        await pulses.get()

    async def broken_recovery(cfg):
        attempts.put_nowait(True)
        raise RuntimeError("temporary recovery failure")

    async def no_op(cfg):
        pass

    monkeypatch.setattr("ouy.agent.watcher.asyncio.sleep", sleep)
    monkeypatch.setattr(w, "_recover", broken_recovery)
    monkeypatch.setattr(w, "_patrol", no_op)
    monkeypatch.setattr(w, "_maintenance", no_op)
    w.running = True
    w._watchdog_task = asyncio.create_task(w._watch_loop())
    try:
        for _ in range(2):
            pulses.put_nowait(True)
            await asyncio.wait_for(attempts.get(), 2)
        assert not w._watchdog_task.done()
        errors = [a for a in rt.store.activity("demo") if a["agent"] == "watchdog" and a["kind"] == "error"]
        assert len(errors) == 1 and errors[0]["data"]["error"]["type"] == "RuntimeError"
    finally:
        await w.stop()


async def test_shutdown_cleans_scheduled_work_even_if_not_marked_running(rt):
    w, st = rt.watcher, rt.watcher._state(BTC)
    started = asyncio.Event()

    async def task():
        started.set()
        await asyncio.Event().wait()

    st.task = asyncio.create_task(task())
    await started.wait()
    assert not w.running
    await w.stop()
    assert st.task.cancelled()
