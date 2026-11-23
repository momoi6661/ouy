import { useEffect, useState } from "react";
import { ACTION_KINDS, LOOK_BARS, STANCES, STYLES, type Stance, type Style, type Wake, KIND_LABEL, permissionFor, api, EFFORTS, type AgentConfig, type AppState, type Effort, type JournalEntry, type LlmInfo, type LogsInfo, type Mode, type Permission } from "../api";
import { Journal } from "../components/Journal";
import { SkillsPanel } from "../components/SkillsPanel";
import { Modal, Num, toast } from "../components/ui";

/** 模型接口：Base URL、API Key、默认模型。存在本机，保存后立即生效，不用改 .env、不用重启 */
function LlmCard() {
  const [info, setInfo] = useState<LlmInfo | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState("");
  const load = () =>
    api.llm().then((r) => {
      setInfo(r);
      setBaseUrl(r.base_url);
      setModel(r.model);
      setKey("");
    });
  useEffect(() => {
    load().catch(() => {});
  }, []);
  const save = async () => {
    setBusy("save");
    try {
      await api.saveLlm({ base_url: baseUrl, model, ...(key.trim() ? { api_key: key.trim() } : {}) });
      await load();
      toast("模型接口已保存，立即生效", "ok");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy("");
    }
  };
  const test = async () => {
    setBusy("test");
    try {
      const r = await api.testLlm();
      toast(r.message, r.ok ? "ok" : "error");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy("");
    }
  };
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>模型接口</h3>
      </div>
      <>
      <label className="field">
        <span>Base URL（接口地址）</span>
        <input className="input" placeholder="https://api.openai.com/v1" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
      </label>
      <label className="field">
        <span>API Key {info?.key_set ? `（已填，结尾 ${info.key_hint || "已隐藏"}；留空就是不改）` : "（还没填）"}</span>
        <input className="input" type="password" autoComplete="off" placeholder={info?.key_set ? "留空 = 保留现在的密钥" : "sk-…"} value={key} onChange={(e) => setKey(e.target.value)} />
      </label>
      <label className="field">
        <span>默认模型</span>
        <input className="input" placeholder="比如 gpt-4o" value={model} onChange={(e) => setModel(e.target.value)} />
      </label>
      <div className="row">
        <button className="btn primary sm" disabled={!!busy} onClick={save}>
          {busy === "save" ? "保存中…" : "保存"}
        </button>
        <button className="btn sm" disabled={!!busy} onClick={test}>
          {busy === "test" ? "测试中…" : "测试连接"}
        </button>
      </div>
      <p className="muted small">密钥只存在你这台电脑的数据目录里，页面上不会再显示完整内容。这里填的优先于 .env；把某一项清空再保存，就改回 .env 里的值。Router、Worker、子代理各自的模型名在下面单独设置，共用这个地址和密钥。</p>
      </>
      <ProxyBox />

    </div>
  );
}

interface Props {
  state: AppState;
  journal: JournalEntry[];
  redUp: boolean;
  setRedUp: (v: boolean) => void;
  refresh: () => void;
}

const MODES: { id: Mode; title: string; desc: string }[] = [
  { id: "auto", title: "全自动", desc: "Agent 自己分析、自己开平仓、自己调杠杆和止损。你不用管。" },
  { id: "confirm", title: "下单前确认", desc: "Agent 想开仓/加仓时先问你，10 分钟内点确认才执行。" },
  { id: "advise", title: "只给建议", desc: "Agent 不会自己动你的账户。它想做的每一步（开仓、平仓、移止损、布置策略）都是一张卡片，你点「同意执行」才做。" },
];

