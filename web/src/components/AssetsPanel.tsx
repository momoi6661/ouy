import { useEffect, useState } from "react";
import { api, fmtUsd, needLogin, type Account } from "../api";
import { Pnl } from "./ui";

/** 资产：现金、可用、占用、当前收益。 */
export function AssetsPanel({ account, loggedIn }: { account: Account | null; loggedIn: boolean }) {
  const [realized, setRealized] = useState<{ pnl: number; count: number } | null>(null);
  const [funding, setFunding] = useState<{ rows: { ccy: string; balance: number; available: number; frozen: number }[]; error: string } | null>(null);
  const positions = account?.positions.length ?? 0;
  useEffect(() => {
    if (!loggedIn) return setFunding(null);
    let alive = true;
    const load = () => api.funding().then((r) => alive && setFunding(r), () => undefined);
    load();
    const timer = window.setInterval(load, 60_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [loggedIn, account?.env]);

  useEffect(() => {
    if (!loggedIn) return setRealized(null);
    let alive = true;
    api
      .history()
      .then((rows) => alive && setRealized({ pnl: rows.reduce((sum, r) => sum + r.pnl, 0), count: rows.length }))
      .catch(() => alive && setRealized(null));
    return () => {
      alive = false;
    };
  }, [loggedIn, positions, account?.env]);

  if (!loggedIn)
    return (
      <div className="panel assets">
        <div className="panel-head">
          <h3>资产</h3>
          <button className="btn xs primary" onClick={needLogin}>
            登录欧易
          </button>
        </div>
        <div className="muted small">登录后显示你的现金、可用保证金和收益。</div>
      </div>
    );

  return (
    <div className="panel assets">
      <div className="panel-head">
        <h3>交易账户 · {account?.env === "live" ? "实盘" : "模拟盘"}</h3>
        <span className="muted small">
          USDT{account?.account_level ? ` · 账户模式 ${account.account_level}` : ""}
          {account ? ` · ${account.positions.length} 个持仓，${account.orders.length} 张挂单` : ""}
        </span>
      </div>
      {!account ? (
        <div className="muted small">加载账户…</div>
      ) : (
        <div className="asset-grid">
          <div>
            <span>总权益</span>
            <b>{fmtUsd(account.equity)}</b>
          </div>
          <div>
            <span>可用（可开仓）</span>
            <b>{fmtUsd(account.available)}</b>
          </div>
          <div>
            <span>USDT 现金</span>
            <b>{fmtUsd(account.cash_usdt)}</b>
          </div>
          <div>
            <span>占用保证金</span>
            <b>{fmtUsd(account.margin_used)}</b>
          </div>
          <div>
            <span>持仓浮动盈亏</span>
            <b>
              <Pnl value={account.upl} />
            </b>
          </div>
          <div>
            <span>今日盈亏</span>
            <b>
              {account.day_pnl != null ? <Pnl value={account.day_pnl} /> : "—"}
              {account.day_pnl_pct != null && (
                <small>
                  {" "}
                  <Pnl value={account.day_pnl_pct} suffix="%" />
                </small>
              )}
            </b>
          </div>
          <div className="wide">
            <span>近期已实现盈亏{realized ? `（最近 ${realized.count} 笔平仓）` : ""}</span>
            <b>{realized ? <Pnl value={realized.pnl} /> : "—"}</b>
          </div>
        </div>
      )}
      <div className="panel-head funding-head">
        <h3>资金账户</h3>
        <span className="muted small">充值、提现、划转用的账户；要交易得先划到交易账户</span>
      </div>
      {!funding ? (
        <div className="muted small">加载中…</div>
      ) : funding.error ? (
        <div className="muted small">查不到资金账户：{funding.error}</div>
      ) : funding.rows.length === 0 ? (
        <div className="muted small">资金账户里没有余额。</div>
      ) : (
        <div className="asset-grid">
          {funding.rows.map((r) => (
            <div key={r.ccy}>
              <span>{r.ccy}</span>
              <b>{r.balance.toLocaleString("en-US", { maximumFractionDigits: 6 })}</b>
              <small className="muted">
                可用 {r.available.toLocaleString("en-US", { maximumFractionDigits: 6 })}
                {r.frozen > 0 ? ` · 冻结 ${r.frozen.toLocaleString("en-US", { maximumFractionDigits: 6 })}` : ""}
              </small>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
