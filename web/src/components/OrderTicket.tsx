import type React from "react";
import { useEffect, useState } from "react";
import { api, fmtPx, STATUS_LABEL, type Account, type TradeAction, type InstrumentInfo, type RiskLimits, type Style, STYLES, needLogin } from "../api";
import { LeverPicker, Num, Pnl, toast } from "./ui";

interface Props {
  inst: string;
  last: number;
  account: Account | null;
  risk: RiskLimits;
  onDone: () => void;
  loggedIn: boolean;
  /** 从盘口点选的价格；n 每点一次加一，保证同一个价格连点也生效 */
  pick?: { price: number; n: number } | null;
  /** 打开窗口时选中的方向；n 每次打开加一 */
  want?: { side: "long" | "short"; n: number } | null;
  defaultMarginMode: "cross" | "isolated";
  /** 这个币的交易风格，下单默认用它；这一笔可以单独改 */
  defaultStyle: Style;
  /** 默认止损：亏掉投入保证金的百分之多少 */
  defaultStopPct: number;
  /** Agent 替用户填好的参数；n 每次加一 */
  preset?: {
    n: number;
    side: "long" | "short";
    order_type?: "market" | "limit";
    price?: number | null;
    stop_loss?: number | null;
    take_profit?: number | null;
    margin_usdt?: number | null;
    leverage?: number | null;
    margin_mode?: "cross" | "isolated" | null;
  } | null;
}

/** 按这个币的最小价格变动取整 */
const rounder = (tickSz: string | undefined, ref: number) => {
  const tick = Number(tickSz) || 0;
  const digits = tick ? (String(tickSz).split(".")[1] ?? "").length : ref >= 100 ? 2 : 6;
  return (raw: number) => Number((tick ? Math.round(raw / tick) * tick : raw).toFixed(digits));
};

/** 止盈 / 止损的一块，和欧易一样：触发价和收益率两个框，下面是滑动条和快捷按钮。pct 是相对投入保证金的百分比，价格距离 = 百分比 ÷ 杠杆。 */
function TpSl(props: {
  kind: "tp" | "sl";
  price: number | "";
  pct: number | "";
  onPrice: (v: number | "") => void;
  onPct: (v: number | "") => void;
  max: number;
  presets: number[];
  placeholder: string;
  est: string;
  required?: boolean;
  extra?: React.ReactNode;
}) {
  const { kind, price, pct, onPrice, onPct, max, presets } = props;
  const sign = kind === "tp" ? "+" : "-";
  return (
    <div className={`tpsl ${kind}`}>
      <div className="tpsl-head">
        <b>
          {kind === "tp" ? "止盈" : "止损"}
          {props.required && <i>必填</i>}
        </b>
        <span>{props.est}</span>
      </div>
      <div className="tpsl-inputs">
        <Num value={price} onChange={onPrice} placeholder={props.placeholder} suffix="触发价" />
        <Num value={pct} onChange={onPct} min={0} max={max} step={0.5} placeholder={kind === "tp" ? "收益率" : "亏损率"} suffix="%" />
      </div>
      <div className="tpsl-slider">
        <input type="range" min={0} max={max} step={0.5} value={pct === "" ? 0 : Math.min(pct, max)} onChange={(e) => onPct(Number(e.target.value))} />
        <span>{pct === "" ? "—" : `${sign}${pct}%`}</span>
      </div>
      <div className="tpsl-chips">
        {presets.map((v) => (
          <button key={v} type="button" className={pct === v ? "on" : ""} onClick={() => onPct(v)}>
            {sign}
            {v}%
          </button>
        ))}
        {props.extra}
      </div>
    </div>
  );
}

