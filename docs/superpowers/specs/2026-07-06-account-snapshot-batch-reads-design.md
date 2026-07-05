# 账户级批量取数（AccountSnapshot）设计

日期：2026-07-06 ｜ 状态：已批准（用户）｜ 关联：per-grid 并行监控（commit 1c177f1）、429 突发观测（mainnet 2026-07-05 19:47 UTC）

## 背景与动机

monitor 读路径目前逐格逐调：每格每轮 fetch_my_trades(20) + fetch_funding_payments(20) +
fetch_price(~2-20) + fetch_open_orders(20，对账) + fetch_positions(2，漂移) +
fetch_open_orders(20，保险丝**重复第二次**) ≈ **84 权重/格/轮**。5 格 ≈ 420/轮、~500/min，
已在 mainnet parallel=4 下观测到一轮 4/5 格 429 突发（HL /info 预算 1200/min）。

而 HL 的 fills（userFillsByTime）、挂单（frontendOpenOrders）、仓位（clearinghouseState）、
价格（allMids）、资金费（userFunding）**全部是账户级端点**——fetch_funding_payments 现状
甚至是每格重复调同一个账户级端点再各自过滤，纯浪费。

根治：每轮固定 5 次账户级调用（≈64 权重，与格数无关），按 symbol 分发给各网格单元。
5 格省 ~6.5×，50 格外推 ~65×，几十格无压。

## 目标

- monitor 读路径请求数与网格数解耦：每轮 5 次账户级调用。
- 语义等价：成交摄入（逐笔 add_if_new 去重）、补单、E2 宽限、漂移告警、保险丝三态、
  止损评估行为与现状一致（差分测试钉死）。
- 旧路径完整保留（snapshot=None 即现状逐格取数），作为测试基线与回退面。

## 非目标

- 不动写路径（补单/撤单/平仓/保险丝重挂照旧逐格、走全局写锁）。
- 不动 signals（ohlcv/funding rate，自带 15min 节流）、equity 快照（fetch_balance）、
  MarginGate、scheduler 选币、dashboard、回测。
- 不改 MONITOR_PARALLEL（mainnet 维持 2，用户决定）。
- 不做快照失败回退逐格取数（失败=整轮跳过，见失败语义）。

## 设计

### 1. AccountSnapshot（新模块 `gridtrade/execution/snapshot.py`）

不可变数据对象，轮首构建一次，只读传给各单元（并行 worker 共享零竞态）：

```python
@dataclass(frozen=True)
class AccountSnapshot:
    ts_ms: int                                   # 构建时刻（观测/日志用）
    trades: List[Trade]                          # 账户级成交，升序
    orders_by_symbol: Dict[str, List[Order]]
    positions: Dict[str, float]                  # symbol -> net_size
    prices: Dict[str, float]                     # symbol -> mid
    funding_by_symbol: Dict[str, List[FundingPayment]]

    def trades_for(symbol, since_ms) -> List[Trade]      # 纯过滤，无 IO
    def orders_for(symbol) -> List[Order]
    def position(symbol) -> Optional[float]              # None=快照中无此仓位行（视为 0）
    def price(symbol) -> Optional[float]                 # None=无此币价 → 单元报错降级
    def funding_for(symbol, since_ms) -> List[FundingPayment]
```

`build_account_snapshot(adapter, symbols, trade_since_ms, funding_since_ms) -> AccountSnapshot`：
顺序 5 次调用，全部走 ResilientAdapter（account_read / market_read 电路照罩、退避重试照旧）。
任何一次重试耗尽 → 异常上抛（cycle 决定整轮跳过）。

**游标口径**（调用方 cycles 计算；实现修订 2026-07-06）：
- `trade_since_ms = max(0, min(各 ACTIVE 格的 fills.max_ts) − 5min 重叠)`，与逐格路径
  **严格等价**（无成交格 = 0）。原设计想顺带用 created_at 修新格全量扫——实现时发现
  与测试替身（FakeExchange 逻辑计数器 ts）时基不兼容，且 HL since=0 也只回最近
  2000 条（代价同现状每格行为），故放弃该顺带优化、保持双路径差分等价。
