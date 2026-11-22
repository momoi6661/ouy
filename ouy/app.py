"""FastAPI 应用：REST + WebSocket，并托管前端。"""
from __future__ import annotations

import asyncio
import httpx
import hmac
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .agent.core import build_model
from .agent.watcher import Watcher
from .strategy import KINDS as STRATEGY_KINDS
from .config import ROOT, Settings, get_settings
from .events import EventBus
from .knowledge import KnowledgeBase
from .market import Market, _f
from .models import AgentConfig, Effort, TradeAction
from .profit_guard import ExitRules
from .okx.auth import OkxAuth, OkxAuthError
from .okx.gateway import Gateway, OkxError, OkxGateway, Patient, Shared
from .okx.runtime import OkxRuntimeMissing
from .risk import Rejected
from .skills import SkillError, Skills
from .skills import fetch as fetch_skill
from .store import Store, now_ms
from .trader import Trader

log = logging.getLogger("ouy")
COOKIE = "ouy_key"
BARS = {"1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "6H", "12H", "1D", "1W"}


class Runtime:
    """把所有组件装在一起。测试时可以换掉 gateway / auth / 模型。"""

    def __init__(self, settings: Settings, *, gateway: Gateway | None = None, auth: Any = None, model_factory: Any = None, store: Store | None = None):
        self.settings = settings
        self.store = store or Store(settings.data_dir / "ouy.db")
        self.bus = EventBus()
        self.gateway: Gateway = Patient(gateway or Shared(OkxGateway(settings)))  # type: ignore[assignment]
        self.auth = auth or OkxAuth(settings)
        self.market = Market(self.gateway)
        self.kb = KnowledgeBase(settings)
        self.trader = Trader(self.gateway, self.market, self.store, self.bus, self.config)
        self.watcher = Watcher(
            gateway=self.gateway, market=self.market, trader=self.trader, store=self.store, bus=self.bus, kb=self.kb,
            get_config=self.config, model_factory=model_factory or (lambda name=None: build_model(settings, name)),
        )
        self.watcher.control = self
        self.skills = Skills(settings.skills_dir, self.store)
        self.watcher.skills = self.skills
        self.watcher.proxy_url = lambda: self.settings.proxy_url or None
        self.watcher.uploads = self.settings.data_dir / "uploads"  # 对话里附的图片存在这里
        self.store.fail_running_work("程序重启，上次没跑完")
        self._env_llm = {"base_url": settings.ai_base_url, "api_key": settings.ai_api_key, "model": settings.ai_model}
        self._env_proxy = settings.proxy_url
        self.apply_llm()
        self.apply_proxy()
        self._config: AgentConfig | None = None
        self._auth_status = ""
        self._swap_meta: tuple[float, dict[str, Any]] = (0.0, {})

    # ----- 模型接口：界面上填的 Base URL / API Key / 默认模型，存在本机数据库里，优先于 .env -----
    def apply_llm(self) -> None:
        saved = self.store.get("llm") or {}
        self.settings.ai_base_url = saved.get("base_url") or self._env_llm["base_url"]
        self.settings.ai_api_key = saved.get("api_key") or self._env_llm["api_key"]
        self.settings.ai_model = saved.get("model") or self._env_llm["model"]
        self.watcher.reset_model()

    def llm(self) -> dict[str, Any]:
        key = self.settings.ai_api_key
        saved = self.store.get("llm") or {}
        return {
            "base_url": self.settings.ai_base_url, "model": self.settings.ai_model, "key_set": bool(key),
            "key_hint": f"…{key[-4:]}" if len(key) >= 8 else "",  # 密钥本身不回传给页面
            "source": {k: "page" if saved.get(k) else "env" for k in ("base_url", "api_key", "model")},
        }

    def save_llm(self, base_url: str | None, api_key: str | None, model: str | None) -> dict[str, Any]:
        saved = dict(self.store.get("llm") or {})
        for name, value in (("base_url", base_url), ("api_key", api_key), ("model", model)):
            if value is None:  # 没传 = 不改（密钥框留空就是保留原来的）
                continue
            if value.strip():
                saved[name] = value.strip().rstrip("/") if name == "base_url" else value.strip()
            else:
                saved.pop(name, None)  # 清空 = 改回 .env 里的
        self.store.set("llm", saved)
        self.apply_llm()
        return self.llm()

    # ---------- 网络代理：连欧易和连模型接口共用；页面上填的优先于 .env，留空 = 不用代理、直接连 ----------

    def apply_proxy(self) -> None:
        saved = self.store.get("proxy")
        self.settings.proxy_url = self._env_proxy if saved is None else str(saved.get("url") or "")
        self.watcher.reset_model()  # 模型的连接下次用到时按新代理重建

    def proxy(self) -> dict[str, Any]:
        return {"url": self.settings.proxy_url, "source": "env" if self.store.get("proxy") is None else "page"}

    async def save_proxy(self, url: str) -> dict[str, Any]:
        self.store.set("proxy", {"url": url.strip()})
        self.apply_proxy()
        await self.gateway.restart()  # 欧易的后台进程要带着新代理重新起
        return self.proxy()

    def config(self) -> AgentConfig:
        if self._config is None:
            self._config = AgentConfig.model_validate(self.store.get("config") or {})
        return self._config

    def save_config(self, cfg: AgentConfig) -> None:
        self._config = cfg
        self.store.set("config", cfg.model_dump())

    async def apply_config(self, new: AgentConfig) -> None:
        """保存新设置并让盯盘循环跟上：换环境要先停，被移出盯盘列表的品种停止复盘。"""
        old = self.config()
        if new.env != old.env:
            await self.watcher.stop("切换交易环境")
            self.store.set("agent_running", False)
            self.watcher.states.clear()
        self.save_config(new)
        for inst in list(self.watcher.states):
            if inst not in new.instruments:
                st = self.watcher.states.pop(inst)
                if st.observe_task:
                    st.observe_task.cancel()
                if st.task and not st.task.done():
                    st.task.cancel()
        self.bus.publish("agent", self.watcher.status())

    async def set_watching(self, on: bool) -> None:
        if on:
            await self.watcher.start()
        else:
            await self.watcher.stop()
        self.store.set("agent_running", on)

    async def auth_status(self, fresh: bool = True) -> dict[str, Any]:
        """欧易登录状态。查一次要起官方命令行（一两秒）：页面每次刷新都会问，所以 fresh=False 时 15 秒内直接用上次的结果。"""
        cached = getattr(self, "_auth_cache", None)
        # 已经登录时不用查得那么勤：每查一次都要起一个官方授权进程，和取令牌的进程挤在一起
        if not fresh and cached and time.time() - cached[0] < (60 if cached[1].get("status") == "logged_in" else 15):
            return cached[1]
        status = await self.auth.status()
        self._auth_cache = (time.time(), status)
        if self._auth_status and status["status"] != self._auth_status:
            await self.gateway.restart()  # 让官方组件重新读取令牌
        self._auth_status = status["status"]
        return status

    async def swap_meta(self, env: str) -> dict[str, Any]:
        """全部 U 本位永续的规格（最高杠杆、面值），缓存 1 小时。"""
        ts, data = self._swap_meta
        if time.time() - ts > 3600 or not data:
            rows = await self.gateway.call("market_get_instruments", {"instType": "SWAP", "demo": env == "demo"}, env=env)  # type: ignore[arg-type]
            data = {r["instId"]: {"lever": _f(r.get("lever")), "ctVal": _f(r.get("ctVal")), "settleCcy": r.get("settleCcy")} for r in rows or []}
            self._swap_meta = (time.time(), data)
        return data

    async def shutdown(self) -> None:
        await self.watcher.stop("服务关闭")
        await self.gateway.close()
        self.store.close()


class ConfigUpdate(BaseModel):
    config: AgentConfig
    confirm_live: bool = False


class OkxCall(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    confirm: bool = False
    env: Literal["demo", "live"] | None = None


class CondIn(BaseModel):
    data: str = Field(..., max_length=60, description="看哪项数据：vol_ratio / chg_pct / score_long / score_short / trend / range_pos / spread_pct / book_bias / dist:线名")
    op: Literal["above", "below"]
    level: float


class RecorderIn(BaseModel):
    inst_id: str = Field(..., max_length=40)
    conds: list[CondIn] = Field(..., min_length=1, max_length=4)
    note: str = Field("", max_length=200)
    cooldown_minutes: int = Field(15, ge=1, le=1440)


class RecorderEdit(BaseModel):
    conds: list[CondIn] | None = Field(None, min_length=1, max_length=4)
    level: float | None = None
    cooldown_minutes: int | None = Field(None, ge=1, le=1440)
    off: bool | None = None


class LogClearIn(BaseModel):
    keep_days: float = Field(0, ge=0, le=90, description="保留最近几天的；0 = 全部清掉")


def _okx_log_files() -> list[Any]:
    from pathlib import Path

    folder = Path.home() / ".okx" / "logs"
    return sorted(folder.glob("trade-*.log")) if folder.is_dir() else []


class ClearIn(BaseModel):
    inst_id: str = Field("", max_length=40, description="哪个币；空 = 所有币（Router 那一栏）")
    what: Literal["tasks", "triggers", "talk"]


UPLOAD_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
UPLOAD_MAX = 6 * 1024 * 1024


def save_upload(folder: Any, data_url: str) -> str:
    """把一张 data URL 图片存到 data/uploads/，返回文件名（内容哈希 + 扩展名）。只收常见的图片格式，单张不超过 6MB。"""
    import base64
    import hashlib

    head, _, body = data_url.partition(",")
    kind = head.removeprefix("data:").split(";")[0].lower()
    if not head.startswith("data:") or ";base64" not in head or kind not in UPLOAD_TYPES:
        raise ValueError("只能附 PNG / JPG / WebP / GIF 图片。")
    try:
        raw = base64.b64decode(body, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("图片内容读不出来，请重新粘贴或选择。") from exc
    if not raw or len(raw) > UPLOAD_MAX:
        raise ValueError("图片太大了，单张不能超过 6MB。")
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{hashlib.sha256(raw).hexdigest()[:24]}.{UPLOAD_TYPES[kind]}"
    (folder / name).write_bytes(raw)
    return name


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    effort: Effort | None = None
    thread: str = Field("general", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    inst_id: str | None = Field(None, max_length=40)
    to: Literal["router", "worker"] = Field("router", description="对 Router 说，还是直接对这个币的 Worker 说（直通路由）")
    queue: bool = Field(False, description="Worker 正在思考时：默认把话直接送进它当前这一轮；True 表示排到这一轮后面单独问")
    images: list[str] = Field(default_factory=list, max_length=4, description="随这句话附上的图片（截图、K 线图），data URL，最多 4 张")


class LlmIn(BaseModel):
    base_url: str | None = Field(None, max_length=300)
    api_key: str | None = Field(None, max_length=400)
    model: str | None = Field(None, max_length=100)


class LessonIn(BaseModel):
    id: int | None = None
    scope: Literal["env", "shared"] = "env"
    inst_id: str = Field("", max_length=40)
    text: str = Field(min_length=6, max_length=300)
    grade: Literal["observe", "rule"] | None = None


class SkillIn(BaseModel):
    name: str = Field("", max_length=60)
    content: str = Field("", max_length=20_000)
    url: str = Field("", max_length=500)


class PlaybookIn(BaseModel):
    name: str = Field(min_length=1, max_length=24)
    base: Literal["breakout", "pullback", "range", "manage", "martingale", "grid"]
    when: str = Field(min_length=1, max_length=300)
    entry: str = Field("", max_length=400)
    exit: str = Field(min_length=1, max_length=400)
    defaults: dict[str, float] = Field(default_factory=dict)
    enabled: bool = True


class ProxyIn(BaseModel):
    url: str = Field("", max_length=300)


class ReviewIn(BaseModel):
    inst_id: str
    role: Literal["intel", "smart", "risk"] = "intel"  # 只有「让子代理去查」用得到
    question: str = Field("", max_length=500)


class GoalIn(BaseModel):
    inst_id: str
    goal: str = Field(min_length=1, max_length=500)
    max_loss_usdt: float | None = Field(None, gt=0)
    hours: float | None = Field(None, gt=0, le=168)
    target_usdt: float | None = Field(None, gt=0)
    stop_when: str = Field("", max_length=200)


class StandbyIn(BaseModel):
    inst_id: str
    on: bool


class InterjectIn(BaseModel):
    inst_id: str
    message: str = Field(min_length=1, max_length=1000)


class UnlockIn(BaseModel):
    key: str


def create_app(runtime: Runtime | None = None) -> FastAPI:
    settings = runtime.settings if runtime else get_settings()
    if not settings.is_loopback and not settings.access_key:
        raise RuntimeError("OUY_HOST 不是本机地址时必须设置 OUY_ACCESS_KEY，否则局域网里任何人都能操作你的欧易账户。")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = runtime or Runtime(settings)
        app.state.rt = rt
        warm = asyncio.create_task(rt.kb.warmup())
        if rt.store.get("agent_running"):
            try:
                if (await rt.auth_status())["status"] == "logged_in":
                    await rt.watcher.start()
            except Exception as exc:  # noqa: BLE001
                log.warning("自动恢复盯盘失败：%s", exc)
        yield
        warm.cancel()
        await rt.shutdown()

    app = FastAPI(title="Ouy", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)

    def rt_of(request: Request) -> Runtime:
        return request.app.state.rt

    def kb_view(rt: Runtime) -> dict[str, Any]:
        """知识库目录；策略文档带上程序给它记的战绩（当前环境）。"""
        view, env = rt.kb.stats(), rt.config().env
        for doc in view["documents"]:
            if doc.get("category") == "strategy":
                doc["score"] = rt.watcher.scores.of(env, str(doc["title"]).removeprefix("策略："))
        return view

    # ---------- 安全：同源 + Host 校验 + 可选访问口令 ----------

    def _authorized(cookies: dict[str, str], headers: Any) -> bool:
        if not settings.access_key:
            return True
        supplied = cookies.get(COOKIE) or headers.get("x-ouy-key") or ""
        return hmac.compare_digest(supplied.encode(), settings.access_key.encode())

    def _same_origin(headers: Any) -> bool:
        origin = headers.get("origin")
        if not origin:
            return True
        return urlparse(origin).netloc == headers.get("host")

    def _host_ok(headers: Any) -> bool:
        if not settings.is_loopback:
            return True
        host = (headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1", "testserver")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if not _host_ok(request.headers):
            return JSONResponse({"detail": "非法的 Host"}, status_code=403)
        if path.startswith("/api/"):
            if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request.headers):
                return JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403)
            if path not in ("/api/unlock", "/api/health") and not _authorized(request.cookies, request.headers):
                return JSONResponse({"detail": "需要访问口令", "locked": True}, status_code=401)
        return await call_next(request)

    @app.get("/api/health")
    async def health():
        """给 Docker 健康检查用：进程活着就返回，不涉及账户信息。"""
        return {"ok": True}

    @app.exception_handler(OkxError)
    async def _okx_error(request: Request, exc: OkxError):
        if exc.needs_login:  # 登录掉了：下一次问状态时重新查，别再用缓存里的「已登录」
            request.app.state.rt._auth_cache = None
        return JSONResponse({"detail": str(exc), "needs_login": exc.needs_login}, status_code=401 if exc.needs_login else 502)

    @app.exception_handler(Rejected)
    async def _rejected(_: Request, exc: Rejected):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(OkxAuthError)
    async def _auth_error(_: Request, exc: OkxAuthError):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(OkxRuntimeMissing)
    async def _runtime_missing(_: Request, exc: OkxRuntimeMissing):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.post("/api/unlock")
    async def unlock(body: UnlockIn):
        if not settings.access_key or not hmac.compare_digest(body.key.encode(), settings.access_key.encode()):
            await asyncio.sleep(1)
            raise HTTPException(401, "访问口令不对")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, body.key, httponly=True, samesite="strict", max_age=30 * 86400)
        return resp

    # ---------- 总状态 ----------

    @app.get("/api/state")
    async def state(request: Request):
        rt = rt_of(request)
        try:
            auth = await rt.auth_status(fresh=False)  # 页面每次操作后都会刷新状态，不能每次都等一两秒
        except (OkxAuthError, OkxRuntimeMissing) as exc:
            auth = {"status": "error", "error": str(exc)}
        return {
            "version": __version__,
            "auth": auth,
            "site": settings.okx_site,
            "config": rt.config().model_dump(),
            "agent": rt.watcher.status(),
            "ai": {"model": settings.ai_model, "configured": bool(settings.ai_api_key)},
            "kb": kb_view(rt),
        }

    # ---------- 欧易授权 ----------

    @app.post("/api/auth/login")
    async def login(request: Request):
        rt = rt_of(request)
        result = await rt.auth.login()
        rt._auth_status = result.get("status", "pending")
        return result

    @app.get("/api/auth/status")
    async def auth_status(request: Request):
        return await rt_of(request).auth_status()

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        rt = rt_of(request)
        await rt.watcher.stop("已退出欧易授权")
        rt.store.set("agent_running", False)
        await rt.auth.logout()
        await rt.gateway.restart()
        rt._auth_status = "not_logged_in"
        return {"ok": True}

    # ---------- 行情 ----------

    @app.get("/api/market/tickers")
    async def tickers(request: Request, inst_type: str = "SWAP"):
        rt = rt_of(request)
        env = rt.config().env
        if inst_type not in ("SWAP", "SPOT"):
            raise HTTPException(400, "inst_type 只支持 SWAP / SPOT")
        rows = await rt.market.tickers(inst_type, env)
        meta = await rt.swap_meta(env) if inst_type == "SWAP" else {}
        out = []
        for r in rows:
            inst = r.get("instId", "")
            if inst_type == "SWAP" and not inst.endswith("-USDT-SWAP"):
                continue
            if inst_type == "SPOT" and not inst.endswith("-USDT"):
                continue
            last, open24 = _f(r.get("last")), _f(r.get("open24h"))
            ct_val = meta.get(inst, {}).get("ctVal") or 1
            turnover = _f(r.get("volCcy24h")) * last if inst_type == "SWAP" else _f(r.get("volCcy24h"))
            out.append({
                "inst_id": inst,
                "last": last,
                "change_pct": round((last / open24 - 1) * 100, 2) if open24 else 0,
                "high_24h": _f(r.get("high24h")),
                "low_24h": _f(r.get("low24h")),
                "turnover_usd": round(turnover),
                "max_leverage": meta.get(inst, {}).get("lever"),
                "ct_val": ct_val if inst_type == "SWAP" else None,
            })
        out.sort(key=lambda x: -x["turnover_usd"])
        return out

    @app.get("/api/market/levels")
    async def levels(request: Request, inst_id: str, bar: str = "15m"):
        """图上要画的关键位：和 Worker 复盘时看到的是同一套算法——这个周期的区间上下沿、支撑阻力，以及上一小时 / 上 4 小时的高低点。"""
        from .agent.signals import structure_shift
        from .indicators import day_profile, read_chart, value_area, window_levels
        from .market import session_anchor

        rt = rt_of(request)
        env = rt.config().env
        inst = inst_id.upper()
        frame, m15, h1 = await asyncio.gather(rt.market.candles(inst, bar, 300, env), rt.market.candles(inst, "15m", 300, env), rt.market.candles(inst, "1H", 300, env))
        chart = read_chart(frame)
        last = m15[-1].close if m15 else None
        return {
            # 上一日的价值区间（POC / VAH / VAL）和开盘以来的走法：和 Worker、量化监测看到的是同一套算法
            "value_area": value_area({"15m": m15, "1H": h1}, last),
            "session": day_profile({"15m": m15, "1H": h1}, last, *session_anchor(await rt.market.asset_class(inst, env))),
            "range": chart.get("range"), "support": chart.get("support", []), "resistance": chart.get("resistance", []),
            "patterns": chart.get("patterns", []), "windows": window_levels({"15m": m15, "1H": h1}),
            "shift": structure_shift(frame[-120:]),
        }

    @app.get("/api/market/candles")
    async def candles(request: Request, inst_id: str, bar: str = "15m", limit: int = 300, src: Literal["env", "live"] = "env"):
        """src=live：不管现在是不是模拟盘，都取真实市场的 K 线（只读，公开行情）。模拟盘有自己独立的盘口和成交，和真实市场对不上。"""
        rt = rt_of(request)
        if bar not in BARS:
            raise HTTPException(400, "不支持的 K 线周期")
        rows = await rt.market.candles(inst_id.upper(), bar, max(min(limit, 300), 2), "live" if src == "live" else rt.config().env)
        return [{"time": c.ts // 1000, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume} for c in rows]

    @app.get("/api/market/ticker")
    async def ticker(request: Request, inst_id: str = "BTC-USDT-SWAP"):
        rt = rt_of(request)
        row = await rt.market.ticker(inst_id.upper(), rt.config().env)
        last, open24 = _f(row.get("last")), _f(row.get("open24h"))
        return {"inst_id": inst_id.upper(), "last": last, "change_24h_pct": round((last / open24 - 1) * 100, 2) if open24 else None}

    @app.get("/api/market/orderbook")
    async def orderbook(request: Request, inst_id: str, depth: int = 10):
        rt = rt_of(request)
        return await rt.market.book(inst_id.upper(), rt.config().env, max(min(depth, 50), 1))

    @app.get("/api/market/snapshot")
    async def snapshot(request: Request, inst_id: str):
        rt = rt_of(request)
        return await rt.market.snapshot(inst_id.upper(), rt.config().env)

    @app.get("/api/market/instrument")
    async def instrument(request: Request, inst_id: str):
        rt = rt_of(request)
        cfg = rt.config()
        inst = await rt.market.instrument(inst_id.upper(), cfg.env)
        out = inst.model_dump()
        try:
            out["leverage"] = await rt.trader._leverage(inst.inst_id, cfg.margin_mode, cfg.env) if inst.inst_type == "SWAP" else None
        except OkxError:
            out["leverage"] = None
        return out

    # ---------- 账户 ----------

    @app.get("/api/account")
    async def account(request: Request):
        return (await rt_of(request).trader.account()).model_dump()

    @app.get("/api/lots")
    async def lots(request: Request, inst_id: str | None = None):
        """每一笔仓位（欧易里合并成一个持仓，这里按笔分开，带各自的风格、入场价、止损止盈）。"""
        rt = rt_of(request)
        env = rt.config().env
        return rt.trader.lots(env, inst_id.upper() if inst_id else None, await rt.trader.account(env))

    @app.get("/api/account/equity")
    async def equity(request: Request, hours: int = 24):
        rt = rt_of(request)
        return rt.store.equity_curve(rt.config().env, now_ms() - max(min(hours, 24 * 90), 1) * 3600_000)

    @app.get("/api/account/funding")
    async def funding(request: Request):
        """资金账户里每个币种的余额（充值、提现、划转用的那个账户）。交易账户的钱在 /api/account 里。查不到就返回空和原因，不影响别的。"""
        rt = rt_of(request)
        try:
            rows = await rt.gateway.call("account_get_asset_balance", {}, env=rt.config().env)
        except Exception as exc:  # noqa: BLE001 - 模拟盘没有资金账户、或登录过期：照实告诉界面
            return {"rows": [], "error": str(exc)[:200]}
        out = [{"ccy": r.get("ccy"), "balance": _f(r.get("bal")), "available": _f(r.get("availBal")), "frozen": _f(r.get("frozenBal"))} for r in rows or []]
        return {"rows": [r for r in out if r["balance"] > 0], "error": ""}

    @app.get("/api/account/history")
    async def history(request: Request, inst_id: str | None = None, limit: int = 30):
        return [t.model_dump() for t in await rt_of(request).trader.history(inst_id.upper() if inst_id else None, limit)]

    @app.get("/api/account/fills")
    async def fills(request: Request, inst_id: str | None = None):
        rt = rt_of(request)
        rows = await rt.gateway.call("swap_get_fills", {"instId": inst_id.upper() if inst_id else None, "limit": 50}, env=rt.config().env)
        return [
            {"ts": int(_f(r.get("ts"))), "inst_id": r.get("instId"), "side": r.get("side"), "pos_side": r.get("posSide"),
             "px": _f(r.get("fillPx")), "sz": _f(r.get("fillSz")), "pnl": _f(r.get("fillPnl")), "fee": _f(r.get("fee"))}
            for r in rows or []
        ]

    # ---------- 交易 ----------

    @app.post("/api/trade")
    async def trade(request: Request, action: TradeAction):
        rt = rt_of(request)
        result = await rt.trader.submit(action, source="user")
        out = result.model_dump()
        if action.kind in ("open", "add") and result.status == "executed":
            out["worker_note"] = rt.watcher.manual_order(action, result.message)
        return out

    @app.post("/api/trade/panic")
    async def panic(request: Request):
        rt = rt_of(request)
        await rt.watcher.stop("一键全平")
        rt.store.set("agent_running", False)
        results = await rt.trader.panic(rt.config().env)
        return {"results": [r.model_dump() for r in results]}

    # ---------- Agent ----------

    @app.get("/api/agent/status")
    async def agent_status(request: Request):
        return rt_of(request).watcher.status()

    @app.put("/api/agent/config")
    async def update_config(request: Request, body: ConfigUpdate):
        rt = rt_of(request)
        old = rt.config()
        new = body.config
        if new.env == "live" and old.env != "live" and not body.confirm_live:
            raise HTTPException(400, "切换到实盘需要明确确认。")
        await rt.apply_config(new)
        return {"config": new.model_dump(), "agent": rt.watcher.status()}

    @app.get("/api/llm")
    async def llm_get(request: Request):
        return rt_of(request).llm()

    @app.put("/api/llm")
    async def llm_put(request: Request, body: LlmIn):
        if body.base_url and not body.base_url.strip().lower().startswith(("http://", "https://")):
            raise HTTPException(400, "接口地址要以 http:// 或 https:// 开头，比如 https://api.openai.com/v1")
        rt = rt_of(request)
        out = rt.save_llm(body.base_url, body.api_key, body.model)
        rt.bus.publish("agent", rt.watcher.status())
        return out

    @app.get("/api/llm/models")
    async def llm_models(request: Request):
        """模型接口上能用的模型名字（OpenAI 兼容接口的 /models），给界面上的下拉框用。拿不到就返回空列表，界面上仍然可以手填。"""
        rt = rt_of(request)
        s = rt.settings
        used = [m for m in dict.fromkeys([s.ai_model, rt.config().worker_model, rt.config().router_model, *(w.model for w in rt.config().workers.values())]) if m]
        cached = getattr(rt, "_models_cache", None)
        if cached and cached[0] == (s.ai_base_url, s.ai_api_key) and time.time() - cached[1] < 600:
            return {"models": sorted(dict.fromkeys([*used, *cached[2]])), "default": s.ai_model}
        found: list[str] = []
        try:
            async with httpx.AsyncClient(proxy=s.proxy_url or None, timeout=12) as client:
                resp = await client.get(f"{(s.ai_base_url or 'https://api.openai.com/v1').rstrip('/')}/models", headers={"Authorization": f"Bearer {s.ai_api_key}"})
            if resp.status_code == 200:
                found = [str(m.get("id")) for m in (resp.json().get("data") or []) if isinstance(m, dict) and m.get("id")]
                rt._models_cache = ((s.ai_base_url, s.ai_api_key), time.time(), found)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 接口不支持列模型：界面上手填
            found = []
        return {"models": sorted(dict.fromkeys([*used, *found])), "default": s.ai_model}

    @app.post("/api/llm/test")
    async def llm_test(request: Request):
        """用当前的接口地址、密钥和默认模型真的发一次最小的请求，告诉用户通不通。"""
        rt = rt_of(request)
        try:
            from pydantic_ai import Agent as _Agent
            result = await asyncio.wait_for(_Agent(rt.watcher.model_factory(None)).run("只回复两个字：正常"), 40)
            return {"ok": True, "message": f"连接正常，模型 {rt.settings.ai_model} 回复：{str(result.output)[:40]}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"连接失败：{str(exc)[:300] or type(exc).__name__}"}

    # ---------- 策略库：Agent 和用户自己写的、可以反复用的策略 ----------

    def _playbooks(rt: Runtime) -> list[dict[str, Any]]:
        books = rt.watcher.strategies.playbooks
        env = rt.config().env
        return [{**p, "base_label": STRATEGY_KINDS[p["base"]]["label"], "score": books.score(p, env)} for p in books.all()]

    def _lessons(rt: Runtime) -> dict[str, Any]:
        env = rt.config().env
        return {"env": env, "lessons": rt.watcher.lessons.all(env), "shared": rt.watcher.lessons.all("shared")}

    @app.get("/api/lessons")
    async def lessons_get(request: Request):
        """长期经验：当前环境（模拟盘 / 实盘各一套）的行情经验 + 两边共用的规矩。"""
        return _lessons(rt_of(request))

    @app.put("/api/lessons")
    async def lessons_put(request: Request, body: LessonIn):
        rt = rt_of(request)
        scope = "shared" if body.scope == "shared" else rt.config().env
        books = rt.watcher.lessons
        if body.id is None:
            books.add(scope, body.inst_id.upper(), body.text, source="user", grade=body.grade or "rule")
        elif books.edit(scope, body.id, text=body.text, grade=body.grade, inst_id=body.inst_id.upper()) is None:
            raise HTTPException(404, "没有这条经验")
        return _lessons(rt)

    @app.delete("/api/lessons/{scope}/{lesson_id}")
    async def lessons_delete(request: Request, scope: Literal["env", "shared"], lesson_id: int):
        rt = rt_of(request)
        if not rt.watcher.lessons.delete("shared" if scope == "shared" else rt.config().env, lesson_id):
            raise HTTPException(404, "没有这条经验")
        return _lessons(rt)

    def _skills(rt: Runtime) -> dict[str, Any]:
        return {"dir": str(rt.skills.root), "skills": rt.skills.all()}

    @app.get("/api/skills")
    async def skills_get(request: Request):
        """扫一遍项目里的技能文件夹（手动拷进去的也能认出来）。"""
        return _skills(rt_of(request))

    @app.post("/api/skills")
    async def skills_install(request: Request, body: SkillIn):
        """装一个技能：粘贴的文本，或者 SKILL.md 的网址。只写进项目的技能文件夹。"""
        rt = rt_of(request)
        try:
            text = body.content if body.content.strip() else await fetch_skill(body.url, rt.settings.proxy_url or None)
            row = rt.skills.install(body.name, text)
        except SkillError as exc:
            raise HTTPException(400, str(exc)) from exc
        rt.watcher._emit(rt.config().env, "", "general", "wake", f"你装了技能「{row['name']}」，所有 Worker 都能用", None, {"skill": row["id"]})
        return _skills(rt)

    @app.get("/api/skills/{name}")
    async def skills_read(request: Request, name: str):
        rt = rt_of(request)
        skill = rt.skills.get(name)
        if skill is None:
            raise HTTPException(404, "没有这个技能")
        return {**skill, "content": rt.skills.read(name)}

    @app.post("/api/skills/{name}/enable")
    async def skills_enable(request: Request, name: str, on: bool = True):
        rt = rt_of(request)
        if rt.skills.set_enabled(name, on) is None:
            raise HTTPException(404, "没有这个技能")
        return _skills(rt)

    @app.delete("/api/skills/{name}")
    async def skills_delete(request: Request, name: str):
        rt = rt_of(request)
        if not rt.skills.delete(name):
            raise HTTPException(404, "没有这个技能")
        return _skills(rt)

    @app.get("/api/playbooks")
    async def playbooks_get(request: Request):
        return _playbooks(rt_of(request))

    @app.put("/api/playbooks")
    async def playbooks_put(request: Request, body: PlaybookIn):
        rt = rt_of(request)
        books = rt.watcher.strategies.playbooks
        try:
            row, created = books.write(body.name, body.base, body.when, body.entry, body.exit, body.defaults, author="用户")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not body.enabled:
            books.set_enabled(row["name"], False, "手动停用")
        rt.watcher._emit(rt.config().env, "", "strategy", "wake", f"你{'往策略库里加了' if created else '改写了策略'}「{row['name']}」（{STRATEGY_KINDS[row['base']]['label']}）", None, {"playbook": row["name"]})
        return _playbooks(rt)

    @app.post("/api/playbooks/{name}/enable")
    async def playbooks_enable(request: Request, name: str, on: bool = True):
        rt = rt_of(request)
        if rt.watcher.strategies.playbooks.set_enabled(name, on, "手动停用") is None:
            raise HTTPException(404, "策略库里没有这条策略")
        return _playbooks(rt)

    @app.delete("/api/playbooks/{name}")
    async def playbooks_delete(request: Request, name: str):
        rt = rt_of(request)
        if not rt.watcher.strategies.playbooks.delete(name):
            raise HTTPException(404, "策略库里没有这条策略")
        rt.watcher._emit(rt.config().env, "", "strategy", "wake", f"你从策略库里删掉了「{name}」", None, {"playbook": name})
        return _playbooks(rt)

    @app.get("/api/proxy")
    async def proxy_get(request: Request):
        return rt_of(request).proxy()

    @app.put("/api/proxy")
    async def proxy_put(request: Request, body: ProxyIn):
        url = body.url.strip()
        if url and not url.lower().startswith(("http://", "https://", "socks5://", "socks5h://")):
            raise HTTPException(400, "代理地址要以 http://、https:// 或 socks5:// 开头，比如 http://127.0.0.1:7890；留空就是不用代理")
        return await rt_of(request).save_proxy(url)

    @app.post("/api/proxy/test")
    async def proxy_test(request: Request):
        """按当前的代理设置真的连一次欧易（查 BTC 行情），告诉用户通不通。模型接口用「模型接口」里的测试连接。"""
        rt = rt_of(request)
        how = f"经代理 {rt.settings.proxy_url}" if rt.settings.proxy_url else "不用代理、直接连"
        try:
            started = time.time()
            await asyncio.wait_for(rt.market.ticker("BTC-USDT-SWAP", rt.config().env), 25)
            return {"ok": True, "message": f"{how}：欧易连接正常（{int((time.time() - started) * 1000)} 毫秒）"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{how}：连不上欧易：{str(exc)[:240] or type(exc).__name__}"}

    @app.post("/api/agent/start")
    async def agent_start(request: Request):
        rt = rt_of(request)
        try:
            await rt.watcher.start()
        except OkxError:
            raise
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        rt.store.set("agent_running", True)
        return rt.watcher.status()

    @app.post("/api/agent/stop")
    async def agent_stop(request: Request):
        rt = rt_of(request)
        await rt.watcher.stop()
        rt.store.set("agent_running", False)
        return rt.watcher.status()

    @app.get("/api/work")
    async def work_items(request: Request, limit: int = 60, inst_id: str | None = None):
        """任务列表：每次唤醒 Worker、每次被 Router 过滤掉的事件、每个用户交办的任务都是一条。"""
        rt = rt_of(request)
        return rt.store.work_items(rt.config().env, max(min(limit, 200), 1), inst_id.upper() if inst_id else None)

    @app.get("/api/work/{work_id}")
    async def work_detail(request: Request, work_id: int):
        rt = rt_of(request)
        row = rt.store.work_detail(rt.config().env, work_id)
        if row is None:
            raise HTTPException(404, "当前环境没有这条任务记录。")
        return row

    @app.post("/api/history/clear")
    async def clear_history(request: Request, body: ClearIn):
        """清除记录：已完成的任务、已结束的触发器、对话区。只删记录，不碰进行中的任务、等待中的触发器、持仓和挂单。"""
        rt = rt_of(request)
        inst = body.inst_id.upper()
        cleared = rt.store.clear_history(rt.config().env, inst, body.what)
        if body.what == "talk":
            rt.watcher.clear_chat(inst or "general")
        return {"cleared": cleared}

    @app.post("/api/agent/goal")
    async def agent_goal(request: Request, body: GoalIn):
        """给某个币的 Worker 定目标：做什么、最多亏多少、做多久。"""
        rt = rt_of(request)
        inst = body.inst_id.upper()
        if inst not in rt.config().instruments:
            raise HTTPException(400, "这个品种不在盯盘列表里。")
        return rt.watcher.set_goal(inst, body.goal, body.max_loss_usdt, body.hours, "用户", body.target_usdt, body.stop_when)

    @app.delete("/api/agent/goal")
    async def agent_goal_clear(request: Request, inst_id: str):
        rt_of(request).watcher.clear_goal(inst_id.upper())
        return {"ok": True}

    @app.post("/api/agent/standby")
    async def agent_standby(request: Request, body: StandbyIn):
        """你直接让某个 Worker 停下（只在触发线、策略、留言、持仓变化时醒来）或恢复定时复盘。"""
        rt = rt_of(request)
        inst = body.inst_id.upper()
        if inst not in rt.config().instruments:
            raise HTTPException(400, "这个品种不在盯盘列表里。")
        rt.watcher.set_standby(inst, body.on)
        return rt.watcher.status()

    @app.post("/api/agent/interject")
    async def agent_interject(request: Request, body: InterjectIn):
        """旧对话入口也走 Worker 提案流程。"""
        rt = rt_of(request)
        inst = body.inst_id.upper()
        if inst not in rt.config().instruments:
            raise HTTPException(400, "这个品种不在盯盘列表里。")
        out = await rt.watcher.chat(body.message.strip(), thread=inst, inst_id=inst, to="worker", queue=True)
        return {"delivered": True, "message": out["reply"], **out}

    @app.get("/api/strategies")
    async def strategies(request: Request, inst_id: str = "", all: bool = False):
        """Agent 布置的策略：正在等触发的、正在管理仓位的；all=true 时带上已经结束的。"""
        rt = rt_of(request)
        return rt.watcher.strategy_rows(rt.config().env, inst_id.upper() or None, live=not all)

    @app.post("/api/lots/{lot_id}/profit-guard")
    async def profit_guard(request: Request, lot_id: int, body: ExitRules):
        rt = rt_of(request)
        lot = rt.store.lot(lot_id)
        if rt.config().env != "demo" or not lot or lot["env"] != "demo" or lot["status"] != "open":
            raise Rejected("只可给当前模拟盘的一笔持仓添加提前止盈。")
        row = rt.watcher.strategies.deploy(rt.config(), lot["inst_id"], "profit_guard", "", {"lot_id": lot_id, **body.model_dump()}, source="user")
        rt.bus.publish("agent", rt.watcher.status())
        return {"id": row["id"]}

    @app.post("/api/recorders")
    async def recorder_add(request: Request, body: RecorderIn):
        """用户给某个 Worker 加一个记录器：程序记录的几项数据都到了，就叫醒这个 Worker（反复生效）。要「到了直接执行」的由 Worker 来设。"""
        from .strategy import recorder_params

        rt = rt_of(request)
        try:
            params = {**recorder_params([c.model_dump() for c in body.conds]), "do": "wake", "wake_note": body.note or "用户设的记录器到了，按现在的行情判断要不要做", "cooldown_minutes": body.cooldown_minutes}
            row = rt.watcher.strategies.deploy(rt.config(), body.inst_id.upper(), "trigger", "", params, source="user", note=body.note)
        except Rejected as exc:
            raise HTTPException(400, str(exc)) from exc
        rt.bus.publish("agent", rt.watcher.status())
        return row

    @app.post("/api/strategies/{strategy_id}/edit")
    async def strategy_edit(request: Request, strategy_id: int, body: RecorderEdit):
        """用户直接改一个记录器 / 触发器的数值、间隔，或者把它关掉、打开。"""
        rt = rt_of(request)
        try:
            changes = body.model_dump(exclude_none=True)
            row = rt.watcher.strategies.edit(strategy_id, changes, "用户改的", env=rt.config().env)
        except Rejected as exc:
            raise HTTPException(400, str(exc)) from exc
        rt.bus.publish("agent", rt.watcher.status())
        rt.bus.publish("chat", {})
        return row

    @app.post("/api/strategies/{strategy_id}/{verb}")
    async def strategy_act(request: Request, strategy_id: int, verb: Literal["approve", "cancel"]):
        rt = rt_of(request)
        engine, env = rt.watcher.strategies, rt.config().env
        try:
            row = engine.approve(strategy_id, env=env) if verb == "approve" else engine.cancel(strategy_id, "用户撤销", env=env)
        finally:
            rt.watcher.on_decided()
            rt.bus.publish("agent", rt.watcher.status())
            rt.bus.publish("chat", {})
        if not row:
            raise HTTPException(404, "没有这条策略。")
        return row

    @app.get("/api/logs")
    async def logs_info(request: Request):
        """各种记录现在占了多少：数据库里的运行记录条数、数据库文件大小、欧易官方组件的调用日志大小。"""
        rt = rt_of(request)
        from pathlib import Path

        db = Path(settings.data_dir) / "ouy.db"
        return {
            "keep_days": rt.config().log_keep_days, "counts": rt.store.log_counts(),
            "db_mb": round(db.stat().st_size / 1e6, 1) if db.is_file() else 0,
            "okx_mb": round(sum(f.stat().st_size for f in _okx_log_files()) / 1e6, 1),
        }

    @app.post("/api/logs/clear")
    async def logs_clear(request: Request, body: LogClearIn):
        """手动清理记录：数据库里的运行记录 + 欧易官方组件的调用日志。进行中的任务、还在等的策略、仓位、经验、战绩、设置都不动。"""
        rt = rt_of(request)
        gone = rt.store.prune(body.keep_days)
        files, cut = 0, time.time() - body.keep_days * 86_400
        for f in _okx_log_files():
            try:
                if f.stat().st_mtime < cut or body.keep_days == 0:
                    f.unlink()
                    files += 1
            except OSError:  # 正在写的那个文件删不掉就算了
                pass
        return {"cleared": gone, "okx_files": files, **(await logs_info(request))}

    @app.get("/api/activity")
    async def activity(request: Request, limit: int = 200, inst_id: str = "", after: int = 0):
        """活动流：每个 Agent 一步步在做什么（被什么唤醒、调了什么工具、交给了谁、做了什么操作、结论）。"""
        rt = rt_of(request)
        return rt.store.activity(rt.config().env, max(min(limit, 500), 1), inst_id.upper() or None, max(after, 0))

    @app.post("/api/agent/intel")
    async def agent_intel(request: Request, body: ReviewIn):
        """让这个币的情报 Worker 现在去查一次。"""
        rt = rt_of(request)
        inst = body.inst_id.upper()
        if body.role in rt.watcher._state(inst).sub_running:
            raise HTTPException(409, "这个子代理正在干活，稍等。")
        try:
            return await rt.watcher.intel(inst, body.question, body.role)
        except (OkxError, Rejected):
            raise
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"情报 Worker 没查成：{str(exc)[:300]}") from exc

    @app.post("/api/agent/review")
    async def agent_review(request: Request, body: ReviewIn):
        rt = rt_of(request)
        inst = body.inst_id.upper()
        if inst not in rt.config().instruments:
            raise HTTPException(400, "这个品种不在盯盘列表里。")
        if rt.watcher._state(inst).reviewing:
            raise HTTPException(409, "Agent 正在复盘这个品种，稍等。")
        try:
            return await rt.watcher.review(inst, "用户手动要求立即复盘")
        except (OkxError, Rejected):
            raise
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 模型接口的错误原样告诉用户
            raise HTTPException(502, f"Agent 复盘失败：{str(exc)[:300]}") from exc
        finally:
            rt.bus.publish("agent", rt.watcher.status())

    @app.get("/api/journal")
    async def journal(request: Request, limit: int = 100, inst_id: str | None = None, before_id: int | None = None):
        rt = rt_of(request)
        return rt.store.journal(rt.config().env, limit, inst_id=inst_id.upper() if inst_id else None, before_id=before_id)

    @app.get("/api/pending")
    async def pending(request: Request):
        rt = rt_of(request)
        from .trader import PENDING_TTL_MS, describe

        rt.store.expire_pending(rt.config().env, PENDING_TTL_MS)
        return [{**row, "title": describe(TradeAction.model_validate(row["action"]))} for row in rt.store.pending(rt.config().env)]

    @app.post("/api/pending/{pending_id}/approve")
    async def approve(request: Request, pending_id: int):
        rt = rt_of(request)
        try:
            return (await rt.trader.approve(pending_id)).model_dump()
        finally:
            rt.watcher.on_decided()  # 这次调用是 Agent 挂起等同意的：把结果交还给它，让它接着做

    @app.post("/api/pending/{pending_id}/reject")
    async def reject(request: Request, pending_id: int):
        try:
            rt_of(request).trader.reject(pending_id)
        finally:
            rt_of(request).watcher.on_decided()
        return {"ok": True}

    @app.post("/api/agent/plan/apply")
    async def apply_plan(request: Request, inst_id: str, ts: int):
        return await rt_of(request).watcher.apply_plan(inst_id.strip().upper(), ts)

    @app.get("/api/agent/plan/status")
    async def plan_status(request: Request, inst_id: str, ts: int):
        return await rt_of(request).watcher.plan_receipt(inst_id.strip().upper(), ts)

    @app.post("/api/chat/consult")
    async def consult(request: Request, body: ChatIn):
        if body.to != "router" or body.queue:
            raise HTTPException(400, "只读问答不能派给 Worker 或加入执行队列。")
        rt = rt_of(request)
        images = [save_upload(rt.settings.data_dir / "uploads", url) for url in body.images]
        return await rt.watcher.consult(body.message, body.thread, body.inst_id, body.effort, images)

    @app.post("/api/chat")
    async def chat(request: Request, body: ChatIn):
        try:
            rt = rt_of(request)
            images = [save_upload(rt.settings.data_dir / "uploads", url) for url in body.images]
            return await rt.watcher.chat(body.message, body.effort, body.thread, body.inst_id.upper() if body.inst_id else None, body.to, body.queue, images=images)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (OkxError, Rejected):
            raise
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Agent 回复失败：{str(exc)[:300]}") from exc

    @app.get("/api/uploads/{name}")
    async def upload(request: Request, name: str):
        """对话里附的图片。文件名是内容的哈希，只认这个目录里的文件。"""
        folder = (rt_of(request).settings.data_dir / "uploads").resolve()
        file = (folder / name).resolve()
        if folder not in file.parents or not file.is_file():
            raise HTTPException(404)
        return FileResponse(file, headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @app.get("/api/chat")
    async def chat_log(request: Request, thread: str = Query("general", max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")):
        return {"messages": rt_of(request).watcher.chat_log(thread)}

    @app.delete("/api/chat")
    async def chat_clear(request: Request, thread: str = Query("general", max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")):
        rt_of(request).watcher.clear_chat(thread)
        return {"ok": True}

    # ---------- 欧易全功能 ----------

    @app.get("/api/okx/tools")
    async def okx_tools(request: Request):
        return [t.public() for t in await rt_of(request).gateway.tools()]

    @app.post("/api/okx/call")
    async def okx_call(request: Request, body: OkxCall):
        rt = rt_of(request)
        info = next((t for t in await rt.gateway.tools() if t.name == body.tool), None)
        if info is None:
            raise HTTPException(404, "没有这个欧易工具")
        if not info.read_only and not body.confirm:
            raise HTTPException(400, "这是会改动账户的操作，需要确认后才能执行。")
        env = rt.config().env
        if body.env and body.env != env:
            # 只读工具可以指定用实盘通道查（新闻、情绪、聪明钱这些在模拟盘通道下不可用）；会改动账户的操作永远只走当前环境
            if not info.read_only:
                raise HTTPException(400, "会改动账户的操作只能在当前环境下执行。")
            return {"result": await rt.gateway.call(body.tool, body.args, env=body.env)}
        result = await rt.gateway.call(body.tool, body.args, env=env)
        if not info.read_only:
            rt.bus.publish("journal", rt.store.log(env, "action", f"手动调用 {info.title}", {"status": "executed", "source": "user", "tool": body.tool, "args": body.args}))
            rt.bus.publish("account", None)
        return {"result": result}

    # ---------- 知识库 ----------

    class KbDocIn(BaseModel):
        source: str = Field(min_length=1, max_length=120)
        content: str = Field(min_length=1, max_length=100_000)

    @app.get("/api/kb")
    async def kb(request: Request):
        return kb_view(rt_of(request))

    @app.get("/api/backtest")
    async def backtest_reports(request: Request):
        """已存盘的回测报告（python -m ouy.backtest_plans ... --save / python -m ouy.backtest_trend ... --save）。"""
        from .backtest_plans import saved_reports

        return saved_reports()

    @app.get("/api/algo")
    async def algo(request: Request, inst_id: str):
        """算法事件：每个算法算出了什么、触发了什么、之后价格有没有照它说的走。"""
        rt = rt_of(request)
        return rt.watcher.algo_report(rt.config().env, inst_id.upper())

    @app.get("/api/stats/cache")
    async def cache_hits(request: Request):
        """最近若干轮复盘的模型缓存命中率（服务端返回的真实读数）。"""
        from .agent.watcher import cache_stats

        rt = rt_of(request)
        return cache_stats(rt.store.get(f"usage:{rt.config().env}", []))

    @app.get("/api/stats/strategies")
    async def strategy_stats(request: Request):
        """各打法的战绩：程序进场时记下用的哪条打法，平仓后把已实现盈亏记到它头上。"""
        rt = rt_of(request)
        env = rt.config().env
        return {"env": env, "stats": rt.watcher.scores.stats(env), "origins": rt.watcher.scores.origins(env), "min_sample": 20}

    @app.get("/api/kb/doc")
    async def kb_doc(request: Request, source: str):
        try:
            return rt_of(request).kb.get_doc(source)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/kb/doc")
    async def kb_save(request: Request, body: KbDocIn):
        try:
            return rt_of(request).kb.save_doc(body.source, body.content)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/kb/doc")
    async def kb_delete(request: Request, source: str):
        try:
            rt_of(request).kb.delete_doc(source)
            return {"ok": True}
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/kb/search")
    async def kb_search(request: Request, q: str, k: int = 6):
        hits = await rt_of(request).kb.search(q, max(min(k, 20), 1))
        return [chunk.public(score) for chunk, score in hits]

    # ---------- 实时推送 ----------

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        if not _host_ok(socket.headers) or not _same_origin(socket.headers) or not _authorized(socket.cookies, socket.headers):
            await socket.close(code=4401)
            return
        await socket.accept()
        rt: Runtime = socket.app.state.rt
        queue = rt.bus.subscribe()
        try:
            while True:
                await socket.send_json(await queue.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            rt.bus.unsubscribe(queue)

    # ---------- 前端 ----------

    dist = ROOT / "web" / "dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            file = (dist / path).resolve()
            if path and file.is_file() and dist.resolve() in file.parents:
                return FileResponse(file)
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    return app
