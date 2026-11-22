import { useState } from "react";
import { api, fmtPx, fmtTime, QUANT_BIASES, QUANT_MODES, shortInst, type InstStatus, type QuantBias, type QuantMode, type Rule, type WorkerOverride } from "../api";

/** 程序持续采样的量化指标：名字、显示名、算法定义 */
export const RECORD_DATA: [string, string, string][] = [
  ["turn_stage", "转折苗头", "+1~+3 看涨转折走到第几步，-1~-3 看跌；3 = 扫流动性后结构反转确认"],
  ["shift_failure", "转折位状态", "1：向上突破后失守；-1：向下突破后收回；0：未失守或已恢复；—：无有效观察"],
  ["score_long", "5m做多分", "0~4：顺 1H 方向、触关键线、收盘确认、放量"],
  ["score_short", "5m做空分", "0~4：顺 1H 方向、触关键线、收盘确认、放量"],
  ["sig_long", "快判多分", "1m 快判做多强度（变盘回踩 / 突破 / 假突破）"],
  ["sig_short", "快判空分", "1m 快判做空强度"],
  ["vol_ratio", "5m量比", "最近已收 5m 成交量 ÷ 前 20 根均值"],
  ["vol_1m", "1m量比", "最近已收 1m 成交量 ÷ 前 20 根均值"],
  ["chg_pct", "5m涨跌%", "最近已收 5m 开收涨跌幅"],
  ["move_1m", "1m涨跌%", "最近已收 1m 开收涨跌幅"],
  ["trend", "1H方向", "上涨 1 / 震荡 0 / 下跌 -1"],
  ["range_pos", "区间位置%", "0 = 下沿，100 = 上沿"],
  ["spread_pct", "点差%", "买一卖一价差占比"],
  ["book_bias", "盘口偏向", "前 20 档买卖量失衡（-100 全卖盘 ~ 100 全买盘）"],
];
export const dataLabel = (name: string) => (name.startsWith("dist:") ? `现价高出「${name.slice(5)}」%` : RECORD_DATA.find(([id]) => id === name)?.[1] ?? name);
const num = (v: number | null | undefined) => (v === null || v === undefined ? "—" : String(Math.round(v * 1000) / 1000));

/** 一个记录器某项数据现在是多少 */
export function recordNow(st: InstStatus | undefined, name: string, last?: number): number | null | undefined {
  const d = st?.data;
  if (!d || Date.now() - d.ts > 180_000) return undefined;
  if (name === "shift_failure" && Date.now() - (d.quote_ts ?? 0) > 15_000) return undefined;
  if (["sig_long", "sig_short", "move_1m", "vol_1m"].includes(name) && Date.now() - (d.fast_ts ?? 0) > 90_000) return undefined;
  if (name.startsWith("dist:")) {
    const line = d.levels[name.slice(5)];
    const px = last || d.values.px;
    return line && px ? (px / line - 1) * 100 : undefined;
  }
  return d.values[name];
}

interface Props {
  coin: string;
  st?: InstStatus;
  rules: Rule[];
  last?: number;
  busy: boolean;
  run: (fn: () => Promise<unknown>, ok: string) => void;
  own?: WorkerOverride;
  onSaveWorker?: (patch: Partial<WorkerOverride>, ok: string) => void;
}

