import { useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as RPointerEvent } from "react";
import { api, fmtPx, fmtUsd, shortInst, type Account, type Activity, type AppState, type InstStatus } from "../api";
import type { RecorderCond, SubAgent } from "../api";
import { dataLabel, recordNow } from "./Recorders";

const fmtNow = (v: number | null | undefined) => (v === null || v === undefined ? "—" : String(Math.round(v * 100) / 100));

/* 团队图：一张可以缩放、拖动的节点图。Router 在上，连到每个币的 Worker 小组；
   小组是一个框，框里是 Worker 和它手下的子代理、正在跑的策略。缩小看全局，放大看每个节点正在做什么。 */

interface Props {
  state: AppState;
  account: Account | null;
  ticks: Record<string, number>;
  activity: Activity[];
  sel: string; // router | 币 | 币:角色 | 币:s策略编号
  onSelect: (sel: string) => void;
}

const GW = 360; // 小组框的宽
const GAP = 56;
const ROUTER = { w: 300, h: 92 };
const WORKER_H = 118;
const SUB_H = 96;
const STRAT_H = 58;
const QUANT_H = 74; // 常驻「量化监测」节点的高
const POS_H = 104; // 有仓位 / 挂单时「仓位」节点的高
const FLAT_H = 38; // 空仓时
const PAD = 16;
const HEAD = 34;
const STRATEGY_STATE: Record<string, string> = { proposed: "等你同意", armed: "等触发", active: "运行中", done: "已结束", cancelled: "已撤销", expired: "已过期", failed: "已停止" };

type Tone = "live" | "wait" | "idle" | "off";

function workerTone(st: InstStatus | undefined, running: boolean): Tone {
  if (!st || st.paused || st.goal?.status === "stopped") return "off";
  if (st.busy || st.reviewing) return "live";
  if (st.awaiting || st.queue || st.inbox) return "wait";
  return running ? "idle" : "off";
}

function workerStatus(st: InstStatus | undefined, running: boolean): string {
  if (!st) return "未启动";
  if (st.paused) return "已暂停";
  if (st.goal?.status === "stopped") return "已停手";
  if (st.awaiting) return `${st.awaiting} 个操作等你同意`;
  if (st.busy || st.reviewing) return "思考中";
  if (st.inbox) return `${st.inbox} 条留言待读`;
  if (!running) return "空闲";
  return st.next_review_in != null ? `${st.next_review_in < 60 ? `${st.next_review_in} 秒` : `${Math.round(st.next_review_in / 60)} 分钟`}后再看` : "待命";
}

const ago = (ts: number) => {
  const m = Math.max(Math.round((Date.now() - ts) / 60000), 0);
  return m < 1 ? "刚刚" : m < 60 ? `${m} 分钟前` : `${Math.round(m / 60)} 小时前`;
};

/** 从 (x1,y1) 向下到 (x2,y2) 的一条平滑连线 */
const curve = (x1: number, y1: number, x2: number, y2: number) => {
  const m = (y1 + y2) / 2;
  return `M ${x1} ${y1} C ${x1} ${m}, ${x2} ${m}, ${x2} ${y2}`;
};

