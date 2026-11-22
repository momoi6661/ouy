"""计划就是指令：Worker 给出计划，程序核对后直接照着下单；空仓时不给计划必须说明原因。"""
from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ouy.agent.plan import build_candidates, candidates_text, check_plan, order_params, same_plan, trade_cost_pct
from ouy.agent.watcher import distinct_notes
from ouy.models import ActionResult, AgentConfig, TradePlan, WorkerOverride

from .test_agent_and_api import BTC, REPORT, make_rt

PLAN = REPORT["plan"]  # 做多 59800~60100，止损 59400，目标 61000 / 62000；假交易所的现价是 60000
PULLBACK = {**PLAN, "entry_low": 59_500, "entry_high": 59_700, "stop_loss": 59_200, "take_profits": [60_600, 61_000]}


def test_strategy_selection_checks_before_deduplicating_and_respects_direction():
    snap = {"last": 100, "timeframes": {"15m": {"structure": "range", "adx14": 13, "atr14": 1, "support": [{"price": 99}], "resistance": [{"price": 102}]}},
            "orderbook": {"spread_pct": 0.01}}
    # 变盘候选先生成，第一目标离入场太近；随后同价位的支撑回踩有足够空间。
    shift = {"level": 99, "extreme": 99.1, "origin": 95, "bars_ago": 1, "side": "long", "kind": "结构"}
    params = dict(style="intraday", min_rr=1.2, entry_mode="limit", quant_bias="long", shift=shift)
    rows = build_candidates(snap, **params)
    assert len(rows) == 1 and rows[0]["ok"] and rows[0]["strategy"] == "区间高抛低吸"
    assert "优先核实" in candidates_text(snap, **params)
    assert not build_candidates(snap, quant_mode="quiet", **params)
    assert not build_candidates(snap, quant_mode="breakout", **params)  # 无箱体，不把普通回踩充作突破
    snap["orderbook"] = {}
    assert "点差缺失" in candidates_text(snap, **params)


@pytest.mark.parametrize("side,near,deeper,target", [("long", 99.95, 99, 100.6), ("short", 100.05, 101, 99.4)])
def test_bad_nearest_level_does_not_hide_viable_limit(side, near, deeper, target):
    own, opposite = ("support", "resistance") if side == "long" else ("resistance", "support")
    frame = {"structure": "range", "adx14": 13, "atr14": 1, own: [{"price": near}, {"price": deeper}], opposite: [{"price": target}]}
    snap = {"last": 100, "orderbook": {"spread_pct": 0.01}, "timeframes": {"5m": frame}}
    rows = build_candidates(snap, style="scalp", min_rr=1.2, quant_bias=side, entry_mode="limit")
    assert rows[0]["ok"] and rows[0]["check"].limit and rows[0]["check"].ref == deeper
    assert rows[0]["plan"].take_profits[0] == target
    assert any(not row["ok"] and row["check"].ref == near for row in rows)


def reporting(*reports: dict, seen: list[str] | None = None, calls: list[int] | None = None) -> FunctionModel:
    """不调用任何工具、直接交结论的模型：第 n 次被问就交第 n 份（用完了一直交最后一份）。"""
    n = {"i": 0}

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append("\n".join(str(getattr(p, "content", "")) for p in messages[-1].parts))
        if calls is not None:
            calls.append(1)
        report = reports[min(n["i"], len(reports) - 1)]
        n["i"] += 1
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, report)])

    return FunctionModel(run)


def plan_rows(rt) -> list[dict]:
    return [r for r in rt.store.strategies("demo", BTC, live=False) if r["params"].get("plan")]


@pytest.mark.parametrize("reason,stance,rechecks", [("cost", "active", 1), ("limit", "active", 0), ("cost", "cautious", 0)])
async def test_active_worker_reconciles_cost_veto_once_without_forcing_trade(settings, fake, monkeypatch, reason, stance, rechecks):
    refused = {**REPORT, "plan": None, "no_plan_reason": reason, "watch_levels": [{"price": 59700, "note": "等结构确认"}]}
    seen = []
    rt = make_rt(settings, fake, reporting(refused, seen=seen))
    rt.save_config(rt.config().model_copy(update={"stance": stance}))
    plan = TradePlan(**PULLBACK)
    check = check_plan(plan, 60000, min_rr=1.2, cost_pct=0.1, limit_cost_pct=0.07)
    row = {"ok": True, "plan": plan, "check": check, "align": "顺大方向", "strategy": "趋势回踩", "basis": "测试支撑"}
    monkeypatch.setattr("ouy.agent.watcher.build_candidates", lambda *a, **kw: [row])
    result = await rt.watcher.review(BTC, "测试")
    assert len(seen) == 1 + rechecks and not fake.orders and not fake.positions
    if rechecks:
        assert "成本结论需要复核" in seen[-1]
    assert result["plan_order"]["status"] == "watching" and "59700" in result["plan_order"]["text"]


