# 真中性网格改造设计

> 日期：2026-07-01
> 定位：把当前**名为「中性」实为「做多」**的网格路径，原地改造成**真市场中性**——开网即 flat，价涨转净空、价跌转净多、entry 附近净仓≈0。

---

## 1. 背景与动机

当前单一网格路径在开网时下一笔初始市价买单（`open()` 的「中性底仓」段：`above = entry 上方线数`，市价买 `每格量 × len(above)`），意图「模拟 OKX 中性网格」。但从净暴露看，这使净仓恒 ≥ 0：

- entry（区间中部）：净多 `+每格量 × N_above`；
- `stop_high`（顶部）：净仓归 0（把 init 全卖掉）；
- `stop_low`（底部）：净多满仓 `+每格量 × grid_count`。

净仓区间 `[0, +每格量 × grid_count]`，**永不为负**——这是**做多/多头网格**的定义，不是中性。真中性应净仓对称绕 0：顶部转净空、底部转净多、中部≈0。

**审计结论（本次改造的前提，已用真 `GridExecutor + FakeExchange` 端到端验证）**：去掉 init、走真中性后，记账/止损/保险丝在净空（`net_position < 0`）下**均成立**，且记账**精确到分**（含持续净空、多次穿零）；反而是当前多头的 init 底仓（按 `entry` 非网格线价记账）造成既有漂移——卖光 init 时瞬时误差可达 −5.7% of cap，价格回落自愈（即已知的 `init-position-accounting-drift`）。故改中性**顺带根治**该漂移。

## 2. 关键事实（已核实）

- **净仓是价格的单调函数**：`net(p) = (p 下方已买) − (p 上方已卖)`。故 `stop_low`（最低）↔ **满多**、`stop_high`（最高）↔ **满空**。保险丝方向天然正确：low 保险丝 `sell` 平底部多头、high 保险丝 `buy` 平顶部空头。当前多头网格里 high 保险丝是**惰性死保险**（永不为空），中性下才真正启用。
- **记账精确的原因**：中性成交只落在网格线价上 → 净档位↔线价一一对应 → `cal_equity_curve` 的 `pos`/`neg` 均价分支恰好精确。打破该对应的唯一来源就是 init 按 `entry`（非线价）记账。
- **保险丝 size**：`worst = grid_count × 每格量` 在中性下每侧超挂约 2×（真实每侧上限≈`grid_count/2 × 每格量`），但 `reduce_only` 由交易所封顶到真实仓 → 安全。
- `simulate_grid_engine` 已有 `neutral_init` 参数（默认 `True`）；金标 parity 测试 `tests/core/test_grid_engine_parity.py` 用默认（含-init）比对 legacy account_0 —— 该测试锁的是**共用盈亏/退出数学能复现 legacy**，与 init 是否开启是正交的策略旋钮。

## 3. 范围

**做**：把单一网格路径原地改成真中性（去 init 底仓，`open()` + `restore()` 对称去除）；离线回测入口同步切中性；补/改测试锁定中性与净空不变量。

**不做**：
- 不引入 `direction`（long/short/neutral）多态——保持单一路径（已决策）。
- 不改共用盈亏/退出引擎数学（`cal_equity_curve` / `_apply_exit` / `evaluate_exit`）。
- 不改 `simulate_grid_engine` 的 `neutral_init` 参数与金标 parity 测试（保留双模式，legacy 含-init 金标原样）。
- 不收紧保险丝 size（reduce_only 已封顶，YAGNI）。
- 无 schema 变更、无 DB migrate。

## 4. 设计

### 4.1 核心行为

- 开网 **不下任何初始市价单**，净仓从 0 开始（flat）。
- 限价挂单不变：entry 上方 `sell`、下方 `buy`（entry 恰在线上的那条跳过，同现状）。
- 价涨 → sell 逐线成交 → **转净空**（趋近 `−N_above × 每格量` @ `stop_high`）；
  价跌 → buy 逐线成交 → **转净多**（趋近 `+N_below × 每格量` @ `stop_low`）；entry 附近 ≈ 0。
- 补单（`sync` 相邻对侧）不变——符号无关，已证正确。

### 4.2 改动点（外科式）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `gridtrade/execution/grid_executor.py` `open()`（约 82–88 行） | **删** `above` 计算 + `create_market_order` 初始买 + 随后的 `record_fill` 循环。开网即 flat。其余（限价挂单、保险丝、状态机跃迁）不变。 |
| 2 | `gridtrade/execution/reconciler.py` `restore()`（约 31–33 行） | **删** `above` 重放循环。`LiveEquity` 仅从持久化成交（`fills.list_by_grid`）重建。**必须与 `open()` 对称**（都无 init），否则重启后模型重建出网格并不持有的多头 → 净仓背离告警。 |
| 3 | `gridtrade/backtest/backtest_run.py`（约 83 行） | `neutral_init=True` → `neutral_init=False`，使回测与新实盘同口径、可预测实盘。 |

