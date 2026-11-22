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
- 算法时钟上新出现的合格候选（同打法、同方向合并，排除回测已判亏的打法）送给非待命 Worker 复核是否备单。观察线是 `price_touch` + `wake` 规则，各自有编号、期限和命中记录，按采样报价穿越检查。同线在最短复盘间隔内只报一次。Worker 正在思考时追加行情证据，空闲时通过信箱唤醒。
- 入场前机械失效条件写在 `plan.cancel_before_entry`，可按价格或确认收盘检查。`invalidation` 文字交给 Worker 解读。改触发器之后只看新版生效后的收盘。入场触发器发现已有持仓或入场单时保留首笔入场权限语义。
- 有生效入场计划时，新的已确认结构转折会通知 Worker 复核，即使快判已关闭。快判关闭且没有数据触发器时，程序按计划中明确的失效条件自动处理，结构转折由 Worker 复核。
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

## 模型缓存
- 实测（2026-11-21，中转接口 + gpt-6.2-sol）：服务端只在三个位置命中缓存——工具定义末尾、input 开头连续的 developer 消息末尾、上一个请求整段末尾（工具往返）。
  用户消息里的内容即使和上一轮一字不差也不命中，拆成两条用户消息也没用。旧发法首请求命中 57%，不变资料放进开头的 developer 消息后 73%。
- 所以 Worker 的提示词按 `ouy/agent/watcher.py` 的 `CACHE_TIERS` 排：不变的说明 → 品种设置 → 策略库 / 战绩 / 经验 → 已收盘数据（大周期 → 中周期）→ 知识库条目 → 已收盘数据（小周期）→ 每轮都变的内容；
  `STABLE_BREAK_AFTER` 标出的几层末尾各有一条分隔行（`STABLE_MARK`），发请求时 `ouy/agent/core.py` 的 `lead_instructions` 把分隔行前面的每一段拆成一条 developer 消息，排在规则后面。
  新加的段落如果不随报价变，把它的开头标签加进 `CACHE_TIERS`；报价、时间、叫醒原因、账户不要往前放。换模型或换中转后先重测命中位置，不要假设规则不变。
- 显式缓存断点（`AI_CACHE_BREAKPOINTS`，默认开）：`core.mark_breakpoints` 在这些 developer 消息的末尾标 `prompt_cache_breakpoint`（最多四个，规则那条不标，
  超了去掉最短那段后面的）。标了服务端才把整段前缀写进缓存并上报写入量（`cache_write_tokens`，中转站后台的黄色）；不标只有自动缓存、只有读取量。
  上游报错点名不认这个参数时，程序去掉断点重发一次，这个进程之后不再标（日志里有一条告警）。写入量是 0 而读取量不是 0 = 上游没上报写入，不代表没写。
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
- 排查掉登录：官方组件的调用日志在 `~/.okx/logs/trade-日期.log`（每行一条 JSON）。我们启动它时带了 `--log-level error`，只记失败的调用；
  要看全部调用临时改成 `info`（一天约 1 GB，看完改回来），完全不记用 `--no-log`。
