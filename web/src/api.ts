// 后端接口与类型

export type Env = "demo" | "live";
export type Mode = "advise" | "confirm" | "auto";
export type Permission = "auto" | "confirm" | "off";
export const ACTION_KINDS = ["open", "add", "set_leverage", "set_stops", "reduce", "close", "cancel_orders"] as const;
export function permissionFor(cfg: { mode: Mode; auto_exit: boolean; permissions: Record<string, Permission> }, kind: string): Permission {
  if (cfg.permissions?.[kind]) return cfg.permissions[kind];
  if (cfg.mode === "advise") return "confirm";
  if (cfg.mode === "confirm" && !(cfg.auto_exit && ["reduce", "close", "cancel_orders"].includes(kind))) return "confirm";
  return "auto";
}
export interface AlgoEvent { id: number; ts: number; algo: string; side: string; px: number; text: string; effect: string; checks: Record<string, { px: number; move: number; ok: "right" | "wrong" | "flat" }> }
export interface AlgoReport { events: AlgoEvent[]; stats: Record<string, { right: number; wrong: number; flat: number }>; labels: Record<string, string>; layers?: Record<string, string> }
/** 算法这一轮给 Worker 的一条入场候选（入场 / 止损 / 目标都核过算术），以及这条打法机械照做的回测成绩。 */
export interface Candidate {
  strategy: string; basis: string; align: string; side: "long" | "short"; how: string; entry: number; stop: number; targets: number[];
  ok: boolean; why_not: string; net_rr: number; stop_pct: number; cost_r: number | null; evidence: string;
}
export interface Regime { kind: string; label: string; side: string; why: string; ts: number; plays: string[]; candidates: Candidate[]; holding: boolean }
export interface BacktestGroup {
  label: string; verdict: string; n: number; win: number; avg_r: number; sum_r: number; sum_pct: number; pf: number | null; max_dd_r: number;
  ci_lo: number | null; ci_hi: number | null; worst_streak: number;
}
export interface BacktestReport {
  name: string; title: string; saved_at: number; meta: Record<string, string | number | string[] | null>; groups: BacktestGroup[]; equity: [number, number][];
  trades: { ts: number; exit_ts: number; inst: string; side: string; strategy: string; how: string; out: string; r: number; pct: number; entry: number; stop: number; exit: number }[];
}
export interface CacheStat { reviews: number; input_tokens: number; cached_tokens: number; cache_write_tokens?: number; hit_pct: number | null; first_hit_pct: number | null; stable_prompt_pct: number | null; unmeasured_requests: number }
export interface CacheStats extends CacheStat {
  instruments: Record<string, CacheStat>;
  recent: { ts: number; inst_id: string; input_tokens: number; cached_tokens: number; first_cached_tokens: number | null; model_requests: number }[];
}
export type Style = "scalp" | "intraday" | "swing";
export const STYLES: { id: Style; label: string; desc: string }[] = [
  { id: "scalp", label: "超短线", desc: "赚 5 分钟结构里一小段的钱。看 1m / 5m / 15m / 1H，一笔单拿几分钟到一小时，止损 0.2%~0.6%，最多 3 分钟复盘一次" },
  { id: "intraday", label: "日内短线", desc: "赚当天主要那一段走势的钱。看 5m / 15m / 1H / 4H，一笔单拿几十分钟到几小时，止损 0.5%~1.5%，尽量当天了结" },
  { id: "swing", label: "波段", desc: "赚几天的趋势延续。看 15m / 1H / 4H / 1D，一笔单拿一到几天，止损 1.5%~4%，杠杆更低" },
];
export const STANCES: { id: "cautious" | "balanced" | "active"; label: string; desc: string }[] = [
  { id: "cautious", label: "保守", desc: "多个条件共振才出手，宁可错过" },
  { id: "balanced", label: "均衡", desc: "方向明确加一个入场信号就出手" },
  { id: "active", label: "积极", desc: "到了区间边缘或回踩位就直接进，价格在中间时提前在两边挂限价单；每次复盘都要给出具体计划" },
];
export type Effort = "default" | "low" | "medium" | "high" | "xhigh";
export const EFFORTS: { id: Effort; label: string }[] = [
  { id: "default", label: "模型默认" },
  { id: "low", label: "低（快）" },
  { id: "medium", label: "中" },
  { id: "high", label: "高" },
  { id: "xhigh", label: "极高（慢）" },
];

export interface OrderBookLevels {
  asks: [number, number][];
  bids: [number, number][];
}

export interface ClosedTrade {
  inst_id: string;
  side: "long" | "short";
  open_px: number;
  close_px: number;
  pnl: number;
  pnl_pct: number;
  leverage: number;
  opened_ts: number;
  closed_ts: number;
  how: string;
}

export interface RiskLimits {
  max_leverage: number;
  risk_per_trade_pct: number;
  max_position_notional_x: number;
  max_total_notional_x: number;
  max_adds: number;
  allow_averaging_down: boolean;
  max_daily_loss_pct: number;
  emergency_exit_loss_pct: number;
}

