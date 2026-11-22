"""历史 K 线缓存：不联网，用假的欧易接口。"""
from __future__ import annotations

import httpx

from ouy import history
from ouy.indicators import Candle

HOUR = 3_600_000


def fake_okx(calls: list[int], missing: set[int] = frozenset()):
    """每小时一根，价格 = 小时序号；missing 里的那几根交易所没有。"""
    def handler(request: httpx.Request) -> httpx.Response:
        after = int(request.url.params["after"])
        calls.append(after)
        top = (after - 1) // HOUR * HOUR
        rows = [[str(ts), "1", "2", "0.5", str(ts // HOUR), "10", "0", "0", "1"] for ts in range(top, top - 100 * HOUR, -HOUR) if ts > 0 and ts not in missing]
        return httpx.Response(200, json={"code": "0", "data": rows})
    return httpx.MockTransport(handler)


async def test_second_load_reads_cache_and_only_fetches_new_bars(tmp_path):
    calls: list[int] = []
    now = 1000 * HOUR + 123  # 第 1000 根正在走
    async with httpx.AsyncClient(transport=fake_okx(calls)) as client:
        rows = await history.load("btc-usdt-swap", "1H", 10, client=client, root=tmp_path, now_ms=now)
        assert len(rows) == 240 and rows[-1].ts == 999 * HOUR and rows[0].ts == 760 * HOUR
        assert all(b.ts - a.ts == HOUR for a, b in zip(rows, rows[1:])) and history.gaps(rows, "1H") == 0
        first = len(calls)
        again = await history.load("BTC-USDT-SWAP", "1H", 10, client=client, root=tmp_path, now_ms=now)
        assert again == rows and len(calls) == first  # 第二次完全不联网
        later = await history.load("BTC-USDT-SWAP", "1H", 10, client=client, root=tmp_path, now_ms=now + 3 * HOUR)
        assert later[-1].ts == 1002 * HOUR and len(calls) == first + 1  # 只补新出来的 3 根
        offline = await history.load("BTC-USDT-SWAP", "1H", 5, root=tmp_path, now_ms=now, refresh=False)
        assert len(offline) == 120 and offline[-1].ts == 999 * HOUR  # 离线复跑：固定截止时间，结果不随今天变


async def test_gaps_are_counted_not_hidden(tmp_path):
    missing = {900 * HOUR, 901 * HOUR}
    async with httpx.AsyncClient(transport=fake_okx([], missing)) as client:
        rows = await history.load("ETH-USDT-SWAP", "1H", 10, client=client, root=tmp_path, now_ms=1000 * HOUR)
    assert history.gaps(rows, "1H") == 2 and len(rows) == 238


def test_cache_round_trip_keeps_prices_exact(tmp_path):
    path = history.cache_path("X-USDT-SWAP", "5m", tmp_path)
    rows = [Candle(300_000, 0.000012345678, 67890.1, 0.5, 1.25, 3.0, True)]
    history.write_cache(path, rows)
    assert history.read_cache(path) == rows
