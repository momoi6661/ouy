"""Check the SDK wire format without connecting to a model service or exchange."""
from __future__ import annotations

import json
from copy import deepcopy

import httpx2
import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolReturnPart
from pydantic_ai.usage import RequestUsage

from ouy.agent import core
from ouy.agent.watcher import _cache_usage, _effort, _worker_cache_key

from .test_agent_and_api import BTC


@pytest.mark.parametrize("mode", ["responses", "chat"])
async def test_two_reviews_share_prefix_but_send_new_market_data(mode, rt, settings, monkeypatch):
    bodies = []
    args = json.dumps({"bias": "neutral", "confidence": 0.1, "summary": "no plan", "no_plan_reason": "limit"})

    def endpoint(request):
        body = json.loads(request.content)
        bodies.append(body)
        tools = [t.get("function", t) for t in body["tools"]]
        name = next(t["name"] for t in tools if "no_plan_reason" in t["parameters"].get("properties", {}))
        cached = 1024 if len(bodies) > 1 else 0
        if mode == "responses":
            response = {"id": "resp_mock", "object": "response", "created_at": 1, "status": "completed", "model": body["model"],
                        "output": [{"type": "function_call", "id": "fc_mock", "call_id": "call_mock", "name": name, "arguments": args}],
                        "usage": {"input_tokens": 2500, "input_tokens_details": {"cached_tokens": cached},
                                  "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 2550}}
        else:
            response = {"id": "chat_mock", "object": "chat.completion", "created": 1, "model": body["model"],
                        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                                     "tool_calls": [{"id": "call_mock", "type": "function", "function": {"name": name, "arguments": args}}]}}],
                        "usage": {"prompt_tokens": 2500, "prompt_tokens_details": {"cached_tokens": cached}, "completion_tokens": 50, "total_tokens": 2550}}
        return httpx2.Response(200, json=response)

    cfg = rt.config()
    account = await rt.trader.account("demo")
    deps = core.Deps(rt.trader, rt.market, rt.gateway, cfg, rt.kb)
    history = ["closed-hour", 90, 110, 88, 100, 1]
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(endpoint)) as client:
        monkeypatch.setattr(core, "_http_client", lambda _: client)
        settings = settings.model_copy(update={"ai_api_mode": mode, "ai_base_url": "https://model.invalid/v1", "ai_model": "gpt-4.1"})
        agent = core.build_trader_agent(core.build_model(settings), rt.kb)
        for i, px in enumerate((101, 102)):
            snap = {"last": px, "real_market": {"marker": "LIVE_ONLY"},
                    "timeframes": {"1H": {"latest_bar_confirmed": False, "candles": [history, ["hour(未收)", 100, px, 99, px, 1]]}}}
            original = deepcopy(snap)
            prompt = rt.watcher._prompt(cfg, BTC, f"event-{i}", snap, account, None, [])
            assert snap == original and prompt.count("closed-hour") == 1 and prompt.count("hour(未收)") == 1
            assert "LIVE_ONLY" not in prompt
            result = await agent.run(prompt, deps=deps, model_settings=_effort("default", _worker_cache_key("review", BTC)))
            usage = _cache_usage(result.all_messages())
            assert usage["cached_tokens"] == usage["first_cached_tokens"] == 1024 * i
            assert usage["input_tokens"] == usage["first_input_tokens"] == 2500 and usage["model_requests"] == 1
    a, b = bodies
    assert a["prompt_cache_key"] == b["prompt_cache_key"] == f"ouy-{BTC}"
    assert a["tools"] == b["tools"] and a.get("instructions") == b.get("instructions")
    field = "input" if mode == "responses" else "messages"
    assert a[field][:-1] == b[field][:-1]
    texts = [json.dumps(body[field][-1], ensure_ascii=False) for body in bodies]
    prefix = texts[0].split("event-0")[0]
    assert "closed-hour" in prefix and "hour(未收)" not in prefix
    assert prefix == texts[1].split("event-1")[0] and texts[0] != texts[1]


