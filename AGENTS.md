# Ouy 项目须知（给 Agent 看）

## 验证
- 后端测试：`.venv/Scripts/python.exe -m pytest -q`（Windows；系统 Python 没装 pytest）。结尾那条「closed database」是测试收尾时后台任务的已知告警，不算失败。
- 前端：`cd web && npm run build`（含 tsc 类型检查）。
- 运行：`.venv/Scripts/python.exe -m ouy`，默认 http://127.0.0.1:8899。改了后端代码要重启；知识库 Markdown 改了会自动重建索引。
- 涉及下单的改动先在模拟盘（env=demo）验证，不要动实盘设置。

## 约定
- 源码和知识库一律 UTF-8、LF 换行。
- 在 Windows 上用 shell 的 heredoc 写文件，单条命令太长会被截断（约 8K 字符）：大文件分开写，或先写脚本文件再执行；heredoc 里的反斜杠会被吃掉一层。

## 知识库（knowledge/）
- `core/` 下的文档每一轮都固定进 Worker 的系统提示词，要短；其余文档按当前盘面检索（`ouy/agent/watcher.py` 的 `kb_queries`）。
- `strategies/` 每篇的标题是策略名，正文第一行是策略目录里的「什么时候用」；Worker 用 `read_strategy(名字)` 读全文，并把名字填进 `plan.strategy`。
- 每篇末尾写「出处与可信度」：原著 / 交易所文档 / 研究，以及它是有证据还是经验。引用要查实，查不到的不写；数字表格用代码算。
- 文档里写的规则必须和程序真实行为一致（止损、保本、时间止损、挂单方式等），改代码时同步改文档。
- 资金费率的门槛一律是 8 小时口径（快照字段 `funding_rate_8h_pct`）。
- 股票 / 商品永续（欧易 `instCategory` 3 / 4）不跟 BTC，不要把加密货币的经验套上去。

## 回测
- 快判信号：`.venv/Scripts/python.exe -m ouy.backtest BTC-USDT-SWAP --days 7`
- 算法候选位（Worker 每轮看到的那几条）：`.venv/Scripts/python.exe -m ouy.backtest_plans BTC-USDT-SWAP ETH-USDT-SWAP --days 30 --style intraday --stance balanced`
- 保盈线（`LOCK_R`）：同一个命令加 `--lock-r 0.5`，报告里「浮盈≥0.5R 后亏超 0.1R」就是回吐转亏的笔数；改默认值前先跑几个风格对比。
- 回测默认是保守成交（限价要被穿过才算成交、同一根里止损优先、跳空按开盘价出、现价单有滑点）；`--ideal` 是旧口径，只用来对比。
- 要能复现：写死 `--end YYYY-MM-DD`；K 线缓存在 `data/candles`，`--offline` 只用缓存不联网。
- 加 `--save` 把结果存到 `data/backtest/`：页面「算法」页的回测面板（`/api/backtest`）和 Worker 候选位后面的「机械照做的回测」都读它。
- 对照用的 4H 突破跟随：`.venv/Scripts/python.exe -m ouy.backtest_trend BTC-USDT-SWAP ETH-USDT-SWAP SOL-USDT-SWAP --days 1100 --end 2026-11-21`（只是研究，没有接进候选）。
- 结论只在笔数够、95% 区间整段在 0 一侧时才写赚 / 亏；一天、一周的结果只看近况，不能用来判断打法好坏。
- 改候选位、快判门槛之前先跑回测；结果写进 `knowledge/evidence.md`，不要只挑好看的数字。
- 各打法的实盘 / 模拟盘战绩由 `ouy/scoreboard.py` 自动记（进场打标签，平仓后归到那条打法），接口 `/api/stats/strategies`。