### 4.3 保持不变（审计已逐条证净空下正确）

`cal_equity_curve` 记账（`pos`/`neg` 均价分支）、`LiveEquity`（符号无关）、`evaluate_exit`/`_apply_exit` 止损（纯 pnl 阈值、无方向假设、平仓费用 `abs(hold_num)`）、`close()`/`finalize_close()`（按 `net_size` 符号选平仓方向 + `reduce_only` 有界补平）、保险丝方向与 size、`check_position_drift`（`abs`）、`simulate_grid_engine` 的 `neutral_init` 参数与金标 parity 测试、`direction` 字段（保留默认 `'neutral'`，现在名副其实）。

## 5. 测试（TDD）

### 5.1 改（现断言 init 底仓的测试）

- `tests/execution/test_grid_executor.py`：
  - `test_open_places_grid_and_neutral_inventory`（net==`on*4`）→ 断言 **net==0**、无初始市价单；改名 `test_open_places_grid_and_starts_flat`。
  - 约 70/73 行依赖 init 净仓（`on*3`）的场景 → 改为驱动价格自然累出净仓后重算期望值。
  - 约 173 行 init 费断言 → 删除（无 init 即无该合成费）。
- `tests/execution/test_live_equity.py`：`test_neutral_init_base_inventory`（复刻 OKX init）→ **删除**（锁的是要废弃的策略；`LiveEquity` 数学本身由其余测试覆盖）。
- `tests/execution/test_monitor.py`（约 31 行）：场景「init 多头浮亏→固定止损」→ 改为「驱动价格下行使 buy 成交累出净多 → 继续跌破 → 固定止损」，断言不变、setup 变。
- `tests/execution/test_chaos_close.py`（约 27 行）：`open()` 后「持有多头净仓」→ 因开网即 flat、close 无仓可平会使该混沌测试失去意义；改为**先驱动成交累出净仓**（多或空皆可），再在故障注入下验 `finalize_close` 续平至 flat（保留原「平仓部分成交残留」加固的验证意图）。
- `tests/execution/test_grid_executor_idempotent.py`（约 40 行）：验证幂等在 flat 起点仍成立（重复 open/sync 不产生额外仓）。

### 5.2 加（锁中性 + 净空不变量，移植审计里的 e2e 校验）

1. **开网即 flat**：`open()` 后 `fetch_positions().net_size == 0`、无 `:init:` 市价成交。
2. **符号跟随**：价涨→净空（`net_position < 0`）、价跌→净多（`> 0`）。
3. **记账精确**：真 `GridExecutor + FakeExchange`，细步长逐线穿越，`(net_value−1)×cap` == 现金流盯市真值（`Σ卖入−Σ买出+净仓×mark−真实费`）；覆盖**持续净空**与**多次穿零**收净空。
4. **restore 对称**：净空往返后重启 restore，`accounting.net_position == 交易所净仓`（无幻影 init）。
5. **保险丝净空**：中性涨破 `stop_high`（此时净空）→ high 保险丝 `buy reduce_only` 平空到 0 → `reconcile_fuses` 判 `fired` → 撑网全拆（grid `CLOSED`）。

> 端到端测试须用**细步长逐线成交**驱动（贴近真实连续行情）；大跳变会触发 FakeExchange 批量成交、破坏 `last_touch` 逐笔链，是测试假象而非引擎行为。

## 6. 迁移 / 上线（纯 ops，无 schema/DB 变更）

1. 部署前：控制台 `PANIC_CLOSE_ALL` 平掉所有旧（init 风格）活跃网格至 flat（同 USDC 切换套路）。
2. 部署中性版本。
3. 下个 scheduler 整点开中性网格。
4. 核验：新网开网**无 `:init:` 市价成交**、net≈0；随价上行观测到 `net_position` 转负（净空）。

## 7. 风险与边界

- **HL 真实性未验证项**：`reduce_only` 是否真封顶超额 size、触发单参考 mark|last —— STATUS.md §5 已列 testnet 待验证；中性下 high 保险丝真正启用后**更吃重**，需在 testnet 用一次自然破顶做有机验证。
- **净空能力**：HL 永续支持净空（`close()` 的 reduce 已在用），账户须允许开空——testnet agent 模式已具备。
- **保证金**：中性每侧最大暴露≈旧多头底部满仓的一半，MarginGate 保守预留（cash≥cap）仍够，无需调整。
- **真值边界**：§5.2 的「精确」是在 FakeExchange 理想撮合下；真实 HL 的滑点/部分成交会带来落在非线价的成交 → 小幅误差，但那是**任何网格通病**、非中性特有。

## 8. 不变量守恒

共用盈亏/退出数学（`cal_equity_curve` / `_apply_exit` / `evaluate_exit`）零改，金标 parity 原样通过；仅动「初始仓位策略（去 init）+ 回测入口口径」。实盘与回测「逐 bar 等价」不变量维持（两侧同切 `neutral_init=False`）。
