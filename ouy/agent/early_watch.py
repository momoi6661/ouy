"""模拟盘的只读预警：看逐次采到的报价，不把触及观察区当成成交条件。"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, FiniteFloat


WINDOW_S = 30
REARM_S = 60


class WatchAdvice(BaseModel):
    reason: str = Field(min_length=1, max_length=200, description="为什么保持或调整观察位，不给交易指令")
    observe_price: FiniteFloat | None = Field(default=None, gt=0, description="有结构依据时补充一个观察价，否则留空")


@dataclass
class ShiftRetest:
    """Each observed loss/recovery is an edge; identical quotes never advance it."""
    key: tuple = ()
    last_ts: float = 0
    stage: int = 0
    hits: int = 0
    valid: bool = False

    @property
    def first_loss(self) -> bool:
        """同一次转折只在第一次盘中失守时叫醒 Worker；之后的收回、再失守只记录。"""
        return self.stage == 1

    def sample(self, now: float, ts: float, px: float, metrics: dict) -> str:
        shift = metrics.get("shift") or {}
        level, side = shift.get("level", 0), shift.get("side")
        if (side not in ("long", "short") or not level or shift.get("bars_ago", 99) > 3
                or not 0 <= now - metrics.get("ts", 0) / 1000 <= 30
                or not 0 <= ts - (shift.get("ts", 0) / 1000 + 300) <= 1200
                or not -2 <= now - ts <= 15 or not math.isfinite(px) or px <= 0):
            self.hits, self.valid = 0, False
            return ""
        key = (shift["ts"], side, level)
        if key != self.key:
            self.key, self.stage, self.hits, self.last_ts = key, 0, 0, 0
        self.valid = ts >= self.last_ts
        if ts <= self.last_ts:
            return ""
        if ts - self.last_ts > 15:
            self.hits = 0
        self.last_ts = ts
        values = metrics.get("values") or {}
        band = max(level * 0.0005, (values.get("atr") or 0) * 0.1, level * (values.get("spread_pct") or 0) / 100)
        distance = (px - level) * (1 if side == "long" else -1)
        beyond = distance <= -band if self.stage % 2 == 0 else distance >= band
        self.hits = self.hits + 1 if beyond else 0
        if self.hits < 2:
            return ""
        self.stage += 1
        self.hits = 0
        event = "已退回突破前一侧，疑似假突破" if self.stage % 2 else "已回到原突破方向，刚才的失守可能只是波动"
        return f"5m {'向上' if side == 'long' else '向下'}突破位 {level:g}，连续两次新报价{event}（现价 {px:g}，缓冲 {band:g}）。盘中预警，未等收盘；不是平仓或反手指令。"


@dataclass
class QuoteWatch:
    samples: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=120))
    near: set[float] = field(default_factory=set)
    last_notice: float = -math.inf
    move_side: int = 0

    def sample(self, ts: float, px: float, levels: set[float], move_pct: float) -> str:
        if not (math.isfinite(px) and px > 0 and math.isfinite(ts)):
            return ""
        if self.samples and ts <= self.samples[-1][0]:
            return ""
        while self.samples and ts - self.samples[0][0] > WINDOW_S:
            self.samples.popleft()
        if not self.samples:
            self.near.clear()
            self.move_side = 0
        band = move_pct / 4
        self.near.intersection_update(levels)
        messages = []
        previous = self.samples[-1][1] if self.samples else px
        for level in sorted(p for p in levels if p > 0):
            dist = abs(px / level - 1) * 100
            if dist > band * 2:
                self.near.discard(level)
            elif dist <= band and level not in self.near:
                self.near.add(level)
                if abs(previous - level) > abs(px - level) and (px - level) * (previous - level) > 0:
                    messages.append(f"接近观察位 {level:g}，相距 {dist:.2f}%，尚未触线")

        self.samples.append((ts, px))
        if len(self.samples) >= 3:
            # 从窗口内最近的极值量回撤/反弹，不把未收盘的报价当成趋势确认。
            lo = min(p for _, p in self.samples)
            hi = max(p for _, p in self.samples)
            up, down = (px / lo - 1) * 100, (1 - px / hi) * 100
            threshold = move_pct / 2
            side = 1 if px > previous and up >= threshold else -1 if px < previous and down >= threshold else 0
            if side and side != self.move_side:
                self.move_side = side
                messages.append(f"{WINDOW_S} 秒内从局部{'低点反弹' if side > 0 else '高点回落'} {(up if side > 0 else down):.2f}%")
            elif max(up, down) < threshold / 2:
                self.move_side = 0
        if not messages or ts - self.last_notice < REARM_S:
            return ""
        self.last_notice = ts
        return "；".join(messages)
