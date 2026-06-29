# GridTradeGP Dashboard 第二期（控制台）— 设计文档

> 日期：2026-06-30
> 状态：设计已确认，待转 writing-plans
> 前序：P1 只读监控 `docs/superpowers/specs/2026-06-29-dashboard-design.md`（已上线 testnet）
> 相关记忆：`dashboard-project`

## 1. 背景与目标

P1 只读监控 Dashboard 已上线 testnet（fly 第三进程 `web`，FastAPI+Jinja+HTMX，登录鉴权，四视图）。P2 给它加**控制能力**：人工运维可从 Dashboard 急停、关/开网格、暂停选币、查看候选币池。

**核心安全立场（延续 P1）**：`web` 进程**永不直接动交易所**。所有控制动作 = web 往 Postgres 写「标志位」或「指令」；真正下单/平仓只发生在 **monitor** 进程（单一执行权威，与现有对账/幂等/重启自愈严丝合缝）。

## 2. 范围（已确认）

第一批动作：
1. **查看候选币池**（只读）——配置币池一览 + 「立即算选币」按需排名。
2. **Kill switch 两档**——「暂停交易」(halt) 与「急平所有」(panic)。
3. **手动关网格**——关某个活跃网格。
4. **暂停/恢复 scheduler**——停整点选币开新（不动现有网格）。
5. **手动开网格**——选币种，web 只读算出 grid_params 预填、可覆盖关键参数，再开。
6. **跨切面：手机响应式 UI**——含 P1 已上线视图一并适配窄屏。

**不在本期**：价格/指标阈值自动触发器（三期，扩展点 `TriggerCondition` 已留）；同币种多网格。

## 3. 架构

```
浏览器 ──登录会话──> web 进程(FastAPI)
   │  控制动作 = 写 DB（绝不直接动交易所）
   ▼
Fly Postgres ── control_flags（标志位）
            ├── control_commands（指令队列）
            └── control_audit（审计日志）
   ▲                              │ 每 ~5s 消费
   │ scheduler 整点前读 flags       ▼
scheduler 进程                 monitor 进程（已握交易 adapter+executor）
（读 halt / scheduler_paused）   消费 commands + 读 flags + 写回结果 + 记审计
```

**不变量**：
- web 只读 bot 状态 + 只读行情；控制动作仅写 `control_*` 三表。**零下单/平仓**。
- 真正动交易所的执行只在 monitor（沿用现有 `executor`/`manager`）。
- 指令认领幂等：monitor 用乐观锁版本守卫认领 PENDING（沿用 `state/models` 的 `transition_status` 套路），保证一条指令至多执行一次。

## 4. 数据模型（3 张新表，SQLAlchemy Core，加入 `state/models.py` 风格）

### 4.1 `control_flags`（标志位，循环读）
| 列 | 类型 | 说明 |
|---|---|---|
| `name` | String PK | `trading_halted` / `scheduler_paused` |
| `value` | String | `'true'` / `'false'`（字符串存布尔，跨后端稳） |
| `updated_at` | BigInteger | UTC ms |
| `updated_by` | String | 会话用户名 |

读取语义：缺行视为 `false`（默认不 halt、不 paused）。

### 4.2 `control_commands`（指令队列，monitor 消费）
| 列 | 类型 | 说明 |
|---|---|---|
| `id` | String PK | uuid hex |
| `type` | String | `CLOSE_GRID` / `OPEN_GRID` / `PANIC_CLOSE_ALL` |
| `payload` | String | JSON 文本（见下） |
| `status` | String | `PENDING` → `RUNNING` → `DONE` / `FAILED` |
| `result` | String nullable | 成功摘要或失败原因（`repr(exc)`） |
| `created_at` | BigInteger | UTC ms |
| `created_by` | String | 会话用户名 |
| `claimed_at` | BigInteger nullable | monitor 认领时刻 |
| `finished_at` | BigInteger nullable | 终态时刻 |
| `version` | Integer | 乐观锁认领守卫 |

payload 约定：
- `CLOSE_GRID`：`{"grid_id": "...", "symbol": "...", "reason": "manual"}`
- `OPEN_GRID`：`{"symbol": "...", "params": {grid_params 全字段}, "tag": "...", "offset": 0}`
- `PANIC_CLOSE_ALL`：`{"reason": "panic"}`

状态机：`PENDING→RUNNING`（版本守卫认领）、`RUNNING→DONE|FAILED`。仅 monitor 写 RUNNING/DONE/FAILED；web 仅插入 PENDING。

### 4.3 `control_audit`（审计日志）
| 列 | 类型 | 说明 |
|---|---|---|
| `id` | String PK | uuid hex |
| `ts` | BigInteger | UTC ms |
| `actor` | String | 会话用户名 |
| `action` | String | `FLAG_SET` / `CMD_SUBMIT` / `CMD_RESULT` |
| `target` | String | 标志名 / 指令 id / 网格 id |
| `detail` | String | JSON 文本（新值、payload 摘要、结果） |
| `outcome` | String | `ok` / `fail` |

记录点：web 每次切标志（FLAG_SET）、每次提交指令（CMD_SUBMIT）；monitor 每条指令终态（CMD_RESULT）。

## 5. 每个动作的行为

