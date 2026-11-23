"""计划就是指令：Worker 给出的交易计划，由程序核对并直接变成单子。

这里只有计算，没有模型、没有下单：
- check_plan：按现价判断这个计划现在该怎么执行（现价就进 / 等回到入场区间 / 等突破），并核对止损止盈有没有放错边、
  扣掉往返手续费和点差之后的净盈亏比够不够【出手尺度】的下限。数字由程序算一次，不让模型每一轮各算各的。
- order_params：把通过核对的计划换成策略执行器（strategy.py）认识的参数，连同进场后的管理规则。
- same_plan：新计划和已经布置的那一条是不是基本一样（一样就不动，免得每一轮都撤了重挂）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..models import STYLES, TREND_FRAMES, TradePlan
from ..risk import LIQUIDATION_BUFFER_PCT, MIN_STOP_DISTANCE_PCT

MAX_CHASE_PCT = 0.6  # 突破单最多追多远（%）
MIN_CHASE_PCT = 0.15
SAME_PLAN_R = 0.25  # 触发价和止损的变动都不到止损距离的这个比例：算同一个计划
RETRY_MINUTES = {"scalp": 10, "intraday": 30, "swing": 120}  # 同一个位置、同一个方向刚平掉一笔，隔这么久才再进


@dataclass
class PlanCheck:
    how: str = ""  # now 现价就进 / pullback 等回到入场区间 / breakout 等突破；空 = 现在没法执行
    ref: float = 0.0  # 按哪个价算的入场（现价，或者入场区间里先碰到的那一边）
    stop_pct: float = 0.0
    tp_pct: float = 0.0
    cost_pct: float = 0.0
    rr: float = 0.0
    net_rr: float = 0.0
    chase_pct: float = 0.0
    limit: bool = False  # 等价格回来的单：直接在交易所挂限价单（进场按挂单手续费算），程序挂了、网断了它也在
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.how) and not self.issues

    def numbers(self) -> str:
        return (f"入场按 {self.ref:g} 算，止损距离 {self.stop_pct:.2f}%，到第一目标 {self.tp_pct:.2f}%，往返成本（手续费 + 点差）{self.cost_pct:.2f}%，"
                f"净盈亏比 {self.net_rr:.2f}")


def trade_cost_pct(taker_fee_pct: float, spread_pct: float | None) -> float:
    """一笔单从进到出要付的成本占价格的百分比：进出各一次吃单手续费，加上盘口点差。"""
    return taker_fee_pct * 2 + max(spread_pct or 0.0, 0.0)


def check_plan(plan: TradePlan, px: float, *, min_rr: float, cost_pct: float, limit_cost_pct: float | None = None) -> PlanCheck:
    """limit_cost_pct：等价格回来的单是在交易所挂限价单成交的，这是它的往返成本（挂单手续费 + 吃单手续费 + 点差）；不给就按到价市价进算。"""
    long = plan.direction == "long"
    lo, hi = sorted((plan.entry_low, plan.entry_high))
    stop, tps = plan.stop_loss, list(plan.take_profits)
    side, sign = ("做多" if long else "做空"), (1 if long else -1)
    out = PlanCheck(cost_pct=cost_pct)
    if not px > 0:
        out.issues.append("拿不到现价，没法核对计划")
        return out
    if (stop >= lo) if long else (stop <= hi):
        out.issues.append(f"{side}的止损 {stop:g} 要放在入场区间 {lo:g}~{hi:g} 的{'下' if long else '上'}方")
        return out

    # 现在该怎么执行
    if plan.entry_type == "breakout":
        trigger = lo if long else hi
        out.chase_pct = max(min((hi - lo) / trigger * 100, MAX_CHASE_PCT), MIN_CHASE_PCT)
        beyond = (px / trigger - 1) * 100 * sign  # 现价已经越过突破位百分之几
        out.how, out.ref = ("now", px) if 0 <= beyond <= out.chase_pct else ("breakout", trigger)
    else:
        deeper = (px < lo) if long else (px > hi)  # 价格已经穿过入场区间，往止损那边去了
        if plan.entry_type == "now" or lo <= px <= hi:
            out.how, out.ref = "now", px
        elif deeper:
            edge = lo if long else hi
            if abs(px - stop) < abs(edge - stop) * 0.5 or (px - stop) * sign <= 0:
                out.issues.append(f"现价 {px:g} 已经穿过入场区间 {lo:g}~{hi:g}、贴近止损 {stop:g}，这个计划的结构快破了：按现在的走势重新给计划")
                return out
            out.how, out.ref = "now", px
        else:
            out.how, out.ref = "pullback", (hi if long else lo)  # 等价格回来：先碰到的是离现价近的那一边
            if limit_cost_pct is not None:
                out.limit, out.cost_pct = True, limit_cost_pct
    ref, cost_pct = out.ref, out.cost_pct
    if (stop - ref) * sign >= 0:
        out.issues.append(f"{side}的止损 {stop:g} 要在入场价 {ref:g} 的{'下' if long else '上'}方")
        return out
    if out.how == "pullback" and (px - (min(tps) if long else max(tps))) * sign >= 0:
        out.issues.append(f"现价 {px:g} 已经过了第一目标，这一段走完了：按现在的走势重新给计划")
        return out
    wrong = [tp for tp in tps if (tp - ref) * sign <= 0]
    if wrong:
        out.issues.append(f"{side}的止盈 {'、'.join(f'{t:g}' for t in wrong)} 要在入场价 {ref:g} 的{'上' if long else '下'}方")
        return out
    tp1 = min(tps) if long else max(tps)
    out.stop_pct, out.tp_pct = abs(ref - stop) / ref * 100, abs(tp1 - ref) / ref * 100
    out.rr = out.tp_pct / out.stop_pct if out.stop_pct else 0.0
    out.net_rr = (out.tp_pct - cost_pct) / (out.stop_pct + cost_pct)
    if out.stop_pct < MIN_STOP_DISTANCE_PCT:
        out.issues.append(f"止损离入场只有 {out.stop_pct:.3f}%（至少 {MIN_STOP_DISTANCE_PCT}%），正常波动就会扫掉：把止损放到 5m / 15m 的结构位之外")
    elif out.net_rr < min_rr:
        need = min_rr * (out.stop_pct + cost_pct) + cost_pct
        out.issues.append(
            f"{out.numbers()}，不到下限 {min_rr:g}。止损不动的话第一目标至少要离入场 {need:.2f}%（{ref * (1 + sign * need / 100):.6g} 之外）："
            "把入场挂在更靠近止损的结构位，或者第一目标取下一个真实的结构位；两样都做不到就不给 plan，no_plan_reason 选 cost"
        )
    return out


def safe_leverage(leverage: int, stop_pct: float) -> int:
    """止损距离 × 杠杆不能超过强平缓冲：放不下就用放得下的最高杠杆。"""
    return max(min(leverage, int((LIQUIDATION_BUFFER_PCT - 1) // stop_pct) if stop_pct else leverage), 1)


def manage_rules(plan: TradePlan, check: PlanCheck, style: str) -> dict[str, Any]:
    """进场之后交给执行器的管理规则：到 1R 移保本；有两个以上目标就在第一目标先平一半；超过这个风格的持仓时间没走出来就离场。"""
    long = plan.direction == "long"
    tps = sorted(plan.take_profits, reverse=not long)
    rules: dict[str, Any] = {"breakeven_r": 1.0}
    if plan.review_level and min(check.ref, plan.stop_loss) < plan.review_level < max(check.ref, plan.stop_loss):
        rules.update(review_level=plan.review_level, review_bar=STYLES[style]["struct_bar"])
    if len(tps) >= 2 and check.stop_pct:
        rules["partials"] = [{"r": round(abs(tps[0] - check.ref) / check.ref * 100 / check.stop_pct, 2), "fraction": 0.5}]
    if STYLES[style].get("hold_minutes"):
        rules["max_hold_minutes"] = STYLES[style]["hold_minutes"]
    return rules


def far_target(plan: TradePlan) -> float:
    """挂在交易所的止盈：最远的那个目标（近的目标由执行器分批平）。"""
    return max(plan.take_profits) if plan.direction == "long" else min(plan.take_profits)


def plan_terms(plan: TradePlan, style: str) -> dict[str, Any]:
    """Executable terms other than the entry/stop prices (which have a price tolerance)."""
    return {
        "targets": sorted(plan.take_profits), "margin": plan.margin_usdt, "risk": plan.risk_pct, "leverage": plan.leverage,
        "minutes": plan.valid_minutes or STYLES[style]["plan_minutes"], "style": style, "review": plan.review_level,
        "reverse": [plan.reverse_trigger, plan.reverse_stop, plan.reverse_target],
        **({"cancel_before_entry": entry_invalidation(plan, style)} if plan.cancel_before_entry else {}),
    }


def entry_invalidation(plan: TradePlan, style: str) -> dict[str, Any] | None:
    if not plan.cancel_before_entry:
        return None
    rule = plan.cancel_before_entry
    return {"when": rule.when, "level": rule.level, "bar": rule.bar or STYLES[style]["struct_bar"]}


def order_params(plan: TradePlan, check: PlanCheck, style: str) -> dict[str, Any]:
    """到价进场的策略参数（check.how 是 pullback 或 breakout 时用）。"""
    long = plan.direction == "long"
    params: dict[str, Any] = {
        "terms": plan_terms(plan, style), "trigger": check.ref, "stop_loss": plan.stop_loss, "take_profit": far_target(plan), "leverage": safe_leverage(plan.leverage, check.stop_pct),
        "risk_pct": plan.risk_pct, "margin_usdt": plan.margin_usdt, "expires_minutes": plan.valid_minutes or STYLES[style]["plan_minutes"], "manage": manage_rules(plan, check, style), "plan": True,
    }
    if check.how == "pullback":  # 没回到入场区间就先到了第一目标：这一段已经走完，不再等
        params["miss_px"] = min(plan.take_profits) if long else max(plan.take_profits)
    else:
        params["max_chase_pct"] = check.chase_pct
    if cancel := entry_invalidation(plan, style):
        params["cancel_before_entry"] = cancel
    return params


def same_plan(row: dict[str, Any], plan: TradePlan, check: PlanCheck, style: str) -> bool:
    """已经布置的那一条（row）和这次的计划是不是基本一样：方向、执行方式相同，触发价和止损的变动都很小。"""
    p = row["params"]
    if p.get("terms") != plan_terms(plan, style) or row["side"] != plan.direction or row["kind"] != ("limit" if check.limit else check.how):
        return False
    unit = abs(p["trigger"] - p["stop_loss"]) * SAME_PLAN_R
    return abs(p["trigger"] - check.ref) <= unit and abs(p["stop_loss"] - plan.stop_loss) <= unit


def _sig(v: float) -> float:
    return float(f"{v:.6g}")


REGIME_ADX = 20  # 结构周期 ADX 到这个数以上、高低点又和大方向一致，才算趋势


TURN_FIRST = ("scalp",)  # 这些风格：5 分钟刚确认、还没被收回的结构转折先于前高前低定大方向（回测见 knowledge/evidence.md「行情判断：转折优先和 ADX 缓冲」）
TURN_BY = "5m 转折"


def major_direction(snapshot: dict[str, Any], style: str, shift: dict[str, Any] | None = None) -> tuple[str, str]:
    if style in TURN_FIRST and shift and shift.get("side") in ("long", "short"):
        return shift["side"], TURN_BY
    windows = snapshot.get("windows") or {}
    for name in TREND_FRAMES[style]:
        word = str((windows.get(name) or {}).get("trend") or "")
        if word.startswith(("上涨", "下跌")):
            return ("long" if word.startswith("上涨") else "short"), name
    return "", ""


def candidate_rows(rows: list[dict[str, Any]], style: str) -> list[dict[str, Any]]:
    """候选位摊平成页面能直接画的数字：入场、止损、目标、成本占多少 R、净盈亏比、合不合格，以及这条打法机械照做的回测成绩。"""
    from ..backtest_plans import evidence

    scores = evidence(style)
    return [{
        "strategy": c.get("strategy", ""), "basis": c.get("basis", ""), "align": c.get("align", ""), "side": c["plan"].direction, "how": c["check"].how,
        "entry": c["check"].ref, "stop": c["plan"].stop_loss, "targets": list(c["plan"].take_profits), "ok": c["ok"], "why_not": c.get("why_not", ""),
        "net_rr": round(c["check"].net_rr, 2), "stop_pct": round(c["check"].stop_pct, 3),
        "cost_r": round(c["check"].cost_pct / c["check"].stop_pct, 2) if c["check"].stop_pct else None,
        "evidence": scores.get(c.get("strategy", ""), ""), "losing": bool(c.get("losing")),
    } for c in rows]


def market_regime(snapshot: dict[str, Any], style: str, shift: dict[str, Any] | None = None) -> dict[str, str]:
    """先判断现在是什么行情，再决定用哪类打法：
    reversal 刚确认的结构反转（5 分钟收盘反向越过摆动点）；trend 大方向明确、结构周期高低点同向且 ADX 够；
    range 结构周期没有方向、ADX 低；mixed 其余（大方向和结构周期打架）。"""
    frames = snapshot.get("timeframes") or {}
    tf = frames.get(STYLES[style]["struct_bar"]) or {}
    major, _ = major_direction(snapshot, style, shift)
    adx, structure = tf.get("adx14"), tf.get("structure")
    if (tf.get("insufficient") or structure not in ("uptrend", "downtrend", "range")
            or not isinstance(adx, (int, float)) or not math.isfinite(adx) or not 0 <= adx <= 100):
        return {"kind": "unknown", "side": "", "why": f"{STYLES[style]['struct_bar']}缺少有效的收盘结构或 ADX，等有效采样"}
    local = {"uptrend": "long", "downtrend": "short"}.get(structure, "")
    if shift and shift.get("kind") == "反转" and shift.get("bars_ago", 99) <= 6:
        return {"kind": "reversal", "side": shift["side"], "why": f"5 分钟刚确认结构反转（{shift.get('why', '')}）"}
    if major and local == major and adx >= REGIME_ADX:
        return {"kind": "trend", "side": major, "why": f"大方向和{STYLES[style]['struct_bar']}结构同向，ADX {adx:.0f}"}
    if not local and adx < REGIME_ADX:
        return {"kind": "range", "side": "", "why": f"{STYLES[style]['struct_bar']}没有方向，ADX {adx:.0f}"}
    return {"kind": "mixed", "side": major, "why": f"大方向{'向上' if major == 'long' else '向下' if major == 'short' else '不明'}，{STYLES[style]['struct_bar']}结构{'同向' if local == major else '不一致'}，ADX {adx:.0f}"}


# 每种行情用哪几类打法。来自候选位回测（evidence「按行情选打法」）：趋势里只做顺势回踩，震荡只做区间两头，
# 刚反转只做变盘后回踩，大方向和结构周期打架时不出候选；假突破反转在哪种行情里机械做都是最差的，自动档不再给。
REGIME_PLAYS = {
    "trend": ("趋势回踩", "前高前低法"),
    "range": ("区间高抛低吸",),
    "reversal": ("变盘后回踩",),
    "mixed": (),
    "unknown": (),
}
REGIME_LABEL = {"trend": "趋势", "range": "震荡", "reversal": "刚反转", "mixed": "方向打架", "unknown": "数据不足"}


def build_candidates(
    snapshot: dict[str, Any],
    *,
    style: str,
    min_rr: float,
    maker_fee_pct: float = 0.02,
    taker_fee_pct: float = 0.05,
    shift: dict[str, Any] | None = None,
    quant_mode: str = "auto",
    quant_bias: str = "both",
    entry_mode: str = "auto",
) -> list[dict[str, Any]]:
    """纯算法候选生成：从行情快照里的真实结构位（支撑阻力、区间上下沿、前高前低、最近变盘点）算出候选计划，
    按当前量化档位（quant_mode）和方向锁（quant_bias）过滤，并逐一过一遍净盈亏比与可达性核验。"""
    from .signals import line_status

    if not isinstance(snapshot, dict) or quant_mode == "quiet":
        return []
    px = float(snapshot.get("last") or 0)
    frames = snapshot.get("timeframes") or {}
    if px <= 0 or not frames:
        return []
    spec = STYLES[style]
    tf = frames.get(spec["struct_bar"]) or next((f for f in frames.values() if isinstance(f, dict) and f.get("atr14")), {})
    atr = float(tf.get("atr14") or px * 0.004)
    spread = float(((snapshot.get("orderbook") or {}).get("spread_pct")) or 0.0)
    taker_cost = trade_cost_pct(taker_fee_pct, spread)
    limit_cost = maker_fee_pct + taker_fee_pct + max(spread, 0.0)
    windows = snapshot.get("windows") or {}

    major, _ = major_direction(snapshot, style, shift)

    ups: list[float] = []
    downs: list[float] = []
    for r in tf.get("resistance") or []:
        if isinstance(r, dict) and r.get("price"):
            ups.append(float(r["price"]))
    for s in tf.get("support") or []:
        if isinstance(s, dict) and s.get("price"):
            downs.append(float(s["price"]))
    rng = tf.get("range") or {}
    if rng.get("upper") and rng.get("lower"):
        ups.append(float(rng["upper"]))
        downs.append(float(rng["lower"]))
        mid = (float(rng["upper"]) + float(rng["lower"])) / 2
        (ups if mid > px else downs).append(_sig(mid))
    for w in windows.values():
        if isinstance(w, dict):
            if w.get("prev_high"):
                (ups if float(w["prev_high"]) > px else downs).append(float(w["prev_high"]))
            if w.get("prev_low"):
                (ups if float(w["prev_low"]) > px else downs).append(float(w["prev_low"]))

    def targets_from(entry: float, stop: float, long: bool) -> list[float]:
        risk = abs(entry - stop)
        pool = sorted({v for v in (ups if long else downs) if (v - entry if long else entry - v) >= risk * 0.5}, reverse=not long)
        t1 = _sig(pool[0]) if pool else _sig(entry + (1 if long else -1) * risk * max(min_rr + 0.4, 1.8))
        t2 = _sig(pool[1]) if len(pool) > 1 else _sig(t1 + (1 if long else -1) * risk)
        return [t1] if t1 == t2 else [t1, t2]

    raw: list[tuple[str, str, TradePlan]] = []
    # 1. 变盘后回踩（刚发生结构转折）
    if shift and shift.get("bars_ago", 99) <= 8 and shift.get("level") and shift.get("extreme") and shift.get("origin"):
        side = shift["side"]
        long, sign = side == "long", (1 if side == "long" else -1)
        lv = float(shift["level"])
        stop = _sig(lv - sign * max(atr, px * 0.0025))
        leg = abs(float(shift["extreme"]) - float(shift["origin"]))
        t1, t2 = _sig(float(shift["extreme"])), _sig(float(shift["extreme"]) + sign * leg * 0.5)
        how = "now" if abs(px - lv) <= 0.25 * atr and (px - stop) * sign > 0 else "pullback"
        entry = _sig(px if how == "now" else lv)
        if (t1 - entry) * sign > 0 and (entry - stop) * sign > 0:
            raw.append(("变盘后回踩", f"5m {shift['kind']}突破 {lv:g} 后的首次回踩位", TradePlan(
                direction=side, strategy="变盘后回踩", entry_low=entry, entry_high=entry, stop_loss=stop,
                take_profits=[t1, t2], leverage=20, entry_type=how, invalidation=f"收盘回到 {lv:g} 另一侧",
            )))

    # 2. 前高前低法（已走完时间段的高低点接受突破或假突破）
    for wname in TREND_FRAMES.get(style, ("1H", "30m")):
        w = windows.get(wname)
        if not isinstance(w, dict) or not w.get("prev_high") or not w.get("prev_low"):
            continue
        hi, lo, now_read = float(w["prev_high"]), float(w["prev_low"]), str(w.get("now") or "")
        if now_read.startswith("接受向上突破"):
            stop = _sig(hi - max(atr, px * 0.0025))
            how = "now" if abs(px - hi) <= 0.2 * atr and px >= hi else "pullback"
            entry = _sig(px if how == "now" else hi)
            if entry > stop:
                raw.append(("前高前低法", f"{wname} 接受站上前高 {hi:g}，回踩接多", TradePlan(
                    direction="long", strategy="前高前低法", entry_low=entry, entry_high=entry, stop_loss=stop,
                    take_profits=targets_from(entry, stop, True), leverage=20, entry_type=how, invalidation=f"收盘跌回 {hi:g} 下方",
                )))
        elif now_read.startswith("接受向下跌破"):
            stop = _sig(lo + max(atr, px * 0.0025))
            how = "now" if abs(px - lo) <= 0.2 * atr and px <= lo else "pullback"
            entry = _sig(px if how == "now" else lo)
            if entry < stop:
                raw.append(("前高前低法", f"{wname} 接受跌破前低 {lo:g}，反弹挂空", TradePlan(
                    direction="short", strategy="前高前低法", entry_low=entry, entry_high=entry, stop_loss=stop,
                    take_profits=targets_from(entry, stop, False), leverage=20, entry_type=how, invalidation=f"收盘站回 {lo:g} 上方",
                )))
        elif now_read.startswith("假突破") and hi > lo:
            stop = _sig(max(hi, px) + max(0.6 * atr, px * 0.0025))
            how = "now" if px <= hi and abs(hi - px) <= 0.25 * atr else "pullback"
            entry = _sig(px if how == "now" else hi)
            mid = _sig((hi + lo) / 2)
            tps = [t for t in ([mid, _sig(lo)] if mid < entry else [_sig(lo)]) if t < entry]
            if tps and entry < stop:
                raw.append(("假突破反转", f"{wname} 刺穿前高 {hi:g} 后收回，看回落", TradePlan(
                    direction="short", strategy="假突破反转", entry_low=entry, entry_high=entry, stop_loss=stop,
                    take_profits=tps, leverage=20, entry_type=how, invalidation=f"连续收盘站上 {stop:g}",
                )))
        elif now_read.startswith("假跌破") and hi > lo:
            stop = _sig(min(lo, px) - max(0.6 * atr, px * 0.0025))
            how = "now" if px >= lo and abs(px - lo) <= 0.25 * atr else "pullback"
            entry = _sig(px if how == "now" else lo)
            mid = _sig((hi + lo) / 2)
            tps = [t for t in ([mid, _sig(hi)] if mid > entry else [_sig(hi)]) if t > entry]
            if tps and entry > stop:
                raw.append(("假突破反转", f"{wname} 刺穿前低 {lo:g} 后收回，看反弹", TradePlan(
                    direction="long", strategy="假突破反转", entry_low=entry, entry_high=entry, stop_loss=stop,
                    take_profits=tps, leverage=20, entry_type=how, invalidation=f"连续收盘跌破 {stop:g}",
                )))

    # 3. 最近结构支撑 / 阻力挂单（趋势回踩或区间高抛低吸）
    near_sup = max((v for v in downs if v < px * 1.0005), default=None)
    supports = {float(s["price"]) for s in tf.get("support") or [] if isinstance(s, dict) and s.get("price") and 0 < float(s["price"]) < px}
    for lv in sorted(supports | ({near_sup} if near_sup else set()), reverse=True)[:3]:
        stop = _sig(lv - max(0.8 * atr, px * 0.0025))
        how = "now" if entry_mode != "limit" and px >= lv and abs(px - lv) <= 0.2 * atr else "pullback"
        entry = _sig(px if how == "now" else lv)
        if lv != near_sup and not any(v > px and v - entry >= (entry - stop) * 0.5 for v in ups):
            continue  # 备选挂位必须有已知的首目标，不为凑候选外推首目标
        strat = "趋势回踩" if major == "long" else "区间高抛低吸"
        raw.append((strat, f"依托支撑 {lv:g} 做多", TradePlan(
            direction="long", strategy=strat, entry_low=entry, entry_high=entry, stop_loss=stop,
            take_profits=targets_from(entry, stop, True), leverage=20, entry_type=how, invalidation=f"{spec['struct_bar']} 收盘跌破 {stop:g}",
        )))
    near_res = min((v for v in ups if v > px * 0.9995), default=None)
    resistances = {float(r["price"]) for r in tf.get("resistance") or [] if isinstance(r, dict) and r.get("price") and float(r["price"]) > px}
    for lv in sorted(resistances | ({near_res} if near_res else set()))[:3]:
        stop = _sig(lv + max(0.8 * atr, px * 0.0025))
        how = "now" if entry_mode != "limit" and px <= lv and abs(lv - px) <= 0.2 * atr else "pullback"
        entry = _sig(px if how == "now" else lv)
        if lv != near_res and not any(v < px and entry - v >= (stop - entry) * 0.5 for v in downs):
            continue
        strat = "趋势回踩" if major == "short" else "区间高抛低吸"
        raw.append((strat, f"依托阻力 {lv:g} 做空", TradePlan(
            direction="short", strategy=strat, entry_low=entry, entry_high=entry, stop_loss=stop,
            take_profits=targets_from(entry, stop, False), leverage=20, entry_type=how, invalidation=f"{spec['struct_bar']} 收盘站上 {stop:g}",
        )))

    # 4. 收敛突破档专属：在箱体上下外沿给出突破追单候选（entry_type=breakout）
    if quant_mode == "breakout" and rng.get("upper") and rng.get("lower"):
        hi_r, lo_r = float(rng["upper"]), float(rng["lower"])
        span = hi_r - lo_r
        if span > 0:
            if major != "short":
                b_hi = _sig(hi_r * 1.0025)
                stop = _sig(max(hi_r - atr, lo_r))
                if hi_r > stop:
                    raw.append(("收敛突破", f"放量突破箱体上沿 {hi_r:g} 顺势追多", TradePlan(
                        direction="long", strategy="收敛突破", entry_low=_sig(hi_r), entry_high=b_hi, stop_loss=stop,
                        take_profits=[_sig(hi_r + span), _sig(hi_r + span * 1.6)], leverage=20, entry_type="breakout",
                        invalidation=f"跌回 {hi_r:g} 箱体内侧",
                    )))
            if major != "long":
                b_lo = _sig(lo_r * 0.9975)
                stop = _sig(min(lo_r + atr, hi_r))
                if lo_r < stop:
                    raw.append(("收敛突破", f"放量跌破箱体下沿 {lo_r:g} 顺势追空", TradePlan(
                        direction="short", strategy="收敛突破", entry_low=b_lo, entry_high=_sig(lo_r), stop_loss=stop,
                        take_profits=[_sig(lo_r - span), _sig(lo_r - span * 1.6)], leverage=20, entry_type="breakout",
                        invalidation=f"收回 {lo_r:g} 箱体内侧",
                    )))

    from ..backtest_plans import losing

    regime = market_regime(snapshot, style, shift)
    bad = losing(style)
    seen_keys: set[tuple[str, int]] = set()
    out: list[dict[str, Any]] = []
    for name, basis, plan in raw:
        if quant_bias in ("long", "short") and plan.direction != quant_bias:
            continue
        if quant_mode == "trend" and major and plan.direction != major and name != "变盘后回踩":
            continue
        if quant_mode == "range" and name not in ("区间高抛低吸", "假突破反转"):
            continue
        if quant_mode == "breakout" and name != "收敛突破":
            continue
        if quant_mode in ("auto", "regime") and (name not in REGIME_PLAYS[regime["kind"]] or (regime["side"] and regime["kind"] != "range" and plan.direction != regime["side"])):
            continue
        check = check_plan(plan, px, min_rr=min_rr, cost_pct=taker_cost, limit_cost_pct=limit_cost)
        stale = "" if check.how == "now" else line_status(px, check.ref, plan.direction, shift, atr, spec["plan_minutes"], spec["bar_minutes"])
        align = "顺大方向" if major == plan.direction else "逆大方向（需反转信号且仓位减半）" if major else "震荡边缘"
        ok = check.ok and not stale
        out.append({
            "strategy": name, "basis": basis, "align": align, "ok": ok, "plan": plan, "check": check,
            "why_not": stale or ("；".join(check.issues) if not check.ok else ""),
            "dist_pct": abs(check.ref / px - 1) * 100 if check.ref and px else 0.0,
            "losing": name in bad,  # 只打标记，不改排序：回测要照同一个顺序才能复现
        })
    preferred = {"limit": "pullback", "market": "now"}.get(entry_mode)
    out.sort(key=lambda c: (not c["ok"], 0 if c["align"] == "顺大方向" else 1 if c["align"] == "震荡边缘" else 2,
                            bool(preferred and c["check"].how != preferred), c["dist_pct"]))
    # 先核验、排序，再对相近价位去重，避免先生成的不合格方案挡掉合格方案。
    picked = []
    for candidate in out:
        plan = candidate["plan"]
        key = (plan.direction, round(plan.entry_low / max(atr * 0.25, px * 1e-4)))
        if key not in seen_keys:
            seen_keys.add(key)
            picked.append(candidate)
    return picked[:4]


NO_EDGE_HINT = (
    "当前通过结构与冷却检查、且不逆大方向的候选，机械照做扣费后是亏的。只有你手里有算法看不到的依据"
    "（比如大级别刚确认的方向、明确的事件、资金费率或持仓量的极端读数）才给 plan，并把这条依据写进 plan.edge；"
    "拿不出来就不给 plan，no_plan_reason 选 no_edge——这是正常结论，不算偷懒。"
)


def plan_origin(plan: TradePlan, check: PlanCheck, candidates: list[dict[str, Any]]) -> str:
    """这个计划和算法候选的关系：照做 / 改过 / 自拟。记到战绩里，以后才知道 Worker 的取舍比机械照做强还是弱。
    candidates 是 candidate_rows 的输出。入场和止损都在候选的 0.25 倍止损距离以内算照做；同方向、入场在 1 倍以内算改过。"""
    best = "Worker 自拟"
    for c in candidates or []:
        risk = abs(c["entry"] - c["stop"])
        if c["side"] != plan.direction or risk <= 0:
            continue
        gap = abs(check.ref - c["entry"])
        if gap <= 0.25 * risk and abs(plan.stop_loss - c["stop"]) <= 0.25 * risk:
            return "照算法候选"
        if gap <= risk:
            best = "改过算法候选"
    return best


def candidates_text(
    snapshot: dict[str, Any],
    *,
    style: str,
    min_rr: float,
    maker_fee_pct: float = 0.02,
    taker_fee_pct: float = 0.05,
    shift: dict[str, Any] | None = None,
    quant_mode: str = "auto",
    quant_bias: str = "both",
    entry_mode: str = "auto",
    rows: list[dict[str, Any]] | None = None,
) -> str:
    """把算法算好的候选计划排成清清楚楚的几行喂给 Worker。"""
    from ..models import QUANT_MODES

    mlabel = QUANT_MODES.get(quant_mode, QUANT_MODES["auto"])["label"]
    if quant_mode == "quiet":
        return f"【算法候选位｜当前档位：{mlabel}】已暂停新开仓候选（只保护已有持仓与清理过时挂单）；盘面恢复清晰后可用 set_quant_mode 切回对应档位。"
    rows = rows if rows is not None else build_candidates(
        snapshot, style=style, min_rr=min_rr, maker_fee_pct=maker_fee_pct, taker_fee_pct=taker_fee_pct,
        shift=shift, quant_mode=quant_mode, quant_bias=quant_bias, entry_mode=entry_mode,
    )
    regime = market_regime(snapshot, style, shift)
    plays = "、".join(REGIME_PLAYS[regime["kind"]]) or "不出候选"
    head = (f"【行情判断（程序算的）】{REGIME_LABEL[regime['kind']]}：{regime['why']}。自动档这轮只看：{plays}"
            + (f"，只做{'多' if regime['side'] == 'long' else '空'}" if regime["side"] and regime["kind"] in ("trend", "reversal") else "") + "。\n"
            if quant_mode == "auto" else "")
    if not rows:
        if quant_mode == "auto" and regime["kind"] == "mixed":
            return head + "大方向和结构周期在打架：这轮不给入场候选。空仓就等其中一边占上风；不要用前高前低或假突破硬找理由。"
        return head + f"【算法候选位｜{mlabel}】当前没有匹配的结构。不要编造入场点，说明缺什么条件以及下次看哪里。"
    btag = "，只盯多" if quant_bias == "long" else "，只盯空" if quant_bias == "short" else ""
    from ..backtest_plans import evidence

    scores = evidence(style)
    lines = [head + f"【算法算好的结构候选位（当前档位：{mlabel}{btag}｜已计入手续费与点差、已查可达性）】"
             "分工：程序负责算价位、核成本、下单和管仓；你负责判断这一条现在该不该做。候选位只是「结构上能挂的位置」，不是「会赚的位置」。"]
    for i, c in enumerate(rows, 1):
        p: TradePlan = c["plan"]
        chk: PlanCheck = c["check"]
        how = (
            "现价市价进 (entry_type=now)" if chk.how == "now"
            else f"突破追{'多' if p.direction == 'long' else '空'} @ {chk.ref:g} (entry_type=breakout)" if chk.how == "breakout"
            else f"欧易挂限价{'买' if p.direction == 'long' else '卖'}单 @ {chk.ref:g} (entry_type=pullback)"
        )
        tps = " / ".join(f"{t:g}" for t in p.take_profits)
        if not c["ok"]:
            verdict = f"✗ 不合格：{c['why_not']}"
        elif c.get("execution_blocker"):
            verdict = f"暂停入场：{c['execution_blocker']}"
        else:
            verdict = f"✓ 通过结构与成本核验（净盈亏比 {chk.net_rr:.2f} ≥ {min_rr:g}）"
        cost_r = f"，成本占 {chk.cost_pct / chk.stop_pct:.2f}R" if chk.stop_pct else ""
        score = f"｜机械照做的回测：{scores[c['strategy']]}" if c["strategy"] in scores else "｜这条打法还没有回测成绩"
        if c.get("losing"):
            score += "（没有优势：要做必须在 plan.edge 写出算法看不到的额外依据）"
        lines.append(f"{i}. [{c['strategy']} · {c['align']}] {c['basis']} → {how}，止损 {p.stop_loss:g}，目标 {tps}{cost_r}｜{verdict}{score}")
    eligible = [c for c in rows if c["ok"] and not c.get("execution_blocker") and not c["align"].startswith("逆")]
    preferred = next((c for c in eligible if not c.get("losing")), None)
    if (snapshot.get("orderbook") or {}).get("spread_pct") is None:
        lines.append("点差缺失，表中成本未计实际点差；先核实，不按这个排名直接进场。")
    elif preferred is not None:
        check = preferred["check"]
        lines.append(f"优先核实「{preferred['strategy']}」@ {check.ref:g}，再对比其余合格候选。"
                     + ("若符合目标且只差价格回到入场位，可填写 pullback 计划让限价单等价。" if check.limit else "")
                     + "仍缺确认或不满足账户、风险条件时，说明具体依据。排名不代表盈利概率。")
    elif eligible:
        lines.append(NO_EDGE_HINT)
    else:
        lines.append("本轮没有可优先核实的候选。说明哪项条件未满足，以及下一次复核的条件。")
    return "\n".join(lines)
