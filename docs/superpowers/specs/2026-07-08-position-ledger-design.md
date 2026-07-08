# PositionLedger:同币多格内部净额化 设计

> 状态:已获用户批准方向(方案 A,一步到位交付)。实现前如遇本文未覆盖的分歧点,**不确定就问,勿猜**。

## 背景与问题

HL 每币只有一个账户级净仓,而系统每格一本账(LiveEquity,成交流水推导)。成交归因(exchange order id → 格)干净,冲突全部在"按仓位操作"的路径。同币双格(tier2 cap=2,2026-07-06 启用)下的四类冲突:

1. **平仓相残残留**:v23(40ab3b1)兄弟格感知修了主案(只平自己份额、clamp 到净仓同号部分),但两格份额互相对冲时正确不动手,幸存格从此模型≠账户带差运行(mainnet 已挂旗另案);
2. **保险丝互噬**:每格挂 worst-case 满量 reduce-only 丝;触发按共享净仓 clamp,可能吃掉兄弟份额;且丝成交不入触发格账本(fuse oid 不在 grid_orders,sync 排除)——已知缺口"丝成交不入 record pnl";
3. **funding 双计**(本次盘点新发现,`grid_executor.py sync` 资金费段):两格各按 symbol+cursor 摄入**同一批** funding 支付、各记 100%,交易所只按净仓收一次;
4. **对账变糊**:`check_position_drift` 只能验"模型之和 vs 交易所净仓",双格漂移互相掩护,且已知残留差持续污染告警。

## 核心不变量

**Σ claims(本币全部活跃格) = 交易所净仓**(容差内)。

- `claim(格)` ≡ 该格模型净仓(成交流水 Σ(order_dir×order_num) 推导;运行时以 `ex.live[gid]` 账本为准——accounting.net_position 是上次 sync 的快照,关格续平场景可能过期,**不作 claim 真相源**);
- 任何会破坏不变量的操作,通过**内部转仓**结算:一对合成成交(synthetic fill),按 mark 价、零手续费,写入 `grid_fills`。LiveEquity 数学天然消化(转出格按市价实现盈亏、转入格按市价建仓);`Reconciler.restore` 重放 fills 时自动恢复——**claims 持久化零新表、零 schema 变更**。

## 组件

新文件 `gridtrade/execution/position_ledger.py`,无状态(全部从 stores/live 派生):

```python
class PositionLedger:
    def __init__(self, executor): ...
    def claims(self, symbol, exchange) -> dict[str, float]
        # {grid_id: claim} 本币全部活跃格;已 restore 的取 live 账本,未加载的回退 accounting
    def settle_transfer(self, from_gid, to_gid, symbol, qty, mark_px, event) -> None
        # 写一对合成成交(见「合成成交规范」),并对已加载的 live 账本同步 record_fill
    def close_share(self, grid_id, symbol) -> None
        # 关格净额化:①clamp reduce(v23 逻辑收编)②残余份额转仓给幸存格
    def funding_weight(self, grid_id, symbol) -> float
        # 签名权重 w_g = claim_g / Σclaims;|Σclaims| < min_amount 时均分(1/n)
    def ingest_fuse_fills(self, grid_id, symbol, fuse_oid) -> int
        # 丝成交按 fuse oid 从 fetch_my_trades 摄入触发格账本(真实 fee,line_index=-1)
```

`GridExecutor.__init__` 构造 ledger 并持有;`Reconciler` 经 `self.ex.ledger` 使用。

## 合成成交规范

| 字段 | 值 |
|---|---|
| trade_id | `ledger:<event>:<grid_id>:<ts_ms>`(每行唯一;转仓一对=两行,两个 grid_id) |
| line_index | `-1`(哨兵,不属于任何网格线) |
| side | 转出格:claim>0 → `sell`,claim<0 → `buy`;转入格取反 |
| price | mark 价(`adapter.fetch_price` 或快照价) |
| fee | `0.0` |
| ts | `now_ms()` |

**游标陷阱(E4 同族,必须处理)**:`FillRepository.max_ts` 是 `fetch_my_trades` 的 since 游标源(sync 与 restore 两处),合成成交 ts=now 会把游标推过尚未摄入的真实成交 → **`max_ts` 的 SQL 排除 `trade_id LIKE 'ledger:%'` 行**(单点修复,两个调用方自然受益)。`list_by_grid`(restore 重放)**不**排除——重放正是恢复 claims 的机制。

**幂等性**:close_share 残余从 live 账本实时计算;转仓写入后 live 账本立即 record_fill → 重入(CLOSING 续平)时残余≈0,不会二次转仓。崩溃恢复:fills 已落库 → restore 重放 → 账本一致;fills 未落库 → 重做。

## 四条路径改造

