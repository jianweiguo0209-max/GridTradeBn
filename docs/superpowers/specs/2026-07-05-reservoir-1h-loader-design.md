# Reservoir 1s→1h/1m 全市场装载器 + 1h 数据源窗口自动切换 — 设计

> 状态：设计已与用户确认，待写实现计划。
> 日期：2026-07-05
> 分支策略：**堆叠在 `backtest-selection-perf` 上**（用户选定）；开工前先提交工作树中的 con2 实验改动（默认关、金标已验）。

## 目标（一句话）

把回测 1h 选币数据的可回溯起点从 HL API 的滚动 ~208 天拓展到 **Reservoir 归档起点 2025-07-31**（可回测窗口起点 ≥ 2025-08-14，含 14 天选币暖机），为多窗 OOS 验证（首个用途：thr−0.005 主动止损参数）提供可复用的预热能力。

## 背景与动机

- 选币在 1h 上算因子；1h 现在只从 HL API 取，滚动 ~5000 根 ≈ 208 天且逐日前移——今天能跑的窗口下个月就跑不了。
- 持仓 1m 已走 Reservoir S3（1s 重采样），归档 2025-07-31 起（实测列桶）；funding 走 HL API，实测可回溯 ≥ 2024-06——都不是瓶颈。
- Reservoir 1s 列含 `volume_quote` → 1h 的 `quote_volume` 可完整重建（$1M 地板 + 交易额分位占比因子不受损）。
- PV 止损 OOS 验证发现 thr−0.005 是两窗方向一致的强候选，需要 2025-08~12 再切 2 个独立窗口确认——正是本装载器的第一个用户。

## 用户决策（已拍板）

1. **1h+1m 全币种同写**：day 文件含当天全部币，一次下载同时产出 1h(全币)+1m(全币)。phase2 选中币 1m 直接命中缓存零重下；未来任何选币结果都可复用。代价：磁盘多 ~2-4GB/150 天。
2. **按窗口自动切换**：`warm_start` 早于 `now − 200 天` → 整个 run 的 1h 走 Reservoir，否则走 HL API。无 env 旋钮、无拼缝（单 run 单源）。接受的代价：同一命令在不同日期跑可能换源（结果微差）。
3. **堆叠在 `backtest-selection-perf` 分支上**，不等合并决策。

## 范围

**做**：① `reservoir.py` 装载器泛化（多 timeframe 一次下载同写）；② `backtest_run.py` main() 1h 数据源自动切换 + Reservoir 起点守卫；③ 单测；④ 文档。

**不做**（明确非目标）：
- 不动 `core/`（选币/因子/引擎零改动；金标不碰）。
- 不动 funding 路径（HL API 够用）。
- 不做更早（<2025-07-31）的数据源（Tardis / hl-mainnet-node-data 另立项目）。
- 不做 API/Reservoir 双源同窗混拼（单 run 单源）。
- 两窗验证跑本身不进代码库（分析脚本走 scratchpad，同 PV 扫参先例）。

## 全局约束

- Python 3.9 / pandas 1.3.5 / numpy 1.22.4 / pyarrow；不新增第三方依赖（S3 仍走 `aws s3 cp` subprocess）。
- 金标 parity 不可破（本项目不碰 core，天然满足；全套测试绿）。
- **近窗口（api 源）行为字节不变**：`_pick_1h_source` 返回 `'api'` 时 main() 走现路径，与改动前逐位一致。
- `warm_reservoir_1m` 公共签名/行为保持向后兼容（现有调用与测试不改仍绿）。
- 「不完整的天不缓存」语义全保留：当天未过完 / S3 404 / 拉取失败 → `retry_later` 不落任何文件；日文件成功但某币无成交 → 落空哨兵。
- 测试命令：`TZ=Asia/Shanghai .venv/bin/python -m pytest`。

## 设计

### 1. `gridtrade/backtest/reservoir.py`

**`candles_1s_resample(df, symbol_map, rule)`**（泛化自 `candles_1s_to_1m`）：
- `rule` ∈ {`'1min'`, `'1H'`}（pandas resample 规则字符串）；agg 不变：open=first / high=max / low=min / close=last / volume=sum / volume_quote=sum；`label='left', closed='left'`（bar-begin 口径）。
- Decimal→float 预转换、tz-naive UTC、CANDLE_COLS 映射（vol=volume, volCcy=quote_volume=volume_quote）全保留。
- `candles_1s_to_1m(df, symbol_map)` = `candles_1s_resample(df, symbol_map, '1min')` 薄包装（向后兼容）。

**`warm_reservoir_ohlcv(cache, universe, start_ms, end_ms, *, timeframes=('1h', '1m'), workdir=None, log=print)`**（泛化自 `warm_reservoir_1m`）：
- 逐 UTC 天循环，骨架与现 `warm_reservoir_1m` 一致：
  - 当天(UTC)未过完 → `retry_later`，不落盘；
  - **幂等跳过条件**：`all(cache.exists(tf, s, day) for tf in timeframes for s in universe)`——所有 timeframe 全命中才 skip；只差其一（如旧缓存只有 1m）也重下 day 文件、把缺的补齐（已存在的 `cache.write` 覆盖写同值，幂等无害）；
  - `aws s3 cp` 失败/404 → `retry_later`，不落盘；
  - 下载成功：对每个 `tf`，`candles_1s_resample(raw, symbol_map, rule_of(tf))` → 逐币写 `cache.write(tf, s, day, df)`；该币当天无成交 → `cache.write_empty(tf, s, day, CANDLE_COLS)`（真空哨兵）；
  - timeframe→rule 映射：`{'1m': '1min', '1h': '1H'}`。
