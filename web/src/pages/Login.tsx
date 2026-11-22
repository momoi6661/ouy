import { useEffect, useState } from "react";
import { api, type AppState, type AuthStatus } from "../api";
import { toast, useInterval } from "../components/ui";

const SITE_LABEL: Record<string, string> = { global: "欧易全球站 www.okx.com", eea: "欧易欧洲站 my.okx.com", us: "欧易美国站 app.okx.com", tr: "欧易土耳其站 tr.okx.com" };

export function Login({ state, onLoggedIn, embedded }: { state: AppState; onLoggedIn: () => void; embedded?: boolean }) {
  const [auth, setAuth] = useState<AuthStatus>(state.auth);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(state.auth.error || "");
  const [deadline, setDeadline] = useState<number | null>(state.auth.ttl ? Date.now() + state.auth.ttl * 1000 : null);
  const [now, setNow] = useState(Date.now());

  const start = async () => {
    // 先同步开一个空白页，拿到授权地址后再跳转；否则浏览器会把它当成弹窗拦掉
    const tab = window.open("", "_blank");
    setBusy(true);
    setError("");
    try {
      const result = await api.login();
      if (result.status === "logged_in") {
        tab?.close();
        return onLoggedIn();
      }
      setAuth(result);
      setDeadline(Date.now() + (result.expiresIn ?? result.ttl ?? 600) * 1000);
      if (tab && result.verificationUri) tab.location.href = result.verificationUri;
    } catch (e: any) {
      tab?.close();
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  useInterval(() => setNow(Date.now()), 1000);
  useInterval(
    async () => {
      try {
        const s = await api.authStatus();
        if (s.status === "logged_in") {
          toast("欧易授权成功", "ok");
          onLoggedIn();
        } else if (s.status !== "pending" && auth.status === "pending") {
          setAuth(s);
          setDeadline(null);
          setError(s.error ? `授权没有完成：${s.error}` : "授权没有完成，欧易没有返回原因。请重新授权一次。");
        }
      } catch {
        /* 下次再查 */
      }
    },
    auth.status === "pending" ? 3000 : null,
  );

  useEffect(() => {
    if (deadline && now > deadline) {
      setAuth({ status: "not_logged_in" });
      setDeadline(null);
      setError("验证码已过期，请重新授权。");
    }
  }, [now, deadline]);

  const left = deadline ? Math.max(Math.floor((deadline - now) / 1000), 0) : 0;
  const pending = auth.status === "pending" && auth.verificationUri && auth.userCode;

  const body = (
    <>
      <p className="muted">用欧易官方的 Agent 授权登录。不用填 API Key，令牌由欧易官方组件保存在这台电脑上。</p>
      {pending ? (
        <>
          <ol className="steps">
            <li>在刚打开的欧易页面里登录你的欧易账户</li>
            <li>
              输入验证码
              <button className="code" title="点击复制" onClick={() => navigator.clipboard?.writeText(auth.userCode!).then(() => toast("验证码已复制", "ok"))}>
                {auth.userCode}
              </button>
            </li>
            <li>确认授权，这里会自动变成已登录</li>
          </ol>
          <a className="btn primary block" href={auth.verificationUri} target="_blank" rel="noreferrer">
            欧易页面没打开？点这里打开
          </a>
          <div className="login-foot">
            <span className="muted small">
              {Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")} 后过期 · 正在等待你在欧易页面确认…
            </span>
          </div>
        </>
      ) : (
        <>
          <button className="btn primary block" disabled={busy} onClick={start}>
            {busy ? "正在向欧易申请授权…" : "去欧易授权"}
          </button>
          <p className="muted small center">{SITE_LABEL[state.site] ?? state.site}</p>
        </>
      )}
      {error && <div className="alert error">{error}</div>}
    </>
  );
  if (embedded) return <div className="login-embedded">{body}</div>;
  return (
    <div className="login">
      <div className="login-card">
        <div className="brand big">
          <span className="logo">O</span> Ouy
        </div>
        <h1>登录欧易</h1>
        {body}
      </div>
    </div>
  );
}

export function Unlock({ onDone }: { onDone: () => void }) {
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  return (
    <div className="login">
      <form
        className="login-card"
        onSubmit={async (e) => {
          e.preventDefault();
          try {
            await api.unlock(key);
            onDone();
          } catch (err: any) {
            setError(err.message);
          }
        }}
      >
        <div className="brand big">
          <span className="logo">O</span> Ouy
        </div>
        <h1>输入访问口令</h1>
        <p className="muted">这台服务开启了局域网访问，需要 .env 里设置的 OUY_ACCESS_KEY。</p>
        <input className="input" type="password" autoFocus value={key} onChange={(e) => setKey(e.target.value)} placeholder="访问口令" />
        <button className="btn primary block" disabled={!key}>
          进入
        </button>
        {error && <div className="alert error">{error}</div>}
      </form>
    </div>
  );
}