/** 算法引擎与记录器面板：展示 4 档量化模式切换、白话走势诊断、实时结构转折、快判信号与条件触发规则 */
export function Recorders({ coin, st, rules, last, busy, run, own, onSaveWorker }: Props) {
  const data = st?.data;
  const shift = st?.fast?.shift;
  const turn = st?.turn;
  const fastLast = st?.fast?.last;
  const qt = st?.quant;
  const qmode: QuantMode = own?.quant_mode ?? qt?.mode ?? "auto";
  const qbias: QuantBias = own?.quant_bias ?? qt?.bias ?? "both";
  const rows = rules.filter((r) => r.recorder && ["waiting", "proposed"].includes(r.status));
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [form, setForm] = useState({ data: "vol_ratio", op: "above", level: "2", cooldown: "15", note: "" });
  const lines = Object.keys(data?.levels ?? {});

  return (
    <div className="ws-card wide rec">
      <h5>
        量化雷达与记录器 <span className="muted small">Worker 可随行情自动换挡，你也随时可手动切档</span>
      </h5>
      {onSaveWorker && (
        <div className="rec-modes">
          <div className="rec-mode-tabs" role="tablist" aria-label="量化算法档位">
            {QUANT_MODES.map((m) => (
              <button
                key={m.id}
                type="button"
                className={qmode === m.id ? "on" : ""}
                disabled={busy}
                title={m.desc}
                onClick={() => onSaveWorker({ quant_mode: m.id, quant_by: "你", quant_reason: `你手动切到「${m.label}」` }, `${shortInst(coin)} 量化雷达已切到「${m.label}」`)}
              >
                {m.label}
              </button>
            ))}
          </div>
          <select
            className="rec-bias"
            value={qbias}
            disabled={busy}
            title="方向锁：限制算法只盯某一边或双向都盯"
            onChange={(e) => onSaveWorker({ quant_bias: e.target.value as QuantBias, quant_by: "你" }, "量化方向锁已更新")}
          >
            {QUANT_BIASES.map((b) => (
              <option key={b.id} value={b.id}>
                {b.label}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="rec-explain">
        <div className="rec-desc">
          <b>{QUANT_MODES.find((x) => x.id === qmode)?.label}档</b>：{QUANT_MODES.find((x) => x.id === qmode)?.desc}
          {qt?.reason ? <span className="muted">（{qt.by || "Worker"}：{qt.reason}）</span> : null}
        </div>
        {qt?.narrative && <div className="rec-narrative">{qt.narrative}</div>}
        {!!st?.tracks?.length && <div className="rec-narrative">
          <b>持仓轨迹</b>
          {st.tracks.map((t) => (
            <div key={t.lot_id}>
              #{t.lot_id} {t.side === "long" ? "多" : "空"} 入场 {fmtPx(t.entry)}：浮盈最高 {num(t.peak)}R（{fmtPx(t.peak_px)}），最深 {num(t.low)}R，现在 {num(t.now)}R，
              {typeof t.locked === "number" ? `保盈线已执行（止损 ${fmtPx(t.locked)}）` : t.locked === "missed" ? "保盈线没来得及执行" : "保盈线未触发"}
            </div>
          ))}
        </div>}
        {qt?.opening_quote && <div className="rec-narrative">
          <b>开盘记录器 {fmtTime(qt.opening_quote.ts)}</b>　采样价 {fmtPx(qt.opening_quote.price)}
          <div>开盘前 1 小时 {num(qt.opening_quote.pre_open?.change_pct)}%　场开 {num(qt.opening_quote.session_open)}（{num(qt.opening_quote.session_pct)}%）</div>
          <div>当前 5 分钟开 {num(qt.opening_quote.bar_open)}（盘中 {num(qt.opening_quote.bar_pct)}%）</div>
        </div>}
      </div>
      {(turn || fastLast) && (
        <div className="rec-signals">
          {turn && (
            <div className={`rec-sig ${turn.side}`}>
              <span className="rec-badge">
                转折 · {turn.label}（{turn.side === "long" ? "看多" : "看空"}）
              </span>
              <span>
                {turn.text}
                {shift && turn.stage !== "early" && (
                  <>
                    （顺势回踩位 <b>{fmtPx(shift.level)}</b>，{shift.bars_ago} 根 K 线前）
                  </>
                )}
                　<span className="muted">{fmtTime(turn.ts)}</span>
              </span>
            </div>
          )}
          {fastLast && (
            <div className={`rec-sig ${fastLast.side}`}>
              <span className="rec-badge">{{ pending: "待确认", advised: "仅建议", executed: "已提交", failed: "提交失败", rejected: "已拒绝", verify: "需核实", skipped: "未通过", ignored: "已忽略" }[fastLast.status] ?? "快判"}</span>
              <span>
                {fastLast.text} {fastLast.result ? `— ${fastLast.result}` : ""}（{fmtTime(fastLast.ts)}）
              </span>
            </div>
          )}
        </div>
      )}
      <div className="rec-data">
        {data ? (
          RECORD_DATA.map(([id, label, help]) => (
            <span key={id} title={help}>
              {label} <b>{num(recordNow(st, id, last))}</b>
            </span>
          ))
        ) : (
          <span className="muted">暂无采样。开启盯盘且 Worker 未暂停时，基础走势观察不依赖快判。</span>
        )}
        {data ? <i>{fmtTime(data.ts)} 更新</i> : null}
      </div>
      {!!data?.log.some((r) => r.reading) && <details><summary>最近的走势判断（按 5 分钟 K 线）</summary>
        {data.log.filter((r) => r.reading).slice().reverse().map((r) => <p key={r.ts}><b>{fmtTime(r.ts)}</b> · {r.reading}</p>)}
      </details>}
      {rows.length === 0 && <p className="muted small rec-empty">未设指标触发规则；这不影响基础观察。</p>}
      {rows.map((r) => {
        const conds = r.conds ?? [];
        const drafts = conds.map((c, i) => edits[`${r.id}:${i}`] ?? String(c.level));
        const dirty = drafts.some((d, i) => d !== String(conds[i].level));
        const ok = drafts.every((d) => d.trim() !== "" && !Number.isNaN(Number(d)));
        return (
          <div key={r.id} className={`rec-row ${r.off ? "off" : ""}`}>
            <input type="checkbox" title="开 / 关这个记录器" checked={!r.off} disabled={busy} onChange={(e) => run(() => api.editStrategy(r.strategy_id!, { off: !e.target.checked }), e.target.checked ? "记录器已打开" : "记录器已关掉")} />
            <div className="rec-main">
              <div className="rec-flow">
                {conds.map((c, i) => (
                  <span key={i} className="rec-cond">
                    {i > 0 ? <i>且</i> : null}
                    {dataLabel(c.data)} {c.op === "above" ? "≥" : "≤"}
                    <input value={drafts[i]} inputMode="decimal" onChange={(e) => setEdits((m) => ({ ...m, [`${r.id}:${i}`]: e.target.value }))} />
                    <em>当前 {num(recordNow(st, c.data, last))}</em>
                  </span>
                ))}
                {dirty && (
                  <button
                    className="btn xs primary"
                    disabled={busy || !ok}
                    onClick={() => run(() => api.editStrategy(r.strategy_id!, { conds: conds.map((c, i) => ({ ...c, level: Number(drafts[i]) })) }).then(() => setEdits({})), "数值已改")}
                  >
                    保存
                  </button>
                )}
                <span className="trg-arrow">→</span>
                <span className="trg-do">{r.mode === "wake" ? r.do : `先${r.do}，再叫醒 Worker`}</span>
              </div>
              <div className="trg-meta">
                <span>#{r.strategy_id}</span>
                <span>{r.by === "默认" ? "系统预设" : `${r.by}设定`}</span>
                <span>间隔 {r.cooldown_minutes ?? 15} 分钟</span>
                <span>{r.runs ? `触发 ${r.runs} 次（上次 ${r.last_run ? fmtTime(r.last_run) : ""}）` : "尚未触发"}</span>
                {r.status === "proposed" ? <span>待同意启用</span> : null}
              </div>
            </div>
            <button className="btn xs" disabled={busy} onClick={() => window.confirm("删掉这个记录器？") && run(() => api.strategyAct(r.strategy_id!, "cancel"), "记录器已删掉")}>
              删除
            </button>
          </div>
        );
      })}
      <div className="rec-new">
        <span className="muted small">新增指标唤醒规则（需达标直接落单可让 Worker 用 set_recorder 设定）</span>
        <select value={form.data} onChange={(e) => setForm({ ...form, data: e.target.value })}>
          {RECORD_DATA.map(([id, label]) => (
            <option key={id} value={id}>{label}</option>
          ))}
          {lines.map((name) => (
            <option key={name} value={`dist:${name}`}>现价高出「{name}」%</option>
          ))}
        </select>
        <select value={form.op} onChange={(e) => setForm({ ...form, op: e.target.value })}>
          <option value="above">≥</option>
          <option value="below">≤</option>
        </select>
        <input value={form.level} inputMode="decimal" title="数值" onChange={(e) => setForm({ ...form, level: e.target.value })} />
        <button
          className="btn xs"
          disabled={busy || form.level.trim() === "" || Number.isNaN(Number(form.level))}
          onClick={() => run(() => api.addRecorder({ inst_id: coin, conds: [{ data: form.data, op: form.op as "above" | "below", level: Number(form.level) }], note: "", cooldown_minutes: 15 }), "记录器已加上")}
        >
          加上
        </button>
      </div>
    </div>
  );
}
