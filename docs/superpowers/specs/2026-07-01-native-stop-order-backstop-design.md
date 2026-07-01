# 交易所原生止损单（灾难保险丝）设计

> 日期：2026-07-01
> 定位：把网格的「软件轮询式软止损」补上一道**交易所原生硬止损**，堵住跳空/爆拉/进程宕机/API 熔断/5s 盲区下软止损失效的结构性风险。

---

## 1. 背景与动机

当前止损是 monitor 机每 ~5s 轮询、软件侧评估 `pnl_ratio` 触发平仓（见 `execution/monitor.py` + `core/stop_rules.py`）。结构性风险：

1. **5s 盲区**：两次轮询之间价格剧烈移动看不到。
2. **无交易所驻留止损**：进程宕机/部署中、或 ResilientAdapter 熔断打开期间，止损完全不评估。
3. **网格下跌反向加仓**：中性网格越跌买单越成交、净多头越大，亏损超线性恶化。
4. **市价平仓滑点穿透**：极端行情 reduce 市价单受 5% 滑点上限可能成交不掉。

本设计**不替换**软止损，而是新增一道**交易所原生 reduce-only 触发单作为灾难保险丝**：守卫放在交易所撮合引擎内、连续盯价、不依赖本进程在线。软止损仍是日常主刹车；保险丝触发价设得更深（破网价），平时不该被触发。

## 2. 关键事实（ccxt 4.5.61 / hyperliquid，已核实源码）

触发单走 `create_order(symbol, type, side, amount, price, params)`：

- 止损：`params={'stopLossPrice': 触发价}`（或别名 `triggerPrice`）→ `tpsl='sl'`。
- 触发后市价：`type='market'`（`isMarket=True`）。
- 只减仓：`params={'reduceOnly': True}`。
- HL 最终结构：`orderType['trigger'] = {'isMarket': True, 'triggerPx': 触发价, 'tpsl': 'sl'}`。

**关键坑（本设计的核心约束）**：HL 触发市价单的成交价底线在**下单时**就锁死：
```
px = price × (1 + slippage)   # buy
px = price × (1 − slippage)   # sell
```
其中 `price` 是**提交时传入的参考价**、`slippage` 默认 5%。若参考价用「现价」、触发价在很远的破网价，触发时市场可能已跌穿 px 底线 → 单子变限价单成交不掉 → 止损失效。

**对策**：参考价**传触发价（破网价）本身**，则底线 = `破网价 × (1∓slippage)`；`slippage` 即「为保成交愿在破网价之外再追多远」，由 config 控制、默认放宽到 0.15。

## 3. 范围

**做**：原生 reduce-only 止损保险丝，开网时挂、对账维护、触发后撑网全拆。

**不做 / 不碰**：
- 软止损逻辑（`monitor.py` / `stop_rules.py`）—— 保留为主刹车。
- core 引擎、`LiveEquity` 记账数学 —— 全部不动。
- 保险丝成交**不进** `grid_fills` —— 与 init/close 市价单一致，避免污染同源记账。
- 动态追踪触发价 —— 破网价开网时定死、不随行情变（YAGNI）。

## 4. 架构落点

不新增模块，复用现有三处职责：

| 层 | 改动 |
|---|---|
| `exchanges/base.py` | `ExchangeAdapter` 加抽象方法 `create_stop_order` |
| `exchanges/ccxt_adapter.py` | 通用实现（HL 继承） |
| `exchanges/hyperliquid.py` | 继承 ccxt 实现（HL 的参考价/滑点已在通用版处理；如 cloid 有特殊规则沿用 `encode_cloid`） |
| `exchanges/fake.py` | `_stops` 待触发簿 + `_match` 内穿越触发 + reduce-only 封顶 |
| `exchanges/faulty.py` | 透传新方法 |
| `exchanges/resilient_adapter.py` | 把 `create_stop_order` 纳入重试包装 |
| `state/models.py` | `grids` 表加两列 `fuse_low_oid` / `fuse_high_oid`（durable 保险丝 exchange order id） |
| `runtime/dbadmin.py` | 幂等迁移：给存量库加这两列 |
| `execution/grid_executor.py` | `open()` 挂两张保险丝、写 `fuse_*_oid`；`_fuses` 内存缓存；`restore()` 从列重建 |
| `execution/reconciler.py` | 每轮判定保险丝三态（在/丢/已触发），驱动重挂或撑网全拆 |
| `config.py` | 新增 `STOP_SLIPPAGE`（默认 0.15）、`STOP_ORDERS_ENABLED`（默认 true） |
| `runtime/factory.py` | 把开关/滑点接进 executor/reconciler 构造 |

