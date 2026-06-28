# 交易所解耦重构设计方案

- **日期**: 2026-06-28
- **状态**: 已通过设计评审，待编写实现计划
- **作者**: Thomas Chang + Claude

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 设计与实现中遇到任何不确定（需求边界、交易所行为、字段语义、技术选型、本文未写清的细节），必须停下来向用户提问确认，禁止用猜测推进。

## 1. 背景与目标

GridTradeGP 当前是一个与 OKX 强绑定的中性合约网格交易机器人，由两部分组成：

- `account_0/` —— 实盘：选币、开网格（委托给 OKX 原生黑盒网格 bot `tradingBot/grid/*`）、止盈止损监控。
- `backtest/` —— 回测：含一套自研的中性网格模拟"引擎"（`grid_engine.py`），以及从 OKX 拉历史数据的层（`okx_history.py`）。

OKX 耦合散布在两处：ccxt `okex5` 客户端、`-USDT-SWAP` 符号格式、`instId/tdMode/posSize/runType` 等 OKX 参数、OKX REST URL、OKX 原生网格 bot API、`x-simulated-trading` 沙盒头、`code=='0'` 成功判定、按 `after` 的分页、UTC+8 时间假设等。

### 目标（11 条需求）

1. 重新设计系统机制与架构，足够健壮，可处理任意网络/服务器异常。
2. 保留所有已有因子的计算逻辑（零漂移）。
3. 主流程延用现有逻辑。
4. 选币流程延用现有逻辑。
5. 网格规则由自管理实现替代 OKX 黑盒 bot，参考 backtest 中性引擎逻辑。
6. 止盈止损沿用现有逻辑。
7. 可根据配置的交易所动态拉取数据进行回测。
8. 提供脚本预热全部回测数据，使回测无需联网。
9. 可把 Hyperliquid 作为目标交易所进行验证。
10. 系统可按自定义触发条件创建多个网格，并有机制同时管理多个网格的挂单与止盈止损而不产生冲突。
11. 使用 OOP、TDD、常用设计模式，使代码简洁且可读性高。

### 已确认的关键决策

| 维度 | 决策 |
|---|---|
| 范围 | 全量解耦 + Hyperliquid 实盘下单 + 回测 |
| 抽象层 | 统一 ccxt 适配器 |
| 网格执行 | 自管理**挂单式**网格；**OKX 也改自管理**，弃用黑盒 bot（统一执行器） |
| 健壮性 | 持久化状态 + 重启自愈对账 |
| 部署 | fly.io：**scheduler 机**（可 scale-to-zero）+ **monitor 机**（轻量常驻） |
| 状态存储 | 外部托管存储（实盘热状态用 Postgres；回测冷缓存用对象存储 Tigris） |
| 多网格并发 | **分阶段**：一期跨币种（币种互斥）；二期同币种（逻辑持仓归属/子账户） |
| 触发条件 | 全四类：定时+选币（现有）/ 价格·指标阈值 / 外部信号·手动 / 仓位·风控（下沉为准入门） |
| 工程方法 | OOP + TDD + 设计模式 |

## 2. 总体架构 —— 端口与适配器分层

核心思路：把"实盘 + 回测"重切为**纯策略核心**和**可替换外围**两层。所有 OKX 字眼下沉到适配器，策略逻辑原样搬进 `core/`。

