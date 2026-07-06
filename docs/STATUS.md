# GridTradeGP — 项目状态与进度（固化文档）

> 单一事实源：任何新 session / 协作者读这一份即可掌握「系统设计完成度 + testnet 运行状态」。
> 最后更新：2026-07-01。代码状态：**SQLite 456 passed（+2 PG-only 并发测试 skipped）/ Postgres 458 passed**（双后端 TDD；含 P6① 混沌加固 + quote_volume 回退 + OrderFilled + funding 缓存 + funding 逐币种归属修复 + sync 先于 reconcile + 净仓对账 + reconcile 撤旧再补 + reconcile 重挂宽限 + sync 游标重叠 + MarginGate + 双模式 PG fixture + 真并发 TOCTOU + 门链拒绝/MarginGate fail-closed 结构化日志 + 交易所原生止损保险丝）。

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

**关键不变量**：`core/` 不依赖任何交易所库；实盘与回测**共用同一套盈亏/退出引擎数学**（逐 bar 等价金标）；执行/对账靠 **exchange order id** 匹配（跨所通用）；成交幂等靠 `grid_fills.trade_id`。**三档半拉黑名单/判定单源共享（2026-07-06）**：名单=`config.DEFAULT_TIER_POLICY`（legacy 档0 移植 9 币，env 双侧只作覆盖）、判定=`core/tier_policy.py`（实盘方案A剔锁与回测递补同一函数+同源守卫测试）；回测评估经 `run_backtest(tiers=...)`/`BT_TIER*` env（top-K 递补+period 锁窗近似），标准跑法见 spec `2026-07-06-tiered-blacklist-backtest-design.md` §6，cap 调整须回测结论+用户批准。

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
- **monitor**：~5s 循环，对账补单 + 实时记账 + 止盈止损 + 跨进程惰性 restore。per-grid 并行单元（`MONITOR_PARALLEL`，1=串行保底）+ 长轮中途心跳打点 + 熔断三路（market_read/account_read/trade_write）+ HL nonce 写锁。**读路径已快照化（2026-07-06，待部署）**：轮首 `AccountSnapshot` 5 次账户级调用（fills/orders/positions/allMids/funding，≈64 权重与格数解耦）替代逐格 ~6 调（~84 权重/格，曾致 mainnet 429 饱和、parallel 被迫回 1）；快照失败=整轮跳过（fail-safe）。**部署前须完成 spec「上线前硬性验证项」**（真 testnet 直调 ccxt `fetchMyTrades(None)`/`fetchOpenOrders(None)`/allMids 行为假设），见 `docs/superpowers/specs/2026-07-06-account-snapshot-batch-reads-design.md`。
- **scheduler**：常驻、睡到整点跑（关旧 tag→选币→准入→开新）。
- **web**：fly 第三进程（**常驻 ≥1 台 / long-live**，`min_machines_running=1`），FastAPI 只读 dashboard（系统健康/活跃网格/单网格明细/历史战绩）；登录鉴权。P1 明细页实时网格价格图：/grid/{id}/chart 片段端点（K 线走势 + 网格挂点/买卖挂单/已成交点/入场止损/当前价，服务端 SVG），原生 JS 每 5s 异步局部刷新（隐藏标签暂停），窗口 生命周期/1h/6h/24h；行情失败降级到 DB 层不崩。P2 控制台：kill 两档(halt/panic) + 关/开网格 + 暂停 scheduler，均经 control_commands 指令队列由 monitor 执行（web 零下单）；control_flags 标志门控；control_audit 审计。注：原设 scale-to-zero(min=0)，但 CI 滚动部署不为空的新进程组建首台机器，故改 min=1（见 deploy/DEPLOY.md）。P3 复盘分析：/analytics 页（权益/已实现曲线、tag 归因、成交分布、退出原因，全服务端 SVG）；真实手续费铺表；equity_snapshots 由 monitor 节流写（EQUITY_SNAPSHOT_INTERVAL_SEC，默认 300s）。所有服务端 SVG 图表（/analytics 与实时网格图）均带 Y 轴刻度 + X 轴时间(HH:MM)/类目标签 + 图例 + 数值标注（`dashboard/svgaxes.py` 共享纯函数；文本仅数值/时间/固定词 + svg_escape 兜底，守 |safe 边界）。

