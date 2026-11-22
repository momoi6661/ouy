import { useEffect, useState } from "react";
import { api, fmtTime, shortInst, type Lesson, type LessonList } from "../api";
import { Modal, toast } from "./ui";

const SOURCE = { review: "平仓后自动复盘", worker: "Worker 自己记的", user: "你加的" } as const;
type Draft = { id?: number; scope: "env" | "shared"; inst_id: string; text: string; grade: "observe" | "rule" };

/** 长期经验：每笔平仓后自动复盘写下的教训。模拟盘和实盘各一套，你定的规矩两边共用。能看、改、删、加。 */
export function MemoryPanel({ coins, onClose, onCount }: { coins: string[]; onClose: () => void; onCount: (n: number) => void }) {
  const [data, setData] = useState<LessonList | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (d: LessonList) => (setData(d), onCount(d.lessons.length + d.shared.length));
  useEffect(() => {
    api.lessons().then(set, (e) => toast(e.message, "error"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const run = async (job: () => Promise<LessonList>, ok: string) => {
    setBusy(true);
    try {
      set(await job());
      toast(ok, "ok");
      return true;
    } catch (e: any) {
      toast(e.message, "error");
      return false;
    } finally {
      setBusy(false);
    }
  };
  const envName = data?.env === "live" ? "实盘" : "模拟盘";
  const row = (r: Lesson, scope: "env" | "shared") => (
    <div key={`${scope}-${r.id}`} className={`pb mem ${r.stale ? "off" : ""}`}>
      <div className="pb-h">
        <span className={`tag ${r.grade === "rule" ? "ok" : ""}`}>{scope === "shared" ? "你定的规矩" : r.grade === "rule" ? "规矩" : "观察"}</span>
        <span className="muted small">{r.inst_id ? shortInst(r.inst_id) : "所有币"}</span>
        {scope === "env" && (
          <span className="muted small">
            印证 {r.confirms} 次 · 推翻 {r.refutes} 次
          </span>
        )}
        <span className="muted small">
          {SOURCE[r.source] ?? r.source} · {fmtTime(r.ts)}
        </span>
        {r.stale && <span className="pb-off">太久没用到，已不给 Worker 看</span>}
      </div>
      <div>{r.text}</div>
      {r.trade && (
        <div className="muted small">
          来自：{r.trade.side === "long" ? "多单" : "空单"} {r.trade.pnl >= 0 ? "+" : ""}
          {r.trade.pnl.toFixed(2)} U（{r.trade.pnl_pct >= 0 ? "+" : ""}
          {r.trade.pnl_pct.toFixed(1)}%），持仓 {r.trade.minutes} 分钟，{r.trade.how}
          {r.trade.playbook ? `，策略「${r.trade.playbook}」` : ""}
        </div>
      )}
      {r.note && <div className="muted small">{r.note}</div>}
      <div className="row">
        <button className="btn xs" onClick={() => setDraft({ id: r.id, scope, inst_id: r.inst_id, text: r.text, grade: r.grade })}>
          改
        </button>
        {scope === "env" && (
          <button className="btn xs" disabled={busy} onClick={() => run(() => api.saveLesson({ id: r.id, scope, inst_id: r.inst_id, text: r.text, grade: r.grade === "rule" ? "observe" : "rule" }), r.grade === "rule" ? "已降回观察" : "已升成规矩")}>
            {r.grade === "rule" ? "降回观察" : "升成规矩"}
          </button>
        )}
        <button className="btn danger xs" disabled={busy} onClick={() => window.confirm("删掉这条经验？") && run(() => api.deleteLesson(scope, r.id), "已删除")}>
          删
        </button>
      </div>
    </div>
  );
  return (
    <Modal title="长期经验" onClose={onClose} wide>
      <p className="muted small">
        每笔仓平掉后 Agent 会自动复盘，写一条经验。新经验先算「观察」；被后面的交易印证 3 次升成「规矩」，被推翻 2 次删掉，30 天没用到会降级。
        模拟盘和实盘的经验各存一套、互不相干；你定的规矩两边共用。
      </p>
      {!data ? (
        <p className="muted">加载中…</p>
      ) : (
        <div className="pb-list">
          <div className="pb-h">
            <b>你定的规矩（模拟盘和实盘共用）</b>
            <button className="btn xs" onClick={() => setDraft({ scope: "shared", inst_id: "", text: "", grade: "rule" })}>
              加一条
            </button>
          </div>
          {data.shared.length === 0 && <p className="muted small">还没有。比如「晚上 12 点后不开新仓」。</p>}
          {data.shared.map((r) => row(r, "shared"))}
          <div className="pb-h" style={{ marginTop: 8 }}>
            <b>
              {envName}的经验（{data.lessons.filter((r) => r.grade === "rule").length} 条规矩，{data.lessons.filter((r) => r.grade !== "rule").length} 条观察）
            </b>
            <button className="btn xs" onClick={() => setDraft({ scope: "env", inst_id: "", text: "", grade: "rule" })}>
              加一条
            </button>
          </div>
          {data.lessons.length === 0 && <p className="muted small">{envName}里还没有经验。平掉第一笔仓之后（盯盘开着时）这里会自动出现。</p>}
          {[...data.lessons].sort((a, b) => Number(b.grade === "rule") - Number(a.grade === "rule") || b.used_ts - a.used_ts).map((r) => row(r, "env"))}
        </div>
      )}
      {draft && (
        <div className="pb-form">
          <b>{draft.id ? "改这条经验" : draft.scope === "shared" ? "加一条规矩（两边共用）" : `加一条${envName}经验`}</b>
          <label className="field">
            <span>写成一句话：什么情况下，该怎么做 / 不该怎么做</span>
            <textarea className="input" rows={2} maxLength={300} value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} />
          </label>
          <div className="row">
            <label className="field">
              <span>对哪个币</span>
              <select className="input" value={draft.inst_id} onChange={(e) => setDraft({ ...draft, inst_id: e.target.value })}>
                <option value="">所有币</option>
                {coins.map((c) => (
                  <option key={c} value={c}>
                    {shortInst(c)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="row">
            <button className="btn primary sm" disabled={busy || draft.text.trim().length < 6} onClick={async () => (await run(() => api.saveLesson({ ...draft, text: draft.text.trim() }), "已保存")) && setDraft(null)}>
              保存
            </button>
            <button className="btn sm" onClick={() => setDraft(null)}>
              取消
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
