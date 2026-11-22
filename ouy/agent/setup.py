"""程序判断：超短线的币，每收一根 5 分钟 K 线，程序按固定的几项条件打分，够分才去叫 Worker。

这里只有判断，没有模型、没有下单。每一项条件是一个「节点」：现在的数值、过线标准、过没过、偏向哪一边，
原样给界面显示，也原样写进给 Worker 的消息里，让人和 Worker 看到的是同一份依据。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..indicators import Candle

SIDE = {"long": "做多", "short": "做空", "": "没有方向"}
ACTIVE_BARS = 6  # 一次机会最多算 6 根 5 分钟 K 线（30 分钟），过了就不再当它还在


def _node(id: str, label: str, value: str, need: str, ok: bool, side: str = "") -> dict[str, Any]:
    return {"id": id, "label": label, "value": value, "need": need, "ok": ok, "side": side}


def judge(closed: Sequence[Candle], levels: dict[str, float], trend: str, *, need: int = 3, vol_ratio: float = 1.3, near_pct: float = 0.15, pos_side: str = "") -> dict[str, Any]:
    """closed：已收盘的 5 分钟 K 线（旧 → 新）；levels：关键线的名字 → 价位；trend：1 小时的走向描述（上涨… / 下跌… / 震荡…）。"""
    last, prev = closed[-1], closed[-2]
    nodes: list[dict[str, Any]] = []

    # 1. 大方向
    trend_side = "long" if trend.startswith("上涨") else "short" if trend.startswith("下跌") else ""
    nodes.append(_node("trend", "大方向（1 小时）", trend.split("（")[0] or "看不出", "上涨或下跌", bool(trend_side), trend_side))

    # 2. 在关键位：这根 K 线碰到了哪条线
    tol = near_pct / 100
    touched = sorted(((name, lv) for name, lv in levels.items() if last.low <= lv * (1 + tol) and last.high >= lv * (1 - tol)), key=lambda x: abs(x[1] - last.close))
    nearest = min(levels.items(), key=lambda x: abs(x[1] - last.close), default=None)
    if touched:
        value = f"碰到 {touched[0][0]} {touched[0][1]:g}" + (f" 等 {len(touched)} 条" if len(touched) > 1 else "")
    elif nearest:
        value = f"最近的是 {nearest[0]} {nearest[1]:g}，差 {abs(nearest[1] / last.close - 1) * 100:.2f}%"
    else:
        value = "还没算出关键线"
    nodes.append(_node("level", "在关键位", value, f"这根 K 线碰到某条线（±{near_pct:g}%）", bool(touched)))

    # 3. 收盘确认：踩到线后收回去，或者收盘站到线的另一边
    found: dict[str, tuple[str, float, str]] = {}
    for name, lv in touched:
        # 影线要真的碰到这条线（不是只靠近），实体方向要对
        if last.close > lv and last.close > last.open and (last.low <= lv or prev.close <= lv):
            found.setdefault("long", (name, lv, f"探到 {last.low:g} 后收在 {name} {lv:g} 上方" if prev.close > lv else f"收盘站上 {name} {lv:g}"))
        if last.close < lv and last.close < last.open and (last.high >= lv or prev.close >= lv):
            found.setdefault("short", (name, lv, f"冲到 {last.high:g} 后收在 {name} {lv:g} 下方" if prev.close < lv else f"收盘跌破 {name} {lv:g}"))
    confirm_side = next(iter(found)) if len(found) == 1 else ""
    confirm = found.get(confirm_side)
    nodes.append(_node("confirm", "收盘确认", confirm[2] if confirm else "上下两边都有信号，不算" if len(found) == 2 else "这根没有在线上给出方向",
                       "碰线后收回，或收盘站到线外", bool(confirm), confirm_side))

    # 4. 量
    base = [c.volume for c in closed[-21:-1]]
    avg = sum(base) / len(base) if base else 0
    ratio = last.volume / avg if avg else 0
    nodes.append(_node("volume", "量", f"是前 20 根平均的 {ratio:.1f} 倍", f"≥ {vol_ratio:g} 倍", ratio >= vol_ratio))

    def score(side: str) -> int:
        # 收盘确认给了反方向的，这一边扣一分：不能靠「碰线 + 放量」就凑够
        return (1 if trend_side == side else -1 if trend_side else 0) + int(bool(touched)) + int(confirm_side == side) + int(ratio >= vol_ratio) - int(bool(confirm_side) and confirm_side != side)

    scores = {"long": score("long"), "short": score("short")}
    side = confirm_side if confirm_side and scores[confirm_side] >= need else ""

    # 5. 持仓走势（有仓位时才看）：反向的收盘确认，或者量连着缩下去
    weak = ""
    if pos_side:
        against = "short" if pos_side == "long" else "long"
        recent = [c.volume for c in closed[-2:]]
        if confirm_side == against:
            weak = f"出现反向的收盘确认：{confirm[2]}"  # type: ignore[index]
        elif avg and all(v < avg * 0.6 for v in recent):
            weak = f"动能变少：连续两根的量都不到平均的 0.6 倍（{recent[0] / avg:.1f}、{recent[1] / avg:.1f} 倍）"
        nodes.append(_node("hold", "持仓走势", weak or "没有变弱的迹象", "没有反向确认、量没有连着缩", not weak, pos_side))

    return {
        "side": side, "score": scores[side] if side else max(scores.values()), "need": need, "scores": scores, "nodes": nodes,
        "level": confirm[1] if confirm else None, "level_name": confirm[0] if confirm else "", "weak": weak, "bar_ts": last.ts,
        "ratio": ratio, "trend_side": trend_side,
    }


def brief(j: dict[str, Any]) -> str:
    """一行：每个节点过没过，给 Worker 和活动记录用。"""
    return "；".join(f"{n['label']}：{n['value']}（{'过' if n['ok'] else '没过'}）" for n in j["nodes"])