```
gridtrade/
  core/            # 交易所无关·策略核心（逻辑原样保留）
    factors.py       <- fancy_grid_function.py 全部因子函数（不改公式）
    selection.py     <- functions.py 选币/排序/offset 逻辑（不改）
    grid_params.py   <- calc_grid_params_v1/v2（不改）
    grid_engine.py   <- backtest/grid_engine.py 中性引擎（实盘+回测共用）
    stop_rules.py    <- stop_loss.py + grid_engine._apply_exit 统一成一份退出规则
    scheduling.py    <- offset/run_time 计算（tools.py）
  exchanges/       # 交易所抽象（唯一含交易所差异的地方）
    base.py          <- ExchangeAdapter 接口 + 规范化符号模型
    ccxt_adapter.py  <- 基于 ccxt 统一接口的通用实现
    okx.py / hyperliquid.py  <- 各所差异(凭证/资金费周期/沙盒)
    fake.py          <- FakeExchange 测试/回测用内存模拟器
    registry.py      <- ExchangeRegistry 按配置构造适配器
  execution/       # 自管理挂单网格
    grid_executor.py <- 网格开/补单/平的生命周期（状态机）
    reconciler.py    <- 意图 vs 交易所实况对账·自愈
    live_equity.py   <- 实时盈亏/手续费/资金费记账（复用引擎数学）
    triggers.py      <- TriggerEngine + TriggerCondition 策略
    gates.py         <- 准入门链（Chain of Responsibility）
    manager.py       <- GridManager 组合编排器
    ownership.py     <- PositionOwnershipPolicy 策略
  state/           # 外部托管状态
    store.py         <- StateStore / Repository 接口
    postgres.py      <- Postgres 实现
    memory.py        <- 内存实现（测试）
    models.py        <- grid/order/record 模型
  runtime/         # fly.io 运行时
    scheduler.py     <- 每小时触发选币+再平衡（主流程延用）
    monitor.py       <- ~5s 对账+止盈止损循环
    notify.py        <- 通知通道（企业微信 + 可插拔）
    health.py        <- 心跳/健康检查
    events.py        <- 事件总线（Observer）
  backtest/        # 回测（复用 core + 适配器拉数）
    datasource.py    <- 替代 okx_history.py
    cache.py         <- 按天 parquet（本地或对象存储）
    prewarm.py       <- 泛化预热
    backtest_run.py  <- 复用 core + datasource
    selection_replay.py <- 复用 core.selection
  config.py        # 统一配置（选交易所/凭证/策略/状态/运行时）
  deploy/          # Dockerfile / fly.toml / 部署文档
```

**关键不变量**：`core/` 不 import 任何交易所库；它只吃 DataFrame 和参数、吐 DataFrame 和决策。需求 2/3/4/6（保留因子、主流程、选币、止盈止损）全部落在 `core/`，原样搬运 + 金标测试锁定输出。

## 3. 交易所适配器（统一 ccxt）—— 需求 7/9

定义 `ExchangeAdapter` 抽象基类，ccxt 实现一份通用版，各所只覆写差异。**采用 ccxt 统一符号 `BTC/USDT:USDT` 作为系统内部规范符号**，彻底移除散落的 `-USDT-SWAP` 字符串判断——符号格式转换、tick/lot 精度由适配器 + ccxt `markets` 元数据提供。

接口方法（覆盖现有全部 OKX 调用）：

```
# 行情（公共）
list_instruments() -> [{symbol, tick, lot, min_size, state, list_ts}]
fetch_ohlcv(symbol, tf, start, end) -> df[candle_begin_time,o,h,l,c,vol,quote_volume]
fetch_funding_history(symbol, start, end) -> df[ts, fundingRate]
fetch_mark_ohlcv(...)            # 可选
fetch_price(symbol) -> float
# 账户/交易（私有）
fetch_balance() -> {equity, cash}
fetch_positions(symbol) -> {net_size, avg_price}
create_limit_order(symbol, side, price, size, *, post_only, reduce_only, client_oid)
create_market_order(symbol, side, size, *, reduce_only, client_oid)
cancel_order(symbol, id) / cancel_all(symbol)
fetch_open_orders(symbol) / fetch_my_trades(symbol, since)
set_leverage(symbol, lev)
exchange_status() -> ok/maintenance
```

各所差异收敛点：

- **OKX**：apiKey/secret/passphrase + `x-simulated-trading` 沙盒头；资金费 8h；符号 `BTC-USDT-SWAP`。
- **Hyperliquid**：钱包地址 + 私钥（无 passphrase）；资金费 1h；符号为币名（如 `BTC`）；无 OKX swaprate CSV，资金费/mark 全走 ccxt REST。

