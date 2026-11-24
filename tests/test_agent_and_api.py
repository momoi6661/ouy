from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ouy.agent.watcher import InstState, kb_queries
from ouy.app import Runtime, create_app
from ouy.config import Settings
from ouy.indicators import Candle, rsi, summarize
from ouy.knowledge import KnowledgeBase, split_markdown, tokenize
from ouy.models import AgentConfig, TradeAction
from ouy.risk import Rejected
from ouy.store import Store

from .fake_okx import FakeAuth, FakeOkx

BTC = "BTC-USDT-SWAP"
ETH = "ETH-USDT-SWAP"
REPORT = {
    "bias": "long", "confidence": 0.7, "summary": "上升趋势回踩 EMA20 企稳，按趋势回踩策略开多。",
    "plan": {"direction": "long", "entry_low": 59_800, "entry_high": 60_100, "stop_loss": 59_400, "take_profits": [61_000, 62_000],
             "leverage": 5, "invalidation": "15m 收盘跌破 59400"},
    "lines": [{"price": 60_600, "note": "1R，止损移保本"}, {"price": 59_300, "note": "跌破这里原判断失效"}],
}


def scripted_model(seen: list[str]) -> FunctionModel:
    """第一轮让 Agent 调 open_position，拿到工具结果后输出结论。"""

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            seen.append(str(messages[-1].parts[-1].content))
            return ModelResponse(parts=[ToolCallPart("open_position", {"inst_id": BTC, "side": "long", "stop_loss": 59_400, "take_profit": 62_000, "leverage": 5, "reason": "趋势回踩确认"})])
        output_tool = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(output_tool, REPORT)])

    return FunctionModel(run)


def make_rt(settings: Settings, fake: FakeOkx, model) -> Runtime:
    rt = Runtime(settings, gateway=fake, auth=FakeAuth(fake), store=Store(":memory:"), model_factory=lambda: model)
    rt.save_config(AgentConfig(mode="auto", instruments=[BTC], fast_entry="off"))  # 快判有自己的测试（test_fast.py），这里关掉免得假行情出信号叫醒 Worker
    return rt


async def test_consult_snapshot_is_local_and_environment_scoped(rt, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Local brief must not call a model, Worker or exchange")

    monkeypatch.setattr(rt.watcher, "_model", forbidden)
    monkeypatch.setattr(rt.watcher, "_dispatch", forbidden)
    monkeypatch.setattr(rt.gateway, "call", forbidden)
    rt.store.set(f"report:demo:{BTC}", {**REPORT, "plan_order": {"text": "挂单待成交"}})
    rt.store.set(f"report:live:{BTC}", {"summary": "LIVE_SENTINEL"})
    answer = await rt.watcher.consult("/简报", "general", BTC, None, [])
    assert "挂单待成交" in answer["reply"] and "LIVE_SENTINEL" not in answer["reply"]
    assert ETH in answer["reply"] and "暂无复盘记录" in answer["reply"]
    assert not answer["actions"] and not rt.store.work_items("demo")


async def test_consult_has_no_tools_even_for_explicit_execution_request(settings, fake):
    prompts = []

    def model(messages, info):
        assert not info.function_tools and not info.output_tools
        prompts.append(str(messages[-1].parts[-1].content))
        return ModelResponse(parts=[TextPart("这是只读问答，未下单。")])

    rt = make_rt(settings, fake, FunctionModel(model))
    rt.store.set(f"report:demo:{BTC}", REPORT)
    result = await rt.watcher.consult("@BTC 立刻下单", "general", BTC, None, [])
    assert REPORT["summary"] in prompts[0] and len(prompts) == 1
    assert not result["actions"] and not result["ui"] and not result["confirms"]
    assert not fake.positions and not rt.store.work_items("demo")
    assert not rt.store.get("chat_hist:general")
    assert rt.store.get("consult:demo:general")[-1]["答"] == result["reply"]


async def test_consult_http_does_not_promote_requests_to_tasks(client):
    response = await client.post("/api/chat/consult", json={"message": "/简报"})
    assert response.status_code == 200 and response.json()["from"] == "router"
    for params in ({"to": "worker"}, {"queue": True}):
        response = await client.post("/api/chat/consult", json={"message": "开仓", **params})
        assert response.status_code == 400


async def test_review_feeds_real_context_and_executes_through_the_guard(settings, fake):
    prompts: list[str] = []
    rt = make_rt(settings, fake, scripted_model(prompts))
    record = await rt.watcher.review(BTC, "测试")
    assert record["bias"] == "long" and record["actions"][0]["status"] == "executed"
    assert fake.positions[BTC]["pos"] == pytest.approx(16.66)
    assert fake.algos[0]["slTriggerPx"] == "59400"
    prompt = prompts[0]
    assert "【行情快照】" in prompt and '"4H"' in prompt and "权益 10000.00" in prompt
    assert "【知识库相关条目】" in prompt and record["knowledge"]
    kinds = [e["kind"] for e in rt.store.journal("demo")]
    assert "cycle" in kinds and "action" in kinds
    assert rt.store.get(f"report:demo:{BTC}")["plan"]["stop_loss"] == 59_400


async def test_triggers(settings, fake):
    rt = make_rt(settings, fake, "test")
    cfg = rt.config()
    w = rt.watcher
    st = InstState()
    assert "首次" in w._trigger(cfg, st, None, 1000.0)
    st.last_review_ts, st.last_review_px, st.next_due_ts = 1000.0, 60_000, 1300.0
    st.report = {"lines": [{"price": 60_600, "note": "1R"}], "plan": None}
    st.prev_px, st.last_px = 60_000, 60_010
    assert w._trigger(cfg, st, None, 1010.0) is None  # 距上次复盘太近
    assert w._trigger(cfg, st, None, 1100.0) is None  # 没异动、没到点
    st.prev_px, st.last_px = 60_590, 60_610
    assert w._trigger(cfg, st, None, 1010.0) is None
    st.report = {}
    st.prev_px, st.last_px = 60_300, 60_400
    assert "上涨" in w._trigger(cfg, st, None, 1100.0)
    st.prev_px = st.last_px = 60_010
    assert w._trigger(cfg, st, None, 1400.0) == "定时复盘"
    pos = (await _open(rt)).position(BTC)
    assert "新的持仓" in w._trigger(cfg, st, pos, 1001.0)  # 持仓变化不受最短间隔限制
    assert "被平掉" in w._trigger(cfg, st, None, 1002.0)


async def _open(rt: Runtime, source: str = "agent"):
    await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_400, leverage=5), source=source)  # type: ignore[arg-type]
    return await rt.trader.account()


async def test_emergency_exit_closes_without_waiting_for_the_model(settings, fake):
    rt = make_rt(settings, fake, "test")
    await _open(rt)
    fake.algos.clear()  # 假设交易所止损没生效
    fake.prices[BTC] = 58_000  # 浮亏 16.66*0.01*2000 = 333U > 2.5% 权益
    await rt.watcher._emergency(rt.config(), await rt.trader.account())
    assert BTC not in fake.positions
    assert any(e["kind"] == "alert" for e in rt.store.journal("demo"))


async def test_manual_positions_are_only_advised_on(settings, fake):
    """用户手动开的仓：Agent 的减仓 / 平仓 / 改止损都变成等确认的方案，紧急线也只提醒不平；关掉开关后恢复自动。"""
    rt = make_rt(settings, fake, "test")
    assert rt.config().hands_off_manual is False  # 默认：用户自己开的仓也由 Worker 管
    rt.save_config(rt.config().model_copy(update={"hands_off_manual": True}))
    await _open(rt, source="user")
    lot = rt.trader.lots("demo", BTC)[0]
    assert lot["source"] == "user"

    for action in (
        TradeAction(kind="close", inst_id=BTC, reason="走势不对"),
        TradeAction(kind="reduce", inst_id=BTC, fraction=0.5, lot_id=lot["id"]),
        TradeAction(kind="set_stops", inst_id=BTC, stop_loss=59_600),
    ):
        asked = await rt.trader.submit(action, source="agent")
        assert asked.status == "pending" and "手动开的仓" in asked.message, asked.message
    assert BTC in fake.positions and [a["slTriggerPx"] for a in fake.algos] == ["59400"]

    # 用户点确认后照常执行
    done = await rt.trader.approve(asked.pending_id)
    assert done.status == "executed" and [a["slTriggerPx"] for a in fake.algos] == ["59600"], done.message

    fake.algos.clear()
    fake.prices[BTC] = 58_000
    await rt.watcher._emergency(rt.config(), await rt.trader.account())
    assert BTC in fake.positions  # 没有替用户平
    assert any(e["kind"] == "alert" and "手动开的仓" in e["title"] for e in rt.store.journal("demo"))

    rt.save_config(rt.config().model_copy(update={"hands_off_manual": False}))
    # 开关关着：Worker 可以保护（收紧止损），但不能替用户了结——平仓、减仓、设止盈仍要他确认，也不能写成触发线绕过去
    fake.prices[BTC] = 60_300
    moved = await rt.trader.submit(TradeAction(kind="set_stops", inst_id=BTC, stop_loss=59_700), source="agent")
    assert moved.status == "executed", moved.message
    for action in (TradeAction(kind="close", inst_id=BTC, reason="走势不对"), TradeAction(kind="set_stops", inst_id=BTC, take_profit=60_500)):
        asked = await rt.trader.submit(action, source="agent")
        assert asked.status == "pending" and "由用户定" in asked.message and BTC in fake.positions
    eng = rt.watcher.strategies
    with pytest.raises(Rejected, match="用户手动开的仓"):
        eng.deploy(rt.config(), BTC, "trigger", "", {"when": "price_above", "level": 60_800, "do": "close", "lot_id": lot["id"]})
    ok = eng.deploy(rt.config(), BTC, "trigger", "", {"when": "price_above", "level": 60_800, "do": "close", "lot_id": lot["id"]}, source="user")
    assert ok["status"] == "armed"
    # 进场时程序自动带的分批止盈碰到用户的仓：不执行
    with pytest.raises(Rejected, match="用户手动开的仓"):
        await eng._order(rt.config(), {"id": 0, "inst_id": BTC, "params": {"by": "Worker"}}, TradeAction(kind="reduce", inst_id=BTC, fraction=0.5), "分批止盈")
    assert BTC in fake.positions


async def test_emergency_exit_respects_advise_mode(settings, fake):
    rt = make_rt(settings, fake, "test")
    await _open(rt)
    fake.algos.clear()
    fake.prices[BTC] = 58_000
    rt.save_config(AgentConfig(mode="advise", instruments=[BTC]))
    await rt.watcher._emergency(rt.config(), await rt.trader.account())
    assert BTC in fake.positions


async def test_watcher_loop_runs_a_review_and_degrades_on_lost_auth(settings, fake):
    rt = make_rt(settings, fake, scripted_model([]))
    rt.save_config(AgentConfig(mode="auto", instruments=[BTC], tick_seconds=2))
    await rt.watcher.start()
    for _ in range(50):
        await asyncio.sleep(0.05)
        if BTC in fake.positions and not rt.watcher._state(BTC).reviewing:
            break
    assert BTC in fake.positions and rt.watcher.status()["instruments"][BTC]["report"]["bias"] == "long"
    try:
        fake.logged_in = False
        writes = len(fake.called("swap_place_order"))
        for _ in range(80):
            await asyncio.sleep(0.05)
            if rt.watcher._auth_retry_at:
                break
        assert rt.watcher.running and rt.watcher._auth_retry_at
        assert len(fake.called("swap_place_order")) == writes
        assert any("授权暂不可用" in e["title"] for e in rt.store.journal("demo"))
    finally:
        await rt.watcher.stop()


async def test_start_refuses_without_login(settings, fake):
    fake.logged_in = False
    rt = make_rt(settings, fake, "test")
    with pytest.raises(Exception, match="credentials"):
        await rt.watcher.start()
    assert not rt.watcher.running


# ---------- 指标 / 知识库 ----------

def test_indicators_on_a_steady_uptrend():
    candles = [Candle(i, 100 + i, 101.5 + i, 99.5 + i, 101 + i, 10) for i in range(260)]
    s = summarize(candles)
    assert s["ema_stack"] == "bull" and s["rsi14"] == 100 and s["adx14"] > 25 and s["plus_di"] > s["minus_di"]
    assert rsi([1, 2, 3])[-1] is None


def test_worker_chart_separates_intrabar_wick_from_confirmed_structure():
    from ouy.indicators import read_chart, structure

    frame = [Candle(i, 100, 101, 99, 100, 10) for i in range(40)]
    moving = Candle(40, 100, 130, 98, 125, 80, False)
    snap = summarize([*frame, moving])
    assert snap["last"] == 125 and snap["last_closed_price"] == 100 and not snap["latest_bar_confirmed"]
    assert snap["structure"] == "range" and snap["change_pct_20"] == 0
    assert snap["atr_pct"] == summarize(frame)["atr_pct"]
    assert structure([(1, 101), (3, 101)], [(2, 99), (4, 98)]) == "range"
    partial = [Candle(i, 100, 130, 98, 125, 80, False) for i in range(40)]
    assert summarize(partial)["insufficient"] and not read_chart(partial)


def test_kb_chunks_keep_strategy_context_and_search_finds_them(settings):
    kb = KnowledgeBase(settings)
    assert len(kb.chunks) > 30 and "禁止事项" in kb.core_text()
    assert "止损" in tokenize("止损怎么放") and "atr" in tokenize("1.5 ATR")
    chunk = split_markdown("x.md", "# 策略A\n\n## 入场\n回踩 EMA20\n\n## 止损\n低点下方")[0]
    assert chunk.title == "策略A" and "〔入场〕" in chunk.text and "〔止损〕" in chunk.text

    async def top(q: str) -> str:
        hits = await kb.search(q, 3)
        return " ".join(c.source for c, _ in hits)

    loop = asyncio.new_event_loop()
    try:
        assert "liquidity_sweep_reversal" in loop.run_until_complete(top("跌破前低又收回 长下影 插针 假突破"))
        assert "leverage_sizing" in loop.run_until_complete(top("止损距离 杠杆怎么选"))
        assert "funding_oi_crowding" in loop.run_until_complete(top("资金费率极端 多头拥挤"))
    finally:
        loop.close()


def test_kb_queries_follow_market_state():
    snap = {"last": 100, "timeframes": {"4H": {"structure": "uptrend"}, "1H": {"adx14": 30, "atr14": 1, "high_20": 100.2, "low_20": 90, "bb_width_pct": 5, "atr_pct": 1}}, "derivatives": {"funding_rate_pct": 0.06}}
    qs = " | ".join(kb_queries(snap, None, "定时复盘"))
    assert "上升趋势" in qs and "突破" in qs and "资金费率" in qs and "开仓前" in qs


def test_kb_doc_read_save_delete_and_path_traversal_guard(settings, tmp_path):
    root = tmp_path / "kb"
    (root / "core").mkdir(parents=True)
    (root / "core" / "discipline.md").write_text("# 纪律清单\n\n必须止损\n", encoding="utf-8")
    kb = KnowledgeBase(settings, root=root)
    doc = kb.save_doc("strategies/my_strat", "# 策略：自建测试\n\n## 入场\n回踩买")
    assert doc["source"] == "strategies/my_strat.md" and doc["category"] == "strategy"
    assert "回踩买" in kb.get_doc("strategies/my_strat.md")["content"]
    with pytest.raises(ValueError):
        kb.get_doc("../pyproject.toml")
    with pytest.raises(ValueError):
        kb.delete_doc("core/discipline.md")
    kb.delete_doc("strategies/my_strat.md")
    assert all(d["source"] != "strategies/my_strat.md" for d in kb.stats()["documents"])


