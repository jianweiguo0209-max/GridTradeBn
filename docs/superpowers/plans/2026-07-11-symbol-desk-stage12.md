# SymbolDesk 阶段 1+2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 spec `2026-07-11-symbol-desk-capn-design.md` 实现阶段 1(组件一 残余比例分摊+EPS、组件三 verify-ledger 审计)与阶段 2(组件二 关格集合净额化+monitor 两阶段),cap=N 操作层收编。

**Architecture:** 全部逻辑收在 PositionLedger(记账/协调)+ cycles(两阶段编排);合成成交规范升级为共享 event_id 配对可审计;关格唯一入口 `close_set`(单格退化 ≡ 现行为)。

**用户已决:** lev_caps 认可(阶段 3 另批)、滑点执行格承担、本次做阶段 1+2。

## Global Constraints

- v1 语义不破:合成行 `ledger:` 前缀 / line_index=-1 / max_ts 游标排除 / restore 重放 / funding 签名权重 / close_share 幂等(claim 真相源=live 账本)。
- N=1 与 N=2 行为逐位不变;现有测试(基线 725+)零改动通过(阶段 2 仅允许 monitor_grid 调用点适配,断言语义不变)。
- 每笔真实成交只归一格(DB trade_id 主键约束);合成转仓只在格间纯记账。
- 实现批准≠部署批准;部署 GH Actions;不确定就问。

---

## 阶段 1

### Task 1: EPS 统一 + close_share 残余比例分摊(组件一)

**Files:** Modify `gridtrade/execution/position_ledger.py`; Test `tests/execution/test_close_share_split.py`(新), 现有 `test_close_share.py` 零改动

**Interfaces (Produces):** `LEDGER_EPS(ex) = max(ex.min_amount, 1e-9)`(模块函数);`close_share(grid_id, symbol, exclude=frozenset())` 新增 exclude(阶段 2 用,默认空=现行为);残余分摊内部函数 `_split_residual(remaining, survivors) -> [(gid, qty)]`。

- [ ] **失败测试**(核心断言,完整代码在测试文件):

```python
def test_three_survivors_two_opposite_proportional(store):
    # 关格 +12;幸存 B=-9, C=-3, D=+5 → 反号集 {B,C} 按 |claim| 比例 9:3
    ...制造 claims,交易所净 = +12-9-3+5=+5,close A...
    # 交易所 reduce 5(同号部分),残余 7 → B 得 7*9/12=5.25, C 得 1.75, D 得 0
    assert abs(book(B) + 9 - 5.25) < 1e-9 ...
def test_all_same_sign_survivors_split_by_claim(store): ...   # 无反号 → 全体按 |claim|
def test_all_zero_survivors_equal_split(store): ...           # 全零 → 均分
def test_dust_claim_no_transfer(store):
    # min_amount=0, 关格 claim=1.4e-14 → EPS 挡住,零合成行(浮点尘埃一行修)
def test_split_conservation_exact(store):
    # Σ分摊 == remaining 逐位(最后一个接收方吃余数)
```

- [ ] **实现**:

```python
def LEDGER_EPS(ex):
    """零判定锚:prod 未配 min_amount(=0)时防浮点尘埃写 1e-14 合成行(2026-07-11 巡查已批)。"""
    return max(ex.min_amount, 1e-9)

def _split_residual(remaining, survivors):
    """survivors: [(gid, claim)]。反号优先按|claim|比例;无反号→全体按|claim|;全零→均分。
    守恒:各份按比例取整到浮点后,最后一名吃余数(Σ==remaining 逐位)。"""
    opp = [(g, c) for g, c in survivors if c * remaining < 0]
    pool = opp or survivors
    w = [abs(c) for _, c in pool]
    tw = sum(w)
    if tw <= 0:
        w = [1.0] * len(pool); tw = float(len(pool))
    out, acc = [], 0.0
    for i, (g, _) in enumerate(pool):
        q = remaining - acc if i == len(pool) - 1 else remaining * (w[i] / tw)
        out.append((g, q)); acc += q
    return out
```

close_share 尾段:`sibs`(经 exclude 过滤)→ `_split_residual` → 逐份 `settle_transfer(..., 'closeshare')`;`ex.min_amount` 判零四处换 `LEDGER_EPS(ex)`(funding_weight 的 1e-12 同步统一);WARN 行删除,换 info:`[ledger] closeshare split %s residual=%+.8g -> {gid: qty}`。无幸存格路径不动(留差给漂移告警)。