ccxt 已统一 `fetchOHLCV/createOrder/cancelOrder/fetchOpenOrders/fetchBalance/fetchPositions/setLeverage/loadMarkets`，差异主要在凭证与资金费周期，覆写量小。

**风险与首要验证任务**：现 `ccxt==2.0.58` 太老、无 hyperliquid。需升级到支持 HL 的 ccxt（4.x，兼容 py3.7+），并在 Phase 1 第一步验证其与 pandas 1.3.5 / py3.9 共存可用。

## 4. 自管理挂单式网格执行器（替代 OKX 黑盒 bot）—— 需求 5

网格几何**直接复用 `core/grid_engine.py` 的 `grid_order_info()`**（等比价位序列 + 每笔数量），与回测完全同源。执行器为显式**状态机**：`PENDING -> OPENING -> ACTIVE -> CLOSING -> CLOSED/FAILED`。

**开网格（OPENING）**：

1. 用引擎算出 N+1 条等比价位线、每笔数量 `order_num`。
2. 中性初始化（复刻 OKX neutral bot 的"先买底仓"）：市价买入 = 入场价以上所有线的 `order_num` 之和，建立多头底仓。
3. 入场价**以上每条线挂限价卖单**、**以下每条线挂限价买单**，全部带 `client_oid = f"{grid_id}:{line_idx}"`。

**补单（ACTIVE，监控循环里）**：买单成交 → 在上一格挂卖单；卖单成交 → 在下一格挂买单（经典网格补位）。每轮拉 `open_orders + my_trades + position`，识别成交、补对侧、更新记账。

**平网格（CLOSING）**：止盈止损触发 → 撤所有单 → 市价 `reduce_only` 平净仓 → 落库结果 → 发事件/通知 → `CLOSED`。

**幂等与自愈基础**：`client_oid` 确定性编码 `(grid_id, line_idx)`，重启后能把交易所真实挂单/成交映射回网格线，实现幂等补单与对账。**交易所是订单/持仓的真相源**，状态库是网格意图 + 峰值 + 记账的真相源。订单操作建模为幂等 **Command**（`PlaceOrderCommand` / `CancelCommand`），可重放。

## 5. 实时盈亏记账 + 止盈止损（实盘/回测同源）—— 需求 6

自管理后须自行计算盈亏。`grid_engine.cal_equity_curve()` 已在回测里做了完全一样的事（已实现/未实现盈亏、手续费、资金费、净值），实盘复用同一数学（**Template Method**）。

`execution/live_equity.py` 维护运行态"已实现/手续费/资金费/净持仓/均价"，用当前 mark price 盯市 → `net_value` → `pnlRatio`，再调用**与回测同一份** `core/stop_rules.py` 的退出规则（按现有阈值，原样保留）：

1. 固定止损 `pnlRatio < -0.034`
2. Chandelier 移动止盈（`trailing_k=0.3`, `trailing_floor=0.00618`，峰值持久化）
3. 资金费率止盈/止损（`|funding| > 0.0015`，via 适配器）
4. PV 主动止损（成交量爆量 + 微亏 `pnlRatio < -0.015`，需 1m/15m 量，via 适配器）
5. 强平兜底（`net_value < 0.05`）

退出优先级与现有一致：固定/Chandelier 先判，命中即返回；其后资金费率；再 PV。峰值 `pnlRatio_max` 从本地 pkl 改存外部状态库。

## 6. 多网格编排 + 触发引擎 + 无冲突管理 —— 需求 10

开网格从"仅按 offset 定时"升级为 **触发 → 准入 → 执行** 三段式。

**① 触发引擎（Strategy 模式，可插拔 `TriggerCondition`）** —— 触发只负责*提议*开网格：