# ---------- HTTP API ----------

@pytest.fixture
async def client(settings, fake):
    rt = make_rt(settings, fake, scripted_model([]))
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            c.rt = rt  # type: ignore[attr-defined]
            yield c


async def test_state_and_login_flow(client, fake):
    fake.logged_in = False
    assert (await client.get("/api/state")).json()["auth"]["status"] == "not_logged_in"
    login = (await client.post("/api/auth/login")).json()
    assert login["userCode"] == "ABCD-EFGH" and login["verificationUri"].startswith("https://www.okx.com/")
    assert (await client.get("/api/account")).status_code == 401
    fake.logged_in = True
    assert (await client.get("/api/auth/status")).json()["status"] == "logged_in"
    assert (await client.get("/api/account")).json()["equity"] == 10_000


async def test_live_needs_explicit_confirmation(client):
    cfg = (await client.get("/api/state")).json()["config"]
    cfg["env"] = "live"
    assert (await client.put("/api/agent/config", json={"config": cfg})).status_code == 400
    ok = await client.put("/api/agent/config", json={"config": cfg, "confirm_live": True})
    assert ok.status_code == 200 and ok.json()["config"]["env"] == "live"


async def test_manual_trade_and_market_endpoints(client, fake):
    r = await client.post("/api/trade", json={"kind": "open", "inst_id": BTC, "side": "short", "stop_loss": 60_600, "leverage": 4})
    assert r.json()["status"] == "executed" and fake.positions[BTC]["pos"] < 0
    candles = (await client.get("/api/market/candles", params={"inst_id": BTC, "bar": "15m", "limit": 50})).json()
    assert len(candles) == 50 and candles[0]["time"] < candles[-1]["time"]
    tick = (await client.get("/api/market/tickers")).json()
    assert tick[0]["max_leverage"] == 100
    assert (await client.post("/api/trade/panic")).status_code == 200 and not fake.positions


async def test_okx_passthrough_guards_writes(client, fake):
    tools = (await client.get("/api/okx/tools")).json()
    assert any(t["name"] == "swap_set_leverage" and not t["readOnly"] for t in tools)
    assert (await client.post("/api/okx/call", json={"tool": "market_get_ticker", "args": {"instId": BTC}})).status_code == 200
    body = {"tool": "swap_set_leverage", "args": {"instId": BTC, "lever": "7", "mgnMode": "cross"}}
    assert (await client.post("/api/okx/call", json=body)).status_code == 400
    assert (await client.post("/api/okx/call", json={**body, "confirm": True})).status_code == 200 and fake.leverage[BTC] == 7


async def test_cross_site_and_foreign_host_requests_are_refused(client):
    assert (await client.post("/api/agent/stop", headers={"Origin": "https://evil.example"})).status_code == 403
    assert (await client.get("/api/state", headers={"Host": "evil.example"})).status_code == 403
    assert (await client.post("/api/agent/stop", headers={"Origin": "http://testserver"})).status_code == 200


async def test_agent_review_endpoint_and_journal(client, fake):
    r = await client.post("/api/agent/review", json={"inst_id": BTC})
    assert r.status_code == 200 and r.json()["plan"]["stop_loss"] == 59_400
    journal = (await client.get("/api/journal")).json()
    assert journal[0]["kind"] == "cycle" and any(j["kind"] == "action" for j in journal)
    hits = (await client.get("/api/kb/search", params={"q": "止损移保本"})).json()
    assert hits and "text" in hits[0]


async def test_access_key_is_required_when_configured(tmp_path, fake):
    settings = Settings(_env_file=None, OPENAI_API_KEY="k", LOCAL_PROXY_URL="", KB_EMBEDDING_MODEL="", OUY_DATA_DIR=str(tmp_path), OUY_ACCESS_KEY="s3cret", OUY_HOST="0.0.0.0")
    app = create_app(make_rt(settings, fake, "test"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://192.168.1.5:8000") as c:
            assert (await c.get("/api/state")).status_code == 401
            assert (await c.post("/api/unlock", json={"key": "s3cret"})).status_code == 200
            assert (await c.get("/api/state")).status_code == 200
    with pytest.raises(RuntimeError, match="OUY_ACCESS_KEY"):
        create_app(Runtime(Settings(_env_file=None, OUY_HOST="0.0.0.0", OUY_DATA_DIR=str(tmp_path), KB_EMBEDDING_MODEL=""), gateway=fake, auth=FakeAuth(fake), store=Store(":memory:")))


async def test_auth_status_polls_one_at_a_time_and_reports_why_authorization_failed(settings, monkeypatch):
    import ouy.okx.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_auth_binary", lambda: None)
    auth = auth_mod.OkxAuth(settings)
    calls: list[tuple[str, ...]] = []
    running = 0
    replies = [
        '{"status":"pending","userCode":"ABCD-EFGH","verificationUri":"https://www.okx.com/account/oauth?flow=device","ttl":590}',
        '{"status":"not_logged_in"}',
    ]

    async def fake_run(*args: str, timeout: float = 60, binary: str | None = None):
        nonlocal running
        running += 1
        assert running == 1, "两次状态查询不能同时进行"
        calls.append(args)
        await asyncio.sleep(0.01)
        running -= 1
        out = replies[min(len(calls) - 1, len(replies) - 1)]
        return 0, out, "Error: token exchange failed: access_denied sk-abcdefghijklmnopqrstuvwxyz0123456789"

    monkeypatch.setattr(auth, "_run", fake_run)
    results = await asyncio.gather(*(auth.status() for _ in range(5)))
    assert len(calls) == 1 and all(r["status"] == "pending" for r in results)  # 等待期间限速，不会连着查

    monkeypatch.setattr(auth_mod, "POLL_INTERVAL", 0.0)
    auth._cached["ttl"] = 590  # type: ignore[index]
    failed = await auth.status()
    assert failed["status"] == "not_logged_in" and "access_denied" in failed["error"]
    assert "abcdefghijklmnop" not in failed["error"]  # 长串（可能是令牌）不外泄


async def test_chat_turns_an_entry_into_a_proposal_the_user_confirms(settings, fake):
    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        router = any(t.name == "ask_worker" for t in info.function_tools)
        answered = any(isinstance(p, ToolReturnPart) for m in messages[-1:] for p in m.parts)
        if router:  # Router 没有交易工具，只能把任务交给这个币的 Worker
            assert not any(t.name == "open_position" for t in info.function_tools)
            if not answered:
                return ModelResponse(parts=[ToolCallPart("ask_worker", {"inst_id": BTC, "task": "用户要做多 BTC，投 300 U，给出方案"})])
            return ModelResponse(parts=[TextPart("方案在卡片里，跌破 59400 作废。")])
        if not answered:
            return ModelResponse(parts=[ToolCallPart("open_position", {
                "inst_id": BTC, "side": "long", "stop_loss": 59_400, "take_profit": 61_200, "take_profits": [61_200, 62_000], "leverage": 5,
                "margin_usdt": 300, "entry_low": 59_900, "entry_high": 60_050, "reason": "回踩企稳",
            })])
        return ModelResponse(parts=[TextPart("做多方案已生成，止损 59400。")])

    rt = make_rt(settings, fake, FunctionModel(run))
    out = await rt.watcher.chat("帮我做多 BTC，投 300 U", thread="BTC-USDT-SWAP", inst_id=BTC)
    card = out["actions"][0]
    assert card["status"] == "pending" and card["pending_status"] == "waiting" and BTC not in fake.positions  # 还没下单
    assert card["detail"]["contracts"] == "2.5" and card["detail"]["margin_usd"] == 300 and card["detail"]["entry_low"] == 59_900
    assert card["detail"]["take_profits"] == [61_200, 62_000] and card["detail"]["reward_risk"] == 2.0

    # 对话按币种分开保存，刷新页面还在
    assert [m["role"] for m in rt.watcher.chat_log("BTC-USDT-SWAP")] == ["user", "ai"]
    assert rt.watcher.chat_log("ETH-USDT-SWAP") == []

    done = await rt.trader.approve(card["pending_id"])
    assert done.status == "executed" and fake.positions[BTC]["pos"] == pytest.approx(2.5)
    assert rt.watcher.chat_log("BTC-USDT-SWAP")[1]["actions"][0]["pending_status"] == "executed"

    rt.watcher.clear_chat("BTC-USDT-SWAP")
    assert rt.watcher.chat_log("BTC-USDT-SWAP") == []


async def test_router_returns_workers_trigger_approval_in_the_shared_conversation(settings, fake):
    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        router = any(tool.name == "ask_worker" for tool in info.function_tools)
        answered = any(isinstance(part, ToolReturnPart) for msg in messages[-1:] for part in msg.parts)
        if router:
            assert not any(tool.name == "tell_worker" for tool in info.function_tools)
            if not answered:
                return ModelResponse(parts=[ToolCallPart("ask_worker", {"inst_id": BTC, "task": "准备突破入场触发线"})])
            return ModelResponse(parts=[TextPart("等你同意。")])
        return ModelResponse(parts=[ToolCallPart("add_trigger", {
            "inst_id": BTC, "when": "price_above", "level": 60_250, "do": "open_market",
            "side": "long", "stop_loss": 59_900, "reason": "突破入场",
        })])

    rt = make_rt(settings, fake, FunctionModel(run))
    result = await rt.watcher.chat("@BTC 准备突破入场触发线", inst_id=BTC)
    [card] = result["actions"]
    assert card == rt.watcher.chat_log("general")[-1]["actions"][0]
    assert card["pending_status"] == "waiting"
    assert rt.store.get_strategy(card["strategy_id"])["status"] == "proposed"
    assert not fake.orders and not fake.positions


async def test_style_changes_timeframes_and_alt_reviews_carry_btc(settings, fake):
    prompts: list[str] = []
    rt = make_rt(settings, fake, scripted_model(prompts))
    # 风格按品种设置：ETH 单独设成波段，默认风格是日内
    rt.save_config(AgentConfig(mode="advise", instruments=[ETH], styles={"eth-usdt-swap": "swing"}))
    await rt.watcher.review(ETH, "测试")
    prompt = prompts[0]
    assert "【这个品种的交易风格】波段" in prompt and '"1D"' in prompt and '"5m"' not in prompt.split("【行情快照】")[1]
    assert "【BTC 大盘】" in prompt and '"inst_id": "BTC-USDT-SWAP"' in prompt

    rt.save_config(AgentConfig(mode="advise", instruments=[BTC], style="scalp", styles={ETH: "swing"}))
    await rt.watcher.review(BTC, "测试")
    assert "【这个品种的交易风格】超短线" in prompts[1] and '"1m"' in prompts[1] and "【BTC 大盘】" not in prompts[1]


async def test_sharp_btc_move_wakes_instruments_that_hold_positions(settings, fake):
    rt = make_rt(settings, fake, "test")
    cfg = AgentConfig(mode="auto", instruments=[ETH])
    w = rt.watcher
    assert await w._watch_btc(cfg, {}, 1000.0) is None  # 第一次只记参考价
    fake.set_price(BTC, 60_200)
    assert await w._watch_btc(cfg, {}, 1060.0) is None  # 0.33%，不算异动
    fake.set_price(BTC, 59_400)
    alert = await w._watch_btc(cfg, {}, 1120.0)
    assert alert and "急跌" in alert and "1.00%" in alert
    assert any("BTC 短时间急跌" in e["title"] for e in rt.store.journal("demo"))
    assert await w._watch_btc(cfg, {}, 1180.0) is None  # 提醒过一次后重新计


async def test_chat_agent_can_operate_the_app_and_loosening_needs_the_user(settings, fake):
    calls = [
        ("edit_watchlist", {"action": "add", "inst_id": "eth"}),
        ("set_trading_style", {"inst_id": "ETH-USDT-SWAP", "style": "scalp"}),
        ("update_risk", {"field": "max_leverage", "value": 5}),      # 收紧：直接生效
        ("update_risk", {"field": "risk_per_trade_pct", "value": 3}),  # 放宽：要用户确认
        ("set_permission", {"kind": "add", "level": "confirm"}),
        ("show_instrument", {"inst_id": "ETH", "timeframe": "1H"}),
        ("open_order_window", {"inst_id": "ETH", "side": "short", "stop_loss": 3050, "margin_usdt": 100}),
        ("set_watching", {"on": False}),
    ]

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        done = sum(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
        if done < len(calls):
            return ModelResponse(parts=[ToolCallPart(*calls[done])])
        return ModelResponse(parts=[TextPart("都处理好了。")])

    rt = make_rt(settings, fake, FunctionModel(run))
    out = await rt.watcher.chat("把 ETH 加进来做超短线，杠杆上限降到 5，单仓风险调到 3%", thread=BTC, inst_id=BTC)
    cfg = rt.config()
    assert cfg.instruments == [BTC, ETH] and cfg.style_for(ETH) == "scalp" and cfg.style_for(BTC) == "intraday"
    assert cfg.risk.max_leverage == 5 and cfg.risk.risk_per_trade_pct == 1.0  # 放宽的那项没有生效
    assert cfg.permission_for("add") == "confirm"
    assert [s["tool"] for s in out["steps"]] == [c[0] for c in calls] and all(s["result"] for s in out["steps"])
    confirm = out["confirms"][0]
    assert "放宽风控" in confirm["summary"] and confirm["patch"]["risk"]["risk_per_trade_pct"] == 3
    kinds = [c["type"] for c in out["ui"]]
    assert {"select_inst", "set_bar", "open_trade", "confirm_config", "refresh"} <= set(kinds)
    trade = next(c for c in out["ui"] if c["type"] == "open_trade")
    assert trade["inst_id"] == ETH and trade["side"] == "short" and trade["margin_usdt"] == 100
    assert rt.watcher.chat_log(BTC)[1]["steps"][0]["label"] == "改盯盘列表"


# ---------- Super-agent：Router / Worker / 情报 Worker ----------


async def test_worker_review_is_a_work_item_and_router_relays_the_plan(settings, fake):
    """Worker 不直接对用户说话：复盘是一条任务，值得用户知道的结果由 Router 主动发到这个币的对话区。"""
    rt = make_rt(settings, fake, scripted_model([]))
    await rt.watcher.review(BTC, "触及盯盘价位")
    work = rt.store.work_items("demo")
    assert [(w["inst_id"], w["status"]) for w in work] == [(BTC, "done")] and "上升趋势" in work[0]["result"]
    msg = rt.store.get(f"chat:{BTC}")[-1]
    assert msg["proactive"] and msg["from"] == f"{BTC} Worker" and msg["text"].startswith("我刚做了操作")
    assert msg["actions"][0]["status"] == "executed"


async def test_router_gate_keeps_quiet_on_soft_events(settings, fake):
    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"act": False, "why": "价格还在区间中部"})])

    rt = make_rt(settings, fake, FunctionModel(run))
    st = rt.watcher._state(BTC)
    st.report = {**REPORT, "ts": 0, "price": 60_000}
    await rt.watcher._review_task(BTC, "定时复盘", gate=True)
    assert st.skips == 1 and st.last_gate == "价格还在区间中部" and BTC not in fake.positions
    assert [(w["status"], w["source"]) for w in rt.store.work_items("demo")] == [("skipped", "event")]


