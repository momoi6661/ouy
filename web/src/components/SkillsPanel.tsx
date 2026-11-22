import { useEffect, useState } from "react";
import { api, fmtTime, type Skill, type SkillList } from "../api";
import { Markdown } from "./Markdown";
import { Modal, toast } from "./ui";

/** 技能：装给 Worker 用的做法说明。只放在项目的 skills 文件夹里，这里能认出、停用、删除、安装 */
export function SkillsPanel() {
  const [list, setList] = useState<SkillList | null>(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ name: "", content: "", url: "" });
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState<(Skill & { content: string }) | null>(null);
  const load = () => api.skills().then(setList, () => {});
  useEffect(() => {
    load();
  }, []);
  const run = async (key: string, job: () => Promise<SkillList>, ok: string) => {
    setBusy(key);
    try {
      setList(await job());
      toast(ok, "ok");
      return true;
    } catch (e: any) {
      toast(e.message, "error");
      return false;
    } finally {
      setBusy("");
    }
  };
  const install = async () => {
    if (await run("install", () => api.installSkill({ name: form.name.trim(), content: form.content, url: form.url.trim() }), "技能已装好，所有 Worker 都能用")) {
      setAdding(false);
      setForm({ name: "", content: "", url: "" });
    }
  };
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>技能</h3>
        <div className="row">
          <button className="btn xs" onClick={load}>
            重新扫描
          </button>
          <button className="btn primary xs" onClick={() => setAdding(true)}>
            安装技能
          </button>
        </div>
      </div>
      <p className="muted small">
        技能是一份写好的做法说明（SKILL.md）。只装在项目里的这个文件夹：<code>{list?.dir ?? "skills"}</code>，每个技能一个子文件夹；你手动拷进去的也会认出来。
        装好后所有 Worker 都能用（每轮只看名字和简介，用得上才打开全文）。也可以直接对 Router 说「装这个技能」并把内容或网址发给它。技能改不了风控和审批。
      </p>
      {list && list.skills.length === 0 && <p className="muted small">还没有装任何技能。</p>}
      <div className="skill-list">
        {list?.skills.map((s) => (
          <div key={s.id} className={`skill-row ${s.enabled ? "" : "off"}`}>
            <div className="skill-main">
              <b>{s.name}</b>
              <span className="muted small">
                {" "}
                文件夹 {s.id} · {s.chars} 字 · {fmtTime(s.ts)}
              </span>
              <div className="small">{s.description || "没有写简介"}</div>
              {s.warnings.length > 0 && <div className="small skill-warn">要注意：这个技能里有「{s.warnings.join("」「")}」的内容。风控和审批不会因此放松，但建议你打开看一遍再启用。</div>}
            </div>
            <div className="row">
              <span className={`tag ${s.enabled ? "ok" : ""}`}>{s.enabled ? "启用中" : "已停用"}</span>
              <button className="btn xs" onClick={() => api.readSkill(s.id).then(setOpen, (e) => toast(e.message, "error"))}>
                查看
              </button>
              <button className="btn xs" disabled={busy === s.id} onClick={() => run(s.id, () => api.enableSkill(s.id, !s.enabled), s.enabled ? `已停用「${s.name}」，Worker 不会再用它` : `已启用「${s.name}」`)}>
                {s.enabled ? "停用" : "启用"}
              </button>
              <button
                className="btn danger xs"
                disabled={busy === s.id}
                onClick={() => window.confirm(`删除技能「${s.name}」？会把 ${s.path.replace(/[\\/]SKILL\.md$/, "")} 这个文件夹删掉。`) && run(s.id, () => api.deleteSkill(s.id), `已删除「${s.name}」`)}
              >
                删除
              </button>
            </div>
          </div>
        ))}
      </div>
      {adding && (
        <Modal title="安装技能" onClose={() => setAdding(false)}>
          <label className="field">
            <span>名字（可以不填，从内容里取）</span>
            <input className="input" maxLength={40} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </label>
          <label className="field">
            <span>SKILL.md 的网址（https；GitHub 上的文件或文件夹地址也行）</span>
            <input className="input" placeholder="https://github.com/…/SKILL.md" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
          </label>
          <label className="field">
            <span>或者直接粘贴技能内容（填了就以这里为准）</span>
            <textarea className="input" rows={10} maxLength={20000} placeholder={"# 技能名字\n\n什么时候用……\n\n1. 第一步……\n2. 第二步……"} value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })} />
          </label>
          <div className="row">
            <button className="btn primary" disabled={busy === "install" || (!form.content.trim() && !form.url.trim())} onClick={install}>
              {busy === "install" ? "安装中…" : "安装"}
            </button>
            <button className="btn" onClick={() => setAdding(false)}>
              取消
            </button>
          </div>
        </Modal>
      )}
      {open && (
        <Modal title={`技能：${open.name}`} onClose={() => setOpen(null)}>
          <p className="muted small">{open.path}</p>
          <div className="skill-body">
            <Markdown text={open.content} />
          </div>
        </Modal>
      )}
    </div>
  );
}
