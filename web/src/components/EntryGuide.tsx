/** 出手图解：出手尺度「积极」时，程序在什么情况下给出可以开单的位置、在哪进、止损和目标放哪。
 *  规则照 ouy/agent/plan.py（market_regime / build_candidates / check_plan）和 ouy/indicators.py 写，改了那边要同步改这里。 */

type Tone = "green" | "red" | "blue" | "muted" | "orange";
interface Sketch {
  path: [number, number][];
  levels?: { y: number; label: string; tone: Tone; solid?: boolean; x1?: number }[];
  zones?: { x1: number; y1: number; y2: number; tone: Tone }[];
  boxes?: { x1: number; x2: number; y1: number; y2: number }[];
  marks?: { x: number; y: number; label: string; tone?: Tone; dot?: boolean; dx?: number; dy?: number; end?: boolean }[];
}
interface Card {
  title: string;
  lead: string;
  sketch: Sketch;
  rows: [string, string][];
}

const TONE: Record<Tone, string> = { green: "var(--green)", red: "var(--red)", blue: "#4c8dff", muted: "var(--muted)", orange: "#f0a020" };
const EDGE = 270; // 走势画到这里，右边留给文字

function Draw({ s, title }: { s: Sketch; title: string }) {
  const d = s.path.map((p, i) => `${i ? "L" : "M"}${p[0]} ${p[1]}`).join(" ");
  return (
    <svg className="sg-chart" viewBox="0 0 430 190" role="img" aria-label={`${title}示意图`}>
      {s.boxes?.map((b, i) => <rect key={`b${i}`} x={b.x1} y={b.y1} width={b.x2 - b.x1} height={b.y2 - b.y1} fill="var(--muted)" opacity={0.12} />)}
      {s.zones?.map((z, i) => (
        <rect key={`z${i}`} x={z.x1} y={Math.min(z.y1, z.y2)} width={EDGE - z.x1} height={Math.abs(z.y2 - z.y1)} fill={TONE[z.tone]} opacity={0.16} />
      ))}
      {s.levels?.map((l, i) => (
        <g key={`l${i}`}>
          <line x1={l.x1 ?? 8} x2={EDGE} y1={l.y} y2={l.y} stroke={TONE[l.tone]} strokeWidth={1.2} strokeDasharray={l.solid ? undefined : "5 4"} />
          <text x={EDGE + 6} y={l.y + 4} className="eg-text" fill={TONE[l.tone]}>{l.label}</text>
        </g>
      ))}
      <path d={d} fill="none" stroke="var(--text)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      {s.marks?.map((m, i) => (
        <g key={`m${i}`}>
          {m.dot !== false && <circle cx={m.x} cy={m.y} r={4.5} fill={TONE[m.tone ?? "blue"]} />}
          <text x={m.x + (m.dx ?? 7)} y={m.y + (m.dy ?? -8)} className="eg-text" fill={TONE[m.tone ?? "blue"]} textAnchor={m.end ? "end" : "start"}>{m.label}</text>
        </g>
      ))}
    </svg>
  );
}

const TREND: Card[] = [
  {
    title: "大周期趋势（日内趋势）",
    lead: "比刚走完的两段时间：后一段的高点和低点都比前一段高，算上涨；都更低，算下跌；一个高一个低，算没有方向。",
    sketch: {
      path: [[20, 140], [45, 95], [70, 148], [95, 100], [120, 110], [130, 112], [155, 70], [180, 118], [205, 62], [230, 75]],
      boxes: [{ x1: 20, x2: 120, y1: 95, y2: 148 }, { x1: 130, x2: 230, y1: 62, y2: 118 }],
      levels: [{ y: 62, tone: "green", label: "后一段的高点更高", x1: 130 }, { y: 118, tone: "green", label: "后一段的低点也更高", x1: 130 }],
      marks: [{ x: 50, y: 172, label: "前一段", tone: "muted", dot: false, dx: 0, dy: 0 }, { x: 160, y: 172, label: "后一段", tone: "muted", dot: false, dx: 0, dy: 0 }, { x: 276, y: 92, label: "两条都抬高 = 上涨", tone: "green", dot: false, dx: 0, dy: 0 }],
    },
    rows: [
      ["看哪一段", "超短线看 1 小时（没方向再看 30 分钟），日内看 4 小时（再看 1 小时），波段看日线（再看 4 小时）。"],
      ["超短线例外", "5 分钟刚确认、还没被收回的转折优先于上面这条：刚转上去就按向上算。"],
    ],
  },
  {
    title: "结构周期趋势（小周期）",
    lead: "看最近两个摆动高点和两个摆动低点：第二个高点、第二个低点都比前一个高，才算上涨；都更低算下跌；其余算没有方向。",
    sketch: {
      path: [[10, 110], [40, 140], [85, 85], [125, 115], [175, 55], [215, 85], [262, 50]],
      marks: [
        { x: 40, y: 140, label: "低点 1", tone: "muted", dx: -14, dy: 18 },
        { x: 85, y: 85, label: "高点 1", tone: "muted", dx: -14, dy: -10 },
        { x: 125, y: 115, label: "低点 2（更高）", tone: "green", dx: -20, dy: 18 },
        { x: 175, y: 55, label: "高点 2（更高）", tone: "green", dx: -30, dy: -10 },
        { x: 197, y: 72, label: "再收 3 根确认 → 算上涨", tone: "orange", dx: 14, dy: 44 },
      ],
    },
    rows: [
      ["什么是摆动点", "一根 K 线的高点比它左右各 3 根都高，就是摆动高点（低点相反）。所以要等后面 3 根收完才认得出来。"],
      ["看哪个周期", "超短线 5 分钟，日内 15 分钟，波段 1 小时。只看已经收盘的 K 线。"],
    ],
  },
];

