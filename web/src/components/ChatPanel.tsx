import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, EFFORTS, KIND_LABEL, fmtPx, fmtTime, fmtUsd, shortInst, shotUrl, STATUS_LABEL, type ActionResult, type Activity, type ChatMessage, type Effort, type PlanPreview, type AgentConfig, type AgentStatus, type UiCommand } from "../api";
import { Markdown } from "./Markdown";
import { toast, useStored } from "./ui";
import { Composer } from "./Composer";
import { ErrorDetails } from "./ErrorDetails";
import { AVATAR, Earlier, NAME, RunCard, liveKey, settleTurns, toTurns, useLiveText, useRecent, type Turn } from "./AgentRun";

/** 图上正在展示的那份方案 */
export interface ShownPlan {
  id: number;
  inst_id: string;
  side: "long" | "short";
  preview: PlanPreview;
}

interface Props {
  inst: string;
  defaultEffort: Effort;
  shown: ShownPlan | null;
  onShow: (plan: ShownPlan | null) => void;
  onActed: () => void;
  config: AgentConfig;
  /** Agent 让界面做的事：切换图表、打开下单窗等 */
  onUi: (commands: UiCommand[]) => void;
  /** 有几条 Agent 主动发来、还没看的消息 */
  onUnread?: (count: number) => void;
  hidden?: boolean;
  /** 盯盘状态：用来知道这个币的 Worker 现在是不是正在思考 */
  agent?: AgentStatus;
  /** 活动记录：Agent 每一轮被谁叫醒、调了哪些工具、结论是什么，直接显示在对话里 */
  activity?: Activity[];
}

const PLAN_STATE: Record<string, string> = {
  waiting: "等你同意",
  running: "执行中…",
  executed: "已执行",
  rejected: "风控拒绝",
  failed: "没成功",
  declined: "已忽略",
  expired: "已过期",
  armed: "等待触发",
  active: "运行中",
  done: "规则已结束",
  cancelled: "已撤销",
  disabled: "已关闭",
  unavailable: "当前环境不可用",
};