- `ScheduledSelectionTrigger` —— 现有 offset + 因子选币（**主流程原样保留**，作为一种内置触发器）。
- `ThresholdTrigger` —— 价格突破 / ATR / 资金费率 / 波动率达阈。
- `ExternalSignalTrigger` —— webhook/API/手动指令。

**② 准入门（Chain of Responsibility，每个开仓提议必须过闸）** —— 无冲突第一道闸；"仓位/风控条件"在此实现（本质是*准入条件*而非*触发*）：

- `SymbolLockGate`（币种互斥）、`MarginGate`（可用保证金）、`MaxConcurrentGate`（并发网格上限）、`RiskBudgetGate`（总风险敞口）。

**③ GridManager（组合编排器）**：持有全部活跃 `GridExecutor`，监控循环里逐网格独立推进。

**无冲突隔离模型（分阶段，用可换策略 `PositionOwnershipPolicy` 抽象）**：

- **一期 `SymbolExclusivePolicy`（跨币种）**：状态库对 `(exchange, symbol)` 加唯一约束——一个币种同时只有一个活跃网格 → 持仓不共享 → `reduce_only` 平仓绝对安全。多网格并发只发生在**不同币种**间。
- **二期 `LogicalAttributionPolicy`（同币种）**：每个网格用 `client_oid` 归属自己的成交、维护**逻辑净仓**，平仓改为"撤自己挂单 + 按本网格逻辑仓精确 reduce（带总量不超聚合仓的护栏）"或子账户隔离。接口一期定义、二期实现。

**并发正确性机制**：每网格乐观锁（version 列）+ 独立事务推进；订单操作靠 `client_oid` 幂等，循环重叠也安全；多监控机时用 DB advisory lock 做 leader 选举或按 symbol 分片，杜绝重复处理。

## 7. 外部托管状态 + 重启自愈对账 —— 需求 1

`state/` 定义 `StateStore` / Repository 接口，**实盘热状态主存 Postgres（Fly Postgres）**，ACID 保证对账正确性。

表（Repository 抽象）：

- `grids` —— 网格意图/几何/状态机状态/version；含 `(exchange, symbol)` 唯一约束（一期）。
- `grid_orders` —— 每条线 ↔ `client_oid` ↔ 交易所订单状态。
- `grid_accounting` —— 已实现/手续费/资金费/`pnlRatio_max`。
- `order_records` —— 历史成交记录（替代 `orderInfo.pkl` / `df_max.pkl`）。

回测冷数据 parquet 缓存走**对象存储（Tigris，S3 兼容）**，prewarm 写一次、回测多机读。

**重启自愈对账流程**（`execution/reconciler.py`）：

1. 从 Postgres 载入所有 open 网格意图。
2. 对每个网格拉交易所真实 `open_orders + position + my_trades(since)`。
3. Diff：缺失网格线补挂、孤儿订单撤掉、用成交流水重建净仓/已实现、刷新 `pnlRatio_max`。
4. 收敛后才进入正常循环。

无论在哪一步崩溃，重启都能从"交易所实况 + 状态库意图"收敛到正确状态。

## 8. 运行时与 fly.io 部署 —— 需求 1

两个 Fly Machine 角色（fly.toml 多 process group）：

- **scheduler 机**（可 scale-to-zero）：每小时被唤醒，跑 `runtime/scheduler.py` = **现有主流程**（触发 `ScheduledSelectionTrigger`：按 offset 关旧网格 → 选币 → 算单 → 过准入门 → GridManager 开新网格 → 落库）。需求 3「主流程延用」落在这里。
- **monitor 机**（轻量常驻）：`runtime/monitor.py` 跑 ~5s 循环 = 对账补单 + 实时记账 + 止盈止损。崩溃由 fly 自动重启，重启即自愈对账。

**健壮性机制（需求 1，处理任意网络/服务器异常）**：

