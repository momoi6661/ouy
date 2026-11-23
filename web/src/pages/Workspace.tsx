import type React from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ACTION_KINDS, EFFORTS, ENTRY_MODES, LOOK_BARS, ENTRY_TYPE, FAST_MODES, KIND_LABEL, MODE_LABEL, noPlanText, STANCES, STYLES, api, shotUrl, fmtPx, fmtTime, fmtUsd, permissionFor, shortInst, STATUS_LABEL,
  type Account, type Activity, type AppState, type FastMode, type WorkerOverride, type Effort, type Mode, type PendingItem, type Permission, type Stance, type Rule, type Style, type UiCommand, type WorkItem,
} from "../api";
import { Chart, useLotEntries, type ChartLine } from "../components/Chart";
import { ErrorDetails } from "../components/ErrorDetails";
import { Positions } from "../components/Positions";
import { AlgoPanel } from "../components/AlgoPanel";
import { LineForm } from "../components/LineForm";
import { AlgoReadings, ReadingConds } from "../components/Recorders";
import { AssetsPanel } from "../components/AssetsPanel";
import { lineCond, lineKind, useLevelLines } from "../components/LevelLines";
import { OrderTicket, PositionCard } from "../components/OrderTicket";
import { PlaybookPanel } from "../components/PlaybookPanel";
import { MemoryPanel } from "../components/MemoryPanel";
import { TeamCanvas } from "../components/TeamCanvas";
import { Earlier, RunCard, liveKey, settleTurns, toTurns, useLiveText, useRecent, type Turn } from "../components/AgentRun";
import { ChatPanel, type ShownPlan } from "../components/ChatPanel";
import { PlanExecution } from "../components/PlanExecution";
import { Num, Pnl, toast, useStored } from "../components/ui";

const RULE_LIVE = ["waiting", "running", "proposed"];
const RULE_STATE: Record<string, string> = { waiting: "等待中", running: "运行中", proposed: "等你同意", done: "已执行", failed: "没执行成", cancelled: "已撤销", expired: "已过期" };
const RULE_MODE: Record<Rule["mode"], [string, string]> = {
  auto: ["自动执行", "条件满足时程序直接执行，不经过 Worker 思考；执行后把结果告诉 Worker"],
  wake: ["只叫醒 Worker", "条件满足时不自动做事，只叫醒 Worker 来判断"],
  task: ["派任务", "条件满足时把写好的任务交给 Worker 自己做，或者交给 Router 去派"],
  worker: ["Worker 对照", "这个条件没法用数字算，由 Worker 自己对照是否达到"],
};
const RULE_ACTION: Record<string, string> = {
  wake: "提醒 Worker", task: "交给 Worker", router_task: "交给 Router", open_market: "市价开仓", open_limit: "限价挂单",
  cancel_orders: "撤单", set_stops: "改止盈止损", reduce: "减仓", close: "平仓", stop: "停止交易",
  breakout: "突破入场", pullback: "回踩入场", manage: "管理仓位", profit_guard: "提前止盈",
  range: "区间交易", martingale: "分层补仓", grid: "网格交易", reverse: "止损反手",
};
const TRG_FILTERS: [string, string][] = [["live", "等待中"], ["ended", "已结束"], ["all", "全部"]];
const trgShown = (r: Rule, f: string) => f === "all" || RULE_LIVE.includes(r.status) === (f === "live");
const SIDE_TABS: [string, string][] = [["overview", "总览"], ["algo", "算法"], ["tasks", "任务"], ["triggers", "触发线"], ["positions", "持仓"], ["order", "下单"], ["chart", "K 线"], ["account", "账户"]];
const BIAS = { long: "看多", short: "看空", neutral: "观望" } as const;
const WORK_STATE: Record<string, string> = { queued: "排队", running: "进行中", waiting: "等你同意", done: "完成", failed: "失败" };
const WORK_FROM: Record<string, string> = { user: "交办", event: "行情触发", manual: "手动复盘" };

/* Agent 工作台：对话为主。上面是架构图（谁在工作、谁把任务交给了谁）和权限，
   中间像聊天一样一问一答：Agent 每一轮是一条消息，工具调用折叠成小卡片，要你同意的操作直接在那条消息里。 */

interface Props {
  state: AppState;
  account: Account | null;
  ticks: Record<string, number>;
  pending: PendingItem[];
  activity: Activity[];
  inst: string;
  redUp: boolean;
  loggedIn: boolean;
  setInst: (inst: string) => void;
  refresh: () => void;
  onTrade: () => void;
}

const BARS = ["1m", "5m", "15m", "1H", "4H"];