async def test_talking_straight_to_a_coin_worker_and_per_coin_settings(settings, fake):
    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert any(t.name == "open_position" for t in info.function_tools)  # 直通路由：是带交易工具的 Worker 在回答，不是 Router
        return ModelResponse(parts=[TextPart("现在在区间中部，下沿 59800 我会挂多单。")])

    rt = make_rt(settings, fake, FunctionModel(run))
    out = await rt.watcher.chat("你现在怎么看", thread=BTC, inst_id=BTC, to="worker")
    assert out["from"] == f"{BTC} Worker" and "59800" in out["reply"]
    assert rt.store.get(f"chat:{BTC}")[-1]["from"] == f"{BTC} Worker"

    cfg = AgentConfig(worker_model="big", workers={BTC: {"model": "small", "wake": "always", "paused": True}})
    assert (cfg.model_for(BTC), cfg.wake_for(BTC), cfg.model_for(ETH), cfg.wake_for(ETH)) == ("small", "always", "big", "event")
    assert cfg.worker(BTC).paused and not cfg.worker(ETH).paused


async def test_intel_worker_writes_a_brief_the_trading_worker_reads(settings, fake):
    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert [t.name for t in info.function_tools] == ["intel_query"]  # 情报 Worker 没有任何下单工具
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("intel_query", {"tool": "swap_place_order", "args": {}})])
        assert "不允许" in str(returns[0].content)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "bias": "short", "summary": "资金费率 0.08% 偏高，多头拥挤。", "points": ["聪明钱净空"], "unavailable": ["新闻：模拟盘不可用"],
        })])

    rt = make_rt(settings, fake, FunctionModel(run))
    brief = await rt.watcher.intel(BTC)
    assert brief["bias"] == "short" and rt.watcher.brief("demo", BTC)["points"] == ["聪明钱净空"]
    prompts: list[str] = []
    rt.watcher.reset_model()
    rt.watcher.model_factory = lambda: scripted_model(prompts)
    await rt.watcher.review(BTC, "测试")
    assert "情报子代理的简报" in prompts[0] and "多头拥挤" in prompts[0] and "没查到：新闻" in prompts[0]


async def test_okx_passthrough_live_channel_is_read_only(client):
    refused = await client.post("/api/okx/call", json={"tool": "swap_place_order", "args": {}, "confirm": True, "env": "live"})
    assert refused.status_code == 400 and "当前环境" in refused.json()["detail"]


async def test_fast_path_skips_quiet_ticks_without_calling_the_model_and_failures_back_off(settings, fake):
    """Fast Path：定时复盘时价格几乎没动、离关键价位都远，直接按规则跳过，不花模型调用；复盘连续失败时重试间隔拉长。"""
    calls = []

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(1)
        raise RuntimeError("模型挂了")

    rt = make_rt(settings, fake, FunctionModel(run))
    st = rt.watcher._state(BTC)
    st.report = {**REPORT, "ts": 0, "price": 60_000, "plan": None, "lines": [{"price": 61_500, "note": "区间上沿"}]}
    st.last_px, st.last_review_px = 60_020, 60_000
    await rt.watcher._review_task(BTC, "定时复盘", gate=True)
    assert not calls and st.skips == 1 and st.last_gate.startswith("规则过滤")
    assert [(w["status"], w["model"]) for w in rt.store.work_items("demo")] == [("skipped", "规则")]

    st.last_px = 61_480  # 快到盯盘位了：规则不再拦，交给后面的判断
    from ouy.agent.watcher import quiet_reason
    assert quiet_reason(st, rt.config().wake_move_pct) == ""

    waits = []
    for _ in range(3):
        await rt.watcher._review_task(BTC, "价格触及盯盘价位 61500")
        waits.append(round(st.next_due_ts - st.last_review_ts))
    assert st.fails == 3 and waits == [120, 240, 480]


async def test_fourth_quiet_timer_does_not_force_a_worker_review(rt, fake):
    w, cfg = rt.watcher, rt.config().model_copy(update={"instruments": [BTC], "intel_minutes": 0})
    st = w._state(BTC)
    st.skips = 3
    st.last_review_ts = time.time() - 3600
    st.last_review_px = fake.prices[BTC]
    st.report = {"summary": "观察", "lines": []}
    await w._tick(cfg)
    assert st.task is not None
    await st.task
    assert st.skips == 4 and st.last_gate.startswith("规则过滤")


async def test_dead_demo_market_is_flagged_and_sized_down(settings, fake):
    """模拟盘里价差很大或 K 线走平的币：标出来，Agent 在上面每笔风险压到四分之一；用户自己下单不受影响。"""
    from ouy.agent.watcher import demo_liquidity

    snap = {"orderbook": {"spread_pct": 0.495}, "timeframes": {"15m": {"atr_pct": 0.02}}, "real_market": {"timeframes": {"15m": {"atr_pct": 0.4}}}}
    assert "价差" in demo_liquidity(snap) and "波动只有真实市场的 5%" in demo_liquidity(snap)
    assert demo_liquidity({"orderbook": {"spread_pct": 0.002}, "timeframes": {"15m": {"atr_pct": 0.3}}, "real_market": {"timeframes": {"15m": {"atr_pct": 0.4}}}}) == ""

    rt = make_rt(settings, fake, "test")
    full = await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_400, leverage=5), source="agent")
    await rt.trader.submit(TradeAction(kind="close", inst_id=BTC), source="agent")
    rt.trader.illiquid[f"demo:{BTC}"] = "买卖价差 0.495%"
    small = await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_400, leverage=5), source="agent")
    assert full.status == small.status == "executed", small.message
    assert float(small.detail["contracts"]) == pytest.approx(float(full.detail["contracts"]) / 4, rel=0.05)


async def test_a_suggestion_is_a_held_tool_call_and_the_answer_goes_back_to_the_agent(settings, fake):
    """建议就是工具调用：Worker 调用开仓工具，权限要用户同意时这次调用原样挂起、出一张卡片；
    用户点同意，执行的就是那次调用，结果回到 Worker 手上，它从那里接着做完这一轮。拒绝也一样会回到它手上。"""
    seen: list[str] = []

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("open_position", {"inst_id": BTC, "side": "long", "stop_loss": 59_400, "take_profit": 61_000, "leverage": 5, "reason": "回踩企稳"})])
        seen.append(str(returns[-1].content))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "summary": "收到结果，继续管理。"})])

    rt = make_rt(settings, fake, FunctionModel(run))
    rt.save_config(AgentConfig(mode="confirm", instruments=[BTC], intel_minutes=0))
    out = await rt.watcher.review(BTC, "定时复盘")
    assert out["awaiting"] == 1 and BTC not in fake.positions and seen == []  # 这一轮停在工具调用上，模型还没拿到结果
    card = rt.store.get(f"chat:{BTC}")[-1]["actions"][0]
    assert card["status"] == "pending" and card["detail"]["margin_usd"] > 0 and card["action"]["stop_loss"] == 59_400
    status = rt.watcher.status()["instruments"][BTC]
    assert status["awaiting"] == 1 and rt.store.work_items("demo")[0]["status"] == "waiting"
    assert [s["kind"] for s in rt.store.activity("demo")] == ["wake", "tool", "ask"]
    with pytest.raises(RuntimeError):  # 挂着等同意的时候不另起一轮
        await rt.watcher.review(BTC, "定时复盘")

    done = await rt.trader.approve(card["pending_id"])  # 用户点「同意执行」：执行的就是 Worker 那次调用
    assert done.status == "executed" and fake.positions[BTC]["pos"] > 0
    size = fake.positions[BTC]["pos"]
    rt.watcher.on_decided()
    await asyncio.gather(*rt.watcher._resumes)
    assert "用户已同意，已执行" in seen[0] and fake.positions[BTC]["pos"] == size  # 结果回到了模型手上，而且没有重复下单
    assert rt.watcher._state(BTC).report["summary"] == "收到结果，继续管理。" and rt.watcher.awaiting("demo", BTC) == []
    assert rt.store.work_items("demo")[0]["status"] == "done"
    assert [s["kind"] for s in rt.store.activity("demo")][3:] == ["answer", "result", "action", "done"]

    # 用户不同意：同样回到 Worker 手上
    fake._flatten(BTC)
    rt.trader._lot_touch.clear()
    await rt.trader.account("demo")
    seen.clear()
    rt.watcher.reset_model()

    def again(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        answers = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not answers:
            return ModelResponse(parts=[ToolCallPart("open_position", {"inst_id": BTC, "side": "long", "stop_loss": 59_400, "leverage": 5, "reason": "再试一次"})])
        seen.append(str(answers[-1].content))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "plan": None, "summary": "用户没同意，先观望。"})])

    rt.watcher.model_factory = lambda: FunctionModel(again)
    await rt.watcher.review(BTC, "定时复盘")
    rt.trader.reject(rt.store.pending("demo")[0]["id"])
    rt.watcher.on_decided()
    await asyncio.gather(*rt.watcher._resumes)
    assert "没有同意" in seen[0] and BTC not in fake.positions and rt.watcher._state(BTC).report["summary"] == "用户没同意，先观望。"


async def test_an_order_left_unfilled_too_long_wakes_the_worker(settings, fake):
    """Agent 自己挂的限价单太久没成交：叫醒 Worker 去处理，不让它一直挂着。"""
    rt = make_rt(settings, fake, scripted_model([]))
    res = await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", order_type="limit", price=59_900, stop_loss=59_600, risk_pct=0.3), source="agent")
    assert res.status == "executed", res.message
    cfg = rt.config()
    account = await rt.trader.account("demo")
    account.orders[0].created_at = int(time.time() * 1000)
    assert rt.watcher._stale_order(cfg, BTC, account) == ""  # 刚挂的不算
    account.orders[0].created_at = int(time.time() * 1000) - (cfg.stale_minutes_for(cfg.style_for(BTC)) + 5) * 60_000
    why = rt.watcher._stale_order(cfg, BTC, account)
    assert "没成交" in why and "市价" in why
    st = rt.watcher._state(BTC)
    st.last_review_ts, st.stale_order, st.had_position = time.time() - 600, why, False
    assert rt.watcher._trigger(cfg, st, None, time.time()) == why


async def test_trading_worker_dispatches_its_own_subagents(settings, fake):
    """交易 Worker 是小组的组长：它自己派子代理（情报 / 聪明钱 / 风险复核）去做事，子代理不能下单，结果记在小组里。"""
    seen: list[str] = []

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = [t.name for t in info.function_tools]
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if tools == ["intel_query"]:  # 子代理：只有只读查询工具
            seen.append(str(messages[-1].parts[-1].content))
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"bias": "neutral", "summary": "可以做：止损离强平价足够远。", "points": ["5 倍杠杆下止损距离 1%"]})])
        if not returns:
            assert "ask_subagent" in tools
            return ModelResponse(parts=[ToolCallPart("ask_subagent", {"inst_id": BTC, "role": "risk", "task": "打算 5 倍做多，入场 60000，止损 59400"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, REPORT)])

    rt = make_rt(settings, fake, FunctionModel(run))
    await rt.watcher.review(BTC, "定时复盘")
    assert "打算 5 倍做多" in seen[0] and "【账户】" in seen[0]  # 风险复核拿到了这笔单和账户情况
    team = {m["role"]: m for m in rt.watcher.status()["instruments"][BTC]["team"]}
    assert set(team) == {"intel", "risk"} and team["risk"]["brief"]["summary"].startswith("可以做") and not team["risk"]["running"]
    work = [w for w in rt.store.work_items("demo") if w["source"] == "risk"]
    assert work and work[0]["status"] == "done" and "BTC Worker 派的" in work[0]["goal"]
    cfg = rt.config()
    assert cfg.intel_minutes == 0 and not cfg.sub_enabled("smart")  # 默认不定时更新；聪明钱默认关
    with pytest.raises(RuntimeError, match="关掉"):
        await rt.watcher.intel(BTC, role="smart")
    rt.save_config(cfg.model_copy(update={"subagents": {"intel": False, "smart": False, "risk": False}}))
    assert rt.watcher.status()["instruments"][BTC]["team"] == []


async def test_every_step_an_agent_takes_shows_up_in_the_activity_stream(settings, fake):
    """活动流：被什么唤醒、调了什么工具、得到什么、做了什么操作、最后的结论，一步步都记下来并推给界面。"""
    rt = make_rt(settings, fake, scripted_model([]))
    pushed: list[dict] = []
    rt.bus.publish = (lambda orig: lambda kind, data=None: (pushed.append(data) if kind == "activity" else None, orig(kind, data))[1])(rt.bus.publish)
    await rt.watcher.review(BTC, "触及盯盘价位 60600")
    steps = rt.store.activity("demo")
    assert [s["kind"] for s in steps] == ["wake", "tool", "result", "action", "done"]
    assert all(s["agent"] == "worker" and s["inst_id"] == BTC and s["work_id"] == steps[0]["work_id"] for s in steps)
    assert steps[0]["text"] == "触及盯盘价位 60600" and steps[1]["data"]["tool"] == "open_position" and "上升趋势" in steps[-1]["text"]
    assert [p["id"] for p in pushed] == [s["id"] for s in steps]
    assert rt.store.activity("demo", after_id=steps[2]["id"])[0]["kind"] == "action"


async def test_worker_deploys_a_strategy_and_the_watch_loop_fires_it_without_the_model(settings, fake):
    """慢的一层（Worker）只负责布置；快的一层（盯盘循环里的策略执行器）到价立刻下单，不再调用模型。"""
    calls = {"n": 0}

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            assert "deploy_strategy" in [t.name for t in info.function_tools]
            return ModelResponse(parts=[ToolCallPart("deploy_strategy", {
                "inst_id": BTC, "kind": "breakout", "side": "long", "trigger": 60_500, "stop_loss": 60_200, "take_profit": 61_500,
                "breakeven_r": 1, "partial_r": 2, "partial_fraction": 0.5, "reason": "突破前高就追",
            })])
        assert "策略 #1 已布置" in str(returns[-1].content)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "plan": None})])

    rt = make_rt(settings, fake, FunctionModel(run))
    rt.save_config(rt.config().model_copy(update={"intel_minutes": 0}))
    await rt.watcher.review(BTC, "定时复盘")
    row = next(r for r in rt.watcher.status()["instruments"][BTC]["strategies"] if r["kind"] == "breakout")
    assert row["label"] == "突破追单" and row["status"] == "armed" and "到 1R 移保本" in row["text"] and BTC not in fake.positions

    used = calls["n"]
    st = rt.watcher._state(BTC)
    st.last_review_ts, st.next_due_ts = time.time(), time.time() + 3600  # 这段时间不该唤醒 Worker
    fake.set_price(BTC, 60_510)
    await rt.watcher._tick(rt.config())
    assert fake.positions[BTC]["pos"] > 0 and calls["n"] == used  # 下单了，但模型一次都没被调用

    prompts: list[str] = []
    rt.watcher.reset_model()
    rt.watcher.model_factory = lambda: scripted_model(prompts)
    await rt.watcher.review(BTC, "测试")
    assert "【正在运行的策略" in prompts[0] and "#1 突破追单（active）" in prompts[0]