## 算法和 Agent 的分工
- 算法（程序）：行情判断和候选位按结构周期自己算（`watcher._algo_tick`，不等复盘、不花模型调用），核成本、下单、管仓也是程序。Worker：判断这一条现在该不该做。
- 算法时钟按交易所确认收盘的 K 线时间推进，新收盘延迟返回时稍后复查。新合格候选（同打法、同方向合并，排除回测已判亏的打法）送给非待命 Worker。Worker 正在思考时追加证据，空闲时合并进信箱，等最短复盘间隔后再启动一轮。
- 入场前机械失效条件写在 `plan.cancel_before_entry`，可按价格或确认收盘检查。`invalidation` 文字交给 Worker 解读。改触发器之后只看新版生效后的收盘。入场触发器发现已有持仓或入场单时保留首笔入场权限语义。
- 有生效入场计划时，新的已确认结构转折会通知 Worker 复核，即使快判已关闭。快判关闭且没有数据触发器时，程序按计划中明确的失效条件自动处理，结构转折由 Worker 复核。
- 交易页和工作台共用 `ChatPanel`。对话交办的交易动作和交易规则都需要确认；只提醒的线可以直接启用。Worker 待命时，已启用的记录器仍按条件采样。
- 计划线（`CycleReport.lines`，`models.PlanLine`）：Worker 每轮必交的预案，最多 8 条，每条 = 价位 + 怎么算到了（`price_touch` / `close_above` / `close_below`）+ 到了做什么（`wake` / `cancel` / `enter`）。叫醒线一律碰到就叫（`PlanLine` 把 `do=wake` 的收盘条件改成 `price_touch`）；收盘确认只留给进场线和撤单线。
  `Watcher._lines_gate` 在结论交出来之前核对：空仓时现价上方、下方各至少一条，缺了退回一次；`enter` 线按「到了那个价再进」过一遍 `_check_plan`，退回一次后还过不了的改成 `wake`。
  每条线存成带 `observation` 标记的触发器（规则接口 `type=watch`，页面「触发线」页），只触发一次，到过之后在有效期内不重设。带动作的线到了由 `Watcher._line_fired` 执行，不是执行器直接下单：
  `enter` 按那一刻的现价重新 `_check_plan`，过了走 `_enter_now`（和快判同一条路：打来源标签、布置保本和分批止盈），并把其余进场线和挂着的计划入场单作废；过不了只叫醒。`cancel` 撤掉还没成交的计划入场。
  `_sync_observation_rules` 只换真的变了的线：条件、动作相同，价位（和进场线的止损止盈）相差不到 `SAME_LINE_PCT` 的原样保留（只更新说明），Worker 这一轮没再划的才撤。不要改回每轮全撤重设，否则线等不到价格。
  用户自己划的线走 `POST /api/lines`（到价 / 收盘站上 / 收盘跌破 → 叫醒 / 撤单 / 平仓 / 市价开仓），不带 `observation` 标记，Worker 重新划线时不会动它。普通触发器通过 `action` 区分提醒、任务、开仓、撤单及仓位操作。
  盯盘期间，Worker 复盘中或等待审批时，行情记录器和结构周期计算照常更新。图表上的事件只在有触发或状态变化时写入。
- 存盘回测里「扣费后亏」的打法（`backtest_plans.losing`）：候选照给但标「没有优势」，程序不催 Worker 做；Worker 要做必须在 `plan.edge` 写出算法看不到的额外依据，
  否则程序核对不通过；不做就选 `no_plan_reason=no_edge`。没有报告、样本太少、分不出赚亏的不拦——没证据不等于亏。改了候选或管理规则后重跑 `--save`，标记跟着报告变。
- 快判守同一条规矩：`python -m ouy.backtest ... --style scalp --save` 存成 `data/backtest/fast_<风格>.json`，里面「扣费后亏」的信号类型（名字是「快判·收盘突破」这样）
  程序只记一条算法事件，不下单、不叫醒 Worker（`watcher._fast`）。快判没有并进候选位：回测比候选位还差一点，见 `knowledge/evidence.md`。
- 每笔单记来源（照算法候选 / 改过算法候选 / Worker 自拟 / 快判），按来源的战绩在 `/api/stats/strategies` 的 `origins`：用它检验 Worker 的取舍有没有比机械照做强。
- 快判的旧 `auto` 配置按 `agent` 执行，直接进场要有 Worker 最近一轮的方向授权。Worker 需要等结构收盘再判断时可用一次性 `close_above` / `close_below` + `do=wake`，部署之后第一根符合条件的已收盘 K 线就能触发。
- 算法事件（`watcher._algo` → `algo_events` 表）：行情判断变了、出了新的合格候选、转折（苗头 → 确认 → 盘中失守 / 收回，原来的结构转折、转折雷达、盘中转折位合成的一个算法，读数用 `signals.turn_state`）、快判、保盈线、持仓提醒、触发器，各记一条；
  带方向的 15 分钟 / 1 小时后核对对错。「算法」页按层摆（`ALGO_LAYER`：看行情 / 找转折 / 给入场位 / 管持仓），K 线图的「算法触发」开关把事件画成圆点。
  新加一种事件要同时在 `ALGOS` 写中文名、在 `ALGO_LAYER` 归层。盯盘没开时算法不跑，不会有事件。
