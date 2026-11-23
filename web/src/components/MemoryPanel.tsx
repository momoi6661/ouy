import { useEffect, useState } from "react";
import {
  api,
  fmtPx,
  fmtTime,
  shortInst,
  type AgentConfig,
  type ContextInspect,
  type Lesson,
  type LessonList,
  type WorkerMemoryItem,
} from "../api";
import { Modal, toast } from "./ui";

const SOURCE = { review: "平仓后自动复盘", worker: "Worker 自己记的", user: "你加的" } as const;
type Draft = { id?: number; scope: "env" | "shared"; inst_id: string; text: string; grade: "observe" | "rule" };
type MemDraft = { id?: number; inst_id: string; note: string; pinned: boolean };
type TabId = "context" | "memory" | "lessons";

interface Props {
  coins: string[];
  config?: AgentConfig;
  defaultInst?: string;
  onClose: () => void;
  onCount: (n: number) => void;
  onChanged?: () => void;
}

/** 记忆与上下文中心：一处看清并管理「本轮喂给大模型的分段真实提示词」「各币目标·全局偏好·Worker工作笔记」「长期经验库（支持暂停不删）」 */
export function MemoryPanel({ coins, config, defaultInst, onClose, onCount, onChanged }: Props) {
  const [tab, setTab] = useState<TabId>("context");
  const [selInst, setSelInst] = useState<string>(defaultInst || coins[0] || "BTC-USDT-SWAP");
  const [ctx, setCtx] = useState<ContextInspect | null>(null);
  const [data, setData] = useState<LessonList | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [memDraft, setMemDraft] = useState<MemDraft | null>(null);
  const [newPref, setNewPref] = useState("");
  const [openBlocks, setOpenBlocks] = useState<Record<number, boolean>>({});
  const [busy, setBusy] = useState(false);

  const syncCount = (d: LessonList | null, c: ContextInspect | null) => {
    const lessonOn = d ? [...d.lessons, ...d.shared].filter((r) => r.enabled !== false && !r.stale).length : 0;
    const memOn = c ? Object.values(c.memories).flat().filter((m) => m.status === "active").length : 0;
    onCount(lessonOn + memOn);
  };

  const loadAll = async (inst = selInst) => {
    try {
      const [lRes, cRes] = await Promise.all([api.lessons(), api.agentContext(inst)]);
      setData(lRes);
      setCtx(cRes);
      syncCount(lRes, cRes);
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  useEffect(() => {
    loadAll(selInst);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selInst]);

  const runLesson = async (job: () => Promise<LessonList>, ok: string) => {
    setBusy(true);
    try {
      const next = await job();
      setData(next);
      const cRes = await api.agentContext(selInst);
      setCtx(cRes);
      syncCount(next, cRes);
      toast(ok, "ok");
      onChanged?.();
      return true;
    } catch (e: any) {
      toast(e.message, "error");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const runCtx = async (job: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    try {
      await job();
      const cRes = await api.agentContext(selInst);
      setCtx(cRes);
      syncCount(data, cRes);
      toast(ok, "ok");
      onChanged?.();
      return true;
    } catch (e: any) {
      toast(e.message, "error");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const savePrefs = async (nextPrefs: string[], msg: string) => {
    if (!config) return;
    await runCtx(() => api.saveConfig({ ...config, prefs: nextPrefs }, config.env === "live"), msg);
  };

  const envName = (data?.env ?? ctx?.env) === "live" ? "实盘" : "模拟盘";
  const allCoins = ctx?.coins?.length ? ctx.coins : coins;
  const curMemories: WorkerMemoryItem[] = ctx?.memories?.[selInst] ?? [];
  const promptStats = ctx?.prompt?.stats ?? {};
  const totalChars = promptStats.prompt_chars || 0;
  const stableChars = promptStats.stable_chars || 0;
  const cachePct = totalChars > 0 ? Math.round((stableChars / totalChars) * 100) : 0;
  const usage = ctx?.prompt?.usage;

  const lessonRow = (r: Lesson, scope: "env" | "shared") => {
    const enabled = r.enabled !== false;
    return (
      <div key={`${scope}-${r.id}`} className={`pb mem ${!enabled || r.stale ? "off" : ""}`}>
        <div className="pb-h">
          <span className={`tag ${enabled && r.grade === "rule" ? "ok" : ""}`}>
            {scope === "shared" ? "你定的规矩" : r.grade === "rule" ? "规矩" : "观察"}
          </span>
          <span className="muted small">{r.inst_id ? shortInst(r.inst_id) : "所有币"}</span>
          {scope === "env" && (
            <span className="muted small">
              印证 {r.confirms} 次 · 推翻 {r.refutes} 次
            </span>
          )}
          <span className="muted small">
            {SOURCE[r.source] ?? r.source} · {fmtTime(r.ts)}
          </span>
          {!enabled && <span className="pb-off">已暂停（保留记录，不喂给 Worker）</span>}
          {enabled && r.stale && <span className="pb-off">超 30 天未用，已归档不喂入</span>}
        </div>
        <div>{r.text}</div>
        {r.trade && (
          <div className="muted small">
            来自：{r.trade.side === "long" ? "多单" : "空单"} {r.trade.pnl >= 0 ? "+" : ""}
            {r.trade.pnl.toFixed(2)} U（{r.trade.pnl_pct >= 0 ? "+" : ""}
            {r.trade.pnl_pct.toFixed(1)}%），持仓 {r.trade.minutes} 分钟，{r.trade.how}
          </div>
        )}
        {r.note && <div className="muted small">{r.note}</div>}
        <div className="row">
          <button
            className="btn xs"
            disabled={busy}
            onClick={() =>
              runLesson(
                () => api.saveLesson({ id: r.id, scope, enabled: !enabled }),
                enabled ? "已暂停注入（未删除，随时可恢复）" : "已恢复启用",
              )
            }
          >
            {enabled ? "暂停" : "启用"}
          </button>
          <button className="btn xs" onClick={() => setDraft({ id: r.id, scope, inst_id: r.inst_id, text: r.text, grade: r.grade })}>
            改写
          </button>
          {scope === "env" && (
            <button
              className="btn xs"
              disabled={busy}
              onClick={() =>
                runLesson(
                  () => api.saveLesson({ id: r.id, scope, inst_id: r.inst_id, text: r.text, grade: r.grade === "rule" ? "observe" : "rule" }),
                  r.grade === "rule" ? "已降回观察" : "已升成规矩",
                )
              }
            >
              {r.grade === "rule" ? "降回观察" : "升成规矩"}
            </button>
          )}
          <button
            className="btn danger xs"
            disabled={busy}
            onClick={() => window.confirm("彻底删除这条经验？（若只是暂时不用，建议点「暂停」）") && runLesson(() => api.deleteLesson(scope, r.id), "已删除")}
          >
            删
          </button>
        </div>
      </div>
    );
  };

  return (
    <Modal title="Agent 记忆与上下文中心" onClose={onClose} wide>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 10, flexWrap: "wrap", gap: 8 }}>
        <div className="seg slim">
          <button className={tab === "context" ? "on" : ""} onClick={() => setTab("context")}>
            本轮上下文透视
          </button>
          <button className={tab === "memory" ? "on" : ""} onClick={() => setTab("memory")}>
            工作记忆 · 目标 · 偏好
          </button>
          <button className={tab === "lessons" ? "on" : ""} onClick={() => setTab("lessons")}>
            长期经验库{data ? ` (${data.shared.length + data.lessons.length})` : ""}
          </button>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <span className="muted small">查看品种：</span>
          <select className="input sm" style={{ width: 110 }} value={selInst} onChange={(e) => setSelInst(e.target.value)}>
            {allCoins.map((c) => (
              <option key={c} value={c}>
                {shortInst(c)}
              </option>
            ))}
          </select>
          <button className="btn xs" disabled={busy} onClick={() => loadAll(selInst)}>
            刷新
          </button>
        </div>
      </div>

      {tab === "context" && (
        <div className="pb-list">
          <p className="muted small">
            这里展示 <b>{shortInst(selInst)} Worker</b> {ctx?.prompt?.preview_only ? "即将喂入下一轮的静态记忆预览（尚未跑首轮复盘）" : "最近一轮真实发给大模型的完整分段上下文"}。
            排在前面的稳定层由模型服务自动命中提示缓存，报价和实时拐点排在最后保证鲜活。
          </p>
          <div className="pb" style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "center" }}>
            <span>
              总字数：<b>{totalChars.toLocaleString()}</b> 字
            </span>
            <span>
              稳定缓存段：<b>{stableChars.toLocaleString()}</b> 字（{cachePct}%）
            </span>
            {usage?.input_tokens ? (
              <span>
                最近一轮真实 Token：输入 <b>{usage.input_tokens.toLocaleString()}</b>
                {usage.cached_tokens ? `（命中缓存 ${usage.cached_tokens.toLocaleString()}，${Math.round((usage.cached_tokens / usage.input_tokens) * 100)}%）` : ""} · 输出{" "}
                <b>{(usage.output_tokens || 0).toLocaleString()}</b>
              </span>
            ) : null}
            {ctx?.prompt?.ts ? <span className="muted small">生成于 {fmtTime(ctx.prompt.ts)}</span> : null}
          </div>
          {(ctx?.prompt?.blocks ?? []).map((b) => {
            const expanded = !!openBlocks[b.index];
            return (
              <div key={b.index} className="pb">
                <div className="pb-h" style={{ cursor: "pointer" }} onClick={() => setOpenBlocks({ ...openBlocks, [b.index]: !expanded })}>
                  <span className={`tag ${b.cached ? "ok" : ""}`}>{b.cached ? `稳定缓存层 T${b.tier + 1}` : "每轮实时动态"}</span>
                  <b>
                    #{b.index} 【{b.title}】
                  </b>
                  <span className="muted small">{b.chars.toLocaleString()} 字</span>
                  <span className="link" style={{ marginLeft: "auto" }}>
                    {expanded ? "收起原文 ▴" : "展开原文 ▾"}
                  </span>
                </div>
                {!expanded ? (
                  <div className="muted small" style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                    {b.text.replace(/\s+/g, " ").slice(0, 140)}
                  </div>
                ) : (
                  <pre className="small" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 280, overflow: "auto", margin: "6px 0 0" }}>
                    {b.text}
                    {b.truncated ? "\n…（长 K 线 JSON 已截断显示前 4500 字）" : ""}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}

      {tab === "memory" && (
        <div className="pb-list">
          <div className="pb-h">
            <b>1. 全局用户偏好（Router 记下的长期要求，所有币生效）</b>
          </div>
          {(ctx?.prefs ?? []).length === 0 ? (
            <p className="muted small">当前没有全局偏好口令。</p>
          ) : (
            (ctx?.prefs ?? []).map((p, idx) => (
              <div key={idx} className="pb" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                <span>{p}</span>
                {config && (
                  <button
                    className="btn danger xs"
                    disabled={busy}
                    onClick={() => savePrefs((ctx?.prefs ?? []).filter((_, i) => i !== idx), "已移除该条偏好")}
                  >
                    移除
                  </button>
                )}
              </div>
            ))
          )}
          {config && (
            <div className="row" style={{ gap: 6 }}>
              <input
                className="input sm"
                style={{ flex: 1 }}
                placeholder="新增全局偏好（如：不做隔夜逆势单）"
                value={newPref}
                onChange={(e) => setNewPref(e.target.value)}
              />
              <button
                className="btn xs"
                disabled={busy || newPref.trim().length < 2}
                onClick={async () => {
                  await savePrefs([...(ctx?.prefs ?? []), newPref.trim()], "已添加偏好");
                  setNewPref("");
                }}
              >
                添加
              </button>
            </div>
          )}

          <div className="pb-h" style={{ marginTop: 12 }}>
            <b>2. 各币当前目标令（{envName}）</b>
          </div>
          {Object.keys(ctx?.goals ?? {}).length === 0 ? (
            <p className="muted small">当前没有单独指定的币种目标令，各 Worker 按默认风格与权限工作。</p>
          ) : (
            Object.entries(ctx?.goals ?? {}).map(([instKey, g]) => (
              <div key={instKey} className={`pb ${g.status !== "active" ? "off" : ""}`}>
                <div className="pb-h">
                  <span className={`tag ${g.status === "active" ? "ok" : ""}`}>{shortInst(instKey)}</span>
                  <span className="muted small">
                    状态：{g.status} · {fmtTime(g.ts)}
                    {g.max_loss_usdt ? ` · 亏损预算 ${g.max_loss_usdt}U` : ""}
                  </span>
                  <button
                    className="btn danger xs"
                    style={{ marginLeft: "auto" }}
                    disabled={busy}
                    onClick={() => window.confirm(`清除 ${shortInst(instKey)} 的目标令？`) && runCtx(() => api.clearGoal(instKey), "已清除该币目标")}
                  >
                    清除目标
                  </button>
                </div>
                <div>{g.goal}</div>
              </div>
            ))
          )}

          <div className="pb-h" style={{ marginTop: 12 }}>
            <b>
              3. {shortInst(selInst)} Worker 工作笔记（超 12 小时或币价偏离 &gt;5% 自动归档保留，不删库、不污染新盘面；重要规则可点「置顶常驻」）
            </b>
            <button className="btn xs" onClick={() => setMemDraft({ inst_id: selInst, note: "", pinned: true })}>
              加一条笔记
            </button>
          </div>
          {curMemories.length === 0 ? (
            <p className="muted small">{shortInst(selInst)} 暂无工作笔记。Worker 盘中调用 remember_note 记录的要点会出现在这里。</p>
          ) : (
            [...curMemories].reverse().map((m) => (
              <div key={m.id} className={`pb mem ${m.status !== "active" ? "off" : ""}`}>
                <div className="pb-h">
                  <span className={`tag ${m.status === "active" ? "ok" : ""}`}>
                    {m.pinned ? "置顶常驻" : m.status === "active" ? "生效中" : m.status === "paused" ? "已暂停" : "已归档"}
                  </span>
                  <span className="muted small">
                    {fmtTime(m.ts)}
                    {m.price ? ` · 记录时价格 ${fmtPx(m.price)}` : ""}
                  </span>
                  <span className="muted small">{m.status_reason}</span>
                </div>
                <div>{m.note}</div>
                <div className="row">
                  <button
                    className="btn xs"
                    disabled={busy}
                    onClick={() =>
                      runCtx(
                        () => api.saveWorkerMemory({ inst_id: selInst, id: m.id, enabled: m.enabled === false }),
                        m.enabled === false ? "已重新激活" : "已暂停（保留不删）",
                      )
                    }
                  >
                    {m.enabled === false ? "启用" : "暂停"}
                  </button>
                  <button
                    className="btn xs"
                    disabled={busy}
                    onClick={() =>
                      runCtx(
                        () => api.saveWorkerMemory({ inst_id: selInst, id: m.id, pinned: !m.pinned, enabled: true }),
                        m.pinned ? "已取消置顶（按 12h 生命周期自动归档）" : "已置顶常驻（不过期）",
                      )
                    }
                  >
                    {m.pinned ? "取消置顶" : "置顶常驻"}
                  </button>
                  <button className="btn xs" onClick={() => setMemDraft({ id: m.id, inst_id: selInst, note: m.note, pinned: !!m.pinned })}>
                    改写
                  </button>
                  <button
                    className="btn danger xs"
                    disabled={busy}
                    onClick={() => window.confirm("删除这条工作笔记？") && runCtx(() => api.deleteWorkerMemory(selInst, m.id), "已删除")}
                  >
                    删
                  </button>
                </div>
              </div>
            ))
          )}
          {memDraft && (
            <div className="pb-form">
              <b>{memDraft.id ? `改写 ${shortInst(memDraft.inst_id)} 工作笔记` : `给 ${shortInst(memDraft.inst_id)} 加一条工作笔记`}</b>
              <label className="field">
                <span>内容（简明写清当前品种要注意的结构或纪律）</span>
                <textarea className="input" rows={2} maxLength={300} value={memDraft.note} onChange={(e) => setMemDraft({ ...memDraft, note: e.target.value })} />
              </label>
              <label className="row small" style={{ gap: 6 }}>
                <input type="checkbox" checked={memDraft.pinned} onChange={(e) => setMemDraft({ ...memDraft, pinned: e.target.checked })} />
                <span>置顶常驻（不受 12 小时或 5% 价格波动归档限制）</span>
              </label>
              <div className="row">
                <button
                  className="btn primary sm"
                  disabled={busy || memDraft.note.trim().length < 2}
                  onClick={async () =>
                    (await runCtx(
                      () => api.saveWorkerMemory({ inst_id: memDraft.inst_id, id: memDraft.id, note: memDraft.note.trim(), pinned: memDraft.pinned, enabled: true }),
                      "已保存工作笔记",
                    )) && setMemDraft(null)
                  }
                >
                  保存
                </button>
                <button className="btn sm" onClick={() => setMemDraft(null)}>
                  取消
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {tab === "lessons" && (
        <div className="pb-list">
          <p className="muted small">
            平仓后自动提炼真实结构经验（保本微摩擦与纯缩手口号已自动过滤，未印证的零散观察最多带 4 条进上下文防刷屏）。
            不想清空记忆时直接点<b>「暂停」</b>即可保留存档而不影响 Agent 判断。
          </p>
          {!data ? (
            <p className="muted">加载中…</p>
          ) : (
            <>
              <div className="pb-h">
                <b>你定的规矩（模拟盘和实盘共用）</b>
                <button className="btn xs" onClick={() => setDraft({ scope: "shared", inst_id: "", text: "", grade: "rule" })}>
                  加一条
                </button>
              </div>
              {data.shared.length === 0 && <p className="muted small">还没有共用规矩。</p>}
              {data.shared.map((r) => lessonRow(r, "shared"))}
              <div className="pb-h" style={{ marginTop: 8 }}>
                <b>
                  {envName}沉淀的经验（{data.lessons.filter((r) => r.grade === "rule").length} 条规矩，{data.lessons.filter((r) => r.grade !== "rule").length} 条观察）
                </b>
                <button className="btn xs" onClick={() => setDraft({ scope: "env", inst_id: "", text: "", grade: "rule" })}>
                  加一条
                </button>
              </div>
              {data.lessons.length === 0 && <p className="muted small">{envName}目前没有经验条目。</p>}
              {[...data.lessons]
                .sort((a, b) => Number(b.enabled !== false) - Number(a.enabled !== false) || Number(b.grade === "rule") - Number(a.grade === "rule") || b.used_ts - a.used_ts)
                .map((r) => lessonRow(r, "env"))}
            </>
          )}
          {draft && (
            <div className="pb-form">
              <b>{draft.id ? "改写这条经验" : draft.scope === "shared" ? "加一条规矩（两边共用）" : `加一条${envName}经验`}</b>
              <label className="field">
                <span>写成一句话：什么盘面结构下，入场 / 止损 / 方向怎么选</span>
                <textarea className="input" rows={2} maxLength={300} value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} />
              </label>
              <div className="row">
                <label className="field">
                  <span>对哪个币</span>
                  <select className="input" value={draft.inst_id} onChange={(e) => setDraft({ ...draft, inst_id: e.target.value })}>
                    <option value="">所有币</option>
                    {allCoins.map((c) => (
                      <option key={c} value={c}>
                        {shortInst(c)}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="row">
                <button
                  className="btn primary sm"
                  disabled={busy || draft.text.trim().length < 6}
                  onClick={async () => (await runLesson(() => api.saveLesson({ ...draft, text: draft.text.trim() }), "已保存")) && setDraft(null)}
                >
                  保存
                </button>
                <button className="btn sm" onClick={() => setDraft(null)}>
                  取消
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}
