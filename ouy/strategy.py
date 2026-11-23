"""策略执行器：快的一层，不调用模型。

Agent（慢的一层）负责判断行情、选策略、定方向和参数；布置好之后，这里每个盯盘节拍（几秒一次）按写死的规则检查，
条件一满足立刻下单和管理仓位，不用再等模型想。每一笔单仍然走 Trader 的硬性风控。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from .models import AccountState, AgentConfig, TradeAction
from .okx.gateway import OkxError
from .profit_guard import ExitRules, quote, signal
from .store import Store, now_ms
from .trader import Rejected, Trader

log = logging.getLogger("ouy.strategy")

KINDS: dict[str, dict[str, str]] = {
    "breakout": {"label": "突破追单", "does": "价格突破触发价就顺着方向市价进场"},
    "pullback": {"label": "回踩进场", "does": "价格回到触发价就市价进场（做多等回落，做空等反弹）"},
    "range": {"label": "区间两头", "does": "到区间下沿做多、到上沿做空，止损放在区间外，可以来回做几轮"},
    "manage": {"label": "移动止损 / 分批止盈", "does": "管理已有的一笔仓位：到 1R 移保本、分批止盈、跟踪止损、超时离场"},
    "profit_guard": {"label": "提前止盈（模拟）", "does": "给指定仓位锁盈：接近目标后停滞或利润明显回吐，尝试只平这一笔"},
    "martingale": {"label": "程序马丁（补仓保本）", "does": "逆向每走一段就按倍数补一层，把均价拉近现价，回到均价上方一点（保本位）整组平掉；空仓时自己开第一层，已有亏损仓位时接手解套；有层数上限和整组止损"},
    "reverse": {"label": "止损后反手", "does": "现在这笔仓被止损之后，价格继续走到触发价就自动开反方向的单；原仓位是止盈走的、或者等太久没到，就作废"},
    "grid": {"label": "网格", "does": "在区间里等距分格，跌一格买一份、涨一格卖一份"},
    "trigger": {"label": "触发器", "does": "预先写好「条件 → 操作」，条件满足时程序直接执行这个操作，不经过模型思考"},
}
# 触发器的条件和操作。条件由程序每个节拍核对；操作是参数已经写死的一次工具调用，照样过权限和硬性风控
TRIGGER_WHEN = {
    "price_above": "价格 ≥ {level:g}", "price_below": "价格 ≤ {level:g}",
    "price_touch": "价格触及盯盘价位 {level:g}",
    "close_above": "{bar} K 线收在 {level:g} 上方", "close_below": "{bar} K 线收在 {level:g} 下方",
    "rise_pct": "比写下这条时上涨 ≥ {level:g}%", "fall_pct": "比写下这条时下跌 ≥ {level:g}%",
    "after_minutes": "过了 {level:g} 分钟", "at_time": "到 {clock}", "every_minutes": "每隔 {level:g} 分钟",
    "profit_above": "这个币的仓位浮盈 ≥ {level:g} U", "loss_above": "这个币的仓位浮亏 ≥ {level:g} U",
    "giveback_pct": "浮盈从最高点回吐 ≥ {level:g}%",
    "has_position": "这个币有了持仓（挂单成交）", "no_position": "这个币的仓位平掉了",
    "data_above": "{data} ≥ {level:g}", "data_below": "{data} ≤ {level:g}",
}
TRIGGER_NO_LEVEL = ("has_position", "no_position")
# 程序持续记录的数据（每 10 秒更新，按最近一根收盘的 5 分钟 K 线算）：触发器的条件可以直接用，数值随时能改
DATA = {
    "shift_failure": "转折位状态",
    "vol_ratio": "量比", "chg_pct": "最近一根 5 分钟涨跌幅%", "score_long": "做多分数", "score_short": "做空分数",
    "trend": "1 小时方向", "range_pos": "在区间里的位置%", "spread_pct": "点差%", "book_bias": "盘口买卖偏向",
    "turn_stage": "转折苗头",
    "sig_long": "快判做多分数", "sig_short": "快判做空分数", "move_1m": "最近一根 1 分钟涨跌幅%", "vol_1m": "1 分钟量比",
}
DATA_HELP = {
    "shift_failure": "1=向上突破后失守，-1=向下突破后收回，0=尚未失守或已恢复；无近期结构或无有效报价时未知",
    "vol_ratio": "最近一根收盘的 5 分钟 K 线的量 ÷ 前 20 根平均", "chg_pct": "最近一根收盘的 5 分钟 K 线从开盘到收盘的涨跌幅（%，跌是负数）",
    "score_long": "做多凑了几分（0~4）：顺 1 小时方向、碰到关键线、收盘确认、放量", "score_short": "做空凑了几分（0~4）",
    "trend": "上涨 1、震荡 0、下跌 -1", "range_pos": "现价在区间里的位置（0 = 下沿，100 = 上沿）",
    "spread_pct": "买一卖一之间的价差占价格的百分比", "book_bias": "盘口前 20 档买盘和卖盘谁多（-100 全是卖盘，100 全是买盘）",
    "turn_stage": "看涨转折走到第几关为正、看跌为负：1 扫流动性，2 加动能背离，3 结构反转确认；0 = 没有",
    "sig_long": "快判现在给做多几分（0 = 没有信号；1 只有形态，带量 +1，顺大方向 +1）", "sig_short": "快判现在给做空几分",
    "move_1m": "最近一根收盘的 1 分钟 K 线从开盘到收盘的涨跌幅（%，每几秒更新）", "vol_1m": "最近一根收盘的 1 分钟 K 线的量 ÷ 前 20 根平均",
}
RECORDER_MAX_CONDS = 4


def label_of(row: dict[str, Any]) -> str:
    return "记录器" if row["kind"] == "trigger" and row["params"].get("recorder") else KINDS[row["kind"]]["label"]


def recorder_params(conds: list[dict[str, Any]]) -> dict[str, Any]:
    """记录器的条件：一项或几项程序记录的数据，全部满足才算到了。"""
    if not 1 <= len(conds) <= RECORDER_MAX_CONDS:
        raise Rejected(f"记录器要看 1~{RECORDER_MAX_CONDS} 项数据。")
    conds = [{"data": str(c["data"]), "op": "below" if c["op"] == "below" else "above", "level": float(c["level"])} for c in conds]
    return {"conds": conds, "when": f"data_{conds[0]['op']}", "data": conds[0]["data"], "level": conds[0]["level"], "recorder": True, "repeat": True}
MANUAL_EXIT = ("{ids} 是用户手动开的仓：什么时候止盈、减仓、平仓由用户定，程序不替他自动了结。"
               "你可以补止损、把止损往盈利方向收紧；觉得该走，就在结论里写明价位和依据建议他，或用 reduce_position / close_position 发一张等他确认的卡片。")
DATA_STALE_MS = 180_000  # 数据超过这么久没更新就不拿来判断
FAST_DATA_MS = 90_000


def data_label(name: str) -> str:
    return f"现价高出「{name[5:]}」的幅度%" if name.startswith("dist:") else DATA.get(name, name)


def _ref(p: dict[str, Any], key: str, line: str, off: str) -> str:
    """一个价位怎么写：固定的数，或者跟着某条线走。"""
    if p.get(line):
        return f"「{p[line]}」" + (f"{p[off]:+g}%" if p.get(off) else "")
    return f"{p[key]:g}" if p.get(key) else ""
TRIGGER_DO = {"open_limit": "open", "open_market": "open", "cancel_orders": "cancel_orders", "close": "close", "reduce": "reduce", "set_stops": "set_stops",
              "wake": "", "task": "", "router_task": ""}
TRIGGER_TALK = ("wake", "task", "router_task")  # 不碰交易的动作：叫醒、Worker 自己做一项任务、交给 Router 派任务
TRIGGER_BARS = {"1m": 1, "5m": 5, "15m": 15, "1H": 60}
ENTRY_TRIGGER_ACTIONS = ("open_limit", "open_market")


def describe_action(side: str, p: dict[str, Any]) -> str:
    do, verb = p["do"], "买" if side == "long" else "卖"
    if do in ("open_limit", "open_market"):
        head = f"挂限价{verb}单 @ {_ref(p, 'price', 'price_line', 'price_offset_pct')}" if do == "open_limit" else f"市价{'做多' if side == 'long' else '做空'}"
        tp = _ref(p, "take_profit", "tp_line", "tp_offset_pct")
        tail = (f"，止盈 {tp}" if tp else "") + (f"，撤单线 {p['cancel_px']:g}" if p.get("cancel_px") else "")
        return f"{head}，止损 {_ref(p, 'stop_loss', 'stop_line', 'stop_offset_pct')}{tail}" + (f"，挂 {p['order_expires_minutes']} 分钟没成交就撤" if p.get("order_expires_minutes") else "")
    which = f"第 #{p['lot_id']} 笔" if p.get("lot_id") else "这个币的仓位"
    if do == "cancel_orders":
        return "撤掉这个币的全部挂单"
    if do == "close":
        return f"市价平掉{which}"
    if do == "reduce":
        return f"{which}减仓 {p['fraction'] * 100:g}%"
    if do == "set_stops":
        return which + "、".join(filter(None, [f"止损移到 {p['stop_loss']:g}" if p.get("stop_loss") else "", f"止盈改到 {p['take_profit']:g}" if p.get("take_profit") else ""]))
    if do == "task":
        return f"Worker 自己做一项任务：{p['task']}"
    if do == "router_task":
        return f"交给 Router 派任务：{p['task']}"
    return f"叫醒 Worker：{p.get('wake_note') or '重新判断'}"


def trigger_conds(p: dict[str, Any]) -> list[tuple[str, float, str]]:
    """触发器的条件：一个，或者两个同时满足。"""
    # 第三项：按 K 线收盘判断的是周期；按程序记录的数据判断的是看哪项数据
    if p.get("conds"):  # 记录器：几项数据同时满足
        return [(f"data_{c['op']}", c["level"], c["data"]) for c in p["conds"]]
    conds = [(p["when"], p.get("level") or 0, (p.get("data") if p["when"].startswith("data") else p.get("bar")) or "")]
    if p.get("and_when"):
        conds.append((p["and_when"], p.get("and_level") or 0, (p.get("and_data") if p["and_when"].startswith("data") else p.get("and_bar")) or ""))
    return conds


def describe_when(p: dict[str, Any]) -> str:
    text = " 且 ".join(TRIGGER_WHEN[w].format(level=level, bar=bar, data=data_label(bar), clock=time.strftime("%H:%M", time.localtime(level / 1000)) if w == "at_time" else "") for w, level, bar in trigger_conds(p))
    return text + (f"（反复生效，每次至少隔 {p.get('cooldown_minutes') or 15:g} 分钟）" if p.get("repeat") else "")


def rule_parts(row: dict[str, Any]) -> tuple[str, str]:
    """把一条策略拆成「等什么 → 做什么」两半，给界面的触发器清单用。"""
    p, kind, text = row["params"], row["kind"], describe(row)
    if kind == "trigger":
        return describe_when(p), describe_action(row["side"], p)
    if kind in ("breakout", "pullback") and row["status"] != "active":
        when, _, do = text.partition(" 时")
        return when, do
    if kind == "profit_guard":
        when, _, do = text.partition("时")
        return when, do.split("，")[0]
    return {"range": "价格到区间两头", "martingale": "价格每逆向走一档", "grid": "价格每走一格"}.get(kind, "这笔仓位浮盈到设定的位置"), text
LIVE = ("armed", "active")
MAX_ERRORS = 3  # 连续出错这么多次就停掉这条策略
RETRY_SECONDS = 30
PROPOSAL_TTL_MS = 10 * 60 * 1000  # 等用户同意的策略过了这么久就作废


def describe(row: dict[str, Any]) -> str:
    """一句话说清这条策略在等什么 / 在做什么，给界面和 Agent 看。"""
    p, kind = row["params"], row["kind"]
    side = "做多" if row["side"] == "long" else "做空" if row["side"] == "short" else ""
    if kind in ("breakout", "pullback"):
        op = ("≥" if row["side"] == "long" else "≤") if kind == "breakout" else ("≤" if row["side"] == "long" else "≥")
        text = f"价格 {op} {p['trigger']:g} 时市价{side}，止损 {p['stop_loss']:g}" + (f"，止盈 {p['take_profit']:g}" if p.get("take_profit") else "")
    elif kind == "range":
        text = f"{p['low']:g} 附近做多（止损 {p['stop_low']:g}）、{p['high']:g} 附近做空（止损 {p['stop_high']:g}），最多 {p.get('max_rounds', 3)} 轮"
    elif kind == "manage":
        text = "管理这笔仓位"
    elif kind == "trigger":
        text = f"当 {describe_when(p)} → {describe_action(row['side'], p)}"
    elif kind == "profit_guard":
        effect = "只提醒 Worker，不下单" if p["on_trigger"] == "notify" else "尝试平这笔"
        text = (f"第 #{p['lot_id']} 笔，走完到目标 {p['target']:g} 的 {p['arm_pct']:g}% 后开始保护；"
                f"{p['stall_minutes']:g} 分钟不再推进或盈利回吐 {p['giveback_pct']:g}% 时{effect}，预留成本 {p['cost_pct']:g}%")
    elif kind == "reverse":
        text = (f"现在的仓位被止损后，价格{'涨到' if row['side'] == 'long' else '跌到'} {p['trigger']:g} 就自动反手{side}，止损 {p['stop_loss']:g}"
                + (f"，止盈 {p['take_profit']:g}" if p.get("take_profit") else "") + f"；平仓后 {p.get('window_minutes', 60):g} 分钟内没到就作废")
    elif kind == "martingale":
        step = f"{p['step_pct']:g}%" if p.get("step_pct") else "一段（程序按到整组止损的距离平均分）"
        text = (f"{side}，每逆向 {step} 按 {p.get('multiplier', 1.6):g} 倍补一层，最多 {p.get('max_layers', 3)} 层，"
                f"回到均价{'上' if row['side'] == 'long' else '下'}方 {p.get('take_profit_pct', 0.15):g}% 整组平掉，整组止损 {p['stop_loss']:g}")
    else:
        text = f"{p['low']:g} ~ {p['high']:g} 分 {p['grids']} 格，每格 {p['margin_per_grid']:g} USDT，止损 {p['stop_loss']:g}"
    rules = []
    m = p.get("manage") or (p if kind == "manage" else {})
    if "lock_r" in m:
        rules.append(f"浮盈到过 {m['lock_r']:g}R 锁住不亏" if m["lock_r"] else "不用保盈线")
    if m.get("breakeven_r"):
        rules.append(f"到 {m['breakeven_r']:g}R 移保本")
    if m.get("review_level"):
        rules.append(f"{m['review_bar']} 收盘越过 {m['review_level']:g} 复核持仓（不是反手单）")
    for part in m.get("partials") or []:
        rules.append(f"到 {part['r']:g}R 平 {int(part['fraction'] * 100)}%")
    if m.get("trail_pct"):
        rules.append(f"之后按 {m['trail_pct']:g}% 跟踪止损")
    if m.get("max_hold_minutes"):
        rules.append(f"{m['max_hold_minutes']} 分钟没走出来就离场")
    return text + ("；" + "，".join(rules) if rules else "")


class StrategyEngine:
    describe = staticmethod(describe)

    def __init__(self, store: Store, trader: Trader, emit: Callable[..., None]):
        from .playbook import Playbooks  # 策略库在执行器上面一层；放这里导入，避免两个模块互相导入

        self.store, self.trader, self.emit = store, trader, emit
        self.on_task: Callable[[str, str, int, str, str, bool], None] | None = None  # 触发器到了要做任务时，由盯盘那一层接手
        self.metrics: dict[str, dict[str, Any]] = {}  # 每个币程序记录的最新数据：{"ts", "values": {名字: 数值}, "levels": {线名: 价位}}，由盯盘那一层不断更新
        self.playbooks = Playbooks(store)
        self.scores: Any = None  # 按打法记战绩（scoreboard.Scoreboard），由盯盘那一层接上
        self._busy = False

    async def score_playbooks(self, env: str) -> None:
        """用策略库里的策略做完的单：查到这一笔的已实现盈亏，记进那条策略在这个环境里的战绩。每条只记一次。"""
        for row in self.store.strategies(env, live=False, limit=60):
            name, state = row["params"].get("playbook"), row["state"]
            if not name or state.get("scored") or row["status"] in ("proposed", "armed") or not state.get("since"):
                continue
            lot = self.store.lot(state["lot_id"]) if state.get("lot_id") else None
            if (lot and lot["status"] == "open") or (not lot and row["status"] in LIVE):
                continue  # 这一笔还没平
            since_ms = int(state["since"] * 1000) - 5_000
            trades = [t for t in await self.trader.history(row["inst_id"], 20, env) if t.closed_ts >= since_ms and t.opened_ts >= since_ms - 60_000]
            if trades:
                pnl = sum(t.pnl for t in trades) if not lot else min(trades, key=lambda t: t.closed_ts).pnl
                book = self.playbooks.record(name, env, pnl)
                self.store.update_strategy(row["id"], state={**state, "scored": True, "pnl": round(pnl, 4)})
                if book:
                    off = "" if book["enabled"] else f" {book['off_reason']}"
                    self.emit(env, row["inst_id"], "strategy", "wake", f"策略「{name}」做完一笔：{pnl:+.2f} USDT。{'模拟盘' if env == 'demo' else '实盘'}战绩：{self.playbooks.score(book, env)}。{off}", None, {"playbook": name, "pnl": pnl})
            elif now_ms() - row["ts"] > 24 * 3_600_000:
                self.store.update_strategy(row["id"], state={**state, "scored": True})  # 太久了还查不到这一笔，不再找

    # ---------- 布置 / 撤销 ----------

    def deploy(self, cfg: AgentConfig, inst_id: str, kind: str, side: str, params: dict[str, Any], *, source: str = "agent", note: str = "", by: str = "", require_approval: bool = False) -> dict[str, Any]:
        if kind not in KINDS:
            raise Rejected(f"没有这种策略：{kind}")
        if inst_id not in cfg.instruments:
            raise Rejected(f"{inst_id} 不在盯盘列表里，不能在它上面布置策略。")
        if kind not in ("manage", "profit_guard") and not (kind == "trigger" and not str(params.get("do", "")).startswith("open")):
            self.trader.check_goal_budget(cfg.env, inst_id)
        params = {k: v for k, v in params.items() if v is not None}
        if source != "user" and kind == "trigger" and params.get("do") in ("reduce", "close", "set_stops"):
            probe = TradeAction(kind=params["do"], inst_id=inst_id, lot_id=params.get("lot_id"), take_profit=params.get("take_profit"))
            if manual := self.trader.manual_exits(cfg.env, probe):
                raise Rejected(MANUAL_EXIT.format(ids="、".join(f"#{l['id']}" for l in manual)))
        if cfg.env == "demo" and cfg.style_for(inst_id) == "scalp" and kind in ("breakout", "pullback"):
            if not params.get("take_profit"):
                raise Rejected("模拟超短线预案需要指定止盈目标。")
            params.update(scalp_plan=True, expires_minutes=min(params.get("expires_minutes", 5), 15))
            if params["expires_minutes"] < 1:
                raise Rejected("模拟超短线预案至少有效 1 分钟。")
        if kind == "profit_guard":
            if cfg.env != "demo":
                raise Rejected("提前止盈试验仅在模拟盘可用。")
            lot = self._lot(cfg.env, inst_id, params.get("lot_id"))
            if not lot:
                raise Rejected("请指定这个币的一笔未平仓位编号 lot_id。")
            target = params.get("take_profit") or lot["take_profit"]
            side = lot["side"]
            if not target or not (target > 0 and (target - lot["entry_px"]) * (1 if side == "long" else -1) > 0):
                raise Rejected("需要在盈利方向有一个明确的止盈目标价。")
            rules = ExitRules.model_validate({k: params[k] for k in ExitRules.model_fields if k in params})
            params = dict(rules.model_dump(), lot_id=lot["id"], target=target, lot_target=lot["take_profit"], entry=lot["entry_px"], long=side == "long")
            if any(s["kind"] == kind and s["params"]["lot_id"] == lot["id"] for s in self.store.strategies(cfg.env, inst_id)):
                raise Rejected("这笔仓位已经有提前止盈规则；先撤销旧规则才能更换。")
        if kind == "martingale":
            if not cfg.risk.allow_averaging_down or cfg.risk.max_adds < 1:
                raise Rejected("程序马丁要在浮亏时补仓，但风控里的「允许亏损加仓」关着（或最多加仓次数是 0）：这是用户才能改的硬开关，请用户先在风控设置里打开。")
            params.setdefault("multiplier", 1.6)
            params["max_layers"] = min(int(params.get("max_layers") or 4), cfg.risk.max_adds + 1)  # 补几次受「最多加仓次数」限制
            params.setdefault("take_profit_pct", round(cfg.taker_fee_pct * 2 + 0.05, 3))  # 保本位：均价再让出往返手续费
        self._validate(kind, side, params)
        if params.get("expires_minutes"):
            params["expires_ts"] = now_ms() + int(params["expires_minutes"]) * 60_000
        params["by"] = by or ("Worker" if source == "agent" else "你")
        permission = cfg.permission_for({"manage": "set_stops", "profit_guard": "close", "trigger": TRIGGER_DO.get(params.get("do", ""), "") or "open"}.get(kind, "open"), inst_id)
        if (kind == "profit_guard" and params["on_trigger"] == "notify") or (kind == "trigger" and params["do"] in TRIGGER_TALK):
            permission = "observe"
        if source == "agent" and permission == "off":
            raise Rejected("用户把这类操作设成了不允许 Agent 做，不能布置自动执行的策略。")
        needs_approval = permission == "confirm" or (require_approval and permission != "observe")
        status = "proposed" if source == "agent" and needs_approval else "active" if kind in ("manage", "profit_guard") else "armed"
        row = self.store.add_strategy(cfg.env, inst_id, kind, side, params, status, note)
        if status != "proposed":
            self._replace_rules(row)
        if not params.get("observation"):
            self.emit(cfg.env, inst_id, "strategy", "wake", f"{'等你同意后启用' if status == 'proposed' else '已布置'}{KINDS[kind]['label']}：{describe(row)}", None, {"strategy_id": row["id"], "status": status})
        return row

    def _replace_rules(self, row: dict[str, Any]) -> None:
        if row["kind"] in ("profit_guard", "trigger"):
            return
        for old in self.store.strategies(row["env"], row["inst_id"]):
            if old["id"] == row["id"]:
                continue
            if row["kind"] == "martingale" and old["kind"] == "manage" and old["status"] in LIVE:
                self.cancel(old["id"], "这笔仓改由程序马丁管理")
            elif old["kind"] == row["kind"] and old["side"] == row["side"] and (
                old["status"] == "armed" or (old["status"] == "proposed" and old["id"] < row["id"])
            ):
                self.cancel(old["id"], "被新启用的同类策略替换")

    def expire(self, row: dict[str, Any]) -> dict[str, Any]:
        if row["status"] not in ("proposed", "armed"):
            return row
        clock = now_ms()
        p = row["params"]
        # An edited proposal gets its own approval window.
        if (p.get("expires_ts") and clock >= p["expires_ts"]) or (
            row["status"] == "proposed" and clock - p.get("effective_ts", row["ts"]) >= PROPOSAL_TTL_MS
        ):
            note = "到时间还没触发，规则已过期" if row["status"] == "armed" else "等待确认超时，规则已过期，应按最新行情重新评估"
            self.store.update_strategy(row["id"], status="expired", note=note)
            return {**row, "status": "expired", "note": note}
        return row

    def edit(self, strategy_id: int, changes: dict[str, Any], why: str = "", side: str | None = None, replace: bool = False, *, env: str | None = None, require_approval: bool = False) -> dict[str, Any]:
        """直接改一条还没触发的触发器 / 入场策略的价位（条件价、挂单价、止损止盈、有效期），不用撤掉重写。改完重新开始等条件。"""
        row = self.store.get_strategy(strategy_id)
        if not row or (env is not None and row["env"] != env):
            raise Rejected("当前交易环境没有这条规则。")
        row = self.expire(row)
        if row["kind"] not in ("trigger", "breakout", "pullback") or row["status"] not in ("proposed", "armed"):
            raise Rejected("只能修改还没触发的触发器或入场策略；已经触发、结束的改不了。")
        changes = {k: v for k, v in changes.items() if v is not None}
        if row["kind"] != "trigger" and "level" in changes:
            changes["trigger"] = changes.pop("level")
        if not changes:
            raise Rejected("没有给出要改的内容。")
        if changes.get("conds"):
            changes.update(recorder_params(changes["conds"]))
        base = {k: row["params"][k] for k in ("by", "recorder", "repeat", "expires_ts") if k in row["params"]} if replace else row["params"]  # replace：条件和操作整套换掉
        params = {**base, **changes}
        params["effective_ts"] = max(now_ms(), int(row["params"].get("effective_ts", row["ts"])) + 1)
        side = row["side"] if side is None else side
        self._validate(row["kind"], side, params)
        if changes.get("expires_minutes"):
            params["expires_ts"] = now_ms() + int(changes["expires_minutes"]) * 60_000
        status = "proposed" if require_approval else row["status"]
        keep = {k: row["state"][k] for k in ("runs", "last_run") if k in row["state"]}
        if require_approval and row["status"] == "armed":
            keep.update(replaces=row["id"], replaces_params=row["params"], replaces_side=row["side"])
            updated = self.store.add_strategy(row["env"], row["inst_id"], row["kind"], side, params, status, why[:300] or row["note"])
            self.store.update_strategy(updated["id"], state=keep)
            return self.store.get_strategy(updated["id"])
        keep.update({k: row["state"][k] for k in ("replaces", "replaces_params", "replaces_side") if k in row["state"]})
        if status == "armed" and not replace:
            keep.update({k: row["state"][k] for k in ("approved", "approved_ts") if k in row["state"]})
        self.store._exec("UPDATE strategies SET params=?, side=?, state=?, status=?, note=?, updated_ts=? WHERE id=?", (json.dumps(params, ensure_ascii=False), side, json.dumps(keep), status, why[:300] or row["note"], now_ms(), strategy_id))
        row = self.store.get_strategy(strategy_id)
        self.emit(row["env"], row["inst_id"], "strategy", "wake", f"{label_of(row)} #{strategy_id} 已修改：{describe(row)}", None, {"strategy_id": strategy_id})
        return row

    def cancel(self, strategy_id: int, why: str = "手动撤销", *, env: str | None = None) -> dict[str, Any] | None:
        row = self.store.get_strategy(strategy_id)
        if not row or (env is not None and row["env"] != env):
            return None
        if row["status"] in ("proposed", *LIVE):
            self.store.update_strategy(strategy_id, status="cancelled", note=why)
            self.emit(row["env"], row["inst_id"], "strategy", "done", f"{label_of(row)}已撤销：{why}", None, {"strategy_id": strategy_id})
        return self.store.get_strategy(strategy_id)

    def approve(self, strategy_id: int, *, env: str | None = None) -> dict[str, Any] | None:
        row = self.store.get_strategy(strategy_id)
        if not row or (env is not None and row["env"] != env):
            return None
        row = self.expire(row)
        if row["status"] == "proposed":
            if old_id := row["state"].get("replaces"):
                old = self.store.get_strategy(old_id)
                if (not old or old["status"] != "armed" or old["params"] != row["state"]["replaces_params"]
                        or old["side"] != row["state"]["replaces_side"] or old["state"].get("last_run") != row["state"].get("last_run")):
                    self.cancel(row["id"], "原规则已改变或触发，请重新核对")
                    raise Rejected("原规则已改变或触发，请重新核对。")
                self.cancel(old_id, f"被确认后的规则 #{row['id']} 替换")
            status = "active" if row["kind"] in ("manage", "profit_guard") else "armed"
            self.store.update_strategy(strategy_id, status=status, state={**row["state"], "approved": True, "approved_ts": now_ms()})
            self._replace_rules(row)
            self.emit(row["env"], row["inst_id"], "strategy", "wake", f"你已同意，{label_of(row)}开始运行：{describe(row)}", None, {"strategy_id": strategy_id})
        else:
            raise Rejected(f"这条规则已处理：{row['note']}" if row["status"] == "expired" else "这条规则已经处理过了。")
        return self.store.get_strategy(strategy_id)

    @staticmethod
    def _validate(kind: str, side: str, p: dict[str, Any]) -> None:
        def need(*names: str) -> None:
            missing = [n for n in names if not p.get(n)]
            if missing:
                raise Rejected(f"{KINDS[kind]['label']}缺少参数：{'、'.join(missing)}")

        if kind == "trigger":
            if p.get("observation") and (p.get("do") != "wake" or p.get("when") not in ("price_touch", "close_above", "close_below")):
                raise Rejected("观察线支持触价或确认收盘越线时叫醒 Worker。")
            need("when", "do")
            if p["do"] not in TRIGGER_DO:
                raise Rejected(f"触发器的操作只能是 {' / '.join(TRIGGER_DO)}。")
            for when, level, bar in trigger_conds(p):
                if when not in TRIGGER_WHEN:
                    raise Rejected(f"触发器的条件只能是 {' / '.join(TRIGGER_WHEN)}。")
                if when.startswith("data"):
                    if bar not in DATA and not bar.startswith("dist:"):
                        raise Rejected(f"按程序记录的数据判断要指定 data：{' / '.join(DATA)}，或 dist:线名（现价高出这条线百分之几）。")
                    continue
                if when not in TRIGGER_NO_LEVEL and not level > 0:
                    raise Rejected(f"条件「{when}」要给一个大于 0 的数值 level。")
                if when.startswith("close") and bar not in TRIGGER_BARS:
                    raise Rejected(f"按 K 线收盘判断要指定周期 bar：{' / '.join(TRIGGER_BARS)}。")
            if p["do"].startswith("open"):
                need(*(() if p.get("stop_line") else ("stop_loss",)), *(("price",) if p["do"] == "open_limit" and not p.get("price_line") else ()))
                if side not in ("long", "short"):
                    raise Rejected("开仓的触发器要指定方向 long 或 short。")
                ref = p.get("price") if p["do"] == "open_limit" else p.get("level") if p["when"].startswith(("price", "close")) else None
                if ref and p.get("stop_loss") and not p.get("stop_line") and not p.get("price_line") and (side == "long") != (p["stop_loss"] < ref):
                    raise Rejected("止损放错边了：做多的止损要低于入场价，做空的要高于入场价。")
            elif p["do"] == "reduce":
                need("fraction")
            elif p["do"] in ("task", "router_task"):
                need("task")
            if any(w == "every_minutes" for w, _, _ in trigger_conds(p)) and p["do"] not in TRIGGER_TALK:
                raise Rejected("「每隔多久」会反复执行，只能用来叫醒 Worker 或派任务，不能用来反复下单。")
            elif p["do"] == "set_stops" and not (p.get("stop_loss") or p.get("take_profit")):
                raise Rejected("移止盈止损的触发器要给新的 stop_loss 或 take_profit。")
        elif kind in ("breakout", "pullback"):
            need("trigger", "stop_loss")
            if side not in ("long", "short"):
                raise Rejected("要指定方向 long 或 short。")
            if (side == "long") != (p["stop_loss"] < p["trigger"]):
                raise Rejected("止损放错边了：做多的止损要低于触发价，做空的要高于触发价。")
        elif kind == "reverse":
            need("trigger", "stop_loss")
            if side not in ("long", "short"):
                raise Rejected("要指定反手之后的方向 long 或 short（和现在的仓位相反）。")
            if (side == "long") != (p["stop_loss"] < p["trigger"]):
                raise Rejected("反手单的止损放错边了：反手做多的止损要低于触发价，反手做空的要高于触发价。")
            if p.get("take_profit") and (side == "long") != (p["take_profit"] > p["trigger"]):
                raise Rejected("反手单的止盈放错边了：反手做多的止盈要高于触发价，反手做空的要低于触发价。")
        elif kind == "range":
            need("low", "high", "stop_low", "stop_high")
            if not p["stop_low"] < p["low"] < p["high"] < p["stop_high"]:
                raise Rejected("区间参数要满足：下方止损 < 下沿 < 上沿 < 上方止损。")
        elif kind == "martingale":
            need("stop_loss")  # 其余参数不给就由程序按到整组止损的距离、风险上限自动算
            if side not in ("long", "short"):
                raise Rejected("要指定方向 long 或 short。")
            if not 2 <= int(p.get("max_layers", 3)) <= 8:
                raise Rejected("程序马丁的层数要在 2 ~ 8 之间（含第一层）。")
            if not 1 < float(p.get("multiplier", 1.6)) <= 3:
                raise Rejected("补仓倍数要大于 1、不超过 3。")
            if any(k in p and not float(p[k]) > 0 for k in ("step_pct", "take_profit_pct", "base_margin")):
                raise Rejected("补仓间距、保本止盈幅度、第一层保证金要大于 0。")
        elif kind == "grid":
            need("low", "high", "grids", "margin_per_grid", "stop_loss")
            if side not in ("long", "short"):
                raise Rejected("要指定方向 long（跌买涨卖）或 short（涨卖跌买）。")
            if not (p["low"] < p["high"] and 2 <= int(p["grids"]) <= 20):
                raise Rejected("网格要满足：下沿 < 上沿，格数 2 ~ 20。")
            if (side == "long") != (p["stop_loss"] < p["low"]):
                raise Rejected("网格的止损要放在区间外：做多网格在下沿下方，做空网格在上沿上方。")
        elif kind == "manage":
            if not (p.get("breakeven_r") or p.get("partials") or p.get("trail_pct") or p.get("max_hold_minutes") or "lock_r" in p):
                raise Rejected("至少给一条管理规则：lock_r、breakeven_r、partials、trail_pct 或 max_hold_minutes。")

    # ---------- 每个节拍 ----------

    def data(self, inst_id: str, name: str, px: float | None = None) -> float | None:
        """程序记录的一项数据现在是多少；没在记或者太久没更新就是 None。dist:线名 = 现价高出这条线百分之几。"""
        m = self.metrics.get(inst_id)
        if not m or now_ms() - m["ts"] > DATA_STALE_MS:
            return None
        if name == "shift_failure" and not -2000 <= now_ms() - m.get("quote_ts", 0) <= 15_000:
            return None
        if name in ("sig_long", "sig_short", "vol_1m", "move_1m") and not 0 <= now_ms() - m.get("fast_ts", 0) <= FAST_DATA_MS:
            return None
        if name.startswith("dist:"):
            line, now = m["levels"].get(name[5:]), px or m["values"].get("px")
            return (now / line - 1) * 100 if line and now else None
        return m["values"].get(name)

    def line(self, inst_id: str, name: str) -> float | None:
        m = self.metrics.get(inst_id)
        return m["levels"].get(name) if m and now_ms() - m["ts"] <= DATA_STALE_MS else None

    def wants_data(self, env: str, inst_id: str) -> bool:
        """这个币上有没有在等的触发器要用程序记录的数据或者线。"""
        keys = ("price_line", "stop_line", "tp_line")
        return any(r["kind"] == "trigger" and r["status"] in ("proposed", "armed") and (any(w.startswith("data") for w, _, _ in trigger_conds(r["params"])) or any(r["params"].get(k) for k in keys))
                   for r in self.store.strategies(env, inst_id))

    def fresh_metrics(self, env: str, inst_id: str) -> dict[str, Any]:
        m = self.metrics.get(inst_id) or {}
        return m if m.get("source_env") == env and 0 <= now_ms() - m.get("ts", 0) <= DATA_STALE_MS else {}

    def snapshot(self, inst_id: str, env: str) -> str:
        m = self.fresh_metrics(env, inst_id)
        if not m:
            return "没有当前环境的有效记录器采样，不能据此判断走势。"
        why = "分数的依据：" + "；".join(f"{k}：{v}" for k, v in m["why"].items()) + "。" if m.get("why") else ""
        reads = [f"{DATA[k]} {v:g}" for k in m["values"] if k in DATA and (v := self.data(inst_id, k)) is not None]
        return f"采样于 {time.strftime('%H:%M:%S', time.localtime(m['ts'] / 1000))}；程序记录的数据：" + "，".join(reads) + "。" + why + "现在的线：" + "，".join(f"{k} {v:g}" for k, v in m["levels"].items()) + "。"

    def _resolve(self, side: str, p: dict[str, Any], inst_id: str, px: float) -> dict[str, Any]:
        """跟着线走的价位：执行这一刻按线的最新位置换成具体的数，并核对止损没有放错边。"""
        q = dict(p)
        for key, line, off in (("price", "price_line", "price_offset_pct"), ("stop_loss", "stop_line", "stop_offset_pct"), ("take_profit", "tp_line", "tp_offset_pct")):
            if p.get(line):
                lv = self.line(inst_id, p[line])
                if lv is None:
                    raise Rejected(f"「{p[line]}」这条线现在算不出来，没有执行。")
                q[key] = float(f"{lv * (1 + (p.get(off) or 0) / 100):.8g}")
        if p["do"].startswith("open"):
            ref = q.get("price") if p["do"] == "open_limit" else px
            if (side == "long") != (q["stop_loss"] < ref):
                raise Rejected(f"按线现在的位置算，止损 {q['stop_loss']:g} 在入场价 {ref:g} 的错边，没有执行。")
        return q

    async def tick(self, cfg: AgentConfig, account: AccountState, prices: dict[str, float], quotes: dict[str, dict] | None = None) -> None:
        if self._busy:  # 上一个节拍的下单还没回来，这一拍跳过，避免同一个条件触发两次
            return
        self._busy = True
        try:
            for row in self.store.strategies(cfg.env):
                if not self._current(row) or self.trader.get_config().env != cfg.env:
                    continue
                row = self.expire(row)
                px = prices.get(row["inst_id"])
                if row["status"] not in LIVE or not px:
                    continue
                if cfg.worker(row["inst_id"]).paused or time.time() < row["state"].get("retry_at", 0) or row["params"].get("off"):
                    continue
                bound = row["params"].get("lot_id") if row["kind"] == "trigger" else None
                if bound and not self._lot(cfg.env, row["inst_id"], bound) and not any(l["id"] == bound for l in self.trader.lots(cfg.env, row["inst_id"])):
                    self.cancel(row["id"], f"第 #{bound} 笔仓位已经平掉，这条规则跟着作废")  # 不是出错：不报警、不重试
                    continue
                state = dict(row["state"])
                try:
                    if row["kind"] == "profit_guard":
                        status, note = await self._profit_guard(cfg, row, state, (quotes or {}).get(row["inst_id"], {}))
                    elif row["params"].get("scalp_plan") and row["status"] == "armed" and not quote((quotes or {}).get(row["inst_id"], {}), row["side"] == "short", time.time()):
                        if now_ms() < row["params"]["expires_ts"]:
                            continue
                        status, note = "expired", "预案已过期，不再入场"
                    else:
                        status, note = await getattr(self, f"_{row['kind']}")(cfg, account, row, state, px)
                    state.pop("errors", None)
                    state.pop("retry_at", None)
                except (Rejected, OkxError) as exc:
                    # 被风控拒绝或交易所报错：隔一会儿再试，连续几次都不行就停掉这条策略
                    state["errors"] = state.get("errors", 0) + 1
                    state["retry_at"] = time.time() + RETRY_SECONDS
                    status, note = ("failed" if state["errors"] >= MAX_ERRORS else row["status"]), f"这一步没做成：{str(exc)[:260]}"
                if self._current(row) and (status != row["status"] or state != row["state"] or (note and note != row["note"])):
                    self.store.update_strategy(row["id"], status=status, state=state, note=note or None)
                    if status != row["status"] and status not in LIVE:
                        self.emit(cfg.env, row["inst_id"], "strategy", "done", f"{label_of(row)}结束（{status}）：{note}", None, {"strategy_id": row["id"], "status": status, "notify_worker": row["kind"] in ("profit_guard", "trigger") or bool(row["params"].get("scalp_plan"))})
        finally:
            self._busy = False

    def _current(self, row: dict[str, Any]) -> bool:
        current = self.store.get_strategy(row["id"])
        return bool(current and current["status"] == row["status"] and current["params"] == row["params"]
                    and current["side"] == row["side"])

    async def _order(self, cfg: AgentConfig, row: dict[str, Any], action: TradeAction, what: str) -> Any:
        """策略触发的每一笔单：照样过硬性风控；结果记进活动流。"""
        if row["params"].get("by") != "你" and (manual := self.trader.manual_exits(cfg.env, action)):
            raise Rejected(MANUAL_EXIT.format(ids="、".join(f"#{l['id']}" for l in manual)))
        if row["kind"] in ("trigger", "breakout", "pullback"):
            def check_current() -> None:
                current = self.trader.get_config()
                if not self._current(row) or current.env != cfg.env or current.worker(row["inst_id"]).paused:
                    raise Rejected("规则已经修改、撤销或暂停")
                if self.expire(row)["status"] == "expired":
                    raise Rejected("规则已过期")
                permission = current.permission_for(action.kind, action.inst_id)
                if permission == "off" or (permission == "confirm" and row["params"].get("by") != "你" and not row["state"].get("approved")):
                    raise Rejected("当前权限不允许执行这条规则")
            result = await self.trader.execute(action, cfg.env, source="agent", preflight=check_current, entry_only=action.kind == "open")
        else:
            result = await self.trader.execute(action, cfg.env, source="agent")
        self.emit(cfg.env, row["inst_id"], "strategy", "action", f"{what}：{result.message}", None, {**result.model_dump(mode="json"), "strategy_id": row["id"]})
        if result.status != "executed":
            raise Rejected(result.message)
        if action.kind == "open" and self.scores and action.side:  # 这一单是哪条打法开的：平仓后战绩记到它头上
            p = row["params"]
            self.scores.tag(cfg.env, row["inst_id"], action.side, p.get("strategy_name") or p.get("playbook") or KINDS[row["kind"]]["label"], KINDS[row["kind"]]["label"])
        return result

    def _entry(self, account: AccountState, row: dict[str, Any], side: str, stop: float, tp: float | None, px: float, *, margin: float | None = None) -> TradeAction:
        p = row["params"]
        return TradeAction(
            kind="add" if account.position(row["inst_id"]) else "open", inst_id=row["inst_id"], side=side, order_type="market", stop_loss=stop, take_profit=tp,
            leverage=p.get("leverage"), risk_pct=p.get("risk_pct"), margin_usdt=margin if margin is not None else p.get("margin_usdt"), style=p.get("style"),
            reason=f"策略 #{row['id']} {label_of(row)}触发（现价 {px:g}）",
        )

    def _lot(self, env: str, inst_id: str, lot_id: int | None) -> dict[str, Any] | None:
        return next((l for l in self.trader.lots(env, inst_id) if l["id"] == lot_id and l["status"] == "open"), None) if lot_id else None

    async def _profit_guard(self, cfg: AgentConfig, row: dict, state: dict, ticker: dict) -> tuple[str, str]:
        inst, p = row["inst_id"], row["params"]
        if cfg.env != "demo" or self.trader.get_config().env != "demo":
            return "cancelled", "不在模拟盘，不执行"
        if state.get("pending_id"):
            self.store.expire_pending("demo", PROPOSAL_TTL_MS)
            pending = self.store.get_pending(state["pending_id"])
            if pending and pending["status"] in ("waiting", "running"):
                return "active", "等用户确认提前平这一笔"
            result = (pending or {}).get("result") or {}
            return ("done" if (pending or {}).get("status") == "executed" else "cancelled",
                    f"第 #{p['lot_id']} 笔：{result.get('message') or (pending or {}).get('status') or '确认记录不存在'}")
        if state.get("claimed"):
            return "failed", f"第 #{p['lot_id']} 笔此前已触发，结果待核对，不重复提交"
        lot = self._lot("demo", inst, p["lot_id"])
        if not lot:
            return "done", f"第 #{p['lot_id']} 笔已不在，未提交平仓"
        if lot["take_profit"] != p["lot_target"]:
            return "cancelled", f"第 #{p['lot_id']} 笔目标已改，请重新评估规则"
        q = quote(ticker, p["long"], time.time())
        if not q or q[1] <= state.get("quote_ts", 0):
            return row["status"], ""
        why = signal(p, state, *q)
        if not why:
            return "active", ""
        why += f"（入场 {p['entry']:g}，目标 {p['target']:g}，可平价 {q[0]:g}）"
        if p["on_trigger"] == "notify":
            return "done", f"第 #{p['lot_id']} 笔：{why}；仅提醒，未下单"
        state.update(claimed=True, why=why, trigger_ts=q[1], contracts=lot["contracts"])
        self.store.update_strategy(row["id"], status="active", state=state, note=why)
        action = TradeAction(kind="close", inst_id=inst, lot_id=p["lot_id"], profit_guard_id=row["id"], reason=why)
        result = await self.trader.submit(action, source="agent")
        note = f"第 #{p['lot_id']} 笔：{why}；{result.message}"
        if result.status == "pending":
            state["pending_id"] = result.pending_id
            self.emit(cfg.env, inst, "strategy", "action", note, None, {"strategy_id": row["id"], "pending_id": result.pending_id, "notify_worker": True})
            return "active", note
        return ("done" if result.ok else "failed"), note

    # ----- 突破追单 / 回踩进场 -----

    async def _trigger_entry(self, cfg: AgentConfig, account: AccountState, row: dict[str, Any], state: dict[str, Any], px: float, breakout: bool) -> tuple[str, str]:
        p, long = row["params"], row["side"] == "long"
        if row["status"] == "active":
            return await self._manage_lot(cfg, row, state, px, p.get("manage") or {})
        if p.get("expires_ts") and now_ms() > p["expires_ts"]:
            return "expired", "到时间还没触发，作废"
        if invalid := await self.entry_invalidation(cfg, account, row, state, px):
            return "cancelled", f"入场前失效：{invalid}"
        if not self._current(row):
            return row["status"], ""
        if (px <= p["stop_loss"]) if long else (px >= p["stop_loss"]):
            return "cancelled", f"还没进场价格就到了止损位 {p['stop_loss']:g}，结构已破，作废"
        if p.get("miss_px") and ((px >= p["miss_px"]) if long else (px <= p["miss_px"])):
            return "cancelled", f"还没回到入场位，价格就先到了第一目标 {p['miss_px']:g}，这一段已经走完，作废"
        triggers_above = long if breakout else not long
        hit = px >= p["trigger"] if triggers_above else px <= p["trigger"]
        if not hit:
            return "armed", ""
        if breakout and abs(px / p["trigger"] - 1) * 100 > p.get("max_chase_pct", 0.4):
            return "armed", f"价格已经冲过触发价 {abs(px / p['trigger'] - 1) * 100:.2f}%，不追，等回到触发价附近"
        if account.position(row["inst_id"]) or any(o.inst_id == row["inst_id"] and not o.reduce_only for o in account.orders):
            return "cancelled", "已有持仓或入场委托，原入场规则不再执行"
        action = self._entry(account, row, row["side"], p["stop_loss"], p.get("take_profit"), px)
        if p.get("scalp_plan"):
            if action.kind != "open":
                return "cancelled", "已有持仓，入场预案不能代替加仓授权"
            action.scalp_plan_id = row["id"]
            try:
                result = await self._order(cfg, row, action, "模拟超短线预案触发")
            except Rejected as exc:
                return "cancelled", f"未确认入场，不重试：{exc}"
        else:
            result = await self._order(cfg, row, action, "触发进场")
        state.update(lot_id=result.detail.get("lot_id"), ord_id=result.detail.get("ordId"), entry=px, r=abs(px - p["stop_loss"]), since=time.time())
        if p.get("scalp_plan"):
            return ("active" if p.get("manage") else "done"), f"入场单已提交（触发报价 {px:g}），成交以交易所为准"
        return ("active" if p.get("manage") else "done"), f"入场单已提交（触发报价 {px:g}），成交以交易所为准"

    async def entry_invalidation(self, cfg: AgentConfig, account: AccountState, row: dict[str, Any], state: dict[str, Any], px: float) -> str | None:
        rule = row["params"].get("cancel_before_entry")
        if not rule:
            return None
        when, level, bar = rule["when"], rule["level"], rule["bar"]
        if await self._cond(cfg, account, row, state, px, when, level, bar, "cancel"):
            return TRIGGER_WHEN[when].format(level=level, bar=bar)
        return None

    async def _cond(self, cfg: AgentConfig, account: AccountState, row: dict[str, Any], state: dict[str, Any], px: float, when: str, level: float, bar: str, key: str) -> bool | None:
        """触发器的一个条件现在满不满足。全部由程序用手上的数据算，不问模型。"""
        inst, pos = row["inst_id"], account.position(row["inst_id"])
        if when in ("price_above", "price_below"):
            return px >= level if when == "price_above" else px <= level
        if when == "price_touch":
            previous = state.get("previous_px", px)
            state["previous_px"] = px
            return previous < level <= px or previous > level >= px
        if when in ("close_above", "close_below"):
            if time.time() - state.get("checked" + key, 0) >= 15:  # K 线 15 秒查一次就够
                state["checked" + key] = time.time()
                span = TRIGGER_BARS[bar] * 60_000
                clock = now_ms()
                effective = max(row["ts"], row["params"].get("effective_ts", 0), row["state"].get("approved_ts", 0))
                closed = [c for c in await self.trader.market.candles(inst, bar, 3, cfg.env) if c.confirmed and effective < c.ts + span <= clock]
                last = max(closed, key=lambda c: c.ts, default=None)
                state["closed" + key] = bool(last) and (last.close > level if when == "close_above" else last.close < level)
                state["close_px" + key] = last.close if last else None
                state["close_end" + key] = last.ts + span if last else None
            end = state.get("close_end" + key)
            if not end or now_ms() - end > TRIGGER_BARS[bar] * 60_000 + 15_000:
                return None
            return bool(state.get("closed" + key))
        if when in ("data_above", "data_below"):
            value = self.data(inst, bar, px)
            return None if value is None else (value >= level if when == "data_above" else value <= level)
        if when in ("rise_pct", "fall_pct"):
            move = (px / state.setdefault("base_px", px) - 1) * 100
            return move >= level if when == "rise_pct" else move <= -level
        if when == "after_minutes":
            return now_ms() >= row["ts"] + level * 60_000
        if when == "at_time":
            return now_ms() >= level
        if when == "every_minutes":
            return now_ms() >= state.setdefault("next" + key, row["ts"] + level * 60_000)
        if when in ("has_position", "no_position"):
            return bool(pos) == (when == "has_position")
        if not pos:
            state.pop("peak_upl", None)
            return False
        if when == "giveback_pct":
            peak = state["peak_upl"] = max(state.get("peak_upl", 0), pos.upl)
            return peak > 0 and pos.upl <= peak * (1 - level / 100)
        return pos.upl >= level if when == "profit_above" else pos.upl <= -level

    async def _trigger(self, cfg: AgentConfig, account: AccountState, row: dict[str, Any], state: dict[str, Any], px: float) -> tuple[str, str]:
        """触发器：条件先不满足、后满足时，直接执行预先写死的那个操作。只执行一次，执行不成也不重试，结果回报给 Worker。"""
        p, inst = row["params"], row["inst_id"]
        if p.get("expires_ts") and now_ms() > p["expires_ts"]:
            return "expired", "到时间条件还没满足，作废"
        hit = True
        missing = False
        evidence = []
        for i, (when, level, bar) in enumerate(trigger_conds(p)):  # 每个条件都算一遍：回吐、涨跌幅要一直记着最高点和起点
            result = await self._cond(cfg, account, row, state, px, when, level, bar, str(i))
            if result and when in ("data_above", "data_below"):
                measured = self.data(inst, bar, px)
                if measured is not None:
                    evidence.append(f"{DATA.get(bar, bar)}实测 {measured:g}")
            if result and when in ("close_above", "close_below") and state.get(f"close_end{i}"):
                end = time.strftime("%H:%M:%S", time.localtime(state[f"close_end{i}"] / 1000))
                evidence.append(f"{bar} 于 {end} 收盘 {state[f'close_px{i}']:g}")
            missing |= result is None
            hit = hit and result is True
        if not self._current(row):
            return row["status"], ""
        if missing:
            return "armed", "指标缺失或过期，等待有效采样"  # 缺数据不等于观察到了“不满足”，不能重置触发边沿
        # 收盘条件已按部署时间过滤；一次性收盘提醒可以响应部署后的第一根有效收盘。
        event_condition = p["when"] == "price_touch" or (not p.get("repeat") and p["when"] in ("close_above", "close_below"))
        if not state.get("ready") and not event_condition:
            if hit:
                return "armed", "条件现在就是满足的，等它先不满足、再次满足时才执行"
            state["ready"] = True
        if not hit:
            return "armed", "在等条件满足"
        repeat, cool = bool(p.get("repeat")), (p.get("cooldown_minutes") or 15) * 60_000
        if repeat and now_ms() < state.get("cool_until", 0):
            return "armed", f"条件满足，但离上次执行还不到 {p.get('cooldown_minutes') or 15:g} 分钟"
        cond = f"{describe_when(p)}【触发评估 {time.strftime('%H:%M:%S')}，{inst} 盘中报价 {px:g}" + ("；" + "，".join(evidence) if evidence else "") + "】"
        if p["do"] in TRIGGER_TALK:
            what = describe_action(row["side"], p)
            if p["do"] != "wake" and self.on_task:  # 任务：直接送到 Worker 的任务里，或者交给 Router 去派
                self.on_task(cfg.env, inst, row["id"], cond, p["task"], p["do"] == "router_task")
            again = [(i, level) for i, (when, level, _) in enumerate(trigger_conds(p)) if when == "every_minutes"]
            if not again and not repeat:
                state["runs"], state["last_run"] = state.get("runs", 0) + 1, now_ms()
                return "done", f"{cond} 已满足，{what}"
            for i, level in again:  # 反复执行的：排下一次
                state[f"next{i}"] = now_ms() + level * 60_000
            if repeat:  # 反复生效的：等条件先不满足、再满足，并且隔够时间
                state["ready"], state["cool_until"] = False, now_ms() + cool
            state["runs"], state["last_run"] = state.get("runs", 0) + 1, now_ms()
            if p["do"] == "wake":
                self.emit(cfg.env, inst, "strategy", "action", f"{cond}（现价 {px:g}），{what}。{self.snapshot(inst, cfg.env) if repeat and not p.get('observation') else ''}", None,
                          {"strategy_id": row["id"], "notify_worker": True, "recorder": bool(p.get("recorder"))})
            if p.get("max_runs") and state["runs"] >= p["max_runs"]:
                return "done", f"已执行满 {state['runs']} 次"
            return "armed", f"已执行 {state['runs']} 次，最近一次 {time.strftime('%H:%M:%S')}"
        if p["do"] in ENTRY_TRIGGER_ACTIONS and (account.position(inst) or any(o.inst_id == inst and not o.reduce_only for o in account.orders)):
            if not repeat:
                return "cancelled", f"{cond} 已满足，已有持仓或入场委托，原开仓规则作废"
            state["ready"] = False
            return "armed", "条件满足，但这个币已经有持仓或挂单，这次没有执行"
        try:
            p = self._resolve(row["side"], p, inst, px)
        except Rejected as exc:
            if not repeat:
                return "failed", f"{cond} 已满足，但没执行成（不重试）：{exc}"
            state["ready"], state["cool_until"] = False, now_ms() + cool
            self.emit(cfg.env, inst, "strategy", "action", f"{cond} 已满足，但没执行成：{exc}", None, {"strategy_id": row["id"], "notify_worker": True})
            return "armed", f"这次没执行成：{exc}"
        reason = f"触发器 #{row['id']}：{cond}（现价 {px:g}）"
        if p["do"].startswith("open"):
            action = TradeAction(
                kind="open", inst_id=inst, side=row["side"], order_type="limit" if p["do"] == "open_limit" else "market",
                price=p.get("price") if p["do"] == "open_limit" else None, stop_loss=p["stop_loss"], take_profit=p.get("take_profit"), leverage=p.get("leverage"),
                risk_pct=p.get("risk_pct"), margin_usdt=p.get("margin_usdt"), style=p.get("style"), reason=reason,
                cancel_px=p.get("cancel_px") if p["do"] == "open_limit" else None, expires_minutes=p.get("order_expires_minutes") if p["do"] == "open_limit" else None,
            )
        else:
            action = TradeAction(kind=p["do"], inst_id=inst, lot_id=p.get("lot_id"), fraction=p.get("fraction"), stop_loss=p.get("stop_loss"), take_profit=p.get("take_profit"), reason=reason)
        if cfg.permission_for(action.kind, inst) == "off":
            return "cancelled", f"{cond} 已满足，但你把这类操作关掉了，没有执行"
        try:
            result = await self._order(cfg, row, action, "触发器执行")
            state["last_result"] = result.model_dump(mode="json")
            verb = "订单已提交，成交以交易所为准" if action.kind in ("open", "add") else "已执行"
            ok, text = True, f"{cond} 已满足，{verb}：{result.message}"
        except (Rejected, OkxError) as exc:
            ok, text = False, f"{cond} 已满足，但没执行成（不重试）：{str(exc)[:220]}"
        if not repeat:
            return ("done" if ok else "failed"), text
        # 反复生效的：结果马上告诉 Worker，然后接着等下一次；开仓类默认最多 3 次
        state["ready"], state["cool_until"] = False, now_ms() + cool
        state["runs"], state["last_run"] = state.get("runs", 0) + 1, now_ms()
        self.emit(cfg.env, inst, "strategy", "action", f"{text}。{self.snapshot(inst, cfg.env)}", None, {"strategy_id": row["id"], "notify_worker": True, "recorder": bool(p.get("recorder"))})
        if state["runs"] >= (p.get("max_runs") or (3 if p["do"].startswith("open") else 10_000)):
            return "done", f"已执行满 {state['runs']} 次。最后一次：{text}"
        return "armed", f"已执行 {state['runs']} 次，最近一次 {time.strftime('%H:%M:%S')}：{text}"

    async def _breakout(self, cfg: AgentConfig, account: AccountState, row: dict[str, Any], state: dict[str, Any], px: float) -> tuple[str, str]:
        return await self._trigger_entry(cfg, account, row, state, px, True)

    async def _pullback(self, cfg: AgentConfig, account: AccountState, row: dict[str, Any], state: dict[str, Any], px: float) -> tuple[str, str]:
        return await self._trigger_entry(cfg, account, row, state, px, False)

    # ----- 止损后反手 -----

    async def _reverse(self, cfg: AgentConfig, account: AccountState, row: dict[str, Any], state: dict[str, Any], px: float) -> tuple[str, str]:
        """row["side"] 是反手之后的方向。先等原来的仓位（反方向）出现并被平掉；是亏着出去的（止损），之后价格又走到触发价，才开反手单。"""
        p, long = row["params"], row["side"] == "long"
        sign = 1 if long else -1
        pos = account.position(row["inst_id"])
        if not state.get("flat_at"):
            if pos and pos.side != row["side"]:
                if state.get("flat_seen") or (state.get("opened_at") and pos.opened_at != state["opened_at"]):
                    return "cancelled", "原仓已经换成新仓，旧反手作废"
                exit_side = "buy" if long else "sell"
                state.update(seen=True, opened_at=pos.opened_at, size=pos.contracts, stop_ids=[
                    a.algo_id for a in account.protections if a.inst_id == row["inst_id"] and a.side == exit_side
                    and a.pos_side in ("net", pos.side) and a.stop_loss and (p["trigger"] - a.stop_loss) * sign >= 0
                ])
                return "armed", f"原仓位还在：它被止损后价格到 {p['trigger']:g} 就反手"
            if pos:
                return "cancelled", "持仓已经是反手的方向，不用再反手"
            if not state.get("seen"):
                if p.get("expires_ts") and now_ms() > p["expires_ts"]:
                    return "expired", "原来的单一直没成交，反手作废"
                return "armed", "等原来的单成交"
            verified = False
            if state.get("stop_ids") and state.get("size"):
                since = state.setdefault("flat_seen", time.time())
                if time.time() - since > 30:
                    return "cancelled", "未及时核实到原仓整笔止损成交回执，不反手"
                if time.time() < state.get("next_receipt_check", 0):
                    return "armed", "等交易所止损成交回执"
                state["next_receipt_check"] = time.time() + 5
                history = await self.trader.gw.call("swap_get_algo_orders", {"instId": row["inst_id"], "status": "history", "state": "effective"}, env=cfg.env)
                for a in history or []:
                    if a.get("algoId") in state["stop_ids"] and a.get("actualSide") == "tp" and a.get("state") == "effective":
                        return "cancelled", "原保护单为止盈触发，不反手"
                    if not (a.get("instId") == row["inst_id"] and a.get("algoId") in state["stop_ids"] and a.get("actualSide") == "sl" and a.get("state") == "effective"):
                        continue
                    oid = a.get("ordId") or next(iter(a.get("ordIdList") or []), "")
                    if not oid:
                        continue
                    fills = await self.trader.gw.call("swap_get_order", {"instId": row["inst_id"], "ordId": oid}, env=cfg.env)
                    for fill in fills or []:
                        if (fill.get("ordId") == oid and fill.get("instId") == row["inst_id"] and fill.get("state") == "filled"
                                and fill.get("side") == ("buy" if long else "sell") and float(fill.get("accFillSz") or 0) >= state["size"]
                                and 0 < float(fill.get("uTime") or 0) <= now_ms()):
                            state.update(stop_receipt=oid, flat_at=float(fill["uTime"]) / 1000)
                            verified = True
                            break
                    if verified:
                        break
            if not verified:
                if state.get("flat_seen"):
                    return "armed", "未核实到原仓整笔止损成交，最多等 30 秒，不按报价猜测"
                return "cancelled", "没有原仓止损单编号可核对，不反手"
        elif pos:
            return "cancelled", "已经有新的仓位了，反手作废"
        if not state.get("stop_receipt"):
            return "cancelled", "旧反手状态没有止损成交回执，不反手"
        if time.time() - state["flat_at"] > p.get("window_minutes", 60) * 60:
            return "expired", f"止损后 {p.get('window_minutes', 60):g} 分钟价格没到 {p['trigger']:g}，反手作废"
        if (px - p["trigger"]) * sign < 0:
            return "armed", f"原仓位已止损，等价格{'涨到' if long else '跌到'} {p['trigger']:g} 反手"
        if abs(px / p["trigger"] - 1) * 100 > p.get("max_chase_pct", 0.5):
            return "cancelled", f"价格已经冲过反手触发价 {abs(px / p['trigger'] - 1) * 100:.2f}%，追进去位置太差，作废"
        if (px - p["stop_loss"]) * sign <= 0:
            return "cancelled", "价格已经越过反手单的止损，作废"
        result = await self._order(cfg, row, self._entry(account, row, row["side"], p["stop_loss"], p.get("take_profit"), px), "止损后自动反手")
        return "done", f"已在 {px:g} 反手{'做多' if long else '做空'}，止损 {p['stop_loss']:g}：{result.message[:120]}"

    # ----- 持仓管理：移保本、分批止盈、跟踪止损、时间止损 -----

    async def _manage_lot(self, cfg: AgentConfig, row: dict[str, Any], state: dict[str, Any], px: float, rules: dict[str, Any]) -> tuple[str, str]:
        lot = self._lot(cfg.env, row["inst_id"], state.get("lot_id"))
        if not lot:
            return "done", "这笔仓位已经平掉"
        long = lot["side"] == "long"
        entry, r = state.get("entry") or lot["entry_px"], state.get("r") or abs(lot["entry_px"] - (lot["stop_loss"] or lot["entry_px"]))
        if not r:
            return "done", "这笔仓位没有止损，算不出 R，不做自动管理"
        gain = ((px - entry) if long else (entry - px)) / r
        state["peak"] = max(state.get("peak", px), px) if long else min(state.get("peak", px), px)
        stops = [s for s in (state.get("stop"), lot["stop_loss"]) if s]
        stop_now = (max(stops) if long else min(stops)) if stops else 0  # 保盈线、Worker 可能已经把止损移得更紧：不往回放
        note = f"现在 {gain:+.2f}R"
        level, bar = rules.get("review_level"), rules.get("review_bar")
        if level and not state.get("review_end") and bar in TRIGGER_BARS:
            close = (self.metrics.get(row["inst_id"], {}).get("structure_closes") or {}).get(bar)
            if (close and 0 <= now_ms() - close["end"] <= DATA_STALE_MS
                    and close["end"] - TRIGGER_BARS[bar] * 60_000 >= state.get("since", time.time()) * 1000
                    and (close["price"] - level) * (1 if long else -1) < 0):
                state["review_end"] = close["end"]
                text = f"第 {lot['id']} 笔：{bar} 收盘 {close['price']:g} 越过原计划复核线 {level:g}。单独评估原仓理由；这不是平仓或反手指令。"
                self.emit(cfg.env, row["inst_id"], "strategy", "action", text, None, {"strategy_id": row["id"], "notify_worker": True})

        async def move(stop: float, what: str) -> None:
            await self._order(cfg, row, TradeAction(kind="set_stops", inst_id=row["inst_id"], lot_id=lot["id"], stop_loss=stop, reason=what), what)
            state["stop"] = stop

        if rules.get("breakeven_r") and not state.get("be") and gain >= rules["breakeven_r"]:
            if not stop_now or (stop_now - entry) * (1 if long else -1) < 0:
                await move(entry, f"浮盈到 {rules['breakeven_r']:g}R，止损移到保本 {entry:g}")
                stop_now = entry
            state["be"] = True
        done = state.setdefault("partials", [])
        for i, part in enumerate(rules.get("partials") or []):
            if i not in done and gain >= part["r"]:
                done.append(i)
                try:
                    await self._order(
                        cfg, row, TradeAction(kind="reduce", inst_id=row["inst_id"], lot_id=lot["id"], fraction=part["fraction"], reason=f"到 {part['r']:g}R 分批止盈"),
                        f"到 {part['r']:g}R，平掉 {int(part['fraction'] * 100)}%",
                    )
                except Rejected as exc:  # 仓位太小分不了批：这一档跳过，后面的移保本、跟踪止损照常做，不让整条管理规则停掉
                    note = f"到 {part['r']:g}R 的分批止盈没做成（{str(exc)[:80]}），这一档跳过"
        if rules.get("trail_pct") and gain >= rules.get("trail_start_r", rules.get("breakeven_r") or 1):
            trail = state["peak"] * (1 - rules["trail_pct"] / 100) if long else state["peak"] * (1 + rules["trail_pct"] / 100)
            better = trail > stop_now + r * 0.25 if long else (not stop_now or trail < stop_now - r * 0.25)
            if better and ((trail < px) if long else (trail > px)):
                await move(trail, f"跟踪止损上移到 {trail:g}" if long else f"跟踪止损下移到 {trail:g}")
        if rules.get("max_hold_minutes") and state.get("since") and time.time() - state["since"] > rules["max_hold_minutes"] * 60 and gain < 0.5:
            await self._order(cfg, row, TradeAction(kind="close", inst_id=row["inst_id"], lot_id=lot["id"], reason="时间止损"), f"{rules['max_hold_minutes']} 分钟没走出来，时间止损离场")
            return "done", "时间止损离场"
        return "active", note

    async def _manage(self, cfg: AgentConfig, account: AccountState, row: dict[str, Any], state: dict[str, Any], px: float) -> tuple[str, str]:
        if not state.get("lot_id"):
            want = row["params"].get("lot_id")
            mine = [l for l in self.trader.lots(cfg.env, row["inst_id"]) if not want or l["id"] == want]
            lots = [l for l in mine if l["status"] == "open"]
            if want and not lots and any(l["status"] == "working" for l in mine):
                return "active", "等挂单成交后开始管理"  # 这一笔是限价单，还没成交
            if not lots:
                return "done", "没有可以管理的持仓"
            state.update(lot_id=lots[0]["id"], since=time.time())
        return await self._manage_lot(cfg, row, state, px, row["params"])

    # ----- 区间两头 -----

    async def _range(self, cfg: AgentConfig, account: AccountState, row: dict[str, Any], state: dict[str, Any], px: float) -> tuple[str, str]:
        p = row["params"]
        if row["status"] == "active":
            if self._lot(cfg.env, row["inst_id"], state.get("lot_id")):
                return "active", f"第 {state.get('rounds', 0) + 1} 轮持仓中"
            state.update(rounds=state.get("rounds", 0) + 1, lot_id=None)
            if state["rounds"] >= p.get("max_rounds", 3):
                return "done", f"做完 {state['rounds']} 轮"
        if p.get("expires_ts") and now_ms() > p["expires_ts"]:
            return "expired", "到时间，作废"
        if px <= p["stop_low"] or px >= p["stop_high"]:
            return "cancelled", f"价格 {px:g} 已经走出区间，区间失效"
        band = (p["high"] - p["low"]) * p.get("edge_pct", 10) / 100  # 离边缘这么近就算到了
        mid = (p["low"] + p["high"]) / 2
        target = lambda far: mid if p.get("target", "mid") == "mid" else far  # noqa: E731
        if px <= p["low"] + band:
            side, stop, tp = "long", p["stop_low"], target(p["high"] - band)
        elif px >= p["high"] - band:
            side, stop, tp = "short", p["stop_high"], target(p["low"] + band)
        else:
            return "armed", ""
        result = await self._order(cfg, row, self._entry(account, row, side, stop, tp, px), f"到区间{'下沿做多' if side == 'long' else '上沿做空'}")
        state.update(lot_id=result.detail.get("lot_id"))
        return "active", f"第 {state.get('rounds', 0) + 1} 轮：{'做多' if side == 'long' else '做空'}，入场 {px:g}"

    # ----- 翻倍补仓（无限子弹） -----

    async def _martingale(self, cfg: AgentConfig, account: AccountState, row: dict[str, Any], state: dict[str, Any], px: float) -> tuple[str, str]:
        """程序马丁：逆向每走一段按倍数补一层，把均价拉近现价；价格回到均价外侧一点（保本位）整组平掉。
        空仓时自己开第一层；已经有同方向的持仓时接手它来解套，不另开第一层。每一笔照样过硬性风控：层数上限、整组止损、单仓风险上限。"""
        p, long = row["params"], row["side"] == "long"
        sign, stop, mult = (1 if long else -1), p["stop_loss"], p.get("multiplier", 1.6)
        layers: list[dict[str, Any]] = state.setdefault("layers", [])
        pos = account.position(row["inst_id"])
        if pos and pos.side != row["side"]:
            return "cancelled", "持仓方向和这组马丁相反，已停止"
        if layers and not pos:
            return "done", f"整组已经平掉（保本止盈或整组止损触发），一共 {len(layers)} 层"
        if (px - stop) * sign <= 0:
            return ("active" if pos else "cancelled"), f"现价 {px:g} 已经越过整组止损 {stop:g}，不再补仓"
        if not layers:
            if pos:  # 接手已有的仓位来解套：把它当成第一层
                layers.append({"px": pos.avg_px, "margin": pos.margin or pos.notional_usd / max(pos.leverage, 1), "seed": True})
                state["rescue"] = True
                if self.scores and pos.opened_at:
                    self.scores.tag(cfg.env, row["inst_id"], row["side"], "程序马丁（解套）", "程序马丁", opened_ts=pos.opened_at)
            elif p.get("trigger") and (px - p["trigger"]) * sign > 0:
                return "armed", ""
        if "step_pct" not in state:  # 第一拍：把补仓间距和层数定下来，保证最后一层补在整组止损之前
            room = abs(px - stop) / px * 100
            adds = max(int(p["max_layers"]) - 1, 1)
            step = p.get("step_pct") or max(room / (adds + 1), 0.3)
            while adds > 1 and step * adds >= room * 0.95:
                adds -= 1
            if step * adds >= room * 0.95:
                return "cancelled", f"现价离整组止损 {stop:g} 只有 {room:.2f}%，一层都补不进去：把整组止损放到更远的结构位之外，或者不用马丁"
            state.update(step_pct=round(step, 3), max_layers=adds + 1)

        def tp_of(avg: float) -> float:  # 保本位：均价再让出一点（默认是往返手续费）
            return avg * (1 + sign * p["take_profit_pct"] / 100)

        if not layers:  # 空仓：开第一层。没指定第一层多大，就按「整组全补满又打到止损时不超过单仓风险上限」来分
            action = self._entry(account, row, row["side"], stop, tp_of(px), px, margin=p.get("base_margin"))
            if action.margin_usdt and action.risk_pct is None:
                action = action.model_copy(update={"risk_pct": cfg.risk.risk_per_trade_pct})
            if not p.get("base_margin"):
                share = cfg.risk.risk_per_trade_pct / sum(mult ** i for i in range(state["max_layers"]))
                action = action.model_copy(update={"risk_pct": round(max(share, 0.01), 4)})
            await self._order(cfg, row, action, f"程序马丁第 1 层（共 {state['max_layers']} 层，间距 {state['step_pct']:g}%）")
            layers.append({"px": px, "margin": p.get("base_margin")})
            state["tp"] = tp_of(px)
            return "active", "已开第 1 层"
        if not layers[0].get("margin"):
            layers[0]["margin"] = pos.margin or pos.notional_usd / max(pos.leverage, 1)

        want = tp_of(pos.avg_px)
        if (px - want) * sign >= 0:  # 已经回到保本位外侧：不等止盈单，直接整组平掉
            await self._order(cfg, row, TradeAction(kind="close", inst_id=row["inst_id"], reason="程序马丁：回到保本位，整组平仓"), f"回到保本位 {want:g}，整组平仓")
            return "done", f"价格回到保本位 {want:g}，整组已平，一共 {len(layers)} 层"
        if (not state.get("tp") or abs(want / state["tp"] - 1) > 0.0003) and state.get("tp_fail") != round(want, 8):
            try:  # 均价变了：整组的保本止盈跟着挪
                await self._order(cfg, row, TradeAction(kind="set_stops", inst_id=row["inst_id"], take_profit=want, stop_loss=stop, reason="程序马丁：保本止盈跟着均价移动"), f"整组保本止盈移到 {want:g}")
                state.update(tp=want, avg=pos.avg_px)
                state.pop("tp_fail", None)
            except Rejected as exc:
                state.update(tp_fail=round(want, 8), tp_note=str(exc)[:140])

        n, last = len(layers), layers[-1]["px"]
        moved = (last - px) * sign / last * 100
        head = f"{'解套中' if state.get('rescue') else '进行中'}：{n} 层，均价 {pos.avg_px:g}，保本位 {want:g}" + (f"（止盈单没挂上：{state['tp_note']}）" if state.get("tp_fail") else "")
        tail = f"等保本止盈或整组止损 {stop:g}"
        if state.get("full"):
            return "active", f"{head}；{state['full']}，不再补，{tail}"
        if n >= state["max_layers"]:
            return "active", f"{head}；已经补满 {n} 层，不再补，{tail}"
        if moved < state["step_pct"]:
            return "active", f"{head}；再逆向 {state['step_pct'] - moved:.2f}% 补下一层"
        margin = (p.get("base_margin") or layers[0]["margin"]) * mult ** n
        try:
            action = self._entry(account, row, row["side"], stop, None, px, margin=margin)
            if action.risk_pct is None:
                action = action.model_copy(update={"risk_pct": cfg.risk.risk_per_trade_pct})
            await self._order(cfg, row, action, f"程序马丁第 {n + 1} 层（保证金约 {margin:.4g} USDT）")
        except Rejected as exc:  # 风险额度、加仓次数、保证金用完：不硬补，守着已有的仓位
            state["full"] = f"风控不让再补（{str(exc)[:120]}）"
            return "active", f"{head}；{state['full']}，{tail}"
        layers.append({"px": px, "margin": margin})
        state["tp"] = None  # 均价变了，下一拍重挂保本止盈
        return "active", f"已补到第 {n + 1} 层，下一拍把保本止盈移到新均价"

    # ----- 网格 -----

    async def _grid(self, cfg: AgentConfig, account: AccountState, row: dict[str, Any], state: dict[str, Any], px: float) -> tuple[str, str]:
        p, long = row["params"], row["side"] == "long"
        n = int(p["grids"])
        step = (p["high"] - p["low"]) / n
        if p.get("expires_ts") and now_ms() > p["expires_ts"]:
            return "expired", "到时间，网格停止（已有的仓位留给止损和 Worker 管理）"
        if (px <= p["stop_loss"]) if long else (px >= p["stop_loss"]):
            return "done", "价格到了网格止损位，网格停止"
        idx = max(min(int((px - p["low"]) // step), n), -1)  # 现在在第几格（0 = 最下面一格）
        held: dict[str, int] = state.setdefault("held", {})
        for k in [k for k, v in held.items() if not self._lot(cfg.env, row["inst_id"], v)]:
            held.pop(k)
        last = state.get("idx")
        state["idx"] = idx
        if last is None or idx == last:
            return "active" if held else row["status"], f"在第 {idx + 1} 格，持有 {len(held)} 份"
        crossed = range(last, idx, -1) if idx < last else range(last + 1, idx + 1)  # 穿过的格线编号
        for line in crossed:
            if not 0 <= line <= n:
                continue
            opening = (idx < last) == long  # 做多网格：跌破一条线买一份；做空网格：涨破一条线卖一份
            key = str(line)
            if opening and key not in held:
                result = await self._order(cfg, row, self._entry(account, row, row["side"], p["stop_loss"], None, px, margin=p["margin_per_grid"]), f"网格第 {line} 线{'买入' if long else '卖出'}一份")
                held[key] = result.detail.get("lot_id")
                account = await self.trader.account(cfg.env)
            elif not opening:
                mate = str(line - 1) if long else str(line + 1)  # 平掉在前一条线开的那一份
                if mate in held:
                    await self._order(cfg, row, TradeAction(kind="close", inst_id=row["inst_id"], lot_id=held.pop(mate), reason="网格止盈一格"), f"网格第 {line} 线止盈一份")
        return "active", f"在第 {idx + 1} 格，持有 {len(held)} 份"