/** 盯盘页里的 Agent 对话区：每个币种一段独立的对话，开仓 / 加仓以方案卡片的形式给出，点确认才下单。 */
/** 回答里用到的工具：和 Worker 运行卡片同一种样子——收成一行「N 步：……」，点开一步一行，再点看参数和返回 */
function MsgSteps({ steps }: { steps: { label: string; args?: Record<string, any>; result?: string; ok?: boolean }[] }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<number | null>(null);
  const name = (label: string) => label.replace(/（.*$/, "").replace("触发器", "触发线"); // 旧消息里存的名字还叫触发器
  const names = [...new Set(steps.map((st) => name(st.label)))];
  const brief = (args?: Record<string, any>) =>
    Object.entries(args || {})
      .filter(([, v]) => v != null && typeof v !== "object")
      .slice(0, 3)
      .map(([k, v]) => `${k}=${v}`)
      .join(" ");
  return (
    <div className="run-steps">
      <button type="button" className="run-fold" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} {steps.length} 步{open ? "" : `：${names.slice(0, 5).join("、")}${names.length > 5 ? "…" : ""}`}
      </button>
      {open && (
        <ol>
          {steps.map((st, k) => (
            <li key={k} className={`run-step ${st.ok === false ? "bad" : "ok"}`}>
              <button type="button" onClick={() => setDetail(detail === k ? null : k)}>
                <i className="dot" />
                <b>{name(st.label)}</b>
                <span>{brief(st.args)}</span>
              </button>
              {detail === k && (
                <div className="step-detail">
                  {Object.keys(st.args || {}).length > 0 && <p>参数：{JSON.stringify(st.args)}</p>}
                  {st.result ? <pre>{st.result}</pre> : <p>没有记到返回</p>}
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/** Agent 的一条回答：和运行卡片同一种样子——左边头像，上面一行名字和时间，下面一个框；框里先是挂在它下面的 Worker，再是它自己的步骤和正文 */
function AiCard({ m, brief, kids, children }: { m: ChatMessage; brief: boolean; kids?: ReactNode; children?: ReactNode }) {
  const [full, setFull] = useState(false);
  const worker = !!m.from && m.from !== "router";
  const who = worker ? (m.from!.includes("-") ? `${shortInst(m.from!.replace(" Worker", ""))} Worker` : m.from!) : "Router";
  const long = m.text.length > 220 || m.text.split("\n").length > 5;
  return (
    <div className={`msg agent ag-${worker ? "worker" : "router"}`}>
      <i className="ava">{worker ? "W" : "R"}</i>
      <div className="col">
        <div className="meta">
          <b>{who}</b>
          {m.ts ? <time>{fmtTime(m.ts)}</time> : null}
          {m.proactive && <span className="run-state">主动汇报</span>}
        </div>
        <div className="bub">
          {kids}
          {m.steps && m.steps.length > 0 && <MsgSteps steps={m.steps} />}
          {m.text && (
            <div className={`body ${brief && long && !full ? "clamp" : ""}`}>
              <Markdown text={m.text} />
            </div>
          )}
          {brief && long && (
            <button type="button" className="run-fold" onClick={() => setFull(!full)}>
              {full ? "收起" : "展开全文"}
            </button>
          )}
          {children}
        </div>
      </div>
    </div>
  );
}

/** 挂在 Router 卡片里的一个 Worker：干完默认收成一行（头像、名字、时间、一句结论），点开就地看步骤和全文；↗ 去它自己的对话看详细 */
function KidRow({ t, active, card, onJump }: { t: Turn; active: boolean; card: ReactNode; onJump?: () => void }) {
  const [picked, setPicked] = useState<boolean | null>(null);
  const waiting = t.asks.length > 0 && !t.done;
  const open = picked ?? (active || waiting);
  const who = `${t.inst && t.agent === "worker" ? `${shortInst(t.inst)} ` : ""}${NAME[t.agent] ?? t.agent}`;
  const line = (t.error ? `出错：${t.error}` : t.body || t.trigger || "").split("\n").map((x) => x.replace(/[*#`>]/g, "").replace(/^\s*[-•]\s*/, "").trim()).find(Boolean) ?? "";
  const jump = onJump && (
    <button type="button" className="kid-jump" title={`去 ${who} 的对话看详细`} aria-label={`去 ${who} 的对话看详细`} onClick={onJump}>
      ↗
    </button>
  );
  if (!open) {
    return (
      <div className={`kid-row ${t.error ? "bad" : ""}`}>
        <button type="button" className="kid-line" title="展开看步骤和全文" onClick={() => setPicked(true)}>
          <i className="kid-caret">▸</i>
          <i className="ava sm">{AVATAR[t.agent] ?? "W"}</i>
          <b>{who}</b>
          <time>{fmtTime(t.endTs ?? t.ts)}</time>
          <span>{line}</span>
        </button>
        {jump}
      </div>
    );
  }
  return (
    <div className="kid-open">
      {card}
      {!active && (
        <div className="kid-bar">
          <button type="button" className="run-fold" onClick={() => setPicked(false)}>
            ▴ 收起
          </button>
          {jump}
        </div>
      )}
    </div>
  );
}

/** 一张 Router 卡片，里面挂着同一批 Worker 干活的卡片。每个 Worker 默认收成一行；条数多了只摆最近 8 条，更早的点开再看 */
function WorkerBox({ list, busy, card }: { list: Turn[]; busy: number; card: (t: Turn) => ReactNode }) {
  const [all, setAll] = useState(false);
  const keep = 8;
  const shown = all ? list : list.slice(-keep);
  return (
    <div className={`msg agent ag-router ${busy ? "running" : ""}`}>
      <i className="ava">R</i>
      <div className="col">
        <div className="meta">
          <b>Router</b>
          <time>{fmtTime(shown[0].ts)}</time>
          <span className={`run-state ${busy ? "run" : ""}`}>
            {busy > 0 && <i className="spin" />}
            {busy ? `${busy} 个 Worker 在干活` : list.length > 1 ? `${list.length} 个 Worker 汇报` : "Worker 汇报"}
          </span>
        </div>
        <div className="bub">
          {list.length > keep && (
            <button type="button" className="run-fold" onClick={() => setAll(!all)}>
              {all ? "▾ 只看最近 8 条" : `▸ 更早的 ${list.length - keep} 条`}
            </button>
          )}
          {shown.map(card)}
        </div>
      </div>
    </div>
  );
}

export function ChatPanel({ inst: chartInst, defaultEffort, shown, onShow, onActed, config, onUi, onUnread, agent, activity, hidden }: Props) {
  // 对话区跟着图表上的币种走；但如果是 Agent 自己把图表切到别的币，这段对话留在原地，回答不会“消失”
  const [inst, setThread] = useState(chartInst);
  const pinned = useRef(false);
  useEffect(() => {
    if (pinned.current) pinned.current = false;
    else setThread(chartInst);
  }, [chartInst]);
  const [effort, setEffort] = useStored<Effort>("ouy.chatEffort", defaultEffort);
  // 直通路由：一般对 Router 说（它分派给 Worker）；也可以直接对这个币的 Worker 说
  const [to, setTo] = useStored<"router" | "worker">("ouy.chatTo", "router");
  const activeThread = to === "router" ? "general" : inst;
  const [unread, setUnread] = useState<Record<string, number>>({});
  const root = useRef<HTMLDivElement>(null);
  const [msgs, setMsgs] = useState<ChatMessage[]>([]);
  const [text, setText] = useState("");
  const [atOff, setAtOff] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const atMatch = /[@＠]([A-Za-z0-9]*)$/.exec(text);
  const atList = atMatch && !atOff
    ? ["Router", ...config.instruments.map(shortInst)].filter((name) => name.toUpperCase().startsWith(atMatch[1].toUpperCase()))
    : [];
  const insertAt = (name: string) => {
    setText((value) => value.replace(/[@＠][A-Za-z0-9]*$/, `@${name} `));
    input.current?.focus();
  };
  // 同时可以有好几句在路上：对方在忙时，新的话进待发框，或者点「立即发送」直接送进它这一轮
  const [sending, setSending] = useState(0);
  const busy = sending > 0;
  // 这句话发出去时活动记录写到哪了：之后才开始的那几轮就是为这句话跑的
  const lastActivity = useRef(0);
  lastActivity.current = activity?.length ? activity[activity.length - 1].id : 0;
  const sentAfter = useRef(0);
  const [acting, setActing] = useState<number | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const current = useRef(activeThread);
  current.current = activeThread;
  const readId = useRef(0);
  // 已经发出去、回答还没回来的话：后端要等回答出来才把这一问一答存下来，中途重新读历史时得把它们接回去，不然这句话会从页面上消失
  const outbox = useRef<(ChatMessage & { thread: string })[]>([]);

  const load = async (thread = current.current) => {
    if (thread !== current.current) return;
    const id = ++readId.current;
    try {
      const r = await api.chatLog(thread);
      if (current.current === thread && readId.current === id) {
        setMsgs([...r.messages, ...outbox.current.filter((o) => o.thread === thread)]);
        return r.messages;
      }
    } catch {
      /* 读不到历史不影响继续聊 */
    }
  };

  useEffect(() => {
    setMsgs([]);
    load(activeThread);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeThread, config.env]);

  useLayoutEffect(() => {
    followLatest.current = true;
  }, [activeThread, config.env, hidden]);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!hidden && el && followLatest.current) el.scrollTop = el.scrollHeight;
  }, [msgs, busy, activeThread, hidden, activity?.length]);
  useEffect(() => {
    const el = scroller.current;
    const body = content.current;
    if (!el || !body) return;
    // A hidden panel and lazy images acquire their height after messages have loaded.
    const observer = new ResizeObserver(() => {
      if (el.clientHeight > 0 && followLatest.current) el.scrollTop = el.scrollHeight;
    });
    observer.observe(el);
    observer.observe(body);
    return () => observer.disconnect();
  }, []);

  // Agent 主动发来的消息（Worker 的方案、对仓位的建议）：正在看这段对话就直接刷新，否则记成未读
  useEffect(() => {
    const onChat = (ev: Event) => {
      const thread = (ev as CustomEvent<{ thread: string }>).detail?.thread;
      if (!thread) {
        load();
        return;
      }
      const visible = !!root.current?.offsetParent && !document.hidden;
      if (thread === current.current) load(thread);
      if (thread !== current.current || !visible) setUnread((u) => ({ ...u, [thread]: (u[thread] ?? 0) + 1 }));
    };
    window.addEventListener("ouy:chat", onChat);
    return () => window.removeEventListener("ouy:chat", onChat);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // 这段对话露出来了，就算看过了
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const seen = new IntersectionObserver((entries) => {
      if (entries.some((x) => x.isIntersecting)) setUnread((u) => (u[current.current] ? { ...u, [current.current]: 0 } : u));
    });
    seen.observe(el);
    return () => seen.disconnect();
  }, []);
  useEffect(() => {
    if (root.current?.offsetParent) setUnread((u) => (u[activeThread] ? { ...u, [activeThread]: 0 } : u));
  }, [activeThread, hidden, msgs.length]);
  const unreadTotal = Object.values(unread).reduce((a, b) => a + b, 0);
  useEffect(() => {
    onUnread?.(unreadTotal);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unreadTotal]);

  const send = async (content = text, images: string[] = []) => {
    const message = content.trim();
    if (!message) return;
    const mentions = [...message.matchAll(/[@＠]([A-Za-z0-9]+)/g)].map((m) => m[1].toUpperCase());
    const otherTarget = mentions.includes("ROUTER") || config.instruments.some((i) => i !== inst && mentions.includes(shortInst(i).toUpperCase()));
    const target = otherTarget ? "router" : to;
    const thread = target === "router" ? "general" : inst;
    if (target !== to) setTo(target);
    if (content === text) setText("");
    followLatest.current = true;
    // 带上发出的时间：不带的话它会排到这之后 Worker 自己那几轮的前面，看起来像没发出去
    const sent = { role: "user" as const, text: message, to: target, images, ts: Date.now(), thread };
    const settle = () => {
      outbox.current = outbox.current.filter((o) => o !== sent);
    };
    outbox.current.push(sent);
    if (current.current === thread) setMsgs((m) => [...m, sent]);
    setSending((n) => {
      if (n === 0) sentAfter.current = lastActivity.current;
      return n + 1;
    });
    try {
      const r = await api.chat(message, effort, thread, inst, target, false, images);
      settle();
      if (r.steered) toast("它正在思考，这句话已直接送进它当前这一轮，回答随这一轮的结论发在这里", "ok");
      await load(thread);
      if (r.ui?.length) {
        if (r.ui.some((c) => c.type === "select_inst" && c.inst_id !== chartInst)) pinned.current = true;
        onUi(r.ui);
      }
      onActed();
    } catch (e: any) {
      settle();
      const history = await load(thread);
      if (current.current === thread && !history?.[history.length - 1]?.error) setMsgs((m) => [...m, { role: "ai", text: `出错了：${e.message}` }]);
    } finally {
      setSending((n) => n - 1);
    }
  };

  const decide = async (id: number, ok: boolean) => {
    setActing(id);
    try {
      if (ok) {
        const r = await api.approve(id);
        toast(r.detail?.unfilled ? r.message : `${STATUS_LABEL[r.status]}：${r.message}`, r.status === "executed" && !r.detail?.unfilled ? "ok" : "error");
      } else {
        await api.reject(id);
      }
      if (shown?.id === id) onShow(null);
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setActing(null);
      load();
      onActed();
    }
  };

  /** Agent 想布置一条策略，等用户点同意 */
  const strategyCard = (a: ActionResult, key: string) => {
    const id = a.strategy_id!;
    const state = a.pending_status || "unavailable";
    const decideStrategy = async (verb: "approve" | "cancel") => {
      setActing(-id);
      try {
        await api.strategyAct(id, verb);
        toast(verb === "approve" ? "策略已启用，执行器开始盯" : "已拒绝，这条策略不会运行", "ok");
      } catch (e: any) {
        toast(e.message, "error");
      } finally {
        setActing(null);
        load();
        onActed();
      }
    };
    return (
      <div key={key} className={`plan ${state}`}>
        <div className="plan-head">
          <span className="tag">
            布置策略 · {a.detail?.label} · {shortInst(a.action?.inst_id ?? "")}
          </span>
          <span className={`plan-state ${state}`}>{PLAN_STATE[state] ?? state}</span>
        </div>
        <div className="small">{a.message}</div>
        {(a.pending_result?.message || a.detail?.note) && <div className="muted small">{a.pending_result?.message || a.detail.note}</div>}
        {a.pending_result?.detail?.ordId && <div className="small">委托号 {a.pending_result.detail.ordId}</div>}
        {state === "waiting" && (
          <div className="row">
            <button className="btn sm primary" disabled={acting === -id} onClick={() => decideStrategy("approve")}>
              {acting === -id ? "处理中…" : "同意启用"}
            </button>
            <button className="btn sm" disabled={acting === -id} onClick={() => decideStrategy("cancel")}>
              不用
            </button>
          </div>
        )}
      </div>
    );
  };

  /** Agent 建议的减仓 / 平仓等：没有自己做，等用户点同意 */
  const suggestion = (a: ActionResult, key: string) => {
    const act = a.action!;
    const id = a.pending_id!;
    const state = a.pending_status || "waiting";
    const what = `${KIND_LABEL[act.kind] ?? act.kind} ${shortInst(act.inst_id)}${act.fraction ? ` ${Math.round(act.fraction * 100)}%` : ""}${act.lot_id ? ` · 第 ${act.lot_id} 笔` : ""}`;
    return (
      <div key={key} className={`plan ${state}`}>
        <div className="plan-head">
          <span className="tag">{what}</span>
          <span className={`plan-state ${state}`}>{PLAN_STATE[state] ?? state}</span>
        </div>
        {act.reason && <div className="muted small">{act.reason}</div>}
        {state !== "waiting" && a.pending_result?.message && <div className="muted small">{a.pending_result.message}</div>}
        {state === "waiting" && (
          <div className="row">
            <button className="btn sm primary" disabled={acting === id} onClick={() => decide(id, true)}>
              {acting === id ? "执行中…" : "同意执行"}
            </button>
            <button className="btn sm" disabled={acting === id} onClick={() => decide(id, false)}>
              不执行
            </button>
          </div>
        )}
      </div>
    );
  };

  const plan = (a: ActionResult, key: string) => {
    const p = a.detail as PlanPreview;
    const act = a.action!;
    const id = a.pending_id!;
    const state = a.pending_status || "waiting";
    const long = act.side === "long";
    const isShown = shown?.id === id;
    return (
      <div key={key} className={`plan ${state}`}>
        <div className="plan-head">
          <span className={`tag ${long ? "long" : "short"}`}>
            {act.kind === "add" ? "加仓" : long ? "做多" : "做空"} {shortInst(act.inst_id)} · {p.leverage}x · {p.margin_mode === "isolated" ? "逐仓" : "全仓"}
          </span>
          <span className={`plan-state ${state}`}>{state === "executed" ? "订单已提交" : (PLAN_STATE[state] ?? state)}</span>
        </div>
        <dl>
          <dt>入场</dt>
          <dd>
            {p.entry_low !== p.entry_high ? `${fmtPx(p.entry_low)} – ${fmtPx(p.entry_high)}` : fmtPx(p.entry)}
            <em>{act.order_type === "limit" ? `限价 ${fmtPx(act.price)}` : "市价"}</em>
          </dd>
          <dt>下单</dt>
          <dd>
            投入 {fmtUsd(p.margin_usd)} U<em>名义 {fmtUsd(p.notional_usd, 0)} U</em>
          </dd>
          <dt>止损</dt>
          <dd className="down-fixed">
            {fmtPx(p.stop_loss)}
            <em>最多亏 {fmtUsd(p.risk_usd)} U</em>
          </dd>
          <dt>止盈</dt>
          <dd className="up-fixed">
            {p.take_profits.length ? p.take_profits.map((t) => fmtPx(t)).join(" / ") : "未设"}
            {p.reward_risk != null && <em>盈亏比 {p.reward_risk}</em>}
          </dd>
        </dl>
        {act.reason && <div className="muted small">{act.reason}</div>}
        {p.notes?.length > 0 && <div className="muted small">{p.notes.join("；")}</div>}
        {state !== "waiting" && a.pending_result?.message && <div className="muted small">{a.pending_result.message}</div>}
        <div className="row">
          {state === "waiting" && (
            <button className="btn sm primary" disabled={acting === id} onClick={() => decide(id, true)}>
              {acting === id ? "执行中…" : "同意执行"}
            </button>
          )}
          <button className={`btn sm ${isShown ? "on" : ""}`} onClick={() => onShow(isShown ? null : { id, inst_id: act.inst_id, side: act.side ?? "long", preview: p })}>
            {isShown ? "从图上收起" : "在图上显示"}
          </button>
          {state === "waiting" && (
            <button className="btn sm" disabled={acting === id} onClick={() => decide(id, false)}>
              忽略
            </button>
          )}
        </div>
      </div>
    );
  };

  const name = shortInst(inst);
  const workerBusy = !!(agent?.instruments[inst]?.busy || agent?.instruments[inst]?.reviewing);

  // Agent 的运行过程直接显示在对话里。对 Worker 的这一段：它自己被叫醒的每一轮（定时看盘、计划线到了、算法事件）按时间插在对话中间；
  // 对 Router 的这一段：正在跑的（Router 自己这一轮，以及此刻正在思考的 Worker），加上各个 Worker 跑完的结论
  const live = useLiveText();
  const turns = useMemo(() => (activity && agent ? settleTurns(toTurns(activity), agent) : []), [activity, agent]);
  const mine = (t: Turn) => t.user === undefined && (to === "worker" ? t.agent === "worker" && t.inst === inst : t.agent === "router" || t.agent === "worker" || t.agent === "general");
  const latestOpen = new Map<string, string>();
  for (const t of turns) if (t.user === undefined && !t.done) latestOpen.set(liveKey(t), t.key);
  const isActive = (t: Turn) => !t.done && latestOpen.get(liveKey(t)) === t.key;
  const busyRun = (t: Turn) => busy && t.id > sentAfter.current;
  const running = turns.filter((t) => mine(t) && (to === "router" ? t.agent === "router" : true) && (isActive(t) || (busyRun(t) && (to === "router" || t.byUser))));
  // 对 Router 的这一段：Worker 干活挂在 Router 的卡片里面。Router 交出去的那几轮挂在它正在跑的这张卡片里，跑完挂到它那条回答里；
  // Worker 自己被叫醒的（定时看盘、线到了、算法事件）收在一张「Worker 汇报」的 Router 卡片里，干完留着，不跟着「思考中」一起消失。没变化跳过的不放
  const kid = (t: Turn) => to === "router" && mine(t) && t.agent !== "router";
  const liveKids = turns.filter((t) => kid(t) && t.byRouter && busyRun(t));
  const side = turns.filter((t) => kid(t) && !t.byUser && !liveKids.includes(t) && (isActive(t) || (t.done && !t.skipped && !!(t.body || t.error || t.asks.length)))).slice(-30);
  // 对 Worker 的这一段：它自己被叫醒的每一轮按时间插在对话中间
  const past = to === "worker" ? turns.filter((t) => mine(t) && t.done && !t.byUser && !running.includes(t)).slice(-30) : [];
  type Item = { ts: number; m?: ChatMessage; i: number; t?: Turn; group?: Turn[]; kids?: Turn[] };
  const items: Item[] = [];
  let lastTs = 0;
  msgs.forEach((m, i) => {
    lastTs = m.ts ?? (lastTs || Date.now());
    items.push({ ts: lastTs, m, i });
  });
  for (const t of past) {
    const at = t.endTs ?? t.ts;
    const k = items.findIndex((x) => x.ts > at);
    items.splice(k < 0 ? items.length : k, 0, { ts: at, t, i: -1 });
  }
  const sameBatch = (g: Turn[], t: Turn) => g.some(isActive) || t.ts <= Math.max(...g.map((x) => x.endTs ?? x.ts)) + 60_000;
  for (const t of side) {
    const at = isActive(t) ? Infinity : t.endTs ?? t.ts;
    const k = items.findIndex((x) => x.ts > at);
    const end = k < 0 ? items.length : k;
    // 一问一答是回答出来时一起存的（时间相同）：Router 交出去的那一轮结束得比它早，挂到那条回答里
    if (t.byRouter && items[end]?.m?.role === "user" && items[end + 1]?.m?.role === "ai") (items[end + 1].kids ??= []).push(t);
    // 汇报一次出一张卡片；同一批的（上一个还在干活或刚汇报完一分钟内又来一个）放进同一张
    else if (items[end - 1]?.group && sameBatch(items[end - 1].group!, t)) items[end - 1].group!.push(t);
    else if (items[end]?.group && at >= Math.min(...items[end].group!.map((x) => x.ts)) - 60_000) items[end].group!.push(t); // 后开工的先干完：和还在后面的那一批是同一批
    else items.splice(end, 0, { ts: at, group: [t], i: -1 });
  }
  const recent = useRecent(items, 30);
  const lastRun = past[past.length - 1];
  const lastMsg = msgs.length - 1;
  const runCard = (t: Turn) => <RunCard key={t.key} t={t} active={isActive(t)} live={isActive(t) ? live[liveKey(t)] : undefined} brief={t !== lastRun && !isActive(t)} />;
  const toWorker = (id: string) => {
    pinned.current = false;
    setTo("worker");
    setThread(id);
    onUi([{ type: "select_inst", inst_id: id }]);
  };
  const kidCard = (t: Turn) => (
    <KidRow
      key={t.key}
      t={t}
      active={isActive(t)}
      onJump={t.agent === "worker" && t.inst ? () => toWorker(t.inst) : undefined}
      card={<RunCard t={t} nested active={isActive(t)} live={isActive(t) ? live[liveKey(t)] : undefined} brief={false} onPick={(id) => onUi([{ type: "select_inst", inst_id: id }])} />}
    />
  );
  const workerBox = (key: string, list: Turn[]) => <WorkerBox key={key} list={list} busy={list.filter(isActive).length} card={kidCard} />;
  const samples = [`${name} 现在能做多还是做空？给我一个方案`, `帮我做多 ${name}，投 200 U`, "看一下我的持仓，要不要动", "把止损移到保本"];

  return (
    <div className="chat-dock" ref={root} hidden={hidden}>
      <div className="chat-thread">
        <b>{to === "router" ? "Router" : `${name} Worker`}</b>
        {to === "worker" && workerBusy && (
          <span className="run-state run">
            <i className="spin" />
            思考中
          </span>
        )}
        {inst !== chartInst && (
          <button className="btn xs" onClick={() => setThread(chartInst)}>
            图表现在是 {shortInst(chartInst)}，切到它的对话
          </button>
        )}
        {Object.entries(unread)
          .filter(([thread, n]) => n > 0 && thread !== activeThread)
          .map(([thread, n]) => (
            <button
              key={thread}
              className="btn xs unread"
              title="这个币的 Worker 主动发来了方案或建议"
              onClick={() => {
                if (thread === "general") {
                  setTo("router");
                  return;
                }
                pinned.current = false;
                setTo("worker");
                setThread(thread);
                onUi([{ type: "select_inst", inst_id: thread }]);
              }}
            >
              {thread === "general" ? "Router" : shortInst(thread)} 有 {n} 条新消息
            </button>
          ))}
        <div className="seg two slim chat-to" title="一般对 Router 说，它看全局、分派任务；也可以直接对这个币的 Worker 说，它用自己的上下文和交易工具回答">
          <button className={to === "router" ? "on" : ""} onClick={() => setTo("router")}>
            对 Router 说
          </button>
          <button className={to === "worker" ? "on" : ""} onClick={() => setTo("worker")}>
            对 {name} Worker 说
          </button>
        </div>
        {to === "worker" && config.instruments.length > 1 && (
          <select aria-label="选择对话 Worker" value={inst} onChange={(event) => {
            const thread = event.target.value;
            pinned.current = false;
            setThread(thread);
            onUi([{ type: "select_inst", inst_id: thread }]);
          }}>
            {config.instruments.map((i) => <option key={i} value={i}>{shortInst(i)}</option>)}
          </select>
        )}
      </div>
      <div className="chat-body" ref={scroller} onScroll={(event) => {
        const el = event.currentTarget;
        if (el.clientHeight > 0) followLatest.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }}>
        <div className="chat-messages" ref={content}>
        {items.length === 0 && running.length === 0 && liveKids.length === 0 && !busy && (
          <div className="chat-empty">
            <div className="chips">
              {samples.map((s) => (
                <button key={s} className="chip" onClick={() => send(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        <Earlier count={recent.earlier} onMore={recent.more} />
        {recent.visible.map(({ m, i, t, group, kids }) => {
          if (t) return runCard(t);
          if (group) return workerBox(`g${group[0].key}`, group);
          if (!m) return null;
          const extras = (
            <>
            {m.error && <ErrorDetails error={m.error} />}
            {!!m.images?.length && (
              <div className="msg-shots">
                {m.images.map((x, k) => (
                  <a key={k} href={shotUrl(x)} target="_blank" rel="noreferrer">
                    <img src={shotUrl(x)} alt={`附图 ${k + 1}`} loading="lazy" />
                  </a>
                ))}
              </div>
            )}
            {m.confirms?.map((c, k) => (
              <div key={`c${k}`} className="plan waiting">
                <div className="plan-head">
                  <b>需要你确认的设置修改</b>
                </div>
                <div>{c.summary}</div>
                <div className="row">
                  <button
                    className="btn sm primary"
                    disabled={!!m.env && m.env !== config.env}
                    onClick={async () => {
                      try {
                        await api.saveConfig({ ...config, ...c.patch, risk: { ...config.risk, ...c.patch.risk } }, config.env === "live");
                        toast("设置已修改", "ok");
                        onActed();
                      } catch (e: any) {
                        toast(e.message, "error");
                      }
                    }}
                  >
                    确认修改
                  </button>
                </div>
              </div>
            ))}
            {m.actions?.map((a, j) =>
              a.pending_id && a.detail?.contracts && a.action ? (
                plan(a, `${i}-${j}`)
              ) : a.strategy_id ? (
                strategyCard(a, `${i}-${j}`)
              ) : a.pending_id && a.action ? (
                suggestion(a, `${i}-${j}`)
              ) : (
                <div key={j} className={`bubble-act ${a.status}`}>
                  {STATUS_LABEL[a.status] ?? a.status}：{a.message}
                </div>
              ),
            )}
            </>
          );
          if (m.role === "ai") return <AiCard key={i} m={m} brief={i !== lastMsg} kids={kids?.map(kidCard)}>{extras}</AiCard>;
          return (
            <div key={i} className="bubble user">
              <div className="bubble-text">{m.text}</div>
              {extras}
            </div>
          );
        })}
        {running.map((t, k) =>
          to === "router" ? (
            <RunCard key={t.key} t={t} active={isActive(t)} live={isActive(t) ? live[liveKey(t)] : undefined}>
              {k === 0 && liveKids.map(kidCard)}
            </RunCard>
          ) : (
            runCard(t)
          ),
        )}
        {to === "router" && !running.length && liveKids.length > 0 && workerBox("live", liveKids)}
        {busy && !running.length && !liveKids.length && (
          <div className={`msg agent ag-${to} running`}>
            <i className="ava">{to === "worker" ? "W" : "R"}</i>
            <div className="col">
              <div className="meta">
                <b>{to === "worker" ? `${name} Worker` : "Router"}</b>
              </div>
              <div className="bub">
                <span className="run-wait"><i /><i /><i /><span>思考中</span></span>
              </div>
            </div>
          </div>
        )}
        </div>
      </div>
      <Composer
        value={text}
        onChange={(value) => {
          setAtOff(false);
          setText(value);
        }}
        inputRef={input}
        onKey={(event) => {
          if (!atList.length) return false;
          if (event.key === "Escape") setAtOff(true);
          if (event.key !== "Enter" && event.key !== "Tab") return false;
          event.preventDefault();
          insertAt(atList[0]);
          return true;
        }}
        pop={atList.length > 0 && <div className="at-pop">
          {atList.map((name) => <button key={name} type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => insertAt(name)}>＠{name}</button>)}
        </div>}
        placeholder={to === "worker" ? `直接对 ${name} 的 Worker 说，比如「你现在怎么看」` : `对 Router 说点什么，比如「帮我做空 ${name}」`}
        queueKey={`chat:${config.env}:${activeThread}`}
        busy={busy || (to === "worker" && workerBusy)}
        busyText={`${to === "worker" ? `${name} Worker` : "Router"} 正在忙`}
        onSend={(t, images) => send(t, images)}
        left={
          <label className="muted small" title="接手这件事的 Worker 的思考强度：越高想得越细，但越慢、越费 token。Router 只负责分发，它的强度在设置里（默认低）">
            思考强度
            <select value={effort} onChange={(e) => setEffort(e.target.value as Effort)}>
              {EFFORTS.map((e) => (
                <option key={e.id} value={e.id}>
                  {e.label}
                </option>
              ))}
            </select>
          </label>
        }
        right={
          <button
            type="button"
            className="cmp-link"
            disabled={busy || msgs.length === 0}
            onClick={async () => {
              if (!window.confirm("清空这段对话？")) return;
              try {
                await api.chatClear(activeThread);
                await load(activeThread);
              } catch (e: any) {
                toast(e.message, "error");
              }
            }}
          >
            清空对话
          </button>
        }
      />
    </div>
  );
}
