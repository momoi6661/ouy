import { useEffect, useMemo, useState, type ReactNode } from "react";
import { fmtTime, shortInst, type Activity, type AgentStatus } from "../api";
import { ErrorDetails } from "./ErrorDetails";
import { Markdown } from "./Markdown";

/** Agent 的一轮运行：被叫醒 → 思考 → 调工具 → 结论。工作台的团队动态和对话区都用这一套来显示。 */
export const NAME: Record<string, string> = { general: "通用 Worker", adhoc: "临时子代理", watchdog: "Watchdog", router: "Router", worker: "Worker", strategy: "策略执行器", intel: "情报", smart: "聪明钱", risk: "风险复核" };
export const AVATAR: Record<string, string> = { general: "通", adhoc: "子", watchdog: "看", router: "R", worker: "W", strategy: "策", intel: "情", smart: "聪", risk: "风" };

export interface Step {
  id: number;
  label: string;
  why?: string;
  result?: string;
  handoff?: boolean;
  sub?: boolean; // 这一步是临时开的子代理：它自己的过程收在这一步里，点开才看
  tool?: string;
  args?: Record<string, any>;
}
/** Router 用这些工具把事情交出去：交出去之后它这条消息就结束，等 Worker 说完，它的总结另起一条排在后面 */
const HANDOFF_TOOLS = new Set(["ask_worker", "ask_general", "ask_intel"]);
/** 开子代理的工具调用本身不出标签：子代理自己会出一个标签，不然每开一个出两个 */
const SPAWN_TOOLS = new Set(["spawn_subagent", "spawn_helper"]);
export interface Turn {
  key: string;
  /** 这一轮第一条活动记录的编号：用来判断它是不是在某个时刻之后才开始的 */
  id: number;
  ts: number;
  /** 这一轮结束的时间（出结论 / 出错 / 跳过） */
  endTs?: number;
  /** 这一轮是你在对话里直接叫起来的：回答会出现在对话里 */
  byUser?: boolean;
  /** 被叫醒后程序判断没变化，没调用模型 */
  skipped?: boolean;
  /** 连着几轮都是「没变化、跳过」时并成一行，这里是并了几轮 */
  skips?: number;
  agent: string;
  inst: string;
  user?: string; // 用户说的话（右边的气泡）
  images?: string[]; // 这句话附的图片
  sub?: string; // 气泡下面的小字：这句话是怎么送到的
  trigger?: string;
  steps: Step[];
  asks: Activity[];
  notes: Activity[]; // 排队、插话、你的决定、执行的操作
  body?: string;
  error?: string;
  errorEvent?: Activity;
  done: boolean;
  handed?: boolean; // Router 已经把事情交出去了：这条消息到此为止
  quiet?: Step[]; // 不出标签的步骤（开子代理的调用）；出错了才显示
}

