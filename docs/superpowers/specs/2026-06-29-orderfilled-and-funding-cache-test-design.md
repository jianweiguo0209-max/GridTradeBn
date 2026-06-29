# OrderFilled 事件 + fetch_funding_range 离线缓存测试 — 设计

> 来源：设计延后件——事件总线第 3 事件（design.md §5/§10）、P5a 评审 follow-up（funding 路径对称测试）。
> 日期：2026-06-29。两项独立、同一计划交付。

## 第 1 项：OrderFilled 事件

### 背景
[events.py](../../gridtrade/execution/events.py) 的事件总线（Observer）目前只有 `GridOpened`/`GridClosed`，
由 `GridManager` 发布。设计规划的第 3 事件 `OrderFilled` 未实现。成交目前只落库
（`grid_fills`）+ 更新记账，不发事件——外部订阅者（通知/指标/看板）看不到逐笔成交。

### 关键决策：由 manager 发布（executor 保持 bus-free）
`GridExecutor` 故意不持事件总线（DI：bus 归 `GridManager`，由它发 GridOpened/GridClosed）。
OrderFilled 同样**由 manager 发**，与现有架构一致，executor 不 import events。

### 数据流与改动
成交只在 `executor.sync` 摄入（靠 `grid_fills.trade_id` 去重，幂等）→ `monitor_grid` →
`GridManager.monitor_all`。

1. **新事件**（events.py）：
   `OrderFilled(grid_id: str, symbol: str, line_index: int, side: str, price: float, size: float, fee: float)`
2. **`GridExecutor.sync`**：现返回 `{'new_fills': count, 'snapshot': snap}`，**加一个 `'fills'` 键**——
   本次新摄入成交的普通 dict 列表，每条 `{'line_index', 'side', 'price', 'size', 'fee', 'ts'}`，
   其中 `fee` 取自该笔交易所成交 `Trade.fee`（真实手续费）。仅 `add_if_new` 成功的成交进列表（幂等）。
   `new_fills` 计数保留（向后兼容）。executor 返回普通 dict，不 import events。
3. **`monitor_grid`**（monitor.py）：返回里透传 `'fills'`（加法，不破坏现有 `closed/reason/pnl_ratio`）。
4. **`GridManager.monitor_all`**：取得 monitor_grid 结果后，对每条 fill
   `publish(OrderFilled(grid_id=grid.id, symbol=grid.symbol, line_index=f['line_index'],
   side=f['side'], price=f['price'], size=f['size'], fee=f['fee']))`；
   再按现有逻辑处理 closed → 发 GridClosed（成交先于平仓）。

### 为何正确
覆盖所有生产成交路径（成交只经 sync→monitor_grid→monitor_all）；`trade_id` 去重保证不重复发；
executor 保持 bus-free；与 GridOpened/GridClosed 同源同模式。纯加法，不改正确性。

### 字段取舍
`grid_id, symbol, line_index, side, price, size, fee`——够外部做「哪条网格线、买卖、价量、手续费」
的通知/指标。不带 ts（YAGNI）。

### 测试
- 桩/FakeExchange 驱动一个网格：开网 → 设价穿越某格成交 → `monitor_all`（经 EventBus 订阅收集事件）→
  断言收到 1 个 `OrderFilled`，字段（line_index/side/price/size/fee）正确、`fee>0`。
- 二次 monitor（无新成交）→ 不再发 OrderFilled（幂等）。
- 同步成交触发止损平仓的场景：先收 OrderFilled 再收 GridClosed（顺序）。

## 第 2 项：fetch_funding_range 离线缓存测试

### 背景
[datasource.py](../../gridtrade/backtest/datasource.py) 的 `fetch_ohlcv_range` 有测试，
`fetch_funding_range` 无。二者共用 `_warm()`，但 funding 走 `namespace='funding'`、
`time_col='ts'` 分支（[datasource.py:41-42](../../gridtrade/backtest/datasource.py#L41)），该分支零覆盖。

### 测试（镜像 ohlcv，换 funding 路径；纯加测试）
用 `FakeExchange.seed_funding` 播 2 天、8h 间隔的资金费率行（FUNDING_COLS=`ts,symbol,fundingRate,realizedRate`）：
1. **warm→离线**：`fetch_funding_range` 预热 → 断言列/行数正确、两天都落缓存；换 `fetch_funding_history`
   会抛错的 `Offline` 适配器 + 同一 cache 的新 DataSource → 仍离线取到、数据一致。
2. **任意覆盖窗口完全复用**（正面验证用户关注点）：预热一个区间后，用 `Offline` 适配器
   （触网即 AssertionError）取该区间内的**子窗口**，断言完全由缓存服务、不触网、行数/数据正确。
3. **空哨兵**：某天无资金费 → 落空哨兵 → 离线仍正常（不报错、不把空当数据并入）。

### 若藏 bug
`time_col='ts'` 分桶分支若有缺陷，本组测试会变红——届时停下汇报（先红暴露、再决定修）。

## 范围外（YAGNI）
- OrderFilled 不接具体通知/告警渠道（只发事件，订阅者另说）；事件不持久化。
- OKX 原始 volCcyQuote、MarginGate、Threshold/ExternalSignal 触发器——属其他延后件，不在本计划。
