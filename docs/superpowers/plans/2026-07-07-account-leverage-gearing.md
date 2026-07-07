# GRID_GEARING + ACCOUNT_LEVERAGE 仓位参数重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 合并冗余参数对 `leverage×max_rate` 为 `GRID_GEARING`(3.4),新增一等公民 `ACCOUNT_LEVERAGE`(最坏净敞口倍数,部署值 3.5),`CAP_EQUITY_FRAC` 降级为推导值;并以专门测试矩阵证明 `grids.leverage` DB 列行为惰性(无迁移)。

**Architecture:** 只收敛 config/executor/gates/reconciler 表面;引擎 API(`grid_order_info`/`simulate_grid_engine`)与回测管线零改动;`GridExecutor.__init__` 向后兼容(旧 `leverage/max_rate` 参数自动折算 gearing),存量测试零churn。

**Tech Stack:** Python 3.11 / pytest / SQLAlchemy(仅测试用 StateStore,无 schema/数据变更)。

## Global Constraints

- 部署值(spec 定稿):`GRID_GEARING="3.4"`、`ACCOUNT_LEVERAGE="3.5"`、`MAX_CONCURRENT="12"` → frac=0.1716。
- 代码默认:gearing=3.4、account_leverage=2.0、max_concurrent=12 → 默认 frac=0.098(≈旧默认 0.10,行为近保持)。
- 旧 env 键 `LEVERAGE`/`CAP_EQUITY_FRAC` 被设置 → **RuntimeError 响亮报错**(含新键与换算公式)。
- 引擎/回测 API 与默认值不动;金标不破。
- **无 DB 迁移**;`grids.leverage` 新行存 gearing(审计),旧行 5.0 保留。
- 实现完成停在"报用户批 push main/testnet"门前;**fly.prod.toml 必须在同一变更里移除 `CAP_EQUITY_FRAC`**(否则新代码部署即 boot 报错)。

---

### Task 1: config — derive_frac + 新字段 + 旧键报错

**Files:**
- Modify: `gridtrade/config.py`(DeployConfig 字段区 ~44-75 行、`load_deploy_config` ~86-113 行)
- Test: `tests/test_config.py`(追加)

**Interfaces:**
- Produces: `derive_frac(account_leverage, max_concurrent, gearing) -> float`;`DeployConfig.grid_gearing: float`、`DeployConfig.account_leverage: float`;`DeployConfig.cap_equity_frac` 保留字段但由推导赋值(不再读 env);`DeployConfig.leverage` 字段删除。

- [ ] **Step 1: 写失败测试**(追加到 tests/test_config.py)

```python
def test_derive_frac_and_new_keys():
    from gridtrade.config import derive_frac, load_deploy_config
    assert abs(derive_frac(3.5, 12, 3.4) - 0.17157) < 1e-4     # 部署值
    assert abs(derive_frac(2.0, 12, 3.4) - 0.09804) < 1e-4     # 代码默认≈旧0.10
    env = {'ACCOUNT_LEVERAGE': '3.5', 'MAX_CONCURRENT': '12', 'GRID_GEARING': '3.4'}
    cfg = load_deploy_config(env)
    assert abs(cfg.cap_equity_frac - 0.17157) < 1e-4            # frac 是推导值
    assert cfg.grid_gearing == 3.4 and cfg.account_leverage == 3.5


def test_legacy_keys_raise_loudly():
    import pytest
    from gridtrade.config import load_deploy_config
    with pytest.raises(RuntimeError, match='GRID_GEARING'):
        load_deploy_config({'LEVERAGE': '5'})
    with pytest.raises(RuntimeError, match='ACCOUNT_LEVERAGE'):
        load_deploy_config({'CAP_EQUITY_FRAC': '0.10'})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`
Expected: FAIL(`derive_frac` 不存在)

- [ ] **Step 3: 实现 config**

DeployConfig:`leverage: float` → 删;`cap_equity_frac` 注释改"推导值,勿从 env 读";新增:

