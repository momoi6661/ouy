import { useEffect, useRef, useState } from "react";
import { ColorType, CrosshairMode, LineStyle, createChart, type IChartApi, type IPriceLine, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { api, type Candle, type Lot, type Position } from "../api";

/** 一笔仓在图上的入场点 */
export interface ChartEntry {
  ts: number; // 入场时间（毫秒）
  price: number;
  side: "long" | "short";
  label: string;
}

/** 当前持仓：从什么时候、什么价格拿到现在。图上把入场价到现价之间这一段涂上颜色（赚绿亏红） */
export interface ChartPosition {
  since: number;
  price: number;
  side: "long" | "short";
}

/** 取这个币还开着的每一笔仓，变成图上的入场点；持仓变了就重新取 */
export function useLotEntries(instId: string, pos: Position | undefined): { entries: ChartEntry[]; position: ChartPosition | null } {
  const [lots, setLots] = useState<Lot[]>([]);
  const key = pos ? `${pos.side}:${pos.contracts}:${pos.avg_px}` : "";
  useEffect(() => {
    let alive = true;
    if (!key) {
      setLots([]);
      return;
    }
    api.lots(instId).then(
      (rows) => alive && setLots(rows.filter((l) => l.inst_id === instId && l.status === "open")),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [instId, key]);
  if (!pos) return { entries: [], position: null };
  const entries = lots.map((l) => ({ ts: l.ts, price: l.entry_px, side: l.side, label: `${l.side === "long" ? "买入" : "卖出开空"} ${l.entry_px}` }));
  const since = lots.length ? Math.min(...lots.map((l) => l.ts)) : 0;
  return { entries, position: since ? { since, price: pos.avg_px, side: pos.side } : null };
}

export interface ChartLine {
  price: number;
  color: string;
  title: string;
  dashed?: boolean;
  /** 线宽：持仓自己的线画粗一点，和参考线分开 */
  width?: 1 | 2;
  /** 设了就可以在图上拖动这条线：sl = 止损，tp = 止盈 */
  drag?: "sl" | "tp";
  /** 我们加的线属于哪一类（plan / watch / trigger），可以在图上方按类开关；欧易自己的线不填 */
  group?: string;
}

interface Props {
  instId: string;
  bar: string;
  lines: ChartLine[];
  tick?: number;
  redUp: boolean;
  onLast?: (price: number) => void;
  onDragLine?: (kind: "sl" | "tp", price: number) => void;
  /** 在某个时间点（毫秒）的 K 线上打一个标记，并把图滚到那里 */
  mark?: { ts: number; text: string } | null;
  /** 算法识别出的结构转折 K 线标记（不滚动视口） */
  shiftMark?: { ts: number; side: "long" | "short"; text: string } | null;
  /** env = 当前环境的行情（模拟盘就是模拟盘自己的盘口）；live = 真实市场 */
  src?: "env" | "live";
  /** 每笔仓的入场点：在入场那根 K 线上画箭头 */
  entries?: ChartEntry[];
  /** 算法触发记录：在触发那根 K 线上画圆点，同一根上的几条并成一个 */
  algoMarks?: AlgoMark[];
  /** 当前持仓：从入场那根 K 线到现在，入场价和价格走势之间涂色 */
  position?: ChartPosition | null;
}

export interface AlgoMark { ts: number; side: string; label: string; ok?: string }
const BAR_SECONDS: Record<string, number> = { "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1H": 3600, "2H": 7200, "4H": 14400, "6H": 21600, "12H": 43200, "1D": 86400, "1W": 604800 };

const TZ_SHIFT = -new Date().getTimezoneOffset() * 60;
const GREEN = "#0ecb81";
const RED = "#f6465d";
const ALGO_COLOR: Record<string, string> = { right: GREEN, wrong: RED };

function precisionOf(price: number): { precision: number; minMove: number } {
  const a = Math.abs(price);
  const precision = a >= 1000 ? 1 : a >= 100 ? 2 : a >= 1 ? 3 : a >= 0.01 ? 5 : 8;
  return { precision, minMove: 1 / 10 ** precision };
}

export function Chart({ instId, bar, lines, tick, redUp, onLast, onDragLine, mark, shiftMark, src = "env", entries, position, algoMarks }: Props) {
  const rows = useRef<Candle[]>([]);
  const band = useRef<ISeriesApi<"Baseline"> | null>(null);
  const bandFrom = useRef(0);
  const firstTime = useRef(0);
  const report = useRef(onLast);
  report.current = onLast;
  const dragCb = useRef(onDragLine);
  dragCb.current = onDragLine;
  const lineDefs = useRef<ChartLine[]>([]);
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volume = useRef<ISeriesApi<"Histogram"> | null>(null);
  const priceLines = useRef<IPriceLine[]>([]);
  const last = useRef<Candle | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const up = redUp ? RED : GREEN;
  const down = redUp ? GREEN : RED;

  useEffect(() => {
    if (!box.current) return;
    const c = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#848e9c", fontSize: 11 },
      grid: { vertLines: { color: "rgba(255,255,255,0.04)" }, horzLines: { color: "rgba(255,255,255,0.04)" } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "rgba(255,255,255,0.08)", scaleMargins: { top: 0.08, bottom: 0.22 } },
      timeScale: { borderColor: "rgba(255,255,255,0.08)", timeVisible: true, secondsVisible: false, rightOffset: 6 },
      localization: { locale: "zh-CN" },
    });
    chart.current = c;
    candles.current = c.addCandlestickSeries({});
    volume.current = c.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
    c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
    return () => {
      c.remove();
      chart.current = candles.current = volume.current = band.current = null;
      priceLines.current = [];
    };
  }, []);

  useEffect(() => {
    candles.current?.applyOptions({ upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down });
  }, [up, down]);

  const paint = (c: Candle) => {
    const time = (c.time + TZ_SHIFT) as UTCTimestamp;
    candles.current?.update({ time, open: c.open, high: c.high, low: c.low, close: c.close });
    volume.current?.update({ time, value: c.volume, color: c.close >= c.open ? `${up}55` : `${down}55` });
    const all = rows.current;
    if (all.length && all[all.length - 1].time === c.time) all[all.length - 1] = c;
    else if (!all.length || c.time > all[all.length - 1].time) all.push(c);
    if (band.current && bandFrom.current && c.time >= bandFrom.current) band.current.update({ time, value: c.close });
  };

  // 切换品种/周期：整段加载；之后每 3 秒补最近两根
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    last.current = null;
    // 先清掉上一个品种的 K 线：新数据没回来之前宁可空着，也不能让旧品种的图留在新品种名下
    candles.current?.setData([]);
    volume.current?.setData([]);
    rows.current = [];
    const hint = lineDefs.current[0]?.price || tick;
    if (hint) candles.current?.applyOptions({ priceFormat: { type: "price", ...precisionOf(hint) } });
    const explain = (e: any) => (/failed to fetch|networkerror|load failed/i.test(e?.message || "") ? "连不上本地服务（可能已停止），恢复后自动重试…" : e?.message || "K 线加载失败");
    const load = async () => {
      try {
        const loaded = await api.candles(instId, bar, 300, src);
        if (!alive || !candles.current || !volume.current) return;
        setError("");
        if (loaded.length) candles.current.applyOptions({ priceFormat: { type: "price", ...precisionOf(loaded[loaded.length - 1].close) } });
        candles.current.setData(loaded.map((r) => ({ time: (r.time + TZ_SHIFT) as UTCTimestamp, open: r.open, high: r.high, low: r.low, close: r.close })));
        volume.current.setData(loaded.map((r) => ({ time: (r.time + TZ_SHIFT) as UTCTimestamp, value: r.volume, color: r.close >= r.open ? `${up}55` : `${down}55` })));
        rows.current = [...loaded];
        last.current = loaded[loaded.length - 1] ?? null;
        firstTime.current = loaded[0]?.time ?? 0;
        if (last.current && src === "env") report.current?.(last.current.close);
        chart.current?.timeScale().scrollToRealTime();
        setLoading(false);
      } catch (e: any) {
        if (alive) {
          setError(explain(e));
          setLoading(false);
        }
      }
    };
    load();
    const timer = setInterval(async () => {
      if (document.hidden) return;
      if (!last.current) return void load(); // 第一次没加载成功：接着重试

      try {
        const fresh = await api.candles(instId, bar, 2, src);
        if (!alive) return;
        for (const r of fresh) {
          if (last.current && r.time >= last.current.time) {
            paint(r);
            last.current = r;
            if (src === "env") report.current?.(r.close);
          }
        }
        setError("");
      } catch (e: any) {
        if (alive) setError(explain(e));
      }
    }, 3000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instId, bar, src]);

  // 盯盘推送的最新价：直接更新最后一根（推送的是当前环境的价格，看真实行情时不能混进去）
  useEffect(() => {
    const c = last.current;
    if (!tick || !c || src !== "env") return;
    const next = { ...c, close: tick, high: Math.max(c.high, tick), low: Math.min(c.low, tick) };
    last.current = next;
    paint(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick]);

  const lineKey = lines.map((l) => `${l.title}:${l.price}:${l.color}`).join("|");
  useEffect(() => {
    const series = candles.current;
    if (!series) return;
    priceLines.current.forEach((l) => series.removePriceLine(l));
    const valid = lines.filter((l) => Number.isFinite(l.price) && l.price > 0);
    lineDefs.current = valid;
    priceLines.current = valid.map((l) =>
      series.createPriceLine({ price: l.price, color: l.color, lineWidth: l.width ?? 1, lineStyle: l.dashed ? LineStyle.Dashed : LineStyle.Solid, axisLabelVisible: true, title: l.title }),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lineKey, instId, src]);

  // 图上的标记：每笔仓的入场点（箭头）+ 某条历史建议发出的时间点
  const entryKey = (entries ?? []).map((e) => `${e.ts}:${e.price}:${e.side}`).join("|") + "#" + (algoMarks ?? []).map((a) => `${a.ts}:${a.label}:${a.side}:${a.ok ?? ""}`).join("|");
  useEffect(() => {
    const series = candles.current;
    if (!series || loading) return;
    const step = BAR_SECONDS[bar] ?? 60;
    const out: { time: UTCTimestamp; position: "aboveBar" | "belowBar"; color: string; shape: "arrowUp" | "arrowDown" | "circle"; text: string }[] = [];
    if (last.current) {
      for (const e of entries ?? []) {
        const t = Math.floor(e.ts / 1000 / step) * step;
        const early = t < firstTime.current; // 入场比图上最早的一根还早：标在最左边
        const time = ((early ? firstTime.current : Math.min(t, last.current.time)) + TZ_SHIFT) as UTCTimestamp;
        const long = e.side === "long";
        out.push({ time, position: long ? "belowBar" : "aboveBar", color: long ? GREEN : RED, shape: long ? "arrowUp" : "arrowDown", text: early ? `更早${e.label}` : e.label });
      }
      // 算法触发记录：同一根 K 线、同一个方向的并成一个圆点；15 分钟后核对过的按对错上色
      const groups = new Map<string, { time: number; side: string; labels: string[]; ok: string }>();
      for (const a of algoMarks ?? []) {
        const t = Math.floor(a.ts / 1000 / step) * step;
        if (t < firstTime.current) continue;
        const time = Math.min(t, last.current.time);
        const g = groups.get(`${time}:${a.side}`) ?? { time, side: a.side, labels: [], ok: "" };
        if (!g.labels.includes(a.label)) g.labels.push(a.label);
        g.ok = g.ok || a.ok || "";
        groups.set(`${time}:${a.side}`, g);
      }
      for (const g of groups.values()) {
        out.push({
          time: (g.time + TZ_SHIFT) as UTCTimestamp,
          position: g.side === "long" ? "belowBar" : "aboveBar",
          color: ALGO_COLOR[g.ok] ?? "#a78bfa",
          shape: "circle",
          text: (g.labels.length > 2 ? `${g.labels.slice(0, 2).join("·")} +${g.labels.length - 2}` : g.labels.join("·")) + (g.side === "long" ? "↑" : g.side === "short" ? "↓" : ""),
        });
      }
      if (shiftMark && shiftMark.ts) {
        const stime = Math.floor(shiftMark.ts / 1000 / step) * step;
        if (stime >= firstTime.current) {
          out.push({
            time: (Math.min(stime, last.current.time) + TZ_SHIFT) as UTCTimestamp,
            position: shiftMark.side === "long" ? "belowBar" : "aboveBar",
            color: "#38bdf8",
            shape: "circle",
            text: shiftMark.text,
          });
        }
      }
    }
    const t = mark ? Math.floor(mark.ts / 1000 / step) * step : 0;
    const showMark = !!mark && !!last.current && t >= firstTime.current;
    const markTime = showMark ? ((Math.min(t, last.current!.time) + TZ_SHIFT) as UTCTimestamp) : (0 as UTCTimestamp);
    if (showMark) out.push({ time: markTime, position: "aboveBar", color: "#ff9f43", shape: "arrowDown", text: mark!.text });
    series.setMarkers(out.sort((x, y) => (x.time as number) - (y.time as number)));
    if (!showMark) return;
    const ts = chart.current?.timeScale();
    const range = ts?.getVisibleRange();
    if (ts && range && (markTime < (range.from as number) || markTime > (range.to as number))) {
      const barsAgo = Math.round((last.current!.time - t) / step);
      ts.scrollToPosition(-Math.max(barsAgo - 30, 0), true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mark?.ts, mark?.text, shiftMark?.ts, shiftMark?.text, entryKey, bar, instId, loading]);

  // 持仓这一段：从入场那根 K 线到现在，入场价和价格走势之间涂色——在入场价有利的一侧是绿的（赚），不利的一侧是红的（亏）
  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    if (band.current) {
      c.removeSeries(band.current);
      band.current = null;
      bandFrom.current = 0;
    }
    if (!position || loading || src !== "env" || !rows.current.length) return;
    const step = BAR_SECONDS[bar] ?? 60;
    const from = Math.max(Math.floor(position.since / 1000 / step) * step, rows.current[0].time);
    const long = position.side === "long";
    // 只留一层很淡的底色，不画线：线会叠在 K 线上把图弄乱
    const NONE = "rgba(0, 0, 0, 0)";
    const win = { line: NONE, fill1: "rgba(14, 203, 129, 0.10)", fill2: "rgba(14, 203, 129, 0.10)" };
    const lose = { line: NONE, fill1: "rgba(246, 70, 93, 0.10)", fill2: "rgba(246, 70, 93, 0.10)" };
    const top = long ? win : lose;
    const bottom = long ? lose : win;
    const series = c.addBaselineSeries({
      baseValue: { type: "price", price: position.price },
      topLineColor: top.line, topFillColor1: top.fill1, topFillColor2: top.fill2,
      bottomLineColor: bottom.line, bottomFillColor1: bottom.fill1, bottomFillColor2: bottom.fill2,
      lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      priceFormat: { type: "price", ...precisionOf(position.price) },
    });
    series.setData(rows.current.filter((r) => r.time >= from).map((r) => ({ time: (r.time + TZ_SHIFT) as UTCTimestamp, value: r.close })));
    band.current = series;
    bandFrom.current = from;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [position?.since, position?.price, position?.side, bar, instId, src, loading]);

  // 拖动止损 / 止盈线：按住线上下拖，松手后把新价格交给上层确认
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    let drag: { idx: number; orig: number } | null = null;
    const yOf = (e: PointerEvent) => e.clientY - el.getBoundingClientRect().top;
    const hit = (y: number) => {
      const series = candles.current;
      if (!series || !dragCb.current) return -1;
      let best = -1;
      let bestDist = 8;
      lineDefs.current.forEach((l, i) => {
        if (!l.drag) return;
        const c = series.priceToCoordinate(l.price);
        if (c == null) return;
        const d = Math.abs(c - y);
        if (d < bestDist) {
          best = i;
          bestDist = d;
        }
      });
      return best;
    };
    const down = (e: PointerEvent) => {
      const i = hit(yOf(e));
      if (i < 0) return;
      drag = { idx: i, orig: lineDefs.current[i].price };
      chart.current?.applyOptions({ handleScroll: false, handleScale: false });
      el.classList.add("dragging");
      try {
        el.setPointerCapture(e.pointerId);
      } catch {
        /* 不支持也能拖 */
      }
      e.preventDefault();
    };
    const move = (e: PointerEvent) => {
      const series = candles.current;
      if (!series) return;
      if (!drag) {
        el.classList.toggle("can-drag", hit(yOf(e)) >= 0);
        return;
      }
      const price = series.coordinateToPrice(yOf(e));
      if (price != null) priceLines.current[drag.idx]?.applyOptions({ price: price as number });
      e.preventDefault();
    };
    const up = (e: PointerEvent) => {
      if (!drag) return;
      const { idx, orig } = drag;
      drag = null;
      const price = candles.current?.coordinateToPrice(yOf(e));
      chart.current?.applyOptions({ handleScroll: true, handleScale: true });
      el.classList.remove("dragging", "can-drag");
      // 线先回到原位：只有交易所真的改成功、账户数据刷新后，线才会移到新位置
      priceLines.current[idx]?.applyOptions({ price: orig });
      const def = lineDefs.current[idx];
      if (e.type === "pointerup" && price != null && def?.drag && Math.abs((price as number) - orig) / orig > 0.0002) {
        dragCb.current?.(def.drag, price as number);
      }
    };
    el.addEventListener("pointerdown", down, true);
    el.addEventListener("pointermove", move, true);
    el.addEventListener("pointerup", up, true);
    el.addEventListener("pointercancel", up, true);
    return () => {
      el.removeEventListener("pointerdown", down, true);
      el.removeEventListener("pointermove", move, true);
      el.removeEventListener("pointerup", up, true);
      el.removeEventListener("pointercancel", up, true);
    };
  }, []);

  return (
    <div className="chart-wrap">
      <div ref={box} className="chart" />
      {loading && <div className="chart-overlay">加载 K 线…</div>}
      {error && <div className="chart-overlay error">{error}</div>}
    </div>
  );
}
