"""回测的成交假设要保守，结论要带把握：这两样错了，后面所有「这条打法赚不赚」的判断都是错的。"""
from __future__ import annotations

from ouy.backtest_plans import Result, Rules, Setup, execute, summary, verdict
from ouy.backtest_trend import TrendRules, report, trend_trades
from ouy.indicators import Candle, mean_ci

M5 = 300_000


def bars(rows: list[tuple[float, float, float, float]], start: int = 0) -> dict[str, list[Candle]]:
    return {"5m": [Candle(start + i * M5, o, h, low, c, 1.0, True) for i, (o, h, low, c) in enumerate(rows)]}


def long_setup(how: str = "pullback") -> dict[int, list[Setup]]:
    return {0: [Setup(0, "趋势回踩", "顺大方向", "long", how, 100.0, 99.0, 102.0, 103.0, 60, "trend", 1.0)]}


def run(rows, how="pullback", **rules) -> list[Result]:
    return execute(long_setup(how), bars(rows), style="intraday", rules=Rules(**rules))


def test_limit_order_fills_only_when_price_trades_through_it():
    touch = [(100.5, 100.6, 100.0, 100.4), (100.4, 102.5, 100.3, 102.4)]  # 只碰到 100，没有穿过
    assert run(touch) == []  # 保守口径：排在队尾的挂单碰一下成交不了，这一段行情没赶上
    assert len(run(touch, through=0)) == 1  # 理想口径：碰到就算成交（旧回测的算法）
    through = [(100.5, 100.6, 99.9, 100.4), (100.4, 102.5, 100.3, 102.4), (102.4, 103.5, 102.3, 103.4)]
    done = run(through)
    assert len(done) == 1 and done[0].how == "限价" and done[0].out == "两个目标都到" and done[0].entry == 100.0


def test_fill_bar_high_does_not_count_as_target_because_it_may_precede_the_fill():
    rows = [(101.5, 102.5, 99.9, 100.2), (100.2, 100.3, 98.5, 98.6)]  # 先冲到 102.5，再回落成交，下一根打止损
    done = run(rows)
    assert len(done) == 1 and done[0].out == "止损" and done[0].r_net < -1.0  # 成交前的高点不算摸到目标；亏 1R 再加成本和滑点


def test_stop_wins_inside_one_bar_and_gaps_exit_at_the_open():
    both = [(100.5, 100.6, 99.9, 100.4), (100.4, 102.5, 98.9, 101.0)]  # 同一根里目标和止损都碰到
    assert run(both)[0].out == "止损"
    gap = [(100.5, 100.6, 99.9, 100.4), (97.0, 97.5, 96.5, 97.2)]  # 开盘直接跳到止损下面
    lost = run(gap, slip=0)[0]
    assert lost.exit_px == 97.0 and lost.r_net < -3.0  # 按开盘价出，不是按止损价


def test_market_entry_pays_slippage_and_open_trade_is_marked_not_closed():
    rows = [(100.0, 100.4, 99.8, 100.2), (100.2, 100.6, 100.0, 100.5)]
    plain, slipped = run(rows, how="now", slip=0)[0], run(rows, how="now", slip=0.1)[0]
    assert plain.entry == 100.0 and abs(slipped.entry - 100.1) < 1e-9 and slipped.r_net < plain.r_net
    assert plain.out == "数据结束" and plain.how == "现价"  # 数据走完还没出场：单独标出来，不混进正常出场


def test_verdict_needs_enough_trades_and_an_interval_clear_of_zero():
    def rows(values: list[float]) -> list[Result]:
        return [Result(i, "x", "顺大方向", "long", "限价", v, v, "止损", exit_ts=i) for i, v in enumerate(values)]
    lo, hi = mean_ci([1.0, -1.0] * 30)
    assert lo < 0 < hi and mean_ci([1.0]) != mean_ci([1.0])  # 一笔算不出区间（nan）
    assert verdict(summary(rows([2.0] * 10))) == "样本太少"  # 10 笔全赚也不下结论
    assert verdict(summary(rows([1.0, -1.0] * 30))) == "分不出赚亏"
    assert verdict(summary(rows([1.0, 1.0, -1.0] * 20))) == "扣费后赚" and verdict(summary(rows([-1.0, -1.0, 0.5] * 20))) == "扣费后亏"
    s = summary(rows([1.0, -1.0, -1.0, 2.0]))
    assert s["max_dd_r"] == 2.0 and s["pf"] == 1.5 and mean_ci([1.0, -1.0] * 30) == (lo, hi)  # 固定随机种子：每次跑结果一样


def trend_bars(n: int = 320) -> list[Candle]:
    """先横 260 根，再一路涨 40 根，最后急跌。"""
    out, px = [], 100.0
    for i in range(n):
        step = 0.0 if i < 260 else 1.0 if i < 300 else -3.0
        o, px = px, px + step + (0.2 if i % 2 else -0.2)
        out.append(Candle(i * 14_400_000, o, max(o, px) + 0.3, min(o, px) - 0.3, px, 1.0, True))
    return out