export interface AgentConfig {
  env: Env;
  mode: Mode;
  auto_exit: boolean;
  hands_off_manual: boolean;
  /** 用户偏好：常用杠杆范围（0 = 不指定） */
  pref_leverage_min: number;
  pref_leverage_max: number;
  /** 用户偏好：一句一条，每个 Agent 每轮都读到 */
  prefs: string[];
  /** 盈利从最高点回吐到一定比例时叫醒 Worker 判断 */
  giveback_wake: boolean;
  /** 回吐百分之多少叫醒；0 = 按风格 */
  giveback_pct: number;
  /** 回吐继续扩大而 Worker 没结论时，先把止损移到保本 */
  giveback_backstop: boolean;
  /** 浮亏到止损距离的这个比例时叫醒 Worker；0 = 关闭 */
  loss_wake_r: number;
  permissions: Record<string, Permission>;
  style: Style;
  styles: Record<string, Style>;
  default_stop_pct: number;
  instruments: string[];
  margin_mode: "cross" | "isolated";
  tick_seconds: number;
  /** 定时看盘按哪个周期的 K 线收盘（分钟）；0 = 按风格自动 */
  look_minutes: number;
  max_event_wakes: number;
  min_review_seconds: number;
  /** 记录器算多空分数用的标准 */
  setup_min_score: number;
  setup_vol_ratio: number;
  setup_near_pct: number;
  wake_move_pct: number;
  reasoning_effort: Effort;
  /** Router 的思考强度 */
  router_effort: Effort;
  /** 子代理的思考强度 */
  sub_effort: Effort;
  notes: string;
  risk: RiskLimits;
  stance: Stance;
  entry_mode: "market" | "limit" | "auto";
  /** 计划就是指令：Worker 给出计划，程序直接照着下单 */
  auto_arm_plan: boolean;
  log_keep_days: number;
  /** 快判信号处理方式：off 关 / verify 叫醒核实 / agent 授权方向直接下单 / auto 双向直接下单 */
  fast_entry?: FastMode;
  /** 挂单手续费（%，单边） */
  maker_fee_pct?: number;
  /** 吃单手续费（%，单边），程序用它算净盈亏比 */
  taker_fee_pct: number;
  max_parallel: number;
  limit_dist_pct: number;
  stale_minutes: number;
  wake: Wake;
  router_model: string;
  worker_model: string;
  intel_model: string;
  intel_minutes: number;
  /** 情报子代理可以用模型自带的联网搜索 */
  web_search: boolean;
  /** 流式输出：Agent 边想边出字 */
  stream?: boolean;
  /** 每个子代理的开关：intel / smart / risk */
  subagents: Record<string, boolean>;
  slow_path: boolean;
  workers: Record<string, WorkerOverride>;
  /** Worker 被行情事件叫醒时的思考强度（要快） */
  trigger_effort?: Effort;
}

export type Stance = "cautious" | "balanced" | "active";
export type Wake = "event" | "always";
export type FastMode = "off" | "verify" | "agent" | "auto";
export const FAST_MODES: { id: FastMode; label: string }[] = [
  { id: "verify", label: "叫醒核实" },
  { id: "agent", label: "授权方向直下" },
  { id: "auto", label: "双向直接下单" },
  { id: "off", label: "关闭" },
];
export type QuantMode = "auto" | "trend" | "range" | "breakout" | "quiet";
export type QuantBias = "both" | "long" | "short";
export const QUANT_MODES: { id: QuantMode; label: string; desc: string }[] = [
  { id: "auto", label: "自动识别", desc: "大方向明确时按顺势回踩算，震荡时按箱体上下沿算" },
  { id: "trend", label: "顺势回踩", desc: "单边趋势用：只盯顺大方向的变盘回踩与前高前低回踩，不追高杀跌、屏蔽逆势信号" },
  { id: "range", label: "震荡箱体", desc: "横盘拉锯用：停止追突破，只盯箱体下沿撑住接多、上沿受阻接空" },
  { id: "breakout", label: "收敛突破", desc: "窄幅蓄势待选方向用：不在箱体中间挨打，盯放量收盘冲出边界的第一下" },
  { id: "quiet", label: "静默防守", desc: "乱插针或消息前后用：暂停新开仓快判信号，只做变盘撤旧单与持仓保护" },
];
export const QUANT_BIASES: { id: QuantBias; label: string }[] = [
  { id: "both", label: "双向都盯" },
  { id: "long", label: "只盯做多" },
  { id: "short", label: "只盯做空" },
];
export interface WorkerOverride {
  /** 这个 Worker 定时看盘的周期（分钟）；不设跟全局走，0 = 按风格自动 */
  look_minutes?: number | null;
  wake?: Wake | null;
  model: string;
  effort?: Effort | null;
  paused: boolean;
  /** 这个 Worker 的审批方式；不设跟全局走 */
  mode?: Mode | null;
  /** 被行情事件叫醒时的思考强度；不设跟全局走 */
  trigger_effort?: Effort | null;
  /** 暂停到什么时候自动恢复（毫秒时间戳） */
  resume_ts?: number | null;
  /** 谁、为什么暂停的 */
  paused_by?: string;
  /** 待命：空仓时不做定时复盘，只在触发线、策略、留言、持仓变化时醒来 */
  standby?: boolean;
  /** 这个 Worker 单独的出手尺度 / 入场方式 / 快判模式；不设跟全局走 */
  stance?: Stance | null;
  entry_mode?: "market" | "limit" | "auto" | null;
  fast_entry?: FastMode | null;
  quant_mode?: QuantMode;
  quant_bias?: QuantBias;
  quant_vol_ratio?: number | null;
  quant_reason?: string;
  quant_by?: string;
}

/** 长期经验里的一条 */
export interface Lesson {
  id: number;
  /** 空 = 对所有币都成立 */
  inst_id: string;
  text: string;
  grade: "observe" | "rule";
  confirms: number;
  refutes: number;
  ts: number;
  used_ts: number;
  /** review 平仓后自动复盘 / worker Worker 自己记的 / user 你加的 */
  source: "review" | "worker" | "user";
  trade?: { side: string; pnl: number; pnl_pct: number; minutes: number; how: string; playbook?: string | null } | null;
  stale?: boolean;
  note?: string;
}
export interface LessonList {
  env: "demo" | "live";
  /** 当前环境的行情经验（模拟盘和实盘各一套） */
  lessons: Lesson[];
  /** 你定的规矩，两边共用 */
  shared: Lesson[];
}