| 项 | 值 |
|---|---|
| 交易所 | Hyperliquid **testnet**（`api.hyperliquid-testnet.xyz`） |
| Postgres | Fly Postgres `gridtrade-pg`（nrt），`pool_pre_ping` 治空闲断连 |
| 凭证 | **agent 模式**：`HL_WALLET_ADDRESS`=主账户（499 USDC）、`HL_PRIVATE_KEY`=agent 私钥 |
| 币池 | `UNIVERSE_WHITELIST=BTC,ETH,SOL,HYPE,PURR /USDT:USDT`（testnet 聚焦真实币，避开~1473 垃圾币） |
| 调度 | monitor 间隔 5s；scheduler 整点；`SCHEDULER_RUN_ON_START=false`（停 churn，见 §8） |
| CI/CD | GitHub Actions：CI 每 push；CD `deploy.yml` 手动触发（`gh workflow run deploy.yml`），需 GH secret `FLY_API_TOKEN` |

**决策依据**：见记忆 `p4-deploy-decisions`；ops 命令清单 [deploy/DEPLOY.md](../deploy/DEPLOY.md)。

> **真实手续费落库（新）**：`grid_fills.fee` 记录每笔成交的交易所真实手续费；`accounting.fee_paid` 与 `net_value/pnl_ratio` 改用真实费（共用回测引擎 `cal_equity_curve` 不动）。**上线对已有库需跑一次幂等迁移**：`fly machine run <image> python -m gridtrade.runtime.dbadmin migrate`（加 `fee` 列）。历史 fill 不回填（fee=0），存量在跑网格重启重放会漏历史段真实费、随新成交自愈。设计/计划见 `docs/superpowers/{specs,plans}/2026-06-30-real-fee-persistence*`。