def test_program_checks_a_plan_with_costs_and_decides_how_to_enter():
    plan = TradePlan(**PLAN)
    now = check_plan(plan, 60_000, min_rr=1.2, cost_pct=0.1)  # 现价在入场区间里：现在就进
    assert now.ok and now.how == "now" and now.stop_pct == pytest.approx(1.0) and now.net_rr == pytest.approx((1.6667 - 0.1) / 1.1, abs=1e-3)
    wait = check_plan(TradePlan(**PULLBACK), 60_000, min_rr=1.2, cost_pct=0.1)  # 还没到：等回到入场区间离现价近的那一边
    assert wait.ok and wait.how == "pullback" and wait.ref == 59_700
    resting = check_plan(TradePlan(**PULLBACK), 60_000, min_rr=1.2, cost_pct=0.1, limit_cost_pct=0.07)  # 挂限价单等：进场按挂单手续费算
    assert resting.limit and resting.cost_pct == 0.07 and resting.net_rr > wait.net_rr and not wait.limit
    params = order_params(TradePlan(**PULLBACK), wait, "scalp")
    assert params["trigger"] == 59_700 and params["take_profit"] == 61_000 and params["miss_px"] == 60_600 and params["plan"] is True
    assert params["manage"]["breakeven_r"] == 1.0 and params["manage"]["partials"][0]["fraction"] == 0.5 and params["manage"]["max_hold_minutes"] == 30
    assert same_plan({"kind": "pullback", "side": "long", "params": params}, TradePlan(**{**PULLBACK, "entry_high": 59_720}), check_plan(TradePlan(**{**PULLBACK, "entry_high": 59_720}), 60_000, min_rr=1.2, cost_pct=0.1), "scalp")
    # 同一份价位，手续费高了就不够格：数字由程序算，告诉它第一目标至少要多远
    thin = check_plan(TradePlan(**{**PLAN, "take_profits": [60_700]}), 60_000, min_rr=1.2, cost_pct=0.1)
    assert not thin.ok and "净盈亏比 0.97" in thin.issues[0] and "不到下限 1.2" in thin.issues[0] and "no_plan_reason 选 cost" in thin.issues[0]
    assert not check_plan(TradePlan(**{**PLAN, "stop_loss": 59_900}), 60_000, min_rr=1.2, cost_pct=0.1).ok  # 止损放进了入场区间
    assert "贴近止损" in check_plan(plan, 59_500, min_rr=1.2, cost_pct=0.1).issues[0]  # 价格已经穿过入场区间
    brk = TradePlan(**{**PLAN, "entry_type": "breakout", "entry_low": 60_300, "entry_high": 60_500, "stop_loss": 59_900, "take_profits": [61_500]})
    assert check_plan(brk, 60_000, min_rr=1.2, cost_pct=0.1).how == "breakout" and check_plan(brk, 60_350, min_rr=1.2, cost_pct=0.1).how == "now"
    assert trade_cost_pct(0.05, 0.02) == pytest.approx(0.12)


async def test_a_plan_whose_entry_zone_holds_the_price_is_executed_without_any_tool_call(settings, fake):
    """以前：结论里有计划，但没调下单工具，就什么都不发生。现在：空仓时计划就是指令，程序照着进场并接手管理。"""
    calls: list[int] = []
    rt = make_rt(settings, fake, reporting(REPORT, calls=calls))
    record = await rt.watcher.review(BTC, "定时复盘")
    assert len(calls) == 1 and fake.positions[BTC]["pos"] > 0 and fake.algos[0]["slTriggerPx"] == "59400" and fake.algos[0]["tpTriggerPx"] == "62000"
    assert record["plan_order"]["status"] == "executed" and "照计划现价做多" in record["plan_order"]["text"]
    assert record["actions"][0]["status"] == "executed"
    manage = plan_rows(rt)[0]
    assert manage["kind"] == "manage" and manage["status"] == "active" and manage["params"]["breakeven_r"] == 1.0 and manage["params"]["partials"]


