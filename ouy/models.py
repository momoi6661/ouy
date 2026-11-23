from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Env = Literal["demo", "live"]
Mode = Literal["advise", "confirm", "auto"]
Direction = Literal["long", "short"]
Permission = Literal["auto", "confirm", "off"]
ACTION_KINDS = ("open", "add", "set_leverage", "set_stops", "reduce", "close", "cancel_orders")
EXIT_KINDS = ("reduce", "close", "cancel_orders")
Style = Literal["scalp", "intraday", "swing"]
Wake = Literal["event", "always"]
Stance = Literal["cautious", "balanced", "active"]
EntryMode = Literal["market", "limit", "auto"]
# auto 是旧配置值，执行时按 agent 处理。
Fast = Literal["off", "verify", "agent", "auto"]
FAST_MODES: dict[str, str] = {"off": "关", "verify": "出信号叫醒 Worker 核实", "agent": "Worker 授权的方向直接下单", "auto": "跟随 Worker 授权（兼容旧配置）"}
QuantMode = Literal["auto", "trend", "range", "breakout", "quiet"]
QuantBias = Literal["both", "long", "short"]
QUANT_MODES: dict[str, dict[str, Any]] = {
    "auto": {"label": "自动识别", "desc": "先判断行情：趋势只做顺势回踩，震荡只做区间两头，刚反转只做变盘后回踩，方向打架不出候选", "kinds": ("retest",)},
    "trend": {"label": "顺势回踩", "desc": "单边趋势用：只盯顺大方向的变盘回踩与前高前低回踩，不追高杀跌、屏蔽逆势信号", "kinds": ("retest",)},
    "range": {"label": "震荡箱体", "desc": "横盘拉锯用：停止追突破，只盯箱体下沿撑住接多、上沿受阻接空", "kinds": ("reject",)},
    "breakout": {"label": "收敛突破", "desc": "窄幅蓄势待选方向用：不在箱体中间挨打，盯放量收盘冲出边界的第一下", "kinds": ("breakout", "impulse", "retest")},
    "quiet": {"label": "静默防守", "desc": "乱插针或消息前后用：暂停新开仓快判信号，只做变盘撤旧单与持仓保护", "kinds": ()},
}
# 入场方式：Agent 看好方向后是按现价直接进，还是挂在更好的价位等成交
ENTRY_MODES: dict[str, dict] = {
    "market": {
        "label": "市价优先",
        "guide": "判断方向成立、止损能放在结构位之外，就现在进：plan 的 entry_type 填 now（程序按现价市价进场），不要挂在远处等价格回来。"
        "只有现价离结构位还远、现在进净盈亏比不够时，才用 pullback 把入场区间放在那个结构位上等。",
    },
    "limit": {
        "label": "挂单优先",
        "guide": "优先把单挂在比现价更有利的结构位上等价格来：plan 的 entry_type 填 pullback（做多的入场区间在现价下方、做空的在现价上方），"
        "程序到价就进。价格已经在入场区间里就直接进（entry_type=now）。入场区间不要放得太远，离现价最好在下面的距离以内，太远的大多等不到。",
    },
    "auto": {
        "label": "Agent 自己定",
        "guide": "现在进还是挂着等，由你按盘面定：动能强、已经接受突破、怕踏空就现在进（entry_type=now）或者追突破（breakout）；"
        "等回踩前高前低、假突破后反着做、震荡里做上下沿，就把入场区间放在那个关键位上等（pullback）。",
    },
}
# 出手尺度：决定 Agent 需要多少条件才肯开仓。写死在代码里的风控（必须带止损、仓位按止损距离算、亏损上限）不受它影响
STANCES: dict[str, dict] = {
    "cautious": {
        "label": "保守",
        "min_rr": 2.0,
        "guide": "多个条件共振才出手：大周期顺势 + 入场周期出现确认信号（放量、吞没、回踩不破）+ 到第一目标盈亏比 ≥ 2。宁可错过，不做没把握的单。",
    },
    "balanced": {
        "label": "均衡",
        "min_rr": 1.5,
        "guide": "不逆大周期（或者是明确的区间边缘）+ 有结构止损 + 到第一目标盈亏比 ≥ 1.5 就可以做。确认信号有更好，没有也不强求。",
    },
    "active": {
        "label": "积极",
        "min_rr": 1.2,
        "guide": "用户要的是多出手、把每一段区间都做到。只要止损能放在一个明确的结构位之外、到第一目标盈亏比 ≥ 1.2，就出手，不等二次确认、不要求放量。\n"
        "- 震荡行情：区间上下沿就是入场点。价格进入区间下沿 1/4 直接做多、进入上沿 1/4 直接做空，止损放在区间外，先看区间中轨再看对侧。\n"
        "  区间太宽、现价离两边都远时，把区间按更小的周期再拆成小区间来做，不要把单子挂在很远的大区间边上干等。\n"
        "- 趋势行情：顺着方向现在就进，止损放在最近的回踩低点 / 反弹高点之外；不等回踩后的再次确认。\n"
        "- 收敛行情：在收敛区间两侧外沿各看一个突破方向，突破后的第一次回踩进。\n"
        "- 「观望」不是默认选项：只有价差或最小报价单位太大吃掉利润、已到当日亏损上限、或者找不到任何能放止损的结构位时才可以不给计划，并且要明说是哪一条。",
    },
}
# 交易风格：决定 Agent 看哪些周期、多久定时看一次盘、止损大概放多宽、一笔单打算拿多久
# look_minutes：空仓时每隔这么久、对齐到 K 线收盘定时看一次盘做计划；有仓时改成每根结构周期 K 线（bar_minutes）看一次。
# 两次定时看盘之间不轮询模型，只有触发线、转折、连续走线、持仓变化这些事件能提前叫醒 Worker。
# 盈利回吐多少就叫醒 Worker 判断：（浮盈至少到过几 R 才开始盯，回吐百分之多少叫醒）。波段和长线的仓放得宽，不因为正常震荡就折腾
GIVEBACK: dict[str, tuple[float, float]] = {"scalp": (0.6, 30.0), "intraday": (1.0, 40.0), "swing": (1.5, 55.0)}
# 保盈线：浮盈最高到过这么多 R（按初始止损距离）之后，程序把止损移到「入场价 + 往返成本」，盈利单不再变亏损单。
# 数值来自候选位回测（python -m ouy.backtest_plans --lock-r，结果见 knowledge/evidence.md），不要凭感觉改。
LOCK_R: dict[str, float] = {"scalp": 0.5, "intraday": 0.5, "swing": 0.5}