/** 活动流按「一轮」归到一起：一次唤醒 → 若干步工具调用 → 结论 */
export function toTurns(rows: Activity[]): Turn[] {
  const turns: Turn[] = [];
  const open = new Map<string, Turn>();
  const subSteps = new Map<number, Step>();
  const subLogs = new Map<number, string>();
  for (const a of rows) {
    if (a.kind === "interject" && a.data?.who === "用户") {
      // 你在它思考时送进去的话，或者留的言：就是你说的一句话
      turns.push({ key: `u${a.id}`, id: a.id, ts: a.ts, agent: "user", inst: a.inst_id, user: a.text.replace(/^[^：]*：/, ""), sub: a.data?.delivered ? "已送进它正在跑的这一轮" : "它空闲，已放进信箱", steps: [], asks: [], notes: [], done: true });
      continue;
    }
    if (a.agent === "adhoc" && a.kind === "closed") continue; // 关子代理这件事已经显示在关它的那条消息里（「关掉子代理」标签）
    // 临时子代理不单开一条消息：收进派它的那条消息里，当成一个工具小卡片
    if (a.agent === "adhoc" && a.work_id != null) {
      let step = subSteps.get(a.work_id);
      if (!step && a.kind === "wake") {
        const by = a.data?.by === "Router" ? "router" : a.data?.by === "通用 Worker" ? "general" : "worker";
        const parent = [...turns].reverse().find((t) => t.user === undefined && t.agent === by && (by !== "worker" || t.inst === a.inst_id));
        if (parent) {
          step = { id: a.id, label: `子代理「${a.data?.name ?? "临时"}」`, why: a.text.replace(/^[^：]*：/, ""), handoff: true, sub: true };
          parent.steps.push(step);
          subSteps.set(a.work_id, step);
          subLogs.set(a.work_id, "");
        }
      }
      if (step) {
        const log = subLogs.get(a.work_id) ?? "";
        if (a.kind === "tool") subLogs.set(a.work_id, `${log}· ${a.text}\n`);
        else if (a.kind === "done") step.result = `${log}结论：${a.text}`;
        else if (a.kind === "error") step.result = `${log}没查成：${a.text}`;
        continue;
      }
    }
    const key = a.work_id != null ? `w${a.work_id}` : a.agent === "router" ? "router" : `s${a.id}`;
    let t = open.get(key);
    const start = !t || (a.kind === "wake" && a.work_id == null);
    if (start) {
      t = { key: `${key}-${a.id}`, id: a.id, ts: a.ts, agent: a.agent, inst: a.inst_id, steps: [], asks: [], notes: [], done: a.agent === "strategy" || a.agent === "watchdog" };
      open.set(key, t);
      turns.push(t);
    }
    if (!t) continue;
    if (a.kind === "interject" && start && a.work_id == null) t.done = true; // 独立留言不是新一轮思考；插入已有任务也不代表它已结束
    // Router 交出任务后，后面的总结另起一条，排在 Worker 的消息后面（中间没有别人说话就还接在原来那条里）
    if (t.handed && a.agent === "router" && a.kind !== "result" && a.kind !== "wake" && a.kind !== "handoff" && !(HANDOFF_TOOLS.has(a.data?.tool) && a.kind === "tool")) {
      if (turns[turns.length - 1] !== t) {
        t = { key: `${key}-${a.id}`, id: a.id, ts: a.ts, agent: a.agent, inst: a.inst_id, steps: [], asks: [], notes: [], done: false };
        open.set(key, t);
        turns.push(t);
      } else {
        t.handed = false;
        t.done = false;
      }
    }
    if (a.kind === "handoff" && a.agent !== "router" && a.data?.goal) t.done = true; // 你在面板上定的目标：这条只是记录，不是它在思考
    if (a.kind === "wake" && a.agent === "strategy" && a.data?.strategy_id && a.data?.status === "proposed") t.asks.push(a);
    if (a.kind === "wake") {
      if (a.data?.source === "user") {
        t.byUser = true;
        turns.splice(turns.length - 1, 0, { key: `u${a.id}`, id: a.id, ts: a.ts, agent: "user", inst: a.agent === "router" ? "" : a.inst_id, user: a.text.replace(/^用户：/, ""), images: a.data?.images, steps: [], asks: [], notes: [], done: true });
      } else t.trigger = a.text;
    } else if (a.kind === "tool" || a.kind === "handoff") {
      const step: Step = { id: a.id, label: a.text, why: a.data?.why, handoff: a.kind === "handoff", tool: a.data?.tool, args: a.data?.args };
      if (SPAWN_TOOLS.has(a.data?.tool)) (t.quiet ??= []).push(step);
      else t.steps.push(step);
      if (a.agent === "router" && (a.kind === "handoff" || HANDOFF_TOOLS.has(a.data?.tool))) {
        t.handed = true;
        t.done = true;
      }
    } else if (a.kind === "result") {
      const pending = [...t.steps, ...(t.quiet ?? [])].filter((x) => x.result === undefined && !x.sub);
      const last = pending.reverse().find((x) => !a.data?.tool || !x.tool || x.tool === a.data.tool) ?? pending[0];
      if (last) {
        last.result = a.text.replace(/^[^→]*→\s*/, "");
        if (t.quiet?.includes(last) && last.result.startsWith("[")) t.steps.push(last); // 子代理没开成：把这一步亮出来
      }
    } else if (a.kind === "ask") t.asks.push(a);
    else if (a.kind === "done") {
      t.body = a.text;
      t.endTs = a.ts;
      t.done = true;
    } else if (a.kind === "error") {
      t.endTs = a.ts;
      t.error = a.text;
      t.errorEvent = a;
      t.done = true;
    } else if (a.kind === "skip") {
      t.endTs = a.ts;
      t.skipped = true;
      t.body = a.text;
      t.done = true;
    } else t.notes.push(a);
  }
  return turns;
}