async def test_a_plan_away_from_the_price_rests_as_a_limit_order_on_the_exchange(settings, fake):
    """等价格回来的计划：直接在交易所挂限价单（带止损止盈），程序挂了它也在；同一个计划不重复挂。"""
    seen: list[str] = []
    rt = make_rt(settings, fake, reporting({**REPORT, "plan": PULLBACK}, seen=seen))
    record = await rt.watcher.review(BTC, "定时复盘")
    order = fake.orders[0]
    assert len(fake.orders) == 1 and (order["side"], order["ordType"], float(order["px"])) == ("buy", "limit", 59_700)
    assert record["plan_order"]["status"] == "armed" and "挂了限价买单 @ 59700" in record["plan_order"]["text"] and BTC not in fake.positions
    guard = rt.trader.order_guards("demo", BTC)[0]
    assert guard["plan"] and guard["cancel_px"] == 60_600 and guard["expires_ts"]  # 没回来就先到第一目标、或者过期：程序撤
    manage = plan_rows(rt)[0]
    assert manage["kind"] == "manage" and manage["status"] == "active"  # 成交后的管理已经备好，等挂单成交
    await rt.watcher._tick(rt.config())
    assert rt.store.get_strategy(manage["id"])["note"] == "等挂单成交后开始管理"
    # 下一轮给的还是同一个计划（价位小幅挪动）：不撤不重挂；并且它看得到程序上次做了什么
    again = await rt.watcher.review(BTC, "定时复盘")
    assert len(fake.orders) == 1 and again["plan_order"]["status"] == "kept" and "【程序对上次计划的处理】程序照计划挂了限价买单" in seen[-1]


async def test_router_board_distinguishes_a_report_from_live_entry_rules_and_orders(settings, fake):
    rt = make_rt(settings, fake, reporting({**REPORT, "plan": PULLBACK}))
    watcher = rt.watcher
    watcher._state(BTC).report = {**REPORT, "plan": PULLBACK, "plan_order": {"status": "skipped"}}
    flat = (await watcher._board())["workers"][BTC]
    assert flat["状态"] == "观望" and flat["计划"] == PULLBACK and flat["上次计划回执"]["status"] == "skipped"
    assert flat["交易所入场委托"] == [] and flat["本地入场规则"] == []

    row = watcher.strategies.deploy(rt.config(), BTC, "breakout", "long", {"trigger": 60_500, "stop_loss": 60_200})
    local = (await watcher._board())["workers"][BTC]
    assert local["状态"] == "本地规则已布置，盯盘未启动"
    assert local["本地入场规则"][0]["编号"] == row["id"] and local["交易所入场委托"] == []
    watcher.strategies.cancel(row["id"])

    await watcher.review(BTC, "定时复盘")
    resting = (await watcher._board())["workers"][BTC]
    assert resting["状态"] == "有入场委托" and resting["交易所入场委托"][0]["价格"] == 59_700


async def test_apply_plan_reads_exchange_receipt_and_honors_entry_invalidation(settings, fake):
    prepared = {**PULLBACK, "cancel_before_entry": {"when": "price_above", "level": 60_200}}
    rt = make_rt(settings, fake, reporting({**REPORT, "plan": prepared}))
    rt.save_config(rt.config().model_copy(update={"auto_arm_plan": False}))
    watcher = rt.watcher
    report = await watcher.review(BTC, "定时复盘")
    stamp = report["ts"]
    assert (await watcher.plan_receipt(BTC, stamp))["state"] == "draft"
    result = await watcher.apply_plan(BTC, stamp)
    assert result["status"] == "armed" and len(fake.orders) == 1
    assert (await watcher.plan_receipt(BTC, stamp))["state"] == "live"
    await watcher._guard_orders(rt.config(), await rt.trader.account("demo"), {BTC: 60_200})
    assert not fake.orders
    assert (await watcher.plan_receipt(BTC, stamp))["state"] == "canceled"


async def test_a_resting_plan_dies_by_itself_or_by_an_explicit_reason_not_by_second_thoughts(settings, fake):
    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if stage["n"] == 1 and not returns:  # 第二轮它想把上一轮挂的单撤掉
            return ModelResponse(parts=[ToolCallPart("cancel_pending_orders", {"inst_id": BTC, "reason": "重新估了一遍", "changed": "盈亏比差一点"})])
        if returns:
            said.append(str(returns[-1].content))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, reports[stage["n"]])])

    said: list[str] = []
    stage = {"n": 0}
    reports = [{**REPORT, "plan": PULLBACK}, {**REPORT, "plan": None, "no_plan_reason": "cost"}, {**REPORT, "plan": None, "no_plan_reason": "objection"}]
    rt = make_rt(settings, fake, FunctionModel(run))
    await rt.watcher.review(BTC, "定时复盘")
    stage["n"] = 1
    kept = await rt.watcher.review(BTC, "定时复盘")
    assert "[没有撤]" in said[0] and "由 plan 管" in said[0] and len(fake.orders) == 1 and kept["plan_order"]["status"] == "kept"
    stage["n"] = 2
    dropped = await rt.watcher.review(BTC, "定时复盘")  # 明确的反对信号：程序把它撤掉
    assert dropped["plan_order"]["status"] == "cancelled" and not fake.orders
    # 没回到入场位就先到了第一目标：这一段走完了，程序自己撤
    stage["n"] = 0
    await rt.watcher.review(BTC, "定时复盘")
    assert len(fake.orders) == 1
    fake.set_price(BTC, 60_650)
    await rt.watcher._tick(rt.config())
    messages = [item["text"] for item in rt.watcher.inbox("demo", BTC)]
    assert not fake.orders and BTC not in fake.positions
    assert any("撤单线 60600" in text for text in messages)
    assert any("触及盯盘价位 60600" in text for text in messages)