> **交易所原生止损保险丝（新，✅ 已部署 v41 testnet / 待有机验证）**：部署核实——`dbadmin migrate` 经 fly `[deploy] release_command` 在 v41 安全时点跑通、`grids` 已加 `fuse_low_oid/fuse_high_oid`；运行机 `TESTNET=True STOP_ENABLED=True SLIP=0.15`。部署时无活跃网格，fuse 行为待下一整点开网后观察（重点看 monitor 有无反复 `fuse re-placed`）。给软止损补一道**交易所原生 reduce-only 触发单**作灾难保险丝——堵软止损在跳空/爆拉/进程宕机/API 熔断/5s 盲区下失效的结构性风险。开网时挂两张（`sell@stop_low_price` / `buy@stop_high_price`，破网价触发、参考价=破网价故成交底线=破网价×(1∓`STOP_SLIPPAGE`)、size=最坏满仓、`reduce_only` 封顶到真实仓），exchange order id 持久化到 `grids.fuse_low_oid/fuse_high_oid`（跨重启可判定已触发）。`Reconciler.reconcile_fuses` 每轮三态：在挂→无动作 / 被丢→重挂 / 已触发→**撑网全拆**（`ex.close` 同软止损收尾路径，exit_reason='保险丝触发'）；并修 `reconcile_open_orders` 排除 fuse oid（HL `frontendOpenOrders` 含触发单，否则每轮误撤保险丝）。软止损保留为主刹车、逻辑零改。开关 `STOP_ORDERS_ENABLED`（默认 true，可一键回退纯软止损）/ `STOP_SLIPPAGE`（默认 0.15）。**上线对已有库跑一次 `dbadmin migrate`**（加 `fuse_low_oid/fuse_high_oid` 列）。**待 testnet 验证**：HL reduce-only 超额 size 是否封顶到持仓、HL 触发对 mark/last、端到端破网触发→撑网全拆、`cancel_all` 是否覆盖触发单。设计/计划见 `docs/superpowers/{specs,plans}/2026-07-01-native-stop-order-backstop*`。

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
| 执行核心（巡检发现） | **sync 先于 reconcile + 净仓对账**：`run_monitor_cycle` 原每轮先 reconcile 后 sync → 卖单成交离开挂单簿后 reconcile 当「被丢」用新 oid 重挂、覆盖成交 oid → 成交永不入账、反复重卖、净仓往空漂（线上 gt011 实测真实成交 17 笔仅入账 2 笔、模型 +0.00165 vs 真实 -0.00017）。修：调顺序 `restore→sync→reconcile`（仅对 sync 成功网格 reconcile）+ `Reconciler.check_position_drift` 净仓偏离超容差只告警。订单对账只对单不对仓的盲点补上。**残留路径（D）**：HL 抖动时 `fetch_open_orders` 漏返回一张仍在挂的单 → reconcile 直接重挂产生重复单（旧单成交漏摄入，线上 gt011 09:43 实测）→ 改为**重挂前先撤旧 oid**（仍在挂则撤、已没则 no-op），杜绝重复。**深层残留（E，循环巡检 gt07 发现）**：成交离开挂单簿但 `fetch_my_trades` 尚未返回的延迟窗口里，reconcile 立即重挂用同 client_oid 覆盖成交 oid（PK=client_oid）→ 漏摄入；叠加游标 `max_ts` 被别的成交推过头跳过。修：**E2 reconcile 重挂宽限**（连续 N 轮从 book 消失才重挂，默认 2，给 sync 时间先摄入、不覆盖 oid、不产生多余单）+ **E4 sync 游标留 5min 重叠**（靠 `add_if_new` trade_id 去重，乱序晚到成交仍被拉到入账） |
| 执行核心（巡检发现，2026-07-02） | **补对侧单缺「opp_line 已占用」守卫 → 持久重复挂单**：`GridExecutor.sync` 成交后在 `opp_line`(line±1) 挂反向单，用新 seq client_oid 无条件 `create_limit_order`，**不查该 (line,side) 是否已有 resting 单**。配对层级网格两侧本就挂单，价格震荡时补单撞上已在的配对单 → 同 line 同向双单（testnet OP/gt00 实证：line13 buy 重复挂 2.3h、line16 sell 重复；两张都成交则该 line 双倍建仓、超仓，但不破记账/drift 仍 0）。修：sync 补单前查 `open_lines={(line,side)}`，opp 已占用则不补（配对单已覆盖，待其成交再补回）。复现测试 `test_sync_replenish_dup`（震荡产重复→RED）。**语义校准**：3 个旧测试原断言「单笔成交后挂单数恒恢复满额」实为旧「无条件补单」行为，改断言「无 (line,side) 重复 + 往返走格才真补挂得住的对侧单」。**待部署**：修复只防新重复；线上 OP 现存 2 张多余单随该网格轮换关闭自然清（或手动撤）。 |
| 记账引擎（巡检发现，2026-07-02） | **hold_num 非均匀成交往返平仓不减仓**：`cal_equity_curve` 原 `hold_num = net_dir(净手数) × order_num`，隐含回测「每笔 lot 均匀」假设。实盘 LiveEquity 逐笔喂真实成交 size，一旦出现**非均匀成交**（如某挂单部分成交），`net_dir×最后一笔size` 立错（testnet TIA/gt011 实证：fills=[buy 1.6, buy 36, sell 36] 裸净和/交易所真值均 +1.6，引擎却算 hold=36；`realized_pnl` 正确 → 平仓盈亏入账了但仓位没减）。当前被 mark≈avg 掩盖（未实现≈0、总 equity 仍吻合），价格一动即 equity 背离、且已触发 drift 告警。修：`hold_num = Σ(order_dir×order_num).expanding()`（累计带符号量）——均匀 lot 下与原式**恒等**（金标 parity 保留），非均匀时才纠正。TDD 复现测试 `test_snapshot_net_position_with_variable_fill_sizes`。**待部署**：deploy 后 monitor 下一记账轮从 fills 重算自愈 net_position。 |

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

