# 实盘 PV legacy 满窗语义移植(阶段二)设计

> 状态:设计已获用户批准(2026-07-07);观察协议=testnet 无报错即可;取数=原生 15m;灰度=改代码默认值;**参数组合经用户拍板:thr=+0.005 × mult=3 × n=100**(n 取甜点档而非 legacy 默认 233)。

## 背景

PV 研究(2026-07-07,详见 memory `pv-thr-sweep-and-reservoir-loader` 与 `data/tiercmp/pv_*.csv`)发现:

1. **语义漂移**:legacy(OKX 时代)的 PV 量能尖峰 = 当前 15m 成交额 > 3 × **过去 233 根 15m(≈58h)满窗滑动均值**(600 根前置历史垫底,config `active_loss_period='15m'/candle_num=600`)。移植到本系统时,实盘 `LiveSignalProvider._pv_spike` 只取**开格以来**的 1m(`fetch_ohlcv(symbol,'1m',open_ms,now_ms)`),`rolling(233, min_periods=1)` 在 ≤48 根 15m 桶上退化为 **expanding**(开格以来均值)——回测窗口内同构,两边彼此一致但都非 legacy 语义。
2. **回测裁决(干净数据+对齐费率,IS/OOS/W1/W2 四窗)**:legacy 满窗语义 × `pv_pnl_thr=+0.005` 四窗全正且全胜漂移语义,更胜线上现参(漂移 × −0.02:诚实均值 −2.67%、W1 −7.16%)。**语义与门槛必须绑定翻转**:legacy 语义 × −0.02 在 W1 反而更差(−8.54%)。**目标组合(n=100,用户拍板)成绩:IS +4.80%/OOS +2.97%/W1 +1.22%/W2 +3.72%,诚实三窗均值 +2.64%、最坏窗 +1.22%、MDD 均值 ≈−1.01%**(n=233 档为 +1.71%/+0.86%)。
3. **参数敏感性全绿**:mult 2-4 全正平台(维持 3)、n 50-300 全正(**n=100 为甜点档,经用户拍板采用**;系同三窗事后择优、带多重比较风险,testnet 前向验证兼作其裁决)、con2 三度验证惰性(维持 0)、双侧带被支配(不采纳)。
4. **策略换形须知**:新配置下 ~60-65% 网格在首个真量能尖峰即撤(每格均值≈0,近乎免费的 regime 避险),盈利引擎=活到期的 23-31% 格 + 少量连续回撤止盈;收益结构=轻微正偏+砍左尾(中位 0/胜率 31-42%),对实盘摩擦敏感,故 testnet 先行兼前向验证。

## 目标

实盘 PV 信号恢复 legacy 满窗语义 + 参数翻至用户拍板组合:**legacy 满窗语义 × thr=+0.005 × mult=3 × n=100 × con2=0**。

## 改动(两文件+测试)

### 1. `gridtrade/execution/signals.py` — `LiveSignalProvider._pv_spike`

- 取数:`fetch_ohlcv(symbol, '1m', open_ms, now_ms)` → **`fetch_ohlcv(symbol, '15m', now_ms − (n+8)×15min×60_000, now_ms)`**(n=100 → 108 根 ≈ 27h,含 8 根缓冲;单次请求,远轻于现最重 3600 根 1m)。
- `calc_pv_spike` 共用不动:原生 15m 喂入,`resample('15min')` 恒等映射;`rolling(n, min_periods=1)` 在 108 根上为**真满窗滑动**(评估点=最后一根,前置充分)。最后一根为进行中的 partial 桶——与现行为一致(legacy 亦含未走完 K 线),不改。
- `get(grid_id, symbol, open_ms)` 签名不变(`open_ms` 保留参数、不再决定取数窗口),`cycles.py` 零改动;节流 900s、失败降级(异常→pv=0)、`evict` 均不动。
- docstring 更新:去掉"开网时刻→现在/expanding 对齐回测"表述,改为 legacy 满窗语义说明。

