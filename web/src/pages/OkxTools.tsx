import { useEffect, useMemo, useState } from "react";
import { api, type OkxTool } from "../api";
import { Empty, Modal, toast } from "../components/ui";

const MODULES: Record<string, string> = {
  market: "行情", account: "账户", spot: "现货", swap: "永续合约", futures: "交割合约", option: "期权", earn: "赚币理财",
  bot: "策略机器人", event: "事件合约", smartmoney: "聪明钱", news: "新闻情绪", trade: "交易历史", skills: "技能市场", system: "系统",
};
const ORDER = Object.keys(MODULES);

function parseValue(raw: string, spec: any): unknown {
  const type = spec?.type;
  if (type === "boolean") return raw === "true";
  if (type === "number" || type === "integer") return Number(raw);
  if (type === "array" || type === "object") return JSON.parse(raw);
  return raw;
}

export function OkxTools({ env, onActed }: { env: string; onActed: () => void }) {
  const [tools, setTools] = useState<OkxTool[]>([]);
  const [module, setModule] = useState("market");
  const [query, setQuery] = useState("");
  const [tool, setTool] = useState<OkxTool | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [result, setResult] = useState<string>("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    api
      .okxTools()
      .then(setTools)
      .catch((e) => setError(e.message));
  }, []);

  const modules = useMemo(() => {
    const counts: Record<string, number> = {};
    tools.forEach((t) => (counts[t.module] = (counts[t.module] ?? 0) + 1));
    return Object.keys(counts).sort((a, b) => (ORDER.indexOf(a) + 100) % 100 - (ORDER.indexOf(b) + 100) % 100).map((m) => ({ id: m, count: counts[m] }));
  }, [tools]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return tools.filter((t) => (q ? `${t.name} ${t.title} ${t.description}`.toLowerCase().includes(q) : t.module === module));
  }, [tools, module, query]);

  const pick = (t: OkxTool) => {
    setTool(t);
    setValues({});
    setResult("");
    setError("");
  };

  const buildArgs = (): Record<string, unknown> | null => {
    if (!tool) return null;
    const props = tool.schema.properties ?? {};
    const args: Record<string, unknown> = {};
    for (const [name, raw] of Object.entries(values)) {
      if (raw === "") continue;
      try {
        args[name] = parseValue(raw, props[name]);
      } catch {
        setError(`参数 ${name} 不是合法的 JSON`);
        return null;
      }
    }
    const missing = (tool.schema.required ?? []).filter((r) => args[r] === undefined);
    if (missing.length) {
      setError(`缺少必填参数：${missing.join("、")}`);
      return null;
    }
    return args;
  };

  const call = async (args: Record<string, unknown>, confirmed: boolean) => {
    if (!tool) return;
    setBusy(true);
    setError("");
    try {
      const r = await api.okxCall(tool.name, args, confirmed);
      setResult(JSON.stringify(r.result, null, 2));
      if (!tool.readOnly) {
        toast("已执行", "ok");
        onActed();
      }
    } catch (e: any) {
      setError(e.message);
      setResult("");
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  const run = () => {
    const args = buildArgs();
    if (!args) return;
    if (tool!.readOnly) call(args, false);
    else setConfirm(args);
  };

  const props = tool?.schema.properties ?? {};
  const required = new Set(tool?.schema.required ?? []);

  return (
    <div className="page okx">
      <div className="panel okx-nav">
        <input className="input sm" placeholder={`搜索全部 ${tools.length} 个功能`} value={query} onChange={(e) => setQuery(e.target.value)} />
        {!query &&
          modules.map((m) => (
            <button key={m.id} className={`nav-item ${m.id === module ? "on" : ""}`} onClick={() => setModule(m.id)}>
              {MODULES[m.id] ?? m.id}
              <em>{m.count}</em>
            </button>
          ))}
      </div>

      <div className="panel okx-list">
        <div className="panel-head">
          <h3>{query ? "搜索结果" : MODULES[module] ?? module}</h3>
          <span className="muted small">{shown.length} 个</span>
        </div>
        <div className="okx-items">
          {shown.map((t) => (
            <button key={t.name} className={`okx-item ${tool?.name === t.name ? "on" : ""}`} onClick={() => pick(t)}>
              <span>
                <b>{t.title}</b>
                <span className={`tag ${t.readOnly ? "read" : "write"}`}>{t.readOnly ? "查询" : "操作"}</span>
              </span>
              <em>{t.name}</em>
            </button>
          ))}
          {!shown.length && <Empty>{tools.length ? "没有匹配的功能" : error || "加载中…"}</Empty>}
        </div>
      </div>

      <div className="panel okx-run">
        {tool ? (
          <>
            <div className="panel-head">
              <h3>{tool.title}</h3>
              <span className={`tag ${tool.readOnly ? "read" : "write"}`}>{tool.readOnly ? "只读查询" : "会改动账户"}</span>
            </div>
            <p className="muted small desc">{tool.description}</p>
            <div className="form-grid">
              {Object.entries(props).map(([name, spec]: [string, any]) => (
                <label key={name} className="field" title={spec.description}>
                  <span>
                    {name}
                    {required.has(name) && <i className="req">*</i>}
                  </span>
                  {spec.enum ? (
                    <select value={values[name] ?? ""} onChange={(e) => setValues({ ...values, [name]: e.target.value })}>
                      <option value="">—</option>
                      {spec.enum.map((v: string) => (
                        <option key={v} value={v}>
                          {v}
                        </option>
                      ))}
                    </select>
                  ) : spec.type === "boolean" ? (
                    <select value={values[name] ?? ""} onChange={(e) => setValues({ ...values, [name]: e.target.value })}>
                      <option value="">—</option>
                      <option value="true">是</option>
                      <option value="false">否</option>
                    </select>
                  ) : (
                    <input
                      className="input sm"
                      value={values[name] ?? ""}
                      placeholder={spec.type === "array" || spec.type === "object" ? "JSON" : (spec.description ?? "").slice(0, 40)}
                      onChange={(e) => setValues({ ...values, [name]: e.target.value })}
                    />
                  )}
                </label>
              ))}
              {!Object.keys(props).length && <span className="muted small">这个功能不需要参数</span>}
            </div>
            <div className="row">
              <button className={`btn ${tool.readOnly ? "primary" : "danger"}`} disabled={busy} onClick={run}>
                {busy ? "执行中…" : tool.readOnly ? "查询" : "执行"}
              </button>
              <span className="muted small">当前环境：{env === "demo" ? "模拟盘" : "实盘"}</span>
            </div>
            {error && <div className="alert error">{error}</div>}
            {result && <pre className="result">{result}</pre>}
          </>
        ) : (
          <Empty>
            这里是欧易官方开放给 Agent 的全部能力：行情、账户、现货、合约、期权、理财、网格/定投机器人、新闻情绪、聪明钱等。
            <br />
            左边选一个功能，填参数就能直接用。Agent 盯盘时也能调用其中所有的查询类功能。
          </Empty>
        )}
      </div>

      {confirm && tool && (
        <Modal title="确认执行" onClose={() => setConfirm(null)}>
          <p>
            即将在<b>{env === "demo" ? "模拟盘" : "实盘"}</b>执行「{tool.title}」，这会改动你的账户，并且不经过 Agent 风控。
          </p>
          <pre className="result">{JSON.stringify(confirm, null, 2)}</pre>
          <div className="modal-foot">
            <button className="btn" onClick={() => setConfirm(null)}>
              取消
            </button>
            <button className="btn danger" disabled={busy} onClick={() => call(confirm, true)}>
              确认执行
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
