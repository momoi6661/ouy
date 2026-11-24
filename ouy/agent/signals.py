"""快判：程序每隔几秒用 1 分钟 K 线判断一次「盘是不是朝一个方向动了」，并把入场、止损、目标算好。

这里只有计算，没有模型、没有下单，一次判断是毫秒级的。三种信号，都只认已经收盘的 K 线（不追没走完的那一根）：
- breakout 收盘突破：1 分钟 K 线收盘站到一条关键线的另一边（区间上下沿、支撑阻力、前高前低）。
- impulse 放量急动：连续几根 K 线朝一个方向走出了平时好几倍的幅度，而且带量。
- reject 假突破收回：影线刺穿关键线，收盘又回到线的这一边，反着做。

出了信号怎么办由盯盘那一层决定：这个方向 Worker（或用户）事先授权过，就直接照这里算好的价位下单；
没有授权就把信号连同算好的价位交给 Worker 核实。算出来的计划和 Worker 自己写的计划走同一套核对（净盈亏比、风控）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..indicators import Candle, atr as atr_series
from ..models import TradePlan

KIND = {"breakout": "收盘突破", "impulse": "放量急动", "reject": "假突破收回", "retest": "变盘后回踩"}
SIDE = {"long": "做多", "short": "做空"}


@dataclass
class FastParams:
    """快判的门槛。默认值是用真实的 1 分钟历史 K 线回测调出来的（python -m ouy.backtest），不要凭感觉改。"""

    vol_ratio: float = 1.5  # 「带量」：这根（这几根）的量至少是前 20 根平均的多少倍
    break_atr: float = 0.25  # 收盘要越过关键线至少多少个 1 分钟 ATR 才算突破，贴着线不算
    chase_atr: float = 2.0  # 现价离关键线超过这么多个 ATR 就不追了
    impulse_bars: int = 3  # 急动看最近几根
    impulse_atr: float = 3.0  # 这几根合起来至少走出多少个 ATR
    impulse_pct: float = 0.25  # 而且至少是价格的百分之多少
    wick_ratio: float = 0.5  # 假突破：影线至少占整根 K 线的多少
    stop_atr: float = 1.2  # 止损放在关键线外侧多少个基准 ATR
    min_stop_pct: float = 0.25  # 止损离入场至少这么远（%），再近手续费占比就太高
    target_r: float = 3.0  # 前面没有够远的关键线时，按止损距离的多少倍定脉冲目标
    min_strength: int = 2  # 至少凑够几分才算信号（1 = 只有形态；带量 +1；顺大方向 +1；逆大方向 -1）
    kinds: tuple[str, ...] = ("breakout", "impulse", "reject", "retest")  # 用哪几种信号
    retest_atr: float = 0.5  # 变盘后回踩：K 线回到被突破的摆动点多近算踩到（几个 5 分钟 ATR）
    retest_room: float = 1.0  # 回踩时离这一段的极值至少还有几个 5 分钟 ATR 的空间，不然没肉
    shift_trend: bool = True  # 最近走势变过的话，大方向按变了之后的算（而不是还按 1 小时的老方向）


@dataclass
class Signal:
    side: str  # long / short
    kind: str  # breakout / impulse / reject
    strength: int
    level: float  # 相关的关键线价位（急动没有关键线时是起动的价位）
    level_name: str
    bar_ts: int  # 出信号的那根 K 线：同一根只出一次
    entry: float
    stop: float
    targets: list[float] = field(default_factory=list)
    why: str = ""

    def text(self) -> str:
        return (f"{KIND[self.kind]}·{SIDE[self.side]}（{self.strength} 分）：{self.why}。程序算好的价位：现价 {self.entry:g} 进，"
                f"止损 {self.stop:g}，目标 {' / '.join(f'{t:g}' for t in self.targets)}")

    def plan(self, leverage: int, risk_pct: float | None = None) -> TradePlan:
        return TradePlan(
            direction=self.side, entry_low=self.entry, entry_high=self.entry, stop_loss=self.stop, take_profits=self.targets, leverage=leverage,
            entry_type="now", risk_pct=risk_pct, invalidation=f"价格回到 {self.level_name or '起动位'} {self.level:g} 的另一边", exit_condition="",
        )


def _sig(v: float) -> float:
    return float(f"{v:.6g}")


def _levels_beyond(levels: dict[str, float], px: float, long: bool, gap: float) -> list[float]:
    """入场价前面（做多是上方）、离得不算太近的关键线，由近到远。"""
    vals = sorted({v for v in levels.values() if (v - px if long else px - v) >= gap}, reverse=not long)
    return vals


def _finish(side: str, kind: str, strength: int, level: float, name: str, bar: Candle, px: float, stop: float, levels: dict[str, float], p: FastParams, why: str,
            targets: list[float] | None = None) -> Signal:
    long = side == "long"
    dist = max(abs(px - stop), px * p.min_stop_pct / 100)  # 止损太近就放宽到最小距离
    stop = px - dist if long else px + dist
    ahead = targets or _levels_beyond(levels, px, long, dist * 0.6)
    t1 = ahead[0] if ahead else (px + dist * p.target_r if long else px - dist * p.target_r)
    t2 = ahead[1] if len(ahead) > 1 else (t1 + dist if long else t1 - dist)
    return Signal(side, kind, strength, _sig(level), name, bar.ts, _sig(px), _sig(stop), [_sig(t1), _sig(t2)], why)


def detect(bars: Sequence[Candle], px: float, levels: dict[str, float], *, atr5: float | None = None, trend: int = 0, p: FastParams | None = None,
           shift: dict[str, Any] | None = None) -> Signal | None:
    """bars：1 分钟 K 线（旧 → 新，最后一根可以是没走完的）；px：现价；levels：关键线名字 → 价位；
    atr5：5 分钟 ATR（定止损用，没有就用 1 分钟 ATR 的 2 倍）；trend：大方向（1 上 / 0 震荡 / -1 下）；
    shift：5 分钟图上最近一次走势变化（structure_shift 的结果），有的话大方向按变了之后的算，并且多一种信号——变盘后第一次回踩。"""
    p = p or FastParams()
    if shift and p.shift_trend:
        trend = 1 if shift["side"] == "long" else -1
    closed = [c for c in bars if c.confirmed]
    if len(closed) < 30 or not px > 0:
        return None
    last, prev = closed[-1], closed[-2]
    series = atr_series(closed)
    a1 = series[-1 - p.impulse_bars] or series[-1]  # 用急动之前的波动做基准，不让这几根自己把基准抬高
    if not a1:
        return None
    a5 = atr5 or a1 * 2
    avg = sum(c.volume for c in closed[-21 - p.impulse_bars:-p.impulse_bars]) / 21 or 0.0
    ratio = last.volume / avg if avg else 0.0
    body, rng = abs(last.close - last.open), last.high - last.low

    def score(side: str, loud: bool) -> int:
        way = 1 if side == "long" else -1
        return 1 + int(loud) + (1 if trend == way else -1 if trend == -way else 0)

    found: list[Signal] = []
    # 1. 收盘突破：上一根还在线的这一边，这一根收盘站到了另一边，而且现价还在那一边、没有冲得太远
    # 次级小摆动支撑/阻力必须同时放量且顺大方向（避免 5m 噪音频繁假突破）；箱体上下沿与多小时前高前低按常规评分
    for name, lv in levels.items():
        minor_level = name.startswith(("支撑", "阻力"))
        for side in ("long", "short"):
            long = side == "long"
            crossed = prev.close <= lv < last.close if long else prev.close >= lv > last.close
            past = (last.close - lv) if long else (lv - last.close)
            if not crossed or past < p.break_atr * a1 or (last.close > last.open) != long:
                continue
            if not ((px > lv) if long else (px < lv)) or abs(px - lv) > p.chase_atr * a1:
                continue
            loud = ratio >= p.vol_ratio
            sc = score(side, loud)
            if minor_level and (not loud or sc < 3):
                continue
            stop = lv - p.stop_atr * a5 if long else lv + p.stop_atr * a5
            why = f"1 分钟 K 线收在 {name} {lv:g} {'上' if long else '下'}方（收 {last.close:g}，量是平时的 {ratio:.1f} 倍）"
            found.append(_finish(side, "breakout", sc, lv, name, last, px, stop, levels, p, why))
    # 2. 假突破收回：影线刺穿关键线，收盘回到线的这一边，反着做
    if rng > 0 and rng >= 0.8 * a1:
        up_wick, low_wick = last.high - max(last.open, last.close), min(last.open, last.close) - last.low
        for name, lv in levels.items():
            if last.high > lv > last.close and up_wick >= p.wick_ratio * rng and px < lv and prev.close < lv:
                why = f"上冲到 {last.high:g} 刺穿 {name} {lv:g} 后收回到 {last.close:g}"
                found.append(_finish("short", "reject", score("short", ratio >= p.vol_ratio), lv, name, last, px, last.high + 0.2 * a1, levels, p, why))
            if last.low < lv < last.close and low_wick >= p.wick_ratio * rng and px > lv and prev.close > lv:
                why = f"下探到 {last.low:g} 刺穿 {name} {lv:g} 后收回到 {last.close:g}"
                found.append(_finish("long", "reject", score("long", ratio >= p.vol_ratio), lv, name, last, px, last.low - 0.2 * a1, levels, p, why))
    # 3. 放量急动：最近几根合起来朝一个方向走出了平时好几倍的幅度
    run = closed[-p.impulse_bars:]
    base = closed[-p.impulse_bars - 1].close
    move = last.close - base
    side = "long" if move > 0 else "short"
    long = side == "long"
    same = sum(1 for c in run if (c.close > c.open) == long)
    loud = avg > 0 and sum(c.volume for c in run) >= p.vol_ratio * avg * len(run)
    kept = (px - base) / move >= 0.6 if move else False  # 现价还留着这段走势的大半，没有马上吐回去
    if abs(move) >= max(p.impulse_atr * a1, base * p.impulse_pct / 100) and same >= len(run) - 1 and loud and kept:
        half = base + move * 0.5  # 回吐掉一半就说明这一下是假的
        why = f"最近 {len(run)} 分钟{'上涨' if long else '下跌'} {abs(move) / base * 100:.2f}%（平时一分钟波动 {a1 / base * 100:.2f}%），量是平时的 {sum(c.volume for c in run) / (avg * len(run)):.1f} 倍"
        found.append(_finish(side, "impulse", score(side, True), base, "", last, px, half, levels, p, why))

    # 4. 变盘后回踩：走势刚变（6 根以内），价格回到被突破的摆动点附近，且这根 1 分钟 K 线吞过前一根重新朝新方向启动（防下跌瀑布里连出假回踩点）
    if shift and 1 <= shift["bars_ago"] <= 6:
        long, lv = shift["side"] == "long", shift["level"]
        sign = 1 if long else -1
        tip = last.low if long else last.high
        near = (tip - lv) * sign <= p.retest_atr * a5 and (last.close - lv) * sign > 0 and (px - lv) * sign > 0
        room = (shift["extreme"] - px) * sign >= p.retest_room * a5
        reclaimed = (last.close > prev.high) if long else (last.close < prev.low)
        if near and room and reclaimed and (last.close > last.open) == long:
            leg = abs(shift["extreme"] - shift["origin"])
            why = f"走势已转向{'上' if long else '下'}（{shift['why']}），现在回踩到 {lv:g} 附近（{'低' if long else '高'}点 {tip:g}）后吞过前一根重新启动"
            found.append(_finish(shift["side"], "retest", score(shift["side"], ratio >= p.vol_ratio), lv, "被突破的摆动点", last, px, lv - sign * p.stop_atr * a5, levels, p, why,
                                 targets=[shift["extreme"], shift["extreme"] + sign * leg * 0.5]))

    found = [s for s in found if s.strength >= p.min_strength and s.kind in p.kinds]
    if not found:
        return None
    order = {"retest": 0, "breakout": 1, "reject": 2, "impulse": 3}
    return max(found, key=lambda s: (s.strength, -order[s.kind]))


def strengths(sig: Signal | None) -> dict[str, Any]:
    """给触发线用的两项读数：现在做多、做空的快判各有几分（没有信号是 0）。"""
    return {"sig_long": sig.strength if sig and sig.side == "long" else 0, "sig_short": sig.strength if sig and sig.side == "short" else 0}


def key_lines(m5: Sequence[Candle], h1: Sequence[Candle], extra: dict[str, float] | None = None) -> tuple[dict[str, float], str, float | None]:
    """关键线：5 分钟图的区间上下沿、支撑阻力，加上一段（30 分钟 / 1 小时 / 4 小时）的高低点。
    返回（线名 → 价位，1 小时的走向描述，5 分钟 ATR）。盯盘和回测用的是同一个函数。"""
    from ..indicators import read_chart, window_levels

    chart, wins = read_chart(m5), window_levels({"5m": m5, "1H": h1})
    levels: dict[str, float] = {}
    for name, w in (wins or {}).items():
        if isinstance(w, dict) and w.get("prev_high") and w.get("prev_low"):
            levels[f"上一个 {name} 高点"], levels[f"上一个 {name} 低点"] = float(w["prev_high"]), float(w["prev_low"])  # type: ignore[arg-type]
    rng = chart.get("range") or {}
    if rng.get("upper") and rng.get("lower"):  # type: ignore[union-attr]
        levels["区间上沿"], levels["区间下沿"] = float(rng["upper"]), float(rng["lower"])  # type: ignore[index]
    for kind, label in (("resistance", "阻力"), ("support", "支撑")):
        for i, row in enumerate((chart.get(kind) or [])[:2]):  # type: ignore[index]
            levels[f"{label} {i + 1}"] = float(row["price"])
    levels.update({k: float(v) for k, v in (extra or {}).items() if v})  # 价值区间、开盘价、开盘区间：盯盘时由调用方算好传进来
    closed = [c for c in m5 if c.confirmed]
    return levels, str(((wins or {}).get("1H") or {}).get("trend") or ""), (atr_series(closed)[-1] if len(closed) > 15 else None)


# ---------- 走势变没变、之前的线还到不到得了 ----------

SHIFT_LOOKBACK = 12  # 往回看多少根 K 线找「走势是在哪一根变的」
SHIFT_BREAK_ATR = 0.3  # 收盘要越过摆动点至少多少个 ATR 才算破
SHIFT_MOVE_ATR = 2.5  # 这一段要走出多少个 ATR 才算有力度（不是磨过去的）


def chart_trend_marks(bars: Sequence[Candle], wing: int = 2, max_marks: int = 12) -> list[dict[str, Any]]:
    """扫描当前图表的已收盘 K 线，标出算法判断出走势的关键位置：方向（上/下）以及是否是延续走势（BOS 延续 vs CHoCH 转折）。"""
    from ..indicators import swings

    closed = [c for c in bars if c.confirmed]
    if len(closed) < 35:
        return []
    series = atr_series(closed)
    marks: list[dict[str, Any]] = []
    trend = ""  # 当前已确立的结构方向："long" / "short" / ""
    last_pivot_ts: dict[str, int] = {}
    start = max(25, len(closed) - 110)
    for i in range(start, len(closed)):
        bar, a = closed[i], series[i - 1]
        if not a:
            continue
        highs, lows = swings(closed[:i], wing)
        if len(highs) < 2 or len(lows) < 2:
            continue
        for long in (True, False):
            side = "long" if long else "short"
            piv_idx, pivot = highs[-1] if long else lows[-1]
            sign = 1 if long else -1
            if (bar.close - pivot) * sign < SHIFT_BREAK_ATR * a or (closed[i - 1].close - pivot) * sign >= SHIFT_BREAK_ATR * a:
                continue
            piv_ts = closed[piv_idx].ts
            if last_pivot_ts.get(side) == piv_ts:
                continue  # 同一个摆动点只标一次
            last_pivot_ts[side] = piv_ts
            # 判断之前结构是否已在同方向（延续）还是从反方向反转过来（转折）
            if trend:
                cont = trend == side
            else:
                cont = (highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]) if long else (highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1])
            trend = side
            dir_txt = "上" if long else "下"
            kind_txt = "延续" if cont else "转折"
            marks.append({
                "ts": bar.ts,
                "side": side,
                "continuation": cont,
                "level": round(pivot, 8),
                "label": f"{dir_txt}·{kind_txt}",
            })
    return marks[-max_marks:]


def structure_shift(bars: Sequence[Candle], wing: int = 2) -> dict[str, Any] | None:
    """最近走势有没有变：K 线收盘有力地越过了最近一个摆动高点（转向上）或摆动低点（转向下），而且到现在还没被收回去。
    之前是反着的结构（越过高点之前高低点都在降低）叫「反转」，本来就同向的叫「延续」。只认已收盘的 K 线。
    返回 {side, kind, level 被越过的那个摆动点, ts 是哪一根变的, bars_ago, origin 这一段从哪里起动, extreme 走到过哪里, why}；没变返回 None。"""
    from ..indicators import swings

    closed = [c for c in bars if c.confirmed]
    if len(closed) < 40:
        return None
    series = atr_series(closed)
    for i in range(len(closed) - 1, max(len(closed) - 1 - SHIFT_LOOKBACK, 30), -1):  # 从最近的一根往回找
        bar, a = closed[i], series[i - 1]
        highs, lows = swings(closed[:i], wing)
        if not a or len(highs) < 2 or len(lows) < 2:
            continue
        for long in (True, False):
            pivot = highs[-1][1] if long else lows[-1][1]
            sign = 1 if long else -1
            if (bar.close - pivot) * sign < SHIFT_BREAK_ATR * a or (closed[i - 1].close - pivot) * sign >= SHIFT_BREAK_ATR * a:
                continue  # 这一根没有越过，或者上一根就已经在外面了（不是在这一根变的）
            start = (lows[-1] if long else highs[-1])  # 这一段从最近的反向摆动点起动
            origin = min(c.low for c in closed[start[0]:i + 1]) if long else max(c.high for c in closed[start[0]:i + 1])
            if (bar.close - origin) * sign < SHIFT_MOVE_ATR * a:
                continue
            after = closed[i:]
            if any((c.close - pivot) * sign < -SHIFT_BREAK_ATR * a for c in after):
                continue  # 之后又收回到摆动点的另一边：这次没变成
            against = (highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]) if long else (highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1])
            extreme = max(c.high for c in after) if long else min(c.low for c in after)
            word, kind = ("上" if long else "下"), ("反转" if against else "延续")
            return {
                "side": "long" if long else "short", "kind": kind, "level": _sig(pivot), "ts": bar.ts, "bars_ago": len(closed) - 1 - i,
                "origin": _sig(origin), "extreme": _sig(extreme),
                "why": f"收盘{'站上' if long else '跌破'}前一个摆动{'高' if long else '低'}点 {_sig(pivot):g}，这一段从 {_sig(origin):g} 走到 {_sig(extreme):g}"
                       f"（{abs(extreme - origin) / a:.1f} 个 ATR）；{'之前高低点都在' + ('降低' if long else '抬高') + '，这是转向' + word if against else '顺着原来的方向继续向' + word}",
            }
    return None


def reach(px: float, level: float, atr: float | None, minutes: float, bar_minutes: float = 5) -> float:
    """一条线离现价有多远，换算成「这段时间里价格平常能走的距离」的倍数：1 以内随时可能碰到，2 以上基本到不了。
    平常能走的距离 = ATR × √(这段时间有几根 K 线)。"""
    if not atr or not px or minutes <= 0:
        return 0.0
    return abs(px - level) / (atr * max(minutes / bar_minutes, 1) ** 0.5)


def line_status(px: float, level: float, side: str, shift: dict[str, Any] | None, atr: float | None, minutes: float, bar_minutes: float = 5) -> str:
    """一条等着进场的线（side 方向的入场价 level）现在还等不等得到。返回空串表示还行，否则是过时的原因。
    两种过时：走势已经朝反方向变了、这条线留在了起动点后面；或者单纯离得太远，这段时间里走不到。"""
    far = reach(px, level, atr, minutes, bar_minutes)
    if shift:
        long = shift["side"] == "long"
        behind = (level <= shift["level"]) if long else (level >= shift["level"])  # 线在被突破的摆动点后面
        if behind and side != shift["side"]:
            return f"走势已经转向{'上' if long else '下'}（{shift['why']}），逆着新方向等在 {level:g} 的单不该再接"
        if behind and far >= 1.0:
            return f"走势已经转向{'上' if long else '下'}，价格离开了 {level:g} 这一带（要回来得走 {far:.1f} 倍的平常波动），到不了了"
    if far >= 2.0:
        return f"离现价太远：这段时间里价格平常走不了这么远（{far:.1f} 倍）"
    return ""


def describe_tape(
    m5: Sequence[Candle],
    levels: dict[str, float],
    shift: dict[str, Any] | None,
    trend_word: str = "",
    mode: str = "auto",
    bias: str = "both",
    px: float = 0.0,
) -> str:
    """把冷冰冰的指标和 K 线序列翻译成一句任何人都能看懂的白话盘面诊断：
    比如连跌几根后转阳是不是该追涨、还是只是下跌喘口气、还是等回踩台阶再挂单。"""
    closed = [c for c in m5 if c.confirmed]
    if len(closed) < 6:
        return "正在积累 K 线数据…"
    last = closed[-1]
    now_px = px or last.close
    last_bull = last.close >= last.open
    # 数最新一根同向连了几根，以及它前面反向连了几根（识别「连跌 N 根后转阳」或「连涨 N 根后转阴」）
    cur_run = 1
    for c in reversed(closed[:-1]):
        if (c.close >= c.open) == last_bull:
            cur_run += 1
        else:
            break
    prev_run = 0
    for c in reversed(closed[: len(closed) - cur_run]):
        if (c.close >= c.open) != last_bull:
            prev_run += 1
        else:
            break

    lo, hi = levels.get("区间下沿"), levels.get("区间上沿")
    rpos = (now_px - lo) / (hi - lo) * 100 if lo and hi and hi > lo else 50.0
    big = "1H 上涨" if trend_word.startswith("上涨") else "1H 下跌" if trend_word.startswith("下跌") else "1H 震荡"

    if mode == "quiet":
        return f"{big}｜当前挂在「静默防守」档，暂停新开仓信号，只保护已有仓位并自动清理过时挂单。"

    # 情形 1：连跌/连涨 ≥ 3 根后刚反向收了 1~2 根
    if prev_run >= 3 and cur_run <= 2:
        if last_bull:
            if shift and shift["side"] == "long" and shift["bars_ago"] <= 3:
                body = f"5m 连跌 {prev_run} 根后转阳站上前高 {shift['level']:g}（跌势已反转）：离突破位近可直接顺势跟多（now），偏远则等回踩 {shift['level']:g} 附近挂单接多"
            elif lo and rpos <= 25:
                body = f"5m 连跌 {prev_run} 根在箱体下沿（{lo:g} 附近）收阳承接：震荡局可依托下沿外止损做小波段反弹多（目标看中轨/上沿）"
            else:
                body = f"5m 连续 {prev_run} 根阴线后刚收阳反弹，但尚未越过前高压力——属于下跌喘气反弹，不能当反转追涨，关注受阻顺空或破前高再转多"
        else:
            if shift and shift["side"] == "short" and shift["bars_ago"] <= 3:
                body = f"5m 连涨 {prev_run} 根后转阴跌破前低 {shift['level']:g}（涨势已转空）：离破位近可直接顺势跟空（now），偏远则在 {shift['level']:g} 浅挂空"
            elif hi and rpos >= 75:
                body = f"5m 连涨 {prev_run} 根在箱体上沿（{hi:g} 附近）收阴受阻：可依托上沿外止损做小波段回落空（目标看中轨/下沿）"
            else:
                body = f"5m 连续 {prev_run} 根阳线后收阴回调，尚未跌破前低支撑——暂按上涨途中浅回踩看待，关注企稳接多"
    # 情形 2：正在走连续 ≥ 3 根单边阳线或阴线（区分起爆推进 vs 末端撞墙）
    elif cur_run >= 3:
        if last_bull:
            body = (
                f"5m 走出 {cur_run} 连阳推进（区间 {rpos:.0f}% 处）："
                + ("已连拉至箱体极值上沿，看能否放量突破打开大波段（breakout），受阻收阴则做回落小波段" if cur_run >= 5 and rpos >= 82
                   else "单边向上动能正在释放，顺势用现价或 EMA20 浅回踩跟进吃波段，勿死挂远端老低点踏空")
            )
        else:
            body = (
                f"5m 走出 {cur_run} 连阴下泄（区间 {rpos:.0f}% 处）："
                + ("已急跌至箱体极值下沿，看放量破位延续（breakout）或止跌承接小反弹，不盲目追最后一棒" if cur_run >= 5 and rpos <= 18
                   else "单边向下动能正在释放，顺势用现价或 EMA20 浅反弹跟空，勿死挂远端老高点踏空")
            )
    # 情形 3：刚发生结构转折不久
    elif shift and shift["bars_ago"] <= 6:
        way = "多" if shift["side"] == "long" else "空"
        act = "浅回踩接多" if shift["side"] == "long" else "浅反抽接空"
        body = f"5m 刚确认转{way}（{shift['kind']}越过 {shift['level']:g}），现价 {now_px:g}：离 {shift['level']:g} 近可直接跟{way}，跑远了则盯 EMA20 或 {shift['level']:g} 一带{act}"
    # 情形 4：常态震荡或窄幅蓄势
    else:
        edge = f"贴近箱体上沿 {hi:g}，观察放量突破或受阻短空" if hi and rpos >= 80 else f"贴近箱体下沿 {lo:g}，观察支撑低吸或放量下破" if lo and rpos <= 20 else f"处于箱体中部（{rpos:.0f}%），窄幅收口时在上下沿预埋突破单，宽震荡等两侧边缘"
        body = f"5m 横盘蓄势或拉锯，{edge}"

    bias_tag = "｜只盯做多" if bias == "long" else "｜只盯做空" if bias == "short" else ""
    return f"{big}{bias_tag}｜{body}。"


# ---------- 转折雷达：扫流动性 → 动能背离 → 结构反转确认 → 等回踩 ----------

TURN_LOOKBACK = 36  # 最近多少根 5 分钟 K 线里找扫单（3 小时）
TURN_REF_BARS = 48  # 扫的是扫单之前多少根的最高 / 最低点（4 小时）


def turn_watch(bars: Sequence[Candle], shift: dict[str, Any] | None, px: float = 0.0) -> dict[str, Any] | None:
    """转折盘按顺序要过三关，全部只看已收盘 K 线：
    ① 扫流动性：刺穿之前 4 小时的最低点（看涨）/ 最高点（看跌），收盘又收回来，而且之后没有再创新低 / 新高；
    ② 动能背离（加分项）：价格创了新低，RSI 却比上一个低点高（看跌反过来）；
    ③ 结构反转确认：扫单之后，收盘反向越过最近的摆动点（structure_shift，kind=反转）。
    过了第三关才算转折确认：入场等回踩被越过的摆动点，止损放在扫单极值外侧。返回进度最靠前的一组，没有就 None。"""
    from ..indicators import rsi

    closed = [c for c in bars if c.confirmed]
    if len(closed) < TURN_REF_BARS + 20:
        return None
    a = atr_series(closed)[-1]
    if not a:
        return None
    rs = rsi([c.close for c in closed])
    now = px or closed[-1].close
    found: list[dict[str, Any]] = []
    for long in (True, False):
        sign = 1 if long else -1
        for i in range(len(closed) - 1, max(len(closed) - 1 - TURN_LOOKBACK, TURN_REF_BARS), -1):
            bar, before = closed[i], closed[i - TURN_REF_BARS:i]
            j = min(range(len(before)), key=lambda k: before[k].low) if long else max(range(len(before)), key=lambda k: before[k].high)
            ref = before[j].low if long else before[j].high
            tip = bar.low if long else bar.high
            if (ref - tip) * sign < 0.05 * a or (bar.close - ref) * sign <= 0:
                continue  # 没刺穿，或者收盘没收回来
            after = closed[i + 1:]
            if any(((c.low if long else c.high) - tip) * sign < 0 for c in after):
                break  # 之后又创了新低 / 新高：这次扫单失败，不再往前找更早的
            ri, rj = rs[i], rs[i - TURN_REF_BARS + j]
            div = ri is not None and rj is not None and (ri - rj) * sign > 2
            choch = shift if shift and shift.get("side") == ("long" if long else "short") and shift.get("kind") == "反转" and shift.get("ts", 0) > bar.ts else None
            stage = 3 if choch else 2 if div else 1
            word = "看涨" if long else "看跌"
            row: dict[str, Any] = {
                "side": "long" if long else "short", "stage": stage, "ts": bar.ts, "sweep": _sig(tip), "ref": _sig(ref), "divergence": div,
                "why": f"扫掉前 4 小时{'低' if long else '高'}点 {_sig(ref):g}（刺到 {_sig(tip):g}）后收回" + ("，RSI 背离" if div else ""),
            }
            if choch:
                entry, stop = float(choch["level"]), _sig(tip - sign * 0.2 * a)
                dist = abs(now - entry) / a
                status = "正在回踩区" if dist <= 0.5 else "已经走远，不追" if dist > 2.5 else f"等回踩 {entry:g}"
                row.update(entry=_sig(entry), stop=stop, choch_ts=choch["ts"], status=status,
                           why=row["why"] + f"，随后收盘{'站上' if long else '跌破'}摆动点 {entry:g}（结构反转）")
                row["text"] = f"{word}转折已确认：{row['why']}。{status}；止损放在扫单极值外 {stop:g}"
            else:
                row["text"] = f"{word}转折第 {stage} 关：{row['why']}。还没有收盘{'站上' if long else '跌破'}最近的摆动点，不算转折，不抄底摸顶"
            found.append(row)
            break
    return max(found, key=lambda r: (r["stage"], r["ts"])) if found else None


# ---------- 转折：苗头 → 确认 → 盘中失守 / 收回，三个阶段合成一个读数 ----------
TURN_STAGES = {"early": "苗头", "confirmed": "确认", "lost": "盘中失守", "reclaimed": "盘中收回"}


def turn_state(shift: dict[str, Any] | None, radar: dict[str, Any] | None, retest: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """「转折」这一个算法现在走到哪一步。三样原料：radar 是 turn_watch 的结果（扫流动性 / 背离，还没确认时叫苗头），
    shift 是 structure_shift 的结果（收盘越过摆动点 = 确认），retest 是确认之后盘中报价对那个摆动点的失守 / 收回。
    以最新发生的那一步为准：确认之后又出现反方向的苗头，就报苗头并带上之前确认的方向。没有任何一步返回 None。"""
    early = radar if radar and radar.get("stage", 0) < 3 else None
    if shift and early and early.get("ts", 0) <= shift.get("ts", 0):
        early = None  # 苗头比确认还早：已经被确认盖过去了
    if not shift and not early:
        return None
    if early:
        text = early["text"] + (f"（之前{'转多' if shift['side'] == 'long' else '转空'}的确认在 {shift['level']:g}，还没被推翻）" if shift else "")
        return {"stage": "early", "label": TURN_STAGES["early"], "side": early["side"], "ts": early["ts"], "level": early.get("ref"), "text": text,
                "confirmed": shift}
    assert shift is not None
    swept = radar if radar and radar.get("stage") == 3 and radar.get("choch_ts") == shift.get("ts") else None
    mine = retest if retest and retest.get("level") == shift.get("level") and retest.get("ts", 0) >= shift.get("ts", 0) else None
    stage = "confirmed" if not mine else "lost" if mine.get("value") else "reclaimed"
    text = f"{'转多' if shift['side'] == 'long' else '转空'}（{shift['kind']}）：{shift['why']}" + (f"；之前{swept['why']}" if swept else "")
    if mine:
        text += f"。盘中：{mine['text']}"
    return {"stage": stage, "label": TURN_STAGES[stage], "side": shift["side"], "kind": shift["kind"], "ts": (mine or shift)["ts"], "level": shift["level"],
            "origin": shift.get("origin"), "bars_ago": shift.get("bars_ago"), "text": text, "confirmed": shift,
            **({"entry": swept["entry"], "stop": swept["stop"], "status": swept["status"]} if swept else {})}


RUN_BARS = 3  # 至少连着几根
RUN_ATR = 3.0  # 每走出这么多倍平均波幅算一档


def trend_run(bars: Sequence[Candle]) -> dict[str, Any] | None:
    """连续走线：最近几根已收盘 K 线的收盘价一根接一根朝同一个方向走，而且合起来走出了平时好几根的幅度。

    只说明「正在连着走」，不说明接下来往哪走。step 是走到第几档（每 RUN_ATR 倍平均波幅一档），同一段走线每升一档才算一次新事件。"""
    closed = [c for c in bars if c.confirmed]
    if len(closed) < 14 + RUN_BARS + 2:
        return None
    sign = (closed[-1].close > closed[-2].close) - (closed[-1].close < closed[-2].close)
    if not sign:
        return None
    start = len(closed) - 1
    while start > 0 and (closed[start].close - closed[start - 1].close) * sign > 0:
        start -= 1
    count = len(closed) - 1 - start
    base = atr_series(closed[: start + 1])[-1] if start >= 14 else None  # 用这段走线之前的波动做基准，不让它自己把基准抬高
    if count < RUN_BARS or not base:
        return None
    origin, last = closed[start].close, closed[-1].close
    size = abs(last - origin) / base
    if size < RUN_ATR:
        return None
    return {"side": "long" if sign > 0 else "short", "bars": count, "origin": origin, "to": last, "pct": (last / origin - 1) * 100,
            "atr_x": size, "step": int(size / RUN_ATR), "ts": closed[start].ts, "bar_ts": closed[-1].ts}
