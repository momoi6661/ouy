"""候选计划回测：把真实历史 K 线一步一步喂给「算法候选位」（plan.build_candidates，和 Worker 每轮看到的是同一套），
每一步都照着排在最前面的合格候选下单，按程序实盘的规则模拟成交和管理，看各条打法扣掉手续费之后赚不赚。

它回答的问题是：如果 Worker 每次都老老实实照算法给的第一条合格候选做，结果怎么样。Worker 的取舍（看大盘、看消息）不在里面。

用法：python -m ouy.backtest_plans BTC-USDT-SWAP ETH-USDT-SWAP --days 30 --style intraday --stance balanced
规则：每走完一根入场周期的 K 线重新算一次候选；等回踩的单挂限价（挂单费率），没成交先到第一目标或过了有效期就撤；
现价单下一根开盘进（吃单费率）；浮盈到 1R 移保本、到第一目标平一半、超过持仓时间没走出来离场。
同一根 K 线里止损和目标都碰到，按先打止损算（保守）。只用已经收盘的 K 线，没有偷看未来。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .agent.plan import IMPULSE_PLAYS, REGIME_LABEL, build_candidates, market_regime
from .agent.signals import structure_shift
from . import history
from .config import ROOT, Settings
from .indicators import BAR_MINUTES, Candle, mean_ci, summarize, window_levels
from .models import STANCES, STYLES

SPREAD_PCT = 0.01
REPORT_DIR = ROOT / "data" / "backtest"


@dataclass(frozen=True)
class Rules:
    """成交和持仓管理的规则。默认值 = 程序实盘的做法 + 保守的成交假设；改实盘管理规则前先在这里换参数对比。"""
    maker: float = 0.02
    taker: float = 0.05
    spread: float = SPREAD_PCT
    slip: float = 0.02  # 市价进场、止损出场各多付这么多 %（盘口吃穿一两档）
    through: float = 0.01  # 限价单：价格要穿过挂单价这么多 % 才算成交。只碰一下时排在后面的单多半成交不了，按成交算会高估
    be_r: float | None = 1.0  # 浮盈到几 R 把止损移到入场价；None = 不移
    t1_part: float = 0.5  # 有两个目标时第一目标平掉的比例
    stop_scale: float = 1.0  # 止损距离放大倍数（目标不变），只用来研究止损宽窄
    trail_atr: float | None = None  # 第一目标之后余仓改用「最高价 − 几倍结构周期 ATR」跟踪，不再等第二目标
    lock_r: float | None = None  # 浮盈最高到过这么多 R 后，止损移到「入场价 + 往返成本」，模拟持仓保护器
    lock_close: bool = False  # 浮盈按细周期 K 线收盘算（不看影线），对应实盘按已收盘 K 线确认
    shift_rule: str | None = None  # 持仓期间 5 分钟结构反向转折：exit 下一根开盘离场；tighten 止损收到这一段极值外侧


@dataclass(frozen=True)
class Setup:
    """某一刻算法给出的一条合格候选。"""
    ts: int  # 结构周期收盘、候选算出来的时刻
    strategy: str
    align: str
    side: str
    how: str  # pullback 挂限价等 / now 下一根开盘进
    entry: float
    stop: float
    t1: float
    t2: float
    valid_minutes: int
    regime: str
    atr: float


@dataclass
class Result:
    ts: int
    strategy: str
    align: str
    side: str
    how: str  # 限价 / 现价
    r_net: float
    pct_net: float
    out: str
    mfe_r: float = 0.0  # 出场前最大浮盈（R，按细周期 K 线极值）
    regime: str = ""
    inst: str = ""
    entry_ts: int = 0
    exit_ts: int = 0
    entry: float = 0.0
    stop: float = 0.0
    exit_px: float = 0.0


def closed_before(series: list[Candle], stamps: list[int], t: int, minutes: int, keep: int = 260) -> list[Candle]:
    """t 这一刻已经收盘的最近 keep 根。"""
    return series[max(bisect_right(stamps, t - minutes * 60_000) - keep, 0):bisect_right(stamps, t - minutes * 60_000)]


def shift_events(m5: list[Candle]) -> dict[int, dict[str, Any]]:
    """每根 5 分钟 K 线收盘那一刻，有没有在这一根上发生结构转折（和实盘 structure_shift 同一套算法）。"""
    out = {}
    for i in range(119, len(m5)):
        sh = structure_shift(m5[i - 119:i + 1])
        if sh and sh["bars_ago"] == 0:
            out[m5[i].ts] = sh
    return out


def setups(data: dict[str, list[Candle]], *, style: str = "intraday", stance: str = "balanced", maker: float = 0.02, taker: float = 0.05, mode: str = "auto",
           top: int = 1) -> dict[int, list[Setup]]:
    """每根结构周期 K 线收盘那一刻，算法给出的合格候选（按程序的排序，最多 top 条）。只用那一刻已经收盘的 K 线。

    候选只取决于当时的行情，和手里有没有仓无关，所以先一次算完；之后换成交 / 管理规则对比时不用重算。"""
    spec, min_rr = STYLES[style], STANCES[stance]["min_rr"]
    follow = bool(STANCES[stance].get("follow_trend"))
    bars = [b for b in spec["bars"] if b in data]
    step_bar, fine = spec["struct_bar"], bars[0]
    stamps = {b: [c.ts for c in data[b]] for b in bars}
    m5 = data.get("5m") or data[fine]
    m5_stamps = [c.ts for c in m5]
    start = max(data[b][min(260, len(data[b]) - 1)].ts + BAR_MINUTES[b] * 60_000 for b in bars)
    out: dict[int, list[Setup]] = {}
    for bar in data[step_bar]:
        t = bar.ts + BAR_MINUTES[step_bar] * 60_000  # 这根刚收盘
        if t < start:
            continue
        frames = {b: closed_before(data[b], stamps[b], t, BAR_MINUTES[b]) for b in bars}
        px = frames[fine][-1].close
        snapshot = {"last": px, "timeframes": {b: summarize(frames[b]) for b in bars}, "windows": window_levels(frames), "orderbook": {"spread_pct": SPREAD_PCT}}
        shift = structure_shift(closed_before(m5, m5_stamps, t, 5, 120))
        cands = [c for c in build_candidates(snapshot, style=style, min_rr=min_rr, maker_fee_pct=maker, taker_fee_pct=taker, shift=shift, quant_mode=mode, follow_trend=follow) if c["ok"]]
        if not cands:
            continue
        regime = market_regime(snapshot, style, shift, follow)["kind"]
        atr = float((snapshot["timeframes"].get(spec.get("atr_bar", step_bar)) or snapshot["timeframes"].get(step_bar) or {}).get("atr14") or 0.0)
        rows = []
        for c in cands[:top]:
            plan, check = c["plan"], c["check"]
            if check.how not in ("pullback", "now", "breakout"):
                continue
            tps = sorted(plan.take_profits, reverse=plan.direction != "long")
            rows.append(Setup(t, c["strategy"], c["align"], plan.direction, check.how, check.ref, plan.stop_loss, tps[0], tps[-1],
                              plan.valid_minutes or spec["plan_minutes"], regime, atr))
        if rows:
            out[t] = rows
    return out


def execute(found: dict[int, list[Setup]], data: dict[str, list[Candle]], *, style: str = "intraday", rules: Rules = Rules(),
            shifts: dict[int, dict[str, Any]] | None = None, inst: str = "", pick: Any = None) -> list[Result]:
    """照着每一刻排在最前面的合格候选下单，按 rules 模拟成交和管理。手里有单（挂着或持仓）时不看新的候选。

    保守的地方：同一根 K 线里止损和目标都碰到算先打止损；限价单要被穿过才算成交；成交那一根只认收盘价到没到目标
    （那根的最高 / 最低可能出在成交之前）；开盘就跳过止损按开盘价出。pick(setup) 返回 False 的候选跳过（研究用）。"""
    spec = STYLES[style]
    fine = next(b for b in spec["bars"] if b in data)
    f = data[fine]
    fs = [c.ts for c in f]
    fine_ms = BAR_MINUTES[fine] * 60_000
    if rules.shift_rule and shifts is None:
        shifts = shift_events(data.get("5m") or f)
    hold = spec.get("hold_minutes") or 10**9
    results: list[Result] = []
    busy_until = 0
    for t in sorted(found):
        if t < busy_until:
            continue
        s = next((x for x in found[t] if pick is None or pick(x)), None)
        if s is None:
            continue
        long = s.side == "long"
        sign = 1 if long else -1
        i = bisect_right(fs, t - 1)  # t 之后的第一根细周期 K 线
        if i >= len(f):
            break
        limit = s.how == "pullback"
        is_breakout = s.how == "breakout"
        if limit:  # 挂限价单等：没成交先到顺向作废线、或过了有效期就撤
            expire, entry, filled = t + s.valid_minutes * 60_000, s.entry, False
            need = entry * (1 - sign * rules.through / 100)
            while i < len(f) and f[i].ts < expire:
                k = f[i]
                if (k.low <= need) if long else (k.high >= need):
                    filled = True
                    break
                if (k.high >= s.t1) if long else (k.low <= s.t1):
                    break
                i += 1
            if not filled:
                busy_until = f[min(i, len(f) - 1)].ts + fine_ms
                continue
            j = i  # 成交的这一根里也可能直接打到止损
        elif is_breakout:  # 预埋突破点火单：在有效期内细周期放量收盘越界（或冲出触发价）后进场；先破止损则作废
            expire, filled, entry = t + s.valid_minutes * 60_000, False, 0.0
            while i < len(f) - 1 and f[i].ts < expire:
                k = f[i]
                if (k.low <= s.stop) if long else (k.high >= s.stop):
                    break
                v_win = f[max(0, i - 20):i]
                v_avg = sum(x.volume for x in v_win) / len(v_win) if v_win else 0.0
                loud = not v_avg or k.volume >= v_avg * 1.25
                crossed = (k.close > s.entry and k.close > k.open) if long else (k.close < s.entry and k.close < k.open)
                if crossed and loud and abs(k.close / s.entry - 1) * 100 <= 0.8:
                    entry, j, filled = f[i + 1].open * (1 + sign * rules.slip / 100), i + 1, True
                    break
                i += 1
            if not filled:
                busy_until = f[min(i, len(f) - 1)].ts + fine_ms
                continue
        else:
            entry, j = f[i].open * (1 + sign * rules.slip / 100), i
        stop = entry - sign * abs(s.entry - s.stop) * rules.stop_scale if rules.stop_scale != 1.0 else s.stop
        t1, t2 = s.t1, s.t2
        risk = abs(entry - stop)
        if risk <= 0 or (t1 - entry) * sign <= 0 or (entry - stop) * sign <= 0:
            continue
        cost = (rules.maker + rules.taker + rules.spread) if limit else (rules.taker * 2 + rules.spread)
        slip = entry * rules.slip / 100
        left, got, out, cur_stop, opened, peak, peak_close, exit_px = 1.0, 0.0, "超时离场", stop, f[j].ts, 0.0, 0.0, 0.0
        locked = entry * (1 + sign * cost / 100)
        impulse = is_breakout or s.strategy in IMPULSE_PLAYS
        t1_part = 0.35 if impulse else rules.t1_part
        be_r = 1.3 if impulse and rules.be_r == 1.0 else rules.be_r
        eff_lock_r = None if impulse else rules.lock_r
        eff_trail_atr = rules.trail_atr or (spec.get("trail_atr") if impulse else None)
        eff_hold = (spec.get("impulse_hold_minutes") or hold) if impulse else hold
        stale_min = spec.get("stale_impulse_min", 0) if impulse else 0
        two = t1 != t2
        first = j
        while j < len(f) and left > 0:
            k = f[j]
            worst, best = (k.low, k.high) if long else (k.high, k.low)
            if j == first and limit:
                best = k.close  # 成交那一根的最高 / 最低可能出在成交之前
            if (worst - cur_stop) * sign <= 0:
                protected = (cur_stop - entry) * sign >= 0
                fill = cur_stop if j == first or (k.open - cur_stop) * sign > 0 else k.open  # 开盘就跳过止损：按开盘价出
                exit_px = fill - sign * slip
                got += left * (exit_px - entry) * sign
                out, left = ("保本出场" if protected and left == 1.0 else "止损" if left == 1.0 else "到第一目标后余仓出场"), 0.0
                break
            if left == 1.0 and (best - t1) * sign >= 0:
                if not two:
                    got, left, out, exit_px = got + (t1 - entry) * sign, 0.0, "到目标", t1
                    break
                got, left, out = got + t1_part * (t1 - entry) * sign, 1.0 - t1_part, "到第一目标"
                cur_stop = cur_stop if (cur_stop - entry) * sign >= 0 else entry
            if 0 < left < 1.0 and not eff_trail_atr and (best - t2) * sign >= 0:
                got, left, out, exit_px = got + left * (t2 - entry) * sign, 0.0, "两个目标都到", t2
                break
            peak, peak_close = max(peak, (best - entry) * sign), max(peak_close, (k.close - entry) * sign)
            if be_r and left == 1.0 and (best - entry) * sign >= risk * be_r and (cur_stop - entry) * sign < 0:
                cur_stop = entry
            if eff_lock_r and (peak_close if rules.lock_close else peak) >= risk * eff_lock_r and (cur_stop - locked) * sign < 0:
                cur_stop = locked
            if eff_trail_atr and (left < 1.0 or peak >= 1.2 * risk) and s.atr:
                trail = entry + sign * peak - sign * eff_trail_atr * s.atr
                if (trail - cur_stop) * sign > 0 and (k.close - trail) * sign > 0:
                    cur_stop = trail
            sh = (shifts or {}).get(k.ts + fine_ms - 300_000) if rules.shift_rule and (k.ts + fine_ms) % 300_000 == 0 else None
            if sh and sh["side"] != s.side and k.ts > opened:
                if rules.shift_rule == "exit" and j + 1 < len(f):
                    exit_px = f[j + 1].open
                    got, left, out = got + left * (exit_px - entry) * sign, 0.0, "结构反向离场"
                    j += 1
                    break
                if rules.shift_rule == "tighten":
                    edge = sh["extreme"] - sign * risk * 0.1
                    if (edge - cur_stop) * sign > 0 and (k.close - edge) * sign > 0:
                        cur_stop = edge
            if stale_min and (k.ts - opened) / 60_000 >= stale_min and peak < risk * 0.4 and (k.close - entry) * sign < risk * 0.15:
                exit_px = k.close
                got, left, out = got + left * (k.close - entry) * sign, 0.0, "动能证伪离场"
                break
            if (k.ts - opened) / 60_000 >= eff_hold and (k.close - entry) * sign < risk * 0.5:
                exit_px = k.close
                got, left = got + left * (k.close - entry) * sign, 0.0
                break
            j += 1
        if left > 0:  # 数据走完了还没出场：按最后一根收盘算
            exit_px = f[-1].close
            got, out = got + left * (exit_px - entry) * sign, "数据结束"
        j = min(j, len(f) - 1)
        pct = got / entry * 100 - cost
        how_label = "限价" if limit else "突破" if is_breakout else "现价"
        results.append(Result(t, s.strategy, s.align, s.side, how_label, pct / (risk / entry * 100), pct, out, peak / risk,
                              s.regime, inst, opened, f[j].ts + fine_ms, entry, stop, exit_px))
        busy_until = f[j].ts + fine_ms
    return results


def simulate(data: dict[str, list[Candle]], *, style: str = "intraday", stance: str = "balanced", maker: float = 0.02, taker: float = 0.05, mode: str = "auto",
             rules: Rules | None = None, shifts: dict[int, dict[str, Any]] | None = None, inst: str = "", **changes: Any) -> list[Result]:
    """一步到位：算候选 + 模拟成交。changes 是对 Rules 的改动（lock_r=0.5、shift_rule="exit" 等）。"""
    rules = replace(rules or Rules(maker=maker, taker=taker), **changes)
    return execute(setups(data, style=style, stance=stance, maker=rules.maker, taker=rules.taker, mode=mode), data, style=style, rules=rules, shifts=shifts, inst=inst)


def summary(rows: list[Result]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    streak = worst = 0
    equity = top = drawdown = 0.0
    for r in sorted(rows, key=lambda r: r.exit_ts or r.ts):
        streak = streak + 1 if r.pct_net <= 0 else 0
        worst = max(worst, streak)
        equity += r.r_net
        top = max(top, equity)
        drawdown = max(drawdown, top - equity)
    wins = [r.r_net for r in rows if r.r_net > 0]
    losses = [r.r_net for r in rows if r.r_net <= 0]
    lo, hi = mean_ci([r.r_net for r in rows])
    return {"n": n, "win": len(wins) / n, "avg_r": sum(r.r_net for r in rows) / n, "sum_r": sum(r.r_net for r in rows), "sum_pct": sum(r.pct_net for r in rows),
            "avg_win": sum(wins) / len(wins) if wins else 0.0, "avg_loss": sum(losses) / len(losses) if losses else 0.0, "worst_streak": worst,
            "faded": sum(1 for r in rows if r.r_net < -0.1 and r.mfe_r >= 0.5),
            "pf": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None, "max_dd_r": drawdown, "ci_lo": lo, "ci_hi": hi}


def verdict(s: dict[str, Any]) -> str:
    """样本够不够下结论：区间整个在 0 上面才算赚，整个在 0 下面才算亏。"""
    if s["n"] < 30 or s["ci_lo"] != s["ci_lo"]:
        return "样本太少"
    return "扣费后赚" if s["ci_lo"] > 0 else "扣费后亏" if s["ci_hi"] < 0 else "分不出赚亏"


def line(label: str, s: dict[str, Any]) -> str:
    if not s["n"]:
        return f"{label:<16} 没有成交"
    pf = f"{s['pf']:.2f}" if s["pf"] is not None else "—"
    return (f"{label:<16} {s['n']:>4} 笔  胜率 {s['win'] * 100:>5.1f}%  平均 {s['avg_r']:+.2f}R [{s['ci_lo']:+.2f}, {s['ci_hi']:+.2f}] {verdict(s)}  "
            f"合计 {s['sum_r']:+.1f}R / {s['sum_pct']:+.2f}%  盈亏比 {pf}  最大回撤 {s['max_dd_r']:.1f}R  "
            f"赚时 {s['avg_win']:+.2f}R 亏时 {s['avg_loss']:+.2f}R  最多连亏 {s['worst_streak']}  浮盈≥0.5R 后亏超 0.1R {s['faded']} 笔")


def groups(rows: list[Result]) -> list[tuple[str, dict[str, Any]]]:
    """全部、前后两半（只在一半时间里赚的不算数）、分打法 / 行情 / 顺逆 / 进场方式 / 方向。"""
    out = [("全部", summary(rows))]
    if rows:
        cut = sorted(r.ts for r in rows)[len(rows) // 2]
        out.append(("  前半段", summary([r for r in rows if r.ts < cut])))
        out.append(("  后半段", summary([r for r in rows if r.ts >= cut])))
    out += [(f"  {name}", summary([r for r in rows if r.strategy == name])) for name in sorted({r.strategy for r in rows})]
    out += [(f"  行情:{REGIME_LABEL.get(name, name)}", summary([r for r in rows if r.regime == name])) for name in sorted({r.regime for r in rows if r.regime})]
    out += [(f"  {name[:6]}", summary([r for r in rows if r.align == name])) for name in sorted({r.align for r in rows})]
    out += [(f"  {name}进场", summary([r for r in rows if r.how == name])) for name in ("限价", "现价", "突破") if any(r.how == name for r in rows)]
    out += [(f"  {label}", summary([r for r in rows if r.side == side])) for side, label in (("long", "做多"), ("short", "做空"))]
    return out


def report(rows: list[Result]) -> str:
    return "\n".join(line(label, s) for label, s in groups(rows))


def save_report(name: str, title: str, rows: list[Result], parts: list[tuple[str, dict[str, Any]]], meta: dict[str, Any], root: Path | None = None) -> Path:
    """把一次回测存成 data/backtest/<name>.json：页面上的回测面板和 Worker 看到的「这条打法的回测成绩」都读它。"""
    def clean(v: Any) -> Any:
        return None if isinstance(v, float) and not math.isfinite(v) else round(v, 4) if isinstance(v, float) else v
    ordered = sorted(rows, key=lambda r: r.exit_ts or r.ts)
    total, equity = 0.0, []
    for r in ordered:
        total += r.r_net
        equity.append([r.exit_ts or r.ts, round(total, 3)])
    body = {
        "name": name, "title": title, "meta": meta, "saved_at": int(datetime.now(timezone.utc).timestamp() * 1000),
        "groups": [{"label": label.strip(), "verdict": verdict(s) if s["n"] else "没有成交", **{k: clean(v) for k, v in s.items()}} for label, s in parts],
        "equity": equity,
        "trades": [{"ts": r.entry_ts or r.ts, "exit_ts": r.exit_ts, "inst": r.inst, "side": r.side, "strategy": r.strategy, "how": r.how, "out": r.out,
                    "r": round(r.r_net, 3), "pct": round(r.pct_net, 3), "entry": r.entry, "stop": r.stop, "exit": r.exit_px} for r in ordered],
    }
    path = (root or REPORT_DIR) / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def saved_reports(root: Path | None = None) -> list[dict[str, Any]]:
    out = []
    for path in sorted((root or REPORT_DIR).glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


_GROUPS: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
LOSING = "扣费后亏"


FAST_PREFIX = "快判·"


def _groups(style: str, root: Path | None = None) -> dict[str, dict[str, Any]]:
    """最近一次存盘回测的各组成绩：算法候选位的各条打法，加上快判的各类信号（`ouy.backtest --save`，名字以「快判·」开头）。"""
    fast = {k: g for k, g in _file_groups((root or REPORT_DIR) / f"fast_{style}.json").items() if k.startswith(FAST_PREFIX)}
    return {**_file_groups((root or REPORT_DIR) / f"plans_{style}.json"), **fast}


def _file_groups(path: Path) -> dict[str, dict[str, Any]]:
    """一份存盘报告里的各组成绩（按文件修改时间缓存）。"""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return {}
    hit = _GROUPS.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {g["label"]: g for g in body.get("groups") or [] if isinstance(g, dict) and g.get("label") and g.get("n")}
    _GROUPS[str(path)] = (stamp, out)
    return out


def evidence(style: str, root: Path | None = None) -> dict[str, str]:
    """每条打法最近一次存盘回测的成绩，一句话：给 Worker 和页面看，免得把「算法给了候选」当成「这条打法赚钱」。"""
    return {k: f"{g['n']} 笔平均 {g['avg_r']:+.2f}R，{g['verdict']}" for k, g in _groups(style, root).items()}


def losing(style: str, root: Path | None = None) -> set[str]:
    """回测里机械照做「扣费后亏」的打法（笔数够、置信区间整段在 0 以下）。程序不再推着 Worker 做这些；
    没有存盘报告、样本太少、分不出赚亏的都不算——没证据不等于亏。"""
    return {k for k, g in _groups(style, root).items() if g.get("verdict") == LOSING}


async def load(inst: str, days: float, bars: tuple[str, ...], *, end_ms: int | None = None, offline: bool = False) -> dict[str, list[Candle]]:
    """从本地缓存取数（缺的那一段才联网补），同一份数据反复跑结果才能互相比较。多取一段给指标预热。"""
    need = tuple(dict.fromkeys((*bars, "5m")))
    async with httpx.AsyncClient(timeout=30, proxy=Settings().proxy_url or None) as client:
        return {b: await history.load(inst, b, days + 300 * BAR_MINUTES[b] / 1440, client=client, now_ms=end_ms, refresh=not offline) for b in need}


async def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="候选计划回测：照算法给的第一条合格候选做，看各条打法扣掉成本后赚不赚")
    ap.add_argument("instruments", nargs="+")
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--end", default=None, help="回测到哪一天为止（UTC，YYYY-MM-DD）；不填 = 现在。写死日期，别人才能复现同一份结果")
    ap.add_argument("--offline", action="store_true", help="只用本地缓存 data/candles，不联网")
    ap.add_argument("--style", default="intraday", choices=list(STYLES))
    ap.add_argument("--stance", default="balanced", choices=list(STANCES))
    ap.add_argument("--mode", default="auto", help="量化档位：auto（按行情选打法）/ all（不按行情过滤，旧的自动档）/ trend / range / breakout")
    ap.add_argument("--lock-r", type=float, default=None, help="浮盈最高到过多少 R 后把止损移到入场价 + 往返成本；不填 = 只按 1R 移保本")
    ap.add_argument("--save", action="store_true", help="把结果存到 data/backtest/plans_<风格>.json：页面的回测面板和 Worker 看到的打法成绩都读它")
    ap.add_argument("--ideal", action="store_true", help="理想成交：限价碰到就算成交、没有滑点（旧口径，用来看保守假设差了多少）")
    args = ap.parse_args(argv)
    rules = Rules(lock_r=args.lock_r, **({"through": 0.0, "slip": 0.0} if args.ideal else {}))
    end_ms = int(datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) if args.end else None
    every: list[Result] = []
    for inst in args.instruments:
        data = await load(inst.upper(), args.days, STYLES[args.style]["bars"], end_ms=end_ms, offline=args.offline)
        short = [b for b in STYLES[args.style]["bars"] if len(data.get(b) or []) < 300]
        if short:  # 少一个周期就没法算候选：说清楚缺什么，别的品种照常跑
            print(f"\n===== {inst.upper()}  跳过：{'、'.join(short)} 的 K 线不够 300 根{'（本地缓存里没有，去掉 --offline 联网补）' if args.offline else ''} =====")
            continue
        rows = simulate(data, style=args.style, stance=args.stance, mode=args.mode, rules=rules, inst=inst.upper())
        every += rows
        fine = next(b for b in STYLES[args.style]["bars"] if b in data)
        span = f"{datetime.fromtimestamp(data[fine][0].ts / 1000, timezone.utc):%Y-%m-%d} ~ {datetime.fromtimestamp(data[fine][-1].ts / 1000, timezone.utc):%Y-%m-%d}" if data[fine] else "没有数据"
        missing = {b: history.gaps(rows_, b) for b, rows_ in data.items() if history.gaps(rows_, b)}
        print(f"\n===== {inst.upper()}  {args.days:g} 天（{span} UTC）  风格 {STYLES[args.style]['label']}  尺度 {STANCES[args.stance]['label']}  档位 {args.mode}"
              f"  {'理想成交' if args.ideal else '保守成交'}{f'  缺 K 线 {missing}' if missing else ''} =====")
        print(report(rows))
    if len(args.instruments) > 1:
        print(f"\n===== 合计 =====\n{report(every)}")
    if args.save:
        meta = {"kind": "plans", "instruments": [i.upper() for i in args.instruments], "days": args.days, "end": args.end, "style": args.style, "stance": args.stance,
                "mode": args.mode, "fills": "理想成交" if args.ideal else "保守成交", "lock_r": args.lock_r}
        path = save_report(f"plans_{args.style}", f"算法候选位 · {STYLES[args.style]['label']}", every, groups(every), meta)
        print(f"\n已存到 {path}")


if __name__ == "__main__":
    asyncio.run(main())