STYLES: dict[str, dict] = {
    "scalp": {
        "label": "超短线",
        "edge": "赚 5 分钟结构里一小段的钱：靠次数和纪律，手续费占比最高，扣费后不够 1.2R 的不做",
        "bars": ("1m", "5m", "15m", "1H"),
        "struct_bar": "5m",
        "bar_minutes": 5,
        "look_minutes": 15,
        "limit_dist_pct": 0.8,
        "stale_min": 10,
        "plan_minutes": 30,
        "hold_minutes": 30,
        "guide": "用 1H/15m 定方向，5m/1m 找入场；一笔单拿几分钟到一小时，当天不留仓过夜。止损放在 5m 结构外，通常 0.3%~0.6%，"
        "目标看下一个 5m / 15m 结构位，行情不动 20~30 分钟就时间止损。手续费和滑点占比高：止损贴得太近、目标太小的单扣完成本不划算，"
        "宁可用 5m / 15m 的结构做宽一点，也不要在 1m 的小波动里抢。",
    },
    "intraday": {
        "label": "日内短线",
        "edge": "赚当天那一段主要走势的钱：等开盘或关键位给出方向，跟几十分钟到几小时",
        "bars": ("5m", "15m", "1H", "4H"),
        "struct_bar": "15m",
        "bar_minutes": 15,
        "look_minutes": 60,
        "limit_dist_pct": 2.0,
        "stale_min": 45,
        "plan_minutes": 180,
        "hold_minutes": 240,
        "guide": "用 4H/1H 定方向，15m/5m 找入场；一笔单拿几十分钟到几小时，尽量当天了结。止损通常放在 15m 结构外 0.5%~1.5%，"
        "先看 1.5~2R，走出趋势再用跟踪止损多拿一段。",
    },
    "swing": {
        "label": "波段",
        "edge": "赚几天的趋势延续：止损宽、次数少，忍得住中间的回撤才拿得到大的一段",
        "bars": ("15m", "1H", "4H", "1D"),
        "struct_bar": "1H",
        "bar_minutes": 60,
        "look_minutes": 240,
        "limit_dist_pct": 5.0,
        "stale_min": 240,
        "plan_minutes": 720,
        "hold_minutes": 0,
        "guide": "用 1D/4H 定方向，1H/15m 找入场；一笔单拿一到几天，不被 5 分钟级别的噪音洗出去。止损放在 4H 结构外 1.5%~4%，"
        "杠杆相应降低，目标 2~4R，分批止盈。",
    },
}
# 每种风格用哪两段时间的高低点对比来定大方向（先看前一个，它是震荡再看后一个）
TREND_FRAMES: dict[str, tuple[str, str]] = {"scalp": ("1H", "30m"), "intraday": ("4H", "1H"), "swing": ("1D", "4H")}
TREND_RULE = (
    "先定大方向，再找入场点，不能只凭眼前几根 K 线的涨跌下单（那是赌）：\n"
    "- 顺着大方向的单正常做：大方向向上就优先等回踩做多，向下就优先等反弹做空。\n"
    "- 逆着大方向的单，只有在关键位（上一段高低点、区间上下沿、支撑阻力）出现明确的反转信号（假突破收回、吞没）时才做，"
    "仓位减半、目标只看最近一个关键位；没有这种信号就不做，哪怕小周期正在往那边走。\n"
    "- 大方向不明不等于震荡。结构周期确认震荡才看区间两头，数据不足不据此开仓。\n"
    "- 每次开仓、给计划，理由里都要写明：大方向是什么、这一单是顺势还是逆势、依据是哪个关键位。用户明确指定方向的单照用户说的做，但要提醒他这是逆势。"
)
Effort = Literal["default", "low", "medium", "high", "xhigh"]