/** 没结束的标记只留给「这个 Agent 最新的一条、而且它确实还在忙」：之前被打断、重启丢掉的那些不再一直转圈 */
export function settleTurns(list: Turn[], agent: AgentStatus): Turn[] {
  const latest = new Map<string, Turn>();
  for (const t of list) if (t.user === undefined && !t.done) latest.set(`${t.agent}:${t.inst}`, t);
  const settled = list.map((t) => {
    if (t.user !== undefined || t.done) return t;
    const w = agent.instruments[t.inst];
    const busy = t.agent === "worker" ? !!(w?.busy || w?.reviewing) : t.agent === "general" ? !!agent.general?.busy : Date.now() - t.ts < 5 * 60_000;
    return latest.get(`${t.agent}:${t.inst}`) !== t || !busy || t.asks.length ? { ...t, done: true } : t;
  });
  // 连着几轮「没变化、跳过」只留最后一行，写上一共几轮：不然定时看盘一多，页面上全是这种行
  const out: Turn[] = [];
  for (const t of settled) {
    const prev = out[out.length - 1];
    const bare = (x: Turn) => !!x.skipped && !x.steps.length && !x.asks.length;
    if (prev && bare(t) && bare(prev) && prev.agent === t.agent && prev.inst === t.inst) out[out.length - 1] = { ...t, skips: (prev.skips ?? 1) + 1 };
    else out.push(t);
  }
  return out;
}

/** 页面上一次只摆最近的一段，更早的点「更早的」再往前翻：记录本身都还在，只是不一次全画出来 */
export function useRecent<T>(list: T[], page = 20): { visible: T[]; earlier: number; more: () => void } {
  const [n, setN] = useState(page);
  const visible = useMemo(() => list.slice(-n), [list, n]);
  return { visible, earlier: Math.max(0, list.length - n), more: () => setN((x) => x + page) };
}
export function Earlier({ count, onMore }: { count: number; onMore: () => void }) {
  if (count <= 0) return null;
  return (
    <button type="button" className="run-earlier" onClick={onMore}>
      更早的 {count} 条
    </button>
  );
}

export interface LiveText {
  text: string;
  thinking: string;
}
/** 流式内容按「谁:哪个币」存；Router 不分币 */
export const liveKey = (t: Pick<Turn, "agent" | "inst">) => `${t.agent}:${t.agent === "router" ? "" : t.inst}`;

/** 流式输出：Agent 正在写的内容（还没写完）；写完就清掉，由正式的结论接上 */
export function useLiveText(): Record<string, LiveText> {
  const [live, setLive] = useState<Record<string, LiveText>>({});
  useEffect(() => {
    const on = (e: Event) => {
      const d = (e as CustomEvent).detail as { agent: string; inst_id: string; text: string; thinking: string; done: boolean };
      const key = liveKey({ agent: d.agent, inst: d.inst_id });
      setLive((all) => {
        if (!d.done) return { ...all, [key]: { text: d.text, thinking: d.thinking } };
        const { [key]: _gone, ...rest } = all;
        return rest;
      });
    };
    window.addEventListener("ouy:stream", on);
    return () => window.removeEventListener("ouy:stream", on);
  }, []);
  return live;
}

const FAILED = /^(出错|\[被风控拒绝\]|\[执行失败\]|调用失败|没查成)/;
const secs = (ms: number) => (ms < 60_000 ? `${Math.max(1, Math.round(ms / 1000))} 秒` : `${Math.floor(ms / 60_000)} 分 ${Math.round((ms % 60_000) / 1000)} 秒`);

interface RunProps {
  t: Turn;
  /** 这一轮正在流出来的内容 */
  live?: LiveText;
  /** 这一轮现在正在跑（它是这个 Agent 最新的一轮，而且 Agent 确实在忙） */
  active: boolean;
  /** 旧的一轮：长结论先收成几行，点「展开」再看全文 */
  brief?: boolean;
  askWaiting?: (a: Activity) => boolean;
  renderAsk?: (a: Activity) => ReactNode;
  onPick?: (inst: string) => void;
}

