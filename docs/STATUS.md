# GridTradeGP — 项目状态与进度（固化文档）

> 单一事实源：任何新 session / 协作者读这一份即可掌握「系统设计完成度 + testnet 运行状态」。
> 最后更新：2026-06-29。代码状态：**SQLite 300 passed（+2 PG-only 并发测试 skipped）/ Postgres 302 passed**（双后端 TDD；含 P6① 混沌加固 + quote_volume 回退 + OrderFilled + funding 缓存 + funding 逐币种归属修复 + sync 先于 reconcile + 净仓对账 + reconcile 撤旧再补 + MarginGate + 双模式 PG fixture + 真并发 TOCTOU）。

---

## TL;DR

把一个**和 OKX 深度耦合**的网格交易机器人，重构成**交易所无关**（Ports & Adapters）、**自管理挂单网格**、**外部托管状态 + 重启自愈**的系统，并**已部署到 fly.io、在 Hyperliquid testnet 端到端跑通**（开网格 / 关网格 / 对账 / 跨进程管理 / 实时记账）。

- **离线**：因子/选币/网格/止损零漂移金标，全程 FakeExchange + 内存 SQLite，258 测试绿，CI（GitHub Actions）每 push 跑。
- **实盘 testnet**：fly app `gridtrade-hl`（nrt 东京），monitor+scheduler 两个常驻进程，Fly Postgres，agent 模式接 HL testnet，已实测开/关/对账/记账。
- **待办**：补单/止损随 testnet 行情有机验证；之后切 mainnet 小额（需求 3 收尾）。

---

## 1. 架构（`gridtrade/` 包）

```
gridtrade/
  core/           # 纯逻辑，不 import 交易所库；从 account_0 字节级搬运 + 金标锁定
    factors / selection / grid_params / grid_engine / stop_rules
  exchanges/      # 唯一含交易所差异处
    base(ExchangeAdapter 端口 + 数据类) / ccxt_adapter(通用) / okx / hyperliquid / fake
    registry(按配置构造) / resilience(重试+熔断) / resilient_adapter(包装所有调用)
  state/          # 外部托管状态（SQLAlchemy 2.0 Core）
    models(6 表+状态机) / store / grids / orders / accounting / records / fills / heartbeats
  execution/      # 自管理挂单网格
    grid_executor(开/补单/平 状态机) / reconciler(对账自愈) / live_equity(实时记账，复用引擎)
    triggers(触发引擎) / gates(准入门链) / manager(GridManager 编排) / events(事件总线) / monitor(单网格步)
  runtime/        # fly.io 运行时
    scheduler(常驻整点：关旧→选币→准入→开新) / monitor(常驻~5s：对账补单+记账+止损)
    cycles(纯循环体) / factory(build_runtime 组装) / universe(币池) / introspect / dbadmin
  config.py       # env 驱动 DeployConfig + 默认策略常量
  deploy/         # Dockerfile / fly.toml / DEPLOY.md(ops 清单)
```

**设计模式**：Adapter（收敛交易所差异）、Strategy（Factor/StopRule/TriggerCondition/PositionOwnershipPolicy）、Factory+Registry、Repository、State Machine（PENDING→OPENING→ACTIVE→CLOSING→CLOSED/FAILED）、Chain of Responsibility（准入门）、Observer（事件总线）、Template Method（实盘/回测共用退出引擎）、DI（全构造注入→可测）。

**关键不变量**：`core/` 不依赖任何交易所库；实盘与回测**共用同一套盈亏/退出引擎数学**（逐 bar 等价金标）；执行/对账靠 **exchange order id** 匹配（跨所通用）；成交幂等靠 `grid_fills.trade_id`。

---

## 2. 需求达成

