import { useEffect, useRef, useState, useSyncExternalStore, type ClipboardEvent, type DragEvent, type KeyboardEvent, type ReactNode, type RefObject } from "react";
import { toast } from "./ui";

/** 待发的一句话 */
export interface Queued {
  id: number;
  text: string;
}

// 待发的话按对话分开存在浏览器里，刷新不丢
const KEY = "ouy.pending";
const EMPTY: Queued[] = [];
let cache: Record<string, Queued[]> = {};
try {
  cache = JSON.parse(localStorage.getItem(KEY) || "{}") || {};
} catch {
  cache = {};
}
const subs = new Set<() => void>();
const write = (next: Record<string, Queued[]>) => {
  cache = next;
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* 隐私模式下忽略 */
  }
  subs.forEach((f) => f());
};
export const pending = {
  push: (k: string, text: string) => write({ ...cache, [k]: [...(cache[k] ?? []), { id: Date.now() + Math.random(), text }] }),
  remove: (k: string, id: number) => write({ ...cache, [k]: (cache[k] ?? []).filter((x) => x.id !== id) }),
  shift: (k: string) => {
    const [first, ...rest] = cache[k] ?? [];
    if (first) write({ ...cache, [k]: rest });
    return first;
  },
};
export function usePending(k: string) {
  return useSyncExternalStore(
    (f) => {
      subs.add(f);
      return () => subs.delete(f);
    },
    () => cache[k] ?? EMPTY,
  );
}

const MAX_IMAGES = 4;
const MAX_SIDE = 1800; // 截图的长边超过这么多就缩小：模型看得清，传得也快
const MAX_BYTES = 6 * 1024 * 1024;

/** 把一张图片读成 data URL；太大的先缩小再转成 JPEG。 */
async function readImage(file: File): Promise<string> {
  const raw = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("图片读不出来"));
    reader.readAsDataURL(file);
  });
  const img = await new Promise<HTMLImageElement>((resolve, reject) => {
    const el = new Image();
    el.onload = () => resolve(el);
    el.onerror = () => reject(new Error("这不是一张能打开的图片"));
    el.src = raw;
  });
  const scale = Math.min(1, MAX_SIDE / Math.max(img.width, img.height));
  if (scale === 1 && file.size <= 1.5 * 1024 * 1024 && ["image/png", "image/jpeg", "image/webp", "image/gif"].includes(file.type)) return raw;
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(img.width * scale);
  canvas.height = Math.round(img.height * scale);
  canvas.getContext("2d")!.drawImage(img, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", 0.9);
}

const Icon = ({ d, size = 16 }: { d: string; size?: number }) => (
  <svg className="cmp-ico" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <path d={d} />
  </svg>
);

/** 对话输入区，工作台和交易页共用：一张卡片，上面是多行输入框（Enter 发送，Shift+Enter 换行），下面一排工具：
 *  左边加图片和「发给谁」之类的选项，右边一个圆形发送键。可以选图、直接粘贴截图、把图片拖进来。
 *  对方正在忙时，发出的话先进上方的待发条：每条可以改、取消、立即发送；它忙完按顺序自动发出。带图片的不排队，直接发。 */
