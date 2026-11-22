"""历史 K 线的本地缓存：回测和研究都从这里取数，同一份数据可以反复跑，结果才能互相比较。

缓存放在 data/candles/<品种>_<周期>.csv（时间升序，只存已收盘的 K 线）。取数时先读缓存，只向欧易公开接口补缺的那一段。
"""
from __future__ import annotations

import asyncio
import csv
import time
from pathlib import Path

import httpx

from .config import ROOT
from .indicators import BAR_MINUTES, Candle

HOST = "https://www.okx.com"
CACHE_DIR = ROOT / "data" / "candles"


def cache_path(inst: str, bar: str, root: Path | None = None) -> Path:
    return (root or CACHE_DIR) / f"{inst.upper()}_{bar}.csv"


def read_cache(path: Path) -> list[Candle]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), True) for r in csv.reader(fh) if r and r[0].isdigit()]


def write_cache(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.ts, f"{c.open:.12g}", f"{c.high:.12g}", f"{c.low:.12g}", f"{c.close:.12g}", f"{c.volume:.12g}"])
    tmp.replace(path)


async def _page(client: httpx.AsyncClient, inst: str, bar: str, after: int) -> list[Candle]:
    """比 after（毫秒）更早的一页，最多 100 根，只留已收盘的。"""
    for attempt in range(5):
        try:
            resp = await client.get(f"{HOST}/api/v5/market/history-candles", params={"instId": inst, "bar": bar, "limit": 100, "after": after})
            body = resp.json()
            if resp.status_code == 429 or str(body.get("code")) == "50011":  # 限频：等一下再来
                raise httpx.HTTPError("rate limited")
            rows = body.get("data") or []
            return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), True) for r in rows if len(r) < 9 or r[8] == "1"]
        except (httpx.HTTPError, ValueError):
            if attempt == 4:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
    return []


async def fetch_range(client: httpx.AsyncClient, inst: str, bar: str, start_ms: int, end_ms: int) -> list[Candle]:
    """[start_ms, end_ms) 之间已收盘的 K 线（旧 → 新）。"""
    out: dict[int, Candle] = {}
    after = end_ms
    while after > start_ms:
        rows = await _page(client, inst, bar, after)
        if not rows:
            break
        for c in rows:
            if start_ms <= c.ts < end_ms:
                out[c.ts] = c
        oldest = min(c.ts for c in rows)
        if oldest >= after:
            break
        after = oldest
        await asyncio.sleep(0.11)
    return sorted(out.values(), key=lambda c: c.ts)


async def load(inst: str, bar: str, days: float, *, client: httpx.AsyncClient | None = None, root: Path | None = None, now_ms: int | None = None, refresh: bool = True) -> list[Candle]:
    """最近 days 天的已收盘 K 线。缓存里有的不重取；refresh=False 时完全不联网（离线复跑）。"""
    inst = inst.upper()
    step = BAR_MINUTES[bar] * 60_000
    now = now_ms or int(time.time() * 1000)
    end = now // step * step  # 正在走的这一根不要
    start = end - int(days * 86_400_000) // step * step
    path = cache_path(inst, bar, root)
    have = {c.ts: c for c in read_cache(path)}
    if refresh:
        own = client is None
        client = client or httpx.AsyncClient(timeout=30)
        try:
            lo, hi = (min(have), max(have)) if have else (end, end - step)
            added = 0
            if start < lo:  # 往前补
                for c in await fetch_range(client, inst, bar, start, lo):
                    have[c.ts], added = c, added + 1
            if hi + step < end:  # 往后补
                for c in await fetch_range(client, inst, bar, hi + step, end):
                    have[c.ts], added = c, added + 1
            if added:
                write_cache(path, sorted(have.values(), key=lambda c: c.ts))
        finally:
            if own:
                await client.aclose()
    return [c for c in sorted(have.values(), key=lambda c: c.ts) if start <= c.ts < end]


def gaps(candles: list[Candle], bar: str) -> int:
    """缺了多少根（交易所停机或接口缺数）。回测报告里写出来，不悄悄当成连续的。"""
    step = BAR_MINUTES[bar] * 60_000
    return sum((b.ts - a.ts) // step - 1 for a, b in zip(candles, candles[1:]) if b.ts - a.ts > step)
