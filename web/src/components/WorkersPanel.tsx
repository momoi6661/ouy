import { useEffect, useState } from "react";
import { api, ENTRY_MODES, ENTRY_TYPE, fmtPx, fmtTime, NO_PLAN, shortInst, STANCES, STYLES, type Account, type AgentConfig, type AgentStatus, type Stance, type Style, type Wake, type WorkerOverride, type WorkItem } from "../api";
import { Empty, Pnl, toast, useInterval } from "./ui";

const BIAS = { long: "偏多", short: "偏空", neutral: "中性" } as const;
const VERDICT = { hold: "继续拿", tighten_stop: "收紧止损", adjust_tp: "调整止盈", reduce: "减仓", add: "加仓", close: "平仓" } as const;

interface Props {
  agent: AgentStatus;
  config: AgentConfig;
  account: Account | null;
  inst: string;
  onSelect: (inst: string) => void;
  onChanged: () => void;
}

/** Worker 总览：一行一个币的小组（交易 Worker + 情报 Worker），可以单独暂停、唤醒、换模型 */
export function WorkersPanel({ agent, config, account, inst, onSelect, onChanged }: Props) {
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState<string | null>(null);

  const save = async (id: string, change: Partial<WorkerOverride>, done: string) => {
    const cur: WorkerOverride = config.workers?.[id] ?? { model: "", paused: false };
    try {
      await api.saveConfig({ ...config, workers: { ...(config.workers ?? {}), [id]: { ...cur, ...change } } }, config.env === "live");
      toast(done, "ok");
      onChanged();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  const saveStyle = async (id: string, style: string) => {
    const { [id]: _drop, ...rest } = config.styles ?? {};
    try {
      await api.saveConfig({ ...config, styles: style ? { ...rest, [id]: style as Style } : rest }, config.env === "live");
      toast(`${shortInst(id)} 的交易风格已修改`, "ok");
      onChanged();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  const run = async (key: string, job: () => Promise<unknown>, done: string) => {
    setBusy(key);
    try {
      await job();
      toast(done, "ok");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy("");
      onChanged();
    }
  };

  const dog = agent.watchdog;
  return (
    <div className="panel workers">
      <div className="panel-head">
        <h3>
          Worker 总览
          <span className={`dot ${agent.running ? "live" : ""}`} />
        </h3>
        <span className="muted small" title="Watchdog 独立监控盯盘循环和卡住的 Worker，出问题自动恢复">
          {agent.running ? `Watchdog 正常${dog?.restarts ? `，已自动恢复 ${dog.restarts} 次` : ""}` : "盯盘未启动"}
        </span>
      </div>
      {dog?.last && <div className="muted small wk-dog">{dog.last}</div>}
      {config.instruments.length === 0 && <Empty>盯盘列表是空的。在行情列表里点星标，把币交给 Agent。</Empty>}
      {config.instruments.map((id) => {
        const st = agent.instruments[id];
        const pos = account?.positions.find((p) => p.inst_id === id);
        const report = st?.report;
        const plan = report?.plan;
        const state = st?.paused ? "已暂停" : st?.reviewing ? "复盘中" : pos ? "持仓中" : plan ? "等入场" : agent.running ? "观望" : "未启动";
        const cls = st?.paused ? "paused" : st?.reviewing ? "busy" : pos ? "holding" : plan ? "waiting" : "";
        const brief = st?.intel;
        const expanded = open === id;
        return (
          <div key={id} className={`wk ${id === inst ? "current" : ""}`}>
            <div className="wk-row" onClick={() => onSelect(id)}>
              <b className="wk-name">{shortInst(id)}</b>
              <span className={`wk-state ${cls}`}>{state}</span>
              <span className="muted small">{STYLES.find((x) => x.id === st?.style)?.label}</span>
              {st?.illiquid && (
                <span className="tag warn" title={`这个币在模拟盘里${st.illiquid}，和真实市场差得远，在这里测不出真实效果。Agent 在它上面每笔风险已压到四分之一。`}>
                  模拟盘测不准
                </span>
              )}
              <span className="wk-px">{fmtPx(st?.last || undefined)}</span>
              <span className="wk-pnl">{pos ? <Pnl value={pos.upl} /> : <span className="muted">—</span>}</span>
              <button
                className="link"
                onClick={(e) => {
                  e.stopPropagation();
                  setOpen(expanded ? null : id);
                }}
              >
                {expanded ? "收起" : "展开"}
              </button>
            </div>
            <div className="wk-line small">
              {plan ? (
                <span>
                  计划{plan.direction === "long" ? "做多" : "做空"}（{ENTRY_TYPE[plan.entry_type ?? "pullback"]}） {fmtPx(plan.entry_low)} ~ {fmtPx(plan.entry_high)}，止损 {fmtPx(plan.stop_loss)}
                </span>
              ) : report ? (
                <span className="muted">{report.summary.slice(0, 60)}</span>
              ) : (
                <span className="muted">还没复盘过</span>
              )}
            </div>
            {!plan && report?.no_plan_reason && <div className="wk-line small muted">这一轮没给计划：{NO_PLAN[report.no_plan_reason]}</div>}
            {report?.plan_order?.text && <div className={`wk-line small plan-order ${report.plan_order.status}`}>{report.plan_order.text}</div>}
            {expanded && (
              <div className="wk-detail">
                <div className="wk-ctl">
                  <button className="btn xs" disabled={busy === `r${id}` || st?.reviewing} onClick={() => run(`r${id}`, () => api.review(id), `${shortInst(id)} 复盘完成`)}>
                    {busy === `r${id}` ? "复盘中…" : "立即唤醒"}
                  </button>
                  <button className="btn xs" onClick={() => save(id, { paused: !st?.paused }, st?.paused ? `${shortInst(id)} 的 Worker 已恢复` : `${shortInst(id)} 的 Worker 已暂停`)}>
                    {st?.paused ? "恢复" : "暂停"}
                  </button>
                  <select
                    className="style-select"
                    value={config.workers?.[id]?.wake ?? ""}
                    title="这个 Worker 的唤醒方式"
                    onChange={(e) => save(id, { wake: (e.target.value || null) as Wake | null }, "唤醒方式已修改")}
                  >
                    <option value="">跟随全局（{config.wake === "always" ? "一直唤醒" : "有事才唤醒"}）</option>
                    <option value="event">有事才唤醒</option>
                    <option value="always">一直唤醒</option>
                  </select>
                </div>
                <div className="wk-ctl">
                  <select className="style-select" value={config.styles?.[id] ?? ""} title="这个币的交易风格" onChange={(e) => saveStyle(id, e.target.value)}>
                    <option value="">风格跟全局（{STYLES.find((x) => x.id === config.style)?.label}）</option>
                    {STYLES.map((x) => (
                      <option key={x.id} value={x.id}>
                        {x.label}
                      </option>
                    ))}
                  </select>
                  <select
                    className="style-select"
                    value={config.workers?.[id]?.stance ?? ""}
                    title="这个 Worker 的出手尺度：也决定程序核对计划时净盈亏比的下限"
                    onChange={(e) => save(id, { stance: (e.target.value || null) as Stance | null }, "出手尺度已修改")}
                  >
                    <option value="">出手跟全局（{STANCES.find((x) => x.id === config.stance)?.label}）</option>
                    {STANCES.map((x) => (
                      <option key={x.id} value={x.id}>
                        {x.label}
                      </option>
                    ))}
                  </select>
                  <select
                    className="style-select"
                    value={config.workers?.[id]?.entry_mode ?? ""}
                    title="这个 Worker 的入场方式"
                    onChange={(e) => save(id, { entry_mode: (e.target.value || null) as WorkerOverride["entry_mode"] }, "入场方式已修改")}
                  >
                    <option value="">入场跟全局（{ENTRY_MODES.find((x) => x.id === config.entry_mode)?.label}）</option>
                    {ENTRY_MODES.map((x) => (
                      <option key={x.id} value={x.id}>
                        {x.label}
                      </option>
                    ))}
                  </select>
                </div>
                <label className="wk-model">
                  <span className="muted small">模型</span>
                  <input
                    className="input sm"
                    defaultValue={config.workers?.[id]?.model ?? ""}
                    placeholder={`跟随全局（${st?.model || "默认"}）`}
                    onBlur={(e) => {
                      const v = e.target.value.trim();
                      if (v !== (config.workers?.[id]?.model ?? "")) save(id, { model: v }, v ? `${shortInst(id)} 的 Worker 改用 ${v}` : "已改回全局模型");
                    }}
                  />
                </label>
                {st?.paused && <div className="muted small">暂停期间它不会被唤醒，也不会下单；紧急平仓线仍然有效。</div>}
                {st?.illiquid && (
                  <div className="src-note">
                    这个币在模拟盘里{st.illiquid}，成交价和真实市场差得远，测不出真实效果。Agent 在它上面只挂限价单，每笔风险压到上限的四分之一。
                  </div>
                )}
                {!!st?.fails && <div className="muted small">已连续 {st.fails} 次复盘失败，重试间隔会自动拉长：{st.error}</div>}
                {!!st?.skips && (
                  <div className="muted small">
                    Router 已过滤掉 {st.skips} 次不值得唤醒的事件{st.last_gate ? `，最近一次：${st.last_gate}` : ""}
                  </div>
                )}
                {!!report?.lot_verdicts?.length && (
                  <div className="wk-block">
                    <div className="wk-title">对每一笔仓位的结论 · {fmtTime(report.ts)}</div>
                    {report.lot_verdicts.map((v) => (
                      <div key={v.lot_id} className="small">
                        第 {v.lot_id} 笔：<b className={`verdict ${v.verdict}`}>{VERDICT[v.verdict]}</b>，{v.reason}
                      </div>
                    ))}
                  </div>
                )}
                <div className="wk-block">
                  <div className="wk-title">
                    情报 Worker（不下单，只搜集信息）
                    <button className="link" disabled={busy === `i${id}` || st?.intel_running} onClick={() => run(`i${id}`, () => api.intel(id), `${shortInst(id)} 的情报已更新`)}>
                      {busy === `i${id}` || st?.intel_running ? "正在查…" : "现在去查"}
                    </button>
                  </div>
                  {brief ? (
                    <>
                      <div className="small">
                        <b className={`bias ${brief.bias}`}>{BIAS[brief.bias]}</b> {brief.summary} <span className="muted">{fmtTime(brief.ts)}</span>
                      </div>
                      {brief.points.map((p, i) => (
                        <div key={i} className="small muted">
                          · {p}
                        </div>
                      ))}
                      {brief.unavailable.length > 0 && <div className="small wk-missing">没查到：{brief.unavailable.join("；")}</div>}
                    </>
                  ) : (
                    <div className="muted small">还没有简报。启动盯盘后会按设置里的间隔自动更新。</div>
                  )}
                </div>
                {!!st?.memory?.length && (
                  <div className="wk-block">
                    <div className="wk-title">这个 Worker 的工作记忆</div>
                    {st.memory.map((m, i) => (
                      <div key={i} className="small muted">
                        {fmtTime(m.ts)} {m.note}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

const SOURCE: Record<string, string> = { event: "事件", user: "你交办的", intel: "情报", timer: "定时", manual: "手动" };
const STATUS: Record<WorkItem["status"], string> = { queued: "排队", running: "进行中", waiting: "等你同意", done: "完成", failed: "失败", skipped: "已过滤" };

/** 任务面板：每次唤醒 Worker、每个被 Router 过滤掉的事件、每个交办的任务都是一条 */
export function TasksPanel({ onSelect }: { onSelect: (inst: string) => void }) {
  const [items, setItems] = useState<WorkItem[]>([]);
  const [hideSkipped, setHideSkipped] = useState(true);
  const load = () => {
    api.work(80).then(setItems, () => undefined);
  };
  useEffect(() => {
    load();
    window.addEventListener("ouy:work", load);
    return () => window.removeEventListener("ouy:work", load);
  }, []);
  useInterval(load, 15000);

  const rank = (w: WorkItem) => (w.status === "failed" ? 0 : w.status === "running" || w.status === "queued" || w.status === "waiting" ? 1 : 2);
  const rows = items.filter((w) => !hideSkipped || w.status !== "skipped").sort((a, b) => rank(a) - rank(b) || b.id - a.id);
  return (
    <div className="panel tasks">
      <div className="panel-head">
        <h3>任务</h3>
        <label className="check small">
          <input type="checkbox" checked={hideSkipped} onChange={(e) => setHideSkipped(e.target.checked)} />
          不看被过滤掉的事件
        </label>
      </div>
      {rows.length === 0 && <Empty>还没有任务。启动盯盘后，每次唤醒 Worker 都会在这里留下一条。</Empty>}
      {rows.map((w) => (
        <div key={w.id} className={`task ${w.status}`} onClick={() => w.inst_id && onSelect(w.inst_id)}>
          <div className="task-top">
            <span className={`task-status ${w.status}`}>{STATUS[w.status]}</span>
            <b>{w.inst_id ? shortInst(w.inst_id) : "全局"}</b>
            <span className="muted small">{SOURCE[w.source] ?? w.source}</span>
            <span className="muted small right">
              {fmtTime(w.ts)}
              {w.model ? ` · ${w.model}` : ""}
            </span>
          </div>
          <div className="small">{w.goal}</div>
          {w.result && <div className="small muted">{w.result}</div>}
        </div>
      ))}
    </div>
  );
}
