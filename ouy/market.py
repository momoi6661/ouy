"""行情：K 线、盘口、资金费率、合约规格，以及给 Agent 的多周期快照。"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .indicators import Candle, day_profile, summarize, value_area, window_levels
from .models import Env, Instrument
from .okx.gateway import Gateway, OkxError

SNAPSHOT_BARS = ("5m", "15m", "1H", "4H")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def inst_type_of(inst_id: str) -> str:
    parts = inst_id.split("-")
    if inst_id.endswith("-SWAP"):
        return "SWAP"
    if len(parts) == 2:
        return "SPOT"
    if len(parts) >= 5:
        return "OPTION"
    return "FUTURES"


ASSET_CLASSES = {"1": "crypto", "3": "stock", "4": "commodity", "5": "forex", "6": "bond"}


def funding_interval_hours(row: dict[str, Any]) -> float:
    """这个合约多久结算一次资金费（小时）。欧易默认 8 小时，也有 4 / 2 / 1 小时的，费率打到上下限时还会自动加密。"""
    at = _f(row.get("fundingTime"))
    for other in (row.get("nextFundingTime"), row.get("prevFundingTime")):
        gap = abs(_f(other) - at) / 3_600_000 if at and other else 0
        if gap in (1, 2, 4, 8):
            return gap
    return 0


def session_anchor(asset: str, ts: float | None = None) -> tuple[int, str]:
    """「开盘」从哪一刻算起：（毫秒时间戳，给人看的说法）。加密货币 24 小时交易，按 UTC 0 点；股票永续按最近一次美股正常开盘（美东 09:30）。"""
    now = ts if ts is not None else time.time()
    if asset != "stock":
        return int(now // 86_400) * 86_400_000, "今天（北京时间 8 点起）"
    utc = datetime.fromtimestamp(now, timezone.utc)
    offset = timedelta(hours=4 if _us_dst(utc) else 5)
    et = utc - offset
    opened = et.replace(hour=9, minute=30, second=0, microsecond=0)
    if et < opened:
        opened -= timedelta(days=1)
    while opened.weekday() >= 5:  # 周末不开盘，往前找到周五
        opened -= timedelta(days=1)
    return int((opened + offset).timestamp() * 1000), f"美股 {opened:%m-%d} 开盘（美东 09:30）"


def _us_dst(utc: datetime) -> bool:
    """美国夏令时：三月第二个周日到十一月第一个周日。"""
    def nth_sunday(month: int, n: int) -> datetime:
        first = datetime(utc.year, month, 1, tzinfo=timezone.utc)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))

    return nth_sunday(3, 2) + timedelta(hours=7) <= utc < nth_sunday(11, 1) + timedelta(hours=6)


def us_stock_session(ts: float | None = None) -> tuple[str, str]:
    """美股现在在哪个时段：(regular / pre / post / overnight / closed, 给 Agent 看的一句话)。
    按美东时间算，夏令时自己推（三月第二个周日到十一月第一个周日），不依赖系统时区库；不含节假日。"""
    utc = datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc)
    dst = _us_dst(utc)
    et = utc - timedelta(hours=4 if dst else 5)
    minutes, day = et.hour * 60 + et.minute, et.weekday()  # 周一 = 0
    clock = f"美东 {et:%H:%M}（{'夏' if dst else '冬'}令时）"
    if (day == 4 and minutes >= 20 * 60) or day == 5 or (day == 6 and minutes < 20 * 60):
        return "closed", f"{clock}：美股周末休市，指数锚在周五的最后价格附近，周日晚重开时可能跳空"
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "regular", f"{clock}：美股正常交易时段，价格发现最充分"
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "pre", f"{clock}：美股盘前，成交比正常时段薄，09:30 开盘前后波动最大"
    if 16 * 60 <= minutes < 20 * 60:
        return "post", f"{clock}：美股盘后，成交薄，财报多在这个时段公布"
    return "overnight", f"{clock}：美股夜盘，成交最薄，走势参考意义低、容易被少量资金带动"


class Market:
    def __init__(self, gateway: Gateway):
        self.gw = gateway
        self._instruments: dict[tuple[str, str], tuple[float, Instrument]] = {}

    @staticmethod
    def _demo(env: Env) -> bool:
        return env == "demo"

    async def candles(self, inst_id: str, bar: str, limit: int, env: Env) -> list[Candle]:
        rows = await self.gw.call(
            "market_get_candles", {"instId": inst_id, "bar": bar, "limit": min(limit, 300), "demo": self._demo(env)}, env=env
        )
        out = [
            Candle(int(r[0]), _f(r[1]), _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5]), str(r[8]) == "1" if len(r) > 8 else True)
            for r in rows or []
        ]
        out.sort(key=lambda c: c.ts)
        return out

    async def ticker(self, inst_id: str, env: Env) -> dict[str, Any]:
        rows = await self.gw.call("market_get_ticker", {"instId": inst_id, "demo": self._demo(env)}, env=env)
        if not rows:
            raise OkxError(f"没有 {inst_id} 的行情", tool="market_get_ticker")
        return rows[0]

    async def tickers(self, inst_type: str, env: Env) -> list[dict[str, Any]]:
        return await self.gw.call("market_get_tickers", {"instType": inst_type, "demo": self._demo(env)}, env=env) or []

    async def instrument(self, inst_id: str, env: Env) -> Instrument:
        key = (env, inst_id)
        cached = self._instruments.get(key)
        if cached and time.time() - cached[0] < 3600:
            return cached[1]
        rows = await self.gw.call(
            "market_get_instruments",
            {"instType": inst_type_of(inst_id), "instId": inst_id, "demo": self._demo(env)},
            env=env,
        )
        if not rows:
            raise OkxError(f"欧易没有 {inst_id} 这个交易品种", tool="market_get_instruments")
        r = rows[0]
        inst = Instrument(
            inst_id=r["instId"],
            inst_type=r.get("instType", ""),
            ct_val=_f(r.get("ctVal"), 1) or 1,
            ct_type=r.get("ctType", ""),
            settle_ccy=r.get("settleCcy", ""),
            lot_sz=r.get("lotSz") or "1",
            min_sz=r.get("minSz") or "1",
            tick_sz=r.get("tickSz") or "0.1",
            max_leverage=_f(r.get("lever"), 1) or 1,
            state=r.get("state", "live"),
            category=str(r.get("instCategory") or "1"),
        )
        self._instruments[key] = (time.time(), inst)
        return inst

    async def asset_class(self, inst_id: str, env: Env) -> str:
        """标的是什么资产：crypto / stock / commodity / forex / bond。查不到按加密货币处理。"""
        try:
            return ASSET_CLASSES.get((await self.instrument(inst_id, env)).category, "crypto")
        except Exception:  # noqa: BLE001 - 类别只用来决定带不带 BTC 大盘参考，拿不到不影响复盘
            return "crypto"

    async def book(self, inst_id: str, env: Env, depth: int = 10) -> dict[str, Any]:
        """盘口档位（给界面用）：asks 从最优卖价往上，bids 从最优买价往下，每档 [价格, 张数]。"""
        rows = await self.gw.call("market_get_orderbook", {"instId": inst_id, "sz": depth, "demo": self._demo(env)}, env=env)
        book = rows[0] if rows else {}
        return {
            "asks": [[_f(p), _f(s)] for p, s, *_ in book.get("asks", [])],
            "bids": [[_f(p), _f(s)] for p, s, *_ in book.get("bids", [])],
        }

    async def orderbook(self, inst_id: str, env: Env, depth: int = 20) -> dict[str, Any]:
        rows = await self.gw.call("market_get_orderbook", {"instId": inst_id, "sz": depth, "demo": self._demo(env)}, env=env)
        book = rows[0] if rows else {"asks": [], "bids": []}
        bids = [(_f(p), _f(s)) for p, s, *_ in book.get("bids", [])]
        asks = [(_f(p), _f(s)) for p, s, *_ in book.get("asks", [])]
        bid_vol, ask_vol = sum(s for _, s in bids), sum(s for _, s in asks)
        total = bid_vol + ask_vol
        return {
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None,
            "spread_pct": round((asks[0][0] - bids[0][0]) / bids[0][0] * 100, 5) if bids and asks else None,
            "bid_volume": bid_vol,
            "ask_volume": ask_vol,
            "imbalance": round((bid_vol - ask_vol) / total, 3) if total else 0,
            "largest_bid": max(bids, key=lambda x: x[1]) if bids else None,
            "largest_ask": max(asks, key=lambda x: x[1]) if asks else None,
            "depth": depth,
        }

    async def derivatives(self, inst_id: str, env: Env) -> dict[str, Any]:
        """永续合约特有数据：资金费率、持仓量。取不到就如实标空，不编数据。"""
        if not inst_id.endswith("-SWAP"):
            return {}
        out: dict[str, Any] = {}
        demo = self._demo(env)
        funding, oi = await asyncio.gather(
            self.gw.call("market_get_funding_rate", {"instId": inst_id, "demo": demo}, env=env),
            self.gw.call("market_get_open_interest", {"instType": "SWAP", "instId": inst_id, "demo": demo}, env=env),
            return_exceptions=True,
        )
        if not isinstance(funding, BaseException) and funding:
            out["funding_rate_pct"] = round(_f(funding[0].get("fundingRate")) * 100, 5)
            out["next_funding_time"] = funding[0].get("fundingTime")
            hours = funding_interval_hours(funding[0])
            if hours:  # 结算周期不是 8 小时的品种，折成 8 小时才能和「0.03% / 0.05%」这类阈值比
                out["funding_interval_hours"] = hours
                out["funding_rate_8h_pct"] = round(out["funding_rate_pct"] * 8 / hours, 5)
        if not isinstance(oi, BaseException) and oi:
            out["open_interest_usd"] = round(_f(oi[0].get("oiUsd")))
        return out

    async def brief(self, inst_id: str, env: Env, bars: tuple[str, ...] = ("15m", "1H", "4H")) -> dict[str, Any]:
        """某个品种的精简走势（给「大盘参考」用）：最新价、24h 涨跌、几个周期的关键读数。"""
        ticker, *frames = await asyncio.gather(self.ticker(inst_id, env), *(self.candles(inst_id, bar, 260, env) for bar in bars))
        last = _f(ticker.get("last"))
        open24 = _f(ticker.get("open24h"))
        return {
            "inst_id": inst_id,
            "last": last,
            "change_24h_pct": round((last / open24 - 1) * 100, 3) if open24 else None,
            "timeframes": {
                bar: {k: v for k, v in summarize(frame).items() if isinstance(v, (str, int, float, bool)) or v is None}
                for bar, frame in zip(bars, frames)
            },
        }

    async def chart(self, inst_id: str, bar: str, env: Env, rows: int = 60) -> dict[str, Any]:
        """一个周期的走势：原始 K 线 + 区间上下沿 + 支撑阻力 + K 线形态（和 Worker 复盘时看到的是同一套算法）。"""
        from .indicators import read_chart

        return {"inst_id": inst_id, "bar": bar, **read_chart(await self.candles(inst_id, bar, 300, env), rows)}

    async def snapshot(self, inst_id: str, env: Env, bars: tuple[str, ...] = SNAPSHOT_BARS) -> dict[str, Any]:
        """多周期指标 + 盘口 + 衍生品数据。任何一块真实数据拿不到就报错，不用假数据顶替。"""
        ticker, book, deriv, *frames = await asyncio.gather(
            self.ticker(inst_id, env),
            self.orderbook(inst_id, env),
            self.derivatives(inst_id, env),
            *(self.candles(inst_id, bar, 260, env) for bar in bars),
        )
        last = _f(ticker.get("last"))
        open24 = _f(ticker.get("open24h"))
        area = value_area(dict(zip(bars, frames)), last)
        session = day_profile(dict(zip(bars, frames)), last, *session_anchor(await self.asset_class(inst_id, env)))
        return {
            "inst_id": inst_id,
            "time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "ticker_ts_ms": int(_f(ticker.get("ts"))) or None,
            "last": last,
            "change_24h_pct": round((last / open24 - 1) * 100, 3) if open24 else None,
            "high_24h": _f(ticker.get("high24h")),
            "low_24h": _f(ticker.get("low24h")),
            "orderbook": book,
            "derivatives": deriv,
            "timeframes": {bar: summarize(frame) for bar, frame in zip(bars, frames)},
            # 前高前低法：上一段（30 分钟 / 1 小时 / 4 小时 / 1 天）的高低点，以及价格现在相对它们的位置
            "windows": window_levels(dict(zip(bars, frames))),
            # 价值区间：上一个 UTC 日成交最密集的价格带（POC / VAH / VAL）
            **({"value_area": area} if area else {}),
            # 开盘以来：从开盘到现在是怎么走的（开盘价、先去了哪边、属于哪种走法、开盘区间）
            **({"session": session} if session else {}),
        }
