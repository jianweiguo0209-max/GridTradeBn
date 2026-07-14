# 保险丝覆盖率保障(权益自适应降 cap/拒币) 设计

> 状态:**已获用户批准(2026-07-15)**。两项用户决策:①路线=E-lite(权益自适应保障:不足额先降
> cap 保住币、降到不可行才拒币);②`FUSE_MIN_COVERAGE` **testnet 与 mainnet 同设 1.0**
> (让机制在 demo 上被真实跑通,不做只在主网启用的死代码)。
> 实现前如遇本文未覆盖的分歧点,不确定就问,勿猜。

## 一、问题与量化(2026-07-15 主网公开数据实测)

**症状**:每格挂两张 reduce-only STOP_MARKET 保险丝,数量 = `worst = order_num × grid_count`
(满仓最大持仓)。币安 `MARKET_LOT_SIZE.maxQty` 限制单笔市价单数量;`worst > maxQty` 时下单
被 -4005 拒 → 开格卡死 OPENING。**ed4616e 把 create_stop_order 封顶到 maxQty**,代价是超出
部分无原生硬保护(只剩软止损 5s 轮 + 爆仓线)。testnet 实证 HMSTR 足额率仅 36%。

**关键量化发现——testnet 的不足额是 demo 环境产物,主网当前零不足额**:

| 币 | demo maxQty | 主网 maxQty | 倍数 | 主网市价名义上限 |
|---|---|---|---|---|
| HMSTR | 7,000,000 | 400,000,000 | 57× | $78,680 |
| PORTAL | 30,000 | 5,000,000 | 167× | $55,500 |

622 个永续里 demo 与主网 maxQty 相同的只有 35 个(中位差 3×,最大 1200×)。

**推导**:`worst ≈ cap×gearing×max_rate / P`(实盘 max_rate=1.0,grid_executor.py:84)⇒
满仓名义额 = `cap×gearing = equity×frac×gearing = equity×0.8333`(frac=0.2451,gearing=3.4)。
足额条件 = `maxQty×P ≥ 满仓名义额`。主网票池(291 币)最小市价名义上限 **$30,570**:

| 权益 | 满仓名义额 | 主网票池不足额币数 |
|---|---|---|
| $3k–$30k | $2.5k–$25k | **0 / 291** |
| **$36,684** | $30,570 | **临界点** |
| $50k | $41,667 | 7 / 291(最差 73%) |
| $100k | $83,333 | 130 / 291(最差 37%) |

**真实风险不是"现在保护不足",而是"随权益增长静默变成不足"**——$36.7k 起出现,$100k 时近半
票池不足额。本设计要的就是:今天零扰动,临界到来时自动接管,且提前可见。

## 二、总览

**不变量(明确不改)**:适配器封顶(ed4616e)保留——它是防 -4005 硬失败的最后一道;
`reconcile_fuses` 重挂路径不改(它按 DB 的 `grid_count × order_num` 重算 worst,而该格开仓时
cap 已被本机制定稿 ⇒ worst 天然 ≤ maxQty,封顶仍作兜底);grids 表 schema 不动(两列 fuse oid);
回测几何不动(理由见 §七)。

**非目标**:多张丝分摊(B——schema 迁移 + 对账重写,撞历史事故高发区,主网当前用不上);
`closePosition=true` 全平丝(见 §八备选闸门);改 gearing/frac/max_rate 仓位体系。

## 三、组件一:数据面 `Instrument.market_max_qty`

沿 `min_cost` 先例(2026-07-14 §5.3 同款):

- `base.py`:`Instrument` **末尾追加** `market_max_qty: float = 0.0`(0=未知/无约束 → fail-open;
  位置参构造兼容不破);
- `ccxt_adapter.py::list_instruments`:从 `m['limits']['market']['max']` 填充(缺失=0.0);
- `BinanceAdapter._market_max_qty`(下单时单币即查)保留不动——与 Instrument 同源(都读
  `limits.market.max`),职责不同:一个批量供门链、一个即时供封顶。

零额外 API 调用:门链已在 `begin_batch` 批量拉 instruments。

## 四、组件二:纯函数 `execution/fuse_policy.py`

```python
def fuse_capped_cap(cap, gearing, grid_params, market_max_qty, *,
                    min_amount=0.0, min_coverage=1.0):
    """返回 (cap', coverage)。coverage = maxQty/worst(1.0=足额;None=未知)。"""
```

语义(**`min_coverage` 只是"干预触发阈值",一旦干预就干到足额**——不存在"降到 80% 就收手"
的中间态,那既不省仓位又不护全额):
- `market_max_qty <= 0`(未知)→ `(cap, None)` **fail-open**(不干预,交易所自会校验);
- `grid_order_info` 返回 None(cap 太低建不了网)→ `(cap, None)`(交给 MinNotionalGate 拒);
- `min_coverage <= 0` → 停用(仅算 coverage 供审计,不降 cap);
- `coverage >= min_coverage` → `(cap, coverage)` 原样(容忍范围内);
- 否则 `cap' = cap × maxQty/worst`(⇒ coverage' = 1.0 足额),再用 `grid_order_info(cap', ...)`
  复核 `worst' ≤ maxQty`(order_num 随 cap 线性、`min_amount` 向下取整只减不增 ⇒ 必然成立;
  **仍加断言**防未来改动)。

口径与 `executor.open` 同源:`grid_order_info(cap, gearing, low, high, grid_count, stop_low,
stop_high, min_amount=min_amount, max_rate=1.0)`,`worst = 每笔数量 × grid_count`。

