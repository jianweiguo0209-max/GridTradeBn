# P6① 故障注入 / 混沌测试 — 设计

> 阶段：P6「加固」第一项（design.md §12）。
> 日期：2026-06-29。前置：`main` 全绿（258 tests）、testnet 端到端跑通。
> 目标：在真金白银上 mainnet 前，用注入式故障**主动验证**执行器+对账+止损在
> 「超时/拒单/限频/交易所维护/部分成交」下仍正确——降级续跑、最终对账收敛、
> 无重复单 / 无孤儿单 / 记账不漂移（需求 1 健壮性的核心异常路径）。

---

## 1. 背景与缺口

`exchanges/resilience.py` 的重试/熔断/错误分类**已有单元测试**
（`tests/exchanges/test_resilience.py`：用假 `fn` 抛 ccxt 异常，孤立测 resilience 层）。

**缺口**：没有让故障发生在交易所边界、穿过**完整执行栈**
（`GridExecutor → ResilientAdapter → 交易所`）的集成层混沌测试。即"resilience 这块零件能转"
已验证，但"整机在异常下端到端是否仍守住不变量"未验证。本设计补这一层。

监控（testnet 常驻）= 正常路径**有机**验证；本设计 = 异常路径**主动**验证，互补。

---

## 2. 机制决策（已与用户敲定）

**包装适配器 `FaultyAdapter`**（而非把钩子塞进 FakeExchange）。理由：
- FakeExchange 保持单一职责（撮合），不被故障逻辑污染；
- 包装器实现 `ExchangeAdapter` 端口，对任意内层适配器通用；
- 测试栈与生产栈同构（生产是 `ResilientAdapter` 包真实适配器），最贴近真实。

**部分成交在纯包装器里的建模**：包装器拦截 `create_market_order`，把传给内层的 size
乘以 ratio（内层 FakeExchange 持仓真的只动一部分），返回 `filled=ratio*size` 的 Order。
覆盖底仓/平仓 reduce 没吃满的真实 HL 场景。**限价单部分成交不在范围**
（由价格穿越驱动、grid 补单只认 trade，价值低）。

---

## 3. 组件：`FaultyAdapter`

新增 `gridtrade/exchanges/faulty.py`，实现 `ExchangeAdapter` 端口的透明包装器。

构造：`FaultyAdapter(inner, schedule: Dict[str, List[Fault]])`。
`schedule` 按「方法名 → 故障列表」组织，每次调用该方法消费列表头一个故障；
列表耗尽后该方法恢复正常透传。

故障类型：

| Fault | 行为 | 分类（resilience） |
|---|---|---|
| `Timeout` | 抛 `ccxt.RequestTimeout` | retryable |
| `Reject` | 抛 `ccxt.InvalidOrder`（或 `BadRequest`） | fatal（不重试） |
| `RateLimit` | 抛 `ccxt.RateLimitExceeded` | rate_limit |
| `Maintenance` | 抛 `ccxt.OnMaintenance` | retryable |
| `Partial(ratio)` | 仅 `create_market_order`：内层 size×ratio，返回 filled=ratio×size | — |
| `OK` / 耗尽 | 透传内层 | — |

放在 `exchanges/` 层（允许 import ccxt，与 `ResilientAdapter` 对称）。Fault 用轻量
dataclass/常量表达，便于脚本可读。

---

## 4. 测试栈

```
GridExecutor
  → ResilientAdapter(sleep=lambda _: None, rng=Random(seed))   # 快进退避、确定性
    → FaultyAdapter(inner=FakeExchange, schedule={...})
      → FakeExchange + StateStore.in_memory()
```

新增 `tests/execution/test_chaos_*.py`：离线、无网络、确定性（注入假 sleep + 定种子 rng）。
每条测试给无故障基线快照，再注入故障，断言端到端不变量收敛回基线。

---

## 5. 混沌场景

| # | 场景 | 注入 | 断言 |
|---|---|---|---|
| A | 开仓中途超时 | `create_limit_order` 前 2 次 `Timeout` | 重试后网格仍达 ACTIVE、全部线挂齐、无重复单（client_oid 幂等） |
| B | 补单超时重试 | sync 补对侧单时 `Timeout`→OK | 恰好 1 个补单、无双重摄入、记账与无故障基线一致 |
| C | 对账遇孤儿/缺失 + 瞬时故障 | reconcile 期间 `fetch_open_orders` 或补单 `Timeout` | 故障清除后收敛到期望单集、无双补 |
| D | 平仓部分成交 / 维护窗口 | close 的 reduce 市价单 `Partial(0.5)`；或 `Maintenance` | 暴露残留持仓是否被处理——见 §7 |
| E | 熔断降级 | 连续故障打开熔断 | cycle 不崩、不 sys.exit；冷却后续跑恢复 |

---

## 6. 不变量（贯穿所有场景）

1. **无重复单** —— client_oid 幂等，重试不产生第二个挂单。
2. **无孤儿单** —— 交易所上不残留非本网格意图的挂单。
3. **无双重补单 / 双重摄入** —— `grid_fills.trade_id` 幂等去重。
4. **记账不漂移** —— `realized_pnl` / `net_position` 与无故障基线一致（容差 1e-9）。
5. **最终收敛** —— 故障清除后一次 reconcile/sync 使交易所+DB 回到期望态。
6. **全程不崩** —— 不 sys.exit、不吞 BaseException；耗尽/熔断由上层降级。

---

## 7. 可能触发的真实加固（测试驱动发现）

打法（已与用户敲定）：**先写红测试暴露问题，证实了再决定修或记**，不预先假定有 bug。

- **per-grid 隔离**：现 `runtime/cycles.py::run_monitor_cycle` 逐网格无 try/except，
  单网格故障可能掀翻整轮 cycle。若混沌测试证实，则加 per-grid 降级
  （一个坏网格不阻塞其他网格的对账/补单）。
- **平仓部分成交残留**（场景 D）：`close()` 是终态、reduce 市价单不重试，
  部分成交会留残仓。若证实不收敛，补一层残仓校验/补平（或显式记为已知限制）。

生产代码改动一律 TDD（红→绿），并在该处停下向用户汇报"发现 X，建议修/记"。

---

## 8. 范围外（YAGNI）

- 限价单部分成交建模。
- 随机/猴子式乱序故障注入（用确定性脚本，保证断言可复现）。
- 真并发线程级 TOCTOU 测试（已记忆延后到多监控机阶段，见 `deferred-toctou-concurrency-test`）。
- MarginGate / ThresholdTrigger / OrderFilled 事件（需口径，属其他延后件，见 `p4-deferred-items`）。

---

## 9. 交付物

- `gridtrade/exchanges/faulty.py` —— FaultyAdapter + Fault 类型。
- `tests/exchanges/test_faulty.py` —— FaultyAdapter 自身单测（脚本消费/各故障映射/部分成交）。
- `tests/execution/test_chaos_*.py` —— 场景 A–E 集成混沌测试。
- 若 §7 证实问题：对应生产代码加固 + 红→绿测试。
- 全套离线、并入现有 pytest（保持全绿）、CI 自动跑。
