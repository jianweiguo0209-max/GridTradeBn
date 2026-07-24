# sweep 票池对齐实盘：复用 backtest_run 构建器 + s030 对齐池复测

日期：2026-07-24
状态：已批准

## 背景与动机（选币可预测性 recon 的直接产物）

2026-07-24 回测可预测性验证（协议版，全 Vision 管道）发现**回测家族内部票池口径分叉**：

| 管道 | 票池 | COIN 过滤 | 低杠杆过滤 |
|---|---|---|---|
| `backtest_run.main()`（自述"唯一预热+回测入口"） | 归档−黑名单−非COIN−低杠杆 | ✅ :629 | ✅ :637（BT_MIN_LEVERAGE=10 默认） |
| `sweep_run.py`（参数扫描，**s030 出处**） | 归档−tier0 裸池 | ❌ | ❌ |
| score_research 研究脚本群（cf_run.py:57 等） | 同裸池 | ❌ | ❌ |

即：**s030 是在含实盘开不出的合约（非 COIN 113 个 + 低杠杆币）的池上扫描选出的**。
选币 recon 定位实证（写实归档）：
- 15 轮重放：机制/因子/数据层 **15/15 零残差**（live 快照池喂正规 select 路径 →
  10:00 轮 BRETT rank1、rank_sum=101.0 与实盘落库逐位相等）
- 宽池 vs live 池的 2/15 pick 差全部源于**池构成**：18:00=symbol-lock（运行态，本质
  不可预测）；10:00=池构成合差（lock 移除 4 币重排交错 + minlev + 24h 量口径）
- **消融**：COIN-only（剔 113 非 COIN）后 10:00 排序纹丝不动——COIN 过滤非该轮主因

**收益边界（诚实预注册）**：本改动收敛 COIN+minlev 两项口径（卫生学：扫描臂不再在
实盘开不出的币上打分），**不会**消掉 lock/24h 量口径近似两类残差。

## 改动一：提取共享票池构建器（方案A）

`gridtrade/backtest/backtest_run.py` 新增模块级函数：

```
resolve_bt_universe(adapter, blacklist, *, log=print) -> (universe: list[str], stats: dict)
```

体内 = 现 `main()` :625-640 票池块**原样搬移**：`归档全量 − blacklist` →
`exclude_non_coin(adapter)`（当前 exchangeInfo，保留退市 COIN 无幸存者偏差）→
`exclude_low_leverage`（`BT_MIN_LEVERAGE` env，默认 10.0=实盘同值；杠杆档私有端点
不可用即 **fail-loud 拒跑**，=0 显式停用回旧口径）。打印剔除统计行语义不变。
stats = `{'n_blacklist':…, 'n_tradfi':…, 'n_lowlev':…, 'min_lev':…}`。

- `main()` 改调 `resolve_bt_universe`（行为逐字节不变，重构等价）
- `scripts/sweep_run.py`：删自建裸池两行，改
  `adapter, _ = _binance_datasource_1h(cache)` + `resolve_bt_universe(adapter, bl)`；
  envfile 注入已在（底部注释本就为杠杆凭证预留）
- **score_research 归档脚本不动**（点时研究记录）；未来研究脚本一律走 `resolve_bt_universe`
- select_cache key 含 universe → 新旧池天然分键不串味
- 预注册：新池 sweep 结果与历史 CSV **不可直接对比**（池收窄是目的）

## 改动二：s030 对齐池复测（研究验证阶段）

实现合入后，在**对齐池**上重跑 s030 当前配置（band3/cmin16/pv−0.01/tr2%/stop0.03）
的六窗评估，与 data/score_research_2026-07-21 的原始（宽池）结果对比：

- 运行器：计划阶段定位原研究六窗评估入口（fwin_bt/holdout_gate 一族），以其同一
  指标口径跑对齐池版本；若原脚本硬编宽池，复制为对齐池变体放新目录（不改归档原件）
- 对比表：逐窗 Σpp / veto（破网/爆仓）/ 双留出判定，宽池 vs 对齐池
- **预注册判据**：关注方向性结论是否翻转——s030 是否仍为正、双留出是否仍过；
  数值漂移本身是预期（池变窄），不构成回滚 s030 的依据，除非留出翻负
- 资源纪律：`__main__` 守卫 + `BT_WORKERS≤4`（本地多进程死机坑，memory 在案）；
  新 universe 首跑全部 select cache MISS，预算数小时、后台跑

## 测试（TDD，改动一）

- `resolve_bt_universe` 契约：非 COIN 被剔 / 退市 COIN 保留 / 低杠杆被剔 /
  `BT_MIN_LEVERAGE=0` 旁路 / 杠杆档不可用 fail-loud（现有 exclude_* 单测之上加组合层）
- `main()` 重构等价：现有 backtest_run 测试全绿
- sweep_run 接线：universe 构建走共享函数（提取可测小函数断言调用/结果）

## 验收

1. 全量套件绿；sweep_run 冒烟打印 `[BT] 全市场票池 N 币(…−非COIN…−低杠杆…)` 统计行
2. s030 对齐池六窗对比表交付，判据按预注册裁定
3. 记忆更新：sweep 口径分叉修复 + s030 复测结论
