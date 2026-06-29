# 真实平台手续费落库 — 设计文档

- 日期：2026-06-30
- 状态：已评审通过（待出实现计划）
- 分支：dashboard-p2

## 背景与问题

系统当前**不记录每笔交易的真实平台手续费**：

- 逐笔成交存于 `grid_fills` 表（`trade_id / side / price / size / ts`），但**无 fee 列**。
- 交易所摄入成交时确实回报了每笔真实手续费 `t.fee`（`exchanges/base.py` 的 `Fill.fee`），但 `grid_executor.py` 构造持久化 `Fill` 时**丢弃**了它（仅放进内存返回值 `new_fills_payload`，不落库）。
- 唯一的手续费字段 `accounting.fee_paid`（每网格汇总）不是真实值，而是**用配置费率估算**：经 `LiveEquity.snapshot()` → 共用引擎 `core.grid_engine.cal_equity_curve` 用 `order_num * touch * fee`（费率）重算得到。

### 关键耦合（决定方案边界）

`cal_equity_curve` 是**回测与实盘共用的同源引擎**。其中：

- `fee` 参数是**费率**，逐笔算 `order_num * touch * fee`，累加成 `df['fee']`。
- `realized_pnl`（来自 `real_profit = grid_gap * order_num`）是**毛利、不含费**。
- 估算费**只影响 `net_value`**：`profit = real_profit - fr_fee - fee + unreal_profit`，`net_value = (profit + cap)/cap`。

结论：估算费当前仅进入 `net_value/pnl_ratio`，不进入 `realized_pnl`。因此真实化必须走 **display/snapshot 层**，**绝不改共用引擎**（否则实盘/回测口径分叉、有破坏回测风险）。

## 目标

1. 每笔成交的真实平台手续费 `t.fee` 落库（`grid_fills.fee`）。
2. `accounting.fee_paid`（每网格汇总）改为真实手续费之和。
3. `net_value/pnl_ratio` 与真实费口径保持一致。
4. 共用回测引擎 `cal_equity_curve` 零改动、回测行为不变。

## 已确认决策

| 决策点 | 选择 |
| --- | --- |
| 落库口径 | 逐笔落库 + 汇总也用真实值 |
| net_value 一致性 | 修正 net_value 用真实费（不动共用引擎） |
| 表迁移 | 手写一次性迁移脚本（部署手动跑一次） |
| 历史回填 | 不回填，只管新成交 |

## 设计

### 1. 数据模型

- `gridtrade/state/models.py`：`grid_fills` 表在 `size` 之后新增
  `Column('fee', Float, nullable=False, default=0.0)`。
- `Fill` dataclass 新增 `fee: float = 0.0`。
- `gridtrade/state/fills.py`：`FillRepository._FIELDS` 加入 `'fee'`
  → `add_if_new` 自动持久化、`list_by_grid` / `_to_fill` 自动读回。

### 2. 写入路径（grid_executor，两处）

- 持久化：`sync()` 构造 `Fill(...)` 时带 `fee=float(t.fee)`（约 119-120 行）。`t.fee` 本就存在（交易所回报），不再被丢弃；`add_if_new` 负责持久化与去重。
- 运行态记账：`sync()` 喂运行中累加器的 `self.live[grid_id].record_fill(t.price, t.side, t.size, t.ts)`（约 127 行）须改为带 `float(t.fee)`，否则运行进程的 `real_fee_paid`（进而 `accounting.fee_paid`，由约 156 行 `acc.fee_paid = snap['fee_paid']` 落库）仍是估算值。
- 底仓不动：`open()` 的合成底仓 `record_fill(entry, 'buy', order_num, 0)`（约 80 行）保持不传 fee，走估算回退（合成、无真实成交，保持 net_value 连续）。

### 3. 汇总真实化（核心）

- `gridtrade/execution/live_equity.py`：
  - `LiveEquity` 新增累加器 `self.real_fee_paid = 0.0`。
  - `record_fill(self, price, side, size, ts_ms, fee=0.0)`：累加 `self.real_fee_paid += float(fee)`。
  - `replay(fills)`：入参元组扩展为 `(price, side, size, ts_ms, fee)`。
  - `snapshot(mark_price)`：
    - `fee_paid` 返回 `self.real_fee_paid`（真实值，替换原 `last['fee']`）。
    - net_value 修正：`net_value += (est_fee - real_fee) / self.cap`，其中 `est_fee = float(last['fee'])`、`real_fee = self.real_fee_paid`。使 `net_value / pnl_ratio` 与真实费口径一致。
    - 空 fills 分支 `fee_paid` 仍返回 `0.0`。
  - **`cal_equity_curve` 不改**。
- 平仓 taker 费：真实平仓成交也会被摄入、带真实 `t.fee`，自然计入 `real_fee_paid`，与 snapshot「不预扣平仓 taker 费」的语义一致。

### 4. 重放/重建路径

- `gridtrade/execution/reconciler.py`：从持久化成交重建时
  `live.record_fill(f.price, f.side, f.size, f.ts)` → 加 `f.fee`。
  因 fee 已落库，重启重放真实费不丢。
- 注：reconciler 中按 `entry_price` 预置的初始底仓 `record_fill(g.entry_price, 'buy', order_num, 0)` 无真实费，fee 省略走默认 0（与既有底仓记账假设一致）。

### 5. 迁移（手写一次性脚本）

- 新增 `scripts/migrations/2026_add_fee_to_grid_fills.py`：
  - 幂等：先查 `grid_fills` 是否已有 `fee` 列（用 SQLAlchemy inspector），有则跳过。
  - 缺列则执行 `ALTER TABLE grid_fills ADD COLUMN fee ... DEFAULT 0`，PG/SQLite 兼容。
  - 部署时手动跑一次。

### 6. 已知后果（不回填的代价）

⚠️ 跨迁移时**已在运行的网格**：重启重放时历史 fill 的 `fee=0`，故这些网格切换后 `real_fee_paid` 会漏掉历史段的真实费（比旧估算值偏低），随新成交逐步累积修正。鉴于「不回填、testnet 历史数据价值不高」的决策，这是接受的代价。

### 7. 测试（TDD）

- `FillRepository`：fee 落库 + 读回；旧行（无 fee）默认 0。
- `LiveEquity`：`record_fill` 累加 `real_fee_paid`；`snapshot` 的 `fee_paid` 为真实和、net_value 修正正确；`replay` 带 fee 重建。
- `grid_executor.step`：`t.fee` 写入 `grid_fills`。
- `reconciler`：重放带 fee，`real_fee_paid` 重建正确。
- 迁移脚本：幂等（重复跑不报错）+ 缺列时加列。

## 影响面 / 非目标

- 不改 `cal_equity_curve`、不改回测路径。
- 不回填历史 fee。
- 不新增 Dashboard 字段（如需展示真实费为后续单独工作）。
