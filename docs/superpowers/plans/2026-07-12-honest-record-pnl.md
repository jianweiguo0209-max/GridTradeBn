# 关格记录诚实化 实现计划(阶段1+2)

> **For agentic workers:** spec = `docs/superpowers/specs/2026-07-12-honest-record-pnl-design.md`(已批准)。
> 每阶段完成直上 testnet 造数验证(用户预批);mainnet 不碰。

**Goal:** record/accounting/止损判定的 pnl 全部改为逐笔精确直算,引擎移出记账链路;补外部干预检测。

**Global Constraints:** 金标差分=干净输入下直算与引擎结果一致、现有测试语义零改动;`cal_equity_curve` 本身分毫不动;snapshot 返回字段名不变;LEDGER/close_set 语义不动。

## 阶段 1(组件一+二)

### Task 1: LiveEquity 精确回放(realized+avg 单遍)
- Modify: `gridtrade/execution/live_equity.py` — `_replay_exact()` 返回 (avg, net, realized);
  `_avg_cost` 改薄壳;新增 `pnl_exact(mark)`。
- Test: `tests/execution/test_live_equity.py` — 多头减仓/空头减仓/过零翻向/恰好平净/费用/funding。
- 语义:减仓 realize (px−avg)×qty(空头对称);过零=先 realize 旧仓再以 px 开新;减仓 avg 不变。

### Task 2: snapshot 切换直算(引擎移出)
- Modify: `live_equity.py:snapshot` — 移除 cal_equity_curve 调用;net_value=1+pnl_exact/cap;
  realized_pnl=realized_exact;其余字段同源直算。
- Verify: 全量 pytest;引擎-直算金标差分若有偏差,逐例核对(直算为准,校准 fixture 并留注)。

### Task 3: 事故形状回归用例
- Test: gt00 形状(41.8×3 @11.10/10.72/10.47 + 合成卖 125.4@10.434 → ratio≈−2.8%,必为负);
  ADA 形状(469买+60卖非均匀);ZRO 部分成交形状;转仓对守恒(两本合计 0)。

### Task 4: dbadmin verify-ledger --records
- Modify: `gridtrade/runtime/dbadmin.py` — 每个有 record 的格:DB fills 重放 pnl_exact(末笔价)
  + accounting.funding_paid,vs record.total_pnl,容差 max($0.05, 0.1%×cap);报偏差清单。
- Test: 造错 record 报警/正常静默。

### Task 5: 提交+testnet 部署+造数验证
- push main(CI 自动)→ 手动触发 testnet Deploy workflow → 造数:manual 关格+部分成交+转仓,
  验 record 与手算一致、verify-ledger --records 全绿。

## 阶段 2(组件三:外部干预检测)——开工前问用户 resolve 形态(spec 待决策②)

### Task 6: 币级外部干预熔断
- Modify: `gridtrade/runtime/cycles.py` — drift 超容差 → 币级只读(跳过补单/挂丝/开格),
  `[intervention]` 告警;resolve 形态按用户答复。
- Test: FakeExchange 手动平仓剧本。

### Task 7: 丝重挂即消守卫
- Modify: reconciler/cycles — 同格丝重挂后下轮又缺 ≥2 连续 → 停挂该格丝 + `[fuse] futile` 告警。
- Test: 连续消失剧本;正常单次重挂不受影响。

### Task 8: testnet 部署+造数验证(手动平仓复现 churn 剧本→熔断生效)

## 阶段 3(组件四:历史修数)——testnet 验证后另起,需 DB(等解禁)