async def test_one_worker_does_one_thing_at_a_time_and_can_be_interrupted(settings, fake):
    """对话任务等待已有复盘完成，显式插话仍可补充当前轮。"""
    gate = asyncio.Event()
    order: list[str] = []
    seen: list[str] = []

    async def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        text = "\n".join(str(getattr(p, "content", "")) for m in messages for p in m.parts)
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if info.output_tools:  # 复盘
            if not returns:
                order.append("review:start")
                await gate.wait()  # 模型还在想
                return ModelResponse(parts=[ToolCallPart("account_state", {})])
            seen.append(text)
            order.append("review:end")
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "plan": None, "no_plan_reason": "limit"})])
        order.append("task")
        return ModelResponse(parts=[TextPart("现在不建议追多。")])

    rt = make_rt(settings, fake, FunctionModel(run))
    review = asyncio.create_task(rt.watcher.review(BTC, "定时复盘"))
    while "review:start" not in order:
        await asyncio.sleep(0.01)
    st = rt.watcher._state(BTC)
    assert st.busy == "复盘" and st.run is not None

    task = asyncio.create_task(rt.watcher._chat_worker("现在能追多吗", None, BTC, BTC))
    await asyncio.sleep(0.05)
    status = rt.watcher.status()["instruments"][BTC]
    assert status["queue"] == 1 and status["busy"] == "复盘" and order == ["review:start"]  # 任务在排队，没有并行开第二份上下文
    assert rt.store.work_items("demo")[0]["status"] == "queued"

    said = rt.watcher.interject(BTC, "先别开仓", "用户")
    assert said["delivered"] is True
    gate.set()
    await review
    reply = await task
    assert order == ["review:start", "review:end", "task"] and reply["reply"] == "现在不建议追多。"
    assert "先别开仓" in seen[0] and "现在能追多吗" not in seen[0]
    log = rt.watcher.chat_log(BTC)
    assert any(m["role"] == "user" and m["text"] == "现在能追多吗" for m in log)
    kinds = [s["kind"] for s in rt.store.activity("demo")]
    assert "queued" in kinds and "interject" in kinds
    assert rt.watcher.status()["instruments"][BTC]["queue"] == 0 and rt.watcher.status()["pool"]["workers"]["max"] == 3

    # Worker 空闲时插话：放进信箱，下次复盘先读
    assert rt.watcher.interject(BTC, "今天只做空", "Router")["delivered"] is False
    assert rt.watcher.status()["instruments"][BTC]["inbox"] == 1
    await rt.watcher.review(BTC, "信箱里有新的留言要处理")
    assert "今天只做空" in seen[-1] and rt.watcher.inbox("demo", BTC) == []


async def test_router_turns_a_goal_into_a_mandate_with_a_loss_budget(settings, fake):
    """用户给目标而不是填表单：Router 把目标交给对应的 Worker，Worker 带着目标开工；亏损额度用完，系统自动平仓停手。"""
    prompts: list[str] = []

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = [t.name for t in info.function_tools]
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if "assign_goal" in tools:  # Router
            if not returns:
                return ModelResponse(parts=[ToolCallPart("assign_goal", {"inst_id": "BTC", "goal": "今天做超短线，顺势为主", "max_loss_usdt": 100, "hours": 8})])
            return ModelResponse(parts=[TextPart("已经交给 BTC 的 Worker。")])
        if not returns:  # Worker 复盘
            prompts.append(str(messages[-1].parts[-1].content))
            return ModelResponse(parts=[ToolCallPart("open_position", {"inst_id": BTC, "side": "long", "stop_loss": 59_400, "leverage": 5, "reason": "按目标开工"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "plan": None})])

    rt = make_rt(settings, fake, FunctionModel(run))
    rt.save_config(rt.config().model_copy(update={"intel_minutes": 0}))
    out = await rt.watcher.chat("今天 BTC 做超短线，最多亏 100 U", thread="general")
    assert out["reply"] == "已经交给 BTC 的 Worker。"
    goal = rt.watcher.status()["instruments"][BTC]["goal"]
    assert goal["status"] == "active" and goal["max_loss_usdt"] == 100 and goal["by"] == "Router" and goal["until_ts"]

    await rt.watcher.review(BTC, "信箱里有新的留言要处理")
    assert "交给你的目标" in prompts[0] and "亏损上限 100 USDT" in prompts[0] and "给了你新的目标" in prompts[0]
    assert fake.positions[BTC]["pos"] > 0
    rt.watcher.strategies.deploy(rt.config(), BTC, "pullback", "long", {"trigger": 59_000, "stop_loss": 58_700})

    st = rt.watcher._state(BTC)
    st.last_review_ts, st.next_due_ts = time.time(), time.time() + 3600
    size = fake.positions[BTC]["pos"]
    fake.prices[BTC] = 60_000 - 105 / (size * 0.01)  # 浮亏 105 U，超过额度
    await rt.watcher._tick(rt.config())
    goal = rt.watcher.goal("demo", BTC)
    assert goal["status"] == "stopped" and "达到上限" in goal["note"] and BTC not in fake.positions
    assert rt.store.strategies("demo", BTC) == []  # 没触发的策略也一起撤掉
    await rt.watcher._tick(rt.config())  # 仓位被平掉：叫醒它一次收尾，然后停下
    if st.task:
        await st.task
    st.last_review_ts, st.next_due_ts = time.time() - 3600, 0.0
    await rt.watcher._tick(rt.config())
    assert not st.reviewing and rt.config().worker(BTC).standby and "达到上限" in rt.config().worker(BTC).paused_by  # 条件达到：Worker 停下
    blocked = await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_000, leverage=5), source="agent")
    assert blocked.status == "rejected" and "目标已结束" in blocked.message
    rt.watcher.set_goal(BTC, "再做一轮", who="用户")  # 用户给了新目标：重新开工
    assert not rt.config().worker(BTC).standby and rt.watcher.goal("demo", BTC)["status"] == "active"


async def test_a_proposed_strategy_is_also_a_held_call_and_chat_tasks_resume_too(settings, fake):
    """布置策略同样是一次工具调用：要用户同意时挂起，同意后 Worker 接着做。对话里交给 Worker 的任务也一样，做完把话发回这个币的对话。"""
    seen: list[str] = []

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert info.model_settings["openai_prompt_cache_key"] == f"ouy-{BTC}"
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("deploy_strategy", {"inst_id": BTC, "kind": "breakout", "side": "long", "trigger": 60_500, "stop_loss": 60_200, "reason": "突破就追"})])
        seen.append(str(returns[-1].content))
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "plan": None})])
        return ModelResponse(parts=[TextPart("策略已经在跑了，突破 60500 会自动追多。")])

    rt = make_rt(settings, fake, FunctionModel(run))
    rt.save_config(AgentConfig(mode="confirm", instruments=[BTC], intel_minutes=0))
    await rt.watcher.review(BTC, "定时复盘")
    row = next(r for r in rt.store.strategies("demo", BTC) if r["status"] == "proposed")
    assert row["status"] == "proposed" and rt.watcher.status()["instruments"][BTC]["awaiting"] == 1
    card = rt.watcher.chat_log(BTC)[-1]["actions"][0]  # 对话里也有这张卡片，带着同意按钮要用的编号
    assert card["strategy_id"] == row["id"] and card["pending_status"] == "waiting" and "60500" in card["message"]
    ask = [a for a in rt.store.activity("demo") if a["kind"] == "ask"][-1]
    assert ask["data"]["strategy_id"] == row["id"]
    rt.watcher.strategies.approve(row["id"])
    rt.watcher.on_decided()
    await asyncio.gather(*rt.watcher._resumes)
    assert "用户已同意，策略 #1 已启用" in seen[0] and rt.store.get_strategy(row["id"])["status"] == "armed"

    # 对话里直接对 Worker 说：它调用的开仓要你同意；同意后它接着做，并把回答发到这个币的对话里
    seen.clear()
    rt.watcher.strategies.cancel(row["id"])

    def chat(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert info.model_settings["openai_prompt_cache_key"] == f"ouy-task-{BTC}"
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("open_position", {"inst_id": BTC, "side": "short", "stop_loss": 60_600, "leverage": 5, "reason": "用户要做空"})])
        seen.append(str(returns[-1].content))
        return ModelResponse(parts=[TextPart("空单已成交，止损 60600。")])

    rt.watcher.reset_model()
    rt.watcher.model_factory = lambda: FunctionModel(chat)
    out = await rt.watcher._chat_worker("帮我做空", None, BTC, BTC)
    assert "需要你同意" in out["reply"] and out["actions"][0]["pending_status"] == "waiting" and BTC not in fake.positions
    await rt.trader.approve(out["actions"][0]["pending_id"])
    rt.watcher.on_decided()
    await asyncio.gather(*rt.watcher._resumes)
    assert fake.positions[BTC]["pos"] < 0 and "用户已同意，已执行" in seen[0]
    last = rt.watcher.chat_log(BTC)[-1]
    assert last["proactive"] and last["text"] == "空单已成交，止损 60600。"


async def test_a_position_outside_the_watch_list_gets_a_worker(settings, fake):
    """用户在盯盘列表之外的币上开了仓：这个币自动加入盯盘，由它的 Worker 接手。"""
    rt = make_rt(settings, fake, "test")
    fake.set_price(ETH, 3_000)
    fake.positions[ETH] = {"pos": 2.0, "avgPx": 3_000.0, "cTime": "1", "mgnMode": "cross"}
    st = rt.watcher._state(BTC)
    st.last_review_ts, st.next_due_ts = time.time(), time.time() + 3600
    await rt.watcher._tick(rt.config())
    assert rt.config().instruments == [BTC, ETH] and ETH in rt.watcher.status()["instruments"]
    assert any(a["kind"] == "handoff" and a["inst_id"] == ETH for a in rt.store.activity("demo"))


def test_model_endpoint_and_key_are_set_from_the_page(settings, fake):
    """Base URL / API Key / 默认模型在设置页里填：存在本机，优先于 .env，立即生效；密钥本身不回传。"""
    rt = make_rt(settings, fake, "test")
    before = rt.llm()
    out = rt.save_llm("https://example.test/v1/", "sk-test-1234567890", "my-model")
    assert out["base_url"] == "https://example.test/v1" and out["model"] == "my-model" and out["key_set"] and out["key_hint"] == "…7890"
    assert "sk-test" not in str(out) and rt.settings.ai_api_key == "sk-test-1234567890"
    assert rt.save_llm(None, None, "other")["key_hint"] == "…7890"  # 没传密钥 = 保留原来的
    back = rt.save_llm("", "", "")  # 清空 = 改回 .env 里的
    assert back["base_url"] == before["base_url"] and back["model"] == before["model"]


async def test_profit_giveback_and_growing_loss_wake_the_worker_to_judge(settings, fake):
    """盈利从最高点回吐、或者浮亏扩大：规则只负责第一时间发现并交给 Worker 判断（震荡 / 减仓 / 平仓 / 反手），不替它平仓。
    只有回吐继续扩大而 Worker 一直没结论时，规则才把止损移到保本。"""
    rt = make_rt(settings, fake, "test")
    rt.save_config(rt.config().model_copy(update={"profit_lock": False}))  # 这一段只看回吐叫醒；保盈线另有测试
    await _open(rt)  # 多单 60000，止损 59400 → 1R = 600
    cfg, st = rt.config(), rt.watcher._state(BTC)
    st.last_review_ts = time.time() - 100

    async def at(px: float) -> str:
        fake.set_price(BTC, px)
        st.last_px, st.lot_alert = px, ""
        await rt.watcher._watch_lots(cfg, BTC, st, time.time())
        return st.lot_alert

    assert await at(60_900) == ""  # +1.5R，新高
    assert await at(60_700) == ""  # 回吐 22%，还在正常范围
    alert = await at(60_450)  # +0.75R，回吐 50% ≥ 日内的 40%
    assert "利润回吐" in alert and "+1.50R" in alert and "反手" in alert and BTC in fake.positions  # 只叫醒，不替它平
    assert await at(60_440) == ""  # 没有再明显扩大：不重复叫醒
    stops = lambda: [a["slTriggerPx"] for a in fake.algos]
    assert "60000" not in stops()
    assert await at(60_250) != "" and "60000" in stops()  # 回吐 72%，Worker 还没结论：止损先移到保本，并再叫醒一次

    # 另一笔仓：浮亏扩大也交给 Worker 判断
    rt = make_rt(settings, fake, "test")
    fake.positions.clear(); fake.algos.clear(); fake.set_price(BTC, 60_000)
    rt.save_config(rt.config().model_copy(update={"profit_lock": False}))
    await _open(rt)
    cfg, st = rt.config(), rt.watcher._state(BTC)
    alert = await at(59_600)  # 掉到 -0.67R
    assert "浮亏扩大" in alert and "方向做错" in alert
    assert await at(59_590) == ""  # 同一段浮亏只叫醒一次

    # 送到 Worker 手上：它空闲就成为这次唤醒的原因，而且不经过 Router 的事件过滤
    reasons: list[tuple[str, bool]] = []

    async def fake_review(inst_id: str, reason: str, gate: bool = False) -> None:
        reasons.append((reason, gate))

    rt.watcher._review_task = fake_review  # type: ignore[method-assign]
    rt.store.delete("lot_track:demo")
    st.had_position = True
    fake.set_price(BTC, 60_900)
    st.last_review_ts, st.next_due_ts = time.time(), time.time() + 3600
    await rt.watcher._tick(rt.config())
    fake.set_price(BTC, 60_450)
    await rt.watcher._tick(rt.config())
    await asyncio.sleep(0)
    assert reasons and "利润回吐" in reasons[0][0] and reasons[0][1] is False


async def test_preferred_leverage_is_used_instead_of_a_low_default(settings, fake):
    """用户的常用杠杆：Agent 开新仓时没填杠杆、或者填得比偏好低，就按偏好来；止损距离放不下时用放得下的最高值；手动下单不受影响。"""
    rt = make_rt(settings, fake, "test")
    risk = rt.config().risk.model_copy(update={"max_leverage": 50})
    rt.save_config(rt.config().model_copy(update={"risk": risk, "pref_leverage_min": 20, "pref_leverage_max": 40}))
    assert rt.config().leverage_pref() == (20, 40) and "常用杠杆 20~40x" in rt.config().prefs_text()

    low = TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_400, leverage=5)  # 止损 1%
    assert (await rt.trader._apply_prefs(low, "demo")).leverage == 20  # 低于偏好：提到下限
    assert (await rt.trader._apply_prefs(low.model_copy(update={"leverage": None}), "demo")).leverage == 30  # 没填：取中间
    wide = TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=57_000)  # 止损 5%：最多放得下 14x
    assert (await rt.trader._apply_prefs(wide, "demo")).leverage == 14

    result = await rt.trader.submit(low, source="agent")
    assert result.status == "executed" and result.detail["leverage"] == 20
    rt.save_config(rt.config().model_copy(update={"prefs": ["不追高"]}))
    assert "不追高" in rt.config().prefs_text()