/** 装给 Worker 用的技能：项目 skills 文件夹里的一个子文件夹（里面是 SKILL.md） */
export interface Skill {
  id: string;
  name: string;
  description: string;
  chars: number;
  enabled: boolean;
  /** 里面有想让 Agent 绕开保护的内容时的提醒 */
  warnings: string[];
  path: string;
  ts: number;
}
export interface SkillList {
  dir: string;
  skills: Skill[];
}

/** 图上画的关键位：和 Worker 看到的是同一套算法 */
export interface StructureShift {
  side: "long" | "short";
  kind: string;
  level: number;
  ts: number;
  bars_ago: number;
  origin: number;
  extreme: number;
  why: string;
}
export interface ChartLevels {
  range: { upper: number; lower: number; where: string; position_pct: number } | null;
  support: { price: number; touches: number }[];
  resistance: { price: number; touches: number }[];
  patterns: string[];
  windows: Record<string, { prev_high: number; prev_low: number; trend: string; now: string; plan?: string }>;
  shift?: StructureShift | null;
  /** 上一个 UTC 日的价值区间：成交最密集的价格 poc，包住七成成交量的上沿 vah / 下沿 val */
  value_area?: { poc: number; vah: number; val: number; open?: string; now?: string; read?: string } | null;
  /** 开盘以来怎么走的：开盘价、相对开盘的涨跌、哪种走法，以及开盘后第一个小时的高低点 */
  session?: {
    since: string; open: number; change_pct: number; high: number; low: number; kind: string; read: string; path: string; text: string;
    opening_range?: { minutes: number; high: number; low: number; dir: string; now: string };
  } | null;
}

export interface TradePlan {
  direction: "long" | "short";
  entry_low: number;
  entry_high: number;
  stop_loss: number;
  take_profits: number[];
  leverage: number;
  /** 怎么进场：now 现价就进 / pullback 等回到入场区间 / breakout 等突破 */
  entry_type?: "now" | "pullback" | "breakout";
  /** 采用的策略名 */
  strategy?: string;
  valid_minutes?: number | null;
  risk_pct?: number | null;
  invalidation: string;
  add_condition: string;
  exit_condition: string;
}

export const ENTRY_TYPE: Record<string, string> = { now: "现价进", pullback: "等回到入场区间", breakout: "等突破" };
export const NO_PLAN: Record<string, string> = { cost: "扣完手续费净盈亏比不够", objection: "有明确的反对信号", limit: "到了亏损上限或你不让开仓", no_edge: "本轮没有合适候选，或回测未显示优势且没有额外依据" };
export const noPlanText = (reason: string) => NO_PLAN[reason] ?? "暂未给出可执行计划，具体依据见复盘结论";
export const ENTRY_MODES: { id: "market" | "limit" | "auto"; label: string }[] = [
  { id: "market", label: "市价优先" },
  { id: "limit", label: "挂单优先" },
  { id: "auto", label: "Agent 自己定" },
];

export interface Report {
  bias: "long" | "short" | "neutral";
  confidence: number;
  summary: string;
  plan: TradePlan | null;
  /** 空仓又不给计划时的原因 */
  no_plan_reason?: "" | "cost" | "objection" | "limit" | "no_edge";
  /** 程序照计划做了什么：executed 已进场 / armed 已布置等触发 / kept 沿用上一轮 / skipped 没通过核对 / pending 等你同意 / cancelled 已撤 / failed */
  plan_order?: { status: string; text: string; strategy_id?: number; pending_id?: number; ord_id?: string } | null;
  /** 计划线：到了由程序执行。wake 叫醒 Worker / cancel 撤掉入场 / enter 按现价进场 */
  lines?: { price: number; note: string; when?: "price_touch" | "close_above" | "close_below"; bar?: string | null; do?: "wake" | "cancel" | "enter"; side?: "long" | "short" | null; stop_loss?: number | null; take_profit?: number | null }[];
  ts: number;
  price: number;
  reason: string;
  actions: { status: string; title: string; message: string }[];
  knowledge: string[];
  tokens: number;
  input_tokens?: number;
  cached_tokens?: number;
  first_cached_tokens?: number | null;
  model_requests?: number;
  unmeasured_requests?: number;
  effort?: Effort;
  model?: string;
  lot_verdicts?: LotVerdict[];
}