@pytest.mark.parametrize("receipt", ["canceled", "missing", "partial"])
async def test_changed_target_requires_verified_zero_fill_before_replacement(settings, fake, monkeypatch, receipt):
    updated = {**PULLBACK, "take_profits": [60_800, 61_500]}
    rt = make_rt(settings, fake, reporting({**REPORT, "plan": PULLBACK}, {**REPORT, "plan": updated}))
    await rt.watcher.review(BTC, "初始计划")
    oid = fake.orders[0]["ordId"]
    if receipt == "missing":
        monkeypatch.setattr(fake, "_swap_get_order", lambda args: [])
    elif receipt == "partial":
        fake.orders[0]["accFillSz"] = "0.01"
    result = (await rt.watcher.review(BTC, "目标更新"))["plan_order"]
    if receipt == "canceled":
        assert result["status"] == "armed" and len(fake.orders) == 1 and fake.orders[0]["ordId"] != oid
    else:
        assert result["status"] == "failed" and not fake.orders
        await rt.watcher.review(BTC, "再次复核")
        assert not fake.orders and len(fake.called("swap_place_order")) == 1


async def test_limit_rejection_never_promotes_to_market(settings, fake, monkeypatch):
    rt = make_rt(settings, fake, reporting({**REPORT, "plan": PULLBACK}))
    attempts = []

    async def reject(action, **kwargs):
        attempts.append(action.order_type)
        return ActionResult(status="rejected", action=action, message="做多限价高于现价")

    monkeypatch.setattr(rt.trader, "submit", reject)
    result = (await rt.watcher.review(BTC, "复核"))["plan_order"]
    assert result["status"] == "rejected" and attempts == ["limit"] and not fake.positions


async def test_flat_with_no_plan_and_no_reason_is_sent_back_once_then_accepted(settings, fake):
    """「再等等」不是一个决定：空仓、没有任何入场在等，又不给计划也不说原因，结论被退回一次。"""
    seen: list[str] = []
    rt = make_rt(settings, fake, reporting({**REPORT, "plan": None}, seen=seen))
    record = await rt.watcher.review(BTC, "定时复盘")
    assert len(seen) == 2 and "既没给 plan，也没选 no_plan_reason" in seen[1] and "entry_type=pullback / breakout" in seen[1]
    assert record["plan"] is None and BTC not in fake.positions and not plan_rows(rt)  # 只退一次，第二次原样收下，不卡死
    # 用户限制不是成本异议，不退回要求入场
    seen.clear()
    rt.watcher.reset_model()
    rt.watcher.model_factory = lambda: reporting({**REPORT, "plan": None, "no_plan_reason": "limit"}, seen=seen)
    await rt.watcher.review(BTC, "定时复盘")
    assert len(seen) == 1


async def test_a_plan_that_fails_the_cost_check_goes_back_with_numbers_and_is_never_executed(settings, fake):
    seen: list[str] = []
    thin = {**REPORT, "plan": {**PLAN, "take_profits": [60_700]}}
    rt = make_rt(settings, fake, reporting(thin, {**REPORT, "plan": PULLBACK}, seen=seen))
    record = await rt.watcher.review(BTC, "定时复盘")
    assert "没通过程序核对" in seen[1] and "净盈亏比 0.97" in seen[1] and "第一目标至少要离入场" in seen[1]
    assert record["plan"]["entry_high"] == 59_700 and float(fake.orders[0]["px"]) == 59_700  # 改过的计划挂出去了
    # 改了还是不行：不执行，原因记下来给用户和下一轮看
    rt.watcher.reset_model()
    rt.watcher.model_factory = lambda: reporting(thin)
    fake.orders.clear()
    rt.store.set(f"order_guard:demo:{BTC}", [])
    record = await rt.watcher.review(BTC, "定时复盘")
    assert record["plan_order"]["status"] == "skipped" and "没有执行" in record["plan_order"]["text"] and BTC not in fake.positions
    assert any(a["agent"] == "strategy" and "没通过程序核对" in a["text"] for a in rt.store.activity("demo", inst_id=BTC))


