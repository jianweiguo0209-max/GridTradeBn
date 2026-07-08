# PositionLedger 同币多格内部净额化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 spec `docs/superpowers/specs/2026-07-08-position-ledger-design.md` 实现每币 PositionLedger:关格残余转仓、丝成交摄入、funding 签名权重分摊、游标防护,根治同币双格四类冲突。

**Architecture:** 新增无状态 `PositionLedger`(从 stores/live 派生),核心不变量 Σclaims=交易所净仓;破坏不变量的操作以合成成交(`ledger:` 前缀 trade_id,写 grid_fills)结算,restore 重放天然恢复。executor/reconciler 三条路径接线。

**Tech Stack:** 现有栈(SQLAlchemy stores、LiveEquity、FakeExchange 测试)。零新表、零新 env。

## Global Constraints

- 单格行为逐位不变(现有测试语义原样通过)。
- `FillRepository.max_ts` 排除 `ledger:%`;`list_by_grid` 不排除。
- 合成成交:trade_id `ledger:<event>:<grid_id>:<ts>[:<n>]`、line_index=-1、fee=0、mark 价;丝成交摄入用**真实** trade_id/fee、line_index=-1。
- claim 真相源 = live 账本(fills 推导),不是 accounting.net_position。
- 不确定就问,勿猜。

---

### Task 1: 游标防护 + LiveEquity.net_position

**Files:** Modify `gridtrade/state/fills.py:38-42`, `gridtrade/execution/live_equity.py`; Test `tests/state/test_fills_ledger_cursor.py`(新), `tests/execution/test_live_equity.py`(追加)

**Produces:** `FillRepository.max_ts` 忽略合成行;`LiveEquity.net_position` property(=Σ order_dir×order_num,与 snapshot['net_position'] 同源同值)。

- [ ] 失败测试:插入真实 fill(ts=100)+`ledger:x:g:200` 合成 fill(ts=200)→ `max_ts(gid)==100`;`list_by_grid` 返回 2 行。`net_position`:buy 5 → +5;再 sell 2 → +3;与 `snapshot(px)['net_position']` 相等。
- [ ] 实现:`max_ts` 加 `.where(~grid_fills.c.trade_id.like('ledger:%'))`;LiveEquity 加

```python
@property
def net_position(self):
    """当前净仓 = Σ(order_dir×order_num),与引擎 hold_num(累计带符号量)同源。"""
    return float(sum(f['order_dir'] * f['order_num'] for f in self._fills))
```

- [ ] `pytest tests/state/test_fills_ledger_cursor.py tests/execution/test_live_equity.py -q` PASS;commit `feat(ledger): max_ts 排除合成行 + LiveEquity.net_position`

### Task 2: PositionLedger 核心(claims/权重/转仓/丝摄入)

**Files:** Create `gridtrade/execution/position_ledger.py`; Modify `gridtrade/execution/grid_executor.py`(__init__ 尾部 `self.ledger = PositionLedger(self)`); Test `tests/execution/test_position_ledger.py`(新)

**Produces:** `PositionLedger(executor)`:`claim(gid)`、`claims(symbol, exchange)`、`funding_weight(gid, symbol)`、`settle_transfer(from_gid, to_gid, symbol, qty, mark_px, event)`、`ingest_fuse_fills(gid, symbol, fuse_oid)`、内部 `_record_synthetic(gid, side, qty, px, event, seq=0)`。

- [ ] 失败测试:双格 claims 表;funding_weight 单格=1 / 双格同号权重和=1 / 对冲(+5,−3)→(2.5,−1.5) / Σ≈0 均分;settle_transfer 写两行 `ledger:` fill 且双方 live 账本 net_position 各自 −qty/+qty;ingest_fuse_fills 按 oid 摄入真实 fee 行且 add_if_new 去重。
- [ ] 实现(核心逻辑;claim 优先 live、回退 accounting;`_active` 用 `grids.list_active()` 同 finalize_close 口径):