export interface InstStatus {
  last: number;
  reviewing: boolean;
  last_review_ts: number | null;
  next_review_in: number | null;
  last_reason: string;
  error: string;
  error_work_id?: number | null;
  review_started_ts?: number | null;
  retry_after_ts?: number | null;
  report: Report | null;
  style?: Style;
  stance?: Stance;
  entry_mode?: "market" | "limit" | "auto";
  fast?: {
    mode: FastMode;
    on: boolean;
    on_signal: string;
    last?: { ts: number; side: string; kind: string; strength: number; text: string; status: string; result: string } | null;
    shift?: StructureShift | null;
  };
  /** 「转折」算法现在走到哪一步：苗头 → 确认 → 盘中失守 / 收回 */
  turn?: { stage: "early" | "confirmed" | "lost" | "reclaimed"; label: string; side: string; ts: number; level?: number | null; text: string } | null;
  quant?: {
    opening_quote?: { ts: number; price: number; session_open?: number; session_pct?: number; bar_open?: number; bar_pct?: number; pre_open?: { change_pct: number; gap_pct: number; close: number } | null } | null;
    last_retest?: { ts: number; text: string } | null;
    sample_ts?: number | null;
    mode: QuantMode;
    label: string;
    desc: string;
    bias: QuantBias;
    vol_ratio?: number | null;
    reason: string;
    by: string;
    narrative: string;
  };
  wake?: Wake;
  /** 最近一次复盘时程序判断的行情和给出的候选位 */
  regime?: Regime | null;
  model?: string;
  paused?: boolean;
  skips?: number;
  last_gate?: string;
  illiquid?: string;
  fails?: number;
  memory?: { ts: number; note: string }[];
  intel?: IntelBrief | null;
  intel_running?: boolean;
  /** 交易 Worker 手下的子代理：情报 / 聪明钱 / 风险复核 */
  team?: SubAgent[];
  /** Worker 自己临时开的子代理（最近几个） */
  adhoc?: { id: number; name: string; task: string; running: boolean; summary: string; bias: string; ts: number; keep?: boolean }[];
  /** 这个币上正在运行的策略（执行器按规则自动执行，不调用模型） */
  strategies?: Strategy[];
  goal?: Goal | null;
  /** Worker 现在在做什么；空 = 空闲 */
  busy?: string;
  /** 排在后面等这个 Worker 的事有几件 */
  queue?: number;
  /** 信箱里还没读的留言 */
  inbox?: number;
  /** Agent 挂的限价单各自的撤单条件：程序盯着，到了自动撤 */
  rules?: Rule[];
  /** 记录器的数据：程序每 10 秒记一次的数值、现在的线、最近几根 K 线各记的一笔 */
  algos?: { id: string; label: string; on: boolean; ts: number | null; reading: string; does: string; layer?: string }[];
  tracks?: { lot_id: number; side: string; entry: number; r0: number; peak: number; peak_px: number; peak_ts: number; low: number; now?: number; locked?: number | string | null }[];
  data?: { ts: number; fast_ts?: number; quote_ts?: number; turn?: { side: string; stage: number; text: string } | null; values: Record<string, number | null>; levels: Record<string, number>; log: { ts: number; close: number; reading?: string; vol_ratio: number; chg_pct: number; score_long: number; score_short: number; trend: number }[] } | null;
  order_guards?: { ord_id: string; side: "long" | "short"; price: number; cancel_px: number | null; expires_ts: number | null; ts: number }[];
  standby?: boolean;
  /** 它调用了几个要你同意的操作，正挂着等你决定 */
  awaiting?: number;
}

export type SubRole = "intel" | "smart" | "risk";
export interface SubAgent {
  role: SubRole;
  label: string;
  does: string;
  running: boolean;
  brief: (IntelBrief & { by?: string }) | null;
}

export interface Strategy {
  id: number;
  ts: number;
  updated_ts: number;
  inst_id: string;
  kind: "breakout" | "pullback" | "range" | "manage" | "martingale" | "grid" | "profit_guard" | "trigger" | "reverse";
  side: "long" | "short" | "";
  status: "proposed" | "armed" | "active" | "done" | "cancelled" | "expired" | "failed";
  params: Record<string, any>;
  state: Record<string, any>;
  note: string;
  label: string;
  text: string;
}

/** 触发器清单里的一行：等什么 → 做什么 */
export interface RecorderCond {
  data: string;
  op: "above" | "below";
  level: number;
}

export interface Rule {
  action?: string;
  id: string;
  type: string;
  label: string;
  when: string;
  do: string;
  /** auto 程序直接执行；wake 只叫醒 Worker；worker 由 Worker 对照 */
  mode: "auto" | "wake" | "worker" | "task";
  status: "waiting" | "running" | "proposed" | "done" | "failed" | "cancelled" | "expired";
  by: string;
  ts: number;
  updated_ts: number;
  note: string;
  level: number | null;
  /** 触发线的条件（price_touch / close_above / close_below …）和它看的 K 线周期 */
  cond?: string | null;
  bar?: string | null;
  strategy_id: number | null;
  expires_ts: number | null;
  ord_id?: string;
  /** 记录器：Worker 小组里的节点，条件是程序记录的几项数据，数值可以直接改 */
  recorder?: boolean;
  conds?: RecorderCond[] | null;
  repeat?: boolean;
  cooldown_minutes?: number | null;
  off?: boolean;
  runs?: number;
  last_run?: number | null;
}

/** 交给某个币的 Worker 的目标 */
export interface Goal {
  goal: string;
  max_loss_usdt: number | null;
  until_ts: number | null;
  ts: number;
  status: "active" | "stopped" | "ended" | "done";
  pnl: number;
  /** 净赚到多少就停（代码核对） */
  target_usdt?: number | null;
  /** 别的停止条件（一句话，Worker 对照） */
  stop_when?: string;
  /** 和哪些币合起来算净赚 */
  shared_with?: string[];
  /** 合起来的净赚 */
  total?: number;
  by: string;
  note: string;
}

/** 活动流里的一步 */
export interface Activity {
  id: number;
  ts: number;
  inst_id: string;
  agent: "router" | "worker" | "general" | "strategy" | "watchdog" | "adhoc" | SubRole;
  work_id: number | null;
  kind: "wake" | "tool" | "result" | "handoff" | "action" | "done" | "skip" | "error" | "queued" | "interject" | "ask" | "answer" | "closed";
  text: string;
  data: Record<string, any>;
}

export interface ErrorDiagnostic {
  type: string;
  message: string;
  status_code?: string;
  model_name?: string;
  request_id?: string;
  truncated?: boolean;
  causes?: ErrorDiagnostic[];
  frames?: { file: string; line: number; function: string }[];
}

/** 情报 Worker 的简报 */
export interface IntelBrief {
  bias: "long" | "short" | "neutral";
  summary: string;
  points: string[];
  unavailable: string[];
  ts: number;
  question?: string;
}

export interface LotVerdict {
  lot_id: number;
  verdict: "hold" | "tighten_stop" | "adjust_tp" | "reduce" | "add" | "close";
  reason: string;
}

