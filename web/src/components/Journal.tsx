import { fmtTime, shortInst, STATUS_LABEL, type JournalEntry } from "../api";
import { Empty } from "./ui";

const KIND = { cycle: "复盘", action: "操作", alert: "警报", system: "系统" } as const;
const BIAS: Record<string, string> = { long: "看多", short: "看空", neutral: "观望" };

export interface PastLevel {
  price: number;
  kind: "entry" | "sl" | "tp" | "watch" | "price";
  title: string;
}

/** 一条记录当时给出的价位：计划的入场区间、止损、止盈，以及盯盘价位。没有价位的记录返回空数组。 */
export function levelsOf(e: JournalEntry): PastLevel[] {
  const out: PastLevel[] = [];
  const add = (price: unknown, kind: PastLevel["kind"], title: string) => {
    const n = Number(price);
    if (Number.isFinite(n) && n > 0 && !out.some((x) => x.kind === kind && x.price === n)) out.push({ price: n, kind, title });
  };
  if (e.kind === "cycle") {
    const plan = e.body?.plan;
    if (plan) {
      if (plan.entry_low !== plan.entry_high) {
        add(plan.entry_high, "entry", "当时入场上沿");
        add(plan.entry_low, "entry", "当时入场下沿");
      } else add(plan.entry_low, "entry", "当时入场");
      add(plan.stop_loss, "sl", "当时止损");
      (plan.take_profits ?? []).forEach((tp: number, i: number) => add(tp, "tp", `当时止盈${i + 1}`));
    }
    for (const l of e.body?.watch_levels ?? []) add(l.price, "watch", "当时盯盘位");
    if (out.length) add(e.body?.price, "price", "当时价格");
  } else if (e.kind === "action") {
    const a = e.body?.action ?? {};
    const d = e.body?.detail ?? {};
    if (d.entry_low && d.entry_high && d.entry_low !== d.entry_high) {
      add(d.entry_high, "entry", "当时入场上沿");
      add(d.entry_low, "entry", "当时入场下沿");
    } else add(d.entry ?? a.price, "entry", "当时入场");
    add(d.stop_loss ?? a.stop_loss, "sl", "当时止损");
    const tps: number[] = d.take_profits?.length ? d.take_profits : [d.take_profit ?? a.take_profit];
    tps.forEach((tp, i) => add(tp, "tp", `当时止盈${tps.length > 1 ? i + 1 : ""}`));
  }
  return out;
}

interface Props {
  entries: JournalEntry[];
  compact?: boolean;
  /** 给了就可以点记录，把它当时的价位临时画到 K 线上 */
  onShow?: (entry: JournalEntry) => void;
  shownId?: number | null;
}

export function Journal({ entries, compact, onShow, shownId }: Props) {
  if (!entries.length) return <Empty>还没有记录。启动盯盘后，Agent 的每次判断和每笔操作都会记在这里。</Empty>;
  return (
    <div className={`journal ${compact ? "compact" : ""}`}>
      {entries.map((e) => {
        const status = e.body?.status as string | undefined;
        const can = !!onShow && levelsOf(e).length > 0;
        const on = shownId === e.id;
        return (
          <div
            key={e.id}
            className={`j-item ${e.kind} ${status ?? ""} ${can ? "clickable" : ""} ${on ? "shown" : ""}`}
            onClick={can ? () => onShow!(e) : undefined}
            title={can ? (on ? "再点一次收起" : "点一下，在 K 线上临时显示当时给的入场、止损、止盈") : undefined}
          >
            <div className="j-head">
              <span className={`j-kind ${e.kind}`}>{KIND[e.kind] ?? e.kind}</span>
              {e.inst_id && <b>{shortInst(e.inst_id)}</b>}
              {e.kind === "cycle" && e.body.bias && <span className={`bias sm ${e.body.bias}`}>{BIAS[e.body.bias]}</span>}
              {e.kind === "action" && status && <span className={`status ${status}`}>{STATUS_LABEL[status] ?? status}</span>}
              {e.kind === "action" && e.body.source && <span className="muted small">{e.body.source === "agent" ? "Agent" : e.body.source === "guard" ? "风控" : "手动"}</span>}
              <span className="muted small right">{fmtTime(e.ts)}</span>
            </div>
            <div className="j-title">{e.title}</div>
            {e.kind === "action" && e.body.message && <div className="j-msg">{e.body.message}</div>}
            {e.kind === "action" && e.body.action?.reason && !compact && <div className="muted small">理由：{e.body.action.reason}</div>}
            {e.kind === "cycle" && !compact && e.body.reason && <div className="muted small">触发：{e.body.reason} · 当时价格 {e.body.price}</div>}
            {e.kind === "alert" && e.body.message && <div className="j-msg">{e.body.message}</div>}
            {can && <div className="j-show">{on ? "已显示在图上 · 点击收起" : "在图上显示当时的价位"}</div>}
          </div>
        );
      })}
    </div>
  );
}