- ✅ **开网格**（选币→**真中性、开网即 flat 无底仓**→26 限价挂单→ACTIVE；价涨转净空/价跌转净多）、**关网格**（cancel_all + reduce）、**对账**、**跨进程管理**（scheduler 开 / monitor 接管）、**实时记账**（accounting 实时更新）。
- ✅ **补单 / 止盈止损平仓** —— **已在 testnet 真实行情有机验证（2026-06-30）**：活跃网格观测到逐笔成交→补对侧（如 NEAR/USDC closed=3 时仍 open=26）；多个网格由 monitor 止盈规则在非整点时刻自动平仓、**已实现正收益**（gt010 NEAR +0.19、gt09 TIA +0.10）。网格随选币按 ~小时级轮换。注：平仓/退出当前**无原因日志**（候选补观测性，同门链拒绝那次）。
- ✅ **mainnet 小额（需求 3）——已上线（2026-07-03）**：独立环境 app `gridtrade-prod` + PG `gridtrade-pg-prod`（nrt）；
  `deploy/fly.prod.toml` `HL_TESTNET=false`/`SCHEDULER_RUN_ON_START=false`/`release_command="sh -c 'dbadmin create && dbadmin migrate'"`（空库首部署必需，已实测生效）。
  **全自动 CD `deploy-prod.yml`：push `production` 分支即 test→deploy 到 gridtrade-prod（= 真钱部署，勿误 push）**；CI 门内建（deploy needs test）；GH secret `FLY_API_TOKEN_PROD`（app 级）。
  币池写死在 fly.prod.toml `[env] UNIVERSE_WHITELIST`=HL mainnet 24h 成交额【非 meme 且 maxLeverage≥5】流动性 Top26（逐个核过 live 永续 swap/active）。
  首部署核验：三进程 healthy(testnet=False)、面板登录可用、无报错；`run_on_start=false` 故首网格待下一个 12H 边界开。设计/计划见 `docs/superpowers/{specs,plans}/2026-07-03-mainnet-production-environment*`，ops 见 deploy/DEPLOY.md。
  上线姿势：`main` 验证 → merge 进 `production` → `git push origin production`（勿直接改 production）。
  - 🔧 **上线次日巡检发现并修（2026-07-03，CAP_EQUITY_FRAC 0.10→0.50）**：本金 $219.91 下默认 frac=0.10 → 单网格 cap≈$22 → 每笔挂单 notional ≈$3，**低于 HL mainnet 全市场 $10 最小下单额（ccxt cost.min=10）→ 首批网格挂单会被全数拒（`InvalidOrder`，fatal 不重试/不计熔断、安全失败）、建不起来**。testnet 未暴露因其账户已涨到 ~$983（cap≈$98、单笔 $9.7–$18.9 天然≥$10）。标定：testnet 实测最差 单笔/cap≈0.099，取 frac=0.50 → cap≈$110、最差单笔 ~$10.9；MarginGate(default_cap=$100) 仍放行 ~2 网格。⚠ 小账户 band-aid：equity 回撤或高档数币可能再跌破 $10（该网格安全失败）；鲁棒解=加本金后回调 frac=0.10，或给 `grid_order_info` 接入 cost.min 自适应降档（`min_amount` 当前从未接线、默认 0.0）。见记忆 `mainnet-order-min-notional`。

---

## 8. 关键运维点 / gotchas