/** 一笔仓位（欧易里合并成一个持仓，项目里按笔分开记） */
export interface Lot {
  id: number;
  ts: number;
  inst_id: string;
  side: "long" | "short";
  style: Style;
  contracts: number;
  entry_px: number;
  stop_loss: number | null;
  take_profit: number | null;
  source: string;
  status: "open" | "working" | "closed";
  pnl_pct: number | null;
}

/** Router 派出去的一条任务 */
export interface WorkItem {
  id: number;
  ts: number;
  updated_ts: number;
  inst_id: string;
  source: string;
  goal: string;
  status: "queued" | "running" | "waiting" | "done" | "failed" | "skipped";
  model: string;
  result: string;
}

export interface AgentStatus {
  running: boolean;
  error: string;
  instruments: Record<string, InstStatus>;
  /** 通用 Worker：不绑定某个币，Router 把普通问题交给它 */
  general?: { busy: boolean; task: string; summary: string; ts: number };
  /** 通用 Worker 临时开的子代理（最近几个） */
  router_adhoc?: { id: number; name: string; task: string; running: boolean; summary: string; bias: string; ts: number; keep?: boolean }[];
  watchdog?: {
    restarts: number;
    last: string;
    beat_age: number | null;
    /** 最近一次巡检发现的问题；空 = 一切正常 */
    checks?: { key: string; level: "bad" | "warn"; inst_id: string; text: string }[];
    checked_ts?: number;
  };
  /** 并发名额：同时在思考的 Worker / 子代理有几个、排队的有几个 */
  pool?: Record<"workers" | "subagents", { running: number; waiting: number; max: number }>;
}

export interface AuthStatus {
  status: "logged_in" | "pending" | "not_logged_in" | "error";
  site?: string;
  scopes?: string | null;
  verificationUri?: string;
  userCode?: string;
  ttl?: number;
  expiresIn?: number;
  error?: string;
}

export interface KbDocItem {
  source: string;
  title: string;
  category?: "core" | "strategy" | "guide";
  chunks: number;
  chars: number;
  /** 这条打法的战绩（程序按平仓结果自动记的），没做过是空串 */
  score?: string;
}
export interface LogsInfo {
  keep_days: number;
  counts: Record<string, number>;
  db_mb: number;
  okx_mb: number;
}
export interface KbStats {
  documents: KbDocItem[];
  chunks: number;
  retrieval: string;
  embedding_model: string | null;
  embedding_error: string | null;
}

export interface AppState {
  version: string;
  auth: AuthStatus;
  site: string;
  config: AgentConfig;
  agent: AgentStatus;
  ai: { model: string; configured: boolean };
  kb: KbStats;
}

export interface Position {
  inst_id: string;
  side: "long" | "short";
  contracts: number;
  avg_px: number;
  mark_px: number;
  upl: number;
  upl_ratio: number;
  leverage: number;
  liq_px: number | null;
  margin_mode: string;
  notional_usd: number;
  margin: number;
}

export interface Protection {
  algo_id: string;
  inst_id: string;
  ord_type: string;
  side: string;
  sz: string;
  stop_loss: number | null;
  take_profit: number | null;
  trigger_px: number | null;
}

export interface OpenOrder {
  leverage?: number;
  notional_usd?: number;
  margin_usd?: number;
  reduce_only?: boolean;
  ord_id: string;
  inst_id: string;
  side: string;
  ord_type: string;
  px: number | null;
  sz: number;
  filled: number;
}

export interface Account {
  env: Env;
  equity: number;
  available: number;
  cash_usdt: number;
  margin_used: number;
  upl: number;
  day_pnl: number | null;
  account_level: string;
  position_mode: string;
  positions: Position[];
  orders: OpenOrder[];
  protections: Protection[];
  day_start_equity: number | null;
  day_pnl_pct: number | null;
  halted: boolean;
}

export interface Ticker {
  inst_id: string;
  last: number;
  change_pct: number;
  high_24h: number;
  low_24h: number;
  turnover_usd: number;
  max_leverage: number | null;
  ct_val: number | null;
}

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface JournalEntry {
  id: number;
  ts: number;
  env: string;
  inst_id: string;
  kind: "cycle" | "action" | "alert" | "system";
  title: string;
  body: Record<string, any>;
}

export interface TradeAction {
  kind: "set_leverage" | "open" | "add" | "reduce" | "close" | "set_stops" | "cancel_orders";
  inst_id: string;
  side?: "long" | "short";
  leverage?: number;
  order_type?: "market" | "limit";
  price?: number;
  stop_loss?: number;
  take_profit?: number;
  risk_pct?: number;
  fraction?: number;
  margin_usdt?: number;
  margin_mode?: "cross" | "isolated";
  style?: Style;
  lot_id?: number;
  reason?: string;
}

export interface ActionResult {
  /** 手动开仓 / 加仓后：有没有 Worker 来接管这笔仓位 */
  worker_note?: string;
  status: "executed" | "pending" | "advised" | "rejected" | "failed";
  message: string;
  detail: Record<string, any>;
  action?: TradeAction;
  pending_id?: number | null;
  /** 等你同意的是「布置策略」时，这是那条策略的编号 */
  strategy_id?: number | null;
  /** 方案卡片的当前状态：waiting 等确认 / running / executed / rejected / failed / declined / expired */
  pending_status?: string | null;
  pending_result?: { message?: string; detail?: Record<string, any> } | null;
}

/** Agent 给出的交易方案里算好的数字 */
export interface PlanPreview {
  contracts: string;
  leverage: number;
  entry: number;
  entry_low: number;
  entry_high: number;
  stop_loss: number;
  take_profits: number[];
  notional_usd: number;
  margin_usd: number;
  risk_usd: number;
  reward_risk: number | null;
  margin_mode: "cross" | "isolated";
  notes: string[];
}