@pytest.mark.parametrize("take_alternative", [True, False])
async def test_worker_sees_cooling_candidate_and_can_choose_a_verified_alternative(settings, fake, monkeypatch, take_alternative):
    deeper = {**PULLBACK, "entry_low": 59_300, "entry_high": 59_400, "stop_loss": 59_100, "strategy": "前高前低法"}
    second = {**REPORT, "plan": deeper} if take_alternative else {**REPORT, "plan": None, "no_plan_reason": "objection"}
    seen: list[str] = []
    rt = make_rt(settings, fake, reporting({**REPORT, "plan": PULLBACK}, second, seen=seen))
    cfg = rt.config()
    lid = rt.store.add_lot("demo", BTC, "long", cfg.style_for(BTC), 1, 59_700)
    rt.store.update_lot(lid, status="closed")
    plans = [TradePlan(**{**PULLBACK, "strategy": "趋势回踩"}), TradePlan(**deeper)]
    rows = []
    for plan in plans:
        check = check_plan(plan, 60_000, min_rr=1.2, cost_pct=0.1, limit_cost_pct=0.07)
        assert check.ok and check.limit
        rows.append({"ok": True, "plan": plan, "check": check, "align": "顺大方向", "strategy": plan.strategy, "basis": "测试支撑"})
    monkeypatch.setattr("ouy.agent.watcher.build_candidates", lambda *a, **kw: rows)

    record = await rt.watcher.review(BTC, "定时复盘")
    assert "暂停入场：同一个位置刚做过一笔" in seen[0]
    assert "优先核实「前高前低法」@ 59400" in seen[0]
    assert len(seen) == 2 and "另一条候选「前高前低法」@ 59400" in seen[1]
    assert [float(o["px"]) for o in fake.orders] == ([59_400] if take_alternative else [])
    assert record["plan_order"]["status"] == ("armed" if take_alternative else "watching")


async def test_user_can_turn_plan_execution_off_and_confirm_mode_still_asks_first(settings, fake):
    seen: list[str] = []
    rt = make_rt(settings, fake, reporting(REPORT, seen=seen))
    rt.save_config(rt.config().model_copy(update={"auto_arm_plan": False}))
    record = await rt.watcher.review(BTC, "定时复盘")
    assert record["plan_order"]["status"] == "advised" and BTC not in fake.positions and "【计划不会自动执行】" in seen[0]
    # 「先问我」：程序照计划提交的是一张等同意的单，同意了才成交
    rt.save_config(rt.config().model_copy(update={"auto_arm_plan": True, "mode": "confirm"}))
    record = await rt.watcher.review(BTC, "定时复盘")
    assert record["plan_order"]["status"] == "pending" and BTC not in fake.positions
    pending = rt.store.pending("demo")[0]
    await rt.trader.approve(pending["id"])
    assert fake.positions[BTC]["pos"] > 0


async def test_pending_pullback_does_not_claim_an_exchange_order(settings, fake):
    rt = make_rt(settings, fake, reporting({**REPORT, "plan": PULLBACK}))
    rt.save_config(rt.config().model_copy(update={"mode": "confirm"}))
    record = await rt.watcher.review(BTC, "测试")
    assert record["plan_order"]["status"] == "pending" and "未提交到交易所" in record["plan_order"]["text"]
    assert not fake.orders and rt.store.pending("demo")
    worker = (await rt.watcher._board())["workers"][BTC]
    assert worker["状态"] == "入场操作待确认"
    assert worker["交易所入场委托"] == [] and worker["待确认入场操作"][0]["价格"] == 59_700


async def test_each_worker_can_have_its_own_style_stance_and_entry_mode(settings, fake):
    seen: list[str] = []
    rt = make_rt(settings, fake, reporting({**REPORT, "plan": None, "no_plan_reason": "cost"}, seen=seen))
    cfg = rt.config().model_copy(update={"stance": "cautious", "entry_mode": "market", "styles": {BTC: "scalp"},
                                         "workers": {BTC: WorkerOverride(stance="active", entry_mode="limit")}})
    rt.save_config(cfg)
    assert cfg.stance_for(BTC) == "active" and cfg.entry_mode_for(BTC) == "limit" and cfg.stance_for("ETH-USDT-SWAP") == "cautious"
    await rt.watcher.review(BTC, "定时复盘")
    prompt = seen[0]
    assert "【这个品种的交易风格】超短线" in prompt and "【出手尺度】积极（到第一目标的净盈亏比至少 1.2）" in prompt and "【入场方式】挂单优先" in prompt
    assert "【成本】吃单手续费单边 0.05%" in prompt and "由程序核对" in prompt
    # 出手尺度也决定程序核对计划的下限：保守要 2，同一个计划就过不了
    assert (await rt.watcher._check_plan(cfg, BTC, TradePlan(**PLAN))).ok
    strict = cfg.model_copy(update={"workers": {BTC: WorkerOverride(stance="cautious")}})
    assert not (await rt.watcher._check_plan(strict, BTC, TradePlan(**PLAN))).ok