- 所有交易所调用：超时 + 指数退避 + 抖动 + 错误分类（可重试/致命/限频）+ 熔断；**绝不再"失败即 sys.exit"**（现 `retry_wrapper` 会硬退出，改为降级 + 告警 + 续跑）。
- 全操作幂等（`client_oid` 驱动）；每轮以对账收敛，绝不信任内存态。
- 心跳写库 + fly 健康检查 + 自动重启；降级时企业微信告警。
- 优雅处理部分成交/拒单/交易所维护窗口（暂停开新、继续监控）/限频。
- 时间全程 UTC 存储，去掉硬编码 UTC+8 假设（offset 公式保留，时区改配置驱动）。

> **P4 carry-forward（来自 P2 最终评审）**：`gridtrade/state/` 的四个仓储在 `get/list_*` 读路径用了 `engine.begin()`（写事务），在真实 Postgres 上会取不必要的写锁。在 P4 上真实 Postgres 前，统一改为 `engine.connect()` 读路径。另：`grids.transition_status` 的 TOCTOU（校验读+版本守卫写，数据一致但并发下可能抛 ConcurrencyError 而非 StateError）的事务内重校验也在此阶段补齐（届时有可测的并发 mutator）。

> **P5 carry-forward（来自 P0–P1 最终评审）**：`CcxtAdapter.fetch_ohlcv` 当前用 `volCcy=vol`、`quote_volume=vol*close` 的近似映射，会使 `vwap=quote_volume/volCcy` 恒等于 `close`，令 Vwapbias/MarketPl 因子在真实 ccxt 数据上失真。P5 的 datasource 必须用各所真实成交额字段（OKX `volCcyQuote`、HL turnover）映射 `quote_volume`，回退 `vol*close` 仅在字段缺失时使用。

## 9. 回测解耦 + 离线预热 + HL 验证 —— 需求 7/8/9

- `backtest/datasource.py` 用同一套适配器实现 `fetch_ohlcv_range / fetch_funding_range / list_instruments`，**替代 `okx_history.py`**；分页/限频在适配器内。
- `bt_config.py` 增加 `EXCHANGE` 选择器，回测**按配置交易所动态拉数**（需求 7）。
- `prewarm.py` 泛化到适配器，按天 parquet 写缓存（本地或对象存储）；预热后回测只读缓存、**全程不联网**（需求 8）。HL 资金费为 1h 周期，预热阶段单独处理。
- `selection_replay.py` 继续复用 `core/selection.py`（同源选币）。
- **HL 验证（需求 9）**：对 HL 币池跑 prewarm → 跑回测 → 校验。HL 经 ccxt：符号 = 币名、`fetchOHLCV`、`fetchFundingRateHistory`、mark price 均可取。

## 10. OOP + 设计模式 + TDD —— 需求 11

**设计模式映射**：

| 模式 | 落点 |
|---|---|
| Adapter | `ExchangeAdapter` 收敛交易所差异 |
| Strategy | Factor / StopRule / TriggerCondition / GridGeometry / PositionOwnershipPolicy / NotificationChannel |
| Factory + Registry | `ExchangeRegistry`、`TriggerFactory` 按配置构造 |
| Repository | `GridRepository / OrderRepository / RecordRepository` 抽象持久化 |
| State Machine | 网格生命周期 `PENDING->OPENING->ACTIVE->CLOSING->CLOSED/FAILED`，reconciler 驱动恢复 |
| Command | 订单操作建模为幂等命令，便于对账/重放 |
| Chain of Responsibility | 准入门链 |
| Observer / Event Bus | 领域事件(`GridOpened/OrderFilled/GridClosed`) → 通知/指标/落库解耦 |
| Template Method | 实盘 `LiveEquityRunner` 与回测 `BacktestRunner` 共用同一套盈亏/退出引擎数学 |
| Dependency Injection | 适配器/存储/策略全构造注入 → 可测 |

**TDD 策略**（实现阶段强制走 test-driven-development，红-绿-重构）：

