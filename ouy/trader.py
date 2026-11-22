"""交易执行层：把一条 TradeAction 过风控后变成欧易订单。

Agent 自动下单、待确认指令、手动下单都走这里——同一套风控、同一条通道、同一份日志。
"""
from __future__ import annotations

import asyncio
import re
import time
from decimal import Decimal
from typing import Any, Callable, Literal

from . import risk
from .account import closed_trades, load_account
from .events import EventBus
from .market import Market, _f
from .models import STYLES, AccountState, ActionResult, AgentConfig, ClosedTrade, Env, Position, TradeAction
from .okx.gateway import Gateway, OkxError
from .profit_guard import quote, signal
from .risk import Rejected, floor_to_step, fmt, round_price
from .store import Store, now_ms

PENDING_TTL_MS = 10 * 60 * 1000

KIND_LABEL = {
    "set_leverage": "调整杠杆",
    "open": "开仓",
    "add": "加仓",
    "reduce": "减仓",
    "close": "平仓",
    "set_stops": "移动止盈止损",
    "cancel_orders": "撤销挂单",
}


MANUAL_SOURCES = ("user", "external")
MANUAL_GUARDED = ("reduce", "close", "set_stops")


def describe(action: TradeAction) -> str:
    parts = [KIND_LABEL[action.kind], action.inst_id]
    if action.side and action.kind in ("open", "add"):
        parts.append("做多" if action.side == "long" else "做空")
    if action.leverage:
        parts.append(f"{action.leverage}x")
    if action.kind in ("open", "add"):
        parts.append(f"限价 {action.price:g}" if action.order_type == "limit" and action.price else "市价")
    if action.fraction:
        parts.append(f"{action.fraction * 100:.0f}%")
    if action.stop_loss:
        parts.append(f"止损 {action.stop_loss:g}")
    if action.take_profit:
        parts.append(f"止盈 {action.take_profit:g}")
    if action.cancel_px:
        parts.append(f"撤单线 {action.cancel_px:g}")
    if action.expires_minutes:
        parts.append(f"{action.expires_minutes} 分钟内有效")
    return " · ".join(parts)


def _check_rows(tool: str, rows: Any) -> Any:
    """欧易批量/下单接口会在每行里带 sCode，非 0 即失败。"""
    for row in rows if isinstance(rows, list) else []:
        code = str(row.get("sCode", "0") or "0") if isinstance(row, dict) else "0"
        if code != "0":
            raise OkxError(f"{row.get('sMsg') or '欧易拒绝了这笔请求'}（代码 {code}）", tool=tool, code=code, detail=row)
    return rows


STYLE_ORDER = {"scalp": 0, "intraday": 1, "swing": 2}
STYLE_LABEL = {"scalp": "超短线", "intraday": "日内短线", "swing": "波段"}