def test_working_memory_keeps_one_note_per_topic(settings, fake):
    rt = make_rt(settings, fake, "test")
    for hhmm in ("17:22", "17:33", "17:40"):
        rt.watcher._remember("demo", BTC, f"{hhmm} 两笔空单都在价格逆向时全平，合亏 0.06 U，不是连续被硬止损扫出；不把 1m 回落当成顺势空")
    rt.watcher._remember("demo", BTC, "用户要求这个币只用 20 倍以上杠杆")
    notes = rt.watcher.memory("demo", BTC)
    assert len(notes) == 2 and notes[0]["note"].startswith("17:40") and "20 倍" in rt.watcher._memory_text("demo", BTC)
    assert len(distinct_notes([{"note": "a" * 30}, {"note": "b" * 30}])) == 2


async def test_worker_can_delegate_one_close_review(settings, fake):
    def run(messages, info):
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("add_trigger", {"inst_id": BTC, "when": "close_above", "bar": "15m",
                "level": 60_100, "do": "wake", "wake_note": "核对突破", "expires_minutes": 60, "reason": "等收盘"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "plan": None, "no_plan_reason": "cost"})])

    rt = make_rt(settings, fake, FunctionModel(run))
    await rt.watcher.review(BTC, "等收盘")
    rows = rt.store.strategies("demo", BTC)
    assert any(r["status"] == "armed" and r["params"].get("wake_note") == "核对突破" for r in rows)
    assert not fake.orders


async def test_procrastinating_tools_steer_into_real_limit_orders_and_shift_cleans_stale_orders(settings, fake):
    """防止光计划不挂单：复盘时拒绝用 deploy_strategy(pullback) 或 add_trigger(do=task) 套娃拖延，引导进 plan 直接挂欧易真实限价单；
    随后一旦结构反转把旧挂单甩在后方，_shift_watch 当场自动撤单。"""
    replies: list[str] = []

    def run(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if len(returns) == 0:
            return ModelResponse(parts=[ToolCallPart("add_trigger", {"inst_id": BTC, "when": "price_below", "level": 59_800, "do": "task", "task": "到时复核", "reason": "拖延"})])
        if len(returns) == 1:
            replies.append(str(returns[-1].content))
            return ModelResponse(parts=[ToolCallPart("deploy_strategy", {"inst_id": BTC, "kind": "pullback", "side": "long", "trigger": 59_700, "stop_loss": 59_200, "reason": "回踩"})])
        replies.append(str(returns[-1].content))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**REPORT, "plan": PULLBACK})])

    rt = make_rt(settings, fake, FunctionModel(run))
    record = await rt.watcher.review(BTC, "定时复盘")
    assert "[不用写复核触发器]" in replies[0] and "[请直接写进 plan 挂真实限价单]" in replies[1]
    assert len(fake.orders) == 1 and float(fake.orders[0]["px"]) == 59_700 and record["plan_order"]["status"] == "armed"

    # 走势发生向上强转折，把旧挂单 59700 远远甩在起动点后方：_shift_watch 自动撤掉欧易上的旧限价单并生成叫醒说明
    w, st = rt.watcher, rt.watcher._state(BTC)
    st.shift_ts = 100  # 已有基准时间戳
    st.last_px = 61_200
    fake.set_price(BTC, 61_200)
    w.strategies.metrics[BTC] = {
        "ts": 200, "values": {"atr": 80.0}, "levels": {}, "why": {},
        "shift": {"side": "long", "kind": "反转", "level": 60_500.0, "ts": 200, "bars_ago": 1, "origin": 59_800.0, "extreme": 61_200.0, "why": "收盘站上前高 60500"},
    }
    woke = await w._shift_watch(rt.config(), BTC, st)
    assert woke and "走势变了" in woke and "撤掉这些等不到的入场" in woke and not fake.orders


