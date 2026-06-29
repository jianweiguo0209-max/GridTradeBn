# 回测实现 — 完成度与待办清单

状态图例：✅ 完成 · 🟡 部分/需完善 · ⬜ 未开始 · 🔒 卡外部数据/输入

## 一、已完成 ✅
- 预热 S0/S1/S2/S3：1H K线、选币回放(候选+tick清单)、资金费+标记价(条件取数)、合约规格冻结
- 按天 Parquet 缓存（原子写 + 空哨兵 + exists 短路 + 幂等续跑）
- **选币 parity 已验证**：FOGO 3/3 + guoj账户2 offset7 的 HMSTR/BILL/RAVE/MMT 4/4，因子值逐位吻合
- 因子配置单一来源（回测直接读 account_0/config.py，自动跟随）
- 网格成交仿真核心 `grid_sim`：等比布网、逐格成交、持仓 MTM、原生 TP/SL；13 个不变量测试
- 退出规则 `apply_exit_rules`：固定止盈损 + 回撤止盈 L1/L2（复用实盘阈值）
- 端到端回测 `backtest_run`：候选→布网→持仓bars→仿真→聚合
- 1m 粒度仿真（`--sim-bar 1m`），已验证显著降低 1H 的假止损噪声
- 单元测试 40 个全过；模拟盘开关 OK_SIMULATED

## 二、仿真器 fidelity（最高优先）
- 🔒 **绝对值校准**：现只有 3 条网格的冒烟校准（~4H 持仓时 MAE 0.43%）。需完整 `gridResult.csv`
  （带关仓时间 + 含止盈/止损/亏损的多样样本）做严谨校准。卡在拿数据。
- ⬜ **每格数量模型校准**：当前"等量"假设，OKX 实际可能等保证金分配——最大待校准建模点
- ⬜ **bar 内路径**：已支持 1m；如需更高保真再接 tick

## 三、退出逻辑完整性（PnL parity 关键）—— ✅ 已完成
`grid_engine._apply_exit` 复刻实盘 calc_loss_or_profit 优先级，逐 bar 取最早触发：
- ✅ 固定止损（pnlRatio < -stop_loss）
- ✅ Chandelier 连续回撤止盈（trailing_k/trailing_floor，已替代旧 L1/L2，对齐最新 config）
- ✅ 资金费率止损（|fundingRate| > 阈值；需 S2 funding 数据在缓存才生效）
- ✅ pv 爆量主动止损（backtest_run.compute_pv_spike：15m 充分历史 rolling(233)）
- ✅ OKX 原生 TP/SL（破网）+ 爆仓（liquidation）
- 🟡 **监控粒度**：实盘每 5s，回测按 bar（1m）——仍有颗粒度差异（可接受）

## 四、PnL 模型完整性
- ⬜ **资金费 PnL 并入 `simulate_grid`**（永续持仓应按 funding 时点收/扣；数据已在 S2 缓存）
- ⬜ **滑点建模**（现在只扣手续费；设计文档 §8 的滑点/次根开盘入场假设未做）
- ⬜ **标记价用途**（mark 已缓存，但强平/资金费基准未建模）
- 🟡 **组合级权益曲线**：当前按 offset 等权复利的简化模型；未对齐实盘 rebalance=True 的资金共担逻辑

## 五、数据层
- 🟡 **1m 取数提到 prewarm 阶段**：现在在 backtest_run 里按需拉（混了网络）；应做成 S2-style 条件预取，
  让回测纯读缓存、扫参更快、更符合架构
- ⬜🔒 **逐笔 tick 下载**：tick_manifest 已产出，但下载器未做（官方下载页自动化链接未确认）
- 🟡 **survivorship bias**：票池来自当前存活合约，已退市币缺失（已文档化，未解决）

## 六、引擎/架构（设计文档支柱）
- 🟡 支柱三 seam-driven：`run_backtest` 已是单函数，但还不是优化器可直接包的干净接缝（配置注入未完全参数化）
- ⬜ 支柱六 **信号漏斗（Funnel）**：未做"候选→通过→各闸门拦截→定仓"的可观测计数，调参缺抓手
- ⬜ 支柱七 **结果落库**：现在只出 CSV；缺 runs/trades 表 + 每次运行的 config 快照（可复现根）
- ⬜ 支柱八 成交成本：滑点/入场假设可切换（sweep价 vs 次根开盘）未做
- ⬜ **守护测试**：缺"回测永不触达实盘下单"的断言测试；point-in-time 边界测试只有部分

## 七、覆盖度/健壮性
- ⬜ **布网 V2 在回测中未验证**（grid_version=1 在用，V2 路径没跑过回测）
- ⬜ **退出/PnL 侧 parity 未验证**（只验证了选币 parity；网格 PnL vs 实盘仅 3 条冒烟）
- ⬜ 参数扫描 demo（用 run_backtest 作接缝扫 V1/V2、price_limit、grid 参数）

## 八、非回测但相关
- ⚠️ config.py `weight_list=[1,1]` 与 3 因子不匹配——实盘 select_grid_coin 会报错，需改 `[1,1,1]`

---
## 建议优先级
1. 拿到完整 gridResult.csv → 做严谨校准（解锁"绝对值可信"）
2. 退出逻辑补全（资金费止损 + pv 主动止损）+ 资金费 PnL 并入 → PnL parity
3. 1m 取数提到 prewarm + 滑点建模 → 干净可复现 + 诚实成本
4. 信号漏斗 + 结果落库 + 守护测试 → 可观测/可复现/安全
5. 参数扫描 / V2 验证 / tick 下载 → 扩展