export function Workspace({ state, account, ticks, pending, activity, inst, redUp, loggedIn, setInst, refresh, onTrade }: Props) {
  // 改设置时先在界面上立刻生效（不等后端和欧易那一两秒），后端存好、状态刷新回来后再以后端为准；保存失败就退回去
  const [optimistic, setOptimistic] = useState<Partial<AppState["config"]> | null>(null);
  useEffect(() => setOptimistic(null), [state.config]);
  const cfg = useMemo(() => (optimistic ? { ...state.config, ...optimistic } : state.config), [state.config, optimistic]);
  const agent = state.agent;
  const [sel, setSel] = useStored("ouy.ws.sel", "router"); // router 或某个币（= 它的 Worker 小组）
  const inspected = cfg.instruments.includes(sel.split(":")[0]) ? sel.split(":")[0] : null;
  // 没有特意选哪个币时，先看有仓位的那个，不用自己去找
  const heldInst = account?.positions.find((p) => cfg.instruments.includes(p.inst_id))?.inst_id;
  const coin = inspected ?? heldInst ?? (cfg.instruments.includes(inst) ? inst : cfg.instruments[0]);
  useEffect(() => {
    if (inspected && inspected !== inst) setInst(inspected);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inspected]);
  const st = agent.instruments[coin];
  const pos = account?.positions.find((p) => p.inst_id === coin);
  const last = ticks[coin] || st?.last || pos?.mark_px || 0;

  /* ---------- 对话 ---------- */
  // 团队动态是活动记录。对话与确认卡由 ChatPanel 提供。
  const [feed, setFeed] = useStored<"router" | "all">("ouy.ws.feed", "router");
  const turns = useMemo(() => settleTurns(toTurns(activity), agent), [activity, agent]);
  const recent = useRecent(turns, 20);
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  useEffect(() => {
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [activity.length, sel]);

  const [acting, setActing] = useState("");
  const [shownPlan, setShownPlan] = useState<ShownPlan | null>(null);
  const [tradePreset, setTradePreset] = useState<(Extract<UiCommand, { type: "open_trade" }> & { n: number }) | null>(null);
  const applyUi = (commands: UiCommand[]) => {
    for (const command of commands) {
      if (command.type === "select_inst" || command.type === "open_trade") {
        setSel(command.inst_id);
        setInst(command.inst_id);
      }
      if (command.type === "set_bar") setBar(command.bar);
      if (command.type === "refresh") refresh();
      if (command.type === "open_trade") {
        setTradePreset((old) => ({ ...command, n: (old?.n ?? 0) + 1 }));
        setTab("coin");
        setSideTab("order");
      }
    }
  };
  const act = async (key: string, fn: () => Promise<unknown>, ok?: string) => {
    setActing(key);
    try {
      const r: any = await fn();
      if (r?.status && r?.message && STATUS_LABEL[r.status]) toast(r.detail?.unfilled ? r.message : `${STATUS_LABEL[r.status]}：${r.message}`, r.status === "executed" && !r.detail?.unfilled ? "ok" : "error");
      else if (ok) toast(ok, "ok");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setActing("");
      refresh();
    }
  };

  const live = useLiveText();
  // 每个 Agent 最新一条还没结束的消息：流式内容就显示在这一条里
  const liveTurn = new Map<string, string>();
  for (const t of turns) if (t.user === undefined && !t.done) liveTurn.set(liveKey(t), t.key);
  const liveOf = (t: Turn) => (t.key === "router-live" ? live["router:"] : !t.done && !t.body && liveTurn.get(liveKey(t)) === t.key ? live[liveKey(t)] : undefined);

  /** 这条要你同意的操作现在还在等吗（同意、拒绝、过期之后就不等了） */
  const askWaiting = (a: Activity) => {
    const pid = a.data?.pending_id as number | undefined;
    const sid = a.data?.strategy_id as number | undefined;
    if (pid) return pending.some((x) => x.id === pid);
    return !!sid && (agent.instruments[a.inst_id]?.strategies ?? []).some((x) => x.id === sid && x.status === "proposed");
  };

  /** 活动记录保留这一步的状态。 */
  const askCard = (a: Activity) => {
    const pid = a.data?.pending_id as number | undefined;
    const sid = a.data?.strategy_id as number | undefined;
    const p = pid ? pending.find((x) => x.id === pid) : undefined;
    const s = sid ? (agent.instruments[a.inst_id]?.strategies ?? []).find((x) => x.id === sid) : undefined;
    const waiting = !!p || s?.status === "proposed";
    return (
      <div key={a.id} className={`ask-card ${waiting ? "waiting" : "settled"}`}>
        <div className="ask-head">
          <b>{p ? p.title : s ? `布置策略「${s.label}」` : a.text.replace(/，等你同意.*$/, "").replace(/^想/, "")}</b>
          <span>{waiting ? "等你同意" : "已处理"}</span>
        </div>
        {!p && !s && a.data?.title && <div className="ask-text">{a.data.title}</div>}
        {p?.preview?.contracts && (
          <div className="ask-grid">
            <span>下单</span>
            <b>
              投入 {fmtUsd(p.preview.margin_usd, 0)} U · 名义 {fmtUsd(p.preview.notional_usd, 0)} U
            </b>
            <span>保证金</span>
            <b>{fmtUsd(p.preview.margin_usd)} U</b>
            <span>止损</span>
            <b className="down-fixed">{fmtPx(p.preview.stop_loss)}</b>
            <span>最多亏</span>
            <b>{fmtUsd(p.preview.risk_usd)} U</b>
          </div>
        )}
        {s && <div className="ask-text">{s.text}</div>}
        {(p?.action.reason || s?.note) && <div className="muted small">{p?.action.reason || s?.note}</div>}
      </div>
    );
  };

  /* ---------- 权限 ---------- */
  const [permOpen, setPermOpen] = useState(false);
  const [booksOpen, setBooksOpen] = useState(false);
  const [memOpen, setMemOpen] = useState(false);
  const [memCount, setMemCount] = useState<number | null>(null);
  const [bookCount, setBookCount] = useState<number | null>(null);
  useEffect(() => {
    api.playbooks().then((rows) => setBookCount(rows.length), () => {});
  }, []);
  const [tabRaw, setTab] = useStored<"chat" | "coin">("ouy.ws.tab", "chat");
  const tab = tabRaw === "coin" ? "coin" : "chat"; // 存下来的值不认识就回到对话
  const [canvasPct, setCanvasPct] = useStored("ouy.ws.canvas", 52); // 画布占工作台高度的百分比
  const [chatW, setChatW] = useStored("ouy.ws.chatw", 460); // 右边对话栏的宽度（像素）
  const [levMin, setLevMin] = useState(String(cfg.pref_leverage_min ?? 0));
  const [levMax, setLevMax] = useState(String(cfg.pref_leverage_max ?? 0));
  const [prefText, setPrefText] = useState("");
  // 能接受亏多少：每笔仓打到止损最多亏权益的百分之几、当天亏到百分之几就停手、单笔浮亏到百分之几不等 Agent 直接平
  const [lossTrade, setLossTrade] = useState(String(cfg.risk.risk_per_trade_pct));
  const [lossDay, setLossDay] = useState(String(cfg.risk.max_daily_loss_pct));
  const [lossCut, setLossCut] = useState(String(cfg.risk.emergency_exit_loss_pct));
  useEffect(() => {
    setLossTrade(String(cfg.risk.risk_per_trade_pct));
    setLossDay(String(cfg.risk.max_daily_loss_pct));
    setLossCut(String(cfg.risk.emergency_exit_loss_pct));
  }, [cfg.risk.risk_per_trade_pct, cfg.risk.max_daily_loss_pct, cfg.risk.emergency_exit_loss_pct]);
  const saveLoss = (key: "risk_per_trade_pct" | "max_daily_loss_pct" | "emergency_exit_loss_pct", raw: string, label: string, max: number, reset: (v: string) => void) => {
    const value = Math.round(Number(raw) * 10) / 10;
    const old = cfg.risk[key];
    const allowZero = key === "max_daily_loss_pct";
    if (!(allowZero ? value >= 0 : value > 0) || value > max) return void (toast(`${label}要填 ${allowZero ? "0（不限）" : "大于 0"} 到 ${max} 之间的百分比`, "error"), reset(String(old)));
    if (value === old) return;
    const looser = (allowZero && value === 0 && old > 0) || (!allowZero || old > 0 ? value > old : false);
    if (looser && !window.confirm(`${label}从 ${old || "不限"}${old ? "%" : ""} 改为 ${value ? `${value}%` : "不限"}？${cfg.env === "live" ? "现在是实盘，" : ""}放宽后可承受亏损上限会提高。`)) return void reset(String(old));
    saveCfg({ risk: { ...cfg.risk, [key]: value } }, value ? `${label}已改成 ${value}%` : `${label}已设为不限`);
  };
  useEffect(() => {
    setLevMin(String(cfg.pref_leverage_min ?? 0));
    setLevMax(String(cfg.pref_leverage_max ?? 0));
  }, [cfg.pref_leverage_min, cfg.pref_leverage_max]);
  const waiting = pending.length + Object.values(agent.instruments).reduce((n, v) => n + (v.strategies ?? []).filter((x) => x.status === "proposed").length, 0);
  const saveCfg = (patch: Partial<typeof cfg>, ok: string) => {
    setOptimistic((o) => ({ ...o, ...patch }));
    return act("cfg", () => api.saveConfig({ ...cfg, ...patch }, cfg.env === "live").catch((e) => (setOptimistic(null), Promise.reject(e))), ok);
  };
  const saveLev = () => {
    const lo = Math.min(Math.max(Math.round(Number(levMin) || 0), 0), 125);
    const hi = Math.min(Math.max(Math.round(Number(levMax) || 0), 0), 125);
    if (lo === (cfg.pref_leverage_min ?? 0) && hi === (cfg.pref_leverage_max ?? 0)) return;
    if (hi && lo > hi) return void toast("常用杠杆的下限不能比上限高", "error");
    saveCfg({ pref_leverage_min: lo, pref_leverage_max: hi }, lo || hi ? `常用杠杆已设为 ${lo || 1}~${hi || cfg.risk.max_leverage}x${(hi || lo) > cfg.risk.max_leverage ? `（风控最高 ${cfg.risk.max_leverage}x，超过的部分不会用）` : ""}` : "常用杠杆已清除，由 Agent 自己定");
  };

  /* ---------- 目标 ---------- */
  const [goalText, setGoalText] = useState("");
  const [goalLoss, setGoalLoss] = useState<number | "">("");
  const [goalHours, setGoalHours] = useState<number | "">("");
  // 右边按标签页分：总览 / 任务 / 触发线 / 持仓 / K 线
  const [sideRaw, setSideTab] = useStored("ouy.ws.sidetab", "overview");
  const sideTab = SIDE_TABS.some(([id]) => id === sideRaw) ? sideRaw : "overview"; // 存下来的值不认识就打开总览
  const [trgFilter, setTrgFilter] = useStored("ouy.ws.trg", "live");
  const [trgAction, setTrgAction] = useState("");
  const [goalTarget, setGoalTarget] = useState<number | "">("");
  const [goalStop, setGoalStop] = useState("");

  /* ---------- 这个币的 Worker 手上的任务 ---------- */
  const [work, setWork] = useState<WorkItem[]>([]);
  useEffect(() => {
    const load = () => api.work(30, coin).then(setWork, () => undefined);
    load();
    window.addEventListener("ouy:work", load);
    return () => window.removeEventListener("ouy:work", load);
  }, [coin]);
  // 触发线清单：已经不在的挂单，它的撤单条件不再列出
  const allRules = (st?.rules ?? []).filter((r) => !r.ord_id || account?.orders.some((o) => o.ord_id === r.ord_id));
  const stForNode = (node: string) => {
    const [instrument, suffix] = node.split(":");
    if (!/^s\d+$/.test(suffix ?? "")) return undefined;
    return agent.instruments[instrument]?.rules?.find((r) => r.strategy_id === Number(suffix.slice(1)));
  };
  // 触发线：只叫醒 Worker 的线和到了直接下单、撤单的线放在同一页，用「到了做什么」筛
  const rules = allRules; // 条件是算法读数的（以前的记录器）也是触发线，列在同一页
  const actions = [...new Set(rules.flatMap((r) => r.action ? [r.action] : []))];
  const actionFilter = actions.includes(trgAction) ? trgAction : "";
  const shownRules = rules.filter((r) => trgShown(r, trgFilter) && (!actionFilter || r.action === actionFilter));
  const liveTriggers = allRules.filter((r) => RULE_LIVE.includes(r.status)).length;
  const openWork = work.filter((w) => w.status === "running" || w.status === "queued" || w.status === "waiting");
  const pastWork = work.filter((w) => w.status === "done" || w.status === "failed").slice(0, 4);

  /* ---------- 右边的图 ---------- */
  const [bar, setBar] = useStored("ouy.bar", "15m");
  const held = useLotEntries(coin, pos);
  const move = pos && last ? (((last - pos.avg_px) / pos.avg_px) * 100 * (pos.side === "long" ? 1 : -1)).toFixed(1) : "";
  // 这个币的 Worker 的单独设置（审批方式、思考强度、模型、暂停）
  const own: WorkerOverride = cfg.workers?.[coin] ?? { model: "", paused: false };
  const saveWorker = (patch: Partial<WorkerOverride>, ok: string) => saveCfg({ workers: { ...cfg.workers, [coin]: { ...own, ...patch } } }, ok);
  // 图上的关键位：区间上下沿、支撑阻力、上一小时的高低点（和 Worker 看到的是同一套算法）
  // 模型接口上能用的模型：给每个 Worker 选模型的下拉框用
  const [modelList, setModelList] = useState<{ models: string[]; default: string }>({ models: [], default: "" });
  useEffect(() => {
    api.llmModels().then(setModelList, () => {});
  }, []);
  // 图上的参考线（前高前低、区间、支撑阻力）和它们的开关：和交易页共用
  const lv = useLevelLines(coin, bar, st?.regime?.candidates);
  // 在「算法」页点了某条触发记录的「在图上看」：图滚到那根 K 线并标出来；换币就清掉
  const [algoMark, setAlgoMark] = useState<{ ts: number; text: string } | null>(null);
  useEffect(() => setAlgoMark(null), [coin]);

  const lines = useMemo<ChartLine[]>(() => {
    const out: ChartLine[] = [];
    const away = (p: number) => (pos && pos.avg_px ? `${(((p - pos.avg_px) / pos.avg_px) * 100 * (pos.side === "long" ? 1 : -1)).toFixed(2)}%` : "");
    if (pos) out.push({ price: pos.avg_px, color: "#f0b90b", width: 2, title: `持仓 ${pos.side === "long" ? "多" : "空"} ${pos.contracts} 张 入场${move ? `（现在 ${Number(move) >= 0 ? "+" : ""}${move}%）` : ""}` });
    if (pos?.liq_px) out.push({ price: pos.liq_px, color: "#ff7a00", width: 2, title: "持仓 强平", dashed: true });
    for (const p of account?.protections.filter((x) => x.inst_id === coin) ?? []) {
      if (p.stop_loss) out.push({ price: p.stop_loss, color: "#f6465d", width: 2, title: `持仓 止损（${away(p.stop_loss)}）` });
      if (p.take_profit) out.push({ price: p.take_profit, color: "#0ecb81", width: 2, title: `持仓 止盈（+${away(p.take_profit)}）` });
    }
    const plan = st?.report?.plan;
    if (plan && !pos) {
      out.push({ price: plan.entry_high, color: "#8ab4ff", title: "计划入场上沿", dashed: true, group: "plan" });
      out.push({ price: plan.entry_low, color: "#8ab4ff", title: "计划入场下沿", dashed: true, group: "plan" });
      out.push({ price: plan.stop_loss, color: "#f6465d", title: "计划止损", dashed: true, group: "plan" });
    }
    (plan?.take_profits ?? []).forEach((tp, i) => out.push({ price: tp, color: "#0ecb81", title: `目标 ${i + 1}`, dashed: true, group: "plan" }));
    if (shownPlan?.inst_id === coin) {
      const preview = shownPlan.preview;
      out.push({ price: preview.entry_low, color: "#c084fc", title: "方案入场下沿", dashed: true });
      out.push({ price: preview.entry_high, color: "#c084fc", title: "方案入场上沿", dashed: true });
      out.push({ price: preview.stop_loss, color: "#f6465d", title: "方案止损", dashed: true });
      preview.take_profits.forEach((price) => out.push({ price, color: "#0ecb81", title: "方案止盈", dashed: true }));
    }
    for (const w of st?.rules ?? []) {
      if (w.type === "watch" && w.status === "waiting" && w.level) out.push({ price: w.level, color: "#9aa4b2", title: (lineKind(w.action) ?? "叫醒线") + lineCond(w.cond, w.bar), dashed: true, group: "watch" });
    }
    for (const order of account?.orders ?? []) {
      if (order.inst_id === coin && order.px) out.push({ price: order.px, color: "#f0b90b", title: order.side === "buy" ? "买单" : "卖单", dashed: true, group: "orders" });
    }
    for (const g of st?.order_guards ?? []) {
      if (!account?.orders.some((o) => o.ord_id === g.ord_id)) continue;
      if (g.cancel_px) out.push({ price: g.cancel_px, color: "#9aa4b2", title: "撤单线", group: "trigger" });
    }
    for (const s of st?.strategies ?? []) {
      const p = s.params;
      if (p.observation) continue;
      const level = s.kind === "trigger" && String(p.when ?? "").match(/^(price|close)/) ? p.level : p.trigger;
      if (level) out.push({ price: level, color: "#c084fc", title: `#${s.id} ${(s.kind === "trigger" && lineKind(p.do)) || "触发"}${s.kind === "trigger" ? lineCond(p.when, p.bar) : ""}`, dashed: true, group: "trigger" });
      if (p.price) out.push({ price: p.price, color: "#c084fc", title: `#${s.id} 挂单价`, dashed: true, group: "trigger" });
      if (p.low) out.push({ price: p.low, color: "#c084fc", title: `#${s.id} 下沿`, dashed: true, group: "trigger" });
      if (p.high) out.push({ price: p.high, color: "#c084fc", title: `#${s.id} 上沿`, dashed: true, group: "trigger" });
      if (p.stop_loss) out.push({ price: p.stop_loss, color: "#f6465d", title: `#${s.id} 止损`, dashed: true, group: "trigger" });
      if (p.take_profit) out.push({ price: p.take_profit, color: "#0ecb81", title: `#${s.id} 止盈`, dashed: true, group: "trigger" });
    }
    const kept = [...out.filter((l) => !l.group || lv.on(l.group)), ...lv.lines];
    return kept.filter((l, i) => kept.findIndex((x) => x.group === l.group && x.title === l.title && x.price === l.price) === i);
  }, [pos, account, st?.report, st?.strategies, st?.order_guards, st?.rules, coin, move, lv.lines, lv.show, shownPlan]);

  return (
    <div className="ws3" style={{ "--ws-canvas": `${canvasPct}%`, "--ws-chat": `${chatW}px` } as React.CSSProperties}>
      {/* ===== 顶部一行：你的偏好 + 权限 ===== */}
      <header className="ws3-top">
        <div className="prefs">
          <span className="prefs-t">我的偏好</span>
          <label className="pref" title="Agent 开新仓时杠杆就在这个范围里选，不会自己往低了挑；不超过风控里的最高杠杆。两个都填 0 = 让 Agent 自己定">
            常用杠杆
            <input type="number" min={0} max={125} value={levMin} onChange={(e) => setLevMin(e.target.value)} onBlur={saveLev} />
            ~
            <input type="number" min={0} max={125} value={levMax} onChange={(e) => setLevMax(e.target.value)} onBlur={saveLev} />x
          </label>
          <label className="pref" title="每笔仓打到止损时，最多亏掉账户权益的百分之几。Agent 按这个数算每笔投多少">
            每笔最多亏
            <input type="number" min={0.1} max={10} step={0.1} value={lossTrade} onChange={(e) => setLossTrade(e.target.value)} onBlur={() => saveLoss("risk_per_trade_pct", lossTrade, "每笔最多亏", 10, setLossTrade)} />%
          </label>
          <label className="pref" title="当天亏损到账户权益的这个比例后，Agent 不再开新仓；填 0 = 不限">
            当天最多亏
            <input type="number" min={0} max={100} step={0.5} value={lossDay} onChange={(e) => setLossDay(e.target.value)} onBlur={() => saveLoss("max_daily_loss_pct", lossDay, "当天最多亏", 100, setLossDay)} />%
          </label>
          <label className="pref" title="单笔仓的浮亏到账户权益的这个比例时，不等 Agent 判断，立即市价平仓">
            单笔浮亏强平
            <input type="number" min={0.5} max={50} step={0.5} value={lossCut} onChange={(e) => setLossCut(e.target.value)} onBlur={() => saveLoss("emergency_exit_loss_pct", lossCut, "单笔浮亏强平", 50, setLossCut)} />%
          </label>
          <label className="pref">
            出手
            <select value={cfg.stance} onChange={(e) => saveCfg({ stance: e.target.value as Stance }, "出手尺度已修改")}>
              {STANCES.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                </option>
              ))}
            </select>
          </label>
          <label className="pref">
            入场
            <select value={cfg.entry_mode} onChange={(e) => saveCfg({ entry_mode: e.target.value as typeof cfg.entry_mode }, "入场方式已修改")}>
              {ENTRY_MODES.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                </option>
              ))}
            </select>
          </label>
          <label className="pref" title="打开：Worker 复盘给出了计划（方向、入场区间、止损、止盈）而这个币空仓时，程序直接照计划下单——价格在入场区间就市价进，没到就布置成到价进场。照样过审批和风控。关掉：计划只是展示">
            <input type="checkbox" checked={cfg.auto_arm_plan ?? true} onChange={(e) => saveCfg({ auto_arm_plan: e.target.checked }, e.target.checked ? "有计划就照着下单" : "计划只展示，不自动下单")} />
            有计划就下单
          </label>
          <label className="pref">
            风格
            <select value={cfg.style} onChange={(e) => saveCfg({ style: e.target.value as Style }, "默认交易风格已修改")}>
              {STYLES.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                </option>
              ))}
            </select>
          </label>
          <span className="pref" title="思考强度越高判断越细，但越慢、越费 token">
            思考
            {(
              [
                ["reasoning_effort", "Worker"],
                ["sub_effort", "子代理"],
              ] as const
            ).map(([field, label]) => (
              <label key={field}>
                {label}
                <select value={cfg[field] ?? "medium"} onChange={(e) => saveCfg({ [field]: e.target.value as Effort }, `${label} 的思考强度已修改`)}>
                  {EFFORTS.map((x) => (
                    <option key={x.id} value={x.id}>
                      {x.label}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </span>
          {(cfg.prefs ?? []).map((t) => (
            <span key={t} className="pref chip" title={t}>
              <span className="pref-text">{t}</span>
              <button title="删掉这条偏好" onClick={() => saveCfg({ prefs: cfg.prefs.filter((x) => x !== t) }, "偏好已删除")}>
                ×
              </button>
            </span>
          ))}
          <form
            className="pref add"
            onSubmit={(e) => {
              e.preventDefault();
              const t = prefText.trim();
              if (!t) return;
              setPrefText("");
              saveCfg({ prefs: [...(cfg.prefs ?? []), t].slice(-20) }, "偏好已记下，每个 Agent 下一轮就会读到");
            }}
          >
            <input placeholder="+ 加一条偏好，比如「不追高」" value={prefText} onChange={(e) => setPrefText(e.target.value)} maxLength={60} />
          </form>
        </div>
        <div className="perm">
          <span className={`env ${cfg.env}`}>{cfg.env === "demo" ? "模拟盘" : "实盘"}</span>
          <div className="seg sm">
            {(["auto", "confirm", "advise"] as Mode[]).map((m) => (
              <button key={m} className={cfg.mode === m && !Object.keys(cfg.permissions ?? {}).length ? "on" : ""} disabled={!!acting} onClick={() => saveCfg({ mode: m, permissions: {} }, `权限已改成「${MODE_LABEL[m]}」`)}>
                {MODE_LABEL[m]}
              </button>
            ))}
          </div>
          <button className="btn xs" onClick={() => setBooksOpen(true)} title="Agent 和你写下来的、可以反复用的策略：看规则和战绩，加、停用、删">
            策略库{bookCount ? ` ${bookCount}` : ""}
          </button>
          {booksOpen && <PlaybookPanel env={cfg.env} onClose={() => setBooksOpen(false)} onCount={setBookCount} />}
          <button className="btn xs" onClick={() => setMemOpen(true)} title="透视本轮发给大模型的完整上下文，查看并开关工作记忆、目标令、全局偏好与长期经验（暂停保留不丢数据）">
            记忆与上下文{memCount ? ` ${memCount}` : ""}
          </button>
          {memOpen && <MemoryPanel coins={cfg.instruments} config={cfg} defaultInst={coin} onClose={() => setMemOpen(false)} onCount={setMemCount} onChanged={refresh} />}
          <button className={`btn xs ${permOpen ? "on" : ""}`} onClick={() => setPermOpen(!permOpen)}>
            每类操作单独设 ▾
          </button>
          {permOpen && (
            <div className="perm-pop">
              {ACTION_KINDS.map((k) => (
                <label key={k}>
                  <span>{KIND_LABEL[k]}</span>
                  <select value={permissionFor(cfg, k)} onChange={(e) => saveCfg({ permissions: { ...(cfg.permissions ?? {}), [k]: e.target.value as Permission } }, `「${KIND_LABEL[k]}」已修改`)}>
                    <option value="auto">自动做</option>
                    <option value="confirm">先问我</option>
                    <option value="off">不允许</option>
                  </select>
                </label>
              ))}
              <p className="muted small">「先问我」的操作会在对话里出现一张卡片，你点同意才执行；结果会回到 Agent 手上，它接着往下做。</p>
            </div>
          )}
        </div>
      </header>

      {/* ===== 中间：团队图 ===== */}
      <TeamCanvas
        state={state}
        account={account}
        ticks={ticks}
        activity={activity}
        sel={sel}
        onSelect={(v) => {
          setSel(v);
          if (v === "router") {
            setTab("chat");
          } else {
            const rule = stForNode(v);
            setSideTab(v.endsWith(":pos") ? "positions" : rule ? "triggers" : v.includes(":") && !v.endsWith(":quant") ? "tasks" : "overview");
            if (rule) {
              setTrgFilter("all");
              setTrgAction("");
            }
            setTab("coin");
          }
        }}
      />

      {/* 上下分隔条：拖动调整画布和对话各占多高 */}
      <div
        className="ws3-split"
        onPointerDown={(e) => {
          const root = e.currentTarget.parentElement!;
          const top = root.getBoundingClientRect().top;
          const total = root.clientHeight;
          const head = (root.firstElementChild as HTMLElement).offsetHeight + 8;
          const drag = (ev: PointerEvent) => {
            const pct = Math.min(Math.max(((ev.clientY - top - head) / total) * 100, 22), 72);
            setCanvasPct(Math.round(pct));
          };
          const stop = () => {
            window.removeEventListener("pointermove", drag);
            window.removeEventListener("pointerup", stop);
          };
          window.addEventListener("pointermove", drag);
          window.addEventListener("pointerup", stop);
          e.preventDefault();
        }}
      />

      {/* 左右分隔条：拖动调整右边对话栏多宽 */}
      <div
        className="ws3-vsplit"
        title="拖动调整左右宽度"
        onPointerDown={(e) => {
          const box = e.currentTarget.parentElement!.getBoundingClientRect();
          const drag = (ev: PointerEvent) => setChatW(Math.round(Math.min(Math.max(box.right - ev.clientX, 320), Math.max(box.width * 0.65, 320))));
          const stop = () => {
            window.removeEventListener("pointermove", drag);
            window.removeEventListener("pointerup", stop);
            document.body.style.userSelect = "";
          };
          document.body.style.userSelect = "none";
          window.addEventListener("pointermove", drag);
          window.addEventListener("pointerup", stop);
          e.preventDefault();
        }}
      />

      {/* ===== 下：和选中的 Agent 对话 / 这个币的图和仓位 ===== */}
      <div className="ws3-panel">
        <div className="ws3-tabs">
          <button className={tab === "chat" ? "on" : ""} onClick={() => setTab("chat")}>
            Agent 对话
            {waiting > 0 && <i>{waiting}</i>}
          </button>
          <button className={tab === "coin" ? "on" : ""} onClick={() => setTab("coin")}>
            {shortInst(coin)} 图表和仓位
          </button>
        </div>
      <section className="ws2-chat" hidden={tab !== "chat"}>
        <div className="ws-chat-head">
          <strong>{feed === "all" ? "团队动态" : "Agent 对话"}</strong>
          <button className="btn xs" aria-pressed={feed === "all"} onClick={() => setFeed(feed === "router" ? "all" : "router")}>
            {feed === "all" ? "返回对话" : "查看团队动态"}
          </button>
        </div>
          <ChatPanel inst={coin} config={cfg} agent={agent} activity={activity} defaultEffort={cfg.reasoning_effort} shown={shownPlan} hidden={feed === "all"}
            onShow={(plan) => {
              setShownPlan(plan);
              if (plan) {
                setSel(plan.inst_id);
                setInst(plan.inst_id);
                setTab("coin");
                setSideTab("chart");
              }
            }} onActed={refresh} onUi={applyUi} />
        {feed === "all" && (
        <div
          className="thread"
          ref={scroller}
          onScroll={(e) => {
            const el = e.currentTarget;
            pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          }}
        >
          {turns.length === 0 && (
            <div className="ws-empty">
              暂无团队动态
            </div>
          )}
          <Earlier count={recent.earlier} onMore={recent.more} />
          {[...recent.visible, ...(live["router:"] && !liveTurn.has("router:") ? [{ key: "router-live", id: 0, ts: Date.now(), agent: "router", inst: "", steps: [], asks: [], notes: [], done: false } as Turn] : [])].map((t, k, shown) =>
            t.user !== undefined ? (
              <div key={t.key} className="msg user">
                <div className="bub">
                  {t.user}
                  {!!t.images?.length && (
                    <div className="msg-shots">
                      {t.images.map((x, k) => (
                        <a key={k} href={shotUrl(x)} target="_blank" rel="noreferrer">
                          <img src={shotUrl(x)} alt={`附图 ${k + 1}`} loading="lazy" />
                        </a>
                      ))}
                    </div>
                  )}
                </div>
                <time>
                  {t.inst ? `对 ${shortInst(t.inst)} Worker · ` : "对 Router · "}
                  {t.sub ? `${t.sub} · ` : ""}
                  {fmtTime(t.ts)}
                </time>
              </div>
            ) : (
              <RunCard key={t.key} t={t} live={liveOf(t)} active={!t.done && liveTurn.get(liveKey(t)) === t.key && (t.agent !== "router" || !!liveOf(t))} brief={k < shown.length - 3}
                askWaiting={askWaiting} renderAsk={askCard} onPick={setSel} />
            ),
          )}
        </div>
        )}
      </section>

      <aside className="ws2-side" hidden={tab !== "coin"}>
        <div className="ws-coin">
          <b>{shortInst(coin)}</b>
          <span className="px">{fmtPx(last)}</span>
          {pos && (
            <span className={`tag ${pos.side}`}>
              {pos.side === "long" ? "多" : "空"} · 投入 {fmtUsd(pos.margin, 0)} U · {pos.leverage}x <Pnl value={pos.upl} />
            </span>
          )}
          <div className="bars">
            {BARS.map((b) => (
              <button key={b} className={b === bar ? "on" : ""} onClick={() => setBar(b)}>
                {b}
              </button>
            ))}
          </div>
          <button
            className="btn xs"
            onClick={() => {
              if (coin !== inst) setInst(coin); // 交易页要打开的是这里正在看的这个币
              onTrade();
            }}
          >
            交易页
          </button>
        </div>
        <div className="ws-tabs">
          {SIDE_TABS.map(([id, label]) => (
            <button key={id} className={sideTab === id ? "on" : ""} onClick={() => setSideTab(id)}>
              {label}
              {id === "tasks" && openWork.length > 0 && <em>{openWork.length}</em>}
              {id === "triggers" && liveTriggers > 0 && <em>{liveTriggers}</em>}
              {id === "positions" && pos && <em>1</em>}
            </button>
          ))}
        </div>
        {sideTab === "account" && (
          <div className="ws-account">
            <AssetsPanel account={account} loggedIn={loggedIn} />
          </div>
        )}
        {sideTab === "order" && (
          <div className="ws-order">
            <div className="ws-order-head">
              <b>{shortInst(coin)} 永续</b>
              <span className="muted small">{coin} · 现价 {last ? fmtPx(last) : "—"}</span>
            </div>
            <div className="ws-order-body">
            <PositionCard
              inst={coin}
              last={last ?? 0}
              account={account}
              onDone={refresh}
              loggedIn={loggedIn}
              worker={
                !agent.running
                  ? { ok: false, text: "盯盘没开：没有 Worker 管这个币的仓位" }
                  : st?.paused
                    ? { ok: false, text: `${shortInst(coin)} Worker 已暂停：不会管这个币的仓位` }
                    : { ok: true, text: `${shortInst(coin)} Worker 在盯：下单后它马上来看仓位、调止损` }
              }
            />
            <OrderTicket
              key={coin}
              inst={coin}
              last={last ?? 0}
              account={account}
              risk={cfg.risk}
              onDone={refresh}
              loggedIn={loggedIn}
              defaultMarginMode={cfg.margin_mode}
              defaultStopPct={cfg.default_stop_pct}
              defaultStyle={cfg.styles?.[coin] ?? cfg.style}
              preset={tradePreset?.inst_id === coin ? tradePreset : null}
            />
            </div>
          </div>
        )}
        <div className="ws-wset" hidden={sideTab !== "overview"}>
          <span className="prefs-t">{shortInst(coin)} Worker</span>
          <label className="pref" title="这个币的交易风格：决定它看哪些周期、多久复盘一次、止损放多宽、一笔单拿多久。不设就用上面的默认风格">
            风格
            <select
              value={cfg.styles?.[coin] ?? ""}
              onChange={(e) => {
                const { [coin]: _drop, ...rest } = cfg.styles ?? {};
                const styles = e.target.value ? { ...rest, [coin]: e.target.value as Style } : rest;
                saveCfg({ styles }, `${shortInst(coin)} 的交易风格已改成「${STYLES.find((x) => x.id === e.target.value)?.label ?? "跟全局"}」`);
              }}
            >
              <option value="">跟全局（{STYLES.find((x) => x.id === cfg.style)?.label}）</option>
              {STYLES.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                </option>
              ))}
            </select>
          </label>
          <label className="pref" title="这个 Worker 需要多少条件才出手；也决定程序核对计划时净盈亏比的下限（保守 2 / 均衡 1.5 / 积极 1）">
            出手
            <select value={own.stance ?? ""} onChange={(e) => saveWorker({ stance: (e.target.value || null) as Stance | null }, `${shortInst(coin)} Worker 的出手尺度已修改`)}>
              <option value="">跟全局（{STANCES.find((x) => x.id === cfg.stance)?.label}）</option>
              {STANCES.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                </option>
              ))}
            </select>
          </label>
          <label className="pref" title="这个 Worker 看好方向后是现价就进，还是把单挂在结构位上等价格回来">
            入场
            <select value={own.entry_mode ?? ""} onChange={(e) => saveWorker({ entry_mode: (e.target.value || null) as WorkerOverride["entry_mode"] }, `${shortInst(coin)} Worker 的入场方式已修改`)}>
              <option value="">跟全局（{ENTRY_MODES.find((x) => x.id === cfg.entry_mode)?.label}）</option>
              {ENTRY_MODES.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                </option>
              ))}
            </select>
          </label>
          <label className="pref" title="程序算法快判（每 4 秒识别变盘回踩等信号）：叫醒 Worker 核实，还是对授权方向直接落单">
            快判
            <select value={own.fast_entry ?? ""} onChange={(e) => saveWorker({ fast_entry: (e.target.value || null) as FastMode | null }, `${shortInst(coin)} Worker 的快判方式已修改`)}>
              <option value="">跟全局（{FAST_MODES.find((x) => x.id === (cfg.fast_entry ?? "verify"))?.label}）</option>
              {FAST_MODES.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                </option>
              ))}
            </select>
          </label>
          <label className="pref" title="这个 Worker 下单前要不要问你。不设就跟右上角的全局设置走">
            审批
            <select
              value={own.mode ?? ""}
              onChange={(e) => {
                const mode = (e.target.value || null) as Mode | null;
                if (mode === "auto" && cfg.env === "live" && !window.confirm(`现在是实盘：把 ${shortInst(coin)} Worker 设成全自动后，它会直接用真钱下单，不再问你。确定吗？`)) return;
                saveWorker({ mode }, `${shortInst(coin)} Worker 的审批方式已改成「${mode ? MODE_LABEL[mode] : "跟全局"}」`);
              }}
            >
              <option value="">跟全局（{MODE_LABEL[cfg.mode]}）</option>
              {(["auto", "confirm", "advise"] as Mode[]).map((m) => (
                <option key={m} value={m}>
                  {MODE_LABEL[m]}
                </option>
              ))}
            </select>
          </label>
          <label className="pref" title="定时复盘和你交代的事用这个强度">
            思考
            <select value={own.effort ?? ""} onChange={(e) => saveWorker({ effort: (e.target.value || null) as Effort | null }, `${shortInst(coin)} Worker 的思考强度已修改`)}>
              <option value="">跟全局</option>
              {EFFORTS.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                </option>
              ))}
            </select>
          </label>
          <label className="pref" title="这个 Worker 定时看盘按哪个周期的 K 线收盘（收盘后 10 秒叫醒）。不设跟全局；跟风格自动 = 空仓 超短线 15 分钟 / 日内 1 小时 / 波段 4 小时，有仓 5 / 15 / 60 分钟">
            看盘
            <select
              value={own.look_minutes ?? ""}
              onChange={(e) => saveWorker({ look_minutes: e.target.value === "" ? null : Number(e.target.value) }, `${shortInst(coin)} Worker 的定时看盘周期已修改`)}
            >
              <option value="">跟全局（{LOOK_BARS.find((x) => x.id === (cfg.look_minutes ?? 0))?.label}）</option>
              {LOOK_BARS.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                </option>
              ))}
            </select>
          </label>
          <label className="pref" title="价格触及盯盘价位、进入入场区间、突然异动把它叫醒时用这个强度：要快">
            触发时
            <select value={own.trigger_effort ?? ""} onChange={(e) => saveWorker({ trigger_effort: (e.target.value || null) as Effort | null }, `${shortInst(coin)} Worker 被行情叫醒时的思考强度已修改`)}>
              <option value="">跟全局（{EFFORTS.find((x) => x.id === (cfg.trigger_effort ?? "low"))?.label ?? "低"}）</option>
              {EFFORTS.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                </option>
              ))}
            </select>
          </label>
          <label className="pref" title="这个 Worker 单独用哪个模型；留空用全局的 Worker 模型">
            模型
            <select
              value={own.model ?? ""}
              onChange={(e) => {
                let model = e.target.value;
                if (model === "__custom__") {
                  const typed = window.prompt("输入模型名字（要和你的模型接口上的名字一致）", own.model ?? "");
                  if (typed === null) return;
                  model = typed.trim();
                }
                if (model !== (own.model ?? "")) saveWorker({ model }, `${shortInst(coin)} Worker 的模型已改成「${model || "跟全局"}」`);
              }}
            >
              <option value="">跟全局{cfg.worker_model || modelList.default ? `（${cfg.worker_model || modelList.default}）` : ""}</option>
              {[...new Set([...(own.model ? [own.model] : []), ...modelList.models])].map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
              <option value="__custom__">手动输入…</option>
            </select>
          </label>
          <button className="btn xs" onClick={() => saveWorker({ paused: !own.paused, resume_ts: null, paused_by: own.paused ? "" : "你手动暂停" }, own.paused ? `${shortInst(coin)} Worker 已重新开工` : `${shortInst(coin)} Worker 已暂停`)}>
            {own.paused ? "重新打开" : "暂停这个 Worker"}
          </button>
          {!own.paused && (
            <button
              className="btn xs"
              title="停下：空仓时不再定时复盘，只在它写的触发线、到价策略、你的留言、出现持仓时醒来"
              onClick={() => act("sb", () => api.standby(coin, !own.standby), own.standby ? `${shortInst(coin)} Worker 恢复定时复盘` : `${shortInst(coin)} Worker 已停下，只等触发`)}
            >
              {own.standby ? "已停下 · 恢复定时复盘" : "让它停下（只等触发）"}
            </button>
          )}
          {!own.paused && own.standby && <span className="muted small">{own.paused_by || "已停下"}：只在触发线、到价策略、留言、出现持仓时醒来</span>}
          {own.paused && (
            <span className="muted small">
              {own.paused_by || "已暂停"}
              {own.resume_ts ? `（${fmtTime(own.resume_ts)} 自动回来）` : ""}
            </span>
          )}
        </div>
        <div className="ws-posctl" hidden={sideTab !== "positions"}>
          <Positions account={account} maxLeverage={cfg.risk.max_leverage} onSelect={(i) => cfg.instruments.includes(i) && setSel(i)} onDone={refresh} loggedIn={loggedIn} />
        </div>
        <div className="ws-lines" hidden={sideTab !== "chart"}>
          {lv.toggles}
        </div>
        {sideTab === "chart" && (
          <div className="ws-chart">
            <Chart instId={coin} bar={bar} lines={lines} tick={ticks[coin]} redUp={redUp} entries={held.entries} position={held.position} shiftMark={lv.shiftMark} algoMarks={lv.algoMarks} mark={algoMark} />
          </div>
        )}
        <div className="ws-card" hidden={sideTab !== "overview"}>
          <h5>
            {shortInst(coin)} Worker 的结论
            <span className="grow" />
            <button className="btn xs" disabled={!loggedIn || !!acting || st?.reviewing || !!st?.awaiting} onClick={() => act("review", () => api.review(coin), "复盘完成")}>
              {st?.reviewing ? "思考中…" : "现在复盘"}
            </button>
          </h5>
          {st?.report ? (
            <>
              <p>
                <b>{BIAS[st.report.bias]}</b>
                <span className="muted small"> · 把握 {Math.round(st.report.confidence * 100)}% · {fmtTime(st.report.ts)}</span>
              </p>
              <p>{st.report.summary}</p>
              {st.report.cached_tokens != null && (
                <p className="muted small" title="来自模型用量回报。缓存只复用相同输入前缀，不复用回答；未返回缓存明细时也可能显示 0。">
                  模型缓存：{st.report.input_tokens ? `${st.report.cached_tokens.toLocaleString()} / ${st.report.input_tokens.toLocaleString()} 输入 token` : `${st.report.cached_tokens.toLocaleString()} token（无输入总量）`}
                  {st.report.model_requests != null && `，${st.report.model_requests} 次请求`}
                  {st.report.first_cached_tokens != null && `，首请求读到 ${st.report.first_cached_tokens.toLocaleString()}`}
                  {!!st.report.unmeasured_requests && `（${st.report.unmeasured_requests} 次未回报用量）`}
                </p>
              )}
              {st.report.plan && (
                <p className="muted small">
                  计划{st.report.plan.strategy ? `（${st.report.plan.strategy}）` : ""}：{st.report.plan.direction === "long" ? "做多" : "做空"} / {ENTRY_TYPE[st.report.plan.entry_type ?? "pullback"]}，入场 {fmtPx(st.report.plan.entry_low)}~{fmtPx(st.report.plan.entry_high)}，止损 {fmtPx(st.report.plan.stop_loss)}
                  {st.report.plan.take_profits?.length ? `，目标 ${st.report.plan.take_profits.map((x) => fmtPx(x)).join(" / ")}` : ""}
                  {st.report.plan.invalidation ? `。作废条件：${st.report.plan.invalidation}` : ""}
                </p>
              )}
              {!st.report.plan && st.report.no_plan_reason && <p className="muted small">这一轮没给计划：{noPlanText(st.report.no_plan_reason)}</p>}
              <PlanExecution inst={coin} report={st.report} account={account} onChanged={refresh} />
              {!!st.report.knowledge?.length && (
                <details className="ws-kb-used">
                  <summary className="muted small">本轮复盘调用了 {st.report.knowledge.length} 条知识库手册（外加 22 篇策略目录常驻）</summary>
                  <ul>
                    {st.report.knowledge.map((k, i) => (
                      <li key={i}>{k}</li>
                    ))}
                  </ul>
                </details>
              )}
            </>
          ) : (
            <p className="muted">还没有复盘过。</p>
          )}
        </div>
        <div className="ws-card" hidden={sideTab !== "overview"}>
          <h5>目标和停止条件 <span className="muted small">条件达到才停，否则一直盯</span></h5>
          {st?.goal ? (
            <div className="goal">
              <p>
                <b>{st.goal.goal}</b>
              </p>
              <p className="muted small">
                {st.goal.by}定的 · {st.goal.status === "active" ? "进行中" : st.goal.note}
              </p>
              {st.goal.target_usdt != null && (
                <>
                  <div className="meter">
                    <i style={{ width: `${Math.min(Math.max((st.goal.total ?? st.goal.pnl) / st.goal.target_usdt, 0), 1) * 100}%`, background: "var(--green)" }} />
                  </div>
                  <p className="muted small">
                    净赚 {fmtUsd(st.goal.target_usdt)} U 就停{st.goal.shared_with?.length ? `（和 ${st.goal.shared_with.map(shortInst).join("、")} 合起来算）` : ""}：现在 <Pnl value={st.goal.total ?? st.goal.pnl} /> U，由程序核对
                  </p>
                </>
              )}
              {st.goal.max_loss_usdt != null && (
                <>
                  <div className="meter">
                    <i style={{ width: `${Math.min(Math.max(-st.goal.pnl / st.goal.max_loss_usdt, 0), 1) * 100}%` }} />
                  </div>
                  <p className="muted small">
                    最多亏 {fmtUsd(st.goal.max_loss_usdt)} U 就停：这个币现在 <Pnl value={st.goal.pnl} /> U，由程序核对
                  </p>
                </>
              )}
              {st.goal.until_ts != null && <p className="muted small">做到 {fmtTime(st.goal.until_ts)} 停</p>}
              {st.goal.stop_when && <p className="muted small">其他停止条件：{st.goal.stop_when}（由 Worker 对照）</p>}
              {st.goal.status === "active" && st.goal.target_usdt == null && st.goal.max_loss_usdt == null && st.goal.until_ts == null && !st.goal.stop_when && (
                <p className="muted small">没有交代停止条件：它会一直盯，直到你叫停。</p>
              )}
              <button className="btn xs" onClick={() => act("cg", () => api.clearGoal(coin), "目标已清除")}>
                清除目标
              </button>
            </div>
          ) : (
            <div className="goal-form">
              <input className="input" placeholder={`给 ${shortInst(coin)} Worker 一个目标`} value={goalText} onChange={(e) => setGoalText(e.target.value)} />
              <div className="field-row">
                <Num value={goalTarget} onChange={setGoalTarget} min={0.1} placeholder="净赚多少就停" suffix="U" />
                <Num value={goalLoss} onChange={setGoalLoss} min={0.1} placeholder="最多亏" suffix="U" />
                <Num value={goalHours} onChange={setGoalHours} min={0.5} step={0.5} placeholder="做多久" suffix="小时" />
              </div>
              <input className="input" placeholder="其他停止条件（可不填），比如「跌破 70 就不做了」" value={goalStop} onChange={(e) => setGoalStop(e.target.value)} />
              <button
                className="btn sm primary"
                disabled={!goalText.trim() || !!acting}
                onClick={() =>
                  act("goal", async () => {
                    await api.setGoal(coin, goalText.trim(), goalLoss === "" ? undefined : goalLoss, goalHours === "" ? undefined : goalHours, goalTarget === "" ? undefined : goalTarget, goalStop.trim());
                    setGoalText("");
                    setGoalStop("");
                  }, `已交给 ${shortInst(coin)} 的 Worker`)
                }
              >
                交给它
              </button>
            </div>
          )}
        </div>
        {sideTab === "algo" && <AlgoReadings coin={coin} st={st} last={last} busy={!!acting} own={own} onSaveWorker={saveWorker} />}
        {sideTab === "algo" && (
          <AlgoPanel
            coin={coin}
            st={st}
            onShow={(ts, text) => {
              setAlgoMark({ ts, text });
              setSideTab("chart");
            }}
          />
        )}
        <div className="ws-card wide" hidden={sideTab !== "tasks"}>
          <h5>
            任务 <span className="muted small">进行中 {openWork.length} 项{st?.queue ? `，排队 ${st.queue} 项` : ""}{st?.inbox ? `，未读留言 ${st.inbox} 条` : ""}</span>
            <button
              className="btn xs"
              disabled={!!acting || pastWork.length === 0}
              title="清掉已经完成和失败的任务记录；正在做的、排队的不动"
              onClick={() =>
                window.confirm(`清除 ${shortInst(coin)} 已完成和失败的任务记录？正在做的和排队的任务不受影响。`) &&
                act("clear-tasks", async () => {
                  await api.clearHistory(coin, "tasks");
                  window.dispatchEvent(new Event("ouy:work"));
                }, "已清除")
              }
            >
              清除已完成
            </button>
          </h5>
          {openWork.length === 0 && <p className="muted">现在没有在做的任务{st?.next_review_in != null ? `，${st.next_review_in} 秒后下一次复盘` : ""}。</p>}
          {[...openWork, ...pastWork].map((w) => (
            <div key={w.id} className="task-row" title={w.result || w.goal}>
              <em className={`st-${w.status}`}>{WORK_STATE[w.status]}</em>
              <div>
                <b>{WORK_FROM[w.source] ?? w.source}：{w.goal}</b>
                {w.result && <span>{w.result.replace(/[*#`]/g, "")}</span>}
                {w.status === "failed" && <ErrorDetails workId={w.id} message={w.result} />}
              </div>
              <time>{fmtTime(w.ts)}</time>
            </div>
          ))}
        </div>
        <div className="ws-card wide trg" hidden={sideTab !== "triggers"}>
          <div className="trg-head">
            <div>
              <b>触发线</b>
              <span className="muted small">
                一行一条：到了什么条件 → 做什么。条件可以是价位，也可以是一项算法读数（读数在「算法」页）。「提醒 Worker」的只叫它来看；其余的由程序直接下单、撤单或平仓（照样过风控），做完再告诉 Worker
              </span>
            </div>
            <button
              className="btn xs"
              disabled={!!acting || !rules.some((r) => !RULE_LIVE.includes(r.status))}
              title="清掉已执行、没执行成、已撤销、已过期的记录；还在等的不动"
              onClick={() =>
                window.confirm(`清除 ${shortInst(coin)} 已结束的触发线记录？还在等待的不受影响。`) && act("clear-trg", () => api.clearHistory(coin, "triggers"), "已清除")
              }
            >
              清除已结束
            </button>
            <div className="trg-filter">
              {TRG_FILTERS.map(([id, label]) => (
                <button key={id} className={trgFilter === id ? "on" : ""} onClick={() => setTrgFilter(id)}>
                  {label} {rules.filter((r) => trgShown(r, id)).length}
                </button>
              ))}
            </div>
          </div>
          <LineForm coin={coin} last={last} lines={Object.keys(st?.data?.levels ?? {})} busy={!!acting} run={(fn, ok) => act("line", fn, ok)} />
          {actions.length > 1 && (
            <select aria-label="按到了做什么筛选" value={actionFilter} onChange={(e) => setTrgAction(e.target.value)}>
              <option value="">到了做什么：全部</option>
              {actions.map((id) => <option key={id} value={id}>{RULE_ACTION[id] ?? id}</option>)}
            </select>
          )}
          {shownRules.length === 0 && (
            <p className="muted trg-empty">
              {actionFilter ? "没有符合当前筛选的触发线。" : trgFilter === "ended" ? "最近 12 小时没有执行过或结束的触发线。" : "现在没有在等的触发线。Worker 做计划时会划；你也可以在上面自己加一条。"}
            </p>
          )}
          {shownRules.map((r) => (
            <div key={r.id} className={`trg-row ${RULE_LIVE.includes(r.status) && !r.off ? "" : "ended"}`}>
              <em className={`trg-state s-${r.status}`}>{r.off && RULE_LIVE.includes(r.status) ? "已关掉" : RULE_STATE[r.status] ?? r.status}</em>
              <div className="trg-main">
                <div className="trg-flow">
                  <span className="trg-when">
                    <i>当</i>
                    {r.recorder && r.conds?.length ? <ReadingConds rule={r} st={st} last={last} busy={!!acting} run={(fn, ok) => act(`e${r.id}`, fn, ok)} /> : r.when}
                  </span>
                  <span className="trg-arrow">→</span>
                  <span className="trg-do">{r.do}</span>
                </div>
                <div className="trg-meta">
                  <span>{r.strategy_id ? `#${r.strategy_id} ` : ""}{r.label}</span>
                  <span>{r.by}写的{r.ts ? ` · ${fmtTime(r.ts)}` : ""}</span>
                  {r.level && last && RULE_LIVE.includes(r.status) ? <span>距现价 {(((r.level - last) / last) * 100).toFixed(2)}%</span> : null}
                  {r.expires_ts && RULE_LIVE.includes(r.status) ? <span>{r.type === "order" || r.type === "stop" ? "时间" : "有效到"} {fmtTime(r.expires_ts)}</span> : null}
                  {r.repeat ? <span>反复生效，间隔 {r.cooldown_minutes ?? 15} 分钟{r.runs ? ` · 已触发 ${r.runs} 次${r.last_run ? `（上次 ${fmtTime(r.last_run)}）` : ""}` : ""}</span> : null}
                  {r.note ? <span className="trg-note" title={r.note}>{r.note}</span> : null}
                </div>
              </div>
              <span className={`trg-mode m-${r.mode}`} title={RULE_MODE[r.mode][1]}>
                {r.action && RULE_ACTION[r.action] || RULE_MODE[r.mode][0]}
              </span>
              <div className="trg-act">
                {r.strategy_id && r.status === "proposed" && (
                  <button className="btn xs primary" disabled={!!acting} onClick={() => act(`a${r.id}`, () => api.strategyAct(r.strategy_id!, "approve"), "已启用")}>
                    同意
                  </button>
                )}
                {r.strategy_id && r.repeat && r.status === "waiting" && (
                  <button className="btn xs" disabled={!!acting} title="先关掉不删，之后可以再打开" onClick={() => act(`o${r.id}`, () => api.editStrategy(r.strategy_id!, { off: !r.off }), r.off ? "已打开" : "已关掉")}>
                    {r.off ? "打开" : "关掉"}
                  </button>
                )}
                {r.strategy_id && RULE_LIVE.includes(r.status) && (
                  <button className="btn xs" disabled={!!acting} onClick={() => act(`x${r.id}`, () => api.strategyAct(r.strategy_id!, "cancel"), "已撤销")}>
                    撤销
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      </aside>
      </div>
    </div>
  );
}
