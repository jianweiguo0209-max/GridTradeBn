# API 权重减负第一步：funding/bar 去重 + 权重遥测

日期：2026-07-23
状态：已批准（巡检发现 429 水位吃紧后的第一刀）

## 背景与动机

2026-07-23 巡检定量：币安 IP 权重限额 2400/min，mainnet monitor 机实测
`x-mbx-used-weight-1m` 单分钟内爬到 ~1100+；每 ~13-14min 出现 1-2min 降级窗
（snapshot 跳轮 / funding_rate 降级 / bar_buffer 走缓冲），scheduler 选币轮偶发
1/286 币被 429 跳过。全部降级按设计自愈、无资金风险，但水位吃紧是
pv-refresh60（07-22 上线）后的新常态，压力再涨可能伤及开关格路径
（07-22 17:01Z B/USDT 开格撕裂疑似发生在 429 压力窗内）。

日志实锤的浪费源：cap2 同币双格（如 GWEI×2）下，funding_rate 同币同秒
fetch 两次——`signals.py` 的缓存按 `grid_id` 键控，同 symbol 的两个 grid
各自独立取数；`bar_buffer.get_closed_bars` 无"已是最新"短路，同币双格
每分钟重复增量拉取。

**本刀不追求大幅减重（去重仅省 ~8 req/min），核心产出是遥测归因**：
把 ~1100/min 的权重大头点名，为下一刀（websocket 化或主动权重闸）提供
裁决数据。

## 范围裁定（已与用户确认）

- ✅ 做：funding 按 symbol 去重、bar_buffer 新鲜度短路、权重遥测（方案A）
- ❌ 不做：断路器改造——三路类别熔断（market_read/account_read/trade_write）
  7-02 事故后已存在且已接线（`resilient_adapter.py`），429 是 IP 级限流、
  三路同倒是正确行为，分组粒度再细无解。原"④断路器按 endpoint 分组"撤销。
- ❌ 不做：pv klines 错峰（限流窗为固定分钟窗，分钟内错峰不减总量；
  跨分钟错峰破坏刚验证的 pv 机制对齐——07-23 recon 5/5 复现、Δ≤0.15pp）
- ⏸ 缓：429 分钟窗感知退避、主动权重闸、websocket——等遥测数据再裁

## 设计

### 1. funding 去重（`gridtrade/execution/signals.py`）

`LiveSignalProvider` 新增 symbol 级费率缓存：`self._fr_cache = {}`
（`symbol → (fetched_at_sec, funding_rate)`），TTL 与现有 `refresh_sec`
相同（默认 60s，读 `SIGNAL_REFRESH_SEC`）。`_funding_rate(symbol, now_ms)`
先查缓存、过期才真 fetch。**grid 级 `_cache` 保留不动**（pv+fr 组合结果
仍按 grid 节流，evict 语义不变）。取数失败仍降级返 0.0，且**失败结果不
写入 symbol 缓存**（避免把降级值粘住 60s——与现行为一致：现在每 grid
每 refresh 都会重试）。

效果：同币双格费率取数 2 次/min → 1 次/min。费率语义零变化
（费率每 8h 才结算一次，60s 内复用无信息损失）。

### 2. bar_buffer 新鲜度短路（`gridtrade/execution/bar_buffer.py`）

`get_closed_bars` 在现有 stale 判定前加短路：缓冲非空且末根
`candle_begin_time == cutoff − 60s`（已含最新已收盘桶）→ 跳过 fetch，
直接走既有切片返回路径。同币双格/同分钟内重复调用自然去重。

**机制零变化**：返回数据与 fetch 后完全同源（增量 fetch 在"已最新"时
本来就只会拉到被丢弃的 forming 桶），不碰 pv 对齐口径。

并发说明：monitor 4 线程下同 symbol 两格可能同时判 stale 双 fetch——
维持现状（良性，drop_duplicates 兜底），不为此加锁。

### 3. 权重遥测（方案A：ResilientAdapter 单咽喉）

- **计数**：`ResilientAdapter._call` 为唯一咽喉（monitor/scheduler 各自
  进程实例、全部交易所调用都过它）。`threading.Lock` 护住
  `self._call_counts[method] += 1`。计逻辑调用数（分页方法计一、重试不
  重复计）——对"找大头"归因够用，接受此偏差。
- **水位**：`CcxtAdapter` 新增 `used_weight_1m() -> Optional[int]`，读
  ccxt `client.last_response_headers` 的 `x-mbx-used-weight-1m`
  （零额外请求）。header 缺失/非币安适配器返 None。
- **上报**：`ResilientAdapter.report_weight(log)`：分钟翻转时打一行
  `[weight] w1m=<水位|?> calls/min: fetch_ohlcv=45 fetch_positions_all=5 ...`
  （按计数降序）并清零；同一分钟内重复调用 no-op。inner 无
  `used_weight_1m` → `w1m=?` 优雅降级。
- **驱动点**：monitor 轮末尾（`cycles.py`，~13s 一轮自然驱动）+
  scheduler 选币轮取数循环内（选币分钟内也能出归因线）。
- **只打日志不落库**（YAGNI）：诊断时 `fly logs` 现场 tail 半小时即可
  归因；真需要回溯分析再加表。

### 4. 错误处理

`report_weight` 全身 try/except——遥测任何异常只打日志、绝不影响交易
路径。计数器每分钟清零，无溢出之虞。

### 5. 测试（TDD）

- funding：同币双格同 refresh 窗内只 fetch 一次；TTL 过期后重新 fetch；
  取数失败降级返 0.0 且不污染 symbol 缓存
- bar_buffer：缓冲已含最新收盘桶 → 零 fetch 返回正确切片；缺新桶 →
  照旧增量；降级沿用缓冲路径不回归
- telemetry：分钟翻转打一行且清零；同分钟 no-op；多线程并发计数不丢；
  inner 无 header → `w1m=?`；report 内部异常不外抛
- 全量套件绿后按部署规矩：合 main → push origin → merge production 走 CD

## 验收

1. 部署后日志每分钟一行 `[weight]`，能读出 top 权重消费方法
2. 同币双格 symbol 的 funding fetch 频次减半（日志不再出现同币同秒×2）
3. pv/funding 判定行为与部署前一致（机制零变化）
4. 用遥测数据裁决下一刀（websocket / 权重闸 / 砍具体大头）