| 动作 | web | monitor / scheduler |
|---|---|---|
| 暂停/恢复 scheduler | 切 `scheduler_paused` + 审计 | scheduler 整点前读，paused 则跳过整轮选币 |
| Kill·暂停交易(halt) | 切 `trading_halted` + 审计 | monitor 跳过补单+开仓执行；scheduler 跳过选币 |
| Kill·急平所有(panic) | 置 `trading_halted=true` + 入队 `PANIC_CLOSE_ALL` + 审计 | monitor 消费→遍历所有活跃网格逐个 `executor.close` |
| 手动关网格 | 入队 `CLOSE_GRID` + 审计 | monitor 消费→`executor.close(grid_id, symbol, reason)` |
| 手动开网格 | web 只读 `fetch candles + core.grid_params` 算默认值→预填可改表单→提交入队 `OPEN_GRID` + 审计 | monitor 消费→`executor.open(...)`（用提交参数；若 `trading_halted` 为真则拒并 FAILED） |
| 查看候选币池 | 只读页：配置币池 + 「立即算选币」（`fetch candles + core.selection` 排名，同步跑、带 loading） | —— |

### 5.1 halt 语义（已确认）
`trading_halted=true` 时：**冻结加仓类新动作**——monitor 跳过 replenish 补单与 OPEN 执行、scheduler 跳过选币。**风险递减与记账照常**——止损平仓（`stop_rules` 触发的 close）、reconcile 对账、accounting 记账继续运行。即按下 halt 不会让该止损的网格扛着不平。

### 5.2 panic 流程
panic = 同一提交内：置 `trading_halted=true`（防止刚平又被开/补）+ 入队 `PANIC_CLOSE_ALL`。monitor 消费时遍历当前所有活跃网格逐个 close，部分失败记入 result、不阻断其他网格（沿用 P6① 的 per-grid 隔离精神）。

### 5.3 monitor 集成
`run_monitor_cycle` 增加两步：**(a) 读 `trading_halted` 门控** replenish/open；**(b) 消费一条 PENDING 指令**（认领→执行→写终态+审计）。每周期处理一条指令即可（人工动作低频），避免单周期长阻塞。reconcile/accounting/stop 保持原有顺序与位置不变。

### 5.4 scheduler 集成
`run_scheduler_once` 开头读 `trading_halted` 或 `scheduler_paused`，任一为真则跳过本轮选币（记一条日志），不影响其睡到下一个整点。

## 6. UI 与手机适配

- **新增页面**：`/controls`（两档 kill + scheduler 暂停/恢复 + 指令队列状态 + 审计日志）、`/open`（候选币池 + 选币 + 参数表单）、`/universe`（币池 + 立即算选币排名）。overview/detail 的活跃网格行加「关」按钮。
- **确认**：每个写动作一个 UI 确认弹窗（轻量，HTMX/原生），不重输密码。**panic 用更刻意的确认**——勾选框或输入确认词（如 `PANIC`）才放行。
- **手机响应式（含 P1 视图）**：纯 CSS `@media` query，无前端框架。窄屏（≤ ~640px）下：宽表格转**堆叠卡片**（每行一张卡、字段竖排带标签）、按钮加大（min 44px 触控）、健康顶栏 flex 换行、导航折叠。桌面保持现表格。改 `static/app.css` + 模板加语义化 class，P1 四视图一并受益。

## 7. 鉴权 / 安全

- 所有控制路由在 P1 登录会话之后，全为 POST（GET 仅渲染表单/页面）。未登录→302 /login（沿用 P1 `_user`）。
- 写动作幂等与校验：CLOSE_GRID 仅对存在的活跃网格入队；OPEN 在 halt 时由 monitor 拒（FAILED）；指令认领版本守卫保证至多一次执行。
- web 仍**零下单**：控制路由只写 `control_*` 三表 + 只读 `fetch_candles/fetch_price` 算开仓参数。私钥的实际下单使用仍只在 monitor。
- 审计全覆盖：标志切换、指令提交、指令结果三类事件均落 `control_audit`，UI 可查。

## 8. 测试（双后端 TDD）

- **控制仓储**：`ControlFlagRepository`（get/set，缺行默认 false）、`CommandRepository`（enqueue/claim 版本守卫/finish）、`AuditRepository`（add/list）——内存 SQLite + PG 双模式单测。
- **monitor 消费**：FakeExchange 下，每种指令执行 + 状态流转（PENDING→RUNNING→DONE/FAILED）；halt 门控（replenish/open 被跳过、止损仍跑）；panic 全平 + per-grid 隔离；故障注入测部分失败→FAILED+result，不阻断健康网格。
- **scheduler 门控**：halt/paused 任一为真→跳过选币。
- **web 路由**：FastAPI TestClient——未登录 401/302；POST 正确切标志/入队指令；审计落库；OPEN 表单参数预填（FakeExchange 注入行情）；CLOSE 按钮入队正确 payload。
- **手机 CSS**：视觉项不单测；保证模板渲染（TestClient 200 + 关键控件存在）。

## 9. 风险与开放项（实现期定）

- 「立即算选币」在 web 同步拉全币池 K 线，可能慢（数秒）；一期接受 + loading 态，后续可移 monitor 预算缓存。
- monitor 每周期处理一条指令：积压时按 FIFO 逐周期消化；人工低频，可接受。
- panic 与正在进行的 reconcile/replenish 的交错：halt 先置真 + per-grid 隔离 close 已缓解；实现期补混沌测试覆盖。
- 手机卡片布局的具体断点与字段取舍，实现期按真实视图微调。