def test_instructions_move_to_the_front_of_input_only_when_safe():
    body = {"instructions": "规" * 300, "input": [{"role": "user", "content": "行情"}], "tools": []}
    assert core.lead_instructions(body)
    assert body["input"][0] == {"role": "developer", "content": "规" * 300} and body["input"][1]["role"] == "user"
    assert body["instructions"] == core.LEAD_NOTE and not core.lead_instructions(body)  # 再过一遍不会重复挪
    for skip in ({"instructions": "规" * 300, "input": "纯文本"}, {"instructions": "规" * 300, "input": [], "previous_response_id": "resp_1"}, {"input": []}):
        before = json.dumps(skip, ensure_ascii=False)
        assert not core.lead_instructions(skip) and json.dumps(skip, ensure_ascii=False) == before


async def test_real_client_sends_rules_before_the_user_message(settings):
    seen = []

    class Sink(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request):
            seen.append(json.loads(request.content))
            return httpx2.Response(200, json={})

    client = core._http_client(settings)
    client._transport.__class__.__bases__[0].handle_async_request, original = (lambda self, request: Sink().handle_async_request(request)), client._transport.__class__.__bases__[0].handle_async_request
    try:
        await client.post("https://model.invalid/v1/responses", json={"instructions": "规" * 300, "input": [{"role": "user", "content": "行情"}]})
        await client.post("https://model.invalid/v1/chat/completions", json={"instructions": "规" * 300, "input": []})
    finally:
        client._transport.__class__.__bases__[0].handle_async_request = original
        await client.aclose()
    assert [i["role"] for i in seen[0]["input"]] == ["developer", "user"] and seen[1]["input"] == []


def _bars(n: int, step_min: int, end_ms: int, live: bool = True):
    from ouy.indicators import Candle

    step = step_min * 60_000
    first = end_ms // step * step - (n - 1) * step
    rows = []
    for i in range(n):
        ts = first + i * step
        v = ts // step % 7  # 同一根 K 线每次取到的值都一样
        rows.append(Candle(ts, 100 + v, 101 + v, 99 + v, 100.5 + v, 10.0 + v % 3, not (live and i == n - 1)))
    return rows


