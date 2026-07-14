# 关格记录诚实化(pnl 直算收编)+ 外部干预检测 设计

> **2026-07-14 追加(testnet 实证)**:ledger:reduce 合成行原恒 fee=0 且用 mark 价——真实市价
> 减仓单的 taker 费(SKYAI 实证 $0.198,5bps)与成交价差双双丢失,record/fee_paid/面板费用列
> 系统性高估盈亏;snapshot 注释"退出时由 executor 落真实费"当时未实现。已修:两处 reduce 站点
> (grid_executor._flatten_symbol / position_ledger.close_share)经 _reduce_fill_px_fee 按
> order_id 回捞 userTrades,合成行携真实 vwap+真实费;回捞失败回退 mark+0 费(fail-open)。
> 转仓双边行(closeshare/closeset,内部净额化无真实成交)保持 0 费不变。

> 状态:**已获用户批准(2026-07-12)**;待决策①按推荐=引擎彻底移出 snapshot;②(resolve 形态)实现 plan 评审时问用户;③按本文组件四口径(v39 后自动修,更早只报告)。实现前如遇本文未覆盖的分歧点,**不确定就问,勿猜**。
> 事故:2026-07-11 mainnet manual 关格 VVV gt00/gt02 记录 +$15/+$11,交易所真实 −$51.7/−$52.5。

## 一、根因(代码考古 + 交易所数据反证,2026-07-12)

**记录 pnl 的取数链**:`close_set → _finalize_record → live.snapshot(fetch_price)` →
`snapshot` 把整本成交流水喂 **`cal_equity_curve`(回测网格引擎)** 重放取 `net_value`
(live_equity.py:101)。引擎是**均匀 lot/网格线价语义**的模拟器;实盘账本里存在它假设之外的输入:

- **非均匀部分成交**(v30 partial-fill 后一线单可拆多笔,ZRO 三笔同毫秒实证);
- **合成行**:`ledger:reduce`(市价)/`ledger:closeshare|closeset` 转仓(mark 价)——不在任何网格线上;
- 过零翻向、重建后的顺序差(book catch-up 乱序整本重建)。

在这些输入上引擎 `net_value` 不可靠。`avg_price` 字段在 2026-07-08 ADA 事故后已改精确直算
(`_avg_cost`,逐笔回放:同向加权/减仓不变/过零重置),**但 pnl_ratio 仍走引擎**——本次是同族缺陷
第三次发作(前两次:①neutral hold_num 往返不减仓 2026-07-02;②ADA engine avg=0 幻影浮盈 +13.5%
2026-07-08),每次都修了触雷的表象,发动机没换。

**反证闭环(为何确定是重放失真而非价格错)**:
- 记录 +$15 = (P − B)×125.4 ⇒ 若 P=真实成交价 10.434,则账本基准 B≈10.31 —— **低于 VVV 当日
  最低价 10.427,任何真实成交组合(同向加权均值)都不可能产出**;
- 若 B=真实基准(≈10.74-10.85),则 P≈10.88 —— 但 `fetch_price` 走 allMids 实时无缓存
  (hyperliquid.py:211),且全 builder dex 无同名 VVV(同名覆写排除),连续两次取价都错不成立;
- 同一 close_set 代码,同币十分钟后 scheduler 轮换关 gt011 记录诚实(−3.68% vs 交易所 −51.6)——
  失真依赖流水形状(部分成交/顺序),不依赖路径:引擎在"恰好干净"的输入上给对答案,这正是它三次
  逃过金标测试的原因。

**待确认性取证(desk session 拿到 DB 后 5 分钟)**:dump gt00/gt02/gt011 三格 grid_fills,
分别喂引擎与直算,复现 +15/−41 分叉,坐实触雷形状(预期:gt00/gt02 有部分成交或乱序行)。

## 二、修复原则

**记账的答案不允许来自模拟器。** 引擎(回测同源)只用于研究/对比;一切进记录、进对账、进展示的
盈亏,一律用与 `_avg_cost` 同源的**逐笔精确直算**。

## 三、组件一:LiveEquity.pnl_exact(核心)

`live_equity.py` 在 `_avg_cost` 的同一条回放循环里同时累计 realized(O(n) 单遍,无 pandas/引擎):

```
逐笔 f(price px, signed qty s),维护 pos/avg:
  开新/同向加仓 → avg 加权(现逻辑);
  减仓(pos×s<0, |s|≤|pos|)→ realized += (px − avg) × (−s 的带符号规约)
      多头减仓: realized += (px − avg) × qty_reduced
      空头减仓: realized += (avg − px) × qty_reduced
  穿越翻向 → 先按上式 realize 掉整个旧仓,再以 px 开新向余量(avg=px);
返回:
  realized_exact   = 上述累计
  unreal_exact     = (mark − avg) × net(带符号,空头自然为 (avg−mark)×|net|)
  pnl_exact        = realized_exact + unreal_exact − real_fee_paid − funding_paid
  pnl_ratio_exact  = pnl_exact / cap
```

