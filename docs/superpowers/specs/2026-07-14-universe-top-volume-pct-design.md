# 币池相对成交额口径（前 55%） 设计

> 状态:**已获用户批准(2026-07-14)**。用户决策:①票池层 $1M 绝对地板 → 全市场 24h 成交额
> **前 55%** 相对口径;②选币层既有 `交易额分位占比<=0.55`(selection.py:89,legacy 经回测验证
> 的策略参数)**保留不动**——两层复合(票池粗筛所级流动性,选币层筛周期内相对活跃,时间尺度
> 不同);③实施后重部 testnet,新口径进入观察期。

## 一、现状与动机

票池层现为绝对地板:live 侧 `resolve_live_universe(min_quote_volume)` 用
`fetch_24h_quote_volumes()`(交易所 24h ticker),prod 配 $1M;回测侧 selection_replay 逐币
PIT 判断(近 24 根 1h K 线 quote_volume 和 < 地板剔除)。绝对阈值随行情水位漂移(牛市门槛
过松/熊市过紧),相对口径自适应市场总量。

## 二、设计

### 2.1 实盘(runtime/universe.py)

`resolve_live_universe` 新增 `top_volume_pct: float = 0.0`(0=停用):

- 时序:黑名单剔除 → 绝对地板(如设,机制保留可叠加="先地板再前 pct") → **前 pct 截取** →
  whitelist 交集(沿现顺序)。
- 截取:按 24h 量降序取前 `ceil(pct × N)`(N=进入本步的票池大小,至少 1);无量数据的币按
  0.0 参与排序(自然垫底);量并列按 symbol 字典序(确定性)。
- fail-open:`fetch_24h_quote_volumes()` 返回空 → 跳过本步不清空票池(沿绝对地板既有语义)。

配置:`config.py` 新增 `UNIVERSE_TOP_VOLUME_PCT` env → `DeployConfig.universe_top_volume_pct`
(默认 0.0);`MIN_QUOTE_VOLUME_24H` 参数机制保留不退役。两 fly toml:设
`UNIVERSE_TOP_VOLUME_PCT="0.55"`,删除 `MIN_QUOTE_VOLUME_24H` 设置行(prod 原 $1M)。
调用点(cycles/scheduler 处 resolve_live_universe 的实参)接新配置。

### 2.2 回测(backtest/selection_replay.py + backtest_run.py)

相对口径需要**跨币当轮排名**,与现"逐币独立判地板"结构不同:

- replay 主循环每 run_time 先对全候选向量化计算 trailing 24×1h `quote_volume` 和,
  取前 `ceil(pct × N)` 生成该轮入选集合;per-symbol worker 改查集合(绝对地板逻辑保留,
  两者可叠加,与 live 同语义)。
- PIT 声明:live 用交易所滚动 24h ticker,回测用近 24 根 1h K 线和——沿用绝对地板时代的
  同口径近似。
- env:`BT_UNIVERSE_TOP_PCT` 默认 `0.55`(与生产对齐);`BT_MIN_QUOTE_VOLUME_24H` 默认改
  `0`。`run_backtest`/`select_grids` 增 `top_volume_pct` 参数并入选币缓存 key(不同 pct
  不串缓存)。

### 2.3 明确不动

`core/selection.py:89` 的 55% 相对过滤、因子体系、tier 名单、grid 参数一律不动。

## 三、验证

- live 单测:5 币量表 pct=0.55 → ceil(2.75)=3 币且为量前三;缺量币垫底被切;ticker 空
  fail-open;并列字典序确定性;与绝对地板叠加时序。
- 回测单测:构造两币量差异序列,断言低量币在该 rt 被剔、高量币入选;pct=0 恒等现状。
- 全量 pytest 回归 + 选币缓存 key 区分验证;实施后重部 testnet(新口径进入 ≥3 天观察期),
  并建议按验收②跑全历史回测重验参数(票池分布已变)。

## 四、影响面预估

币安 ~530 在市永续 → 票池 ~292 币(前 55%);选币层复合后有效候选约前 30-40%。
