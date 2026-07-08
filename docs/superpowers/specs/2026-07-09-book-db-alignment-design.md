# 账本↔DB fills 对齐(跨进程合成成交摄入) 设计

> 状态:方案已获用户批准(2026-07-09)。不确定就问,勿猜。

## 缺口(mainnet GRAM 转仓首样本,巡查验收发现)

LiveEquity 内存账本只认两个来源:restore 重放 + 本进程 record_fill。而 `grid_fills`
已成为第三方可写的账本真相源——scheduler 关格写转仓合成行、手工修复补摄入真实成交行,
monitor 进程幸存格的内存账本看不见(合成行不来自交易所,sync 拉不到)→ acc 停旧值,
止损/补单决策基于虚净仓,直到重启 restore 才归一。手工修复也因此每次都要重启 monitor。

## 设计

**不变量:内存账本的成交集合 == DB grid_fills 的成交集合(每 sync 轮末收敛)。**

### 1. 已入账集合 `GridExecutor._book_ids: {grid_id: set[trade_id]}`

维护点(账本收到成交的所有路径):
- `Reconciler.restore`:重放后整集初始化;
- `open()`:空集初始化;
- sync 交易所摄入循环:add_if_new 成功后追加;
- `PositionLedger._record_synthetic` / `ingest_fuse_fills`:本进程账本 record_fill 后追加
  (live 未加载则不加——restore 会带上)。

### 2. sync 对齐步(交易所摄入之后、快照/记账之前)

```
db = fills.list_by_grid(gid)            # 已按 ts 升序;量级同已有 orders.list_by_grid
new = [f for f in db if f.trade_id not in _book_ids[gid]]
if new:
    if 全部 new.ts >= 账本最后成交 ts(或账本为空):
        按 ts 序逐笔 record_fill 追加(常规:转仓行 ts=now)
    else:                                # 乱序(如手工补历史 ts 成交)
        整本重建:fresh LiveEquity(同 cap/fee/entry_price)+ 全量重放 db,
        funding_paid 从旧账本拷贝(funding 与 fills 分账,重放不含)
        —— LiveEquity 平均成本是路径依赖的,乱序追加必错,重建是唯一正确解
    log('[ledger] book catch-up grid=%s rows=%d rebuild=%s')
```

对齐在快照之前 → 本轮 acc 即反映对齐后净仓。

### 3. 明确不变的

- 游标:sync 的 trade 游标继续用 `fills.max_ts`(合成行已排除;手工补的历史行 ts<max 不推游标);
- 单进程行为零变化(自己写的行都在集合里,对齐步空转);
- restore/金标语义。

## 收益

一个机制覆盖三类 DB 侧账本写入:跨进程转仓合成行、**手工修复补摄入(以后不用重启 monitor)**、
未来任何 DB 侧账本操作。

## 测试

1. 跨进程转仓:双执行器共 store,A settle_transfer → B sync 后幸存格账本/acc 归一;
2. 手工补历史 ts 行 → 触发重建,净仓/已实现与全量重放一致,funding_paid 保留;
3. 顺序追加路径:转仓行(ts=now)走 append 不重建;
4. 单进程常规 sync 零 catch-up(现有全套测试零改动通过);
5. 空账本格(新开)对齐不炸。
