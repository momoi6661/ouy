import { useEffect, useState } from "react";
import { api, type Activity, type ErrorDiagnostic } from "../api";

/** Summaries stay compact; task evidence and provider diagnostics are available on demand. */
export function ErrorDetails({ workId, error, message = "" }: { workId?: number | null; error?: ErrorDiagnostic; message?: string }) {
  const [open, setOpen] = useState(false);
  const [events, setEvents] = useState<Activity[] | null>(null);
  const [failure, setFailure] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    setEvents(null);
    setFailure("");
    if (!open || !workId) return;
    let active = true;
    api.workDetail(workId).then(
      (row) => { if (active) setEvents(row.activity); },
      (e: Error) => { if (active) setFailure(e.message); },
    );
    return () => { active = false; };
  }, [open, workId, attempt]);
  const errors: ErrorDiagnostic[] = error ? [error] : (events ?? []).flatMap((e) => e.data?.error ? [e.data.error as ErrorDiagnostic] : []);
  return (
    <details className="error-details" open={open} onClick={(e) => e.stopPropagation()} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>{workId ? `查看详情 · 任务 #${workId}` : "查看错误详情"}</summary>
      {open && <div>
        {failure ? <p role="alert">{failure} <button className="link" onClick={() => setAttempt((n) => n + 1)}>重试</button></p> : !error && workId && events === null ? <p className="muted">正在读取…</p> : <>
          {errors.length ? errors.map((x, i) => <pre key={i}>{JSON.stringify(x, null, 2)}</pre>) : <pre>{message || "这条历史记录没有保存更完整的错误信息。"}</pre>}
          {!!events?.length && <details><summary>任务执行记录</summary><pre>{events.map((e) => `${new Date(e.ts).toLocaleString()} [${e.agent}/${e.kind}] ${e.text}`).join("\n\n")}</pre></details>}
        </>}
      </div>}
    </details>
  );
}
