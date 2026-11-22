import { useEffect, useRef, useState, type ReactNode } from "react";

export function useInterval(fn: () => void, ms: number | null) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (ms === null) return;
    const t = setInterval(() => ref.current(), ms);
    return () => clearInterval(t);
  }, [ms]);
}

export function useStored<T>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  const set = (v: T) => {
    setValue(v);
    try {
      localStorage.setItem(key, JSON.stringify(v));
    } catch {
      /* 隐私模式下忽略 */
    }
  };
  return [value, set];
}

export interface Toast {
  id: number;
  kind: "ok" | "error" | "info";
  text: string;
}

let toastId = 0;
const listeners = new Set<(t: Toast) => void>();

export function toast(text: string, kind: Toast["kind"] = "info") {
  const t = { id: ++toastId, kind, text };
  listeners.forEach((l) => l(t));
}

export function Toasts() {
  const [items, setItems] = useState<Toast[]>([]);
  useEffect(() => {
    const add = (t: Toast) => {
      setItems((prev) => [...prev.slice(-3), t]);
      setTimeout(() => setItems((prev) => prev.filter((x) => x.id !== t.id)), t.kind === "error" ? 8000 : 4500);
    };
    listeners.add(add);
    return () => {
      listeners.delete(add);
    };
  }, []);
  return (
    <div className="toasts" role="status" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`} onClick={() => setItems((prev) => prev.filter((x) => x.id !== t.id))}>
          {t.text}
        </div>
      ))}
    </div>
  );
}

export function Modal({ title, children, onClose, wide }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-mask" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="icon-btn" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Num({ value, onChange, step, min, max, placeholder, suffix }: { value: number | ""; onChange: (v: number | "") => void; step?: number; min?: number; max?: number; placeholder?: string; suffix?: string }) {
  return (
    <span className="num">
      <input
        type="number"
        inputMode="decimal"
        value={value}
        step={step ?? "any"}
        min={min}
        max={max}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
      />
      {suffix && <em>{suffix}</em>}
    </span>
  );
}

export function Pnl({ value, suffix = "" }: { value: number; suffix?: string }) {
  return <span className={value > 0 ? "up" : value < 0 ? "down" : ""}>{`${value > 0 ? "+" : ""}${value.toFixed(2)}${suffix}`}</span>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

const LEVERAGE_STEPS = [1, 2, 3, 5, 10, 15, 20, 25, 30, 50, 75, 100, 125];

/** 杠杆档位：只能点选。超过这个币种最高倍数的不显示，超过风控上限的置灰。 */
export function LeverPicker({ value, onChange, instMax, riskMax }: { value: number | ""; onChange: (v: number) => void; instMax: number; riskMax: number }) {
  const steps = LEVERAGE_STEPS.filter((x) => x <= Math.max(instMax || riskMax, 1));
  if (value !== "" && !steps.includes(value)) steps.push(value);
  steps.sort((a, b) => a - b);
  return (
    <div className="lever-picker">
      {steps.map((x) => (
        <button
          key={x}
          type="button"
          className={x === value ? "on" : ""}
          disabled={x > riskMax}
          title={x > riskMax ? `超过你的风控上限 ${riskMax}x，要用得先在 Agent 设置里调高` : `${x} 倍`}
          onClick={() => onChange(x)}
        >
          {x}x
        </button>
      ))}
    </div>
  );
}
