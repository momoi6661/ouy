/** 交易风格与打法示意图：每种做法赚的是哪一段钱、在哪进、止损和目标放哪。只是示意，不代表能赚钱。 */

type Mark = { at: number; kind: "entry" | "stop" | "target"; label: string };
interface Card {
  title: string;
  edge: string;
  path: number[];
  lines?: { y: number; label: string; tone: "muted" | "red" | "blue" | "green" }[];
  marks: Mark[];
  fits: string;
  avoid: string;
  program: string;
}

// path：价格从左到右的折线，0 = 图的底部，100 = 顶部
const STYLES: Card[] = [
  {
    title: "超短线",
    edge: "赚 5 分钟结构里一小段的钱：一笔只拿一个小波段，靠次数和纪律，手续费占比最高。",
    path: [50, 56, 48, 60, 54, 66, 58, 52, 62, 70, 64, 58, 66, 74],
    marks: [{ at: 2, kind: "entry", label: "回踩进" }, { at: 5, kind: "target", label: "小目标" }, { at: 7, kind: "entry", label: "再进" }, { at: 9, kind: "target", label: "走" }],
    fits: "波动够、点差小的大币；方向在 15m 上清楚的时候。",
    avoid: "止损小于 0.3% 或目标扣完成本不到 1.2R；没量的横盘。",
    program: "快判每几秒看 1 分钟 K 线；保盈线约 0.5R 起锁住不亏；30 分钟没走出来时间止损。",
  },
  {
    title: "日内短线",
    edge: "赚当天那一段主要走势的钱：等开盘或关键位给出方向，跟一段几十分钟到几小时的行情。",
    path: [50, 54, 47, 52, 49, 58, 63, 60, 68, 74, 71, 79, 83, 80],
    lines: [{ y: 56, label: "开盘区间上沿", tone: "muted" }, { y: 46, label: "开盘区间下沿", tone: "muted" }],
    marks: [{ at: 5, kind: "entry", label: "突破后回踩进" }, { at: 4, kind: "stop", label: "止损在区间里" }, { at: 12, kind: "target", label: "日内目标" }],
    fits: "4H / 1H 方向清楚，15m 有结构可以放止损。",
    avoid: "大周期在打架、消息前后的来回扫。",
    program: "开盘记录器对照开盘价和开盘前走势；到第一目标平一半，余仓保本跟踪。",
  },
  {
    title: "波段",
    edge: "赚几天的趋势延续：止损宽、次数少，忍得住中间的回撤才拿得到大的一段。",
    path: [20, 30, 25, 40, 33, 50, 44, 60, 52, 70, 63, 80, 74, 88],
    marks: [{ at: 2, kind: "entry", label: "回调进" }, { at: 0, kind: "stop", label: "4H 结构外" }, { at: 8, kind: "entry", label: "加仓点" }, { at: 13, kind: "target", label: "分批走" }],
    fits: "1D / 4H 趋势明确，杠杆低。",
    avoid: "用 5 分钟的噪音管理一笔波段单；高杠杆。",
    program: "保盈线同样按初始止损距离算；Worker 可以对波段单关掉它给更多空间。",
  },
];

