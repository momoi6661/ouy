import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, fmtPx, fmtTime, fmtUsd, shortInst, STATUS_LABEL, type Account, type AppState, type JournalEntry, type PendingItem, type UiCommand } from "../api";
import { AgentPanel } from "../components/AgentPanel";
import { Chart, useLotEntries, type ChartLine } from "../components/Chart";
import { Journal, levelsOf } from "../components/Journal";
import { MarketList } from "../components/MarketList";
import { OrderTicket } from "../components/OrderTicket";
import { useLevelLines } from "../components/LevelLines";
import { Positions } from "../components/Positions";
import { AssetsPanel } from "../components/AssetsPanel";
import { ChatPanel, type ShownPlan } from "../components/ChatPanel";
import { OrderBook } from "../components/OrderBook";
import { Modal, toast, useStored } from "../components/ui";
import { TasksPanel, WorkersPanel } from "../components/WorkersPanel";

const BARS = ["1m", "5m", "15m", "1H", "4H", "1D"];

/* ---------- 固定布局：左边行情，中间 K 线 + 下方持仓，右边 Agent。每一块的位置不变，只有两条分隔线可以拖 ---------- */
const TITLES: Record<string, string> = {
  market: "行情",
  chart: "K 线",
  positions: "持仓 / 挂单",
  agent: "Agent 盯盘",
  assets: "资产",
  chat: "Agent 对话",
  feed: "最新动态",
  trade: "下单 / 盘口",
  workers: "Worker 总览",
  tasks: "任务",
};
const SIDE_TABS = ["chat", "agent", "feed", "trade"] as const;
const BOTTOM_TABS = ["positions", "workers", "tasks", "assets"] as const;
type SideTab = (typeof SIDE_TABS)[number];
type BottomTab = (typeof BOTTOM_TABS)[number];
const SIDE_W = { min: 320, max: 620, initial: 400 };
const BOTTOM_H = { min: 160, max: 520, initial: 270 };
const clamp = (v: number, lim: { min: number; max: number }) => Math.min(lim.max, Math.max(lim.min, v));

/** 分隔线：只能在限定范围内拖，双击恢复默认 */
function Splitter({ dir, value, lim, onChange }: { dir: "col" | "row"; value: number; lim: { min: number; max: number; initial: number }; onChange: (v: number) => void }) {
  const start = useRef<{ at: number; value: number } | null>(null);
  return (
    <div
      className={`split ${dir}`}
      title="拖动调整大小，双击恢复默认"
      onDoubleClick={() => onChange(lim.initial)}
      onPointerDown={(e) => {
        start.current = { at: dir === "col" ? e.clientX : e.clientY, value };
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        if (!start.current) return;
        // 右栏在分隔线右边、底栏在分隔线下边：往左 / 往上拖是变大
        onChange(clamp(start.current.value + start.current.at - (dir === "col" ? e.clientX : e.clientY), lim));
      }}
      onPointerUp={() => (start.current = null)}
      onPointerCancel={() => (start.current = null)}
    />
  );
}

interface Props {
  state: AppState;
  account: Account | null;
  ticks: Record<string, number>;
  journal: JournalEntry[];
  pending: PendingItem[];
  inst: string;
  redUp: boolean;
  setInst: (inst: string) => void;
  onToggleWatch: (inst: string) => void;
  refresh: () => void;
  loggedIn: boolean;
}