- **核心层纯单测**：因子金标测试、网格几何、退出规则——零 I/O。
- **`FakeExchange`**：实现同一 `ExchangeAdapter` 接口的内存交易所模拟器。它是整套执行/对账/止损能离线 TDD 的关键支点，**同时与回测填单逻辑同源**。
- **集成测试**：Executor + FakeExchange + 内存 StateStore，覆盖 开仓/成交/补单/止损/崩溃后对账自愈。
- **混沌/故障注入**：FakeExchange 注入超时/拒单/部分成交/维护窗口，验证健壮性（需求 1）。
- **回测平价金标**：实盘与回测退出口径逐行一致。

> `ExchangeAdapter` 端口同时支撑真实交易所、FakeExchange、回测填单——三者同源。

## 11. 因子保真保障 —— 需求 2/4

**金标（golden master）测试**：重构前先用当前代码在缓存数据上跑出所有因子/选币结果存档，重构后逐字段比对，保证因子逻辑零漂移。涵盖：

- 单币因子：`Reg_v2_2/5`、`Sgcz_2/5`、`Er_2`、`Atr_5`、`middle_5`、`ma_2/5/13`、`涨跌幅`。
- 截面因子：`上涨数量/下跌数量/上涨比例/交易额分位占比`。
- 选币排序：成交量分位过滤、极端下跌过滤、加权 rank 聚合、top-N 选择。
- 网格参数：`calc_grid_params_v1/v2`。

## 12. 迁移分期

- **P0 脚手架**：新包结构 + 搬运 `core`（逻辑不改）+ 因子/选币/网格参数金标测试。
- **P1 适配器**：ccxt 升级验证 + OKX/HL 适配器 + 符号规范化 + FakeExchange。
- **P2 状态层**：Postgres + Repository + 模型 + 内存实现。
- **P3 执行器**：挂单网格状态机 + 实时记账 + 对账自愈（一期 `SymbolExclusivePolicy`）。
- **P4 运行时**：scheduler/monitor + 触发引擎 + 准入门 + GridManager + 事件/通知 + fly.io（Dockerfile/fly.toml/secrets）。
- **P5 回测数据层**：datasource + 泛化 prewarm + HL 验证。
- **P6 加固**：故障注入/混沌测试 + HL 小额实盘端到端 + 自管理网格 vs 原 OKX 黑盒 A/B 校准。
- **P7（二期）同币种多网格**：`LogicalAttributionPolicy` / 子账户隔离。

## 13. 主要风险

| 风险 | 缓解 |
|---|---|
| ccxt 升级破坏 py3.9/pandas 1.3.5 | P1 首步独立验证；必要时锁定特定 ccxt 版本 |
| 因子逻辑漂移 | 金标测试逐字段比对 |
| 自管理网格 vs OKX 黑盒行为偏差 | 用现有 `gridResult` + 回测引擎 A/B 校准；HL 小额先验证 |
| 同币种持仓 reduce_only 误平 | 一期币种互斥硬约束；二期逻辑归属/子账户 |
| 监控机宕机致止损失效 | fly 自动重启 + 重启对账 + 心跳告警 |
| 多监控机重复处理 | DB advisory lock leader 选举 / symbol 分片 |
| HL 链上/资金费 1h 差异 | 适配器内吸收；回测预热单独处理 funding 周期 |

## 14. 验收标准

1. `core/` 不依赖任何交易所库，金标测试全绿（因子/选币/网格参数零漂移）。
2. 同一份代码经配置可在 OKX 与 Hyperliquid 上拉数回测；预热后回测全程离线。
3. Hyperliquid 上完成一次端到端小额实盘：开网格 → 补单 → 止盈止损平仓，全程自管理挂单。
4. 杀进程/断网后重启，系统能从交易所实况 + 状态库对账并续跑，无重复下单、无孤儿单。
5. 可按定时/阈值/外部信号三类触发并发创建多个（不同币种）网格，互不冲突。
6. 全程 OOP + 设计模式，关键路径有单测 + 集成测试 + 故障注入测试。
