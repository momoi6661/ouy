"""账户状态：权益、持仓、挂单、止盈止损单。全部来自欧易真实接口，取不到就报错。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .market import _f
from .models import AccountState, ClosedTrade, Env, OpenOrder, Position, ProtectiveOrder, RiskLimits
from .okx.gateway import Gateway
from .store import Store


def _opt(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_position(row: dict[str, Any]) -> Position | None:
    raw = _f(row.get("pos"))
    if raw == 0:
        return None
    pos_side = row.get("posSide") or "net"
    side = pos_side if pos_side in ("long", "short") else ("long" if raw > 0 else "short")
    return Position(
        inst_id=row.get("instId", ""),
        side=side,  # type: ignore[arg-type]
        contracts=abs(raw),
        avg_px=_f(row.get("avgPx")),
        mark_px=_f(row.get("markPx")) or _f(row.get("last")),
        upl=_f(row.get("upl")),
        upl_ratio=_f(row.get("uplRatio")),
        leverage=_f(row.get("lever"), 1),
        liq_px=_opt(row.get("liqPx")),
        margin_mode=row.get("mgnMode") or "cross",
        pos_side=pos_side,
        notional_usd=abs(_f(row.get("notionalUsd"))),
        # 投入的保证金：逐仓看 margin，全仓看 imr；都没有就按 名义价值 ÷ 杠杆 算
        margin=_f(row.get("margin")) or _f(row.get("imr")) or abs(_f(row.get("notionalUsd"))) / max(_f(row.get("lever"), 1), 1),
        opened_at=int(row["cTime"]) if str(row.get("cTime") or "").isdigit() else None,
    )


def parse_protection(row: dict[str, Any]) -> ProtectiveOrder | None:
    sl, tp, trig = _opt(row.get("slTriggerPx")), _opt(row.get("tpTriggerPx")), _opt(row.get("triggerPx"))
    if not (sl or tp or trig or row.get("ordType") == "move_order_stop"):
        return None
    return ProtectiveOrder(
        algo_id=str(row.get("algoId", "")),
        inst_id=row.get("instId", ""),
        ord_type=row.get("ordType", ""),
        side=row.get("side", ""),
        pos_side=row.get("posSide") or "net",
        sz=str(row.get("sz") or ("全仓" if row.get("closeFraction") else "")),
        stop_loss=sl,
        take_profit=tp,
        trigger_px=trig,
    )


def parse_order(row: dict[str, Any]) -> OpenOrder:
    return OpenOrder(
        ord_id=str(row.get("ordId", "")),
        inst_id=row.get("instId", ""),
        side=row.get("side", ""),
        pos_side=row.get("posSide") or "net",
        ord_type=row.get("ordType", ""),
        px=_opt(row.get("px")),
        sz=_f(row.get("sz")),
        filled=_f(row.get("accFillSz")),
        reduce_only=str(row.get("reduceOnly", "")).lower() == "true",
        state=row.get("state", ""),
        created_at=int(row["cTime"]) if str(row.get("cTime") or "").isdigit() else None,
        leverage=_f(row.get("lever")),
    )


async def load_account(gw: Gateway, env: Env, store: Store, limits: RiskLimits) -> AccountState:
    config, balance, positions, orders, algos = await asyncio.gather(
        gw.call("account_get_config", {}, env=env),
        gw.call("account_get_balance", {}, env=env),
        gw.call("account_get_positions", {"instType": "SWAP"}, env=env),
        gw.call("swap_get_orders", {"status": "open"}, env=env),
        gw.call("swap_get_algo_orders", {"status": "pending"}, env=env),
    )
    cfg = (config or [{}])[0]
    bal = (balance or [{}])[0]
    usdt = next((d for d in bal.get("details", []) if d.get("ccy") == "USDT"), {})
    equity = _f(bal.get("totalEq")) or _f(usdt.get("eq"))
    available = _f(usdt.get("availEq")) or _f(usdt.get("availBal"))
    parsed = [p for p in (parse_position(r) for r in positions or []) if p]

    day_key = f"day:{env}:{time.strftime('%Y-%m-%d')}"
    day_start = store.get(day_key)
    if day_start is None and equity > 0:
        store.set(day_key, equity)
        day_start = equity
    day_pnl_pct = (equity / day_start - 1) * 100 if day_start else None
    return AccountState(
        env=env,
        equity=equity,
        available=available,
        cash_usdt=_f(usdt.get("cashBal")) or _f(usdt.get("eq")),
        margin_used=_f(usdt.get("frozenBal")) or _f(usdt.get("imr")) or sum(p.margin for p in parsed) or max(_f(usdt.get("eq")) - available, 0.0),
        upl=sum(p.upl for p in parsed),
        day_pnl=equity - day_start if day_start else None,
        account_level=str(cfg.get("acctLv", "")),
        position_mode=cfg.get("posMode") or "net_mode",
        positions=parsed,
        orders=[parse_order(r) for r in orders or []],
        protections=[p for p in (parse_protection(r) for r in algos or []) if p],
        day_start_equity=day_start,
        day_pnl_pct=day_pnl_pct,
        halted=bool(limits.max_daily_loss_pct > 0 and day_pnl_pct is not None and day_pnl_pct <= -limits.max_daily_loss_pct),
    )


_CLOSE_TYPES = {"1": "部分平仓", "2": "全部平仓", "3": "强平", "4": "部分强平", "5": "自动减仓"}


async def closed_trades(gw: Gateway, env: Env, inst_id: str | None = None, limit: int = 20) -> list[ClosedTrade]:
    """最近平掉的合约仓位，新的在前。"""
    rows = await gw.call("account_get_positions_history", {"instType": "SWAP", "instId": inst_id, "limit": max(1, min(limit, 100))}, env=env)
    trades = [
        ClosedTrade(
            inst_id=str(r.get("instId", "")),
            side="short" if str(r.get("direction") or r.get("posSide")) == "short" else "long",
            open_px=_f(r.get("openAvgPx")),
            close_px=_f(r.get("closeAvgPx")),
            pnl=_f(r.get("realizedPnl") if r.get("realizedPnl") not in (None, "") else r.get("pnl")),
            pnl_pct=_f(r.get("pnlRatio")) * 100,
            leverage=_f(r.get("lever")),
            opened_ts=int(_f(r.get("cTime"))),
            closed_ts=int(_f(r.get("uTime"))),
            how=_CLOSE_TYPES.get(str(r.get("type")), "平仓"),
        )
        for r in rows or []
        if isinstance(r, dict)
    ]
    return sorted(trades, key=lambda t: t.closed_ts, reverse=True)