/** 一轮运行的卡片：谁、为什么被叫醒、一步步调了什么工具（正在跑的转圈，跑完的打勾，点开看参数和返回）、正在想 / 正在写的内容、结论。 */
export function RunCard({ t, live, active, brief, askWaiting, renderAsk, onPick }: RunProps) {
  const [openStep, setOpenStep] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<boolean | null>(null);
  const [full, setFull] = useState(false);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);

  const who = `${t.inst && t.agent !== "router" && t.agent !== "watchdog" && t.agent !== "general" ? `${shortInst(t.inst)} ` : ""}${NAME[t.agent] ?? t.agent}`;
  if (t.skipped && !t.steps.length && !t.asks.length) {
    return (
      <div className="run-skip" title={t.trigger}>
        {fmtTime(t.ts)} · {who}：{t.skips ? `连着 ${t.skips} 轮 · ` : ""}{t.body}
      </div>
    );
  }
  const asking = t.asks.some((a) => askWaiting?.(a));
  const stepState = (s: Step) => (s.result === undefined ? (active ? "run" : "idle") : FAILED.test(s.result) ? "bad" : "ok");
  const runningStep = active ? [...t.steps].reverse().find((s) => s.result === undefined && !s.sub) : undefined;
  const stage = runningStep ? `正在${runningStep.label.replace(/（.*$/, "")}…` : live?.text ? "正在写结论…" : "思考中…";
  // 正在跑的时候步骤全部摊开；跑完收成一行，点开再看
  const open = expanded ?? active;
  const long = !!t.body && (t.body.length > 220 || t.body.split("\n").length > 5);
  const clamp = !!brief && !active && !asking && long && !full;
  const names = [...new Set(t.steps.map((s) => s.label.replace(/（.*$/, "")))];

  return (
    <div className={`msg agent ag-${t.agent} ${active ? "running" : ""} ${asking ? "has-ask" : ""}`}>
      <i className="ava">{AVATAR[t.agent] ?? "A"}</i>
      <div className="col">
        <div className="meta">
          <b onClick={() => t.inst && onPick?.(t.inst)}>{who}</b>
          <time>{fmtTime(t.ts)}</time>
          {active && !t.asks.length ? (
            <span className="run-state run">
              <i className="spin" />
              {stage} {secs(now - t.ts)}
            </span>
          ) : asking ? (
            <span className="run-state ask">等你确认</span>
          ) : t.error ? (
            <span className="run-state bad">出错</span>
          ) : t.done && t.endTs && t.agent !== "strategy" && t.agent !== "watchdog" ? (
            <span className="run-state">完成 · 用时 {secs(t.endTs - t.ts)}</span>
          ) : null}
        </div>
        <div className="bub">
          {t.trigger && t.agent !== "strategy" && t.agent !== "watchdog" && <div className="trigger">{t.agent === "router" ? t.trigger : `被叫醒：${t.trigger}`}</div>}
          {t.steps.length > 0 && (
            <div className="run-steps">
              <button type="button" className="run-fold" onClick={() => setExpanded(!open)}>
                {open ? "▾" : "▸"} {t.steps.length} 步{open ? "" : `：${names.slice(0, 5).join("、")}${names.length > 5 ? "…" : ""}`}
              </button>
              {open && (
                <ol>
                  {t.steps.map((s) => (
                    <li key={s.id} className={`run-step ${stepState(s)} ${s.handoff ? "handoff" : ""}`}>
                      <button type="button" onClick={() => setOpenStep(openStep === s.id ? null : s.id)}>
                        <i className="dot" />
                        <b>{s.label.replace(/（.*$/, "")}</b>
                        <span>{s.why || (/（(.*)）$/.exec(s.label)?.[1] ?? "")}</span>
                      </button>
                      {openStep === s.id && (
                        <div className="step-detail">
                          {s.why && <p>为什么：{s.why}</p>}
                          {s.args && Object.keys(s.args).length > 0 && <p>参数：{JSON.stringify(s.args)}</p>}
                          {s.result ? <pre>{s.result}</pre> : <p>{active ? "还在跑…" : "没有记到返回"}</p>}
                        </div>
                      )}
                    </li>
                  ))}
                </ol>
              )}
            </div>
          )}
          {t.notes.map((n) => (
            <div key={n.id} className={`note ${n.kind}`}>
              {n.text}
            </div>
          ))}
          {live && (live.text || live.thinking) ? (
            <div className="body live">
              {live.thinking && !live.text && <p className="think">{live.thinking}</p>}
              {live.text && <Markdown text={live.text} />}
              <i className="caret" />
            </div>
          ) : (
            active && !t.asks.length && !runningStep && (
              <div className="run-wait">
                <i /><i /><i />
                <span>思考中</span>
              </div>
            )
          )}
          {renderAsk ? t.asks.map(renderAsk) : t.asks.length > 0 && !t.done && <div className="note answer">有一项操作在等你同意</div>}
          {(t.agent === "strategy" || t.agent === "watchdog") && t.trigger && <div className="body">{t.trigger}</div>}
          {t.body && (
            <div className={`body ${clamp ? "clamp" : ""}`}>
              <Markdown text={t.body} />
            </div>
          )}
          {brief && !active && long && (
            <button type="button" className="run-fold" onClick={() => setFull(!full)}>
              {full ? "收起" : "展开全文"}
            </button>
          )}
          {t.error && (
            <div className="note error">
              出错：{t.error}
              <ErrorDetails workId={t.errorEvent?.work_id ?? t.errorEvent?.data?.related_work_id} error={t.errorEvent?.data?.error} message={t.error} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
