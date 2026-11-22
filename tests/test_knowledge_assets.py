from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

import pytest

from ouy.agent.watcher import kb_queries
from ouy.indicators import Candle, value_area
from ouy.knowledge import KnowledgeBase
from ouy.market import funding_interval_hours, us_stock_session
from ouy.models import AgentConfig

from . import fake_okx
from .test_agent_and_api import BTC, make_rt, scripted_model

STOCK = "SNDK-USDT-SWAP"


def ts(*args: int) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def test_us_stock_session_follows_eastern_time_and_dst():
    assert us_stock_session(ts(2026, 7, 15, 14, 0))[0] == "regular"  # 夏令时：美东 10:00
    assert us_stock_session(ts(2026, 7, 15, 13, 0))[0] == "pre"  # 美东 09:00
    assert us_stock_session(ts(2026, 12, 15, 14, 0))[0] == "pre"  # 冬令时：同一个 UTC 时间是美东 09:00
    assert us_stock_session(ts(2026, 12, 15, 15, 0))[0] == "regular"
    assert us_stock_session(ts(2026, 12, 15, 22, 0))[0] == "post"  # 美东 17:00
    assert us_stock_session(ts(2026, 12, 16, 3, 0))[0] == "overnight"  # 美东 22:00
    assert us_stock_session(ts(2026, 12, 19, 15, 0))[0] == "closed"  # 周六
    assert us_stock_session(ts(2026, 12, 21, 0, 30))[0] == "closed"  # 周日美东 19:30，夜盘还没开
    assert us_stock_session(ts(2026, 12, 21, 1, 30))[0] == "overnight"  # 周日美东 20:30


def test_funding_rate_is_normalised_to_eight_hours():
    row = {"fundingTime": "1795190400000", "nextFundingTime": "1795219200000"}
    assert funding_interval_hours(row) == 8
    assert funding_interval_hours({"fundingTime": "1795190400000", "prevFundingTime": "1795186800000"}) == 1
    assert funding_interval_hours({"fundingTime": "1795190400000"}) == 0  # 算不出来就不折算，不编


def test_value_area_finds_where_most_volume_traded():
    day = 86_400_000
    t0 = 1_700_006_400_000 - 1_700_006_400_000 % day
    bars = []
    for i in range(-200, 40):  # 15 分钟 K 线：前两天 + 今天 10 小时；100~101 之间成交最密
        px = 100.5 + 3 * math.sin(i / 9)
        bars.append(Candle(t0 + i * 900_000, px, px + 0.3, px - 0.3, px, 500 if 100 <= px <= 101 else 100))
    area = value_area({"15m": bars}, 104.9)
    assert area and 100 <= area["poc"] <= 101 and area["val"] < area["poc"] < area["vah"]
    assert area["vah"] - area["val"] < area["day_high"] - area["day_low"]
    assert "上方" in area["now"] and "站得住" in area["read"]
    assert value_area({"5m": bars[:50]}) is None  # 盖不住完整的一天就不给


def test_kb_queries_follow_what_the_program_computed():
    snap = {"last": 100, "timeframes": {"4H": {"structure": "range"}, "1H": {"adx14": 15, "atr14": 1}},
            "windows": {"1H": {"now": "假跌破：下探到 98 跌破上一段低点 99，但收盘回到里面"}},
            "derivatives": {"funding_rate_pct": 0.01, "funding_rate_8h_pct": 0.08}, "asset_class": "stock"}
    qs = " | ".join(kb_queries(snap, None, "定时复盘", {"shift": {"side": "long", "bars_ago": 3}}))
    assert "价值区间" in qs and "变盘后回踩" in qs and "海龟汤" in qs and "股票永续" in qs
    assert "资金费率" in qs  # 本期只有 0.01%，但一小时结算一次，折成 8 小时是 0.08%


def test_new_knowledge_is_indexed_and_strategies_can_be_opened(settings):
    kb = KnowledgeBase(settings)
    assert "判断的底子" in kb.core_text() and "证据的分量" in kb.core_text()
    names = [r["name"] for r in kb.catalog()]
    for name in ("二次入场", "价值区间", "开盘区间突破"):
        assert any(n.startswith(name) for n in names)
        assert "出处与可信度" in kb.read_strategy(name)
    assert "1N" in kb.read_strategy("海龟") and "value_area" in kb.catalog_text()

    async def top(q: str) -> str:
        return " ".join(c.source for c, _ in await kb.search(q, 4))

    loop = asyncio.new_event_loop()
    try:
        assert "stock_perps" in loop.run_until_complete(top("股票永续 美股时段 休市 跳空 财报 不跟 BTC"))
        assert "risk_math" in loop.run_until_complete(top("连续亏损 胜率 期望值 凯利"))
        assert "value_area_auction" in loop.run_until_complete(top("价值区间 POC VAH VAL 回到区间内"))
        assert "second_entry_brooks" in loop.run_until_complete(top("趋势里的回调 二次入场 两段式回调"))
        assert "evidence" in loop.run_until_complete(top("哪些方法有研究证据 可信度"))
    finally:
        loop.close()


