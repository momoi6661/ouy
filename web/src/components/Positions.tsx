import { Fragment, useState, useEffect } from "react";
import { api, fmtPx, fmtUsd, shortInst, STATUS_LABEL, type Account, type Position, type TradeAction, needLogin, fmtTime, type ClosedTrade, type Lot, STYLES } from "../api";
import { Empty, Modal, Pnl, toast, LeverPicker } from "./ui";
import { StopsEditor } from "./OrderTicket";

interface Props {
  account: Account | null;
  maxLeverage: number;
  onSelect: (inst: string) => void;
  onDone: () => void;
  loggedIn: boolean;
}

export function Positions({ account, maxLeverage, onSelect, onDone, loggedIn }: Props) {
  const [tab, setTab] = useState<"pos" | "orders" | "stops" | "closed">("pos");
  const [closed, setClosed] = useState<ClosedTrade[] | null>(null);
  const [closedError, setClosedError] = useState("");
  const loadClosed = () => {
    setTab("closed");
    if (!loggedIn) return;
    api
      .history()
      .then((rows) => {
        setClosed(rows);
        setClosedError("");
      })
      .catch((e) => setClosedError(e.message));
  };
  const [edit, setEdit] = useState<{ pos: Position; mode: "stops" | "lever" | "close" | "reverse" | "add" } | null>(null);
  // 每一笔仓位：欧易里同一个币同方向只有一个合并持仓，项目里按笔分开记（各自的风格、入场价、止损止盈）
  const [lots, setLots] = useState<Lot[]>([]);
  const [guard, setGuard] = useState<Lot | null>(null);
  const lotKey = (account?.positions ?? []).map((p) => `${p.inst_id}${p.side}${p.contracts}`).join("|") + `#${account?.orders.length ?? 0}#${account?.protections.length ?? 0}`;
  useEffect(() => {
    if (!loggedIn) return setLots([]);
    api.lots().then(setLots, () => undefined);
  }, [lotKey, loggedIn]);
  const [busy, setBusy] = useState("");

  const run = async (action: TradeAction, label: string) => {
    setBusy(label);
    try {
      const r = await api.trade(action);
      toast(r.detail?.unfilled ? r.message : `${STATUS_LABEL[r.status]}：${r.message}`, r.status === "executed" && !r.detail?.unfilled ? "ok" : "error");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy("");
      onDone();
    }
  };

  const positions = account?.positions ?? [];
  const orders = account?.orders ?? [];
  const stops = account?.protections ?? [];
  const stopOf = (inst: string) => {
    const mine = stops.filter((s) => s.inst_id === inst);
    return { sl: mine.find((s) => s.stop_loss)?.stop_loss ?? null, tp: mine.find((s) => s.take_profit)?.take_profit ?? null };
  };

  // 挂单一共投入了多少钱：每张单的保证金 = 没成交部分的名义价值 ÷ 杠杆
  const orderMargin = orders.reduce((sum, o) => sum + (o.margin_usd ?? 0), 0);
  const orderNotional = orders.reduce((sum, o) => sum + (o.notional_usd ?? 0), 0);

  return (
    <div className="panel positions">
      <div className="tabs">
        <button className={tab === "pos" ? "on" : ""} onClick={() => setTab("pos")}>
          持仓 {positions.length || ""}
        </button>
        <button className={tab === "orders" ? "on" : ""} onClick={() => setTab("orders")}>
          挂单 {orders.length || ""}
          {orderMargin > 0 && <em className="tab-sum">{fmtUsd(orderMargin, 0)} U</em>}
        </button>
        <button className={tab === "stops" ? "on" : ""} onClick={() => setTab("stops")}>
          止盈止损 {stops.length || ""}
        </button>
        <button className={tab === "closed" ? "on" : ""} onClick={loadClosed}>
          已平仓
        </button>
      </div>
      {!loggedIn && (
        <Empty>
          还没登录欧易，登录后这里显示你的持仓、挂单和历史盈亏。
          <div>
            <button className="btn sm primary" onClick={needLogin}>
              登录欧易
            </button>
          </div>
        </Empty>
      )}
      {loggedIn &&
        tab === "closed" &&
        (closedError ? (
          <Empty>读取历史平仓失败：{closedError}</Empty>
        ) : !closed ? (
          <Empty>加载中…</Empty>
        ) : closed.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>平仓时间</th>
                  <th>合约</th>
                  <th>开仓均价</th>
                  <th>平仓均价</th>
                  <th>已实现盈亏</th>
                  <th>出场方式</th>
                </tr>
              </thead>
              <tbody>
                {closed.map((t, i) => (
                  <tr key={`${t.closed_ts}${i}`} onClick={() => onSelect(t.inst_id)}>
                    <td>{fmtTime(t.closed_ts)}</td>
                    <td>
                      <b>{shortInst(t.inst_id)}</b> <span className={`tag ${t.side}`}>{t.side === "long" ? "多" : "空"} {t.leverage}x</span>
                    </td>
                    <td>{fmtPx(t.open_px)}</td>
                    <td>{fmtPx(t.close_px)}</td>
                    <td>
                      <Pnl value={t.pnl} /> <span className="small">(<Pnl value={t.pnl_pct} suffix="%" />)</span>
                    </td>
                    <td>{t.how}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>最近没有已平仓的记录</Empty>
        ))}

      {loggedIn && tab === "pos" &&
        (positions.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>合约</th>
                  <th>投入（保证金）</th>
                  <th>开仓均价</th>
                  <th>标记价</th>
                  <th>强平价</th>
                  <th>浮动盈亏</th>
                  <th>止损 / 止盈</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => {
                  const s = stopOf(p.inst_id);
                  const mine = lots.filter((l) => l.inst_id === p.inst_id && l.status !== "closed");
                  return (
                    <Fragment key={p.inst_id + p.side}>
                    <tr onClick={() => onSelect(p.inst_id)}>
                      <td>
                        <b>{shortInst(p.inst_id)}</b>{" "}
                        <span className={`tag ${p.side}`}>
                          {p.side === "long" ? "多" : "空"} {p.leverage}x
                        </span>
                      </td>
                      <td>
                        {fmtUsd(p.margin)} U<div className="muted small">名义 {fmtUsd(p.notional_usd, 0)} U</div>
                      </td>
                      <td>{fmtPx(p.avg_px)}</td>
                      <td>{fmtPx(p.mark_px)}</td>
                      <td>{fmtPx(p.liq_px)}</td>
                      <td>
                        <Pnl value={p.upl} />
                        <div className="small">
                          <Pnl value={p.upl_ratio * 100} suffix="%" />
                        </div>
                      </td>
                      <td>
                        {s.sl ? <span className="down-fixed">{fmtPx(s.sl)}</span> : <span className="warn">未设止损</span>} / {s.tp ? <span className="up-fixed">{fmtPx(s.tp)}</span> : "—"}
                      </td>
                      <td className="actions" onClick={(e) => e.stopPropagation()}>
                        <button className="btn xs" onClick={() => setEdit({ pos: p, mode: "stops" })}>
                          止盈止损
                        </button>
                        <button className="btn xs" onClick={() => setEdit({ pos: p, mode: "lever" })}>
                          杠杆
                        </button>
                        <button className="btn xs" onClick={() => setEdit({ pos: p, mode: "add" })}>
                          加仓
                        </button>
                        <button className="btn xs" onClick={() => setEdit({ pos: p, mode: "close" })}>
                          减仓
                        </button>
                        <button className="btn xs" onClick={() => setEdit({ pos: p, mode: "reverse" })}>
                          反手
                        </button>
                        <button className="btn xs danger" disabled={!!busy} onClick={() => run({ kind: "close", inst_id: p.inst_id, reason: "手动平仓" }, "close")}>
                          市价全平
                        </button>
                      </td>
                    </tr>
                    {mine.map((l) => (
                      <tr key={`lot${l.id}`} className="lot-row" onClick={() => onSelect(p.inst_id)}>
                        <td>
                          <span className="muted">└ 第 {l.id} 笔</span> <span className="tag style">{STYLES.find((x) => x.id === l.style)?.label ?? l.style}</span>
                          {l.source === "external" && <span className="muted small" title="不是在本项目里开的仓，按这个币的默认风格记账"> 外部</span>}
                          {l.source === "user" && <span className="muted small" title="你手动开的仓"> 手动</span>}
                        </td>
                        <td>{p.contracts ? `${fmtUsd((p.margin * l.contracts) / p.contracts)} U` : "—"}</td>
                        <td>{fmtPx(l.entry_px)}</td>
                        <td className="muted">{l.status === "working" ? "挂单未成交" : fmtTime(l.ts)}</td>
                        <td />
                        <td>{l.pnl_pct != null ? <Pnl value={l.pnl_pct} suffix="%" /> : "—"}</td>
                        <td>
                          {l.stop_loss ? <span className="down-fixed">{fmtPx(l.stop_loss)}</span> : <span className="warn">未设止损</span>} /{" "}
                          {l.take_profit ? <span className="up-fixed">{fmtPx(l.take_profit)}</span> : "—"}
                        </td>
                        <td className="actions" onClick={(e) => e.stopPropagation()}>
                          {account?.env === "demo" && l.status === "open" && (
                            <button className="btn xs" disabled={!l.take_profit} title={l.take_profit ? "为这笔仓位设置停滞或回吐规则" : "先给这一笔设置止盈目标"} onClick={() => setGuard(l)}>提前止盈</button>
                          )}
                          {l.status === "open" && mine.length > 1 && (
                            <button
                              className="btn xs"
                              disabled={!!busy}
                              title="只平这一笔，别的几笔不动"
                              onClick={() => run({ kind: "close", inst_id: p.inst_id, lot_id: l.id, reason: `手动平掉第 ${l.id} 笔` }, "close")}
                            >
                              平这一笔
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>{account ? "当前没有持仓" : "加载账户…"}</Empty>
        ))}

      {loggedIn && tab === "orders" &&
        (orders.length ? (
          <div className="table-scroll">
            <div className="orders-sum">
              挂单合计投入保证金 <b>{fmtUsd(orderMargin)} USDT</b>
              <span className="muted">
                {orders.length} 张，名义价值 {fmtUsd(orderNotional)} USDT。没成交前这笔钱被冻结，撤单后退回。
              </span>
            </div>
            <table>
              <thead>
                <tr>
                  <th>合约</th>
                  <th>方向</th>
                  <th>类型</th>
                  <th>价格</th>
                  <th>数量 / 已成交</th>
                  <th>杠杆</th>
                  <th>名义价值 (USDT)</th>
                  <th>投入保证金 (USDT)</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {orders.map((o) => (
                  <tr key={o.ord_id} onClick={() => onSelect(o.inst_id)}>
                    <td>
                      <b>{shortInst(o.inst_id)}</b>
                    </td>
                    <td className={o.side === "buy" ? "up" : "down"}>{o.side === "buy" ? "买入" : "卖出"}</td>
                    <td>{o.ord_type}</td>
                    <td>{fmtPx(o.px)}</td>
                    <td>
                      {o.sz} / {o.filled}
                    </td>
                    <td>{o.leverage ? `${o.leverage}x` : "—"}</td>
                    <td>{o.notional_usd ? fmtUsd(o.notional_usd) : "—"}</td>
                    <td>
                      <b>{o.reduce_only ? "只减仓，不占用" : o.margin_usd ? fmtUsd(o.margin_usd) : "—"}</b>
                    </td>
                    <td className="actions" onClick={(e) => e.stopPropagation()}>
                      <button className="btn xs" disabled={!!busy} onClick={() => run({ kind: "cancel_orders", inst_id: o.inst_id, reason: "手动撤单" }, "cancel")}>
                        撤销该品种挂单
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>没有未成交的挂单</Empty>
        ))}

      {loggedIn && tab === "stops" &&
        (stops.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>合约</th>
                  <th>类型</th>
                  <th>方向</th>
                  <th>数量</th>
                  <th>止损触发价</th>
                  <th>止盈触发价</th>
                </tr>
              </thead>
              <tbody>
                {stops.map((s) => (
                  <tr key={s.algo_id} onClick={() => onSelect(s.inst_id)}>
                    <td>
                      <b>{shortInst(s.inst_id)}</b>
                    </td>
                    <td>{s.ord_type}</td>
                    <td>{s.side === "buy" ? "买入平空" : "卖出平多"}</td>
                    <td>{s.sz || "—"}</td>
                    <td className="down-fixed">{fmtPx(s.stop_loss ?? s.trigger_px)}</td>
                    <td className="up-fixed">{fmtPx(s.take_profit)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>没有止盈止损单</Empty>
        ))}

      {account?.env === "demo" && guard && <ProfitGuardModal lot={guard} onClose={() => setGuard(null)} />}
      {edit?.mode === "stops" && <StopsModal pos={edit.pos} current={stopOf(edit.pos.inst_id)} onClose={() => setEdit(null)} onSubmit={(a) => run(a, "stops").then(() => setEdit(null))} />}
      {edit?.mode === "close" && (
        <CloseModal
          pos={edit.pos}
          busy={!!busy}
          onClose={() => setEdit(null)}
          onSubmit={(fraction) =>
            run(fraction >= 1 ? { kind: "close", inst_id: edit.pos.inst_id, reason: "手动平仓" } : { kind: "reduce", inst_id: edit.pos.inst_id, fraction, reason: `手动平仓 ${fraction * 100}%` }, "close").then(() => setEdit(null))
          }
        />
      )}
      {edit?.mode === "reverse" && (
        <ReverseModal
          pos={edit.pos}
          busy={!!busy}
          onClose={() => setEdit(null)}
          onSubmit={async (sl, tp) => {
            const pos = edit.pos;
            setBusy("reverse");
            try {
              const closed = await api.trade({ kind: "close", inst_id: pos.inst_id, reason: "一键反手：先平仓" });
              if (closed.status !== "executed") {
                toast(`反手没有执行，原仓位未动：${closed.message}`, "error");
                return;
              }
              const opened = await api.trade({
                kind: "open",
                inst_id: pos.inst_id,
                side: pos.side === "long" ? "short" : "long",
                leverage: pos.leverage,
                stop_loss: sl,
                take_profit: tp,
                reason: "一键反手：反向开仓",
              });
              if (opened.status === "executed") toast(`反手完成：${opened.message}`, "ok");
              else toast(`原仓位已平，但反向开仓没成功：${opened.message}`, "error");
            } catch (e: any) {
              toast(e.message, "error");
            } finally {
              setBusy("");
              setEdit(null);
              onDone();
            }
          }}
        />
      )}
      {edit?.mode === "add" && (
        <AddModal pos={edit.pos} stop={stopOf(edit.pos.inst_id).sl} busy={!!busy} onClose={() => setEdit(null)} onSubmit={(a) => run(a, "add").then(() => setEdit(null))} />
      )}
      {edit?.mode === "lever" && <LeverModal pos={edit.pos} max={maxLeverage} onClose={() => setEdit(null)} onSubmit={(a) => run(a, "lever").then(() => setEdit(null))} />}
    </div>
  );
}

function ProfitGuardModal({ lot, onClose }: { lot: Lot; onClose: () => void }) {
  const [rules, setRules] = useState({ arm_pct: 70, giveback_pct: 25, stall_minutes: 3, cost_pct: 0.15 });
  const [effect, setEffect] = useState<"notify" | "close">("notify");
  const [saving, setSaving] = useState(false);
  return <Modal title={`模拟盘 · 第 ${lot.id} 笔提前止盈`} onClose={onClose}>
    <p>从入场 {fmtPx(lot.entry_px)} 向止盈 {fmtPx(lot.take_profit)} 走到设定比例后开始保护。接近目标但不再推进，或峰值利润明显回吐且仍留有成本余量时触发。</p>
    <form onSubmit={async e => {
      e.preventDefault();
      setSaving(true);
      try {
        const r = await api.profitGuard(lot.id, { ...rules, on_trigger: effect });
        toast(`策略 #${r.id} 已添加；盯盘运行时生效，可在策略卡片撤销。`, "ok");
        onClose();
      } catch (err: any) { toast(err.message, "error"); } finally { setSaving(false); }
    }}>
      {([
        ["arm_pct", "开始保护：已走完目标距离（%）", 10, 99.9],
        ["giveback_pct", "触发：从峰值利润回吐（%）", 5, 80],
        ["stall_minutes", "或接近目标但不再推进（分钟）", 0.5, 240],
        ["cost_pct", "预留往返费用和滑点（入场价 %）", 0.1, 5],
      ] as const).map(([key, label, min, max]) => <label className="field" key={key}><span>{label}</span>
        <input className="input" type="number" required min={min} max={max} step="any" value={rules[key] || ""} onChange={e => setRules({ ...rules, [key]: Number(e.target.value) })} />
      </label>)}
      <label className="field"><span>触发后</span><select className="input" value={effect} onChange={e => setEffect(e.target.value === "close" ? "close" : "notify")}>
        <option value="notify">只提醒 Worker</option><option value="close">按现有权限尝试平这一笔，并通知 Worker</option>
      </select></label>
      <p className="muted small">不会启动盯盘，不替换原止损止盈；无法按保护价格整笔成交时不追价。</p>
      <button className="btn primary" disabled={saving}>{saving ? "保存中…" : "给这笔仓位添加规则"}</button>
    </form>
  </Modal>;
}

function StopsModal({ pos, current, onClose, onSubmit }: { pos: Position; current: { sl: number | null; tp: number | null }; onClose: () => void; onSubmit: (a: TradeAction) => void }) {
  const [sl, setSl] = useState<number | "">(current.sl ?? "");
  const [tp, setTp] = useState<number | "">(current.tp ?? "");
  return (
    <Modal title={`${shortInst(pos.inst_id)} 整仓止盈止损`} onClose={onClose}>
      <p className="muted small">
        {pos.side === "long" ? "多" : "空"}单均价 {fmtPx(pos.avg_px)}，现价 {fmtPx(pos.mark_px)}。止损只能收紧，放宽会被风控拒绝。
      </p>
      <StopsEditor inst={pos.inst_id} side={pos.side} entry={pos.avg_px} leverage={pos.leverage} contracts={pos.contracts} sl={sl} tp={tp} setSl={setSl} setTp={setTp} breakeven stack />
      <div className="modal-foot">
        <button className="btn" onClick={onClose}>
          取消
        </button>
        <button
          className="btn primary"
          disabled={sl === "" && tp === ""}
          onClick={() => onSubmit({ kind: "set_stops", inst_id: pos.inst_id, stop_loss: sl === "" ? undefined : sl, take_profit: tp === "" ? undefined : tp, reason: "手动调整" })}
        >
          确定
        </button>
      </div>
    </Modal>
  );
}

function AddModal({ pos, stop, busy, onClose, onSubmit }: { pos: Position; stop: number | null; busy: boolean; onClose: () => void; onSubmit: (a: TradeAction) => void }) {
  const [usd, setUsd] = useState<number | "">("");
  const [sl, setSl] = useState<number | "">(stop ?? "");
  return (
    <Modal title={`${shortInst(pos.inst_id)} 加仓`} onClose={onClose}>
      <p className="muted small">
        现在是{pos.side === "long" ? "多" : "空"}单，已投入 {fmtUsd(pos.margin)} U（{pos.leverage}x），均价 {fmtPx(pos.avg_px)}，现价 {fmtPx(pos.mark_px)}。加仓按市价、沿用现在的杠杆，这一笔单独带止损。
      </p>
      <label className="field">
        <span>再投入多少 U（保证金）</span>
        <input className="input" type="number" min={0} step="any" autoFocus value={usd} onChange={(e) => setUsd(e.target.value === "" ? "" : Number(e.target.value))} />
      </label>
      <div className="seg four">
        {[5, 10, 20, 50].map((v) => (
          <button key={v} type="button" onClick={() => setUsd(v)}>
            {v} U
          </button>
        ))}
      </div>
      <p className="muted small">加仓后整仓的止损（按现价算亏损率）：</p>
      <StopsEditor inst={pos.inst_id} side={pos.side} entry={pos.mark_px} leverage={pos.leverage} sl={sl} tp="" setSl={setSl} stack />
      {usd !== "" && <p className="muted small">名义价值约 {fmtUsd(usd * pos.leverage, 0)} U。超过风控上限时会被自动压低或拒绝。</p>}
      <div className="modal-foot">
        <button className="btn" onClick={onClose}>
          取消
        </button>
        <button
          className="btn primary"
          disabled={busy || usd === "" || usd <= 0 || sl === ""}
          onClick={() => onSubmit({ kind: "add", inst_id: pos.inst_id, side: pos.side, order_type: "market", margin_usdt: usd as number, stop_loss: sl as number, reason: "手动加仓" })}
        >
          市价加仓
        </button>
      </div>
    </Modal>
  );
}

function LeverModal({ pos, max, onClose, onSubmit }: { pos: Position; max: number; onClose: () => void; onSubmit: (a: TradeAction) => void }) {
  const [lever, setLever] = useState<number | "">(pos.leverage);
  const [instMax, setInstMax] = useState<number | null>(null);
  useEffect(() => {
    api.instrument(pos.inst_id).then((i) => setInstMax(i.max_leverage)).catch(() => undefined);
  }, [pos.inst_id]);
  return (
    <Modal title={`${shortInst(pos.inst_id)} 调整杠杆`} onClose={onClose}>
      <p className="muted small">当前 {pos.leverage}x，这个币最高 {instMax ?? "—"}x，你的风控上限 {max}x。持仓中调高杠杆会让强平价更近。</p>
      <LeverPicker value={lever} onChange={setLever} instMax={instMax ?? max} riskMax={max} />
      <div className="modal-foot">
        <button className="btn" onClick={onClose}>
          取消
        </button>
        <button className="btn primary" disabled={lever === ""} onClick={() => onSubmit({ kind: "set_leverage", inst_id: pos.inst_id, leverage: lever as number, reason: "手动调整" })}>
          确定
        </button>
      </div>
    </Modal>
  );
}

function CloseModal({ pos, busy, onClose, onSubmit }: { pos: Position; busy: boolean; onClose: () => void; onSubmit: (fraction: number) => void }) {
  const [pct, setPct] = useState(100);
  return (
    <Modal title={`${shortInst(pos.inst_id)} 市价平仓`} onClose={onClose}>
      <p className="muted small">
        {pos.side === "long" ? "多" : "空"}单，投入 {fmtUsd(pos.margin)} U（{pos.leverage}x），浮动盈亏 <Pnl value={pos.upl} /> USDT。
      </p>
      <div className="seg four">
        {[25, 50, 75, 100].map((v) => (
          <button key={v} className={pct === v ? "on" : ""} onClick={() => setPct(v)}>
            {v}%
          </button>
        ))}
      </div>
      <input type="range" min={5} max={100} step={5} value={pct} onChange={(e) => setPct(Number(e.target.value))} />
      <p className="muted small">
        平掉 {pct}% ≈ 释放保证金 {fmtUsd((pos.margin * pct) / 100)} U（数量按合约最小下单单位向下取整），预计实现盈亏约 <Pnl value={(pos.upl * pct) / 100} /> USDT。
      </p>
      <div className="modal-foot">
        <button className="btn" onClick={onClose}>
          取消
        </button>
        <button className="btn danger" disabled={busy} onClick={() => onSubmit(pct / 100)}>
          {busy ? "提交中…" : `市价平仓 ${pct}%`}
        </button>
      </div>
    </Modal>
  );
}

function ReverseModal({ pos, busy, onClose, onSubmit }: { pos: Position; busy: boolean; onClose: () => void; onSubmit: (sl: number, tp?: number) => void }) {
  const [sl, setSl] = useState<number | "">("");
  const [tp, setTp] = useState<number | "">("");
  const next = pos.side === "long" ? "空" : "多";
  return (
    <Modal title={`${shortInst(pos.inst_id)} 一键反手`} onClose={onClose}>
      <p className="muted small">
        先市价平掉现在的{pos.side === "long" ? "多" : "空"}单（投入 {fmtUsd(pos.margin)} U），再市价开{next}单，杠杆沿用 {pos.leverage}x。新仓的张数由风控按止损距离重新计算，不一定和原来相同。
      </p>
      <p className="muted small">新{next}单的止盈止损（按现价 {fmtPx(pos.mark_px)} 算）：</p>
      <StopsEditor inst={pos.inst_id} side={pos.side === "long" ? "short" : "long"} entry={pos.mark_px} leverage={pos.leverage} sl={sl} tp={tp} setSl={setSl} setTp={setTp} stack />
      <div className="modal-foot">
        <button className="btn" onClick={onClose}>
          取消
        </button>
        <button className="btn danger" disabled={busy || sl === ""} onClick={() => onSubmit(sl as number, tp === "" ? undefined : tp)}>
          {busy ? "执行中…" : `平仓并开${next}`}
        </button>
      </div>
    </Modal>
  );
}