- 测试不读本机 `data/backtest`（`tests/conftest.py` 把报告目录指到空目录）；要测有报告时的行为，在测试里自己存一份。
- Watchdog 每 15 秒检查任务存活，每分钟独立巡检账户，不调用交易 Worker 做定时复盘。战绩整理和平仓后的模型复盘在单独任务里执行；平仓复盘每批最多尝试 3 笔（失败也计数）。
- 叫醒 Worker 只有两条路，不按秒数轮询：
  定时看盘（`Watcher._next_look`）对齐 K 线收盘：空仓按风格的 `look_minutes`（超短线 15 / 日内 60 / 波段 240 分钟），有仓按结构周期（5 / 15 / 60 分钟）。设置里的 `look_minutes`（全局，或 `workers[币].look_minutes`）选了 5 / 15 / 30 / 60 / 240 就不分空仓有仓都按它，0 = 按风格；改了之后 `apply_config` 立刻重排下一次。Worker 不能要求「过一会儿再看」，要提前被叫醒就划线。
  事件：硬事件（首次、持仓变化、用户留言、挂单超时、仓位告警、程序真的下单 / 撤单的回报）马上叫醒；行情事件（只叫醒的触发线、转折、连续走线、新候选、价格波动、快判）等最短间隔，
  并且每个品种每小时最多 `max_event_wakes` 次（默认 6），超过的留在信箱和 `pending_wakes` 里，并到下一次定时看盘或硬事件一起送。新加一种叫醒先想清楚它是哪一类。
  `event` 和 `always` 的区别只剩定时看盘过不过滤（价格没动、离线都远就跳过）。复盘失败后用独立的 `retry_after_ts` 退避 2 / 4 / 8 / 15 分钟，留言、持续模式和风险事件都不能绕过；事件保留到恢复后处理，程序采样和已授权规则照常运行。手动复盘可主动试一次模型服务。
- 连续走线（`signals.trend_run`，算法事件 `run`）：结构周期 K 线连续 ≥ 3 根同向收盘、合起来 ≥ 3 倍走线之前的 ATR；同一段走线每多走出一档才再报一次。
  只叫醒 Worker 核对计划和线，不下单：60 天回放里走出这种线之后 4 根继续同向的只有约 43%（5m）到 47%（15m），不是追单信号。门槛按回放定的（5m 约 6 次 / 天，15m 约 2 次 / 天，1H 约 0.6 次 / 天），改之前重算。
- 新的任务错误通过 `diagnostics.exception_detail` 脱敏后保存在活动 `data.error` 中；不要只留 300 字摘要。`/api/work/{id}` 返回当前环境的任务详情。Super-agent 与原文的实现边界见 `docs/superagent-review.md`。

- Router 是分发台，不思考：固定用最低思考强度（`watcher.ROUTER_EFFORT`），只带最近 `ROUTER_KEEP` 条对话；要想、要查的普通问题用 `ask_general` 交给通用 Worker，
  落到某个币的交易交给那个币的 Worker。对话框里选的思考强度给接手的 Worker，不给 Router。不要往 Router 的提示词里加分析类的职责。
- 页面上的运行过程统一用 `web/src/components/AgentRun.tsx`（`toTurns` 把活动流并成一轮一轮，`RunCard` 画叫醒原因、工具步骤、思考中、结论）。
  记录不在页面上堆：一次只画最近一段（`useRecent`，点「更早的」往前翻），旧的长结论收成几行，连着的「跳过」并成一行；
  存量由后端管：对话每段留 200 条，活动流和日志留 `log_keep_days` 天（仓位、战绩、经验不删）。

