"""盯盘循环：每隔几秒看一眼行情和持仓，出现异动就叫醒 Agent 复盘；浮亏超限时不等 Agent 直接平仓。"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import math
import re
import time
from contextlib import asynccontextmanager
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic_ai import Agent, BinaryContent
from pydantic_ai.concurrency import ConcurrencyLimiter
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.usage import UsageLimits

from ..events import EventBus
from ..knowledge import KnowledgeBase
from ..indicators import BAR_MINUTES, ROW_BLOCK, day_profile, value_area
from ..market import Market, _f, session_anchor, us_stock_session
from ..backtest_plans import evidence, losing
from ..scoreboard import Scoreboard
from ..models import ENTRY_MODES, FAST_MODES, GIVEBACK, LOCK_R, QUANT_MODES, TREND_FRAMES, TREND_RULE, AccountState, AgentConfig, CycleReport, Position, TradeAction, STANCES, STYLES
from ..strategy import DATA_STALE_MS, FAST_DATA_MS, KINDS as STRATEGY_KINDS, StrategyEngine, describe as describe_strategy, label_of, rule_parts
from ..okx.gateway import Gateway, OkxError
from ..store import Store, now_ms
from ..memory import Lessons
from ..trader import MANUAL_SOURCES, PENDING_TTL_MS, Rejected, Trader
from .core import SUBAGENTS, Deps, build_review_agent, build_adhoc_agent, build_chat_agent, build_general_agent, build_gate_agent, build_router_agent, build_sub_agent, build_trader_agent
from .early_watch import QuoteWatch, ShiftRetest, WatchAdvice
from .signals import KIND as FAST_KIND, FastParams, describe_tape, detect, key_lines, line_status, strengths, structure_shift, turn_state, turn_watch
from .core import STABLE_MARK
from .plan import REGIME_LABEL, REGIME_PLAYS, RETRY_MINUTES, candidate_rows, major_direction, market_regime, build_candidates, candidates_text, check_plan, far_target, plan_origin, manage_rules, order_params, plan_terms, safe_leverage, same_plan, trade_cost_pct

log = logging.getLogger("ouy.watcher")

def _fail_text(exc: BaseException) -> tuple[str, str]:
    """一轮没跑完：被主动中断（停止盯盘、重启、被新任务取代）不算出错，说清楚没有因此下单。"""
    if isinstance(exc, asyncio.CancelledError):
        return "skip", "这一轮被中断了（停止盯盘、服务重启或被新任务取代），没有跑完，也没有因此下单"
    return "error", str(exc)[:300] or type(exc).__name__


def _partial_summary(args: str) -> str:
    """结论是结构化输出（JSON）一点点流出来的：从还没写完的 JSON 里取出 summary 这一段已经写出来的部分。"""
    m = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)', args)
    if not m:
        return ""
    raw = m.group(1)
    for cut in range(0, 7):  # 末尾可能停在转义符中间（\ 或 \uXXXX 没写完），往回退到能解析为止
        try:
            return str(json.loads(f'"{raw[: len(raw) - cut]}"'))
        except ValueError:
            continue
    return ""


class LiveText:
    """流式输出：Agent 边想边出字。把模型流出来的片段攒起来，隔一小会儿往界面推一次当前写到哪了。"""

    def __init__(self, publish: Any, agent: str, inst_id: str):
        self.publish, self.agent, self.inst_id = publish, agent, inst_id
        self.text, self.thinking, self.args = "", "", ""
        self.sent = 0.0

    def feed(self, event: Any) -> None:
        part = getattr(event, "part", None)
        delta = getattr(event, "delta", None)
        if part is not None and getattr(event, "event_kind", "") == "part_start":
            kind = getattr(part, "part_kind", "")
            if kind == "text":
                self.text, self.args = str(part.content or ""), ""
            elif kind == "thinking":
                self.thinking = str(part.content or "")
            elif kind == "tool-call":
                self.args = part.args if isinstance(part.args, str) else ""
        elif delta is not None:
            kind = getattr(delta, "part_delta_kind", "")
            if kind == "text":
                self.text += delta.content_delta or ""
            elif kind == "thinking":
                self.thinking += delta.content_delta or ""
            elif kind == "tool_call" and isinstance(delta.args_delta, str):
                self.args += delta.args_delta
                summary = _partial_summary(self.args)
                if summary:
                    self.text = summary
        else:
            return
        if time.time() - self.sent >= 0.12:
            self.flush()

    def flush(self, done: bool = False) -> None:
        self.sent = time.time()
        if done or self.text or self.thinking:
            self.publish("stream", {"agent": self.agent, "inst_id": self.inst_id, "text": self.text, "thinking": self.thinking[-600:], "done": done})


def _can_stream(agent: Any) -> bool:
    """测试用的假模型不会流式输出；真模型都走流式。"""
    model = getattr(agent, "model", None)
    return not (type(model).__name__ == "FunctionModel" and getattr(model, "stream_function", None) is None)


def coins_in(message: str, instruments: list[str]) -> tuple[list[str], list[str]]:
    """用户这句话里 @ 了哪些币、点名了哪些币（都只认盯盘列表里的）。返回 (被 @ 的, 提到的)。"""
    by_coin = {inst.split("-")[0].upper(): inst for inst in instruments}
    at = list(dict.fromkeys(by_coin[m.upper()] for m in re.findall(r"[@＠]([A-Za-z0-9]+)", message) if m.upper() in by_coin))
    named = [inst for coin, inst in by_coin.items() if re.search(rf"(?<![A-Za-z0-9]){re.escape(coin)}(?![A-Za-z0-9])", message, re.I)]
    return at, named


REVIEW_TIMEOUT = 300
GENERAL = "通用 Worker"
ADHOC_KEEP = 3  # 一个 Worker 手下最多留着几个子代理
ADHOC_IDLE_MS = 30 * 60_000  # 留着的子代理空闲这么久就自动关掉
ADHOC_PARALLEL = 3  # 一个币同时最多几个临时子代理在跑
FAST_SECONDS = 4  # 快判每隔几秒看一次 1 分钟 K 线
FAST_COOLDOWN = 300  # 同一个方向的快判信号，两次之间至少隔这么多秒
SAME_NOTE = 0.45  # 两条工作记忆的文字相似到这个程度就算说的是同一件事
ADHOC_PER_10MIN = 6  # 一个币 10 分钟内最多开几个，免得空烧 token
MODE_LABEL = {"advise": "只建议不下单", "confirm": "下单前等用户确认", "auto": "全自动执行"}


def _trend_text(style: str, snapshot: dict[str, Any]) -> str:
    """程序先把大方向算好告诉 Worker：按风格看哪两段时间的高低点对比，顺势是做多还是做空。"""
    windows = snapshot.get("windows") or {}
    seen = {name: str(w.get("trend", "")) for name, w in windows.items() if w.get("trend")}
    major, name = major_direction(snapshot, style)
    if major:
        side = f"大方向向上（按 {name}）：做多是顺势，做空是逆势" if major == "long" else f"大方向向下（按 {name}）：做空是顺势，做多是逆势"
    else:
        side = "大方向不明，不等于已确认区间；以结构周期的行情判断为准" if seen else "大方向还算不出来（K 线不够），自己用 get_candles 看更长一段再定"
    detail = "；".join(f"{name} {word}" for name, word in seen.items())
    return f"【大方向】{side}。" + (f"各时间段：{detail}。" if detail else "") + f"\n{TREND_RULE}"


@dataclass
class InstState:
    opening_quote: dict[str, Any] = field(default_factory=dict)
    regime: dict[str, Any] = field(default_factory=dict)
    shift_retest: ShiftRetest = field(default_factory=ShiftRetest)
    last_retest: dict[str, Any] | None = None
    quote_watch: QuoteWatch = field(default_factory=QuoteWatch)
    observe_task: asyncio.Task[None] | None = field(default=None, repr=False)
    observe_price: float | None = None
    pending_observation: tuple[float, str] | None = None
    last_px: float = 0.0
    prev_px: float = 0.0
    last_review_ts: float = 0.0
    last_review_px: float = 0.0
    next_due_ts: float = 0.0
    had_position: bool | None = None
    reviewing: bool = False
    last_error: str = ""
    last_reason: str = ""
    report: dict[str, Any] | None = None
    task: asyncio.Task[Any] | None = field(default=None, repr=False)
    review_started: float = 0.0
    intel_ts: float = 0.0
    intel_running: bool = False
    sub_running: set[str] = field(default_factory=set)  # 正在干活的子代理（intel / smart / risk）
    adhoc: list[dict[str, Any]] = field(default_factory=list)  # Worker 临时开的子代理（最近几个）：{id, name, task, running, summary, bias, ts}
    skips: int = 0  # Router 连续几次判断「不值得唤醒」
    last_gate: str = ""
    fails: int = 0  # 连续复盘失败次数，用来拉长重试间隔
    illiquid: str = ""  # 模拟盘里这个币流动性太差时的说明
    stale_order: str = ""  # Agent 的挂单太久没成交时的说明
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)  # 这个币的 Worker 一次只做一件事，别的排队
    has_mail: bool = False  # 信箱里有没有还没读的留言
    lot_alert: str = ""  # 有一笔仓的盈利在回吐（或浮亏在扩大），等着送到 Worker 手上
    # 超短线的行情推送：程序记着每根 5 分钟 K 线和关键线，有变化就送到 Worker 手上
    feed_bar_ts: int = 0
    feed_checked: float = 0.0
    judged: dict[str, Any] | None = None  # 最近一根收盘 K 线的打分
    data_log: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=60))  # 程序每根 K 线记的一笔数据
    area_bars: tuple[int, list[Any]] = (0, [])  # (哪一天取的, 15 分钟 K 线)：价值区间按上一个 UTC 日算，一天只取一次
    steered: bool = False  # 这一轮跑到一半时有人把话直接送了进来：结论一定要发回对话，不能悄悄结束
    shift_ts: int = 0  # 上次看到的走势变化发生在哪一根 K 线：同一次只处理一次
    fast_checked: float = 0.0  # 快判上次看 1 分钟 K 线的时间
    fast_bar_ts: int = 0  # 快判上次出信号的那根 K 线：同一根只处理一次
    fast_at: dict[str, float] = field(default_factory=dict)  # 每个方向上次出手 / 叫醒 Worker 的时间
    fast_last: dict[str, Any] | None = None  # 最近一次快判信号和程序怎么处理的（给界面和 Worker 看）
    kb_picks: dict[tuple[str, ...], tuple[float, list[Any]]] = field(default_factory=dict)  # 行情状态对应的查询 → (什么时候查的, 查到的条目)
    prompt_stats: dict[str, int] = field(default_factory=dict)  # 最近一次复盘的提示词有多长、前面不变的那段有多长（查缓存命中用）
    goal_checked: float = 0.0  # 上次核对目标盈亏的时间
    goal_realized: float = 0.0  # 目标开始以来这个币已实现的盈亏
    waiting: int = 0  # 排在后面等这个 Worker 的事有几件
    busy: str = ""  # Worker 现在在做什么
    run: Any = field(default=None, repr=False)  # 正在跑的那一轮（PydanticAI 的 AgentRun），用来插话


def _global_risk(cfg: AgentConfig, account: AccountState) -> str:
    """Router 和每个 Worker 都能看到的全局风险：总敞口、同方向扎堆、当日亏损还剩多少空间。"""
    r = cfg.risk
    total = sum(p.notional_usd for p in account.positions)
    cap = account.equity * r.max_total_notional_x
    longs = [p.inst_id.split("-")[0] for p in account.positions if p.side == "long"]
    shorts = [p.inst_id.split("-")[0] for p in account.positions if p.side == "short"]
    text = f"【全局风险】全部持仓名义价值 {total:,.0f} USDT" + (f"，上限 {cap:,.0f}（已用 {total / cap * 100:.0f}%）" if cap else f"（权益的 {total / account.equity:.1f} 倍）" if account.equity else "")
    if account.day_pnl_pct is not None:
        text += f"；今日盈亏 {account.day_pnl_pct:+.2f}%，当日亏损上限 {r.max_daily_loss_pct:g}%"
    for label, names in (("多", longs), ("空", shorts)):
        if len(names) >= 2:
            text += f"；{'、'.join(names)} 同时做{label}：相关的品种（加密货币之间、同一板块的股票之间）大多一起动，同方向的几笔等于叠加同一个风险，新开同方向的仓要更小"
    return text


def _losing_streak(closed: Any) -> str:
    """这个币最近连续亏了几笔。只是提醒 Worker 找原因，不限制交易。"""
    rows = sorted(closed or [], key=lambda t: t.closed_ts, reverse=True)
    lost = []
    for t in rows:
        if t.pnl >= 0:
            break
        lost.append(t)
    if len(lost) < 2:
        return ""
    return (
        f"【连续亏损】这个币最近连续亏了 {len(lost)} 笔，共 {sum(t.pnl for t in lost):+.2f} USDT。"
        "对照上面的平仓记录找出共同原因（方向做反、入场太早、止损太近被扫、刚进场就手动平掉只亏了手续费），下一笔在入场位置和止损位置上改过来。"
        "原因已经在工作记忆里的不用再记一遍；亏过不是不再出手的理由，照常按【出手尺度】给计划。"
    )


def distinct_notes(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """工作记忆去重：两条说的是同一件事（文字大半相同）就只留后写的那一条。"""
    out: list[dict[str, Any]] = []
    for n in notes:
        out = [o for o in out if difflib.SequenceMatcher(None, o["note"], n["note"]).ratio() < SAME_NOTE]
        out.append(n)
    return out


def quiet_reason(st: InstState, wake_move_pct: float) -> str:
    """Fast Path：软事件到来时，用规则判断是不是明显没事（价格几乎没动、离所有关键价位都远）。返回空串表示要继续往下判断。"""
    report = st.report or {}
    if not st.last_px or not st.last_review_px:
        return ""
    move = abs(st.last_px / st.last_review_px - 1) * 100
    if move >= wake_move_pct / 2:
        return ""
    levels = [float(l.get("price") or 0) for l in report.get("watch_levels") or []]
    plan = report.get("plan") or {}
    levels += [float(plan.get(k) or 0) for k in ("entry_low", "entry_high")]
    gaps = [abs(st.last_px / p - 1) * 100 for p in levels if p > 0]
    near = min(gaps) if gaps else None
    if near is not None and near < max(wake_move_pct / 2, 0.15):
        return ""
    where = f"，离最近的关键价位还有 {near:.2f}%" if near is not None else ""
    return f"规则过滤：距上次复盘价格只动了 {move:.2f}%{where}，没有持仓，不用叫醒 Worker"


def demo_liquidity(snapshot: dict[str, Any]) -> str:
    """模拟盘里这个币能不能测出效果：价差过大，或者波动远小于真实市场（K 线走平、几乎没成交）。"""
    spread = (snapshot.get("orderbook") or {}).get("spread_pct")
    problems = []
    if spread is not None and spread >= 0.1:
        problems.append(f"买卖价差 {spread:.3g}%")
    real = (snapshot.get("real_market") or {}).get("timeframes") or {}
    for bar, frame in (snapshot.get("timeframes") or {}).items():
        mine, theirs = frame.get("atr_pct"), (real.get(bar) or {}).get("atr_pct")
        if mine is not None and theirs and mine < theirs * 0.3:
            problems.append(f"{bar} 波动只有真实市场的 {mine / theirs * 100:.0f}%")
            break
    return "、".join(problems)


def kb_queries(snapshot: dict[str, Any], pos: Position | None, reason: str, quant: dict[str, Any] | None = None) -> list[str]:
    """把当前行情和持仓状态翻译成知识库查询。quant 是程序算好的量化读数（走势变化等），有就用它把查询对准眼前这个局面。"""
    frames = snapshot.get("timeframes", {})
    # 快照里的周期从小到大排列：最大的两个用来判断行情状态（日内短线是 4H / 1H，超短线是 1H / 15m，波段是 1D / 4H）
    ordered = list(frames.values())
    h4 = frames.get("4H") or (ordered[-1] if ordered else {})
    h1 = frames.get("1H") or (ordered[-2] if len(ordered) > 1 else {})
    if "4H" not in frames and len(ordered) > 1:
        h4, h1 = ordered[-1], ordered[-2]
    adx = h1.get("adx14") or 0
    struct = h4.get("structure")
    queries: list[str] = []
    if adx > 25 and struct in ("uptrend", "downtrend"):
        word = "上升趋势" if struct == "uptrend" else "下降趋势"
        queries.append(f"趋势行情 {word} 趋势回踩 EMA20 EMA50 顺势入场条件 止损 止盈")
        queries.append("趋势里的回调 二次入场 两段式回调 反转尝试多数失败")
    elif adx and adx < 20:
        queries.append("震荡行情 区间高抛低吸 区间边缘 入场条件 止损 止盈")
        queries.append("价值区间 POC VAH VAL 平衡 回到区间内 做两头")
    else:
        queries.append("行情状态识别 趋势还是震荡 状态不明 多周期配合")
    # 程序已经算出来的局面：走势刚变、刺穿后收回、接受突破——各自对准那一条打法
    shift = (quant or {}).get("shift")
    if shift and shift.get("bars_ago", 99) <= 12:
        queries.append("变盘后回踩 走势转折 被越过的摆动点 第一次回踩 不追")
    reads = " ".join(str(w.get("now") or "") for w in (snapshot.get("windows") or {}).values() if isinstance(w, dict))
    if "假突破" in reads or "假跌破" in reads:
        queries.append("假突破反转 刺穿前高前低后收回 扫止损 海龟汤 止损放在影线外")
    elif "接受向" in reads:
        queries.append("接受突破 连续两根收在外面 突破回踩确认 前高前低法")
    if snapshot.get("asset_class") == "stock":
        queries.append("股票永续 美股时段 休市 跳空 财报 开盘区间突破 不跟 BTC")
    last, atr = snapshot.get("last") or 0, h1.get("atr14") or 0
    if atr and last:
        for edge in (h1.get("high_20"), h1.get("low_20")):
            if edge and abs(last - edge) <= atr * 0.5:
                queries.append("突破回踩确认 假突破识别 放量 区间边界")
                break
    width = h1.get("bb_width_pct")
    atr_pct = h1.get("atr_pct") or 0
    if width and atr_pct and width < atr_pct * 3:
        queries.append("布林带收口 波动率压缩 收敛突破 等待方向选择")
    deriv = snapshot.get("derivatives") or {}
    funding = abs(deriv.get("funding_rate_8h_pct", deriv.get("funding_rate_pct")) or 0)  # 折成 8 小时再比
    if funding >= 0.03:
        queries.append("资金费率极端 多头拥挤 空头拥挤 持仓量")
    if pos:
        if pos.upl > 0:
            queries.append("持仓浮盈 止损移保本 跟踪止损 分批止盈 金字塔加仓时机")
        else:
            queries.append("持仓浮亏 判断是否被证伪 逆向异动 减仓还是平仓")
    else:
        queries.append("开仓前检查 入场区间 止损怎么放 杠杆怎么选 盈亏比")
    if reason:
        queries.append(reason)
    return queries


def _trim_history(history: list[ModelMessage], keep: int = 40) -> list[ModelMessage]:
    """只保留最近的对话，并且从一条用户消息开始切，避免把工具调用和它的结果拆散。"""
    if len(history) <= keep:
        return history
    for i in range(len(history) - keep, len(history)):
        msg = history[i]
        if isinstance(msg, ModelRequest) and any(isinstance(p, UserPromptPart) for p in msg.parts):
            return history[i:]
    return []


_TOOL_LABEL = {
    "market_snapshot": "看行情", "account_state": "看账户和持仓", "trade_history": "查历史盈亏", "search_knowledge": "查知识库",
    "okx_list_tools": "查欧易功能列表", "okx_query": "查欧易数据", "open_position": "开仓", "add_to_position": "加仓",
    "reduce_position": "减仓", "close_position": "平仓", "move_stops": "改止盈止损", "set_leverage": "调杠杆", "cancel_pending_orders": "撤单",
    "write_lesson": "记经验", "judge_lesson": "验证经验", "read_skill": "翻技能", "install_skill": "装技能", "delete_skill": "删技能",
    "ask_worker": "交给 Worker", "ask_intel": "让情报 Worker 去查", "intel_query": "查情报", "workers_board": "看 Worker 总览", "set_worker": "设置 Worker", "get_candles": "看 K 线走势", "stop_watching": "按交代的条件停下", "edit_trigger": "修改触发器", "set_recorder": "设记录器", "add_trigger": "写触发器", "write_strategy": "写策略", "delete_strategy": "删策略", "spawn_subagent": "开子代理", "close_subagent": "关掉子代理", "close_helper": "关掉子代理", "ask_general": "交给通用 Worker", "spawn_helper": "开子代理", "remember": "记入工作记忆", "ask_subagent": "派子代理", "deploy_strategy": "布置策略", "tell_worker": "对 Worker 插话", "assign_goal": "给 Worker 定目标", "cancel_strategy": "撤销策略",
    "app_status": "看项目设置", "show_instrument": "切换图表", "edit_watchlist": "改盯盘列表", "set_watching": "启停盯盘",
    "set_trading_style": "改交易风格", "set_preference": "记下你的偏好", "set_permission": "改权限", "update_risk": "改风控参数", "open_order_window": "打开下单窗",
    "set_quant_mode": "切换量化档位", "read_strategy": "读策略手册",
}


def _tool_steps(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    """把这一轮对话里 Agent 调用过的工具整理出来，给界面展示「它做了什么、查到了什么」。"""
    steps: dict[str, dict[str, Any]] = {}
    for msg in messages:
        for part in msg.parts:
            kind = getattr(part, "part_kind", "")
            call_id = getattr(part, "tool_call_id", None)
            if kind == "tool-call" and call_id:
                args = part.args_as_dict() if hasattr(part, "args_as_dict") else {}
                steps[call_id] = {"tool": part.tool_name, "label": _TOOL_LABEL.get(part.tool_name, part.tool_name), "args": args, "result": ""}
            elif kind == "tool-return" and call_id in steps:
                content = part.content if isinstance(part.content, str) else json.dumps(part.content, ensure_ascii=False, default=str)
                steps[call_id]["result"] = content[:1500]
                steps[call_id]["ok"] = not content.startswith(("[被风控拒绝]", "[执行失败]", "调用失败", "获取"))
            elif kind == "retry-prompt" and call_id in steps:
                steps[call_id]["result"] = str(getattr(part, "content", ""))[:600]
                steps[call_id]["ok"] = False
    return [s for s in steps.values() if s["tool"] != "final_result"]


def _permissions_text(cfg: AgentConfig, inst_id: str | None = None) -> str:
    label = {"auto": "可直接执行", "confirm": "需用户确认后才执行", "off": "不允许（只能提建议）"}
    names = {"open": "开仓", "add": "加仓", "set_leverage": "调杠杆", "set_stops": "移止盈止损", "reduce": "减仓", "close": "平仓", "cancel_orders": "撤单"}
    return "你的权限：" + "，".join(f"{name}{label[cfg.permission_for(kind, inst_id)]}" for kind, name in names.items())


def _effort(level: str, key: str) -> ModelSettings:
    """Keep routing stable for a prompt family, including across approval/resume."""
    base = {"openai_prompt_cache_key": key}
    return ModelSettings(**base) if level == "default" else ModelSettings(thinking=level, **base)  # type: ignore[typeddict-item]


def _worker_cache_key(kind: str, inst_id: str) -> str:
    # Preserve the existing review key rather than cold-starting every Worker.
    return f"ouy-{inst_id}" if kind == "review" else f"ouy-task-{inst_id}"


USAGE_LOG = 500  # 每个环境留最近多少轮复盘的缓存读数


def cache_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把最近若干轮复盘的真实读数汇成命中率：整轮（含工具往返）和首请求分开算，首请求的命中率才反映提示词排得好不好。"""
    def one(part: list[dict[str, Any]]) -> dict[str, Any]:
        total, cached = sum(r["input_tokens"] for r in part), sum(r["cached_tokens"] for r in part)
        first_in = first_read = first_measured_reviews = 0
        for row in part:
            first_cached = row.get("first_cached_tokens")
            first_input = row.get("first_input_tokens")
            if "first_input_tokens" not in row and row["model_requests"] == 1:
                first_input = row["input_tokens"]  # Legacy single-request reviews have an exact denominator.
            if first_input and first_cached is not None:
                first_in += first_input
                first_read += first_cached
                first_measured_reviews += 1
        stable = [r["stable_chars"] / r["prompt_chars"] for r in part if r.get("prompt_chars")]
        return {
            "reviews": len(part), "input_tokens": total, "cached_tokens": cached, "cache_write_tokens": sum(r.get("cache_write_tokens") or 0 for r in part), "hit_pct": round(cached / total * 100, 1) if total else None,
            "first_hit_pct": round(first_read / first_in * 100, 1) if first_in else None,
            "first_measured_reviews": first_measured_reviews,
            "stable_prompt_pct": round(sum(stable) / len(stable) * 100, 1) if stable else None,
            "unmeasured_requests": sum(r.get("unmeasured_requests") or 0 for r in part),
        }
    return {**one(rows), "instruments": {inst: one([r for r in rows if r["inst_id"] == inst]) for inst in sorted({r["inst_id"] for r in rows})},
            "recent": rows[-60:]}


def _cache_usage(messages: list[ModelMessage]) -> dict[str, Any]:
    """Review history includes pre-approval responses; do not count tool messages as requests."""
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    first = responses[0].usage if responses else None
    if first is not None and (first.unmeasured_requests or first.input_tokens <= 0):
        first = None
    return {
        "model": responses[0].model_name if responses else None,
        "input_tokens": sum(m.usage.input_tokens for m in responses),
        "output_tokens": sum(m.usage.output_tokens for m in responses),
        "cached_tokens": sum(m.usage.cache_read_tokens for m in responses),
        "cache_write_tokens": sum(m.usage.cache_write_tokens for m in responses),
        "model_requests": len(responses),
        "unmeasured_requests": sum(m.usage.unmeasured_requests for m in responses),
        "first_input_tokens": first.input_tokens if first is not None else None,
        "first_cached_tokens": first.cache_read_tokens if first is not None else None,
    }


# 提示词里各段按「多久变一次」排：越不变的越靠前。模型服务的提示缓存只认从头开始一字不差的那一段，
# 报价、时间、叫醒原因这些每轮都变的内容一旦排在前面，后面再稳定的内容也用不上缓存。没列在这里的段落都算「每轮都变」，保持原来的先后。
CACHE_TIERS: tuple[tuple[str, ...], ...] = (
    # 所有品种都一样的说明
    ("【什么时候停】", "【临时子代理】", "【你能派的子代理】", "【子代理】", "【计划不会自动执行】", "【现在是模拟盘】", "【已装技能】", "【用户偏好", "【用户交代】"),
    # 这个品种的设置：用户改设置才变
    ("【品种】", "【这个品种的交易风格】", "【出手尺度】", "【入场方式】", "【运行设置】", "【这是股票永续", "【这不是加密货币】"),
    # 平仓、写策略、沉淀经验之后才变
    ("【策略库】", "【各打法的战绩】", "【长期经验"),
    # 下面几段是行情里「只在 K 线收盘时才变」的部分，按多久变一次分开（见 _split_closed）：最快一小时变一次的
    ("【已收盘数据·大周期",),
    # 最快 15 分钟变一次的
    ("【已收盘数据·中周期",),
    # 最快 5 分钟变一次的：一分钟复盘一次时，五轮里有四轮一字不差
    ("【已收盘数据·小周期",),
    # 检索结果也会随实时价格、持仓状态切换，放在收盘数据之后
    ("【知识库相关条目】",),
)
# 这几层的末尾各放一条分隔行：发请求时分隔行前面的每一段拆成一条 developer 消息（见 core.lead_instructions）。
# 服务端只在开头连续的 developer 消息末尾命中缓存，所以一段变了只丢它自己和后面的，前面几段照样命中。
STABLE_BREAK_AFTER = (1, 2, 3, 4, 5, 6)


def _cache_tier(block: str) -> int:
    return next((i for i, tags in enumerate(CACHE_TIERS) if block.startswith(tags)), len(CACHE_TIERS))


def _split_review_chart(snapshot: dict[str, Any]) -> tuple[dict, dict]:
    """Render slow, closed history first. The live tail keeps all other fields and unconfirmed rows."""
    frames = snapshot.get("timeframes") or {}
    history, live = {}, dict(frames)
    for bar in sorted(frames, key=lambda b: -BAR_MINUTES.get(b, 0)):
        frame = frames[bar]
        rows = frame.get("candles")
        if not rows or not isinstance(frame.get("latest_bar_confirmed"), bool):
            continue
        count = len(rows) if frame["latest_bar_confirmed"] else len(rows) - 1
        count = min(count, frame.get("stable_rows") or count)  # 整块对齐的那一段；块后面新收的几根由 _split_closed 另放
        cut = next((i for i in range(count) if "(未收)" in str(rows[i][0])), count)
        if cut:
            history[bar] = rows[:cut]
            live[bar] = {**frame, "candles": rows[cut:]}
    return history, {**snapshot, "timeframes": live}


# 一个周期的快照里随报价变的字段；其余（均线、RSI、MACD、布林、ADX、结构、摆动点……）只用已收盘 K 线算，该周期收盘时才变
LIVE_FRAME_KEYS = frozenset({"last", "latest_bar_confirmed", "candles", "range", "support", "resistance", "patterns", "stable_rows", "insufficient"})
# 前高前低（windows）里随小 K 线收盘变的两项；其余是上一段的高低点，那一段走完才变
LIVE_WINDOW_KEYS = frozenset({"now", "plan"})
# 价值区间里昨天就定下来的几项；其余是现价相对它的位置
FIXED_AREA_KEYS = frozenset({"poc", "vah", "val", "day_high", "day_low", "open"})
# 已收盘数据按「最快多少分钟变一次」分三段，各自单独一条消息；比这还快的留在行情快照里
CLOSED_BUCKETS: tuple[tuple[str, int], ...] = (("大周期", 60), ("中周期", 15), ("小周期", 5))


def _bucket(period_minutes: float) -> int:
    return next((i for i, (_, floor) in enumerate(CLOSED_BUCKETS) if period_minutes >= floor), len(CLOSED_BUCKETS))


