"""盘中实时报价跟踪：已确认转折的失守/收回，以及秒级拐点、走向与幅度记录器（WaveTracker）。"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class WaveTracker:
    """盘中实时折线（ZigZag 拐点、走向、延续判定与实时涨速记录器）：
    - 按动态反转阈值（max(0.18%, 0.35 × 5m ATR)）+ 2 秒抗插针驻留确认（hold_ms）过滤盘口 0.2s 毛刺假针；
    - 滑动窗口实时计算近 10s / 30s / 60s 涨跌幅（chg_10s / chg_30s / chg_60s）与急涨急跌启动；
    - 明确区分「走势延续（wave_cont=1）」与「新转折（wave_cont=0）」。"""

    dir: int = 0
    anchor_px: float = 0.0
    anchor_ts: int = 0
    extreme_px: float = 0.0
    extreme_ts: int = 0
    prev_pct: float = 0.0
    prev_peak: float = 0.0
    last_peak: float = 0.0
    last_peak_ts: int = 0
    prev_trough: float = 0.0
    last_trough: float = 0.0
    last_trough_ts: int = 0
    turned_ts: int = 0
    reported_trend: str = ""
    hold_ms: int = 2000  # 高频 WS 报价拐头需连续站稳 2 秒防 0.2s 刺针；单次采样间隔 >=2s 时直接确认
    _last_ts: int = 0
    _pending_dir: int = 0
    _pending_ts: int = 0
    _surge_at: dict[str, int] = field(default_factory=dict, repr=False)
    _ticks: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=600), repr=False)

    @staticmethod
    def threshold_pct(px: float, atr5: float | None) -> float:
        return max(0.18, (atr5 / px * 100 * 0.35) if atr5 and px > 0 else 0.20)

    def _record_tick(self, ts_ms: int, px: float) -> None:
        self._ticks.append((ts_ms, px))
        cut = ts_ms - 75_000
        while self._ticks and self._ticks[0][0] < cut:
            self._ticks.popleft()

    def _confirmed_hold(self, ts_ms: int, want_dir: int, gap_ms: int) -> bool:
        """高频实时流（间隔 < hold_ms）要求连续站稳 hold_ms 防单跳毛刺；低频/离线采样间隔本身 >= hold_ms 则立即确认。"""
        if self.hold_ms <= 0 or gap_ms >= self.hold_ms:
            self._pending_dir = 0
            self._pending_ts = 0
            return True
        if self._pending_dir != want_dir:
            self._pending_dir = want_dir
            self._pending_ts = ts_ms
            return False
        if ts_ms - self._pending_ts >= self.hold_ms:
            self._pending_dir = 0
            self._pending_ts = 0
            return True
        return False

    def _clear_pending(self) -> None:
        self._pending_dir = 0
        self._pending_ts = 0

    def update(self, ts_ms: int, px: float, atr5: float | None = None) -> bool:
        """喂入一次实时报价；若刚确认了一个新的盘中拐点则返回 True。"""
        if not math.isfinite(px) or px <= 0:
            return False
        gap_ms = (ts_ms - self._last_ts) if self._last_ts > 0 else self.hold_ms
        self._last_ts = ts_ms
        self._record_tick(ts_ms, px)
        rev = self.threshold_pct(px, atr5)
        if self.anchor_px <= 0:
            self.anchor_px = self.extreme_px = px
            self.anchor_ts = self.extreme_ts = ts_ms
            return False
        if self.dir == 0:
            move = (px / self.anchor_px - 1) * 100
            if move >= rev:
                if not self._confirmed_hold(ts_ms, 1, gap_ms):
                    return False
                self.dir = 1
                self.last_trough, self.last_trough_ts = self.anchor_px, self.anchor_ts
                self.extreme_px, self.extreme_ts = px, ts_ms
                self.turned_ts = ts_ms
                return True
            if move <= -rev:
                if not self._confirmed_hold(ts_ms, -1, gap_ms):
                    return False
                self.dir = -1
                self.last_peak, self.last_peak_ts = self.anchor_px, self.anchor_ts
                self.extreme_px, self.extreme_ts = px, ts_ms
                self.turned_ts = ts_ms
                return True
            self._clear_pending()
            return False
        if self.dir == 1:
            if px >= self.extreme_px:
                self.extreme_px, self.extreme_ts = px, ts_ms
                self._clear_pending()
                return False
            pull = (self.extreme_px - px) / self.extreme_px * 100
            if pull >= rev:
                if not self._confirmed_hold(ts_ms, -1, gap_ms):
                    return False
                self.prev_pct = round((self.extreme_px / self.anchor_px - 1) * 100, 3)
                if self.last_peak > 0:
                    self.prev_peak = self.last_peak
                self.last_peak, self.last_peak_ts = self.extreme_px, self.extreme_ts
                self.dir = -1
                self.anchor_px, self.anchor_ts = self.extreme_px, self.extreme_ts
                self.extreme_px, self.extreme_ts = px, ts_ms
                self.turned_ts = ts_ms
                return True
            self._clear_pending()
            return False
        if px <= self.extreme_px:
            self.extreme_px, self.extreme_ts = px, ts_ms
            self._clear_pending()
            return False
        bounce = (px - self.extreme_px) / self.extreme_px * 100
        if bounce >= rev:
            if not self._confirmed_hold(ts_ms, 1, gap_ms):
                return False
            self.prev_pct = round((self.extreme_px / self.anchor_px - 1) * 100, 3)
            if self.last_trough > 0:
                self.prev_trough = self.last_trough
            self.last_trough, self.last_trough_ts = self.extreme_px, self.extreme_ts
            self.dir = 1
            self.anchor_px, self.anchor_ts = self.extreme_px, self.extreme_ts
            self.extreme_px, self.extreme_ts = px, ts_ms
            self.turned_ts = ts_ms
            return True
        self._clear_pending()
        return False

    def velocity(self, seconds: int, px: float) -> float:
        """最近 N 秒内的实时涨跌幅（%）。"""
        if not self._ticks or px <= 0:
            return 0.0
        now_ts = self._ticks[-1][0]
        target_ts = now_ts - seconds * 1000
        base_px = self._ticks[0][1]
        if now_ts - self._ticks[0][0] < seconds * 400:
            return 0.0  # 采样时长还不足窗口的 40%，避免刚启动时虚高
        for ts, p in self._ticks:
            if ts >= target_ts:
                base_px = p
                break
        return round((px / base_px - 1) * 100, 3) if base_px > 0 else 0.0

    def is_continuation(self, px: float) -> bool:
        """当前推进是否属于同向延续走势（越过前波峰/波谷，且前高/前低本身已在同向抬高/降低）。"""
        if self.dir == 1:
            return bool(self.prev_trough > 0 and self.last_trough >= self.prev_trough and self.last_peak > 0 and px >= self.last_peak)
        if self.dir == -1:
            return bool(self.prev_peak > 0 and 0 < self.last_peak <= self.prev_peak and self.last_trough > 0 and px <= self.last_trough)
        return False

    def surge_event(self, ts_ms: int, px: float, atr5: float | None = None) -> tuple[str, str] | None:
        """盘中急拉/急砸或整段反包异动检测（30s 急冲、60s 单边冲刺、或单波 105%+ 反包前段；同方向 90 秒冷却防刷屏）。"""
        if px <= 0:
            return None
        atr_pct = (atr5 / px * 100) if atr5 and px > 0 else 0.30
        c30 = self.velocity(30, px)
        c60 = self.velocity(60, px)
        r = self.readings(px)
        th30 = max(0.35, atr_pct * 0.90)
        th60 = max(0.40, atr_pct * 1.05)
        th_engulf = max(0.30, atr_pct * 0.85)
        engulf = self.dir in (1, -1) and r["retrace_pct"] >= 105.0 and abs(r["wave_move_pct"]) >= th_engulf
        if abs(c30) >= th30:
            side, kind_hit = ("long" if c30 > 0 else "short"), "30秒急拉" if c30 > 0 else "30秒急砸"
        elif abs(c60) >= th60:
            side, kind_hit = ("long" if c60 > 0 else "short"), "1分钟急拉" if c60 > 0 else "1分钟急砸"
        elif engulf:
            side, kind_hit = ("long" if self.dir == 1 else "short"), "盘中反包大涨" if self.dir == 1 else "盘中反包急跌"
        else:
            return None
        if ts_ms - self._surge_at.get(side, 0) < 90_000:
            return None
        self._surge_at[side] = ts_ms
        c10 = self.velocity(10, px)
        return side, f"{kind_hit}（波幅 {r['wave_move_pct']:+.2f}% / 反包 {r['retrace_pct']:.0f}%，30s {c30:+.2f}% / 10s {c10:+.2f}%）：{self.summary(px)}"

    def readings(self, px: float) -> dict[str, Any]:
        c10, c30, c60 = self.velocity(10, px), self.velocity(30, px), self.velocity(60, px)
        if self.anchor_px <= 0 or px <= 0 or self.dir == 0:
            return {"wave_dir": 0, "wave_move_pct": 0.0, "retrace_pct": 0.0, "wave_cont": 0, "chg_10s": c10, "chg_30s": c30, "chg_60s": c60}
        move = round((px / self.anchor_px - 1) * 100, 3)
        retrace = round(abs(move / self.prev_pct) * 100, 1) if abs(self.prev_pct) >= 0.05 else 0.0
        return {
            "wave_dir": self.dir, "wave_move_pct": move, "retrace_pct": retrace,
            "wave_cont": 1 if self.is_continuation(px) else 0,
            "chg_10s": c10, "chg_30s": c30, "chg_60s": c60,
        }

    def summary(self, px: float) -> str:
        if self.dir == 0 or self.anchor_px <= 0 or px <= 0:
            return ""
        r = self.readings(px)
        way = "向上拐头推进" if self.dir == 1 else "向下拐头回落"
        pivot_name = "波谷" if self.dir == 1 else "波峰"
        tag = "【走势延续】" if r["wave_cont"] else "【新拐点】"
        prev = f"上一段波幅 {self.prev_pct:+.2f}%，" if self.prev_pct else ""
        ret = f"（占前段 {r['retrace_pct']:.0f}%）" if r["retrace_pct"] else ""
        peaks = []
        if self.last_peak:
            peaks.append(f"最近波峰 {self.last_peak:g}")
        if self.last_trough:
            peaks.append(f"最近波谷 {self.last_trough:g}")
        vel = f"｜近 10s {r['chg_10s']:+.2f}% / 30s {r['chg_30s']:+.2f}% / 60s {r['chg_60s']:+.2f}%" if any((r["chg_10s"], r["chg_30s"], r["chg_60s"])) else ""
        tail = f"｜{'、'.join(peaks)}" if peaks else ""
        return f"{tag}{prev}自{pivot_name} {self.anchor_px:g} {way} {r['wave_move_pct']:+.2f}%{ret}{vel}{tail}"

    def live_trend(self, px: float, atr5: float | None = None) -> str:
        """盘中实时结构走向：波峰波谷同步抬高、突破前波峰加速、或单波 100%+ 反包前段波幅均即时判出方向。"""
        if self.anchor_px <= 0 or px <= 0 or self.dir == 0:
            return ""
        atr_pct = (atr5 / px * 100) if atr5 and px > 0 else 0.30
        surge_pct = max(0.32, atr_pct * 0.95)
        engulf_pct = max(0.28, atr_pct * 0.85)
        r = self.readings(px)
        if self.dir == 1:
            hh_hl = self.prev_trough > 0 and self.last_trough > self.prev_trough and (
                (self.last_peak > 0 and px > self.last_peak) or (self.prev_peak > 0 and self.last_peak > self.prev_peak)
            )
            surge = self.last_peak > 0 and px > self.last_peak and r["wave_move_pct"] >= surge_pct
            engulf = r["retrace_pct"] >= 100.0 and r["wave_move_pct"] >= engulf_pct
            return "long" if (hh_hl or surge or engulf) else ""
        lh_ll = self.prev_peak > 0 and 0 < self.last_peak < self.prev_peak and (
            (self.last_trough > 0 and px < self.last_trough) or (self.prev_trough > 0 and 0 < self.last_trough < self.prev_trough)
        )
        plunge = self.last_trough > 0 and px < self.last_trough and -r["wave_move_pct"] >= surge_pct
        engulf = r["retrace_pct"] >= 100.0 and -r["wave_move_pct"] >= engulf_pct
        return "short" if (lh_ll or plunge or engulf) else ""

    def trend_flip(self, px: float, atr5: float | None = None) -> tuple[str, str] | None:
        """盘中实时走向首次切换到新方向时返回 (side, 描述)，同一方向不重复触发。"""
        tr = self.live_trend(px, atr5)
        if tr in ("long", "short") and tr != self.reported_trend:
            self.reported_trend = tr
            return tr, f"盘中实时走向转{'多' if tr == 'long' else '空'}：{self.summary(px)}"
        return None


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
