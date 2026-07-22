# pv 实盘评估机制对齐回测（收盘桶 + 每分钟 + 增量缓冲）

2026-07-22。目标：**提高回测对实盘的预测准确性**，做法是让实盘 pv 止损的评估机制与回测优化时假设的机制一致。**非对账**（不追求事后逐格 byte 复现）。

## 动机

回测（`simulate_tasks` → `_apply_exit`）**每根收盘 1m bar 判一次 pv 止损**，pv 由 `calc_pv_spike` 在收盘 1m 上算。s030 等参数的六窗寻优/验证全在此机制下得出。

实盘 `LiveSignalProvider`（`signals.py`）却是：
- **(a) 每 900s 采样一次**（按格节流缓存），非逐分钟；
- **(b)** 评估时取到的最后一根 1m 是**未收盘的半截桶**（成交额欠计）；
- **(c) 取数降级**：`_pv_spike` 每次**全量重取 27h×1m**，任一 fetch 失败 → 返回 `pv_spike=0` 并缓存 900s（FIL 型尾部分歧根因）。

→ **实盘执行的不是被回测优化的那套策略**，回测自然预测不准。

### 与 2026-07-20 归档方案的关系
`2026-07-20-pv-sampling-mirror-deferred.md` 分析过同一问题，但走**反方向**（在回测里镜像实盘 900s 采样）——那会造成「pv 驱动 ~54% 退出 → 第四次口径断代 → 历史 sweep 全部重基线」，故 defer。**本方案改实盘去匹配回测，回测一行不动，历史 sweep 全部仍有效**，绕开该坑。

该台账（17 格裸对账）的经验证据：**pv 退出 17/17 一致、PnL 中位差 ~1bp**，说明 (a) 节流本身极少翻结果；真正的尾部分歧是 **(c) 取数降级**。故本方案**重心为增量缓冲（治 (c)）**，(a)(b) 为低成本的机制对齐一并做。

## 设计（改三处，全在 `LiveSignalProvider`，引擎/回测零改动）

### 1. 收盘桶 (b)——无条件，正确性
`_pv_spike` 只在**已收盘** 1m 上算：丢弃 `candle_begin_time >= floor(now,'1min')` 的 forming 分钟。使 `cur(t)` 与回测同用已收盘桶。无旗标（收盘桶就是对的）。

### 2. per-symbol 增量 1m 缓冲（主战场，治 (c)）
`LiveSignalProvider` 内持一个按 symbol 的收盘 1m 缓冲：
- **冷启动/首访**：全量拉 `[now-(n+8)·period, now]` 的已收盘 1m（≈1620 根，~2 次 klines），入缓冲。
- **之后每次**：只增量拉 `since=最后一根缓冲 ts` 的新收盘 bar（常 1~几根），append + trim 到窗宽。
- **取数失败降级改语义（关键）**：增量拉失败时，**继续用缓冲里已有的收盘 bar 算 pv**（记 degraded 日志），**不再塌回 pv_spike=0**。仅冷启动无缓冲时才返 0。→ 直接消除 FIL 型「fetch 失败→静默 0→错过尖峰」。
- **缺口/过期**：若缓冲最后 ts 距 now 超过窗宽（进程停过久），重新冷启动全载。

pv 计算：`calc_pv_spike(缓冲收盘 bar)`，取 `pv_spike.iloc[-1]`（=最后一根已收盘 bar）。

### 3. 每分钟评估 (a)——默认开
- 沿用现有「按 grid 节流缓存」结构，唯一改动是**节流阈值** `refresh_sec`：由 env **`SIGNAL_REFRESH_SEC` 配置，默认 60**（现状硬编码 900）。即每格每 60s 复算一次 pv → 每根新收盘 1m 都会被读到（bars 也是 1m 一根）。
- `factory.py` 传入该值（现在没传、吃类默认 900）。设 `SIGNAL_REFRESH_SEC=900` 即回退到 15min 节奏（仍保留收盘桶+缓冲）。
- 缓冲使每 60s 复算成本 ≈ 增量拉 1~2 根，权重可忽略。
- 残留 action 抖动：实盘对尖峰动作最多晚 `refresh_sec`（默认 ≤60s）vs 回测 bar 收盘即动——已从 900s 收窄到 60s，二阶。

## 确定性论证
做完 (a)+(b) 后，实盘与回测在同一时刻 t 均为「从同一批已收盘 1m（回看 ~27h）跑同一个 `calc_pv_spike`」。币安已收盘 K 线不可变 → **两侧 pv_spike 序列逐位相同**。pv 信号层完全可预测。

## 明确不做 / 残差（诚实交底）
- **不做** signal_snapshots 录放——那是对账，非本目标。
- **不做** 回测退出滑点建模——单独的二阶改进（可另立）。
- **残差（(a)(b) 治不了，二阶）**：① pv 止损联合门的 `pnl_ratio` 用实盘真实 pnl vs 回测仿真 pnl，−1% 刀刃处仍会微差；② 市价平仓滑点（CAP 实测 ~2pp）回测不建模、偏乐观。二者属网格执行保真度，不在本次范围。

## 组件边界
- 新增 per-symbol 缓冲：可内嵌 `LiveSignalProvider`，或抽 `OneMinuteBarBuffer`（fetch_fn 注入，便于 fake 测试）。倾向后者——职责单一、可独立测。
- `LiveSignalProvider._pv_spike` 改为「刷新缓冲 → calc_pv_spike(收盘桶) → iloc[-1]」。
- `funding_rate` 路径不动（本次仅 pv）。
- `factory.py` 传 `refresh_sec` 与 config `SIGNAL_REFRESH_SEC`；`config.py` 加该 env（默认 60）。

## 错误处理
| 场景 | 行为 |
|---|---|
| 增量拉失败、缓冲有数据 | 用现有缓冲算 pv（degraded 日志），**不塌回 0** |
| 冷启动拉失败、缓冲空 | 返回 pv_spike=0（安全默认）+ 日志，下轮重试 |
| 缓冲过期（停机久） | 重新冷启动全载 |
| 无 quote_volume 列 | 返回 0（同现状） |

## 测试（TDD，fake adapter 注入 bar 序列）
1. **(b) 收盘桶**：喂含 forming 半截桶的序列，pv 只按已收盘 bar 算（结果 == 丢掉最后 forming 行）。
2. **缓冲冷启动**：首访全量拉一次，覆盖 (n+8)·period 窗。
3. **缓冲增量**：第二次只拉 `since=last_ts`，且「冷载 + 增量」结果 == 「一次性全载」（等价性）。
4. **降级不塌 0**：增量拉抛异常、缓冲有数据 → pv 仍按缓冲算（≠0 当序列本应 spike）。
5. **冷启动失败**：缓冲空 + 拉失败 → 返回 0，不抛。
6. **每分钟节流**：同一分钟内多次 get 只算一次；跨到新收盘分钟才复算。
7. **确定性**：同一批收盘 1m 下，`LiveSignalProvider` 的 pv_spike == `pv_spike_for_window`/`calc_pv_spike` 逐位一致。
8. **配置**：`SIGNAL_REFRESH_SEC` 从 env 生效（默认 60）。

## 上线 / 回滚
- (a) 默认开（`SIGNAL_REFRESH_SEC=60`）。回滚：`fly secrets set SIGNAL_REFRESH_SEC=900`（仍保收盘桶+缓冲，严格优于原状）。
- 走 production 分支 CI/CD（见 [[deploy-prod-via-cicd-only]]），verify-ledger 预检。
- 观察：上线后核 monitor 权重无 429、pv 止损频率/pnl 是否落在回测预期附近。