- `funding_since_ms = min(各格 accounting.funding_cursor，0/缺失时 created_at)`（读 DB，
  与 restore 语义一致；快照构建时单元的惰性 restore 尚未发生，不能依赖内存态）。
- 每格消费时再按**本格游标**二次过滤（trades_for/funding_for 的 since_ms 参数），
  与现状逐格 since 语义一致；成交归属仍靠 by_oid（exchange_order_id → 网格线）匹配，
  快照只是供给面变化。

### 2. Adapter 接口扩展

base.py 新增 5 个账户级方法，**默认实现 = 逐 symbol 循环现有单币方法合成**（任何交易所
天然可用；即便走默认实现，也把保险丝那次重复 open_orders 合并掉了）：

| 方法（签名统一带 symbols） | 默认实现（base） | HL 原生实现 | HL 权重 |
|------|----------------|------------|---------|
| `fetch_my_trades_all(symbols, since_ms=None)` | 逐 symbol fetch_my_trades 合并按 ts 排序 | ccxt `fetchMyTrades(symbol=None, since)`；**逐行 symbol 必须由 fill 的 coin 字段映射为 canonical**（防 funding 同款"查询 symbol 盖到每行"坑） | 20 |
| `fetch_open_orders_all(symbols)` | 逐 symbol fetch_open_orders 合并 | ccxt `fetchOpenOrders(None)`（frontendOpenOrders，含触发单/保险丝） | 20 |
| `fetch_positions_all(symbols)` | 逐 symbol fetch_positions | ccxt `fetch_positions()` 无参（clearinghouseState） | 2 |
| `fetch_prices_all(symbols)` | 逐 symbol fetch_price | **allMids 直调**（`publicPostInfo {'type':'allMids'}`，coin→canonical 映射），不用 fetchTickers（权重高） | 2 |
| `fetch_funding_payments_all(symbols, since_ms=None)` | 逐 symbol fetch_funding_payments | 现有账户级返回按 `info.delta.coin` 分组一次出全部（现状浪费的直接根治） | 20 |

注意：base 默认实现需要 symbol 列表——方法签名带 `symbols` 参数
（`fetch_*_all(symbols, ...)`），HL 原生实现忽略该参数（账户级天然全量）、返回后按
symbols 过滤字典键（不泄漏非监控币种数据给调用方，行为可预期）。

- FakeExchange：原生实现全部 5 个（内部状态全在手），测试零阻力。
- ResilientAdapter：包装 5 个新方法；电路归类 —— trades/orders/positions/funding →
  account_read，prices → market_read；均为读，不过写锁。

### 3. Executor / Reconciler 改造（可选参数，旧路径保留）

- `GridExecutor.sync(grid_id, symbol, *, skip_replenish=False, snapshot=None)`：
  - `snapshot` 非 None：trades ← `snapshot.trades_for(symbol, 本格游标−重叠)`；
    price ← `snapshot.price(symbol)`（None 则抛错走单元降级，本轮该格跳过）；
    funding ← `snapshot.funding_for(symbol, 本格 funding 游标)`。
  - None：现状逐格调用，一行不动。
  - 摄入/记账/补单逻辑完全共用（供给面之下无分叉）。
- `Reconciler.reconcile_open_orders(grid_id, symbol, snapshot=None)`：
  on_exchange ← `snapshot.orders_for(symbol)`。
- `Reconciler.check_position_drift(grid_id, symbol, snapshot=None)`：
  real ← `snapshot.position(symbol)`（None 视为 0.0，与交易所无仓位行同义）。
- `Reconciler.reconcile_fuses(grid_id, symbol, snapshot=None)`：
  on_exchange ← 快照挂单；`_fuse_filled` ← 快照 trades（order_id 匹配）。
  覆盖性论证：快照 trades 窗口起点 = 全格最小游标−重叠 ≤ 本格最后已知成交，宕机窗口内
  发生的保险丝成交必然晚于该点 → 必在快照中。

### 4. 语义边界（写进测试的关键交互）

**快照时序 × E2 宽限**：本轮 sync 补挂的新单，在轮首快照里必然缺席 → 本轮 reconcile
对它 missing 计 1；下一轮快照已含它 → 计数清零。`replace_grace=2` 恰好吸收，无幻影重挂。
保险丝本轮重挂同理（下轮快照可见）。该交互必须有专门测试钉死（含 grace=1 会误杀的
反例注释，防未来有人调小 grace）。

