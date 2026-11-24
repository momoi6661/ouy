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
IMPULSE_PLAYS = ("收敛突破", "趋势突破跟随", "动量延续", "开盘区间突破")
TREND_PLAYS = ("趋势回踩", "前高前低法", "变盘后回踩")


def miss_price(plan: TradePlan, ref: float, px: float | None = None) -> float:
    """挂单未成交前的顺向作废线：至少留出 0.8R 且不少于入场挂距的 80%，防止现价贴近第一目标时刚挂上去几秒就被误撤。"""
    long = plan.direction == "long"
    tp1 = min(plan.take_profits) if long else max(plan.take_profits)
    if not px or px <= 0:
        return tp1
    risk = abs(ref - plan.stop_loss)
    floor = _sig(px + (1 if long else -1) * max(risk * 0.8, abs(ref - px) * 0.8))
    return max(tp1, floor) if long else min(tp1, floor)


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


def check_plan(
    plan: TradePlan,
    px: float,
    *,
    min_rr: float,
    cost_pct: float,
    limit_cost_pct: float | None = None,
    max_dist_pct: float = 0.0,
) -> PlanCheck:
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
            if max_dist_pct > 0 and abs(out.ref / px - 1) * 100 > max_dist_pct:
                dist_now = abs(out.ref / px - 1) * 100
                out.issues.append(
                    f"挂单价 {out.ref:g} 离现价 {px:g} 达 {dist_now:.2f}%，超过当前风格允许的挂单距离上限 {max_dist_pct:g}%（挂太远根本成交不了）："
                    "请改为现价附近直接进场（entry_type=now），或改挂在离现价更近的 5m/15m 支撑阻力位"
                )
                return out
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
    elif cost_pct > 0 and out.tp_pct < cost_pct * 3.0:
        out.issues.append(
            f"到第一目标只有 {out.tp_pct:.2f}%，不到往返成本（手续费+点差 {cost_pct:.2f}%）的 3 倍，赚的还不够交手续费："
            f"把第一目标放到至少 {cost_pct * 3.0:.2f}% 外的真实高/低点，或者放弃这笔微利单（no_plan_reason 选 cost）"
        )
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
    """进场之后交给执行器的管理规则：
    - 突破与动量脉冲单（IMPULSE_PLAYS / breakout）：关掉 0.5R 过早保盈锁（lock_r=0），1.3R 移保本，首目标只平 35%，余仓用结构 ATR 吊灯止损跟完整段脉冲，并带迟滞时间证伪；
    - 顺势回踩单（TREND_PLAYS）：保盈线推迟到 1.2R，首目标平 40%，余仓启用 ATR 跟踪；
    - 普通与震荡区间单：保持 1R 移保本、首目标平一半、按风格时间止损。"""
    spec = STYLES[style]
    long = plan.direction == "long"
    tps = sorted(plan.take_profits, reverse=not long)
    impulse = check.how == "breakout" or plan.strategy in IMPULSE_PLAYS
    trend_play = not impulse and plan.strategy in TREND_PLAYS
    rules: dict[str, Any] = {"breakeven_r": 1.5}
    if impulse:
        rules.update(
            lock_r=0,
            trail_atr=spec.get("trail_atr", 3.5),
            trail_bar=spec.get("trail_bar", spec["struct_bar"]),
            trail_start_r=1.5,
        )
        if spec.get("stale_impulse_min"):
            rules.update(stale_minutes=spec["stale_impulse_min"], stale_min_r=0.4)
    elif trend_play:
        rules.update(
            lock_r=1.5,
            trail_atr=spec.get("trail_atr", 3.5),
            trail_bar=spec.get("trail_bar", spec["struct_bar"]),
            trail_start_r=1.5,
        )
    if plan.review_level and min(check.ref, plan.stop_loss) < plan.review_level < max(check.ref, plan.stop_loss):
        rules.update(review_level=plan.review_level, review_bar=spec["struct_bar"])
    if len(tps) >= 2 and check.stop_pct:
        frac = 0.35 if impulse else 0.4 if trend_play else 0.5
        rules["partials"] = [{"r": round(abs(tps[0] - check.ref) / check.ref * 100 / check.stop_pct, 2), "fraction": frac}]
    hold = spec.get("impulse_hold_minutes") if impulse else spec.get("hold_minutes")
    if hold:
        rules["max_hold_minutes"] = hold
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