| # | 需求 | 状态 |
|---|---|---|
| 1 | 健壮性（任意网络/服务器异常不挂） | ✅ 重试+退避+熔断+降级不 sys.exit + 重启对账自愈 |
| 2 | 保留全部因子 | ✅ 金标零漂移 |
| 3 | 主流程延用（选币→开网格） | ✅ ScheduledSelectionTrigger 对齐 legacy；testnet 已开网格 |
| 4 | 选币逻辑保留 | ✅ 复用 core.selection |
| 5 | 自管理挂单网格（替代 OKX 黑盒 bot） | ✅ GridExecutor 状态机；testnet 实测 |
| 6 | 止盈止损 | ✅ core.stop_rules.evaluate_exit（实盘/回测同源） |
| 7 | 按配置交易所拉数回测 | ✅ EXCHANGE 选择器 + DataSource |
| 8 | 预热后离线回测 | ✅ parquet 缓存 |
| 9 | Hyperliquid 验证 | ✅ 回测 + **testnet 端到端实盘** |
| 10 | 多触发/多网格无冲突管理 | ✅ 触发引擎 + 准入门链 + GridManager（一期 SymbolExclusivePolicy） |
| 11 | OOP/TDD/设计模式 | ✅ |

---

## 3. 阶段历史（全部合并入 main）

| 阶段 | 内容 |
|---|---|
| P0/P1 | core 搬运 + 因子/选币/网格参数金标；统一 ccxt 适配器（OKX/HL/Fake/registry；ccxt 2.0.58→4.5.61） |
| P2 | 状态层（表+仓储，乐观锁/状态机/并发安全 upsert） |
| P3a–d | core 引擎迁移 + 标量止损；LiveEquity 增量记账；GridExecutor 自管理网格；Reconciler 重启自愈+monitor 步+幂等成交 |
| P4a–e | 状态层收尾；准入门链；触发引擎；GridManager+事件总线；runtime 循环 |
| P4f–n | 健壮性核心；CI；ResilientAdapter；config；币池/心跳；组装工厂；守护进程；scheduler 常驻整点 |
| P4-deploy | Dockerfile/fly.toml/CD/ops 清单 + 部署 |
| P4x–P5a | **testnet 实战修复**（见 §6） |
| P6① | **故障注入/混沌测试**：FaultyAdapter 包装器（超时/拒单/限频/维护/部分成交/丢响应）；开仓·补单·对账·平仓·cycle 五场景端到端不变量；并修两处真实缺口（见 §6b） |

每阶段计划存档于 [docs/superpowers/plans/](superpowers/plans/)；总设计 [docs/superpowers/specs/2026-06-28-exchange-decoupling-design.md](superpowers/specs/2026-06-28-exchange-decoupling-design.md)。

---

## 4. 测试

- **双后端**：默认 `pytest` 走内存 SQLite（快、离线、CI 不依赖 PG）= **292 passed + 2 skipped**；
  设 `TEST_DATABASE_URL=postgresql://…` 则全量走真 Postgres = **294 passed**（含 2 个 PG-only 并发 TOCTOU 测试）。