## 模型缓存
- 实测（2026-11-21，中转接口 + gpt-6.2-sol）：服务端只在三个位置命中缓存——工具定义末尾、input 开头连续的 developer 消息末尾、上一个请求整段末尾（工具往返）。
  用户消息里的内容即使和上一轮一字不差也不命中，拆成两条用户消息也没用。旧发法首请求命中 57%，不变资料放进开头的 developer 消息后 73%。
- 所以 Worker 的提示词按 `ouy/agent/watcher.py` 的 `CACHE_TIERS` 排：不变的说明 → 品种设置 → 策略库 / 战绩 / 经验 → 已收盘数据（大周期 → 中周期）→ 知识库条目 → 已收盘数据（小周期）→ 每轮都变的内容；
  `STABLE_BREAK_AFTER` 标出的几层末尾各有一条分隔行（`STABLE_MARK`），发请求时 `ouy/agent/core.py` 的 `lead_instructions` 把分隔行前面的每一段拆成一条 developer 消息，排在规则后面。
  新加的段落如果不随报价变，把它的开头标签加进 `CACHE_TIERS`；报价、时间、叫醒原因、账户不要往前放。换模型或换中转后先重测命中位置，不要假设规则不变。
- 显式缓存断点（`prompt_cache_breakpoint`）试过，已删：2026-11-23 实盘日志里上游对 gpt-6.2-sol 直接报「prompt_cache_breakpoint is not supported on this model」。
  现在只靠服务端自动缓存，所以只有读取量；`cache_write_tokens` 是 0 属正常（上游不报写入），不代表没写。换模型或换中转后可以重试，但先对着真实接口发一个请求确认。
- 已收盘 K 线按 10 根一块对齐（`ouy/indicators.py` 的 `stable_window`）：前 30 根在一整块时间里不变；块后面新收的几根（`new_candles`）跟着本周期的指标放，行情快照里只留没收盘的那根。
- 行情快照里只在 K 线收盘时才变的内容（各周期的指标、整块对齐的 K 线、前高前低的上一段高低点、昨日价值区间）由 `_split_closed` 拿出来，
  按最快多久变一次分成大 / 中 / 小周期三段（`CLOSED_BUCKETS`：60 / 15 / 5 分钟）；随报价变的（最新价、区间位置、支撑阻力、形态、盘口）留在行情快照里。
  只是换位置，数值不变；往快照里加新字段时先想清楚它跟报价变还是跟收盘变，跟报价变的要加进 `LIVE_FRAME_KEYS` 一类的名单，否则会被当成收盘才变的挪到前面。
- 知识库条目按行情状态查（`Watcher._knowledge`）：同一种状态 30 分钟内用同一批，叫醒原因只在重新查的那一次带上。
- 离线估算（回放 BTC 真实 K 线、每分钟复盘一轮、只跟上一轮比）：日内首请求约 87%，超短线约 80%；每 5 分钟一轮时约 77% / 71%。这是按上面实测的命中位置推算的，不是服务端读数。
- 命中率看真实读数：`/api/stats/cache`（「算法」页最下面），不要凭感觉判断；模型服务没返回用量的请求记为「没有读数」，不当成 0。

## 欧易登录（OAuth）
- 令牌由欧易官方组件保存和续期，我们的代码接触不到。官方 MCP 组件把访问令牌缓存 60 秒，缓存过期时同时到达的每个请求都会各起一个 `okx-auth token` 进程，没有去重。
- 实测掉登录都发生在几个账户请求同时去取令牌的时刻，所以 `ouy/okx/gateway.py` 的 `TokenGate` 保证每次缓存过期只放一个请求去取。不要绕过它直接并发调用需要登录的工具。
- 查登录状态（`okx-auth status`）也会起授权进程：已登录时 60 秒查一次就够，不要加密轮询。
- 授权命令和实盘、模拟盘取令牌共用互斥锁。盯盘遇到授权类错误后保留运行意图，按 10 / 20 / 40 / 60 秒退避读取账户，期间仅采样公开行情。账户读通后继续检查订单、持仓和触发器。用户主动退出授权仍停止盯盘。
- 排查掉登录：官方组件的调用日志在 `~/.okx/logs/trade-日期.log`（每行一条 JSON）。我们启动它时带了 `--log-level error`，只记失败的调用；
  要看全部调用临时改成 `info`（一天约 1 GB，看完改回来），完全不记用 `--no-log`。
