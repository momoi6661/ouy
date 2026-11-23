import { useState } from "react";
import { api } from "../api";
import { RECORD_DATA } from "./Recorders";

type When = "price_touch" | "close_above" | "close_below" | "data_above" | "data_below";
type Do = "wake" | "cancel_orders" | "close" | "open";

const num = (v: string) => (v.trim() === "" || Number.isNaN(Number(v)) ? undefined : Number(v));

/** 自己划一条触发线：到了什么条件 → 做什么。一句话就是一条规则，不用懂别的参数。 */
export function LineForm({ coin, last, lines = [], busy, run }: { coin: string; last?: number; lines?: string[]; busy: boolean; run: (fn: () => Promise<unknown>, ok: string) => void }) {
  const [when, setWhen] = useState<When>("price_touch");
  const [level, setLevel] = useState("");
  const [bar, setBar] = useState("15m");
  const [data, setData] = useState("turn_stage");
  const [todo, setTodo] = useState<Do>("wake");
  const [side, setSide] = useState<"long" | "short">("long");
  const [stop, setStop] = useState("");
  const [target, setTarget] = useState("");
  const price = num(level);
  const reading = when.startsWith("data"); // 条件是一项算法读数：数值可以是 0 或负数，反复生效，不能直接开仓
  const opening = todo === "open" && !reading;
  const sl = num(stop);
  // 止损放错边的话后端也会拒绝，这里先提示，免得白点一次
  const wrongSide = opening && price !== undefined && sl !== undefined && (side === "long") !== sl < price;
  const ready = price !== undefined && (reading || price > 0) && (!opening || (sl !== undefined && !wrongSide));
  return (
    <div className="line-new">
      <span className="muted small">加一条：</span>
      <select aria-label="条件" value={when} onChange={(e) => setWhen(e.target.value as When)}>
        <option value="price_touch">价格到</option>
        <option value="close_above">K 线收盘站上</option>
        <option value="close_below">K 线收盘跌破</option>
        <option value="data_above">算法读数 ≥</option>
        <option value="data_below">算法读数 ≤</option>
      </select>
      {reading && (
        <select aria-label="哪一项读数" value={data} title={RECORD_DATA.find(([id]) => id === data)?.[2]} onChange={(e) => setData(e.target.value)}>
          {RECORD_DATA.map(([id, label]) => (
            <option key={id} value={id}>{label}</option>
          ))}
          {lines.map((name) => (
            <option key={name} value={`dist:${name}`}>现价高出「{name}」%</option>
          ))}
        </select>
      )}
      <input aria-label={reading ? "数值" : "价位"} value={level} inputMode="decimal" placeholder={reading ? "数值" : last ? String(last) : "价位"} onChange={(e) => setLevel(e.target.value)} />
      {!reading && when !== "price_touch" && (
        <select aria-label="K 线周期" value={bar} onChange={(e) => setBar(e.target.value)}>
          <option value="5m">5 分钟线</option>
          <option value="15m">15 分钟线</option>
          <option value="1H">1 小时线</option>
        </select>
      )}
      <span className="muted small">就</span>
      <select aria-label="到了做什么" value={reading && todo === "open" ? "wake" : todo} onChange={(e) => setTodo(e.target.value as Do)}>
        <option value="wake">叫 Worker 来看</option>
        <option value="cancel_orders">撤掉这个币的挂单</option>
        <option value="close">平掉这个币的仓位</option>
        {!reading && <option value="open">市价开仓</option>}
      </select>
      {opening && (
        <>
          <select aria-label="方向" value={side} onChange={(e) => setSide(e.target.value as "long" | "short")}>
            <option value="long">做多</option>
            <option value="short">做空</option>
          </select>
          <input aria-label="止损价" value={stop} inputMode="decimal" placeholder="止损价（必填）" onChange={(e) => setStop(e.target.value)} />
          <input aria-label="止盈价" value={target} inputMode="decimal" placeholder="止盈价（可不填）" onChange={(e) => setTarget(e.target.value)} />
        </>
      )}
      <button
        className="btn xs primary"
        disabled={busy || !ready}
        title={wrongSide ? "止损放错边了：做多的止损要低于这个价位，做空的要高于" : reading ? "反复生效：读数每次从没到变成到了就执行一次，两次之间至少隔 15 分钟；不要了点撤销" : "只执行一次；12 小时没到就自动作废。仓位大小、杠杆按你的风控设置算"}
        onClick={() =>
          run(async () => {
            await api.addLine({
              inst_id: coin, when, level: price!, bar: reading || when === "price_touch" ? undefined : bar, do: reading && todo === "open" ? "wake" : todo,
              ...(reading ? { data } : {}),
              ...(opening ? { side, stop_loss: sl, take_profit: num(target) } : {}),
            });
            setLevel("");
            setStop("");
            setTarget("");
          }, "触发线已加上")
        }
      >
        加上
      </button>
      {wrongSide && <span className="small" style={{ color: "var(--red)" }}>止损放错边了</span>}
    </div>
  );
}