- **`SCHEDULER_RUN_ON_START`**：只决定 scheduler 启动瞬间是否抢跑一轮。生产 `false`（避免每次部署/重启在半点关掉刚开的网格再重开 = churn）；调试期想立即看效果设 `true`。
- **agent 模式地址**：`HL_WALLET_ADDRESS` 填**有钱的主账户地址**，`HL_PRIVATE_KEY` 填 **agent 私钥**（二者地址不同是正常的）。私钥须 66 字符（`0x`+64hex）；40hex 那是地址不是私钥。
- **DB 重置**：`fly console -a gridtrade-hl -C "python -m gridtrade.runtime.dbadmin reset"`（drop+create，仅 testnet/无价值数据时）。
- **观察状态**：`fly logs -a gridtrade-hl`；或 console 查 `grids/grid_orders/grid_fills/grid_accounting/order_records/heartbeats`。一键只读快照：`bash scripts/testnet_status.sh`（fly 机器状态 + 心跳/标志/活跃网格/指令/余额）。
- **「该开未开」诊断（app v40+）**：scheduler 选币提案后 0 开仓时，先看 `fly logs` 的 `[gate] rejected <symbol> by <gate>: <reason>`——多数是 `SymbolLockGate: active grid already exists`（选中币已活跃，正确拒绝、稳态 1~N 网格随选币轮换，**非 bug**）。`MarginGate fail-closed: balance fetch failed: ...` 才是余额读取异常需关注。2026-06-30 巡检曾因门链拒绝静默无日志误判一次假警报，已加结构化日志根治（见记忆 `margin-gate-silent-fail-closed`）。
- **时区**：内部全 UTC（无机器 TZ 依赖，已铲平 `utc_offset`/`tm_gmtoff`）；换仓 offset 相位现为纯 UTC（与回测 `utc_offset=0` 同口径）；显示时区由 `DISPLAY_TZ`（IANA，默认 UTC）控制，仅面板层。**注**：本次上线令 live 换仓 12H 边界相位相较旧 +8 平移 8h（有意变更，与回测一致）。
- **候选票池**：`list_instruments` 只留 swap 永续 + canonical 去重；`resolve_live_universe` 黑名单无条件生效（含白名单模式）；可配 `MIN_QUOTE_VOLUME_24H` 绝对成交额地板（ccxt `quoteVolume`，code 默认 0=停用，prod 设 $1M）。**prod 已去 `UNIVERSE_WHITELIST` 走全市场动态**（全部永续 −黑名单 −24h成交额<$1M → 选币再 55%相对过滤）。档1/档2 由 SymbolLockGate 覆盖不实现。
- **回测票池与 prod 同步**：回测候选池从写死 8 币 → 全市场动态（`list_instruments` swap+去重 −黑名单 −逐 run_time PIT `$1M` 成交额地板，地板从缓存 1h `quote_volume` 前置 24h 重建、无未来函数）；`selection_replay.build_pit_candidates` 承载；两段式预热（1h 全市场→选币→仅选中币 1m/funding）。选币数学不动。`BT_MIN_QUOTE_VOLUME_24H`/`BT_BLACKLIST` env 可调。忠实度：candle-vol≈dayNtlVlm 近似 + 存活者偏差。
- **回测选币性能（并行 + 磁盘缓存，纯离线工具）**：`BT_WORKERS>1` 现**同时并行选币回放（按 run_time 连续切块多进程）与网格仿真**（原先只并行仿真）——选币是长跑瓶颈、CPU-bound、各 run_time 独立，多核近似线性加速；连续切块 + `map` 保序 ⇒ 与串行逐位一致。`BT_SELECT_CACHE`（默认开）把选币结果按「选币参数 + 每币缓存天范围指纹」pickle 落盘 `data/hl_validate/_select_cache/`，同窗口+同参数+同数据重跑秒回；重新预热改变缓存天自动换 key、不返回过期（已知盲区：同窗口跨 200 天边界重跑 api→reservoir 换源、天集合不变时不换 key，靠删缓存/off 兜底，差异仅 bar 级微差）；`off` 旁路 / `rm -rf` 该目录强制重算。`core/selection.py` 顺带修 2 warning（`resample base=`→`offset=` + 删遗留 debug-print，金标 parity 不破、live 日志同步去噪）。命令详见 [docs/回测使用文档.md](回测使用文档.md) §3。
- **回测可回溯拓展（Reservoir 1h，纯离线工具）**：窗口早于 HL API 1h 滚动(~200 天)时 main() 自动把 phase1 切到 Reservoir 归档（`warm_reservoir_ohlcv` 1s→1h+1m 全币种一次下载同写，phase2 1m 秒命中）；归档起点 2025-07-31 → 最早窗口起点 **2025-08-14**（更早响亮报错）。近窗口 api 路径字节不变。fidelity：两源 bar 微差、单 run 单源；老窗口票池仍今日上市表（存活者偏差随窗口变早加重）。

---

## 9. 仍延后（需产品/口径决策，动手前先问）

- ✅ ~~**计价/结算币诚实化**~~（HL 曾用假 `USDT` 标签、实为 USDC）——**P1+P2 完成**：`quote_currency` 驱动 canonical 符号（HL→`/USDC:USDC`，OKX 不变 `/USDT:USDT`），新增可选 `QUOTE_CURRENCY` config；registry 实例覆写、factory 透传；core 零逻辑改动。**testnet 已切 USDC（2026-06-30，app v39）**：A 等 flat（PANIC_CLOSE_ALL 平掉 4 个旧 USDT 网格）→ stage `UNIVERSE_WHITELIST`→USDC（26 币）→ push+CD 部署 → 核验 universe 全 USDC + 新网格 `OP/USDC:USDC`（26 挂单）开成。历史已平网格留旧 USDT 标签。完整方案见 [docs/计价币诚实化设计.md](计价币诚实化设计.md)。
  - 遗留小观察：HL `list_instruments()` 含重复 native 折叠到同一 canonical（whitelist 26 → universe 56），**非本次引入**（base 提取结构未变），候选后续去重。
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