export interface ChatMessage {
  role: "user" | "ai";
  text: string;
  error?: ErrorDiagnostic;
  ts?: number;
  env?: AgentConfig["env"];
  actions?: ActionResult[];
  steps?: ChatStep[];
  confirms?: ConfigConfirm[];
  /** 谁说的：某个币的 Worker；没有就是 Router */
  from?: string;
  /** Agent 主动发的，不是回答用户 */
  proactive?: boolean;
  to?: "router" | "worker";
  /** 这句话附的图片：刚发出去时是 data URL，存下来之后是 data/uploads/ 里的文件名 */
  images?: string[];
}

/** 对话里一张图片的地址 */
export const shotUrl = (x: string) => (x.startsWith("data:") ? x : `/api/uploads/${x}`);

/** Agent 在这一轮对话里调用的一个工具 */
export interface ChatStep {
  tool: string;
  label: string;
  args: Record<string, any>;
  result: string;
  ok?: boolean;
}

/** 需要用户点确认才生效的设置修改（放宽权限 / 放宽风控） */
export interface ConfigConfirm {
  type: "confirm_config";
  summary: string;
  patch: Partial<AgentConfig>;
}

/** Agent 让界面执行的动作 */
export type UiCommand =
  | { type: "select_inst"; inst_id: string }
  | { type: "set_bar"; bar: string }
  | { type: "refresh" }
  | ConfigConfirm
  | ({ type: "open_trade"; inst_id: string; side: "long" | "short" } & Partial<Pick<TradeAction, "order_type" | "price" | "stop_loss" | "take_profit" | "margin_usdt" | "leverage" | "margin_mode">>);

export interface PlanReceipt {
  state: string;
  ord_id?: string;
  price?: number;
  filled?: number;
  text?: string;
}

export interface PendingItem {
  id: number;
  ts: number;
  title: string;
  action: TradeAction;
  preview?: Partial<PlanPreview>;
}

export interface OkxTool {
  name: string;
  module: string;
  title: string;
  description: string;
  readOnly: boolean;
  destructive: boolean;
  schema: { properties?: Record<string, any>; required?: string[] };
}

export interface InstrumentInfo {
  inst_id: string;
  inst_type: string;
  ct_val: number;
  lot_sz: string;
  min_sz: string;
  tick_sz: string;
  max_leverage: number;
  leverage: number | null;
}

export class ApiError extends Error {
  status: number;
  needsLogin: boolean;
  locked: boolean;
  constructor(message: string, status: number, body: any) {
    super(message);
    this.status = status;
    this.needsLogin = !!body?.needs_login;
    this.locked = !!body?.locked;
  }
}

/** 需要欧易授权的操作在没登录时调用它：弹出登录窗口。 */
export function needLogin() {
  window.dispatchEvent(new CustomEvent("ouy:need-login"));
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json", ...(init.headers || {}) } : init?.headers,
  });
  let body: any = null;
  try {
    body = await resp.json();
  } catch {
    /* 非 JSON 响应 */
  }
  if (!resp.ok) {
    const detail = body?.detail;
    let message = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d: any) => d.msg).join("；") : `请求失败（${resp.status}）`;
    if (body?.needs_login) {
      message = "这个操作需要先登录欧易";
      if (init?.method && init.method !== "GET") needLogin();
    }
    throw new ApiError(message, resp.status, body);
  }
  return body as T;
}

const get = <T,>(path: string) => request<T>(path);
const post = <T,>(path: string, data?: unknown) => request<T>(path, { method: "POST", body: data === undefined ? undefined : JSON.stringify(data) });

