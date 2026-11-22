import { useState } from "react";
import { api, fmtPx, fmtTime, shortInst, MODE_LABEL, NO_PLAN, STATUS_LABEL, STYLES, type Account, type Style, type AgentConfig, type AgentStatus, type PendingItem } from "../api";
import { PlanExecution } from "./PlanExecution";
import { toast } from "./ui";

interface Props {
  inst: string;
  agent: AgentStatus;
  config: AgentConfig;
  pending: PendingItem[];
  account?: Account | null;
  onChanged: () => void;
  onToggleWatch: (inst: string) => void;
}

const BIAS = { long: "看多", short: "看空", neutral: "观望" } as const;

export function AgentPanel({ inst, agent, config, pending, account = null, onChanged, onToggleWatch }: Props) {
  const [busy, setBusy] = useState(false);
  const st = agent.instruments[inst];
  const watching = config.instruments.includes(inst);
  const report = st?.report;

  const review = async () => {
    setBusy(true);
    try {
      await api.review(inst);
      toast(`${shortInst(inst)} 复盘完成`, "ok");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  const decide = async (id: number, ok: boolean) => {
    try {
      if (ok) {
        const r = await api.approve(id);
        toast(r.detail?.unfilled ? r.message : `${STATUS_LABEL[r.status]}：${r.message}`, r.status === "executed" && !r.detail?.unfilled ? "ok" : "error");
      } else {
        await api.reject(id);
      }
    } catch (e: any) {
      toast(e.message, "error");
    }
    onChanged();
  };

  return (
    <div className="panel agent">
      <div className="panel-head">
        <h3>
          Agent 盯盘 · {shortInst(inst)}
          <span className={`dot ${agent.running && watching ? "live" : ""}`} />
        </h3>
        <span className="row">
          <select
            className="style-select"
            value={config.styles?.[inst] ?? config.style}
            title="这个币种的交易风格：决定 Agent 盯它时看的周期、复盘频率和止损宽度。只影响这一个币"
            onChange={async (e) => {
              try {
                await api.saveConfig({ ...config, styles: { ...(config.styles || {}), [inst]: e.target.value as Style } }, config.env === "live");
                toast(`${shortInst(inst)} 的交易风格已改为${STYLES.find((x) => x.id === e.target.value)?.label}`, "ok");
                onChanged();
              } catch (err: any) {
                toast(err.message, "error");
              }
            }}
          >
            {STYLES.map((x) => (
              <option key={x.id} value={x.id}>
                {x.label}
              </option>
            ))}
          </select>
          <span className="muted small">{MODE_LABEL[config.mode]}</span>
        </span>
      </div>

      {pending.length > 0 && (
        <div className="pending">
          {pending.map((p) => (
            <div key={p.id} className="pending-item">
              <div>
                <b>等你确认</b> {p.title}
                {p.action.reason && <div className="muted small">{p.action.reason}</div>}
              </div>
              <div className="row">
                <button className="btn sm primary" onClick={() => decide(p.id, true)}>
                  执行
                </button>
                <button className="btn sm" onClick={() => decide(p.id, false)}>
                  忽略
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {!watching ? (
        <div className="agent-empty">
          <p className="muted">这个品种还没交给 Agent。</p>
          <button className="btn" onClick={() => onToggleWatch(inst)}>
            加入盯盘列表
          </button>
        </div>
      ) : (
        <>
          <div className="agent-status">
            {st?.reviewing || busy ? (
              <span className="thinking">Agent 正在复盘…{st?.last_reason ? `（${st.last_reason}）` : ""}</span>
            ) : agent.running ? (
              <span className="muted small">{st?.next_review_in != null ? `约 ${st.next_review_in} 秒后例行复盘，价格异动会提前叫醒` : "盯盘中"}</span>
            ) : (
              <span className="muted small">Agent 未启动。点右上角「启动盯盘」后它会自己分析、自己下单。</span>
            )}
            <button className="btn sm" disabled={busy || st?.reviewing} onClick={review}>
              立即复盘
            </button>
          </div>
          {st?.error && <div className="alert error">{st.error}</div>}

          {report ? (
            <div className="report">
              <div className="report-top">
                <span className={`bias ${report.bias}`}>{BIAS[report.bias]}</span>
                <span className="conf">
                  <i style={{ width: `${Math.round(report.confidence * 100)}%` }} />
                </span>
                <span className="muted small">把握 {Math.round(report.confidence * 100)}%</span>
                <span className="muted small right">{fmtTime(report.ts)}</span>
              </div>
              <p className="summary">{report.summary}</p>

              {report.actions.length > 0 && (
                <ul className="acts">
                  {report.actions.map((a, i) => (
                    <li key={i} className={a.status}>
                      <b>{STATUS_LABEL[a.status] ?? a.status}</b> {a.message}
                    </li>
                  ))}
                </ul>
              )}

              {report.plan ? (
                <div className="plan">
                  <div className="plan-grid">
                    <span>方向</span>
                    <b className={report.plan.direction === "long" ? "up" : "down"}>
                      {report.plan.direction === "long" ? "做多" : "做空"} · {report.plan.leverage}x
                    </b>
                    <span>入场区间</span>
                    <b>
                      {fmtPx(report.plan.entry_low)} ~ {fmtPx(report.plan.entry_high)}
                    </b>
                    <span>止损</span>
                    <b className="down-fixed">{fmtPx(report.plan.stop_loss)}</b>
                    <span>止盈</span>
                    <b className="up-fixed">{report.plan.take_profits.map(fmtPx).join(" / ")}</b>
                  </div>
                  <dl>
                    <dt>证伪条件</dt>
                    <dd>{report.plan.invalidation}</dd>
                    {report.plan.add_condition && (
                      <>
                        <dt>加仓条件</dt>
                        <dd>{report.plan.add_condition}</dd>
                      </>
                    )}
                    {report.plan.exit_condition && (
                      <>
                        <dt>主动离场</dt>
                        <dd>{report.plan.exit_condition}</dd>
                      </>
                    )}
                  </dl>
                </div>
              ) : (
                <div className="muted small plan-none">
                  {report.no_plan_reason ? `这一轮没给计划：${NO_PLAN[report.no_plan_reason]}。` : "当前没有交易计划。"}
                </div>
              )}
              <PlanExecution inst={inst} report={report} account={account} onChanged={onChanged} />

              {report.watch_levels.length > 0 && (
                <div className="levels">
                  <h4>盯盘价位（到价立刻叫醒 Agent）</h4>
                  {report.watch_levels.map((l, i) => (
                    <div key={i} className="level">
                      <b>{fmtPx(l.price)}</b>
                      <span>{l.note}</span>
                    </div>
                  ))}
                </div>
              )}
              {report.knowledge.length > 0 && (
                <details className="kb-used">
                  <summary>本次参考了 {report.knowledge.length} 条知识库内容</summary>
                  <ul>
                    {report.knowledge.map((k, i) => (
                      <li key={i}>{k}</li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          ) : (
            <div className="muted small plan-none">还没有复盘结论。</div>
          )}
        </>
      )}
    </div>
  );
}
