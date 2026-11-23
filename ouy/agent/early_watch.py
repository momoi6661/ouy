"""转折确认之后，盘中报价对那个摆动点的失守 / 收回。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


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