async def test_closed_history_stays_identical_while_new_bars_close(rt):
    """每 5 分钟复盘一次：整块对齐的已收盘 K 线在一整块时间里一字不变，没收盘的那根排在后面的行情快照里。"""
    from ouy.agent.watcher import _split_review_chart
    from ouy.indicators import ROW_BLOCK, STABLE_ROWS, summarize

    cfg, account, start = rt.config(), await rt.trader.account("demo"), 1_795_000_000_000 // 3_000_000 * 3_000_000  # 从 5 分钟图的一个整块开头起
    prompts, tails = [], []
    for k in range(ROW_BLOCK):
        now = start + k * 300_000 + 60_000
        snap = {"last": 100.0, "timeframes": {"5m": summarize(_bars(260, 5, now)), "1H": summarize(_bars(260, 60, now))}}
        history, live = _split_review_chart(snap)
        assert len(history["5m"]) == len(history["1H"]) == STABLE_ROWS
        tails.append(len(live["timeframes"]["5m"]["candles"]))
        prompts.append(rt.watcher._prompt(cfg, BTC, f"event-{k}", snap, account, None, []))
    assert tails == list(range(1, ROW_BLOCK + 1))  # 0~9 根新收的 + 没收盘的那一根：一根不丢，也不重复
    block = lambda p, name: p.split(f"【已收盘数据·{name}")[1].split("\n【")[0].split(core.STABLE_MARK)[0]  # noqa: E731
    heads = {p.split("【已收盘数据·大周期")[0] for p in prompts}
    assert len(heads) == 1 and "【什么时候停】" in next(iter(heads)) and "【叫醒原因】" not in next(iter(heads))  # 不变的说明排在前面，每轮都变的排在后面
    slow = [block(p, "大周期") for p in prompts]
    assert len(set(slow)) <= 2 and '"ema20"' in slow[0] and '"candles"' in slow[0]  # 1 小时的 K 线和指标：这 50 分钟里最多在整点变一次
    mid = {block(p, "中周期") for p in prompts}
    assert len(mid) == 1 and '"candles"' in next(iter(mid)) and '"ema20"' not in next(iter(mid))  # 5 分钟图整块对齐的那 30 根：一整块时间里一字不变
    assert len({block(p, "小周期") for p in prompts}) == ROW_BLOCK  # 5 分钟图的指标每收一根变一次，所以排在最后一段
    assert all('"ema20"' not in p.split("【行情快照】")[1] for p in prompts)  # 挪到前面的指标不在行情快照里再出现一遍
    order = [prompts[0].index(tag) for tag in ("【品种】", "【已收盘数据·大周期", "【已收盘数据·中周期", "【已收盘数据·小周期", "【叫醒原因】", "【行情快照】")]
    assert order == sorted(order)  # 变得越慢的越靠前
    stats = rt.watcher._state(BTC).prompt_stats
    assert 0 < stats["stable_chars"] < stats["prompt_chars"] and stats["stable_chars"] / stats["prompt_chars"] > 0.3
    later = summarize(_bars(260, 5, start + ROW_BLOCK * 300_000 + 60_000))  # 过了一整块：前面那段才往前挪一块
    assert later["candles"][:STABLE_ROWS] != summarize(_bars(260, 5, start + 60_000))["candles"][:STABLE_ROWS]
    assert later["candles"][0] == summarize(_bars(260, 5, start + 60_000))["candles"][ROW_BLOCK]


def test_cache_stats_report_real_hit_rate_per_instrument():
    from ouy.agent.watcher import cache_stats

    row = lambda inst, total, cached, first, n=1: {"ts": 1, "inst_id": inst, "input_tokens": total, "cached_tokens": cached, "first_cached_tokens": first,  # noqa: E731
                                                   "model_requests": n, "unmeasured_requests": 0, "prompt_chars": 40_000, "stable_chars": 20_000}
    stats = cache_stats([row("ETH-USDT-SWAP", 36_000, 11_136, 11_136), row("SOL-USDT-SWAP", 79_000, 54_784, 18_560, 2), row("ETH-USDT-SWAP", 36_000, 25_200, 25_200)])
    assert stats["reviews"] == 3 and stats["hit_pct"] == round((11_136 + 54_784 + 25_200) / 151_000 * 100, 1)
    assert stats["first_hit_pct"] == round((11_136 + 25_200) / 72_000 * 100, 1)  # 旧版多请求记录没有首请求输入量
    assert stats["first_measured_reviews"] == 2
    assert stats["instruments"]["SOL-USDT-SWAP"]["first_hit_pct"] is None and stats["stable_prompt_pct"] == 50.0
    assert cache_stats([])["hit_pct"] is None and cache_stats([])["reviews"] == 0


def test_first_hit_rate_counts_cold_tool_rounds_and_skips_missing_first_usage():
    from ouy.agent.watcher import cache_stats

    tool_result = ModelRequest(parts=[ToolReturnPart("lookup", "result", tool_call_id="call")])
    follow_up = ModelResponse(parts=[], usage=RequestUsage(input_tokens=120, cache_read_tokens=100))
    cold = _cache_usage([ModelResponse(parts=[], usage=RequestUsage(input_tokens=100, cache_write_tokens=80)), tool_result, follow_up])
    missing = _cache_usage([ModelResponse(parts=[], usage=RequestUsage(unmeasured_requests=1)), tool_result, follow_up])
    warm = _cache_usage([ModelResponse(parts=[], usage=RequestUsage(input_tokens=100, cache_read_tokens=70))])
    assert (cold["model_requests"], cold["first_input_tokens"], cold["first_cached_tokens"]) == (2, 100, 0)
    assert missing["first_input_tokens"] is missing["first_cached_tokens"] is None

    stats = cache_stats([{"inst_id": BTC, **usage} for usage in (cold, missing, warm)])
    assert stats["hit_pct"] == 61.4
    assert stats["cache_write_tokens"] == 80
    assert stats["first_hit_pct"] == 35.0
    assert stats["first_measured_reviews"] == 2
    assert stats["unmeasured_requests"] == 1


