# MarginGate（可用保证金准入门）— 设计

> 来源：design.md §6② 准入门链的第 4 道门（4 门中唯一未实现；SymbolLock/MaxConcurrent/RiskBudget 已落地）。
> 日期：2026-06-29。口径已与用户敲定（见下）。

## 背景

准入门链（Chain of Responsibility）是「触发→准入→执行」三段式里开网格的第一道闸：
触发器产出 `GridProposal` → `GateChain` 顺序过闸（短路）→ 放行的交 `GridManager` 开仓。
门只读状态、不下单、不写库。

已有的「钱」相关判断都**不是** MarginGate：
- `RiskBudgetGate`：`∑(活跃网格 cap)+本提议 cap ≤ total_budget`——**静态计划预算**，不看真实余额。
- `grid_order_info` 返回 None → 开仓抛「建网失败：保证金不足」——**几何可行性**（cap 太小凑不出合法网格）。

**MarginGate 补的是「实盘资金现实校验」**：实时查交易所**可用余额**，确认此刻真有钱开得起这个网格，
把「计划允许但账户实际不够」的提议在开仓前干净拒绝（避免 InsufficientFunds 拒单、半开网格、贴爆仓线）。

## 口径决策（用户敲定）

- **所需保证金**：`cash ≥ cap`（保守）。整份 cap 当所需保证金；网格实际只用 `cap×max_rate(0.68)` 做名义敞口，
  故天然留 ~32% 缓冲。不做按名义/初始保证金率精算，不做额外可配缓冲系数。
- **同一轮多提议**：累计扣减（防同轮超额放行）。
- **比对字段**：可用余额 `Balance.cash`（计价币种，HL=USDC，与 cap 同单位，无需换算）。
- **失败兜底**：fail-closed（余额读不到 → 该批全拒，不开看不到余额的仓）。

## 组件与语义

新增 `MarginGate(adapter, default_cap)`（gates.py）：
- **所需** = `proposal.cap` if not None else `default_cap`。
- **放行**：`self._available − self._reserved ≥ 所需` → 放行并 `self._reserved += 所需`；否则拒绝带原因。

### 累计扣减 → 给门链加批次钩子
累计扣减要求门在「一轮 filter」内记账。现 `GateChain.filter` 逐提议无状态，故加轻量生命周期钩子：
- `AdmissionGate` 增默认空实现 `begin_batch(self)`（其余三门不覆写、不受影响）。
- `GateChain.filter` 在遍历提议前，对每个门调一次 `gate.begin_batch()`。
- `MarginGate.begin_batch()`：快照 `self._available = adapter.fetch_balance().cash`、`self._reserved = 0.0`、
  `self._balance_ok = True`；若 `fetch_balance()` 抛异常则 `self._balance_ok = False`（fail-closed）。
- `MarginGate.check(p)`：`_balance_ok` 为 False → 拒绝「balance unavailable」；否则按上面放行/拒绝。
- 健壮性：`check` 在 `_available is None`（未经 begin_batch 的独立 `evaluate` 调用）时惰性初始化一次
  （fetch 一次、reserved=0），保证 `GateChain.evaluate` 单提议路径也可用。

### 排序约束：MarginGate 放链尾
「放行即预留」仅当「过 MarginGate = 最终准入」才正确。`GateChain.evaluate` 短路——能走到 MarginGate
说明前置门都过了；只要它在**链尾**，过它即被准入，预留不会因后续门拒绝而虚高。factory 把它追加为最后一道。

## 接线（factory）

[factory.py:52-56](../../gridtrade/runtime/factory.py#L52) 的 `GateChain([...])` 末尾追加
`MarginGate(adapter, config.default_cap)`。复用已有 `adapter`（ResilientAdapter）与 `config.default_cap`，
**无新增 config 项**。

## 测试

- **门单测**：`cash≥cap` 放行 / `<` 拒绝；累计扣减（如 cash=250、cap=100、default_cap=100 → 前两个过、
  第三个因 `250−200<100` 拒）；`begin_batch` 跨批刷新余额（第二批余额变化后结果随之变）；
  `fetch_balance` 抛异常 → 该批全拒（fail-closed）；`proposal.cap` 显式给值时按它而非 default_cap。
- **门链集成**：MarginGate 在链尾，`GateChain.filter` 触发各门 `begin_batch`、同轮累计正确（多提议按 cash 分配）。
- **factory 接线**：构造的 GateChain 含 MarginGate。
- 现有全套（当前 279）测试零回归。

## 范围外（YAGNI）

- 按名义敞口/初始保证金率精算所需保证金。
- 可配安全缓冲系数。
- 跨进程余额并发一致性（门每批快照一次实时余额即可；多监控机并发属后续阶段）。