def test_auto_mode_picks_plays_by_market_regime():
    from ouy.agent.plan import market_regime

    frame = {"atr14": 1, "adx14": 30, "structure": "uptrend", "support": [{"price": 99}], "resistance": [{"price": 103}]}
    snap = {"last": 100, "orderbook": {"spread_pct": 0.01}, "timeframes": {"15m": frame}, "windows": {"4H": {"trend": "上涨（高点低点都在抬高）"}}}
    assert market_regime(snap, "intraday")["kind"] == "trend"
    rows = build_candidates(snap, style="intraday", min_rr=1.2)
    assert rows and all(r["plan"].direction == "long" and r["strategy"] in ("趋势回踩", "前高前低法") for r in rows)
    assert any(r["plan"].direction == "short" for r in build_candidates(snap, style="intraday", min_rr=1.2, quant_mode="all"))  # 不按行情过滤时才有逆势空单
    snap["timeframes"]["15m"] = {**frame, "structure": "downtrend"}  # 大方向向上、结构周期向下：打架
    assert market_regime(snap, "intraday")["kind"] == "mixed" and not build_candidates(snap, style="intraday", min_rr=1.2)
    assert "这轮不给入场候选" in candidates_text(snap, style="intraday", min_rr=1.2)
    shift = {"side": "short", "kind": "反转", "level": 100.5, "extreme": 99.0, "origin": 102.0, "bars_ago": 2, "why": "收盘跌破前低"}
    assert market_regime(snap, "intraday", shift)["kind"] == "reversal"


@pytest.mark.parametrize("structure,adx", [("unknown", 10), ("range", None), ("range", float("nan"))])
def test_missing_structure_is_not_a_tradeable_range(structure, adx):
    frame = {"structure": structure, "adx14": adx, "atr14": 1, "support": [{"price": 99}], "resistance": [{"price": 104}]}
    snap = {"last": 100, "timeframes": {"15m": frame}, "orderbook": {"spread_pct": 0.01}}
    assert not build_candidates(snap, style="intraday", min_rr=1.2)
    assert "数据不足" in candidates_text(snap, style="intraday", min_rr=1.2)
    # A measured ADX of zero with a real range is not a missing value.
    frame.update(structure="range", adx14=0)
    assert build_candidates(snap, style="intraday", min_rr=1.2)


def _losing_report(tmp_path, monkeypatch, name="区间高抛低吸"):
    from ouy import backtest_plans as bp

    monkeypatch.setattr(bp, "REPORT_DIR", tmp_path)
    rows = [bp.Result(ts=i * 60_000, strategy=name, align="震荡边缘", side="long", how="限价", r_net=-0.3, pct_net=-0.1, out="止损", inst=BTC, exit_ts=i * 60_000 + 1) for i in range(120)]
    bp.save_report("plans_intraday", "算法候选位 · 日内短线", rows, bp.groups(rows), {"kind": "plans"})
    return bp


async def test_losing_playbook_is_not_pushed_and_needs_an_extra_reason(rt, tmp_path, monkeypatch):
    """回测里机械照做显著亏的打法：候选照给，但程序不再催 Worker 做；Worker 要做必须写出额外依据，否则 no_edge 是合格结论。"""
    snap = {"last": 100, "timeframes": {"15m": {"structure": "range", "adx14": 13, "atr14": 1, "support": [{"price": 99}], "resistance": [{"price": 102}]}},
            "orderbook": {"spread_pct": 0.01}}
    params = dict(style="intraday", min_rr=1.2, entry_mode="limit", quant_bias="long")
    assert not build_candidates(snap, **params)[0]["losing"] and "优先核实" in candidates_text(snap, **params)  # 没有报告：没证据不等于亏

    bp = _losing_report(tmp_path, monkeypatch)
    assert "区间高抛低吸" in bp.losing("intraday") and bp.losing("swing") == set()
    rows = build_candidates(snap, **params)
    assert rows[0]["ok"] and rows[0]["losing"]  # 仍然给出来、排序不变，只打标记
    text = candidates_text(snap, **params)
    assert "no_edge" in text and "plan.edge" in text and "优先核实" not in text and "不是「会赚的位置」" in text

    cfg = rt.config().model_copy(update={"styles": {BTC: "intraday"}})
    bare = await rt.watcher._check_plan(cfg, BTC, TradePlan(**{**PLAN, "strategy": "区间高抛低吸"}))
    assert not bare.ok and "plan.edge" in "；".join(bare.issues)
    assert (await rt.watcher._check_plan(cfg, BTC, TradePlan(**{**PLAN, "strategy": "区间高抛低吸", "edge": "4H 刚收盘站上前高，资金费率 -0.03%"}))).ok
    assert (await rt.watcher._check_plan(cfg, BTC, TradePlan(**{**PLAN, "strategy": "趋势回踩"}))).ok  # 这条没有回测结论，不拦