export function Cockpit({ state, account, ticks, journal, pending, inst, redUp, setInst, onToggleWatch, refresh, loggedIn }: Props) {
  const [bar, setBar] = useStored("ouy.bar", "15m");
  const [chartSrc, setChartSrc] = useStored<"env" | "live">("ouy.chartsrc", "env");
  const demo = state.config.env === "demo";
  const src = demo ? chartSrc : "env";
  const [picker, setPicker] = useState(false);
  const [chartPx, setChartPx] = useState<{ inst: string; px: number } | null>(null);
  const [pick, setPick] = useState<{ price: number; n: number } | null>(null);
  const [stopEdit, setStopEdit] = useState<{ kind: "sl" | "tp"; from: number; to: number } | null>(null);
  const [saving, setSaving] = useState(false);
  const [trade, setTrade] = useState<{ side: "long" | "short"; n: number } | null>(null);
  const [shown, setShown] = useState<ShownPlan | null>(null);
  const [past, setPast] = useState<JournalEntry | null>(null); // 动态里点开的那条历史建议，临时画在图上
  const [preset, setPreset] = useState<(Extract<UiCommand, { type: "open_trade" }> & { n: number }) | null>(null);
  const [side, setSide] = useStored<SideTab>("ouy.cockpit.side", "chat");
  const [bottom, setBottom] = useStored<BottomTab>("ouy.cockpit.bottom", "positions");
  const [sideW, setSideW] = useStored("ouy.cockpit.sidew", SIDE_W.initial);
  const [bottomH, setBottomH] = useStored("ouy.cockpit.bottomh", BOTTOM_H.initial);
  const [unread, setUnread] = useState(0);
  const [narrow, setNarrow] = useState(() => window.innerWidth < 1100);
  useEffect(() => {
    const onResize = () => setNarrow(window.innerWidth < 1100);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  useEffect(() => {
    // 旧版自由拖动的布局已经不用了，把它存下来的东西清掉
    for (const key of ["ouy.layout.v1", "ouy.layout.known"]) localStorage.removeItem(key);
  }, []);
  const openTrade = (want: "long" | "short") => {
    setTrade((t) => ({ side: want, n: (t?.n ?? 0) + 1 }));
    setSide("trade");
  };
  const st = state.agent.instruments[inst];
  const pos = account?.positions.find((p) => p.inst_id === inst);
  const last = ticks[inst] || (chartPx?.inst === inst ? chartPx.px : 0) || st?.last || pos?.mark_px || 0;
  const up = redUp ? "#f6465d" : "#0ecb81";
  const down = redUp ? "#0ecb81" : "#f6465d";

  const held = useLotEntries(inst, pos);
  const move = pos && last ? (((last - pos.avg_px) / pos.avg_px) * 100 * (pos.side === "long" ? 1 : -1)).toFixed(1) : "";
  // 前高前低、区间、支撑阻力：和工作台的 K 线是同一套线、同一份开关
  const lv = useLevelLines(inst, bar, st?.regime?.candidates);
  const lines = useMemo<ChartLine[]>(() => {
    const out: ChartLine[] = [];
    if (pos) {
      out.push({ price: pos.avg_px, color: "#f0b90b", title: `${pos.side === "long" ? "多" : "空"}单入场${move ? ` · 现在 ${Number(move) >= 0 ? "+" : ""}${move}%` : ""}` });
      if (pos.liq_px) out.push({ price: pos.liq_px, color: "#ff7a00", title: "强平", dashed: true });
    }
    const seen = new Set<string>();
    for (const p of account?.protections.filter((x) => x.inst_id === inst) ?? []) {
      if (p.stop_loss && !seen.has(`sl${p.stop_loss}`)) {
        out.push({ price: p.stop_loss, color: "#f6465d", title: "止损", drag: "sl" });
        seen.add(`sl${p.stop_loss}`);
      }
      if (p.take_profit && !seen.has(`tp${p.take_profit}`)) {
        out.push({ price: p.take_profit, color: "#0ecb81", title: "止盈", drag: "tp" });
        seen.add(`tp${p.take_profit}`);
      }
    }
    for (const o of account?.orders.filter((x) => x.inst_id === inst) ?? []) {
      if (o.px) out.push({ price: o.px, color: "#8ab4ff", title: o.side === "buy" ? "买单" : "卖单", dashed: true, group: "orders" });
    }
    const plan = st?.report?.plan;
    if (plan && !pos) {
      out.push({ price: plan.entry_high, color: "#8ab4ff", title: "计划入场上沿", dashed: true, group: "plan" });
      out.push({ price: plan.entry_low, color: "#8ab4ff", title: "计划入场下沿", dashed: true, group: "plan" });
      out.push({ price: plan.stop_loss, color: "#f6465d", title: "计划止损", dashed: true, group: "plan" });
      plan.take_profits.forEach((tp, i) => out.push({ price: tp, color: "#0ecb81", title: `计划止盈${i + 1}`, dashed: true, group: "plan" }));
    } else if (plan && pos) {
      plan.take_profits.forEach((tp, i) => {
        if (!seen.has(`tp${tp}`)) out.push({ price: tp, color: "#0ecb81", title: `TP${i + 1}`, dashed: true, group: "plan" });
      });
    }
    if (shown && shown.inst_id === inst) {
      const p = shown.preview;
      if (p.entry_low !== p.entry_high) {
        out.push({ price: p.entry_high, color: "#c084fc", title: "方案入场上沿", dashed: true });
        out.push({ price: p.entry_low, color: "#c084fc", title: "方案入场下沿", dashed: true });
      } else {
        out.push({ price: p.entry, color: "#c084fc", title: "方案入场", dashed: true });
      }
      out.push({ price: p.stop_loss, color: "#f6465d", title: "方案止损", dashed: true });
      p.take_profits.forEach((tp, i) => out.push({ price: tp, color: "#0ecb81", title: `方案止盈${p.take_profits.length > 1 ? i + 1 : ""}`, dashed: true }));
    }
    if (past && past.inst_id === inst) {
      const color = { entry: "#ff9f43", sl: "#f6465d", tp: "#0ecb81", watch: "#848e9c", price: "#ffffff" } as const;
      for (const l of levelsOf(past)) out.push({ price: l.price, color: color[l.kind], title: l.title, dashed: true });
    }
    for (const l of st?.rules ?? []) {
      if (l.type === "watch" && l.status === "waiting" && l.level) out.push({ price: l.level, color: "#848e9c", title: "观察", dashed: true, group: "watch" });
    }
    for (const s of st?.strategies ?? []) {
      const p = s.params;
      if (p.observation) continue;
      const level = s.kind === "trigger" && String(p.when ?? "").match(/^(price|close)/) ? p.level : p.trigger;
      if (level) out.push({ price: level, color: "#c084fc", title: `#${s.id} 触发`, dashed: true, group: "trigger" });
      if (p.price) out.push({ price: p.price, color: "#c084fc", title: `#${s.id} 挂单价`, dashed: true, group: "trigger" });
    }
    const kept = out.filter((l) => !l.group || lv.on(l.group));
    for (const l of lv.lines) {
      if (!kept.some((x) => Math.abs(x.price - l.price) / l.price < 0.00005)) kept.push(l);
    }
    return kept;
  }, [pos, account, st?.report, st?.strategies, st?.rules, inst, shown, past, move, lv.lines, lv.show]);

  const guards = account?.protections.filter((x) => x.inst_id === inst) ?? [];
  const curSl = guards.find((g) => g.stop_loss)?.stop_loss ?? null;
  const curTp = guards.find((g) => g.take_profit)?.take_profit ?? null;
  const digits = last >= 1000 ? 1 : last >= 100 ? 2 : last >= 1 ? 3 : last >= 0.01 ? 5 : 8;
  const onDragLine = (kind: "sl" | "tp", price: number) => {
    const from = kind === "sl" ? curSl : curTp;
    if (from != null) setStopEdit({ kind, from, to: Number(price.toFixed(digits)) });
  };
  const saveStop = async () => {
    if (!stopEdit) return;
    setSaving(true);
    try {
      const r = await api.trade({
        kind: "set_stops",
        inst_id: inst,
        stop_loss: stopEdit.kind === "sl" ? stopEdit.to : curSl ?? undefined,
        take_profit: stopEdit.kind === "tp" ? stopEdit.to : curTp ?? undefined,
        reason: "在图上拖动修改",
      });
      toast(r.detail?.unfilled ? r.message : `${STATUS_LABEL[r.status]}：${r.message}`, r.status === "executed" && !r.detail?.unfilled ? "ok" : "error");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setSaving(false);
      setStopEdit(null);
      refresh();
    }
  };

  const market = (
    <MarketList
      selected={inst}
      watch={state.config.instruments}
      ticks={ticks}
      onSelect={(i) => {
        setInst(i);
        setPicker(false);
      }}
      onToggleWatch={onToggleWatch}
    />
  );

  const slots: Record<string, ReactNode> = {
    market,
    chart: (
        <div className="panel chart-panel">
          <div className="inst-head">
            <button className="inst-name" onClick={() => setPicker(true)} title="切换品种">
              {shortInst(inst)}
              <em>USDT 永续 ▾</em>
            </button>
            <span className="inst-last" style={{ color: pos ? (pos.upl >= 0 ? up : down) : undefined }}>
              {fmtPx(last)}
            </span>
            {pos && (
              <span className={`tag ${pos.side}`}>
                {pos.side === "long" ? "多" : "空"} · 投入 {fmtUsd(pos.margin, 0)} U · {pos.leverage}x
              </span>
            )}
            <div className="quick-trade">
              <button className="btn sm buy" onClick={() => openTrade("long")}>
                {pos?.side === "long" ? "加仓" : "做多"}
              </button>
              <button className="btn sm sell" onClick={() => openTrade("short")}>
                {pos?.side === "short" ? "加仓" : "做空"}
              </button>
            </div>
            {demo && (
              <div className="seg sm src-toggle" title="模拟盘有自己独立的盘口和成交，价格和成交量都和真实市场不一样。你的单子按模拟盘价格成交。">
                <button className={src === "env" ? "on" : ""} onClick={() => setChartSrc("env")}>
                  模拟盘行情
                </button>
                <button className={src === "live" ? "on" : ""} onClick={() => setChartSrc("live")}>
                  真实行情
                </button>
              </div>
            )}
            <div className="chart-levels">{lv.toggles}</div>
            <div className="bars">
              {BARS.map((b) => (
                <button key={b} className={b === bar ? "on" : ""} onClick={() => setBar(b)}>
                  {b}
                </button>
              ))}
            </div>
          </div>
          {src === "live" && <div className="src-note">正在看真实市场的 K 线。你的模拟盘单子按模拟盘价格成交，图上的持仓线、止损止盈线是模拟盘的价位，和这张图会有偏差。</div>}
          <Chart src={src} instId={inst} bar={bar} lines={lines} entries={held.entries} position={held.position} tick={ticks[inst]} redUp={redUp} onLast={(px) => setChartPx({ inst, px })} onDragLine={pos ? onDragLine : undefined} mark={past && past.inst_id === inst ? { ts: past.ts, text: `${fmtTime(past.ts)} 的建议` } : null} shiftMark={lv.shiftMark} algoMarks={lv.algoMarks} />
          <div className="legend">
            <span>
              <i style={{ background: "#f0b90b" }} />
              持仓均价
            </span>
            <span>
              <i style={{ background: "#f6465d" }} />
              止损
            </span>
            <span>
              <i style={{ background: "#0ecb81" }} />
              止盈
            </span>
            <span>
              <i style={{ background: "#ff7a00" }} />
              强平
            </span>
            <span>
              <i style={{ background: "#8ab4ff" }} />
              Agent 计划入场 / 挂单
            </span>
            <span>
              <i style={{ background: "#848e9c" }} />
              Agent 盯盘价位
            </span>
            {shown && shown.inst_id === inst && (
              <span>
                <i style={{ background: "#c084fc" }} />
                对话里的方案
                <button className="link" onClick={() => setShown(null)}>
                  收起
                </button>
              </span>
            )}
            {past && past.inst_id === inst && (
              <span>
                <i style={{ background: "#ff9f43" }} />
                {fmtTime(past.ts)} 的建议（临时显示）
                <button className="link" onClick={() => setPast(null)}>
                  收起
                </button>
              </span>
            )}
            {pos && (curSl || curTp) && <span>按住止损 / 止盈线上下拖动可以修改</span>}
          </div>
        </div>
    ),
    positions: <Positions account={account} maxLeverage={state.config.risk.max_leverage} onSelect={setInst} onDone={refresh} loggedIn={loggedIn} />,
    agent: (
      <AgentPanel inst={inst} agent={state.agent} config={state.config} account={account} pending={pending.filter((x) => !x.preview?.contracts)} onChanged={refresh} onToggleWatch={onToggleWatch} />
    ),
    assets: <AssetsPanel account={account} loggedIn={loggedIn} />,
    workers: <WorkersPanel agent={state.agent} config={state.config} account={account} inst={inst} onSelect={setInst} onChanged={refresh} />,
    tasks: <TasksPanel onSelect={setInst} />,
    chat: (
      <ChatPanel
        inst={inst}
          hidden={!narrow && side !== "chat"}
        agent={state.agent}
        defaultEffort={state.config.reasoning_effort}
        shown={shown}
        onShow={(plan) => {
          setShown(plan);
          if (plan && plan.inst_id !== inst) setInst(plan.inst_id);
        }}
        onActed={refresh}
        config={state.config}
        onUnread={setUnread}
        onUi={(commands) => {
          for (const c of commands) {
            if (c.type === "select_inst") setInst(c.inst_id);
            else if (c.type === "set_bar") setBar(c.bar);
            else if (c.type === "refresh") refresh();
            else if (c.type === "open_trade") {
              setInst(c.inst_id);
              setPreset((p) => ({ ...c, n: (p?.n ?? 0) + 1 }));
              openTrade(c.side);
            }
          }
        }}
      />
    ),
    feed: (
      <Journal
        entries={journal.slice(0, 60)}
        compact
        shownId={past?.id ?? null}
        onShow={(e) => {
          if (past?.id === e.id) return setPast(null);
          if (e.inst_id && e.inst_id !== inst) setInst(e.inst_id);
          setPast(e);
        }}
      />
    ),
    trade: (
      <div className="trade-win">
        <OrderBook inst={inst} last={last} onPick={(price) => setPick((p) => ({ price, n: (p?.n ?? 0) + 1 }))} />
        <OrderTicket
          inst={inst}
          last={last}
          account={account}
          risk={state.config.risk}
          onDone={refresh}
          loggedIn={loggedIn}
          pick={pick}
          want={trade}
          defaultMarginMode={state.config.margin_mode}
          defaultStopPct={state.config.default_stop_pct}
          defaultStyle={state.config.styles?.[inst] ?? state.config.style}
          preset={preset}
        />
      </div>
    ),
  };

  const pendingOrders = account?.orders.length ?? 0;
  const tabLabel = (id: string) => {
    if (id === "chat") return unread ? `对话 (${unread})` : "对话";
    if (id === "positions") return `持仓${account?.positions.length ? ` ${account.positions.length}` : ""}${pendingOrders ? ` · 挂单 ${pendingOrders}` : ""}`;
    return { agent: "Agent 盯盘", feed: "动态", trade: "下单", workers: "Worker 总览", tasks: "任务", assets: "资产" }[id] ?? id;
  };
  const tabs = <T extends string>(ids: readonly T[], active: T, set: (id: T) => void) => (
    <div className="pane-tabs">
      {ids.map((id) => (
        <button key={id} className={id === active ? "on" : ""} onClick={() => set(id)}>
          {tabLabel(id)}
        </button>
      ))}
    </div>
  );
  // 对话和下单窗一直挂着（切走只是隐藏）：这样未读消息照常计数，填到一半的单子不会丢
  const pane = (id: string, active: string, keep = false) =>
    id === active || keep ? (
      <div key={id} className="dock-slot" hidden={id !== active}>
        {slots[id]}
      </div>
    ) : null;

  return (
    <div className="cockpit-wrap">
      {narrow ? (
        <div className="cockpit-stack">
          {["chart", "trade", "positions", "agent", "workers", "assets", "chat", "feed", "tasks"].map((id) => (
            <section key={id} className="stack-item">
              <h4>{TITLES[id]}</h4>
              {slots[id]}
            </section>
          ))}
        </div>
      ) : (
        <div className="cockpit-grid" style={{ gridTemplateColumns: `250px minmax(0, 1fr) 6px ${clamp(sideW, SIDE_W)}px` }}>
          <aside className="pane">
            <div className="dock-slot">{slots.market}</div>
          </aside>
          <main className="pane-col" style={{ gridTemplateRows: `minmax(0, 1fr) 6px ${clamp(bottomH, BOTTOM_H)}px` }}>
            <section className="pane">
              <div className="dock-slot">{slots.chart}</div>
            </section>
            <Splitter dir="row" value={clamp(bottomH, BOTTOM_H)} lim={BOTTOM_H} onChange={setBottomH} />
            <section className="pane">
              {tabs(BOTTOM_TABS, bottom, setBottom)}
              {BOTTOM_TABS.map((id) => pane(id, bottom))}
            </section>
          </main>
          <Splitter dir="col" value={clamp(sideW, SIDE_W)} lim={SIDE_W} onChange={setSideW} />
          <aside className="pane">
            {tabs(SIDE_TABS, side, setSide)}
            {SIDE_TABS.map((id) => pane(id, side, id === "chat" || id === "trade"))}
          </aside>
        </div>
      )}
      {stopEdit && (
        <Modal title={`修改${stopEdit.kind === "sl" ? "止损" : "止盈"} · ${shortInst(inst)}`} onClose={() => setStopEdit(null)}>
          <p>
            把{stopEdit.kind === "sl" ? "止损" : "止盈"}从 <b>{fmtPx(stopEdit.from)}</b> 改到 <b>{fmtPx(stopEdit.to)}</b>？
          </p>
          {stopEdit.kind === "sl" && <p className="muted small">风控规则：止损只能往保护利润的方向移动，不能放宽到超过单仓风险上限，否则会被拒绝。</p>}
          <div className="modal-foot">
            <button className="btn" onClick={() => setStopEdit(null)}>
              取消
            </button>
            <button className="btn primary" disabled={saving} onClick={saveStop}>
              {saving ? "提交中…" : "确认修改"}
            </button>
          </div>
        </Modal>
      )}
      {picker && (
        <Modal title="选择合约" onClose={() => setPicker(false)}>
          {market}
        </Modal>
      )}
    </div>
  );
}
