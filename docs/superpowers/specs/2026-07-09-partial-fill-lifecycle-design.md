# 部分成交订单生命周期 彻底修复 设计

> 状态:方向已获用户批准(2026-07-09,"要彻底修复而不是只打补丁")。不确定就问,勿猜。

## 事故与根因(mainnet GRAM 2026-07-08 实证)

gt02 line15 卖单(42 档)分三笔部分成交(18+18+6 @1.6027);sync 摄入首笔时把订单行
upsert 成 `closed`,且**抹掉 exchange_order_id、size 被 t.size 覆写**→ 下一轮 by_oid
无此 oid,第三笔(6)静默丢 → 模型幻影 +6,关格时会误平兄弟格真仓。事后清账=补摄入真实
成交行+重启 monitor(已实操)。此外衍生危害:行提前 closed 使交易所残单(24→6)脱离
protected 集合,可能被 reconciler 误当孤儿撤;补单在首笔部分成交即触发全量对侧单。

## 设计(不是补丁,是生命周期修正)

**核心:订单行跟踪累计成交量 `filled`,吃满才 `closed`;行字段全程保真。**

### 1. Schema/模型

- `grid_orders` 加 `filled FLOAT NOT NULL DEFAULT 0`(dbadmin 幂等迁移
  `add_grid_orders_filled`;存量行 default 0 正确——旧代码下任何部分成交都立即
  closed,不存在"open 且已有部分摄入"的行)。
- `GridOrder.filled: float = 0.0` + orders.py `_FIELDS`。

### 2. sync 摄入重构(grid_executor.py candidates 循环)

- 累计:`new_filled = go.filled + t.size`;吃满判定
  `new_filled >= go.size − max(1e-9, go.size×1e-6)`;
- upsert 行字段**保真**:`side/price/size` 用 go 的(修 size=t.size 行损坏),
  `exchange_order_id=go.exchange_order_id`(修 oid 抹除),`filled=new_filled`,
  status 只在吃满时 'closed';
- **同轮多笔部分成交**:by_oid[t.order_id] 更新为新行,累计正确;
- 补单(replenish 对侧)**只在吃满时触发**(全量 lot 语义,与回测/legacy 一致);
  open_lines 仅在吃满时 discard——部分成交期间 (line,side) 仍占用,防重复挂单。

### 3. 量化 size 保真(开格/补单/E2 重挂)

DB 行 size 曾存我方浮点(42.187),交易所量化成 42 成交 → 吃满判定永假 → E2 会误重挂。
修:三处 create_limit_order 后,行 size 存 `order.size or 请求值`(adapter._to_order
回传 r['amount'] 即交易所量化值;FakeExchange 回显请求值,测试不变)。

### 4. E2 重挂三态升级(reconciler.reconcile_open_orders)

达宽限要重挂前,先问 `order_status(oid)` 权威(复用 reconcile_fuses 三态模式):
- `'filled'` → **不重挂**,fills 由 sync 摄入(游标 overlap 覆盖),仅清 missing 计数
  (行留给 sync 标 closed);
- `'open'` → 信息面盲区,不动、清计数;
- 其余('canceled'/'unknown')→ 今日行为:撤旧+重挂,重挂行 filled=0、size 存量化值。

覆盖遗留行(oid 已被抹/size 未量化)与"已吃满但行未闭合"的一切病态,杜绝重挂重复建仓。

### 5. finalize_close 撤单标记保真

canceled upsert 保留 `exchange_order_id` 与原 size(撤单窗口内的在途部分成交仍可匹配摄入)。

## 不变的东西

fills 摄入/去重/游标、补单查重守卫、LiveEquity、PositionLedger、单格金标行为
(全量成交时 filled==size 一次吃满,逐位等价旧路径)。

## 测试

1. 跨轮部分成交(42=18+18+6 拆 3 轮):全摄入、行 open→open→closed、补单只触发一次;
2. 同轮多笔部分成交累计;
3. 量化 size:行存回传值,吃满判定为真;
4. E2 三态:'filled' 不重挂且清计数;'open' 不动;'canceled' 重挂;
5. oid 保真:成交标记/撤单标记后行仍带 oid;
6. 迁移幂等(缺列加/有列跳);
7. 全量金标回归(全量成交行为逐位等价)。

## 部署

testnet → **两网各跑一次 `dbadmin migrate`**(fly machine run)→ mainnet;
巡查项:部分成交币(薄盘)漂移告警清零、无重复挂单。
