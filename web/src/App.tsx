import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, fmtUsd, MODE_LABEL, type Account, type Activity, type AppState, type JournalEntry, type PendingItem } from "./api";
import { Modal, Pnl, Toasts, toast, useInterval, useStored } from "./components/ui";
import { AgentSettings } from "./pages/AgentSettings";
import { Cockpit } from "./pages/Cockpit";
import { Knowledge } from "./pages/Knowledge";
import { Login, Unlock } from "./pages/Login";
import { OkxTools } from "./pages/OkxTools";
import { Workspace } from "./pages/Workspace";

type Tab = "workspace" | "cockpit" | "agent" | "kb" | "okx";
const TABS: { id: Tab; label: string }[] = [
  { id: "workspace", label: "Agent 工作台" },
  { id: "cockpit", label: "交易" },
  { id: "agent", label: "设置" },
  { id: "kb", label: "知识库" },
  { id: "okx", label: "欧易全功能" },
];

export default function App() {
  const [state, setState] = useState<AppState | null>(null);
  const [locked, setLocked] = useState(false);
  const [fatal, setFatal] = useState("");
  const [account, setAccount] = useState<Account | null>(null);
  const [accountError, setAccountError] = useState("");
  const [ticks, setTicks] = useState<Record<string, number>>({});
  const [journal, setJournal] = useState<JournalEntry[]>([]);
  const [pending, setPending] = useState<PendingItem[]>([]);
  const [tab, setTab] = useStored<Tab>("ouy.tab.v2", "workspace");
  const [activity, setActivity] = useState<Activity[]>([]);
  const [inst, setInst] = useStored("ouy.inst", "BTC-USDT-SWAP");
  const [redUp, setRedUp] = useStored("ouy.redUp", false);
  const [busy, setBusy] = useState(false);
  const [panicAsk, setPanicAsk] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const [btc, setBtc] = useState<{ last: number; change_24h_pct: number | null } | null>(null);
  const loadBtc = useCallback(() => {
    if (!document.hidden) api.ticker("BTC-USDT-SWAP").then(setBtc).catch(() => undefined);
  }, []);
  const loggedIn = state?.auth.status === "logged_in";

  useEffect(() => {
    const open = () => setLoginOpen(true);
    window.addEventListener("ouy:need-login", open);
    return () => window.removeEventListener("ouy:need-login", open);
  }, []);

  const loadState = useCallback(async () => {
    try {
      setState(await api.state());
      setLocked(false);
      setFatal("");
    } catch (e: any) {
      if (e instanceof ApiError && e.locked) setLocked(true);
      else setFatal(e.message || "连不上后端服务");
    }
  }, []);

  const loadAccount = useCallback(async () => {
    try {
      setAccount(await api.account());
      setAccountError("");
    } catch (e: any) {
      if (e instanceof ApiError && e.needsLogin) loadState();
      else setAccountError(e.message);
    }
  }, [loadState]);

  // 活动流：先取最近的，之后靠实时推送一条条追加；断线重连后按最后一条的 id 补齐
  const lastActivity = useRef(0);
  const loadActivity = useCallback(async () => {
    try {
      const rows = await api.activity(300, lastActivity.current);
      if (!rows.length) return;
      lastActivity.current = rows[rows.length - 1].id;
      setActivity((all) => [...all, ...rows.filter((r) => !all.some((x) => x.id === r.id))].slice(-600));
    } catch {
      /* 下一轮再试 */
    }
  }, []);

  const loadFeeds = useCallback(async () => {
    try {
      const [j, p] = await Promise.all([api.journal(150), api.pending()]);
      setJournal(j);
      setPending(p);
    } catch {
      /* 下一轮再试 */
    }
  }, []);

  const refresh = useCallback(() => {
    loadState();
    loadAccount();
    loadFeeds();
  }, [loadState, loadAccount, loadFeeds]);

  useEffect(() => {
    loadState();
  }, [loadState]);
  useEffect(() => {
    loadFeeds();
    if (loggedIn) {
      loadAccount();
      setLoginOpen(false);
    } else {
      setAccount(null);
    }
  }, [loggedIn, state?.config.env, loadAccount, loadFeeds]);
  useInterval(loadState, 8000);
  useEffect(loadBtc, [loadBtc]);
  useInterval(loadBtc, 5000);
  useInterval(() => !document.hidden && loadAccount(), loggedIn ? 6000 : null);
  useInterval(loadFeeds, 20000);
  useEffect(() => {
    lastActivity.current = 0;
    setActivity([]);
    loadActivity();
  }, [state?.config.env, loadActivity]);
  useInterval(loadActivity, 15000);

  // 实时推送
  const handlers = useRef({ loadAccount, loadFeeds });
  handlers.current = { loadAccount, loadFeeds };
  useEffect(() => {
    if (!state) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout>;
    const connect = () => {
      ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "tick") setTicks((t) => ({ ...t, [msg.data.inst_id]: msg.data.last }));
        else if (msg.type === "account") msg.data ? setAccount(msg.data) : handlers.current.loadAccount();
        else if (msg.type === "agent") setState((s) => (s ? { ...s, agent: msg.data } : s));
        else if (msg.type === "journal") {
          setJournal((j) => [msg.data, ...j].slice(0, 300));
          if (msg.data.kind === "alert") toast(msg.data.title, "error");
        } else if (msg.type === "pending") {
          handlers.current.loadFeeds();
          window.dispatchEvent(new CustomEvent("ouy:chat"));
        }
        else if (msg.type === "activity") {
          lastActivity.current = Math.max(lastActivity.current, msg.data.id);
          setActivity((all) => (all.some((x) => x.id === msg.data.id) ? all : [...all, msg.data].slice(-600)));
        } else if (msg.type === "work") window.dispatchEvent(new CustomEvent("ouy:work"));
        else if (msg.type === "chat") window.dispatchEvent(new CustomEvent("ouy:chat", { detail: msg.data }));
        else if (msg.type === "stream") window.dispatchEvent(new CustomEvent("ouy:stream", { detail: msg.data }));
      };
      ws.onclose = () => {
        if (!closed) retry = setTimeout(connect, 3000);
      };
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      ws?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [!!state]);

  if (locked) return <Unlock onDone={loadState} />;
  if (!state)
    return (
      <div className="login">
        <div className="login-card">
          <div className="brand big">
            <span className="logo">O</span> Ouy
          </div>
          {fatal ? <div className="alert error">{fatal}</div> : <p className="muted center">正在连接…</p>}
        </div>
      </div>
    );
  const cfg = state.config;
  const toggleAgent = async () => {
    if (!loggedIn) {
      toast("启动盯盘前需要先登录欧易", "info");
      return setLoginOpen(true);
    }
    setBusy(true);
    try {
      const agent = state.agent.running ? await api.agentStop() : await api.agentStart();
      setState({ ...state, agent });
      toast(agent.running ? "Agent 开始盯盘，会自己分析和下单" : "Agent 已停止盯盘", agent.running ? "ok" : "info");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const toggleWatch = async (i: string) => {
    const has = cfg.instruments.includes(i);
    if (has && cfg.instruments.length === 1) return toast("盯盘列表至少保留一个品种", "error");
    if (!has && cfg.instruments.length >= 8) return toast("最多同时盯 8 个品种", "error");
    try {
      const r = await api.saveConfig({ ...cfg, instruments: has ? cfg.instruments.filter((x) => x !== i) : [...cfg.instruments, i] }, cfg.env === "live");
      setState({ ...state, config: r.config, agent: r.agent });
      toast(has ? `已把 ${i} 移出盯盘列表` : `已把 ${i} 交给 Agent 盯盘`, "ok");
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const panic = async () => {
    setBusy(true);
    try {
      const r = await api.panic();
      const failed = r.results.filter((x) => x.status !== "executed");
      const unfilled = r.results.filter((x) => x.status === "executed" && x.detail?.unfilled);
      if (failed.length) toast(`有 ${failed.length} 项没成功：${failed[0].message}`, "error");
      else if (unfilled.length) toast(`有 ${unfilled.length} 个仓位还没平掉：${unfilled.map((x) => x.message).join(" ")}`, "error");
      else toast("已撤掉全部挂单并市价平掉全部持仓，Agent 已停止", "ok");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
      setPanicAsk(false);
      refresh();
    }
  };

  const logout = async () => {
    if (!window.confirm("退出欧易授权？Agent 会停止盯盘，已有持仓和交易所里的止盈止损单不受影响。")) return;
    await api.logout().catch((e) => toast(e.message, "error"));
    refresh();
  };

  return (
    <div className={`app ${redUp ? "red-up" : ""}`}>
      <header className="top">
        <div className="brand">
          <span className="logo">O</span> Ouy
        </div>
        <nav className="tabs-top">
          {TABS.map((t) => (
            <button key={t.id} className={tab === t.id ? "on" : ""} onClick={() => setTab(t.id)}>
              {t.label}
              {t.id === "workspace" && pending.length > 0 && <i className="badge">{pending.length}</i>}
            </button>
          ))}
        </nav>
        <div className="top-right">
          {btc && (
            <button className="btc-chip" onClick={() => { setInst("BTC-USDT-SWAP"); setTab("cockpit"); }} title="BTC 是大盘，其他币大多跟着它走。点击查看 BTC">
              <span>BTC</span>
              <b>{(ticks["BTC-USDT-SWAP"] || btc.last).toLocaleString("en-US", { maximumFractionDigits: 1 })}</b>
              {btc.change_24h_pct != null && <Pnl value={btc.change_24h_pct} suffix="%" />}
            </button>
          )}
          <button className={`env ${cfg.env}`} onClick={() => setTab("agent")} title="点击去切换交易环境">
            {cfg.env === "demo" ? "模拟盘" : "实盘"}
          </button>
          {!loggedIn && (
            <button className="btn primary" onClick={() => setLoginOpen(true)}>
              登录欧易
            </button>
          )}
          <div className={`stat ${loggedIn ? "" : "hidden"}`}>
            <span>权益 USDT</span>
            <b>{account ? fmtUsd(account.equity) : "—"}</b>
          </div>
          <div className={`stat ${loggedIn ? "" : "hidden"}`}>
            <span>今日</span>
            <b>{account?.day_pnl_pct != null ? <Pnl value={account.day_pnl_pct} suffix="%" /> : "—"}</b>
          </div>
          <div className={`stat hide-sm ${loggedIn ? "" : "hidden"}`}>
            <span>浮动盈亏</span>
            <b>{account ? <Pnl value={account.upl} /> : "—"}</b>
          </div>
          <button className={`btn agent-btn ${state.agent.running ? "running" : "primary"}`} disabled={busy} onClick={toggleAgent}>
            {state.agent.running ? (
              <>
                <span className="dot live" />
                盯盘中 · 停止
              </>
            ) : (
              "启动盯盘"
            )}
          </button>
          {loggedIn && (
            <>
              <button className="btn danger hide-sm" onClick={() => setPanicAsk(true)} title="撤掉全部挂单并市价平掉全部持仓">
                一键全平
              </button>
              <button className="icon-btn" onClick={logout} title="退出欧易授权">
                ⎋
              </button>
            </>
          )}
        </div>
      </header>

      {(account?.halted || accountError || state.agent.error || !state.ai.configured) && (
        <div className="banner">
          {account?.halted && <span>今日亏损已达上限 {cfg.risk.max_daily_loss_pct}%，今天只允许减仓和平仓。</span>}
          {accountError && <span>读取账户失败：{accountError}</span>}
          {state.agent.error && <span>盯盘异常：{state.agent.error}</span>}
          {!state.ai.configured && <span>还没配置 模型密钥：请在项目的 .env 里填写 OPENAI_API_KEY 后重启。</span>}
        </div>
      )}

      <main>
        {(tab === "workspace" || !TABS.some((t) => t.id === tab)) && (
          <Workspace
            state={state}
            account={account}
            ticks={ticks}
            pending={pending}
            activity={activity}
            inst={inst}
            redUp={redUp}
            loggedIn={loggedIn}
            setInst={setInst}
            refresh={refresh}
            onTrade={() => setTab("cockpit")}
          />
        )}
        {tab === "cockpit" && (
          <Cockpit
            state={state}
            account={account}
            ticks={ticks}
            journal={journal}
            pending={pending}
            activity={activity}
            inst={inst}
            redUp={redUp}
            setInst={setInst}
            onToggleWatch={toggleWatch}
            refresh={refresh}
            loggedIn={loggedIn}
          />
        )}
        {tab === "agent" && <AgentSettings state={state} journal={journal} redUp={redUp} setRedUp={setRedUp} refresh={refresh} />}
        {tab === "kb" && <Knowledge kb={state.kb} onChanged={loadState} />}
        {tab === "okx" && <OkxTools env={cfg.env} onActed={refresh} />}
      </main>

      <footer className="foot">
        <span>
          {MODE_LABEL[cfg.mode]} · 最高 {cfg.risk.max_leverage}x · 单仓风险 {cfg.risk.risk_per_trade_pct}% · 盯 {cfg.instruments.length} 个品种
        </span>
        <span className="hide-sm">模型 {state.ai.model} · v{state.version}</span>
        {loggedIn && (
          <button className="btn xs danger show-sm" onClick={() => setPanicAsk(true)}>
            一键全平
          </button>
        )}
      </footer>

      {loginOpen && !loggedIn && (
        <Modal title="登录欧易" onClose={() => setLoginOpen(false)}>
          <Login state={state} onLoggedIn={refresh} embedded />
        </Modal>
      )}
      {panicAsk && (
        <Modal title="一键全平" onClose={() => setPanicAsk(false)}>
          <p>
            将在<b>{cfg.env === "demo" ? "模拟盘" : "实盘"}</b>撤掉全部合约挂单、<b>市价平掉全部合约持仓</b>，并停止 Agent 盯盘。
          </p>
          <div className="modal-foot">
            <button className="btn" onClick={() => setPanicAsk(false)}>
              取消
            </button>
            <button className="btn danger" disabled={busy} onClick={panic}>
              {busy ? "执行中…" : "确认全平"}
            </button>
          </div>
        </Modal>
      )}
      <Toasts />
    </div>
  );
}