def test_trend_breakout_enters_on_next_open_and_exits_on_the_trailing_stop():
    candles = trend_bars()
    trades = trend_trades(candles, "4H", TrendRules(), "BTC-USDT-SWAP")
    first = trades[0]
    signal = next(i for i, c in enumerate(candles) if c.ts == first.ts)
    assert first.side == "long" and first.entry == candles[signal + 1].open and first.entry_ts == candles[signal + 1].ts  # 信号那根收盘后才进
    assert candles[signal].close > max(c.high for c in candles[signal - 20:signal])  # 只和之前的 K 线比，不含自己
    assert first.out == "跟踪止损" and first.r_net > 3 and first.exit_px < max(c.high for c in candles)  # 赚的是中间一段，不是最高点
    cheap, dear = trend_trades(candles, "4H", TrendRules(cost=0.0, funding_8h=0.0))[0], trend_trades(candles, "4H", TrendRules(cost=1.0))[0]
    assert cheap.pct_net - dear.pct_net > 1.0  # 成本和资金费率都从结果里扣
    assert trend_trades(candles[:signal + 1], "4H") == []  # 信号出在最后一根：没有下一根可以进，不算
    assert "样本外" in report(trades)


def test_saved_report_feeds_the_page_and_the_candidate_text(tmp_path):
    from ouy.backtest_plans import evidence, groups, save_report, saved_reports

    rows = [Result(i * M5, "趋势回踩", "顺大方向", "long", "限价", v, v, "止损", inst="BTC-USDT-SWAP", exit_ts=i * M5 + M5) for i, v in enumerate([-1.0, -1.0, 0.5] * 20)]
    path = save_report("plans_intraday", "算法候选位 · 日内", rows, groups(rows), {"days": 190}, root=tmp_path)
    body = saved_reports(tmp_path)[0]
    assert path.name == "plans_intraday.json" and body["groups"][0]["label"] == "全部" and body["groups"][0]["verdict"] == "扣费后亏"
    assert body["equity"][-1][1] == -30.0 and len(body["trades"]) == 60 and body["meta"]["days"] == 190
    assert evidence("intraday", tmp_path)["趋势回踩"] == "60 笔平均 -0.50R，扣费后亏" and evidence("swing", tmp_path) == {}


async def test_backtest_and_cache_endpoints_serve_saved_numbers(rt, tmp_path, monkeypatch):
    """页面读的两个接口：存盘的回测报告、最近若干轮复盘的真实缓存读数。"""
    import httpx

    from ouy import backtest_plans as bp
    from ouy.app import create_app

    monkeypatch.setattr(bp, "REPORT_DIR", tmp_path)
    rows = [bp.Result(ts=i * 60_000, strategy="趋势回踩", align="顺大方向", side="long", how="限价", r_net=-0.3, pct_net=-0.1, out="止损", inst="BTC-USDT-SWAP", exit_ts=i * 60_000 + 1) for i in range(3)]
    bp.save_report("plans_intraday", "算法候选位 · 日内短线", rows, bp.groups(rows), {"kind": "plans", "days": 1})
    rt.store.set("usage:demo", [{"ts": 1, "inst_id": "BTC-USDT-SWAP", "input_tokens": 30_000, "cached_tokens": 12_000, "first_cached_tokens": 12_000, "model_requests": 1, "unmeasured_requests": 0}])
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            reports = (await client.get("/api/backtest")).json()
            cache = (await client.get("/api/stats/cache")).json()
    assert [r["name"] for r in reports] == ["plans_intraday"] and reports[0]["groups"][0]["n"] == 3 and len(reports[0]["equity"]) == 3
    assert cache["hit_pct"] == 40.0 and cache["first_hit_pct"] == 40.0 and cache["instruments"]["BTC-USDT-SWAP"]["reviews"] == 1


def test_candidate_rows_give_the_page_levels_and_cost_in_r():
    from ouy.agent.plan import PlanCheck, candidate_rows
    from ouy.models import TradePlan

    plan = TradePlan(direction="long", entry_low=100, entry_high=100, stop_loss=99.5, take_profits=[101, 102], leverage=3, invalidation="跌破 99.5")
    row = candidate_rows([{"strategy": "趋势回踩", "basis": "回踩 20 均线", "align": "顺大方向", "ok": True, "why_not": "", "plan": plan,
                           "check": PlanCheck(how="pullback", ref=100, stop_pct=0.5, tp_pct=1.0, cost_pct=0.1, net_rr=1.5, limit=True)}], "intraday")[0]
    assert (row["entry"], row["stop"], row["targets"], row["side"], row["cost_r"]) == (100, 99.5, [101, 102], "long", 0.2)