## 五、组件三:门链 `FuseCoverageGate`

与 `MinNotionalGate` **完全同构**(gates.py 既有模式):`begin_batch` 刷 `{symbol: market_max_qty}`
映射(经 `adapter.list_instruments()`),取数失败 → 空映射 **fail-open**;未经 begin_batch 的独立
`evaluate` 惰性加载一次(`is None` 哨兵)。

`check(proposal)` 职责**只有两件**:
1. 不足额 → 降 cap,**写回 `proposal.cap = cap'`** 并 log(降幅+coverage);
2. `cap' < CAP_MIN` → 拒(reason 含 coverage/cap'/CAP_MIN)。

**降后"每笔名义额够不够"不重复实现**——交给紧随其后的 `MinNotionalGate` 用新 cap 自然拒
(DRY;它已按币 `max(env, min_cost)` 判)。

**链序**(factory.py):`MaxConcurrent → FuseCoverage → RiskBudget → MinNotional → Margin`
——cap 在被任何"吃 cap"的门消费前定稿。`RiskBudgetGate`/`MarginGate`/`executor.open`/
`LiveEquity` 都已 honor `proposal.cap`(pnl 分母诚实,不重演 restore-cap 3x 错标事故)。

## 六、组件四:执行面告警 + 组件五:选币轮审计

- **执行面**:`executor.open` 挂丝时若实际下单量被封顶(worst > maxQty),打结构化告警(含
  coverage%、symbol、grid_id),**不阻断**——手动 `OPEN_GRID` 直调 `ex.open` 绕过门链是既有
  设计("手动指令不经票池、保留实验自由度",fly.toml 注释),给它响亮日志而非拦截;自动路径
  已被门链保障,此告警只应在手动/fail-open 时出现。
- **选币轮审计**(scheduler,resolve_live_universe 之后):当前 cap 下票池里**几个币不足额、
  最差币与其覆盖率**,一行 log。limits 复用 ccxt 缓存 markets(零权重);价格走 fetch_prices_all
  (权重 2,每选币轮一次 → 可忽略,非"零额外 API")。作用:让"逼近 $36.7k
  临界"提前数月可见,而非等出事;也是 §七 回测偏离的报警信号。

## 七、配置与回测口径

- `FUSE_MIN_COVERAGE` → `DeployConfig.fuse_min_coverage`(默认 **1.0**);两 toml 均设 `"1.0"`
  (用户定:demo 上机制被真实跑通——降 cap 与拒币两条路径都会触发,不做主网才启用的死代码)。
  `0` = 仅审计不干预(紧急回退开关)。

- **回测不建模封顶,两侧口径当前一致**:主网不足额集合为空(§一)⇒ 门链恒不触发 ⇒ 实盘几何
  与回测几何相同。**边界声明(诚实)**:一旦 §六 审计日志报出不足额币(权益 > ~$36.7k 或币安
  收紧 maxQty),即为**实盘几何开始偏离回测**的信号;届时须回测同步建模(历史 maxQty 无归档,
  Vision 只有 K 线,需另立项),或临时把 `FUSE_MIN_COVERAGE` 置 0 换回"不足额但口径一致"。
  demo 侧因 maxQty 离谱而恒触发,属测试环境已知偏离(demo 数据本就不入回测)。

## 八、备选闸门(不实现,记录触发条件)

当权益增长到降 cap 的机会成本变得显著($100k 时近半票池被缩仓)时,两条升级路:
- **多张丝分摊(B)**:护全额不缩仓。需 grids schema 迁移(两列 oid → 多张)+ set_fuse_oids/
  reconcile_fuses/_fuses 重写;`reduce_only` 触发按实际持仓执行使多丝叠加安全,但**同触发价
  多张须 testnet 实测**(残丝是否被拒/成孤儿)。撞历史事故高发区(HL 166 张孤儿触发单)。
- **`closePosition=true` 全平丝(F)**:ccxt 支持(exchange-specific param),不带 quantity ⇒
  天然绕过 MARKET_LOT_SIZE,单丝护全额、零 schema 改动。**阻断条件**:它平掉**整个 symbol
  净仓**,而 tier2_cap=2 允许同币多格(PositionLedger 的 claims/转仓/close_share 全建立在
  "多格共享一个交易所净仓"之上)⇒ 会误伤兄弟格。仅当 `tier2_cap=1`(禁同币多格)时才可选。

## 九、测试

- **纯函数**(`tests/execution/test_fuse_policy.py`):足额不动 / 不足额降到刚好(worst' ≤ maxQty
  且 ≈ maxQty) / maxQty 未知 fail-open / `min_coverage=0` 停用 / min_amount 取整后仍达标 /
  grid_order_info 返 None 时 fail-open。
- **门链**(`tests/execution/test_gates.py` 追加):降 cap 写回 proposal.cap / `cap' < CAP_MIN`
  拒 / `list_instruments` 抛异常 fail-open / 与 MinNotionalGate 串联(降后每笔名义额不足 →
  被 MinNotionalGate 自然拒,验证 DRY 分工)。
- **数据面**(`tests/exchanges/test_ccxt_adapter.py` 追加):`market_max_qty` 从
  `limits.market.max` 填充 / 缺失 → 0.0。
- **既有** `test_create_stop_order_clamps_to_market_max_qty` 不动(封顶保留)。
- 全量 pytest 全绿;**不部署**(部署由运维会话按"避开整点 HH:00–HH:12"手动做)。
