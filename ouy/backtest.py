"""快判回测：把真实的 1 分钟历史 K 线一根一根喂给快判（signals.detect），照程序实盘时的规则模拟进出场，
看这些信号扣掉手续费之后到底赚不赚。调快判的门槛之前先跑它，不凭感觉改。

用法：python -m ouy.backtest BTC-USDT-SWAP ETH-USDT-SWAP --days 3
模拟规则和实盘一致：信号出在一根 1 分钟 K 线收盘，下一根开盘价进；止损、目标用快判算好的；浮盈到 1R 止损移到保本；
到第一目标平一半、剩下的看第二目标；超过持仓时间没走出来按收盘价离场。同一根 K 线里止损和目标都碰到，按先打止损算（保守）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from . import history
from .agent.plan import check_plan
from .agent.signals import KIND, FastParams, detect, key_lines, structure_shift
from .config import Settings
from .indicators import Candle, mean_ci
from .models import STANCES, STYLES

WARMUP = 1500  # 前面留这么多根 1 分钟 K 线算关键线（300 根 5 分钟）


def to_5m(bars: list[Candle]) -> list[Candle]:
    groups: dict[int, list[Candle]] = {}
    for c in bars:
        groups.setdefault(c.ts // 300_000, []).append(c)
    return [Candle(k * 300_000, g[0].open, max(c.high for c in g), min(c.low for c in g), g[-1].close, sum(c.volume for c in g), len(g) == 5) for k, g in sorted(groups.items())]


@dataclass
class Trade:
    ts: int
    kind: str
    side: str
    strength: int
    entry: float
    stop: float
    r_net: float  # 这一笔扣完成本之后赚了几个 R
    pct_net: float  # 占价格的百分比
    how: str
    exit_ts: int = 0


def simulate(m1: list[Candle], h1: list[Candle], *, style: str = "scalp", min_rr: float = 1.2, cost_pct: float = 0.11, p: FastParams | None = None, cooldown: int = 10,
             breakeven: bool = True, partial: bool = True, hold_minutes: int | None = None) -> list[Trade]:
    p = p or FastParams()
    hold = hold_minutes or STYLES[style].get("hold_minutes") or 30
    trades: list[Trade] = []
    levels: dict[str, float] = {}
    trend, atr5, busy_until, seen, shift = 0, None, 0, 0, None
    for i in range(WARMUP, len(m1) - 1):
        bar = m1[i]
        if bar.ts // 300_000 != m1[i - 1].ts // 300_000 or not levels:  # 走完一根 5 分钟 K 线：重算关键线
            m5 = to_5m(m1[i - WARMUP:i])
            levels, word, atr5 = key_lines(m5, [c for c in h1 if c.ts + 3_600_000 <= bar.ts][-300:])
            shift = structure_shift(m5[-120:])
            trend = 1 if word.startswith("上涨") else -1 if word.startswith("下跌") else 0
        if i < busy_until:
            continue
        sig = detect(m1[i - 59:i + 1], bar.close, levels, atr5=atr5, trend=trend, p=p, shift=shift)
        if not sig or sig.bar_ts == seen:
            continue
        seen = sig.bar_ts
        if not check_plan(sig.plan(20), bar.close, min_rr=min_rr, cost_pct=cost_pct).ok:
            continue
        long, entry = sig.side == "long", m1[i + 1].open
        sign = 1 if long else -1
        risk = abs(entry - sig.stop)
        if risk <= 0 or (sig.targets[0] - entry) * sign <= 0 or (entry - sig.stop) * sign <= 0:
            continue
        stop, t1, t2 = sig.stop, sig.targets[0], sig.targets[1]
        left, got, how, j = 1.0, 0.0, "超时离场", i + 1
        while j < len(m1) and left > 0:
            c = m1[j]
            worst, best = (c.low, c.high) if long else (c.high, c.low)
            if (worst - stop) * sign <= 0:  # 先看止损（保守）
                got += left * (stop - entry) * sign
                how, left = ("保本出场" if stop == entry and left == 1.0 else "止损" if left == 1.0 else "到第一目标后余仓保本"), 0.0
                break
            if left == 1.0 and (best - t1) * sign >= 0:
                if not partial:  # 不分批：第一目标全平
                    got += (t1 - entry) * sign
                    left, how = 0.0, "到目标"
                    break
                got += 0.5 * (t1 - entry) * sign
                left, stop, how = 0.5, entry, "到第一目标"
            if left == 0.5 and (best - t2) * sign >= 0:
                got += 0.5 * (t2 - entry) * sign
                left, how = 0.0, "两个目标都到"
                break
            if breakeven and left == 1.0 and (best - entry) * sign >= risk:  # 浮盈到 1R：止损移到保本
                stop = entry
            if (c.ts - m1[i + 1].ts) / 60_000 >= hold and (c.close - entry) * sign < risk * 0.5:
                got += left * (c.close - entry) * sign
                left = 0.0
                break
            j += 1
        if left > 0:  # 数据走完了还没出场：按最后一根收盘价算
            got += left * (m1[-1].close - entry) * sign
        pct = got / entry * 100 - cost_pct
        trades.append(Trade(bar.ts, sig.kind, sig.side, sig.strength, entry, sig.stop, pct / (risk / entry * 100), pct, how, m1[min(j, len(m1) - 1)].ts))
        busy_until = max(j + 1, i + 1 + cooldown)
    return trades


def summarize(trades: list[Trade]) -> dict[str, Any]:
    n = len(trades)
    if not n:
        return {"n": 0}
    streak = worst = 0
    for t in trades:
        streak = streak + 1 if t.pct_net <= 0 else 0
        worst = max(worst, streak)
    lo, hi = mean_ci([t.r_net for t in trades])
    return {"n": n, "win": sum(1 for t in trades if t.pct_net > 0) / n, "avg_r": sum(t.r_net for t in trades) / n,
            "sum_pct": sum(t.pct_net for t in trades), "sum_r": sum(t.r_net for t in trades), "worst_streak": worst, "ci_lo": lo, "ci_hi": hi}


def line(label: str, s: dict[str, Any]) -> str:
    if not s["n"]:
        return f"{label:<14} 没有信号"
    ci = f" [{s['ci_lo']:+.2f}, {s['ci_hi']:+.2f}]" if s["n"] >= 2 else ""
    return f"{label:<14} {s['n']:>4} 笔  胜率 {s['win'] * 100:>5.1f}%  平均 {s['avg_r']:+.2f}R{ci}  合计 {s['sum_r']:+.1f}R / {s['sum_pct']:+.2f}%  最多连亏 {s['worst_streak']}"


def report(trades: list[Trade]) -> str:
    rows = [line("全部信号", summarize(trades))]
    rows += [line(f"  {KIND[k]}", summarize([t for t in trades if t.kind == k])) for k in KIND]
    rows += [line(f"  {n} 分", summarize([t for t in trades if t.strength == n])) for n in (1, 2, 3)]
    rows += [line(f"  {'做多' if s == 'long' else '做空'}", summarize([t for t in trades if t.side == s])) for s in ("long", "short")]
    return "\n".join(rows)


def save(style: str, trades: dict[str, list[Trade]], meta: dict[str, Any], root: Any = None) -> Any:
    """存成 data/backtest/fast_<风格>.json，格式和候选位回测一样：页面的回测面板直接能看，
    `backtest_plans.losing` 也读它——快判和算法候选位用同一条规矩，回测扣费后亏的那几类信号程序不再照着下单。"""
    from .backtest_plans import Result, save_report, summary

    rows = [Result(t.ts, f"快判·{KIND[t.kind]}", "", t.side, "现价", t.r_net, t.pct_net, t.how, inst=inst, entry_ts=t.ts, exit_ts=t.exit_ts, entry=t.entry, stop=t.stop)
            for inst, part in trades.items() for t in part]
    rows.sort(key=lambda r: r.ts)
    half = len(rows) // 2
    parts = [("全部", summary(rows)), ("前半段", summary(rows[:half])), ("后半段", summary(rows[half:]))]
    parts += [(name, summary([r for r in rows if r.strategy == name])) for name in sorted({r.strategy for r in rows})]
    parts += [(label, summary([r for r in rows if r.side == side])) for side, label in (("long", "做多"), ("short", "做空"))]
    return save_report(f"fast_{style}", f"快判信号 · {STYLES[style]['label']}", rows, parts, meta, root)


async def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="快判回测")
    ap.add_argument("instruments", nargs="+")
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--style", default="scalp", choices=list(STYLES))
    ap.add_argument("--stance", default="active", choices=list(STANCES))
    ap.add_argument("--cost", type=float, default=0.11, help="一笔单进出的总成本 %%（两次吃单手续费 + 点差）")
    ap.add_argument("--min-strength", type=int, default=FastParams().min_strength)
    ap.add_argument("--end", default=None, help="回测截止日期 YYYY-MM-DD（UTC）。固定它，同一条命令每次跑出来的结果才一样")
    ap.add_argument("--offline", action="store_true", help="只用 data/candles 里已有的历史 K 线，不联网")
    ap.add_argument("--save", action="store_true", help="把结果存到 data/backtest/fast_<风格>.json：页面的回测面板读它，回测扣费后亏的信号类型程序不再照着下单")
    args = ap.parse_args(argv)
    done: dict[str, list[Trade]] = {}
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    settings = Settings()
    end_ms = int(datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) if args.end else None
    async with httpx.AsyncClient(proxy=settings.proxy_url or None, timeout=30) as client:
        for inst in args.instruments:
            m1 = await history.load(inst.upper(), "1m", args.days + WARMUP / 1440, client=client, now_ms=end_ms, refresh=not args.offline)
            h1 = await history.load(inst.upper(), "1H", args.days + 320 / 24, client=client, now_ms=end_ms, refresh=not args.offline)
            if len(m1) <= WARMUP + 100:
                print(f"\n== {inst.upper()}：历史 K 线不够（只有 {len(m1)} 根），跳过 ==")
                continue
            trades = simulate(m1, h1, style=args.style, min_rr=STANCES[args.stance]["min_rr"], cost_pct=args.cost, p=FastParams(min_strength=args.min_strength))
            holes = history.gaps(m1, "1m")
            print(f"\n== {inst.upper()}：{(m1[-1].ts - m1[WARMUP].ts) / 86_400_000:.1f} 天，成本按 {args.cost:g}% 算{f'，1 分钟 K 线缺 {holes} 根' if holes else ''} ==")
            print(report(trades))
            done[inst.upper()] = trades
    if args.save and done:
        path = save(args.style, done, {"instruments": list(done), "days": args.days, "end": args.end, "style": args.style, "stance": args.stance, "cost_pct": args.cost,
                                       "fills": "下一根 1 分钟开盘按现价进；同一根里止损优先；成本按固定百分比扣"})
        print(f"\n已存到 {path}")


if __name__ == "__main__":
    asyncio.run(main())