def test_stable_parts_of_the_prompt_become_leading_developer_messages():
    """服务端只在开头连续的 developer 消息末尾命中缓存：提示词里分隔行前面的每一段拆成一条 developer 消息，每轮都变的留在用户消息里。"""
    mark = core.STABLE_MARK
    body = {"instructions": "规" * 300, "input": [{"role": "user", "content": f"设置\n{mark}\nK线\n{mark}\n报价 1"}], "tools": []}
    assert core.lead_instructions(body)
    assert body["input"] == [{"role": "developer", "content": "规" * 300}, {"role": "developer", "content": "设置"},
                             {"role": "developer", "content": "K线"}, {"role": "user", "content": "报价 1"}]
    assert not core.lead_instructions(body)  # 工具往返时同一条消息再过一遍：不重复拆
    parts = {"instructions": "规" * 300, "input": [{"role": "user", "content": [{"type": "input_text", "text": f"设置\n{mark}\n报价 2"}, {"type": "input_image", "image_url": "x"}]}]}
    assert core.lead_instructions(parts)
    assert parts["input"][1] == {"role": "developer", "content": "设置"}
    assert parts["input"][2] == {"role": "user", "content": [{"type": "input_text", "text": "报价 2"}, {"type": "input_image", "image_url": "x"}]}
    chained = {"instructions": "规" * 300, "input": [{"role": "user", "content": f"设置\n{mark}\n报价"}], "previous_response_id": "resp_1"}
    assert not core.lead_instructions(chained) and mark in chained["input"][0]["content"]  # 接着服务端状态续跑的请求不动


async def test_only_the_changed_block_and_what_follows_it_miss_the_cache(rt):
    """连续两轮复盘：规则、品种设置、整块对齐的 K 线三条 developer 消息一字不差；K 线换了一整块之后，设置那一条照样相同。"""
    from ouy.indicators import ROW_BLOCK, summarize

    cfg, account, start = rt.config(), await rt.trader.account("demo"), 1_795_000_000_000 // 3_000_000 * 3_000_000

    def request(k: int, reason: str) -> list[dict]:
        now = start + k * 300_000 + 60_000
        snap = {"last": 100.0 + k, "timeframes": {"5m": summarize(_bars(260, 5, now)), "1H": summarize(_bars(260, 60, now))}}
        body = {"instructions": "规" * 300, "input": [{"role": "user", "content": rt.watcher._prompt(cfg, BTC, reason, snap, account, None, [])}]}
        assert core.lead_instructions(body)
        return body["input"]

    a, b, later = request(0, "定时复盘"), request(0, "触及观察位"), request(ROW_BLOCK, "定时复盘")
    assert [m["role"] for m in a] == ["developer"] * 6 + ["user"]
    assert a[:-1] == b[:-1] and a[-1] != b[-1]  # 同一根 5 分钟 K 线里的两轮：只有最后那条每轮提示词不一样
    for i, tag in enumerate(("【品种】", "【策略库】", "【已收盘数据·大周期", "【已收盘数据·中周期", "【已收盘数据·小周期", "【叫醒原因】"), start=1):
        assert tag in a[i]["content"]
    assert core.STABLE_MARK not in "".join(m["content"] for m in a)
    closed_one = request(1, "定时复盘")  # 5 分钟图新收了一根：只有小周期那一条和后面的变
    assert closed_one[:5] == a[:5] and closed_one[5] != a[5]
    assert later[:3] == a[:3] and later[4] != a[4]  # 5 分钟图换了一整块：设置那一条照样相同