export function Composer(props: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  /** 这段对话的待发队列用哪个名字存 */
  queueKey: string;
  /** 对方现在在忙吗（Worker 在思考，或上一句还没回完） */
  busy: boolean;
  busyText: string;
  /** 立刻发出。对方在忙时 = 直接送进它当前这一轮。images 是随这句话附的图片（data URL） */
  onSend: (text: string, images?: string[]) => Promise<unknown> | void;
  left?: ReactNode;
  right?: ReactNode;
  /** 浮在输入框上面的东西（@ 列表） */
  pop?: ReactNode;
  inputRef?: RefObject<HTMLTextAreaElement>;
  /** 返回 true 表示这次按键已经处理了（比如在选 @ 的对象） */
  onKey?: (e: KeyboardEvent<HTMLTextAreaElement>) => boolean | void;
}) {
  const { value, onChange, queueKey, busy, onSend } = props;
  const items = usePending(queueKey);
  const own = useRef<HTMLTextAreaElement>(null);
  const ref = props.inputRef ?? own;
  const picker = useRef<HTMLInputElement>(null);
  const flushing = useRef(false);
  const [tick, setTick] = useState(0);
  const [images, setImages] = useState<string[]>([]);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    // 输入框跟着内容变高，最多八九行
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [value, ref]);

  useEffect(() => {
    // 对方忙完了：把待发的第一句发出去，回来之后再看下一句
    if (busy || !items.length || flushing.current) return;
    flushing.current = true;
    const first = pending.shift(queueKey);
    Promise.resolve(first && onSend(first.text))
      .catch(() => undefined)
      .finally(() => {
        flushing.current = false;
        setTick((n) => n + 1);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy, items.length, queueKey, tick]);

  const attach = async (files: File[]) => {
    const pics = files.filter((f) => f.type.startsWith("image/"));
    if (!pics.length) return;
    const room = MAX_IMAGES - images.length;
    if (room <= 0 || pics.length > room) toast(`一次最多附 ${MAX_IMAGES} 张图片`, "error");
    const added: string[] = [];
    for (const file of pics.slice(0, Math.max(room, 0))) {
      try {
        const url = await readImage(file);
        if (url.length * 0.75 > MAX_BYTES) throw new Error("图片太大了，单张不能超过 6MB");
        added.push(url);
      } catch (e: any) {
        toast(e.message, "error");
      }
    }
    if (added.length) setImages((cur) => [...cur, ...added].slice(0, MAX_IMAGES));
    ref.current?.focus();
  };
  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(e.clipboardData.files);
    if (files.some((f) => f.type.startsWith("image/"))) {
      e.preventDefault(); // 粘贴的是截图：收成附件，不往输入框里塞乱码
      attach(files);
    }
  };
  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(false);
    attach(Array.from(e.dataTransfer.files));
  };

  const ready = !!value.trim() || images.length > 0;
  const submit = (now = false) => {
    const text = value.trim() || (images.length ? "看一下这张图" : "");
    if (!text) return;
    onChange("");
    if (images.length) {
      // 带图片的不进待发条（图片不往浏览器的本地存储里放），直接发；对方在忙时由后端排到它这一轮后面
      onSend(text, images);
      setImages([]);
    } else if (busy && !now) pending.push(queueKey, text);
    else onSend(text);
  };

  return (
    <div className="cmp">
      {items.length > 0 && (
        <div className="cmp-queue">
          <div className="cmp-queue-head">
            <b>待发 {items.length} 条</b>
            <span>{busy ? `${props.busyText}，忙完按顺序自动发出` : "正在发出…"}</span>
          </div>
          {items.map((q, i) => (
            <div key={q.id} className="cmp-item">
              <i>{i + 1}</i>
              <span title={q.text}>{q.text}</span>
              <button
                type="button"
                title="不等了，现在就发：对方在忙就直接送进它当前这一轮"
                onClick={() => {
                  pending.remove(queueKey, q.id);
                  onSend(q.text);
                }}
              >
                立即发送
              </button>
              <button
                type="button"
                title="放回输入框里修改"
                onClick={() => {
                  pending.remove(queueKey, q.id);
                  onChange(q.text);
                  ref.current?.focus();
                }}
              >
                修改
              </button>
              <button type="button" className="x" title="取消这一条" onClick={() => pending.remove(queueKey, q.id)}>
                ×
              </button>
            </div>
          ))}
        </div>
      )}
      <div
        className={`cmp-box ${dragging ? "drop" : ""}`}
        onDragOver={(e) => {
          if (Array.from(e.dataTransfer.types).includes("Files")) {
            e.preventDefault();
            setDragging(true);
          }
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        {props.pop}
        {images.length > 0 && (
          <div className="cmp-shots">
            {images.map((src, i) => (
              <figure key={i}>
                <img src={src} alt={`附图 ${i + 1}`} />
                <button type="button" title="去掉这张图" onClick={() => setImages((cur) => cur.filter((_, j) => j !== i))}>
                  ×
                </button>
              </figure>
            ))}
          </div>
        )}
        <textarea
          ref={ref}
          rows={1}
          value={value}
          placeholder={dragging ? "松手把图片放进来" : props.placeholder}
          onChange={(e) => onChange(e.target.value)}
          onPaste={onPaste}
          onKeyDown={(e) => {
            if (props.onKey?.(e)) return;
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="cmp-bar">
          <div className="cmp-left">
            <button type="button" className="cmp-tool" title="附图片：也可以直接粘贴截图，或者把图片拖进来" disabled={images.length >= MAX_IMAGES} onClick={() => picker.current?.click()}>
              <Icon d="M12 5v14M5 12h14" />
            </button>
            <input
              ref={picker}
              type="file"
              accept="image/png,image/jpeg,image/webp,image/gif"
              multiple
              hidden
              onChange={(e) => {
                attach(Array.from(e.target.files ?? []));
                e.target.value = "";
              }}
            />
            {props.left}
          </div>
          <div className="cmp-right">
            {props.right}
            <span className="cmp-hint">
              {busy ? (
                props.busyText
              ) : (
                <>
                  <kbd>Enter</kbd> 发送 <kbd>Shift</kbd>
                  <kbd>Enter</kbd> 换行
                </>
              )}
            </span>
            {busy && !images.length && (
              <button type="button" className="cmp-now" disabled={!ready} title="不排队：直接送进它当前这一轮" onClick={() => submit(true)}>
                立即发送
              </button>
            )}
            <button
              type="button"
              className={`cmp-send ${busy && !images.length ? "queue" : ""}`}
              disabled={!ready}
              title={busy && !images.length ? "对方正在忙：先放进待发条，忙完自动发出" : "发送"}
              aria-label={busy && !images.length ? "排队" : "发送"}
              onClick={() => submit()}
            >
              {busy && !images.length ? <Icon d="M12 7v5l3 2M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z" size={17} /> : <Icon d="M12 19V5M5 12l7-7 7 7" size={17} />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