const ENTRIES: Card[] = [
  {
    title: "① 蓄势突破 / 放量起爆（抓快速单边走势）",
    lead: "布林带宽收口蓄势或顺大方向贴近箱体边界：提前在边界预埋秒级突破点火单；若刚放量突破未跑远则现价跟进。",
    sketch: {
      path: [[10, 118], [38, 98], [66, 114], [94, 100], [122, 110], [150, 96], [176, 86], [212, 54], [264, 28]],
      boxes: [{ x1: 10, x2: 165, y1: 94, y2: 120 }],
      levels: [{ y: 28, tone: "green", label: "余仓 65%：ATR 移动止损不封顶" }, { y: 58, tone: "green", label: "首目标：减仓 35% + 移保本" }, { y: 94, tone: "blue", label: "点火位：箱体上沿（带量越界秒进）", solid: true }, { y: 132, tone: "red", label: "止损：边界内侧 1.8×基准 ATR" }],
      zones: [{ x1: 170, y1: 94, y2: 28, tone: "green" }, { x1: 170, y1: 94, y2: 132, tone: "red" }],
      marks: [{ x: 170, y: 90, label: "放量破位秒进", tone: "orange", dx: -16, dy: -12 }, { x: 45, y: 148, label: "波动率收口蓄势", tone: "muted", dot: false, dx: 0, dy: 0 }],
    },
    rows: [
      ["进场", "蓄势阶段预埋 entry_type=breakout，带量冲破或站稳两拍秒进（自动撤掉远端未成交的旧回踩挂单）；刚放量收出边界未跑远直接现价进。"],
      ["止损", "按基准周期 ATR 留足呼吸（超短线 ≥0.45%、日内 ≥0.55%、波段 ≥0.85%），避免手续费吃掉盈亏比。"],
      ["管理", "关闭 0.5R 过早保盈锁；迟迟不脱离成本区提前动能证伪离场，冲起来用 ATR 吊灯止损吃完整段快走势。"],
    ],
  },
  {
    title: "② 顺势浅回踩（EMA20 / 近端支撑）",
    lead: "趋势推进中段：优先看结构周期 EMA20 企稳或最近支撑，不再死挂永远回不去的老极值。",
    sketch: {
      path: [[10, 165], [55, 105], [85, 132], [135, 72], [180, 104], [228, 72], [265, 44]],
      levels: [{ y: 44, tone: "green", label: "余仓：ATR 跟踪止损" }, { y: 72, tone: "green", label: "目标 1：前高（平 40%）" }, { y: 104, tone: "blue", label: "进场：EMA20 或最近支撑", solid: true }, { y: 128, tone: "red", label: "止损：结构位外 ≥0.8×基准 ATR" }],
      zones: [{ x1: 180, y1: 104, y2: 44, tone: "green" }, { x1: 180, y1: 104, y2: 128, tone: "red" }],
      marks: [{ x: 180, y: 104, label: "浅回踩企稳进", dx: -30, dy: 18 }],
    },
    rows: [
      ["进场", "贴近 EMA20（0.45×ATR 内）且收盘顺向时直接现价进（动量延续）；离得稍远在支撑/EMA20 挂限价买单。"],
      ["撤单保护", "顺向撤单线至少留出 0.8R 且不少于挂距的 80%，避免刚挂上去微幅波动就误撤。"],
      ["保盈与跟踪", "顺势回踩单保盈线推迟到 1.2R，1.5R 起启用 ATR 跟踪止损。"],
    ],
  },
  {
    title: "③ 两个周期不一致：跟大周期",
    lead: "大周期上涨，小周期正在回调（高低点在降低）。保守和均衡这时不给位置，积极照样按大周期做多。",
    sketch: {
      path: [[10, 160], [60, 60], [85, 90], [105, 72], [135, 112], [155, 96], [185, 128], [225, 90], [265, 62]],
      levels: [{ y: 72, tone: "green", label: "目标 2：再上一个阻力" }, { y: 96, tone: "green", label: "目标 1：上方最近的阻力" }, { y: 128, tone: "blue", label: "进场：下方最近的支撑", solid: true }, { y: 152, tone: "red", label: "止损：支撑下 0.8 倍 ATR" }],
      zones: [{ x1: 185, y1: 128, y2: 72, tone: "green" }, { x1: 185, y1: 128, y2: 152, tone: "red" }],
      marks: [{ x: 12, y: 40, label: "大周期：上涨", tone: "green", dot: false, dx: 0, dy: 0 }, { x: 98, y: 52, label: "小周期在回调", tone: "orange", dot: false, dx: 0, dy: 0 }, { x: 185, y: 128, label: "在支撑接多", dx: -24, dy: 18 }],
    },
    rows: [
      ["大周期没有方向时", "按小周期的趋势做：小周期上涨就在支撑做多，下跌就在阻力做空。"],
      ["进场、止损、目标", "和 ① 一样。"],
    ],
  },
  {
    title: "④ 站上前高 / 跌破前低",
    lead: "行情判成趋势时，最近连续两根 K 线都收在上一段时间的高点之上（接受突破）。跌破前低做空相反。",
    sketch: {
      path: [[9, 122], [38, 92], [65, 152], [94, 112], [117, 100], [146, 68], [166, 74], [185, 92], [225, 60], [263, 38]],
      boxes: [{ x1: 8, x2: 117, y1: 92, y2: 152 }],
      levels: [{ y: 38, tone: "green", label: "目标 2" }, { y: 60, tone: "green", label: "目标 1：上方最近的结构位" }, { y: 92, tone: "blue", label: "进场：上一段的高点", solid: true }, { y: 114, tone: "red", label: "止损：前高下 1 倍 ATR" }],
      zones: [{ x1: 185, y1: 92, y2: 38, tone: "green" }, { x1: 185, y1: 92, y2: 114, tone: "red" }],
      marks: [{ x: 146, y: 68, label: "连续两根收在前高上", tone: "orange", dx: 4, dy: -10, end: true }, { x: 185, y: 92, label: "回踩前高进", dx: -24, dy: 18 }, { x: 40, y: 172, label: "上一段时间", tone: "muted", dot: false, dx: 0, dy: 0 }],
    },
    rows: [
      ["进场", "在前高挂限价买单等回踩；现价就贴在前高上方（不到 0.2 倍 ATR）时按现价进。"],
      ["止损", "前高下方 1 倍 ATR，最少 0.25%。"],
      ["目标", "进场价上方最近的两个结构位。"],
    ],
  },
  {
    title: "⑤ 震荡：区间两头",
    lead: "小周期没有方向，ADX 低于 20。下沿做多、上沿做空，不在中间进。",
    sketch: {
      path: [[10, 64], [42, 146], [78, 62], [112, 148], [137, 150], [180, 106], [225, 66], [263, 62]],
      boxes: [{ x1: 8, x2: EDGE, y1: 60, y2: 150 }],
      levels: [{ y: 60, tone: "green", label: "目标 2：区间上沿" }, { y: 105, tone: "green", label: "目标 1：区间中点" }, { y: 150, tone: "blue", label: "进场：区间下沿或支撑", solid: true }, { y: 172, tone: "red", label: "止损：下沿外 0.8 倍 ATR" }],
      zones: [{ x1: 137, y1: 150, y2: 60, tone: "green" }, { x1: 137, y1: 150, y2: 172, tone: "red" }],
      marks: [{ x: 137, y: 150, label: "下沿接多", dx: -20, dy: 18 }],
    },
    rows: [
      ["进场", "下沿或支撑挂限价买单，上沿或阻力挂限价卖单；贴着边沿时按现价进。"],
      ["止损", "边沿外侧 0.8 倍 ATR，最少 0.25%。"],
      ["注意", "大周期有方向时，震荡区间两头都会作为双向结构位画进盘面地图，顺大周期那一边排在前面，由 Worker 综合判断。"],
    ],
  },
  {
    title: "⑥ 刚反转：转折单",
    lead: "5 分钟 K 线收盘有力地越过最近的摆动高点，而之前的走势是向下的。只在确认后 6 根 K 线内给。这条规则没有改。",
    sketch: {
      path: [[9, 55], [36, 100], [56, 80], [86, 145], [135, 50], [171, 80], [216, 50], [263, 24]],
      levels: [{ y: 24, tone: "green", label: "目标 2：再加半段" }, { y: 50, tone: "green", label: "目标 1：已到过的最高点" }, { y: 80, tone: "blue", label: "进场：被突破的高点", solid: true }, { y: 108, tone: "red", label: "止损：下方 1 倍 ATR" }],
      zones: [{ x1: 171, y1: 80, y2: 24, tone: "green" }, { x1: 171, y1: 80, y2: 108, tone: "red" }],
      marks: [{ x: 116, y: 80, label: "收盘越过 = 确认", tone: "orange", dx: -4, dy: -10, end: true }, { x: 171, y: 80, label: "回踩进", dx: -14, dy: 18 }],
    },
    rows: [
      ["怎么算确认", "收盘越过摆动点至少 0.3 倍 ATR，这一段走出至少 2.5 倍 ATR，之后没有收回去。"],
      ["进场", "在被突破的点挂限价单等回踩；现价离它不到 0.25 倍 ATR 才按现价进。不追。"],
      ["常见结果", "价格不回踩、直接走到目标 1：单子没成交就撤掉。回放里大多数转折单是这样错过的。"],
    ],
  },
];

