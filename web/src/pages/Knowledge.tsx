import { useEffect, useState } from "react";
import { api, type KbDocItem, type KbStats } from "../api";
import { Markdown } from "../components/Markdown";
import { StyleGuide } from "../components/StyleGuide";
import { Empty, toast } from "../components/ui";

type Hit = Awaited<ReturnType<typeof api.kbSearch>>[number];

const CAT_LABEL: Record<string, { label: string; desc: string }> = {
  core: { label: "核心铁律（每轮固定注入 Agent）", desc: "Agent 任何时候都必须遵守的纪律与计划模板" },
  strategy: { label: "实战策略库（Agent 按盘面选用）", desc: "22 套完整交易打法，目录常驻喂给 Agent，需要时调全文" },
  guide: { label: "盘面与风控基础（按行情自动检索）", desc: "K 线形态、指标读法、大盘联动、杠杆仓位与持仓情景手册" },
};

export function Knowledge({ kb, onChanged }: { kb: KbStats; onChanged?: () => void }) {
  const [sel, setSel] = useState<string>(kb.documents[0]?.source ?? "core/discipline.md");
  const [doc, setDoc] = useState<{ source: string; title: string; content: string } | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [filter, setFilter] = useState("");

  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!sel) return;
    let alive = true;
    setEditing(false);
    api
      .kbDoc(sel)
      .then((r) => {
        if (alive) {
          setDoc(r);
          setDraft(r.content);
        }
      })
      .catch((e: any) => toast(e.message, "error"));
    return () => {
      alive = false;
    };
  }, [sel]);

  const search = async (text = q) => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      setHits(await api.kbSearch(text));
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!doc) return;
    setSaving(true);
    try {
      const saved = await api.kbSave(doc.source, draft);
      setDoc(saved);
      setEditing(false);
      toast("已保存并重新索引，Agent 下一轮即生效", "ok");
      onChanged?.();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setSaving(false);
    }
  };

  const createNew = async () => {
    const name = window.prompt("输入新策略或笔记的文件名（英文，存进 strategies/ 目录，例如 my_breakout）：");
    if (!name?.trim()) return;
    const slug = name.trim().replace(/\.md$/i, "").replace(/[^a-zA-Z0-9_\-/]/g, "_");
    const source = slug.includes("/") ? `${slug}.md` : `strategies/${slug}.md`;
    const tpl = `# 策略：自定义战法\n\n## 适用行情\n在这里写清楚什么行情用、什么行情不用。\n\n## 入场条件\n1. 入场位置与触发条件\n\n## 止损\n止损放在哪个结构位之外。\n\n## 止盈\n分批止盈目标怎么定。\n`;
    try {
      const created = await api.kbSave(source, tpl);
      onChanged?.();
      setSel(created.source);
      setDoc(created);
      setDraft(created.content);
      setEditing(true);
      toast(`已创建 ${created.source}`, "ok");
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const remove = async () => {
    if (!doc || !window.confirm(`确认删除《${doc.title}》（${doc.source}）吗？`)) return;
    try {
      await api.kbDelete(doc.source);
      toast("文档已删除", "ok");
      onChanged?.();
      const next = kb.documents.find((d) => d.source !== doc.source);
      if (next) setSel(next.source);
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const catOf = (d: KbDocItem) => d.category ?? (d.source.startsWith("core/") ? "core" : d.source.startsWith("strategies/") ? "strategy" : "guide");
  const filtered = kb.documents.filter((d) => !filter.trim() || d.title.toLowerCase().includes(filter.toLowerCase()) || d.source.toLowerCase().includes(filter.toLowerCase()));
  const samples = ["前高前低 假跌破收回怎么抄底", "变盘后回踩怎么挂单", "浮盈 1R 之后止损怎么移", "资金费率极端还能追吗"];

  return (
    <div className="page kb-layout">
      <aside className="panel kb-sidebar">
        <div className="panel-head">
          <h3>交易知识库</h3>
          <button className="btn xs primary" onClick={createNew}>
            新建文档
          </button>
        </div>
        <div className="muted small kb-meta">
          共 {kb.documents.length} 篇 / {kb.chunks} 个检索片段（{kb.retrieval}）
        </div>
        {kb.embedding_error && <div className="alert error">向量模型未就绪，已退回关键词检索：{kb.embedding_error}</div>}
        <input className="input sm" placeholder="筛选文档标题或文件名…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <div className="kb-groups">
          {(["core", "strategy", "guide"] as const).map((cat) => {
            const list = filtered.filter((d) => catOf(d) === cat);
            if (!list.length) return null;
            return (
              <div key={cat} className="kb-group">
                <div className="kb-group-title" title={CAT_LABEL[cat].desc}>
                  {CAT_LABEL[cat].label}
                </div>
                {list.map((d) => (
                  <button key={d.source} type="button" className={`kb-nav-item ${sel === d.source ? "on" : ""}`} onClick={() => setSel(d.source)}>
                    <span>
                      {d.title}
                      {d.score && <small className="kb-score" title="程序按每一笔平仓的已实现盈亏自动记的战绩">{d.score}</small>}
                    </span>
                    <em>{d.chunks} 段</em>
                  </button>
                ))}
              </div>
            );
          })}
        </div>
      </aside>

      <div className="kb-main">
        <StyleGuide />
        <div className="panel kb-reader">
          {doc ? (
            <>
              <div className="panel-head">
                <div>
                  <h3>{doc.title}</h3>
                  <span className="muted small">{doc.source}</span>
                </div>
                <div className="row">
                  {editing ? (
                    <>
                      <button className="btn xs primary" disabled={saving} onClick={save}>
                        {saving ? "保存中…" : "保存并生效"}
                      </button>
                      <button
                        className="btn xs"
                        disabled={saving}
                        onClick={() => {
                          setDraft(doc.content);
                          setEditing(false);
                        }}
                      >
                        取消
                      </button>
                    </>
                  ) : (
                    <>
                      <button className="btn xs" onClick={() => setEditing(true)}>
                        编辑原文
                      </button>
                      {doc.source !== "core/discipline.md" && (
                        <button className="btn xs" onClick={remove}>
                          删除
                        </button>
                      )}
                    </>
                  )}
                </div>
              </div>
              {editing ? (
                <textarea className="kb-editor" value={draft} onChange={(e) => setDraft(e.target.value)} spellCheck={false} />
              ) : (
                <div className="kb-article">
                  <Markdown text={doc.content} />
                </div>
              )}
            </>
          ) : (
            <Empty>选择左侧一篇文档查看完整交易手册</Empty>
          )}
        </div>

        <div className="panel">
          <div className="panel-head">
            <h3>检索测试（模拟 Agent 复盘前自动查到的片段）</h3>
          </div>
          <form
            className="row"
            onSubmit={(e) => {
              e.preventDefault();
              search();
            }}
          >
            <input className="input" placeholder="输入盘面情况，看 Agent 会翻到哪几段规则…" value={q} onChange={(e) => setQ(e.target.value)} />
            <button className="btn primary" disabled={busy || !q.trim()}>
              {busy ? "检索中…" : "检索"}
            </button>
          </form>
          <div className="chips">
            {samples.map((s) => (
              <button
                key={s}
                type="button"
                className="chip"
                onClick={() => {
                  setQ(s);
                  search(s);
                }}
              >
                {s}
              </button>
            ))}
          </div>
          {hits && (
            <div className="kb-hits">
              {hits.length === 0 ? (
                <Empty>没有匹配的片段</Empty>
              ) : (
                hits.map((h) => (
                  <div key={h.id} className="kb-hit" onClick={() => setSel(h.source)} style={{ cursor: "pointer" }} title="点击打开所在全文">
                    <div className="kb-hit-head">
                      <b>{h.title}</b>
                      <span className="muted small">{h.section}</span>
                    </div>
                    <pre>{h.text}</pre>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