```python
    grid_gearing: float = 3.4       # 单格名义部署倍数(=旧 leverage5×max_rate0.68);挂单总名义额=gearing×cap
    account_leverage: float = 2.0   # 账户最坏净敞口倍数(12格同侧扫穿上限);frac 由此推导
```

新纯函数(放 compute_cap 旁):

```python
def derive_frac(account_leverage, max_concurrent, gearing):
    """cap 占权益比例 = 账户杠杆 / (最大仓数 × 单格最坏净敞口倍数 gearing/2)。
    中性网格双侧梯子最坏只吃单侧,故 /2(spec 2026-07-07-account-leverage-gearing)。"""
    return float(account_leverage) / (int(max_concurrent) * float(gearing) / 2.0)
```

`load_deploy_config` 开头加守卫、字段改推导:

```python
    for legacy, repl in (('LEVERAGE', 'GRID_GEARING(=LEVERAGE×0.68,默认3.4)'),
                         ('CAP_EQUITY_FRAC', 'ACCOUNT_LEVERAGE(frac=AL/(N×gearing/2))')):
        if legacy in env:
            raise RuntimeError('env %s 已退役,请改用 %s——语义变更,禁止静默映射'
                               % (legacy, repl))
    ...
    grid_gearing=_f(env, 'GRID_GEARING', 3.4),
    account_leverage=_f(env, 'ACCOUNT_LEVERAGE', 2.0),
    max_concurrent=_i(env, 'MAX_CONCURRENT', 12),          # 20→12(部署一律显式设)
    cap_equity_frac=derive_frac(_f(env, 'ACCOUNT_LEVERAGE', 2.0),
                                _i(env, 'MAX_CONCURRENT', 12),
                                _f(env, 'GRID_GEARING', 3.4)),
```

删 `leverage=_f(env, 'LEVERAGE', 5.0),`。

- [ ] **Step 4: 跑测试确认通过**(tests/test_config.py 全绿;若既有用例引用 cfg.leverage 则同步删改)

- [ ] **Step 5: Commit** `feat(config): GRID_GEARING+ACCOUNT_LEVERAGE,frac 降级推导值,旧键响亮报错`

### Task 2: executor/gates/reconciler/factory — gearing 化(向后兼容)

**Files:**
- Modify: `gridtrade/execution/grid_executor.py`(`__init__` 23-36 行、`open` 73/88 行)
- Modify: `gridtrade/execution/reconciler.py`(restore 26-28 行)
- Modify: `gridtrade/execution/gates.py`(MinNotionalGate check 128-133 行)
- Modify: `gridtrade/runtime/factory.py`(60-65 行)
- Test: `tests/execution/test_gearing_equiv.py`(新建)

**Interfaces:**
- Produces: `GridExecutor.gearing: float`;`__init__(..., gearing=None, leverage=None, max_rate=None)`——gearing 未给时 = (leverage or 5.0)×(max_rate or 0.68)(存量测试传 leverage=5 行为逐位不变);内部全部 `grid_order_info(cap, self.gearing, ..., max_rate=1.0)`。

- [ ] **Step 1: 写失败测试**(tests/execution/test_gearing_equiv.py)

```python
"""gearing 换元等价:新 (gearing, max_rate=1.0) 与旧 (leverage, max_rate) 逐位一致。"""
from gridtrade.core.grid_engine import grid_order_info


def test_order_num_bitwise_equal():
    old = grid_order_info(302.0, 5.0, 10.0, 12.0, 20, 9.0, 13.0, max_rate=0.68)
    new = grid_order_info(302.0, 3.4, 10.0, 12.0, 20, 9.0, 13.0, max_rate=1.0)
    assert old['每笔数量'] == new['每笔数量']                       # 5×0.68 == 3.4×1.0
    assert list(old['价格序列']) == list(new['价格序列'])


def test_executor_backcompat_and_gearing():
    from gridtrade.execution.grid_executor import GridExecutor
    class _A:  # 最小假 adapter(本测试不触网)
        pass
    ex_old = GridExecutor(_A(), None, cap=100.0, leverage=5.0)      # 旧签名
    ex_new = GridExecutor(_A(), None, cap=100.0, gearing=3.4)       # 新签名
    assert abs(ex_old.gearing - 3.4) < 1e-12
    assert abs(ex_new.gearing - 3.4) < 1e-12
```

