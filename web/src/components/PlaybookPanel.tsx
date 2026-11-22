import { useEffect, useState } from "react";
import { api, type Playbook } from "../api";
import { Modal, toast } from "./ui";

const BASES: { id: Playbook["base"]; label: string }[] = [
  { id: "pullback", label: "回踩进场" },
  { id: "breakout", label: "突破追单" },
  { id: "range", label: "区间两头" },
  { id: "manage", label: "移动止损 / 分批止盈" },
  { id: "martingale", label: "翻倍补仓" },
  { id: "grid", label: "网格" },
];
const DEFAULT_LABEL: Record<string, string> = {
  leverage: "杠杆", risk_pct: "每笔风险 %", margin_usdt: "保证金 U", expires_minutes: "多久没触发作废（分钟）", breakeven_r: "到几 R 移保本",
  partial_r: "到几 R 分批止盈", partial_fraction: "分批平掉的比例", trail_pct: "跟踪止损回撤 %", max_hold_minutes: "最长持仓（分钟）", max_rounds: "最多做几轮",
  base_margin: "第一层保证金 U", step_pct: "每逆向多少 % 补一层", multiplier: "补仓倍数", max_layers: "最多几层", take_profit_pct: "整组止盈 %", grids: "格数", margin_per_grid: "每格保证金 U",
};
const EMPTY = { name: "", base: "pullback" as Playbook["base"], when: "", entry: "", exit: "", breakeven_r: "", trail_pct: "", max_hold_minutes: "" };

