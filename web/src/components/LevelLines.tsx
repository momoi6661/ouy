import { useEffect, useMemo, useState } from "react";
import { api, fmtPx, type AlgoReport, type Candidate, type ChartLevels } from "../api";
import type { ChartLine } from "./Chart";
import { useStored } from "./ui";

const WINDOW_LINES: [string, string][] = [["30m", "#d8c06a"], ["1H", "#e0a458"], ["4H", "#d98a6a"], ["1D", "#c77dba"]];
const WINDOW_NAME: Record<string, string> = { "30m": "30 分钟", "1H": "1 小时", "4H": "4 小时", "1D": "1 天" };
/** 参考线和当前挂单按类控制显示。 */
const GROUPS: [string, string, string, string][] = [
  ["shift", "变盘点", "#38bdf8", "算法识别出的最近一次结构转折（BOS/CHoCH）：标出转折那根 K 线和顺势回踩参考线"],
  ["value", "价值区间", "#e879f9", "上一个 UTC 日成交最密集的价格带：POC 是成交最多的价格，VAH / VAL 是包住七成成交量的上沿和下沿。里面做两头，站稳在外面顺着做，冲出去又收回反着做"],
  ["open", "开盘价", "#facc15", "今天的开盘价（加密货币按 UTC 0 点，股票永续按美股开盘）和开盘后第一个小时的高低点；开盘价是当天的多空分界"],
  ["range", "区间上下沿", "#5b6b7f", "这个周期最近一段走势的上沿和下沿；Worker 复盘时用的是同一套算法"],
  ["support", "支撑", "#5f9e7d", "下方被多次碰到又弹起的价位"],
  ["resistance", "阻力", "#b06a6a", "上方被多次碰到又回落的价位"],
  ["events", "算法触发", "#a78bfa", "各个算法（行情判断、转折、候选位、快判、保盈线……）每次触发的那根 K 线上画一个圆点：紫色是还没核对，绿色 / 红色是 15 分钟后价格照它说的走了 / 走反了。明细在「算法」页"],
  ["algo", "算法候选位", "#f59e0b", "最近一次复盘时算法给 Worker 的入场候选：入场、止损、目标。只画合格的前两条；是参考位，不是已经下的单"],
  ["plan", "Worker 计划", "#8ab4ff", "Worker 当前计划的入场区间、止损和目标"],
  ["watch", "计划线", "#9aa4b2", "Worker 每轮划的线：到了由程序进场、撤单或叫醒它"],
  ["trigger", "触发线", "#c084fc", "触发线和到价策略的触发价、止损止盈，以及挂单的撤单线"],
  ["orders", "当前挂单", "#f0b90b", "交易所当前委托的价格"],
];

/** 加新的一类线之前存下来的开关：沿用，不让用户重新选一遍。 */
function previousShow(): string[] {
  try {
    const raw = localStorage.getItem("ouy.chart.show.v5");
    if (raw) return JSON.parse(raw) as string[];
    const v4 = localStorage.getItem("ouy.chart.show.v4");
    if (v4) return [...(JSON.parse(v4) as string[]), "events"];
    const older = localStorage.getItem("ouy.chart.show.v3");
    if (older) return [...(JSON.parse(older) as string[]), "algo"];
  } catch {
    /* 读不到就用默认值 */
  }
  return ["shift", "plan", "trigger", "algo", "events"];
}

/** 一条线到了之后程序做什么，画在图上的名字。action 是规则的动作（open_market / cancel_orders / close / wake），
 *  或者计划线的 do（enter / cancel / wake）；不是这几种的返回 null，由调用的地方用原来的叫法。 */
/** 线的条件写在标签上：碰到就算，还是要等某个周期的 K 线收盘越过。两种线在图上长得一样，不写出来看不出为什么价格过了线却没动。 */
export function lineCond(when: unknown, bar?: unknown): string {
  const w = String(when ?? "");
  const tf = bar ? `${bar} ` : "";
  if (w === "close_above") return ` · ${tf}收盘站上`;
  if (w === "close_below") return ` · ${tf}收盘跌破`;
  if (w === "price_above") return " · 涨到";
  if (w === "price_below") return " · 跌到";
  if (w.startsWith("price")) return " · 碰到";
  return "";
}

export function lineKind(action: unknown): string | null {
  switch (action) {
    case "open_market":
    case "enter":
      return "进场线";
    case "cancel_orders":
    case "cancel":
      return "撤单线";
    case "close":
      return "平仓线";
    case "wake":
      return "叫醒线";
    default:
      return null;
  }
}

/** K 线上的参考线：上一段的高点低点（按时间段分别开关）、区间上下沿、支撑、阻力（各一个开关）。每 20 秒更新。
 *  Worker 的计划、计划线、触发线这几类线由页面自己画，这里只管它们的开关（on）。
 *  工作台和交易页共用同一份开关设置。返回要画的线和那一排开关。 */