## 5. 详细设计

### 5.1 端口 `create_stop_order`

```python
# base.py — ExchangeAdapter
@abstractmethod
def create_stop_order(self, symbol: str, side: str, size: float,
                      trigger_price: float, *,
                      reduce_only: bool = True,
                      slippage: float = 0.15,
                      client_oid: Optional[str] = None) -> Order: ...
```

```python
# ccxt_adapter.py
def create_stop_order(self, symbol, side, size, trigger_price, *,
                      reduce_only=True, slippage=0.15, client_oid=None) -> Order:
    p = self._params(reduce_only, client_oid)
    p['stopLossPrice'] = trigger_price      # → tpsl='sl'
    p['slippage'] = slippage
    # 参考价传触发价本身：成交底线 = trigger_price × (1∓slippage)
    r = self.client.create_order(self.to_native(symbol), 'market', side, size,
                                 trigger_price, p)
    return self._to_order(r)
```

HL 适配器直接继承上面的实现，无需重写。

### 5.2 Fake 撮合

- 新增 `self._stops: dict[symbol, list[Order]]`（待触发簿，不进 `_open`）。
- `create_stop_order`：建 Order（`price=trigger_price`，`status='open'`），入 `_stops`，返回。
- `_match(symbol, price)` 内追加 `_check_stops`：
  - 穿越判据：`side=='sell' and price <= trigger` 或 `side=='buy' and price >= trigger`。
  - 触发 → 按当前价成交（可叠加 slippage 模拟成交价），`reduce_only=True` 时成交量封顶到 `abs(当前 net_size)`；若当前无反向持仓则不成交（reduce-only 空操作）。
  - 成交后从 `_stops` 移除；`fetch_my_trades` 能查到该成交（带 `order_id`）。

### 5.3 开网挂保险丝（`GridExecutor.open()`）

转 ACTIVE 前，若 `stop_orders_enabled`：

```python
worst = grid_count * order_num           # 最坏满仓上界
low = self.adapter.create_stop_order(symbol, 'sell', worst, stop_low_price,
                                     reduce_only=True, slippage=self.stop_slippage,
                                     client_oid='%s:fuse:low' % gid)
high = self.adapter.create_stop_order(symbol, 'buy', worst, stop_high_price,
                                      reduce_only=True, slippage=self.stop_slippage,
                                      client_oid='%s:fuse:high' % gid)
# 持久化 exchange order id 到 grids 行（跨重启可对账）
self.grids.set_fuse_oids(gid, low_oid=getattr(low, 'id', None),
                         high_oid=getattr(high, 'id', None))
self._fuses[gid] = {'low': getattr(low, 'id', None), 'high': getattr(high, 'id', None)}  # 内存缓存
```

- client_oid 专属命名空间 `:fuse:low` / `:fuse:high`，与网格线 oid（`gid:line:seq`）、init/close 市价单区分。
- **exchange order id 持久化到 `grids.fuse_low_oid` / `fuse_high_oid`**：HL 成交只带 oid、不带 cloid，5.4 的「已触发」判定靠 order id 反查成交；持久化保证**跨进程重启**也能判定一张已触发的保险丝（否则重启后无从得知它触发过）。
- 保险丝**不写 `grid_orders`**（与 init/close 市价单一致），故 `sync()` 的 by_oid 不含它、成交不会被误记入 `grid_fills` / 误触发补对侧单。
- `restore()`：从 `grids` 行读破网价 + `fuse_low_oid`/`fuse_high_oid` 重建 `_fuses` 内存缓存。

> **待 testnet 验证假设**：HL/ccxt 的 reduce-only 把超额 size 封顶到持仓（标准行为）。若不封顶，回退为「size = 当前 net_size、每轮 sync 同步改单」。

### 5.4 Reconciler 判定保险丝三态

新增 `reconcile_fuses(grid_id, symbol)`（与网格线对账 `reconcile_open_orders` 分开，因动作不同）。对每个 ACTIVE 网格的两张保险丝（order id 从 `grids.fuse_*_oid` 读）：

| 状态 | 判据 | 动作 |
|---|---|---|
| 在挂 | order id 仍在 `fetch_open_orders` | 无动作 |
| 被丢 | 不在挂单簿 **且** `fetch_my_trades` 无该 oid 成交 | 重挂（新 order id 回写 `grids.fuse_*_oid`） |
| 已触发 | 不在挂单簿 **且** `fetch_my_trades` 有该 oid 成交 | 撑网全拆：`executor.close(grid_id, symbol, '保险丝触发')` |

