import { useEffect, useState } from "react";
import { api, fmtPx, type Account, type PlanReceipt, type Report } from "../api";
import { toast } from "./ui";

const LABEL: Record<string, string> = {
  draft: "待应用", waiting: "待确认", pending: "待确认", proposed: "触发规则待确认",
  armed: "等待触发", active: "规则运行中", live: "已挂单", partially_filled: "部分成交",
  filled: "已成交", canceled: "已撤单", cancelled: "已撤销", expired: "已过期",
  rejected: "已拒绝", declined: "已拒绝", failed: "执行失败", skipped: "未下单",
  unknown: "委托状态未核实", updated: "计划已更新", watching: "观察中", done: "规则已结束",
};

export function PlanExecution({ inst, report, account, onChanged }: {
  inst: string;
  report: Report;
  account: Account | null;
  onChanged: () => void;
}) {
  const [receipt, setReceipt] = useState<PlanReceipt | null>(null);
  const [busy, setBusy] = useState(false);
  const plan = report.plan_order;
  const order = account?.orders.find((o) => o.inst_id === inst && o.ord_id === plan?.ord_id);

  useEffect(() => {
    let alive = true;
    setReceipt(null);
    if (!report.plan || order) return;
    const load = () => api.planReceipt(inst, report.ts).then(
      (value) => { if (alive) setReceipt(value); },
      () => { if (alive) setReceipt({ state: "unknown" }); },
    );
    load();
    const timer = window.setInterval(load, 12_000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [inst, report.ts, report.plan, plan?.status, order?.ord_id]);

  if (!report.plan) return plan?.text ? <div className="small plan-order">{plan.text}</div> : null;
  const state = order ? (order.filled > 0 ? "partially_filled" : "live") : receipt?.state ?? (plan?.status === "advised" ? "draft" : "unknown");
  const canApply = state === "draft" && !!account;
  const waiting = (state === "waiting" || state === "pending" || state === "proposed") && (plan?.pending_id || plan?.strategy_id);
  const act = async (accept: boolean) => {
    setBusy(true);
    try {
      if (canApply) {
        if (account?.env === "live" && !window.confirm("在实盘应用这份入场计划？")) return;
        await api.applyPlan(inst, report.ts);
      } else if (plan?.pending_id) {
        if (accept) {
          const result = await api.approve(plan.pending_id);
          toast(result.message, result.status === "executed" ? "ok" : "error");
        } else await api.reject(plan.pending_id);
      } else if (plan?.strategy_id) {
        await api.strategyAct(plan.strategy_id, accept ? "approve" : "cancel");
      }
      setReceipt(await api.planReceipt(inst, report.ts));
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
      onChanged();
    }
  };
  const price = order?.px || receipt?.price;
  const filled = order?.filled ?? receipt?.filled;
  return (
    <div className={`small plan-order ${state}`}>
      <b>{LABEL[state] ?? state}</b>
      {!!price && <span> · {fmtPx(price)}</span>}
      {!!filled && <span> · 已成交 {filled} 张</span>}
      {(order?.ord_id || receipt?.ord_id) && <span> · 委托 {order?.ord_id || receipt?.ord_id}</span>}
      {receipt?.text && <div>{receipt.text}</div>}
      {plan?.text && <div className="muted">上轮回执 · {plan.text}</div>}
      {(canApply || waiting) && (
        <div className="row">
          <button className="btn sm primary" disabled={busy} onClick={() => act(true)}>
            {busy ? "处理中…" : canApply ? "应用计划" : state === "proposed" ? "确认启用" : "确认执行"}
          </button>
          {!!waiting && <button className="btn sm" disabled={busy} onClick={() => act(false)}>拒绝</button>}
        </div>
      )}
    </div>
  );
}
