"""登录令牌的闸门：官方组件的令牌缓存过期时，不能让几个请求同时去取令牌（实测那样会把登录挤掉）。"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from ouy.okx.gateway import TokenGate, needs_token

TTL = 0.25


class FakeMcp:
    """照官方组件的做法：令牌缓存 TTL 秒，过期后每个到达的请求各自去取一次（取一次要 40 毫秒），没有去重。"""

    def __init__(self) -> None:
        self.fetched_at = float("-inf")
        self.fetching = self.worst = self.fetches = 0
        self.fail = False

    async def call(self) -> SimpleNamespace:
        if time.monotonic() - self.fetched_at >= TTL:
            self.fetching += 1
            self.fetches += 1
            self.worst = max(self.worst, self.fetching)
            await asyncio.sleep(0.04)
            self.fetching -= 1
            if self.fail:
                return SimpleNamespace(is_error=True)
            self.fetched_at = time.monotonic()
        await asyncio.sleep(0.005)
        return SimpleNamespace(is_error=False)


async def bursts(run, seconds: float = 1.3) -> None:
    """每 20 毫秒同时来 5 个账户请求，和盯盘时一样。"""
    tasks, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        tasks += [asyncio.create_task(run()) for _ in range(5)]
        await asyncio.sleep(0.02)
    await asyncio.gather(*tasks)


async def test_without_the_gate_several_requests_fetch_the_token_at_once():
    mcp = FakeMcp()
    await bursts(mcp.call)
    assert mcp.worst > 1  # 这就是掉登录的那种情况


async def test_gate_lets_only_one_request_fetch_the_token_and_keeps_the_rest_concurrent():
    mcp, gate = FakeMcp(), TokenGate(ttl=TTL, margin=0.03)
    done = 0

    async def run() -> None:
        nonlocal done
        await gate.run(mcp.call)
        done += 1

    await bursts(run)
    assert mcp.worst == 1 and mcp.fetches >= 3  # 每次过期都只有一个请求去取
    assert done > 150  # 其余时间照常并发，没有被拖成一个一个排队


async def test_failed_fetch_does_not_open_the_gate():
    mcp, gate = FakeMcp(), TokenGate(ttl=TTL, margin=0.03)
    mcp.fail = True
    await asyncio.gather(*(gate.run(mcp.call) for _ in range(6)))
    assert mcp.worst == 1 and mcp.fetches == 6  # 没取到令牌：后面的仍然一个一个走，不会一拥而上
    mcp.fail = False
    await asyncio.gather(*(gate.run(mcp.call) for _ in range(6)))
    assert mcp.worst == 1 and mcp.fetches == 7


def test_only_private_tools_need_the_token():
    assert needs_token("account_get_balance") and needs_token("swap_place_order")
    assert not needs_token("market_get_candles")


def test_login_error_is_distinct_from_unrelated_error_codes():
    from ouy.okx.gateway import OkxError

    assert OkxError("HTTP 401 Unauthorized").needs_login
    assert OkxError("No credentials found.", code="ConfigError").needs_login
    assert not OkxError("Exchange error 50401, reference 40123").needs_login


async def test_authorization_cli_and_gateway_share_the_refresh_lock(settings, monkeypatch):
    from ouy.okx.auth import OkxAuth

    lock = asyncio.Lock()
    gate = TokenGate(lock=lock)
    auth = OkxAuth(settings, auth_lock=lock)
    entered, release, cli_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def fetch():
        entered.set()
        await release.wait()
        return SimpleNamespace(is_error=False)

    async def spawn(*args, **kwargs):
        cli_started.set()

        async def communicate():
            return b"{}", b""

        return SimpleNamespace(communicate=communicate, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    tool = asyncio.create_task(gate.run(fetch))
    await entered.wait()
    status = asyncio.create_task(auth._run("status", binary="test-auth"))
    await asyncio.sleep(0)
    assert not cli_started.is_set()
    release.set()
    await asyncio.gather(tool, status)
    assert cli_started.is_set()


async def test_shared_gateway_does_not_repeat_the_same_account_query():
    """同样的账户查询：正在路上的共用一个结果，短时间内的用缓存；下过单之后缓存作废。"""
    from ouy.okx.gateway import Shared

    class Inner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call(self, tool, args=None, *, env):
            self.calls.append(tool)
            await asyncio.sleep(0.02)
            return [{"n": len(self.calls)}]

    inner = Inner()
    gw = Shared(inner)
    got = await asyncio.gather(*(gw.call("account_get_positions", {"instType": "SWAP"}, env="demo") for _ in range(6)))
    assert inner.calls == ["account_get_positions"] and all(g == got[0] for g in got)
    await gw.call("account_get_positions", {"instType": "SWAP"}, env="demo")  # 一秒内：用缓存
    await gw.call("account_get_config", {}, env="demo")
    assert len(inner.calls) == 2
    await gw.call("account_get_positions", {"instType": "SWAP"}, env="live")  # 另一个环境不共用
    await gw.call("swap_place_order", {"instId": "BTC-USDT-SWAP"}, env="demo")  # 下了单
    await gw.call("account_get_positions", {"instType": "SWAP"}, env="demo")  # 必须重新读
    await gw.call("account_get_config", {}, env="demo")  # 账户配置不受下单影响
    assert inner.calls == ["account_get_positions", "account_get_config", "account_get_positions", "swap_place_order", "account_get_positions"]
    await gw.call("market_get_ticker", {"instId": "BTC-USDT-SWAP"}, env="demo")
    await gw.call("market_get_ticker", {"instId": "BTC-USDT-SWAP"}, env="demo")
    assert inner.calls.count("market_get_ticker") == 2  # 行情不缓存