const STEPS = ["程序画出双向盘面地图并核成本（不替模型删方向）", "Worker 综合盘面、K 线和资金费率拍板（可采纳参考位，也可按结构微调）", "认定了方向本轮直接给 plan（不靠叫醒线拖延）", "程序立刻下单或挂限价单", "盘运行中自动盯单撤改，成交后自动管仓"];

const SKIP: [string, string][] = [
  ["认不出结构", "小周期收盘的 K 线不够，找不到两个摆动高低点或算不出 ADX。"],
  ["盈亏比不到 1", "扣掉手续费和点差后，到目标 1 赚的还不如止损亏的多。"],
  ["位置已经过时", "进场位离现价太远、有效期内按平常波动走不到；或者走势已经反转，这个位置被甩在了后面。"],
  ["这一段走完了", "还没回到进场位，现价已经过了目标 1。"],
  ["挂了单没成交", "价格没回来，过了有效期撤单。默认超短线 30 分钟，日内 3 小时，波段 12 小时。"],
  ["刚在这里平过", "同一个位置、同一个方向刚平掉一笔：超短线 10 分钟、日内 30 分钟、波段 2 小时内不再进。"],
  ["账户下不了", "已有反向持仓、不够最小下单量、保证金不够。"],
  ["Worker 有具体理由", "比如马上有重要数据、盘口异常。它要写明理由。"],
];

