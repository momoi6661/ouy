"""技术指标（纯 Python，无第三方依赖）。输入均为按时间升序排列的序列。"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    confirmed: bool = True


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        out.append(total / period if i >= period - 1 else None)
    return out


def _wilder(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    avg = sum(values[:period]) / period
    out[period - 1] = avg
    for i in range(period, len(values)):
        avg = (avg * (period - 1) + values[i]) / period
        out[i] = avg
    return out


def rsi(closes: Sequence[float], period: int = 14) -> list[float | None]:
    if len(closes) < 2:
        return [None] * len(closes)
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = _wilder(gains, period), _wilder(losses, period)
    out: list[float | None] = [None]
    for g, l in zip(ag, al):
        if g is None or l is None:
            out.append(None)
        elif l == 0:
            out.append(100.0 if g > 0 else 50.0)
        else:
            out.append(100 - 100 / (1 + g / l))
    return out


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    out: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
        else:
            prev = candles[i - 1].close
            out.append(max(c.high - c.low, abs(c.high - prev), abs(c.low - prev)))
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    return _wilder(true_ranges(candles), period)


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float], list[float], list[float]]:
    f, s = ema(closes, fast), ema(closes, slow)
    line = [a - b for a, b in zip(f, s)]
    sig = ema(line, signal)
    return line, sig, [a - b for a, b in zip(line, sig)]


def bollinger(closes: Sequence[float], period: int = 20, mult: float = 2.0) -> tuple[float, float, float] | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    sd = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    return mid - mult * sd, mid, mid + mult * sd


def adx(candles: Sequence[Candle], period: int = 14) -> tuple[float, float, float] | None:
    """返回 (ADX, +DI, -DI)。"""
    if len(candles) < period * 2 + 1:
        return None
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, len(candles)):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr = _wilder(true_ranges(candles), period)
    pdm, mdm = _wilder(plus_dm, period), _wilder(minus_dm, period)
    dx: list[float] = []
    pdi = mdi = 0.0
    for t, p, m in zip(tr, pdm, mdm):
        if t is None or p is None or m is None or t == 0:
            continue
        pdi, mdi = 100 * p / t, 100 * m / t
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0)
    smoothed = _wilder(dx, period)
    if not smoothed or smoothed[-1] is None:
        return None
    return smoothed[-1], pdi, mdi


def swings(candles: Sequence[Candle], wing: int = 3) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """分形摆动点：左右各 wing 根都更低/更高。返回 (高点列表, 低点列表)，元素为 (索引, 价格)。"""
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(wing, len(candles) - wing):
        window = candles[i - wing : i + wing + 1]
        if candles[i].high == max(c.high for c in window):
            highs.append((i, candles[i].high))
        if candles[i].low == min(c.low for c in window):
            lows.append((i, candles[i].low))
    return highs, lows


def structure(highs: list[tuple[int, float]], lows: list[tuple[int, float]]) -> str:
    """用最近两个摆动高低点判断结构：HH+HL 上升，LH+LL 下降，否则震荡。"""
    if len(highs) < 2 or len(lows) < 2:
        return "unknown"
    hh, hl = highs[-1][1] > highs[-2][1], lows[-1][1] > lows[-2][1]
    if hh and hl:
        return "uptrend"
    if highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]:
        return "downtrend"
    return "range"


def summarize(candles: Sequence[Candle]) -> dict[str, object]:
    """一个周期的指标快照，喂给 Agent 用。只用已收盘 K 线算指标，最新价单独给。"""
    closed = [c for c in candles if c.confirmed]
    if len(closed) < 30:
        return {"bars": len(closed), "insufficient": True}
    closes = [c.close for c in closed]
    last = candles[-1].close
    e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1] if len(closes) >= 200 else None
    r = rsi(closes)[-1]
    a = atr(closed)[-1]
    line, sig, hist = macd(closes)
    bb = bollinger(closes)
    bb_width = (bb[2] - bb[0]) / bb[1] * 100 if bb and bb[1] else None
    bb_hist = [
        (b[2] - b[0]) / b[1] * 100
        for end in range(max(20, len(closes) - 60), len(closes) + 1)
        if (b := bollinger(closes[end - 20 : end])) and b[1]
    ]
    bb_pctile = sum(1 for w in bb_hist if w <= bb_width) / len(bb_hist) if bb_width is not None and len(bb_hist) >= 15 else None
    dmi = adx(closed)
    hs, ls = swings(closed)
    vols = [c.volume for c in closed]
    vol_avg = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else None
    recent = closed[-20:]
    shown, fixed = stable_window(candles)

    def rnd(v: float | None) -> float | None:
        return None if v is None else float(f"{v:.6g}")

    return {
        "bars": len(closed),
        "last": rnd(last),
        "last_closed_price": rnd(closed[-1].close),
        "last_closed_open_ts_ms": closed[-1].ts,
        "latest_bar_confirmed": candles[-1].confirmed,
        "ema20": rnd(e20),
        "ema50": rnd(e50),
        "ema200": rnd(e200),
        "ema_stack": "bull" if e20 > e50 and (e200 is None or e50 > e200) else "bear" if e20 < e50 and (e200 is None or e50 < e200) else "mixed",
        "rsi14": rnd(r),
        "atr14": rnd(a),
        "atr_pct": rnd(a / closes[-1] * 100) if a and closes[-1] else None,
        "macd": rnd(line[-1]),
        "macd_signal": rnd(sig[-1]),
        "macd_hist": rnd(hist[-1]),
        "macd_hist_prev": rnd(hist[-2]),
        "bb_lower": rnd(bb[0]) if bb else None,
        "bb_mid": rnd(bb[1]) if bb else None,
        "bb_upper": rnd(bb[2]) if bb else None,
        "bb_width_pct": rnd(bb_width),
        "bb_width_pctile": round(bb_pctile, 2) if bb_pctile is not None else None,
        "squeeze": bool(bb_pctile is not None and bb_pctile <= 0.45),
        "adx14": rnd(dmi[0]) if dmi else None,
        "plus_di": rnd(dmi[1]) if dmi else None,
        "minus_di": rnd(dmi[2]) if dmi else None,
        "structure": structure(hs, ls),
        "swing_highs": [rnd(p) for _, p in hs[-3:]],
        "swing_lows": [rnd(p) for _, p in ls[-3:]],
        "high_20": rnd(max(c.high for c in recent)),
        "low_20": rnd(min(c.low for c in recent)),
        "volume_ratio": rnd(vols[-1] / vol_avg) if vol_avg else None,
        "change_pct_20": rnd((closes[-1] / closed[-21].close - 1) * 100) if len(closed) >= 21 and closed[-21].close else None,
        "last_closes": [rnd(c) for c in closes[-8:]],
        **read_chart(candles, shown),
        **({"stable_rows": STABLE_ROWS} if fixed else {}),
    }


STABLE_ROWS, ROW_BLOCK = 30, 10


def stable_window(candles: Sequence[Candle]) -> tuple[int, bool]:
    """给 Agent 看多少根 K 线、前 STABLE_ROWS 根是不是「整块对齐」的。
    滚动的「最近 30 根」每收一根就整段换一遍，模型服务的提示缓存一个字都用不上；
    改成按 ROW_BLOCK 根一块对齐：前 30 根在一整块时间里一字不变（放在提示词前面，能命中缓存），块后面新收的 0~9 根和没收盘的那根跟在后面。"""
    closed = [c for c in candles if c.confirmed]
    step = closed[-1].ts - closed[-2].ts if len(closed) >= 2 else 0
    if step <= 0 or len(closed) < STABLE_ROWS + ROW_BLOCK:
        return STABLE_ROWS, False
    end = next(i for i in range(len(closed), len(closed) - ROW_BLOCK, -1) if ((closed[i].ts if i < len(closed) else closed[-1].ts + step) // step) % ROW_BLOCK == 0)
    return len(candles) - (end - STABLE_ROWS), True


# ---------- 给 Agent 读走势用：原始 K 线、区间上下沿、支撑阻力、K 线形态 ----------

def _sig(v: float) -> float:
    return float(f"{v:.6g}")


def candle_rows(candles: Sequence[Candle], n: int = 30) -> list[list[object]]:
    """最近 n 根 K 线，原样给 Agent 看走势：[时间, 开, 高, 低, 收, 量比]。量比 = 这根的量 ÷ 前 20 根的平均量。
    最后一根如果还没收盘，时间后面带「(未收)」。"""
    import time as _time

    rows: list[list[object]] = []
    start = max(len(candles) - n, 0)
    for i in range(start, len(candles)):
        c = candles[i]
        before = [x.volume for x in candles[max(i - 20, 0) : i]]
        avg = sum(before) / len(before) if before else 0
        stamp = _time.strftime("%m-%d %H:%M", _time.localtime(c.ts / 1000)) + ("" if c.confirmed else "(未收)")
        rows.append([stamp, _sig(c.open), _sig(c.high), _sig(c.low), _sig(c.close), round(c.volume / avg, 2) if avg else None])
    return rows


def key_levels(candles: Sequence[Candle], last: float, tol: float) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """支撑和阻力：把摆动高低点里挨得近的并成一个价位，数它被碰过几次。返回 (支撑由近到远, 阻力由近到远)，各最多 3 个。"""
    highs, lows = swings(candles)
    points = sorted([(p, i) for i, p in highs] + [(p, i) for i, p in lows])
    clusters: list[list[tuple[float, int]]] = []
    for point in points:
        if clusters and point[0] - clusters[-1][-1][0] <= tol:
            clusters[-1].append(point)
        else:
            clusters.append([point])
    levels = [
        {"price": _sig(sum(p for p, _ in c) / len(c)), "touches": len(c), "bars_ago": len(candles) - 1 - max(i for _, i in c)}
        for c in clusters
    ]
    support = sorted((l for l in levels if l["price"] < last), key=lambda l: -l["price"])[:3]  # type: ignore[operator]
    resistance = sorted((l for l in levels if l["price"] > last), key=lambda l: l["price"])[:3]  # type: ignore[arg-type,return-value]
    return support, resistance


def range_bounds(candles: Sequence[Candle], last: float, tol: float, n: int = 60) -> dict[str, object]:
    """最近 n 根的上沿和下沿：价格在这一段走势里的上下限，各被碰过几次，现价在区间的什么位置。"""
    recent = list(candles[-n:])
    upper, lower = max(c.high for c in recent), min(c.low for c in recent)
    width = upper - lower
    pos = (last - lower) / width * 100 if width else 50.0
    where = "已经突破上沿" if last > upper else "已经跌破下沿" if last < lower else "贴近上沿" if pos >= 85 else "贴近下沿" if pos <= 15 else "在区间中部" if 35 <= pos <= 65 else "偏上" if pos > 65 else "偏下"
    return {
        "bars": len(recent),
        "upper": _sig(upper),
        "lower": _sig(lower),
        "upper_touches": sum(1 for c in recent if upper - c.high <= tol),
        "lower_touches": sum(1 for c in recent if c.low - lower <= tol),
        "width_pct": round(width / last * 100, 3) if last else None,
        "position_pct": round(pos, 1),
        "where": where,
    }


def candle_patterns(candles: Sequence[Candle], support: list[dict[str, object]], resistance: list[dict[str, object]], atr_value: float | None, tol: float) -> list[str]:
    """最近几根已收盘 K 线的形态，用一句话说清楚，并标出是不是出现在支撑 / 阻力位上（在关键位上才有意义）。"""
    if len(candles) < 25:
        return []
    c1, c2, c3 = candles[-1], candles[-2], candles[-3]
    out: list[str] = []

    def parts(c: Candle) -> tuple[float, float, float, float]:
        body, rng = abs(c.close - c.open), c.high - c.low
        return body, rng, c.high - max(c.open, c.close), min(c.open, c.close) - c.low

    b1, r1, up1, low1 = parts(c1)
    b2, _, _, _ = parts(c2)
    bull1, bull2, bull3 = c1.close > c1.open, c2.close > c2.open, c3.close > c3.open
    big = atr_value or r1
    if r1 > 0:
        if not bull2 and bull1 and c1.close >= c2.open and c1.open <= c2.close and b1 > b2:
            out.append("看涨吞没：最新这根阳线的实体把前一根阴线整个包住，买方接管")
        elif bull2 and not bull1 and c1.close <= c2.open and c1.open >= c2.close and b1 > b2:
            out.append("看跌吞没：最新这根阴线的实体把前一根阳线整个包住，卖方接管")
        if low1 >= 2 * max(b1, r1 * 0.05) and up1 <= r1 * 0.25 and r1 >= 0.6 * big:
            out.append(f"长下影线（锤子线）：下探到 {_sig(c1.low)} 被买回来，下方有承接")
        elif up1 >= 2 * max(b1, r1 * 0.05) and low1 <= r1 * 0.25 and r1 >= 0.6 * big:
            out.append(f"长上影线（射击之星）：上冲到 {_sig(c1.high)} 被砸回来，上方有抛压")
        elif b1 <= r1 * 0.1:
            out.append("十字星：开盘和收盘几乎一样，多空僵持，等下一根选方向")
        if b1 >= r1 * 0.7 and b1 >= 1.2 * big:
            out.append(f"{'大阳线' if bull1 else '大阴线'}：实体是平时波动的 {b1 / big:.1f} 倍，{'买方' if bull1 else '卖方'}很强")
        if c1.high <= c2.high and c1.low >= c2.low:
            out.append("内包线：这根的高低点都在前一根里面，波动收缩，等突破")
    if bull1 and bull2 and bull3 and c1.close > c2.close > c3.close:
        out.append("连续 3 根阳线，收盘价一根比一根高")
    elif not bull1 and not bull2 and not bull3 and c1.close < c2.close < c3.close:
        out.append("连续 3 根阴线，收盘价一根比一根低")
    prior = candles[-22:-1]
    top, bottom = max(c.high for c in prior), min(c.low for c in prior)
    if c1.high > top and c1.close < top:
        out.append(f"假突破上沿：上探到 {_sig(c1.high)}，超过前 20 根高点 {_sig(top)}，但收盘又回到下面")
    if c1.low < bottom and c1.close > bottom:
        out.append(f"假跌破下沿：下探到 {_sig(c1.low)}，跌破前 20 根低点 {_sig(bottom)}，但收盘又回到上面")
    if out:  # 形态出现的位置：在支撑 / 阻力位上才值得当回事
        near_support = next((l for l in support if abs(c1.low - float(l["price"])) <= tol), None)  # type: ignore[arg-type]
        near_resistance = next((l for l in resistance if abs(c1.high - float(l["price"])) <= tol), None)  # type: ignore[arg-type]
        if near_support:
            out.append(f"位置：出现在支撑 {near_support['price']} 附近（这个位置之前被碰过 {near_support['touches']} 次）")
        elif near_resistance:
            out.append(f"位置：出现在阻力 {near_resistance['price']} 附近（这个位置之前被碰过 {near_resistance['touches']} 次）")
        else:
            out.append("位置：不在明显的支撑或阻力位上，形态的参考意义要打折扣")
    return out


def read_chart(candles: Sequence[Candle], n: int = 30) -> dict[str, object]:
    """一个周期的「走势」部分：原始 K 线 + 区间上下沿 + 支撑阻力 + 形态。只用已收盘的 K 线判断，没收盘的那根只在 K 线列表里给出。"""
    closed = [c for c in candles if c.confirmed]
    if len(closed) < 30:
        return {}
    last = candles[-1].close
    a = atr(closed)[-1]
    tol = max((a or 0) * 0.35, last * 0.0008)
    support, resistance = key_levels(closed[-200:], last, tol)
    return {
        "candles": candle_rows(candles, n),
        "range": range_bounds(closed, last, tol),
        "support": support,
        "resistance": resistance,
        "patterns": candle_patterns(closed, support, resistance, a, tol),
    }


# ---------- 前高前低法（Previous Hour High/Low）：上一段的高低点是关键位，看接下来的小 K 线是站稳了还是被打回来 ----------

BAR_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "4H": 240, "1D": 1440}
WINDOWS = (("30m", 30), ("1H", 60), ("4H", 240), ("1D", 1440))


def window_levels(frames: dict[str, Sequence[Candle]]) -> dict[str, dict[str, object]]:
    """30 分钟 / 1 小时 / 4 小时 / 1 天各算一份：上一段（已走完）的最高点、最低点，和再前一段比是抬高还是降低；
    现在这一段里，最近两根已收盘的小 K 线相对上一段高低点的位置——站稳在外面（接受突破）、刺穿后收回（假突破）、还是在里面。
    附带按这套方法该在哪挂限价单、止损和目标放哪。只用已收盘的 K 线。"""
    out: dict[str, dict[str, object]] = {}
    for name, minutes in WINDOWS:
        # 选一个够细、又至少盖住两段多的周期来切段
        pick = next(
            (bar for bar in sorted(frames, key=lambda b: BAR_MINUTES.get(b, 10**6))
             if BAR_MINUTES.get(bar, 10**6) * 2 <= minutes and len(frames[bar]) * BAR_MINUTES[bar] >= minutes * 2.3),
            None,
        )
        if pick is None:
            continue
        closed = [c for c in frames[pick] if c.confirmed]
        span = minutes * 60_000
        groups: dict[int, list[Candle]] = {}
        for c in closed:
            groups.setdefault(c.ts // span, []).append(c)
        keys = sorted(groups)
        full = minutes // BAR_MINUTES[pick]
        done = [k for k in keys[:-1] if len(groups[k]) >= full * 0.8]  # 最后一组是正在走的这一段
        if len(done) < 2 or len(closed) < 20:
            continue
        prev, before, now = groups[done[-1]], groups[done[-2]], groups[keys[-1]] if keys[-1] != done[-1] else []
        hi, lo = max(c.high for c in prev), min(c.low for c in prev)
        hi0, lo0 = max(c.high for c in before), min(c.low for c in before)
        highs, lows = ("抬高" if hi > hi0 else "降低"), ("抬高" if lo > lo0 else "降低")
        trend = "上涨（高点低点都在抬高）" if highs == lows == "抬高" else "下跌（高点低点都在降低）" if highs == lows == "降低" else "震荡（一边抬高一边降低）"
        a = atr(closed)[-1] or (hi - lo)
        last2 = now[-2:]
        read, plan = "这一段还没走出方向：价格在上一段的高低点之间", ""
        if now:
            wick_up, wick_dn = max(c.high for c in now) > hi, min(c.low for c in now) < lo
            close = now[-1].close
            if len(last2) == 2 and all(c.close > hi for c in last2):
                risk = max(a, close - hi)
                read = f"接受向上突破：连续两根收在上一段高点 {_sig(hi)} 之上"
                plan = f"顺势做多：回踩 {_sig(hi)} 附近挂买单，止损 {_sig(hi - risk)}，目标 {_sig(hi + 2 * risk)}"
            elif len(last2) == 2 and all(c.close < lo for c in last2):
                risk = max(a, lo - close)
                read = f"接受向下跌破：连续两根收在上一段低点 {_sig(lo)} 之下"
                plan = f"顺势做空：反弹到 {_sig(lo)} 附近挂卖单，止损 {_sig(lo + risk)}，目标 {_sig(lo - 2 * risk)}"
            elif wick_up and wick_dn:
                read = "上下两边都刺穿过：这一段不做"
            elif wick_up and close < hi:
                top = max(c.high for c in now)
                read = f"假突破：上探到 {_sig(top)} 超过上一段高点 {_sig(hi)}，但收盘回到里面"
                plan = f"看回落：在 {_sig(hi)} 附近挂卖单，止损放在 {_sig(top)} 上方，目标上一段低点 {_sig(lo)}"
            elif wick_dn and close > lo:
                bottom = min(c.low for c in now)
                read = f"假跌破：下探到 {_sig(bottom)} 跌破上一段低点 {_sig(lo)}，但收盘回到里面"
                plan = f"看反弹：在 {_sig(lo)} 附近挂买单，止损放在 {_sig(bottom)} 下方，目标上一段高点 {_sig(hi)}"
        out[name] = {
            "prev_high": _sig(hi), "prev_low": _sig(lo), "before_high": _sig(hi0), "before_low": _sig(lo0),
            "highs": highs, "lows": lows, "trend": trend, "now": read, **({"plan": plan} if plan else {}),
        }
    return out

def value_area(frames: dict[str, Sequence[Candle]], last: float | None = None, share: float = 0.7, bins: int = 48) -> dict[str, object] | None:
    """上一个 UTC 日的价值区间（拍卖市场理论 / 市场轮廓）：成交最密集的价格 POC，以及包住 70% 成交量的上沿 VAH、下沿 VAL。
    没有逐笔成交，用 K 线近似：每根 K 线的成交量平均摊到它的最高最低之间。按 Dalton 的算法从 POC 向两边扩，每次比较上下相邻两格的量，并入多的那一边。"""
    pick = next((b for b in ("15m", "30m", "5m", "1H") if b in frames and len(frames[b]) * BAR_MINUTES[b] >= 1440 * 2), None)
    if pick is None:
        return None
    groups: dict[int, list[Candle]] = {}
    for c in frames[pick]:
        if c.confirmed:
            groups.setdefault(c.ts // 86_400_000, []).append(c)
    keys = sorted(groups)
    done = [k for k in keys[:-1] if len(groups[k]) >= 1440 // BAR_MINUTES[pick] * 0.8]  # 最后一组是今天，还没走完
    if not done:
        return None
    prev, today = groups[done[-1]], groups[keys[-1]]
    day_hi, day_lo, total = max(c.high for c in prev), min(c.low for c in prev), sum(c.volume for c in prev)
    # 分格的范围去掉最极端的两根影线：一根插针会把格子撑得很粗，价值区间就不准了
    cut = max(len(prev) // 50, 1)
    hi, lo = sorted(c.high for c in prev)[-1 - cut], sorted(c.low for c in prev)[cut]
    if hi <= lo or total <= 0:
        return None
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for c in prev:
        a, b = min(max(int((c.low - lo) / step), 0), bins - 1), min(max(int((c.high - lo) / step), 0), bins - 1)
        for i in range(a, b + 1):
            vol[i] += c.volume / (b - a + 1)
    poc = max(range(bins), key=vol.__getitem__)
    lo_i = hi_i = poc
    acc = vol[poc]
    while acc < total * share and (lo_i > 0 or hi_i < bins - 1):
        up, down = sum(vol[hi_i + 1:hi_i + 3]), sum(vol[max(lo_i - 2, 0):lo_i])
        if hi_i >= bins - 1 or (lo_i > 0 and down > up):
            acc, lo_i = acc + down, max(lo_i - 2, 0)
        else:
            acc, hi_i = acc + up, min(hi_i + 2, bins - 1)
    vah, val = lo + (hi_i + 1) * step, lo + lo_i * step

    def side(px: float) -> str:
        return "上方" if px > vah else "下方" if px < val else "里面"

    out: dict[str, object] = {"poc": _sig(lo + (poc + 0.5) * step), "vah": _sig(vah), "val": _sig(val), "day_high": _sig(day_hi), "day_low": _sig(day_lo)}
    opened = side(today[0].open)
    out["open"] = f"今天开在价值区间{opened}"
    if last:
        now = side(last)
        out["now"] = f"现价在价值区间{now}"
        if opened != "里面" and now == "里面":
            out["read"] = f"开在区间外又回到了里面：在里面站稳的话，常会走到对侧的{'下沿 ' + str(_sig(val)) if opened == '上方' else '上沿 ' + str(_sig(vah))}"
        elif now != "里面":
            out["read"] = f"价格在价值区间{now}：站得住是在找新的价值（顺着做），站不住回到里面就是被拒绝（反着做）"
    return out

def day_profile(frames: dict[str, Sequence[Candle]], last: float | None, anchor_ms: int, label: str, or_minutes: int = 60) -> dict[str, object] | None:
    """从开盘（anchor_ms）到现在是怎么走的：开盘价、相对开盘的涨跌、先去了哪一边、现在在当天区间的什么位置、属于哪种走法，
    以及开盘后第一个小时的高低点（开盘区间）和现价在它的哪一边。K 线盖不到开盘那一刻就不给。"""
    pick = next((b for b in ("5m", "15m", "30m", "1H") if any(c.ts == anchor_ms for c in frames.get(b, ()))), None)
    bars = [c for c in frames[pick] if c.ts >= anchor_ms] if pick else []
    if not bars or not bars[0].open:
        return None
    open_px, px = bars[0].open, last or bars[-1].close
    hi_bar, lo_bar = max(bars, key=lambda c: c.high), min(bars, key=lambda c: c.low)
    hi, lo = max(hi_bar.high, px), min(lo_bar.low, px)

    def pct(v: float) -> float:
        return (v / open_px - 1) * 100

    span = hi - lo
    pos = (px - lo) / span * 100 if span > 0 else 50.0
    open_pos = (open_px - lo) / span * 100 if span > 0 else 50.0
    low_time = bars[-1].ts if px < lo_bar.low else lo_bar.ts
    high_time = bars[-1].ts if px > hi_bar.high else hi_bar.ts
    low_first = low_time < high_time
    path = (f"先下探到 {_sig(lo)}（{pct(lo):+.2f}%），再上行到 {_sig(hi)}（{pct(hi):+.2f}%）" if low_first
            else f"先冲高到 {_sig(hi)}（{pct(hi):+.2f}%），再回落到 {_sig(lo)}（{pct(lo):+.2f}%）")
    if low_time == high_time:
        path = "高低点落在同一根 K 线内，先后未知"
    if pos >= 75 and open_pos <= 30:
        kind, read = "单边上行", "开在低位、现在贴近日内高点：顺势，只等回踩做多，不摸顶"
    elif pos <= 25 and open_pos >= 70:
        kind, read = "单边下行", "开在高位、现在贴近日内低点：顺势，只等反弹做空，不抄底"
    elif high_time < low_time and px < open_px:
        kind, read = "冲高回落", "涨上去的部分全吐回来了，现价低于开盘价：偏弱，开盘价和日内高点是阻力"
    elif low_first and px > open_px:
        kind, read = "探底回升", "跌下去的部分全收回来了，现价高于开盘价：偏强，开盘价和日内低点是支撑"
    else:
        kind, read = "围绕开盘价震荡", "还没走出方向：按区间做两头，不在开盘价附近追"
    out: dict[str, object] = {
        "since": label, "anchor_ms": anchor_ms, "open": open_px, "change_pct": round(pct(px), 2), "high": _sig(hi), "low": _sig(lo), "pos_pct": round(pos),
        "path": path, "kind": kind, "read": read,
        "text": f"{label}开 {_sig(open_px)}，现在 {pct(px):+.2f}%，{kind}",
    }
    for bar in ("5m", "15m", "30m", "1H"):
        step = BAR_MINUTES[bar] * 60_000
        pre = [c for c in frames.get(bar, ()) if c.confirmed and anchor_ms - 3_600_000 <= c.ts < anchor_ms]
        if [c.ts for c in pre] != list(range(anchor_ms - 3_600_000, anchor_ms, step)) or not pre[0].open or not pre[-1].close:
            continue
        out["pre_open"] = {"close": pre[-1].close, "high": max(c.high for c in pre), "low": min(c.low for c in pre),
                           "change_pct": round((pre[-1].close / pre[0].open - 1) * 100, 3),
                           "gap_pct": round((open_px / pre[-1].close - 1) * 100, 3)}
        break
    first = [c for c in bars if c.ts < anchor_ms + or_minutes * 60_000]
    if first and bars[-1].ts >= anchor_ms + or_minutes * 60_000:  # 开盘区间走完了才给
        or_hi, or_lo = max(c.high for c in first), min(c.low for c in first)
        out["opening_range"] = {
            "minutes": or_minutes, "high": _sig(or_hi), "low": _sig(or_lo), "dir": "收涨" if first[-1].close >= open_px else "收跌",
            "now": "现价在开盘区间上方" if px > or_hi else "现价在开盘区间下方" if px < or_lo else "现价还在开盘区间里面",
        }
    return out


def mean_ci(values: list[float], rounds: int = 2000, seed: int = 7) -> tuple[float, float]:
    """平均值的 95% 区间（自助法重抽样，固定随机种子）。区间跨过 0 就不能说这条打法赚钱或亏钱。"""
    n = len(values)
    if n < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(rounds))
    return means[int(rounds * 0.025)], means[int(rounds * 0.975) - 1]