def order_params(plan: TradePlan, check: PlanCheck, style: str, px: float | None = None) -> dict[str, Any]:
    """到价进场的策略参数（check.how 是 pullback 或 breakout 时用）。"""
    spec = STYLES[style]
    params: dict[str, Any] = {
        "terms": plan_terms(plan, style), "trigger": check.ref, "stop_loss": plan.stop_loss, "take_profit": far_target(plan), "leverage": safe_leverage(plan.leverage, check.stop_pct),
        "risk_pct": plan.risk_pct, "margin_usdt": plan.margin_usdt, "expires_minutes": plan.valid_minutes or spec["plan_minutes"], "manage": manage_rules(plan, check, style), "plan": True,
    }
    if check.how == "pullback":  # 没回到入场区间就顺向走远了：这一段已经走完，不再等
        params["miss_px"] = miss_price(plan, check.ref, px)
    else:
        params["max_chase_pct"] = check.chase_pct
        params["confirm_bar"] = spec.get("confirm_bar", "1m")
        params["confirm_vol"] = 1.25
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
WAVE_BY = "实时拐点走向"


def major_direction(snapshot: dict[str, Any], style: str, shift: dict[str, Any] | None = None) -> tuple[str, str]:
    # 盘中实时走向（live_wave）不参与定大方向：1m 回放里它切换后 15 分钟继续同向的只有约 45%（见 knowledge/evidence.md）
    if style in TURN_FIRST and shift and shift.get("side") in ("long", "short"):
        return shift["side"], TURN_BY
    win_major, win_name = "", ""
    windows = snapshot.get("windows") or {}
    for name in TREND_FRAMES[style]:
        word = str((windows.get(name) or {}).get("trend") or "")
        if word.startswith(("上涨", "下跌")):
            win_major, win_name = ("long" if word.startswith("上涨") else "short"), name
            break
    return win_major, win_name


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
        "in_regime": bool(c.get("in_regime", True)),
    } for c in rows]


def market_regime(snapshot: dict[str, Any], style: str, shift: dict[str, Any] | None = None, follow_trend: bool = False) -> dict[str, str]:
    """先判断现在是什么行情，再决定用哪类打法：
    reversal 刚确认的结构反转（5 分钟收盘反向越过摆动点）；trend 大方向明确、结构周期高低点同向且 ADX 够；
    range 结构周期没有方向、ADX 低；mixed 其余（大方向和结构周期打架）。
    follow_trend（出手尺度「积极」）：打架时不停手，按大周期趋势做；大周期没有方向就按结构周期的趋势做。"""
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
        if tf.get("squeeze"):
            bw = tf.get("bb_width_pct")
            detail = f"（布林带宽 {bw:g}% 收口蓄势）" if bw else "（波动率收口蓄势）"
            return {"kind": "range", "side": "", "why": f"{STYLES[style]['struct_bar']}没有方向{detail}，ADX {adx:.0f}"}
        return {"kind": "range", "side": "", "why": f"{STYLES[style]['struct_bar']}没有方向，ADX {adx:.0f}"}
    if follow_trend and (major or local):
        word = {"long": "向上", "short": "向下"}
        by = f"大周期趋势{word[major]}" if major else f"大周期没有方向，{STYLES[style]['struct_bar']}趋势{word[local]}"
        return {"kind": "trend", "side": major or local, "why": f"{by}，按趋势做（出手尺度：积极），ADX {adx:.0f}"}
    return {"kind": "mixed", "side": major, "why": f"大方向{'向上' if major == 'long' else '向下' if major == 'short' else '不明'}，{STYLES[style]['struct_bar']}结构{'同向' if local == major else '不一致'}，ADX {adx:.0f}"}


