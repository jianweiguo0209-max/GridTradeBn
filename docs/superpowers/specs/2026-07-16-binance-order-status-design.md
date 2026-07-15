# 覆写 BinanceAdapter.order_status(修保险丝触发漏判) 设计

> 状态:**已获用户批准(2026-07-16)**。范围:核心修复(覆写 order_status)+ 端到端测试(用户定)。
> systematic-debugging 已完成根因(KITE 压测 grid 33b02230 实证)。实现前遇本文未覆盖分歧点,不确定就问。

## 一、根因(已实证)

`BinanceAdapter` **未覆写 `order_status`** → 用基类默认返 `'unknown'`(base.py:176)。灾难保险丝三态
对账 `reconcile_fuses`(reconciler.py:244)与 E2 补单三态(reconciler.py:101)都靠 `order_status` 判
"已触发/已吃满";得 `'unknown'` 时退到脆弱的 `_fuse_filled` fills 兜底(按存的 fuse oid 匹配成交)。

**KITE 压测实证(grid 33b02230,2026-07-15)**:高位保险丝在 stop_high 0.12943 触发(reduce-only 买
18402 平空仓,交易所归 0),但账本**漏摄入这笔丝成交**(交易所 298 笔 vs 账本 297 笔,独缺 buy 18402)
→ 账本净仓卡 -18402、交易所 0 → drift 18402 > tol 9201 连续 2 轮 → **外部干预熔断**(安全网正确捕获)。
连带:丝被误判"丢失"反复重挂(churn);格未关(还挂单,可再累仓再触发)。

**关键定位**:reconciler 的"丝触发→拆网"逻辑本身正确——既有 `test_fired_fuse_tears_down_grid`
(test_reconcile_fuses.py:28)在 FakeExchange(order_status 已三态)下通过。**缺口纯在 BinanceAdapter
未覆写 order_status**。HL 时代有可用 order_status,迁币安退化为 'unknown'。

**为何非量化漂移**:tol = 1.5 × order_num(非 1.5 币),触发要整格级背离(>9200 币);实证 drift
恰 = 3 × order_num(漏了一笔整仓平仓的丝成交)。故根在"漏摄入",非"整数精度量化"。

## 二、总览

覆写 `BinanceAdapter.order_status(symbol, order_id)`:**双簿查单**(常规簿 → algo/trigger 簿,与既有
`cancel_order` 双簿回退同模式)+ 状态映射。**一处修好三个消费者**:①丝三态(丝触发→'filled'→
`ingest_fuse_fills`+关格,不再误重挂);②E2 补单三态(reconciler:101);③churn(丝仍在挂→'open'→不重挂)。

**不变量(不改)**:reconciler 三态逻辑(已正确)、`ingest_fuse_fills`、`ResilientAdapter.order_status`
转发(:166 已在)、base 默认 'unknown'、其余适配器。**回测无关**:纯实盘 API,FakeExchange.order_status
已三态、行为不变。

**非目标**:已背离格的恢复/重摄入(暂缓,人工);丝/带宽 sizing;E2 补单逻辑本身(只恢复其 order_status 输入)。

## 三、组件

### 3.1 双簿行为(demo 只读实测确认 2026-07-15)

| 单类型 | `fetch_order(oid, native)` | `fetch_order(oid, native, {'trigger':True})` |
|---|---|---|
| 限价单(常规簿) | ✓ status=open type=limit | ✗ OrderNotFound(-2013) |
| 丝止损单(algo/trigger 簿) | ✗ OrderNotFound(-2013) | ✓ status=open type=market |

结论:必须**两簿都试**——常规 → `OrderNotFound` → trigger。与 `cancel_order`(binance.py)同款。

### 3.2 `BinanceAdapter.order_status` 覆写(binance.py,置于 `cancel_order` 附近)

```python
def order_status(self, symbol, order_id) -> str:
    """权威单状态('open'/'filled'/'canceled'/'unknown')。双簿查单:常规簿→algo/trigger 簿
    (同 cancel_order;保险丝是 STOP_MARKET 在 algo 簿,demo 实测常规簿查不到)。两簿皆无
    (古老/purged,fetch_order 对已成交/已撤在保留期内仍可查故极罕见)→ 'unknown',保留调用方
    _fuse_filled fills 兜底安全网(spec 2026-07-16)。"""
    native = self.to_native(symbol)
    for params in ({}, {'trigger': True}):
        try:
            o = self.client.fetch_order(order_id, native, params)
            return self._map_order_status(o.get('status'))
        except ccxt.OrderNotFound:
            continue
    return 'unknown'
```

