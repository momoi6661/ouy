import { Fragment, useEffect, useState } from "react";
import { api, fmtPx, fmtTime, type AlgoReport, type BacktestGroup, type BacktestReport, type CacheStats, type InstStatus } from "../api";

const VERDICT: Record<string, [string, string]> = { right: ["对", "ok"], wrong: ["错", "bad"], flat: ["没动", "flat"] };

/** 算法页：每个算法在不在跑、刚算出了什么；下面是它们触发过的事件、触发了什么，以及之后价格有没有照它说的走。 */
export function AlgoPanel({ coin, st, onShow }: { coin: string; st: InstStatus | undefined; onShow?: (ts: number, text: string) => void }) {
  const [report, setReport] = useState<AlgoReport | null>(null);
  const [only, setOnly] = useState("");
  useEffect(() => {
    let alive = true;
    const load = () => api.algo(coin).then((r) => alive && setReport(r), () => undefined);
    setReport(null);
    load();
    const timer = window.setInterval(load, 15_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [coin]);
  const events = (report?.events ?? []).filter((e) => !only || e.algo === only);
  return (
    <div className="ws-card wide algo">
      <RegimeCard st={st} />
      <h5>算法现在的状态</h5>
      <table className="algo-table">
        <tbody>
          {(st?.algos ?? []).map((a, i, all) => {
            const s = report?.stats[a.id];
            const judged = s ? s.right + s.wrong : 0;
            const head = a.layer && a.layer !== all[i - 1]?.layer ? report?.layers?.[a.layer] : "";
            return (
              <Fragment key={a.id}>
              {head && (
                <tr className="algo-layer">
                  <td colSpan={3}>{head}</td>
                </tr>
              )}
              <tr className={a.on ? "" : "off"}>
                <td>
                  <i className={`algo-dot ${a.on ? "on" : ""}`} />
                  <b>{a.label}</b>
                  <small>{a.on ? "在跑" : "没在跑"}{a.ts ? `，${fmtTime(a.ts)}` : ""}</small>
                </td>
                <td>
                  {a.reading}
                  <small>{a.does}</small>
                </td>
                <td className="algo-score" title="出信号 15 分钟后，价格朝它说的方向走了至少半个平均波幅算对">
                  {judged ? `${s!.right} 对 ${s!.wrong} 错` : "—"}
                </td>
              </tr>
              </Fragment>
            );
          })}
        </tbody>
      </table>
      <h5>
        触发记录
        <span className="grow" />
        <select value={only} onChange={(e) => setOnly(e.target.value)}>
          <option value="">全部算法</option>
          {Object.entries(report?.labels ?? {}).map(([id, label]) => (
            <option key={id} value={id}>{label}</option>
          ))}
        </select>
      </h5>
      {!events.length && (
        <p className="muted small">
          {(st?.algos ?? []).some((a) => a.on)
            ? "还没有触发记录。算法只在读数发生变化时记一条（行情判断变了、出了新候选、转折走到下一步、快判出信号……）。"
            : "盯盘没开，算法没在跑，所以没有记录。开启盯盘后，行情判断和候选位会在第一根结构周期 K 线收盘后记下第一条。"}
        </p>
      )}
      {events.map((e) => (
        <div key={e.id} className="algo-ev">
          <div className="algo-ev-head">
            <time>{fmtTime(e.ts)}</time>
            <b>{report?.labels[e.algo] ?? e.algo}</b>
            {onShow && (
              <button className="btn xs" title="切到 K 线图，滚到这条记录触发的那根 K 线" onClick={() => onShow(e.ts, report?.labels[e.algo] ?? e.algo)}>
                在图上看
              </button>
            )}
            {e.side && <span className={e.side === "long" ? "up" : "down"}>{e.side === "long" ? "看涨" : "看跌"}</span>}
            {!!e.px && <span className="muted">当时 {fmtPx(e.px)}</span>}
            <span className="grow" />
            {(["15m", "1h"] as const).map((k) => {
              const c = e.checks[k];
              if (!c) return e.side ? <span key={k} className="algo-chk wait">{k === "15m" ? "15 分钟" : "1 小时"}后核对</span> : null;
              return (
                <span key={k} className={`algo-chk ${VERDICT[c.ok][1]}`} title={`当时 ${fmtPx(e.px)} → ${fmtPx(c.px)}`}>
                  {k === "15m" ? "15 分钟" : "1 小时"}：{VERDICT[c.ok][0]}（{c.move >= 0 ? "+" : ""}{c.move}%）
                </span>
              );
            })}
          </div>
          <p>{e.text}</p>
          <p className="algo-effect">触发了：{e.effect || "只记录"}</p>
        </div>
      ))}
      <BacktestCard />
      <CacheCard coin={coin} />
    </div>
  );
}

const signed = (v: number, digits = 2) => `${v >= 0 ? "+" : ""}${v.toFixed(digits)}`;
const tone = (v: number) => (v > 0 ? "up" : v < 0 ? "down" : "");

/** 最近一次复盘时程序判断的行情，和它给 Worker 的候选位：入场、止损、目标、成本占多少 R，以及这条打法照做的回测成绩。 */
function RegimeCard({ st }: { st: InstStatus | undefined }) {
  const r = st?.regime;
  if (!r) return <p className="muted small">行情判断和候选位由程序在下一根结构周期 K 线收盘后算出。</p>;
  return (
    <>
      <h5>
        行情判断
        <span className={`algo-badge ${r.kind}`}>{r.label}{r.side ? (r.side === "long" ? " · 偏多" : " · 偏空") : ""}</span>
        <span className="grow" />
        <small className="muted">{fmtTime(r.ts)}</small>
      </h5>
      <p className="algo-note">
        {r.why}。{r.plays.length ? `这种行情自动档只给：${r.plays.join("、")}。` : "这种行情自动档不出候选。"}
      </p>
      {r.holding && <p className="muted small">当时有持仓，不出新的入场候选。</p>}
      {!r.holding && !r.candidates.length && <p className="muted small">这一轮没有匹配的结构，没有候选位。</p>}
      {!!r.candidates.length && (
        <table className="algo-table cand">
          <thead>
            <tr><th>打法</th><th>入场</th><th>止损</th><th>目标</th><th title="一进一出的手续费和点差，相当于止损距离的多少">成本</th><th>净盈亏比</th><th>核对</th></tr>
          </thead>
          <tbody>
            {r.candidates.map((c, i) => (
              <tr key={i} className={c.ok ? "" : "off"}>
                <td>
                  <b className={c.side === "long" ? "up" : "down"}>{c.side === "long" ? "多" : "空"}</b> {c.strategy}
                  <small>{c.align}｜{c.basis}</small>
                  {c.evidence && <small title="照这条打法机械地做的历史回测，不是对这一笔的预测">回测：{c.evidence}</small>}
                </td>
                <td>{fmtPx(c.entry)}<small>{c.how === "now" ? "现价" : c.how === "breakout" ? "突破" : "限价"}</small></td>
                <td>{fmtPx(c.stop)}<small>{c.stop_pct}%</small></td>
                <td>{c.targets.map((t) => fmtPx(t)).join(" / ")}</td>
                <td className={c.cost_r !== null && c.cost_r >= 0.25 ? "down" : ""}>{c.cost_r === null ? "—" : `${c.cost_r}R`}</td>
                <td>{c.net_rr}</td>
                <td>{c.ok ? <span className="up">合格</span> : <span className="muted" title={c.why_not}>不合格<small>{c.why_not}</small></span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

/** 累计 R 的走势：横轴是平仓时间，纵轴是从头累加的净 R。 */
function EquityCurve({ points }: { points: [number, number][] }) {
  if (points.length < 2) return <p className="muted small">笔数太少，画不出曲线。</p>;
  const W = 640, H = 150, pad = 4;
  const t0 = points[0][0], t1 = points[points.length - 1][0];
  const lo = Math.min(0, ...points.map((p) => p[1])), hi = Math.max(0, ...points.map((p) => p[1]));
  const x = (t: number) => pad + ((t - t0) / Math.max(t1 - t0, 1)) * (W - pad * 2);
  const y = (v: number) => pad + (1 - (v - lo) / Math.max(hi - lo, 1e-9)) * (H - pad * 2);
  const step = Math.max(1, Math.floor(points.length / 600));
  const shown = points.filter((_, i) => i % step === 0 || i === points.length - 1);
  const end = points[points.length - 1][1];
  const day = (t: number) => new Date(t).toISOString().slice(0, 10);
  return (
    <div className="bt-curve">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="累计净 R 曲线">
        <line x1={pad} x2={W - pad} y1={y(0)} y2={y(0)} className="bt-zero" />
        <polyline points={shown.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ")} className={end >= 0 ? "bt-line up" : "bt-line down"} />
      </svg>
      <div className="bt-axis">
        <span>{day(t0)}</span>
        <span>最高 {signed(hi, 1)}R ｜ 最低 {signed(lo, 1)}R ｜ 最后 {signed(end, 1)}R</span>
        <span>{day(t1)}</span>
      </div>
    </div>
  );
}

function GroupRow({ g }: { g: BacktestGroup }) {
  return (
    <tr className={g.label === "全部" ? "total" : ""}>
      <td>{g.label}</td>
      <td>{g.n}</td>
      <td>{(g.win * 100).toFixed(0)}%</td>
      <td className={tone(g.avg_r)}>{signed(g.avg_r)}R</td>
      <td className="muted">{g.ci_lo === null || g.ci_hi === null ? "—" : `${signed(g.ci_lo)} ~ ${signed(g.ci_hi)}`}</td>
      <td>{g.pf === null ? "—" : g.pf.toFixed(2)}</td>
      <td>{g.max_dd_r.toFixed(1)}R</td>
      <td className={g.verdict === "扣费后赚" ? "up" : g.verdict === "扣费后亏" ? "down" : "muted"}>{g.verdict}</td>
    </tr>
  );
}

/** 存盘的回测报告（python -m ouy.backtest_plans ... --save）：每条打法机械照做、扣掉手续费和滑点之后的成绩。 */
function BacktestCard() {
  const [reports, setReports] = useState<BacktestReport[] | null>(null);
  const [name, setName] = useState("");
  useEffect(() => {
    api.backtests().then(setReports, () => setReports([]));
  }, []);
  const report = reports?.find((r) => r.name === name) ?? reports?.[0];
  return (
    <>
      <h5>
        回测：照算法机械地做
        <span className="grow" />
        {!!reports?.length && (
          <select value={report?.name ?? ""} onChange={(e) => setName(e.target.value)}>
            {reports.map((r) => (
              <option key={r.name} value={r.name}>{r.title}</option>
            ))}
          </select>
        )}
      </h5>
      {reports && !reports.length && (
        <p className="muted small">
          还没有存盘的回测。运行 <code>python -m ouy.backtest_plans BTC-USDT-SWAP ETH-USDT-SWAP --days 190 --style intraday --save</code> 后这里会显示。
        </p>
      )}
      {report && (
        <>
          <p className="algo-note">
            {(report.meta.instruments as string[] | undefined)?.map((i) => i.split("-")[0]).join(" / ")}，{report.meta.days} 天，截止 {report.meta.end ?? "运行当时"}，
            {report.meta.fills}{report.meta.rules ? `；${report.meta.rules}` : ""}。{fmtTime(report.saved_at)} 跑的。
            平均一栏后面是 95% 区间：整段在 0 的一侧、且笔数够，才写赚或亏。这是历史回放，不是对下一笔的预测。
          </p>
          <EquityCurve points={report.equity} />
          <table className="algo-table bt">
            <thead>
              <tr><th>分组</th><th>笔数</th><th>胜率</th><th>平均</th><th>95% 区间</th><th title="赚的总和 ÷ 亏的总和">盈亏比</th><th>最大回撤</th><th>结论</th></tr>
            </thead>
            <tbody>
              {report.groups.map((g) => <GroupRow key={g.label} g={g} />)}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}

/** 模型缓存命中：服务端返回的真实读数。没有读数的轮次不算进来，不当成 0。 */
function CacheCard({ coin }: { coin: string }) {
  const [stats, setStats] = useState<CacheStats | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.cacheStats().then((r) => alive && setStats(r), () => undefined);
    load();
    const timer = window.setInterval(load, 60_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);
  const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v}%`);
  const rows = stats ? [["全部品种", stats] as const, ...Object.entries(stats.instruments).map(([k, v]) => [k, v] as const)] : [];
  const recent = (stats?.recent ?? []).filter((r) => r.input_tokens > 0);
  return (
    <>
      <h5>模型缓存命中（最近 {stats?.reviews ?? 0} 轮复盘）</h5>
      {!stats?.reviews && <p className="muted small">还没有读数。Worker 复盘一轮之后，这里显示模型服务返回的缓存读取量。</p>}
      {!!stats?.reviews && (
        <>
          <p className="algo-note">
            命中率 = 从缓存读的输入 ÷ 全部输入。「首请求」只算一轮里的第一次请求，反映提示词开头有多少是不变的；「不变部分」是提示词里排在前面、不随报价变化的那一段占多少。
            {stats.unmeasured_requests ? `有 ${stats.unmeasured_requests} 次请求模型服务没有返回用量，没算进来。` : ""}
            {stats.cache_write_tokens
              ? `模型服务另外上报了 ${(stats.cache_write_tokens / 1000).toFixed(0)}k 的缓存写入量。`
              : "模型服务没有上报缓存写入量（这个接口只报读取量），所以中转站后台的「写入」一栏是 0，不代表没写进缓存。"}
          </p>
          <table className="algo-table bt">
            <thead>
              <tr><th>品种</th><th>轮数</th><th>输入</th><th>缓存读取</th><th>命中率</th><th>首请求命中率</th><th>提示词不变部分</th></tr>
            </thead>
            <tbody>
              {rows.map(([label, s]) => (
                <tr key={label} className={label === "全部品种" ? "total" : label === coin ? "here" : ""}>
                  <td>{label.replace("-USDT-SWAP", "")}</td>
                  <td>{s.reviews}</td>
                  <td>{(s.input_tokens / 1000).toFixed(0)}k</td>
                  <td>{(s.cached_tokens / 1000).toFixed(0)}k</td>
                  <td>{pct(s.hit_pct)}</td>
                  <td>{pct(s.first_hit_pct)}</td>
                  <td>{pct(s.stable_prompt_pct)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="cache-bars" title="最近每一轮的命中率，从左到右由旧到新">
            {recent.map((r, i) => (
              <i key={i} style={{ height: `${Math.max(4, Math.round((r.cached_tokens / r.input_tokens) * 100))}%` }} title={`${fmtTime(r.ts)} ${r.inst_id.split("-")[0]}：${Math.round((r.cached_tokens / r.input_tokens) * 100)}%`} />
            ))}
          </div>
        </>
      )}
    </>
  );
}