export function TeamCanvas({ state, account, ticks, activity, sel, onSelect }: Props) {
  const cfg = state.config;
  const agent = state.agent;
  const box = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const [size, setSize] = useState({ w: 0, h: 0 });
  const touched = useRef(false); // 用户自己缩放 / 拖动过之后，不再自动适应窗口

  // 每个 Agent 最近在做的一步（工具调用、结果、结论），用来在节点里显示「正在做什么」
  const latest = useMemo(() => {
    const map = new Map<string, Activity>();
    for (const a of activity) {
      if (["tool", "handoff", "result", "done", "ask", "wake", "action", "error", "skip"].includes(a.kind)) map.set(a.agent === "router" ? "router" : `${a.inst_id}:${a.agent}`, a);
    }
    return map;
  }, [activity]);

  // 最近一分半钟里发生过交接的连线：亮起来并流动
  const hot = useMemo(() => {
    const since = Date.now() - 90_000;
    const set = new Set<string>();
    for (const a of activity) {
      if (a.ts < since) continue;
      if (a.agent === "router" && a.kind === "handoff" && a.inst_id) set.add(`router>${a.inst_id}`);
      if (a.agent === "worker" && a.kind === "wake" && a.data?.source === "router") set.add(`router>${a.inst_id}`);
      if (["intel", "smart", "risk"].includes(a.agent)) set.add(`${a.inst_id}>${a.agent}`);
      if (a.agent === "strategy") {
        set.add(`${a.inst_id}>strategy`);
        if (a.data?.quant || a.data?.shift || a.data?.fast) set.add(`${a.inst_id}>quant`);
      }
    }
    return set;
  }, [activity]);

  // 布局：Router 居中在上，小组排成几行。排几列由画布的形状决定——哪种排法缩放后最大就用哪种
  const layout = useMemo(() => {
    const insts = cfg.instruments;
    const build = (cols: number) => {
      const groups = insts.map((inst, i) => {
        const st = agent.instruments[inst];
        const strategies = st?.strategies ?? [];
        // 预设的子代理 + Worker 自己临时开的：都画成这个小组里的节点，和 Worker 连着线。
        // 临时开的只在它干活时、或者 Worker 决定留着它时才在图上；一次性的交回结果就消失，留着的等 Worker 自己关掉才消失
        const team: SubAgent[] = [
          ...(st?.team ?? []),
          ...(st?.adhoc ?? []).slice(-4).map((a) => ({
            role: `adhoc-${a.name}` as SubAgent["role"],
            label: a.keep && !a.running ? `${a.name} · 留用` : a.name,
            does: a.task,
            running: a.running,
            brief: a.summary ? ({ summary: a.summary, ts: a.ts } as SubAgent["brief"]) : null,
          })),
        ];
        const pos = account?.positions.find((p) => p.inst_id === inst);
        const orders = (account?.orders ?? []).filter((o) => o.inst_id === inst);
        const posH = pos || orders.length ? POS_H + (pos && orders.length ? 18 : 0) : FLAT_H;
        const h = HEAD + PAD + WORKER_H + 22 + posH + 20 + QUANT_H + (team.length ? 34 + SUB_H : 0) + (strategies.length ? 30 + strategies.length * (STRAT_H + 8) : 0) + PAD;
        return { inst, st, team, strategies, pos, orders, posH, h, col: i % cols, row: Math.floor(i / cols), x: 0, y: 0 };
      });
      const rows = Math.max(...groups.map((g) => g.row), 0) + 1;
      const width = Math.max(cols * GW + (cols - 1) * GAP, ROUTER.w + (70 + 270) * 2); // 至少放得下 Router 和它右边的 Watchdog
      let y = ROUTER.h + 90;
      for (let r = 0; r < rows; r++) {
        const inRow = groups.filter((g) => g.row === r);
        const rowW = inRow.length * GW + (inRow.length - 1) * GAP;
        inRow.forEach((g, i) => {
          g.x = (width - rowW) / 2 + i * (GW + GAP);
          g.y = y;
        });
        y += Math.max(...inRow.map((g) => g.h), 0) + 80;
      }
      return { groups, width, height: y - 80 + 10, router: { x: width / 2 - ROUTER.w / 2, y: 0 } };
    };
    const n = Math.max(insts.length, 1);
    const options = [n, Math.ceil(n / 2)].filter((c, i, all) => c >= 1 && all.indexOf(c) === i).map(build); // 一行，或者两行
    const score = (l: ReturnType<typeof build>) => Math.min((size.w || 1200) / l.width, (size.h || 500) / l.height);
    return options.reduce((best, l) => (score(l) > score(best) ? l : best), options[0]);
  }, [cfg.instruments, agent.instruments, account, size.w, size.h]);

  const fit = () => {
    const el = box.current;
    if (!el) return;
    const w = el.clientWidth;
    const h = el.clientHeight;
    const k = Math.min((w - 40) / layout.width, (h - 40) / layout.height, 1.15);
    setView({ k, x: (w - layout.width * k) / 2, y: Math.max((h - layout.height * k) / 2, 16) });
  };
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  useEffect(() => {
    if (!touched.current) fit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size.w, size.h, layout.width, layout.height]);

  // 滚轮缩放（以鼠标位置为中心）
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      touched.current = true;
      const r = el.getBoundingClientRect();
      const px = e.clientX - r.left;
      const py = e.clientY - r.top;
      setView((v) => {
        const k = Math.min(Math.max(v.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12), 0.25), 2.2);
        return { k, x: px - ((px - v.x) / v.k) * k, y: py - ((py - v.y) / v.k) * k };
      });
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, []);

  // 按住空白处拖动
  const drag = useRef<{ x: number; y: number; vx: number; vy: number; moved: boolean } | null>(null);
  const down = (e: RPointerEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest(".tc-node, .tc-tools, .tc-legend")) return; // 点节点、点工具条不算拖动画布
    drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const move = (e: RPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    touched.current = true;
    setView((v) => ({ ...v, x: d.vx + e.clientX - d.x, y: d.vy + e.clientY - d.y }));
  };
  const up = () => {
    drag.current = null;
  };
  const zoom = (f: number) => {
    touched.current = true;
    setView((v) => {
      const k = Math.min(Math.max(v.k * f, 0.25), 2.2);
      const cx = size.w / 2;
      const cy = size.h / 2;
      return { k, x: cx - ((cx - v.x) / v.k) * k, y: cy - ((cy - v.y) / v.k) * k };
    });
  };

  const lod = view.k < 0.5 ? 0 : view.k < 0.85 ? 1 : 2; // 缩得越小显示得越少
  // Router 到小组的连线：第一行直接连；后面几行的线从上一行小组之间的空隙里穿下去，不压在别的小组上
  const routerEdge = (g: (typeof layout.groups)[number]) => {
    const wx = g.x + GW / 2;
    if (g.row === 0) return curve(rx, ry, wx, g.y);
    const first = layout.groups.filter((x) => x.row === 0);
    const gaps = [first[0].x - GAP / 2, ...first.map((x) => x.x + GW + GAP / 2)];
    const gx = gaps.reduce((best, x) => (Math.abs(x - wx) < Math.abs(best - wx) ? x : best), gaps[0]);
    const top = first[0].y - 34;
    return `${curve(rx, ry, gx, top)} L ${gx} ${g.y - 46} ${curve(gx, g.y - 46, wx, g.y).replace(/^M [^C]+/, "")}`;
  };
  const pool = agent.pool?.workers;
  const dog = agent.watchdog;
  const dogIssues = dog?.checks ?? [];
  const routerNow = latest.get("router");
  const rx = layout.router.x + ROUTER.w / 2;
  const ry = layout.router.y + ROUTER.h;

  return (
    <div className={`tc lod-${lod}`} ref={box} onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up}>
      <div className="tc-world" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})`, width: layout.width, height: layout.height }}>
        <svg className="tc-edges" width={layout.width} height={layout.height}>
          <path className={`tc-edge thin dashed ${dogIssues.length ? "hot" : ""}`} d={`M ${layout.router.x + ROUTER.w} ${layout.router.y + ROUTER.h / 2} L ${layout.router.x + ROUTER.w + 70} ${layout.router.y + ROUTER.h / 2}`} />
          {layout.groups.map((g) => {
            const wx = g.x + GW / 2;
            const wy = g.y + HEAD + PAD + WORKER_H;
            const py = wy + 22 + g.posH; // 「仓位」节点的下沿
            const qy = py + 20 + QUANT_H; // 「量化监测」节点的下沿
            const subY = qy + 34;
            const subW = g.team.length ? (GW - PAD * 2 - (g.team.length - 1) * 8) / g.team.length : 0;
            return (
              <g key={g.inst}>
                <path className={`tc-edge ${hot.has(`router>${g.inst}`) ? "hot" : ""} ${workerTone(g.st, agent.running) === "live" ? "live" : ""}`} d={routerEdge(g)} />
                {g.team.map((m, i) => {
                  const sx = g.x + PAD + i * (subW + 8) + subW / 2;
                  return <path key={m.role} className={`tc-edge thin ${m.running || hot.has(`${g.inst}>${m.role}`) ? "hot" : ""}`} d={curve(wx, qy, sx, subY)} />;
                })}
                <path className={`tc-edge thin ${g.pos ? "live" : ""}`} d={`M ${wx} ${wy} L ${wx} ${wy + 22}`} />
                <path className={`tc-edge thin ${hot.has(`${g.inst}>quant`) ? "hot" : g.st?.fast?.on ? "live" : ""}`} d={`M ${wx} ${py} L ${wx} ${py + 20}`} />
                {g.strategies.length > 0 && (
                  // 策略的连线走小组左边的留白，不从别的节点上面穿过去：Worker 左边出来 → 往下 → 拐进每条策略
                  <path
                    className={`tc-edge thin dashed ${hot.has(`${g.inst}>strategy`) ? "hot" : ""}`}
                    d={
                      `M ${g.x + PAD} ${wy - 16} L ${g.x + 8} ${wy - 16} L ${g.x + 8} ${g.y + g.h - PAD - STRAT_H / 2}` +
                      g.strategies.map((_, i) => ` M ${g.x + 8} ${g.y + g.h - PAD - STRAT_H / 2 - i * (STRAT_H + 8)} L ${g.x + PAD + 24} ${g.y + g.h - PAD - STRAT_H / 2 - i * (STRAT_H + 8)}`).join("")
                    }
                  />
                )}
              </g>
            );
          })}
          {/* Router ↔ 通用 Worker，通用 Worker → 它临时开的子代理 */}
          <path
            className={`tc-edge ${agent.general?.busy ? "hot" : ""}`}
            d={`M ${layout.router.x} ${layout.router.y + ROUTER.h / 2} L ${layout.router.x - 70} ${layout.router.y + ROUTER.h / 2}`}
          />
          {(agent.router_adhoc ?? []).slice(-3).map((a, i) => {
            const right = layout.router.x - 340 - i * 200;
            return <path key={a.id} className={`tc-edge thin ${a.running ? "hot" : ""}`} d={`M ${right} ${layout.router.y + ROUTER.h / 2} L ${right - 30} ${layout.router.y + ROUTER.h / 2}`} />;
          })}
        </svg>

        <button className={`tc-node tc-router ${agent.running ? "live" : "off"} ${sel === "router" ? "on" : ""}`} style={{ left: layout.router.x, top: layout.router.y, width: ROUTER.w, height: ROUTER.h }} onClick={() => onSelect("router")}>
          <div className="tc-h">
            <i className="tc-light" />
            <b>Router</b>
            <span className="tc-s">
              {agent.running ? "值班中" : "盯盘没开"}
              {pool ? ` · ${pool.running}/${pool.max} 个 Worker 在思考` : ""}
            </span>
          </div>
          <div className="tc-d">{routerNow ? routerNow.text : "对你说话、把任务分给各个币的 Worker、看全局风险"}</div>
        </button>

        <button
          className={`tc-node tc-general ${agent.general?.busy ? "live" : "idle"}`}
          style={{ left: layout.router.x - 340, top: layout.router.y, width: 270, height: ROUTER.h }}
          onClick={() => onSelect("router")}
          title="通用 Worker：不绑定某个币。Router 把普通问题（看好什么币、有什么消息、比较几个币）交给它去想、去查，结论交回 Router"
        >
          <div className="tc-h">
            <i className="tc-light" />
            <b>通用 Worker</b>
            <span className="tc-s">{agent.general?.busy ? "思考中" : "待命"}</span>
          </div>
          <div className="tc-d">{agent.general?.busy ? agent.general.task : agent.general?.summary || "不绑定某个币：回答普通问题、查消息、比较币种，需要时自己开子代理"}</div>
        </button>

        {(agent.router_adhoc ?? []).slice(-3).map((a, i) => (
          <button
            key={a.id}
            className={`tc-node tc-sub ${a.running ? "live" : "idle"}`}
            style={{ left: layout.router.x - 340 - (i + 1) * 200, top: layout.router.y, width: 170, height: ROUTER.h }}
            onClick={() => onSelect("router")}
            title={`通用 Worker 临时开的子代理\n任务：${a.task}`}
          >
            <div className="tc-h">
              <i className="tc-light" />
              <b>{a.name}</b>
            </div>
            <span className="tc-s">{a.running ? "工作中" : "留用中"}</span>
            <div className="tc-d">{a.running ? a.task : a.summary || a.task}</div>
          </button>
        ))}

        <button
          className={`tc-node tc-dog ${!agent.running ? "off" : dogIssues.length ? (dogIssues.some((c) => c.level === "bad") ? "bad" : "wait") : "live"} ${sel === "watchdog" ? "on" : ""}`}
          style={{ left: layout.router.x + ROUTER.w + 70, top: layout.router.y, width: 270, height: ROUTER.h }}
          onClick={() => onSelect("router")}
          title="Watchdog 独立于所有 Agent：每 15 秒看盯盘循环和每个 Worker 有没有卡住（卡住就重启），每分钟巡检账户和团队"
        >
          <div className="tc-h">
            <i className="tc-light" />
            <b>Watchdog</b>
            <span className="tc-s">{!agent.running ? "盯盘没开" : dogIssues.length ? `${dogIssues.length} 个问题` : "一切正常"}</span>
          </div>
          <div className="tc-d">
            {dogIssues.length
              ? dogIssues.map((c) => c.text).join("；")
              : `盯着循环、每个 Worker、止损单和挂单${dog?.restarts ? `；已自动修复 ${dog.restarts} 次${dog.last ? `（${dog.last}）` : ""}` : ""}`}
          </div>
        </button>

        {layout.groups.map((g) => {
          const tone = workerTone(g.st, agent.running);
          const pos = g.pos;
          const prot = (account?.protections ?? []).filter((x) => x.inst_id === g.inst);
          const sl = prot.find((x) => x.stop_loss)?.stop_loss;
          const tp = prot.find((x) => x.take_profit)?.take_profit;
          const below = WORKER_H + 22 + g.posH + 20 + QUANT_H; // Worker + 仓位 + 常驻量化监测节点一共占的高度
          const now = latest.get(`${g.inst}:worker`);
          const busy = !!(g.st?.busy || g.st?.reviewing);
          const subW = g.team.length ? (GW - PAD * 2 - (g.team.length - 1) * 8) / g.team.length : 0;
          const wy = HEAD + PAD;
          const qt = g.st?.quant;
          const qActive = agent.running && !g.st?.paused;
          const sampled = qt?.sample_ts && Date.now() - qt.sample_ts < 60_000;
          const qTone = !qActive ? "off" : sampled ? "live" : "idle";
          const fast = g.st?.fast;
          const fastLabel = !qActive ? "已暂停" : !sampled ? "暂无近期采样" : !fast?.on || qt?.mode === "quiet" || (g.st?.standby && !pos)
            ? "仅监测" : fast.mode === "verify" ? "信号需核实" : fast.mode === "auto" ? "双向自动"
            : ({ long: "已授权多", short: "已授权空", both: "已授权双向" }[fast.on_signal] ?? "信号需核实");
          const qBias = qt?.bias === "long" ? "只盯多" : qt?.bias === "short" ? "只盯空" : "";
          return (
            <div key={g.inst} className={`tc-group ${tone} ${sel.split(":")[0] === g.inst ? "on" : ""}`} style={{ left: g.x, top: g.y, width: GW, height: g.h }}>
              <div className="tc-gh">
                <b>{shortInst(g.inst)}</b>
                <em>{fmtPx(ticks[g.inst] || g.st?.last)}</em>
                {pos && (
                  <span className={`tc-pos ${pos.upl >= 0 ? "win" : "lose"}`}>
                    {pos.side === "long" ? "多" : "空"} {fmtUsd(pos.margin, 0)} U {pos.upl >= 0 ? "+" : ""}
                    {pos.upl.toFixed(2)}
                  </span>
                )}
              </div>
              <button className={`tc-node tc-worker ${tone} ${sel === g.inst ? "on" : ""}`} style={{ left: PAD, top: wy, width: GW - PAD * 2, height: WORKER_H }} onClick={() => onSelect(g.inst)}>
                <div className="tc-h">
                  <i className="tc-light" />
                  <b>Worker</b>
                  <span className="tc-s">{workerStatus(g.st, agent.running)}</span>
                </div>
                <div className="tc-d">
                  {busy && now ? `正在：${now.text}` : g.st?.awaiting ? "有操作等你同意，点开查看" : g.st?.report ? g.st.report.summary : "还没有复盘过"}
                </div>
                {g.st?.goal && <div className="tc-goal">目标：{g.st.goal.goal}</div>}
              </button>
              <button
                className={`tc-node tc-posn ${pos ? (pos.upl >= 0 ? "win" : "lose") : g.orders.length ? "wait" : "flat"} ${sel === `${g.inst}:pos` ? "on" : ""}`}
                style={{ left: PAD, top: wy + WORKER_H + 22, width: GW - PAD * 2, height: g.posH }}
                onClick={() => onSelect(`${g.inst}:pos`)}
              >
                {pos ? (
                  <>
                    <div className="tc-h">
                      <b>
                        仓位 · {pos.side === "long" ? "多" : "空"} · 投入 {fmtUsd(pos.margin, 0)} U · {pos.leverage}x
                      </b>
                      <span className="tc-pnl">
                        {pos.upl >= 0 ? "+" : ""}
                        {fmtUsd(pos.upl)} U（{pos.upl_ratio >= 0 ? "+" : ""}
                        {(pos.upl_ratio * 100).toFixed(1)}%）
                      </span>
                    </div>
                    <div className="tc-kv">
                      <span>入场</span>
                      <b>{fmtPx(pos.avg_px)}</b>
                      <span>现价</span>
                      <b>{fmtPx(ticks[g.inst] || pos.mark_px)}</b>
                      <span>止损</span>
                      <b className={sl ? "" : "warn"}>{sl ? fmtPx(sl) : "没设"}</b>
                      <span>止盈</span>
                      <b>{tp ? fmtPx(tp) : "没设"}</b>
                      <span>强平</span>
                      <b>{pos.liq_px ? fmtPx(pos.liq_px) : "—"}</b>
                      <span>名义</span>
                      <b>{fmtUsd(pos.notional_usd, 0)} U</b>
                    </div>
                    {g.orders.length > 0 && <div className="tc-ord">另有 {g.orders.length} 张挂单{g.orders.some((o) => o.reduce_only) ? "（含没成交的平仓单）" : ""}</div>}
                  </>
                ) : g.orders.length ? (
                  <>
                    <div className="tc-h">
                      <b>仓位 · 空仓</b>
                      <span className="tc-s">{g.orders.length} 张挂单等成交</span>
                    </div>
                    <div className="tc-d">
                      {g.orders
                        .slice(0, 3)
                        .map((o) => `${o.side === "buy" ? "买" : "卖"} ${o.margin_usd ? `投入 ${fmtUsd(o.margin_usd, 0)} U` : "只减仓"} @ ${o.px ? fmtPx(o.px) : "市价"}`)
                        .join("；")}
                    </div>
                  </>
                ) : (
                  <div className="tc-h">
                    <b>仓位 · 空仓</b>
                    <span className="tc-s">没有持仓和挂单</span>
                  </div>
                )}
              </button>
              <button
                className={`tc-node tc-quant ${qTone} ${sel === `${g.inst}:quant` ? "on" : ""}`}
                style={{ left: PAD, top: wy + WORKER_H + 22 + g.posH + 20, width: GW - PAD * 2, height: QUANT_H }}
                onClick={() => onSelect(`${g.inst}:quant`)}
                title="常驻量化监测引擎：每 10 秒识别结构转折与撤旧单，每 4 秒跑快判信号；点开可在右侧切换档位"
              >
                <div className="tc-h">
                  <i className="tc-light" />
                  <b>量化监测</b>
                  <span className="tc-s">
                    {fastLabel} / {qt?.label || "自动识别"}
                    {qBias ? ` / ${qBias}` : ""}
                  </span>
                </div>
                <div className="tc-d">{qt?.narrative || qt?.reason || qt?.desc || "等待量化读数"}</div>
              </button>
              {g.team.map((m, i) => {
                const a = latest.get(`${g.inst}:${m.role}`);
                return (
                  <button
                    key={m.role}
                    className={`tc-node tc-sub ${m.running ? "live" : m.brief ? "idle" : "off"} ${sel === `${g.inst}:${m.role}` ? "on" : ""}`}
                    style={{ left: PAD + i * (subW + 8), top: wy + below + 34, width: subW, height: SUB_H }}
                    onClick={() => onSelect(String(m.role).startsWith("adhoc-") ? g.inst : `${g.inst}:${m.role}`)}
                    title={m.does}
                  >
                    <div className="tc-h">
                      <i className="tc-light" />
                      <b>{m.label}</b>
                    </div>
                    <span className="tc-s">{m.running ? "工作中" : m.brief ? ago(m.brief.ts) : "还没查过"}</span>
                    <div className="tc-d">{m.running && a ? a.text : m.brief ? m.brief.summary : m.does}</div>
                  </button>
                );
              })}
              {g.strategies.map((s, i) => (
                <button
                  key={s.id}
                  className={`tc-node tc-strat ${s.status === "active" ? "live" : s.status === "proposed" ? "wait" : "idle"} ${sel === `${g.inst}:s${s.id}` ? "on" : ""}`}
                  style={{ left: PAD + 24, top: wy + below + (g.team.length ? 34 + SUB_H : 0) + 30 + i * (STRAT_H + 8), width: GW - PAD * 2 - 24, height: STRAT_H }}
                  onClick={() => onSelect(`${g.inst}:s${s.id}`)}
                >
                  <div className="tc-h">
                    <i className="tc-light" />
                    <b>{s.params.recorder ? "记录器" : `策略 · ${s.label}`}</b>
                    <span className="tc-s">
                      {s.params.recorder ? (s.params.off ? "已关" : s.status === "proposed" ? "等你同意" : s.state.runs ? `在记 · 到过 ${s.state.runs} 次` : "在记") : STRATEGY_STATE[s.status] ?? s.status}
                    </span>
                    {s.status === "proposed" && (
                      <span className="tc-acts">
                        <span role="button" className="yes" onClick={(e) => (e.stopPropagation(), void api.strategyAct(s.id, "approve").catch(() => {}))}>
                          同意
                        </span>
                        <span role="button" onClick={(e) => (e.stopPropagation(), void api.strategyAct(s.id, "cancel").catch(() => {}))}>
                          不执行
                        </span>
                      </span>
                    )}
                  </div>
                  <div className="tc-d one">
                    {s.params.recorder
                      ? (s.params.conds as RecorderCond[]).map((c) => `${dataLabel(c.data)} ${c.op === "above" ? "≥" : "≤"} ${c.level}（现在 ${fmtNow(recordNow(g.st, c.data))}）`).join(" 且 ") +
                        (s.params.do === "wake" ? " → 叫醒 Worker" : ` → 先${s.text.split(" → ")[1] ?? "执行"}，再叫醒 Worker`)
                      : s.text}
                  </div>
                </button>
              ))}
            </div>
          );
        })}
      </div>

      <div className="tc-tools">
        <button onClick={() => zoom(1.25)} title="放大">
          ＋
        </button>
        <button onClick={() => zoom(0.8)} title="缩小">
          －
        </button>
        <button
          onClick={() => {
            touched.current = false;
            fit();
          }}
          title="适应窗口"
        >
          适应窗口
        </button>
        <span>{Math.round(view.k * 100)}%</span>
      </div>
      <div className="tc-legend">
        <span>
          <i className="tc-light live" /> 正在工作
        </span>
        <span>
          <i className="tc-light wait" /> 等你同意 / 有留言
        </span>
        <span>
          <i className="tc-light idle" /> 待命
        </span>
        <span className="muted">滚轮缩放 · 拖动空白处平移 · 点节点和它对话</span>
      </div>
    </div>
  );
}