async def test_a_close_that_did_not_fill_is_not_reported_as_closed(settings, fake):
    """平仓单提交了但没成交（模拟盘的小币没有对手盘）：不能说「已平仓」，仓位记录保持不变；改挂贴着对手价的限价平仓单；
    刚提交的平仓单还挂着时再点平仓，直接告诉用户不用重复点。成交了才算平掉。"""
    rt = make_rt(settings, fake, "test")
    rt.trader.settle_seconds = 0
    await _open(rt)
    fake.stuck.add(BTC)
    result = await rt.trader.submit(TradeAction(kind="close", inst_id=BTC, reason="一键全平"), source="user")
    assert result.detail["unfilled"] is True and "还没成交" in result.message and "已市价全平" not in result.message
    assert BTC in fake.positions and [l["status"] for l in rt.trader.lots("demo", BTC)] == ["open"]  # 仓位和记录都还在
    closing = [o for o in fake.orders if o.get("reduceOnly") == "true"]
    assert len(closing) == 1 and closing[0]["ordType"] == "limit" and float(closing[0]["px"]) < 60_000  # 平多单：贴着买盘卖

    closing[0]["cTime"] = str(int(time.time() * 1000))
    again = await rt.trader.submit(TradeAction(kind="close", inst_id=BTC, reason="再点一次"), source="user")
    assert again.status == "rejected" and "不用重复点" in again.message and len([o for o in fake.orders if o.get("reduceOnly") == "true"]) == 1

    fake.stuck.clear(); fake.orders.clear()
    done = await rt.trader.submit(TradeAction(kind="close", inst_id=BTC, reason="有对手盘了"), source="user")
    assert done.status == "executed" and "已市价全平" in done.message and BTC not in fake.positions
    assert [l["status"] for l in rt.trader.lots("demo", BTC)] == []


async def test_rate_limited_calls_are_retried_instead_of_failing(settings, fake):
    """欧易限流（Too Many Requests）：等一下再试，不直接报失败。"""
    rt = make_rt(settings, fake, "test")
    rt.gateway.delays = (0, 0, 0)
    await _open(rt)
    fake.limited = 2
    result = await rt.trader.submit(TradeAction(kind="close", inst_id=BTC, reason="平仓"), source="user")
    assert result.status == "executed" and BTC not in fake.positions
    fake.limited = 99  # 一直被限流：重试几次后如实报错
    assert (await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_400), source="user")).status == "failed"


async def test_watchdog_patrols_the_account_and_the_team(settings, fake):
    """Watchdog 每分钟巡检：没有止损的持仓、挂着不成交的平仓单、连续失败的 Worker 都会被发现并记下；没止损的仓直接交给 Worker 去补；问题消失也会说。"""
    rt = make_rt(settings, fake, "test")
    await _open(rt)
    cfg = rt.config()
    await rt.watcher._patrol(cfg)
    assert rt.watcher.status()["watchdog"]["checks"] == []  # 一切正常

    fake.algos.clear()  # 止损单没了
    rt.watcher._state(BTC).fails = 3
    await rt.watcher._patrol(cfg)
    keys = {c["key"] for c in rt.watcher.status()["watchdog"]["checks"]}
    assert keys == {f"naked:{BTC}", f"fails:{BTC}"}
    assert "没有止损单" in rt.watcher._state(BTC).lot_alert  # 交给 Worker 去补止损
    acts = [a for a in rt.store.activity("demo") if a["agent"] == "watchdog"]
    assert len(acts) == 2 and any(e["kind"] == "alert" and "Watchdog" in e["title"] for e in rt.store.journal("demo"))
    await rt.watcher._patrol(cfg)
    assert len([a for a in rt.store.activity("demo") if a["agent"] == "watchdog"]) == 2  # 同一个问题不重复报

    await rt.trader.submit(TradeAction(kind="set_stops", inst_id=BTC, stop_loss=59_400), source="user")
    rt.watcher._state(BTC).fails = 0
    await rt.watcher._patrol(cfg)
    assert rt.watcher.status()["watchdog"]["checks"] == []
    assert [a["kind"] for a in rt.store.activity("demo") if a["agent"] == "watchdog"].count("done") == 2  # 恢复了也说一声


async def test_proxy_setting_is_saved_and_empty_means_direct(settings, fake):
    """设置页的网络代理：填了就用，留空就是不用代理；页面上存的优先于 .env，重启后还在。"""
    rt = make_rt(settings, fake, "test")
    assert rt.proxy() == {"url": "", "source": "env"}
    assert (await rt.save_proxy("http://127.0.0.1:7890"))["url"] == "http://127.0.0.1:7890"
    assert rt.settings.proxy_url == "http://127.0.0.1:7890"
    assert (await rt.save_proxy("")) == {"url": "", "source": "page"}
    assert rt.settings.proxy_url == ""


async def test_worker_spawns_its_own_subagents_on_the_fly(settings, fake):
    """常见 Agent 的做法：Worker 干活时现场开子代理，名字和任务都由它自己写；子代理只有只读工具，结果交回给 Worker；可以并行开几个；有上限。"""
    tasks: list[str] = []

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = [t.name for t in info.function_tools]
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if tools == ["intel_query"]:  # 临时子代理：只有只读查询工具，不能下单
            tasks.append(str(messages[-1].parts[-1].content))
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"bias": "neutral", "summary": "关键位 60000 守住了。", "points": ["15m 三次回踩不破"]})])
        if not returns:
            assert "spawn_subagent" in tools
            return ModelResponse(parts=[
                ToolCallPart("spawn_subagent", {"inst_id": ETH, "name": "关键位核对", "task": "核对 60000 这个位置近 4 小时有没有守住"}),
                ToolCallPart("ask_subagent", {"inst_id": ETH, "role": "intel", "task": "查现在的资金费率和持仓量，说明拥挤不拥挤"}),
            ])
        assert len(returns) == 2 and all("60000" in str(r.content) and "子代理没查成" not in str(r.content) for r in returns)  # 结果交回到了 Worker 手上
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, REPORT)])

    rt = make_rt(settings, fake, FunctionModel(run))
    await rt.watcher.review(BTC, "定时复盘")
    assert len(tasks) == 2 and any("核对 60000" in t for t in tasks) and all(ETH in t for t in tasks)
    work = rt.store.work_items("demo", inst_id=BTC)
    children = [w for w in work if w["source"] in ("adhoc", "intel")]
    assert len(children) == 2 and all(w["status"] == "done" for w in children)
    assert not rt.store.work_items("demo", inst_id=ETH)
    parent = next(w for w in work if w["source"] == "manual")
    acts = [a for a in rt.store.activity("demo") if a["agent"] in ("adhoc", "intel") and a["kind"] == "done"]
    assert len(acts) == 2 and all(a["data"]["parent_work_id"] == parent["id"] and a["data"]["subject_inst_id"] == ETH for a in acts)
    assert rt.watcher.status()["instruments"][BTC]["adhoc"] == []  # 一次性的：交回结果就从图上消失
    for i in range(5):  # 10 分钟内最多 6 次
        await rt.watcher._spawn(BTC, f"第{i}个", "随便查查")
    with pytest.raises(RuntimeError, match="10 分钟内"):
        await rt.watcher._spawn(BTC, "多出来的", "随便查查")


async def test_worker_keeps_a_subagent_asks_it_again_and_closes_it_itself(settings, fake):
    """Worker 自己决定子代理留不留：keep=True 的留在图上、记得之前查过什么，用同一个名字再交任务就是接着问；不需要了 Worker 自己关，关了才从图上消失。"""
    histories: list[int] = []

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        histories.append(len(messages))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"bias": "neutral", "summary": f"第 {len(histories)} 次查完。", "points": []})])

    rt = make_rt(settings, fake, FunctionModel(run))
    first = await rt.watcher._spawn(BTC, "消息面", "盯着 ETF 的消息", keep=True)
    assert "留着了" in first["kept"]
    rows = rt.watcher.status()["instruments"][BTC]["adhoc"]
    assert [(r["name"], r["keep"], r["running"]) for r in rows] == [("消息面", True, False)] and rows[0]["summary"] == "第 1 次查完。"
    await rt.watcher._spawn(BTC, "消息面", "有新进展吗")  # 同一个名字：接着问，不是另开一个
    assert histories[1] > histories[0]  # 第二次带着第一次的对话
    assert len(rt.watcher.status()["instruments"][BTC]["adhoc"]) == 1
    assert "你现在留着的子代理：「消息面」" in rt.watcher._prompt(rt.config(), BTC, "定时复盘", {}, await rt.trader.account("demo"), None, [])
    assert "没有叫" in rt.watcher._close_sub(BTC, "不存在")
    assert rt.watcher._close_sub(BTC, "消息面") == "已关掉子代理「消息面」。"
    assert rt.watcher.status()["instruments"][BTC]["adhoc"] == []
    for i in range(3):
        await rt.watcher._spawn("ETH-USDT-SWAP", f"留{i}", "查", keep=True)
    with pytest.raises(RuntimeError, match="最多 3 个"):
        await rt.watcher._spawn("ETH-USDT-SWAP", "留3", "查", keep=True)
    rt.watcher._state("ETH-USDT-SWAP").adhoc[0]["ts"] -= 31 * 60_000  # 空闲超过 30 分钟：自动关
    assert [r["name"] for r in rt.watcher.status()["instruments"].get("ETH-USDT-SWAP", {}).get("adhoc", rt.watcher._state("ETH-USDT-SWAP").adhoc)] == ["留1", "留2"]


async def test_router_hands_general_questions_to_the_general_worker(settings, fake):
    """用户问 Router 普通问题（看好什么币、有什么消息）：Router 自己不研究，交给通用 Worker；通用 Worker 去查、可以开子代理，结论交回 Router。
    不惊动任何盯盘的币种 Worker。"""
    seen: dict[str, list[str]] = {"general": [], "helper": []}

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = [t.name for t in info.function_tools]
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if tools == ["intel_query"]:  # 通用 Worker 开的临时子代理
            seen["helper"].append(str(messages[-1].parts[-1].content))
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"bias": "neutral", "summary": "涨幅榜前三是 SOL、NEAR、ETH。", "points": ["SOL 24 小时 +6%"]})])
        if "spawn_helper" in tools:  # 通用 Worker：只读工具 + 开子代理，没有交易工具
            assert "open_position" not in tools and "ask_worker" not in tools
            if not returns:
                seen["general"].append(str(messages[-1].parts[-1].content))
                return ModelResponse(parts=[ToolCallPart("spawn_helper", {"name": "热点扫描", "task": "看 24 小时涨幅榜，说出现在最强的三个币"})])
            return ModelResponse(parts=[TextPart("现在最强的是 SOL、NEAR、ETH。")])
        if not returns:  # Router
            assert "ask_general" in tools and "ask_worker" in tools and "research" not in tools
            return ModelResponse(parts=[ToolCallPart("ask_general", {"task": "用户问：现在看好什么币？给出最强的三个和依据"})])
        assert "SOL" in str(returns[-1].content)
        return ModelResponse(parts=[TextPart("通用 Worker 看下来，现在最强的是 SOL、NEAR、ETH。")])

    rt = make_rt(settings, fake, FunctionModel(run))
    out = await rt.watcher.chat("现在看好什么币？")
    assert "SOL" in out["reply"]
    assert len(seen["general"]) == 1 and "现在看好什么币" in seen["general"][0] and len(seen["helper"]) == 1
    status = rt.watcher.status()
    assert not status["general"]["busy"] and "SOL" in status["general"]["summary"]
    assert status["router_adhoc"] == []  # 一次性的子代理：查完就不在图上了
    assert [w["status"] for w in rt.store.work_items("demo") if w["source"] == "adhoc"] == ["done"]
    kinds = [(a["agent"], a["kind"]) for a in rt.store.activity("demo")]
    assert ("general", "wake") in kinds and ("general", "done") in kinds and ("worker", "wake") not in kinds  # 没有惊动任何币种 Worker
    assert all(not st.adhoc for st in rt.watcher.states.values())


async def test_router_dispatches_at_low_effort_by_default_and_the_worker_gets_the_chosen_one(settings, fake):
    """Router 是分发台：思考强度默认低（用户可以在设置里改），对话框里选的强度不给它、交给接手的通用 Worker；
    带进上下文的旧对话有上限（ROUTER_KEEP）。"""
    from ouy.agent.watcher import ROUTER_KEEP

    efforts: dict[str, list[object]] = {"router": [], "general": []}
    carried: list[int] = []

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = [t.name for t in info.function_tools]
        who = "general" if "spawn_helper" in tools else "router"
        if who == "general":
            return ModelResponse(parts=[TextPart("查过了。")])
        if not any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
            carried.append(len(messages))
            return ModelResponse(parts=[ToolCallPart("ask_general", {"task": "用户问：帮我比较一下 BTC 和 ETH"})])
        return ModelResponse(parts=[TextPart("通用 Worker 查过了。")])

    rt = make_rt(settings, fake, FunctionModel(run))
    assert rt.config().router_effort == "low"  # 默认低
    rt.save_config(rt.config().model_copy(update={"reasoning_effort": "medium"}))
    run_model = rt.watcher._run
    async def spy(agent, prompt, st, timeout, **kw):
        efforts["router" if agent.name == "ouy-router" else "general"].append((kw.get("model_settings") or {}).get("thinking"))
        return await run_model(agent, prompt, st, timeout, **kw)
    rt.watcher._run = spy
    for _ in range(14):
        await rt.watcher.chat("帮我比较一下 BTC 和 ETH", effort="high")
    assert set(efforts["router"]) == {"low"}
    assert set(efforts["general"]) == {"high"}
    assert 30 < max(carried) <= ROUTER_KEEP + 1  # 每问一次进 4 条消息：14 次是 56 条，带进去的是最近 40 条以内再加这一句
    efforts["router"].clear()
    rt.save_config(rt.config().model_copy(update={"router_effort": "high"}))  # 用户自己调高：照用户的
    await rt.watcher.chat("帮我比较一下 BTC 和 ETH", effort="high")
    assert set(efforts["router"]) == {"high"}


async def test_at_mention_makes_router_forward_to_exactly_that_worker(settings, fake):
    """用户 @ 了某个币：Router 只能把话转给被 @ 的 Worker，转给别的币会被打回；没 @ 但点名了币，以点名的为准而不是正在看的品种。"""
    from ouy.agent.watcher import coins_in

    watch = [BTC, "ETH-USDT-SWAP", "NEAR-USDT-SWAP"]
    assert coins_in("@eth 帮我做空", watch) == (["ETH-USDT-SWAP"], ["ETH-USDT-SWAP"])
    assert coins_in("帮我做空eth", watch) == ([], ["ETH-USDT-SWAP"])
    assert coins_in("现在看好什么币", watch) == ([], [])
    prompts: list[str] = []
    wrong: list[str] = []

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = [t.name for t in info.function_tools]
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if "ask_worker" not in tools:  # Worker 那一侧
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, REPORT)])
        if not returns:
            prompts.append(str(messages[-1].parts[-1].content))
            return ModelResponse(parts=[ToolCallPart("ask_worker", {"inst_id": "NEAR-USDT-SWAP", "task": "做空"})])  # 故意转错
        if len(returns) == 1:
            wrong.append(str(returns[0].content))
            return ModelResponse(parts=[ToolCallPart("ask_worker", {"inst_id": BTC, "task": "做空"})])
        return ModelResponse(parts=[TextPart("已交给 BTC Worker。")])

    rt = make_rt(settings, fake, FunctionModel(run))
    rt.save_config(rt.config().model_copy(update={"instruments": watch}))
    await rt.watcher.chat("@BTC 帮我做空", inst_id="NEAR-USDT-SWAP")
    assert "【用户 @ 了】BTC-USDT-SWAP" in prompts[0] and "正在看的品种" not in prompts[0]
    assert wrong and wrong[0].startswith("[转错了]") and "NEAR-USDT-SWAP" in wrong[0]
    await rt.watcher.chat("帮我做空 btc", inst_id="NEAR-USDT-SWAP", thread="t2")
    assert "【用户话里点名的币】BTC-USDT-SWAP" in prompts[1] and "正在看的品种" not in prompts[1]