const PLAYS: Card[] = [
  {
    title: "趋势回踩",
    edge: "赚趋势继续走的钱：不追高，等回到前面突破过的位置再进。",
    path: [20, 35, 50, 42, 38, 46, 60, 72, 66, 80],
    lines: [{ y: 40, label: "前高变支撑", tone: "muted" }],
    marks: [{ at: 4, kind: "entry", label: "回踩进" }, { at: 4, kind: "stop", label: "支撑下方" }, { at: 9, kind: "target", label: "下一个阻力" }],
    fits: "高低点都在抬高（或都在降低）。",
    avoid: "回踩已经跌破前低，结构坏了。",
    program: "候选位「趋势回踩」由程序算好；挂真实限价单等价格回来。",
  },
  {
    title: "区间高抛低吸",
    edge: "赚来回震荡的钱：下沿买、上沿卖，目标先看中轨。",
    path: [30, 70, 34, 68, 30, 66, 36, 70, 32, 64],
    lines: [{ y: 72, label: "上沿", tone: "red" }, { y: 28, label: "下沿", tone: "green" }],
    marks: [{ at: 2, kind: "entry", label: "下沿买" }, { at: 3, kind: "target", label: "上沿卖" }, { at: 4, kind: "entry", label: "再买" }],
    fits: "上下沿各被碰过两次以上，量能平稳。",
    avoid: "放量收盘冲出区间之后还按区间做。",
    program: "区间上下沿每 10 秒更新；冲出区间时撤掉逆着走的旧挂单。",
  },
  {
    title: "突破回踩",
    edge: "赚盘整之后新一段行情的钱：突破那一下不追，等回踩确认站稳再进。",
    path: [45, 52, 46, 53, 47, 54, 66, 58, 56, 70, 82],
    lines: [{ y: 55, label: "箱体上沿", tone: "muted" }],
    marks: [{ at: 8, kind: "entry", label: "回踩上沿进" }, { at: 8, kind: "stop", label: "回到箱体里" }, { at: 10, kind: "target", label: "箱高等幅" }],
    fits: "收敛很久、放量收盘突破。",
    avoid: "缩量突破马上又收回（那是假突破）。",
    program: "「转折」算法的盘中阶段：突破后连续两个报价收回就记成疑似假突破，第一次失守时叫醒 Worker。",
  },
  {
    title: "转折盘",
    edge: "赚趋势反过来的第一段：先扫掉前低的止损，再收盘站上前一个高点，回踩才进。",
    path: [80, 70, 74, 60, 64, 50, 38, 52, 58, 66, 60, 72, 80],
    lines: [{ y: 47, label: "前 4 小时低点", tone: "muted" }, { y: 64, label: "被站上的摆动高点", tone: "blue" }],
    marks: [{ at: 6, kind: "stop", label: "① 扫单后收回" }, { at: 9, kind: "entry", label: "③ 站上高点" }, { at: 10, kind: "entry", label: "回踩进" }, { at: 12, kind: "target", label: "目标" }],
    fits: "大周期位置有支撑；三关依次出现：扫流动性、动能背离、结构反转。",
    avoid: "只扫了单还没站上高点就抄底；大周期还在加速下跌。",
    program: "「转折」算法标出走到哪一步（苗头 → 确认 → 盘中失守 / 收回）；手里有逆着它的仓会立刻提醒。回测里单凭它机械进场不赚钱。",
  },
  {
    title: "程序马丁（解套）",
    edge: "赚震荡里的回弹：逆向分批补仓把均价拉近，回到均价上方一点整组走。代价是单边行情会亏得更多。",
    path: [60, 52, 56, 46, 50, 40, 44, 52, 58],
    lines: [{ y: 49, label: "均价（保本位）", tone: "blue" }],
    marks: [{ at: 0, kind: "entry", label: "第 1 层" }, { at: 3, kind: "entry", label: "补" }, { at: 5, kind: "entry", label: "补" }, { at: 7, kind: "target", label: "回到保本整组平" }],
    fits: "明确的震荡区间，结构没破。",
    avoid: "单边趋势；没有整组止损。",
    program: "有层数上限和整组止损，要用户打开「允许亏损加仓」才能用。",
  },
];

const W = 260;
const H = 120;
const TONE = { muted: "var(--muted)", red: "var(--red)", blue: "#4c8dff", green: "var(--green)" };

function Diagram({ card }: { card: Card }) {
  const n = card.path.length - 1;
  const x = (i: number) => 10 + (i / n) * (W - 20);
  const y = (v: number) => H - 10 - (v / 100) * (H - 20);
  const pts = card.path.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg className="sg-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${card.title}示意图`}>
      {card.lines?.map((l) => (
        <g key={l.label}>
          <line x1={6} x2={W - 6} y1={y(l.y)} y2={y(l.y)} stroke={TONE[l.tone]} strokeDasharray="4 4" strokeWidth={1} />
          <text x={W - 8} y={y(l.y) - 3} textAnchor="end" className="sg-line-label" fill={TONE[l.tone]}>
            {l.label}
          </text>
        </g>
      ))}
      <polyline points={pts} fill="none" stroke="var(--text)" strokeWidth={1.6} strokeLinejoin="round" />
      {card.marks.map((m, i) => {
        const px = x(m.at);
        const py = y(card.path[m.at]);
        const color = m.kind === "entry" ? "var(--green)" : m.kind === "stop" ? "var(--red)" : TONE.blue;
        const dy = m.kind === "stop" ? 16 : -8;
        return (
          <g key={i}>
            {m.kind === "stop" ? <line x1={px - 8} x2={px + 8} y1={py + 8} y2={py + 8} stroke={color} strokeWidth={2} /> : <circle cx={px} cy={py} r={3.5} fill={color} />}
            <text x={px} y={py + dy} textAnchor="middle" className="sg-mark" fill={color}>
              {m.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function Cards({ list }: { list: Card[] }) {
  return (
    <div className="sg-grid">
      {list.map((c) => (
        <article key={c.title} className="sg-card">
          <h4>{c.title}</h4>
          <p className="sg-edge">{c.edge}</p>
          <Diagram card={c} />
          <dl>
            <dt>适合</dt>
            <dd>{c.fits}</dd>
            <dt>不要用</dt>
            <dd>{c.avoid}</dd>
            <dt>程序做什么</dt>
            <dd>{c.program}</dd>
          </dl>
        </article>
      ))}
    </div>
  );
}

export function StyleGuide() {
  return (
    <details className="panel sg">
      <summary>
        <b>交易风格和打法示意图</b>
        <span className="muted">每种做法赚的是哪一段钱；只是示意，不代表能赚钱</span>
      </summary>
      <h5>交易风格</h5>
      <Cards list={STYLES} />
      <h5>常用打法</h5>
      <Cards list={PLAYS} />
      <p className="muted sg-note">绿点是进场，红线是止损，蓝色是目标。所有打法都受同一套风控：必须带止损，浮盈到过约 0.5R 后程序把止损移到入场价加手续费。</p>
    </details>
  );
}