- [ ] **Step 2: 确认失败**(GridExecutor 无 gearing)

- [ ] **Step 3: 实现**

grid_executor.py `__init__`:

```python
    def __init__(self, adapter, store, *, cap, gearing=None, leverage=None, fee=0.0002,
                 c_rate_taker=0.0005, max_rate=None, min_amount=0.0, ...):
        ...
        # gearing(单格名义部署倍数)= 旧 leverage×max_rate;旧参数保留向后兼容(测试/脚本)
        if gearing is None:
            gearing = float(leverage if leverage is not None else 5.0) \
                      * float(max_rate if max_rate is not None else 0.68)
        self.gearing = float(gearing)
```

删 `self.leverage`/`self.max_rate`;`open()`:`grid_order_info(cap, self.gearing, ..., max_rate=1.0)`、落库 `leverage=self.gearing`;reconciler restore:`grid_order_info(grid_cap, ex.gearing, ..., max_rate=1.0)`;gates MinNotionalGate:`grid_order_info(cap, self.executor.gearing, ..., max_rate=1.0)`(docstring 同步);factory:`GridExecutor(..., gearing=config.grid_gearing, ...)`(去 leverage=)。

- [ ] **Step 4: 跑定向+受影响套件** `pytest tests/execution/ tests/runtime/ tests/state/ -q` Expected: 全 PASS(存量用例经向后兼容签名折算,行为逐位不变)

- [ ] **Step 5: Commit** `feat(execution): executor sizing 收敛为 gearing(向后兼容旧签名),gates/reconciler/factory 随迁`

### Task 3: DB 影响验证矩阵(用户点名重点)

**Files:**
- Test: `tests/execution/test_gearing_db_impact.py`(新建;复用 tests 现有 StateStore/FakeExchange fixture 风格)

**Interfaces:** Consumes Task 2 的 executor/gearing。纯测试任务——证明 `grids.leverage` 列行为惰性、新旧行共存安全。

- [ ] **Step 1: 写测试矩阵**(五个用例,全部先写、逐个跑通)

```python
"""DB 影响验证矩阵(spec 2026-07-07-account-leverage-gearing):
grids.leverage 列在 gearing 重构下行为惰性——旧行(5.0)/新行(3.4)/NULL 共存安全,
restore/补单/图表逐位连续,无迁移。"""
import pytest
from gridtrade.core.grid_engine import grid_order_info

# 用例1:旧行(leverage=5.0,order_num 持久化)restore → 补单尺寸=持久化真值,逐位不变
def test_restore_old_row_uses_persisted_order_num(store_with_old_grid):
    ex, grid_id, persisted = store_with_old_grid          # fixture: 5.0/0.68 时代开的格
    ex.reconciler.restore(grid_id)
    assert ex._geom[grid_id]['order_num'] == persisted     # 逐位,不经任何重算

# 用例2:旧行缺 order_num(回退重算路径)→ 新代码重算 == 旧代码重算(换元恒等)
def test_restore_fallback_recompute_equal():
    old = grid_order_info(302.0, 5.0, 10.0, 12.0, 20, 9.0, 13.0, max_rate=0.68)
    new = grid_order_info(302.0, 3.4, 10.0, 12.0, 20, 9.0, 13.0, max_rate=1.0)
    assert old['每笔数量'] == new['每笔数量']

# 用例3:新行 roundtrip——open 落库 leverage==gearing(3.4),restore 后几何连续
def test_new_row_roundtrip(fresh_executor_with_grid):
    ex, grid_id = fresh_executor_with_grid
    g = ex.grids.get(grid_id)
    assert abs(g.leverage - 3.4) < 1e-12
    before = dict(ex._geom[grid_id])
    ex._geom.pop(grid_id); ex.reconciler.restore(grid_id)
    assert ex._geom[grid_id] == before

# 用例4:图表——旧行(5.0)与新行(3.4)价格档逐位一致(价格档与杠杆无关)
def test_chart_lines_leverage_invariant(grid_row_factory):
    from gridtrade.dashboard.gridchart import _grid_lines
    old_row = grid_row_factory(leverage=5.0)
    new_row = grid_row_factory(leverage=3.4)
    assert _grid_lines(old_row) == _grid_lines(new_row) != []

# 用例5:leverage=NULL 史前行——图表不崩(返回 [],现行为保持)
def test_chart_null_leverage_safe(grid_row_factory):
    from gridtrade.dashboard.gridchart import _grid_lines
    assert _grid_lines(grid_row_factory(leverage=None)) == []
```