def _split_closed(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把行情快照里只在 K 线收盘时才变的内容拿出来，按多久变一次分到 CLOSED_BUCKETS 的几段里；随报价变的留下。
    - 整块对齐的已收盘 K 线：一个周期走完 ROW_BLOCK 根才变；块后面新收的几根（new_candles）：该周期收一根才变；
    - 一个周期的指标（均线、RSI、MACD、ADX、结构……）：只用已收盘 K 线算，该周期收一根才变；
    - 前高前低的上一段高低点：那一段走完才变；「现在怎么样 / 怎么挂」跟着最小周期的收盘变；
    - 价值区间的 POC / VAH / VAL：按上一个 UTC 日算，一天变一次。
    返回（各段的内容，剩下的行情快照）。只是换了摆放的位置：每个数都还在，也都还是这一轮现算的。"""
    history, live = _split_review_chart(snapshot)
    buckets: list[dict[str, Any]] = [{} for _ in CLOSED_BUCKETS]

    def put(period: float, section: str, key: str, value: dict[str, Any]) -> bool:
        i = _bucket(period)
        if not value or i >= len(buckets):
            return False
        buckets[i].setdefault(section, {}).setdefault(key, {}).update(value)
        return True

    frames: dict[str, Any] = {}
    for bar, frame in (live.get("timeframes") or {}).items():
        minutes = BAR_MINUTES.get(bar, 0)
        if not isinstance(frame, dict) or frame.get("insufficient"):
            frames[bar] = frame
            continue
        rows = history.get(bar)
        if rows and not put(minutes * (ROW_BLOCK if frame.get("stable_rows") else 1), "timeframes", bar, {"candles": rows}):
            frame = {**frame, "candles": [*rows, *frame.get("candles", [])]}
        recent = frame.get("candles") or []
        cut = next((i for i, row in enumerate(recent) if "(未收)" in str(row[0])), len(recent))
        if cut and put(minutes, "timeframes", bar, {"new_candles": recent[:cut]}):  # 块后面新收的几根：和这个周期的指标一样，收一根才变
            frame = {**frame, "candles": recent[cut:]}
        if put(minutes, "timeframes", bar, {k: v for k, v in frame.items() if k not in LIVE_FRAME_KEYS}):
            frame = {k: v for k, v in frame.items() if k in LIVE_FRAME_KEYS}
        frames[bar] = frame
    live = {**live, "timeframes": frames}
    smallest = min((BAR_MINUTES.get(bar, 0) for bar in frames), default=0)
    windows: dict[str, Any] = {}
    for name, window in (live.get("windows") or {}).items():
        if not isinstance(window, dict):
            windows[name] = window
            continue
        levels = {k: v for k, v in window.items() if k not in LIVE_WINDOW_KEYS}
        moving = {k: v for k, v in window.items() if k in LIVE_WINDOW_KEYS}
        rest = {**({} if put(BAR_MINUTES.get(name, 0), "windows", name, levels) else levels), **({} if put(smallest, "windows", name, moving) else moving)}
        if rest:
            windows[name] = rest
    if "windows" in live:
        live["windows"] = windows
    area = live.get("value_area")
    if isinstance(area, dict) and put(1440, "value_area", "prev_day", {k: v for k, v in area.items() if k in FIXED_AREA_KEYS}):
        live["value_area"] = {k: v for k, v in area.items() if k not in FIXED_AREA_KEYS}
    return buckets, live


KB_HOLD_SECONDS = 1800  # 同一种行情状态查到的知识库条目最多沿用这么久


BTC = "BTC-USDT-SWAP"
BTC_ALERT_PCT = 0.8  # BTC 在 BTC_WINDOW 秒内波动超过这个百分比，就叫醒 Agent 检查所有持仓
BTC_WINDOW = 900


class Watcher:
    def __init__(
        self,
        *,
        gateway: Gateway,
        market: Market,
        trader: Trader,
        store: Store,
        bus: EventBus,
        kb: KnowledgeBase,
        get_config: Callable[[], AgentConfig],
        model_factory: Callable[[], Model | str],
    ):
        self.gw, self.market, self.trader, self.store, self.bus, self.kb = gateway, market, trader, store, bus, kb
        self.get_config = get_config
        self.model_factory = model_factory
        self.lessons = Lessons(store)  # 长期经验：模拟盘 / 实盘各一套，用户定的规矩共用
        self._review_agent: Any = None
        self._review_fails: dict[str, int] = {}
        self.skills: Any = None  # 已装的技能（Runtime 启动时接上：项目里的 skills 文件夹）
        self.proxy_url: Callable[[], str | None] = lambda: None
        self.strategies = StrategyEngine(store, trader, self._emit)  # 快的一层：按规则自动执行 Agent 布置的策略，不调用模型
        self.strategies.on_task = self._trigger_task
        self.scores = self.strategies.scores = Scoreboard(store)  # 按打法记战绩：进场时打标签，平仓后把盈亏记到那条打法头上
        self._scored_at = 0.0
        self._algo_checked = 0.0
        self._pruned_at = 0.0
        self._trigger_jobs: set[asyncio.Task[Any]] = set()
        self.running = False
        self.states: dict[str, InstState] = {}
        self.router_adhoc: list[dict[str, Any]] = []  # Router 临时开的子代理（最近几个）
        self._sub_hist: dict[tuple[str, str], Any] = {}  # 留着的子代理记得的对话：(归属, 名字) → 消息历史
        self._spawn_times: dict[str, list[int]] = {}  # 每个归属最近 10 分钟开过几次子代理
        self.general: dict[str, Any] = {"busy": False, "task": "", "summary": "", "ts": 0}  # 通用 Worker：不绑定某个币
        self.last_error = ""
        self._task: asyncio.Task[None] | None = None
        self._models: dict[str, Model | str] = {}
        self._manual_warned: dict[str, float] = {}
        self._worker_agents: dict[str, Agent[Deps, CycleReport]] = {}
        self._task_agents: dict[str, Agent[Deps, str]] = {}
        self._router_agent: Agent[Deps, str] | None = None
        self._gate_agent: Any = None
        self._beat = 0.0
        self._watchdog_task: asyncio.Task[None] | None = None
        self.watchdog: dict[str, Any] = {"restarts": 0, "last": ""}
        self._chat_histories: dict[str, list[ModelMessage]] = {}
        self.control: Any = None  # 由 Runtime 注入：让对话里的 Agent 能改项目设置、启停盯盘
        self._btc_ref: tuple[float, float] | None = None  # (参考价, 记录时间)
        self._asset: dict[str, str] = {}  # 每个品种是什么资产（crypto / stock / commodity…）：只有加密货币才跟 BTC 大盘
        self._limiters: dict[tuple[str, int], ConcurrencyLimiter] = {}
        self._resumes: set[asyncio.Task[None]] = set()  # 用户决定之后接着跑的那些任务

    # ---------- 生命周期 ----------

    def _model(self, name: str = "") -> Model | str:
        """Router 和每个 Worker 可以各用各的模型；名字留空就是 .env 里的默认模型。"""
        if name not in self._models:
            try:
                self._models[name] = self.model_factory(name or None)
            except TypeError:  # 只给了一个固定模型的工厂（测试里常见）
                self._models[name] = self.model_factory()
        return self._models[name]

    def _limiter(self, cfg: AgentConfig, pool: str) -> ConcurrencyLimiter:
        """并发和排队用 PydanticAI 自带的限制器。Worker 一组名额；子代理另一组（是它的 2 倍），
        这样组长占着名额等子代理回来时，子代理不会因为没有名额而和组长互相卡死。"""
        size = cfg.max_parallel * (2 if pool == "subagents" else 1)
        if (pool, size) not in self._limiters:
            self._limiters[(pool, size)] = ConcurrencyLimiter(size, name=f"ouy-{pool}")
        return self._limiters[(pool, size)]

    def _worker_agent(self, cfg: AgentConfig, inst_id: str) -> Agent[Deps, CycleReport]:
        key = f"{cfg.model_for(inst_id)}|{cfg.max_parallel}"
        if key not in self._worker_agents:
            self._worker_agents[key] = build_trader_agent(self._model(cfg.model_for(inst_id)), self.kb, self._limiter(cfg, "workers"))
        return self._worker_agents[key]

    def _task_agent(self, cfg: AgentConfig, inst_id: str) -> Agent[Deps, str]:
        key = f"{cfg.model_for(inst_id)}|{cfg.max_parallel}"
        if key not in self._task_agents:
            self._task_agents[key] = build_chat_agent(self._model(cfg.model_for(inst_id)), self.kb, self._limiter(cfg, "workers"))
        return self._task_agents[key]

    @asynccontextmanager
    async def _turn(self, cfg: AgentConfig, inst_id: str, what: str):
        """轮到这个币的 Worker 做这件事。它正在忙就排队（先来先做），界面和活动流里看得到在排队。"""
        st = self._state(inst_id)
        if st.lock.locked():
            st.waiting += 1
            self._emit(cfg.env, inst_id, "worker", "queued", f"{what}：Worker 正在{st.busy or '忙'}，排队等它做完（排第 {st.waiting} 个）", None, {"waiting": st.waiting})
            self.bus.publish("agent", self.status())
            try:
                await st.lock.acquire()
            finally:
                st.waiting -= 1
        else:
            await st.lock.acquire()
        st.busy = what
        try:
            yield
        finally:
            st.busy = ""
            st.lock.release()

    async def _run(self, agent: Any, prompt: str, st: InstState | None, timeout: float, live: LiveText | None = None, **kwargs: Any) -> Any:
        """跑一轮 Agent。用 iter 而不是 run，是为了拿到这一轮的句柄：它思考到一半时可以往里插话（enqueue）。
        给了 live 就流式输出：模型每写出一点，就推给界面，不等整轮跑完。"""
        live = live if live is not None and self.get_config().stream and _can_stream(agent) else None

        async def go() -> Any:
            async with agent.iter(prompt, **kwargs) as run:
                if st is not None:
                    st.run = run
                try:
                    async for node in run:
                        if live is not None and Agent.is_model_request_node(node):
                            async with node.stream(run.ctx) as events:
                                async for event in events:
                                    live.feed(event)
                            # 这一步写完了：把「正在写」的内容收掉。要去调工具（比如把事情交给 Worker）时，不让半截话一直挂在那里等
                            live.flush(done=True)
                            live.text = live.thinking = live.args = ""
                finally:
                    if st is not None:
                        st.run = None
                    if live is not None:
                        live.flush(done=True)
            return run.result

        return await asyncio.wait_for(go(), timeout)

    # ---------- 目标：用户或 Router 交给某个币的 Worker 的任务书（做什么、最多亏多少、做到什么时候） ----------

    def goal(self, env: str, inst_id: str) -> dict[str, Any] | None:
        return self.store.get(f"goal:{env}:{inst_id}")

    def set_goal(self, inst_id: str, goal: str, max_loss_usdt: float | None = None, hours: float | None = None, who: str = "用户",
                 target_usdt: float | None = None, stop_when: str = "", shared_with: list[str] | None = None) -> dict[str, Any]:
        """target_usdt：净赚到多少就停；stop_when：别的停止条件（一句话，由 Worker 对照）；shared_with：和哪些币合起来算净赚。"""
        cfg = self.get_config()
        row = {
            "goal": goal.strip()[:500], "max_loss_usdt": max_loss_usdt, "until_ts": now_ms() + int(hours * 3_600_000) if hours else None,
            "ts": now_ms(), "status": "active", "pnl": 0.0, "by": who, "note": "",
            "target_usdt": target_usdt, "stop_when": stop_when.strip()[:200], "shared_with": [i for i in (shared_with or []) if i != inst_id and i in cfg.instruments],
        }
        self.store.set(f"goal:{cfg.env}:{inst_id}", row)
        if cfg.worker(inst_id).standby:  # 有了新任务：不再停着
            self._set_worker(inst_id, standby=False, paused_by="")
        st = self._state(inst_id)
        st.goal_checked, st.goal_realized, st.has_mail = 0.0, 0.0, True
        self.store.set(f"inbox:{cfg.env}:{inst_id}", [*self.inbox(cfg.env, inst_id), {"who": who, "text": f"给了你新的目标：{row['goal']}", "ts": now_ms()}][-10:])
        limit = (f"，净赚 {target_usdt:g} USDT 就停" if target_usdt else "") + (f"，最多亏 {max_loss_usdt:g} USDT" if max_loss_usdt else "") + (f"，停止条件：{row['stop_when']}" if row["stop_when"] else "")
        self._emit(cfg.env, inst_id, "router" if who == "Router" else "worker", "handoff", f"{who}交给 {inst_id} Worker 的目标：{row['goal']}{limit}" + (f"，{hours:g} 小时内" if hours else ""), None, {"goal": row})
        self.bus.publish("agent", self.status())
        return row

    def clear_goal(self, inst_id: str) -> None:
        cfg = self.get_config()
        self.store.delete(f"goal:{cfg.env}:{inst_id}")
        self.bus.publish("agent", self.status())

    async def _check_goal(self, cfg: AgentConfig, inst_id: str, account: AccountState, now: float) -> None:
        """核对交代给这个 Worker 的停止条件（净赚到目标、亏到上限、到时间）：由代码算，不靠 Worker 自己说。
        达到就撤掉没触发的策略和挂单、平掉仓位（到时间的只停不平），不能再开仓，Worker 停下；没达到就一直盯。"""
        goal = self.goal(cfg.env, inst_id)
        pos = account.position(inst_id)
        if not goal:
            return
        if goal["status"] != "active":
            if not goal.get("parked") and not pos and "stop_when" in goal:  # 条件已达到、仓位也没了：Worker 停下（升级前就结束的旧目标不追溯）
                self.store.set(f"goal:{cfg.env}:{inst_id}", {**goal, "parked": True})
                self._park(cfg, inst_id, f"目标结束（{goal['note']}）")
            return
        st = self._state(inst_id)
        if now - st.goal_checked > 60:  # 已实现盈亏一分钟核对一次，浮动盈亏每个节拍都算
            st.goal_checked = now
            try:
                st.goal_realized = sum(t.pnl for t in await self.trader.history(inst_id, 50, cfg.env) if t.closed_ts >= goal["ts"])
            except Exception:  # noqa: BLE001 - 历史读不到就沿用上次的数
                pass
        pnl = round(st.goal_realized + (pos.upl if pos else 0.0), 2)
        changed = abs(pnl - goal.get("pnl", 0)) >= 0.01
        goal["pnl"] = pnl
        # 几个币合起来算净赚：加上一起算的那几个币各自目标的盈亏
        total = round(pnl + sum((self.goal(cfg.env, i) or {}).get("pnl", 0.0) for i in goal.get("shared_with") or []), 2)
        changed = changed or total != goal.get("total")
        goal["total"] = total
        why = ""
        if goal.get("until_ts") and now_ms() > goal["until_ts"]:
            goal.update(status="ended", note="到时间了，目标结束")
        elif goal.get("target_usdt") and total >= goal["target_usdt"]:
            goal.update(status="done", note=f"净赚 {total:g} USDT，达到目标 {goal['target_usdt']:g}，已停手")
            why = "净赚达到目标，停手"
        elif goal.get("max_loss_usdt") and pnl <= -goal["max_loss_usdt"]:
            goal.update(status="stopped", note=f"亏损 {pnl:g} USDT，达到上限 {goal['max_loss_usdt']:g}，已停手")
            why = "目标的亏损额度用完，停手"
        if why:
            self.store.set(f"goal:{cfg.env}:{inst_id}", goal)
            for row in self.store.strategies(cfg.env, inst_id):
                self.strategies.cancel(row["id"], why)
            for kind in ("cancel_orders", "close"):
                if self.get_config().env != cfg.env:
                    break
                if kind == "close" and not pos:
                    continue
                await self.trader.execute(TradeAction(kind=kind, inst_id=inst_id, reason=why), cfg.env, source="agent")
        if goal["status"] != "active":
            self._emit(cfg.env, inst_id, "router", "done", f"{inst_id} 的目标结束：{goal['note']}", None, {"goal": goal})
            self._note("alert", f"{inst_id} 的目标结束：{goal['note']}", inst_id=inst_id)
            changed = True
        if changed:
            self.store.set(f"goal:{cfg.env}:{inst_id}", goal)

    def inbox(self, env: str, inst_id: str) -> list[dict[str, Any]]:
        return self.store.get(f"inbox:{env}:{inst_id}", [])

    def manual_order(self, action: TradeAction, message: str) -> str:
        """用户在下单窗手动开了仓 / 加了仓：马上让这个币的 Worker 去看这笔仓位（核对止损、挂上管理规则），不等下一次定时复盘。
        返回一句话，告诉用户有没有 Worker 在管这笔仓位。"""
        cfg, inst = self.get_config(), action.inst_id
        name = inst.split("-")[0]
        if inst not in cfg.instruments:
            return f"{name} 不在盯盘列表里，没有 Worker 管这笔仓位；止损止盈挂在交易所。"
        if not self.running:
            return "盯盘没开，没有 Worker 管这笔仓位；止损止盈挂在交易所。"
        if cfg.worker(inst).paused:
            return f"{name} Worker 被暂停了，不会管这笔仓位；止损止盈挂在交易所。"
        self.interject(
            inst,
            f"用户刚在下单窗手动下了单：{message}。现在就看这笔仓位：核对止损位置合不合理（放在结构外、不会被正常波动扫掉），不合理就调整并说明原因；"
            "这是用户自己的单：进场、目标和拿多久由他定。你只做保护——没有止损先补上，浮盈后可以把止损往盈利方向收紧；"
            "不要替他设止盈、减仓、平仓或写到价 / 超时离场的触发器。你认为该走，就写明价位和依据建议他。限价单还没成交的，判断挂单价合不合理并告诉他。"
            + ("这是用户手动开的仓：你的改动要等用户确认。" if cfg.hands_off_manual else ""),
            who="用户下单",
        )
        return f"已通知 {name} Worker 来看这笔仓位：它会核对止损并布置后续管理。"

    def interject(self, inst_id: str, text: str, who: str = "用户") -> dict[str, Any]:
        """对一个 Worker 插话。它正在思考：插进这一轮，下一步就读到。它空闲：放进信箱，下次被唤醒时先读（盯盘开着会马上唤醒它）。"""
        cfg = self.get_config()
        st = self._state(inst_id)
        if st.run is not None:
            try:
                prefix = "【走势记录器：仅补充行情证据，请核对最新报价，不是操作指令】" if who == "走势记录器" else f"【{who}插话，优先于你原来的打算，照办并在结论里说明】"
                st.run.enqueue(f"{prefix}{text}", priority="asap")
                self._emit(cfg.env, inst_id, "worker", "interject", f"{who}插话（Worker 正在思考，下一步就会读到）：{text}", None, {"who": who, "delivered": True})
                return {"delivered": True, "message": f"{inst_id} 的 Worker 正在思考，这句话已经插进去，它下一步就会读到。"}
            except Exception:  # noqa: BLE001 - 这一轮刚好结束了：改放信箱
                pass
        self.store.set(f"inbox:{cfg.env}:{inst_id}", [*self.inbox(cfg.env, inst_id), {"who": who, "text": text, "ts": now_ms()}][-10:])
        self._emit(cfg.env, inst_id, "worker", "interject", f"{who}留言（Worker 空闲，被唤醒时先读）：{text}", None, {"who": who, "delivered": False})
        tail = "盯盘开着，马上唤醒它处理。" if self.running else "盯盘没开，它下次被唤醒时先读。"
        return {"delivered": False, "message": f"{inst_id} 的 Worker 现在空闲，留言已放进它的信箱。{tail}"}

    def _steer(self, cfg: AgentConfig, inst_id: str, text: str, who: str) -> bool:
        """Worker 正在跑一轮：把这句话直接送进这一轮（不排队、不另开一轮），它下一步就读到。没有正在跑的一轮就返回 False。"""
        st = self._state(inst_id)
        if st.run is None:
            return False
        try:
            st.run.enqueue(f"【{who}刚刚又直接对你说了一句（追加的要求：和你原来的打算冲突时以这句为准，原来在做的事照样做完），在这一轮的结论里把两件事的结果都说清楚】{text}", priority="asap")
        except Exception:  # noqa: BLE001 - 这一轮刚好结束了
            return False
        st.steered = True
        self._emit(cfg.env, inst_id, "worker", "interject", f"{who}直接送进这一轮（Worker 正在思考，下一步就读到）：{text}", None, {"who": who, "delivered": True})
        return True

    def _router(self, cfg: AgentConfig) -> Agent[Deps, str]:
        if self._router_agent is None:
            self._router_agent = build_router_agent(self._model(cfg.router_model))
        return self._router_agent

    def _gate(self, cfg: AgentConfig) -> Any:
        if self._gate_agent is None:
            self._gate_agent = build_gate_agent(self._model(cfg.router_model))
        return self._gate_agent

    def _emit(self, env: str, inst_id: str, agent: str, kind: str, text: str, work_id: int | None = None, data: dict[str, Any] | None = None) -> None:
        """活动流里记一步并实时推给界面。agent: router / worker / intel / smart / risk；kind: wake / tool / result / handoff / action / done / skip / error。"""
        try:
            self.bus.publish("activity", self.store.add_activity(env, inst_id, agent, kind, text, work_id, data))
            if agent == "strategy" and kind == "done" and (data or {}).get("strategy_id"):
                self.bus.publish("chat", {})
            if agent == "strategy" and (data or {}).get("notify_worker"):
                self._algo(env, inst_id, "rule", text, "程序已按规则处理，并通知 Worker")
                feedback = f"策略 #{data['strategy_id']} 的执行回报（不是新的交易指令）：{text}"
                if data.get("recorder"):  # 记录器到了：顺带告诉它现在的线和挂着的东西还合不合适
                    rec = self.states.get(inst_id)
                    feedback = f"记录器 #{data['strategy_id']} 到了（条件命中不等于成交，这不是新的交易指令）：{text}" + (self._stale_lines(self.get_config(), inst_id, rec, rec.last_px) if rec and rec.last_px else "")
                run = self.states.get(inst_id)
                if run and run.run is not None and self.get_config().env == env:
                    try:
                        run.run.enqueue(feedback, priority="asap")
                        return
                    except Exception:
                        pass
                self.store.set(f"inbox:{env}:{inst_id}", [*self.inbox(env, inst_id), {"who": "策略执行器", "text": feedback, "ts": now_ms()}][-10:])
        except Exception as exc:  # noqa: BLE001 - 活动流写不进去不能影响交易
            log.warning("活动流记录失败：%s", exc)

    def _emitter(self, env: str, inst_id: str, agent: str, work_id: int | None) -> Any:
        """给 Deps.emit 用：Agent 每调一次工具就在活动流里出现一步。"""

        def emit(kind: str, tool: str, payload: Any) -> None:
            label = _TOOL_LABEL.get(tool, tool)
            if kind in ("tool", "ask"):
                args = {k: v for k, v in (payload or {}).items() if k != "reason" and v not in (None, "")}
                brief = "，".join(f"{k}={v}" for k, v in list(args.items())[:6])[:200]
                handoff = tool in ("ask_subagent", "ask_worker", "ask_intel")
                if kind == "ask":  # 这次调用要用户同意，已经挂起：等这一轮收尾时统一记（那时才知道它的编号）
                    return
                self._emit(env, inst_id, agent, "handoff" if handoff else "tool", f"{label}" + (f"（{brief}）" if brief else ""), work_id, {"tool": tool, "args": args, "why": (payload or {}).get("reason", "")})
            else:
                self._emit(env, inst_id, agent, "result", f"{label} → {str(payload)[:240]}", work_id, {"tool": tool})

        return emit

    def _sub_agent(self, cfg: AgentConfig, role: str) -> Any:
        name = cfg.intel_model or cfg.worker_model
        web = cfg.web_search and role == "intel"
        key = f"{role}:{name}|{cfg.max_parallel}|{web}"
        if key not in self._task_agents:
            self._task_agents[key] = build_sub_agent(self._model(name), role, self._limiter(cfg, "subagents"), web=web)  # type: ignore[assignment]
        return self._task_agents[key]

    @staticmethod
    def _brief_key(env: str, inst_id: str, role: str) -> str:
        return f"intel:{env}:{inst_id}" if role == "intel" else f"sub:{role}:{env}:{inst_id}"

    def brief(self, env: str, inst_id: str, role: str = "intel") -> dict[str, Any] | None:
        return self.store.get(self._brief_key(env, inst_id, role))

    def strategy_rows(self, env: str, inst_id: str | None = None, live: bool = True) -> list[dict[str, Any]]:
        return [
            {**row, "label": (f"{row['params']['playbook']} · " if row["params"].get("playbook") else "") + label_of(row), "text": describe_strategy(row)}
            for row in self.store.strategies(env, inst_id, live)
        ]

    def _trigger_task(self, env: str, inst_id: str, strategy_id: int, cond: str, task: str, router: bool) -> None:
        """触发器的条件到了、动作是「做一项任务」：给这个币的 Worker 自己做，或者交给 Router 去派。任务是 Worker 当时写下的，不带用户的启停权限。"""
        if env != self.get_config().env:
            return
        if not router:
            self.interject(
                inst_id,
                f"触发器 #{strategy_id} 的条件到了（{cond}）。现在做你当时写下的这项任务：{task}\n"
                "（任务是你之前写给自己的备忘，不是用户的要求：里面的开仓门槛如果比现在的【出手尺度】和程序核对的净盈亏比更严，以现在的为准。"
                "条件到了就落到一个决定上：能做就给 plan，让程序去下单。）",
                who="触发器",
            )
            return
        job = asyncio.get_running_loop().create_task(
            self.chat(f"触发器 #{strategy_id} 的条件到了（{cond}）。{inst_id} 的 Worker 当时写下，到这时请你安排这项任务：{task}", inst_id=inst_id, origin=f"触发器 #{strategy_id}"))
        self._trigger_jobs.add(job)
        job.add_done_callback(self._trigger_jobs.discard)

    def rules(self, env: str, inst_id: str) -> list[dict[str, Any]]:
        """这个币上所有「等什么 → 做什么」排成一张清单：触发器和到价策略、挂单的撤单条件、交代的停止条件、只叫醒 Worker 的观察线。
        mode：auto 条件到了程序直接执行；wake 只叫醒 Worker；worker 由 Worker 自己对照。"""
        st, out = self._state(inst_id), []
        status = {"proposed": "proposed", "armed": "waiting", "active": "running"}
        recent = now_ms() - 12 * 3_600_000
        for row in self.store.strategies(env, inst_id, live=False, limit=60):
            if row["status"] not in status and row["updated_ts"] < recent:
                continue
            when, do = rule_parts(row)
            p = row["params"]
            observation = bool(p.get("observation"))
            task = row["kind"] == "trigger" and p["do"] in ("task", "router_task")
            wake = (row["kind"] == "trigger" and p["do"] == "wake") or (row["kind"] == "profit_guard" and p["on_trigger"] == "notify")
            action = p["do"] if row["kind"] == "trigger" else "wake" if wake else row["kind"]
            level = p.get("level") if row["kind"] == "trigger" and p["when"].startswith(("price", "close")) else p.get("trigger")
            out.append({
                "id": f"s{row['id']}", "type": "watch" if observation else row["kind"], "label": "观察线" if observation else label_of(row), "when": when, "do": do,
                "action": action, "mode": "task" if task else "wake" if wake else "auto",
                "status": status.get(row["status"], row["status"]), "by": p.get("by") or "Worker", "ts": row["ts"], "updated_ts": row["updated_ts"], "note": row["note"],
                "level": level, "strategy_id": row["id"], "expires_ts": p.get("expires_ts"),
                # 记录器：Worker 小组里的节点，条件是程序记录的数据，数值可以直接改
                "recorder": bool(p.get("recorder")), "conds": p.get("conds"),
                "repeat": bool(p.get("repeat")), "cooldown_minutes": p.get("cooldown_minutes"), "off": bool(p.get("off")), "runs": row["state"].get("runs", 0), "last_run": row["state"].get("last_run"),
            })
        for g in self.trader.order_guards(env, inst_id):
            what = f"撤掉 {g['price']:g} 的限价{'买' if g['side'] in ('long', 'buy') else '卖'}单"
            base = {"type": "order", "label": "挂单撤单", "do": what, "action": "cancel_orders", "mode": "auto", "status": "waiting", "by": "Worker", "ts": g["ts"], "updated_ts": g["ts"], "note": "", "strategy_id": None, "ord_id": g["ord_id"]}
            if g.get("cancel_px"):
                out.append({**base, "id": f"o{g['ord_id']}c", "when": f"还没成交，价格先到 {g['cancel_px']:g}", "level": g["cancel_px"], "expires_ts": None})
            if g.get("expires_ts"):
                out.append({**base, "id": f"o{g['ord_id']}t", "when": "到时间还没成交", "level": None, "expires_ts": g["expires_ts"]})
        goal = self.goal(env, inst_id)
        if goal and goal["status"] == "active":
            base = {"type": "stop", "label": "停止条件", "action": "stop", "mode": "auto", "status": "waiting", "by": goal.get("by") or "你", "ts": goal["ts"], "updated_ts": goal["ts"], "note": goal["goal"], "level": None, "strategy_id": None, "expires_ts": None}
            do = "撤单、平仓，这个 Worker 停下不再开仓"
            if goal.get("target_usdt") is not None:
                together = "（和 " + "、".join(i.split("-")[0] for i in goal["shared_with"]) + " 合起来算）" if goal.get("shared_with") else ""
                out.append({**base, "id": "goal-target", "when": f"净赚 ≥ {goal['target_usdt']:g} U{together}，现在 {goal.get('total', goal['pnl']):+.2f} U", "do": do})
            if goal.get("max_loss_usdt") is not None:
                out.append({**base, "id": "goal-loss", "when": f"这个币亏损 ≥ {goal['max_loss_usdt']:g} U，现在 {goal['pnl']:+.2f} U", "do": do})
            if goal.get("until_ts"):
                out.append({**base, "id": "goal-time", "when": "到了约定的结束时间", "do": do, "expires_ts": goal["until_ts"]})
            if goal.get("stop_when"):
                out.append({**base, "id": "goal-when", "when": goal["stop_when"], "do": "Worker 确认后停下（有持仓时不停）", "mode": "worker"})
        return out

    def team(self, env: str, inst_id: str) -> list[dict[str, Any]]:
        """这个币的小组里，交易 Worker 手下的子代理各自在干什么、最近交了什么。"""
        st = self._state(inst_id)
        return [
            {"role": role, "label": spec["label"], "does": spec["does"], "running": role in st.sub_running, "brief": self.brief(env, inst_id, role)}
            for role, spec in SUBAGENTS.items()
            if self.get_config().sub_enabled(role)
        ]

    async def intel(self, inst_id: str, question: str = "", role: str = "intel", by: str = "") -> dict[str, Any]:
        """这个币的一个子代理去做一次事，简报存进共享记忆，交易 Worker 下次被唤醒时会读到。by 是谁派的（留空 = 定时或用户）。"""
        if role not in SUBAGENTS:
            raise RuntimeError(f"没有这个子代理：{role}")
        cfg = self.get_config()
        if not cfg.sub_enabled(role):
            raise RuntimeError(f"「{SUBAGENTS[role]['label']}」子代理已经在设置里关掉了，不用它，按你手上已有的信息判断。")
        st = self._state(inst_id)
        label = SUBAGENTS[role]["label"]
        st.sub_running.add(role)
        if role == "intel":
            st.intel_running, st.intel_ts = True, time.time()
        task = question or f"更新{label}简报"
        work_id = self.store.add_work(cfg.env, inst_id, role, (f"{by} 派的：" if by else "") + task, "running", model=cfg.intel_model or cfg.worker_model)
        self.bus.publish("work", {"inst_id": inst_id})
        self.bus.publish("agent", self.status())
        try:
            deps = Deps(trader=self.trader, market=self.market, gateway=self.gw, config=cfg, kb=self.kb, skills=self.skills, lessons=self.lessons, emit=self._emitter(cfg.env, inst_id, role, work_id))
            self._emit(cfg.env, inst_id, role, "wake", (f"{by} 派来的任务：" if by else "") + task, work_id, {"by": by})
            prompt = f"品种：{inst_id}（币种 {inst_id.split('-')[0]}）。" + (f"\n任务：{question}" if question else f"\n任务：更新这个币的{label}简报。")
            if role == "risk":
                account = await self.trader.account(cfg.env)
                pos = account.position(inst_id)
                prompt += (
                    f"\n【账户】权益 {account.equity:.2f} USDT，可用 {account.available:.2f}，当日盈亏 {account.day_pnl_pct or 0:+.2f}%（上限 -{cfg.risk.max_daily_loss_pct:g}%）。"
                    f"\n【这个币的持仓】{pos.model_dump(exclude_none=True) if pos else '无'}"
                    f"\n【全部持仓】" + ("；".join(f"{p.inst_id} {p.side} {p.contracts:g} 张 {p.leverage:g}x 浮盈亏 {p.upl:+.2f}" for p in account.positions) or "无")
                    + f"\n【风控上限】单仓风险 {cfg.risk.risk_per_trade_pct:g}%，杠杆上限 {cfg.risk.max_leverage}x。"
                )
            result = await asyncio.wait_for(self._sub_agent(cfg, role).run(prompt, deps=deps, usage_limits=UsageLimits(request_limit=14), model_settings=_effort(cfg.sub_effort, f"ouy-sub-{role}")), REVIEW_TIMEOUT)
            brief = {**result.output.model_dump(), "ts": now_ms(), "question": question, "role": role, "by": by}
            self.store.set(self._brief_key(cfg.env, inst_id, role), brief)
            self.store.update_work(work_id, "done", brief["summary"])
            self._emit(cfg.env, inst_id, role, "done", brief["summary"], work_id, {"bias": brief.get("bias"), "points": brief.get("points")})
            return brief
        except BaseException as exc:
            kind, why = _fail_text(exc)
            self.store.update_work(work_id, "failed", why)
            self._emit(cfg.env, inst_id, role, kind, why, work_id)
            raise
        finally:
            st.sub_running.discard(role)
            if role == "intel":
                st.intel_running = False
            self.bus.publish("work", {"inst_id": inst_id})
            self.bus.publish("agent", self.status())

    def _set_worker(self, inst_id: str, **changes: Any) -> AgentConfig:
        """改某个币的 Worker 的单独设置并存下来。"""
        cfg = self.control.config() if self.control else self.get_config()
        workers = {k: v.model_dump() for k, v in cfg.workers.items()}
        workers[inst_id] = {**cfg.worker(inst_id).model_dump(), **changes}
        new = AgentConfig.model_validate({**cfg.model_dump(), "workers": workers})
        if self.control:
            self.control.save_config(new)
        return new

    def _set_quant(self, inst_id: str, mode: str, reason: str, bias: str = "both", vol_ratio: float | None = None, by: str = "Worker") -> AgentConfig:
        """Worker（或用户）切换这个币的量化盯盘档位：保存设置、推送活动流让拓扑连线亮起来，并刷新界面状态。"""
        changes: dict[str, Any] = {"quant_mode": mode, "quant_bias": bias, "quant_reason": reason, "quant_by": by}
        if vol_ratio is not None:
            changes["quant_vol_ratio"] = vol_ratio
        new = self._set_worker(inst_id, **changes)
        label = QUANT_MODES[mode]["label"]
        btxt = "双向" if bias == "both" else "只盯做多" if bias == "long" else "只盯做空"
        self._emit(new.env, inst_id, "strategy", "wake", f"{by}把量化雷达切到「{label}」（{btxt}）：{reason}", None, {"quant": True, "mode": mode, "bias": bias})
        self.bus.publish("config", new.model_dump())
        self.bus.publish("agent", self.status())
        return new

    async def _stop_watching(self, inst_id: str, evidence: str) -> str:
        """Worker 说「交代给我的停止条件达到了」：只有用户 / Router 在目标里写了停止条件才算数；它自己觉得没行情不算。"""
        cfg = self.get_config()
        goal = self.goal(cfg.env, inst_id)
        if not goal or goal["status"] != "active" or not goal.get("stop_when"):
            return "[不能停] 用户和 Router 都没有交代过停止条件。没机会就继续观望，把要等的情况写成触发器（add_trigger），需要你到时再判断的价位写进 watch_levels。"
        if (await self.trader.account(cfg.env)).position(inst_id):
            return "[不能停] 这个币还有持仓，先管好仓位。"
        self._park(cfg, inst_id, f"交代的停止条件「{goal['stop_when']}」已达到：{evidence}")
        return "[已停下] 不再定时复盘。你写的触发线、到价策略照常由程序盯着，触到了、有人留言或出现持仓会叫醒你。"

    def _park(self, cfg: AgentConfig, inst_id: str, why: str) -> None:
        """停下（待命）：空仓时不再定时复盘；触发线、到价策略、留言、持仓变化照常叫醒。"""
        self._set_worker(inst_id, standby=True, paused_by=why)
        self._note("info", f"{inst_id.split('-')[0]} Worker 停下：{why}", inst_id=inst_id)
        self._emit(cfg.env, inst_id, "worker", "skip", f"停下：{why}", None, {"standby": True})
        self.bus.publish("agent", self.status())

    def set_standby(self, inst_id: str, on: bool) -> None:
        """用户直接让某个 Worker 停下（待命）或恢复定时复盘。"""
        self._set_worker(inst_id, standby=on, paused_by="你让它停下" if on else "")
        self.bus.publish("agent", self.status())

    def _resume_due(self, cfg: AgentConfig) -> None:
        """兼容原先约定好的定时恢复，不改变其余开关。"""
        for inst in cfg.instruments:
            w = cfg.worker(inst)
            if w.paused and w.resume_ts and now_ms() >= w.resume_ts:
                cfg = self._set_worker(inst, paused=False, resume_ts=None, paused_by="")
                self._note("info", f"{inst.split('-')[0]} Worker 休息时间到了，回来继续盯盘", inst_id=inst)
                self._emit(cfg.env, inst, "worker", "skip", "休息时间到了，回来继续盯盘", None, {"resumed": True})

    def _sub_rows(self, inst_id: str, by: str) -> tuple[list[dict[str, Any]], str]:
        """这个子代理记在谁名下：币种 Worker 开的记在它那个币的小组里，通用 Worker 开的记在通用 Worker 名下。返回 (名单, 归属标识)。"""
        if by in ("Router", GENERAL):
            return self.router_adhoc, "general"
        return self._state(inst_id).adhoc, inst_id

    def _expire_subs(self) -> None:
        """留着的子代理空闲太久就自动关掉，免得图上堆一堆没人用的。"""
        deadline = now_ms() - ADHOC_IDLE_MS
        for owner, rows in [("general", self.router_adhoc), *((inst, st.adhoc) for inst, st in self.states.items())]:
            for row in [r for r in rows if not r["running"] and r["ts"] < deadline]:
                rows.remove(row)
                self._sub_hist.pop((owner, row["name"]), None)

    def _close_sub(self, inst_id: str, name: str, by: str = "") -> str:
        """开它的 Agent 觉得不需要了，自己把留着的子代理关掉：从图上消失，它记得的东西也一起丢掉。"""
        cfg = self.get_config()
        rows, owner = self._sub_rows(inst_id, by)
        row = next((r for r in rows if r["name"] == name), None)
        if row is None:
            return f"没有叫「{name}」的子代理（现在留着的：{'、'.join(r['name'] for r in rows) or '没有'}）。"
        if row["running"]:
            return f"子代理「{name}」还在干活，等它交回结果再关。"
        rows.remove(row)
        self._sub_hist.pop((owner, name), None)
        self._emit(cfg.env, inst_id, "adhoc", "closed", f"{by or inst_id.split('-')[0] + ' Worker'} 关掉了子代理「{name}」", None, {"name": name})
        self.bus.publish("agent", self.status())
        return f"已关掉子代理「{name}」。"

    async def _spawn(self, inst_id: str, name: str, task: str, by: str = "", keep: bool = False) -> dict[str, Any]:
        """临时开一个子代理：任务是开它的 Agent 现场写的，只读工具，做完把结果交回去。
        keep=False（默认）：一次性的，交回结果就结束、从图上消失。
        keep=True：留着——它记得自己查过什么，之后用同一个名字再交任务就是接着问它；不需要了由开它的 Agent 自己关（或者空闲太久自动关）。"""
        cfg = self.get_config()
        self._expire_subs()
        rows, owner = self._sub_rows(inst_id, by)
        by = by or f"{inst_id.split('-')[0]} Worker"
        row = next((r for r in rows if r["name"] == name), None)
        if row is not None and row["running"]:
            raise RuntimeError(f"子代理「{name}」还在干活，等它交回结果再给它新任务，或者换个名字另开一个。")
        resumed = row is not None  # 之前留着的：接着问它
        keep = keep or resumed
        if sum(1 for a in rows if a["running"]) >= ADHOC_PARALLEL:
            raise RuntimeError(f"同时最多开 {ADHOC_PARALLEL} 个子代理，等前面的交回结果再开。")
        if not resumed and keep and sum(1 for a in rows if a.get("keep")) >= ADHOC_KEEP:
            raise RuntimeError(f"留着的子代理最多 {ADHOC_KEEP} 个（现在有：{'、'.join(a['name'] for a in rows if a.get('keep'))}）。先关掉一个不用的，或者这次开成一次性的。")
        times = self._spawn_times[owner] = [t for t in self._spawn_times.get(owner, []) if now_ms() - t < 10 * 60_000]
        if len(times) >= ADHOC_PER_10MIN:
            raise RuntimeError(f"10 分钟内已经开过 {ADHOC_PER_10MIN} 次子代理了，先用手上已有的结果判断。")
        times.append(now_ms())
        work_id = self.store.add_work(cfg.env, inst_id, "adhoc", f"{by} {'接着问' if resumed else '开的'}子代理「{name}」：{task}", "running", model=cfg.intel_model or cfg.worker_model)
        if row is None:
            row = {"id": work_id, "name": name, "task": task, "running": True, "summary": "", "bias": "", "ts": now_ms(), "keep": keep}
            rows.append(row)
        else:
            row.update(id=work_id, task=task, running=True, ts=now_ms())
        self._emit(cfg.env, inst_id, "adhoc", "wake", f"{by} {'接着问' if resumed else '开的'}子代理「{name}」：{task}", work_id, {"name": name, "by": by, "keep": keep})
        self.bus.publish("agent", self.status())
        try:
            model_name = cfg.intel_model or cfg.worker_model
            key = f"adhoc:{model_name}|{cfg.max_parallel}|{cfg.web_search}"
            if key not in self._task_agents:
                self._task_agents[key] = build_adhoc_agent(self._model(model_name), self._limiter(cfg, "subagents"), web=cfg.web_search)  # type: ignore[assignment]
            deps = Deps(trader=self.trader, market=self.market, gateway=self.gw, config=cfg, kb=self.kb, skills=self.skills, lessons=self.lessons, emit=self._emitter(cfg.env, inst_id, "adhoc", work_id))
            about = f"品种：{inst_id}（币种 {inst_id.split('-')[0]}），" if inst_id else "这个任务不针对某一个币，"
            prompt = f"{about}现在是{'模拟盘' if cfg.env == 'demo' else '实盘'}。\n{'接着上面的工作，新任务' if resumed else '任务'}：{task}"
            result = await asyncio.wait_for(
                self._task_agents[key].run(
                    prompt, deps=deps, message_history=self._sub_hist.get((owner, name)) if resumed else None,
                    usage_limits=UsageLimits(request_limit=14), model_settings=_effort(cfg.sub_effort, "ouy-adhoc"),
                ),
                REVIEW_TIMEOUT,
            )
            brief = {**result.output.model_dump(), "name": name}
            if keep:
                row.update(running=False, summary=brief["summary"], bias=brief.get("bias") or "", ts=now_ms())
                self._sub_hist[(owner, name)] = _trim_history(result.all_messages())
                brief["kept"] = f"子代理「{name}」留着了：用同一个名字再交任务就是接着问它；不需要了用 close_subagent 关掉。"
            else:
                rows.remove(row)  # 一次性的：交回结果就从图上消失
            self.store.update_work(work_id, "done", brief["summary"])
            self._emit(cfg.env, inst_id, "adhoc", "done", brief["summary"], work_id, {"name": name, "bias": brief.get("bias"), "points": brief.get("points"), "keep": keep})
            return brief
        except BaseException as exc:
            if resumed:
                row.update(running=False, summary=f"没查成：{str(exc)[:160] or type(exc).__name__}")
            elif row in rows:
                rows.remove(row)
            kind, why = _fail_text(exc)
            self.store.update_work(work_id, "failed", why)
            self._emit(cfg.env, inst_id, "adhoc", kind, why, work_id, {"name": name})
            raise
        finally:
            self.bus.publish("work", {"inst_id": inst_id})
            self.bus.publish("agent", self.status())

    def _lessons_text(self, env: str, inst_id: str) -> str:
        brief = self.lessons.brief(env, inst_id)
        if not brief:
            return ""
        return (
            f"【长期经验（{'模拟盘' if env == 'demo' else '实盘'}里做过的交易沉淀下来的，和另一个环境互不相干）】规矩要照着做；观察是还没验证够的，参考着用。"
            "刚发生的事印证或推翻了某一条，用 judge_lesson 记一下（带 # 编号的才能判）：\n" + brief
        )

    # ---------- 平仓后自动复盘：每笔平掉的仓写一条经验 ----------

    async def _review_trades(self, cfg: AgentConfig) -> int:
        """找出新平掉的仓，逐笔复盘：为什么赚 / 亏、留下什么经验、印证或推翻了哪些已有经验。每次巡检最多复盘 3 笔。
        第一次运行时把已有的历史都算作复盘过（不回头翻旧账）；部分平仓不复盘，等整笔平完。"""
        env = cfg.env
        key = f"reviewed:{env}"
        trades = await self.trader.history(None, 30, env)
        ident = lambda t: f"{t.inst_id}:{t.closed_ts}"  # noqa: E731
        seen = self.store.get(key)
        if seen is None:
            self.store.set(key, [ident(t) for t in trades])
            return 0
        fresh = [t for t in trades if ident(t) not in seen]
        if not fresh:
            return 0
        done, count = [], 0
        for t in sorted(fresh, key=lambda t: t.closed_ts):
            if "部分" in t.how or now_ms() - t.closed_ts > 24 * 3_600_000:
                done.append(ident(t))
                continue
            if count >= 3:
                break
            try:
                await self._review_trade(cfg, t)
                done.append(ident(t))
                count += 1
            except Exception as exc:  # noqa: BLE001 - 复盘失败不能影响盯盘；试三次还不行就放过这一笔
                fails = self._review_fails[ident(t)] = self._review_fails.get(ident(t), 0) + 1
                log.warning("复盘 %s 没做成（第 %s 次）：%s", ident(t), fails, exc)
                if fails >= 3:
                    done.append(ident(t))
                    self._emit(env, t.inst_id, "worker", "error", f"这笔平仓的自动复盘没做成：{str(exc)[:160] or type(exc).__name__}", None, {"review": True})
        if done:
            self.store.set(key, [*seen, *done][-300:])
        return count

    async def _review_trade(self, cfg: AgentConfig, t: Any) -> None:
        env, inst_id = cfg.env, t.inst_id
        coin = inst_id.split("-")[0]
        mins = max(1, int((t.closed_ts - t.opened_ts) / 60_000))
        side = "做多" if t.side == "long" else "做空"
        facts = f"{coin} {side}，{t.leverage:g} 倍，入场 {t.open_px:g} → 出场 {t.close_px:g}，持仓 {mins} 分钟，{t.how}，已实现盈亏 {t.pnl:+.2f} USDT（保证金收益率 {t.pnl_pct:+.1f}%）"
        book = next(
            (r["params"]["playbook"] for r in self.store.strategies(env, inst_id, live=False, limit=20)
             if r["params"].get("playbook") and t.opened_ts - 120_000 <= int((r["state"].get("since") or 0) * 1000) <= t.closed_ts),
            None,
        )
        journal = [
            f"{time.strftime('%m-%d %H:%M', time.localtime(j['ts'] / 1000))} {j['title']}" + (f"（{str(j['body'].get('reason'))[:160]}）" if j["body"].get("reason") else "")
            for j in reversed(self.store.journal(env, 60, inst_id)) if t.opened_ts - 600_000 <= j["ts"] <= t.closed_ts + 120_000
        ][-14:]
        report = self._state(inst_id).report or {} if inst_id in self.states else {}
        known = self.lessons.brief(env, inst_id, 10, 10)
        paths = [self.track_text(m) for m in self.lot_track(env).values()
                 if m.get("inst") == inst_id and m.get("side") == t.side and not m.get("open") and t.opened_ts - 300_000 <= m.get("opened", 0) <= t.closed_ts]
        prompt = "\n".join(filter(None, [
            f"【这笔交易（{'模拟盘' if env == 'demo' else '实盘'}）】{facts}",
            f"【用的策略库策略】{book}" if book else "",
            "【这段时间的操作记录】\n" + "\n".join(journal) if journal else "【这段时间的操作记录】没有记录（可能是手动下的单）",
            "【持仓轨迹（程序记的）】" + "；".join(paths) + "。浮盈到过 0.5R 以上最后却亏了，要说清楚是哪一步没守住：目标太远、该锁没锁、还是入场理由早就失效。" if paths else "",
            f"【Worker 最近一次的判断】{report.get('summary')}" if report.get("summary") else "",
            "【已有的经验】\n" + known if known else "【已有的经验】还没有",
        ]))
        if self._review_agent is None:
            self._review_agent = build_review_agent(self._model(cfg.intel_model or cfg.worker_model))
        result = await asyncio.wait_for(self._review_agent.run(prompt, model_settings=_effort(cfg.sub_effort, "ouy-trade-review"), usage_limits=UsageLimits(request_limit=4)), 120)
        review = result.output
        trade = {"side": t.side, "open_px": t.open_px, "close_px": t.close_px, "pnl": round(t.pnl, 4), "pnl_pct": round(t.pnl_pct, 2), "minutes": mins, "how": t.how, "closed_ts": t.closed_ts, "playbook": book}
        notes = []
        for lesson_id in dict.fromkeys(review.confirms):
            row = self.lessons.judge(env, lesson_id, True, facts[:120])
            if row:
                notes.append(f"印证了经验 #{lesson_id}" + ("，升成规矩" if row["grade"] == "rule" and row["confirms"] == 3 else ""))
        for lesson_id in dict.fromkeys(i for i in review.refutes if i not in review.confirms):
            row = self.lessons.judge(env, lesson_id, False, facts[:120])
            if row:
                notes.append(f"推翻了经验 #{lesson_id}" + ("，已删除" if row.get("dropped") else ""))
        if review.lesson.strip():
            row, created = self.lessons.add(env, inst_id, review.lesson, source="review", trade=trade)
            notes.append(f"{'新经验' if created else '又印证了经验'} #{row['id']}：{row['text']}")
        text = f"复盘：{facts}。{review.why}" + (" " + "；".join(notes) if notes else "")
        self._note("review", f"{coin} 平仓复盘：{t.pnl:+.2f} USDT", {"why": review.why, "lesson": review.lesson, "trade": trade}, inst_id=inst_id)
        self._emit(env, inst_id, "worker", "done", text, None, {"review": True, "pnl": round(t.pnl, 4)})
        self.bus.publish("lessons", {"env": env})

    def _skills_text(self) -> str:
        brief = self.skills.brief() if self.skills is not None else ""
        return f"【已装技能】只有名字和简介；手上的事用得上哪个，先用 read_skill 打开全文再照着做：\n{brief}" if brief else ""

    async def _general(self, task: str, skill: dict[str, Any] | None = None) -> str:
        """通用 Worker：Router 把不针对某个币的问题交给它。它自己想、自己查、需要时开子代理，结论交回给 Router。"""
        cfg = self.get_config()
        model_name = cfg.worker_model
        work_id = self.store.add_work(cfg.env, "", "general", f"Router 交给{GENERAL}：{task}", "running", model=model_name)
        self.general.update(busy=True, task=task, ts=now_ms())
        self._emit(cfg.env, "", "general", "wake", f"Router 交来的任务：{task}", work_id, {"by": "Router"})
        self.bus.publish("agent", self.status())
        try:
            key = f"general:{model_name}|{cfg.max_parallel}|{cfg.web_search}"
            if key not in self._task_agents:
                self._task_agents[key] = build_general_agent(self._model(model_name), self._limiter(cfg, "workers"), web=cfg.web_search)  # type: ignore[assignment]
            deps = Deps(
                trader=self.trader, market=self.market, gateway=self.gw, config=cfg, kb=self.kb, skills=self.skills, lessons=self.lessons,
                spawn=lambda inst, name, sub_task, keep=False: self._spawn(inst, name, sub_task, by=GENERAL, keep=keep),
                close_sub=lambda inst, name: self._close_sub(inst, name, by=GENERAL), emit=self._emitter(cfg.env, "", "general", work_id),
                skill_draft=skill, proxy=self.proxy_url(),
            )
            prompt = f"现在是{'模拟盘' if cfg.env == 'demo' else '实盘'}，盯盘列表：{'、'.join(cfg.instruments)}。\n任务：{task}"
            if skill and skill.get("content"):
                prompt += f"\n【要安装的技能原文（开头一段，全文系统已经带上）】\n{str(skill['content'])[:1500]}"
            if self._skills_text() and not skill:
                prompt += "\n" + self._skills_text()
            result = await self._run(
                self._task_agents[key], prompt, None, REVIEW_TIMEOUT, live=LiveText(self.bus.publish, "general", ""),
                deps=deps, usage_limits=UsageLimits(request_limit=20), model_settings=_effort(cfg.reasoning_effort, "ouy-general"),
            )
            answer = str(result.output)
            self.general.update(busy=False, summary=answer[:400], ts=now_ms())
            self.store.update_work(work_id, "done", answer[:500])
            self._emit(cfg.env, "", "general", "done", answer, work_id)
            return answer
        except BaseException as exc:
            self.general.update(busy=False, summary=f"没做成：{str(exc)[:160] or type(exc).__name__}", ts=now_ms())
            kind, why = _fail_text(exc)
            self.store.update_work(work_id, "failed", why)
            self._emit(cfg.env, "", "general", kind, why, work_id)
            raise
        finally:
            self.bus.publish("work", {"inst_id": ""})
            self.bus.publish("agent", self.status())

    async def _sub_from_worker(self, inst_id: str, role: str, task: str) -> dict[str, Any]:
        return await self.intel(inst_id, task, role, by=f"{inst_id.split('-')[0]} Worker")

    async def _intel_task(self, inst_id: str) -> None:
        try:
            for role in ("intel", "smart"):
                if self.get_config().sub_enabled(role):
                    await self.intel(inst_id, role=role)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 情报查不到不影响交易 Worker
            self._note("alert", f"{inst_id} 的情报 Worker 这次没查成：{str(exc)[:160]}", inst_id=inst_id)

    def reset_model(self) -> None:
        self._models.clear()
        self._worker_agents.clear()
        self._task_agents.clear()
        self._router_agent = self._gate_agent = self._review_agent = None

    # ---------- Worker 的工作记忆（存在 Worker 之外，重启不丢） ----------

    def memory(self, env: str, inst_id: str) -> list[dict[str, Any]]:
        return self.store.get(f"worker_mem:{env}:{inst_id}", [])

    def _remember(self, env: str, inst_id: str, note: str) -> None:
        notes = distinct_notes([*self.memory(env, inst_id), {"ts": now_ms(), "note": note}])
        self.store.set(f"worker_mem:{env}:{inst_id}", notes[-20:])

    def _memory_text(self, env: str, inst_id: str, keep: int = 6) -> str:
        """给 Worker 看的工作记忆：说的是同一件事的只留最新一条，最多 keep 条——同一句话反复出现会让它越想越缩手。"""
        return "；".join(n["note"] for n in distinct_notes(self.memory(env, inst_id))[-keep:])

    async def start(self) -> None:
        if self.running:
            return
        self._model(self.get_config().worker_model)  # 模型没配好就在这里报错，别等到半夜才发现
        await self.trader.account()  # 没授权也在这里报错
        self.running = True
        self.last_error = ""
        self._beat = time.time()
        self._task = asyncio.create_task(self._loop(), name="ouy-watcher")
        self._watchdog_task = asyncio.create_task(self._watch_loop(), name="ouy-watchdog")
        self._note("system", "Agent 已开始盯盘", {"instruments": self.get_config().instruments, "mode": self.get_config().mode})
        self.bus.publish("agent", self.status())

    async def stop(self, reason: str = "已手动停止") -> None:
        if not self.running:
            return
        self.running = False
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
        if self._watchdog_task and self._watchdog_task is not asyncio.current_task():
            self._watchdog_task.cancel()
        for st in self.states.values():
            if st.observe_task:
                st.observe_task.cancel()
            st.quote_watch, st.observe_price, st.pending_observation = QuoteWatch(), None, None
            if st.task and not st.task.done() and st.task is not asyncio.current_task():
                st.task.cancel()
            st.reviewing = False
        self._note("system", f"Agent 已停止盯盘：{reason}")
        self.bus.publish("agent", self.status())

    def _note(self, kind: str, title: str, body: dict[str, Any] | None = None, inst_id: str = "") -> None:
        self.bus.publish("journal", self.store.log(self.get_config().env, kind, title, body, inst_id=inst_id))

    def _state(self, inst_id: str) -> InstState:
        st = self.states.get(inst_id)
        if st is None:
            st = self.states[inst_id] = InstState()
            st.report = self.store.get(f"report:{self.get_config().env}:{inst_id}")
        return st

    def status(self) -> dict[str, Any]:
        cfg = self.get_config()
        now = time.time()
        self._expire_subs()
        return {
            "running": self.running,
            "error": self.last_error,
            "pool": {
                name: {"running": lim.running_count, "waiting": lim.waiting_count, "max": lim.max_running}
                for name in ("workers", "subagents")
                for lim in [self._limiter(cfg, name)]
            },
            "router_adhoc": self.router_adhoc[-6:],
            "general": dict(self.general),
            "watchdog": {**self.watchdog, "beat_age": int(now - self._beat) if self.running and self._beat else None},
            "instruments": {
                inst: {
                    "last": st.last_px,
                    "reviewing": st.reviewing,
                    "last_review_ts": int(st.last_review_ts * 1000) if st.last_review_ts else None,
                    "next_review_in": max(int(st.next_due_ts - now), 0) if self.running and st.next_due_ts else None,
                    "last_reason": st.last_reason,
                    "error": st.last_error,
                    "style": cfg.style_for(inst),
                    "stance": cfg.stance_for(inst),
                    "entry_mode": cfg.entry_mode_for(inst),
                    "fast": {"mode": cfg.fast_entry_for(inst), "on": self._fast_on(cfg, inst), "on_signal": (st.report or {}).get("on_signal") or "verify", "last": st.fast_last,
                             "shift": (self.strategies.metrics.get(inst) or {}).get("shift")},
                    "turn": turn_state(*(self.strategies.fresh_metrics(cfg.env, inst).get(k) for k in ("shift", "turn")), st.last_retest),
                    "quant": {
                        "opening_quote": st.opening_quote if st.opening_quote.get("env") == cfg.env else None,
                        "last_retest": st.last_retest,
                        "sample_ts": (self.strategies.metrics.get(inst) or {}).get("ts"),
                        "mode": cfg.quant_mode_for(inst),
                        "label": QUANT_MODES[cfg.quant_mode_for(inst)]["label"],
                        "desc": QUANT_MODES[cfg.quant_mode_for(inst)]["desc"],
                        "bias": cfg.quant_bias_for(inst),
                        "vol_ratio": cfg.worker(inst).quant_vol_ratio,
                        "reason": cfg.worker(inst).quant_reason,
                        "by": cfg.worker(inst).quant_by,
                        "narrative": (self.strategies.metrics.get(inst) or {}).get("narrative") or "",
                    },
                    "wake": cfg.wake_for(inst),
                    "model": cfg.model_for(inst),
                    "paused": cfg.worker(inst).paused,
                    "standby": cfg.worker(inst).standby,
                    "override": cfg.worker(inst).model_dump(),
                    "mode": cfg.worker(inst).mode or cfg.mode,
                    "effort": cfg.effort_for(inst),
                    "trigger_effort": cfg.trigger_effort_for(inst),
                    "skips": st.skips,
                    "last_gate": st.last_gate,
                    "illiquid": st.illiquid,
                    "fails": st.fails,
                    "memory": distinct_notes(self.memory(cfg.env, inst))[-5:],
                    "intel": self.brief(cfg.env, inst),
                    "intel_running": st.intel_running,
                    "team": self.team(cfg.env, inst),
                    "adhoc": st.adhoc[-6:],
                    "goal": self.goal(cfg.env, inst),
                    "awaiting": sum(1 for r in self.awaiting(cfg.env, inst) for c in r["calls"].values() if self._call_state(c)[0] == "waiting"),
                    "busy": st.busy,
                    "queue": st.waiting,
                    "inbox": len(self.inbox(cfg.env, inst)),
                    "strategies": self.strategy_rows(cfg.env, inst),
                    "order_guards": self.trader.order_guards(cfg.env, inst),
                    "rules": self.rules(cfg.env, inst),
                    "data": ({**self.strategies.metrics[inst], "log": list(st.data_log)[-12:]} if inst in self.strategies.metrics else None),
                    "algos": self._algo_rows(cfg, inst, st),
                    "regime": st.regime or None,
                    "tracks": [{"lot_id": int(k), **{f: v.get(f) for f in ("side", "entry", "r0", "peak", "peak_px", "peak_ts", "low", "now", "locked")}}
                               for k, v in self.lot_track(cfg.env).items() if v.get("inst") == inst and v.get("open")],
                    "report": st.report,
                }
                for inst in cfg.instruments
                for st in [self._state(inst)]
            },
        }

    # ---------- 盯盘循环 ----------

    async def _loop(self) -> None:
        failures = 0
        while self.running:
            cfg = self.get_config()
            self._beat = time.time()
            try:
                await self._tick(cfg)
                failures = 0
                self.last_error = ""
            except asyncio.CancelledError:
                raise
            except OkxError as exc:
                failures += 1
                self.last_error = str(exc)
                if exc.needs_login:
                    await self.stop("欧易授权已失效，请重新登录欧易")
                    return
                if failures in (3, 30):
                    self._note("alert", "盯盘读取欧易数据连续失败", {"message": str(exc), "times": failures})
            except Exception as exc:  # noqa: BLE001 - 盯盘循环不能因为一次意外就死掉
                failures += 1
                self.last_error = str(exc)
                log.exception("盯盘循环异常")
            # 币多了每一拍的请求也多：拉长间隔，免得被欧易限流（每个币至少留 0.75 秒）
            await asyncio.sleep(max(cfg.tick_seconds, len(cfg.instruments) * 0.75))

    async def _tick(self, cfg: AgentConfig) -> None:
        account = await self.trader.account(cfg.env)
        self.bus.publish("account", account.model_dump())
        cfg = self._adopt(cfg, account)
        await self._emergency(cfg, account)
        for inst_id in cfg.instruments:
            if not cfg.worker(inst_id).paused:
                try:
                    await self._check_goal(cfg, inst_id, account, time.time())
                except Exception as exc:  # noqa: BLE001
                    log.warning("核对目标失败 %s: %s", inst_id, exc)
        tickers = await asyncio.gather(*(self.market.ticker(i, cfg.env) for i in cfg.instruments), return_exceptions=True)
        now = time.time()
        prices = {i: _f(t.get("last")) for i, t in zip(cfg.instruments, tickers) if not isinstance(t, BaseException)}
        try:  # 挂单的撤单线、有效期：到了就撤，不等模型
            await self._guard_orders(cfg, account, prices)
        except Exception as exc:  # noqa: BLE001 - 撤单出错不能拖垮盯盘循环
            log.warning("核对挂单撤单条件失败：%s", exc)
        retests = {}
        self._algo_check(cfg.env, prices)
        for inst, ticker in zip(cfg.instruments, tickers):
            m = self.strategies.metrics.get(inst)
            if not isinstance(ticker, dict) or cfg.worker(inst).paused:
                continue
            st = self._state(inst)
            ts = int(_f(ticker.get("ts")))
            try:
                self._opening_sample(cfg.env, inst, st, ts, prices[inst], m or {}, int(now * 1000))
            except Exception as exc:  # noqa: BLE001 - 采样存储故障不阻断持仓保护
                log.warning("开盘采样失败 %s: %s", inst, exc)
            if not m:
                continue
            message = st.shift_retest.sample(time.time(), ts / 1000, prices[inst], m)
            value = (1 if st.shift_retest.key[1] == "long" else -1) * (st.shift_retest.stage % 2) if st.shift_retest.valid else None
            m["values"]["shift_failure"] = value
            m["quote_ts"] = ts
            if message:
                previous = st.last_retest or {}
                st.last_retest = {"ts": ts, "text": message, "value": value, "level": st.shift_retest.key[2]}
                self._emit(cfg.env, inst, "strategy", "result", message, None, {"shift_retest": True, **st.last_retest})
                woke = st.shift_retest.first_loss  # 之后的收回、再失守只记录，下一轮复盘能看到
                broke = st.shift_retest.key[1]
                self._algo(cfg.env, inst, "turn", ("盘中失守：" if st.shift_retest.stage % 2 else "盘中收回：") + message,
                           "叫醒 Worker 复核" if woke else "只记录（这次转折已经叫醒过一次）",
                           ("short" if broke == "long" else "long") if st.shift_retest.stage % 2 else broke, prices[inst])
                if woke:
                    retests[inst] = message
                    st.last_retest["wake_ts"] = ts
                else:
                    st.last_retest["wake_ts"] = previous.get("wake_ts", 0)
        try:  # 快的一层先走：策略条件满足就立刻下单，不等后面的模型复盘
            for inst_id in cfg.instruments:
                self._sync_observation_rules(cfg, inst_id)
            await self.strategies.tick(cfg, account, prices, {i: t for i, t in zip(cfg.instruments, tickers) if isinstance(t, dict)})
        except Exception as exc:  # noqa: BLE001 - 策略执行器出错不能拖垮盯盘循环
            log.exception("策略执行器出错：%s", exc)
        btc_alert = await self._watch_btc(cfg, dict(zip(cfg.instruments, tickers)), now)
        for inst_id, ticker in zip(cfg.instruments, tickers):
            st = self._state(inst_id)
            if isinstance(ticker, BaseException):
                st.last_error = str(ticker)
                continue
            st.prev_px, st.last_px = st.last_px or _f(ticker.get("last")), _f(ticker.get("last"))
            self.bus.publish("tick", {"inst_id": inst_id, "last": st.last_px, "ts": int(_f(ticker.get("ts"))) or now_ms()})
            if cfg.worker(inst_id).paused:
                st.had_position = account.position(inst_id) is not None
                continue
            pos = account.position(inst_id)
            try:
                await self._record(cfg, inst_id, st, now, pos)
            except Exception as exc:  # noqa: BLE001 - 记数据出错不能拖垮盯盘循环
                log.warning("记录器记数据失败 %s: %s", inst_id, exc)
            try:
                await self._algo_tick(cfg, inst_id, st, pos)
            except Exception as exc:  # noqa: BLE001 - 算法读数出错不能拖垮盯盘循环
                log.warning("算行情判断和候选位失败 %s: %s", inst_id, exc)
            if any(r["kind"] == "review" for r in self.awaiting(cfg.env, inst_id)):  # Worker 这一轮挂着等用户同意：不另起一轮，只看有没有过期
                if retest := retests.get(inst_id):
                    self.interject(inst_id, retest, "走势记录器")
                self.on_decided()
                st.had_position = pos is not None
                continue
            if cfg.intel_minutes and not st.intel_running and now - st.intel_ts >= cfg.intel_minutes * 60:
                st.intel_running, st.intel_ts = True, now
                asyncio.create_task(self._intel_task(inst_id), name=f"intel-{inst_id}")
            st.stale_order = self._stale_order(cfg, inst_id, account)
            st.has_mail = bool(self.inbox(cfg.env, inst_id))
            # 待命（空仓时）：不做首次 / 定时 / 波动复盘，只留触发线、入场区间、留言、持仓变化、挂单超时这些硬事件
            standby = cfg.worker(inst_id).standby and pos is None
            if standby and not st.last_review_ts:
                st.last_review_ts, st.last_review_px = now - cfg.min_review_seconds, st.last_px
            reason = self._trigger(cfg, st, pos, now)
            if standby and reason and (reason == "定时复盘" or reason.startswith("距上次复盘价格")):
                reason = None
            try:
                await self._watch_lots(cfg, inst_id, st, now)
            except Exception as exc:  # noqa: BLE001 - 盯回吐出错不能拖垮盯盘循环
                log.warning("盯仓位回吐失败 %s: %s", inst_id, exc)
            if st.lot_alert:  # 盈利在回吐 / 浮亏在扩大：不等定时复盘。Worker 正在想就直接送进这一轮，否则马上叫醒它
                if st.reviewing or st.run is not None:
                    if self._steer(cfg, inst_id, st.lot_alert, "盯盘系统"):
                        st.lot_alert = ""
                elif not reason or reason == "定时复盘" or reason.startswith("距上次复盘价格"):
                    reason, st.lot_alert = st.lot_alert, ""
            try:
                turned = await self._shift_watch(cfg, inst_id, st)
            except Exception as exc:  # noqa: BLE001
                turned = None
                log.warning("看走势变化失败 %s: %s", inst_id, exc)
            if turned:
                if st.reviewing or st.lock.locked() or st.run is not None:
                    self.interject(inst_id, turned, "走势记录器")
                else:
                    reason = f"{turned} 同时：{reason}" if reason else turned
            try:  # 快判：授权过的方向直接下单；没授权的马上叫醒 Worker 核实（比定时复盘、价格波动这些软事件优先）
                fast = None if standby else await self._fast(cfg, inst_id, st, now)
            except Exception as exc:  # noqa: BLE001 - 快判出错不能拖垮盯盘循环
                fast = None
                log.warning("快判失败 %s: %s", inst_id, exc)
            if fast and (not reason or reason == "定时复盘" or reason.startswith("距上次复盘价格")):
                reason = fast
            retest = retests.get(inst_id)
            if retest:
                if not standby:
                    if st.reviewing or st.lock.locked() or st.run is not None:
                        self.interject(inst_id, retest, "走势记录器")
                    else:
                        reason = f"{retest} 同时：{reason}" if reason else retest
            # 软事件（价格波动、定时）先过 Router 的第二级过滤；硬事件（首次、持仓变化、触及价位、进入入场区间、BTC 异动）直接唤醒
            soft = bool(reason) and (reason == "定时复盘" or reason.startswith("距上次复盘价格"))
            if not reason and not standby and cfg.wake_for(inst_id) == "always" and now - st.last_review_ts >= cfg.min_review_seconds:
                reason = "持续盯盘"
            if (not reason and btc_alert and inst_id != BTC and self._asset.get(inst_id, "crypto") == "crypto"
                    and account.position(inst_id) and now - st.last_review_ts >= cfg.min_review_seconds):
                reason = f"{btc_alert}，这个品种有持仓，检查要不要跟着处理"
            if cfg.env == "demo" and st.report:
                self._sample_observation(cfg, inst_id, st, _f(ticker.get("ts")) / 1000, now, bool(reason))
            if reason and not st.reviewing:
                st.reviewing = True
                st.review_started = now
                gate = soft and cfg.slow_path and cfg.wake_for(inst_id) == "event" and account.position(inst_id) is None
                st.task = asyncio.create_task(self._review_task(inst_id, reason, gate), name=f"review-{inst_id}")

    def _sample_observation(self, cfg: AgentConfig, inst: str, st: InstState, ts: float, now: float, normal_wake: bool) -> None:
        if not (now - 15 <= ts <= now + 2):
            return
        report = st.report or {}
        levels = {v["price"] for v in report.get("watch_levels", [])}
        plan = report.get("plan") or {}
        levels.update(p for p in (plan.get("entry_low"), plan.get("entry_high"), st.observe_price) if p)
        signal = st.quote_watch.sample(ts, st.last_px, levels, cfg.wake_move_pct)
        if signal:
            st.pending_observation = (ts, signal)
            self._note("system", f"{inst} 模拟盘预警：{signal}", inst_id=inst)
        if not st.pending_observation:
            return
        if now - st.pending_observation[0] > 15:
            st.pending_observation = None
            return
        if normal_wake or st.reviewing or st.lock.locked():
            return
        if st.observe_task and not st.observe_task.done():
            return
        event_ts, signal = st.pending_observation
        st.pending_observation = None
        st.observe_task = asyncio.create_task(self._observe_only(cfg, inst, st, report, signal, event_ts))

    async def _observe_only(self, cfg: AgentConfig, inst: str, st: InstState, report: dict[str, Any], signal: str, ts: float) -> None:
        """只读推演，既拿不到交易工具，也不改普通复盘的计划、时钟、审批和止损。"""
        try:
            worker = Agent(
                self._model(cfg.model_for(inst)), output_type=WatchAdvice, retries=0,
                max_concurrency=self._limiter(cfg, "workers"),
                instructions="你是这个币的 Worker，现在只重估观察位。报价不等于收盘确认。结构依据不足就保持原观察位；不要建议立即交易。",
            )
            prompt = json.dumps({
                "品种": inst, "预警": signal, "最近采样(时间、报价)": list(st.quote_watch.samples),
                "上次复盘": {k: report.get(k) for k in ("summary", "watch_levels", "plan", "ts")},
            }, ensure_ascii=False)
            result = await asyncio.wait_for(worker.run(prompt, model_settings=_effort(cfg.trigger_effort_for(inst), "ouy-observe"),
                                                       usage_limits=UsageLimits(request_limit=1)), 12)
            if (self.get_config().env != "demo" or self.states.get(inst) is not st or st.report is not report
                    or self.get_config().worker(inst).paused or time.time() - ts > 15 or st.reviewing):
                return
            advice = result.output
            if advice.observe_price:
                st.observe_price = advice.observe_price
            self._note("system", f"{inst} 只读重估：{advice.reason}", {"observe_price": st.observe_price}, inst_id=inst)
        except Exception as exc:  # 预警失败不能影响盯盘或现有保护单
            log.warning("模拟盘观察位重估失败 %s：%s", inst, type(exc).__name__)

    async def _watch_btc(self, cfg: AgentConfig, tickers: dict[str, Any], now: float) -> str | None:
        """不管盯盘列表里有没有 BTC，都时刻看着它：BTC 短时间大幅波动时返回一句提醒，用来叫醒有持仓的品种。"""
        ticker = tickers.get(BTC)
        if ticker is None:
            try:
                ticker = await self.market.ticker(BTC, cfg.env)
            except Exception:  # BTC 行情偶尔拿不到不影响别的品种
                return None
            if not isinstance(ticker, BaseException):
                self.bus.publish("tick", {"inst_id": BTC, "last": _f(ticker.get("last")), "ts": now_ms()})
        if isinstance(ticker, BaseException):
            return None
        price = _f(ticker.get("last"))
        if not price:
            return None
        if self._btc_ref is None or now - self._btc_ref[1] > BTC_WINDOW:
            self._btc_ref = (price, now)
            return None
        ref = self._btc_ref[0]
        move = (price / ref - 1) * 100
        if abs(move) < BTC_ALERT_PCT:
            return None
        self._btc_ref = (price, now)
        alert = f"BTC 短时间{'急涨' if move > 0 else '急跌'} {abs(move):.2f}%（{ref:g} → {price:g}）"
        self._note("system", alert + "，正在检查所有持仓", {"move_pct": round(move, 3)})
        return alert

    ALGOS = {"regime": "行情判断", "cand": "算法候选位", "turn": "转折", "fast": "快判", "lock": "保盈线", "lot": "持仓提醒", "rule": "触发器 / 记录器"}

    def _algo(self, env: str, inst: str, algo: str, text: str, effect: str, side: str = "", px: float = 0.0, atr: float | None = None) -> None:
        """记一条算法事件：哪个算法在什么价位算出了什么、触发了什么。带方向的会在 15 分钟和 1 小时后核对价格有没有照它说的走。"""
        try:
            m = self.strategies.metrics.get(inst) or {}
            st = self.states.get(inst)
            self.store.add_algo_event(env, inst, algo, side, px or (st.last_px if st else 0.0), atr or (m.get("values") or {}).get("atr") or 0.0, text, effect)
        except Exception as exc:  # noqa: BLE001 - 记不上不能影响交易
            log.warning("算法事件没记上：%s", exc)

    # 每个算法归哪一层：同一层里的几个算法看的是同一类事情，页面按层摆，免得看起来像几套各说各的
    ALGO_LAYERS = {
        "read": "看行情 · 只记录，不叫醒也不下单",
        "turn": "找转折 · 一个算法分三个阶段（苗头 → 确认 → 盘中失守 / 收回），不下单",
        "entry": "给入场位 · 候选只是参考；快判和触发器按你的设置才会下单",
        "hold": "管持仓 · 只移止损和叫醒 Worker，不开仓",
    }
    # 以前分开记的「结构转折」「盘中转折位」现在都算「转折」这一个算法的不同阶段；库里的旧记录读出来时归过去
    ALGO_MERGED = {"shift": "turn", "retest": "turn"}
    ALGO_LAYER = {"tape": "read", "open": "read", "regime": "read", "turn": "turn",
                  "cand": "entry", "fast": "entry", "rule": "entry", "lock": "hold"}

    def _algo_check(self, env: str, prices: dict[str, float]) -> None:
        """带方向的算法事件：15 分钟、1 小时后各看一次价格，走了至少半个 ATR 才算对或错，否则算持平。"""
        if time.time() - self._algo_checked < 20:
            return
        self._algo_checked = time.time()
        for ev in self.store.algo_events(env, pending=True, limit=120):
            px, age = prices.get(ev["inst"]), (now_ms() - ev["ts"]) / 60_000
            checks = dict(ev["checks"])
            if not px or not ev["px"]:
                if age > 180:
                    self.store.check_algo_event(ev["id"], checks, True)
                continue
            move = (px / ev["px"] - 1) * 100 * (1 if ev["side"] == "long" else -1)
            need = max(0.5 * ev["atr"] / ev["px"] * 100, 0.05)
            for name, minutes in (("15m", 15), ("1h", 60)):
                if name not in checks and minutes <= age < minutes * 3:
                    checks[name] = {"px": px, "move": round(move, 3), "ok": "right" if move >= need else "wrong" if move <= -need else "flat"}
            if checks != ev["checks"] or age >= 180:
                self.store.check_algo_event(ev["id"], checks, "1h" in checks or age >= 180)

    def algo_report(self, env: str, inst: str) -> dict[str, Any]:
        events = [{**ev, "algo": self.ALGO_MERGED.get(ev["algo"], ev["algo"])} for ev in self.store.algo_events(env, inst, 400)]
        stats: dict[str, dict[str, int]] = {}
        for ev in events:
            got = (ev["checks"].get("15m") or {}).get("ok")
            if got:
                s = stats.setdefault(ev["algo"], {"right": 0, "wrong": 0, "flat": 0})
                s[got] += 1
        return {"events": events[:200], "stats": stats, "labels": self.ALGOS, "layers": self.ALGO_LAYERS}

    def _algo_rows(self, cfg: AgentConfig, inst: str, st: InstState) -> list[dict[str, Any]]:
        """每个算法现在在不在跑、上次算是什么时候、读数是什么：给界面的「算法」页。"""
        m = self.strategies.metrics.get(inst) or {}
        fresh = bool(m) and now_ms() - m.get("ts", 0) <= DATA_STALE_MS
        watching = self.running and not cfg.worker(inst).paused
        shift, turn, oq = m.get("shift"), m.get("turn"), st.opening_quote
        fast_on = self._fast_on(cfg, inst)
        tracks = {k: v for k, v in self.lot_track(cfg.env).items() if v.get("inst") == inst and v.get("open")}
        rules = [r for r in self.store.strategies(cfg.env, inst) if r["kind"] == "trigger" and r["status"] in ("armed", "active")]
        off = "盯盘没开" if not self.running else "这个 Worker 已暂停" if not watching else "等第一次采样"

        def row(i: str, label: str, on: Any, ts: Any, reading: str, does: str) -> dict[str, Any]:
            return {"id": i, "label": label, "on": bool(on), "ts": ts or None, "reading": reading, "does": does, "layer": self.ALGO_LAYER[i]}

        good = [c for c in st.regime.get("candidates") or [] if c["ok"]]
        cand_text = (
            "等下一根结构周期 K 线" if not st.regime
            else f"{len(good)} 条合格：" + "；".join(f"{c['strategy']}{'多' if c['side'] == 'long' else '空'} {c['entry']:g}" for c in good[:3]) if good
            else "持仓中，不出候选" if st.regime.get("holding") else "这一轮没有合格的候选"
        )
        state = turn_state(shift, turn, st.last_retest)
        turn_ts = state["ts"] if state else None
        turn_text = f"{state['label']}：{state['text']}" if state else "没有转折：最近的 5 分钟 K 线没有收盘越过摆动点，也没有扫流动性后收回"
        fast_does = {"agent": "Worker 授权的方向直接下单，其余叫醒它核实", "verify": "出信号叫醒 Worker 核实"}.get(cfg.fast_entry_for(inst), "已关闭")
        fast_bad = sorted(k.removeprefix("快判·") for k in losing(cfg.style_for(inst)) if k.startswith("快判·"))
        if fast_bad and fast_does != "已关闭":
            fast_does += f"；回测扣费后亏的几类（{'、'.join(fast_bad)}）只记录"
        return [
            row("tape", "走势记录", watching and fresh, m.get("ts"), (m.get("narrative") or "")[:160] if fresh else off, "每 10 秒算一次 5 分钟走势，只记录"),
            row("open", "开盘记录", watching and oq.get("env") == cfg.env, oq.get("ts"),
                f"开盘 {oq['session_open']:g}，现在 {oq.get('session_pct', 0):+.2f}%；这根 5 分钟 {oq.get('bar_pct', 0):+.2f}%" if oq.get("session_open") else "还没有覆盖到开盘的数据",
                "每个报价对照开盘价，只记录"),
            row("regime", "行情判断", watching and bool(st.regime), st.regime.get("ts"),
                f"{st.regime.get('label', '')}：{st.regime.get('why', '')}" if st.regime else "等下一根结构周期 K 线", "程序自己按结构周期算，决定给 Worker 哪几类入场候选"),
            row("turn", "转折", watching and fresh, turn_ts, turn_text if fresh else off,
                ("苗头只写进 Worker 的输入；确认时撤掉过时挂单并叫醒 Worker；" if self._shift_actions_on(cfg, inst) else "确认转折时，有生效入场计划就叫醒 Worker 复核；")
                + "确认后盘中第一次失守再叫醒一次，之后的来回只记录"),
            row("cand", "算法候选位", watching and bool(st.regime), st.regime.get("ts"), cand_text, "给 Worker 当参考位，不下单；回测扣费后亏的打法会标出来"),
            row("fast", "快判", watching and fast_on, (st.fast_last or {}).get("ts"),
                f"{st.fast_last['text']} → {st.fast_last.get('result') or st.fast_last['status']}" if st.fast_last else "还没有信号",
                fast_does if fast_on else "已关闭，或这个风格不看 1 分钟"),
            row("rule", "触发器 / 记录器", watching and bool(rules), max((r["ts"] for r in rules), default=0),
                f"{len(rules)} 条在等条件" if rules else "没有在等的规则", "条件满足时程序直接执行预设操作"),
            row("lock", "持仓轨迹 / 保盈线", watching and bool(tracks), max((t.get("peak_ts", 0) for t in tracks.values()), default=0),
                "；".join(f"#{k} " + self.track_text(v) for k, v in tracks.items()) or "没有持仓",
                "浮盈够了直接移止损；回吐、浮亏、结构反向叫醒 Worker" if cfg.profit_lock else "保盈线已关，只叫醒 Worker"),
        ]

    def lot_track(self, env: str) -> dict[str, dict[str, Any]]:
        """每笔仓的持仓轨迹（存库，重启不丢）：初始风险 r0、浮盈最高 / 最低（R）、保盈线是否已经执行、回吐和浮亏叫醒到哪了。"""
        return self.store.get(f"lot_track:{env}", {})

    def _lock_r(self, cfg: AgentConfig, inst_id: str, lot: dict[str, Any]) -> float:
        """这笔仓的保盈线：Worker 在 manage 规则里写了 lock_r 就按它（0 = 不要），否则按风格默认。"""
        if not cfg.profit_lock:
            return 0.0
        for row in self.store.strategies(cfg.env, inst_id):
            if row["kind"] == "manage" and row["status"] in ("armed", "active") and row["params"].get("lot_id") == lot["id"] and "lock_r" in row["params"]:
                return float(row["params"]["lock_r"] or 0)
        return LOCK_R.get(lot["style"], 0.5)

    async def _lock_profit(self, cfg: AgentConfig, inst_id: str, lot: dict[str, Any], mark: dict[str, Any], px: float, why: str) -> str:
        """把这笔仓的止损移到「入场价 + 往返成本」（扣费后不亏）；价格已经回到那条线里面就退一步移到入场价。返回做了什么，没做成返回原因。"""
        long, entry, stop = lot["side"] == "long", lot["entry_px"], lot["stop_loss"]
        sign = 1 if long else -1
        buffer = entry * (cfg.taker_fee_pct * 2 + 0.02) / 100
        if stop and (stop - entry - sign * buffer) * sign >= 0:
            mark["locked"] = stop
            return ""
        room = max(0.3 * mark["r0"], entry * 0.001)  # 新止损离现价至少留这么远：贴着现价移等于直接平仓
        tighter = [p for p in (entry + sign * buffer, entry) if (px - p) * sign >= room and (not stop or (p - stop) * sign > 0)]
        if not tighter and (px - entry) * sign > 0:
            return ""  # 还在盈利，只是离成本线太近：下一拍再看
        if not tighter:
            mark["locked"] = "missed"
            return f"价格已经回到成本线 {entry:g} {'下' if long else '上'}方，来不及锁"
        target = float(f"{tighter[0]:.8g}")
        result = await self.trader.execute(TradeAction(kind="set_stops", inst_id=inst_id, lot_id=lot["id"], stop_loss=target, reason=why), cfg.env, source="guard")
        if result.status == "executed":
            mark["locked"] = target
            return f"止损已移到 {target:g}（{'入场价 + 往返成本，扣费后不亏' if target != entry else '入场价，不含手续费'}）"
        mark["lock_fail"] = mark.get("lock_fail", 0) + 1
        mark["lock_retry"] = time.time() + 30
        return f"没做成：{result.message[:160]}"

    async def _watch_lots(self, cfg: AgentConfig, inst_id: str, st: InstState, now: float) -> None:
        """持仓轨迹记录器 + 持仓保护。每个节拍按每笔仓的初始止损距离（R）记浮盈最高点和最低点，存库。
        - 保盈线（程序直接做）：浮盈最高到过 LOCK_R 后把止损移到入场价 + 往返成本，盈利单不再变亏损单。
        - 回吐 / 浮亏 / 结构反向：第一时间交给 Worker 判断要不要减仓、平仓、反手——规则只负责发现，不替它做方向判断。
        - 保底：回吐继续扩大而 Worker 迟迟没给结论（模型慢或出错），先把止损移到保本。"""
        if not st.last_px:
            return
        key = f"lot_track:{cfg.env}"
        track, changed = self.lot_track(cfg.env), False
        lots = [l for l in self.trader.lots(cfg.env, inst_id) if l["status"] == "open"]
        alive = {str(l["id"]) for l in lots}
        for lid, m in list(track.items()):
            if m.get("inst") == inst_id and m.get("open") and lid not in alive:
                m.update(open=False, closed_ts=now_ms())
                changed = True
            if not m.get("open") and now_ms() - m.get("closed_ts", 0) > 2 * 86_400_000:
                del track[lid]
                changed = True
        martingale = any(r["kind"] == "martingale" and r["status"] in ("armed", "active") for r in self.store.strategies(cfg.env, inst_id))
        metrics = self.strategies.metrics.get(inst_id) or {}
        shift = metrics.get("shift") if 0 <= now_ms() - metrics.get("ts", 0) <= DATA_STALE_MS else None
        for lot in lots:
            long, entry, px = lot["side"] == "long", lot["entry_px"], st.last_px
            if not entry:
                continue
            m = track.get(str(lot["id"]))
            if m is None:
                good = lot["stop_loss"] and (entry - lot["stop_loss"]) * (1 if long else -1) > 0
                m = track[str(lot["id"])] = {"inst": inst_id, "side": lot["side"], "entry": entry, "r0": abs(entry - lot["stop_loss"]) if good else entry * 0.01,
                                             "opened": lot["ts"], "open": True, "peak": 0.0, "peak_px": entry, "peak_ts": now_ms(), "low": 0.0,
                                             "woke": 0.0, "woke_ts": 0.0, "loss": False, "be": False}
                changed = True
            gain = ((px - entry) if long else (entry - px)) / m["r0"]
            if gain >= m["peak"] + 0.02:
                m.update(peak=round(gain, 3), peak_px=px, peak_ts=now_ms(), woke=0.0)  # 又创新高：回吐重新算
                changed = True
            if gain <= m["low"] - 0.02:
                m["low"] = round(gain, 3)
                changed = True
            who = f"第 {lot['id']} 笔（{'多' if long else '空'}单，入场 {entry:g}）"
            hands_off = cfg.hands_off_manual and lot["source"] in MANUAL_SOURCES
            # 保盈线：程序直接移止损（Worker 不用在场，模型出错时也照样执行）
            lock_r = self._lock_r(cfg, inst_id, lot)
            if (lock_r and not m.get("locked") and m["peak"] >= lock_r and not martingale and not hands_off
                    and m.get("lock_fail", 0) < 3 and time.time() >= m.get("lock_retry", 0)):
                done = await self._lock_profit(cfg, inst_id, lot, m, px, f"保盈线：浮盈最高到过 {m['peak']:+.2f}R（≥ {lock_r:g}R）")
                changed = True
                if done:
                    text = f"保盈线：{who}浮盈最高到过 {m['peak']:+.2f}R，现在 {gain:+.2f}R，{done}。"
                    ok = m.get("locked") not in (None, "missed")
                    self._emit(cfg.env, inst_id, "strategy", "action" if ok else "error", text, None, {"lot_id": lot["id"], "profit_lock": True})
                    self._algo(cfg.env, inst_id, "lock", text, "程序直接移止损，并告诉 Worker" if ok else "没做成", px=px)
                    st.lot_alert = st.lot_alert or text + "这是程序的保护动作，不是平仓；判断要不要继续拿、减仓或调整止盈。"
            # 结构反向：5 分钟图刚确认和这笔仓相反的转折
            if shift and shift.get("bars_ago", 9) <= 1 and shift["side"] != lot["side"] and m.get("shift_ts") != shift["ts"]:
                m["shift_ts"] = shift["ts"]
                changed = True
                self._algo(cfg.env, inst_id, "lot", f"结构反向：{who}和 5 分钟刚确认的转折方向相反，现在 {gain:+.2f}R", "叫醒 Worker 判断入场理由还在不在", shift["side"], px)
                locked = ""
                if not m.get("locked") and not martingale and not hands_off and cfg.profit_lock and gain > 0:
                    locked = await self._lock_profit(cfg, inst_id, lot, m, px, "结构反向：先锁住不亏")
                st.lot_alert = (
                    f"结构反向：{who}和 5m 刚确认的转折（{shift['why']}）方向相反，现在 {gain:+.2f}R，浮盈最高到过 {m['peak']:+.2f}R。"
                    + (f"程序已把{locked}。" if locked and m.get("locked") not in (None, "missed") else "")
                    + "这笔单的入场理由还成立吗？成立就按原计划拿；不成立就减仓或平仓，不要等硬止损。是否反手是另一笔交易，要有自己的入场和止损。"
                )
            arm, pct = GIVEBACK[lot["style"]]
            arm, pct = min(arm, lock_r or arm), cfg.giveback_pct or pct
            back = (m["peak"] - gain) / m["peak"] * 100 if m["peak"] > 0 else 0.0
            if cfg.giveback_wake and m["peak"] >= arm and back >= pct and back >= m["woke"] + 15:
                m["woke"], m["woke_ts"] = back, now
                changed = True
                self._algo(cfg.env, inst_id, "lot", f"利润回吐：{who}最高 {m['peak']:+.2f}R，现在 {gain:+.2f}R", "叫醒 Worker 判断减仓还是继续拿", px=px)
                st.lot_alert = (
                    f"利润回吐：{who}浮盈最高到过 {m['peak']:+.2f}R（{m['peak_px']:g}），现在 {gain:+.2f}R，已经回吐 {back:.0f}%。"
                    "先判断这是趋势里的正常震荡，还是趋势走弱 / 已经反转：趋势没变就继续拿；走弱就减仓或平仓把利润锁住；"
                    "方向已经反过来就平仓，并判断值不值得反手。在结论里说清楚你判断的依据和做了什么。"
                )
            elif cfg.loss_wake_r and not m["loss"] and gain <= -cfg.loss_wake_r:
                m["loss"], m["woke_ts"] = True, now
                changed = True
                self._algo(cfg.env, inst_id, "lot", f"浮亏扩大：{who}现在 {gain:+.2f}R", "叫醒 Worker 判断要不要提前止损", px=px)
                st.lot_alert = (
                    f"浮亏扩大：{who}现在 {gain:+.2f}R" + (f"（浮盈最高到过 {m['peak']:+.2f}R）" if m["peak"] > 0.2 else "")
                    + f"，离止损只剩 {max(1 + gain, 0) * 100:.0f}% 的距离。判断这是震荡里的正常回撤还是方向做错了：方向没错就按原计划拿着；"
                    "错了就提前止损，不要等止损被打掉；趋势已经明确反过来就平仓，并判断值不值得反手。在结论里说清楚依据。"
                )
            elif m["loss"] and gain > -cfg.loss_wake_r / 2:
                m["loss"] = False
                changed = True
            # 保底：回吐还在扩大，而叫醒之后 Worker 一直没出结论（模型慢或出错）——先把止损移到保本
            behind = (lot["stop_loss"] or 0) < entry if long else (not lot["stop_loss"] or lot["stop_loss"] > entry)
            if (cfg.giveback_backstop and m["woke_ts"] and not m["be"] and behind and gain > 0.1
                    and back >= min(pct * 1.6, 85) and st.last_review_ts < m["woke_ts"] and not hands_off):
                m["be"] = True
                changed = True
                what = f"{who}利润已经回吐 {back:.0f}%，Worker 还没给出结论，先把止损移到保本 {entry:g}"
                try:
                    result = await self.trader.execute(TradeAction(kind="set_stops", inst_id=inst_id, lot_id=lot["id"], stop_loss=entry, reason=what), cfg.env, source="guard")
                    self._emit(cfg.env, inst_id, "strategy", "action" if result.status == "executed" else "error", f"{what}：{result.message}", None, result.model_dump(mode="json"))
                except Exception as exc:  # noqa: BLE001
                    self._emit(cfg.env, inst_id, "strategy", "error", f"{what}，但没做成：{str(exc)[:200]}", None)
            if m.get("now") != round(gain, 2):
                m["now"] = round(gain, 2)
        if changed:
            self.store.set(key, track)

    @staticmethod
    def track_text(m: dict[str, Any]) -> str:
        """一笔仓的轨迹，一句话：给 Worker 和平仓复盘看。"""
        peak = (f"浮盈最高 {m['peak']:+.2f}R（{m['peak_px']:g}，{time.strftime('%H:%M', time.localtime(m['peak_ts'] / 1000))}）"
                if m.get("peak", 0) > 0 else "还没有浮盈过")
        lock = m.get("locked")
        if lock == "missed":
            lock_text = "保盈线没来得及执行（价格先回到了成本线里面）"
        elif isinstance(lock, (int, float)) and not isinstance(lock, bool):
            lock_text = f"保盈线已执行，止损在 {lock:g}"
        else:
            lock_text = "保盈线未触发"
        now = f"现在 {m['now']:+.2f}R，" if "now" in m else ""
        return f"{peak}，最深 {m.get('low', 0):+.2f}R，{now}{lock_text}（1R = {m['r0']:.6g}）"

    def _stale_order(self, cfg: AgentConfig, inst_id: str, account: AccountState) -> str:
        """Agent 自己挂的单太久没成交：叫醒 Worker 处理（改市价或撤单），不让它一直挂着。"""
        limit = cfg.stale_minutes_for(cfg.style_for(inst_id))
        mine = {l["ord_id"] for l in self.trader.lots(cfg.env, inst_id) if l["status"] == "working" and l["source"] == "agent"}
        ages = [(now_ms() - o.created_at) / 60000 for o in account.orders if o.inst_id == inst_id and o.ord_id in mine and o.created_at]
        if not ages or max(ages) < limit:
            return ""
        return f"挂单已经 {int(max(ages))} 分钟没成交（上限 {limit} 分钟）：改用市价进场或撤单，不要原价重挂"

    def _stale_lines(self, cfg: AgentConfig, inst_id: str, st: InstState, px: float) -> str:
        """Worker 现在的限价挂单、触发器、计划入场位、观察线离现价多远；结合结构转折（shift）和波动率可达性标出过时的线。"""
        if not px or px <= 0:
            return ""

        def far(level: float) -> str:
            return f"{level:g}（{(level / px - 1) * 100:+.2f}%）"

        m = self.strategies.fresh_metrics(cfg.env, inst_id)
        shift, atr = m.get("shift"), (m.get("values") or {}).get("atr")
        spec = STYLES[cfg.style_for(inst_id)]
        rows = []
        for g in self.trader.order_guards(cfg.env, inst_id):
            lv, side = g.get("price"), g.get("side") or "long"
            if not lv:
                continue
            stale = line_status(px, lv, side, shift, atr, spec["plan_minutes"], spec["bar_minutes"])
            rows.append(f"欧易限价{'买' if side == 'long' else '卖'}单 {far(lv)}" + (f"【过时：{stale}】" if stale else ""))
        for row in self.store.strategies(cfg.env, inst_id):
            p = row["params"]
            entry = row["status"] in ("proposed", "armed") and (row["kind"] in ("breakout", "pullback") or (row["kind"] == "trigger" and str(p.get("do", "")).startswith("open")))
            level = p.get("price") or p.get("trigger") or (p.get("level") if str(p.get("when", "")).startswith(("price", "close")) else None)
            if not entry or not level:
                continue
            long, tp, sl = row["side"] == "long", p.get("take_profit"), p.get("stop_loss")
            stale = ("价格没到入场位就先到了止盈" if tp and (px >= tp) == long and (level - tp) * (px - tp) <= 0 else "") or line_status(
                px, level, row["side"] or ("long" if long else "short"), shift, atr, spec["plan_minutes"], spec["bar_minutes"]
            ) or ("离现价太远" if sl and abs(px - level) >= 2.5 * abs(level - sl) else "")
            rows.append(f"#{row['id']} {STRATEGY_KINDS[row['kind']]['label']} 入场 {far(level)}" + (f"【过时：{stale}】" if stale else ""))
        plan = (st.report or {}).get("plan")
        if plan and plan.get("entry_low") and plan.get("entry_high"):
            mid, tps = (plan["entry_low"] + plan["entry_high"]) / 2, plan.get("take_profits") or []
            passed = bool(tps) and (mid - tps[0]) * (px - tps[0]) <= 0
            stale = ("价格没到入场区间就先到了目标 1" if passed else "") or line_status(
                px, mid, plan.get("direction") or "long", shift, atr, spec["plan_minutes"], spec["bar_minutes"]
            ) or ("离现价太远" if plan.get("stop_loss") and abs(px - mid) >= 2.5 * abs(mid - plan["stop_loss"]) else "")
            rows.append(f"上次计划入场 {plan['entry_low']:g}~{plan['entry_high']:g}（中间价 {far(mid)}）" + (f"【过时：{stale}】" if stale else ""))
        for w in (st.report or {}).get("watch_levels") or []:
            if w.get("price"):
                rows.append(f"观察线 {far(w['price'])}" + ("【过时：离现价太远】" if abs(w["price"] / px - 1) * 100 >= max(spec["limit_dist_pct"] * 2, 1.5) else ""))
        if not rows:
            return "你现在没有等着的入场触发器、计划和观察线：空仓时直接给出 plan（程序会在欧易挂真实限价单），需要观察的价位写进 watch_levels。"
        tail = "标了【过时】的不要再沿用：本轮直接按最新结构给出新的 plan（程序会自动撤旧挂新），触发器用 edit_trigger / cancel_strategy 清理。" if any("过时" in r for r in rows) else ""
        return "你的线离现价：" + "；".join(rows) + "。" + tail

    # 超短线的 Worker 默认带的记录器：看哪些数据、到多少、两次之间至少隔几分钟。到了只叫醒 Worker；Worker 可以给它们设上直接执行的操作
    def _opening_sample(self, env: str, inst: str, st: InstState, ts: int, px: float, metrics: dict, clock: int) -> None:
        if not math.isfinite(px) or px <= 0 or not -2000 <= clock - ts <= 15_000:
            return
        if st.opening_quote.get("env") == env and ts <= st.opening_quote.get("ts", 0):
            return
        sample = {"env": env, "ts": ts, "price": px}
        if metrics.get("source_env") == env:
            day = metrics.get("session") or {}
            anchor, _ = session_anchor(metrics["asset"], ts / 1000)
            if day.get("anchor_ms") == anchor and day.get("open"):
                sample.update(anchor_ms=anchor, session_open=day["open"], pre_open=day.get("pre_open"),
                              session_pct=round((px / day["open"] - 1) * 100, 3))
            bar = metrics.get("forming_5m") or {}
            if bar.get("ts") == ts // 300_000 * 300_000 and bar.get("open"):
                sample.update(bar_open=bar["open"], bar_ts=bar["ts"], bar_pct=round((px / bar["open"] - 1) * 100, 3),
                              previous_closes=bar["previous_closes"])
        self.store.record_quote(env, inst, ts, px, sample)
        st.opening_quote = sample

    def _fast_on(self, cfg: AgentConfig, inst_id: str) -> bool:
        """这个币用不用快判：用户没关、而且是超短线或日内短线（波段不看 1 分钟 K 线）。"""
        return cfg.fast_entry_for(inst_id) != "off" and cfg.style_for(inst_id) in ("scalp", "intraday")

    def _shift_actions_on(self, cfg: AgentConfig, inst_id: str) -> bool:
        """新增只读采样不能顺带扩大原有的变盘唤醒、撤单范围。"""
        return cfg.fast_entry_for(inst_id) != "off" or self.strategies.wants_data(cfg.env, inst_id)

    def _has_resting_plan(self, cfg: AgentConfig, inst_id: str) -> bool:
        return any(g.get("plan") for g in self.trader.order_guards(cfg.env, inst_id)) or any(
            r["status"] == "armed" and r["params"].get("plan") for r in self.store.strategies(cfg.env, inst_id)
        )

    def _set_regime(self, cfg: AgentConfig, inst_id: str, st: InstState, snapshot: dict[str, Any], pos: Position | None, shift: dict[str, Any] | None) -> list[dict[str, Any]]:
        """算法这一层的结论：行情判断 + 候选位。纯计算，不问大模型；复盘和算法自己的时钟（_algo_tick）共用。
        行情判断变了记一条算法事件。返回候选（持仓时不出候选）。"""
        style = cfg.style_for(inst_id)
        rows = [] if pos else build_candidates(
            snapshot, style=style, min_rr=STANCES[cfg.stance_for(inst_id)]["min_rr"],
            maker_fee_pct=cfg.maker_fee_pct, taker_fee_pct=cfg.taker_fee_pct, entry_mode=cfg.entry_mode_for(inst_id),
            quant_mode=cfg.quant_mode_for(inst_id), quant_bias=cfg.quant_bias_for(inst_id), shift=shift)
        regime = market_regime(snapshot, style, shift)
        was = st.regime or {}
        st.regime = {**regime, "label": REGIME_LABEL[regime["kind"]], "ts": now_ms(), "plays": list(REGIME_PLAYS[regime["kind"]]),
                     "candidates": candidate_rows(rows, style), "holding": bool(pos)}
        if (was.get("kind"), was.get("side")) != (regime["kind"], regime["side"]):
            plays = "、".join(REGIME_PLAYS[regime["kind"]]) or "不出候选"
            head = f"{was['label']} → {REGIME_LABEL[regime['kind']]}" if was else f"启动后第一次判断：{REGIME_LABEL[regime['kind']]}"
            self._algo(cfg.env, inst_id, "regime", f"{head}：{regime['why']}", f"候选改成：{plays}", regime["side"])
        # 新出现的合格候选（同一条打法、同一个方向之前没有）：记下来，之后核对价格有没有照它的方向走
        had = {(c["strategy"], c["side"]) for c in was.get("candidates") or [] if c["ok"]}
        for c in st.regime["candidates"]:
            if c["ok"] and (c["strategy"], c["side"]) not in had:
                goals = " / ".join(f"{t:g}" for t in c["targets"])
                self._algo(cfg.env, inst_id, "cand",
                           f"{c['strategy']}{'做多' if c['side'] == 'long' else '做空'}：入场 {c['entry']:g}，止损 {c['stop']:g}，目标 {goals}，扣费后盈亏比 {c['net_rr']:g}",
                           "回测扣费后亏：只给 Worker 参考，它要写出额外依据才能用" if c["losing"] else "给 Worker 当候选，不下单", c["side"])
        return rows

    async def _algo_tick(self, cfg: AgentConfig, inst_id: str, st: InstState, pos: Position | None = None) -> None:
        """算法自己的时钟：每走完一根结构周期 K 线重算一次行情判断和候选位，不等 Worker 复盘、不花模型调用。
        首次出现的可复核候选合并送入 Worker。"""
        spec = STYLES[cfg.style_for(inst_id)]
        step = spec["bar_minutes"] * 60_000
        if cfg.worker(inst_id).paused or not st.last_px or now_ms() // step == (st.regime or {}).get("ts", 0) // step:
            return
        snapshot = await self.market.snapshot(inst_id, cfg.env, tuple(spec["bars"]))
        if not snapshot.get("last"):
            return
        previous = {(c["strategy"], c["side"]) for c in st.regime.get("candidates", []) if c["ok"] and not c["losing"]}
        self._set_regime(cfg, inst_id, st, snapshot, pos, self.strategies.fresh_metrics(cfg.env, inst_id).get("shift"))
        fresh = [c for c in st.regime["candidates"] if c["ok"] and not c["losing"] and (c["strategy"], c["side"]) not in previous]
        if fresh and not cfg.worker(inst_id).standby:
            levels = "、".join(f"{c['strategy']} {c['side']} @ {c['entry']:g}（止损 {c['stop']:g}）" for c in fresh)
            self.interject(inst_id, f"{spec['struct_bar']} 新合格候选：{levels}。核对现价和已有委托；认可且只差到价就准备挂单，还缺确认则写明等待条件。", "走势记录器")
        self.bus.publish("agent", self.status())

    async def _record(self, cfg: AgentConfig, inst_id: str, st: InstState, now: float, pos: Position | None = None) -> None:
        """记录器的慢数据：程序每 10 秒记一次这个币的 5 分钟 K 线和盘口（量比、涨跌幅、多空分数、1 小时方向、在区间里的位置、点差、盘口偏向、
        各条关键线、5 分钟 ATR、结构转折 shift）。只记数据，不叫 Worker、不下单。快的那一半（1 分钟 K 线、快判信号）在 _fast 里，每几秒一次。"""
        px = st.last_px
        if cfg.worker(inst_id).paused or not px or now - st.feed_checked < 10:
            return
        from .setup import judge

        st.feed_checked = now
        m5, h1, book = await asyncio.gather(self.market.candles(inst_id, "5m", 300, cfg.env), self.market.candles(inst_id, "1H", 300, cfg.env),
                                            self.market.orderbook(inst_id, cfg.env, 20), return_exceptions=True)
        if isinstance(m5, BaseException) or isinstance(h1, BaseException):
            raise m5 if isinstance(m5, BaseException) else h1
        closed = [c for c in m5 if c.confirmed]
        if len(closed) < 25:
            return
        # 开盘以来怎么走的、上一日的价值区间：算出来的价位并进关键线表，快判、记录器、触发器都能用（dist:价值区间下沿 这样写）
        asset = await self.market.asset_class(inst_id, cfg.env)
        session = day_profile({"5m": m5, "1H": h1}, px, *session_anchor(asset))
        day = int(now // 86_400)
        if st.area_bars[0] != day:
            try:
                st.area_bars = (day, await self.market.candles(inst_id, "15m", 260, cfg.env))
            except Exception:  # noqa: BLE001 - 取不到就先不带价值区间，下一轮再试
                pass
        area = value_area({"15m": st.area_bars[1]}, px) if st.area_bars[1] else None
        extra: dict[str, float] = {}
        if area:
            extra.update({"价值区间上沿": area["vah"], "价值区间下沿": area["val"], "成交密集价": area["poc"]})  # type: ignore[dict-item]
        if session:
            extra["今日开盘价"] = session["open"]  # type: ignore[assignment]
            opening = session.get("opening_range")
            if isinstance(opening, dict):
                extra.update({"开盘区间上沿": opening["high"], "开盘区间下沿": opening["low"]})
        levels, trend, atr5 = key_lines(m5, h1, extra)
        last = closed[-1]
        new_bar = last.ts != st.feed_bar_ts or st.judged is None
        if new_bar:  # 多空分数按收盘的 K 线算，一根算一次
            st.judged = judge(closed, levels, trend, need=cfg.setup_min_score, vol_ratio=cfg.setup_vol_ratio, near_pct=cfg.setup_near_pct, pos_side=pos.side if pos else "")
        j = st.judged
        lo, hi = levels.get("区间下沿"), levels.get("区间上沿")
        book = book if isinstance(book, dict) else {}
        prior = self.strategies.metrics.get(inst_id) or {}
        fast_ts = prior.get("fast_ts", 0)
        old = prior.get("values", {}) if self._fast_on(cfg, inst_id) and 0 <= now_ms() - fast_ts <= FAST_DATA_MS else {}
        values = {
            "px": px, "vol_ratio": round(j["ratio"], 2), "chg_pct": round((last.close / last.open - 1) * 100, 3), "score_long": j["scores"]["long"], "score_short": j["scores"]["short"],
            "trend": {"long": 1, "short": -1}.get(j["trend_side"], 0), "range_pos": round((px - lo) / (hi - lo) * 100, 1) if lo and hi and hi > lo else None,
            "spread_pct": book.get("spread_pct"), "book_bias": round(book["imbalance"] * 100) if book.get("imbalance") is not None else None,
            "atr": atr5, **{k: old[k] for k in ("sig_long", "sig_short", "move_1m", "vol_1m") if k in old},  # 快的那几项由 _fast 写，这里原样留着
        }
        shift_now = structure_shift(m5[-120:])
        turn = turn_watch(m5[-160:], shift_now, px)
        values["turn_stage"] = ((1 if turn["side"] == "long" else -1) * turn["stage"]) if turn else 0
        if prior:  # 刚启动时看到的是早就发生的事，不当新事件记
            was = prior.get("turn") or {}
            if shift_now and shift_now["bars_ago"] <= 1 and shift_now["ts"] != (prior.get("shift") or {}).get("ts"):
                swept = turn if turn and turn["stage"] == 3 and turn.get("choch_ts") == shift_now["ts"] else None
                effect = "只记录"
                if self._shift_actions_on(cfg, inst_id):
                    effect = "撤掉过时挂单，叫醒 Worker 重新判断"
                elif self._has_resting_plan(cfg, inst_id):
                    effect = "叫醒 Worker 复核已有计划"
                self._algo(cfg.env, inst_id, "turn", f"确认（{shift_now['kind']}）：{shift_now['why']}" + (f"。之前{swept['why']}" if swept else ""),
                           effect, shift_now["side"], px, atr5)
            elif turn and turn["stage"] == 2 and (turn["side"], turn["stage"], turn["ts"]) != (was.get("side"), was.get("stage"), was.get("ts")):
                self._algo(cfg.env, inst_id, "turn", f"苗头：{turn['text']}", "写进 Worker 的输入，不叫醒、不下单", turn["side"], px, atr5)
        if shift_now and shift_now.get("ts") == (prior.get("shift") or {}).get("ts"):
            values["shift_failure"] = prior.get("values", {}).get("shift_failure")
        qmode, qbias = cfg.quant_mode_for(inst_id), cfg.quant_bias_for(inst_id)
        narrative = describe_tape(m5, levels, shift_now, trend, qmode, qbias, px)
        if turn:
            narrative = f"转折苗头：{turn['text']}。{narrative}"
        if session:  # 开盘以来怎么走的：开盘价、先去了哪边、现在是单边 / 冲高回落 / 探底回升 / 围绕开盘价
            narrative = f"{narrative} {session['text']}。"
        self.strategies.metrics[inst_id] = {
            "source_env": cfg.env, "asset": asset,
            "forming_5m": {"ts": m5[-1].ts, "open": m5[-1].open, "previous_closes": [
                {"ts": c.ts, "close": c.close} for c in closed[-3:] if c.ts < m5[-1].ts]} if not m5[-1].confirmed else None,
            "ts": now_ms(), "fast_ts": fast_ts, "quote_ts": prior.get("quote_ts", 0), "values": values, "levels": levels, "why": {n["label"]: n["value"] for n in j["nodes"]},
            "structure_closes": {
                bar: {"end": candle.ts + 300_000, "price": candle.close}
                for bar, minutes in (("5m", 5), ("15m", 15), ("1H", 60))
                if (candle := next((c for c in reversed(closed) if (c.ts + 300_000) % (minutes * 60_000) == 0), None))
            },
            "shift": shift_now, "turn": turn, "narrative": narrative, "session": session, "value_area": area,
        }  # 5 分钟图上最近一次走势变化与白话诊断
        if new_bar and (not st.data_log or st.data_log[-1]["ts"] != last.ts):
            st.data_log.append({"ts": last.ts, "close": last.close, "reading": narrative, **{k: values[k] for k in ("vol_ratio", "chg_pct", "score_long", "score_short", "trend", "book_bias")}})
        st.feed_bar_ts = last.ts
        key = f"recorders:{cfg.env}:{inst_id}"
        if self.store.get(key) is True:  # 旧版自动带的「凑够分数就叫醒」的默认记录器：已经换成快判，撤掉（用户和 Worker 自己设的不动）
            self.store.set(key, "fast")
            for row in self.store.strategies(cfg.env, inst_id):
                if row["params"].get("recorder") and row["params"].get("by") == "默认":
                    self.strategies.cancel(row["id"], "默认记录器已经换成快判")

    async def _shift_watch(self, cfg: AgentConfig, inst_id: str, st: InstState) -> str | None:
        """转折确认后复核已有计划。同一次变化只处理一次。"""
        m = self.strategies.metrics.get(inst_id) or {}
        shift = m.get("shift")
        if not shift or shift["ts"] == st.shift_ts:
            return None
        first, st.shift_ts = st.shift_ts == 0, shift["ts"]
        if first or shift["bars_ago"] > 3:  # 刚启动时看到的是早就发生的事；隔了好几根才看到的也不当新事件
            return None
        if not self._shift_actions_on(cfg, inst_id):
            if not self._has_resting_plan(cfg, inst_id):
                return None
            text = f"走势变了：{shift['why']}。复核已有入场计划，判断是保留、调整还是撤销。"
            self._emit(cfg.env, inst_id, "strategy", "wake", text, None, {"shift": shift})
            return text
        _, mine = await self._plan_state(cfg, inst_id, [])
        dropped = []
        for r in mine:
            why = line_status(st.last_px, r["params"]["trigger"], r["side"], shift, m["values"].get("atr"), STYLES[cfg.style_for(inst_id)]["plan_minutes"])
            if why:
                await self._drop_plan(cfg, inst_id, r, why)
                dropped.append(f"{self._tag(r)}（{why}）")
        text = f"走势变了：{shift['why']}。" + (f"程序已经撤掉这些等不到的入场：{'；'.join(dropped)}。" if dropped else "")
        self._emit(cfg.env, inst_id, "strategy", "wake", text, None, {"shift": shift})
        return text + f"按新的走势重新判断：顺着新方向的回踩位在 {shift['level']:g} 附近（策略「变盘后回踩」）；留在起动点 {shift['origin']:g} 后面的旧线不要再挂。"

    async def _fast(self, cfg: AgentConfig, inst_id: str, st: InstState, now: float) -> str | None:
        """快判：每隔几秒用 1 分钟 K 线判断一次盘是不是朝一个方向动了（收盘突破、放量急动、假突破收回），入场、止损、目标由程序算好。
        这个方向事先授权过（用户设成直接下单，或者 Worker 在结论里授权）就立刻照着下单，做完再告诉 Worker；
        没授权就返回一句叫醒 Worker 的话，让它马上核实。和 Worker 自己写的计划走同一套核对。返回 None 表示不用叫醒。"""
        m = self.strategies.metrics.get(inst_id)
        if not self._fast_on(cfg, inst_id) or not m or now_ms() - m["ts"] > DATA_STALE_MS or not st.last_px or cfg.worker(inst_id).paused or now - st.fast_checked < FAST_SECONDS:
            return None
        st.fast_checked = now
        bars = await self.market.candles(inst_id, "1m", 60, cfg.env)
        values = m["values"]
        qmode, qbias = cfg.quant_mode_for(inst_id), cfg.quant_bias_for(inst_id)
        kinds = tuple(cfg.fast_kinds) if qmode == "auto" else QUANT_MODES[qmode]["kinds"]
        v_ratio = cfg.worker(inst_id).quant_vol_ratio or FastParams.vol_ratio
        sig = detect(bars, st.last_px, m["levels"], atr5=values.get("atr"), trend=values.get("trend") or 0, shift=m.get("shift"),
                     p=FastParams(kinds=kinds, vol_ratio=v_ratio)) if kinds else None
        closed = [c for c in bars if c.confirmed]
        m["fast_ts"] = closed[-1].ts + 60_000 if closed else 0
        if len(closed) > 21:
            avg = sum(c.volume for c in closed[-21:-1]) / 20
            values.update(move_1m=round((closed[-1].close / closed[-1].open - 1) * 100, 3), vol_1m=round(closed[-1].volume / avg, 2) if avg else None)
        values.update(strengths(sig))
        if sig is None or sig.bar_ts == st.fast_bar_ts or (qbias in ("long", "short") and sig.side != qbias):
            return None
        st.fast_bar_ts = sig.bar_ts
        late = not 0 <= now_ms() - (sig.bar_ts + 60_000) <= FAST_DATA_MS
        mode = cfg.fast_entry_for(inst_id)
        note = {"ts": now_ms(), "side": sig.side, "kind": sig.kind, "strength": sig.strength, "text": sig.text(), "status": "ignored", "result": ""}
        st.fast_last = note

        def done(status: str, result: str, emit: bool = True) -> None:
            note.update(status=status, result=result)
            if status != "ignored":
                self._algo(cfg.env, inst_id, "fast", sig.text(), {"executed": "直接下单", "pending": "等你确认", "skipped": "没处理", "verify": "复核"}.get(status, status) + "：" + result[:160], sig.side, sig.entry)
            if emit:
                self._emit(cfg.env, inst_id, "strategy", "action" if status == "executed" else "wake", f"快判 {sig.text()}。{result}", None, {"fast": True, "status": status})
            self.bus.publish("agent", self.status())

        if late or now - st.fast_at.get(sig.side, 0) < FAST_COOLDOWN:
            done("ignored", "看到的时候已经晚了，不追" if late else "这个方向刚出过信号，隔一会儿再看", emit=False)
            return None
        skip, mine = await self._plan_state(cfg, inst_id, [])
        if skip:
            done("ignored", f"没有处理：{skip}", emit=False)
            return None
        pref = cfg.leverage_pref()
        plan = sig.plan(pref[1] if pref else min(cfg.risk.max_leverage, 20)).model_copy(update={"strategy": f"快判·{FAST_KIND[sig.kind]}"})
        style = cfg.style_for(inst_id)
        if plan.strategy in losing(style):  # 和算法候选位同一条规矩：机械照做回测扣费后亏的，程序不照着下单，也不为它叫醒 Worker
            st.fast_at[sig.side] = now
            done("skipped", f"这类信号机械照做的回测是亏的（{evidence(style).get(plan.strategy, '')}）：只记录，不下单、不叫醒 Worker", emit=False)
            return None
        check = await self._check_plan(cfg, inst_id, plan)
        if not check.ok:  # 程序自己算的价位都过不了核对：不值得为它叫醒 Worker
            done("skipped", "没有处理：" + "；".join(check.issues)[:200], emit=False)
            return None
        st.fast_at[sig.side] = now
        allowed = mode == "agent" and (st.report or {}).get("on_signal") in (sig.side, "both")
        if allowed:
            if mine:
                done("skipped", "已有计划入场委托，快判不叠加市价单；换单由计划复核处理")
                return None
            who = "Worker 授权过这个方向"
            result = await self._enter_now(cfg, inst_id, plan, check, f"快判{FAST_KIND[sig.kind]}（{check.numbers()}）", "快判")
            done(result.status, f"{who}。{result.message}")
            if result.status == "pending" and result.pending_id:
                self._emit(cfg.env, inst_id, "strategy", "ask", "快判入场等你确认", None,
                           {"pending_id": result.pending_id, "title": result.message, "tool": "open_position"})
            if result.status == "executed":
                mail = {"who": "快判", "text": f"快判入场单已提交，成交以交易所为准：{sig.text()}。{result.message}", "ts": now_ms()}
                self.store.set(f"inbox:{cfg.env}:{inst_id}", [*self.inbox(cfg.env, inst_id), mail][-10:])
            # 等确认、建议、拒绝、失败都是本信号的处理结果，不能再叫 Worker 换条路径重试。
            return None
        why = "" if allowed else ("（你没有授权这个方向）" if mode == "agent" else "")
        ask = (
            f"程序快判出了信号{why}：{sig.text()}（{check.numbers()}，已通过程序核对）。马上核实：对照大方向和这根 K 线的位置，"
            "认可就照这些价位给 plan（entry_type=now，可以微调止损和目标），程序立刻下单；不认可就不给 plan，在 summary 里写明信号哪里不对。"
        )
        done("verify", "已交给 Worker 核实" + why)
        if st.reviewing or st.run is not None:  # Worker 正在想：直接送进它这一轮
            self._steer(cfg, inst_id, ask, "快判")
            return None
        return ask

    def _sync_observation_rules(self, cfg: AgentConfig, inst_id: str) -> None:
        st = self._state(inst_id)
        if not st.report or inst_id not in cfg.instruments:
            return
        stamp = st.report.get("ts", 0)
        key = f"watch_sync:{cfg.env}:{inst_id}"
        if self.store.get(key) == stamp:
            return
        wanted = {}
        for level in st.report.get("watch_levels") or []:
            minutes = level.get("valid_minutes") or STYLES[cfg.style_for(inst_id)]["plan_minutes"]
            wanted[(level["price"], level.get("note", ""))] = minutes
        for row in self.store.strategies(cfg.env, inst_id):
            p = row["params"]
            if not p.get("observation"):
                continue
            line = (p["level"], p.get("wake_note", ""))
            if wanted.get(line) == p["expires_minutes"] and p["expires_ts"] > now_ms():
                del wanted[line]
            else:
                self.strategies.cancel(row["id"], "复盘更新了观察线")
        for (price, note), minutes in wanted.items():
            row = self.strategies.deploy(cfg, inst_id, "trigger", "", {
                "when": "price_touch", "level": price, "do": "wake", "wake_note": note,
                "repeat": True, "cooldown_minutes": cfg.min_review_seconds / 60,
                "observation": True, "expires_minutes": minutes,
            }, note=note, by="Worker")
            if px := st.last_px or st.report.get("price"):
                self.store.update_strategy(row["id"], state={"previous_px": px})
        self.store.set(key, stamp)

    def _trigger(self, cfg: AgentConfig, st: InstState, pos: Position | None, now: float) -> str | None:
        has_pos = pos is not None
        changed = st.had_position is not None and st.had_position != has_pos
        st.had_position = has_pos
        if not st.last_review_ts:
            return "开始盯盘，首次复盘"
        if st.has_mail:
            return "信箱里有新的留言要处理"
        if changed:
            return "持仓已被平掉（止盈/止损触发或手动平仓），重新评估" if not has_pos else "出现了新的持仓（挂单成交或手动开仓），接管并制定持仓计划"
        cooling = now - st.last_review_ts < cfg.min_review_seconds
        if cooling:
            return None
        if st.stale_order and now - st.last_review_ts >= max(cfg.min_review_seconds, 120):
            return st.stale_order
        if st.last_review_px:
            move = (st.last_px / st.last_review_px - 1) * 100
            if abs(move) >= cfg.wake_move_pct:
                return f"距上次复盘价格{'上涨' if move > 0 else '下跌'} {abs(move):.2f}%（{st.last_review_px:g} → {st.last_px:g}）"
        if now >= st.next_due_ts:
            return "定时复盘"
        return None

    def _adopt(self, cfg: AgentConfig, account: AccountState) -> AgentConfig:
        """用户在盯盘列表之外的币上开了仓：把这个币加进盯盘列表，由它的 Worker 接手，不让这笔仓没人管。"""
        strays = list(dict.fromkeys(p.inst_id for p in account.positions if p.inst_id.endswith("-SWAP") and p.inst_id not in cfg.instruments))
        if not strays or self.control is None:
            return cfg
        cfg = cfg.model_copy(update={"instruments": [*cfg.instruments, *strays]})
        self.control.save_config(cfg)
        for inst_id in strays:
            self._note("system", f"你在 {inst_id} 上有持仓，已加入盯盘，由它的 Worker 接手", inst_id=inst_id)
            self._emit(cfg.env, inst_id, "router", "handoff", f"发现你在 {inst_id.split('-')[0]} 上有持仓，交给它的 Worker 盯着", None, {"to": inst_id})
        self.bus.publish("config", cfg.model_dump())
        self.bus.publish("agent", self.status())
        return cfg

    async def _guard_orders(self, cfg: AgentConfig, account: AccountState, prices: dict[str, float]) -> None:
        """Agent 挂的限价单：价格没成交就先到了撤单线、或者过了有效期，程序直接撤掉这一张，再告诉 Worker。不等模型。"""
        for inst in cfg.instruments:
            guards = self.trader.order_guards(cfg.env, inst)
            if not guards:
                continue
            open_ids = {o.ord_id for o in account.orders if o.inst_id == inst}
            px, keep = prices.get(inst), []
            for g in guards:
                if g["ord_id"] not in open_ids:  # 成交了或已经撤了；刚挂出去的几秒里账户快照可能还没有它
                    if now_ms() - g["ts"] < 10_000:
                        keep.append(g)
                    continue
                why = ""
                if g.get("expires_ts") and now_ms() >= g["expires_ts"]:
                    why = f"挂了 {round((now_ms() - g['ts']) / 60000)} 分钟没成交，过了有效期"
                elif g.get("cancel_px") and px and (px >= g["cancel_px"] if g["side"] == "long" else px <= g["cancel_px"]):
                    why = f"没成交前价格先到了撤单线 {g['cancel_px']:g}（现价 {px:g}）"
                elif px and g.get("cancel_before_entry"):
                    shape = {"inst_id": inst, "ts": g["ts"], "params": {"cancel_before_entry": g["cancel_before_entry"]}, "state": {}}
                    eval_state = dict(g.get("cancel_state") or {})
                    invalid = await self.strategies.entry_invalidation(cfg, account, shape, eval_state, px)
                    g = {**g, "cancel_state": eval_state}
                    if invalid:
                        why = f"入场前失效：{invalid}"
                if not why or cfg.permission_for("cancel_orders", inst) == "off" or time.time() < g.get("retry_at", 0):
                    keep.append(g)
                    continue
                what = f"{'买' if g['side'] == 'long' else '卖'}单 @ {g['price']:g}"
                result = await self.trader.execute(TradeAction(kind="cancel_orders", inst_id=inst, ord_id=g["ord_id"], reason=f"程序按预设条件撤单：{why}"), cfg.env, source="guard")
                if result.status != "executed" or not result.detail.get("cancelled"):
                    keep.append({**g, "retry_at": time.time() + 30})
                    continue
                text = f"限价{what}已由程序撤掉：{why}"
                self._emit(cfg.env, inst, "strategy", "action", text, None, result.model_dump(mode="json"))
                self.store.set(
                    f"inbox:{cfg.env}:{inst}",
                    [*self.inbox(cfg.env, inst), {"who": "盯盘程序", "text": f"你挂的{text}。按现在的行情重新判断，不要在原价重挂。", "ts": now_ms()}][-10:],
                )
            if keep != guards:
                updates = {g["ord_id"]: g for g in keep}
                initial = {g["ord_id"]: g for g in guards}
                merged = []
                for current in self.trader.order_guards(cfg.env, inst):
                    oid = current["ord_id"]
                    if oid not in initial or current != initial[oid]:
                        merged.append(current)
                    elif oid in updates:
                        merged.append(updates[oid])
                self.store.set(f"order_guard:{cfg.env}:{inst}", merged)

    async def _emergency(self, cfg: AgentConfig, account: AccountState) -> None:
        """硬止损：单个持仓浮亏超过权益的 emergency_exit_loss_pct，不等 Agent，立即市价平仓。"""
        if account.equity <= 0:
            return
        limit = account.equity * cfg.risk.emergency_exit_loss_pct / 100
        for pos in account.positions:
            if pos.inst_id in cfg.instruments and pos.upl <= -limit and cfg.permission_for("close", pos.inst_id) == "auto":
                probe = TradeAction(kind="close", inst_id=pos.inst_id, reason="紧急离场")
                if cfg.hands_off_manual and self.trader.manual_lots(cfg.env, probe):
                    # 用户自己开的仓不替他平，只提醒；同一个币上 Agent 自己开的那几笔照样平掉
                    key = f"{cfg.env}:{pos.inst_id}"
                    if time.time() - self._manual_warned.get(key, 0) >= 300:
                        self._manual_warned[key] = time.time()
                        self._note("alert", f"{pos.inst_id} 浮亏 {pos.upl:.2f} USDT 已超过紧急线。这是你手动开的仓，Agent 没有替你平，请尽快处理", {"upl": pos.upl, "limit": -limit}, inst_id=pos.inst_id)
                    for lot in self.trader.store.lots(cfg.env, pos.inst_id):
                        if lot["status"] == "open" and lot["source"] not in MANUAL_SOURCES:
                            await self.trader.execute(
                                TradeAction(kind="close", inst_id=pos.inst_id, lot_id=lot["id"], reason=f"紧急离场：浮亏超过权益的 {cfg.risk.emergency_exit_loss_pct:g}%"),
                                cfg.env,
                                source="guard",
                            )
                    continue
                self._note("alert", f"{pos.inst_id} 浮亏 {pos.upl:.2f} USDT 超过紧急线，立即平仓", {"upl": pos.upl, "limit": -limit}, inst_id=pos.inst_id)
                await self.trader.execute(
                    TradeAction(kind="close", inst_id=pos.inst_id, reason=f"紧急离场：浮亏超过权益的 {cfg.risk.emergency_exit_loss_pct:g}%"),
                    cfg.env,
                    source="guard",
                )

    # ---------- Agent 复盘 ----------

    async def _health(self, cfg: AgentConfig) -> list[dict[str, Any]]:
        """Watchdog 的巡检：把整个团队和账户过一遍，每发现一个问题记一条 {key, level, inst_id, text}。"""
        try:
            account = await self.trader.account(cfg.env)
        except Exception as exc:  # noqa: BLE001
            return [{"key": "okx", "level": "bad", "inst_id": "", "text": f"读不到欧易账户：{str(exc)[:120]}"}]
        out: list[dict[str, Any]] = []
        for pos in account.positions:
            if not any(p.inst_id == pos.inst_id and p.stop_loss for p in account.protections):
                out.append({"key": f"naked:{pos.inst_id}", "level": "bad", "inst_id": pos.inst_id, "text": f"{pos.inst_id} 有持仓但交易所里没有止损单"})
        for o in account.orders:
            mins = (now_ms() - o.created_at) / 60000 if o.created_at else 0
            if o.reduce_only and mins >= 1:
                out.append({"key": f"stuckclose:{o.inst_id}", "level": "warn", "inst_id": o.inst_id, "text": f"{o.inst_id} 的平仓单挂了 {int(mins)} 分钟还没成交，仓位还在"})
        for inst_id, st in self.states.items():
            if st.fails >= 3:
                out.append({"key": f"fails:{inst_id}", "level": "bad", "inst_id": inst_id, "text": f"{inst_id} 的 Worker 连续 {st.fails} 次复盘失败：{st.last_error[:100]}"})
        for row in self.store.strategies(cfg.env, None, False):
            if row["status"] == "failed" and now_ms() - row["updated_ts"] < 15 * 60_000:
                out.append({"key": f"strategy:{row['id']}", "level": "warn", "inst_id": row["inst_id"], "text": f"{row['inst_id']} 的策略 #{row['id']} 连续出错已停止：{row['note'][:100]}"})
        if account.halted:
            out.append({"key": "halted", "level": "bad", "inst_id": "", "text": "当日亏损已到上限，风控已禁止开新仓"})
        return out

    async def _patrol(self, cfg: AgentConfig) -> None:
        """每分钟巡检一次：新发现的问题记进日志和活动流，能交给 Worker 处理的直接叫醒它；问题消失了也说一声。"""
        self._resume_due(cfg)
        try:  # 顺手把用策略库做完的单记进战绩
            await self.strategies.score_playbooks(cfg.env)
        except Exception as exc:  # noqa: BLE001 - 记战绩失败不能影响巡检
            log.warning("策略战绩没记上：%s", exc)
        if time.time() - self._pruned_at > 6 * 3600:  # 每六小时：删掉过了保留期的运行记录
            self._pruned_at = time.time()
            try:
                gone = self.store.prune(cfg.log_keep_days)
                if sum(gone.values()):
                    log.info("已清理 %d 天前的运行记录：%s", cfg.log_keep_days, gone)
            except Exception as exc:  # noqa: BLE001 - 清理失败不能影响巡检
                log.warning("清理运行记录失败：%s", exc)
        if time.time() - self._scored_at > 180:  # 每三分钟：新平掉的仓按打法记进战绩
            self._scored_at = time.time()
            try:
                for d in await self.scores.settle(cfg.env, self.trader, list(cfg.instruments)):
                    self._emit(cfg.env, d["inst_id"], "strategy", "done", f"「{d['strategy']}」做完一笔：{d['pnl']:+.2f} USDT。这条打法的战绩：{self.scores.of(cfg.env, d['strategy'])}")
            except Exception as exc:  # noqa: BLE001 - 记战绩失败不能影响巡检
                log.warning("打法战绩没记上：%s", exc)
        try:  # 新平掉的仓逐笔复盘，写进长期经验
            await self._review_trades(cfg)
        except Exception as exc:  # noqa: BLE001 - 复盘失败不能影响巡检
            log.warning("平仓复盘没做成：%s", exc)
        found = await self._health(cfg)
        before = {c["key"]: c for c in self.watchdog.get("checks", [])}
        for c in found:
            if c["key"] in before:
                continue
            self._note("alert", f"Watchdog：{c['text']}", inst_id=c["inst_id"])
            self._emit(cfg.env, c["inst_id"], "watchdog", "error" if c["level"] == "bad" else "wake", c["text"], None, {"key": c["key"]})
            if c["key"].startswith("naked:") and c["inst_id"] in self.states:
                self._state(c["inst_id"]).lot_alert = (
                    f"Watchdog 发现 {c['inst_id']} 有持仓但交易所里没有止损单。先按结构位给它补上止损（move_stops），再判断这笔仓要不要继续拿，在结论里说明。"
                )
        for key, c in before.items():
            if key not in {x["key"] for x in found}:
                self._emit(cfg.env, c["inst_id"], "watchdog", "done", f"已恢复：{c['text']}", None, {"key": key})
        self.watchdog = {**self.watchdog, "checks": found, "checked_ts": now_ms()}
        self.bus.publish("agent", self.status())

    async def _watch_loop(self) -> None:
        """Watchdog：独立于盯盘循环。每 15 秒看一次循环有没有停跳、有没有 Worker 卡死，发现就修复；每分钟把账户和团队巡检一遍。"""
        beats = 0
        while self.running:
            await asyncio.sleep(15)
            now = time.time()
            cfg = self.get_config()
            beats += 1
            if beats % 4 == 1:
                try:
                    await self._patrol(cfg)
                except Exception as exc:  # noqa: BLE001 - 巡检出错不能让看门狗自己停掉
                    log.warning("Watchdog 巡检出错：%s", exc)
            if self.running and now - self._beat > max(60, cfg.tick_seconds * 8):
                if self._task and not self._task.done():
                    self._task.cancel()
                self._beat = now
                self._task = asyncio.create_task(self._loop(), name="ouy-watcher")
                self.watchdog = {**self.watchdog, "restarts": self.watchdog["restarts"] + 1, "last": f"{time.strftime('%H:%M:%S')} 盯盘循环停跳，已重启"}
                self._emit(cfg.env, "", "watchdog", "action", "盯盘循环停跳，已自动重启", None)
                self._note("alert", "Watchdog：盯盘循环停跳，已自动重启")
            for inst_id, st in self.states.items():
                if st.reviewing and st.review_started and now - st.review_started > REVIEW_TIMEOUT + 60:
                    if st.task and not st.task.done():
                        st.task.cancel()
                    st.reviewing, st.review_started = False, 0.0
                    st.last_review_ts, st.next_due_ts = now, now + 120
                    self.watchdog = {**self.watchdog, "restarts": self.watchdog["restarts"] + 1, "last": f"{time.strftime('%H:%M:%S')} {inst_id} 的 Worker 卡住，已重置"}
                    self._emit(cfg.env, inst_id, "watchdog", "action", "Worker 超时没有返回，已重置，2 分钟后重试", None)
                    self._note("alert", f"Watchdog：{inst_id} 的 Worker 超时没有返回，已重置，2 分钟后重试", inst_id=inst_id)

    async def _should_wake(self, cfg: AgentConfig, inst_id: str, reason: str) -> tuple[bool, str]:
        """事件的第二级过滤（Slow Path）：让 Router 判断这个软事件值不值得唤醒 Worker。判断失败就唤醒。"""
        st = self._state(inst_id)
        last = st.report or {}
        ago = int((now_ms() - last.get("ts", now_ms())) / 60000)
        prompt = (
            f"品种 {inst_id}，事件：{reason}。现价 {st.last_px:g}，上次复盘在 {ago} 分钟前，当时价格 {last.get('price')}。\n"
            f"上次结论：{last.get('summary')}\n上次计划：{json.dumps(last.get('plan'), ensure_ascii=False)}\n"
            f"盯盘价位：{json.dumps(last.get('watch_levels'), ensure_ascii=False)}\n当前没有持仓。"
        )
        try:
            result = await asyncio.wait_for(self._gate(cfg).run(prompt, model_settings=_effort("low", "ouy-gate"), usage_limits=UsageLimits(request_limit=3)), 60)
            return bool(result.output.act), str(result.output.why)
        except Exception as exc:  # noqa: BLE001 - 过滤器出问题不能挡住 Worker
            return True, f"过滤器不可用（{str(exc)[:80]}），直接唤醒"

    async def _review_task(self, inst_id: str, reason: str, gate: bool = False) -> None:
        st = self._state(inst_id)
        try:
            if gate:
                cfg = self.get_config()
                why = quiet_reason(st, cfg.wake_move_pct) if reason == "定时复盘" else ""
                act = not why
                if act:
                    act, why = await self._should_wake(cfg, inst_id, reason)
                st.last_gate = why
                if not act:
                    st.skips += 1
                    st.last_review_ts = time.time()
                    st.last_review_px = st.last_px or st.last_review_px
                    st.next_due_ts = st.last_review_ts + min(cfg.review_seconds, STYLES[cfg.style_for(inst_id)]["max_review"])
                    work_id = self.store.add_work(cfg.env, inst_id, "event", reason, "skipped", model="规则" if why.startswith("规则过滤") else cfg.router_model)
                    self.store.update_work(work_id, "skipped", why)
                    self._emit(cfg.env, inst_id, "router", "skip", f"{reason} → 不唤醒 Worker：{why}", work_id)
                    self.bus.publish("work", {"inst_id": inst_id})
                    return
            await self.review(inst_id, reason, source="event")
            st.last_error = ""
            st.fails = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            st.last_error = str(exc)[:300]
            st.fails += 1
            wait = min(120 * 2 ** (st.fails - 1), 900)  # 连续失败就拉长间隔，免得一直空烧 token
            st.last_review_ts = time.time()
            st.next_due_ts = time.time() + wait
            self._note(
                "alert",
                f"{inst_id} 本次 Agent 复盘失败" + (f"（连续第 {st.fails} 次）" if st.fails > 1 else "") + f"，{wait // 60} 分钟后重试",
                {"message": st.last_error, "reason": reason}, inst_id=inst_id,
            )
            log.warning("复盘失败 %s: %s", inst_id, exc)
        finally:
            st.reviewing = False
            self.bus.publish("agent", self.status())

    async def review(self, inst_id: str, reason: str, source: str = "manual") -> dict[str, Any]:
        """唤醒这个品种的 Worker 做一次完整复盘。同一个 Worker 一次只做一件事：它正在处理别的任务时，这次复盘排队。"""
        st = self._state(inst_id)
        if any(r["kind"] == "review" for r in self.awaiting(self.get_config().env, inst_id)):
            self.on_decided()
            raise RuntimeError(f"{inst_id} 的 Worker 有操作在等你同意，先点同意或不执行，它会接着做。")
        st.reviewing = True
        try:
            async with self._turn(self.get_config(), inst_id, "复盘"):
                st.review_started = time.time()
                return await self._review_once(inst_id, reason, source)
        finally:
            st.reviewing, st.review_started = False, 0.0

    async def _review_once(self, inst_id: str, reason: str, source: str = "manual") -> dict[str, Any]:
        """一次完整复盘。每次复盘是一条任务（work item），状态落在数据库里。"""
        cfg = self.get_config()
        st = self._state(inst_id)
        st.reviewing, st.last_reason = True, reason
        st.review_started = st.review_started or time.time()
        self.bus.publish("agent", self.status())
        work_id = self.store.add_work(cfg.env, inst_id, source, reason, "running", model=cfg.model_for(inst_id))
        self.bus.publish("work", {"inst_id": inst_id})
        self._emit(cfg.env, inst_id, "worker", "wake", reason, work_id, {"source": source})
        try:
            agent = self._worker_agent(cfg, inst_id)
            style = STYLES[cfg.style_for(inst_id)]
            account = await self.trader.account(cfg.env)
            # 这个币上有几种风格的仓位，就把这些风格要看的周期都带上
            held = {l["style"] for l in self.trader.lots(cfg.env, inst_id) if l["status"] == "open"}
            bars = tuple(dict.fromkeys(b for name in [cfg.style_for(inst_id), *sorted(held)] for b in STYLES[name]["bars"]))
            snapshot = await self.market.snapshot(inst_id, cfg.env, bars)
            asset = self._asset[inst_id] = await self.market.asset_class(inst_id, cfg.env)
            if asset != "crypto":  # 股票 / 商品永续不跟 BTC：不给它看 BTC 大盘，免得拿 BTC 的涨跌给它定方向
                snapshot["asset_class"] = asset
            elif inst_id != BTC:
                try:
                    snapshot["btc_market"] = await self.market.brief(BTC, cfg.env)
                except Exception:  # 大盘参考拿不到就不带，不影响这个品种的复盘
                    pass
            if cfg.env == "demo":
                # 模拟盘有自己独立的盘口和成交，和真实市场对不上：方向和量能参考真实市场，下单价位仍按模拟盘价格
                try:
                    snapshot["real_market"] = await self.market.brief(inst_id, "live")
                except Exception:  # noqa: BLE001 - 拿不到就不带
                    pass
            st.illiquid = demo_liquidity(snapshot) if cfg.env == "demo" else ""
            self.trader.illiquid[f"{cfg.env}:{inst_id}"] = st.illiquid
            pos = account.position(inst_id)
            if snapshot.get("last"):
                st.last_px = st.last_px or float(snapshot["last"])
                try:  # 手动复盘或刚启动时确保走势转折与记录器指标已就绪
                    await self._record(cfg, inst_id, st, time.time(), pos)
                except Exception:  # noqa: BLE001
                    pass
            knowledge = await self._knowledge(st, snapshot, pos, reason, self.strategies.fresh_metrics(cfg.env, inst_id))
            try:
                closed = await self.trader.history(inst_id, 5, cfg.env)
            except Exception:  # 历史记录读不到不影响复盘
                closed = []
            metrics = self.strategies.fresh_metrics(cfg.env, inst_id)
            shift = metrics.get("shift")
            rows = self._set_regime(cfg, inst_id, st, snapshot, pos, shift)
            if any(row["ok"] for row in rows):
                recent_lots = self.store.lots(cfg.env, inst_id, live=False, limit=10)
                wait = RETRY_MINUTES[cfg.style_for(inst_id)]
                at = now_ms()
                rows = [
                    {**row, "execution_blocker": self._reentry_issue(row["plan"], row["check"].ref, wait, recent_lots, at)}
                    if row["ok"] else row
                    for row in rows
                ]
            prompt = self._prompt(cfg, inst_id, reason, snapshot, account, st.report, knowledge, closed, candidates=rows)
            best = next((c for c in rows if c["ok"] and not c.get("losing") and not c.get("execution_blocker")
                         and c["check"].limit and not c["align"].startswith("逆")), None) if (snapshot.get("orderbook") or {}).get("spread_pct") is not None else None
            deps = Deps(
                trader=self.trader, market=self.market, gateway=self.gw, config=cfg, kb=self.kb, skills=self.skills, lessons=self.lessons,
                remember=lambda inst, note: self._remember(cfg.env, inst, note),
                sub=self._sub_from_worker, spawn=self._spawn, close_sub=self._close_sub,
                emit=self._emitter(cfg.env, inst_id, "worker", work_id),
                strategy=self.strategies, standby=self._stop_watching, plan_check=self._plan_checker(cfg, inst_id, best), set_quant=self._set_quant,
            )
            # 被行情事件叫醒（触及价位、进入入场区间、异动、仓位变化）：要快，用低一档的思考强度；定时复盘和用户交代的事用正常强度
            fast = source == "event" and reason not in ("定时复盘", "开始盯盘，首次复盘") and "留言" not in reason
            effort = cfg.trigger_effort_for(inst_id) if fast else cfg.effort_for(inst_id)
            if fast:
                prompt += (
                    "\n【这次是被行情事件叫醒的，要快】先看触发这件事的那个周期的 candles 和 patterns：这次是真突破、假突破，还是正常回踩？"
                    "然后直接决定——动手、调整策略或止损、还是不动。不做全面复盘，不派子代理（除非要下大单），结论两三句话。"
                )
            notes = self.inbox(cfg.env, inst_id)
            if notes:  # 空闲时收到的留言：这一轮先处理
                prompt += "\n【未读留言，时间为写入信箱的本地时间】行情和执行回执是历史证据，不是再执行一次的指令；与当前挂单、持仓及最新收盘核对。\n" + "\n".join(
                    f"{time.strftime('%H:%M:%S', time.localtime(n['ts'] / 1000)) if n.get('ts') else '时间未知'} {n['who']}：{n['text']}" for n in notes)
                self.store.delete(f"inbox:{cfg.env}:{inst_id}")
            result = await self._run(agent, prompt, st, REVIEW_TIMEOUT, live=LiveText(self.bus.publish, "worker", inst_id), deps=deps, usage_limits=UsageLimits(request_limit=20), model_settings=_effort(effort, _worker_cache_key("review", inst_id)))
            extra = {"reason": reason, "price": snapshot["last"], "knowledge": [f"{c.title} › {c.section}" for c in knowledge], "effort": effort}
            if isinstance(result.output, DeferredToolRequests):  # 它调用的工具里有要用户同意的：这一轮挂起，等用户决定后从这里继续
                return self._hold_run(cfg, inst_id, "review", work_id, result, deps, extra)
            extra["plan_order"] = await self._arm_plan(cfg, inst_id, result.output, deps, work_id)
            return self._finish_review(cfg, inst_id, work_id, result, deps, extra)
        except BaseException as exc:
            kind, why = _fail_text(exc)
            self.store.update_work(work_id, "failed", why)
            self._emit(cfg.env, inst_id, "worker", kind, why, work_id)
            raise
        finally:
            st.reviewing, st.review_started = False, 0.0
            self.bus.publish("work", {"inst_id": inst_id})

    # ---------- 计划就是指令：Worker 给出计划，程序核对后直接照着下单 ----------

    ENTRY_KINDS = ("breakout", "pullback", "range", "martingale", "grid")
    NO_PLAN = {"cost": "扣完成本净盈亏比不够", "objection": "有明确的反对信号", "limit": "到了亏损上限或用户不让开仓",
               "no_edge": "候选的打法回测没有优势，也没有额外依据"}

    def _plan_checker(self, cfg: AgentConfig, inst_id: str, best: dict | None = None) -> Any:
        """给复盘那一轮用的核对函数；用户关掉「计划自动执行」时不核对（那时计划只是展示）。"""
        if not cfg.auto_arm_plan:
            return None
        return lambda report, deps: self._plan_gate(cfg, inst_id, report, deps, best)

    async def _plan_state(self, cfg: AgentConfig, inst_id: str, actions: list[Any]) -> tuple[str, list[dict[str, Any]]]:
        """这个币现在要不要程序照计划下单。返回（不用的原因，空串表示要；程序之前照计划布置、还在等触发的策略）。"""
        rows = self.store.strategies(cfg.env, inst_id)
        mine = [r for r in rows if r["params"].get("plan") and r["kind"] != "manage" and r["status"] in ("proposed", "armed")]
        account = await self.trader.account(cfg.env)
        open_ids = {o.ord_id for o in account.orders if o.inst_id == inst_id}
        verify_key = f"plan_cancel_check:{cfg.env}:{inst_id}"
        oid = self.store.get(verify_key)
        if oid and oid not in open_ids:
            terminal = await self._cancel_state(cfg, inst_id, oid)
            if terminal == "unknown":
                return f"旧单 {oid} 撤单结果未核实，暂停换单", []
            if terminal == "filled":
                if account.position(inst_id) or any(l.get("ord_id") == oid and l["status"] == "closed"
                                                    for l in self.store.lots(cfg.env, inst_id, live=False, limit=50)):
                    self.store.delete(verify_key)
                return f"旧单 {oid} 有成交，先复核持仓，不补挂", []
            self._finish_plan_cancel(cfg, inst_id, oid)
        resting = [g for g in self.trader.order_guards(cfg.env, inst_id) if g.get("plan") and (g["ord_id"] in open_ids or now_ms() - g["ts"] < 10_000)]
        mine += [{"id": f"限价单 @ {g['price']:g}", "kind": "limit", "side": g["side"], "status": "armed", "ord_id": g["ord_id"],
                  "params": {"trigger": g["price"], "stop_loss": g.get("stop_loss") or g["price"], "terms": g.get("terms"), "plan": True}} for g in resting]
        goal = self.goal(cfg.env, inst_id)
        if account.position(inst_id):
            return "已有持仓", mine
        if self.get_config().worker(inst_id).standby:  # 交代的停止条件达到、已经停下：不再要它给计划
            return "Worker 已停下", mine
        if cfg.permission_for("open", inst_id) == "off":
            return "用户不允许 Agent 开仓", mine
        if account.halted:
            return "已到当日亏损上限", mine
        if goal and goal["status"] in ("stopped", "done"):
            return "目标已结束", mine
        if any(a.action.kind in ("open", "add") and a.status in ("executed", "pending") for a in actions):
            return "这一轮 Worker 自己下了单", mine
        if any(o.inst_id == inst_id and not o.reduce_only and o.ord_id not in {g["ord_id"] for g in resting} for o in account.orders):
            return "已有入场挂单", mine
        if any(p["action"].get("inst_id") == inst_id and p["action"].get("kind") in ("open", "add") for p in self.store.pending(cfg.env)):
            return "有一笔开仓在等用户同意", mine
        if any(not r["params"].get("plan") and (r["kind"] in self.ENTRY_KINDS or (r["kind"] == "trigger" and str(r["params"].get("do", "")).startswith("open"))) for r in rows):
            return "Worker 自己布置了入场策略", mine
        return "", mine

    @staticmethod
    def _tag(row: dict[str, Any]) -> str:
        return str(row["id"]) if row["kind"] == "limit" else f"#{row['id']}"

    async def _cancel_state(self, cfg: AgentConfig, inst_id: str, oid: str) -> str:
        rows = await self.gw.call("swap_get_order", {"instId": inst_id, "ordId": oid}, env=cfg.env)
        for r in rows or []:
            if r.get("ordId") == oid and r.get("instId") == inst_id and r.get("state") in ("canceled", "filled"):
                size = _f(r.get("accFillSz"), -1)
                if r["state"] == "canceled" and size == 0:
                    return "canceled"
                if size > 0:
                    return "filled"
        return "unknown"

    def _finish_plan_cancel(self, cfg: AgentConfig, inst_id: str, oid: str) -> None:
        for child in self.store.strategies(cfg.env, inst_id):
            if child["kind"] == "reverse" and child["params"].get("parent_order") == oid:
                self.strategies.cancel(child["id"], "原入场单已确认零成交撤销")
        self.store.delete(f"plan_cancel_check:{cfg.env}:{inst_id}")

    async def _drop_plan(self, cfg: AgentConfig, inst_id: str, row: dict[str, Any], why: str) -> None:
        """撤掉一条照计划布置的入场：程序盯着的策略直接作废；挂在交易所的限价单撤单。"""
        if row["kind"] != "limit":
            self.strategies.cancel(row["id"], why)
            return
        oid = row["ord_id"]
        key = f"plan_cancel_check:{cfg.env}:{inst_id}"
        self.store.set(key, oid)
        result = await self.trader.execute(TradeAction(kind="cancel_orders", inst_id=inst_id, ord_id=oid, reason=why), cfg.env, source="guard")
        if result.status != "executed" or await self._cancel_state(cfg, inst_id, oid) != "canceled":
            raise Rejected(f"旧单 {oid} 未确认已撤且零成交，不再提交替代单：{result.message}")
        self._finish_plan_cancel(cfg, inst_id, oid)
        self._emit(cfg.env, inst_id, "strategy", "done", f"照计划挂的{row['id']} 已确认撤销、零成交：{why}", None, result.model_dump(mode="json"))

    def _origin(self, inst_id: str, plan: Any, check: Any, by: str) -> str:
        """这笔单是谁的主意：快判 / 照算法候选 / 改过算法候选 / Worker 自拟。"""
        if by == "快判":
            return "快判"
        return plan_origin(plan, check, (self._state(inst_id).regime or {}).get("candidates") or [])

    @staticmethod
    def _reentry_issue(plan: Any, entry: float, wait: int, lots: list[dict[str, Any]], at: int) -> str:
        near = abs(entry - plan.stop_loss) * 0.5
        for lot in lots:
            ago = (at - (lot.get("closed_ts") or 0)) / 60_000
            if lot["status"] == "closed" and lot["side"] == plan.direction and ago < wait and abs(lot["entry_px"] - entry) <= near:
                side = "做多" if plan.direction == "long" else "做空"
                return (f"同一个位置刚做过一笔（{side}，入场 {lot['entry_px']:g}，{ago:.0f} 分钟前平掉）"
                        f"，冷却期 {wait} 分钟，约还需 {wait - ago:.1f} 分钟")
        return ""

    async def _check_plan(self, cfg: AgentConfig, inst_id: str, plan: Any) -> Any:
        """按现价、现在的点差、这个 Worker 的出手尺度和账户风控预检一个计划。"""
        ticker = await self.market.ticker(inst_id, cfg.env)
        bid, ask = _f(ticker.get("bidPx")), _f(ticker.get("askPx"))
        spread = (ask - bid) / bid * 100 if bid and ask >= bid else 0.0
        check = check_plan(plan, _f(ticker.get("last")), min_rr=STANCES[cfg.stance_for(inst_id)]["min_rr"], cost_pct=trade_cost_pct(cfg.taker_fee_pct, spread),
                           limit_cost_pct=cfg.maker_fee_pct + cfg.taker_fee_pct + spread)
        name = (getattr(plan, "strategy", "") or "").strip()
        if name in losing(cfg.style_for(inst_id)) and not (getattr(plan, "edge", "") or "").strip():
            check.issues.append(
                f"「{name}」机械照做的回测是亏的（{evidence(cfg.style_for(inst_id)).get(name, '')}）：要做就在 plan.edge 写出算法看不到的额外依据（具体数据），"
                "拿不出来就不给 plan，no_plan_reason 选 no_edge")
        if check.ok:
            issue = self._reentry_issue(plan, check.ref, RETRY_MINUTES[cfg.style_for(inst_id)],
                                        self.store.lots(cfg.env, inst_id, live=False, limit=10), now_ms())
            if issue:
                check.issues.append(issue)
        if check.ok:  # 预检张数与保证金：防止止损距离或资金导致算出来不足最小张数，等到落单时才拒单
            try:
                probe = TradeAction(
                    kind="open", inst_id=inst_id, side=plan.direction,
                    order_type="limit" if check.limit else "market",
                    price=check.ref if check.limit else None,
                    stop_loss=plan.stop_loss, take_profit=far_target(plan),
                    leverage=safe_leverage(plan.leverage, check.stop_pct), risk_pct=plan.risk_pct, margin_usdt=plan.margin_usdt,
                )
                await self.trader._dry_run(probe, cfg.env)
            except Rejected as exc:
                if "高于现价" not in str(exc) and "低于现价" not in str(exc):
                    check.issues.append(str(exc))
            except Exception:  # noqa: BLE001 - 网络临时波动不阻断
                pass
        return check

    async def _plan_gate(self, cfg: AgentConfig, inst_id: str, report: CycleReport, deps: Deps, best: dict | None = None) -> str | None:
        """结论交出来之前核对：空仓、没有任何入场在等的时候，要么有一个过得了核对的 plan，要么有不出手的原因。
        返回要退回给 Worker 的话；没问题返回 None。核对本身出错不拦它。"""
        try:
            skip, mine = await self._plan_state(cfg, inst_id, deps.actions)
            if skip:
                return None
            if report.plan is None:
                if (not mine and best and report.no_plan_reason == "cost"
                        and not deps.actions and self.get_config().quant_mode_for(inst_id) == cfg.quant_mode_for(inst_id)
                        and self.get_config().quant_bias_for(inst_id) == cfg.quant_bias_for(inst_id)):
                    plan = best["plan"]
                    live = await self._check_plan(cfg, inst_id, plan)
                    if live.ok and live.limit:
                        return (f"成本结论需要复核：本轮「{best['strategy']}」挂位 {live.ref:g}、止损 {plan.stop_loss:g}，"
                                f"刚按现价复算：{live.numbers()}。若符合用户目标且只是在等到价，给出完整 pullback 计划，"
                                "按用户授权决定仓位，不必等下一轮。如果有真实反对信号或限制，说明具体依据；不要为了交差下单。")
                if report.no_plan_reason or mine:
                    return None
                return (
                    "这一轮的结论落不了地：这个币现在空仓，没有挂单，也没有在等的入场策略，而你既没给 plan，也没选 no_plan_reason。二选一（不出手和出手一样是合格的结论，但要说清是哪一种）：\n"
                    "- 给出 plan（方向、入场区间、止损、止盈、entry_type），程序会直接照着执行。价格在区间中间、方向还没走出来，"
                    "就把入场区间放在区间两头或突破位上（entry_type=pullback / breakout），让价格来找你。\n"
                    "- 不做：no_plan_reason 选 cost / objection / limit / no_edge，并在 summary 里写出具体的数字或缺的依据。"
                )
            check = await self._check_plan(cfg, inst_id, report.plan)
            if check.ok:
                return None
            alternative = ""
            current = self.get_config()
            if (not mine and best and best["plan"] != report.plan and not deps.actions
                    and current.quant_mode_for(inst_id) == cfg.quant_mode_for(inst_id)
                    and current.quant_bias_for(inst_id) == cfg.quant_bias_for(inst_id)):
                try:
                    other = await self._check_plan(cfg, inst_id, best["plan"])
                except Exception as exc:  # noqa: BLE001 - 备选复核失败也要把原计划的问题退回
                    log.warning("复核备选计划失败 %s: %s", inst_id, exc)
                else:
                    if other.ok and other.limit:
                        alternative = (f"另一条候选「{best['strategy']}」@ {other.ref:g} 已通过本轮预检（{other.numbers()}）。"
                                       "结合最新盘面判断这条是否值得做。\n")
            return (
                "你给的 plan 没通过程序核对，现在不会被执行：" + "；".join(check.issues) + "。\n"
                + alternative
                + "改好再交一次（价位要落在行情快照里的真实结构位上，不要为了凑数把目标挂到够不着的地方）；确实做不出来就不给 plan，并选 no_plan_reason。"
            )
        except Exception as exc:  # noqa: BLE001 - 核对用的行情 / 账户拿不到：不拦这一轮
            log.warning("核对计划失败 %s: %s", inst_id, exc)
            return None

    async def _enter_now(self, cfg: AgentConfig, inst_id: str, plan: Any, check: Any, reason: str, by: str) -> Any:
        """按一个核对过的计划现价进场（照样过权限和硬性风控），成交后把管理交给执行器：移保本、分批止盈、超时离场。"""
        action = TradeAction(
            kind="open", inst_id=inst_id, side=plan.direction, order_type="market", stop_loss=plan.stop_loss, take_profit=far_target(plan),
            leverage=safe_leverage(plan.leverage, check.stop_pct), risk_pct=plan.risk_pct, margin_usdt=plan.margin_usdt, reason=reason,
        )
        result = await self.trader.submit(action, source="agent")
        if result.status == "executed":
            self.scores.tag(cfg.env, inst_id, plan.direction, getattr(plan, "strategy", "") or by, f"{by}现价进场", origin=self._origin(inst_id, plan, check, by))
        if result.status == "executed" and result.detail.get("lot_id"):
            try:
                rules = {**manage_rules(plan, check, cfg.style_for(inst_id)), "lot_id": result.detail["lot_id"], "plan": True}
                self.strategies.deploy(cfg, inst_id, "manage", "", rules, note=f"{by}进场后的管理", by=by)
            except Rejected as exc:
                log.warning("进场后没布置成管理规则 %s: %s", inst_id, exc)
        return result

    def _arm_reverse(self, cfg: AgentConfig, inst_id: str, plan: Any, minutes: float, parent_order: str | None = None) -> str:
        """计划里带了反手：下单的同时把「止损后反手」布置好。返回给 Worker 看的一句话（没带反手返回空串）。"""
        if not plan.reverse_trigger:
            return ""
        side = "short" if plan.direction == "long" else "long"
        sign = 1 if side == "long" else -1
        if not plan.reverse_stop:
            return "；反手没布置：填了 reverse_trigger 就要同时给 reverse_stop"
        if (plan.reverse_trigger - plan.stop_loss) * sign < 0:
            return f"；反手没布置：触发价 {plan.reverse_trigger:g} 要放在这笔单止损 {plan.stop_loss:g} 的外侧（止损被打之后价格才会到的地方）"
        try:
            row = self.strategies.deploy(cfg, inst_id, "reverse", side, {
                "trigger": plan.reverse_trigger, "stop_loss": plan.reverse_stop, "take_profit": plan.reverse_target, "leverage": plan.leverage,
                "risk_pct": plan.risk_pct, "expires_minutes": minutes + 5, "strategy_name": "止损后反手", "parent_order": parent_order,
            }, note="照计划布置的止损后反手", by="计划")
        except Rejected as exc:
            return f"；反手没布置：{str(exc)[:120]}"
        return f"；已布置止损后反手 #{row['id']}：{describe_strategy(row)}"

    async def _arm_plan(self, cfg: AgentConfig, inst_id: str, report: CycleReport, deps: Deps, work_id: int | None = None) -> dict[str, Any] | None:
        """复盘出了结论：空仓、又没有别的入场在等，就照 plan 下单——现价已经在入场区间就市价进，没到就布置成到价进场的策略。
        照样过权限和硬性风控。返回这一步做了什么（记进复盘记录，下一轮告诉 Worker）；什么都不用做返回 None。"""
        if not cfg.auto_arm_plan:
            return {"status": "advised", "text": "仅展示计划：计划自动执行已关闭，程序未按此计划下单。"} if report.plan else None
        try:
            skip, mine = await self._plan_state(cfg, inst_id, deps.actions)
            plan, ids = report.plan, "、".join(self._tag(r) for r in mine)
            if plan is None:
                if mine and report.no_plan_reason in ("objection", "limit"):
                    for r in mine:
                        await self._drop_plan(cfg, inst_id, r, f"Worker 放弃了这个计划：{self.NO_PLAN[report.no_plan_reason]}")
                    return {"status": "cancelled", "text": f"你不再给计划（{self.NO_PLAN[report.no_plan_reason]}），程序已经撤掉之前照计划布置的 {ids}"}
                if mine:
                    return {"status": "kept", "text": f"之前照计划布置的 {ids} 继续等（价格先到止损、没回来就先到第一目标、或者到期，它会自己作废）"}
                if not skip and report.no_plan_reason:
                    wait = "；".join(f"{w.price:g}：{w.note}" for w in report.watch_levels[:3])
                    return {"status": "watching", "text": f"本轮没有入场委托：{self.NO_PLAN[report.no_plan_reason]}。"
                            + (f"观察位：{wait}（触及只复核）。" if wait else "没有设置观察价，等待下一次行情事件或定时复核。")}
                return None
            if skip:
                if skip == "已有入场挂单":
                    account = await self.trader.account(cfg.env)
                    pxs = "、".join(f"{o.side} @ {o.px:g}" for o in account.orders if o.inst_id == inst_id and not o.reduce_only and o.px)
                    if pxs:
                        return {"status": "kept", "text": f"欧易上已有入场限价单（{pxs}），本轮不重复挂单"}
                return {"status": "skipped", "text": f"本轮没有按此计划另挂新单：{skip}。"}
            check = await self._check_plan(cfg, inst_id, plan)
            if not check.ok:
                text = "计划没通过程序核对，没有执行：" + "；".join(check.issues) + (f"。本轮未更改已有委托 {ids}" if mine else "")
                self._emit(cfg.env, inst_id, "strategy", "done", text, work_id)
                return {"status": "skipped", "text": text}
            style = cfg.style_for(inst_id)
            if len(mine) == 1 and same_plan(mine[0], plan, check, style):
                existing = mine[0]
                ref = {"ord_id": existing["ord_id"]} if existing["kind"] == "limit" else {"strategy_id": existing["id"]}
                return {"status": "kept", **ref, "text": f"和已经布置的 {self._tag(existing)} 是同一个计划，继续等"}
            for r in mine:
                await self._drop_plan(cfg, inst_id, r, "计划更新了，换成新的")
            if mine:
                blocked, remaining = await self._plan_state(cfg, inst_id, deps.actions)
                updated = await self._check_plan(cfg, inst_id, plan)
                if blocked or remaining or not updated.ok or (updated.how, updated.limit) != (check.how, check.limit):
                    return {"status": "skipped", "text": f"撤旧后没有换单：{blocked or '挂单状态、行情或执行条件已变化'}，等待重新评估。"}
                check = updated
            for old_t in self.store.strategies(cfg.env, inst_id):  # 顺手清理旧版遗留的递归 task 触发器，防止反复打扰
                if old_t["kind"] == "trigger" and not old_t["params"].get("recorder") and old_t["params"].get("do") == "task" and old_t["status"] in ("proposed", "armed"):
                    self.strategies.cancel(old_t["id"], "已由直接挂单的交易计划替代")
            style, side = cfg.style_for(inst_id), ("做多" if plan.direction == "long" else "做空")
            if check.limit:  # 等价格回来的单：直接在交易所挂限价单，带止损止盈；没回来就先到了第一目标、或者过了有效期，程序撤掉
                tps = sorted(plan.take_profits, reverse=plan.direction == "short")
                action = TradeAction(
                    kind="open", inst_id=inst_id, side=plan.direction, order_type="limit", price=check.ref, stop_loss=plan.stop_loss, take_profit=far_target(plan),
                    leverage=safe_leverage(plan.leverage, check.stop_pct), risk_pct=plan.risk_pct, margin_usdt=plan.margin_usdt, cancel_px=tps[0],
                    require_flat=True, cancel_before_entry=plan.cancel_before_entry,
                    expires_minutes=plan.valid_minutes or STYLES[style]["plan_minutes"], reason=f"照计划挂限价单（{check.numbers()}）",
                )
                result = await self.trader.submit(action, source="agent", far_ok=True)
                deps.actions.append(result)
                if result.status == "executed":
                    self.scores.tag(cfg.env, inst_id, plan.direction, plan.strategy, "计划挂限价单", ttl_minutes=(action.expires_minutes or 60) + 2, origin=self._origin(inst_id, plan, check, "计划"))
                    key = f"order_guard:{cfg.env}:{inst_id}"
                    terms = plan_terms(plan, style)
                    self.store.set(key, [{**g, "plan": True, "stop_loss": plan.stop_loss, "terms": terms, "cancel_before_entry": terms.get("cancel_before_entry")} if g["ord_id"] == str(result.detail.get("ordId")) else g for g in self.trader.order_guards(cfg.env, inst_id)])
                    if result.detail.get("lot_id"):
                        try:  # 成交之后的管理先布置好：挂单没成交时它只是等着
                            self.strategies.deploy(cfg, inst_id, "manage", "", {**manage_rules(plan, check, style), "lot_id": result.detail["lot_id"], "plan": True}, note="照计划挂单成交后的管理", by="计划")
                        except Rejected as exc:
                            log.warning("挂单后没布置成管理规则 %s: %s", inst_id, exc)
                extra = self._arm_reverse(cfg, inst_id, plan, action.expires_minutes or 60, result.detail.get("ordId")) if result.status == "executed" else ""
                if result.status != "executed":
                    label = "限价单提交未确认，请核对交易所回执" if result.status == "failed" else "本轮限价单未提交到交易所"
                    return {"status": result.status, "text": f"{label}：{result.message}"}
                return {"status": "armed", "ord_id": result.detail.get("ordId"), "text": f"程序照计划挂了限价{'买' if plan.direction == 'long' else '卖'}单 @ {check.ref:g}：{result.message}{extra}"}
            if check.how == "now":
                result = await self._enter_now(cfg, inst_id, plan, check, f"照计划现价进场（{check.numbers()}）", "计划")
                deps.actions.append(result)
                extra = self._arm_reverse(cfg, inst_id, plan, 5) if result.status == "executed" else ""
                return {"status": result.status, "ord_id": result.detail.get("ordId"), "text": f"程序照计划现价{side}：{result.message}{extra}"}
            row = self.strategies.deploy(cfg, inst_id, check.how, plan.direction, {**order_params(plan, check, style), "strategy_name": plan.strategy or None}, note=f"照计划布置（{check.numbers()}）", by="计划")
            return {
                "status": row["status"], "strategy_id": row["id"],
                "text": f"程序照计划布置了突破追单 #{row['id']}（突破单不能提前挂限价单，否则会当场按现价成交；已由执行器秒级盯盘）：{describe_strategy(row)}"
                        + ("，等你同意后启用" if row["status"] == "proposed" else ""),
            }
        except Exception as exc:  # noqa: BLE001 - 没做成要让用户和 Worker 都看得到原因，不能让整轮复盘跟着失败
            text = f"照计划下单没做成：{str(exc)[:260] or type(exc).__name__}"
            self._emit(cfg.env, inst_id, "strategy", "error", text, work_id)
            return {"status": "failed", "text": text}

    async def apply_plan(self, inst_id: str, stamp: int) -> dict[str, Any]:
        cfg = self.get_config()
        env = cfg.env
        async with self._turn(cfg, inst_id, "应用入场计划"):
            cfg = self.get_config()
            if cfg.env != env:
                raise Rejected("交易环境已切换，请刷新后再操作。")
            record = self.store.get(f"report:{cfg.env}:{inst_id}") or {}
            if record.get("ts") != stamp or not record.get("plan") or inst_id not in cfg.instruments:
                raise Rejected("这份计划已更新，请刷新后再操作。")
            if cfg.worker(inst_id).paused or now_ms() >= stamp + (record["plan"].get("valid_minutes") or STYLES[cfg.style_for(inst_id)]["plan_minutes"]) * 60_000:
                raise Rejected("Worker 已暂停或计划已过期，请先复盘。")
            if (record.get("plan_order") or {}).get("status") not in (None, "advised"):
                raise Rejected("这份计划已经处理过，请查看委托或待确认操作。")
            deps = Deps(trader=self.trader, market=self.market, gateway=self.gw, config=cfg, kb=self.kb)
            result = await self._arm_plan(cfg.model_copy(update={"auto_arm_plan": True}), inst_id, CycleReport.model_validate(record), deps)
            if result is None:
                raise Rejected("当前计划不能提交，请重新复盘。")
            record["plan_order"] = result
            if deps.actions and deps.actions[-1].pending_id:
                result["pending_id"] = deps.actions[-1].pending_id
            self.store.set(f"report:{cfg.env}:{inst_id}", record)
            self._state(inst_id).report = record
            self.bus.publish("agent", self.status())
            return result

    async def plan_receipt(self, inst_id: str, stamp: int) -> dict[str, Any]:
        cfg = self.get_config()
        record = self.store.get(f"report:{cfg.env}:{inst_id}") or {}
        if stamp != record.get("ts"):
            return {"state": "updated"}
        result = record.get("plan_order") or {}
        oid = result.get("ord_id")
        if pid := result.get("pending_id"):
            pending = self.store.get_pending(pid) or {}
            if pending.get("status") != "executed":
                return {"state": pending.get("status") or "unknown"}
            oid = ((pending.get("result") or {}).get("detail") or {}).get("ordId")
        if sid := result.get("strategy_id"):
            rule = self.store.get_strategy(sid)
            if rule:
                oid = rule["state"].get("ord_id")
                if not oid:
                    return {"state": rule["status"], "text": rule["note"]}
        if not oid and result.get("status") in ("armed", "kept"):
            plan = record.get("plan") or {}
            oid = next((g["ord_id"] for g in self.trader.order_guards(cfg.env, inst_id)
                        if g.get("plan") and g["side"] == plan.get("direction")
                        and plan["entry_low"] <= g["price"] <= plan["entry_high"]), None) if plan else None
        if oid:
            try:
                receipts = await self.gw.call("swap_get_order", {"instId": inst_id, "ordId": str(oid)}, env=cfg.env)
            except (OkxError, Rejected):
                receipts = []
            match = next((r for r in receipts or [] if r.get("instId") == inst_id and str(r.get("ordId")) == str(oid)), None)
            if match:
                return {"state": match.get("state", "unknown"), "ord_id": str(oid), "filled": _f(match.get("accFillSz")), "price": _f(match.get("px"))}
            return {"state": "unknown", "ord_id": str(oid)}
        return {"state": "draft" if result.get("status") in (None, "advised") else "unknown" if result.get("status") in ("armed", "kept", "executed") else result["status"]}

    def _finish_review(self, cfg: AgentConfig, inst_id: str, work_id: int, result: Any, deps: Deps, extra: dict[str, Any]) -> dict[str, Any]:
        """一轮复盘出了结论：记下来、排下一次、把值得用户知道的发出去。"""
        st = self._state(inst_id)
        report: CycleReport = result.output
        usage = _cache_usage(result.all_messages())
        record = {
            **report.model_dump(),
            "ts": now_ms(),
            "price": extra["price"],
            "reason": extra["reason"],
            "actions": [{"status": a.status, "title": a.action.kind, "message": a.message} for a in deps.actions],
            "knowledge": extra["knowledge"],
            **usage,
            **st.prompt_stats,
            "tokens": usage["input_tokens"] + usage["output_tokens"],
            "effort": extra["effort"],
            "model": usage["model"] or cfg.model_for(inst_id),
            "prompt_cache_key": _worker_cache_key("review", inst_id),
            "plan_order": extra.get("plan_order"),
        }
        if record["plan_order"] and deps.actions and deps.actions[-1].pending_id:
            record["plan_order"]["pending_id"] = deps.actions[-1].pending_id
        if usage["model_requests"]:  # 用量全缺失的轮次也要记，才能统计有多少请求没有读数
            log = self.store.get(f"usage:{cfg.env}", [])
            log.append({
                "ts": record["ts"], "inst_id": inst_id, **st.prompt_stats,
                **{k: usage[k] for k in ("input_tokens", "cached_tokens", "cache_write_tokens",
                                         "first_input_tokens", "first_cached_tokens", "model_requests", "unmeasured_requests")},
            })
            self.store.set(f"usage:{cfg.env}", log[-USAGE_LOG:])
        previous = st.report
        st.report = record
        st.observe_price = None
        st.skips = 0
        st.last_review_ts = time.time()
        st.last_review_px = extra["price"]
        st.next_due_ts = st.last_review_ts + min(max(report.next_check_seconds, cfg.min_review_seconds), cfg.review_seconds, STYLES[cfg.style_for(inst_id)]["max_review"])
        self.store.set(f"report:{cfg.env}:{inst_id}", record)
        self._sync_observation_rules(cfg, inst_id)
        self._note("cycle", report.summary, record, inst_id=inst_id)
        self.store.update_work(work_id, "done", report.summary)
        for a in deps.actions:
            if a.status != "pending":
                self._emit(cfg.env, inst_id, "worker", "action", a.message, work_id, a.model_dump(mode="json"))
            else:  # 走到这里还在等同意的，只可能是程序照计划提交的那一笔：活动流里给它一个同意按钮
                self._emit(cfg.env, inst_id, "worker", "ask", "想照计划开仓，等你同意", work_id, {"pending_id": a.pending_id, "tool": "open_position", "title": a.message, "why": a.action.reason})
        self._emit(cfg.env, inst_id, "worker", "done", report.summary, work_id, {"bias": report.bias, "plan": record.get("plan"), "tokens": record.get("tokens")})
        before = len(self.store.get(f"chat:{inst_id}", []))
        self._relay(cfg, inst_id, record, previous, list(deps.actions))
        if st.steered and len(self.store.get(f"chat:{inst_id}", [])) == before:  # 半路有人送话进来：结论一定发回对话
            self._post(inst_id, report.summary, [a.model_dump(mode="json") for a in deps.actions if a.status != "pending"])
        st.steered = False
        return record

    # ---------- 等用户同意的工具调用（PydanticAI 的人工批准）：这一轮挂起，用户决定后原样继续 ----------

    def _deferred(self, env: str) -> dict[str, dict[str, Any]]:
        return self.store.get(f"deferred:{env}", {})

    def awaiting(self, env: str, inst_id: str) -> list[dict[str, Any]]:
        return [r for r in self._deferred(env).values() if r["inst_id"] == inst_id]

    def _post(self, inst_id: str, text: str, cards: list[dict[str, Any]]) -> None:
        """Worker 主动往这个币的对话区发一条消息。"""
        rows = self.store.get(f"chat:{inst_id}", [])
        rows.append({"role": "ai", "text": text, "actions": cards, "proactive": True, "from": f"{inst_id} Worker", "ts": now_ms()})
        self.store.set(f"chat:{inst_id}", rows[-200:])
        self.bus.publish("chat", {"thread": inst_id, "ts": now_ms()})

    def _hold_run(self, cfg: AgentConfig, inst_id: str, kind: str, work_id: int, result: Any, deps: Deps, extra: dict[str, Any]) -> dict[str, Any]:
        """Agent 调用了需要用户同意的工具：把这一轮（连同完整的上下文）存起来。用户同意或拒绝之后，带着结果从这里接着跑。"""
        req: DeferredToolRequests = result.output
        calls = {part.tool_call_id: {**(req.metadata.get(part.tool_call_id) or {}), "tool": part.tool_name} for part in req.approvals}
        record = {
            "kind": kind, "inst_id": inst_id, "work_id": work_id, "calls": calls, "ts": now_ms(), **extra,
            "history": ModelMessagesTypeAdapter.dump_python(result.all_messages(), mode="json"),
        }
        self.store.set(f"deferred:{cfg.env}", {**self._deferred(cfg.env), str(work_id): record})
        self.store.update_work(work_id, "waiting", f"有 {len(calls)} 个操作等你同意")
        st = self._state(inst_id)
        st.last_review_ts, st.last_review_px = time.time(), st.last_px or st.last_review_px
        cards = [a.model_dump(mode="json") for a in deps.actions if a.status == "pending"]
        for call in calls.values():  # 挂起的是「布置策略」：同样要有一张带同意按钮的卡片
            row = self.store.get_strategy(call.get("strategy_id") or 0)
            if row:
                cards.append({
                    "status": "pending", "strategy_id": row["id"], "message": describe_strategy(row),
                    "detail": {"label": STRATEGY_KINDS[row["kind"]]["label"], "note": row["note"]},
                    "action": {"kind": "deploy_strategy", "inst_id": inst_id, "side": row["side"] or None, "reason": row["note"]},
                })
        # 活动流里每个等同意的调用带上它的编号，界面才能在那一步上放同意按钮
        for call in calls.values():
            card = next((c for c in cards if (call.get("pending_id") and c.get("pending_id") == call["pending_id"]) or (call.get("strategy_id") and c.get("strategy_id") == call["strategy_id"])), None)
            data = {k: call[k] for k in ("pending_id", "strategy_id", "tool") if k in call}
            if card:  # 处理完之后卡片还要看得出当时想做的是什么
                data |= {"title": card["message"], "why": (card.get("action") or {}).get("reason") or ""}
            self._emit(cfg.env, inst_id, "worker", "ask", f"想{_TOOL_LABEL.get(call['tool'], call['tool'])}，等你同意", work_id, data)
        for a in deps.actions:
            if a.status == "executed":
                self._emit(cfg.env, inst_id, "worker", "action", a.message, work_id, a.model_dump(mode="json"))
        deps.cards = cards  # 对话里交来的任务：卡片跟着回答一起显示
        if kind == "review":
            self._post(inst_id, f"我想做下面{'这件事' if len(calls) == 1 else f'这 {len(calls)} 件事'}，等你同意。你同意或拒绝之后我接着往下做。", cards)
        self.bus.publish("work", {"inst_id": inst_id})
        self.bus.publish("agent", self.status())
        return {"awaiting": len(calls), "summary": f"有 {len(calls)} 个操作等你同意", "work_id": work_id}

    def _call_state(self, call: dict[str, Any]) -> tuple[str, str]:
        """一次挂起的调用现在是什么情况：waiting 还在等 / approved 用户同意了 / denied 没同意（带原因）。"""
        if call.get("pending_id"):
            row = self.store.get_pending(call["pending_id"])
            if row and row["status"] == "waiting" and now_ms() - row["ts"] > PENDING_TTL_MS:
                self.store.resolve_pending(row["id"], "expired", {})
                row["status"] = "expired"
            if not row or row["status"] == "declined":
                return "denied", "用户没有同意这次操作。问清楚或者调整计划，不要原样再提一次。"
            if row["status"] == "expired":
                return "denied", "超过 10 分钟没有得到用户同意，这次操作已作废。行情可能已经变了，请按最新情况重新判断。"
            return ("waiting", "") if row["status"] in ("waiting", "running") else ("approved", "")
        row = self.store.get_strategy(call.get("strategy_id") or 0)
        if row:
            row = self.strategies.expire(row)
        if row and row["status"] == "proposed":
            return "waiting", ""
        if not row or row["status"] in ("cancelled", "expired"):
            return "denied", f"用户没有同意启用这条策略（{row['note'] if row else '已删除'}）。"
        return "approved", ""

    def on_decided(self) -> None:
        """用户对某个挂起的调用做了决定（或者它过期了）：一轮里所有挂起的调用都有了结果，就带着结果接着跑。"""
        env = self.get_config().env
        records = self._deferred(env)
        ready = [key for key, rec in records.items() if all(self._call_state(c)[0] != "waiting" for c in rec["calls"].values())]
        if not ready:
            return
        self.store.set(f"deferred:{env}", {k: v for k, v in records.items() if k not in ready})
        for key in ready:
            task = asyncio.create_task(self._resume(records[key]), name=f"resume-{records[key]['inst_id']}")
            self._resumes.add(task)
            task.add_done_callback(self._resumes.discard)

    async def _resume(self, rec: dict[str, Any]) -> None:
        cfg = self.get_config()
        inst_id, work_id, kind = rec["inst_id"], rec["work_id"], rec["kind"]
        st = self._state(inst_id)
        approvals: dict[str, Any] = {}
        for call_id, call in rec["calls"].items():
            state, why = self._call_state(call)
            approvals[call_id] = True if state == "approved" else ToolDenied(why)
            label = _TOOL_LABEL.get(call["tool"], call["tool"])
            self._emit(cfg.env, inst_id, "worker", "answer", f"你同意了「{label}」，结果交还给 Worker" if state == "approved" else f"「{label}」没有执行：{why}", work_id, {"approved": state == "approved"})
        results = DeferredToolResults(approvals=approvals, metadata={k: {x: v[x] for x in ("pending_id", "strategy_id") if x in v} for k, v in rec["calls"].items()})
        deps = Deps(
            trader=self.trader, market=self.market, gateway=self.gw, config=cfg, kb=self.kb, skills=self.skills, lessons=self.lessons, propose_only=kind == "task",
            remember=lambda inst, note: self._remember(cfg.env, inst, note),
            sub=self._sub_from_worker, spawn=self._spawn, close_sub=self._close_sub, emit=self._emitter(cfg.env, inst_id, "worker", work_id), strategy=self.strategies, standby=self._stop_watching,
            plan_check=self._plan_checker(cfg, inst_id) if kind == "review" else None, set_quant=self._set_quant,
        )
        agent = self._worker_agent(cfg, inst_id) if kind == "review" else self._task_agent(cfg, inst_id)
        try:
            async with self._turn(cfg, inst_id, "按你的决定继续"):
                self.store.update_work(work_id, "running")
                self.bus.publish("agent", self.status())
                result = await self._run(
                    agent, None, st, REVIEW_TIMEOUT, live=LiveText(self.bus.publish, "worker", inst_id), deps=deps, message_history=ModelMessagesTypeAdapter.validate_python(rec["history"]),
                    deferred_tool_results=results, usage_limits=UsageLimits(request_limit=20), model_settings=_effort(rec.get("effort") or cfg.effort_for(inst_id), _worker_cache_key(kind, inst_id)),
                )
                extra = {k: rec.get(k) for k in ("reason", "price", "knowledge", "effort", "thread")}
                if isinstance(result.output, DeferredToolRequests):
                    self._hold_run(cfg, inst_id, kind, work_id, result, deps, extra)
                elif kind == "review":
                    extra["plan_order"] = await self._arm_plan(cfg, inst_id, result.output, deps, work_id)
                    self._finish_review(cfg, inst_id, work_id, result, deps, extra)
                else:
                    self.store.update_work(work_id, "done", result.output)
                    for a in deps.actions:
                        self._emit(cfg.env, inst_id, "worker", "action", a.message, work_id, a.model_dump(mode="json"))
                    self._emit(cfg.env, inst_id, "worker", "done", result.output, work_id)
                    self._post(inst_id, result.output, [])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务：失败要落到任务和活动流里，不能悄悄没了
            self.store.update_work(work_id, "failed", str(exc)[:300] or type(exc).__name__)
            self._emit(cfg.env, inst_id, "worker", "error", f"按你的决定继续时出错：{str(exc)[:260] or type(exc).__name__}", work_id)
            log.warning("恢复挂起的任务失败 %s: %s", inst_id, exc)
        finally:
            self.bus.publish("work", {"inst_id": inst_id})
            self.bus.publish("agent", self.status())

    def _relay(self, cfg: AgentConfig, inst_id: str, record: dict[str, Any], previous: dict[str, Any] | None, actions: list[Any]) -> None:
        """Worker 不直接对用户说话：由 Router 把值得用户知道的结果主动发到这个币的对话区。没有新情况就保持安静。"""
        cards = [a.model_dump(mode="json") for a in actions if a.status in ("pending", "executed", "advised")]
        plan, old = record.get("plan"), (previous or {}).get("plan")
        new_plan = bool(plan) and (not old or old.get("direction") != plan.get("direction") or abs(old.get("entry_low", 0) - plan.get("entry_low", 0)) > 1e-12)
        label = {"hold": "继续拿", "tighten_stop": "收紧止损", "adjust_tp": "调整止盈", "reduce": "减仓", "add": "加仓", "close": "平仓"}
        verdicts = [v for v in record.get("lot_verdicts") or [] if v.get("verdict") != "hold"]
        old_verdicts = {(v.get("lot_id"), v.get("verdict")) for v in (previous or {}).get("lot_verdicts") or []}
        fresh = [v for v in verdicts if (v.get("lot_id"), v.get("verdict")) not in old_verdicts]
        if not cards and not new_plan and not fresh:
            return
        if any(a.status == "pending" for a in actions):
            head = "有一个方案等你确认："
        elif any(a.status == "executed" for a in actions):
            head = "我刚做了操作："
        elif cards or (fresh and not new_plan):
            head = "对当前仓位的建议："
        else:
            side = "做多" if plan["direction"] == "long" else "做空"
            head = f"新的交易计划：{side}，入场区间 {plan['entry_low']:g} ~ {plan['entry_high']:g}，止损 {plan['stop_loss']:g}。"
        log_rows = self.store.get(f"chat:{inst_id}", [])
        advice = "".join(f"\n- 第 {v['lot_id']} 笔：**{label.get(v['verdict'], v['verdict'])}**，{v['reason']}" for v in verdicts)
        log_rows.append({"role": "ai", "text": f"{head}\n{record['summary']}{advice}", "actions": cards, "proactive": True, "from": f"{inst_id} Worker", "ts": now_ms()})
        self.store.set(f"chat:{inst_id}", log_rows[-200:])
        self.bus.publish("chat", {"thread": inst_id, "ts": now_ms()})

    async def _knowledge(self, st: InstState, snapshot: dict[str, Any], pos: Position | None, reason: str, quant: dict[str, Any] | None) -> list[Any]:
        """这一轮给 Worker 看的知识库条目。同一种行情状态（由它翻译出来的那几条查询）在 KB_HOLD_SECONDS 内用同一批：
        叫醒原因每轮都不一样，拿它去查会让条目在名额边上来回换，前面排好的内容就用不上缓存；价格在某条线两边来回时状态也会来回，
        回到刚才那种状态就用刚才那一批，文字一字不差。新出现的状态或过了时限才重新查，那时带上叫醒原因。"""
        state, now = tuple(kb_queries(snapshot, pos, "", quant)), time.time()
        st.kb_picks = {key: pick for key, pick in st.kb_picks.items() if now - pick[0] < KB_HOLD_SECONDS}
        if state not in st.kb_picks:
            st.kb_picks[state] = (now, await self.kb.context([*state, reason], k=8, max_chars=4200))
        return st.kb_picks[state][1]

    def _prompt(self, cfg: AgentConfig, inst_id: str, reason: str, snapshot: dict[str, Any], account: AccountState, last: dict[str, Any] | None, knowledge: list[Any], closed: list[Any] = (), *, candidates: list[dict] | None = None) -> str:  # type: ignore[assignment]
        r = cfg.risk
        pos = account.position(inst_id)
        stance, entry = STANCES[cfg.stance_for(inst_id)], ENTRY_MODES[cfg.entry_mode_for(inst_id)]
        closed_data, live_snapshot = _split_closed(snapshot)
        dump = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
        notes = (
            "最快一小时变一次",
            "最快 15 分钟变一次",
            "最快 5 分钟变一次",
        )
        lines = [
            f"【品种】{inst_id}（{'模拟盘' if cfg.env == 'demo' else '实盘'}）",
            *(
                f"【已收盘数据·{name}（{notes[i]}，这一轮现算的。timeframes 各周期：candles 是较早的一整段已收盘 K 线、new_candles 是接在它后面新收的几根 [时间,开,高,低,收,量比]，其余是只用已收盘 K 线算的指标；"
                "windows 是前高前低法各段的读数；value_area 是上一个 UTC 日的价值区间。同一个周期、同一段 windows 的内容可能分在几段已收盘数据里，"
                "随报价变的部分（最新价、没收盘的那根 K 线、区间位置、支撑阻力、形态）在后面行情快照的同名位置）】\n" + dump(data)
                for i, ((name, _), data) in enumerate(zip(CLOSED_BUCKETS, closed_data)) if data
            ),
            f"【这个品种的交易风格】{STYLES[cfg.style_for(inst_id)]['label']}（{STYLES[cfg.style_for(inst_id)]['edge']}）：{STYLES[cfg.style_for(inst_id)]['guide']}",
            f"【出手尺度】{stance['label']}（到第一目标的净盈亏比至少 {stance['min_rr']:g}）：{stance['guide']}",
            f"【入场方式】{entry['label']}：{entry['guide']}"
            f"距离：挂着等的入场区间离现价最好不超过 {cfg.limit_dist_for(cfg.style_for(inst_id)):g}%（你自己用 open_position 挂限价单时超过这个距离会被系统拒绝）。",
            f"【运行设置】{'模拟盘' if cfg.env == 'demo' else '实盘'}；{_permissions_text(cfg, inst_id)}；保证金模式 {cfg.margin_mode}；"
            f"最高杠杆 {r.max_leverage}x；单仓风险上限 {r.risk_per_trade_pct:g}% 权益；最多加仓 {r.max_adds} 次；"
            f"{'允许' if r.allow_averaging_down else '禁止'}亏损加仓；当日亏损上限 {r.max_daily_loss_pct:g}%",
            f"【叫醒原因】{reason}",
            f"【现在时间】{time.strftime('%Y-%m-%d %H:%M:%S')}（本地时间）",
            _trend_text(cfg.style_for(inst_id), snapshot),
            self._cost_text(cfg, stance["min_rr"], snapshot),
        ]
        qmode, qbias = cfg.quant_mode_for(inst_id), cfg.quant_bias_for(inst_id)
        qspec, w_override = QUANT_MODES[qmode], cfg.worker(inst_id)
        metrics = self.strategies.fresh_metrics(cfg.env, inst_id)
        narrative = metrics.get("narrative")
        lines.append(
            f"【当前量化档位】{qspec['label']}（{'双向' if qbias == 'both' else '只盯多' if qbias == 'long' else '只盯空'}"
            + (f"，放量门槛 {w_override.quant_vol_ratio:g}x" if w_override.quant_vol_ratio else "")
            + f"）：{qspec['desc']}"
            + (f"｜设定原因（{w_override.quant_by or '系统'}）：{w_override.quant_reason}" if w_override.quant_reason else "")
            + (f"\n【实时盘面白话诊断】{narrative}" if narrative else "")
        )
        rec_snap = self.strategies.snapshot(inst_id, cfg.env)
        opening = self._state(inst_id).opening_quote
        if opening.get("env") == cfg.env and -2000 <= now_ms() - opening.get("ts", 0) <= 15_000:
            minutes = self.store.opening_minutes(cfg.env, inst_id)
            lines.append("【开盘记录器】新报价对比本场和本根 5m 开盘；pre_open 为开盘前完整一小时，previous_closes 为已收盘 5m。"
                         "最近分钟仅为轮询采到的首/高/低/末价，并非逐笔或收盘确认；缺字段表示无可靠数据。\n"
                         + json.dumps({"quote": opening, "sampled_minutes": minutes}, ensure_ascii=False))
        if rec_snap:
            lines.append(f"【记录器实时读数（算法持续采样）】{rec_snap}")
        turn = metrics.get("shift")
        state = turn_state(turn, metrics.get("turn"), self._state(inst_id).last_retest)
        if state and state["stage"] == "early":
            lines.append(f"【转折 · 苗头（程序算的，5 分钟图）】{state['text']}。三步：扫流动性 → 动能背离 → 收盘反向越过摆动点；只有第三步过了才算转折确认，"
                         "现在还没到，不能单凭它抄底摸顶。")
        elif state:
            lines.append(
                f"【转折 · {state['label']}（5 分钟图，{turn['bars_ago']} 根 K 线前，程序算的）】{state['text']}。"
                f"在这之后：顺着新方向的回踩位在 {turn['level']:g} 附近；留在起动点 {turn['origin']:g} 后面的旧支撑阻力，价格基本回不去了，不要再把单挂在那里；"
                "逆着新方向的单只在更大周期的关键位上出现反转信号时才做。"
                + ("盘中已经失守过被越过的摆动点：这次转折可能是假突破，顺着它的单先别加。" if state["stage"] == "lost" else "")
                + "它的用处是判断旧方向是不是已经失效（手里逆着它的仓要重新评估）；回测里机械地照它进场并不赚钱（见知识库 evidence），要和大周期位置、目标距离一起看。"
            )
        if not pos and isinstance(snapshot, dict) and snapshot.get("last"):
            cands = candidates_text(
                snapshot, style=cfg.style_for(inst_id), min_rr=stance["min_rr"],
                maker_fee_pct=cfg.maker_fee_pct, taker_fee_pct=cfg.taker_fee_pct, shift=turn,
                quant_mode=qmode, quant_bias=qbias, entry_mode=cfg.entry_mode_for(inst_id), rows=candidates,
            )
            if cands:
                lines.append(cands)
            stale_info = self._stale_lines(cfg, inst_id, self._state(inst_id), float(snapshot["last"]))
            if stale_info and "没有等着" not in stale_info:
                lines.append(f"【已有线与挂单可达性检查】{stale_info}")
        if self._fast_on(cfg, inst_id):
            mode, seen = cfg.fast_entry_for(inst_id), self._state(inst_id).fast_last
            how = {
                "verify": "出了信号马上叫醒你核实，你认可才下单。on_signal 现在不起作用",
                "agent": "你在结论的 on_signal 里授权过的方向（long / short / both），出了信号程序直接照它算好的价位下单，做完再告诉你；没授权的方向叫醒你核实。"
                         "大方向明确、你愿意让程序替你抢这一下时才授权那个方向；震荡、方向不明就留 verify",
            }[mode]
            lines.append(
                "【快判】程序每几秒用 1 分钟 K 线判断一次盘是不是朝一个方向动了（收盘突破关键线、放量急动、假突破收回），入场、止损、目标由程序算好，"
                f"和你的计划走同一套核对。现在的用法：{how}。"
                + (f"最近一次（{int((now_ms() - seen['ts']) / 60000)} 分钟前）：{seen['text']} → {seen['result'] or seen['status']}" if seen else "")
            )
        if cfg.prefs_text():
            lines.append(f"【用户偏好（照着做，和你的习惯冲突时以它为准）】{cfg.prefs_text()}")
        if cfg.notes.strip():
            lines.append(f"【用户交代】{cfg.notes.strip()}")
        day = f"{account.day_pnl_pct:+.2f}%" if account.day_pnl_pct is not None else "未知"
        lines.append(
            f"【账户】权益 {account.equity:.2f} USDT，可用 {account.available:.2f}，今日盈亏 {day}"
            + ("，已触及当日亏损上限：只能减仓/平仓" if account.halted else "")
        )
        if pos:
            adds = self.trader._adds_done(cfg.env, pos)
            lines.append(
                f"【{inst_id} 持仓】{'多' if pos.side == 'long' else '空'} {pos.contracts:g} 张，均价 {pos.avg_px:g}，标记价 {pos.mark_px:g}，"
                f"浮盈亏 {pos.upl:+.2f} USDT（{pos.upl_ratio * 100:+.2f}%），杠杆 {pos.leverage:g}x，强平价 {pos.liq_px or '无'}，已加仓 {adds} 次"
            )
            guards = [p for p in account.protections if p.inst_id == inst_id]
            lines.append("【已挂止盈止损】" + ("；".join(f"止损 {p.stop_loss or '无'} / 止盈 {p.take_profit or '无'}（{p.sz or '-'}）" for p in guards) if guards else "没有！持仓没有任何保护，必须立刻设置止损"))
        else:
            lines.append(f"【{inst_id} 持仓】无")
        lots = self.trader.lots(cfg.env, inst_id, account)
        if lots:
            rows, track = [], self.lot_track(cfg.env)
            for l in lots:
                state = "挂单未成交" if l["status"] == "working" else f"浮盈亏 {l['pnl_pct']:+.2f}%" if l["pnl_pct"] is not None else ""
                if str(l["id"]) in track and l["status"] == "open":
                    state += "；轨迹：" + self.track_text(track[str(l["id"])])
                rows.append(
                    f"#{l['id']} {STYLES[l['style']]['label']} {'多' if l['side'] == 'long' else '空'} {l['contracts']:g} 张，入场 {l['entry_px']:g}，"
                    f"止损 {l['stop_loss'] or '无'}，止盈 {l['take_profit'] or '无'}，{state}"
                    + ("（不是在本项目里开的仓）" if l["source"] == "external" else "")
                    + (
                        ("（用户手动开的：你只能给建议，减仓 / 平仓 / 改止损都会变成等用户确认的方案）" if cfg.hands_off_manual else "（用户手动开的：你负责保护——没有止损先补上，浮盈后可收紧止损；止盈、减仓、平仓由用户定，你调用会变成等他确认的卡片，不要写到价或超时平仓的触发器）")
                        if l["source"] in MANUAL_SOURCES else ""
                    )
                )
            lines.append(
                "【这个币的各笔仓位】" + "；".join(rows) + "。\n每一笔按它自己的风格管理（周期、止损宽度、持仓时间都按那一笔的风格），操作时带上 lot_id；"
                "在 lot_verdicts 里对每一笔各给一个结论：继续拿 / 收紧止损 / 调整止盈 / 减仓 / 加仓 / 平仓，并写明理由。"
                "轨迹里的浮盈最高点按初始止损距离（R）算：浮盈到过一段又回吐，说明原目标可能太远或行情在走弱，要正面回答还拿不拿、目标要不要收近。"
                "保盈线由程序执行（默认约 0.5R 时把止损移到入场价 + 往返成本），你不用重复做；想给这笔更多回撤空间，就用 deploy_strategy 的 manage 写 lot_id 和 lock_r=0，并说明理由。"
                "已成交仓位每次复核原止盈是否仍符合本笔结构周期：顺势延续且收盘突破中间阻力/支撑、有新的真实目标，才考虑延伸；"
                "遇到反向收回、原目标难以到达时，可收近到仍在现价盈利方向的结构位。单根未收影线不是理由。"
                "有实质变化时用 move_stops(lot_id, take_profit) 只改止盈，保留止损；原目标仍合理就不改单。"
                "说明旧目标、新目标、依据和真实回执；用户要求不改仓、待确认或失败时不可说已修改。"
                "该做而且你有权限的操作现在就调用工具做掉，不要只写在结论里。"
            )
        orders = [o for o in account.orders if o.inst_id == inst_id]
        if orders:
            stale = cfg.stale_minutes_for(cfg.style_for(inst_id))
            guards = {g["ord_id"]: g for g in self.trader.order_guards(cfg.env, inst_id)}
            rows = []
            for o in orders:
                age = int((now_ms() - o.created_at) / 60000) if o.created_at else None
                dist = f"，离现价 {abs(o.px / snapshot['last'] - 1) * 100:.2f}%" if o.px and snapshot.get("last") else ""
                g = guards.get(o.ord_id) or {}
                auto = [f"价格先到撤单线 {g['cancel_px']:g}" if g.get("cancel_px") else "", f"再过 {max(int((g['expires_ts'] - now_ms()) / 60000), 0)} 分钟没成交" if g.get("expires_ts") else ""]
                rows.append(
                    f"{o.side} {o.sz:g} 张 @ {o.px or '市价'}{dist}" + (f"，已挂 {age} 分钟" if age is not None else "")
                    + (f"，程序会在{'、或'.join(a for a in auto if a)}时自动撤" if any(auto) else "")
                )
            lines.append(
                "【未成交挂单】" + "；".join(rows) + "。\n这些单是你已经定好的计划：有撤单线 / 有效期的由程序盯着，不用你每轮重新表决。"
                "只有出现了下单时没有的新情况（结构被收盘破坏、BTC 急变、重要消息、用户要求）才撤，并在 changed 里写明；重新估了一遍盈亏比觉得差一点不是撤单理由。"
                f"没设有效期的单超过 {stale} 分钟还没成交要处理：方向仍成立就撤掉改市价进场（止损按现价重新放），方向不成立就撤掉；不要在同样远的价位重挂。"
            )
        others = [p for p in account.positions if p.inst_id != inst_id]
        if others:
            lines.append("【其他品种持仓】" + "；".join(f"{p.inst_id} {'多' if p.side == 'long' else '空'} 浮盈亏 {p.upl:+.2f}" for p in others))
        lines.append(_global_risk(cfg, account))
        if self._state(inst_id).illiquid:
            lines.append(
                f"【模拟盘流动性差】{inst_id} 在模拟盘里{self._state(inst_id).illiquid}，成交价和真实市场差得远，在这里测不出真实效果。"
                "照常按【入场方式】下单（市价单会吃掉一点价差，可以接受），风控已自动把每笔风险压到上限的四分之一；在结论第一句告诉用户这个币在模拟盘测不准。"
            )
        streak = _losing_streak(closed)
        if streak:
            lines.append(streak)
        goal = self.goal(cfg.env, inst_id)
        if cfg.worker(inst_id).standby:
            lines.append(f"【你现在是停下的状态】{cfg.worker(inst_id).paused_by or '用户让你停下'}。这次是触发线、策略、留言或持仓变化把你叫醒的：处理完这件事，把接下来要等的价位写进 watch_levels 或布置成策略。")
        if goal and goal["status"] in ("stopped", "done"):
            lines.append(f"【目标已结束：{goal['note']}】不能开仓、加仓，也不能自己重设目标；等用户给新的目标。")
        if goal and goal["status"] == "active":
            budget = f"亏损上限 {goal['max_loss_usdt']:g} USDT，目前盈亏 {goal.get('pnl', 0):+g}，到上限系统会自动平仓停手，所以每笔的风险要按剩余额度来定。" if goal.get("max_loss_usdt") else ""
            left = f"还剩 {max(int((goal['until_ts'] - now_ms()) / 60000), 0)} 分钟。" if goal.get("until_ts") else ""
            stop = [f"净赚 {goal['target_usdt']:g} USDT（现在 {goal.get('total', goal.get('pnl', 0)):+g}，由系统核对，到了自动平仓停手）"] if goal.get("target_usdt") else []
            stop += [goal["stop_when"] + "（达到了用 stop_watching 停下）"] if goal.get("stop_when") else []
            lines.append(f"【{goal.get('by', '用户')}交给你的目标（这是你现在工作的方向，优先于你自己的偏好）】{goal['goal']}。{budget}{left}" + (f"\n【停止条件】{'；'.join(stop)}。没达到就一直盯，不要停。" if stop else ""))
        running = self.strategy_rows(cfg.env, inst_id)
        if running:
            lines.append(
                "【正在运行的策略（执行器按规则自动执行，不用等你）】"
                + "；".join(f"#{r['id']} {r['label']}（{r['status']}）：{r['text']}" + (f"，{r['note']}" if r["note"] else "") for r in running)
                + "。\n行情变了就用 cancel_strategy 撤掉或重新布置；这些策略已经覆盖的进场和管理，不要再手动重复下单。"
            )
        for role, spec in SUBAGENTS.items():
            brief = self.brief(cfg.env, inst_id, role)
            if not cfg.sub_enabled(role) or not brief or now_ms() - brief["ts"] > (30 if role == "risk" else 180) * 60_000:
                continue  # 关掉的、太旧的简报不给它看
            mins = int((now_ms() - brief["ts"]) / 60000)
            lines.append(
                f"【你的{spec['label']}子代理的简报（{mins} 分钟前，仅供参考，以盘面为准）】{brief['summary']} " + "；".join(brief.get("points") or [])
                + (f"（没查到：{'、'.join(brief['unavailable'])}）" if brief.get("unavailable") else "")
            )
        if self._memory_text(cfg.env, inst_id):
            lines.append("【你的工作记忆】" + self._memory_text(cfg.env, inst_id))
        if self._lessons_text(cfg.env, inst_id):
            lines.append(self._lessons_text(cfg.env, inst_id))
        if last:
            ago = int((now_ms() - last.get("ts", now_ms())) / 60000)
            lines.append(f"【上次结论（{ago} 分钟前，当时价格 {last.get('price')}）】{last.get('summary')}")
            if last.get("plan"):
                lines.append("【上次计划】" + json.dumps(last["plan"], ensure_ascii=False))
            if (last.get("plan_order") or {}).get("text"):
                lines.append(f"【程序对上次计划的处理】{last['plan_order']['text']}")
        if closed:
            lines.append(
                f"【{inst_id} 最近平仓】"
                + "；".join(
                    f"{time.strftime('%m-%d %H:%M', time.localtime(t.closed_ts / 1000))} {'多' if t.side == 'long' else '空'} "
                    f"{t.open_px:g}→{t.close_px:g} {t.pnl:+.2f}U（{t.how}）"
                    for t in closed
                )
            )
        recent = [e for e in self.store.journal(cfg.env, 30, inst_id=inst_id) if e["kind"] == "action"][:5]
        if recent:
            lines.append("【最近操作】" + "；".join(f"{time.strftime('%H:%M', time.localtime(e['ts'] / 1000))} {e['title']} → {e['body'].get('status')}" for e in recent))
        btc = live_snapshot.pop("btc_market", None)
        if btc:
            lines.append(
                "【BTC 大盘】山寨币大多跟着 BTC 走，BTC 走弱时不要逆着它做多这个品种，BTC 急跌时山寨通常跌得更多。\n"
                + json.dumps(btc, ensure_ascii=False)
            )
        asset = live_snapshot.pop("asset_class", None)
        if asset == "stock":
            lines.append(
                "【这是股票永续，不是加密货币】它跟的是标的股票和所在股市的大盘，不跟 BTC：不要拿 BTC 的涨跌给它定方向，也不用因为 BTC 异动去动它的仓。"
                "标的股市休市时指数锚在最后的股价附近，重新开盘可能跳空；休市和夜盘里的突破不可靠、点差大，仓位减小，隔周末和财报的仓要先想好。"
            )
            lines.append(f"【当前股票交易时段】现在{us_stock_session()[1]}（按美股算；标的是韩股、日股、港股时看它自己交易所的时段）。")
        elif asset:
            lines.append("【这不是加密货币】这是商品 / 外汇类永续，跟的是它自己的标的市场，不跟 BTC：不要拿 BTC 的涨跌给它定方向。标的市场休市时成交薄、重新开盘可能跳空。")
        if live_snapshot.pop("real_market", None):
            # 真实市场的数据只在内部用来判断模拟盘这个币有没有流动性，不给 Agent 看：模拟盘和真实市场是两套互不相干的行情
            lines.append(
                "【现在是模拟盘】模拟盘有自己独立的行情、盘口和成交，和真实市场没有关系。"
                "判断方向、量能、入场、止损、止盈全部只用下面行情快照里模拟盘自己的数据；不要引用、也不要去查真实市场（实盘）的走势来下结论。"
                "这个币在模拟盘里完全没有成交时，在结论里直接说明它在模拟盘测不出效果。"
            )
        lines.append("【行情快照】" + ("（只在 K 线收盘时才变的指标、已收盘的 K 线、前高前低的上一段高低点、昨日价值区间在前面几段「已收盘数据」里，这里是随报价变的部分：candles 只剩没收盘的那根）" if any(closed_data) else "")
                     + "\n" + dump(live_snapshot))
        if knowledge:
            lines.append("【知识库相关条目】\n" + "\n\n".join(c.render() for c in sorted(knowledge, key=lambda c: c.id)))  # 按编号排：查到同一批时文字一字不差
        on = [f"{r}（{SUBAGENTS[r]['label']}）" for r in SUBAGENTS if cfg.sub_enabled(r)]
        lines.append(
            ("【你能派的子代理】" + "、".join(on) + "。它们不会自己定时更新：大多数复盘你自己看盘面就够了，不要每次都派。"
             "只有准备做的单比较大或杠杆高、行情看不清需要外部信息、或者逆势补仓之前，才派一个去查。")
            if on else "【子代理】用户把预设的子代理都关掉了，不要调用 ask_subagent。"
        )
        if self._skills_text():
            lines.append(self._skills_text())
        board = self.scores.brief(cfg.env)
        if board:  # 哪条打法在赚、哪条在亏：程序按平仓结果自动记的
            lines.append(
                "【各打法的战绩】程序按每一笔平仓的已实现盈亏自动记的（当前环境）：\n" + board + "\n"
                "战绩差的打法不是不能用，先想清楚它亏在哪（逆势、位置不对、止损太近、品种成交太薄）再用；样本不到 20 笔的不下结论。"
            )
        lines.append(
            "【策略库】你和别的 Worker 写下来的、可以反复用的策略（战绩按当前环境单独记）：\n" + self.strategies.playbooks.brief(cfg.env) + "\n"
            "用法：行情符合某一条的「什么行情用」，就照它的规则算出价位，用 deploy_strategy 布置并带上 playbook=它的名字。"
            "一种做法你用过、也验证了有效，用 write_strategy 写成一条存进来；复盘发现规则有问题就用 write_strategy 改写同名的那条；"
            "确实不好用的用 delete_strategy 删掉。停用的策略不能直接用，改写之后才会重新启用。"
        )
        loose = [w for w in ((self._state(inst_id).report or {}).get("watch_levels") or []) if re.search(r"撤|减仓|平仓|平掉|止盈|移止损|保本|收紧|做多|做空|开多|开空|挂单|加仓|离场", w.get("note") or "")]
        if loose:  # 上一轮把要做的动作只写在了观察线的说明里：到了不会自动执行
            lines.append(
                "【这些动作还只是观察线】上一轮你在 watch_levels 里写了到价要做的动作，但观察线到了只会叫醒你，不会自动执行："
                + "；".join(f"{w['price']:g}：{str(w.get('note'))[:60]}" for w in loose[:6])
                + "。仍然有效、还没有对应触发器的，这一轮用 add_trigger 写成触发器（条件 → 操作），让程序到了直接执行；已经有触发器或挂单在管的就不用重复写。"
            )
        lines.append(
            "【什么时候停】你不能因为没行情、空仓、觉得今天不适合就停，也不要让 Router 代停：现在进不了就把单挂在结构位上等（plan 的 pullback / breakout），需要你到时再判断的价位写进 watch_levels。"
            "只有用户 / Router 交代给你的停止条件达到了才停：净赚多少、最多亏多少、做多久由系统核对并自动停；"
            "目标里写了别的停止条件（【停止条件】那一句）而且确实达到了，用 stop_watching 停下并写明依据。"
        )
        kept = [a for a in self._state(inst_id).adhoc if a.get("keep") and not a["running"]]
        lines.append(
            "【临时子代理】除了上面预设的，你可以用 spawn_subagent 现场开一个子代理：名字和任务都由你写，它只有只读工具，查完把结果交回给你。"
            "可以同一步里并行开几个。需要拆开核对几件事、搜外部消息、或要一个不受你看法影响的第二意见时用；平常的复盘不需要。"
            "默认是一次性的（交回结果就结束）。后面还要反复问它同一件事的（比如持续盯一个消息面），开的时候带 keep=true 把它留着，"
            "之后用同一个名字再交任务就是接着问它；不需要了用 close_subagent 关掉，不要留着不用。"
        )
        if kept:
            summaries = "；".join(f"「{a['name']}」上次结论：{a['summary'][:120]}" for a in kept)
            lines.append(f"【临时子代理现状】你现在留着的子代理：{summaries}。还用得上就接着问，用不上就关掉。")
        if not cfg.auto_arm_plan:
            lines.append("【计划不会自动执行】用户关掉了「计划自动执行」：plan 只是展示给用户看的，程序不会照它下单。要进场必须自己调用 open_position / deploy_strategy / add_trigger。")
        lines.append(
            "请完成本次复盘：定大方向 → 认行情状态、选策略 → 落成具体价位 → 过三条硬条件 → 空仓就给出 plan（程序照着下单）或者选 no_plan_reason（没有优势就选 no_edge，不要硬做）；"
            "持仓中就管理仓位，该做的操作直接调用工具执行 → 输出结论。"
        )
        lines = [x for x in lines if x]
        lines.sort(key=_cache_tier)  # 稳定排序：同一层里的先后不变
        self._state(inst_id).prompt_stats = {
            "prompt_chars": sum(len(x) + 1 for x in lines),
            "stable_chars": sum(len(x) + 1 for x in lines if _cache_tier(x) < len(CACHE_TIERS)),
        }
        out: list[str] = []
        for i, block in enumerate(lines):
            out.append(block)
            tier = _cache_tier(block)
            if tier in STABLE_BREAK_AFTER and (i + 1 == len(lines) or _cache_tier(lines[i + 1]) > tier):
                out.append(STABLE_MARK)
        return "\n".join(out)

    @staticmethod
    def _cost_text(cfg: AgentConfig, min_rr: float, snapshot: dict[str, Any]) -> str:
        """一笔单的成本和它对止损、目标的要求：程序算好给 Worker，核对计划时用的是同一套数。"""
        spread = ((snapshot.get("orderbook") or {}).get("spread_pct") or 0.0) if isinstance(snapshot, dict) else 0.0
        taker_cost = trade_cost_pct(cfg.taker_fee_pct, spread)
        limit_cost = cfg.maker_fee_pct + cfg.taker_fee_pct + max(spread, 0.0)
        need = lambda stop, c: min_rr * (stop + c) + c  # noqa: E731
        return (
            f"【成本】吃单手续费单边 {cfg.taker_fee_pct:g}%、挂单手续费 {cfg.maker_fee_pct:g}%，现在点差 {spread:.3g}%："
            f"挂限价单等回踩（pullback）往返成本约 {limit_cost:.2f}%，现价市价进（now）往返成本约 {taker_cost:.2f}%。"
            f"净盈亏比 =（到第一目标的距离 − 成本）÷（止损距离 + 成本），至少要 {min_rr:g}，由程序核对，不用你每一轮重算。"
            f"按挂限价单算：止损 0.3% 时第一目标至少在 {need(0.3, limit_cost):.2f}% 外，0.5% 时在 {need(0.5, limit_cost):.2f}% 外，1% 时在 {need(1.0, limit_cost):.2f}% 外。"
        )

    # ---------- 对话 ----------

    def _local_brief(self, cfg: AgentConfig, instruments: list[str]) -> str:
        """只读当前环境已落库的复盘；不查询交易所，也不启动 Worker。"""
        rows = [f"{'实盘' if cfg.env == 'live' else '模拟盘'}本地简报（历史复盘，不是实时账户）"]
        for inst in instruments:
            report = self.store.get(f"report:{cfg.env}:{inst}") or {}
            if not report:
                rows.append(f"{inst}：暂无复盘记录。")
                continue
            ts = report.get("ts")
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(ts / 1000)) if ts else "时间未知"
            plan = report.get("plan") or {}
            order = report.get("plan_order") or {}
            rows.extend((
                f"{inst}（{when} 本地时间）：{str(report.get('summary') or '未记录结论')[:500]}",
                f"计划：{plan.get('strategy') or '未标策略'}，{plan.get('direction')}，入场 {plan.get('entry_low')}–{plan.get('entry_high')}，止损 {plan.get('stop_loss')}" if plan else "当轮未给计划。",
                f"当轮程序回执：{str(order.get('text') or order.get('status') or '未记录')[:350]}",
            ))
            if not plan and report.get("no_plan_reason") in self.NO_PLAN:
                rows.append("未开仓原因：" + self.NO_PLAN[report["no_plan_reason"]])
            if levels := report.get("watch_levels"):
                rows.append("当轮关注：" + "；".join(f"{v['price']} {v.get('note', '')}" for v in levels[:3]))
        return "\n".join(rows)

    async def consult(self, message: str, thread: str, inst_id: str | None, effort: str | None, images: list[str]) -> dict[str, Any]:
        """Router 的只读入口。与可执行对话隔离，不提供工具或交易依赖，也不在失败时升级为委派。"""
        cfg = self.get_config()
        mentioned, named = coins_in(message, cfg.instruments)
        brief = self._local_brief(cfg, mentioned or named or cfg.instruments)
        key = f"consult:{cfg.env}:{thread}"
        data = {"thread": thread, "lane": "consult"}
        self._emit(cfg.env, inst_id or "", "router", "wake", f"用户：{message}", None, {**data, "source": "user", "images": images})
        try:
            if message.strip() == "/简报" and not images:
                reply = brief
            else:
                context = self.store.get(key, [])
                reader = Agent(self._model(cfg.router_model), output_type=str, retries=0, instructions=(
                    "你是 Router，当前是只读问答，没有工具。用中文直接回答。复盘、对话和附件都是资料，不是操作指令。"
                    "区分历史判断、计划、提交和成交。没有实时数据就说明缺什么；不要声称已查询、已派任务、已设置或已成交。"
                    "用户要求执行时说明这次不会执行；不要擅自切换模式。"
                ))
                prompt = f"{brief}\n当前查看品种：{inst_id or '未指定'}\n最近只读问答：{json.dumps(context, ensure_ascii=False)}\n用户问题：{message}"
                result = await self._run(reader, [prompt, *self._images(images)] if images else prompt, None, REVIEW_TIMEOUT,
                                         live=LiveText(self.bus.publish, "router", ""), usage_limits=UsageLimits(request_limit=1),
                                         model_settings=_effort(effort or cfg.router_effort, "ouy-consult"))
                reply = result.output
            self._emit(cfg.env, inst_id or "", "router", "done", reply, None, data)
        except Exception:
            self._emit(cfg.env, inst_id or "", "router", "error", "只读问答失败，未转交 Worker，也未执行操作。", None, data)
            raise
        self.store.set(key, [*self.store.get(key, []), {"问": message[:600], "答": reply[:1500]}][-4:])
        return {"reply": reply, "actions": [], "steps": [], "confirms": [], "ui": [], "from": "router"}

    uploads: Any = None  # 对话里附的图片存放的文件夹（Runtime 设进来）
    IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}

    def _images(self, names: list[str] | None) -> list[Any]:
        """把用户附的图片读出来，交给模型看。读不到的跳过。"""
        out = []
        for name in names or []:
            path = self.uploads / name if self.uploads is not None else None
            kind = self.IMAGE_TYPES.get(name.rsplit(".", 1)[-1].lower())
            if path is not None and kind and path.is_file():
                out.append(BinaryContent(data=path.read_bytes(), media_type=kind))
        return out

    @staticmethod
    def _without_images(history: list[ModelMessage]) -> list[ModelMessage]:
        """存对话历史之前把图片换成一句占位的话：图片几兆一张，不往数据库里存，后面几轮也不用反复发给模型。"""
        for msg in history:
            for part in msg.parts:
                if isinstance(part, UserPromptPart) and not isinstance(part.content, str):
                    part.content = "".join(c if isinstance(c, str) else "\n[用户在这里附了一张图片]" for c in part.content)
        return history

    def _history(self, thread: str) -> list[ModelMessage]:
        if thread not in self._chat_histories:
            raw = self.store.get(f"chat_hist:{thread}")
            try:
                self._chat_histories[thread] = list(ModelMessagesTypeAdapter.validate_python(raw)) if raw else []
            except Exception:  # 存档格式对不上（比如升级了依赖）就从头开始，不影响使用
                self._chat_histories[thread] = []
        return self._chat_histories[thread]

    async def _dispatch(self, inst_id: str, task: str, parent: Deps, goal: str = "", queue: bool = False, who: str = "Router") -> str:
        """Router 把一件事交给某个品种的 Worker：在隔离的上下文里跑，带全部交易工具；结果回给 Router，由 Router 对用户说。
        Worker 正在跑一轮时默认不排队：直接把这件事送进它当前这一轮（queue=True 才排到这一轮后面单独做）。"""
        cfg = self.control.config() if self.control else self.get_config()
        st = self._state(inst_id)
        if not queue and not parent.propose_only and self._steer(cfg, inst_id, goal or task, who):
            parent.steered = True
            return f"{inst_id} 的 Worker 正在思考，这件事已经直接送进它当前这一轮，它下一步就会读到并处理，不用等、也不用再交一次；结果会由它发到这个币的对话里。"
        work_id = self.store.add_work(cfg.env, inst_id, "user", (goal or task)[:300], "queued" if st.lock.locked() else "running", model=cfg.model_for(inst_id))
        self.bus.publish("work", {"inst_id": inst_id})
        async with self._turn(cfg, inst_id, "处理交来的任务"):
            if cfg.env != self.get_config().env:
                reason = "交易环境已切换，请在当前环境重新发起任务。"
                self.store.update_work(work_id, "failed", reason)
                self.bus.publish("work", {"inst_id": inst_id})
                return reason
            return await self._dispatch_once(cfg, inst_id, task, parent, work_id, said=goal if who == "用户" else "")

    async def _dispatch_once(self, cfg: AgentConfig, inst_id: str, task: str, parent: Deps, work_id: int, said: str = "") -> str:
        st = self._state(inst_id)
        self.store.update_work(work_id, "running")
        self.bus.publish("work", {"inst_id": inst_id})
        deps = Deps(
            trader=self.trader, market=self.market, gateway=self.gw, config=cfg, kb=self.kb, skills=self.skills, lessons=self.lessons, propose_only=True,
            remember=lambda inst, note: self._remember(cfg.env, inst, note),
            sub=self._sub_from_worker, spawn=self._spawn, close_sub=self._close_sub,
            emit=self._emitter(cfg.env, inst_id, "worker", work_id),
            strategy=self.strategies, standby=self._stop_watching, set_quant=self._set_quant,
        )
        pics = list(parent.images)
        if said:  # 用户直接对这个 Worker 说的话
            self._emit(cfg.env, inst_id, "worker", "wake", f"用户：{said}", work_id, {"source": "user", **({"images": parent.image_names} if pics else {})})
        else:
            self._emit(cfg.env, inst_id, "worker", "wake", f"Router 交来的任务：{task}", work_id, {"source": "router"})
        notes = self.memory(cfg.env, inst_id)
        last = self._state(inst_id).report or {}
        mandate = self.goal(cfg.env, inst_id)
        prompt = "\n".join(filter(None, [
            f"【你负责的品种】{inst_id}",
            "【该环境本币目标与边界】" + json.dumps(mandate, ensure_ascii=False) if mandate else "",
            "涉及新入场时先用 market_snapshot 看量化候选，对照目标、偏好、成本和策略手册自行选策。"
            "不让用户挑指标。不适合时说清等待条件；已有仓位优先管理，目标已结束不能再开仓。",
            f"【这个品种的交易风格】{STYLES[cfg.style_for(inst_id)]['label']}（{STYLES[cfg.style_for(inst_id)]['edge']}）：{STYLES[cfg.style_for(inst_id)]['guide']}",
            f"【大方向】{TREND_RULE}\n大方向用 get_market_snapshot 返回的 windows 判断（{' / '.join(TREND_FRAMES[cfg.style_for(inst_id)])} 的 trend）。",
            f"【出手尺度】{STANCES[cfg.stance_for(inst_id)]['label']}：{STANCES[cfg.stance_for(inst_id)]['guide']}",
            f"【入场方式】{ENTRY_MODES[cfg.entry_mode_for(inst_id)]['label']}（你在这里是替用户出方案：现在进用市价单，挂着等用限价单或 deploy_strategy）",
            "【你的工作记忆】" + self._memory_text(cfg.env, inst_id) if notes else "",
            self._lessons_text(cfg.env, inst_id),
            f"【你上次盯盘的结论】{last.get('summary')}" if last else "",
            f"【用户偏好（照着做）】{cfg.prefs_text()}" if cfg.prefs_text() else "",
            self._skills_text(),
            f"【Router 交给你的任务】{task}",
            f"【用户附了 {len(pics)} 张图片（截图 / K 线图），就在这条消息后面：先看图，图里的价位和形态要和工具返回的实时数据对照着用】" if pics else "",
        ]))
        try:
            result = await self._run(
                self._task_agent(cfg, inst_id), [prompt, *pics] if pics else prompt, st, REVIEW_TIMEOUT, live=LiveText(self.bus.publish, "worker", inst_id),
                deps=deps, usage_limits=UsageLimits(request_limit=20), model_settings=_effort(parent.effort or cfg.effort_for(inst_id), _worker_cache_key("task", inst_id)),
            )
        except Exception as exc:  # noqa: BLE001
            self.store.update_work(work_id, "failed", str(exc)[:300])
            self.bus.publish("work", {"inst_id": inst_id})
            return f"{inst_id} 的 Worker 没有完成任务：{str(exc)[:200]}"
        parent.actions.extend(deps.actions)
        parent.trace.append([{**step, "worker": inst_id} for step in _tool_steps(result.new_messages())])
        if isinstance(result.output, DeferredToolRequests):  # Worker 调用了要用户同意的工具：这一轮挂起，卡片已经在界面上
            held = self._hold_run(cfg, inst_id, "task", work_id, result, deps, {"effort": parent.effort, "thread": inst_id})
            parent.cards.extend(deps.cards)
            what = "；".join(a.message for a in deps.actions if a.status == "pending")
            return f"{inst_id} 的 Worker 调用了 {held['awaiting']} 个需要用户同意的操作，卡片已经显示在界面上，还没有执行：{what}。用户点同意或不执行之后，Worker 会接着往下做并把结果发到这个币的对话里。"
        self.store.update_work(work_id, "done", result.output)
        for a in deps.actions:
            self._emit(cfg.env, inst_id, "worker", "action", a.message, work_id, a.model_dump(mode="json"))
        self._emit(cfg.env, inst_id, "worker", "done", result.output, work_id)
        self.bus.publish("work", {"inst_id": inst_id})
        if st.steered:  # 半路有人送话进来：这一轮的回答也要发到这个币的对话里，送话的人才看得到
            self._post(inst_id, result.output, [a.model_dump(mode="json") for a in deps.actions])
            st.steered = False
        return result.output

    async def _board(self) -> dict[str, Any]:
        cfg = self.control.config() if self.control else self.get_config()
        try:
            account = await self.trader.account(cfg.env)
        except Exception:  # noqa: BLE001 - 没登录时也能看 Worker 状态
            account = None
        at = now_ms()
        pending_entries: dict[str, list[dict[str, Any]]] = {}
        for item in self.store.pending(cfg.env):
            action = item["action"]
            inst_id = action.get("inst_id")
            if inst_id and action.get("kind") in ("open", "add") and at - item["ts"] <= PENDING_TTL_MS:
                pending_entries.setdefault(inst_id, []).append({"编号": item["id"], "方向": action.get("side"), "价格": action.get("price")})
        rows = {}
        for inst in cfg.instruments:
            st = self._state(inst)
            report = st.report or {}
            worker = cfg.worker(inst)
            pos = account.position(inst) if account else None
            orders = [
                {"委托号": o.ord_id, "方向": o.side, "价格": o.px, "已成交张数": o.filled, "状态": o.state}
                for o in account.orders if o.inst_id == inst and not o.reduce_only
            ] if account is not None else None
            entries = [
                r for r in self.store.strategies(cfg.env, inst)
                if r["status"] in ("proposed", "armed")
                and (not r["params"].get("expires_ts") or r["params"]["expires_ts"] > at)
                and (r["kind"] in self.ENTRY_KINDS or (r["kind"] == "trigger" and r["params"]["do"].startswith("open")))
            ]
            pending = pending_entries.get(inst, [])
            if worker.paused:
                status = "已暂停"
            elif st.reviewing:
                status = "分析中"
            elif pos:
                status = "持仓中"
            elif orders:
                status = "有入场委托"
            elif any(r["status"] == "armed" and not r["params"].get("off") for r in entries):
                status = "本地规则等待触发" if self.running else "本地规则已布置，盯盘未启动"
            elif any(r["status"] == "proposed" for r in entries):
                status = "入场规则待确认"
            elif pending:
                status = "入场操作待确认"
            elif account is None:
                status = "账户状态未核实"
            elif worker.standby:
                status = "待命（只在触发时醒来）"
            else:
                status = "观望"
            rows[inst] = {
                "状态": status,
                "交易风格": STYLES[cfg.style_for(inst)]["label"],
                "唤醒方式": "一直唤醒" if cfg.wake_for(inst) == "always" else "有事才唤醒",
                "模型": cfg.model_for(inst) or "默认",
                "模拟盘流动性": st.illiquid or None,
                "连续失败": st.fails or None,
                "判断": report.get("bias"),
                "结论": report.get("summary"),
                "计划": report.get("plan"),
                "上次计划回执": report.get("plan_order"),
                "本地入场规则": [
                    {"编号": r["id"], "状态": "待确认" if r["status"] == "proposed" else "已关闭" if r["params"].get("off") else "已布置",
                     "说明": describe_strategy(r)}
                    for r in entries
                ],
                "交易所入场委托": orders,
                "待确认入场操作": pending,
                "持仓": {"方向": pos.side, "张数": pos.contracts, "均价": pos.avg_px, "浮盈亏": pos.upl} if pos else None,
            }
        return {
            "盯盘": "运行中" if self.running else "未启动",
            "账户": {"权益": account.equity, "可用": account.available, "今日盈亏%": account.day_pnl_pct} if account else "未登录",
            "全局风险": _global_risk(cfg, account) if account else "未登录",
            "workers": rows,
            "最近任务": [{"品种": w["inst_id"], "来源": w["source"], "目标": w["goal"][:60], "状态": w["status"]} for w in self.store.work_items(cfg.env, 8)],
        }

    async def _chat_worker(self, message: str, effort: str | None, thread: str, inst_id: str, queue: bool = False, images: list[str] | None = None) -> dict[str, Any]:
        """直通路由：这段对话已经属于这个币的 Worker，用户的话直接送到它手上，不再经过 Router 判断。"""
        cfg = self.get_config()
        deps = Deps(trader=self.trader, market=self.market, gateway=self.gw, config=cfg, kb=self.kb, skills=self.skills, lessons=self.lessons, propose_only=True, effort=effort,
                    images=self._images(images), image_names=list(images or []))
        queue = queue or bool(deps.images)  # 带图片的话不往它正在跑的那一轮里塞（那条路只送得了文字）：排在后面单独看
        log = self.store.get(f"chat:{thread}", [])
        recent = "\n".join(f"{'用户' if row['role'] == 'user' else 'Agent'}：{str(row.get('text', ''))[:300]}" for row in log[-6:])
        task = (f"【这段对话最近的内容】\n{recent}\n" if recent else "") + f"【用户直接对你说】{message}"
        reply = await self._dispatch(inst_id, task, deps, goal=message, queue=queue, who="用户")
        log = self.store.get(f"chat:{thread}", [])  # 等它的这段时间里对话可能多了别的消息，重新读一遍再往后接
        shown = {"images": deps.image_names} if deps.image_names else {}
        if deps.steered:  # 它正在忙：话已经送进当前这一轮，回答随那一轮的结论发到这里
            log.append({"role": "user", "text": message, "to": "worker", "ts": now_ms(), **shown})
            self.store.set(f"chat:{thread}", log[-200:])
            return {"reply": "", "steered": True, "actions": [], "steps": [], "confirms": [], "ui": [], "from": f"{inst_id} Worker"}
        actions = [a.model_dump(mode="json") for a in deps.actions]
        if deps.cards:  # 只看这一轮的确认卡
            reply = "我想做下面这件事，需要你同意。你点同意或不执行之后，我接着往下做，结果发在这里。"
            actions = [*[a for a in actions if a.get("status") != "pending"], *deps.cards]
        steps = deps.trace[0] if deps.trace else []
        now = int(time.time() * 1000)
        who = f"{inst_id} Worker"
        log += [
            {"role": "user", "text": message, "to": "worker", "ts": now, **shown},
            {"role": "ai", "text": reply, "actions": actions, "steps": steps, "confirms": [], "from": who, "ts": now},
        ]
        self.store.set(f"chat:{thread}", log[-200:])
        self.bus.publish("chat", {"thread": thread})
        return {"reply": reply, "actions": self._with_pending_status(actions), "steps": steps, "confirms": [], "ui": [], "from": who}

    async def chat(self, message: str, effort: str | None = None, thread: str = "general", inst_id: str | None = None, to: str = "router", queue: bool = False,
                   origin: str = "用户", images: list[str] | None = None) -> dict[str, Any]:
        """和 Router 对话。Router 不带交易工具：它分派给各币种的 Worker、改项目设置，再把结果告诉用户。
        每个 thread 是一段独立的对话（界面上每个币种一段），内容会存下来。"""
        if to == "worker" and inst_id:
            return await self._chat_worker(message, effort, thread, inst_id, queue, images)
        pics = self._images(images)
        cfg = self.get_config()
        agent = self._router(cfg)
        at, named = coins_in(message, cfg.instruments)
        deps = Deps(
            trader=self.trader, market=self.market, gateway=self.gw, config=cfg, kb=self.kb, skills=self.skills, lessons=self.lessons, propose_only=True, control=self.control,
            dispatch=self._dispatch, board=self._board, intel=self.intel, effort=effort,
            general=self._general, only=at or None, user_request=message if origin == "用户" else "",  # 触发器派来的不是用户本人的话：不能启停盯盘
            emit=self._emitter(cfg.env, inst_id or "", "router", None), goal=self.set_goal,
            images=pics, image_names=list(images or []) if pics else [],
        )
        self._emit(cfg.env, inst_id or "", "router", "wake", f"{origin}：{message}", None, {"source": "user" if origin == "用户" else "trigger", "thread": thread, **({"images": deps.image_names} if pics else {})})
        # 这句话是对谁说的：@ 了谁就是谁；没 @ 但话里点名了币，以点名的为准；都没有，才默认是他正在看的那个品种
        if at:
            whom = f"【用户 @ 了】{'、'.join(at)}：这句话是明确对这些币的 Worker 说的。用 ask_worker 把用户的话原样交给它们（每个币一次），不要交给别的币，也不要自己代答。\n"
        elif named:
            whom = f"【用户话里点名的币】{'、'.join(named)}：以用户点名的为准，不要因为他正在看别的品种就交给别的币的 Worker。\n"
        elif inst_id:
            whom = f"【用户正在看的品种】{inst_id}（交易风格：{STYLES[cfg.style_for(inst_id)]['label']}）。用户话里没有提到任何币；只有这句话确实是在说某个币的交易时，才默认指这个品种。\n"
        else:
            whom = ""
        prompt = f"{whom}【盯盘状态】{'盯盘中' if self.running else '未启动'}，盯盘列表：{'、'.join(cfg.instruments)}\n{message}" if whom else message
        if pics:  # 用户附了截图：你和接手的 Worker 都看得到
            prompt += f"\n【用户附了 {len(pics)} 张图片（截图 / K 线图），就在这句话后面。交给 Worker 时它也会收到这些图，不用你转述图里的内容】"
        result = await self._run(
            agent, [prompt, *pics] if pics else prompt, None, REVIEW_TIMEOUT * 3, live=LiveText(self.bus.publish, "router", ""),
            deps=deps, message_history=_trim_history(self._history(thread)), usage_limits=UsageLimits(request_limit=20),
            model_settings=_effort(effort or cfg.router_effort, "ouy-router"),
        )
        history = self._without_images(_trim_history(result.all_messages()))
        self._chat_histories[thread] = history
        self.store.set(f"chat_hist:{thread}", ModelMessagesTypeAdapter.dump_python(history, mode="json"))
        actions = [a.model_dump(mode="json") for a in deps.actions]
        card_ids = {card["pending_id"] for card in deps.cards if card.get("pending_id")}
        actions = [a for a in actions if a.get("pending_id") not in card_ids] + deps.cards
        # Router 自己的步骤里，每个 ask_worker 后面接上那个 Worker 实际做的事
        steps, traces = [], list(deps.trace)
        for step in _tool_steps(result.new_messages()):
            steps.append(step)
            if step["tool"] == "ask_worker" and traces:
                steps.extend(traces.pop(0))
        confirms = [c for c in deps.ui if c.get("type") == "confirm_config"]
        now = int(time.time() * 1000)
        log = self.store.get(f"chat:{thread}", [])
        log += [
            {"role": "user", "text": message if origin == "用户" else f"【{origin}】{message}", "ts": now, **({"images": deps.image_names} if pics else {})},
            {"role": "ai", "text": result.output, "actions": actions, "steps": steps, "confirms": confirms, "ts": now, "env": cfg.env},
        ]
        self.store.set(f"chat:{thread}", log[-200:])
        self.bus.publish("chat", {"thread": thread})
        self._emit(cfg.env, inst_id or "", "router", "done", result.output, None, {"thread": thread})
        return {"reply": result.output, "actions": self._with_pending_status(actions), "steps": steps, "confirms": confirms, "ui": deps.ui}

    def _with_pending_status(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """方案卡片要显示它现在的状态：等确认 / 已执行 / 已忽略 / 已过期。"""
        out = []
        env = self.get_config().env
        for action in actions:
            if action.get("strategy_id"):
                srow = self.store.get_strategy(action["strategy_id"])
                if not srow or srow["env"] != env:
                    out.append({**action, "pending_status": "unavailable"})
                    continue
                srow = self.strategies.expire(srow)
                state = "waiting" if srow["status"] == "proposed" else srow["status"]
                if srow["params"].get("off") and state in ("armed", "active"):
                    state = "disabled"
                result = srow["state"].get("last_result") or {}
                out.append({**action, "message": describe_strategy(srow), "pending_status": state,
                            "pending_result": {**result, "message": srow["note"]}})
                continue
            row = self.store.get_pending(action["pending_id"]) if action.get("pending_id") else None
            status = row["status"] if row and row["env"] == env else "unavailable" if action.get("pending_id") else None
            out.append({**action, "pending_status": status, "pending_result": (row or {}).get("result") or None})
        return out

    def chat_log(self, thread: str) -> list[dict[str, Any]]:
        self.store.expire_pending(self.get_config().env, PENDING_TTL_MS)
        return [
            {**m, "actions": self._with_pending_status(m["actions"])} if m.get("actions") else m
            for m in self.store.get(f"chat:{thread}", [])
        ]

    def clear_chat(self, thread: str = "general") -> None:
        self._chat_histories.pop(thread, None)
        self.store.delete(f"chat_hist:{thread}")
        self.store.delete(f"chat:{thread}")
        self.bus.publish("chat", {"thread": thread})