/** 策略库：Agent 和你自己写的、可以反复用的策略。能看每条的规则和战绩，能加、停用、删。 */
export function PlaybookPanel({ env, onClose, onCount }: { env: "demo" | "live"; onClose: () => void; onCount: (n: number) => void }) {
  const [rows, setRows] = useState<Playbook[] | null>(null);
  const [form, setForm] = useState<typeof EMPTY | null>(null);
  const [busy, setBusy] = useState("");
  const set = (list: Playbook[]) => (setRows(list), onCount(list.length));
  useEffect(() => {
    api.playbooks().then(set, (e) => toast(e.message, "error"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const run = async (key: string, fn: () => Promise<Playbook[]>, ok: string) => {
    setBusy(key);
    try {
      set(await fn());
      toast(ok, "ok");
      return true;
    } catch (e: any) {
      toast(e.message, "error");
      return false;
    } finally {
      setBusy("");
    }
  };
  const save = async () => {
    if (!form) return;
    const defaults: Record<string, number> = {};
    for (const k of ["breakeven_r", "trail_pct", "max_hold_minutes"] as const) if (form[k] !== "" && Number(form[k]) > 0) defaults[k] = Number(form[k]);
    if (await run("save", () => api.savePlaybook({ name: form.name.trim(), base: form.base, when: form.when, entry: form.entry, exit: form.exit, defaults }), `「${form.name.trim()}」已存进策略库`)) setForm(null);
  };
  const where = env === "demo" ? "模拟盘" : "实盘";
  return (
    <Modal title="策略库" onClose={onClose} wide>
      <p className="muted small">
        这里是 Agent 和你写下来的、可以反复用的策略。Worker 觉得行情符合哪一条，就按它的规则布置；每条做完的单会记进战绩（{where}和{env === "demo" ? "实盘" : "模拟盘"}分开记）。
        做够 5 笔、胜率不到三成而且亏钱的会自动停用，改写后重新启用。
      </p>
      {rows === null ? (
        <div className="muted small">加载中…</div>
      ) : rows.length === 0 && !form ? (
        <div className="muted small">还没有策略。Worker 验证过有效的做法会自己写进来，你也可以先加一条。</div>
      ) : (
        <div className="pb-list">
          {rows.map((p) => (
            <div key={p.name} className={`pb ${p.enabled ? "" : "off"}`}>
              <div className="pb-h">
                <b>{p.name}</b>
                <span className="tag">{p.base_label}</span>
                <span className="muted small">
                  第 {p.version} 版 · {p.author} 写的{p.updated_by && p.updated_by !== p.author ? `，${p.updated_by} 改过` : ""}
                </span>
                <span className="pb-score">
                  {where}战绩：{p.score}
                </span>
              </div>
              {!p.enabled && <div className="pb-off">已停用：{p.off_reason || "手动停用"}</div>}
              <dl>
                <dt>什么行情用</dt>
                <dd>{p.when}</dd>
                {p.entry && (
                  <>
                    <dt>触发价怎么定</dt>
                    <dd>{p.entry}</dd>
                  </>
                )}
                <dt>止损止盈</dt>
                <dd>{p.exit}</dd>
                {Object.keys(p.defaults).length > 0 && (
                  <>
                    <dt>默认参数</dt>
                    <dd>
                      {Object.entries(p.defaults)
                        .map(([k, v]) => `${DEFAULT_LABEL[k] ?? k} ${v}`)
                        .join("，")}
                    </dd>
                  </>
                )}
              </dl>
              <div className="row">
                <button className="btn xs" disabled={!!busy} onClick={() => run(`e${p.name}`, () => api.enablePlaybook(p.name, !p.enabled), p.enabled ? `「${p.name}」已停用` : `「${p.name}」已启用`)}>
                  {p.enabled ? "停用" : "启用"}
                </button>
                <button
                  className="btn xs"
                  disabled={!!busy}
                  onClick={() =>
                    setForm({
                      name: p.name, base: p.base, when: p.when, entry: p.entry, exit: p.exit,
                      breakeven_r: String(p.defaults.breakeven_r ?? ""), trail_pct: String(p.defaults.trail_pct ?? ""), max_hold_minutes: String(p.defaults.max_hold_minutes ?? ""),
                    })
                  }
                >
                  改
                </button>
                <button className="btn xs danger" disabled={!!busy} onClick={() => window.confirm(`从策略库里删掉「${p.name}」？它的战绩也会一起删掉。`) && run(`d${p.name}`, () => api.deletePlaybook(p.name), `「${p.name}」已删除`)}>
                  删除
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      {form ? (
        <div className="pb-form">
          <div className="row">
            <label className="field">
              <span>名字（同名就是改写那一条）</span>
              <input className="input" value={form.name} maxLength={24} placeholder="比如：缩量回踩均线多" onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </label>
            <label className="field">
              <span>用哪种方式执行</span>
              <select className="input" value={form.base} onChange={(e) => setForm({ ...form, base: e.target.value as Playbook["base"] })}>
                {BASES.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label className="field">
            <span>什么行情用、什么行情不能用</span>
            <textarea className="input" rows={2} value={form.when} onChange={(e) => setForm({ ...form, when: e.target.value })} />
          </label>
          <label className="field">
            <span>触发价怎么定</span>
            <textarea className="input" rows={2} value={form.entry} onChange={(e) => setForm({ ...form, entry: e.target.value })} />
          </label>
          <label className="field">
            <span>止损、止盈、多久没走出来离场</span>
            <textarea className="input" rows={2} value={form.exit} onChange={(e) => setForm({ ...form, exit: e.target.value })} />
          </label>
          <div className="row">
            {(["breakeven_r", "trail_pct", "max_hold_minutes"] as const).map((k) => (
              <label className="field" key={k}>
                <span>{DEFAULT_LABEL[k]}（可不填）</span>
                <input className="input" type="number" min={0} step="any" value={form[k]} onChange={(e) => setForm({ ...form, [k]: e.target.value })} />
              </label>
            ))}
          </div>
          <div className="modal-foot">
            <button className="btn" onClick={() => setForm(null)}>
              取消
            </button>
            <button className="btn primary" disabled={!!busy || !form.name.trim() || !form.when.trim() || !form.exit.trim()} onClick={save}>
              {busy === "save" ? "保存中…" : "存进策略库"}
            </button>
          </div>
        </div>
      ) : (
        <div className="modal-foot">
          <button className="btn primary" onClick={() => setForm({ ...EMPTY })}>
            + 加一条策略
          </button>
        </div>
      )}
    </Modal>
  );
}