### 2. `gridtrade/config.py` — `DEFAULT_STOP_CFG`

- `pv_pnl_thr: -0.02 → 0.005`(触发条件 = `pv_spike && pnlRatio < +0.005`,即尖峰时浮盈不足 +0.5% 即撤)。
- **`pv_n: 233 → 100`**(量能基线记忆 58h→25h,n 扫描甜点档)。
- `pv_n` 注释更新(真滚动窗,非"窗内 expanding");`pv_mult=3`/`pv_period='15min'` 不动。

### 3. 测试

- signals 单测:fake adapter 断言取数 `timeframe=='15m'` 且 `since ≈ now−108×15min`;满窗行为差分(前置历史充分时基线为滑动均值而非 expanding——构造前 100 根高量+近期低量场景,expanding 与满窗给出不同尖峰判定)。
- 回归:全套测试(`evaluate_exit`/stop_rules 显式传参用例不受默认值影响;引擎金标不破——`simulate_grid_engine` 独立默认 `pv_pnl_thr=-0.015` 未动)。若有测试断言 `DEFAULT_STOP_CFG['pv_pnl_thr']==-0.02` 则同步校准语义。

## 连带影响(已获用户接受)

1. **mainnet 继承**:改代码默认,下次 production push 即带上策略换形;mainnet 部署审批门不变;回滚 = git revert + 重部署。
2. **回测默认口径缺口(已知,不在本期)**:`DEFAULT_STOP_CFG` 翻转使回测默认 thr 同步为 +0.005(`HL_STRATEGY` 镜像引用,方向一致);但**回测管线默认仍是漂移语义**(窗口内算 pv、无 lookback)——本期后实盘为满窗 × n=100、回测默认为漂移(漂移语义下 n≥48 逐位等价,pv_n 翻转不改回测默认数字),不再逐位一致。漂移 × +0.005 亦四窗全正(+0.97%),方向一致数字有别;满窗语义研究复现走 `data/tiercmp/pv_legacy_sem.py`/`pv_n_sweep.py` harness。**后续独立改进**:把 lookback 接入 `assemble_grid_tasks`(引擎 `pv_spike_df` 注入口已在)。

## 非目标

- con2/pv_body_ratio(维持 0,三度验证惰性;0.5 档三度复测均微害)。
- mult 变更(mult=4 为激进候选,待前向验证后另议)、n 进一步精调(150 档留作对照)。
- 双侧带 `pv_pnl_thr_hi`(已 revert,不采纳)。
- 回测管线 lookback 接入(上节缺口,单独立项)。

## 上线路径与观察

1. 实现+全套测试绿 → **用户批准 push main → testnet 自动部署**。
2. testnet 观察(用户定协议:**无报错即可**;顺带记录 pv 退出占比是否 ~70% 量级(n=100 四窗回测 68-71%)、无异常 taker 风暴)。
3. 达标后 → **用户批准 main→production push(真钱,策略换形正式生效)**。
4. 回滚:revert 两处默认 + 重部署。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 15m 原生桶 vs 回测 1m-resample 桶边界差异 | 均为 quarter-hour UTC 对齐,成交额同源聚合;testnet 观察期核对 pv 触发频率量级(~70% 格) |
| thr=+0.005 下 pv 退出为主退出(~70%),taker 平仓次数增 | 回测已按 taker 4.5bps 计费仍四窗全正;testnet 观察无异常风暴 |
| n=100 为同三窗事后择优(多重比较) | testnet 前向验证兼作 n=100 的裁决;若不及预期回落 n=233(单参数改回) |
| 实盘摩擦(滑点)侵蚀轻微正偏 | testnet 前向验证;mainnet 小资金先行(现状 ~$3k) |
| 尖峰判定对 partial 桶的抖动 | 与现行为一致(15min 粒度 900s 节流),不新增 |
