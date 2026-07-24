# snapshot 重读降频第一批：income TTL 300s + 保险丝 algo 簿 TTL 60s

日期：2026-07-23
状态：已批准（权重遥测归因后的第二刀）

## 背景与动机

`[weight]` 遥测（07-23 上线，prod v23）把权重大头点名：monitor 基线 ≈ 790/min，
其中 snapshot 六种批量读占 92%，头三名 = `fetch_open_orders_all` 80/轮（常规簿 40 +
algo 簿 40）、`fetch_my_trades_all` 50/轮、`fetch_funding_payments_all`(income) 30/轮，
×4.6 轮/min。整点选币扫描分钟实测 **w1m=1506/2400**（含同 IP monitor 流量），
headroom 仅 ~900——monitor 尖峰分钟对齐即 429（04:01Z THE 被跳实证）。

语义审计：income 喂 `grid_accounting.funding_paid` 记账，而币安资金费 **8h 才结算
一次**——13s 刷新纯浪费；algo 簿只喂保险丝存在性对账，丝极少变动。二者降频合计
**−280/min（基线 −35%），选币分钟 headroom 900→~1200**。

**明确不动**：常规簿（判成交核心）、userTrades（成交发现=补挂回转，降频=真损失）、
positions/prices/balance（权重小）。

## 前提审计（已核实，设计依赖）

1. **保险丝对账天然抗陈旧**（`reconciler.py:220-281` 三态主判，2026-07-06 KIOXIA/
   XYZ-MSTR 事故根治）：丝不在（可见）挂单簿 → 先问 `order_status` 权威状态，
   'open'=信息盲区不动。陈旧 algo 簿**不会导致重复挂丝**；重挂即消守卫（streak≥2
   停手）继续兜底。
2. **income 的 since_ms 每轮变**（`cycles.py:209-210`，全格 funding_cursor 取 min；
   新开格若无 funding_cursor，回退用 `created_at` 而非 0，since 一般不倒退——新币
   击穿走 symbols 超集规则，since 倒退护栏是纵深防御）——缓存必须带参数语义，不能只看 TTL。
3. **快照契约允许**：`base.py` 批量读契约原文"返回调用时刻的最新已知状态（只读幂等，
   **不要求强一致**）……实现不得让上层感知分页游标/权重/调用时序"——adapter 内缓存
   是契约内行为。
4. snapshot 每轮由 cycles 单线程构建一次（`cycles.py:211`），缓存无并发写压力；
   scheduler 为独立进程、独立 adapter 实例，互不影响。

## 设计（方案A：BinanceAdapter 内缓存）

缓存放 `gridtrade/exchanges/binance.py` 的 `BinanceAdapter`——两簿拆分与 income
单流本来就是它的私有知识；HL 遗留路径、snapshot builder、cycles、契约测试全部不动。
ResilientAdapter 的重试/熔断继续包住真实 fetch（缓存命中时根本不发请求，电路无感）。

### 1. income 缓存（`fetch_funding_payments_all`）

状态：`self._income_cache = None | (fetched_at_sec, since_used, symbols_used:set, grouped:dict)`
（`grouped` = 按 canonical symbol 分组的全量行，含 since_used 起的所有行）。

命中规则（三条全满足才命中）：
- `now - fetched_at < SNAPSHOT_INCOME_TTL_SEC`
- `请求 since_ms >= since_used`（cursor 只会前进；新开格 cursor=0 把 since 拉回 → miss 真取，正确性自保）
- `请求 symbols ⊆ symbols_used`（新币开格 → miss 真取）

命中 → 本地过滤：`{s: [p for p in grouped.get(s, []) if p.ts >= 请求since]}`，
只返回请求的 symbols 键（保持现契约:每个请求 symbol 必有键，缺省空列表）。
miss → 走现有真取逻辑（分页+去重不动），成功后整体写缓存。真取抛异常 → 原样上抛
（现行为：snapshot 构建失败整轮跳过），不写缓存。

### 2. algo 簿缓存（`fetch_open_orders_all` 内部）