### 3.3 状态映射 `_map_order_status`

ccxt 归一化 order['status']:`'open'`(binance NEW / PARTIALLY_FILLED)、`'closed'`(FILLED)、
`'canceled'`(CANCELED)。注(ccxt 4.5.61 实测):EXPIRED/REJECTED 被归一化为 `'expired'`/`'rejected'`
(非 `'canceled'`),经下方 fall-through 落到 `'unknown'`——功能等价且更稳:无成交的 canceled 与
unknown 在两消费者都走重挂,而 unknown 还先跑 `_fuse_filled` 兜底(能救到期但已部分成交的丝)。
映射到调用方三态词表:

```python
@staticmethod
def _map_order_status(ccxt_status) -> str:
    if ccxt_status == 'closed':
        return 'filled'
    if ccxt_status == 'open':
        return 'open'          # 含 PARTIALLY_FILLED(真所仍在挂;与 reconciler 语义一致)
    if ccxt_status == 'canceled':
        return 'canceled'
    return 'unknown'           # 未知/None → 保守 unknown(fills 兜底)
```

**PARTIALLY_FILLED→'open'**:与 FakeExchange.order_status 终审语义一致(fake.py:193「在簿判定先于
成交」——残单既在簿又有成交,真所是 open)。避免把在挂的残额丝/单误判 filled。

### 3.4 both-not-found → 'unknown'(而非 'canceled')

两簿皆 OrderNotFound → `'unknown'`,不返 'canceled'。理由:保守——保留调用方对 'unknown' 的既有
`_fuse_filled`/sync 兜底路径(古老单极罕见,不值为它改变 canceled 语义、冒误重挂/误撤风险)。

## 四、影响面(一处修、三处受益,须验无回归)

- **丝三态** `reconcile_fuses`(reconciler.py:244):丝触发→order_status 'filled'→`ingest_fuse_fills`+
  `ex.close`(拆网),不再误判丢失重挂。**根治本 bug。**
- **E2 补单三态**(reconciler.py:101):缺失限价单→order_status 'filled'(已吃满,残量量化 0→闭合腾线)
  /'open'(仍在挂,不动)/'canceled'(撤旧重挂)。现也退化 'unknown',一并恢复。
- **churn**:丝/单仍在挂→'open'→不重挂,消除反复重挂堆孤儿。
- `ResilientAdapter.order_status`(:166)已 `_call` 转发,无需改。

## 五、测试(TDD)

- **`order_status` 单测**(mock ccxt client,`FakeBinanceClient` 补 `fetch_order`):
  - 常规单:`fetch_order(oid, native, {})` 返 status → 映射;`{'trigger':True}` 分支不被调(常规命中即返)。
  - algo/丝单:`fetch_order(oid, native, {})` 抛 `ccxt.OrderNotFound` → 重试 `{'trigger':True}` 命中 → 映射。
  - 状态映射:'closed'→'filled'、'open'→'open'(含 PARTIALLY_FILLED)、'canceled'→'canceled'、None/未知→'unknown'。
  - both-not-found:两簿皆 `OrderNotFound` → 'unknown'。
- **端到端丝触发对账测试**(FakeExchange,扩展/紧邻 test_reconcile_fuses.py):丝触发(离簿+成交)→
  `reconcile_fuses` 得 'filled'→`ingest_fuse_fills`+关格 → **断言账本净仓归 0 且 `check_position_drift`
  返 ok(drift ≤ tol,不熔断)**——顺带验证 `ingest_fuse_fills` 真把丝成交入了账(闭合本 bug 的账本背离)。
- **回归**:既有 `tests/execution/test_reconcile_fuses.py`(丝三态)+ E2 补单测试 + `tests/exchanges/`
  全绿;`ResilientAdapter` order_status 转发既有测试不破。
- 全量 pytest 全绿;**不部署**(部署由主运维会话按避开整点 HH:00–HH:12 手动做)。

## 六、风险/开放项

- `order_status` 每次调多一次(或两次)`fetch_order` API——但 reconciler 仅在单从簿上**缺失**时才调
  (健康网格几乎不调),频率低,权重可忽略。
- 已在运行的 testnet 若有其他格已静默背离(本次修复前),本 forward-fix 不自动恢复它们(非目标);
  部署后可选巡查 drift 日志。
- **部署后 testnet 核对**:构造/等待一次丝触发,确认日志 `fuse fired -> grid closed`(而非 re-placed)、
  该格账本净仓与交易所一致、无 `Σclaims≠交易所净仓` 熔断。