export function OrderTicket({ inst, last: pushed, account, risk, onDone, loggedIn, pick, defaultMarginMode, defaultStopPct, defaultStyle, preset }: Props) {
  // 盯盘没开时没有推送的现价：自己每 5 秒查一次
  const [polled, setPolled] = useState(0);
  useEffect(() => {
    setPolled(0);
    if (pushed) return;
    let alive = true;
    const load = () =>
      api
        .ticker(inst)
        .then((t) => alive && setPolled(t.last))
        .catch(() => undefined);
    load();
    const timer = window.setInterval(load, 5000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [inst, !pushed]);
  const last = pushed || polled;
  const [info, setInfo] = useState<InstrumentInfo | null>(null);
  const [type, setType] = useState<"market" | "limit">("limit");
  const [price, setPrice] = useState<number | "">("");
  // 止损：默认按「亏掉投入的百分之多少」算，开多开空各算各的；手动填了价格就用这个价格
  const [slPrice, setSlPrice] = useState<number | "">("");
  const [slPct, setSlPct] = useState(defaultStopPct);
  // 止盈：可以不设；设的话同样按「赚到投入的百分之多少」算，或者直接填价格
  const [tpPrice, setTpPrice] = useState<number | "">("");
  const [tpPct, setTpPct] = useState<number | "">("");
  const [lever, setLever] = useState<number | "">("");
  const [leverOpen, setLeverOpen] = useState(false);
  const [riskPct, setRiskPct] = useState<number | "">("");
  const [amount, setAmount] = useState<number | "">("");
  const [mgn, setMgn] = useState<"cross" | "isolated">(defaultMarginMode);
  const [busy, setBusy] = useState<"" | "long" | "short">("");
  const [style, setStyle] = useState<Style>(defaultStyle);
  useEffect(() => setStyle(defaultStyle), [inst, defaultStyle]);
  useEffect(() => setSlPct(defaultStopPct), [inst, defaultStopPct]);

  useEffect(() => {
    let alive = true;
    setInfo(null);
    setSlPrice("");
    setTpPrice("");
    setTpPct("");
    setPrice("");
    api
      .instrument(inst)
      .then((i) => {
        if (!alive) return;
        setInfo(i);
        setLever(i.leverage ? Math.min(i.leverage, risk.max_leverage) : "");
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inst]);

  useEffect(() => {
    if (!pick) return;
    setType("limit");
    setPrice(pick.price);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pick?.n]);

  useEffect(() => {
    if (!preset) return;
    setType(preset.order_type ?? "market");
    setPrice(preset.price ?? "");
    setSlPrice(preset.stop_loss ?? "");
    setTpPrice(preset.take_profit ?? "");
    setAmount(preset.margin_usdt ?? "");
    if (preset.leverage) setLever(preset.leverage);
    if (preset.margin_mode) setMgn(preset.margin_mode);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preset?.n]);

  const pos = account?.positions.find((p) => p.inst_id === inst);
  const entry = type === "limit" && price !== "" ? price : last;
  const levNow = lever === "" ? pos?.leverage || info?.leverage || 1 : lever;
  const round = rounder(info?.tick_sz, entry);
  // 亏掉投入保证金的百分之多少 → 止损价。价格距离 = 百分比 ÷ 杠杆
  const slFor = (s: "long" | "short"): number | "" => {
    if (slPrice !== "") return slPrice;
    if (!entry || !levNow) return "";
    const d = slPct / 100 / levNow;
    return round(s === "long" ? entry * (1 - d) : entry * (1 + d));
  };
  const tpFor = (s: "long" | "short"): number | "" => {
    if (tpPrice !== "") return tpPrice;
    if (tpPct === "" || !entry || !levNow) return "";
    const d = tpPct / 100 / levNow;
    return round(s === "long" ? entry * (1 + d) : entry * (1 - d));
  };
  // 手动填的止损 / 止盈在入场价哪一边，决定了这一单只能开多还是只能开空
  const why = (s: "long" | "short"): string => {
    if (!entry) return "还没有拿到现价";
    if (slPrice !== "" && (s === "long" ? slPrice >= entry : slPrice <= entry)) return s === "long" ? "止损价要低于入场价才能开多" : "止损价要高于入场价才能开空";
    if (tpPrice !== "" && (s === "long" ? tpPrice <= entry : tpPrice >= entry)) return s === "long" ? "止盈价要高于入场价才能开多" : "止盈价要低于入场价才能开空";
    const stop = slFor(s);
    const gap = stop !== "" ? (Math.abs(entry - stop) / entry) * 100 : 0;
    if (gap && gap < 0.1) return `止损离入场只有 ${gap.toFixed(3)}%，至少要 0.1%（${levNow}x 下亏损率至少 ${Math.ceil(0.1 * levNow)}%）`;
    if (gap * levNow > 70) return `止损 ${gap.toFixed(2)}% 配 ${levNow}x，没到止损就先强平：杠杆降到 ${Math.max(Math.floor(70 / gap), 1)}x 以下`;
    return "";
  };
  const dist = slPrice !== "" && entry ? Math.abs(entry - slPrice) / entry : levNow ? slPct / 100 / levNow : 0;
  const pctShown = slPrice !== "" && entry ? Math.min(Math.round(dist * levNow * 1000) / 10, 100) : slPct;
  const tpShown: number | "" = tpPrice !== "" && entry ? Math.round((Math.abs(tpPrice - entry) / entry) * levNow * 1000) / 10 : tpPct;
  const pct = riskPct === "" ? risk.risk_per_trade_pct : Math.min(riskPct, risk.risk_per_trade_pct);
  const riskUsd = account ? (account.equity * pct) / 100 : 0;
  const byRisk = info && dist > 0 && entry ? riskUsd / (dist * entry * info.ct_val) : 0;
  const byAmount = info && amount !== "" && entry ? (amount * levNow) / (info.ct_val * entry) : 0;
  const capped = riskPct !== "" && byAmount > 0 && byRisk > 0 && byAmount > byRisk;
  const estContracts = byAmount > 0 ? (capped ? byRisk : byAmount) : byRisk;
  const estNotional = info ? estContracts * info.ct_val * entry : 0;
  const estMargin = levNow ? estNotional / levNow : 0;
  const estLoss = info ? estContracts * dist * entry * info.ct_val : 0;
  useEffect(() => {
    // 限价单默认填上现价，和交易所一样；之后不跟着跳，要更新点「最新」
    if (type === "limit" && price === "" && info && last) setPrice(round(last));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inst, info, !!last]);
  const maxAmount = account ? account.available * 0.95 : 0;
  const amountPct = amount !== "" && maxAmount > 0 ? Math.min(Math.round((amount / maxAmount) * 100), 100) : 0;
  const setAmountPct = (v: number) => setAmount(v === 0 ? "" : Number(((maxAmount * v) / 100).toFixed(2)));
  const effMgn = pos ? (pos.margin_mode as "cross" | "isolated") : mgn;

  const submit = async (s: "long" | "short") => {
    const side = pos ? pos.side : s;
    const bad = why(side);
    if (bad) return toast(bad, "error");
    const sl = slFor(side);
    if (sl === "") return toast("必须有止损价，没有止损不下单", "error");
    if (type === "limit" && price === "") return toast("限价单要填价格", "error");
    setBusy(side);
    try {
      const r = await api.trade({
        kind: pos ? "add" : "open",
        inst_id: inst,
        side,
        order_type: type,
        price: type === "limit" && price !== "" ? price : undefined,
        stop_loss: sl,
        take_profit: tpFor(side) === "" ? undefined : (tpFor(side) as number),
        leverage: lever === "" ? undefined : lever,
        risk_pct: riskPct === "" ? undefined : riskPct,
        margin_usdt: amount === "" ? undefined : amount,
        margin_mode: pos ? undefined : mgn,
        style,
        reason: "手动下单",
      });
      toast(r.detail?.unfilled ? r.message : `${STATUS_LABEL[r.status]}：${r.message}`, r.status === "executed" && !r.detail?.unfilled ? "ok" : "error");
      if (r.worker_note) window.setTimeout(() => toast(r.worker_note!, "ok"), 600);
      onDone();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy("");
    }
  };

  const sideButton = (s: "long" | "short", label: string) => {
    const bad = why(s);
    const sl = slFor(s);
    const tp = tpFor(s);
    return (
      <button className={`btn ${s === "long" ? "buy" : "sell"}`} disabled={!!busy || !account || !!bad} title={bad || undefined} onClick={() => submit(s)}>
        <b>{busy === s ? "提交中…" : label}</b>
        <small>{bad ? bad : sl !== "" ? `止损 ${fmtPx(sl)}${tp !== "" ? ` · 止盈 ${fmtPx(tp)}` : ""}` : ""}</small>
      </button>
    );
  };

  return (
    <div className="ticket tk">
      <div className="tk-col">
        <div className="tk-top">
          <div className="seg two slim">
            <button className={effMgn === "cross" ? "on" : ""} disabled={!!pos} onClick={() => setMgn("cross")} title="全仓：账户里的可用资金都给这个仓位做保证金">
              全仓
            </button>
            <button className={effMgn === "isolated" ? "on" : ""} disabled={!!pos} onClick={() => setMgn("isolated")} title="逐仓：只用这笔单投入的保证金，亏完为止">
              逐仓
            </button>
          </div>
          <button
            className={`tk-lev ${leverOpen ? "on" : ""}`}
            onClick={() => setLeverOpen((v) => !v)}
            title={info ? `这个币最高 ${info.max_leverage}x，你的风控上限 ${risk.max_leverage}x` : undefined}
          >
            {levNow}x <i>▾</i>
          </button>
        </div>
        {leverOpen && (
          <LeverPicker
            value={lever}
            onChange={(v) => {
              setLever(v);
              setLeverOpen(false);
            }}
            instMax={info?.max_leverage ?? risk.max_leverage}
            riskMax={risk.max_leverage}
          />
        )}
        <div className="tk-tabs">
          <button
            className={type === "limit" ? "on" : ""}
            onClick={() => {
              setType("limit");
              if (price === "" && last) setPrice(round(last));
            }}
          >
            限价
          </button>
          <button className={type === "market" ? "on" : ""} onClick={() => setType("market")}>
            市价
          </button>
        </div>
        <label className="field">
          <span>价格（USDT）</span>
          {type === "limit" ? (
            <span className="tk-price">
              <Num value={price} onChange={setPrice} placeholder={fmtPx(last)} />
              <button type="button" onClick={() => last && setPrice(round(last))} title="填入最新成交价">
                最新
              </button>
            </span>
          ) : (
            <span className="num tk-market">按市价成交 · 现价 {fmtPx(last)}</span>
          )}
        </label>
        <label className="field">
          <span>
            投入（保证金）
            {account && <em className="muted"> 可用 {account.available.toFixed(2)} U</em>}
          </span>
          <Num value={amount} onChange={setAmount} min={0} placeholder="不填则按风险额度自动算" suffix="U" />
        </label>
        {account && (
          <>
            <div className="amount-slider">
              <input type="range" min={0} max={100} step={1} value={amountPct} onChange={(e) => setAmountPct(Number(e.target.value))} title="投入可用资金的百分之多少" />
              <span>{amountPct}%</span>
            </div>
            <div className="tk-pcts">
              {[25, 50, 75, 100].map((v) => (
                <button key={v} type="button" className={amountPct === v ? "on" : ""} onClick={() => setAmountPct(v)}>
                  {v}%
                </button>
              ))}
            </div>
          </>
        )}
      </div>
      <div className="tk-col">
        <div className="tk-sec">止盈止损</div>
        <TpSl
          kind="tp"
          price={tpPrice}
          pct={tpShown}
          onPrice={setTpPrice}
          onPct={(v) => {
            setTpPrice("");
            setTpPct(v === "" || v <= 0 ? "" : v);
          }}
          max={300}
          presets={[25, 50, 100, 200]}
          placeholder={tpPct === "" ? "可不设" : pos ? String(tpFor(pos.side)) : `多 ${tpFor("long")} / 空 ${tpFor("short")}`}
          est={tpShown !== "" && estMargin > 0 ? `预计赚 +${((estMargin * tpShown) / 100).toFixed(2)} U` : "不设止盈"}
        />
        <TpSl
          kind="sl"
          required
          price={slPrice}
          pct={pctShown}
          onPrice={setSlPrice}
          onPct={(v) => {
            setSlPrice("");
            setSlPct(v === "" ? defaultStopPct : Math.max(v, 0.5));
          }}
          max={100}
          presets={[5, 10, 20, 50]}
          placeholder={pos ? String(slFor(pos.side)) : `多 ${slFor("long")} / 空 ${slFor("short")}`}
          est={estLoss > 0 ? `预计亏 -${estLoss.toFixed(2)} U` : slPrice === "" ? `默认 -${slPct}%，下单后 Worker 会再调` : ""}
        />
        <div className="est">
          {info && estContracts > 0 ? (
            <>
              预计 {estContracts.toFixed(2)} 张 ≈ {estNotional.toFixed(0)} U 名义价值，占用保证金约 {estMargin.toFixed(2)} U
              <br />
              止损距离 {(dist * 100).toFixed(2)}% · 打到止损最多亏约 {estLoss.toFixed(2)} U
              {capped && (
                <>
                  <br />
                  <span className="warn">投入金额超过了单仓风险上限，数量会按上限缩小</span>
                </>
              )}
            </>
          ) : (
            "填了投入就按「投入 × 杠杆」下单；不填则按风险额度自动算。两种都不会超过风控上限。"
          )}
        </div>
        <details className="tk-more">
          <summary>更多设置</summary>
          <label className="field inline">
            <span title="这一笔的交易风格。默认用这个币的风格；Agent 会按这一笔自己的风格来管理它（看的周期、止损宽度、拿多久）">这一笔的风格</span>
            <select value={style} onChange={(e) => setStyle(e.target.value as Style)}>
              {STYLES.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                  {x.id === defaultStyle ? "（这个币的默认）" : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>风险额度（每笔最多亏权益的百分之多少）</span>
            <Num value={riskPct} onChange={setRiskPct} min={0.1} max={risk.risk_per_trade_pct} step={0.1} placeholder={String(risk.risk_per_trade_pct)} suffix="%" />
          </label>
        </details>
        {!loggedIn ? (
          <button className="btn block primary" onClick={needLogin}>
            登录欧易后下单
          </button>
        ) : pos ? (
          <>
            <div className="muted small">
              已有{pos.side === "long" ? "多" : "空"}
              单，这里提交的是加仓；止损是加仓后整仓的新止损。
            </div>
            <div className="tk-buttons one">{sideButton(pos.side, pos.side === "long" ? "加仓（多）" : "加仓（空）")}</div>
          </>
        ) : (
          <div className="tk-buttons">
            {sideButton("long", "开多")}
            {sideButton("short", "开空")}
          </div>
        )}
      </div>
    </div>
  );
}

/** 下单页上面的「当前仓位」：这个币的仓位、止损止盈、挂单，下完单就在这里调，不用换页。 */
export function PositionCard({ inst, last, account, onDone, loggedIn, worker }: { inst: string; last: number; account: Account | null; onDone: () => void; loggedIn: boolean; worker: { ok: boolean; text: string } }) {
  const pos = account?.positions.find((p) => p.inst_id === inst);
  const orders = (account?.orders ?? []).filter((o) => o.inst_id === inst);
  const stops = (account?.protections ?? []).filter((s) => s.inst_id === inst);
  const slNow = stops.find((s) => s.stop_loss)?.stop_loss ?? null;
  const tpNow = stops.find((s) => s.take_profit)?.take_profit ?? null;
  const [sl, setSl] = useState<number | "">("");
  const [tp, setTp] = useState<number | "">("");
  const [busy, setBusy] = useState("");
  useEffect(() => {
    // 交易所上的止损止盈变了（下单、Worker 调整）就跟着更新；你正在改的不覆盖
    setSl(slNow ?? "");
    setTp(tpNow ?? "");
  }, [inst, slNow, tpNow, !!pos]);

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
  const px = last || pos?.mark_px || 0;
  const changed = (sl === "" ? null : sl) !== slNow || (tp === "" ? null : tp) !== tpNow;

  return (
    <div className="pc">
      <div className="pc-head">
        <b>当前仓位</b>
        <span className={`pc-worker ${worker.ok ? "ok" : "off"}`}>{worker.text}</span>
      </div>
      {!pos && <p className="muted pc-empty">这个币现在没有仓位。下单成交后，仓位和止损显示在这里，可以直接调。</p>}
      {pos && (
        <>
          <div className="pc-stats">
            <span className={`pc-side ${pos.side}`}>
              {pos.side === "long" ? "多" : "空"} {pos.leverage}x {pos.margin_mode === "isolated" ? "逐仓" : "全仓"}
            </span>
            <div>
              <em>浮动盈亏</em>
              <b>
                <Pnl value={pos.upl} suffix=" U" /> <small className={pos.upl >= 0 ? "up" : "down"}>{(pos.upl_ratio * 100).toFixed(1)}%</small>
              </b>
            </div>
            <div>
              <em>均价 / 现价</em>
              <b>
                {fmtPx(pos.avg_px)} / {fmtPx(px)}
              </b>
            </div>
            <div>
              <em>数量 / 保证金</em>
              <b>
                {pos.contracts} 张 / {pos.margin.toFixed(2)} U
              </b>
            </div>
            <div>
              <em>强平价</em>
              <b>{pos.liq_px ? fmtPx(pos.liq_px) : "—"}</b>
            </div>
          </div>
          {slNow == null && <div className="pc-warn">这笔仓位现在没有止损。在下面填一个并保存。</div>}
          <StopsEditor inst={inst} side={pos.side} entry={pos.avg_px} leverage={pos.leverage} contracts={pos.contracts} sl={sl} tp={tp} setSl={setSl} setTp={setTp} breakeven />
          <div className="pc-save">
            <button
              className="btn primary"
              disabled={!loggedIn || !!busy || !changed || sl === ""}
              title={sl === "" ? "止损不能为空" : undefined}
              onClick={() => run({ kind: "set_stops", inst_id: inst, stop_loss: sl === "" ? undefined : sl, take_profit: tp === "" ? undefined : tp, reason: "手动调整止损止盈" }, "stops")}
            >
              {busy === "stops" ? "保存中…" : changed ? "保存止损止盈" : "止损止盈已是最新"}
            </button>
            {changed && (
              <button
                className="btn"
                disabled={!!busy}
                onClick={() => {
                  setSl(slNow ?? "");
                  setTp(tpNow ?? "");
                }}
              >
                还原
              </button>
            )}
          </div>
          <div className="pc-acts">
            <span className="muted small">减仓</span>
            {[25, 50, 75].map((v) => (
              <button key={v} className="btn xs" disabled={!loggedIn || !!busy} onClick={() => run({ kind: "reduce", inst_id: inst, fraction: v / 100, reason: `手动减仓 ${v}%` }, `r${v}`)}>
                {v}%
              </button>
            ))}
            <button className="btn xs danger" disabled={!loggedIn || !!busy} onClick={() => run({ kind: "close", inst_id: inst, reason: "手动平仓" }, "close")}>
              全部平仓
            </button>
          </div>
        </>
      )}
      {orders.length > 0 && (
        <div className="pc-orders">
          <span className="muted small">挂单</span>
          {orders.map((o) => (
            <span key={o.ord_id} className="pc-order">
              {o.side === "buy" ? "买" : "卖"} {o.px ? fmtPx(o.px) : "市价"} · {o.sz} 张
            </span>
          ))}
          <button className="btn xs" disabled={!loggedIn || !!busy} onClick={() => run({ kind: "cancel_orders", inst_id: inst, reason: "手动撤单" }, "cancel")}>
            全部撤单
          </button>
        </div>
      )}
    </div>
  );
}

/** 方向已定的一笔仓位（或马上要开的仓）的止盈、止损两块。entry 是参照价（均价，或新仓的现价），百分比按投入保证金算。 */
export function StopsEditor(props: {
  inst: string;
  side: "long" | "short";
  entry: number;
  leverage: number;
  /** 张数：给了就显示到这里赚 / 亏多少 U */
  contracts?: number;
  sl: number | "";
  tp: number | "";
  setSl: (v: number | "") => void;
  setTp?: (v: number | "") => void;
  /** 止损快捷里加一个「保本」（移到均价） */
  breakeven?: boolean;
  stack?: boolean;
}) {
  const { inst, side, entry, leverage, sl, tp, setSl, setTp } = props;
  const [info, setInfo] = useState<InstrumentInfo | null>(null);
  useEffect(() => {
    let alive = true;
    setInfo(null);
    api.instrument(inst).then(
      (i) => alive && setInfo(i),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [inst]);
  const round = rounder(info?.tick_sz, entry);
  const dir = side === "short" ? -1 : 1;
  // 价格 ⇄ 相对投入保证金的百分比（正 = 赚）
  const roe = (v: number | "") => (v !== "" && entry ? Math.round((((v - entry) * dir) / entry) * leverage * 1000) / 10 : null);
  const at = (pct: number) => round(entry * (1 + (dir * pct) / 100 / leverage));
  const usd = (v: number | "") => (props.contracts && info && v !== "" ? (v - entry) * dir * props.contracts * info.ct_val : null);
  const say = (v: number | "", none: string) => {
    const u = usd(v);
    const r = roe(v);
    if (v === "" || r == null) return none;
    return u == null ? `到这里${r >= 0 ? "赚" : "亏"} ${Math.abs(r)}%` : `到这里${u >= 0 ? "赚" : "亏"} ${u >= 0 ? "+" : "-"}${Math.abs(u).toFixed(2)} U`;
  };
  const slRoe = roe(sl);
  const tpRoe = roe(tp);
  return (
    <div className={`pc-tpsl ${props.stack ? "stack" : ""}`}>
      {setTp && (
        <TpSl
          kind="tp"
          price={tp}
          pct={tpRoe != null && tpRoe > 0 ? tpRoe : ""}
          onPrice={setTp}
          onPct={(v) => setTp(v === "" || v <= 0 ? "" : at(v))}
          max={300}
          presets={[25, 50, 100, 200]}
          placeholder="可不设"
          est={say(tp, "没有止盈")}
        />
      )}
      <TpSl
        kind="sl"
        required
        price={sl}
        pct={slRoe != null && slRoe < 0 ? -slRoe : ""}
        onPrice={setSl}
        onPct={(v) => v !== "" && v > 0 && setSl(at(-v))}
        max={100}
        presets={[5, 10, 20, 50]}
        placeholder={side === "long" ? "低于现价" : "高于现价"}
        est={slRoe != null && slRoe >= 0 ? `已锁定盈利，${say(sl, "")}` : say(sl, "没有止损")}
        extra={
          props.breakeven ? (
            <button type="button" title="止损移到开仓均价：之后最多不亏" onClick={() => setSl(round(entry))}>
              保本
            </button>
          ) : undefined
        }
      />
    </div>
  );
}