async def test_stock_perp_review_is_not_benchmarked_against_btc(settings, fake, monkeypatch):
    monkeypatch.setitem(fake_okx.SPECS, STOCK, {"ctVal": "1", "lotSz": "0.001", "minSz": "0.001", "tickSz": "0.01", "lever": "75", "instCategory": "3"})
    fake.prices[STOCK] = 50.0
    prompts: list[str] = []
    rt = make_rt(settings, fake, scripted_model(prompts))
    rt.save_config(AgentConfig(mode="advise", instruments=[STOCK, BTC], fast_entry="off"))
    await rt.watcher.review(STOCK, "测试")
    prompt = prompts[0]
    assert "【这是股票永续，不是加密货币】" in prompt and "美东" in prompt
    assert "【BTC 大盘】" not in prompt and '"asset_class"' not in prompt
    assert rt.watcher._asset[STOCK] == "stock"


def test_day_profile_records_how_price_moved_since_the_open():
    from ouy.indicators import day_profile
    from ouy.market import session_anchor

    t0 = 1_700_006_400_000 - 1_700_006_400_000 % 86_400_000  # UTC 0 点

    def bars(path: list[float]) -> list[Candle]:
        out, prev = [Candle(t0 - 900_000, 99.5, 100.2, 99.4, 100, 1)], 100.0
        for i, px in enumerate(path):
            out.append(Candle(t0 + i * 900_000, prev, max(prev, px) + 0.05, min(prev, px) - 0.05, px, 1))
            prev = px
        return out

    up = day_profile({"15m": bars([100.5, 101, 101.5, 102, 102.6, 103, 103.4, 104])}, 104, t0, "今天")
    assert up["kind"] == "单边上行" and up["open"] == 100 and up["change_pct"] == 4.0
    assert up["opening_range"]["dir"] == "收涨" and "上方" in up["opening_range"]["now"] and "今天开 100" in up["text"]

    fade = day_profile({"15m": bars([101, 102, 103, 102, 101, 100, 99.4, 99])}, 99, t0, "今天")
    assert fade["kind"] == "冲高回落" and fade["path"].startswith("先冲高到") and fade["change_pct"] == -1.0

    rebound = day_profile({"15m": bars([99, 98, 97.5, 98.5, 99.5, 100.4, 100.8])}, 100.8, t0, "今天")
    assert rebound["kind"] == "探底回升" and rebound["path"].startswith("先下探到")

    assert "opening_range" not in day_profile({"15m": bars([100.2, 100.1])}, 100.1, t0, "今天")  # 第一个小时还没走完
    pre = [Candle(t0 - i * 900_000, 98, 102, 97, 101, 1) for i in (4, 3, 2, 1)]
    post = bars([100.5, 101])[1:]
    full = day_profile({"15m": pre + post}, 101, t0, "今天")
    assert full["pre_open"]["close"] == 101 and full["pre_open"]["gap_pct"] < 0
    assert "pre_open" not in day_profile({"15m": pre[1:] + post}, 101, t0, "今天")
    assert day_profile({"15m": pre + post[1:]}, 101, t0, "今天") is None  # 缺锚点，不用下一根冒充开盘
    assert day_profile({"15m": bars([101])[1:]}, 101, t0 - 86_400_000, "今天") is None  # K 线盖不到开盘那一刻

    assert session_anchor("crypto", ts(2026, 11, 20, 15, 38)) == (int(ts(2026, 11, 20)) * 1000, "今天（北京时间 8 点起）")
    # 股票永续：周五美东 10:38（冬令时）→ 当天 09:30 开盘 = UTC 14:30；周日 → 仍然是周五那次开盘
    assert session_anchor("stock", ts(2026, 11, 20, 15, 38))[0] == int(ts(2026, 11, 20, 14, 30)) * 1000
    assert session_anchor("stock", ts(2026, 11, 22, 12, 0))[0] == int(ts(2026, 11, 20, 14, 30)) * 1000
    assert session_anchor("stock", ts(2026, 7, 15, 13, 0))[0] == int(ts(2026, 7, 14, 13, 30)) * 1000  # 夏令时盘前：算上一个交易日的开盘