class RiskLimits(BaseModel):
    """硬性风控上限。Agent 的任何下单都要先过这一关，Agent 改不了这些数字。"""

    max_leverage: int = Field(10, ge=1, le=125, description="允许的最高杠杆")
    risk_per_trade_pct: float = Field(1.0, gt=0, le=10, description="单个持仓打到止损时最多亏掉权益的百分比")
    max_position_notional_x: float = Field(0, ge=0, le=500, description="单品种名义价值 ≤ 权益 × 该倍数；0 = 不限（默认）。仓位大小已经由单仓风险、杠杆和可用保证金管着")
    max_total_notional_x: float = Field(0, ge=0, le=1000, description="全部持仓名义价值 ≤ 权益 × 该倍数；0 = 不限（默认）")
    max_adds: int = Field(2, ge=0, le=10, description="同一持仓最多加仓次数")
    allow_averaging_down: bool = Field(False, description="是否允许亏损时加仓（摊平）。默认只允许浮盈加仓")
    max_daily_loss_pct: float = Field(5.0, gt=0, le=50, description="当日亏损达到权益该比例后只许减仓/平仓")
    emergency_exit_loss_pct: float = Field(2.5, gt=0, le=50, description="单个持仓浮亏达到权益该比例时不等 Agent，立即市价平仓")


LookMinutes = Literal[0, 5, 15, 30, 60, 240]


class WorkerOverride(BaseModel):
    """某个币种的 Worker 的单独设置；没填的沿用全局。"""

    look_minutes: LookMinutes | None = Field(None, description="这个 Worker 定时看盘按哪个周期的 K 线收盘；留空跟全局走，0 = 按风格自动")
    wake: Wake | None = Field(None, description="定时看盘时：event 没变化就跳过 / always 每次都看；留空用全局设置")
    model: str = Field("", description="这个 Worker 用的模型；留空用全局的 Worker 模型")
    effort: Effort | None = Field(None, description="思考强度；留空用全局设置")
    paused: bool = Field(False, description="暂停：这个币的 Worker 不再被唤醒（紧急平仓线仍然有效）")
    mode: Mode | None = Field(None, description="这个 Worker 的审批方式：auto 全自动 / confirm 下单前确认 / advise 只给建议；留空跟全局走")
    trigger_effort: Effort | None = Field(None, description="被行情事件叫醒（触及价位、异动）时的思考强度；留空用全局设置")
    resume_ts: int | None = Field(None, description="暂停到什么时候自动恢复（毫秒时间戳）；留空 = 一直停到有人重新打开")
    paused_by: str = Field("", description="用户暂停原因或历史暂停记录")
    standby: bool = Field(False, description="停下（待命）：空仓时不做定时复盘，只在触发线、策略、留言、持仓变化时叫醒；交代的停止条件达到或用户设置时为真")
    stance: Stance | None = Field(None, description="这个 Worker 的出手尺度：cautious 保守 / balanced 均衡 / active 积极；留空跟全局走")
    entry_mode: EntryMode | None = Field(None, description="这个 Worker 的入场方式：market 市价优先 / limit 挂单优先 / auto 自己定；留空跟全局走")
    fast_entry: Fast | None = Field(None, description="这个 Worker 的快判怎么用：off / verify / agent / auto；留空跟全局走")
    quant_mode: QuantMode = Field("auto", description="量化雷达档位：auto 自动 / trend 顺势回踩 / range 震荡箱体 / breakout 收敛突破 / quiet 静默防守")
    quant_bias: QuantBias = Field("both", description="量化雷达方向锁：both 双向 / long 只盯多 / short 只盯空")
    quant_vol_ratio: float | None = Field(None, ge=1.0, le=5.0, description="放量判定倍数门槛（留空默认 1.5）")
    quant_reason: str = Field("", max_length=200, description="切换到当前量化档位的原因")
    quant_by: str = Field("", max_length=40, description="谁切换的档位")


