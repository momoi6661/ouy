import { useEffect, useMemo, useState } from "react";
import { api, fmtCompact, fmtPx, shortInst, type Ticker } from "../api";
import { Empty, useInterval } from "./ui";

interface Props {
  selected: string;
  watch: string[];
  ticks: Record<string, number>;
  onSelect: (inst: string) => void;
  onToggleWatch: (inst: string) => void;
}

export function MarketList({ selected, watch, ticks, onSelect, onToggleWatch }: Props) {
  const [rows, setRows] = useState<Ticker[]>([]);
  const [query, setQuery] = useState("");
  const [only, setOnly] = useState(false);
  const [error, setError] = useState("");

  const load = async () => {
    if (document.hidden) return;
    try {
      setRows(await api.tickers("SWAP"));
      setError("");
    } catch (e: any) {
      setError(e.message);
    }
  };
  useEffect(() => {
    load();
  }, []);
  useInterval(load, 5000);

  const shown = useMemo(() => {
    const q = query.trim().toUpperCase();
    const list = rows.filter((r) => (!q || r.inst_id.includes(q)) && (!only || watch.includes(r.inst_id)));
    // 盯盘中的排最前，其余按成交额
    return list.sort((a, b) => Number(watch.includes(b.inst_id)) - Number(watch.includes(a.inst_id)) || b.turnover_usd - a.turnover_usd).slice(0, 120);
  }, [rows, query, only, watch]);

  return (
    <div className="panel market">
      <div className="panel-head">
        <input className="input sm" placeholder="搜索币种" value={query} onChange={(e) => setQuery(e.target.value)} />
        <button className={`chip ${only ? "on" : ""}`} onClick={() => setOnly(!only)} title="只看 Agent 盯盘的品种">
          盯盘中
        </button>
      </div>
      <div className="market-cols">
        <span>永续合约 / 最高杠杆</span>
        <span>最新价</span>
        <span>24h</span>
      </div>
      <div className="market-rows">
        {error && <div className="alert error">{error}</div>}
        {!error && !shown.length && <Empty>{rows.length ? "没有匹配的币种" : "加载行情…"}</Empty>}
        {shown.map((r) => {
          const last = ticks[r.inst_id] ?? r.last;
          const watching = watch.includes(r.inst_id);
          return (
            <div key={r.inst_id} className={`market-row ${r.inst_id === selected ? "active" : ""}`} onClick={() => onSelect(r.inst_id)}>
              <span className="sym">
                <button
                  className={`star ${watching ? "on" : ""}`}
                  title={watching ? "移出盯盘列表" : "加入盯盘列表，交给 Agent"}
                  onClick={(e) => {
                    e.stopPropagation();
                    onToggleWatch(r.inst_id);
                  }}
                >
                  {watching ? "★" : "☆"}
                </button>
                <span>
                  <b>{shortInst(r.inst_id)}</b>
                  <em>
                    {r.max_leverage ? `${r.max_leverage}x` : ""} · {fmtCompact(r.turnover_usd)}
                  </em>
                </span>
              </span>
              <span className={r.change_pct >= 0 ? "up" : "down"}>{fmtPx(last)}</span>
              <span className={`pct ${r.change_pct >= 0 ? "up-bg" : "down-bg"}`}>
                {r.change_pct >= 0 ? "+" : ""}
                {r.change_pct.toFixed(2)}%
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