async def test_agent_output_is_streamed_to_the_page_while_it_is_being_written(settings, fake):
    """流式输出：Agent 边写边推给界面，不等整轮跑完；结构化结论（JSON）里的 summary 也能边写边取出来。"""
    from ouy.agent.watcher import _partial_summary

    assert _partial_summary('{"bias": "long", "summary": "上升趋势，回踩') == "上升趋势，回踩"
    assert _partial_summary('{"summary": "第一句。\\n第二') == "第一句。\n第二"
    assert _partial_summary('{"bias": "lo') == ""

    async def stream(messages: list[ModelMessage], info: AgentInfo):
        for piece in ("现在最强的", "是 SOL，", "其次是 NEAR。"):
            yield piece

    def whole(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("现在最强的是 SOL，其次是 NEAR。")])

    rt = make_rt(settings, fake, FunctionModel(whole, stream_function=stream))
    pushed: list[dict] = []
    rt.bus.publish = (lambda orig: lambda kind, data=None: (pushed.append(data) if kind == "stream" else None, orig(kind, data))[1])(rt.bus.publish)
    out = await rt.watcher.chat("现在看好什么币？")
    assert out["reply"] == "现在最强的是 SOL，其次是 NEAR。"
    assert pushed and pushed[-1]["done"] and pushed[-1]["agent"] == "router" and any(row["text"] == out["reply"] for row in pushed)
    assert any(not row["done"] and row["text"] for row in pushed)  # 结束之前就已经推过内容
    rt.save_config(rt.config().model_copy(update={"stream": False}))
    pushed.clear()
    await rt.watcher.chat("再说一遍", thread="t2")
    assert not pushed  # 关掉流式就不推


async def test_router_can_close_and_reopen_a_worker_and_warns_about_its_position(settings, fake):
    """用户让 Router 关掉某个币的 Worker：Router 自己能关、能重新开；Worker 手上还有仓位时要提醒这笔仓没人盯了。"""
    notes: list[str] = []
    step = {"n": 0}

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = [t.name for t in info.function_tools]
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if "set_worker" not in tools:  # 币种 Worker：先开一笔仓
            if not returns:
                return ModelResponse(parts=[ToolCallPart("open_position", {"inst_id": BTC, "side": "long", "stop_loss": 59_400, "take_profit": 62_000, "leverage": 5, "reason": "测试"})])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, REPORT)])
        if messages[-1].parts and isinstance(messages[-1].parts[-1], ToolReturnPart):
            notes.append(str(messages[-1].parts[-1].content))
            return ModelResponse(parts=[TextPart("好了。")])
        step["n"] += 1
        return ModelResponse(parts=[ToolCallPart("set_worker", {"inst_id": "BTC", "paused": step["n"] == 1})])

    rt = make_rt(settings, fake, FunctionModel(run))
    await rt.watcher.review(BTC, "测试")
    assert BTC in fake.positions
    await rt.watcher.chat("把 BTC 的 Worker 关掉")
    assert rt.config().worker(BTC).paused and "还有多仓" in notes[0] and "不再有人盯" in notes[0]
    assert rt.watcher.status()["instruments"][BTC]["paused"]
    await rt.watcher.chat("重新打开 BTC 的 Worker", thread="t2")
    assert not rt.config().worker(BTC).paused and "重新开工" in notes[1]


async def test_worker_writes_uses_rewrites_and_deletes_its_own_strategies(settings, fake):
    """策略库：Worker 把做法写成一条有名字的策略存起来，之后布置时点名用它（默认参数自动补上），做完的单记进它的战绩；
    模拟盘和实盘的战绩分开；表现差的自动停用，改写后重新启用；不好用的可以删。"""
    from ouy.playbook import Playbooks

    calls: list[str] = []

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = [t.name for t in info.function_tools]
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        assert {"write_strategy", "delete_strategy", "deploy_strategy"} <= set(tools)
        if len(returns) == 0:
            return ModelResponse(parts=[ToolCallPart("write_strategy", {
                "inst_id": BTC, "name": "缩量回踩多", "base": "pullback", "when": "15m 上升趋势，回踩缩量", "entry": "触发价放在 20 均线上方 0.1%",
                "exit": "止损放前低下方，到 1R 移保本", "breakeven_r": 1, "expires_minutes": 30,
            })])
        if len(returns) == 1:
            calls.append(str(returns[0].content))
            return ModelResponse(parts=[ToolCallPart("deploy_strategy", {
                "inst_id": BTC, "kind": "pullback", "playbook": "缩量回踩多", "side": "long", "trigger": 59_800, "stop_loss": 59_400, "reason": "按策略库",
            })])
        if len(returns) == 2:
            calls.append(str(returns[1].content))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, REPORT)])

    rt = make_rt(settings, fake, FunctionModel(run))
    await rt.watcher.review(BTC, "定时复盘")
    books: Playbooks = rt.watcher.strategies.playbooks
    assert "已存进策略库" in calls[0] and "已布置" in calls[1]
    book = books.get("缩量回踩多")
    assert book and book["author"] == "BTC Worker" and book["version"] == 1 and book["defaults"] == {"breakeven_r": 1, "expires_minutes": 30}
    row = next(r for r in rt.store.strategies("demo", BTC) if r["params"].get("playbook") == "缩量回踩多")
    assert row["params"]["playbook"] == "缩量回踩多" and row["params"]["manage"]["breakeven_r"] == 1 and row["params"]["expires_minutes"] == 30
    assert any(r["label"].startswith("缩量回踩多 · ") for r in rt.watcher.status()["instruments"][BTC]["strategies"])
    assert "「缩量回踩多」" in rt.watcher._prompt(rt.config(), BTC, "定时复盘", {}, await rt.trader.account("demo"), None, [])
    # 战绩：模拟盘和实盘分开；连亏到一定程度自动停用
    for pnl in (-3, -2, 1.5, -4, -1):
        books.record("缩量回踩多", "demo", pnl)
    book = books.get("缩量回踩多")
    assert book["stats"]["demo"] == {"n": 5, "wins": 1, "pnl": -8.5, "last_ts": book["stats"]["demo"]["last_ts"]} and "live" not in book["stats"]
    assert not book["enabled"] and "自动停用" in book["off_reason"] and "【已停用" in books.brief("demo")
    assert books.score(book, "live") == "还没用过"
    again, created = books.write("缩量回踩多", "pullback", "只在 1H 也向上时用", "同上", "止损收紧", {"breakeven_r": 0.8}, author="ETH Worker")
    assert not created and again["version"] == 2 and again["enabled"] and again["author"] == "BTC Worker" and again["stats"]["demo"]["n"] == 5
    with pytest.raises(ValueError, match="具体价位"):
        books.write("带价位的", "pullback", "x", "x", "x", {"trigger": 60000})
    assert books.delete("缩量回踩多") and books.all() == [] and not books.delete("缩量回踩多")


async def test_playbook_results_are_scored_once_and_the_user_can_manage_the_library(client):
    """用策略库里的策略做完的一笔，已实现盈亏记进那条策略的战绩（只记一次）；用户也能在界面上加、停用、删策略。"""
    from ouy.models import ClosedTrade

    rt = client.rt
    r = await client.put("/api/playbooks", json={"name": "区间低吸", "base": "pullback", "when": "震荡区间下沿", "entry": "下沿上方 0.2%", "exit": "区间外止损", "defaults": {"breakeven_r": 1}})
    assert r.status_code == 200 and r.json()[0]["author"] == "用户" and r.json()[0]["base_label"] == "回踩进场" and r.json()[0]["score"] == "还没用过"
    assert (await client.put("/api/playbooks", json={"name": "坏的", "base": "pullback", "when": "x", "entry": "x", "exit": "x", "defaults": {"trigger": 1}})).status_code == 400
    row = rt.store.add_strategy("demo", BTC, "pullback", "long", {"trigger": 59_800, "stop_loss": 59_400, "playbook": "区间低吸"}, "done", "已进场")
    since = time.time() - 600
    rt.store.update_strategy(row["id"], state={"since": since})
    trade = ClosedTrade(inst_id=BTC, side="long", open_px=59_800, close_px=60_400, pnl=12.5, pnl_pct=5, leverage=5, opened_ts=int(since * 1000) + 2_000, closed_ts=int(since * 1000) + 300_000, how="全部平仓")

    async def history(inst_id=None, limit=20, env=None):
        return [trade]

    rt.trader.history = history
    await rt.watcher.strategies.score_playbooks("demo")
    await rt.watcher.strategies.score_playbooks("demo")  # 第二次不会重复记
    book = (await client.get("/api/playbooks")).json()[0]
    assert book["stats"]["demo"]["n"] == 1 and book["stats"]["demo"]["pnl"] == 12.5 and book["score"].startswith("1 笔，赢 1 笔")
    assert rt.store.get_strategy(row["id"])["state"]["scored"] is True
    off = (await client.post("/api/playbooks/区间低吸/enable", params={"on": "false"})).json()[0]
    assert off["enabled"] is False and off["off_reason"] == "手动停用"
    assert (await client.delete("/api/playbooks/区间低吸")).json() == []
    assert (await client.delete("/api/playbooks/区间低吸")).status_code == 404


def _candles(closes: list[float], step_ms: int = 900_000, start: int = 1_700_000_000_000, spread: float = 5.0):
    from ouy.indicators import Candle

    out, prev = [], closes[0]
    for i, close in enumerate(closes):
        out.append(Candle(start - start % 3_600_000 + i * step_ms, prev, max(prev, close) + spread, min(prev, close) - spread, close, 100.0, True))
        prev = close
    return out


def test_worker_gets_raw_candles_range_levels_patterns_and_previous_high_low():
    """Worker 看得到之前的走势：原始 K 线、区间上下沿、支撑阻力、K 线形态；以及前高前低法——上一段高低点和现在是接受突破还是假突破。"""
    from ouy.indicators import Candle, read_chart, summarize, window_levels

    wave = [100 + 10 * ((i % 20) / 10 if i % 20 < 10 else 2 - (i % 20) / 10) for i in range(120)]  # 在 100 ~ 110 之间来回
    frame = _candles(wave)
    chart = read_chart(frame)
    assert len(chart["candles"]) == 30 and len(chart["candles"][-1]) == 6
    assert chart["range"]["upper"] > 109 and chart["range"]["lower"] < 101 and chart["range"]["upper_touches"] >= 2
    assert chart["support"] and chart["resistance"] and chart["support"][0]["price"] < frame[-1].close < chart["resistance"][0]["price"]
    assert {"candles", "range", "support", "resistance", "patterns"} <= set(summarize(frame))
    # 看涨吞没：前一根阴线，最新一根阳线把它整个包住
    last = frame[-1]
    frame2 = [*frame[:-2], Candle(frame[-2].ts, 105, 105.5, 102.8, 103, 100, True), Candle(last.ts, 102.9, 106.5, 102.7, 106, 300, True)]
    assert any("看涨吞没" in p for p in read_chart(frame2)["patterns"])
    # 前高前低法：上一小时在 100~104，这一小时连续两根 15 分钟收在 104 之上 = 接受突破；刺穿后收回 = 假突破
    hour = lambda lo, hi: [lo + 1, hi - 1, lo + 2, hi - 2]  # noqa: E731
    base = [c for _ in range(6) for c in hour(96, 100)] + hour(100, 104)
    accept = window_levels({"15m": _candles(base + [106, 107], spread=0.5)})["1H"]
    assert accept["trend"].startswith("上涨") and "接受向上突破" in accept["now"] and "挂买单" in accept["plan"]
    swept = _candles(base + [103], spread=0.5)
    swept[-1] = Candle(swept[-1].ts, 102, 105.5, 101.5, 103, 100, True)
    fade = window_levels({"15m": swept})["1H"]
    assert "假突破" in fade["now"] and "挂卖单" in fade["plan"]
    assert window_levels({"15m": _candles(base + [102, 102.5], spread=0.2)})["1H"]["now"].startswith("这一段还没走出方向")


async def test_worker_controls_fast_triggers_and_no_autonomous_exit(settings, fake):
    """Worker 有独立权限和思考强度，但不能因为没行情就停掉自己或全局盯盘。"""
    efforts: list[str] = []
    rt = make_rt(settings, fake, scripted_model([]))
    ETH = "ETH-USDT-SWAP"
    cfg = rt.config().model_copy(update={"mode": "auto", "instruments": [BTC, ETH], "reasoning_effort": "high", "trigger_effort": "low"})
    rt.save_config(AgentConfig.model_validate({**cfg.model_dump(), "workers": {ETH: {"mode": "confirm", "effort": "medium"}}}))
    cfg = rt.config()
    assert cfg.permission_for("open", BTC) == "auto" and cfg.permission_for("open", ETH) == "confirm" and cfg.permission_for("open") == "auto"
    assert cfg.effort_for(ETH) == "medium" and cfg.trigger_effort_for(ETH) == "low" and cfg.effort_for(BTC) == "high"
    status = rt.watcher.status()["instruments"]
    assert status[ETH]["mode"] == "confirm" and status[BTC]["mode"] == "auto" and status[ETH]["trigger_effort"] == "low"

    run = rt.watcher._run
    async def spy(agent, prompt, st, timeout, **kw):
        efforts.append(str((kw.get("model_settings") or {}).get("thinking")) + ("|fast" if "要快" in (prompt or "") else ""))
        return await run(agent, prompt, st, timeout, **kw)
    rt.watcher._run = spy
    await rt.watcher.review(BTC, "价格触及盯盘价位 60600：前高", source="event")
    await rt.watcher.review(BTC, "定时复盘", source="event")
    assert efforts == ["low|fast", "high"]

    from ouy.agent.core import toolset
    assert "stop_watching" in toolset.tools and not {"stand_down", "set_watching", "set_worker", "edit_watchlist"}.intersection(toolset.tools)
    rt.watcher.running = True
    rt.store.set("agent_running", True)
    for inst in (BTC, ETH):
        rt.watcher._set_worker(inst, paused=True, paused_by="你手动暂停")
    fake.positions.clear()
    rt.watcher._resume_due(rt.config())
    assert rt.watcher.running and rt.store.get("agent_running") and all(rt.config().worker(i).paused for i in (BTC, ETH))
    rt.watcher._set_worker(ETH, resume_ts=1)
    rt.watcher._resume_due(rt.config())
    assert not rt.config().worker(ETH).paused and rt.config().worker(BTC).paused