async def test_live_context_and_knowledge_changes_keep_closed_candle_prefix(rt, monkeypatch):
    from ouy.agent import watcher
    from ouy.indicators import summarize
    from ouy.knowledge import Chunk

    cfg, account = rt.config(), await rt.trader.account("demo")
    now = 1_795_000_000_000
    snap = {"last": 100.0, "asset_class": "stock", "timeframes": {"5m": summarize(_bars(260, 5, now))}}
    st = rt.watcher._state(BTC)

    def request(session: str, summary: str, reference: str) -> list[dict]:
        monkeypatch.setattr(watcher, "us_stock_session", lambda: ("test", session))
        st.adhoc = [{"name": "观察", "keep": True, "running": False, "summary": summary}]
        knowledge = [Chunk("ref", "doc", "资料", "资料", reference)]
        prompt = rt.watcher._prompt(cfg, BTC, "定时复盘", snap, account, None, knowledge)
        body = {"instructions": "规" * 300, "input": [{"role": "user", "content": prompt}]}
        assert core.lead_instructions(body)
        return body["input"]

    a = request("开市", "旧结论", "旧条目")
    b = request("休市", "新结论", "旧条目")
    c = request("休市", "新结论", "新条目")
    assert a[:-1] == b[:-1]
    assert a[-1]["content"] != b[-1]["content"]
    assert "休市" in b[-1]["content"] and "新结论" in b[-1]["content"]
    assert b[:-2] == c[:-2] and "【已收盘数据·小周期" in c[-3]["content"]
    assert "新条目" in c[-2]["content"]


def test_closed_data_split_keeps_every_number_and_only_moves_it():
    """已收盘数据只是换了摆放位置：几段拼回去和原来的行情快照一模一样；随报价变的字段一个都不进前面几段。"""
    from ouy.agent.watcher import CLOSED_BUCKETS, FIXED_AREA_KEYS, LIVE_FRAME_KEYS, LIVE_WINDOW_KEYS, _split_closed
    from ouy.indicators import summarize, value_area, window_levels

    now = 1_795_000_000_000 // 3_000_000 * 3_000_000 + 7 * 300_000 + 60_000
    raw = {"1m": _bars(260, 1, now), "5m": _bars(700, 5, now), "15m": _bars(260, 15, now), "1H": _bars(260, 60, now), "4H": _bars(40, 240, now), "1D": _bars(20, 1440, now)}
    snap = {"inst_id": BTC, "last": 100.3, "orderbook": {"best_bid": 100.2, "best_ask": 100.4}, "timeframes": {bar: summarize(rows) for bar, rows in raw.items()},
            "windows": window_levels(raw), "value_area": value_area(raw, 100.3)}
    assert snap["windows"] and snap["value_area"] and snap["timeframes"]["1D"].get("insufficient")
    before = json.dumps(snap, ensure_ascii=False, sort_keys=True)
    buckets, live = _split_closed(snap)
    assert json.dumps(snap, ensure_ascii=False, sort_keys=True) == before  # 不改传进来的快照：算法候选位、风控用的还是原来那份
    assert len(buckets) == len(CLOSED_BUCKETS) and all(buckets)

    merged = json.loads(json.dumps(live))
    for data in buckets:
        for bar, part in data.get("timeframes", {}).items():
            frame = merged["timeframes"][bar]
            frame.setdefault("_early", []).extend(part.get("candles", []))
            frame.setdefault("_new", []).extend(part.get("new_candles", []))
            frame.update({k: v for k, v in part.items() if k not in ("candles", "new_candles")})
        for name, part in data.get("windows", {}).items():
            merged["windows"].setdefault(name, {}).update(part)
        merged["value_area"].update(data.get("value_area", {}).get("prev_day", {}))
    for frame in merged["timeframes"].values():
        if "_early" in frame:
            frame["candles"] = [*frame.pop("_early"), *frame.pop("_new"), *frame["candles"]]
    assert merged == json.loads(json.dumps(snap))

    for data in buckets:  # 随报价变的字段一个都不在前面几段里
        assert not set(data) - {"timeframes", "windows", "value_area"}
        assert all(not (set(part) & LIVE_FRAME_KEYS) - {"candles"} for part in data.get("timeframes", {}).values())
        assert all("(未收)" not in str(row[0]) for part in data.get("timeframes", {}).values() for row in [*part.get("candles", []), *part.get("new_candles", [])])
        assert set(data.get("value_area", {}).get("prev_day", {})) <= FIXED_AREA_KEYS
    slow, mid, small = buckets
    assert set(slow["timeframes"]) == {"15m", "1H", "4H"} and "ema20" in slow["timeframes"]["1H"] and "ema20" not in slow["timeframes"]["15m"]
    assert "ema20" in mid["timeframes"]["15m"] and set(mid["timeframes"]["5m"]) == {"candles"} and "ema20" in small["timeframes"]["5m"]
    for bar, bucket in (("1H", slow), ("15m", mid), ("5m", small)):  # 新收的几根跟着本周期的指标走，行情快照里只剩没收盘的那根
        assert bucket["timeframes"][bar]["new_candles"] and all("(未收)" in str(row[0]) for row in live["timeframes"][bar]["candles"])
    assert len(live["timeframes"]["1m"]["candles"]) > 1  # 1 分钟图新收的几根每分钟都变：留在行情快照里
    assert set(small["timeframes"]["1m"]) == {"candles"} and "ema20" in live["timeframes"]["1m"]  # 1 分钟图每分钟收一根：指标留在行情快照里
    assert all(not set(w) & LIVE_WINDOW_KEYS for b in buckets for w in b.get("windows", {}).values())  # 最小周期是 1 分钟：「现在怎么样」每分钟都可能变，留在后面
    assert all(set(w) <= LIVE_WINDOW_KEYS for w in live["windows"].values()) and "prev_high" in slow["windows"]["1H"] and "prev_high" in mid["windows"]["30m"]
    assert "now" in live["value_area"] and "poc" not in live["value_area"]
    assert live["timeframes"]["1D"] == snap["timeframes"]["1D"]  # K 线不够的周期原样留着