class Trader:
    def __init__(self, gateway: Gateway, market: Market, store: Store, bus: EventBus, get_config: Callable[[], AgentConfig]):
        self.gw = gateway
        self.market = market
        self.store = store
        self.bus = bus
        self.get_config = get_config
        self._locks: dict[str, asyncio.Lock] = {}
        self._equity_ts: dict[str, float] = {}
        self._lot_touch: dict[str, float] = {}
        self._stale_orders: list[tuple[str, str, str, int]] = []
        self.settle_seconds = 0.5  # 平仓单提交后隔多久去核对一次仓位
        self.illiquid: dict[str, str] = {}  # 模拟盘里流动性太差的币（由盯盘循环更新），Agent 在上面每笔风险压到四分之一

    def _lock(self, env: str, inst_id: str) -> asyncio.Lock:
        return self._locks.setdefault(f"{env}:{inst_id}", asyncio.Lock())

    async def account(self, env: Env | None = None) -> AccountState:
        cfg = self.get_config()
        target = env or cfg.env
        state = await load_account(self.gw, target, self.store, cfg.risk)
        if state.equity > 0 and time.time() - self._equity_ts.get(target, 0) >= 60:
            self._equity_ts[target] = time.time()
            self.store.add_equity(target, state.equity)
        try:
            self._sync_lots(target, state)
        except Exception as exc:  # noqa: BLE001 - 记账对不上不能影响读账户
            self.bus.publish("journal", self.store.log(target, "alert", "分笔记账对账出错", {"message": str(exc)[:300]}))
        if self._stale_orders:
            await self._drop_stale_orders(state)
        for order in state.orders:  # 每张挂单占用了多少资金
            if not order.px:
                continue
            try:
                inst = await self.market.instrument(order.inst_id, target)
            except Exception:  # noqa: BLE001 - 合约规格读不到就不算，不影响读账户
                continue
            order.notional_usd = round(max(order.sz - order.filled, 0) * inst.ct_val * order.px, 2)
            order.margin_usd = 0.0 if order.reduce_only else round(order.notional_usd / (order.leverage or 1), 2)
        return state

    async def _drop_stale_orders(self, state: AccountState) -> None:
        """两头挂单时一边成交了：撤掉另一边，否则它成交会把刚开的仓抵消掉。"""
        stale, self._stale_orders = self._stale_orders, []
        for env, inst_id, ord_id, lot_id in stale:
            try:
                await self._call("swap_cancel_order", {"instId": inst_id, "ordId": ord_id}, env)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 - 撤不掉就留着这一笔，下次对账再试
                self.bus.publish("journal", self.store.log(env, "alert", f"{inst_id} 反方向的挂单没撤掉", {"message": str(exc)[:300], "inst_id": inst_id}))
                continue
            self.store.update_lot(lot_id, status="closed", note="另一边已成交，撤掉这一边的挂单")
            state.orders = [o for o in state.orders if o.ord_id != ord_id]
            self.bus.publish("journal", self.store.log(env, "system", f"{inst_id} 一边的挂单已成交，自动撤掉了反方向的挂单", {"inst_id": inst_id, "ord_id": ord_id}))

    # ---------- 分笔记账（lots） ----------
    # 欧易把同一个币、同一方向的仓位合并成一个持仓。每一笔的风格、入场价、止损止盈由这里记账，
    # 每笔自带一张按它自己张数挂的止盈止损单，所以不同风格的几笔可以分别管理。

    def _touch(self, env: str, inst_id: str) -> None:
        self._lot_touch[f"{env}:{inst_id}"] = time.time()

    def lots(self, env: Env, inst_id: str | None = None, account: AccountState | None = None) -> list[dict[str, Any]]:
        rows = self.store.lots(env, inst_id)
        for row in rows:
            pos = account.position(row["inst_id"]) if account else None
            sign = 1 if row["side"] == "long" else -1
            row["pnl_pct"] = round((pos.mark_px - row["entry_px"]) / row["entry_px"] * 100 * sign, 3) if pos and row["status"] == "open" and row["entry_px"] else None
        return rows

    @staticmethod
    def _match_algo(lot: dict[str, Any], algos: list[Any], used: set[str]) -> str:
        """这一笔的止盈止损单：优先用记下的 algoId，没有就按「张数相同、止损价相同」去认。"""
        ids = {a.algo_id for a in algos}
        if lot["algo_id"] and lot["algo_id"] in ids:
            return lot["algo_id"]
        for a in algos:
            if a.algo_id in used or not a.sz or abs(_f(a.sz) - lot["contracts"]) > 1e-9:
                continue
            if lot["stop_loss"] and a.stop_loss and abs(a.stop_loss - lot["stop_loss"]) / lot["stop_loss"] > 1e-6:
                continue
            return a.algo_id
        return ""

    def _sync_lots(self, env: str, state: AccountState) -> None:
        """拿欧易的真实持仓对账：外部成交、止盈止损触发、挂单成交都会让两边对不上。"""
        cfg = self.get_config()
        live = self.store.lots(env)
        now = time.time()
        for inst_id in {l["inst_id"] for l in live} | {p.inst_id for p in state.positions}:
            if self._lock(env, inst_id).locked() or now - self._lot_touch.get(f"{env}:{inst_id}", 0) < 6:
                continue  # 正在下单或刚下完单，欧易那边的持仓可能还没更新
            lots = [l for l in live if l["inst_id"] == inst_id]
            pos = state.position(inst_id)
            open_ords = {o.ord_id for o in state.orders if o.inst_id == inst_id}
            algos = [a for a in state.protections if a.inst_id == inst_id]
            for lot in [l for l in lots if l["status"] == "open" and (not pos or pos.side != l["side"])]:
                self.store.update_lot(lot["id"], status="closed", note="持仓已经不在了")
                lot["status"] = "closed"
            opened = [l for l in lots if l["status"] == "open"]
            working = [l for l in lots if l["status"] == "working"]
            used: set[str] = set()
            for lot in opened:
                bound = self._match_algo(lot, algos, used)
                if bound:
                    used.add(bound)
                    if bound != lot["algo_id"]:
                        self.store.update_lot(lot["id"], algo_id=bound)
                        lot["algo_id"] = bound
            have = pos.contracts if pos else 0.0
            booked = sum(l["contracts"] for l in opened)
            eps = 1e-9
            if have > booked + eps:
                for lot in working:  # 挂单成交了
                    if lot["ord_id"] in open_ords or have <= booked + eps or (pos and lot["side"] != pos.side):
                        continue
                    take = round(min(lot["contracts"], have - booked), 9)
                    self.store.update_lot(lot["id"], status="open", contracts=take)
                    lot["status"] = "open"
                    booked += take
                if have > booked + eps and pos:  # 不是通过本项目开的（比如在欧易 App 里下的单）
                    ext = next((l for l in opened if l["source"] == "external"), None)
                    if ext:
                        self.store.update_lot(ext["id"], contracts=round(ext["contracts"] + have - booked, 9))
                    else:
                        self.store.add_lot(env, inst_id, pos.side, cfg.style_for(inst_id), round(have - booked, 9), pos.avg_px, source="external", note="不是在本项目里开的仓")
            elif have < booked - eps and pos:
                gone = booked - have
                ids = {a.algo_id for a in algos}
                for lot in opened:  # 止盈止损单不见了的那一笔，就是被触发平掉的那一笔
                    if lot["algo_id"] and lot["algo_id"] not in ids and lot["contracts"] <= gone + eps:
                        self.store.update_lot(lot["id"], status="closed", note="止盈或止损触发")
                        lot["status"] = "closed"
                        gone -= lot["contracts"]
                rest = sorted([l for l in opened if l["status"] == "open"], key=lambda l: (STYLE_ORDER.get(l["style"], 1), -l["id"]))
                for lot in rest:
                    if gone <= eps:
                        break
                    cut = min(lot["contracts"], gone)
                    gone -= cut
                    if lot["contracts"] - cut <= eps:
                        self.store.update_lot(lot["id"], status="closed", note="在别处被平掉")
                    else:
                        self.store.update_lot(lot["id"], contracts=lot["contracts"] - cut)
            for lot in working:
                if lot["status"] == "working" and lot["ord_id"] not in open_ords:
                    self.store.update_lot(lot["id"], status="closed", note="挂单没有成交")
                elif lot["status"] == "working" and pos and lot["side"] != pos.side:
                    # 两头挂单，一边已经成交：另一边的挂单留着会把刚开的仓抵消掉，记下来交给调用方撤掉
                    self._stale_orders.append((env, inst_id, lot["ord_id"], lot["id"]))

    def manual_lots(self, env: str, action: TradeAction) -> list[dict[str, Any]]:
        """这个动作会动到的、由用户自己开的仓（下单窗或欧易 App 里开的）。"""
        if action.kind not in MANUAL_GUARDED:
            return []
        lots = [l for l in self.store.lots(env, action.inst_id) if l["status"] == "open"]
        if action.lot_id is not None:
            lots = [l for l in lots if l["id"] == action.lot_id]
        return [l for l in lots if l["source"] in MANUAL_SOURCES]

    def manual_exits(self, env: str, action: TradeAction) -> list[dict[str, Any]]:
        """这个动作会替用户把他自己开的仓了结掉（减仓、平仓、设 / 改止盈）：补止损、收紧止损是保护，不算。"""
        exits = action.kind in ("reduce", "close") or (action.kind == "set_stops" and action.take_profit is not None)
        return self.manual_lots(env, action) if exits else []

    def _lot(self, action: TradeAction, env: str) -> dict[str, Any] | None:
        if action.lot_id is None:
            return None
        lot = self.store.lot(action.lot_id)
        if not lot or lot["env"] != env or lot["inst_id"] != action.inst_id or lot["status"] != "open":
            raise Rejected(f"{action.inst_id} 没有编号 {action.lot_id} 的持仓。")
        return lot

    async def _place_sized(self, pos: Position, contracts: float, sl: Any, tp: Any, env: Env) -> str:
        step = risk.Decimal((await self.market.instrument(pos.inst_id, env)).lot_sz)
        # 分笔张数是浮点相减得来的（5.900000000000004）：对齐到最小下单单位，否则欧易报 51121
        qty = (risk.Decimal(str(contracts)) / step).to_integral_value(rounding=risk.ROUND_HALF_UP) * step
        if qty <= 0:
            raise Rejected(f"这一笔只有 {contracts:g} 张，不到最小下单单位 {step}，没法单独挂保护单。")
        args: dict[str, Any] = {
            "instId": pos.inst_id, "tdMode": pos.margin_mode, "side": "sell" if pos.side == "long" else "buy", "posSide": pos.pos_side,
            "ordType": "oco" if sl and tp else "conditional", "sz": fmt(qty),
        }
        if pos.pos_side == "net":
            args["reduceOnly"] = True
        if sl:
            args.update({"slTriggerPx": str(sl), "slOrdPx": "-1"})
        if tp:
            args.update({"tpTriggerPx": str(tp), "tpOrdPx": "-1"})
        rows = await self._call("swap_place_algo_order", args, env)
        return str(rows[0].get("algoId", "")) if rows else ""

    async def _cancel_algos(self, inst_id: str, ids: list[str], env: Env) -> None:
        if not ids:
            return
        try:
            await self._call("swap_cancel_algo_orders", {"orders": [{"algoId": i, "instId": inst_id} for i in ids]}, env)
        except OkxError as exc:
            self.bus.publish("journal", self.store.log(env, "alert", f"{inst_id} 旧的止盈止损单没撤掉", {"message": str(exc)}, inst_id=inst_id))

    async def _lot_algo(self, lot: dict[str, Any], pos: Position, env: Env) -> dict[str, Any] | None:
        rows = await self._protections(pos, env)
        if lot["algo_id"]:
            hit = next((r for r in rows if r.get("algoId") == lot["algo_id"]), None)
            if hit:
                return hit
        return next((r for r in rows if r.get("sz") and abs(_f(r.get("sz")) - lot["contracts"]) < 1e-9
                     and (not lot["stop_loss"] or not r.get("slTriggerPx") or abs(_f(r.get("slTriggerPx")) - lot["stop_loss"]) / lot["stop_loss"] < 1e-6)), None)

    async def _resize_lot(self, lot: dict[str, Any], pos: Position, left: float, env: Env) -> None:
        """这一笔减掉一部分或全部之后，把它自己的止盈止损单换成剩余张数的（先挂新的再撤旧的）。"""
        mine = await self._lot_algo(lot, pos, env)
        if left <= 1e-9:
            if mine:
                await self._cancel_algos(pos.inst_id, [mine["algoId"]], env)
            self.store.update_lot(lot["id"], status="closed", note="已平仓")
            return
        fields: dict[str, Any] = {"contracts": left}
        if mine:
            fields["algo_id"] = await self._place_sized(pos, left, mine.get("slTriggerPx") or None, mine.get("tpTriggerPx") or None, env)
            await self._cancel_algos(pos.inst_id, [mine["algoId"]], env)
        self.store.update_lot(lot["id"], **fields)

    async def history(self, inst_id: str | None = None, limit: int = 20, env: Env | None = None) -> list[ClosedTrade]:
        return await closed_trades(self.gw, env or self.get_config().env, inst_id, limit)

    async def _call(self, tool: str, args: dict[str, Any], env: Env) -> Any:
        return _check_rows(tool, await self.gw.call(tool, args, env=env))

    # ---------- 入口 ----------

    async def submit(self, action: TradeAction, *, source: Literal["agent", "user"], far_ok: bool = False) -> ActionResult:
        """far_ok：程序照计划挂的限价单带着撤单线和有效期，不受「离现价太远」的限制。"""
        cfg = self.get_config()
        env = cfg.env
        if source == "user":
            return await self.execute(action, env, source=source)
        if action.inst_id not in cfg.instruments:
            return self._finish(env, action, "rejected", f"{action.inst_id} 不在盯盘列表里，Agent 不能交易它。", source)
        if env == "demo" and action.kind in ("open", "add") and self.illiquid.get(f"{env}:{action.inst_id}"):
            cap = cfg.risk.risk_per_trade_pct / 4
            action = action.model_copy(update={"risk_pct": min(action.risk_pct or cap, cap)})
        if action.kind in ("open", "add") and action.order_type == "limit" and action.price and not far_ok:
            far = await self._too_far(action, cfg, env)
            if far:
                return self._finish(env, action, "rejected", far, source)
        action = await self._apply_prefs(action, env)
        permission = cfg.permission_for(action.kind, action.inst_id)
        manual = [] if permission == "off" else self.manual_lots(env, action) if cfg.hands_off_manual else self.manual_exits(env, action)
        if manual:
            ids = "、".join(f"#{l['id']}" for l in manual)
            try:
                await self._dry_run(action, env)
            except (Rejected, OkxError) as exc:
                return self._finish(env, action, "rejected", str(exc), source)
            self.store.expire_pending(env, PENDING_TTL_MS)
            pending_id = self.store.add_pending(env, action.model_dump())
            result = self._finish(
                env, action, "pending",
                f"{ids} 是用户手动开的仓，" + ("Agent 不能自己动" if cfg.hands_off_manual else "什么时候止盈离场由用户定，你只能补止损、收紧止损") + "，已发给用户确认（10 分钟内有效）。"
                "只想处理你自己开的那几笔，就带上那一笔的 lot_id 重新调用。",
                source, pending_id=pending_id,
            )
            self.bus.publish("pending", {"id": pending_id})
            return result
        if permission == "off":
            return self._finish(env, action, "advised", f"用户把「{KIND_LABEL[action.kind]}」设成了不允许 Agent 做，没有执行，也不会生成待同意的卡片。", source)
        if permission == "confirm":
            try:
                await self._dry_run(action, env)
            except (Rejected, OkxError) as exc:
                return self._finish(env, action, "rejected", str(exc), source)
            self.store.expire_pending(env, PENDING_TTL_MS)
            pending_id = self.store.add_pending(env, action.model_dump())
            result = self._finish(env, action, "pending", "已提交，等待你在界面上确认后执行（10 分钟内有效）。", source, pending_id=pending_id)
            self.bus.publish("pending", {"id": pending_id})
            return result
        return await self.execute(action, env, source=source)

    async def _too_far(self, action: TradeAction, cfg: AgentConfig, env: Env) -> str:
        """Agent 的限价单挂得离现价太远就成交不了：超过这种风格允许的距离就拒绝，让它改市价或挂近一点。"""
        style = action.style or cfg.style_for(action.inst_id)
        limit = cfg.limit_dist_for(style)
        try:
            last = _f((await self.market.ticker(action.inst_id, env)).get("last"))
        except OkxError:
            return ""
        if not last or not action.price:
            return ""
        dist = abs(action.price / last - 1) * 100
        if dist <= limit:
            return ""
        return (
            f"限价 {action.price:g} 离现价 {last:g} 有 {dist:.2f}%，超过了{STYLE_LABEL.get(style, style)}允许的 {limit:g}%，这样的单很可能一直成交不了。"
            "方向成立就改用市价单（order_type=market）现在进场，止损按现价重新放在结构位之外；否则把价格挂到允许的距离以内，或者这次不下单、把这个价位写进 watch_levels。"
        )

    async def approve(self, pending_id: int) -> ActionResult:
        row = self.store.get_pending(pending_id)
        if not row:
            raise Rejected("没有这条待确认指令。")
        action = TradeAction.model_validate(row["action"])
        if row["status"] != "waiting":
            raise Rejected(f"这条指令已经处理过了（{row['status']}）。")
        if row["env"] != self.get_config().env:
            raise Rejected("这条指令是在另一个交易环境下生成的，已失效。")
        if now_ms() - row["ts"] > PENDING_TTL_MS:
            self.store.resolve_pending(pending_id, "expired", {})
            raise Rejected("这条指令已超过 10 分钟，行情已变，已作废。")
        if not self.store.claim_pending(pending_id):
            raise Rejected("这条指令正在执行或已处理。")
        result = await self.execute(action, row["env"], source="agent")
        self.store.resolve_pending(pending_id, result.status, result.model_dump(mode="json"))
        self.bus.publish("pending", {"id": pending_id})
        return result

    def reject(self, pending_id: int) -> None:
        if not self.store.claim_pending(pending_id):
            raise Rejected("这条指令正在执行或已处理。")
        self.store.resolve_pending(pending_id, "declined", {})
        self.bus.publish("pending", {"id": pending_id})

    async def _apply_prefs(self, action: TradeAction, env: Env) -> TradeAction:
        """Agent 开新仓时杠杆没填、或者低于用户的常用杠杆：按用户偏好补上。止损距离放不下这么高的杠杆时，用放得下的最高值。"""
        pref = self.get_config().leverage_pref()
        if action.kind != "open" or not pref or not action.stop_loss or (action.leverage or 0) >= pref[0]:
            return action
        try:
            account, _, last = await self._context(action, env)
        except (OkxError, Rejected):
            return action
        if account.position(action.inst_id):  # 已有持仓：沿用这个仓位的杠杆
            return action
        entry = action.price if action.order_type == "limit" and action.price else last
        dist_pct = abs(entry - action.stop_loss) / entry * 100 if entry else 0
        if not dist_pct:
            return action
        safe = max(int(risk.LIQUIDATION_BUFFER_PCT // dist_pct), 1)
        target = pref[0] if action.leverage else (pref[0] + pref[1]) // 2
        return action.model_copy(update={"leverage": max(min(target, safe), action.leverage or 1)})

    async def execute(self, action: TradeAction, env: Env, *, source: str, preflight: Callable[[], None] | None = None, entry_only: bool = False) -> ActionResult:
        if source == "agent":
            action = await self._apply_prefs(action, env)
        async with self._lock(env, action.inst_id):
            try:
                if preflight:
                    preflight()
                if action.kind in ("open", "add"):
                    detail = await self._do_open(action, env, agent=source != "user", preflight=preflight, entry_only=entry_only)
                else:
                    detail = await getattr(self, f"_do_{action.kind}")(action, env)
            except Rejected as exc:
                return self._finish(env, action, "rejected", str(exc), source)
            except OkxError as exc:
                return self._finish(env, action, "failed", f"欧易返回错误：{exc}", source)
            message = detail.pop("message", "已执行")
            if detail.get("lot_id"):
                self.store.update_lot(detail["lot_id"], source=source)
            self._touch(env, action.inst_id)
            result = self._finish(env, action, "executed", message, source, detail=detail)
        self.bus.publish("account", None)
        return result

    def _finish(self, env: str, action: TradeAction, status: str, message: str, source: str, *, detail: dict[str, Any] | None = None, pending_id: int | None = None) -> ActionResult:
        result = ActionResult(status=status, message=message, action=action, detail=detail or {}, pending_id=pending_id)  # type: ignore[arg-type]
        entry = self.store.log(
            env,
            "action",
            describe(action),
            {"status": status, "message": message, "source": source, "action": action.model_dump(exclude_none=True), "detail": detail or {}, "pending_id": pending_id},
            inst_id=action.inst_id,
        )
        self.bus.publish("journal", entry)
        return result

    # ---------- 准备 ----------

    async def _context(self, action: TradeAction, env: Env) -> tuple[AccountState, Any, float]:
        account, inst, ticker = await asyncio.gather(
            self.account(env), self.market.instrument(action.inst_id, env), self.market.ticker(action.inst_id, env)
        )
        if account.account_level == "1":
            raise Rejected("你的欧易账户是「现货模式」，不能交易合约。请在欧易 App 的交易设置里切换到合约模式。")
        return account, inst, _f(ticker.get("last"))

    async def _leverage(self, inst_id: str, mgn_mode: str, env: Env) -> float:
        rows = await self.gw.call("swap_get_leverage", {"instId": inst_id, "mgnMode": mgn_mode}, env=env)
        return _f(rows[0].get("lever"), 1) if rows else 1

    def _adds_key(self, env: str, inst_id: str) -> str:
        return f"adds:{env}:{inst_id}"

    def order_guards(self, env: str, inst_id: str) -> list[dict[str, Any]]:
        """Agent 挂的限价单各自的撤单条件（撤单线、有效期）：盯盘程序每个节拍核对，到了就撤。"""
        return self.store.get(f"order_guard:{env}:{inst_id}", [])

    @staticmethod
    def _check_cancel_line(action: TradeAction, last: float) -> None:
        """撤单线必须在「价格不经过挂单价就能碰到」的一侧，否则永远不会在成交前触发。"""
        if action.order_type != "limit" or not action.cancel_px or not last:
            return
        if (action.side == "long") != (action.cancel_px > last):
            side = "高于" if action.side == "long" else "低于"
            raise Rejected(f"撤单线 {action.cancel_px:g} 放错了边：{'做多' if action.side == 'long' else '做空'}的限价单要等价格回来才成交，撤单线要{side}现价 {last:g}（价格先走到那里说明机会没了）。往成交方向的保护用止损。")

    def check_goal_budget(self, env: str, inst_id: str) -> None:
        goal = self.store.get(f"goal:{env}:{inst_id}") or {}
        if goal.get("status") in ("stopped", "done"):
            raise Rejected(f"目标已结束（{goal.get('note', '')}），不能开仓或加仓，等用户给新的目标。")

    def _adds_done(self, env: str, pos: Position | None) -> int:
        if not pos:
            return 0
        rec = self.store.get(self._adds_key(env, pos.inst_id)) or {}
        return int(rec.get("count", 0)) if rec.get("opened_at") == pos.opened_at else 0

    async def _dry_run(self, action: TradeAction, env: Env) -> risk.SizedOrder | None:
        """预检：过一遍风控但不下单。开仓/加仓会返回算好的数量，用来给用户看。"""
        cfg = self.get_config()
        account, inst, last = await self._context(action, env)
        pos = account.position(action.inst_id)
        if action.kind in ("open", "add"):
            self.check_goal_budget(env, action.inst_id)
            self._check_cancel_line(action, last)
            lever = await self._leverage(action.inst_id, pos.margin_mode if pos else (action.margin_mode or cfg.margin_mode), env)
            return risk.size_entry(action, account=account, inst=inst, limits=cfg.risk, last_px=last, current_leverage=lever, adds_done=self._adds_done(env, pos))
        if action.kind == "set_stops":
            if not pos:
                raise Rejected(f"{action.inst_id} 没有持仓，没法设置止盈止损。")
            risk.check_stop_update(pos, action.stop_loss, action.take_profit, account=account, inst=inst, limits=cfg.risk, last_px=last)
        elif action.kind == "set_leverage":
            self._check_leverage(action, inst)
        return None

    async def propose(self, action: TradeAction, *, entry_low: float | None = None, entry_high: float | None = None, take_profits: list[float] | None = None) -> ActionResult:
        """把开仓/加仓做成一份「方案」：算好数量和风险，等用户点确认后才下单。"""
        cfg = self.get_config()
        env = cfg.env
        try:
            sized = await self._dry_run(action, env)
        except (Rejected, OkxError) as exc:
            return self._finish(env, action, "rejected", str(exc), "agent")
        assert sized is not None
        targets = sorted({float(t) for t in (take_profits or []) + ([action.take_profit] if action.take_profit else [])}, reverse=action.side == "short")
        reward = abs(targets[0] - sized.entry) if targets else 0.0
        stop_dist = abs(sized.entry - sized.stop_loss)
        preview = {
            "contracts": fmt(sized.contracts),
            "leverage": sized.leverage,
            "entry": sized.entry,
            "entry_low": min(entry_low, entry_high) if entry_low and entry_high else sized.entry,
            "entry_high": max(entry_low, entry_high) if entry_low and entry_high else sized.entry,
            "stop_loss": sized.stop_loss,
            "take_profits": targets,
            "notional_usd": sized.notional_usd,
            "margin_usd": sized.margin_usd,
            "risk_usd": sized.risk_usd,
            "reward_risk": round(reward / stop_dist, 2) if stop_dist and reward else None,
            "margin_mode": action.margin_mode or cfg.margin_mode,
            "notes": sized.notes,
        }
        self.store.expire_pending(env, PENDING_TTL_MS)
        pending_id = self.store.add_pending(env, action.model_dump(), preview)
        result = self._finish(
            env, action, "pending",
            f"方案已生成：{'做多' if action.side == 'long' else '做空'}，投入约 {sized.margin_usd:g} USDT，{sized.leverage}x（名义约 {sized.notional_usd:g} USDT），"
            f"止损 {sized.stop_loss:g}，最多亏约 {sized.risk_usd:g} USDT。等用户点「同意执行」后才会下单（10 分钟内有效）。",
            "agent", detail=preview, pending_id=pending_id,
        )
        self.bus.publish("pending", {"id": pending_id})
        return result

    def _check_leverage(self, action: TradeAction, inst: Any) -> int:
        if not action.leverage:
            raise Rejected("调整杠杆必须给出倍数。")
        ceiling = int(min(self.get_config().risk.max_leverage, inst.max_leverage))
        if action.leverage > ceiling:
            raise Rejected(f"杠杆 {action.leverage}x 超过上限 {ceiling}x。")
        return action.leverage

    def _pos_side(self, account: AccountState, side: str) -> str | None:
        return side if account.position_mode == "long_short_mode" else None

    # ---------- 各类动作 ----------

    async def _do_set_leverage(self, action: TradeAction, env: Env) -> dict[str, Any]:
        cfg = self.get_config()
        account, inst, _ = await self._context(action, env)
        risk.check_instrument(inst)
        lever = self._check_leverage(action, inst)
        pos = account.position(action.inst_id)
        mgn = pos.margin_mode if pos else cfg.margin_mode
        await self._set_leverage(action.inst_id, lever, mgn, account, pos.side if pos else action.side, env)
        return {"message": f"{action.inst_id} 杠杆已调整为 {lever}x（{mgn}）", "leverage": lever}

    async def _set_leverage(self, inst_id: str, lever: int, mgn: str, account: AccountState, side: str | None, env: Env) -> None:
        args: dict[str, Any] = {"instId": inst_id, "lever": str(lever), "mgnMode": mgn}
        if mgn == "isolated" and account.position_mode == "long_short_mode":
            args["posSide"] = side or "long"
        await self._call("swap_set_leverage", args, env)

    async def _scalp_quote(self, action: TradeAction, env: Env, account: AccountState) -> float:
        inst = action.inst_id
        ticker = await self.market.ticker(inst, env)
        cfg = self.get_config()
        row = self.store.get_strategy(action.scalp_plan_id)
        if (env != "demo" or cfg.env != env or not row or row["env"] != env or row["inst_id"] != inst
                or row["status"] != "armed" or not row["params"].get("scalp_plan") or row["side"] != action.side
                or row["kind"] not in ("breakout", "pullback") or inst not in cfg.instruments or cfg.style_for(inst) != "scalp"
                or cfg.worker(inst).paused or account.position(inst) or any(o.inst_id == inst for o in account.orders)
                or self.illiquid.get(f"{env}:{inst}")):
            raise Rejected("超短线预案已失效、已有持仓或挂单、或流动性不足。")
        permission = cfg.permission_for("open", inst)
        if permission == "off" or (permission == "confirm" and not row["state"].get("approved")):
            raise Rejected("当前权限不允许直接按该预案开仓。")
        p = row["params"]
        long = row["side"] == "long"
        q = quote(ticker, not long, time.time())
        if not q or now_ms() >= p["expires_ts"]:
            raise Rejected("预案或买卖报价已过期。")
        px = q[0]
        t = p["trigger"]
        direction = 1 if (long == (row["kind"] == "breakout")) else -1
        last = _f(ticker.get("last"))
        if (not 0 < last < float("inf") or min((px - t) * direction, (last - t) * direction) < 0
                or abs(px / t - 1) > 0.004 or float(ticker["askPx"]) / float(ticker["bidPx"]) - 1 > 0.001):
            raise Rejected("价格已收回、偏离触发位超过 0.4%，或点差超过 0.1%，放弃这次入场。")
        return px

    async def _do_open(self, action: TradeAction, env: Env, *, agent: bool = True, preflight: Callable[[], None] | None = None, entry_only: bool = False) -> dict[str, Any]:
        if agent:
            self.check_goal_budget(env, action.inst_id)
        cfg = self.get_config()
        account, inst, last = await self._context(action, env)
        pos = account.position(action.inst_id)
        if (entry_only or action.require_flat) and (pos or any(o.inst_id == action.inst_id and not o.reduce_only for o in account.orders)):
            raise Rejected("已有持仓或入场委托，原开仓规则不再执行")
        mgn = pos.margin_mode if pos else (action.margin_mode or cfg.margin_mode)
        current = await self._leverage(action.inst_id, mgn, env)
        adds_done = self._adds_done(env, pos)
        style = action.style or cfg.style_for(action.inst_id)
        self._check_cancel_line(action, last)
        if action.scalp_plan_id:
            last = await self._scalp_quote(action, env, account)
        existing = None
        booked = [l for l in self.store.lots(env, action.inst_id) if l["status"] == "open"] if pos else []
        if pos and booked and action.stop_loss and abs(sum(l["contracts"] for l in booked) - pos.contracts) < 1e-6:
            existing = 0.0
            for row in booked:  # 同风格的旧仓位止损会跟着收紧到新止损，别的风格按它自己的止损算
                stop = row["stop_loss"] or action.stop_loss
                if row["style"] == style and (action.stop_loss > stop if pos.side == "long" else action.stop_loss < stop):
                    stop = action.stop_loss
                existing += row["contracts"] * inst.ct_val * ((row["entry_px"] - stop) if pos.side == "long" else (stop - row["entry_px"]))
        sized = risk.size_entry(action, account=account, inst=inst, limits=cfg.risk, last_px=last, current_leverage=current, adds_done=adds_done, existing_risk=existing)
        assert action.side is not None

        if int(current) != sized.leverage:
            if preflight:
                preflight()
            if agent:
                self.check_goal_budget(env, action.inst_id)
            if action.scalp_plan_id and self.get_config().permission_for("set_leverage", action.inst_id) != "auto":
                raise Rejected("预案需要调杠杆，但当前未授权自动调整。")
            await self._set_leverage(action.inst_id, sized.leverage, mgn, account, action.side, env)
            if action.scalp_plan_id:
                last = await self._scalp_quote(action, env, account)
                sized = risk.size_entry(action, account=account, inst=inst, limits=self.get_config().risk, last_px=last, current_leverage=sized.leverage)

        sl = round_price(sized.stop_loss, inst.tick_sz)
        tp = round_price(sized.take_profit, inst.tick_sz) if sized.take_profit else None
        order: dict[str, Any] = {
            "instId": action.inst_id,
            "tdMode": mgn,
            "side": "buy" if action.side == "long" else "sell",
            "posSide": self._pos_side(account, action.side),
            "ordType": action.order_type,
            "sz": fmt(sized.contracts),
            "px": round_price(sized.entry, inst.tick_sz) if action.order_type == "limit" else None,
        }
        # 每一笔（开仓和加仓都一样）自带按它自己张数挂的止损止盈，成交即生效；不动这个币上别的几笔
        order.update({"slTriggerPx": sl, "slOrdPx": "-1"})
        if tp:
            order.update({"tpTriggerPx": tp, "tpOrdPx": "-1"})
        if agent:
            self.check_goal_budget(env, action.inst_id)
        if preflight:
            preflight()
        if action.scalp_plan_id and not self.store.claim_demo_order(action.scalp_plan_id):
            raise Rejected("预案已经提交过，不重复开仓。")
        try:
            rows = await self._call("swap_place_order", order, env)
        except OkxError as exc:
            # 小市值/盘前合约在当前杠杆档位有张数上限（欧易 51004）：自动压到档位剩余可开张数重试一次，只减风险不增风险
            m = re.search(r"more than ([0-9.]+)\s*\(contracts\)", str(exc))
            if exc.code != "51004" or not m:
                raise
            max_cap = float(m.group(1))
            same_side = "buy" if action.side == "long" else "sell"
            pending_sz = sum(o.sz - o.filled for o in account.orders if o.inst_id == action.inst_id and o.side == same_side and not o.reduce_only)
            held_sz = pos.contracts if pos and pos.side == action.side else 0.0
            room_sz = floor_to_step(max(max_cap - held_sz - pending_sz, 0.0), inst.lot_sz)
            if room_sz < Decimal(inst.min_sz) or room_sz >= sized.contracts:
                raise
            qty = float(room_sz)
            sized.contracts = room_sz
            sized.notional_usd = round(qty * inst.ct_val * sized.entry, 2)
            sized.margin_usd = round(sized.notional_usd / sized.leverage, 2)
            sized.risk_usd = round(qty * abs(sized.entry - sized.stop_loss) * inst.ct_val, 4)
            sized.notes.append(f"数量受交易所当前杠杆档位上限（{max_cap:g} 张）限制")
            order["sz"] = fmt(room_sz)
            rows = await self._call("swap_place_order", order, env)
        ord_id = str(rows[0].get("ordId") or "") if rows else ""
        if pos:
            # 顺势加仓：同一种风格的旧仓位，止损跟着收紧到新止损（只收紧不放宽）；别的风格的几笔不动
            for row in self.store.lots(env, action.inst_id):
                if row["status"] != "open" or row["style"] != style or row["side"] != action.side:
                    continue
                tighter = not row["stop_loss"] or (float(sl) > row["stop_loss"] if action.side == "long" else float(sl) < row["stop_loss"])
                if not tighter:
                    continue
                mine = await self._lot_algo(row, pos, env)
                keep_tp = (mine or {}).get("tpTriggerPx") or (str(row["take_profit"]) if row["take_profit"] else None)
                new_id = await self._place_sized(pos, row["contracts"], sl, keep_tp, env)
                if mine:
                    await self._cancel_algos(action.inst_id, [mine["algoId"]], env)
                self.store.update_lot(row["id"], stop_loss=float(sl), algo_id=new_id)
        lot_id = self.store.add_lot(
            env, action.inst_id, action.side, style, float(sized.contracts), float(order["px"] or last or sized.entry),
            stop_loss=float(sl), take_profit=float(tp) if tp else None, ord_id=ord_id, status="working" if action.order_type == "limit" else "open",
        )
        if pos:
            self.store.set(self._adds_key(env, action.inst_id), {"opened_at": pos.opened_at, "count": adds_done + 1})
        guard = ""
        if action.order_type == "limit" and ord_id:
            row = {
                "ord_id": ord_id, "lot_id": lot_id, "side": action.side, "price": float(order["px"]), "ts": now_ms(),
                "cancel_px": action.cancel_px, "expires_ts": now_ms() + action.expires_minutes * 60_000 if action.expires_minutes else None,
            }
            if action.cancel_before_entry:
                row["cancel_before_entry"] = action.cancel_before_entry.model_dump()
                row["cancel_before_entry"]["bar"] = action.cancel_before_entry.bar or STYLES[style]["struct_bar"]
            self.store.set(f"order_guard:{env}:{action.inst_id}", [*self.order_guards(env, action.inst_id), row][-20:])
            conds = [f"价格先到 {action.cancel_px:g}" if action.cancel_px else "", f"{action.expires_minutes} 分钟没成交" if action.expires_minutes else ""]
            guard = f"。程序盯着这张单：{'、或'.join(c for c in conds if c)}就自动撤" if any(conds) else ""
        verb = "加仓" if pos else "开仓"
        note = f"（{'；'.join(sized.notes)}）" if sized.notes else ""
        return {
            "message": f"{verb}已提交：{'做多' if action.side == 'long' else '做空'} {action.inst_id}，投入约 {sized.margin_usd:g} USDT，"
            f"{sized.leverage}x {'逐仓' if mgn == 'isolated' else '全仓'}（名义价值约 {sized.notional_usd:g} USDT），止损 {sl}，打到止损最多亏约 {sized.risk_usd:g} USDT{note}{guard}",
            "ordId": rows[0].get("ordId") if rows else None,
            "contracts": fmt(sized.contracts),
            "leverage": sized.leverage,
            "notional_usd": sized.notional_usd,
            "margin_usd": sized.margin_usd,
            "margin_mode": mgn,
            "risk_usd": sized.risk_usd,
            "stop_loss": sl,
            "take_profit": tp,
            "style": style,
            "lot_id": lot_id,
        }

    async def _protections(self, pos: Position, env: Env) -> list[dict[str, Any]]:
        rows = await self.gw.call("swap_get_algo_orders", {"status": "pending", "instId": pos.inst_id}, env=env)
        return [
            r for r in rows or []
            if (r.get("slTriggerPx") or r.get("tpTriggerPx")) and (r.get("posSide") or "net") == pos.pos_side
            and r.get("side") == ("sell" if pos.side == "long" else "buy")
        ]

    async def _amend_protection(self, inst_id: str, old: dict[str, Any], sl: str | None, tp: str | None, env: Env) -> dict[str, Any]:
        oid = old.get("algoId")
        if not oid:
            raise Rejected("保护单缺少编号，未撤单或新挂，请先核对。")
        args = {"instId": inst_id, "algoId": oid}
        for leg, price in (("Sl", sl), ("Tp", tp)):
            key = leg.lower()
            if price is not None and _f(price) != _f(old.get(key + "TriggerPx")):
                args.update({f"new{leg}TriggerPx": price, f"new{leg}OrdPx": old.get(key + "OrdPx") or "-1"})
        if len(args) > 2:
            rows = await self._call("swap_amend_algo_order", args, env)
            if not any(r.get("algoId") == oid and str(r.get("sCode")) == "0" for r in rows or []):
                raise Rejected(f"保护单 {oid} 修改回执未核实，请先核对，不重挂。")
        return {"algo_id": oid, "stop_loss": sl or old.get("slTriggerPx"), "take_profit": tp or old.get("tpTriggerPx")}

    async def _replace_stops(self, pos: Position, account: AccountState, sl: str | None, tp: str | None, env: Env) -> dict[str, Any]:
        """已有整仓保护时原位修改；不能为换价先撤掉保护。"""
        old = await self._protections(pos, env)
        whole = [r for r in old if _f(r.get("closeFraction")) == 1 or not _f(r.get("sz"))]
        if whole:
            if len(whole) != 1 or (len(old) > 1 and not (sl and tp and _f(whole[0].get("closeFraction")) == 1)):
                raise Rejected("整仓和其他保护单并存，无法安全替换；保留原单，请按仓位核对。")
            applied = await self._amend_protection(pos.inst_id, whole[0], sl, tp, env)
            leftovers = [{"instId": pos.inst_id, "algoId": r["algoId"]} for r in old if r["algoId"] != applied["algo_id"]]
            if leftovers:  # 明确同时指定整仓 SL/TP；整仓保护先就位，再清除可识别的分笔旧单
                await self._call("swap_cancel_algo_orders", {"orders": leftovers}, env)
            return applied
        if sl is None:
            sl = next((r["slTriggerPx"] for r in old if r.get("slTriggerPx")), None)
        if tp is None:
            tp = next((r["tpTriggerPx"] for r in old if r.get("tpTriggerPx")), None)
        if not sl and not tp:
            raise Rejected("至少要给出止损或止盈其中一个价格。")
        args: dict[str, Any] = {
            "instId": pos.inst_id,
            "tdMode": pos.margin_mode,
            "side": "sell" if pos.side == "long" else "buy",
            "posSide": pos.pos_side,
            "ordType": "oco" if sl and tp else "conditional",
            "closeFraction": "1",
            "cxlOnClosePos": True,
        }
        if pos.pos_side == "net":
            args["reduceOnly"] = True
        if sl:
            args.update({"slTriggerPx": sl, "slOrdPx": "-1"})
        if tp:
            args.update({"tpTriggerPx": tp, "tpOrdPx": "-1"})
        await self._call("swap_place_algo_order", args, env)
        if old:
            try:
                await self._call("swap_cancel_algo_orders", {"orders": [{"algoId": r["algoId"], "instId": pos.inst_id} for r in old]}, env)
            except OkxError as exc:
                self.bus.publish("journal", self.store.log(env, "alert", f"{pos.inst_id} 旧的止盈止损单没撤掉", {"message": str(exc)}, inst_id=pos.inst_id))
        return {"stop_loss": sl, "take_profit": tp}

    async def _do_set_stops(self, action: TradeAction, env: Env) -> dict[str, Any]:
        account, inst, last = await self._context(action, env)
        pos = account.position(action.inst_id)
        if not pos:
            raise Rejected(f"{action.inst_id} 没有持仓，没法设置止盈止损。")
        if action.stop_loss is None and action.take_profit is None:
            raise Rejected("至少要给出止损或止盈其中一个价格。")
        lot = self._lot(action, env)
        if lot and len([l for l in self.store.lots(env, action.inst_id) if l["status"] == "open"]) > 1:
            return await self._set_lot_stops(lot, pos, action, inst, last, env)
        risk.check_stop_update(pos, action.stop_loss, action.take_profit, account=account, inst=inst, limits=self.get_config().risk, last_px=last)
        sl = round_price(action.stop_loss, inst.tick_sz) if action.stop_loss else None
        tp = round_price(action.take_profit, inst.tick_sz) if action.take_profit else None
        applied = await self._replace_stops(pos, account, sl, tp, env)
        for row in self.store.lots(env, action.inst_id):
            if row["status"] == "open":
                self.store.update_lot(row["id"], stop_loss=_f(applied["stop_loss"]) or None, take_profit=_f(applied["take_profit"]) or None, algo_id="")
        return {"message": f"{action.inst_id} 整仓止损 {applied['stop_loss'] or '未设'}，止盈 {applied['take_profit'] or '未设'}", **applied}

    async def _set_lot_stops(self, lot: dict[str, Any], pos: Position, action: TradeAction, inst: Any, last: float, env: Env) -> dict[str, Any]:
        """只改这个币的某一笔的止盈止损，别的几笔不动。"""
        risk.check_stop_side(pos.side, last, action.stop_loss, action.take_profit, what="现价")
        if action.stop_loss is not None and lot["stop_loss"]:
            looser = action.stop_loss < lot["stop_loss"] if pos.side == "long" else action.stop_loss > lot["stop_loss"]
            if looser:
                raise Rejected(f"这一笔的止损现在是 {lot['stop_loss']:g}，只能收紧不能放宽；想给更多空间请先减仓。")
        sl = round_price(action.stop_loss, inst.tick_sz) if action.stop_loss else (str(lot["stop_loss"]) if lot["stop_loss"] else None)
        tp = round_price(action.take_profit, inst.tick_sz) if action.take_profit else (str(lot["take_profit"]) if lot["take_profit"] else None)
        old = await self._protections(pos, env)
        mine = await self._lot_algo(lot, pos, env)
        whole = [r for r in old if _f(r.get("closeFraction")) == 1 or not _f(r.get("sz"))]
        if mine and not whole and mine.get("algoId") == lot["algo_id"] and abs(_f(mine.get("sz")) - lot["contracts"]) < 1e-9:
            applied = await self._amend_protection(pos.inst_id, mine, sl if action.stop_loss else None, tp if action.take_profit else None, env)
            self.store.update_lot(lot["id"], stop_loss=_f(applied["stop_loss"]) or None, take_profit=_f(applied["take_profit"]) or None)
            return {"message": f"{action.inst_id} 第 {lot['id']} 笔保护单 {mine['algoId']}：止损 {applied['stop_loss']}，止盈 {applied['take_profit']}", **applied}
        new_id = await self._place_sized(pos, lot["contracts"], sl, tp, env)
        drop = [mine["algoId"]] if mine else []
        if whole:  # 原来是一张整仓的止盈止损：拆成每笔各一张，别的几笔保持原价位
            for other in self.store.lots(env, pos.inst_id):
                if other["status"] == "open" and other["id"] != lot["id"]:
                    osl, otp = whole[0].get("slTriggerPx") or None, whole[0].get("tpTriggerPx") or None
                    oid = await self._place_sized(pos, other["contracts"], osl, otp, env)
                    self.store.update_lot(other["id"], algo_id=oid, stop_loss=_f(osl) or None, take_profit=_f(otp) or None)
            drop += [r["algoId"] for r in whole]
        await self._cancel_algos(pos.inst_id, list(dict.fromkeys(drop)), env)
        self.store.update_lot(lot["id"], stop_loss=_f(sl) or None, take_profit=_f(tp) or None, algo_id=new_id)
        return {"message": f"{action.inst_id} 第 {lot['id']} 笔（{lot['contracts']:g} 张）止损 {sl or '未设'}，止盈 {tp or '未设'}", "stop_loss": sl, "take_profit": tp}

    async def _do_reduce(self, action: TradeAction, env: Env) -> dict[str, Any]:
        account, inst, _ = await self._context(action, env)
        pos = account.position(action.inst_id)
        if not pos:
            raise Rejected(f"{action.inst_id} 没有持仓可减。")
        lot = self._lot(action, env)
        fraction = action.fraction or (1.0 if lot else 0.5)
        qty = risk.floor_to_step((lot["contracts"] if lot else pos.contracts) * fraction, inst.lot_sz)
        remaining = pos.contracts - float(qty)
        if (fraction >= 0.999 and not lot) or remaining < float(inst.min_sz) or qty < risk.Decimal(inst.min_sz):
            return await self._close(pos, account, env)
        rows = await self._call(
            "swap_place_order",
            {
                "instId": pos.inst_id,
                "tdMode": pos.margin_mode,
                "side": "sell" if pos.side == "long" else "buy",
                "posSide": self._pos_side(account, pos.side),
                "ordType": "market",
                "sz": fmt(qty),
                "reduceOnly": True if account.position_mode != "long_short_mode" else None,
            },
            env,
        )
        if lot:
            await self._resize_lot(lot, pos, lot["contracts"] - float(qty), env)
        else:  # 没指定哪一笔：先减持仓时间最短的风格，同风格先减最新的一笔
            gone = float(qty)
            for row in sorted([l for l in self.store.lots(env, pos.inst_id) if l["status"] == "open"], key=lambda l: (STYLE_ORDER.get(l["style"], 1), -l["id"])):
                if gone <= 1e-9:
                    break
                cut = min(row["contracts"], gone)
                gone -= cut
                await self._resize_lot(row, pos, row["contracts"] - cut, env)
        which = f"第 {lot['id']} 笔 " if lot else ""
        return {"message": f"已市价减仓 {which}{pos.inst_id} 约 {fraction * 100:.0f}%（释放保证金约 {pos.margin * float(qty) / pos.contracts:.2f} USDT），整个持仓还投入约 {pos.margin * float(remaining) / pos.contracts:.2f} USDT", "ordId": rows[0].get("ordId") if rows else None, "contracts": fmt(qty)}

    async def _still_open(self, inst_id: str, env: Env, tries: int = 3) -> Position | None:
        """平仓单提交之后去欧易核对：仓位真的没了返回 None，还在就返回它。"""
        left: Position | None = None
        for i in range(tries):
            left = (await self.account(env)).position(inst_id)
            if not left:
                return None
            if i < tries - 1:
                await asyncio.sleep(self.settle_seconds)
        return left

    async def _close_at_touch(self, left: Position, account: AccountState, env: Env) -> float | None:
        """市价平仓单没成交（对手盘不够，模拟盘的小币常见）：撤掉它，改挂一张贴着对手价的限价平仓单，能成交多少先成交多少。"""
        inst = await self.market.instrument(left.inst_id, env)
        book = await self.market.book(left.inst_id, env, 5)
        levels = book["asks"] if left.side == "short" else book["bids"]
        if not levels:
            return None
        px = levels[min(2, len(levels) - 1)][0]  # 往里吃两档，尽量一次成交
        for row in await self.gw.call("swap_get_orders", {"status": "open", "instId": left.inst_id}, env=env) or []:
            if str(row.get("reduceOnly", "")).lower() == "true":
                try:
                    await self._call("swap_cancel_order", {"instId": left.inst_id, "ordId": row["ordId"]}, env)
                except OkxError:
                    pass
        await self._call(
            "swap_place_order",
            {
                "instId": left.inst_id, "tdMode": left.margin_mode, "side": "buy" if left.side == "short" else "sell",
                "posSide": self._pos_side(account, left.side), "ordType": "limit", "px": round_price(px, inst.tick_sz),
                "sz": fmt(risk.floor_to_step(left.contracts, inst.lot_sz)), "reduceOnly": True if account.position_mode != "long_short_mode" else None,
            },
            env,
        )
        return px

    async def _close(self, pos: Position, account: AccountState, env: Env) -> dict[str, Any]:
        await self._call(
            "swap_close_position",
            {"instId": pos.inst_id, "mgnMode": pos.margin_mode, "posSide": self._pos_side(account, pos.side), "autoCxl": True},
            env,
        )
        left = await self._still_open(pos.inst_id, env)
        if left:  # 平仓单提交了但仓位还在：不能说「已平仓」，也不能动这笔仓的记录
            limit_px: float | None = None
            try:
                limit_px = await self._close_at_touch(left, account, env)
            except (OkxError, Rejected):
                pass
            if limit_px:
                left = await self._still_open(pos.inst_id, env, tries=2)
        if left:
            where = "模拟盘里这个币的对手盘不够" if env == "demo" else "对手盘不够"
            how = f"已改挂一张限价 {limit_px:g} 的平仓单" if limit_px else "平仓单还挂着"
            return {
                "message": f"平仓单已提交，但还没成交：{pos.inst_id} 还有约 {left.margin:.2f} USDT 的仓位没平掉（{where}）。{how}，成交后仓位才会消失，不用重复点平仓。",
                "unfilled": True, "left": left.contracts, "upl": left.upl,
            }
        self.store.delete(self._adds_key(env, pos.inst_id))
        for row in self.store.lots(env, pos.inst_id):
            if row["status"] == "open":
                self.store.update_lot(row["id"], status="closed", note="已平仓")
        leftovers = [r for r in await self._protections(pos, env)]
        if leftovers:
            try:
                await self._call("swap_cancel_algo_orders", {"orders": [{"algoId": r["algoId"], "instId": pos.inst_id} for r in leftovers]}, env)
            except OkxError:
                pass  # 仓位已平，这些单多半已被欧易自动撤掉
        return {"message": f"已市价全平 {pos.inst_id}（投入 {pos.margin:.2f} USDT，平仓前浮盈亏 {pos.upl:+.2f} USDT）", "upl": pos.upl}

    async def _do_close(self, action: TradeAction, env: Env) -> dict[str, Any]:
        if action.profit_guard_id is not None:
            return await self._profit_exit(action, env)
        account, _, _ = await self._context(action, env)
        pos = account.position(action.inst_id)
        if not pos:
            raise Rejected(f"{action.inst_id} 没有持仓。")
        waiting = [o for o in account.orders if o.inst_id == action.inst_id and o.reduce_only and o.created_at and now_ms() - o.created_at < 20_000]
        if waiting:  # 刚提交的平仓单还挂着：再点只会撤了重挂，不会更快
            secs = int((now_ms() - max(o.created_at for o in waiting)) / 1000)
            raise Rejected(f"{action.inst_id} 已经有一张平仓单在挂着（{secs} 秒前提交，还没成交），不用重复点。它成交后仓位就会消失。")
        lot = self._lot(action, env)
        if lot and len([l for l in self.store.lots(env, action.inst_id) if l["status"] == "open"]) > 1:
            return await self._do_reduce(action.model_copy(update={"kind": "reduce", "fraction": 1.0}), env)
        return await self._close(pos, account, env)

    async def _profit_exit(self, action: TradeAction, env: Env) -> dict[str, Any]:
        if env != "demo" or self.get_config().env != env:
            raise Rejected("提前止盈试验仅在模拟盘可用。")
        inst_id, sid = action.inst_id, action.profit_guard_id
        account, inst, ticker = await asyncio.gather(self.account(env), self.market.instrument(inst_id, env), self.market.ticker(inst_id, env))
        row = self.store.get_strategy(sid)
        if (not row or row["status"] != "active" or row["kind"] != "profit_guard" or row["env"] != env
                or row["inst_id"] != inst_id or not action.lot_id or row["params"]["lot_id"] != action.lot_id):
            raise Rejected("分笔止盈规则已撤销或不匹配。")
        p, st = row["params"], row["state"]
        lot, pos = self._lot(action, env), account.position(inst_id)
        others = [l for l in self.store.lots(env, inst_id) if l["status"] == "open" and l["id"] != action.lot_id]
        if (not lot or not pos or pos.side != lot["side"] or pos.side != row["side"] or lot["entry_px"] != p["entry"] or lot["take_profit"] != p["lot_target"]
                or p["on_trigger"] != "close" or lot["contracts"] != st.get("contracts")
                or abs(pos.contracts - lot["contracts"] - sum(l["contracts"] for l in others)) > 1e-8
                or any(l["side"] != pos.side for l in others)):
            raise Rejected("分笔记录与交易所不一致，不提交。")
        if any(o.inst_id == inst_id and (o.reduce_only or o.side == ("sell" if p["long"] else "buy")) for o in account.orders):
            raise Rejected("已有同方向委托，先核对，不重复平仓。")
        if len([v for v in account.positions if v.inst_id == inst_id]) != 1:
            raise Rejected("同币存在多条交易所持仓，无法唯一核对。")
        def protected(l: dict) -> bool:
            return any(a.inst_id == inst_id and a.algo_id == l["algo_id"] and abs(_f(a.sz) - l["contracts"]) < 1e-9
                       and a.stop_loss and a.side == ("sell" if p["long"] else "buy") for a in account.protections)
        if others and (not all(protected(l) for l in [lot, *others]) or any(l["algo_id"] == lot["algo_id"] for l in others)):
            raise Rejected("不能唯一确认这笔仓的保护单，不动同币其他仓位。")
        cfg = self.get_config()
        permission = cfg.permission_for("close", inst_id)
        confirmation = self.store.get_pending(st["pending_id"]) if st.get("pending_id") else None
        approved = bool(confirmation and confirmation["status"] == "running" and confirmation["action"].get("profit_guard_id") == sid)
        if (cfg.env != env or inst_id not in cfg.instruments or cfg.worker(inst_id).paused or permission == "off" or
                ((permission != "auto" or (cfg.hands_off_manual and lot["source"] in MANUAL_SOURCES)) and not approved)):
            raise Rejected("当前权限不允许，或缺少这笔仓的平仓确认。")
        now = time.time()
        q = quote(ticker, p["long"], now)
        if not st.get("claimed") or now - st.get("trigger_ts", 0) > 45 or not q or not signal(p, dict(st), *q):
            raise Rejected("平仓前复核：止盈信号已过期、消失或报价无效，保留原保护单。")
        risk.check_instrument(inst)
        qty = risk.floor_to_step(lot["contracts"], inst.lot_sz)
        if qty < risk.Decimal(inst.min_sz) or abs(float(qty) - lot["contracts"]) > 1e-9:
            raise Rejected("这笔仓位数量不符合交易所要求，不改为平整仓。")
        # 只允许以当前对手价或更好的价格整笔成交；绝不追价，也不撤原保护单等成交。
        px = round_price(q[0], inst.tick_sz)
        if abs(float(px) - q[0]) > float(inst.tick_sz) * 0.0001:
            raise Rejected("对手价不符合价格精度，不提交。")
        if not self.store.claim_demo_order(sid):
            raise Rejected("此规则已提交过平仓请求，先核对，不重试。")
        rows = await self._call("swap_place_order", {
            "instId": inst_id, "tdMode": pos.margin_mode, "side": "sell" if p["long"] else "buy",
            "posSide": self._pos_side(account, pos.side), "reduceOnly": True if pos.pos_side == "net" else None,
            "ordType": "fok", "sz": fmt(qty), "px": px,
        }, env)
        oid = str(rows[0].get("ordId") or "") if rows else ""
        if not oid:
            raise Rejected("未拿到委托编号，成交未知；不重试，不撤保护单。")
        for attempt in range(3):
            receipt = await self._call("swap_get_order", {"instId": inst_id, "ordId": oid}, env)
            fill = receipt[0] if receipt else {}
            if str(fill.get("ordId")) == oid:
                if fill.get("state") == "canceled":
                    raise Rejected(f"限价止盈单 {oid} 未成交已撤销，不追价，保留原保护单。")
                if (fill.get("state") == "filled" and abs(_f(fill.get("accFillSz")) - float(qty)) < 1e-9
                        and _f(fill.get("avgPx")) > 0):
                    self.store.update_lot(lot["id"], status="closed", note=f"提前止盈 {oid}")
                    algo = lot.get("algo_id")
                    if algo and not any(l["algo_id"] == algo for l in others):
                        await self._cancel_algos(inst_id, [algo], env)
                    return {"message": f"已核对第 #{lot['id']} 笔全部成交，成交均价 {fill['avgPx']}。", "ordId": oid, "avgPx": fill["avgPx"]}
            if attempt < 2:
                await asyncio.sleep(self.settle_seconds)
        raise Rejected(f"委托 {oid} 尚未确认全成，先核对，不重复平仓、不撤保护单。")

    async def _do_cancel_orders(self, action: TradeAction, env: Env) -> dict[str, Any]:
        rows = await self.gw.call("swap_get_orders", {"status": "open", "instId": action.inst_id}, env=env)
        cancelled: list[str] = []
        for row in rows or []:
            if action.ord_id and str(row["ordId"]) != action.ord_id:
                continue
            await self._call("swap_cancel_order", {"instId": action.inst_id, "ordId": row["ordId"]}, env)
            cancelled.append(str(row["ordId"]))
        if cancelled:
            self.store.set(f"order_guard:{env}:{action.inst_id}", [g for g in self.order_guards(env, action.inst_id) if g["ord_id"] not in cancelled])
        return {"message": f"已撤销 {action.inst_id} 的 {len(cancelled)} 个挂单", "cancelled": len(cancelled)}

    async def panic(self, env: Env) -> list[ActionResult]:
        """一键跑路：撤掉全部挂单，市价平掉全部合约持仓。"""
        account = await self.account(env)
        results: list[ActionResult] = []
        for inst_id in {o.inst_id for o in account.orders}:
            results.append(await self.execute(TradeAction(kind="cancel_orders", inst_id=inst_id, reason="一键全平"), env, source="user"))
        for pos in account.positions:
            results.append(await self.execute(TradeAction(kind="close", inst_id=pos.inst_id, reason="一键全平"), env, source="user"))
        return results