export function useLevelLines(inst: string, bar: string, candidates?: Candidate[]) {
  const [levels, setLevels] = useState<ChartLevels | null>(null);
  const [showWin, setShowWin] = useStored<string[]>("ouy.chart.windows", ["1H"]);
  const [show, setShow] = useStored<string[]>("ouy.chart.show.v6", [...previousShow(), "orders"]);
  const [algo, setAlgo] = useState<AlgoReport | null>(null);
  const showEvents = show.includes("events");
  useEffect(() => {
    if (!showEvents) return setAlgo(null);
    let alive = true;
    const load = () => api.algo(inst).then((r) => alive && setAlgo(r), () => {});
    setAlgo(null);
    load();
    const timer = window.setInterval(load, 20_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [inst, showEvents]);
  const algoMarks = useMemo(
    () => (algo?.events ?? []).map((e) => ({ ts: e.ts, side: e.side, label: algo?.labels[e.algo] ?? e.algo, ok: e.checks["15m"]?.ok })),
    [algo],
  );
  const on = (group: string) => show.includes(group);
  useEffect(() => {
    let alive = true;
    const load = () => api.levels(inst, bar).then((r) => alive && setLevels(r), () => {});
    setLevels(null);
    load();
    const timer = window.setInterval(load, 20_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [inst, bar]);

  const lines = useMemo<ChartLine[]>(() => {
    const out: ChartLine[] = [];
    if (!levels) return out;
    for (const [name, color] of WINDOW_LINES) {
      const w = levels.windows?.[name];
      if (!w || !showWin.includes(name)) continue;
      out.push({ price: w.prev_high, color, title: `上一个 ${WINDOW_NAME[name]} 高点`, dashed: true });
      out.push({ price: w.prev_low, color, title: `上一个 ${WINDOW_NAME[name]} 低点`, dashed: true });
    }
    if (levels.shift && show.includes("shift")) {
      out.push({
        price: levels.shift.level,
        color: "#38bdf8",
        title: `${levels.shift.side === "long" ? "转多" : "转空"}回踩位`,
        dashed: true,
      });
    }
    const va = levels.value_area;
    if (va && show.includes("value")) {
      out.push({ price: va.vah, color: "#e879f9", title: "价值区间上沿 VAH", dashed: true });
      out.push({ price: va.poc, color: "#e879f9", title: "成交密集价 POC" });
      out.push({ price: va.val, color: "#e879f9", title: "价值区间下沿 VAL", dashed: true });
    }
    const ss = levels.session;
    if (ss && show.includes("open")) {
      out.push({ price: ss.open, color: "#facc15", title: `开盘价（${ss.kind}，现在 ${ss.change_pct >= 0 ? "+" : ""}${ss.change_pct}%）` });
      if (ss.opening_range) {
        out.push({ price: ss.opening_range.high, color: "#facc15", title: "开盘区间上沿", dashed: true });
        out.push({ price: ss.opening_range.low, color: "#facc15", title: "开盘区间下沿", dashed: true });
      }
    }
    if (levels.range && show.includes("range")) {
      out.push({ price: levels.range.upper, color: "#5b6b7f", title: `区间上沿（${bar}）`, dashed: true });
      out.push({ price: levels.range.lower, color: "#5b6b7f", title: `区间下沿（${bar}）`, dashed: true });
    }
    if (show.includes("resistance")) for (const r of levels.resistance.slice(0, 3)) out.push({ price: r.price, color: "#b06a6a", title: `阻力（${bar}，碰过 ${r.touches} 次）`, dashed: true });
    if (show.includes("support")) for (const r of levels.support.slice(0, 3)) out.push({ price: r.price, color: "#5f9e7d", title: `支撑（${bar}，碰过 ${r.touches} 次）`, dashed: true });
    if (show.includes("algo")) {
      (candidates ?? []).filter((c) => c.ok).slice(0, 2).forEach((c, i) => {
        const tag = `候选${i + 1} ${c.strategy}${c.side === "long" ? "多" : "空"}`;
        out.push({ price: c.entry, color: "#f59e0b", title: `${tag} 入场（${c.how === "now" ? "现价" : c.how === "breakout" ? "突破" : "限价"}）` });
        out.push({ price: c.stop, color: "#f6465d", title: `${tag} 止损`, dashed: true });
        c.targets.forEach((t, k) => out.push({ price: t, color: "#0ecb81", title: `${tag} 目标${k + 1}`, dashed: true }));
      });
    }
    return out.filter((l) => Number.isFinite(l.price) && l.price > 0);
  }, [levels, showWin, show, bar, candidates]);

  const shiftMark = useMemo(() => {
    const s = levels?.shift;
    if (!s || !show.includes("shift")) return null;
    return { ts: s.ts, side: s.side, text: `${s.side === "long" ? "转多" : "转空"} (${s.kind})` };
  }, [levels?.shift, show]);

  const toggles = (
    <>
      <span className="muted small" title="上一段时间走完后的最高点和最低点；每 20 秒更新一次，新的一段走完就换成新的">
        前高前低
      </span>
      {WINDOW_LINES.map(([name, color]) => {
        const w = levels?.windows?.[name];
        const on = showWin.includes(name);
        return (
          <button
            key={name}
            className={`chip ${on ? "on" : ""}`}
            disabled={!w}
            style={on ? { borderColor: color, color } : undefined}
            title={w ? `高 ${fmtPx(w.prev_high)} / 低 ${fmtPx(w.prev_low)} · ${w.trend}` : "K 线不够，算不出来"}
            onClick={() => setShowWin(on ? showWin.filter((x) => x !== name) : [...showWin, name])}
          >
            {WINDOW_NAME[name]}
          </button>
        );
      })}
      <i className="chip-sep" />
      {GROUPS.map(([id, label, color, tip]) => (
        <button
          key={id}
          className={`chip ${on(id) ? "on" : ""}`}
          style={on(id) ? { borderColor: color, color } : undefined}
          title={tip}
          onClick={() => setShow(on(id) ? show.filter((x) => x !== id) : [...show, id])}
        >
          {label}
        </button>
      ))}
    </>
  );
  return { lines, toggles, show, on, shiftMark, algoMarks };
}