**轮内一致性**：同轮所有单元读到同一时刻的账户视图（现状是每调一次一个时刻）。
止损评估价格新鲜度从"单元执行时刻"变为"轮首"，差 ≤ 轮长（秒级）；交易所侧
reduce-only 触发单保险丝独立护极端行情，语义上可接受。

**失败语义（用户决定：整轮跳过）**：`build_account_snapshot` 抛错 → cycle 记日志
`[monitor] snapshot failed: %r`，本轮不派发任何单元（不 sync/不止损/不对账），心跳照打，
下轮重建。与现状 account_read 电路 open 的行为一致；保险丝在交易所侧独立生效。

### 5. Cycle 接线（cycles.py）

`run_monitor_cycle`：前置段（CLOSING 续平 / 死 OPENING 清理）之后、单元派发之前，
主线程收集 ACTIVE 格 → 计算两个 since 游标 → `build_account_snapshot(...)` →
`_grid_unit(..., snapshot=snap)` 透传。快照构建失败 → 跳过单元段，其余（指令消费、
equity、心跳、总结行）照常；总结行加 `snap=miss` 标记。无新增 env 开关：
snapshot=None 代码路径本身就是回退面，真要线上回退直接部署旧版。

## 权重账

| | 每轮读权重 | ~1.2 轮/分 | 50 格外推 |
|---|---|---|---|
| 现状（逐格） | ~420（5 格） | ~500/min | ~4200/轮，不可行 |
| 快照后 | **~64，与格数无关** | ~77/min | 仍 ~64/轮 |

预算 1200/min；余量还给 scheduler 选币窗口（~690/min 峰值）与未来扩格。

## 测试策略

1. snapshot 模块：视图过滤正确性（symbol/since 边界）、游标口径（新格用 created_at）、
   构建失败传播（fault 注入某一调用）。
2. base 默认 `_all` 实现与逐格调用**差分等价**。
3. HL adapter：stub client 验证 `fetchMyTrades(None)` 逐行 symbol 来自 coin 映射
   （盖印坑回归）、allMids 解析、funding 按 delta.coin 分组、`fetchOpenOrders(None)` 解析。
4. sync/reconcile 快照等价性：同场景 snapshot vs None 双路终态一致（成交摄入/补单/
   对账 canceled+replaced/漂移/保险丝三态）。
5. E2 宽限交互：本轮补单下轮不被幻影重挂（§4）。
6. cycle：快照失败整轮跳过 + 日志 + 心跳照打；并行单元共享快照（真线程）结果正确。
7. 现有全套（570+）保持全绿：默认参数 None 时逐字节走旧路径。

## 上线前硬性验证项（真 testnet 直调）

- ccxt `fetchMyTrades(None, since)`：确认可用、返回逐行真实 symbol（或 coin 可映射）。
- ccxt `fetchOpenOrders(None)`：确认可用、含触发单。
- `allMids` 响应结构与 coin→canonical 映射（含 kPEPE 类前缀币）。
- 若任何一项与假设不符 → HL 原生实现改用 ccxt 底层 `publicPostInfo`/`privatePostInfo`
  直调对应端点，接口层设计不变。

## 部署与验证

1. testnet 先行：观察轮长（读路径趋零，轮长≈写延迟+计算）、429=0、成交摄入/补单/
   保险丝/漂移与快照前行为一致、跨换仓（含 scheduler 选币窗口叠加时段）。
2. 验证通过 → main→production（mainnet **MONITOR_PARALLEL 维持 2**，用户决定）。
3. 回退 = 部署旧版（无 schema/状态迁移；快照为纯供给面变化）。

## 改动面

`gridtrade/execution/snapshot.py`（新）；`exchanges/base.py`、`hyperliquid.py`、`fake.py`、
`resilient_adapter.py`（接口+实现+电路归类）；`execution/grid_executor.py`、`reconciler.py`
（可选 snapshot 参数）；`runtime/cycles.py`（轮首构建+透传+失败跳过）。