class AgentConfig(BaseModel):
    env: Env = "demo"
    mode: Mode = "auto"
    auto_exit: bool = Field(True, description="确认模式下，减仓/平仓/收紧止损不用等确认，直接执行")
    hands_off_manual: bool = Field(
        False,
        description="默认关：用户手动开的仓（下单窗或欧易 App 里开的）由 Worker 盯着并保护（补止损、收紧止损、保盈线），但减仓/平仓/设止盈要用户点确认。打开后连改止损也要用户点确认",
    )
    pref_leverage_min: int = Field(0, ge=0, le=125, description="用户偏好：常用杠杆的下限。0 = 不指定，由 Agent 按行情自己定")
    pref_leverage_max: int = Field(0, ge=0, le=125, description="用户偏好：常用杠杆的上限。0 = 不指定")
    prefs: list[str] = Field(default_factory=list, max_length=20, description="用户偏好（一句一条，比如「喜欢高杠杆」「不做山寨币」）：Router 从对话里记下来，也可以在界面上改；每个 Agent 每轮都会读到")
    giveback_wake: bool = Field(True, description="每笔仓的浮盈从最高点回吐到一定比例时，立刻叫醒 Worker 判断：是趋势里的正常震荡，还是该减仓、平仓、反手")
    giveback_pct: float = Field(0, ge=0, le=90, description="浮盈从最高点回吐百分之多少叫醒 Worker；0 = 按这笔仓的风格（超短线 30 / 日内 40 / 波段 55）")
    giveback_backstop: bool = Field(True, description="回吐继续扩大（到叫醒线的 1.6 倍）而 Worker 还没给出结论时，先把这笔仓的止损移到保本，不让盈利单变亏损单")
    profit_lock: bool = Field(True, description="保盈线：浮盈最高到过 LOCK_R（按风格，约 0.5R）后，程序直接把止损移到入场价加往返成本；Worker 可以按笔改 lock_r 或关掉")
    loss_wake_r: float = Field(0.6, ge=0, le=0.95, description="浮亏到止损距离的这个比例时叫醒 Worker 判断是震荡还是方向错了（要不要提前止损、反手）；0 = 关闭")
    permissions: dict[str, Permission] = Field(
        default_factory=dict,
        description="按动作单独设置权限：auto 直接执行 / confirm 先问用户 / off 不允许。没写的动作按 mode 的预设来",
    )
    style: Style = Field("intraday", description="默认交易风格：scalp 超短线 / intraday 日内短线 / swing 波段")
    router_model: str = Field("", description="Router（负责和用户沟通、分派任务、过滤事件）用的模型；留空用 .env 里的 AI_MODEL")
    worker_model: str = Field("", description="Worker（每个币一个，负责分析和交易）用的模型；留空用 .env 里的 AI_MODEL")
    wake: Wake = Field("event", description="定时看盘（对齐 K 线收盘）时：event 价格没动、离线都远就跳过（软事件先过滤）/ always 每次都叫醒 Worker。两种都只在 K 线收盘和事件时叫醒，硬事件可提前，自动唤醒都遵守失败退避")
    max_parallel: int = Field(3, ge=1, le=8, description="最多同时有几个 Worker 在思考（不同的币并行）；超出的排队。子代理另有一组名额，是它的 2 倍")
    intel_minutes: int = Field(0, ge=0, le=1440, description="子代理多久定时更新一次简报（分钟）；0 = 不定时更新，只在 Worker 觉得需要时才派（默认）")
    trigger_effort: Effort = Field("low", description="Worker 被行情事件叫醒（价格触及盯盘价位、进入入场区间、突然异动）时的思考强度：要快，默认低")
    stream: bool = Field(True, description="流式输出：Agent 边想边出字，不等整轮跑完才显示（模型接口不支持流式时关掉）")
    web_search: bool = Field(True, description="情报子代理可以用模型自带的联网搜索查新闻、公告和监管消息（模型接口不支持时自动不用）")
    subagents: dict[str, bool] = Field(
        default_factory=lambda: {"intel": True, "smart": False, "risk": True},
        description="每个子代理的开关：intel 情报 / smart 聪明钱 / risk 风险复核。关掉的 Worker 不能派，界面上也不显示",
    )
    intel_model: str = Field("", description="情报 Worker 用的模型；留空和交易 Worker 相同")
    slow_path: bool = Field(True, description="事件的第二级过滤：价格波动、定时这类软事件先让 Router 判断值不值得唤醒 Worker")
    workers: dict[str, WorkerOverride] = Field(default_factory=dict, description="按币种单独设置 Worker（唤醒方式、模型、思考强度、暂停）")
    stance: Stance = Field("active", description="出手尺度：cautious 保守 / balanced 均衡 / active 积极")
    entry_mode: EntryMode = Field("market", description="入场方式：market 市价优先（看好就按现价进）/ limit 限价优先（挂在更好的价位等成交）/ auto Agent 自己定")
    log_keep_days: int = Field(7, ge=1, le=90, description="运行记录（活动流、复盘日志、已结束的任务和策略）保留几天，更早的每天自动删掉")
    auto_arm_plan: bool = Field(
        True,
        description="计划就是指令：Worker 复盘给出了交易计划（方向、入场区间、止损、止盈）而这个币上没有持仓、挂单和在等的入场策略时，"
        "程序直接照计划执行——价格已经在入场区间就市价进，没到就布置成到价进场的策略。照样过权限和硬性风控。关掉后计划只是展示，要 Worker 自己调用工具下单",
    )
    fast_entry: Fast = Field(
        "verify",
        description="快判（程序每几秒用 1 分钟 K 线判断收盘突破、放量急动、假突破收回，并算好入场 / 止损 / 目标）出了信号怎么办："
        "off 不用 / verify 马上叫醒 Worker 核实，它认可才下单（默认）/ agent Worker 在结论里授权过的方向直接下单，没授权的叫醒它核实 / auto 同 agent。"
        "直接下单的照样过净盈亏比核对、权限和硬性风控。只对超短线和日内短线的币生效",
    )
    fast_kinds: list[Literal["retest", "breakout", "reject", "impulse"]] = Field(
        default_factory=lambda: ["retest"],
        description="快判用哪几种信号：retest 变盘后回踩（走势刚变，价格回到被突破的摆动点又朝新方向收回）/ breakout 1 分钟收盘突破关键线 / reject 假突破收回 / impulse 放量急动。"
        "默认只开 retest：用 BTC、ETH 各 30 天的 1 分钟 K 线回测，另外三种扣掉手续费之后都是亏的（python -m ouy.backtest 可以自己跑）",
    )
    maker_fee_pct: float = Field(0.02, ge=0, le=1, description="挂单手续费（%，单边）。计划里等价格回来的单是挂限价单成交的，进场这一边按它算")
    taker_fee_pct: float = Field(0.05, ge=0, le=1, description="吃单手续费（%，单边）。程序用它算扣掉往返手续费和点差之后的净盈亏比，不够【出手尺度】下限的计划不布置")
    limit_dist_pct: float = Field(0, ge=0, le=20, description="Agent 的限价单离现价最多多远（%）；0 = 按风格自动（超短线 0.3 / 日内 1 / 波段 3）。超过就不让挂")
    stale_minutes: int = Field(0, ge=0, le=1440, description="Agent 的挂单多少分钟没成交就叫醒它处理（改市价或撤单）；0 = 按风格自动（超短线 10 / 日内 45 / 波段 240）")
    default_stop_pct: float = Field(10.0, gt=0, le=100, description="手动下单时默认的止损：亏掉投入保证金的百分之多少（价格距离 = 该值 ÷ 杠杆）")
    styles: dict[str, Style] = Field(default_factory=dict, description="按品种单独设置的交易风格，没写的品种用默认风格")
    instruments: list[str] = Field(default_factory=lambda: ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    margin_mode: Literal["cross", "isolated"] = "cross"
    tick_seconds: int = Field(3, ge=1, le=60, description="盯盘轮询间隔（秒）；策略执行器也按这个间隔检查触发条件")
    look_minutes: LookMinutes = Field(0, description="定时看盘按哪个周期的 K 线收盘（分钟）：0 = 按风格自动（空仓 超短线 15 / 日内 60 / 波段 240，有仓 5 / 15 / 60）；选了 5 / 15 / 30 / 60 / 240 就不分空仓有仓都按它")
    max_event_wakes: int = Field(6, ge=1, le=60, description="每个品种每小时最多被行情事件（触发线、转折、连续走线、新候选、价格波动、快判）叫醒几次；超过的先记着，并到下一次叫醒里，不丢")
    min_review_seconds: int = Field(45, ge=10, le=600, description="两次 Agent 复盘的最短间隔（秒），防止刷爆模型额度")
    setup_min_score: int = Field(3, ge=1, le=4, description="记录器算多空分数时用（四项：大方向、在关键位、收盘确认、量），保留给打分函数")
    setup_vol_ratio: float = Field(1.3, ge=0.5, le=10, description="多空分数里「放量」这一项：这根 5 分钟 K 线的量至少是前 20 根平均的多少倍")
    setup_near_pct: float = Field(0.15, ge=0.02, le=2, description="多空分数里「碰到关键线」这一项：K 线离关键线多近算碰到（百分比）")
    wake_move_pct: float = Field(0.6, gt=0, le=20, description="距上次复盘价格波动超过该百分比就立刻叫醒 Agent")
    reasoning_effort: Effort = Field("medium", description="Agent 盯盘复盘时的思考强度；default 表示不指定，用模型默认值")
    router_effort: Effort = Field("low", description="Router 的思考强度：它只分发任务和收尾，默认用低；要想、要查的事交给 Worker")
    sub_effort: Effort = Field("low", description="子代理（情报 / 聪明钱 / 风险复核）干活时的思考强度")
    notes: str = Field("", max_length=2000, description="你给 Agent 的额外交代（风格、偏好、禁忌）")
    risk: RiskLimits = Field(default_factory=RiskLimits)

    @field_validator("permissions")
    @classmethod
    def _known_kinds(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - set(ACTION_KINDS)
        if unknown:
            raise ValueError(f"未知的动作类型：{', '.join(sorted(unknown))}")
        return value

    @field_validator("styles", "workers", mode="before")
    @classmethod
    def _style_keys(cls, value: Any) -> Any:
        return {str(k).strip().upper(): v for k, v in value.items()} if isinstance(value, dict) else value

    def worker(self, inst_id: str) -> WorkerOverride:
        return self.workers.get(inst_id) or WorkerOverride()

    def wake_for(self, inst_id: str) -> str:
        return self.worker(inst_id).wake or self.wake

    def model_for(self, inst_id: str) -> str:
        return self.worker(inst_id).model or self.worker_model

    def effort_for(self, inst_id: str) -> str:
        return self.worker(inst_id).effort or self.reasoning_effort

    def trigger_effort_for(self, inst_id: str) -> str:
        """被行情事件叫醒时的思考强度：要快，默认比定时复盘低一档。"""
        return self.worker(inst_id).trigger_effort or self.trigger_effort

    def look_for(self, inst_id: str) -> int:
        own = self.worker(inst_id).look_minutes
        return self.look_minutes if own is None else own

    def style_for(self, inst_id: str) -> str:
        return self.styles.get(inst_id, self.style)

    def stance_for(self, inst_id: str) -> str:
        return self.worker(inst_id).stance or self.stance

    def entry_mode_for(self, inst_id: str) -> str:
        return self.worker(inst_id).entry_mode or self.entry_mode

    def fast_entry_for(self, inst_id: str) -> str:
        mode = self.worker(inst_id).fast_entry or self.fast_entry
        return "agent" if mode == "auto" else mode

    def quant_mode_for(self, inst_id: str) -> str:
        return self.worker(inst_id).quant_mode or "auto"

    def quant_bias_for(self, inst_id: str) -> str:
        return self.worker(inst_id).quant_bias or "both"

    def limit_dist_for(self, style: str) -> float:
        return self.limit_dist_pct or STYLES[style]["limit_dist_pct"]

    def sub_enabled(self, role: str) -> bool:
        return bool(self.subagents.get(role, role != "smart"))

    def leverage_pref(self) -> tuple[int, int] | None:
        """用户的常用杠杆范围（不超过风控上限）；没设就是 None。"""
        lo, hi = self.pref_leverage_min, self.pref_leverage_max
        if not lo and not hi:
            return None
        hi = min(hi or self.risk.max_leverage, self.risk.max_leverage)
        lo = min(lo or 1, hi)
        return lo, hi

    def prefs_text(self) -> str:
        """给 Agent 看的用户偏好。"""
        rows = []
        pref = self.leverage_pref()
        if pref:
            rows.append(
                f"常用杠杆 {pref[0]}~{pref[1]}x：开仓、布置策略时 leverage 就在这个范围里选（信号越强用越高的一端），不要自己往低了挑；"
                "杠杆越高强平越近，止损要相应放近（止损距离% × 杠杆不能超过 70），放不下就用范围里更低的一端。"
                "这是偏好，不是开仓的前提：某个币的最高杠杆低于这个范围时，就用它的最高杠杆照常做，不要因为杠杆达不到而不开仓"
            )
        rows += [p.strip() for p in self.prefs if p.strip()]
        return "；".join(rows)

    def stale_minutes_for(self, style: str) -> int:
        return self.stale_minutes or STYLES[style]["stale_min"]

    def permission_for(self, kind: str, inst_id: str | None = None) -> str:
        """某个动作 Agent 能不能做。这个币的 Worker 单独设了审批方式就按它的来（全局明确「不允许」的动作它也不能做）；
        否则：按动作单独设置的优先，再按全局 mode 预设。"""
        own = self.worker(inst_id).mode if inst_id else None
        if own:
            if self.permissions.get(kind) == "off":
                return "off"
            if own == "advise" or (own == "confirm" and not (self.auto_exit and kind in EXIT_KINDS)):
                return "confirm"
            return "auto"
        if kind in self.permissions:
            return self.permissions[kind]
        if self.mode == "advise":  # 只给建议：每个动作都是一条等用户点同意的建议，什么都不自动做
            return "confirm"
        if self.mode == "confirm" and not (self.auto_exit and kind in EXIT_KINDS):
            return "confirm"
        return "auto"

    @field_validator("instruments")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for item in value:
            inst = item.strip().upper()
            if inst and inst not in seen:
                seen.append(inst)
        if len(seen) > 8:
            raise ValueError("最多同时盯 8 个品种")
        return seen


class EntryInvalidation(BaseModel):
    when: Literal["price_above", "price_below", "close_above", "close_below"]
    level: float = Field(gt=0)
    bar: Literal["1m", "5m", "15m", "1H"] | None = None


class TradeAction(BaseModel):
    """一条交易指令。Agent 和手动下单用同一个结构，走同一条风控 + 执行通道。"""

    kind: Literal["set_leverage", "open", "add", "reduce", "close", "set_stops", "cancel_orders"]
    inst_id: str
    side: Direction | None = None
    leverage: int | None = Field(None, ge=1, le=125)
    order_type: Literal["market", "limit"] = "market"
    price: float | None = Field(None, gt=0)
    stop_loss: float | None = Field(None, gt=0)
    take_profit: float | None = Field(None, gt=0)
    risk_pct: float | None = Field(None, gt=0, le=10, description="本次愿意承担的风险占权益百分比，不填用上限")
    fraction: float | None = Field(None, gt=0, le=1, description="减仓比例")
    margin_usdt: float | None = Field(None, gt=0, description="指定保证金时按金额和杠杆定量，不按默认风险百分比缩单；另填 risk_pct 时两者取小")
    margin_mode: Literal["cross", "isolated"] | None = Field(None, description="全仓 cross / 逐仓 isolated；不填用设置里的默认值。已有持仓时沿用持仓的模式")
    style: Style | None = Field(None, description="这一笔的交易风格；不填用这个币种的风格")
    lot_id: int | None = Field(None, description="只操作这个币的某一笔仓位（减仓 / 平仓 / 改止盈止损）；不填是整个持仓")
    profit_guard_id: int | None = Field(None, gt=0, description="只供已触发的模拟盘分笔止盈规则绑定回执")
    scalp_plan_id: int | None = Field(None, gt=0, description="模拟盘超短线执行器的预设入场计划")
    require_flat: bool = False
    cancel_before_entry: EntryInvalidation | None = None
    cancel_px: float | None = Field(None, gt=0, description="限价单的撤单线：没成交前价格先到这里，程序自动撤掉这张单")
    expires_minutes: int | None = Field(None, ge=1, le=1440, description="限价单的有效期（分钟）：到时间没成交，程序自动撤掉")
    ord_id: str | None = Field(None, description="撤单时只撤这一张；不填是这个币的全部挂单")
    reason: str = ""

    @field_validator("inst_id")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class ActionResult(BaseModel):
    status: Literal["executed", "pending", "advised", "rejected", "failed"]
    message: str
    action: TradeAction
    detail: dict[str, Any] = Field(default_factory=dict)
    pending_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "executed"


class Position(BaseModel):
    inst_id: str
    side: Direction
    contracts: float
    avg_px: float
    mark_px: float
    upl: float
    upl_ratio: float
    leverage: float
    liq_px: float | None = None
    margin_mode: str = "cross"
    pos_side: str = "net"
    notional_usd: float = 0
    margin: float = 0
    opened_at: int | None = None


class ProtectiveOrder(BaseModel):
    algo_id: str
    inst_id: str
    ord_type: str
    side: str
    pos_side: str = "net"
    sz: str = ""
    stop_loss: float | None = None
    take_profit: float | None = None
    trigger_px: float | None = None


class OpenOrder(BaseModel):
    ord_id: str
    inst_id: str
    side: str
    pos_side: str = "net"
    ord_type: str
    px: float | None = None
    sz: float = 0
    filled: float = 0
    reduce_only: bool = False
    state: str = ""
    created_at: int | None = None
    leverage: float = 0
    notional_usd: float = Field(0, description="这张挂单没成交部分的名义价值（USDT）")
    margin_usd: float = Field(0, description="这张挂单占用的保证金（USDT）= 名义价值 ÷ 杠杆；只减仓的单不占用")


class AccountState(BaseModel):
    env: Env
    equity: float
    available: float
    cash_usdt: float = Field(0, description="账户里的 USDT 现金余额")
    margin_used: float = Field(0, description="持仓和挂单占用的 USDT 保证金")
    upl: float = 0
    day_pnl: float | None = Field(None, description="今日盈亏（USDT），按当天第一次读到的权益算")
    account_level: str = ""
    position_mode: str = "net_mode"
    positions: list[Position] = Field(default_factory=list)
    orders: list[OpenOrder] = Field(default_factory=list)
    protections: list[ProtectiveOrder] = Field(default_factory=list)
    day_start_equity: float | None = None
    day_pnl_pct: float | None = None
    halted: bool = False

    def position(self, inst_id: str) -> Position | None:
        return next((p for p in self.positions if p.inst_id == inst_id), None)


class Instrument(BaseModel):
    inst_id: str
    inst_type: str
    ct_val: float = 1
    ct_type: str = ""
    settle_ccy: str = ""
    lot_sz: str = "1"
    min_sz: str = "1"
    tick_sz: str = "0.1"
    max_leverage: float = 1
    state: str = "live"
    category: str = "1"  # 欧易的 instCategory：1 加密货币 / 3 股票 / 4 大宗商品 / 5 外汇 / 6 债券


# ---------- Agent 输出 ----------

class PlanLine(BaseModel):
    """计划线：价格到了这个位置（或 K 线收盘越过它）时程序做什么。到了由程序直接执行，只执行一次。"""

    price: float = Field(gt=0)
    when: Literal["price_touch", "close_above", "close_below"] = Field(
        "price_touch", description="怎么算到了：price_touch 报价碰到；close_above / close_below 指定周期的 K 线收盘站上 / 跌破这个价（只有 do=enter / cancel 能用，叫醒线一律碰到就叫）")
    bar: Literal["1m", "5m", "15m", "1H"] | None = Field(None, description="收盘确认周期，省略时用当前风格的结构周期")
    do: Literal["wake", "cancel", "enter"] = Field(
        "wake",
        description="到了做什么：wake 叫醒你来看；cancel 撤掉这个币还没成交的入场挂单和入场策略；"
        "enter 程序按现价进场（必须填 side、stop_loss、take_profit），进场前照样核对成本、盈亏比和风控，过不了就改成叫醒你",
    )
    side: Direction | None = Field(None, description="do=enter 时的方向")
    stop_loss: float | None = Field(None, gt=0, description="do=enter 时的止损价")
    take_profit: float | None = Field(None, gt=0, description="do=enter 时的止盈价")
    strategy: str = Field("", max_length=40, description="do=enter 时用的是哪条策略（【策略目录】里的名字）")
    edge: str = Field("", max_length=200, description="do=enter 而且这条打法标了「没有优势」时必填：算法看不到的额外依据")
    note: str = Field(description="这条线是什么位置、到了为什么这么做")
    valid_minutes: int | None = Field(None, ge=1, le=1440, description="这条线有效多少分钟；省略则按当前交易风格")

    @model_validator(mode="after")
    def _entry_fields(self) -> "PlanLine":
        if self.do == "wake":  # 叫醒线碰到就叫：等收盘的话价格过了线 Worker 还不知道，写了收盘条件也按碰到算
            self.when, self.bar = "price_touch", None
        if self.do != "enter":
            return self
        if not (self.side and self.stop_loss and self.take_profit):
            raise ValueError("do=enter 的线要同时给 side、stop_loss、take_profit")
        sign = 1 if self.side == "long" else -1
        if (self.price - self.stop_loss) * sign <= 0 or (self.take_profit - self.price) * sign <= 0:
            raise ValueError(f"{self.price:g} 这条进场线的止损 / 止盈放错边了：做多的止损在线下方、止盈在线上方，做空相反")
        return self


class TradePlan(BaseModel):
    review_level: float | None = Field(None, gt=0, description="可选的持仓逻辑复核线，位于入场与硬止损之间。按本笔交易风格的结构周期收盘越线，仅提醒一次复核，不自动平仓或反手")
    margin_usdt: float | None = Field(None, gt=0, description="本笔明确选定的保证金（USDT）；未指定则按风险定量，不要将账户余额或目标收益当作投入金额")
    direction: Direction
    entry_low: float = Field(gt=0, description="入场区间下沿")
    entry_high: float = Field(gt=0, description="入场区间上沿")
    stop_loss: float = Field(gt=0)
    take_profits: list[float] = Field(min_length=1, max_length=3, description="分批止盈价，由近到远")
    leverage: int = Field(ge=1, le=125)
    entry_type: Literal["now", "pullback", "breakout"] = Field(
        "pullback",
        description="怎么进场。now：现价就进（市价）；pullback：等价格回到入场区间再进（做多等回落、做空等反弹），默认；"
        "breakout：等价格突破后顺势追（做多：entry_low 是突破位、entry_high 是最多追到哪；做空：entry_high 是跌破位、entry_low 是最多追到哪）",
    )
    strategy: str = Field("", max_length=40, description="这个计划用的是哪条策略（【策略目录】里的名字，比如「前高前低法」「趋势回踩」「变盘后回踩」）")
    edge: str = Field("", max_length=200, description="用的打法在【算法候选位】里标了「没有优势」（机械照做回测亏）时必填：算法看不到、你看到的额外依据，写具体数据；其余情况留空")
    valid_minutes: int | None = Field(None, ge=1, le=1440, description="这个计划多久没成交就作废（分钟）；不填按交易风格（超短线 30 / 日内 180 / 波段 720）")
    risk_pct: float | None = Field(None, gt=0, le=10, description="这一笔占用的风险额度（占权益百分比）；不填用风控上限，信号一般或逆势时主动填小一些")
    reverse_trigger: float | None = Field(None, gt=0, description="自动反手：这笔单被止损之后，价格继续走到这个价位就由程序自动开反方向的单（做多的填在止损下方，做空的填在止损上方）；不想反手留空")
    reverse_stop: float | None = Field(None, gt=0, description="反手单的止损；填了 reverse_trigger 就必须填（反手做空的放在触发价上方，反手做多的放在下方）")
    reverse_target: float | None = Field(None, gt=0, description="反手单的止盈目标")
    invalidation: str = Field(description="什么情况说明判断错了、要立刻离场")
    cancel_before_entry: EntryInvalidation | None = Field(None, description="未入场前由程序检查的撤单条件，收盘条件省略 bar 时用当前风格的结构周期")
    add_condition: str = Field("", description="什么情况下加仓；不打算加仓留空")
    exit_condition: str = Field("", description="除止盈止损外的主动离场条件")


class LotVerdict(BaseModel):
    """对这个币上某一笔仓位的结论。"""

    lot_id: int = Field(description="仓位编号（「各笔仓位」里的 #编号）")
    verdict: Literal["hold", "tighten_stop", "adjust_tp", "reduce", "add", "close"] = Field(description="继续拿 / 收紧止损 / 调整止盈 / 减仓 / 加仓 / 平仓")
    reason: str = Field(description="一句话理由，带上关键价位")


class CycleReport(BaseModel):
    """Agent 每次复盘的结论。"""

    bias: Literal["long", "short", "neutral"]
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(description="2-4 句中文：现在什么行情、做了什么、为什么")
    plan: TradePlan | None = Field(
        None,
        description="当前有效的交易计划。空仓时这就是下单指令：程序会直接照它执行（价格在入场区间就市价进，没到就布置成到价进场的策略），"
        "所以只写你现在就愿意成交的计划；不想让程序下单就留空，并在 no_plan_reason 里选原因",
    )
    no_plan_reason: Literal["", "cost", "objection", "limit", "no_edge"] = Field(
        "",
        description="空仓又不给 plan 时必填，只能是这四种：cost = 找不到扣完手续费和点差后净盈亏比够格的结构位（summary 里写出你算的入场 / 止损 / 目标）；"
        "objection = 工具返回了明确的反对信号（资金费率极端、点差过大、重大事件临近），或者已布置的计划所依据的结构被 K 线收盘破坏了（summary 里写出具体数据）；"
        "limit = 当日亏损上限、目标已结束或用户不让开仓；no_edge = 这轮没有候选，或者候选用的打法机械照做回测是亏的、你也拿不出算法看不到的额外依据（summary 里写出是哪条打法、缺什么依据）。「再等确认」不是原因：有优势就给一个挂在结构位上的 pullback / breakout 计划，没有优势就选 no_edge",
    )
    on_signal: Literal["verify", "long", "short", "both"] = Field(
        "verify",
        description="空仓时，程序快判（每几秒用 1 分钟 K 线判断收盘突破、放量急动、假突破收回）出了信号怎么办：verify = 叫醒我核实（默认）；"
        "long / short / both = 这些方向的信号我事先同意，程序直接照它算好的价位进场，做完再告诉我。只在大方向明确、你愿意让程序替你抢这一下的时候才授权",
    )
    lines: list[PlanLine] = Field(
        default_factory=list, max_length=8,
        description="计划线：这一轮的预案，每轮必交。空仓时现价上方至少一条、下方至少一条——往上走到哪里做什么、往下走到哪里做什么都要事先定好；"
        "到了由程序执行（进场 / 撤单 / 叫醒你），两次看盘之间不会有人替你盯",
    )
    lot_verdicts: list[LotVerdict] = Field(default_factory=list, description="有持仓时必填：对「各笔仓位」里的每一笔各给一个结论；没有持仓留空")


class ClosedTrade(BaseModel):
    """一笔已经平掉的仓位（来自欧易的历史持仓记录）。"""

    inst_id: str
    side: str
    open_px: float
    close_px: float
    pnl: float = Field(description="已实现盈亏（含手续费和资金费），USDT")
    pnl_pct: float = Field(description="相对保证金的收益率 %")
    leverage: float
    opened_ts: int
    closed_ts: int
    how: str = Field(description="出场方式：全部平仓 / 部分平仓 / 强平 / 自动减仓")
