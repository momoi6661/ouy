"""欧易交易网关：把欧易官方 MCP 服务（okx-trade-mcp）作为常驻子进程，暴露其全部工具。

模拟盘、实盘各一个进程（分别带 --demo / --live），互不串线。凭据由官方组件用授权
得到的 OAuth 令牌处理，本进程不接触任何密钥。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import Settings
from .runtime import child_env, mcp_command

log = logging.getLogger("ouy.okx")

Env = Literal["demo", "live"]


class OkxError(RuntimeError):
    def __init__(self, message: str, *, tool: str = "", code: str = "", detail: Any = None):
        super().__init__(message)
        self.tool = tool
        self.code = code
        self.detail = detail

    @property
    def needs_login(self) -> bool:
        text = str(self).lower()
        return self.code in ("401", "AUTH_MISSING", "ConfigError", "AuthenticationError") or any(
            key in text for key in ("no credentials", "not logged in", "auth login")
        ) or re.search(r"\b(?:http(?: status)?|status(?: code)?)\s*[:=]?\s*401\b", text) is not None


@dataclass(frozen=True)
class ToolInfo:
    name: str
    module: str
    title: str
    description: str
    read_only: bool
    destructive: bool
    schema: dict[str, Any]

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "module": self.module,
            "title": self.title,
            "description": self.description,
            "readOnly": self.read_only,
            "destructive": self.destructive,
            "schema": self.schema,
        }


class Gateway(Protocol):
    async def call(self, tool: str, args: dict[str, Any] | None = None, *, env: Env) -> Any: ...
    async def tools(self) -> list[ToolInfo]: ...
    async def restart(self) -> None: ...
    async def close(self) -> None: ...


def rate_limited(exc: Exception) -> bool:
    text = str(exc).lower()
    return "too many requests" in text or "rate limit" in text or getattr(exc, "code", "") in ("50011", "429")


class Patient:
    """包在网关外面：欧易限流（Too Many Requests）时等一下再试，不直接把失败丢给上层。
    被限流的请求没有被执行，所以读和写都可以安全地重试。"""

    def __init__(self, inner: Any, delays: tuple[float, ...] = (0.5, 1.2, 2.5)):
        self.inner = inner
        self.delays = delays

    async def call(self, tool: str, args: dict[str, Any] | None = None, *, env: Env) -> Any:
        for delay in (*self.delays, None):
            try:
                return await self.inner.call(tool, args, env=env)
            except OkxError as exc:
                if delay is None or not rate_limited(exc):
                    raise
                await asyncio.sleep(delay)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class Shared:
    """包在欧易网关外面：同样的账户查询不重复发。

    读一次账户要同时发 5 个请求，而盯盘循环、每个 Worker 的复盘、策略执行器、页面轮询都在各读各的；
    欧易对这些接口限流（账户配置每秒 5 次），撞上了就是「读取账户失败」。这里：
    - 同一个查询正在路上时，后来的直接等它的结果，不再发一遍；
    - 账户配置几乎不变，缓存 10 分钟；历史持仓缓存 15 秒；余额、持仓、挂单缓存 1 秒；
    - 任何下单、撤单、改单之后，缓存立刻作废，下一次读到的一定是新的。"""

    TTL = {"account_get_config": 600.0, "account_get_positions_history": 15.0, "account_get_balance": 1.0,
           "account_get_positions": 1.0, "swap_get_orders": 1.0, "swap_get_algo_orders": 1.0}

    def __init__(self, inner: Any):
        self.inner = inner
        self._cache: dict[str, tuple[float, Any]] = {}
        self._flying: dict[str, asyncio.Future[Any]] = {}
        self._epoch = 0

    async def call(self, tool: str, args: dict[str, Any] | None = None, *, env: Env) -> Any:
        ttl = self.TTL.get(tool)
        if ttl is None:
            if "_get_" not in tool:  # 写操作：账户变了
                self._epoch += 1
                self._cache = {k: v for k, v in self._cache.items() if k.startswith("account_get_config|")}
            return await self.inner.call(tool, args, env=env)
        key = f"{tool}|{env}|{json.dumps(args or {}, sort_keys=True, default=str)}"
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        flying = self._flying.get(key)
        if flying is None:
            epoch = self._epoch
            flying = self._flying[key] = asyncio.ensure_future(self.inner.call(tool, args, env=env))

            def done(f: asyncio.Future[Any], key: str = key, epoch: int = epoch) -> None:
                self._flying.pop(key, None)
                if not f.cancelled() and f.exception() is None and epoch == self._epoch:
                    self._cache[key] = (time.monotonic(), f.result())

            flying.add_done_callback(done)
        return await asyncio.shield(flying)

    async def restart(self) -> None:
        self._cache.clear()
        await self.inner.restart()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class TokenGate:
    """不让几个请求同时去取登录令牌。

    欧易官方组件把访问令牌缓存 60 秒，缓存一过期，这时同时到达的每个请求都会各起一个 okx-auth 进程去取令牌；
    令牌大约每小时续期一次，赶上那一刻就是几个进程同时去续同一份登录。实测掉登录都发生在这种时刻：
    同一秒里的几个账户请求有的成功、有的报「没有凭据」，来回几次之后整个登录失效。
    这里的做法：缓存确定有效的时候照常并发；快到期时让需要登录的请求先等几秒，等缓存确定过期后只放一个过去取令牌，取回来再放行其余的。"""

    def __init__(self, ttl: float = 60.0, margin: float = 0.75, lock: asyncio.Lock | None = None):
        self.ttl, self.margin = ttl, margin
        self._lock = lock or asyncio.Lock()
        self._fresh_from = float("-inf")  # 当前这份缓存最早是什么时候取的（那个请求发出的时间）
        self._stale_by = float("-inf")  # 到这个时间它一定已经过期

    def _fresh(self) -> bool:
        return time.monotonic() < self._fresh_from + self.ttl - self.margin

    async def run(self, call: Callable[[], Awaitable[Any]]) -> Any:
        if self._fresh():
            return await call()
        async with self._lock:
            if self._fresh():  # 排队的时候前面那个已经把令牌取回来了
                return await call()
            wait = self._stale_by - time.monotonic()
            if wait > 0:  # 等旧缓存确定过期，这样下面这一个请求一定是它去取令牌，而且只有它一个
                await asyncio.sleep(wait)
            sent = time.monotonic()
            result = await call()
            if not getattr(result, "is_error", False):  # 成功了说明令牌取到了；失败的不算，下一个请求仍然单独走
                # 多等一个 margin 再认定过期：两边的时钟有十几毫秒的误差，早到一步的话这一个请求就不是去取令牌的了
                self._fresh_from, self._stale_by = sent, time.monotonic() + self.ttl + self.margin
            return result

    def reset(self) -> None:
        self._fresh_from = self._stale_by = float("-inf")


def needs_token(tool: str) -> bool:
    """公开行情不用登录；其余（账户、下单、持仓……）都要带登录令牌。"""
    return not tool.startswith("market_")


_MODULE_PREFIXES = {
    "smartmoney": "smartmoney",
    "smart": "bot",
    "grid": "bot",
    "dca": "bot",
    "earn": "earn",
    "onchain": "earn",
    "dcd": "earn",
}


def _module_of(name: str) -> str:
    head = name.split("_", 1)[0]
    return _MODULE_PREFIXES.get(head, head)


def _unwrap(tool: str, result: Any) -> Any:
    """官方 MCP 的返回形如 {tool, ok, data:{endpoint, requestTime, data:[...]}}，取出最里层数据。"""
    payload: Any = getattr(result, "structured_content", None)
    if payload is None:
        texts = [getattr(c, "text", "") for c in (getattr(result, "content", None) or [])]
        raw = "\n".join(t for t in texts if t)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"message": raw} if getattr(result, "is_error", False) else raw
    if getattr(result, "is_error", False) or (isinstance(payload, dict) and payload.get("error")):
        if isinstance(payload, dict):
            message = payload.get("message") or "欧易接口调用失败"
            suggestion = payload.get("suggestion")
            code = str(payload.get("code") or payload.get("type") or "")
            if suggestion:
                message = f"{message}（{suggestion}）"
            raise OkxError(message, tool=tool, code=code, detail=payload)
        raise OkxError(str(payload), tool=tool)
    if isinstance(payload, dict) and "data" in payload and ("ok" in payload or "tool" in payload):
        payload = payload["data"]
    if isinstance(payload, dict) and "data" in payload and "endpoint" in payload:
        payload = payload["data"]
    return payload


class _Session(ClientSession):
    """不按工具声明的输出格式校验返回值。

    欧易有些工具（比如聪明钱）出错时返回的内容不符合它自己声明的格式，MCP 客户端会直接抛
    「Invalid structured content」，把真正的错误信息吞掉，还会被当成进程故障触发重启。
    这里跳过校验，交给 _unwrap 读出欧易返回的真实内容。
    """

    async def validate_tool_result(self, name: str, result: Any) -> None:  # noqa: ARG002
        return None

    async def _validate_tool_result(self, name: str, result: Any) -> None:  # noqa: ARG002 - 旧版本 mcp 的方法名
        return None


class _Server:
    """一个 okx-trade-mcp 子进程 + MCP 会话。上下文的进入和退出必须在同一个任务里。"""

    def __init__(self, settings: Settings, env: Env):
        self.settings = settings
        self.env = env
        self.session: ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._ready: asyncio.Future[None] | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._ready = loop.create_future()
        self._task = asyncio.create_task(self._run(), name=f"okx-mcp-{self.env}")
        await self._ready

    async def _run(self) -> None:
        assert self._ready is not None
        try:
            command, args = mcp_command()
            params = StdioServerParameters(
                command=command,
                # 官方组件默认把每一次调用（含返回的全部行情）写进 ~/.okx/logs，一天能写 1 GB：只留失败的那几条，排查掉登录、报错时用得上
                args=[*args, "--modules", "all", f"--{self.env}", "--site", self.settings.okx_site, "--log-level", "error"],
                env=child_env(self.settings),
            )
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(_Session(read, write))
                await session.initialize()
                self.session = session
                if not self._ready.done():
                    self._ready.set_result(None)
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - 需要把启动失败传回调用方
            if not self._ready.done():
                self._ready.set_exception(exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            elif not isinstance(exc, asyncio.CancelledError):
                log.warning("okx-trade-mcp(%s) 已退出: %s", self.env, exc)
        finally:
            self.session = None

    @property
    def alive(self) -> bool:
        return self.session is not None and self._task is not None and not self._task.done()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, 10)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                self._task.cancel()
        self._task = None
        self.session = None


class OkxGateway:
    def __init__(self, settings: Settings, *, timeout: float = 30, auth_lock: asyncio.Lock | None = None):
        self.settings = settings
        self.timeout = timeout
        self._servers: dict[str, _Server] = {}
        self._locks: dict[str, asyncio.Lock] = {"demo": asyncio.Lock(), "live": asyncio.Lock()}
        self._tools: list[ToolInfo] | None = None
        fetch = auth_lock or asyncio.Lock()
        self._gates: dict[str, TokenGate] = {"demo": TokenGate(lock=fetch), "live": TokenGate(lock=fetch)}

    async def _server(self, env: Env) -> _Server:
        async with self._locks[env]:
            server = self._servers.get(env)
            if server is None or not server.alive:
                if server is not None:
                    await server.stop()
                server = _Server(self.settings, env)
                await server.start()
                self._servers[env] = server
                self._gates[env].reset()  # 新进程没有令牌缓存
            return server

    async def call(self, tool: str, args: dict[str, Any] | None = None, *, env: Env) -> Any:
        clean = {k: v for k, v in (args or {}).items() if v is not None and v != ""}
        last: Exception | None = None
        for attempt in range(2):
            server = await self._server(env)
            assert server.session is not None
            try:
                session = server.session
                if needs_token(tool):
                    result = await self._gates[env].run(lambda: session.call_tool(tool, clean, read_timeout_seconds=self.timeout))
                else:
                    result = await session.call_tool(tool, clean, read_timeout_seconds=self.timeout)
            except OkxError:
                raise
            except Exception as exc:  # noqa: BLE001 - 进程挂了/超时：重启一次再试（仅对只读安全）
                last = exc
                await server.stop()
                if attempt == 0 and await self._is_read_only(tool):
                    continue
                raise OkxError(f"欧易网关调用 {tool} 失败：{exc}", tool=tool) from exc
            return _unwrap(tool, result)
        raise OkxError(f"欧易网关调用 {tool} 失败：{last}", tool=tool)

    async def _is_read_only(self, tool: str) -> bool:
        try:
            return any(t.name == tool and t.read_only for t in await self.tools())
        except Exception:  # noqa: BLE001
            return False

    async def tools(self) -> list[ToolInfo]:
        if self._tools is None:
            server = await self._server("demo")
            assert server.session is not None
            listed = await server.session.list_tools()
            tools: list[ToolInfo] = []
            for t in listed.tools:
                ann = t.annotations
                tools.append(
                    ToolInfo(
                        name=t.name,
                        module=_module_of(t.name),
                        title=(getattr(t, "title", None) or (ann.title if ann else None) or t.name),
                        description=t.description or "",
                        read_only=bool(ann and ann.read_only_hint),
                        destructive=bool(ann and ann.destructive_hint),
                        schema=dict(t.input_schema or {}),
                    )
                )
            self._tools = tools
        return self._tools

    async def restart(self) -> None:
        """授权状态变化后调用，让官方组件重新读取令牌。"""
        for env in list(self._servers):
            async with self._locks[env]:
                await self._servers.pop(env).stop()

    async def close(self) -> None:
        await self.restart()
