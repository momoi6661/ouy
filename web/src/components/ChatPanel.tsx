import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, EFFORTS, KIND_LABEL, fmtPx, fmtUsd, shortInst, shotUrl, STATUS_LABEL, type ActionResult, type ChatMessage, type Effort, type PlanPreview, type AgentConfig, type AgentStatus, type UiCommand } from "../api";
import { Markdown } from "./Markdown";
import { toast, useStored } from "./ui";
import { Composer } from "./Composer";
import { ErrorDetails } from "./ErrorDetails";

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
/** 工具调用折叠成一排小卡片，点开才看参数和返回 */
function StepChips({ steps }: { steps: { label: string; args?: Record<string, any>; result?: string; ok?: boolean }[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const x = open == null ? null : steps[open];
  return (
    <>
      <div className="chips">
        {steps.map((st, k) => (
          <button key={k} type="button" className={`chip ${open === k ? "on" : ""} ${st.ok === false ? "bad" : ""}`} onClick={() => setOpen(open === k ? null : k)}>
            {st.label}
          </button>
        ))}
      </div>
      {x && (
        <div className="step-detail">
          <b>{x.label}</b>
          {Object.keys(x.args || {}).length > 0 && <p>{JSON.stringify(x.args)}</p>}
          {x.result && <pre>{x.result}</pre>}
        </div>
      )}
    </>
  );
}

export function ChatPanel({ inst: chartInst, defaultEffort, shown, onShow, onActed, config, onUi, onUnread, agent, hidden }: Props) {
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
  const [acting, setActing] = useState<number | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const current = useRef(activeThread);
  current.current = activeThread;
  const readId = useRef(0);

  const load = async (thread = current.current) => {
    if (thread !== current.current) return;
    const id = ++readId.current;
    try {
      const r = await api.chatLog(thread);
      if (current.current === thread && readId.current === id) {
        setMsgs(r.messages);
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
  }, [msgs, busy, activeThread, hidden]);
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
    setMsgs((m) => [...m, { role: "user", text: message, to: target, images }]);
    setSending((n) => n + 1);
    try {
      const r = await api.chat(message, effort, thread, inst, target, false, images);
      if (r.steered) toast("它正在思考，这句话已直接送进它当前这一轮，回答随这一轮的结论发在这里", "ok");
      else await load(thread);
      if (r.ui?.length) {
        if (r.ui.some((c) => c.type === "select_inst" && c.inst_id !== chartInst)) pinned.current = true;
        onUi(r.ui);
      }
      onActed();
    } catch (e: any) {
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
  const samples = [`${name} 现在能做多还是做空？给我一个方案`, `帮我做多 ${name}，投 200 U`, "看一下我的持仓，要不要动", "把止损移到保本"];

  return (
    <div className="chat-dock" ref={root} hidden={hidden}>
      <div className="chat-thread">
        <b>{to === "router" ? "Router" : `${name} Worker`}</b>
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
        {msgs.length === 0 && !busy && (
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
        {msgs.map((m, i) => (
          <div key={i} className={`bubble ${m.role} ${m.proactive ? "proactive" : ""}`}>
            {m.role === "ai" && (
              <div className="bubble-from">
                <i className={`ava sm ${m.from ? "" : "router"}`}>{m.from ? "W" : "R"}</i>
                {m.from ? shortInst(m.from.replace(" Worker", "")) + " Worker" : "Router"}
              </div>
            )}
            {m.role === "user" && m.to === "worker" && <div className="bubble-from">直接对 Worker 说</div>}
            {m.steps && m.steps.length > 0 && <StepChips steps={m.steps} />}
            {m.role === "ai" ? <Markdown text={m.text} /> : <div className="bubble-text">{m.text}</div>}
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
          </div>
        ))}
        {busy && <div className="bubble ai thinking">Agent 正在看行情和账户…</div>}
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
          <label className="muted small" title="思考强度：越高想得越细，但越慢、越费 token">
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