- 所有 DB 测试经 `tests/conftest.py` 的 `store`（双模式）/ `pg_store`（PG-only）fixture；PG 模式每测 TRUNCATE 隔离。
- 真并发 TOCTOU：`tests/state/test_transition_concurrency.py` 用真线程 + Barrier 验证 `transition_status` 版本守卫只放一个赢家。
- 跑测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest`（仓库 `.venv`：py3.9 / pandas 1.3.5 / numpy 1.22.4 / TA-Lib 0.6.8 / ccxt 4.5.61 / SQLAlchemy 2.0）。
  本地 PG：`docker run -d --name gridpg -e POSTGRES_PASSWORD=grid -e POSTGRES_DB=gridtrade -p 5432:5432 postgres:16` + `export TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade`。
- CI：`.github/workflows/ci.yml`（ubuntu py3.9 + TA-Lib bundled wheel + pytest），每 push 跑、已验证绿。

---

## 5. 部署（fly.io）— 当前 testnet 运行中

**fly app `gridtrade-hl`（region nrt 东京）**，三个 process group（同一镜像）：
- **monitor**：~5s 循环，对账补单 + 实时记账 + 止盈止损 + 跨进程惰性 restore。
- **scheduler**：常驻、睡到整点跑（关旧 tag→选币→准入→开新）。
- **web**：fly 第三进程（**常驻 ≥1 台 / long-live**，`min_machines_running=1`），FastAPI 只读 dashboard（系统健康/活跃网格/单网格明细/历史战绩）；登录鉴权。注：原设 scale-to-zero(min=0)，但 CI 滚动部署不为空的新进程组建首台机器，故改 min=1（见 deploy/DEPLOY.md）。

| 项 | 值 |
|---|---|
| 交易所 | Hyperliquid **testnet**（`api.hyperliquid-testnet.xyz`） |
| Postgres | Fly Postgres `gridtrade-pg`（nrt），`pool_pre_ping` 治空闲断连 |
| 凭证 | **agent 模式**：`HL_WALLET_ADDRESS`=主账户（499 USDC）、`HL_PRIVATE_KEY`=agent 私钥 |
| 币池 | `UNIVERSE_WHITELIST=BTC,ETH,SOL,HYPE,PURR /USDT:USDT`（testnet 聚焦真实币，避开~1473 垃圾币） |
| 调度 | monitor 间隔 5s；scheduler 整点；`SCHEDULER_RUN_ON_START=false`（停 churn，见 §8） |
| CI/CD | GitHub Actions：CI 每 push；CD `deploy.yml` 手动触发（`gh workflow run deploy.yml`），需 GH secret `FLY_API_TOKEN` |

**决策依据**：见记忆 `p4-deploy-decisions`；ops 命令清单 [deploy/DEPLOY.md](../deploy/DEPLOY.md)。

---

## 6. testnet 实战修复（离线测试抓不到、真 HL 才暴露）

全部 TDD + 合入 main + 部署：

| 类别 | 修复 |
|---|---|
| 状态层 | BigInteger（ms 时间戳列 Postgres INT4 溢出，SQLite 测不出）；`pool_pre_ping`（Fly PG 关空闲连接） |
| 行情 | timeframe `1H`→`1h`（ccxt 小写）；fetch_universe_candles 逐币容错；熔断不计 fatal（坏币不拉垮全局电路）；fetch_balance 读 USDC（`quote_currency`） |
| 下单 | 市价单传参考价（HL 滑点）；**省略非法 cloid**（HL cloid 须 128-bit hex）；`to_canonical/to_native` 处理 None（createOrder 响应不带 symbol）；`cancel_all` 逐个撤（ccxt 无 HL cancelAllOrders） |
| 执行核心 | **fill/对账改 exchange order id 匹配**（HL fill/open order 只带 oid 不带 cloid）；monitor 周期**惰性 restore** 他进程开的网格（跨进程内存态） |
| 记账（巡检发现） | **funding 逐币种 + 开仓后归属**：开仓游标=0（计入开仓前）+ symbol 未过滤（计入他币种）→ 新网格把别币种＋开仓前的 funding 全计入自己（线上实测多网格 funding_paid 雷同 `-0.652633`）。游标修复（`open`/`reconciler.restore` 置 `grid.created_at`）见 v20。**v21 改正 symbol 口径**：HL 的 `fetch_funding_history` 返回账户级全币种且把【查询 symbol】盖到每行，靠 symbol 区分不出币种（v20 误用 `r['symbol']` → no-op）；改 `HyperliquidAdapter` 覆写按 `info.delta.coin` 过滤（真实 HL testnet 验证 BTC/ETH 已正确区分） |
| 执行核心（巡检发现） | **sync 先于 reconcile + 净仓对账**：`run_monitor_cycle` 原每轮先 reconcile 后 sync → 卖单成交离开挂单簿后 reconcile 当「被丢」用新 oid 重挂、覆盖成交 oid → 成交永不入账、反复重卖、净仓往空漂（线上 gt011 实测真实成交 17 笔仅入账 2 笔、模型 +0.00165 vs 真实 -0.00017）。修：调顺序 `restore→sync→reconcile`（仅对 sync 成功网格 reconcile）+ `Reconciler.check_position_drift` 净仓偏离超容差只告警。订单对账只对单不对仓的盲点补上。**残留路径（D）**：HL 抖动时 `fetch_open_orders` 漏返回一张仍在挂的单 → reconcile 直接重挂产生重复单（旧单成交漏摄入，线上 gt011 09:43 实测）→ 改为**重挂前先撤旧 oid**（仍在挂则撤、已没则 no-op），杜绝重复 |

---

## 6b. P6① 混沌测试主动暴露并修复的缺口（mainnet 前加固）

故障注入（FaultyAdapter）穿过完整执行栈主动验证异常路径，抓到两处离线常规测试抓不到、真实异常才暴露的缺口，均 TDD 修复：

| 缺口 | 现象 | 修复 |
|---|---|---|
| **平仓部分成交残留** | `close()` 的 reduce 市价单只成交一半 → 网格转 CLOSED 后留**无人认领的残仓** | `close()` 平仓后重拉持仓、有界补 reduce（≤3 次）直至 ≤min_amount |
| **单网格故障掀翻整轮 cycle** | 一个币种持续故障耗尽重试 → 异常冒泡，**健康网格的对账/补单/止损全被阻断** | `run_monitor_cycle` 与 `monitor_all` 均加 **per-grid 隔离**（坏网格记 degraded/error、不阻塞他人；catch Exception 不吞 BaseException） |

另：补强 FakeExchange 按 client_oid 去重 create_limit_order（模拟真实交易所幂等，验证丢响应重试不产生重复单）。

---

## 7. testnet 验证状态

- ✅ **开网格**（选币→中性市价底仓→26 限价挂单→ACTIVE）、**关网格**（cancel_all + reduce）、**对账**、**跨进程管理**（scheduler 开 / monitor 接管）、**实时记账**（accounting 实时更新）。
- ⏳ **补单 / 止盈止损平仓** —— 已接好、monitor 在跑，随 testnet 行情**有机触发**（某格成交→补对侧；触发条件→平仓），待观察。
- ⏳ **mainnet 小额**（需求 3 收尾）：testnet 稳定后 `HL_TESTNET=false` + 确认 live 策略参数 + 切主账户凭证。

---

## 8. 关键运维点 / gotchas

- **`SCHEDULER_RUN_ON_START`**：只决定 scheduler 启动瞬间是否抢跑一轮。生产 `false`（避免每次部署/重启在半点关掉刚开的网格再重开 = churn）；调试期想立即看效果设 `true`。
- **agent 模式地址**：`HL_WALLET_ADDRESS` 填**有钱的主账户地址**，`HL_PRIVATE_KEY` 填 **agent 私钥**（二者地址不同是正常的）。私钥须 66 字符（`0x`+64hex）；40hex 那是地址不是私钥。
- **DB 重置**：`fly console -a gridtrade-hl -C "python -m gridtrade.runtime.dbadmin reset"`（drop+create，仅 testnet/无价值数据时）。
- **观察状态**：`fly logs -a gridtrade-hl`；或 console 查 `grids/grid_orders/grid_fills/grid_accounting/order_records/heartbeats`。

---

## 9. 仍延后（需产品/口径决策，动手前先问）

- **ThresholdTrigger / ExternalSignalTrigger**（价格/指标阈值、外部信号触发器）——**三期**（需产品定义）；**扩展点已留**：子类化 `TriggerCondition` + 在 `TriggerEngine` 注册（见 `gridtrade/execution/triggers.py` 末尾的「三期预留扩展点」注释）。
- ✅ ~~MarginGate（保证金门）~~ —— 已实现（准入门链 4/4：cash≥cap 保守口径 + 同轮累计扣减 + fail-closed，置链尾）。
- ✅ ~~真并发 TOCTOU 测试~~ —— 已补（`tests/state/test_transition_concurrency.py` 真线程+Barrier 在本地 PG 验证版本守卫只放一个赢家；CI 仍 SQLite，CI PG job 待多监控机阶段）。
- ✅ ~~OrderFilled 事件~~ —— 已实现（GridManager.monitor_all 逐笔成交发布，带 fee）。
- **同币种多网格**（二期 LogicalAttributionPolicy / 子账户）。
- 详见记忆 `p4-deferred-items`、`deferred-toctou-concurrency-test`。

---

## 10. 指针

- 记忆（每 session 自动加载）：`exchange-decoupling-project`、`hl-testnet-deploy-state`、`p4-deploy-decisions`、`p4-deferred-items`、`deferred-toctou-concurrency-test`、`plan-execution-workflow`、`always-ask-when-unclear`。
- 设计：[docs/superpowers/specs/2026-06-28-exchange-decoupling-design.md](superpowers/specs/2026-06-28-exchange-decoupling-design.md)
- 计划：[docs/superpowers/plans/](superpowers/plans/)
- 部署 ops：[deploy/DEPLOY.md](../deploy/DEPLOY.md)
- legacy 实盘 + 旧回测（已 archive 到 `legacy/`，作金标对照/历史留存）：`legacy/account_0/`、`legacy/backtest/`。生产包 `gridtrade/` 与活跃测试套件对其零运行时依赖；仅 `tests/golden/gen_*.py`（一次性重生成金标脚本，pytest 不收集）会注入它。