fixture 按 tests/execution 既有模式搭(StateStore 内存 + FakeExchange/FakeAdapter;`store_with_old_grid` 直接向 grids 表写 leverage=5.0 的行模拟旧部署产物)。

- [ ] **Step 2: 逐个跑通**(哪个红修哪个——预期全绿,因为设计就是惰性;任何红=发现真实耦合,停下报告)

- [ ] **Step 3: Commit** `test(execution): grids.leverage DB 影响验证矩阵——旧/新/NULL 行共存惰性实证`

### Task 4: 部署配置 + 记档 + 停在部署门

**Files:**
- Modify: `deploy/fly.toml`、`deploy/fly.prod.toml`、`docs/STATUS.md`

- [ ] **Step 1: 两 toml 更新**

fly.toml:`MAX_CONCURRENT = "12"`;`[env]` 加 `GRID_GEARING = "3.4"`、`ACCOUNT_LEVERAGE = "3.5"`。
fly.prod.toml:同上,并**删除 `CAP_EQUITY_FRAC = "0.10"` 行**(遗留即 boot 报错——这是特性不是事故,但部署配置必须先清)。

- [ ] **Step 2: STATUS.md gotchas 区追加**(仓位参数体系换代 + 无 DB 迁移依据 + AL=3.5 部署值与回测预期)

- [ ] **Step 3: 全套回归** `pytest -q` Expected: 全 PASS(数量 ≥ 652+新增)

- [ ] **Step 4: Commit** `feat(deploy): AL=3.5×12仓 部署配置(frac 0.1716),prod 清退 CAP_EQUITY_FRAC`

- [ ] **Step 5: 停——报用户批**。部署前核对单(报告里附):
  1. `fly secrets list -a gridtrade-hl / gridtrade-prod` **确认无 `LEVERAGE`/`CAP_EQUITY_FRAC` secret**(有则先 unset,否则部署即 boot 报错);
  2. 预告行为变化:cap testnet $98→$169、mainnet $304→$521,新格生效、存量格不变(order_num 持久化);
  3. testnet 先行,production 单独批。

## Self-Review

- Spec 覆盖:新参数/推导(T1)✓ 旧键报错(T1)✓ executor/gates/reconciler/factory(T2)✓ DB 矩阵五用例=spec 测试④(T3)✓ toml/prod 清退(T4)✓ 引擎/回测不动=无任务 ✓ 无迁移=无任务 ✓。
- 占位符:无。
- 签名一致:`derive_frac(al, n, gearing)` T1 定义 T1 测试同名;`GridExecutor(gearing=)` T2 定义 T3 fixture 使用;`_grid_lines` 为 gridchart 现有函数(实读确认)。
- 风险点已卡:prod toml 清退与代码同 commit 链;fly secrets 预检入部署核对单。