- 返回 stats 按 namespace 分列：`{'1h': {'days': n, 'rows': n}, '1m': {'days': n, 'rows': n}, 'skipped_cached': n, 'retry_later': n}`。
- **`warm_reservoir_1m(cache, universe, start_ms, end_ms, *, workdir=None, log=print)`** 保留为薄包装：调 `warm_reservoir_ohlcv(..., timeframes=('1m',))`，把返回映射回旧格式 `{'days', 'rows', 'skipped_cached', 'retry_later'}`（现有调用方/测试零改动）。

### 2. `gridtrade/backtest/backtest_run.py`

**常量**：
```python
RESERVOIR_START = pd.Timestamp('2025-07-31')   # Reservoir 归档起点（实测列桶）
_API_1H_MAX_DAYS = 200                          # HL 1h 滚动 ~5000 根≈208 天，留余量
```

**`_pick_1h_source(warm_start, now) -> 'api' | 'reservoir'`**（纯函数，可单测）：
- `warm_start < now − _API_1H_MAX_DAYS 天` → `'reservoir'`；否则 `'api'`。

**main() 集成**（phase1 分叉，其余不动）：
- `source = _pick_1h_source(warm_start, pd.Timestamp.utcnow())`，响亮打印所选源。
- `'api'`：现路径字节不变（`resolve_universe` → `PW.prewarm_ohlcv`）。
- `'reservoir'`：
  - 守卫：`warm_start >= RESERVOIR_START`，否则 `raise SystemExit`（信息含最早可用窗口起点 = RESERVOIR_START + 14 天暖机 = 2025-08-14）；
  - universe 仍来自 `resolve_universe(_ds1h)`（今日上市表；存活者偏差照旧，见忠实度注记）；
  - `RV.warm_reservoir_ohlcv(cache, universe, _ms(warm_start), _ms(win_end), timeframes=('1h', '1m'))`（惰性 import，同现有模式）；
  - 选币/phase2/回测代码路径不变——`prewarm_sim_and_funding` 的选中币 1m 将全命中缓存（`skipped_cached`），funding 照旧 HL API。

### 3. 忠实度注记（文档写明）

- Reservoir-1h 与 HL-API-1h 是两个采集源，同一根 bar 数值可能微差；单 run 单源保证窗口内一致。同一币的 1h 缓存可能跨 run 混源（不同天来自不同源）——按天分界，选币磁盘缓存的指纹（缓存天范围）变了会自然换 key，不会静默复用过期选币结果。
- 老窗口 universe = 今日上市表 → 存活者偏差随窗口变早而加重；组间相对比较（如 PV 参数）不受影响，绝对收益读数打折扣。

### 4. 测试

| 测试 | 断言 |
|---|---|
| resample 正确性 | 构造 1s 假数据 → `candles_1s_resample(..., '1H')` == 手工聚合期望值（OHLC/vol/quote_volume 逐列） |
| **1h/1m 一致性** | 同一 1s 输入：1s→1h 直采 == 1s→1m 输出再按 1H 聚合（agg 同构 ⇒ 恒等；防重采样口径漂移） |
| 双命名空间落盘 | `warm_reservoir_ohlcv(timeframes=('1h','1m'))` 后 cache 两个 namespace 各有该天数据/空哨兵 |
| 幂等 | 全命中二跑 `skipped_cached`；只删 1h 一边 → 重下补齐两边 |
| 薄包装兼容 | `warm_reservoir_1m` 返回旧格式、现有 `test_reservoir.py` 全绿 |
| `_pick_1h_source` | 边界：now−199d→api、now−201d→reservoir |
| main 守卫 | reservoir 源 + `warm_start < RESERVOIR_START` → SystemExit（用假 argv 只测到守卫，不触网） |

S3 真网络端到端不进 CI（人工验证跑覆盖，与现状一致）。

### 5. 实现后的两窗验证（不进代码库，scratchpad 脚本）

- **W1：2025-08-15 ~ 2025-10-14**（暖机 08-01 起，贴档案起点）
- **W2：2025-10-15 ~ 2025-12-14**（与 W1、与既有 2026-01~02 / 2026-03~06 窗口均不重叠）
- 每窗 4 组：baseline(thr−0.02) / **thr−0.005** / thr−0.002 / 无主动止损；con2 恒 0（已证惰性）；全口径 + 去尾（各窗按各自 top 贡献币）双列，含 MDD/年化/Calmar（相对比较）。
- 预估：~136 个 day 文件、下载 ~1 小时、egress $2-5。

## 交付顺序

1. 提交工作树 con2 实验改动（`grid_engine.py`/`backtest_run.py`，默认关）。
2. 本 spec → plan → SDD（loader + 自动切换 + 测试 + 文档）。
3. 验证跑 W1/W2 → thr−0.005 结论报告。
