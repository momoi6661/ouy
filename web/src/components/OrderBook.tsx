import { useEffect, useRef, useState } from "react";
import { api, fmtCompact, fmtPx, type OrderBookLevels } from "../api";

const ROWS = 25;
const fmtSize = (v: number) => (v >= 1000 ? fmtCompact(v) : v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : String(Number(v.toFixed(2))));

/** 盘口：点任意一档价格，把它填进下单区的限价。 */
export function OrderBook({ inst, last, onPick }: { inst: string; last: number; onPick: (price: number) => void }) {
  const [book, setBook] = useState<OrderBookLevels | null>(null);
  const [error, setError] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const mid = useRef<HTMLDivElement>(null);
  const centered = useRef("");

  useEffect(() => {
    let alive = true;
    setBook(null);
    const load = async () => {
      if (document.hidden) return;
      try {
        const b = await api.orderbook(inst, ROWS);
        if (alive) {
          setBook(b);
          setError("");
        }
      } catch (e: any) {
        if (alive) setError(e.message || "盘口加载失败");
      }
    };
    load();
    const timer = setInterval(load, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [inst]);

  // 第一次拿到这个品种的盘口时，把最新价那一行滚到中间；之后用户自己滑动，不再打扰
  useEffect(() => {
    if (!book || centered.current === inst || !scroller.current || !mid.current) return;
    centered.current = inst;
    // 中间那行是吸附定位的，offsetTop 不可靠；用它上面最后一档卖单的位置来算
    const above = mid.current.previousElementSibling as HTMLElement | null;
    const top = above ? above.offsetTop + above.offsetHeight : 0;
    scroller.current.scrollTop = top - scroller.current.clientHeight / 2 + mid.current.offsetHeight / 2;
  }, [book, inst]);

  const asks = (book?.asks ?? []).slice(0, ROWS);
  const bids = (book?.bids ?? []).slice(0, ROWS);
  const biggest = Math.max(1, ...asks.map((a) => a[1]), ...bids.map((b) => b[1]));
  // 按这个品种实际的报价精度显示，否则相邻档位会显示成同一个价格
  const digits = Math.min(
    Math.max(0, ...[...asks, ...bids].map(([px]) => (String(px).split(".")[1] ?? "").length)),
    10,
  );
  const fmtLevel = (px: number) => px.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const spread = asks.length && bids.length ? ((asks[0][0] - bids[0][0]) / bids[0][0]) * 100 : null;

  // 买卖双方占比：盘口里这些档位的买盘挂单量 ÷（买盘 + 卖盘）
  const bidVol = bids.reduce((sum, [, size]) => sum + size, 0);
  const askVol = asks.reduce((sum, [, size]) => sum + size, 0);
  const buyPct = bidVol + askVol > 0 ? (bidVol / (bidVol + askVol)) * 100 : null;

  const row = (side: "ask" | "bid", [price, size]: [number, number]) => (
    <button key={`${side}${price}`} className={`ob-row ${side}`} onClick={() => onPick(price)} title="点击用这个价格下限价单">
      <i style={{ width: `${Math.min((size / biggest) * 100, 100)}%` }} />
      <span className={side === "ask" ? "down" : "up"}>{fmtLevel(price)}</span>
      <span>{fmtSize(size)}</span>
    </button>
  );

  return (
    <div className="panel orderbook">
      <div className="panel-head">
        <h3>盘口</h3>
        <span className="muted small">点价格填入</span>
      </div>
      {error && !book ? (
        <div className="muted small">{error}</div>
      ) : !book ? (
        <div className="muted small">加载盘口…</div>
      ) : (
        <>
          <div className="ob-head">
            <span>价格</span>
            <span>数量（张）</span>
          </div>
          <div className="ob-scroll" ref={scroller}>
            {[...asks].reverse().map((a) => row("ask", a))}
            <div className="ob-mid" ref={mid}>
              <b>{last ? fmtPx(last) : asks[0] ? fmtLevel(asks[0][0]) : "—"}</b>
              {spread != null && <span className="muted small spread">价差 {spread.toFixed(3)}%</span>}
              {buyPct != null && (
                <div className="ob-ratio" title="按盘口里这些档位的挂单量算：买盘和卖盘各占多少">
                  <span className="up">买 {buyPct.toFixed(0)}%</span>
                  <div className="bar">
                    <i style={{ width: `${buyPct}%` }} />
                  </div>
                  <span className="down">卖 {(100 - buyPct).toFixed(0)}%</span>
                </div>
              )}
            </div>
            {bids.map((b) => row("bid", b))}
          </div>
        </>
      )}
    </div>
  );
}