async def test_knowledge_entries_are_reused_until_the_market_state_changes(rt):
    """叫醒原因每轮都不一样：行情状态没变时沿用上一批知识库条目，状态变了或过了时限才重新查。"""
    from ouy.agent import watcher as w

    calls: list[list[str]] = []

    async def context(queries, k=6, max_chars=3000):
        calls.append(list(queries))
        return [f"条目 {len(calls)}"]

    rt.watcher.kb.context = context
    st = rt.watcher._state(BTC)
    quiet = {"last": 100.0, "timeframes": {"1H": {"adx14": 15, "structure": "range"}, "4H": {"structure": "range"}}}
    trend = {"last": 100.0, "timeframes": {"1H": {"adx14": 30, "structure": "uptrend"}, "4H": {"structure": "uptrend"}}}
    first = await rt.watcher._knowledge(st, quiet, None, "定时复盘", None)
    assert await rt.watcher._knowledge(st, quiet, None, "触及观察位 100.5", None) is first and len(calls) == 1
    assert calls[0][-1] == "定时复盘"  # 重新查的那一次带上叫醒原因
    assert await rt.watcher._knowledge(st, trend, None, "定时复盘", None) == ["条目 2"]  # 行情状态变了
    assert await rt.watcher._knowledge(st, quiet, None, "定时复盘", None) is first and len(calls) == 2  # 回到刚才的状态：还是刚才那一批
    st.kb_picks = {key: (ts - w.KB_HOLD_SECONDS - 1, found) for key, (ts, found) in st.kb_picks.items()}
    assert await rt.watcher._knowledge(st, trend, None, "定时复盘", None) == ["条目 3"]  # 过了时限


