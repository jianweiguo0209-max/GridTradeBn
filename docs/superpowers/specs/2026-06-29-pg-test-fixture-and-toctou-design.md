# 双模式 PG 测试 fixture + 真并发 TOCTOU 测试 — 设计

> 来源：用户要求把所有「用到数据库」的测试改为可在 Docker Postgres 上跑，并补上延后的
> `transition_status` 真并发 TOCTOU 测试（见记忆 `deferred-toctou-concurrency-test`）。
> 日期：2026-06-29。方向：双模式（用户选 A）；CI 不加 PG job（用户决定）。

## 背景与动机

测试套件目前在 23 个文件、约 30 处硬编码 `StateStore.in_memory()`（SQLite）。SQLite 掩盖了
若干只有 Postgres 才暴露的 bug（如 ms 时间戳列 INT4 溢出，见 STATUS §6），且其 StaticPool
单连接造不出真并发交错。目标：让**所有 DB 测试都能在真 Postgres 上跑**（按需），并新增
`transition_status` 状态机守卫的**真线程并发**红→绿测试。

本地已起 Docker PG 验证通过：`StateStore.from_url` 连通、`create_all`、`transition_status`
版本守卫在真 PG 上行为正确（stale 写抛 `ConcurrencyError`）。

## 方向决策（用户敲定）

- **双模式共享 fixture**（A）：设 `TEST_DATABASE_URL` → 全量走 PG；否则 SQLite。保住默认
  「快/离线/CI 不依赖 PG」，要保真时一行环境变量切到 PG。
- **CI 不变**：默认 `pytest -q` 仍 SQLite；不加 PG job。PG 测试本地按需跑。
- **不改生产代码**：纯测试基础设施 + 测试。

## 组件

### 1. 两个 fixture（`tests/conftest.py`）
- `_truncate_all(store)` 辅助：按 `gridtrade.state.models.metadata.sorted_tables`，
  `TRUNCATE <全部表> RESTART IDENTITY CASCADE`（仅 PG 分支用）。
- **`store`（双模式）** fixture：
  - `TEST_DATABASE_URL` 有值 → `StateStore.from_url(url)`；`create_all()`；`_truncate_all` 清台；
    `yield`；收尾 `store.engine.dispose()`。
  - 无值 → `StateStore.in_memory()`；`create_all()`；`yield`（SQLite 天然每测一新库，无需 truncate）。
- **`pg_store`（PG-only）** fixture：`TEST_DATABASE_URL` 无值 → `pytest.skip('set TEST_DATABASE_URL …')`；
  有值 → 同 `store` 的 PG 分支。仅并发测试用（SQLite StaticPool 单连接造不出真竞态）。

### 2. 重构 23 文件用 `store` fixture
把各文件内联的 `StateStore.in_memory(); create_all()` 改为注入 `store` fixture；内部自建库的
helper（如 `_setup` / `_grid_repo_with` / `_new_executor`）改为**接收** `store` 入参而非自建。
保留每个测试原意——尤其 `test_reconciler` 中「gx 与 gx2 共享同一 store 模拟重启」的跨进程语义，
继续共用该 fixture store（两者 `StateStore` 指向同一库即可）。

涉及文件（23）：`tests/state/`（test_grids, test_orders, test_accounting, test_records,
test_fills, test_heartbeats, test_store_schema, test_transition_revalidate）、
`tests/execution/`（test_gates, test_grid_executor, test_grid_executor_idempotent, test_manager,
test_monitor, test_reconciler, test_reconcile_orderid, test_sync_orderid, test_chaos_open,
test_chaos_replenish, test_chaos_reconcile, test_chaos_close）、
`tests/runtime/`（test_cycles, test_chaos_cycle, test_dbadmin）。
（`test_factory` 通过 `build_runtime` 内部建 in_memory，不直接持 store——见下注。）

> 注：`test_factory` / `build_runtime` 走 `config.database_url` 自建 store（空→in_memory）。
> 该路径已支持 PG（设 `DATABASE_URL`），但与测试 fixture 解耦；本计划**不强行**把 factory 测试
> 接入 fixture（避免改 factory 行为），仅保证它在默认 SQLite 下照常绿。

### 3. 并发 TOCTOU 测试（`tests/state/test_transition_concurrency.py`，用 `pg_store`）
`threading.Thread` + `threading.Barrier(N)` 同刻触发，各线程经 `engine`（QueuePool）拿独立连接：
- **场景 A — 版本守卫竞态**：建一个 grid（version=v）；N=8 线程在 barrier 后同时
  `transition_status(grid, OPENING, expected_version=v)`；收集结果。断言：恰好 1 个成功
  （返回 OPENING、version=v+1）、其余 7 个抛 `ConcurrencyError`、最终库内 `version == v+1`
  （只 +1 一次、无 lost update、无双赢）。
- **场景 B — 双重平仓竞态**：建一个 ACTIVE grid；2 线程在 barrier 后同时 `ACTIVE→CLOSING`；
  断言恰好 1 赢、另一个抛、最终 status=CLOSING 且 version 只 +1（证明不会双重 close）。

线程数 N=8 在默认 QueuePool（size 5 + overflow 10 = 15）容量内。每线程捕获自身异常与返回值，
主线程 join 后聚合断言。

## 验收标准

- **两种后端都全绿**：默认 `pytest -q`（SQLite，286 + 重构后不减）与
  `TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade pytest -q`（PG）均通过。
- 并发测试：无 PG 时 skip；有 PG 时 A/B 两场景通过。
- 不改任何生产代码。

## 文档 / 记忆

- 并发测试文件 docstring + STATUS/DEPLOY 记跑法：`docker run -d --name gridpg -e POSTGRES_PASSWORD=grid
  -e POSTGRES_DB=gridtrade -p 5432:5432 postgres:16` + `export TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade`。
- 更新记忆 `deferred-toctou-concurrency-test`：「延后」→「已补（本地 PG 真线程；CI 仍 SQLite）」。

## 范围外（YAGNI）

- 不加 PG CI job（用户决定；本地按需跑）。
- 不改生产代码、不动 factory/`build_runtime` 的建库路径。
- executor 级端到端「两 monitor 各自 close」竞态不做（守卫即 transition_status，已在守卫层真线程测）。