async def test_algorithm_reads_the_market_on_its_own_clock_and_trades_are_booked_by_origin(rt, fake):
    """行情判断和候选位由程序按结构周期自己算，不等大模型复盘；每笔单记下是谁的主意，平仓后按来源另记一份战绩。"""
    from ouy.agent.plan import PlanCheck, plan_origin
    from ouy.models import TradeAction

    cfg, st = rt.config(), rt.watcher._state(BTC)
    st.last_px = 60_000
    await rt.watcher._algo_tick(cfg, BTC, st)
    assert st.regime and st.regime["kind"] in ("trend", "range", "reversal", "mixed", "unknown") and isinstance(st.regime["candidates"], list)
    first = st.regime["ts"]
    await rt.watcher._algo_tick(cfg, BTC, st)
    assert st.regime["ts"] == first  # 同一根结构周期 K 线里不重算
    st.reviewing = True
    st.regime["ts"] = 0
    await rt.watcher._algo_tick(cfg, BTC, st)
    assert st.regime["ts"] > 0  # Worker 正在复盘，算法仍按自己的时钟刷新

    # 行情判断和新出现的合格候选都记成算法事件（带中文名），页面和图上才看得到
    events = rt.store.algo_events("demo", BTC)
    assert [e["algo"] for e in events].count("regime") == 1 and "启动后第一次判断" in events[-1]["text"]
    assert {"regime", "cand"} <= set(rt.watcher.ALGOS)
    st.reviewing = False
    frame = {"atr14": 300, "adx14": 30, "structure": "uptrend", "support": [{"price": 59_700}], "resistance": [{"price": 60_900}]}
    snap = {"last": 60_000, "orderbook": {"spread_pct": 0.01}, "timeframes": {"15m": frame}, "windows": {"4H": {"trend": "上涨（高点低点都在抬高）"}}}
    st.regime = {"kind": "mixed", "side": "", "label": "方向打架", "candidates": []}
    rows = rt.watcher._set_regime(cfg, BTC, st, snap, None, None)
    new = [e for e in rt.store.algo_events("demo", BTC) if e["id"] > events[0]["id"]]
    assert any(e["algo"] == "regime" and e["text"].startswith("方向打架 → ") for e in new)
    assert len([e for e in new if e["algo"] == "cand"]) == len({(c["strategy"], c["plan"].direction) for c in rows if c["ok"]}) > 0
    assert all(e["side"] == "long" and "入场" in e["text"] for e in new if e["algo"] == "cand")
    rt.watcher._set_regime(cfg, BTC, st, snap, None, None)
    assert len(rt.store.algo_events("demo", BTC)) == len(events) + len(new)  # 没变化就不重复记

    cands = [{"side": "long", "entry": 100.0, "stop": 99.0}]
    plan = TradePlan(**{**PLAN, "entry_low": 100.1, "entry_high": 100.1, "stop_loss": 98.9, "take_profits": [103]})

    def chk(ref):
        return PlanCheck(ref=ref, how="pullback", limit=True, stop_pct=1.0, cost_pct=0.08, net_rr=2.0, issues=[])

    assert plan_origin(plan, chk(100.1), cands) == "照算法候选"
    assert plan_origin(plan, chk(100.8), cands) == "改过算法候选"
    assert plan_origin(plan, chk(103.0), cands) == "Worker 自拟" and plan_origin(plan, chk(100.1), []) == "Worker 自拟"
    assert plan_origin(plan.model_copy(update={"direction": "short"}), chk(100.1), cands) == "Worker 自拟"

    scores = rt.watcher.scores
    await scores.settle("demo", rt.trader, [BTC])
    opened = await rt.trader.execute(TradeAction(kind="open", inst_id=BTC, side="long", stop_loss=59_400, take_profit=61_200, leverage=3, risk_pct=0.5, reason="测试"), "demo", source="agent")
    assert opened.status == "executed"
    scores.tag("demo", BTC, "long", "趋势回踩", "计划现价进场", origin="改过算法候选")
    tags = rt.store.get("score_tags:demo")
    tags[-1]["ts"] -= 120_000
    rt.store.set("score_tags:demo", tags)
    fake.set_price(BTC, 61_300)
    done = await scores.settle("demo", rt.trader, [BTC])
    assert done[0]["origin"] == "改过算法候选" and scores.origins("demo")["改过算法候选"]["n"] == 1 and scores.stats("demo")["趋势回踩"]["wins"] == 1


async def test_waiting_review_keeps_market_recording_without_starting_another_review(rt, fake, monkeypatch):
    cfg = rt.config().model_copy(update={"instruments": [BTC], "intel_minutes": 0})
    rt.save_config(cfg)
    watcher = rt.watcher
    state = watcher._state(BTC)
    monkeypatch.setattr(watcher, "awaiting", lambda env, inst: [{"kind": "review"}])
    checked = []
    monkeypatch.setattr(watcher, "on_decided", lambda: checked.append(True))

    await watcher._tick(cfg)

    assert checked == [True]
    assert watcher.strategies.metrics[BTC]["ts"] > 0
    assert state.regime["ts"] > 0
    assert state.task is None and not fake.orders