def test_explicit_cache_breakpoints_sit_at_the_end_of_each_stable_block():
    """标了显式断点服务端才把「到这里为止」整段写进缓存并上报写入量；最多四个，规则那条和每轮都变的用户消息不标。"""
    mark = core.STABLE_MARK
    body = {"instructions": "规" * 300, "input": [{"role": "user", "content": f"设置\n{mark}\n大周期K线\n{mark}\n报价 1"}], "tools": []}
    assert core.lead_instructions(body, True)
    point = {"mode": "explicit"}
    assert body["input"] == [
        {"role": "developer", "content": "规" * 300},
        {"role": "developer", "content": [{"type": "input_text", "text": "设置", "prompt_cache_breakpoint": point}]},
        {"role": "developer", "content": [{"type": "input_text", "text": "大周期K线", "prompt_cache_breakpoint": point}]},
        {"role": "user", "content": "报价 1"},
    ]
    assert not core.lead_instructions(body, True)  # 工具往返时再过一遍：不重复标
    only_rules = {"instructions": "规" * 300, "input": [{"role": "user", "content": "报价"}]}
    assert core.lead_instructions(only_rules, True)  # 没有别的不变资料时断点标在规则后面
    assert only_rules["input"][0]["content"][0]["prompt_cache_breakpoint"] == point and only_rules["input"][1] == {"role": "user", "content": "报价"}
    blocks = ["设置" * 50, "策略" * 40, "大" * 30, "中", "知识" * 20, "小" * 10]
    many = {"instructions": "规" * 300, "input": [{"role": "user", "content": f"\n{mark}\n".join([*blocks, "报价"])}]}
    assert core.lead_instructions(many, True)
    marked = [item["content"][0]["text"] for item in many["input"] if isinstance(item["content"], list)]
    assert marked == [blocks[0], blocks[1], blocks[4], blocks[5]]  # 多于四段：去掉最短的两段后面的断点，最后一段的保留
    assert [item["content"] if isinstance(item["content"], str) else item["content"][0]["text"] for item in many["input"]] == ["规" * 300, *blocks, "报价"]  # 内容和顺序不变
    core.drop_breakpoints(many)
    assert all(isinstance(item["content"], str) for item in many["input"])


async def test_a_gateway_that_rejects_breakpoints_gets_the_same_request_without_them(settings, monkeypatch):
    """上游不认显式断点（400 里点名这个参数）：去掉断点原样重发一次，这个进程之后不再标；别的报错不重发。"""
    import httpx2

    seen: list[dict] = []

    async def upstream(self, request):
        body = json.loads(request.content)
        seen.append(body)
        if "prompt_cache_breakpoint" in request.content.decode():
            return httpx2.Response(400, json={"error": {"code": "invalid_parameter", "message": "input[1].prompt_cache_breakpoint is not supported on this model"}})
        return httpx2.Response(503 if body.get("fail") else 200, json={"ok": True})

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", upstream)
    client = core._http_client(settings.model_copy(update={"ai_cache_breakpoints": True}))
    body = {"instructions": "规" * 300, "input": [{"role": "user", "content": f"设置\n{core.STABLE_MARK}\n报价"}]}
    async with client:
        first = await client.post("https://model.invalid/v1/responses", json=body)
        assert first.status_code == 200 and len(seen) == 2
        assert "prompt_cache_breakpoint" in json.dumps(seen[0]) and "prompt_cache_breakpoint" not in json.dumps(seen[1])
        assert seen[1]["input"] == [{"role": "developer", "content": "规" * 300}, {"role": "developer", "content": "设置"}, {"role": "user", "content": "报价"}]
        await client.post("https://model.invalid/v1/responses", json=body)
        assert len(seen) == 3 and "prompt_cache_breakpoint" not in json.dumps(seen[2])  # 之后直接不标
        assert (await client.post("https://model.invalid/v1/responses", json={**body, "fail": True})).status_code == 503 and len(seen) == 4