export const api = {
  state: () => get<AppState>("/api/state"),
  unlock: (key: string) => post<{ ok: boolean }>("/api/unlock", { key }),
  login: () => post<AuthStatus>("/api/auth/login"),
  authStatus: () => get<AuthStatus>("/api/auth/status"),
  logout: () => post("/api/auth/logout"),
  tickers: (type: "SWAP" | "SPOT" = "SWAP") => get<Ticker[]>(`/api/market/tickers?inst_type=${type}`),
  candles: (inst: string, bar: string, limit = 300, src: "env" | "live" = "env") =>
    get<Candle[]>(`/api/market/candles?inst_id=${inst}&bar=${bar}&limit=${limit}&src=${src}`),
  lots: (inst?: string) => get<Lot[]>(`/api/lots${inst ? `?inst_id=${inst}` : ""}`),
  work: (limit = 60, inst?: string) => get<WorkItem[]>(`/api/work?limit=${limit}${inst ? `&inst_id=${inst}` : ""}`),
  workDetail: (id: number) => get<WorkItem & { activity: Activity[] }>(`/api/work/${id}`),
  intel: (inst: string, role: SubRole = "intel", question = "") => post<IntelBrief>("/api/agent/intel", { inst_id: inst, role, question }),
  logsInfo: () => get<LogsInfo>("/api/logs"),
  clearLogs: (keepDays: number) => post<LogsInfo & { cleared: Record<string, number>; okx_files: number }>("/api/logs/clear", { keep_days: keepDays }),
  activity: (limit = 300, after = 0) => get<Activity[]>(`/api/activity?limit=${limit}&after=${after}`),
  strategies: (all = false) => get<Strategy[]>(`/api/strategies?all=${all}`),
  profitGuard: (id: number, rules: { on_trigger: "notify" | "close"; arm_pct: number; giveback_pct: number; stall_minutes: number; cost_pct: number }) =>
    post<{ id: number }>(`/api/lots/${id}/profit-guard`, rules),
  strategyAct: (id: number, verb: "approve" | "cancel") => post<Strategy>(`/api/strategies/${id}/${verb}`),
  editStrategy: (id: number, patch: { conds?: RecorderCond[]; level?: number; cooldown_minutes?: number; off?: boolean }) => post<Strategy>(`/api/strategies/${id}/edit`, patch),
  addLine: (body: { inst_id: string; when: "price_touch" | "close_above" | "close_below"; level: number; bar?: string; do: "wake" | "cancel_orders" | "close" | "open"; side?: "long" | "short"; stop_loss?: number; take_profit?: number; note?: string }) =>
    post<Strategy>("/api/lines", body),
  addRecorder: (body: { inst_id: string; conds: RecorderCond[]; note: string; cooldown_minutes: number }) => post<Strategy>("/api/recorders", body),
  interject: (inst: string, message: string) => post<{ delivered: boolean; message: string }>("/api/agent/interject", { inst_id: inst, message }),
  standby: (inst: string, on: boolean) => post<AgentStatus>("/api/agent/standby", { inst_id: inst, on }),
  setGoal: (inst: string, goal: string, max_loss_usdt?: number, hours?: number, target_usdt?: number, stop_when?: string) =>
    post<Goal>("/api/agent/goal", { inst_id: inst, goal, max_loss_usdt, hours, target_usdt, stop_when: stop_when ?? "" }),
  clearGoal: (inst: string) => request(`/api/agent/goal?inst_id=${inst}`, { method: "DELETE" }),
  instrument: (inst: string) => get<InstrumentInfo>(`/api/market/instrument?inst_id=${inst}`),
  ticker: (inst: string) => get<{ inst_id: string; last: number; change_24h_pct: number | null }>(`/api/market/ticker?inst_id=${inst}`),
  orderbook: (inst: string, depth = 8) => get<OrderBookLevels>(`/api/market/orderbook?inst_id=${inst}&depth=${depth}`),
  account: () => get<Account>("/api/account"),
  /** 资金账户各币种余额；查不到时 rows 为空、error 是原因 */
  funding: () => get<{ rows: { ccy: string; balance: number; available: number; frozen: number }[]; error: string }>("/api/account/funding"),
  history: (inst?: string) => get<ClosedTrade[]>(`/api/account/history${inst ? `?inst_id=${inst}` : ""}`),
  equity: (hours = 24) => get<{ ts: number; equity: number }[]>(`/api/account/equity?hours=${hours}`),
  trade: (action: TradeAction) => post<ActionResult>("/api/trade", action),
  panic: () => post<{ results: ActionResult[] }>("/api/trade/panic"),
  saveConfig: (config: AgentConfig, confirmLive = false) =>
    request<{ config: AgentConfig; agent: AgentStatus }>("/api/agent/config", { method: "PUT", body: JSON.stringify({ config, confirm_live: confirmLive }) }),
  agentStart: () => post<AgentStatus>("/api/agent/start"),
  agentStop: () => post<AgentStatus>("/api/agent/stop"),
  review: (inst: string) => post<Report>("/api/agent/review", { inst_id: inst }),
  planReceipt: (inst: string, ts: number) => get<PlanReceipt>(`/api/agent/plan/status?inst_id=${encodeURIComponent(inst)}&ts=${ts}`),
  applyPlan: (inst: string, ts: number) => post<NonNullable<Report["plan_order"]>>(`/api/agent/plan/apply?inst_id=${encodeURIComponent(inst)}&ts=${ts}`),
  journal: (limit = 100, inst?: string) => get<JournalEntry[]>(`/api/journal?limit=${limit}${inst ? `&inst_id=${inst}` : ""}`),
  pending: () => get<PendingItem[]>("/api/pending"),
  approve: (id: number) => post<ActionResult>(`/api/pending/${id}/approve`),
  reject: (id: number) => post(`/api/pending/${id}/reject`),
  /** queue：Worker 正在思考时，默认把话直接送进它当前这一轮；true 表示排到这一轮后面单独问 */
  algo: (inst: string) => get<AlgoReport>(`/api/algo?inst_id=${encodeURIComponent(inst)}`),
  backtests: () => get<BacktestReport[]>("/api/backtest"),
  cacheStats: () => get<CacheStats>("/api/stats/cache"),
  chat: (message: string, effort: Effort | undefined, thread: string, inst: string, to: "router" | "worker" = "router", queue = false, images: string[] = []) =>
    post<{ reply: string; steered?: boolean; actions: ActionResult[]; steps: ChatStep[]; confirms: ConfigConfirm[]; ui: UiCommand[]; from?: string }>(to === "router" && message.trim() === "/简报" && !queue && !images.length ? "/api/chat/consult" : "/api/chat", {
      message,
      effort,
      thread,
      inst_id: inst,
      to,
      queue,
      images,
    }),
  llm: () => get<LlmInfo>("/api/llm"),
  /** 不想改的项传 undefined；传空字符串 = 改回 .env 里的 */
  saveLlm: (body: { base_url?: string; api_key?: string; model?: string }) => request<LlmInfo>("/api/llm", { method: "PUT", body: JSON.stringify(body) }),
  testLlm: () => post<{ ok: boolean; message: string }>("/api/llm/test"),
  levels: (inst: string, bar: string) => get<ChartLevels>(`/api/market/levels?inst_id=${inst}&bar=${bar}`),
  llmModels: () => get<{ models: string[]; default: string }>("/api/llm/models"),
  lessons: () => get<LessonList>("/api/lessons"),
  saveLesson: (body: { id?: number; scope: "env" | "shared"; inst_id: string; text: string; grade?: "observe" | "rule" }) =>
    request<LessonList>("/api/lessons", { method: "PUT", body: JSON.stringify(body) }),
  deleteLesson: (scope: "env" | "shared", id: number) => request<LessonList>(`/api/lessons/${scope}/${id}`, { method: "DELETE" }),
  skills: () => get<SkillList>("/api/skills"),
  readSkill: (id: string) => get<Skill & { content: string }>(`/api/skills/${encodeURIComponent(id)}`),
  installSkill: (body: { name: string; content: string; url: string }) => post<SkillList>("/api/skills", body),
  enableSkill: (id: string, on: boolean) => post<SkillList>(`/api/skills/${encodeURIComponent(id)}/enable?on=${on}`),
  deleteSkill: (id: string) => request<SkillList>(`/api/skills/${encodeURIComponent(id)}`, { method: "DELETE" }),
  playbooks: () => get<Playbook[]>("/api/playbooks"),
  builtinPlaybooks: () => get<BuiltinPlaybook[]>("/api/playbooks/builtin"),
  savePlaybook: (body: { name: string; base: Playbook["base"]; when: string; entry: string; exit: string; defaults: Record<string, number> }) =>
    request<Playbook[]>("/api/playbooks", { method: "PUT", body: JSON.stringify(body) }),
  enablePlaybook: (name: string, on: boolean) => post<Playbook[]>(`/api/playbooks/${encodeURIComponent(name)}/enable?on=${on}`),
  deletePlaybook: (name: string) => request<Playbook[]>(`/api/playbooks/${encodeURIComponent(name)}`, { method: "DELETE" }),
  proxy: () => get<{ url: string; source: "page" | "env" }>("/api/proxy"),
  saveProxy: (url: string) => request<{ url: string; source: "page" | "env" }>("/api/proxy", { method: "PUT", body: JSON.stringify({ url }) }),
  testProxy: () => post<{ ok: boolean; message: string }>("/api/proxy/test"),
  clearChat: (thread: string) => request(`/api/chat?thread=${thread}`, { method: "DELETE" }),
  chatLog: (thread: string) => get<{ messages: ChatMessage[] }>(`/api/chat?thread=${thread}`),
  /** 清除记录：tasks 已完成的任务；triggers 已结束的触发器；talk 对话区。inst 为空 = 所有币 */
  clearHistory: (inst: string, what: "tasks" | "triggers" | "talk") => post<{ cleared: number }>("/api/history/clear", { inst_id: inst, what }),
  chatClear: (thread: string) => request(`/api/chat?thread=${thread}`, { method: "DELETE" }),
  okxTools: () => get<OkxTool[]>("/api/okx/tools"),
  okxCall: (tool: string, args: Record<string, unknown>, confirm = false) => post<{ result: unknown }>("/api/okx/call", { tool, args, confirm }),
  kbSearch: (q: string) => get<{ id: string; source: string; title: string; section: string; text: string; score: number }[]>(`/api/kb/search?q=${encodeURIComponent(q)}`),
  kbDoc: (source: string) => get<{ source: string; title: string; category: string; content: string }>(`/api/kb/doc?source=${encodeURIComponent(source)}`),
  kbSave: (source: string, content: string) => request<{ source: string; title: string; category: string; content: string }>("/api/kb/doc", { method: "PUT", body: JSON.stringify({ source, content }) }),
  kbDelete: (source: string) => request<{ ok: boolean }>(`/api/kb/doc?source=${encodeURIComponent(source)}`, { method: "DELETE" }),
};