function Cards({ list }: { list: Card[] }) {
  return (
    <div className="sg-grid eg-grid">
      {list.map((c) => (
        <article key={c.title} className="sg-card">
          <h4>{c.title}</h4>
          <p className="sg-edge">{c.lead}</p>
          <Draw s={c.sketch} title={c.title} />
          <dl>
            {c.rows.map(([k, v]) => (
              <div key={k} className="eg-row">
                <dt>{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        </article>
      ))}
    </div>
  );
}

export function EntryGuide() {
  return (
    <details className="panel sg">
      <summary>
        <b>什么情况会开单（出手尺度：积极）</b>
        <span className="muted">程序怎么认趋势、在哪进、止损和目标放哪；只说明规则，不代表能赚钱</span>
      </summary>
      <h5>先认趋势</h5>
      <Cards list={TREND} />
      <h5>会开单的六种情况（图按做多画）</h5>
      <Cards list={ENTRIES} />
      <h5>从出位置到真正下单</h5>
      <ol className="eg-steps">
        {STEPS.map((s) => <li key={s}>{s}</li>)}
      </ol>
      <h5>不出手的情况</h5>
      <dl className="eg-skip">
        {SKIP.map(([k, v]) => (
          <div key={k} className="eg-row">
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
      <p className="muted sg-note">
        新分工：算法只画客观的双向盘面地图和参考位，不再按行情分类提前删掉方向，也不再拿纯算法盲做回测去拦 Worker 的单；由 Worker 看图结合 K 线和盘口决定做不做、直接挂单，程序负责算账、盯盘和自动撤改。
        蓝线是进场价，绿色是赚钱的区间，红色是亏钱的区间。ATR 是最近 14 根 K 线的平均波动幅度，ADX 是趋势强度。
        回测里纯算法机械照做，扣费后每笔平均是亏的（超短线约 -0.41R，日内约 -0.34R），仅作基准参考；实际效果看 Worker 决策后的战绩统计。
      </p>
    </details>
  );
}