### a) 关格 `finalize_close`(grid_executor.py)

有兄弟分支的"只平自己份额"循环收编进 `ledger.close_share`,并新增残余转仓:

1. remaining = 关格 live 账本净仓;
2. 与 v23 相同:`while |remaining| > min_amount and 交易所净仓×remaining > 0`,clamp reduce-only 市价平(≤3 次);
3. **新增**:仍有 |remaining| > min_amount(被兄弟对冲的部分)→ `settle_transfer(关格, 幸存格, remaining, mark)`——关格 claim 归零,幸存格账本收到镜像合成成交,模型与交易所重新对齐,**冲突①根治**;
4. cap=2 ⇒ 至多一个兄弟;若将来 >1 个幸存格,按各自与 remaining 反号的 claim 比例分摊(实现里 assert+log 提示该分支未经生产验证);
5. 无兄弟分支(symbol 级扫除)行为不变。

关格记录语义不变:snapshot 本就按 mark 实现浮盈亏,转仓只是把这层语义落成双方账本的真实流水。

### b) 保险丝 `reconcile_fuses`(reconciler.py)

worst-case 尺寸(`grid_count×order_num`)**不变**——安全属性优先(丝必须在 sync 滞后时也足以撑网全平)。触发分支(`fired`)改为:

1. `ledger.ingest_fuse_fills(grid_id, symbol, fired_oid)`:按 fuse oid 匹配 trades,以**真实 trade_id/价格/fee** 落 grid_fills(line_index=-1)并 record_fill 进触发格账本——**顺带根治已知缺口"丝成交不入 record pnl"**(snapshot-fuse-blind-window 余项);
2. 照旧 `ex.close(grid_id, symbol, '保险丝触发')` → close_share 接管:reduce-only clamp 已在交易所发生且成交已摄入,不变量自动保持;残余(吃了兄弟的部分的镜像)走标准转仓——**冲突②无需独立机制**。

### c) funding 分摊 `sync`(grid_executor.py 资金费段)

```python
w = self.ledger.funding_weight(grid_id, symbol)
for p in pays:
    self.live[grid_id].add_funding(p.amount * w)
```

- 签名权重 `w_g = claim_g / Σclaims`:HL 按净仓收费、per-unit 费率均匀,该分摊经济上精确(对冲侧负权重=赚对侧 funding);双格权重和=1 ⇒ 总额=账户实收,**冲突③根治**;
- `|Σclaims| < min_amount` → 支付本身≈0,均分兜底(1/n);
- 单格 w=1,与现行为逐位一致;
- 游标机制不动(两格各自摄入同批行、各乘权重;两次 sync 间 claims 微动导致的权重和微偏差可接受,远小于当前 100%+100%)。

### d) 对账 `check_position_drift`(reconciler.py)

symbol 级求和检查保留不动。a/b 消除结构性差源后,超容差告警从此指向真实事故(漏摄入),不再被已知残留污染。**代码无改动,验收标准改变**(见测试)。

## 展示

dashboard fills 相关视图:`trade_id` 前缀 `ledger:` 的行标注「内部转仓」(fee=0、mark 价);`line_index=-1` 且非 `ledger:` 前缀的行(丝成交)标注「保险丝」。两类都不破现有渲染,Recent Fills 照常显示。

## 不变的东西

策略数学、LiveEquity 内部、records 语义、开格路径、DB schema、单格行为(全部现有测试语义原样通过)、reconcile_open_orders 受保护集合逻辑、丝尺寸与三态对账主判。

## 测试计划

FakeExchange 双格集成 + 单元:

1. 对冲关格:A +5 / B −5,关 A → 交易所零下单、A 记录按 mark 实现、B 收合成买 5、`check_position_drift` 双格皆 ok(**残留根治验证**);
2. 同号关格:与 v23 行为等价(clamp reduce,金标);
3. 丝触发吃兄弟:A 多 B 空,A 丝 fire → 丝成交入 A 账本(真实 fee)、残余转 B、全体对齐;
4. funding:双格权重和=1、双格 add_funding 总额=支付额;对冲双格权重一正一负;单格 w=1;Σclaims≈0 均分;
5. 游标:合成行不推 `max_ts`;真实成交在合成行 ts 之前仍被摄入;
6. restore:含合成行的 fills 重放后 claims 复原、`_trade_cursor` 不受合成行影响;
7. 幂等:close_share 重入(CLOSING 续平)不二次转仓;
8. 金标:单格全链路现有测试零改动通过。

## 部署

合入 main → testnet(GH Actions)观察双格开关格/funding 一轮 → 用户批准后 production。上线验证要点:mainnet 已挂旗的 NBIS 幻影差在下一次同币双格关格场景不再新增;dashboard 内部转仓行可见。