// ---------- 格式化 ----------

export function fmtPx(v: number | null | undefined): string {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  const a = Math.abs(v);
  const digits = a >= 1000 ? 1 : a >= 100 ? 2 : a >= 1 ? 3 : a >= 0.01 ? 5 : 8;
  return v.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

export function fmtUsd(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtSigned(v: number, digits = 2): string {
  return `${v >= 0 ? "+" : ""}${fmtUsd(v, digits)}`;
}

export function fmtCompact(v: number): string {
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(0)}K`;
  return v.toFixed(0);
}

export function fmtTime(ts: number): string {
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, "0");
  const sameDay = new Date().toDateString() === d.toDateString();
  return `${sameDay ? "" : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} `}${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function shortInst(inst: string): string {
  return inst.replace("-USDT-SWAP", "").replace("-USDT", "");
}

export const MODE_LABEL: Record<Mode, string> = { auto: "全自动", confirm: "下单前确认", advise: "只给建议" };
export const KIND_LABEL: Record<string, string> = {
  set_leverage: "调杠杆", open: "开仓", add: "加仓", reduce: "减仓", close: "平仓", set_stops: "移止盈止损", cancel_orders: "撤单",
};
export const STATUS_LABEL: Record<string, string> = { executed: "已执行", pending: "待确认", advised: "仅建议", rejected: "风控拒绝", failed: "失败" };

/** 模型接口：地址、默认模型、密钥是否已填（密钥本身不回传，只给后四位） */
/** 策略库里的一条策略：Agent 或你写的、可以反复用的做法 */
/** 内置打法：知识库 strategies/ 里的一篇 */
export interface BuiltinPlaybook {
  id: string;
  name: string;
  when: string;
  score: string;
  losing: boolean;
}

export interface Playbook {
  name: string;
  base: "breakout" | "pullback" | "range" | "manage" | "martingale" | "grid";
  base_label: string;
  when: string;
  entry: string;
  exit: string;
  defaults: Record<string, number>;
  author: string;
  updated_by?: string;
  version: number;
  enabled: boolean;
  off_reason: string;
  /** 当前环境（模拟盘 / 实盘）的战绩，已经写成一句话 */
  score: string;
  stats: Record<string, { n: number; wins: number; pnl: number; last_ts: number }>;
}

export interface LlmInfo {
  base_url: string;
  model: string;
  key_set: boolean;
  key_hint: string;
  source: Record<"base_url" | "api_key" | "model", "page" | "env">;
}

export const LOOK_BARS: { id: number; label: string }[] = [
  { id: 0, label: "跟风格自动" },
  { id: 5, label: "5 分钟盘" },
  { id: 15, label: "15 分钟盘" },
  { id: 30, label: "30 分钟盘" },
  { id: 60, label: "1 小时盘" },
  { id: 240, label: "4 小时盘" },
];