- [ ] `pytest tests/execution/test_close_share_split.py tests/execution/test_close_share.py tests/execution/test_position_ledger.py -q` 全 PASS(旧文件零改动)
- [ ] Commit `feat(desk): close_share 残余比例分摊+EPS 统一(组件一,N=2 退化等价)`

### Task 2: 合成行 event_id 配对(组件三前置)

**Files:** Modify `position_ledger.py`(`_record_synthetic`/`settle_transfer`); Test `tests/execution/test_ledger_eventid.py`(新)

**Produces:** 新 trade_id 格式 `ledger:<event>:<gid>:<eid>`,`eid = '%d-%d' % (ts, seq)` **由 settle_transfer 生成一次、两行共享**;`ledger:reduce` 单边行沿用独享 eid。旧格式行(历史库)只读兼容。

- [ ] 失败测试:settle_transfer 两行 trade_id 尾段 eid 相同、买卖相反、同价、量相等;reduce 行 eid 独享;max_ts 仍排除(前缀不变)。
- [ ] 实现:`_record_synthetic(grid_id, side, qty, px, event, eid=None)`,eid=None 时自生成;settle_transfer 先 `eid = '%d-%d' % (now_ms(), next(self._seq))` 再传两次。
- [ ] PASS + 全 ledger 相关测试回归;Commit `feat(desk): 合成转仓行共享 event_id(配对守恒可审计)`

### Task 3: dbadmin verify-ledger(组件三)

**Files:** Modify `gridtrade/runtime/dbadmin.py`(新函数+CLI 分支); Test `tests/runtime/test_verify_ledger.py`(新)

**Produces:** `verify_ledger(store, adapter=None, log=print) -> dict(pairs_bad, reduce_orphan, replay_bad, symbol_drift, scanned)`;CLI `python -m gridtrade.runtime.dbadmin verify-ledger`(离线三查)/ 机上加 adapter 时四查。

- [ ] 失败测试:①正常库(造对冲关格数据)→ 全零静默;②手工插单边合成行 → pairs_bad=1;③插量不守恒对 → pairs_bad=1;④篡改 accounting.net_position → replay_bad=1。
- [ ] 实现要点:新格式行按 eid 分组断言(恰 2 行/带符号和<EPS/同价);`ledger:reduce` 跳过配对查;旧格式(4 段 trade_id)整体跳过配对(记 legacy 计数);重放查=每活跃格 Σ(signed fills) vs accounting.net_position(容差 max(EPS, lot));adapter 给出时加 per-symbol Σclaims vs fetch_positions。
- [ ] PASS;Commit `feat(desk): dbadmin verify-ledger 离线守恒审计(组件三)`

## 阶段 2

### Task 4: PositionLedger.close_set(组件二核心)

**Files:** Modify `position_ledger.py`(close_set + per-symbol 锁)、`grid_executor.py`(`close()` 委托 close_set,finalize_close 保留供 CLOSING 续平); Test `tests/execution/test_close_set.py`(新)

**Produces:** `close_set(grid_ids, symbol, reason) -> [{'grid_id','reason','pnl_ratio'}]`;`ex.close(gid, symbol, reason)` ≡ `ledger.close_set([gid], symbol, reason)[0]`(单格退化)。

内部序(spec 组件二):

```python
def close_set(self, grid_ids, symbol, reason):
    with self._symbol_lock(symbol):                    # 进程内 per-symbol mutex(defaultdict Lock+守护锁)
        ex = self.ex
        gset = [ex.grids.get(g) for g in grid_ids]
        for g in gset:                                  # ①真因落库+CAS 转 CLOSING(逐格,复用 ex.close 头部语义)
            ex.grids.set_close_reason(g.id, reason)
            if g.status != CLOSING:
                ex.grids.transition_status(g.id, CLOSING, expected_version=g.version)
        for g in gset:                                  # ②撤线单+撤丝(复用 finalize_close 现有段,提为 _cancel_grid_orders)
            self._cancel_grid_orders(g.id, symbol, has_siblings=按币上是否还有集合外活跃格或集合>1)
        exclude = frozenset(grid_ids)
        if len(gset) > 1:                               # ③内部预净额:选执行格,其余 claim 全额转给它
            net = ex.adapter.fetch_positions(symbol).net_size
            claims = [(g.id, self.claim(g.id)) for g in gset]
            same = [x for x in claims if x[1] * net > 0]
            exec_gid = max(same or claims, key=lambda x: abs(x[1]))[0]
            px = float(ex.adapter.fetch_price(symbol))
            for gid, c in claims:
                if gid != exec_gid and abs(c) > LEDGER_EPS(ex):
                    self.settle_transfer(gid, exec_gid, symbol, c, px, 'closeset')
        else:
            exec_gid = gset[0].id
        self.close_share(exec_gid, symbol, exclude=exclude)   # ④执行格 reduce+残余分摊(exclude 集合成员)
        out = []
        for g in gset:                                   # ⑤逐格 records+CLOSED(复用 finalize_close 尾段,提为 _finalize_record)
            out.append(self._finalize_record(g.id, symbol, reason))
        return out
```