状态：`self._algo_book_cache = None | (fetched_at_sec, rows:list)`（账户级原始行，无参数）。

`fetch_open_orders_all`：常规簿**每次真取**（不变）；algo 簿
`now - fetched_at < SNAPSHOT_ALGO_BOOK_TTL_SEC` 时用缓存行，否则真取并更新。
merge 后按 want 过滤（现逻辑不变）。algo 簿真取抛异常 → 原样上抛（同现行为）。

**写失效**：`create_stop_order` 成功返回后 `self._algo_book_cache = None`——新挂的丝
下一轮立即可见，省掉三态判的 order_status 兜底链。`cancel_order` **trigger 路径**撤单
成功同样失效（终审修订 2026-07-23：原设计"幽灵行无行为影响"只对三态判成立，漏了同簿
的孤儿清扫消费方——cap2 同币关一格后，幸存格的孤儿清扫会对幽灵丝行 cancel_order →
OrderNotFound 上抛 → 该格 reconcile 降级 2-4 轮。trigger 路径失效根除之；常规簿路径
撤单不失效，网格常规撤单不牵连缓存收益）。

### 3. 配置

`config.py` 新增两个 env（跟随现有 SIGNAL_REFRESH_SEC 模式）：
- `SNAPSHOT_INCOME_TTL_SEC`，默认 300
- `SNAPSHOT_ALGO_BOOK_TTL_SEC`，默认 60
- 语义：`<=0` = 关闭缓存（每次真取，逐字节恢复旧行为）

接线：`BinanceAdapter.__init__` 增加 kwargs `income_ttl_sec=300, algo_book_ttl_sec=60`
（硬默认，adapter 不 import config 保持层洁净、可测纯净）；factory 构建处把 config
值传入。fly.prod.toml 不写值（用默认）。

## 风险面（预注册）

- **丝触发发现延迟最坏 +60s**（13s→73s）：丝是交易所侧 STOP_MARKET，触发执行不受
  影响；延迟只作用于我方关格记账反应。软止损用每轮新鲜 prices 照跑。接受。
- 丝重挂后可见性：create_stop_order 失效钩子保证下轮可见；即便钩子失灵，三态判
  order_status 'open' 分支兜底不重挂。
- income 记账最坏延迟 5min：资金费 8h 结算，`funding_cursor` 单调推进语义不变，
  只晚记不漏记。verify-ledger 不受影响（对账走 trades/positions，不走 income）。
- 遥测判效口径：缓存命中发生在 BinanceAdapter 内部，但计数在 ResilientAdapter._call
  咽喉（逻辑调用）——**`[weight]` 的 calls/min 数字不会变，判效只看 w1m 水位下降**
  （预估监控基线 −280/min）。此点写进验收防误读。

## 测试（TDD）

- income：TTL 内同参二调只真取一次；since 回退（新格 cursor=0）击穿；symbols 超集
  击穿；命中时按请求 since/symbols 正确过滤切片；TTL 过期重取；真取异常上抛不污染缓存；
  `<=0` 关闭=每次真取
- algo 簿：TTL 内二调 algo 只取一次而常规簿两次；create_stop_order 后缓存失效下轮真取；
  TTL 过期重取；`<=0` 关闭
- 保险丝行为：陈旧缓存含已撤丝（幽灵行）→ 三态判不动作；缓存缺新丝+order_status
  'open' → 不重挂（既有用例应已覆盖，补缓存交互变体）
- 契约：fetch_funding_payments_all 返回键=请求 symbols 全集（缺省空列表）不因缓存破坏

## 验收

1. 部署后 monitor `[weight]` 线 **w1m 水位显著下降**（分钟头读数中位 ~150 → 预期明显回落；
   注意 calls/min 计数不变——计的是逻辑调用，判效以 w1m 为准）
2. 整点选币分钟 w1m 从 ~1506 回落到 ~1230（monitor 基线份额 −280）
3. 429/CircuitOpenError 降级窗频率下降（对比 ~2窗/15min 基线）
4. verify-ledger 持续 clean；资金费记账无漏（晚 ≤5min 可接受）