export function AgentSettings({ state, journal, redUp, setRedUp, refresh }: Props) {
  const [cfg, setCfg] = useState<AgentConfig>(state.config);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [liveAsk, setLiveAsk] = useState(false);
  const [liveText, setLiveText] = useState("");
  const [newInst, setNewInst] = useState("");

  useEffect(() => {
    if (!dirty) setCfg(state.config);
  }, [state.config, dirty]);

  const patch = (p: Partial<AgentConfig>) => {
    setCfg({ ...cfg, ...p });
    setDirty(true);
  };
  const risk = (p: Partial<AgentConfig["risk"]>) => patch({ risk: { ...cfg.risk, ...p } });
  const n = (v: number | "", fallback: number) => (v === "" ? fallback : v);

  const save = async (confirmLive = false) => {
    if (cfg.env === "live" && state.config.env !== "live" && !confirmLive) {
      setLiveText("");
      setLiveAsk(true);
      return;
    }
    setSaving(true);
    try {
      await api.saveConfig(cfg, confirmLive);
      toast("设置已保存", "ok");
      setDirty(false);
      setLiveAsk(false);
      refresh();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setSaving(false);
    }
  };

  const addInst = () => {
    let inst = newInst.trim().toUpperCase();
    if (!inst) return;
    if (!inst.includes("-")) inst = `${inst}-USDT-SWAP`;
    if (!cfg.instruments.includes(inst)) patch({ instruments: [...cfg.instruments, inst] });
    setNewInst("");
  };

  return (
    <div className="page two-col">
      <div className="stack">
        <div className="panel">
          <div className="panel-head">
            <h3>交易环境</h3>
          </div>
          <div className="seg two">
            <button className={cfg.env === "demo" ? "on" : ""} onClick={() => patch({ env: "demo" })}>
              模拟盘
            </button>
            <button className={cfg.env === "live" ? "on live" : ""} onClick={() => patch({ env: "live" })}>
              实盘
            </button>
          </div>
          <p className="muted small">模拟盘用的是欧易的模拟交易环境，资金是虚拟的。先在模拟盘跑顺了再切实盘。切换环境会先停止盯盘。</p>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h3>Agent 的权限</h3>
          </div>
          <div className="mode-list">
            {MODES.map((m) => (
              <label key={m.id} className={`mode ${cfg.mode === m.id ? "on" : ""}`}>
                <input type="radio" name="mode" checked={cfg.mode === m.id} onChange={() => patch({ mode: m.id, permissions: {} })} />
                <span>
                  <b>{m.title}</b>
                  <em>{m.desc}</em>
                </span>
              </label>
            ))}
          </div>
          {cfg.mode === "confirm" && (
            <label className="check">
              <input type="checkbox" checked={cfg.auto_exit} onChange={(e) => patch({ auto_exit: e.target.checked, permissions: {} })} />
              减仓、平仓、撤单不用等我确认（走势不对时让它能马上跑）
            </label>
          )}
          <label className="check">
            <input type="checkbox" checked={cfg.hands_off_manual} onChange={(e) => patch({ hands_off_manual: e.target.checked })} />
            我手动开的仓 Agent 只给建议，不替我动（默认不勾：我自己开的仓，包括在欧易 App 里开的，也由 Worker 盯着并帮我移止损、减仓、止盈；不在盯盘列表里的币有持仓时会自动加入盯盘）
          </label>
          <h4>按动作单独设置（改这里会覆盖上面的预设）</h4>
          <div className="perm-grid">
            {ACTION_KINDS.map((kind) => (
              <label key={kind} className="field">
                <span>{KIND_LABEL[kind]}</span>
                <select value={permissionFor(cfg, kind)} onChange={(e) => patch({ permissions: { ...cfg.permissions, [kind]: e.target.value as Permission } })}>
                  <option value="auto">自动执行</option>
                  <option value="confirm">先问我</option>
                  <option value="off">不允许</option>
                </select>
              </label>
            ))}
          </div>
          <p className="muted small">「先问我」的动作会变成待确认请求，出现在盯盘页和对话页，10 分钟内点执行才生效。平仓设成「自动执行」时，紧急平仓线才会自动生效。</p>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h3>盯盘列表</h3>
            <span className="muted small">最多 8 个 U 本位永续</span>
          </div>
          <div className="chips">
            {cfg.instruments.map((i) => (
              <span key={i} className="chip on">
                {i.replace("-USDT-SWAP", "")}
                <button onClick={() => patch({ instruments: cfg.instruments.filter((x) => x !== i) })} aria-label={`移除 ${i}`}>
                  ×
                </button>
              </span>
            ))}
          </div>
          <div className="row">
            <input className="input sm" placeholder="输入币种，如 SOL" value={newInst} onChange={(e) => setNewInst(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addInst()} />
            <button className="btn sm" onClick={addInst}>
              添加
            </button>
          </div>
          <label className="field">
            <span>保证金模式</span>
            <select value={cfg.margin_mode} onChange={(e) => patch({ margin_mode: e.target.value as "cross" | "isolated" })}>
              <option value="cross">全仓</option>
              <option value="isolated">逐仓</option>
            </select>
          </label>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h3>硬性风控（Agent 改不了）</h3>
          </div>
          <div className="form-grid">
            <label className="field">
              <span>最高杠杆</span>
              <Num value={cfg.risk.max_leverage} onChange={(v) => risk({ max_leverage: n(v, 1) })} min={1} max={125} step={1} suffix="x" />
            </label>
            <label className="field">
              <span>单仓最多亏（占权益）</span>
              <Num value={cfg.risk.risk_per_trade_pct} onChange={(v) => risk({ risk_per_trade_pct: n(v, 1) })} min={0.1} max={10} step={0.1} suffix="%" />
            </label>
            <label className="field">
              <span>当日亏损上限（0 = 不限）</span>
              <Num value={cfg.risk.max_daily_loss_pct} onChange={(v) => risk({ max_daily_loss_pct: n(v, 0) })} min={0} max={100} step={0.5} suffix="%" />
            </label>
            <label className="field">
              <span>紧急平仓线（单仓浮亏占权益）</span>
              <Num value={cfg.risk.emergency_exit_loss_pct} onChange={(v) => risk({ emergency_exit_loss_pct: n(v, 2.5) })} min={0.5} max={50} step={0.5} suffix="%" />
            </label>
          </div>
          <p className="muted small">
            仓位大小不由 Agent 说了算：系统按「权益 × 单仓风险 ÷ 止损距离」计算张数。达到当日亏损上限后只允许减仓和平仓；单仓浮亏触及紧急线时不等 Agent，直接市价平掉。
          </p>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h3>补仓 / 无限子弹（翻倍补仓）</h3>
          </div>
          <label className="check">
            <input type="checkbox" checked={cfg.risk.allow_averaging_down} onChange={(e) => risk({ allow_averaging_down: e.target.checked })} />
            允许亏损时补仓。关着的时候只允许浮盈加仓，无限子弹打法不能用
          </label>
          <div className="form-grid">
            <label className="field">
              <span>最多补几层（同一持仓最多加仓次数）</span>
              <Num value={cfg.risk.max_adds} onChange={(v) => risk({ max_adds: n(v, 0) })} min={0} max={10} step={1} suffix="次" />
            </label>
          </div>
          <p className="muted small">
            无限子弹的保证金是有限的，这几项是它的硬限制：补到「最多补几层」就不再补；所有层共用一个止损价，整组打到止损的亏损不能超过上面的「单仓最多亏」；整个持仓浮亏触及「紧急平仓线」时不等
            Agent 直接全平；总占用受可用保证金限制。翻倍补仓每补一层保证金翻一倍、离强平更近，只能用低杠杆。
          </p>
        </div>

        <LlmCard />
        <div className="panel">
          <div className="panel-head">
            <h3>Agent 团队（Router 和 Worker）</h3>
          </div>
          <p className="muted small">
            Router 负责和你对话、看全局、分派任务，自己不下单。每个盯盘的币有一个交易 Worker（唯一能下单的）和一个情报 Worker（只搜集信息）。每个币的单独设置（交易风格、出手尺度、入场方式、审批、模型）在工作台选中那个币后的「Worker」一栏，或者盯盘页的「Worker 总览」面板里改。
          </p>
          <label className="field">
            <span>出手尺度</span>
            <select value={cfg.stance} onChange={(e) => patch({ stance: e.target.value as Stance })}>
              {STANCES.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                </option>
              ))}
            </select>
          </label>
          <p className="muted small">{STANCES.find((x) => x.id === cfg.stance)?.desc}。不管选哪种，硬性风控都不变。</p>
          <label className="field">
            <span>最多同时有几个 Worker 在思考（不同的币并行，超出的排队；同一个 Worker 一次只做一件事）</span>
            <input className="input" type="number" min={1} max={8} step={1} value={cfg.max_parallel} onChange={(e) => patch({ max_parallel: Math.min(Math.max(Number(e.target.value) || 1, 1), 8) })} />
          </label>
          <label className="field">
            <span>入场方式</span>
            <select value={cfg.entry_mode} onChange={(e) => patch({ entry_mode: e.target.value as AgentConfig["entry_mode"] })}>
              <option value="market">市价优先：看好方向就按现价直接进</option>
              <option value="limit">挂单优先：把单挂在结构位上等价格回来</option>
              <option value="auto">Agent 自己定</option>
            </select>
          </label>
          <label className="check">
            <input type="checkbox" checked={cfg.auto_arm_plan ?? true} onChange={(e) => patch({ auto_arm_plan: e.target.checked })} />
            有计划就挂单/下单：Worker 复盘给出计划而这个币空仓时，等回踩的直接在欧易挂真实限价单（带止损止盈与变盘自动撤单），现价到位的直接市价进，照样过审批和硬性风控
          </label>
          <div className="field-row">
            <label className="field">
              <span>算法快判模式（每 4 秒识别变盘回踩等信号）</span>
              <select value={cfg.fast_entry ?? "verify"} onChange={(e) => patch({ fast_entry: e.target.value as AgentConfig["fast_entry"] })}>
                <option value="verify">出信号叫醒 Worker 核实（推荐）</option>
                <option value="agent">Worker 授权的方向直接下单，其余叫醒核实</option>
                <option value="auto">两个方向都直接下单</option>
                <option value="off">关闭快判</option>
              </select>
            </label>
            <label className="field">
              <span>挂单手续费（%，单边 Maker）</span>
              <input className="input" type="number" min={0} max={1} step={0.005} value={cfg.maker_fee_pct ?? 0.02} onChange={(e) => patch({ maker_fee_pct: Math.min(Math.max(Number(e.target.value) || 0, 0), 1) })} />
            </label>
            <label className="field">
              <span>吃单手续费（%，单边 Taker）</span>
              <input className="input" type="number" min={0} max={1} step={0.005} value={cfg.taker_fee_pct ?? 0.05} onChange={(e) => patch({ taker_fee_pct: Math.min(Math.max(Number(e.target.value) || 0, 0), 1) })} />
            </label>
          </div>
          <div className="field-row">
            <label className="field">
              <span>限价单离现价最多多远（%，0 = 按风格自动）</span>
              <input className="input" type="number" min={0} max={20} step={0.1} value={cfg.limit_dist_pct} onChange={(e) => patch({ limit_dist_pct: Number(e.target.value) || 0 })} />
            </label>
            <label className="field">
              <span>挂单多少分钟没成交就处理（0 = 按风格自动）</span>
              <input className="input" type="number" min={0} max={1440} step={1} value={cfg.stale_minutes} onChange={(e) => patch({ stale_minutes: Number(e.target.value) || 0 })} />
            </label>
          </div>
          <p className="muted small">
            Agent 的限价单超过这个距离会被拒绝，让它改用市价。挂单到时间还没成交，Agent 会被叫醒：方向还成立就撤单改市价进场，否则撤单。按风格自动时：超短线 0.3% / 10 分钟，日内 1% / 45 分钟，波段 3% / 240 分钟。
          </p>
          <label className="field">
            <span>Worker 的唤醒方式（默认值）</span>
            <select value={cfg.wake} onChange={(e) => patch({ wake: e.target.value as Wake })}>
              <option value="event">定时看盘时没变化就跳过（省 token）</option>
              <option value="always">每次定时看盘都看（K 线收盘时）</option>
            </select>
          </label>
          <label className="check">
            <input type="checkbox" checked={cfg.slow_path} onChange={(e) => patch({ slow_path: e.target.checked })} />
            事件二级过滤：价格波动、定时这类事件先让 Router 判断值不值得唤醒 Worker（触及盯盘价位、成交、持仓变化不过滤）
          </label>
          <div className="form-grid">
            <label className="field">
              <span>Router 模型</span>
              <input className="input" placeholder={state.ai.model} value={cfg.router_model} onChange={(e) => patch({ router_model: e.target.value.trim() })} />
            </label>
            <label className="field">
              <span>交易 Worker 模型</span>
              <input className="input" placeholder={state.ai.model} value={cfg.worker_model} onChange={(e) => patch({ worker_model: e.target.value.trim() })} />
            </label>
            <label className="field">
              <span>情报 Worker 模型</span>
              <input className="input" placeholder={cfg.worker_model || state.ai.model} value={cfg.intel_model} onChange={(e) => patch({ intel_model: e.target.value.trim() })} />
            </label>
            <label className="field">
              <span>子代理定时更新（0 = 不定时，只在需要时才跑）</span>
              <Num value={cfg.intel_minutes} onChange={(v) => patch({ intel_minutes: n(v, 0) })} min={0} max={1440} step={5} suffix="分钟" />
            </label>
          </div>
          <label className="check">
            <input type="checkbox" checked={cfg.web_search ?? true} onChange={(e) => patch({ web_search: e.target.checked })} />
            情报子代理可以联网搜索（用模型自带的搜索查新闻、公告、监管消息，并写明来源；模型接口不支持时自动不用）
          </label>
          <label className="check">
            <input type="checkbox" checked={cfg.stream ?? true} onChange={(e) => patch({ stream: e.target.checked })} />
            流式输出（Agent 边想边出字，不等整轮跑完才显示；模型接口不支持流式、对话报错时关掉）
          </label>
          <h4>Worker 手下的子代理（都不能下单；关掉的 Worker 不能派，画布上也不显示）</h4>
          {(
            [
              ["risk", "风险复核：下大单、用高杠杆、逆势补仓之前，复核止损、强平距离和总敞口"],
              ["intel", "情报：行情看不清或突然异动时，查资金费率、持仓量、新闻和财经日历"],
              ["smart", "聪明钱：看顶级交易员在这个币上的多空和持仓"],
            ] as const
          ).map(([role, text]) => (
            <label className="check" key={role}>
              <input type="checkbox" checked={cfg.subagents?.[role] ?? role !== "smart"} onChange={(e) => patch({ subagents: { ...(cfg.subagents ?? {}), [role]: e.target.checked } })} />
              {text}
            </label>
          ))}
          <p className="muted small">模型名留空就用 .env 里配置的那个（{state.ai.model}），填的名字必须是同一个接口地址下可用的模型。情报更新间隔填 0 是关闭定时更新，仍可以手动或让 Router 派它去查。</p>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h3>盈利回吐、浮亏扩大时让 Agent 判断</h3>
          </div>
          <label className="check">
            <input type="checkbox" checked={cfg.giveback_wake} onChange={(e) => patch({ giveback_wake: e.target.checked })} />
            一笔仓的浮盈从最高点回吐到下面的比例时，立刻叫醒它的 Worker 判断：趋势里的正常震荡就继续拿，走弱就减仓或平仓，方向反了就平仓并考虑反手
          </label>
          <div className="form-grid">
            <label className="field">
              <span>回吐多少叫醒（0 = 按风格）</span>
              <Num value={cfg.giveback_pct} onChange={(v) => patch({ giveback_pct: n(v, 0) })} min={0} max={90} step={5} suffix="%" />
            </label>
            <label className="field">
              <span>浮亏到止损距离的多少叫醒（0 = 关闭）</span>
              <Num value={Math.round(cfg.loss_wake_r * 100)} onChange={(v) => patch({ loss_wake_r: n(v, 60) / 100 })} min={0} max={95} step={5} suffix="%" />
            </label>
          </div>
          <label className="check">
            <input type="checkbox" checked={cfg.giveback_backstop} onChange={(e) => patch({ giveback_backstop: e.target.checked })} />
            保底：回吐继续扩大而 Worker 还没给出结论（模型慢或出错）时，先把这笔仓的止损移到保本
          </label>
          <p className="muted small">
            按风格的默认值：超短线浮盈到过 0.6R 后回吐 30% 叫醒，日内 1R / 40%，波段 1.5R / 55%（R = 入场到止损的距离）。规则只负责第一时间发现，减仓、平仓、反手由 Worker 看行情后决定，照样受你的权限和风控限制。
          </p>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h3>盯盘节奏</h3>
          </div>
          <div className="form-grid">
            <label className="field">
              <span>看盘间隔</span>
              <Num value={cfg.tick_seconds} onChange={(v) => patch({ tick_seconds: n(v, 3) })} min={1} max={60} step={1} suffix="秒" />
            </label>
            <label className="field">
              <span title="每到这个周期的 K 线收盘（也就是下一根开盘）后 10 秒叫 Worker 看一次盘。跟风格自动：空仓 超短线 15 分钟 / 日内 1 小时 / 波段 4 小时，有仓 5 / 15 / 60 分钟。选了固定周期就不分空仓有仓都按它；每个 Worker 可以在 Worker 总览里单独选">定时看盘按哪个盘</span>
              <select value={cfg.look_minutes ?? 0} onChange={(e) => patch({ look_minutes: Number(e.target.value) })}>
                {LOOK_BARS.map((x) => (
                  <option key={x.id} value={x.id}>
                    {x.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span title="触发线、转折、连续走线、新候选、价格波动、快判这些行情事件，每个品种每小时最多叫醒 Worker 几次；超过的先记着，并到下一次看盘。持仓变化、仓位告警、你的留言不受限制">行情事件每小时最多叫醒</span>
              <Num value={cfg.max_event_wakes} onChange={(v) => patch({ max_event_wakes: n(v, 6) })} min={1} max={60} step={1} suffix="次" />
            </label>
            <label className="field">
              <span>两次复盘最短间隔</span>
              <Num value={cfg.min_review_seconds} onChange={(v) => patch({ min_review_seconds: n(v, 45) })} min={10} max={600} step={5} suffix="秒" />
            </label>
            <label className="field">
              <span>价格波动多少立刻叫醒 Agent</span>
              <Num value={cfg.wake_move_pct} onChange={(v) => patch({ wake_move_pct: n(v, 0.6) })} min={0.1} max={20} step={0.1} suffix="%" />
            </label>
          </div>
          <label className="field">
            <span>手动下单的默认止损（亏掉投入保证金的百分之多少）</span>
            <Num value={cfg.default_stop_pct} onChange={(v) => patch({ default_stop_pct: v === "" ? 10 : v })} min={0.5} max={100} step={0.5} suffix="%" />
          </label>
          <p className="muted small">打开下单窗时止损会自动停在这个位置。比如 10% 配 10 倍杠杆，就是价格反向走 1% 止损。</p>
          <label className="field">
            <span>默认交易风格（每个币可以在盯盘页单独改）</span>
            <select value={cfg.style} onChange={(e) => patch({ style: e.target.value as Style })}>
              {STYLES.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.label}
                </option>
              ))}
            </select>
          </label>
          <p className="muted small">{STYLES.find((x) => x.id === cfg.style)?.desc}。不管选哪种，Agent 都会时刻盯着 BTC：BTC 15 分钟内波动超过 0.8% 会立刻检查所有持仓。</p>
          <div className="form-grid">
            {(
              [
                ["router_effort", "Router 的思考强度（分发、收尾）"],
                ["reasoning_effort", "Worker 的思考强度（复盘、下单）"],
                ["sub_effort", "子代理的思考强度"],
              ] as const
            ).map(([field, label]) => (
              <label className="field" key={field}>
                <span>{label}</span>
                <select value={cfg[field] ?? "medium"} onChange={(e) => patch({ [field]: e.target.value as Effort })}>
                  {EFFORTS.map((e) => (
                    <option key={e.id} value={e.id}>
                      {e.label}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>
          <p className="muted small">思考强度越高判断越细，但越慢、越费 token。超短线建议 Worker 用「低」或「中」；每个币还可以在 Worker 总览里单独设。</p>
          <p className="muted small">思考强度越高，Agent 分析越细，但每次复盘更慢、更费 token。对话框里可以单独选。Router 只负责把事情分给 Worker 和收尾，默认用「低」就够，调高只会让分发变慢。</p>
          <p className="muted small">每次复盘会调用一次 模型（当前：{state.ai.model}）。间隔越短、品种越多，模型费用越高。</p>
        </div>

        <SkillsPanel />

        <div className="panel">
          <div className="panel-head">
            <h3>给 Agent 的额外交代</h3>
          </div>
          <textarea className="input" rows={4} maxLength={2000} placeholder="例如：只做 BTC 和 ETH 的趋势单，不做震荡；晚上 12 点后不开新仓。" value={cfg.notes} onChange={(e) => patch({ notes: e.target.value })} />
        </div>

        <div className="panel">
          <div className="panel-head">
            <h3>界面</h3>
          </div>
          <label className="check">
            <input type="checkbox" checked={redUp} onChange={(e) => setRedUp(e.target.checked)} />
            红涨绿跌（默认跟欧易一样绿涨红跌）
          </label>
        </div>

        <div className="save-bar">
          <button className="btn primary" disabled={!dirty || saving || !cfg.instruments.length} onClick={() => save()}>
            {saving ? "保存中…" : "保存设置"}
          </button>
          {dirty && (
            <button
              className="btn"
              onClick={() => {
                setCfg(state.config);
                setDirty(false);
              }}
            >
              放弃修改
            </button>
          )}
          {!cfg.instruments.length && <span className="warn small">盯盘列表不能为空</span>}
        </div>
      </div>

      <div className="panel journal-full">
        <div className="panel-head">
          <h3>全部日志</h3>
          <span className="muted small">{state.config.env === "demo" ? "模拟盘" : "实盘"}</span>
        </div>
        <LogCleaner days={cfg.log_keep_days ?? 7} onDays={(n) => patch({ log_keep_days: n })} onCleared={refresh} />
        <Journal entries={journal} />
      </div>

      {liveAsk && (
        <Modal title="切换到实盘" onClose={() => setLiveAsk(false)}>
          <p>
            实盘用的是你欧易账户里的<b>真实资金</b>。{cfg.mode === "auto" ? "当前是全自动模式，Agent 会不经确认直接下单。" : ""}
          </p>
          <p className="muted small">Agent 的判断可能出错，合约交易可能亏光保证金。请确认风控参数是你能接受的。</p>
          <label className="field">
            <span>输入 yes 确认</span>
            <input
              className="input"
              value={liveText}
              placeholder="yes"
              onChange={(e) => setLiveText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && liveText.trim().toLowerCase() === "yes" && !saving && save(true)}
              autoFocus
            />
          </label>
          <div className="modal-foot">
            <button className="btn" onClick={() => setLiveAsk(false)}>
              取消
            </button>
            <button className="btn danger" disabled={liveText.trim().toLowerCase() !== "yes" || saving} onClick={() => save(true)}>
              切换到实盘
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}


function ProxyBox() {
  const [url, setUrl] = useState("");
  const [source, setSource] = useState<"page" | "env">("env");
  const [busy, setBusy] = useState("");
  useEffect(() => {
    api.proxy().then((r) => {
      setUrl(r.url);
      setSource(r.source);
    }, () => {});
  }, []);
  const run = async (what: "save" | "test") => {
    setBusy(what);
    try {
      if (what === "save") {
        const r = await api.saveProxy(url.trim());
        setUrl(r.url);
        setSource(r.source);
        toast(r.url ? `代理已保存：${r.url}，连欧易和连模型接口都走它` : "已保存：不用代理，直接连", "ok");
      } else {
        const r = await api.testProxy();
        toast(r.message, r.ok ? "ok" : "error");
      }
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy("");
    }
  };
  return (
    <>
      <div className="panel-head" style={{ marginTop: 14 }}>
        <h3>网络代理</h3>
      </div>
      <label className="field">
        <span>代理地址（留空 = 不用代理，直接连）{source === "env" && url ? "（现在用的是 .env 里的）" : ""}</span>
        <input className="input" placeholder="留空不用代理；要用就填，比如 http://127.0.0.1:7890" value={url} onChange={(e) => setUrl(e.target.value)} />
      </label>
      <div className="row">
        <button className="btn primary sm" disabled={!!busy} onClick={() => run("save")}>
          {busy === "save" ? "保存中…" : "保存"}
        </button>
        <button className="btn sm" disabled={!!busy} onClick={() => run("test")}>
          {busy === "test" ? "测试中…" : "测试连欧易"}
        </button>
      </div>
      <p className="muted small">连欧易和连模型接口共用这一个代理，保存后立即生效（欧易的后台连接会重连一次）。模型接口通不通用上面的「测试连接」。</p>
    </>
  );
}

/** 记录清理：自动保留几天（随上面的「保存」一起生效），以及现在就清。 */
function LogCleaner({ days, onDays, onCleared }: { days: number; onDays: (n: number) => void; onCleared: () => void }) {
  const [info, setInfo] = useState<LogsInfo | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.logsInfo().then(setInfo, () => {});
  }, []);
  const clear = async (keep: number) => {
    const what = keep ? `${keep} 天之前的记录` : "全部运行记录";
    if (!window.confirm(`删除${what}（活动流、复盘日志、已结束的任务和策略、欧易组件的调用日志）？删了不能恢复。进行中的任务、仓位、经验、战绩和设置不受影响。`)) return;
    setBusy(true);
    try {
      const r = await api.clearLogs(keep);
      setInfo(r);
      const n = Object.values(r.cleared).reduce((a, b) => a + b, 0);
      toast(`已删除 ${n} 条记录${r.okx_files ? `、${r.okx_files} 个欧易日志文件` : ""}`, "ok");
      onCleared();
    } catch (e) {
      toast(String((e as Error).message || e), "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="log-cleaner">
      <label className="muted small">
        自动只保留最近
        <select className="input sm" value={days} onChange={(e) => onDays(Number(e.target.value))}>
          {[1, 3, 7, 14, 30, 90].map((n) => (
            <option key={n} value={n}>{n} 天</option>
          ))}
        </select>
        的记录（改了要点保存）
      </label>
      {info && (
        <span className="muted small">
          现在：活动流 {info.counts["活动流"]} 条、日志 {info.counts["日志"]} 条，数据库 {info.db_mb} MB，欧易调用日志 {info.okx_mb} MB
        </span>
      )}
      <span className="log-cleaner-btns">
        <button className="btn sm" disabled={busy} onClick={() => clear(days)}>清掉 {days} 天前的</button>
        <button className="btn sm danger" disabled={busy} onClick={() => clear(0)}>全部清空</button>
      </span>
    </div>
  );
}