# 每种行情用哪几类打法：趋势纳入顺势突破、动量延续、浅回踩及关键位假突破反转（踩支撑收回）；震荡与收口蓄势做区间边缘、假突破收回与收敛突破。
REGIME_PLAYS = {
    "trend": ("收敛突破", "动量延续", "趋势回踩", "前高前低法", "假突破反转"),
    "range": ("区间高抛低吸", "假突破反转", "收敛突破"),
    "reversal": ("变盘后回踩", "假突破反转", "收敛突破"),
    "mixed": ("假突破反转",),
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
    follow_trend: bool = False,
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
    atr_frame = frames.get(spec.get("atr_bar", spec["struct_bar"])) or tf
    wide_atr = float(atr_frame.get("atr14") or atr)
    has_box = bool((tf.get("range") or {}).get("upper") or len(frames) > 1)
    stop_floor = px * (spec.get("min_stop_pct", 0.25) / 100 if has_box else 0.0025)
    spread = float(((snapshot.get("orderbook") or {}).get("spread_pct")) or 0.0)
    taker_cost = trade_cost_pct(taker_fee_pct, spread)
    limit_cost = maker_fee_pct + taker_fee_pct + max(spread, 0.0)
    windows = snapshot.get("windows") or {}

    major, _ = major_direction(snapshot, style, shift)
    if follow_trend and not major:  # 大周期没有方向：按结构周期的趋势做
        major = {"uptrend": "long", "downtrend": "short"}.get(tf.get("structure"), "")

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
        # 跳过离入场太近（不足 1.15R）的贴身微噪音高低点，防止为了凑盈亏比被迫把挂单推到天边
        min_gap = risk * max(min_rr * 1.15, 1.2) if has_box else risk * 0.5
        pool = sorted({v for v in (ups if long else downs) if (v - entry if long else entry - v) >= min_gap}, reverse=not long)
        t1 = _sig(pool[0]) if pool else _sig(entry + (1 if long else -1) * risk * max(min_rr + 0.4, 1.8))
        t2 = _sig(pool[1]) if len(pool) > 1 else _sig(t1 + (1 if long else -1) * risk)
        return [t1] if t1 == t2 else [t1, t2]

    raw: list[tuple[str, str, TradePlan]] = []
    # 1. 变盘后顺向启动与回踩（刚发生结构转折：刚破位未跑远直接现价跟进抓主升/主跌浪，跑远了在突破位挂回踩单）
    if shift and shift.get("bars_ago", 99) <= 8 and shift.get("level") and shift.get("extreme") and shift.get("origin"):
        side = shift["side"]
        long, sign = side == "long", (1 if side == "long" else -1)
        lv = float(shift["level"])
        stop = _sig(lv - sign * max(wide_atr, stop_floor))
        leg = abs(float(shift["extreme"]) - float(shift["origin"]))
        just_broke = shift.get("bars_ago", 99) <= 1 and 0 <= (px - lv) * sign <= 0.65 * wide_atr
        how = "now" if entry_mode != "limit" and (abs(px - lv) <= 0.3 * atr or just_broke) and (px - stop) * sign > 0 else "pullback"
        entry = _sig(px if how == "now" else lv)
        t1 = _sig(max(float(shift["extreme"]), entry + 1.6 * abs(entry - stop)) if long else min(float(shift["extreme"]), entry - 1.6 * abs(entry - stop)))
        t2 = _sig(t1 + sign * max(leg * 0.6, 1.5 * abs(entry - stop)))
        if (t1 - entry) * sign > 0 and (entry - stop) * sign > 0:
            raw.append(("变盘后回踩", f"5m {shift['kind']}越过 {lv:g} {'刚启动顺势跟进' if how == 'now' else '回踩位挂单'}", TradePlan(
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
            stop = _sig(max(hi, px) + max(0.6 * atr, stop_floor))
            how = "now" if entry_mode != "limit" and px <= hi and abs(hi - px) <= 0.65 * wide_atr else "pullback"
            entry = _sig(px if how == "now" else hi)
            tps = targets_from(entry, stop, False)
            if tps and entry < stop:
                raw.append(("假突破反转", f"{wname} 刺穿前高 {hi:g} 后收回，{'现价直接做空' if how == 'now' else '近端挂空'}", TradePlan(
                    direction="short", strategy="假突破反转", entry_low=entry, entry_high=entry, stop_loss=stop,
                    take_profits=tps, leverage=20, entry_type=how, invalidation=f"连续收盘站上 {stop:g}",
                )))
        elif now_read.startswith("假跌破") and hi > lo:
            stop = _sig(min(lo, px) - max(0.6 * atr, stop_floor))
            how = "now" if entry_mode != "limit" and px >= lo and abs(px - lo) <= 0.65 * wide_atr else "pullback"
            entry = _sig(px if how == "now" else lo)
            tps = targets_from(entry, stop, True)
            if tps and entry > stop:
                raw.append(("假突破反转", f"{wname} 刺穿前低 {lo:g} 后收回，{'现价直接做多' if how == 'now' else '近端挂多'}", TradePlan(
                    direction="long", strategy="假突破反转", entry_low=entry, entry_high=entry, stop_loss=stop,
                    take_profits=tps, leverage=20, entry_type=how, invalidation=f"连续收盘跌破 {stop:g}",
                )))

    # 2b. 近端支撑踩稳 / 阻力受阻现价直进（解决「都已经踩住收回了还不买、跑去老远处挂单」）
    patterns_text = " ".join(str(p) for p in (tf.get("patterns") or []))
    near_sup = max((v for v in downs if v < px * 1.0005), default=None)
    near_res = min((v for v in ups if v > px * 0.9995), default=None)
    if entry_mode != "limit" and near_sup and 0 <= (px - near_sup) <= 0.6 * wide_atr:
        bull_hold = any(k in patterns_text for k in ("假跌破", "长下影线", "看涨吞没"))
        if bull_hold:
            stop = _sig(near_sup - max(0.7 * wide_atr, stop_floor))
            if px > stop:
                raw.append(("假突破反转", f"近端支撑 {near_sup:g} 已踩稳收回（{spec['struct_bar']} 承接信号），现价直接做多", TradePlan(
                    direction="long", strategy="假突破反转", entry_low=_sig(px), entry_high=_sig(px), stop_loss=stop,
                    take_profits=targets_from(px, stop, True), leverage=20, entry_type="now", invalidation=f"{spec['struct_bar']} 收盘跌破 {stop:g}",
                )))
    if entry_mode != "limit" and near_res and 0 <= (near_res - px) <= 0.6 * wide_atr:
        bear_hold = any(k in patterns_text for k in ("假突破上沿", "长上影线", "看跌吞没"))
        if bear_hold:
            stop = _sig(near_res + max(0.7 * wide_atr, stop_floor))
            if px < stop:
                raw.append(("假突破反转", f"近端阻力 {near_res:g} 上冲受阻收回（{spec['struct_bar']} 承压信号），现价直接做空", TradePlan(
                    direction="short", strategy="假突破反转", entry_low=_sig(px), entry_high=_sig(px), stop_loss=stop,
                    take_profits=targets_from(px, stop, False), leverage=20, entry_type="now", invalidation=f"{spec['struct_bar']} 收盘站上 {stop:g}",
                )))

    # 3. 最近结构支撑 / 阻力挂单（趋势回踩或区间高抛低吸）及 EMA20 顺势浅回踩
    regime = market_regime(snapshot, style, shift, follow_trend)
    boxed = regime["kind"] == "range"  # 震荡/收敛：两头按区间单保留，不因叫「趋势回踩」被筛掉
    e20 = float(tf.get("ema20") or 0)
    adx_val = float(tf.get("adx14") or 0)
    last_close = float(tf.get("last_closed_price") or px)
    vol_ratio = float(tf.get("volume_ratio") or 0)
    if regime["kind"] == "trend" and e20 > 0 and adx_val >= 20 and regime["side"] in ("long", "short") and entry_mode != "limit":
        t_long = regime["side"] == "long"
        t_sign = 1 if t_long else -1
        if -0.2 * atr <= (px - e20) * t_sign <= 1.15 * wide_atr and (last_close - e20) * t_sign >= 0:
            stop = _sig(e20 - t_sign * max(1.15 * wide_atr, stop_floor))
            if (px - stop) * t_sign > 0:
                risk_m = abs(px - stop)
                raw.append(("动量延续", f"顺 1H/结构周期趋势在 {spec['struct_bar']} EMA20（{e20:g}）同侧推进，现价直接跟进", TradePlan(
                    direction=regime["side"], strategy="动量延续", entry_low=_sig(px), entry_high=_sig(px), stop_loss=stop,
                    take_profits=[_sig(px + t_sign * 2.0 * risk_m), _sig(px + t_sign * 4.0 * risk_m)],
                    leverage=20, entry_type="now", invalidation=f"{spec['struct_bar']} 收盘跌破 {stop:g}" if t_long else f"{spec['struct_bar']} 收盘站上 {stop:g}",
                )))
    supports = {float(s["price"]) for s in tf.get("support") or [] if isinstance(s, dict) and s.get("price") and 0 < float(s["price"]) < px}
    if regime["kind"] == "trend" and regime["side"] == "long" and 0 < e20 < px:
        supports.add(_sig(e20))
    for lv in sorted(supports | ({near_sup} if near_sup else set()), reverse=True)[:3]:
        stop = _sig(lv - max(0.8 * wide_atr, stop_floor))
        how = "now" if entry_mode != "limit" and px >= lv and abs(px - lv) <= 0.45 * atr else "pullback"
        entry = _sig(px if how == "now" else lv)
        if lv != near_sup and lv != _sig(e20) and not any(v > px and v - entry >= (entry - stop) * 0.5 for v in ups):
            continue  # 备选挂位必须有已知的首目标，不为凑候选外推首目标
        strat = "趋势回踩" if major == "long" and not boxed else "区间高抛低吸"
        raw.append((strat, f"依托支撑 {lv:g} 做多", TradePlan(
            direction="long", strategy=strat, entry_low=entry, entry_high=entry, stop_loss=stop,
            take_profits=targets_from(entry, stop, True), leverage=20, entry_type=how, invalidation=f"{spec['struct_bar']} 收盘跌破 {stop:g}",
        )))
    resistances = {float(r["price"]) for r in tf.get("resistance") or [] if isinstance(r, dict) and r.get("price") and float(r["price"]) > px}
    if regime["kind"] == "trend" and regime["side"] == "short" and e20 > px:
        resistances.add(_sig(e20))
    for lv in sorted(resistances | ({near_res} if near_res else set()))[:3]:
        stop = _sig(lv + max(0.8 * wide_atr, stop_floor))
        how = "now" if entry_mode != "limit" and px <= lv and abs(lv - px) <= 0.45 * atr else "pullback"
        entry = _sig(px if how == "now" else lv)
        if lv != near_res and lv != _sig(e20) and not any(v < px and entry - v >= (stop - entry) * 0.5 for v in downs):
            continue
        strat = "趋势回踩" if major == "short" and not boxed else "区间高抛低吸"
        raw.append((strat, f"依托阻力 {lv:g} 做空", TradePlan(
            direction="short", strategy=strat, entry_low=entry, entry_high=entry, stop_loss=stop,
            take_profits=targets_from(entry, stop, False), leverage=20, entry_type=how, invalidation=f"{spec['struct_bar']} 收盘站上 {stop:g}",
        )))

    # 4. 箱体 / 收敛起爆（自动档、顺势档、突破档共用：解决快速单边走势踏空）
    # - 已经放量收出箱体且未跑远：直接给 entry_type=now 顺势上车；
    # - 正在箱体边界附近蓄势：预埋 entry_type=breakout，由执行器在带量越界第一下秒级点火。
    if quant_mode in ("auto", "regime", "trend", "breakout") and rng.get("upper") and rng.get("lower"):
        hi_r, lo_r = float(rng["upper"]), float(rng["lower"])
        span = hi_r - lo_r
        is_squeeze = bool(tf.get("squeeze")) or quant_mode == "breakout"
        if span > 0:
            for b_side, edge in (("long", hi_r), ("short", lo_r)):
                b_long = b_side == "long"
                b_sign = 1 if b_long else -1
                if major and major != b_side and quant_mode != "breakout":
                    continue
                if not major and not is_squeeze and not follow_trend:
                    continue
                b_risk = max(1.8 * wide_atr, stop_floor)
                b_stop = _sig(edge - b_sign * b_risk)
                if (edge - b_stop) * b_sign <= 0:
                    continue
                beyond = (px - edge) * b_sign
                closed_out = (last_close - edge) * b_sign > 0
                if entry_mode != "limit" and closed_out and vol_ratio >= 1.25 and 0 <= beyond <= 0.85 * wide_atr and (px - b_stop) * b_sign > 0:
                    act_risk = abs(px - b_stop)
                    raw.append(("收敛突破", f"{spec['struct_bar']} 放量（{vol_ratio:.1f}x）突破箱体{'上沿' if b_long else '下沿'} {edge:g} 起爆", TradePlan(
                        direction=b_side, strategy="收敛突破", entry_low=_sig(px), entry_high=_sig(px), stop_loss=b_stop,
                        take_profits=[_sig(px + b_sign * max(span, 2.0 * act_risk)), _sig(px + b_sign * max(span * 1.8, 4.2 * act_risk))],
                        leverage=20, entry_type="now", invalidation=f"{'跌回' if b_long else '收回'} {edge:g} 箱体内侧",
                    )))
                elif beyond < 0 and (is_squeeze or quant_mode == "breakout") and abs(beyond) <= 2.2 * wide_atr:
                    far_edge = _sig(edge * (1.003 if b_long else 0.997))
                    lo_e, hi_e = (_sig(edge), far_edge) if b_long else (far_edge, _sig(edge))
                    raw.append(("收敛突破", f"波动收口贴近箱体{'上沿' if b_long else '下沿'} {edge:g}，突破秒级追{'多' if b_long else '空'}", TradePlan(
                        direction=b_side, strategy="收敛突破", entry_low=lo_e, entry_high=hi_e, stop_loss=b_stop,
                        take_profits=[_sig(edge + b_sign * max(span, 2.0 * b_risk)), _sig(edge + b_sign * max(span * 1.8, 4.2 * b_risk))],
                        leverage=20, entry_type="breakout", invalidation=f"{'跌回' if b_long else '收回'} {edge:g} 箱体内侧",
                    )))

    from ..backtest_plans import losing

    if quant_mode in ("auto", "regime") and regime["kind"] == "unknown":
        return []
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
        in_regime = bool(
            quant_mode not in ("auto", "regime")
            or (name in REGIME_PLAYS[regime["kind"]] and (not regime["side"] or regime["kind"] == "range" or name == "假突破反转" or plan.direction == regime["side"]))
        )
        max_dist = float(spec.get("limit_dist_pct") or 0.0) if has_box else 0.0
        check = check_plan(plan, px, min_rr=min_rr, cost_pct=taker_cost, limit_cost_pct=limit_cost, max_dist_pct=max_dist)
        stale = "" if check.how in ("now", "breakout") else line_status(px, check.ref, plan.direction, shift, atr, spec["plan_minutes"], spec["bar_minutes"])
        align = "顺大方向" if major == plan.direction else "逆大方向" if major else "震荡边缘"
        ok = check.ok and not stale
        out.append({
            "strategy": name, "basis": basis, "align": align, "ok": ok, "in_regime": in_regime, "plan": plan, "check": check,
            "why_not": stale or ("；".join(check.issues) if not check.ok else ""),
            "dist_pct": abs(check.ref / px - 1) * 100 if check.ref and px else 0.0,
            "losing": name in bad,  # 只打标记作参考，不再用来拦单
        })
    preferred = {"limit": "pullback", "market": "now"}.get(entry_mode)
    squeeze_now = bool(tf.get("squeeze"))
    out.sort(key=lambda c: (
        not c["ok"],
        not c["in_regime"],
        0 if c["check"].how == "now" else 1 if (squeeze_now and c["strategy"] == "收敛突破") else 2,
        0 if c["align"] == "顺大方向" else 1 if c["align"] == "震荡边缘" or c["strategy"] == "假突破反转" else 2,
        bool(preferred and c["check"].how != preferred),
        c["dist_pct"],
    ))
    # 先核验、排序，再对相近价位去重，保留双向结构位给大模型综合判断。
    picked = []
    for candidate in out:
        plan = candidate["plan"]
        key = (plan.direction, round(plan.entry_low / max(atr * 0.25, px * 1e-4)))
        if key not in seen_keys:
            seen_keys.add(key)
            picked.append(candidate)
    return picked[:6]


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
    follow_trend: bool = False,
) -> str:
    """把算法算好的候选计划排成清清楚楚的几行喂给 Worker。"""
    from ..models import QUANT_MODES

    mlabel = QUANT_MODES.get(quant_mode, QUANT_MODES["auto"])["label"]
    if quant_mode == "quiet":
        return f"【算法候选位｜当前档位：{mlabel}】已暂停新开仓候选（只保护已有持仓与清理过时挂单）；盘面恢复清晰后可用 set_quant_mode 切回对应档位。"
    rows = rows if rows is not None else build_candidates(
        snapshot, style=style, min_rr=min_rr, maker_fee_pct=maker_fee_pct, taker_fee_pct=taker_fee_pct,
        shift=shift, quant_mode=quant_mode, quant_bias=quant_bias, entry_mode=entry_mode, follow_trend=follow_trend,
    )
    regime = market_regime(snapshot, style, shift, follow_trend)
    head = (f"【盘面地图（程序算的客观结构，不替你做决定）】行情分类：{REGIME_LABEL[regime['kind']]}（{regime['why']}）。\n"
            if quant_mode == "auto" else "")
    if not rows:
        if quant_mode == "auto" and regime["kind"] == "unknown":
            return head + "结构周期数据不足，先等待有效收盘。"
        return head + f"【结构参考位｜{mlabel}】当前没有算出参考位；你可以直接看 K 线结构自己定 plan，或说明等待条件。"
    btag = "，只盯多" if quant_bias == "long" else "，只盯空" if quant_bias == "short" else ""
    from ..backtest_plans import evidence

    scores = evidence(style)
    lines = [head + f"【双向结构参考位（当前档位：{mlabel}{btag}｜已计入手续费与点差、已查可达性）】"
             "分工：程序只提供盘面地图、参考价位和成本核对，不替你否决方向；你结合 K 线、盘口、资金费率和用户目标自己拍板，"
             "可直接采纳参考位，也可按结构微调止损和目标。纯算法机械回测只是「不带脑子照做」的参考，不再用来禁掉你的单。"]
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
            verdict = f"✗ 默认公式未过核对：{c['why_not']}"
        elif c.get("execution_blocker"):
            verdict = f"暂停入场：{c['execution_blocker']}"
        else:
            verdict = f"✓ 通过成本与结构核对（净盈亏比 {chk.net_rr:.2f} ≥ {min_rr:g}）"
        cost_r = f"，成本占 {chk.cost_pct / chk.stop_pct:.2f}R" if chk.stop_pct else ""
        score = f"｜纯算法盲做回测：{scores[c['strategy']]}" if c["strategy"] in scores else ""
        if c.get("losing") and score:
            score += "（仅作参考，不限制你的判断）"
        tag = "主推" if c.get("in_regime", True) else "备选"
        lines.append(f"{i}. [{tag} · {c['strategy']} · {c['align']}] {c['basis']} → {how}，止损 {p.stop_loss:g}，目标 {tps}{cost_r}｜{verdict}{score}")
    eligible = [c for c in rows if c["ok"] and not c.get("execution_blocker")]
    preferred = next((c for c in eligible if c.get("in_regime", True) and not c["align"].startswith("逆")), None) or (eligible[0] if eligible else None)
    if (snapshot.get("orderbook") or {}).get("spread_pct") is None:
        lines.append("点差缺失，表中成本未计实际点差；先核实再决定。")
    elif preferred is not None:
        check = preferred["check"]
        lines.append(f"优先看「{preferred['strategy']}」@ {check.ref:g}，同时对照另一侧结构位。"
                     + ("方向认可且只差到价，本轮直接给 pullback 计划挂限价单等成交；也可以按结构微调止损和目标。" if check.limit else "方向认可可直接给 plan，或按结构微调止损和目标。"))
    else:
        lines.append("以上参考位按默认固定公式（如 0.8×ATR 止损）未通过核对；如果你看盘面有更合理的结构止损和目标，可直接自己填 plan，程序会按你的价位重核成本。")
    return "\n".join(lines)