async def test_worker_stops_only_when_an_assigned_stop_condition_is_met(settings, fake):
    """Worker 不能自己停：没人交代停止条件时 stop_watching 被拒绝；交代过的条件达到才停。停下后不再定时复盘，
    但它写的触发线、留言照常叫醒它，到价策略照常执行；出现持仓就恢复正常盯盘。"""
    answers: list[str] = []

    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("stop_watching", {"inst_id": BTC, "evidence": "1H 收在 59000 下方"})])
        answers.append(str(returns[-1].content))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "plan": None, "no_plan_reason": "limit"})])

    rt = make_rt(settings, fake, FunctionModel(model))
    rt.save_config(rt.config().model_copy(update={"intel_minutes": 0, "wake": "always"}))
    w, st = rt.watcher, rt.watcher._state(BTC)
    await w.review(BTC, "没行情")
    assert "不能停" in answers[0] and not rt.config().worker(BTC).standby  # 没人交代过停止条件
    w.set_goal(BTC, "只做多", who="Router", stop_when="1H 收盘跌破 59000 就不做了")
    await w.review(BTC, "测试")
    assert "已停下" in answers[1] and rt.config().worker(BTC).standby and "59000" in rt.config().worker(BTC).paused_by
    assert w.status()["instruments"][BTC]["standby"]
    rt.store.delete(f"inbox:demo:{BTC}")

    async def tick(px):
        fake.prices[BTC] = px
        st.last_review_ts, st.next_due_ts = time.time() - 3600, 0.0  # 早就过了定时复盘的时间
        await w._tick(rt.config())
        woke = st.reviewing
        if st.task:
            await st.task
        return woke

    assert not await tick(60_010) and not await tick(60_500)  # 定时、波动都不叫醒
    assert await tick(60_700)  # 穿过它写的触发线 60600：叫醒
    assert not await tick(60_710)
    w.interject(BTC, "看一下现在", "用户")
    assert await tick(60_720)  # 留言：叫醒
    rt.watcher.strategies.deploy(rt.config(), BTC, "breakout", "long", {"trigger": 61_000, "stop_loss": 60_600, "take_profit": 62_000})
    await tick(61_050)
    assert BTC in fake.positions  # 到价策略照常执行
    assert await tick(61_060)  # 有持仓了：恢复正常盯盘（定时复盘）


async def test_profit_target_is_checked_by_code_and_can_be_shared_across_coins(settings, fake):
    """「净赚 5 U 就停」由代码核对：几个币合起来算；达到就撤策略、平仓、不能再开仓，Worker 停下；没达到一直盯。"""
    rt = make_rt(settings, fake, scripted_model([]))
    rt.save_config(rt.config().model_copy(update={"instruments": [BTC, ETH], "intel_minutes": 0}))
    w = rt.watcher
    for inst, other in ((BTC, ETH), (ETH, BTC)):
        w.set_goal(inst, "超短线", who="Router", target_usdt=50, shared_with=[other])
    await rt.trader.submit(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_400, leverage=5), source="agent")
    size = fake.positions[BTC]["pos"]
    for inst in (BTC, ETH):
        w._state(inst).last_review_ts, w._state(inst).next_due_ts = time.time(), time.time() + 3600
    fake.prices[BTC] = 60_000 + 30 / (size * 0.01)  # 浮盈 30 U：没到
    await w._tick(rt.config())
    assert w.goal("demo", BTC)["status"] == "active" and w.goal("demo", BTC)["total"] == pytest.approx(30, abs=0.5) and BTC in fake.positions
    fake.prices[BTC] = 60_000 + 55 / (size * 0.01)  # 浮盈 55 U：到了
    await w._tick(rt.config())
    goal = w.goal("demo", BTC)
    assert goal["status"] == "done" and "达到目标" in goal["note"] and BTC not in fake.positions
    await w._tick(rt.config())
    assert w.goal("demo", ETH)["status"] == "done"  # 一起算的另一个币也停
    await w._tick(rt.config())
    assert all(rt.config().worker(i).standby for i in (BTC, ETH))
    assert (await rt.trader.submit(TradeAction(kind="open", inst_id=ETH, side="long", stop_loss=2900, leverage=5), source="agent")).status == "rejected"


@pytest.mark.parametrize("message,tool,args", [
    ("BTC 没行情，继续等", "set_worker", {"inst_id": BTC, "paused": True}),
    ("不要暂停 BTC Worker", "set_worker", {"inst_id": BTC, "paused": True}),
    ("暂停 ETH Worker", "set_worker", {"inst_id": BTC, "paused": True}),
    ("暂停 BTC Worker", "set_watching", {"on": False}),
    ("全体空仓", "set_watching", {"on": False}),
    ("有没有新机会？", "edit_watchlist", {"action": "remove", "inst_id": BTC}),
])
async def test_router_cannot_disable_watching_without_matching_user_command(settings, fake, message, tool, args):
    def model(messages, info):
        if isinstance(messages[-1].parts[-1], ToolReturnPart):
            assert "未修改" in str(messages[-1].parts[-1].content)
            return ModelResponse(parts=[TextPart("继续盯盘")])
        return ModelResponse(parts=[ToolCallPart(tool, args)])
    rt = make_rt(settings, fake, FunctionModel(model))
    rt.save_config(rt.config().model_copy(update={"instruments": [BTC, ETH]}))
    rt.watcher.running = True
    await rt.watcher.chat(message)
    assert rt.watcher.running and not rt.config().worker(BTC).paused and BTC in rt.config().instruments


SKILL_TEXT = "---\nname: 假突破反手\ndescription: 价格刺穿前高后收盘收回时，反手做回落\n---\n\n# 假突破反手\n\n1. 等 15 分钟 K 线收盘确认收回前高之下。\n2. 在前高附近挂限价卖单，止损放在影线尖上方。\n3. 目标看上一段的低点。\n"


async def test_router_has_the_general_worker_install_a_skill_and_every_worker_can_use_it(settings, fake):
    """用户把技能交给 Router：Router 自己不装，交给通用 Worker 装进项目的 skills 文件夹（原文由系统带过去，不靠模型抄）；
    装好后所有币的 Worker 都在输入里看到它，用 read_skill 打开全文。"""
    seen: dict[str, list[str]] = {"worker": []}

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = [t.name for t in info.function_tools]
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if "spawn_helper" in tools:  # 通用 Worker
            assert {"install_skill", "read_skill", "delete_skill"} <= set(tools)
            if not returns:
                assert "要安装的技能原文" in str(messages[-1].parts[-1].content)
                return ModelResponse(parts=[ToolCallPart("install_skill", {})])  # 原文系统带上了，不用抄
            return ModelResponse(parts=[TextPart(f"装好了：{returns[-1].content}")])
        if "open_position" in tools:  # 币种 Worker
            if not returns:
                seen["worker"].append(str(messages[-1].parts[-1].content))
                return ModelResponse(parts=[ToolCallPart("read_skill", {"name": "假突破反手"})])
            seen["worker"].append(str(returns[-1].content))
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, REPORT)])
        if not returns:  # Router
            assert "install_skill" in tools
            return ModelResponse(parts=[ToolCallPart("install_skill", {"content": SKILL_TEXT})])
        return ModelResponse(parts=[TextPart("技能装好了，所有 Worker 都能用。")])

    rt = make_rt(settings, fake, FunctionModel(run))
    out = await rt.watcher.chat("帮我装这个技能：" + SKILL_TEXT)
    assert "装好了" in out["reply"]
    file = settings.skills_dir / "假突破反手" / "SKILL.md"
    assert file.read_text(encoding="utf-8").strip() == SKILL_TEXT.strip()  # 一个字没变，只写在项目的技能文件夹里
    assert [s["name"] for s in rt.skills.all()] == ["假突破反手"] and rt.skills.all()[0]["enabled"]

    await rt.watcher.review(BTC, "定时复盘", source="event")
    assert "【已装技能】" in seen["worker"][0] and "假突破反手" in seen["worker"][0]
    assert "挂限价卖单" in seen["worker"][1] and "止损必须带" in seen["worker"][1]


async def test_skills_live_in_one_project_folder_and_settings_can_list_disable_delete(client, settings):
    """技能只在项目的 skills 文件夹里：界面能装（粘贴）、能认出手动拷进去的、能停用、能删除（删掉那个子文件夹）；
    想让 Agent 绕开保护的内容会被标出来；名字不能跳出这个文件夹。"""
    r = await client.post("/api/skills", json={"name": "", "content": SKILL_TEXT})
    assert r.status_code == 200 and r.json()["dir"] == str(settings.skills_dir)
    manual = settings.skills_dir / "hand-copied"
    manual.mkdir()
    (manual / "SKILL.md").write_text("# 梭哈\n\n看准了就全仓梭哈，不设止损，这样赚得快。\n", encoding="utf-8")
    rows = {s["id"]: s for s in (await client.get("/api/skills")).json()["skills"]}
    assert set(rows) == {"假突破反手", "hand-copied"} and rows["假突破反手"]["warnings"] == []
    assert "让 Agent 不带止损" in rows["hand-copied"]["warnings"] and "让 Agent 用满仓或最大杠杆" in rows["hand-copied"]["warnings"]
    assert "挂限价卖单" in (await client.get("/api/skills/假突破反手")).json()["content"]

    off = (await client.post("/api/skills/hand-copied/enable", params={"on": False})).json()["skills"]
    assert {s["id"]: s["enabled"] for s in off} == {"假突破反手": True, "hand-copied": False}
    left = (await client.delete("/api/skills/hand-copied")).json()["skills"]
    assert [s["id"] for s in left] == ["假突破反手"] and not manual.exists()
    assert (await client.delete("/api/skills/nope")).status_code == 404
    assert (await client.post("/api/skills", json={"name": "../../evil", "content": SKILL_TEXT})).status_code == 200
    assert sorted(p.name for p in settings.skills_dir.iterdir()) == ["evil", "假突破反手"] and not (settings.skills_dir.parent.parent / "evil").exists()
    assert (await client.post("/api/skills", json={"content": "", "url": "http://127.0.0.1/SKILL.md"})).status_code == 400  # 只收 https 公网地址
    assert (await client.post("/api/skills", json={"content": "", "url": "https://192.168.1.1/SKILL.md"})).status_code == 400


async def test_closed_trades_are_reviewed_into_lessons_that_grow_into_rules_or_get_dropped(settings, fake):
    """长期经验：每笔平掉的仓自动复盘写一条经验（先是观察）；被后面的交易印证 3 次升成规矩，被推翻 2 次删掉；
    模拟盘和实盘各存一套；Worker 复盘时看得到，并能自己印证 / 推翻。"""
    from ouy.models import ClosedTrade

    reviews: list[str] = []
    seen: list[str] = []
    plan: list[dict] = [
        {"lesson": "资金费率很高时不要追多，等回踩前高再进", "why": "追在高点，被回踩打掉止损"},
        {"lesson": "", "why": "又是追高被打掉", "confirms": [1]},
        {"lesson": "震荡里在区间中间开仓容易两头挨打", "why": "区间中间进场", "confirms": [1], "refutes": []},
    ]

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = [t.name for t in info.function_tools]
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not tools:  # 复盘员：没有工具，只输出结论
            reviews.append(str(messages[-1].parts[-1].content))
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, plan[len(reviews) - 1])])
        assert {"write_lesson", "judge_lesson"} <= set(tools)
        if not returns:
            seen.append(str(messages[-1].parts[-1].content))
            return ModelResponse(parts=[ToolCallPart("judge_lesson", {"lesson_id": 1, "verdict": "confirm", "reason": "这次等回踩再进，赚了"}), ToolCallPart("judge_lesson", {"lesson_id": 2, "verdict": "refute", "reason": "区间中间进场也赚了"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "actions_taken": []})])

    rt = make_rt(settings, fake, FunctionModel(run))
    cfg = rt.config()
    trades: list[ClosedTrade] = []
    now = int(time.time() * 1000)

    def closed(i: int, pnl: float, how: str = "全部平仓") -> ClosedTrade:
        return ClosedTrade(inst_id=BTC, side="long", open_px=60_000, close_px=60_000 + pnl * 10, pnl=pnl, pnl_pct=pnl, leverage=5, opened_ts=now - 600_000 + i, closed_ts=now - 1_000 + i, how=how)

    async def history(inst_id=None, limit=20, env=None):
        return list(reversed(trades))

    rt.trader.history = history
    trades.append(closed(0, -8))
    assert await rt.watcher._review_trades(cfg) == 0 and reviews == []  # 第一次运行：已有的历史不回头翻
    trades.append(closed(1, -12))
    trades.append(closed(2, 3, "部分平仓"))  # 部分平仓不复盘
    assert await rt.watcher._review_trades(cfg) == 1 and "已实现盈亏 -12.00" in reviews[0] and "还没有" in reviews[0]
    lessons = rt.watcher.lessons
    first = lessons.all("demo")[0]
    assert first["grade"] == "observe" and first["source"] == "review" and first["trade"]["pnl"] == -12 and lessons.all("live") == []
    assert await rt.watcher._review_trades(cfg) == 0  # 同一笔不会复盘两次
    trades.append(closed(3, -6))
    trades.append(closed(4, -5))
    assert await rt.watcher._review_trades(cfg) == 2 and "#1 [观察" in reviews[1]
    assert [(r["id"], r["grade"], r["confirms"]) for r in lessons.all("demo")] == [(1, "observe", 2), (2, "observe", 0)]

    await rt.watcher.review(BTC, "定时复盘", source="event")  # Worker 看得到经验，并自己印证 #1、推翻 #2
    assert "【长期经验（模拟盘" in seen[0] and "#1 [观察，印证 2" in seen[0] and "不要追多" in seen[0]
    assert [(r["id"], r["grade"], r["confirms"], r["refutes"]) for r in lessons.all("demo")] == [(1, "rule", 3, 0), (2, "observe", 0, 1)]
    assert lessons.judge("demo", 2, False, "又错了")["dropped"] and [r["id"] for r in lessons.all("demo")] == [1]
    # 30 天没用到：规矩降回观察
    rows = rt.store.get("lessons:demo")
    rows[0]["used_ts"] = now - 31 * 86_400_000
    rt.store.set("lessons:demo", rows)
    assert lessons.all("demo")[0]["grade"] == "observe" and "降回观察" in lessons.all("demo")[0]["note"]
    assert "【长期经验" not in rt.watcher._lessons_text("live", BTC)  # 实盘看不到模拟盘的经验


async def test_lessons_api_lets_the_user_view_edit_delete_and_keep_shared_rules(client, settings):
    """界面上的经验面板：当前环境的经验和两边共用的规矩都能加、改、删。"""
    r = await client.put("/api/lessons", json={"scope": "shared", "text": "晚上 12 点后不开新仓"})
    assert r.status_code == 200 and r.json()["env"] == "demo" and r.json()["shared"][0]["grade"] == "rule"
    r = await client.put("/api/lessons", json={"scope": "env", "inst_id": "btc-usdt-swap", "text": "BTC 整数关口前先减一半", "grade": "observe"})
    row = r.json()["lessons"][0]
    assert row["inst_id"] == BTC and row["grade"] == "observe" and row["source"] == "user"
    r = await client.put("/api/lessons", json={"id": row["id"], "scope": "env", "inst_id": BTC, "text": "BTC 整数关口前先减三分之一", "grade": "rule"})
    assert r.json()["lessons"][0]["text"].endswith("三分之一") and r.json()["lessons"][0]["grade"] == "rule"
    assert (await client.put("/api/lessons", json={"id": 999, "scope": "env", "text": "不存在的这一条经验"})).status_code == 404
    r = await client.delete(f"/api/lessons/env/{row['id']}")
    assert r.json()["lessons"] == [] and len(r.json()["shared"]) == 1
    assert (await client.delete(f"/api/lessons/shared/{r.json()['shared'][0]['id']}")).json()["shared"] == []