```python
def funding_weight(self, grid_id, symbol):
    g = self.ex.grids.get(grid_id)
    cl = self.claims(symbol, g.exchange)
    if grid_id not in cl:
        return 1.0
    total = sum(cl.values())
    if abs(total) < max(self.ex.min_amount, 1e-12):
        return 1.0 / len(cl)
    return cl[grid_id] / total
```

- [ ] PASS;commit `feat(ledger): PositionLedger 核心(claims/签名权重/合成转仓/丝摄入)`

### Task 3: funding 分摊接线

**Files:** Modify `gridtrade/execution/grid_executor.py:206-212`; Test `tests/execution/test_funding_split.py`(新)

- [ ] 失败测试(FakeExchange 双格同币):一笔 funding 支付 → 两格 `funding_paid` 之和 == 支付额(现状=2×);单格不变。
- [ ] 实现:

```python
w = self.ledger.funding_weight(grid_id, symbol)
for p in pays:
    self.live[grid_id].add_funding(p.amount * w)
```

- [ ] PASS;commit `fix(ledger): funding 按签名权重分摊,根治同币双格双计`

### Task 4: close_share + finalize_close 收编

**Files:** Modify `gridtrade/execution/position_ledger.py`(加 close_share)、`gridtrade/execution/grid_executor.py:276-301`(siblings 分支换 `self.ledger.close_share(grid_id, symbol)`;无兄弟分支不动); Test `tests/execution/test_close_share.py`(新)

close_share 逻辑:①remaining=claim(live);②v23 clamp reduce 循环(≤3 次),**每次 reduce 同步写 `ledger:reduce` 合成行入本格账本**(续平幂等的关键:崩溃后 restore 重放即知已平多少);③残余>min_amount 且有幸存格 → `settle_transfer(..., 'closeshare')` 转给反号 claim 幸存格(cap=2 至多 1 个;>1 个打 WARN 取首个反号);无幸存格 → 留差给漂移告警。

- [ ] 失败测试:对冲关格(A+5/B−5,交易所净 0)→ 零市价单、B 收合成买 5、双格 drift ok;同号关格(A+5/B+3,净+8)→ reduce 5 与 v23 等价;幂等(close_share 跑两遍不二次转仓);reduce 部分后残余转仓组合案例。
- [ ] PASS + 现有 finalize_close 兄弟测试全绿;commit `feat(ledger): close_share 关格净额化,残余转仓根治对冲残留`

### Task 5: 丝触发接线

**Files:** Modify `gridtrade/execution/reconciler.py:192-196`(fired 分支先 `ex.ledger.ingest_fuse_fills(grid_id, symbol, oid)` 再 close); Test `tests/execution/test_fuse_ledger.py`(新)

- [ ] 失败测试:双格对冲,A 丝 fire(reduce-only clamp 成交)→ 丝成交(真实 fee)入 A 账本、A close 后残余转 B、全体 Σclaims==净仓;丝成交计入 A 的 record pnl(根治已知缺口)。
- [ ] PASS;commit `feat(ledger): 丝成交摄入触发格账本+残余经 close_share 结算`

### Task 6: restore/游标集成 + dashboard 标注

**Files:** Test `tests/execution/test_ledger_restore.py`(新); Modify `gridtrade/dashboard/queries.py`(RecentFill 加 `kind` 字段:trade_id `ledger:` 前缀→'内部转仓',line_index==-1 其余→'保险丝',否则 '')、对应模板 fills 表 line 列显示 kind 替代 -1; Test dashboard 现有测试追加断言

- [ ] 失败测试:含合成行 restore → live.net_position 复原、`_trade_cursor` 不含合成 ts;dashboard kind 三态。
- [ ] PASS;commit `feat(ledger): restore 集成验证 + dashboard 内部转仓/保险丝标注`

### Task 7: 全量回归

- [ ] `pytest -q` 全绿(基线 684 passed, 2 skipped;新增≈25);金标单格零改动。
- [ ] 更新 docs/STATUS.md 一行;commit `docs(status): PositionLedger 落地记档`

部署(用户已批 testnet):GH Actions deploy workflow → testnet;同币双格若不存在则手动开两格同币验收(对冲关格/funding 一轮)。