`snapshot(mark)` 返回值全面切换:`pnl_ratio`/`net_value`(=1+ratio)/`realized_pnl` 改由
pnl_exact 供数;引擎重放从 snapshot 中**移除**(或仅保留在带 `engine_debug=True` 的研究入口)。
下游(records 落库、accounting 快照、dashboard、monitor 止损判定)取数字段名不变——**止损判定
(固定/回撤/PV 的 pnl_ratio)也随之诚实化**,这是本方案的隐藏收益:止损此前判在同一个失真数上。

**转仓行语义自然正确**:closeshare/closeset 转出格按 mark realize(= 现设计意图),转入格按 mark
建仓——直算天然满足,不再依赖引擎凑。

## 四、组件二:record 守恒审计(组件三 verify-ledger 扩展)

关格落库后新增一条可离线验证的守恒:

- `record.total_pnl ≈ Σ(该格 DB fills 直算 realized) + funding − fees`(容差 max($0.05, 0.1%×cap));
- `dbadmin verify-ledger` 增 `--records` 段:全量重算历史 record,报偏差清单——**引擎时代的历史
  失真一次性全部曝光**(不只这次 VVV 两笔)。

## 五、组件三:外部干预检测(churn 缺口,同为重启前置)

2026-07-11 churn 根因(已考古坐实):用户 HL 前端手动全平(11:03:46)→ HL 自动撤销无仓位的
reduce-only 触发单 → monitor 不知情(手动成交不在 grid_orders 无法归因),每轮重挂丝 → 又被撤
→ 17 秒后起无限循环。修两道闸:

1. **币级外部干预熔断**:monitor 每轮已有 Σclaims vs 交易所净仓对账;偏差超容差(复用现有
   drift 容差)→ 该币全部网格**降级为只读**(不补单/不挂丝/不新开,只告警
   `[intervention] symbol=X Σclaims=… exchange=… 管理已暂停`),直到人工 resolve(dashboard
   控制台按钮或指令表)——系统永不与人抢方向盘;
2. **重挂即消升级告警**:同一格的丝"重挂后下一轮又不在"连续 ≥2 轮 → 停止重挂该格丝并告警
   `[fuse] re-place futile grid=X n=2 已停止`(与①互为兜底:①靠仓位差触发,②靠订单行为触发,
   覆盖"仓位没差但交易所拒收"的场景如保证金/资产下架)。

## 六、组件四:历史修数(部署后一次性)

v39 上线(2026-07-11 06:35)以来的全部 records 用 `--records` 审计重算;超容差的(至少 VVV gt00
+15→约−42、gt02 +11→约−42,以 DB fills 直算为准)按既往回填流程修正(dry-run → 用户过目 → 应用),
并在 record 备注(或日志)留痕。更早的历史失真(引擎时代存量)只报告不自动改,清单交用户定夺。

## 七、不变的东西

`_avg_cost` 本身、PositionLedger 全部(claims/transfer/close_set/比例分摊)、fills 摄入/游标、
引擎 `cal_equity_curve`(回测继续用,分毫不动)、records schema、止损阈值参数。

## 八、测试计划

1. **金标差分**:均匀 lot、纯线价、无合成行的流水(现有全部测试场景)——pnl_exact 与引擎
   net_value 差 < 1e-9,现有测试零改动通过;
2. **实证回归用例(本次事故钉死)**:构造 gt00 形状(3 买多价 + 合成 reduce 卖 @低于均价)→
   pnl_ratio 必须为负且 = 手算值;ADA 形状(469 买+60 卖非均匀)→ 无幻影;ZRO 三笔部分成交形状;
3. 转仓对:转出格 realize、转入格建仓 @mark,双格 pnl_exact 之和 = 0(零费转仓守恒);
4. 过零翻向、空头方向对称、funding/fee 扣减;
5. verify-ledger --records:对造错 record 报警,正常库静默;
6. 干预熔断:FakeExchange 手动平仓剧本 → 币级只读 + 告警,不再重挂;重挂即消 ≥2 轮 → 停手;
7. 止损判定回归:固定/回撤/PV 在 pnl_exact 口径下的现有测试语义校准。

## 九、分期与部署

- **阶段 1 = 组件一+二**(直算收编+审计):关账本质修复,最高优先;
- **阶段 2 = 组件三**(干预检测):重启前置的另一半;
- **阶段 3 = 组件四**(历史修数):部署验证后执行;
- 每期:实现批准 → **testnet 直接部署+造数验证(用户已预批,2026-07-12:"每阶段完成直接上
  testnet 造数据验证",造数须覆盖 manual 入口 + 部分成交 + 转仓)**;
- **mainnet 一律不碰**(hands-off 令持续有效,重启与 mainnet 部署另行等用户指令)。
  部署走 GH Actions;`push production`=真钱,当前禁止。

## 十、待用户决策

1. 引擎重放从 snapshot 中彻底移除,还是保留 debug 入口(推荐:彻底移除,研究用途另行入口)?
2. 组件三①的"人工 resolve"形态:dashboard 按钮 / 指令表行 / 重启视同 resolve?
3. 历史修数范围:仅 v39 后,还是全量扫描引擎时代存量失真一并报告?
