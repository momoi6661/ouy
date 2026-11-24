"""欧易公共行情 WebSocket 实时报价源（单连接订阅全部盯盘品种，毫秒级推送，不占 REST 限频）。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

import websockets

from ..store import now_ms

log = logging.getLogger("ouy.feed")

WS_URLS = {
    "live": "wss://ws.okx.com:8443/ws/v5/public",
    "demo": "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999",
}


class PriceFeed:
    """维护一条欧易公共 tickers WebSocket 长连接，收到报价立刻回调 on_tick 并缓存最新价。"""

    def __init__(
        self,
        *,
        proxy_url: Callable[[], str | None] = lambda: None,
        on_tick: Callable[[str, str, float, int], None] | None = None,
    ):
        self.proxy_url = proxy_url
        self.on_tick = on_tick
        self.prices: dict[tuple[str, str], tuple[float, int, float]] = {}  # (env, inst) -> (px, ts_ms, mono)
        self._env = ""
        self._insts: tuple[str, ...] = ()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self.connected = False

    def fresh_price(self, env: str, inst_id: str, max_age: float = 4.0) -> tuple[float, int] | None:
        item = self.prices.get((env, inst_id))
        if item and time.monotonic() - item[2] <= max_age:
            return item[0], item[1]
        return None

    def start(self, env: str, instruments: list[str]) -> None:
        want = tuple(sorted({i.upper() for i in instruments if i}))
        if self._running and self._env == env and self._insts == want and self._task and not self._task.done():
            return
        self._env, self._insts, self._running = env, want, True
        if self._task and not self._task.done():
            self._task.cancel()
        if want:
            self._task = asyncio.create_task(self._run(env, want), name=f"ouy-feed-{env}")

    async def stop(self) -> None:
        self._running = False
        self.connected = False
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self, env: str, insts: tuple[str, ...]) -> None:
        url = WS_URLS.get(env, WS_URLS["live"])
        delay = 1.0
        while self._running and self._env == env and self._insts == insts:
            try:
                proxy: Any = self.proxy_url() or True
                async with websockets.connect(url, proxy=proxy, open_timeout=10, ping_interval=20, ping_timeout=15) as ws:
                    self.connected = True
                    delay = 1.0
                    args = [{"channel": "tickers", "instId": i} for i in insts]
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    async for raw in ws:
                        if not self._running:
                            break
                        if not isinstance(raw, str) or raw == "pong":
                            continue
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        for row in msg.get("data") or ():
                            inst = row.get("instId")
                            try:
                                px = float(row.get("last") or 0)
                            except (TypeError, ValueError):
                                continue
                            if not inst or px <= 0:
                                continue
                            try:
                                ts = int(row.get("ts") or 0) or now_ms()
                            except (TypeError, ValueError):
                                ts = now_ms()
                            self.prices[(env, inst)] = (px, ts, time.monotonic())
                            if self.on_tick:
                                try:
                                    self.on_tick(env, inst, px, ts)
                                except Exception:  # noqa: BLE001 - 单条回调异常不断流
                                    log.exception("实时报价回调异常 %s", inst)
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                log.debug("欧易实时行情 WebSocket 断开，%.0f 秒后重连：%s", delay, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)
