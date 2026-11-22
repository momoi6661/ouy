"""趋势突破跟随的回测：一条和候选位完全不同的思路，拿来当对照。

规则（全部只用已收盘的 K 线）：收盘创 N 根新高、且收盘在长均线上方 → 下一根开盘做多（做空相反）；
初始止损 = 入场价 ∓ stop_atr × ATR；之后止损只朝有利方向收：最高价 − trail_atr × 当前 ATR（吊灯止损）；不设固定目标、不移保本。
同一根 K 线里碰到止损就按止损出（开盘已经跳过止损按开盘价）。成本 = 进出各一次吃单 + 点差 + 滑点；做多另扣资金费率。

用法：python -m ouy.backtest_trend BTC-USDT-SWAP ETH-USDT-SWAP SOL-USDT-SWAP --bar 4H --days 1100 --end 2026-11-21
加 --grid 把周围的参数也跑一遍：只有一组参数赚、挪一点就不赚的，不算数。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import httpx

from . import history
from .backtest_plans import Result, line, save_report, summary
from .config import Settings
from .indicators import BAR_MINUTES, Candle, atr, ema

NAME = "趋势突破跟随"


@dataclass(frozen=True)
class TrendRules:
    lookback: int = 20  # 收盘越过前面这么多根的最高 / 最低才算突破
    trend_ema: int = 200  # 只做收盘在这条均线同侧的方向；0 = 不过滤
    stop_atr: float = 2.0
    trail_atr: float = 3.0
    cost: float = 0.14  # 一进一出占价格的百分比：吃单 0.05×2 + 点差 0.01 + 滑点
    funding_8h: float = 0.01  # 做多每 8 小时付的资金费率 %（长期平均量级）


def trend_trades(candles: list[Candle], bar: str, rules: TrendRules = TrendRules(), inst: str = "") -> list[Result]:
    n, minutes = len(candles), BAR_MINUTES[bar]
    closes = [c.close for c in candles]
    atrs = atr(candles)
    base = ema(closes, rules.trend_ema) if rules.trend_ema else None
    warm = max(rules.lookback, rules.trend_ema, 15) + 10
    out: list[Result] = []
    i = warm
    while i < n - 1:
        a, c = atrs[i], candles[i]
        window = candles[i - rules.lookback:i]  # 不含当前这一根
        up = c.close > max(x.high for x in window) and (base is None or c.close > base[i])
        down = c.close < min(x.low for x in window) and (base is None or c.close < base[i])
        if not a or not (up or down):
            i += 1
            continue
        sign = 1 if up else -1
        entry = candles[i + 1].open
        risk = rules.stop_atr * a
        stop, best, j, exit_px, how = entry - sign * risk, entry, i + 1, None, "数据结束"
        while j < n:
            bar_ = candles[j]
            if (bar_.low <= stop) if sign > 0 else (bar_.high >= stop):
                exit_px = min(stop, bar_.open) if sign > 0 else max(stop, bar_.open)
                how = "止损" if (exit_px - entry) * sign <= 0 else "跟踪止损"
                break
            best = max(best, bar_.high) if sign > 0 else min(best, bar_.low)
            if atrs[j]:
                trail = best - sign * rules.trail_atr * atrs[j]
                if (trail - stop) * sign > 0:
                    stop = trail
            j += 1
        if exit_px is None:
            j, exit_px = n - 1, candles[-1].close
        held = max(j - i - 1, 0) + 1
        gross = (exit_px - entry) * sign / entry * 100
        pct = gross - rules.cost - (rules.funding_8h * held * minutes / 480 if sign > 0 else 0.0)
        risk_pct = risk / entry * 100
        peak = (best - entry) * sign / risk
        out.append(Result(ts=c.ts, strategy=NAME, align="顺大方向", side="long" if sign > 0 else "short", how="现价", r_net=pct / risk_pct, pct_net=pct, out=how,
                          mfe_r=peak, inst=inst, entry_ts=candles[i + 1].ts, exit_ts=candles[j].ts, entry=entry, stop=entry - sign * risk, exit_px=exit_px))
        i = max(j, i + 1)
    return out


def groups(rows: list[Result], split: float = 0.6) -> list[tuple[str, dict]]:
    """前 60% 的时间当样本内、后 40% 当样本外：两段都站得住才算数。"""
    out = [("全部", summary(rows))]
    if rows:
        stamps = sorted(r.ts for r in rows)
        cut = stamps[0] + (stamps[-1] - stamps[0]) * split
        out.append(("  样本内（前 60%）", summary([r for r in rows if r.ts < cut])))
        out.append(("  样本外（后 40%）", summary([r for r in rows if r.ts >= cut])))
    out += [(f"  {name.split('-')[0]}", summary([r for r in rows if r.inst == name])) for name in sorted({r.inst for r in rows if r.inst})]
    out += [(f"  {label}", summary([r for r in rows if r.side == side])) for side, label in (("long", "做多"), ("short", "做空"))]
    return out


def report(rows: list[Result], split: float = 0.6) -> str:
    return "\n".join(line(label, s) for label, s in groups(rows, split))


GRID = {
    "默认": {},
    "突破 10 根": {"lookback": 10}, "突破 40 根": {"lookback": 40}, "突破 55 根": {"lookback": 55},
    "不看均线": {"trend_ema": 0}, "均线 100": {"trend_ema": 100},
    "止损 3 ATR": {"stop_atr": 3.0}, "跟踪 2 ATR": {"trail_atr": 2.0}, "跟踪 4 ATR": {"trail_atr": 4.0},
    "成本翻倍": {"cost": 0.28},
}


async def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="趋势突破跟随回测")
    ap.add_argument("instruments", nargs="+")
    ap.add_argument("--bar", default="4H", choices=["15m", "1H", "4H", "1D"])
    ap.add_argument("--days", type=float, default=1100)
    ap.add_argument("--end", default=None, help="回测截止日期 YYYY-MM-DD（UTC）")
    ap.add_argument("--offline", action="store_true", help="只用 data/candles 里已有的历史 K 线，不联网")
    ap.add_argument("--save", action="store_true", help="把结果存到 data/backtest/trend_<周期>.json，页面的回测面板读它")
    ap.add_argument("--grid", action="store_true", help="把周围的参数也跑一遍，看结果是不是只靠某一组参数")
    for f in ("lookback", "trend_ema"):
        ap.add_argument(f"--{f.replace('_', '-')}", type=int, default=getattr(TrendRules(), f))
    for f in ("stop_atr", "trail_atr", "cost"):
        ap.add_argument(f"--{f.replace('_', '-')}", type=float, default=getattr(TrendRules(), f))
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    rules = TrendRules(lookback=args.lookback, trend_ema=args.trend_ema, stop_atr=args.stop_atr, trail_atr=args.trail_atr, cost=args.cost)
    end_ms = int(datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) if args.end else None
    data: dict[str, list[Candle]] = {}
    async with httpx.AsyncClient(proxy=Settings().proxy_url or None, timeout=30) as client:
        for inst in args.instruments:
            data[inst.upper()] = await history.load(inst.upper(), args.bar, args.days, client=client, now_ms=end_ms, refresh=not args.offline)
    for inst, rows_ in data.items():
        if rows_:
            holes = history.gaps(rows_, args.bar)
            print(f"{inst} {args.bar}：{datetime.fromtimestamp(rows_[0].ts / 1000, timezone.utc):%Y-%m-%d} ~ {datetime.fromtimestamp(rows_[-1].ts / 1000, timezone.utc):%Y-%m-%d}，{len(rows_)} 根{f'，缺 {holes} 根' if holes else ''}")
    run = lambda r: [t for inst, rows_ in data.items() for t in trend_trades(rows_, args.bar, r, inst)]  # noqa: E731
    print(f"\n== {NAME}：突破 {rules.lookback} 根、均线 {rules.trend_ema}、止损 {rules.stop_atr:g} ATR、跟踪 {rules.trail_atr:g} ATR、成本 {rules.cost:g}% ==")
    rows = run(rules)
    print(report(rows))
    if args.save:
        meta = {"kind": "trend", "instruments": list(data), "days": args.days, "end": args.end, "bar": args.bar, "fills": "下一根开盘进，止损优先",
                "rules": f"突破 {rules.lookback} 根、均线 {rules.trend_ema}、止损 {rules.stop_atr:g} ATR、跟踪 {rules.trail_atr:g} ATR、成本 {rules.cost:g}%"}
        print(f"已存到 {save_report(f'trend_{args.bar}', f'{NAME} · {args.bar}', rows, groups(rows), meta)}")
    if args.grid:
        print("\n== 换参数（每行只改一项）==")
        for name, change in GRID.items():
            print(line(name, summary(run(replace(rules, **change)))))


if __name__ == "__main__":
    asyncio.run(main())
