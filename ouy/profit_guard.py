"""提前止盈只看这一笔的入场、目标和可平仓报价，不改交易所保护单。"""
from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field


class ExitRules(BaseModel):
    on_trigger: Literal["notify", "close"] = "notify"
    arm_pct: float = Field(70, ge=10, lt=100)
    giveback_pct: float = Field(25, ge=5, le=80)
    stall_minutes: float = Field(3, ge=0.5, le=240)
    cost_pct: float = Field(0.15, ge=0.1, le=5)


def quote(ticker: dict, long: bool, now: float) -> tuple[float, float] | None:
    try:
        bid, ask, ts = float(ticker["bidPx"]), float(ticker["askPx"]), float(ticker["ts"]) / 1000
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 < bid <= ask and math.isfinite(ask) and now - 10 <= ts <= now + 1):
        return None
    return (bid if long else ask), ts


def signal(p: dict, state: dict[str, Any], px: float, ts: float) -> str:
    target = abs(p["target"] - p["entry"])
    gain = (px - p["entry"]) * (1 if p["long"] else -1)
    if not target or ts < state.get("quote_ts", 0):
        return ""
    gap = ts - state.get("quote_ts", 0) > 15
    near = gain >= target * p["arm_pct"] / 100
    peak = max(gain, 0 if gap else state.get("peak", 0))
    # 报价断档（含服务重启）不能算作横盘时间；微小跳价不重置时钟。
    if gap or not near or not state.get("near"):
        state.update(progress=gain, progress_ts=ts)
    if gain >= state.get("progress", 0) + target * 0.02:
        state.update(progress=gain, progress_ts=ts)
    state.update(peak=peak, quote_ts=ts, near=near)
    if gain <= p["entry"] * p["cost_pct"] / 100 or gain >= target or peak < target * p["arm_pct"] / 100:
        return ""
    back = (peak - gain) / peak * 100
    if back >= p["giveback_pct"]:
        return f"盈利从峰值回吐 {back:.1f}%"
    if gain >= target * p["arm_pct"] / 100 and ts - state.get("progress_ts", ts) >= p["stall_minutes"] * 60:
        return f"接近目标后 {p['stall_minutes']:g} 分钟没有明显推进"
    return ""
