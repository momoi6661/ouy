"""风控护栏：纯函数，决定一笔开仓/加仓能不能做、最多做多大。

核心规则：仓位大小由「止损距离」反推——整个持仓打到止损时的亏损不得超过权益的
risk_per_trade_pct。加仓同理：想加仓就必须先把止损往有利方向挪，腾出风险额度。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from .models import AccountState, Instrument, Position, RiskLimits, TradeAction

# 止损距离 × 杠杆 不得超过这个百分比，否则止损还没触发就先爆仓了
LIQUIDATION_BUFFER_PCT = 70.0
MIN_STOP_DISTANCE_PCT = 0.1


class Rejected(Exception):
    """风控拒绝。message 会原样返回给 Agent / 用户。"""


@dataclass
class SizedOrder:
    contracts: Decimal
    leverage: int
    entry: float
    stop_loss: float
    take_profit: float | None
    risk_usd: float
    notional_usd: float
    margin_usd: float = 0.0
    notes: list[str] = field(default_factory=list)


def floor_to_step(value: float | Decimal, step: str) -> Decimal:
    s = Decimal(step)
    return (Decimal(str(value)) / s).to_integral_value(rounding=ROUND_DOWN) * s


def round_price(value: float, tick: str) -> str:
    t = Decimal(tick)
    return format(((Decimal(str(value)) / t).to_integral_value(rounding=ROUND_HALF_UP) * t).normalize(), "f")


def fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


def position_risk(pos: Position, stop_loss: float, ct_val: float) -> float:
    """整个持仓在 stop_loss 被打掉时的亏损（负数表示已锁定利润）。"""
    diff = pos.avg_px - stop_loss if pos.side == "long" else stop_loss - pos.avg_px
    return pos.contracts * ct_val * diff


def check_instrument(inst: Instrument) -> None:
    if inst.inst_type != "SWAP" or inst.ct_type != "linear":
        raise Rejected(f"{inst.inst_id} 不是 U 本位永续合约。智能下单目前只做 U 本位永续，其他品种请用「欧易全功能」面板。")
    if inst.state != "live":
        raise Rejected(f"{inst.inst_id} 当前不可交易（状态 {inst.state}）。")


def check_stop_side(side: str, ref_px: float, stop_loss: float | None, take_profit: float | None, what: str = "入场价") -> None:
    if stop_loss is not None:
        if side == "long" and stop_loss >= ref_px:
            raise Rejected(f"做多的止损必须低于{what} {ref_px:g}，现在是 {stop_loss:g}。")
        if side == "short" and stop_loss <= ref_px:
            raise Rejected(f"做空的止损必须高于{what} {ref_px:g}，现在是 {stop_loss:g}。")
    if take_profit is not None:
        if side == "long" and take_profit <= ref_px:
            raise Rejected(f"做多的止盈必须高于{what} {ref_px:g}，现在是 {take_profit:g}。")
        if side == "short" and take_profit >= ref_px:
            raise Rejected(f"做空的止盈必须低于{what} {ref_px:g}，现在是 {take_profit:g}。")


def clamp_leverage(requested: int | None, current: float, limits: RiskLimits, inst: Instrument, notes: list[str]) -> int:
    ceiling = int(min(limits.max_leverage, inst.max_leverage))
    lever = requested if requested is not None else int(current or 1)
    if lever > ceiling:
        notes.append(f"杠杆 {lever}x 超过上限，已压到 {ceiling}x")
        lever = ceiling
    return max(lever, 1)


def size_entry(
    action: TradeAction,
    *,
    account: AccountState,
    inst: Instrument,
    limits: RiskLimits,
    last_px: float,
    current_leverage: float,
    adds_done: int = 0,
    existing_risk: float | None = None,
    manual: bool = False,
) -> SizedOrder:
    """给开仓/加仓定量。任何一条规则不满足就抛 Rejected，并说明怎么改。
    manual：用户自己在页面上下的单，不过风控（当日亏损上限、加仓次数、亏损加仓、止损最小距离、杠杆配止损、单仓风险、名义价值上限都不拦，
    数量照用户填的来）；只留不改就一定出错的：方向 / 止损必填、止损止盈放错边、反向持仓、保证金不够、小于最小下单量。"""
    check_instrument(inst)
    if account.halted and not manual:
        raise Rejected(f"今日亏损已达 {limits.max_daily_loss_pct:g}% 上限，今天只允许减仓和平仓。")
    if action.side is None:
        raise Rejected("开仓必须指定方向 long 或 short。")
    if action.stop_loss is None:
        raise Rejected("开仓/加仓必须带止损价，没有止损不下单。")
    if action.order_type == "limit" and action.price is None:
        raise Rejected("限价单必须给出价格。")

    entry = action.price if action.order_type == "limit" and action.price else last_px
    if action.order_type == "limit":
        if action.side == "long" and entry > last_px * 1.001:
            raise Rejected(f"做多限价 {entry:g} 高于现价 {last_px:g}，会立刻成交；想马上进场请用市价单。")
        if action.side == "short" and entry < last_px * 0.999:
            raise Rejected(f"做空限价 {entry:g} 低于现价 {last_px:g}，会立刻成交；想马上进场请用市价单。")
    check_stop_side(action.side, entry, action.stop_loss, action.take_profit)

    notes: list[str] = []
    pos = account.position(action.inst_id)
    if pos and pos.side != action.side:
        raise Rejected(f"{action.inst_id} 已有反向持仓（{pos.side}），请先平仓再反手。")
    if pos and not manual:
        if adds_done >= limits.max_adds:
            raise Rejected(f"这个持仓已经加仓 {adds_done} 次，达到上限 {limits.max_adds} 次。")
        if not limits.allow_averaging_down and pos.upl <= 0:
            raise Rejected("当前持仓是浮亏的，风控不允许亏损加仓（摊平）。走势不对就减仓或平仓。")
    if pos:
        check_stop_side(action.side, last_px, action.stop_loss, None, what="现价")

    dist_pct = abs(entry - action.stop_loss) / entry * 100
    if dist_pct < MIN_STOP_DISTANCE_PCT and not manual:
        raise Rejected(f"止损离入场价只有 {dist_pct:.3f}%，太近了（至少 {MIN_STOP_DISTANCE_PCT}%），正常波动就会被扫掉。")

    leverage = clamp_leverage(action.leverage, pos.leverage if pos else current_leverage, limits, inst, notes)
    if dist_pct * leverage > LIQUIDATION_BUFFER_PCT and not manual:
        safe = max(int(LIQUIDATION_BUFFER_PCT // dist_pct), 1)
        raise Rejected(f"止损距离 {dist_pct:.2f}% 配 {leverage}x 杠杆，止损没到就先爆仓了。请把杠杆降到 {safe}x 以下或收紧止损。")

    equity = account.equity
    if equity <= 0:
        raise Rejected("账户权益为 0，无法开仓。请先往交易账户划转资金。")
    risk_pct = (action.risk_pct or limits.risk_per_trade_pct) if manual else min(action.risk_pct or limits.risk_per_trade_pct, limits.risk_per_trade_pct)
    budget = equity * risk_pct / 100
    per_contract = abs(entry - action.stop_loss) * inst.ct_val
    if manual:  # 用户自己下的单：这一笔按他填的风险比例或金额算，不扣已有持仓占的额度
        existing = position_risk(pos, action.stop_loss, inst.ct_val) if pos else 0.0
        room = budget
    elif pos and existing_risk is not None:
        # 分笔记账：这个币上已有的几笔各按自己的止损算风险，合计不超过单仓上限；新的一笔再受自己的风险比例限制
        existing = existing_risk
        room = min(budget, equity * limits.risk_per_trade_pct / 100 - existing)
        budget = equity * limits.risk_per_trade_pct / 100
    else:
        existing = position_risk(pos, action.stop_loss, inst.ct_val) if pos else 0.0
        room = budget - existing
    by_amount = action.margin_usdt is not None and action.risk_pct is None
    if not by_amount and room <= 0:
        raise Rejected(
            f"按止损 {action.stop_loss:g} 计算，现有持仓的风险已是 {existing:.2f} USDT，"
            f"超过单仓上限 {budget:.2f} USDT。想加仓得先把止损往有利方向移。"
        )
    contracts = room / per_contract
    if action.margin_usdt:
        wanted = action.margin_usdt * leverage / (inst.ct_val * entry)
        if by_amount or manual or wanted <= contracts:
            contracts = wanted
        else:
            notes.append(f"投入 {action.margin_usdt:g} USDT 对应的数量超过单仓风险上限，已按上限缩小")

    held = pos.contracts if pos else 0.0
    unit, inf = inst.ct_val * entry, float("inf")
    cap_single = equity * limits.max_position_notional_x / unit - held if limits.max_position_notional_x and not manual else inf  # 0 = 不限
    others = sum(p.notional_usd for p in account.positions if p.inst_id != action.inst_id)
    cap_total = (equity * limits.max_total_notional_x - others) / unit - held if limits.max_total_notional_x and not manual else inf
    cap_margin = account.available * 0.95 * leverage / unit
    for cap, label in ((cap_single, "单品种名义价值上限"), (cap_total, "总名义价值上限"), (cap_margin, "可用保证金")):
        if contracts > cap:
            contracts = cap
            notes.append(f"数量受{label}限制")
    if contracts <= 0:
        raise Rejected("名义价值或保证金额度已用完，不能再开/加仓。")

    sized = floor_to_step(contracts, inst.lot_sz)
    if sized < Decimal(inst.min_sz) and pos and not by_amount and not manual:
        raise Rejected(
            f"按止损 {action.stop_loss:g} 计算，现有持仓已占用 {max(existing, 0):.2f} / {budget:.2f} USDT 的风险额度，"
            "剩下的不够加仓。想加仓得先把止损往有利方向移，腾出额度。"
        )
    if sized < Decimal(inst.min_sz) and manual:
        raise Rejected(f"算出的数量 {contracts:.6g} 张小于最小下单量 {inst.min_sz} 张，把金额或风险比例填大一些。")
    if sized < Decimal(inst.min_sz):
        raise Rejected(
            f"按风控算出的数量 {contracts:.6g} 张小于最小下单量 {inst.min_sz} 张。"
            "可以把止损设近一些，或者账户资金太少不适合做这个品种。"
        )
    qty = float(sized)
    return SizedOrder(
        contracts=sized,
        leverage=leverage,
        entry=entry,
        stop_loss=action.stop_loss,
        take_profit=action.take_profit,
        risk_usd=round(qty * per_contract + max(existing, 0.0), 4),
        notional_usd=round(qty * inst.ct_val * entry, 2),
        margin_usd=round(qty * inst.ct_val * entry / leverage, 2),
        notes=notes,
    )


def check_stop_update(pos: Position, stop_loss: float | None, take_profit: float | None, *, account: AccountState, inst: Instrument, limits: RiskLimits, last_px: float, current_stop: float | None = None, manual: bool = False) -> None:
    """同方向改保护：超过预算时允许维持或降低已核实的整仓风险。manual（用户自己改的）只查止损止盈有没有放错边。"""
    check_stop_side(pos.side, last_px, stop_loss, take_profit, what="现价")
    if stop_loss is not None and account.equity > 0 and not manual:
        risk = position_risk(pos, stop_loss, inst.ct_val)
        budget = account.equity * limits.risk_per_trade_pct / 100
        current_risk = position_risk(pos, current_stop, inst.ct_val) if current_stop is not None else None
        if risk > budget * 1.1 and (current_risk is None or risk > current_risk):
            current = f"当前风险 {current_risk:.4g} USDT，" if current_risk is not None else ""
            raise Rejected(
                f"{current}止损设到 {stop_loss:g} 后整仓风险为 {risk:.4g} USDT，"
                f"超过按账户权益 {limits.risk_per_trade_pct:g}% 计算的单仓预算 {budget:.4g} USDT。"
                "请收紧止损或减仓。"
            )