- 「被丢 vs 已触发」唯一靠**该 exchange order id 有没有成交**区分。复用现有按 order id 匹配成交的机制。
- 重挂时按 5.4 的查询窗口拉 `fetch_my_trades`（带 since 游标），避免每轮全量拉。
- 与网格线重挂宽限（E2）正交：保险丝消失即按「有无成交」二分，不需要宽限——它要么触发（成交可查）、要么真被丢（无成交、立即补）。

### 5.5 触发收尾

`executor.close()` 与软止损**同一条路径**：
- `cancel_all(symbol)` 一并撤掉另一张未触发的保险丝 + 所有网格限价单；
- 重拉持仓、有界 reduce 残仓（≤3 次）；
- `Record(exit_reason='保险丝触发')` 落库（幂等）；
- 转 CLOSED。

中途失败留下的 CLOSING 网格由 monitor 循环 `finalize_close` 续平自愈（已有机制）。

### 5.6 配置

```python
# config.py DeployConfig 新增
stop_orders_enabled: bool = True      # STOP_ORDERS_ENABLED
stop_slippage: float = 0.15           # STOP_SLIPPAGE
```

`STOP_ORDERS_ENABLED=false` 时：`open()` 不挂保险丝、Reconciler 跳过判定 → 纯软止损回退，**零行为变化**（便于灰度/回滚）。

## 6. 测试（TDD，FakeExchange 上）

1. **端口撮合**：`create_stop_order` 挂单不立即成交；价格穿越触发价后成交，`fetch_my_trades` 可见、带 order_id。
2. **reduce-only 封顶**：stop size 大于持仓时，成交量被封顶到持仓；无反向持仓时空操作。
3. **开网挂两张**：`open()` 后挂出 sell@stop_low、buy@stop_high，size=grid_count×order_num，方向/价/reduce_only 正确。
4. **触发→撑网全拆**：价格穿 stop_low → 保险丝成交 → Reconciler 判定「已触发」→ 网格转 CLOSED、剩余限价单全撤、Record exit_reason='保险丝触发'。
5. **被丢→重挂**：保险丝从挂单簿消失但无成交 → Reconciler 重挂、不平仓。
6. **开关**：`STOP_ORDERS_ENABLED=false` → open 零保险丝、Reconciler 跳过、行为与现状一致。
7. **restore**：重启后 `_fuses` 从 `grids` 行（破网价 + `fuse_*_oid`）重建；保险丝仍在挂 → 判在挂；保险丝在重启前已触发 → Reconciler 靠持久化 order id 反查成交、判已触发 → 撑网全拆。
8. **迁移幂等**：`dbadmin migrate` 给存量 grids 表加 `fuse_low_oid`/`fuse_high_oid` 列；重复跑无副作用；存量 ACTIVE 网格列为 NULL（视为未挂保险丝、Reconciler 当轮补挂）。

双后端（SQLite + Postgres）沿用现有 `store` fixture。

## 7. testnet 验证清单（mainnet 前）

- 部署前跑一次幂等迁移给存量库加列：`fly machine run <image> python -m gridtrade.runtime.dbadmin migrate`（同 `fee` 列迁移先例）。
- HL testnet 实测 reduce-only 超额 size 是否封顶到持仓（5.3 假设）。
- **确认刚挂的保险丝出现在 `fetch_open_orders`（frontendOpenOrders）**——这是 `reconcile_fuses` 判「在挂」的前提；若真 HL 触发单不在该端点返回，会每轮误判「被丢」而重挂、孤儿触发单堆积。cycles.py 已在 `replaced>0` 打日志，testnet 首轮观察该行是否反复出现即可暴露。
- HL 触发单默认对哪种行情价触发（mark/last）—— `describe()` 里 `triggerPriceType` 默认 None，实跑确认，必要时显式传。
- 触发市价单的真实成交滑点（注意 testnet 薄盘会放大，区分「机制」vs「流动性」）。
- 端到端：人为把网格区间设窄，观察价格穿破网价 → 保险丝触发 → 撑网全拆。

## 8. 风险与回退

- **reduce-only 不封顶**（假设不成立）：回退到 5.3 的「按当前 net_size 每轮同步」分支。
- **触发价类型不符预期**（mark vs last 提前/滞后触发）：显式设 `triggerPriceType`。
- **保险丝误触发**（破网价设太近）：破网价由布网参数控制，已有 stop_buffer；必要时调 `grid_v2_config`。
- **整体回滚**：`STOP_ORDERS_ENABLED=false` 一键退回纯软止损，零行为变化。

## 9. 开放问题

无（关键决策已在 brainstorming 敲定：灾难保险丝定位 / 破网价触发 / 最坏仓+对账重挂 / config 滑点默认 0.15 / 触发后撑网全拆）。