async def test_context_center_inspects_prompt_and_archives_without_wiping_memory(settings, fake):
    """记忆与上下文中心：经验与工作记忆均可单条暂停/置顶而不删数据；超 12h 或价格偏离 >5% 的临时笔记自动归档不注入；真实分段提示词可直接透视。"""
    rt = make_rt(settings, fake, FunctionModel(lambda _m, info: ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, REPORT)])))
    app = create_app(rt)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8899") as client:
        # 1. 长期经验暂停：保留在列表里，但不再注入 brief
        r = await client.put("/api/lessons", json={"scope": "env", "inst_id": BTC, "text": "支撑刺穿收回时可现价顺势试多", "grade": "rule"})
        lid = r.json()["lessons"][0]["id"]
        assert "支撑刺穿收回" in rt.watcher.lessons.brief("demo", BTC)
        r = await client.put("/api/lessons", json={"id": lid, "scope": "env", "enabled": False})
        assert r.json()["lessons"][0]["enabled"] is False
        assert "支撑刺穿收回" not in rt.watcher.lessons.brief("demo", BTC)
        await client.put("/api/lessons", json={"id": lid, "scope": "env", "enabled": True})
        assert "支撑刺穿收回" in rt.watcher.lessons.brief("demo", BTC)

        # 2. 工作记忆：记录时带价格，偏离 >5% 自动归档保留（不删库、不喂入），点置顶则常驻生效
        st = rt.watcher._state(BTC)
        st.last_px = 60_000.0
        rt.watcher._remember("demo", BTC, "60000 附近缩量横盘，等突破方向")
        assert "60000 附近" in rt.watcher._memory_text("demo", BTC)
        st.last_px = 64_000.0  # 偏离 +6.6% > 5%：自动归档，不删但不再喂给 Worker
        items = rt.watcher.memory_items("demo", BTC, st.last_px)
        assert len(items) == 1 and items[0]["status"] == "expired" and rt.watcher._memory_text("demo", BTC) == ""
        mid = items[0]["id"]
        r = await client.put("/api/agent/memory", json={"inst_id": BTC, "id": mid, "pinned": True})
        assert r.status_code == 200 and r.json()["items"][0]["status"] == "active"
        assert "60000 附近" in rt.watcher._memory_text("demo", BTC)

        # 3. 真实分段上下文透视接口 + Router app_status 工具无过期字段
        from pydantic_ai import RunContext
        from pydantic_ai.usage import RunUsage
        from ouy.agent.core import Deps, app_status

        status_text = await app_status(RunContext(deps=Deps(trader=rt.trader, market=rt.market, gateway=rt.gateway, config=rt.config(), kb=rt.kb, control=rt), model=rt.watcher._model(""), usage=RunUsage()))
        assert "最短复盘间隔秒" in status_text and "按风格自动" in status_text
        await rt.watcher.review(BTC, "定时复盘")
        ctx = (await client.get(f"/api/agent/context?inst_id={BTC}")).json()
        titles = [b["title"] for b in ctx["prompt"]["blocks"]]
        assert ctx["inst_id"] == BTC and not ctx["prompt"]["preview_only"]
        assert "你的工作记忆" in titles and any("长期经验" in t for t in titles) and ctx["prompt"]["stats"]["prompt_chars"] > 100
        assert (await client.delete(f"/api/agent/memory/{BTC}/{mid}")).json()["items"] == []


async def test_anti_paper_hands_guard_blocks_early_breakeven_and_panic_close(settings, fake):
    """方案 A 持仓硬护栏：浮盈未达 1.2R 拦截 Worker 把止损贴回入场价；开仓 10 分钟内无结构收盘破坏拦截恐慌平仓。"""
    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage
    from ouy.agent.core import Deps, close_position, move_stops

    rt = make_rt(settings, fake, "test")
    await _open(rt)  # 多单 60000，止损 59400 → 1R = 600
    lot = rt.trader.lots("demo", BTC)[0]
    fake.set_price(BTC, 60_360)  # +0.6R，未达 1.2R 门槛
    deps = Deps(trader=rt.trader, market=rt.market, gateway=rt.gateway, config=rt.config(), kb=rt.kb, plan_check=lambda _p: None)
    rctx = RunContext(deps=deps, model=rt.watcher._model(""), usage=RunUsage())

    # 1. +0.6R 想提保本到 60000：拦截，原止损 59400 不变
    res_sl = await move_stops(rctx, BTC, "先锁一下保本", stop_loss=60_000, lot_id=lot["id"])
    assert "禁止过早提保本" in res_sl and fake.algos[0]["slTriggerPx"] == "59400"

    # 2. 刚开仓不到 10 分钟，无已收盘结构破坏想直接平仓：拦截，仓位保留
    res_cl = await close_position(rctx, BTC, "短线有点回撤先出来", lot_id=lot["id"])
    assert "禁止刚开仓就恐慌平仓" in res_cl and BTC in fake.positions

    # 3. 浮盈冲到 +1.3R（60780）后再提保本：放行！
    fake.set_price(BTC, 60_780)
    res_ok = await move_stops(rctx, BTC, "浮盈超 1.2R 移到成本线上", stop_loss=60_080, lot_id=lot["id"])
    assert "拦截" not in res_ok and fake.algos[0]["slTriggerPx"] == "60080"

    # 4. 明确写明已收盘跌破结构失效时平仓：放行！
    res_break = await close_position(rctx, BTC, "15m 收盘跌破支撑结构失效", lot_id=lot["id"])
    assert "拦截" not in res_break and BTC not in fake.positions


async def test_router_replaces_outdated_preferences_and_keeps_them_short(settings, fake):
    """用户改口时，Router 把过时的偏好换掉，不让新旧两种说法同时留着；太长的偏好不收（界面上显示不全）。"""
    replies: list[str] = []

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[
                ToolCallPart("set_preference", {"action": "add", "text": "用户原话很长" * 20}),
                ToolCallPart("set_preference", {"action": "add", "text": "总本金按交易账户 5 U 算", "replaces": ["用户只投入 2 USDT 总本金", "当前 2 USDT 总本金只考虑 SOL"]}),
            ])
        replies.extend(str(r.content) for r in returns)
        return ModelResponse(parts=[TextPart("已更新")])

    rt = make_rt(settings, fake, FunctionModel(run))
    rt.save_config(rt.config().model_copy(update={"prefs": ["用户只投入 2 USDT 总本金（所有品种共用）", "当前 2 USDT 总本金只考虑 SOL 的新交易", "不追高"]}))
    await rt.watcher.chat("我说的是 5 U")
    assert rt.config().prefs == ["不追高", "总本金按交易账户 5 U 算"]
    assert "太长" in replies[0] and "换掉了过时的 2 条" in replies[1]


async def test_worker_is_told_the_bigger_trend_and_that_counter_trend_needs_a_reversal_signal(settings, fake):
    """超短线也要先看大方向：程序把大方向算好告诉 Worker，并要求逆势单必须有关键位的反转信号。"""
    from ouy.agent.watcher import _trend_text

    up = _trend_text("scalp", {"windows": {"30m": {"trend": "震荡（一边抬高一边降低）"}, "1H": {"trend": "上涨（高点低点都在抬高）"}}})
    assert "大方向向上（按 1H）：做多是顺势，做空是逆势" in up and "30m 震荡" in up and "逆着大方向的单" in up
    assert "大方向向下（按 30m）" in _trend_text("scalp", {"windows": {"30m": {"trend": "下跌（高点低点都在降低）"}, "1H": {"trend": "震荡（一边抬高一边降低）"}}})
    assert "不等于已确认区间" in _trend_text("scalp", {"windows": {"1H": {"trend": "震荡（一边抬高一边降低）"}}})
    assert "算不出来" in _trend_text("swing", {})
    prompts: list[str] = []
    rt = make_rt(settings, fake, scripted_model(prompts))
    await rt.watcher.review(BTC, "测试")
    assert "【大方向】" in prompts[0] and "不能只凭眼前几根 K 线的涨跌下单" in prompts[0]


async def test_profit_lock_keeps_a_winner_from_turning_into_a_loss(settings, fake):
    """保盈线：浮盈最高到过约 0.5R，程序直接把止损移到入场价 + 往返成本；轨迹存库，重启后不会重复移；之后回落只会保本出场。"""
    from ouy.store import now_ms

    rt = make_rt(settings, fake, "test")
    await _open(rt)  # 多单 60000，止损 59400 → 1R = 600
    cfg = rt.config()
    rt.watcher.strategies.deploy(cfg, BTC, "manage", "", {"lot_id": rt.trader.lots("demo", BTC)[0]["id"], "breakeven_r": 1})

    async def at(px: float) -> str:
        st = rt.watcher._state(BTC)
        fake.set_price(BTC, px)
        st.last_px, st.lot_alert = px, ""
        await rt.watcher._watch_lots(cfg, BTC, st, time.time())
        return st.lot_alert

    assert await at(60_600) == "" and fake.algos[0]["slTriggerPx"] == "59400"  # +1.0R：未到 1.5R 高点，不提前锁死
    alert = await at(60_960)  # +1.6R：冲出高点后才启动保盈线
    lid = str(rt.trader.lots("demo", BTC)[0]["id"])
    assert "保盈线" in alert and "扣费后不亏" in alert and float(fake.algos[0]["slTriggerPx"]) >= 60_060
    locked_px = float(fake.algos[0]["slTriggerPx"])
    writes = len(fake.called("swap_place_algo_order")) + len(fake.called("swap_amend_algo_order"))
    rt.watcher.states.clear()  # 模拟重启：内存状态没了，轨迹还在库里
    assert await at(60_800) == "" and writes == len(fake.called("swap_place_algo_order")) + len(fake.called("swap_amend_algo_order"))
    track = rt.watcher.lot_track("demo")[lid]
    assert track["peak"] >= 1.6 and track["locked"] == pytest.approx(locked_px) and "保盈线已执行" in rt.watcher.track_text(track)
    fake.set_price(BTC, 61_000)  # 移保本规则不能把已经锁好的止损往回放
    await rt.watcher.strategies.tick(cfg, await rt.trader.account("demo"), {BTC: 61_000})
    assert float(fake.algos[0]["slTriggerPx"]) == pytest.approx(locked_px) and all(r["status"] != "failed" for r in rt.store.strategies("demo", BTC, live=False))
    cash = fake.cash
    await at(60_040)  # 回落：止损在成本线外，打掉也不亏
    assert BTC not in fake.positions and fake.cash > cash

    # Worker 对这笔写了 lock_r=0：不用保盈线
    fake.positions.clear(); fake.algos.clear(); fake.set_price(BTC, 60_000)
    rt2 = make_rt(settings, fake, "test")
    await _open(rt2)
    rt2.watcher.strategies.deploy(rt2.config(), BTC, "manage", "", {"lot_id": rt2.trader.lots("demo", BTC)[0]["id"], "lock_r": 0})
    st2 = rt2.watcher._state(BTC)
    fake.set_price(BTC, 60_420)
    st2.last_px = 60_420
    await rt2.watcher._watch_lots(rt2.config(), BTC, st2, time.time())
    assert fake.algos[0]["slTriggerPx"] == "59400"

    # 结构反向：5m 刚确认转空，多单在浮盈里先锁住不亏，并叫醒 Worker 判断入场理由还在不在
    rt2.watcher.strategies.metrics[BTC] = {"ts": now_ms(), "values": {}, "levels": {}, "shift": {"side": "short", "kind": "反转", "level": 60_300.0, "ts": now_ms(), "bars_ago": 0,
                                                                                                "origin": 60_500.0, "extreme": 60_250.0, "why": "收盘跌破前一个摆动低点 60300"}}
    rt2.save_config(rt2.config().model_copy(update={"profit_lock": True}))
    rt2.store.delete("lot_track:demo")
    st2.lot_alert = ""
    await rt2.watcher._watch_lots(rt2.config(), BTC, st2, time.time())
    assert "结构反向" in st2.lot_alert and "入场理由" in st2.lot_alert


def test_turn_radar_needs_sweep_then_structure_reversal():
    from ouy.agent.signals import turn_watch

    bars = [Candle(i * 300_000, 110 - i * 0.15 + 0.1, 110 - i * 0.15 + 0.3, 110 - i * 0.15 - 0.3, 110 - i * 0.15, 10) for i in range(60)]
    bars.append(Candle(60 * 300_000, 101.0, 101.4, 99.8, 101.2, 30))  # 扫掉前 4 小时低点 100.85 后收回
    bars += [Candle((61 + k) * 300_000, 101.2 + k * 0.2, 101.6 + k * 0.2, 101.0 + k * 0.2, 101.4 + k * 0.2, 10) for k in range(9)]
    early = turn_watch(bars, None)
    assert early["side"] == "long" and early["stage"] in (1, 2) and "不算转折" in early["text"]
    shift = {"side": "long", "kind": "反转", "level": 102.5, "ts": 66 * 300_000}
    done = turn_watch(bars, shift)
    assert done["stage"] == 3 and done["entry"] == 102.5 and done["stop"] < 99.8 and "已确认" in done["text"]
    assert turn_watch(bars, {**shift, "kind": "延续"})["stage"] < 3  # 顺原方向的延续不算转折
    broken = bars + [Candle(70 * 300_000, 102, 102.2, 99.5, 99.6, 10)]  # 之后又创了新低、收不回来：这次扫单失败
    assert (turn_watch(broken, shift) or {}).get("side") != "long"


def test_turn_is_one_algorithm_with_stages():
    """苗头（扫流动性 / 背离）→ 确认（收盘越过摆动点）→ 盘中失守 / 收回：三样原料合成一个读数，以最新发生的那一步为准。"""
    from ouy.agent.signals import turn_state

    radar = {"side": "long", "stage": 2, "ts": 100, "ref": 99.0, "text": "看涨苗头", "why": "扫掉前低后收回"}
    shift = {"side": "long", "kind": "反转", "level": 102.5, "origin": 99.8, "ts": 200, "bars_ago": 1, "why": "收盘站上 102.5"}
    assert turn_state(None, None) is None
    assert turn_state(None, radar)["stage"] == "early"
    done = turn_state(shift, {**radar, "stage": 3, "choch_ts": 200, "entry": 102.5, "stop": 99.5, "status": "等回踩 102.5"})
    assert done["stage"] == "confirmed" and done["stop"] == 99.5 and "扫掉前低" in done["text"]  # 第三步和确认是同一件事，只报一次
    assert turn_state(shift, radar)["stage"] == "confirmed"  # 比确认更早的苗头已经被盖过去
    lost = turn_state(shift, None, {"ts": 300_000, "level": 102.5, "value": 1, "text": "连续两次报价退回"})
    assert lost["stage"] == "lost" and lost["ts"] == 300_000 and "连续两次报价退回" in lost["text"]
    assert turn_state(shift, None, {"ts": 400_000, "level": 102.5, "value": 0, "text": "收回"})["stage"] == "reclaimed"
    assert turn_state(shift, None, {"ts": 400_000, "level": 90.0, "value": 1, "text": "上一次转折的"})["stage"] == "confirmed"  # 别的转折位的失守不算
    against = turn_state(shift, {**radar, "side": "short", "ts": 300})
    assert against["stage"] == "early" and against["side"] == "short" and "102.5" in against["text"]  # 确认之后出现反方向的苗头


async def test_builtin_strategies_are_listed_and_a_new_look_period_reschedules_the_next_look(client):
    """策略库面板里列出知识库的内置打法；改了定时看盘周期，已排好的下一次看盘按新周期重排。"""
    rows = (await client.get("/api/playbooks/builtin")).json()
    assert len(rows) >= 20 and all(r["name"] and r["when"] and r["score"] == "" and r["losing"] is False for r in rows)
    assert any(r["name"].startswith("趋势回踩") for r in rows)

    rt = client.rt
    st = rt.watcher._state(BTC)
    st.had_position, st.next_due_ts = False, time.time() + 4 * 3600
    await rt.apply_config(rt.config().model_copy(update={"look_minutes": 5}))
    assert 0 < st.next_due_ts - time.time() <= 610
    with pytest.raises(ValueError):
        AgentConfig(look_minutes=7)