async def test_scoreboard_credits_closed_trades_to_the_strategy_that_opened_them(rt, fake):
    """按打法记战绩：进场时记下用的哪条打法，平仓后已实现盈亏记到它头上；没标记的单单独一栏，开始记之前的历史不算。"""
    from ouy.models import TradeAction

    scores = rt.watcher.scores
    assert scores.brief("demo") == "" and await scores.settle("demo", rt.trader, [BTC]) == []
    opened = await rt.trader.execute(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_400, take_profit=61_200, leverage=3, risk_pct=0.5, reason="测试"), "demo", source="agent")
    assert opened.status == "executed"
    scores.tag("demo", BTC, "long", "前高前低法", "计划现价进场")
    tags = rt.store.get("score_tags:demo")
    tags[-1]["ts"] -= 120_000  # 假交易所把开仓时间记成平仓前一分钟：把标签的时间往前挪，让「先下单、后开仓」对得上
    rt.store.set("score_tags:demo", tags)
    fake.set_price(BTC, 61_300)  # 止盈触发，这一笔赚
    done = await scores.settle("demo", rt.trader, [BTC])
    assert [d["strategy"] for d in done] == ["前高前低法"] and done[0]["pnl"] > 0
    assert await scores.settle("demo", rt.trader, [BTC]) == []  # 同一笔不重复记

    await rt.trader.execute(TradeAction(kind="open", inst_id=BTC, side="short", stop_loss=61_900, take_profit=60_000, leverage=3, risk_pct=0.5, reason="手动"), "demo", source="agent")
    fake.set_price(BTC, 62_000)  # 没打标签的一笔，止损
    await scores.settle("demo", rt.trader, [BTC])
    stats = scores.stats("demo")
    assert stats["前高前低法"]["n"] == 1 and stats["前高前低法"]["wins"] == 1
    assert stats["未标记（手动或 Worker 直接下单）"]["pnl"] < 0
    assert "样本不到 20 笔" in scores.of("demo", "前高前低法（上一段的最高点和最低点）") and "前高前低法" in scores.brief("demo")

    # 按时间对：限价单在有效期内成交算它的；过了有效期才开的仓、反方向的仓不算；后打的标签优先
    from types import SimpleNamespace as NS
    tags = [{"ts": 1_000_000, "inst_id": BTC, "side": "long", "strategy": "趋势回踩", "via": "计划挂限价单", "ttl": 600_000, "opened_ts": None},
            {"ts": 1_300_000, "inst_id": BTC, "side": "long", "strategy": "变盘后回踩", "via": "计划挂限价单", "ttl": 600_000, "opened_ts": None}]
    match = scores._match
    assert match(tags, BTC, NS(side="long", opened_ts=1_200_000))["strategy"] == "趋势回踩"
    assert match(tags, BTC, NS(side="long", opened_ts=1_400_000))["strategy"] == "变盘后回踩"
    assert match(tags, BTC, NS(side="long", opened_ts=2_000_000)) is None and match(tags, BTC, NS(side="short", opened_ts=1_200_000)) is None


def test_prune_drops_old_run_records_but_keeps_what_matters(rt):
    """清理记录：过了保留期的活动流、日志、已结束的任务删掉；进行中的任务、新的记录、逐笔复盘得到的经验留着。"""
    s = rt.store
    old = s.add_activity("demo", BTC, "worker", "wake", "旧的")
    s._exec("INSERT INTO journal (ts, env, inst_id, kind, title) VALUES (1, 'demo', '', 'cycle', '旧复盘'), (1, 'demo', '', 'review', '旧经验')")
    s._exec("INSERT INTO work_items (ts, updated_ts, env, source, goal, status) VALUES (1, 1, 'demo', 't', '做完的', 'done'), (1, 1, 'demo', 't', '还在跑的', 'running')")
    s._exec("UPDATE activity SET ts=1 WHERE id=?", (old["id"],))
    s.add_activity("demo", BTC, "worker", "wake", "新的")
    gone = s.prune(7)
    assert gone["活动流"] == 1 and gone["日志"] == 1 and gone["任务"] == 1
    assert [r["title"] for r in s._rows("SELECT title FROM journal")] == ["旧经验"]
    assert [r["goal"] for r in s._rows("SELECT goal FROM work_items")] == ["还在跑的"]
    assert s.log_counts()["活动流"] == 1 and s.prune(0)["活动流"] == 1
