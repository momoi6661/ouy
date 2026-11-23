"""盘中实时报价跟踪：已确认转折的失守/收回，以及秒级拐点、走向与幅度记录器（WaveTracker）。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class WaveTracker:
    """盘中实时折线（ZigZag 拐点、走向与幅度记录器）：
    按动态反转阈值（max(0.18%, 0.35 × 5m ATR)）过滤盘口微抖动，实时记录：
    - 最近已确认的波峰 last_peak / 波谷 last_trough 及拐点时刻；
    - 上一段完整波幅 prev_pct（%）；
    - 当前推进方向 dir（1=从波谷向上拐头推进，-1=从波峰向下拐头推进，0=初始未分向）；
    - 当前从最近拐点走出的幅度 wave_move_pct（%）与相对前段的回撤/收复比例 retrace_pct（%）。
    驱动两件事：① 挂单在近区提前止跌/受阻拐头时自动撤旧追进；② 持仓顺势走出新波谷/波峰时自动推结构止损。"""

    dir: int = 0
    anchor_px: float = 0.0
    anchor_ts: int = 0
    extreme_px: float = 0.0
    extreme_ts: int = 0
    prev_pct: float = 0.0
    last_peak: float = 0.0
    last_peak_ts: int = 0
    last_trough: float = 0.0
    last_trough_ts: int = 0
    turned_ts: int = 0

    @staticmethod
    def threshold_pct(px: float, atr5: float | None) -> float:
        return max(0.18, (atr5 / px * 100 * 0.35) if atr5 and px > 0 else 0.20)

    def update(self, ts_ms: int, px: float, atr5: float | None = None) -> bool:
        """喂入一次实时报价；若刚确认了一个新的盘中拐点则返回 True。"""
        if not math.isfinite(px) or px <= 0:
            return False
        rev = self.threshold_pct(px, atr5)
        if self.anchor_px <= 0:
            self.anchor_px = self.extreme_px = px
            self.anchor_ts = self.extreme_ts = ts_ms
            return False
        if self.dir == 0:
            move = (px / self.anchor_px - 1) * 100
            if move >= rev:
                self.dir = 1
                self.last_trough, self.last_trough_ts = self.anchor_px, self.anchor_ts
                self.extreme_px, self.extreme_ts = px, ts_ms
                self.turned_ts = ts_ms
                return True
            if move <= -rev:
                self.dir = -1
                self.last_peak, self.last_peak_ts = self.anchor_px, self.anchor_ts
                self.extreme_px, self.extreme_ts = px, ts_ms
                self.turned_ts = ts_ms
                return True
            return False
        if self.dir == 1:
            if px >= self.extreme_px:
                self.extreme_px, self.extreme_ts = px, ts_ms
                return False
            pull = (self.extreme_px - px) / self.extreme_px * 100
            if pull >= rev:
                self.prev_pct = round((self.extreme_px / self.anchor_px - 1) * 100, 3)
                self.last_peak, self.last_peak_ts = self.extreme_px, self.extreme_ts
                self.dir = -1
                self.anchor_px, self.anchor_ts = self.extreme_px, self.extreme_ts
                self.extreme_px, self.extreme_ts = px, ts_ms
                self.turned_ts = ts_ms
                return True
            return False
        if px <= self.extreme_px:
            self.extreme_px, self.extreme_ts = px, ts_ms
            return False
        bounce = (px - self.extreme_px) / self.extreme_px * 100
        if bounce >= rev:
            self.prev_pct = round((self.extreme_px / self.anchor_px - 1) * 100, 3)
            self.last_trough, self.last_trough_ts = self.extreme_px, self.extreme_ts
            self.dir = 1
            self.anchor_px, self.anchor_ts = self.extreme_px, self.extreme_ts
            self.extreme_px, self.extreme_ts = px, ts_ms
            self.turned_ts = ts_ms
            return True
        return False

    def readings(self, px: float) -> dict[str, Any]:
        if self.anchor_px <= 0 or px <= 0 or self.dir == 0:
            return {"wave_dir": 0, "wave_move_pct": 0.0, "retrace_pct": 0.0}
        move = round((px / self.anchor_px - 1) * 100, 3)
        retrace = round(abs(move / self.prev_pct) * 100, 1) if abs(self.prev_pct) >= 0.05 else 0.0
        return {"wave_dir": self.dir, "wave_move_pct": move, "retrace_pct": retrace}

    def summary(self, px: float) -> str:
        if self.dir == 0 or self.anchor_px <= 0 or px <= 0:
            return ""
        r = self.readings(px)
        way = "向上拐头推进" if self.dir == 1 else "向下拐头回落"
        pivot_name = "波谷" if self.dir == 1 else "波峰"
        prev = f"上一段波幅 {self.prev_pct:+.2f}%，" if self.prev_pct else ""
        ret = f"（占前段 {r['retrace_pct']:.0f}%）" if r["retrace_pct"] else ""
        peaks = []
        if self.last_peak:
            peaks.append(f"最近波峰 {self.last_peak:g}")
        if self.last_trough:
            peaks.append(f"最近波谷 {self.last_trough:g}")
        tail = f"｜{'、'.join(peaks)}" if peaks else ""
        return f"{prev}自{pivot_name} {self.anchor_px:g} {way} {r['wave_move_pct']:+.2f}%{ret}{tail}"


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