重构注记:finalize_close 拆三段可复用函数(`_cancel_grid_orders` / 仓位段(close_share 已是) / `_finalize_record`),finalize_close 本体保留原签名供 monitor CLOSING 续平(行为不变,内部改调三段)。

- [ ] 失败测试:
  - 对冲对同关(PUMP 案型 ±X):**交易所零下单**(FakeExchange create_market_order 计数=0)、两格 records 按 mark 实现、Σ 不变量保持、双格 CLOSED;
  - 4 格混合(+5,−3,+2,−1,交易所净+3):恰 1 张 reduce 单、量=3、执行格=+5;
  - 单格退化:close_set([g]) 与旧 ex.close 全套断言逐位一致(复用 test_close_sibling 场景跑双路径对比);
  - 幂等:close_set 重入无新合成行/无新市价单;
  - exclude 语义:残余分摊只给集合外幸存格。
- [ ] PASS + `test_close_sibling.py`/`test_close_share.py` 零改动回归;Commit `feat(desk): close_set 关格集合净额化+币级互斥(单格退化等价)`

### Task 5: monitor 两阶段 + 全入口收编(组件二编排)

**Files:** Modify `gridtrade/execution/monitor.py`(defer_close 参数)、`gridtrade/runtime/cycles.py`(阶段 A/B)、`gridtrade/execution/manager.py`(close_by_tag 按币分组)、`gridtrade/execution/reconciler.py`(fuse fired → close_set); Test `tests/execution/test_two_phase_monitor.py`(新)

- [ ] monitor_grid 加 `defer_close=False`:True 且触发时返回 `{'closed': False, 'close_intent': reason, ...}` 不执行(默认 False 兼容全部现有直调测试);
- [ ] `_grid_unit` 传 defer_close=True;有 intent 的单元跳过 reconcile(与现 closed 跳过同位);
- [ ] `run_monitor_cycle` 阶段 B:收集 intents → 按 symbol 分组 → 组间用现有线程池并行、组内一次 `manager.close_set(gids, symbol, reason)`(manager 薄封装:调 ledger.close_set + 逐格 `_publish(GridClosed)` + signals.evict,事件仍主线程之外不发——沿用现有"事件收在主线程"约束:close_set 结果回主线程再发布);
- [ ] `manager.close_by_tag`:按 symbol 分组改调 close_set(组≥1 格);`reconcile_fuses` fired 分支:`ingest_fuse_fills` 后改 `ex.close`→不变(ex.close 已委托 close_set 单格);
- [ ] 失败测试:阶段 A 不执行(create_market_order 计数 0 直到阶段 B);同币两 intent 合并为一次 close_set;跨币并行完成且无互锁(两币各一 intent,线程池=2);PV 币级信号下同币 4 格 → 恰 1 张市价单;轮换同 tag 双币 → 两次 close_set。
- [ ] 全量回归(基线 725+,新增 ~25);金标差分:单格路径行为逐位不变;Commit `feat(desk): monitor 两阶段+关格全入口收编 close_set`

### Task 6: 收尾

- [ ] `pytest -q` 全绿;docs/STATUS.md 记档一行;memory 更新(same-symbol-sibling-conflicts 追加 SymbolDesk 阶段 1+2 已实现待部署)。
- [ ] Commit `docs(status): SymbolDesk 阶段1+2 落地记档`
- [ ] **部署另批**:testnet 验收要点=PUMP 对冲对同关零交易所单(`[ledger] closeshare split`/`closeset` 行)、verify-ledger 干净、monitor 轮健康;→ 用户批 → mainnet。

## Self-Review 备注

- 事件发布线程约束:close_set 在 worker 线程执行(阶段 B 跨币并行),GridClosed 发布延后到主线程汇总——与现有 `_grid_unit 不发事件` 约束同构;
- close_set 内 fetch_positions/fetch_price 走 ResilientAdapter(写锁串行天然保证 HL nonce);
- CLOSING 续平(monitor resume)不经 close_set:单格 finalize_close 原语义,组件一分摊自动生效;
- 风险点:同币 intent 与轮换关格同轮竞争——close_set 的 CLOSING CAS 使后到者跳过(ConcurrencyError→该格已被处理),测试覆盖。
