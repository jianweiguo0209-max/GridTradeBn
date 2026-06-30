# Dashboard 第三期（复盘分析）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 Dashboard 加复盘分析——权益/盈亏曲线、tag 盈亏归因、成交分布、退出原因统计，全部服务端内联 SVG；并把真实平台手续费铺进表格。

**Architecture:** 新增 `equity_snapshots` 表（monitor 节流写真权益，P3 唯一新写）；`dashboard/analytics.py` 只读聚合（order_records/grid_fills/equity_snapshots）；`dashboard/charts.py` 纯函数生成内联 SVG；web 加 `GET /analytics`（登录门控、只读）。web 仍零写，唯一新写在 monitor（单一写者），SVG 零 JS。

**Tech Stack:** Python 3.9 / FastAPI / Jinja2 / 内联 SVG / SQLAlchemy 2.0 Core / pytest（双后端 fixture）。

## Global Constraints

- Python 3.9；测试命令 `TZ=Asia/Shanghai .venv/bin/python -m pytest`。
- DB 测试用 `tests/conftest.py` 的 `store` fixture（默认内存 SQLite；`TEST_DATABASE_URL` 走 PG）。
- 时间戳一律 UTC 毫秒整数，用 `gridtrade.state.models.now_ms`。
- **web 进程零写**：`/analytics` 与 analytics.py 只读 order_records/grid_fills/equity_snapshots；P3 唯一新写在 monitor（`equity_snapshots`）。
- 图表全部**服务端内联 SVG**，纯函数生成、零新 JS、不走 CDN；空数据优雅退化（占位 SVG，不抛异常）。
- monitor 快照写是「尽力而为」旁路：取余额失败/限频 try/except 跳过、记日志，**绝不崩 cycle**。
- 新表 `equity_snapshots` 随 `store.create_all()` 启动自动建（幂等）；无需 ALTER/migrate（`grid_fills.fee` 已迁好）。
- 所有新参数默认 off/None → 既有 monitor/web/测试不回归。
- 模板内文本经 Jinja autoescape，无 `|safe`。

---

### Task 1: equity_snapshots 表 + 数据类

**Files:**
- Modify: `gridtrade/state/models.py`（heartbeats/control 表区后追加表；数据类区追加 dataclass）
- Test: `tests/state/test_equity_models.py`

**Interfaces:**
- Consumes: `gridtrade.state.models`（`metadata`, `BigInteger/Column/Float/Index/String/Table`, `now_ms`, `Optional`）。
- Produces:
  - 表 `equity_snapshots`（列 `id` String PK / `ts` BigInteger / `equity` Float / `cash` Float nullable；Index `ix_equity_snapshots_ts` on `ts`）。
  - `@dataclass EquitySnapshot(id: str, ts: int, equity: float, cash: Optional[float] = None)`

- [ ] **Step 1: Write the failing test**

```python
# tests/state/test_equity_models.py
from gridtrade.state.models import equity_snapshots, EquitySnapshot, metadata


def test_equity_snapshots_table_registered():
    assert 'equity_snapshots' in metadata.tables
    cols = set(metadata.tables['equity_snapshots'].columns.keys())
    assert {'id', 'ts', 'equity', 'cash'} <= cols


def test_equity_snapshot_dataclass_defaults():
    s = EquitySnapshot(id='s1', ts=1000, equity=499.0)
    assert s.cash is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_equity_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'equity_snapshots'`

- [ ] **Step 3: Write minimal implementation**

在 `gridtrade/state/models.py` 的 `control_audit` 表定义之后追加：

```python
equity_snapshots = Table(
    'equity_snapshots', metadata,
    Column('id', String, primary_key=True),
    Column('ts', BigInteger, nullable=False),
    Column('equity', Float, nullable=False),
    Column('cash', Float, nullable=True),
    Index('ix_equity_snapshots_ts', 'ts'),
)
```

在数据类区（`AuditEntry` 之后）追加：

```python
@dataclass
class EquitySnapshot:
    id: str
    ts: int
    equity: float
    cash: Optional[float] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_equity_models.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/state/models.py tests/state/test_equity_models.py
git commit -m "feat(analytics): equity_snapshots 表 + EquitySnapshot 数据类"
```

---

### Task 2: EquitySnapshotRepository（节流写 + 范围读）

**Files:**
- Create: `gridtrade/state/equity.py`
- Test: `tests/state/test_equity_repo.py`

**Interfaces:**
- Consumes: `gridtrade.state.models`（`equity_snapshots`, `EquitySnapshot`, `now_ms`）。
- Produces:
  - `class EquitySnapshotRepository(store)`：
    - `latest_ts() -> Optional[int]`（最新快照 ts，无则 None）
    - `add_if_due(equity: float, cash: Optional[float] = None, *, interval_sec: int, now_ms_fn=now_ms) -> bool`（无快照或 `now - latest >= interval_sec*1000` 才插入；返回是否写入）
    - `list_range(start_ms: int, end_ms: Optional[int] = None) -> List[EquitySnapshot]`（ts 升序）

- [ ] **Step 1: Write the failing test**

```python
# tests/state/test_equity_repo.py
from gridtrade.state.equity import EquitySnapshotRepository


def test_add_if_due_throttles(store):
    repo = EquitySnapshotRepository(store)
    t = [1_000_000]
    assert repo.add_if_due(499.0, 400.0, interval_sec=300, now_ms_fn=lambda: t[0]) is True
    # 间隔内（+100s）不写
    t[0] = 1_100_000
    assert repo.add_if_due(500.0, None, interval_sec=300, now_ms_fn=lambda: t[0]) is False
    # 超间隔（+300s）才写
    t[0] = 1_300_000
    assert repo.add_if_due(501.0, None, interval_sec=300, now_ms_fn=lambda: t[0]) is True
    rows = repo.list_range(0)
    assert [r.equity for r in rows] == [499.0, 501.0]      # 升序，只 2 行
    assert repo.latest_ts() == 1_300_000


def test_list_range_filters(store):
    repo = EquitySnapshotRepository(store)
    for ts in (1000, 2000, 3000):
        repo.add_if_due(float(ts), None, interval_sec=0, now_ms_fn=lambda ts=ts: ts)
    assert [r.ts for r in repo.list_range(2000)] == [2000, 3000]
    assert [r.ts for r in repo.list_range(1000, 2000)] == [1000, 2000]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_equity_repo.py -v`
Expected: FAIL — `ModuleNotFoundError: gridtrade.state.equity`

- [ ] **Step 3: Write minimal implementation**

```python
# gridtrade/state/equity.py
"""EquitySnapshotRepository：monitor 节流写真权益快照（节流逻辑落 DB，重启安全）。"""
import uuid
from typing import List, Optional

from sqlalchemy import insert, select

from gridtrade.state.models import equity_snapshots, EquitySnapshot, now_ms

_FIELDS = ('id', 'ts', 'equity', 'cash')


def _to_snap(row) -> EquitySnapshot:
    m = row._mapping
    return EquitySnapshot(**{f: m[f] for f in _FIELDS})


class EquitySnapshotRepository:
    def __init__(self, store):
        self.engine = store.engine

    def latest_ts(self) -> Optional[int]:
        with self.engine.connect() as c:
            row = c.execute(
                select(equity_snapshots.c.ts)
                .order_by(equity_snapshots.c.ts.desc()).limit(1)
            ).first()
        return int(row[0]) if row is not None else None

    def add_if_due(self, equity: float, cash: Optional[float] = None, *,
                   interval_sec: int, now_ms_fn=now_ms) -> bool:
        now = now_ms_fn()
        latest = self.latest_ts()
        if latest is not None and now - latest < interval_sec * 1000:
            return False
        with self.engine.begin() as c:
            c.execute(insert(equity_snapshots), {
                'id': uuid.uuid4().hex, 'ts': now, 'equity': float(equity),
                'cash': None if cash is None else float(cash),
            })
        return True

    def list_range(self, start_ms: int,
                   end_ms: Optional[int] = None) -> List[EquitySnapshot]:
        q = select(equity_snapshots).where(equity_snapshots.c.ts >= start_ms)
        if end_ms is not None:
            q = q.where(equity_snapshots.c.ts <= end_ms)
        with self.engine.connect() as c:
            rows = c.execute(q.order_by(equity_snapshots.c.ts)).all()
        return [_to_snap(r) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_equity_repo.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/state/equity.py tests/state/test_equity_repo.py
git commit -m "feat(analytics): EquitySnapshotRepository（add_if_due 节流 + list_range）"
```

---

### Task 3: monitor 节流写快照 + factory/config 接线

**Files:**
- Modify: `gridtrade/config.py`（DeployConfig 加 `equity_snapshot_interval_sec`）
- Modify: `gridtrade/runtime/cycles.py`（`run_monitor_cycle` 末尾写快照）
- Modify: `gridtrade/runtime/factory.py`（Runtime 加 `equity`）
- Modify: `gridtrade/runtime/monitor.py`（传 `equity_repo`/`snapshot_interval_sec` 进 cycle）
- Test: `tests/runtime/test_equity_snapshot_cycle.py`

**Interfaces:**
- Consumes: `EquitySnapshotRepository`（Task 2）；`manager.executor.adapter.fetch_balance() -> Balance(equity, cash)`。
- Produces:
  - `config.DeployConfig` 加 `equity_snapshot_interval_sec: float = 300.0`；`load_deploy_config` 读 `EQUITY_SNAPSHOT_INTERVAL_SEC` 默认 300。
  - `run_monitor_cycle(reconciler, manager, log=print, *, flags=None, commands=None, audit=None, exchange='', equity_repo=None, snapshot_interval_sec=300)`——末尾若 `equity_repo` 非 None：try 取 `manager.executor.adapter.fetch_balance()` 调 `add_if_due`；except 记日志跳过。
  - `factory.Runtime` 加 `equity` 字段（`EquitySnapshotRepository(store)`）。

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_equity_snapshot_cycle.py
from gridtrade.runtime.cycles import run_monitor_cycle
from gridtrade.state.equity import EquitySnapshotRepository
from gridtrade.exchanges.base import Balance


class _Grids:
    def list_active(self): return []
class _Adapter:
    def __init__(self, raise_=False): self._raise = raise_
    def fetch_balance(self):
        if self._raise: raise RuntimeError('rate limited')
        return Balance(equity=499.0, cash=400.0)
class _Executor:
    def __init__(self, adapter): self.grids = _Grids(); self.adapter = adapter
    def is_loaded(self, gid): return True
class _Manager:
    def __init__(self, adapter): self.executor = _Executor(adapter)
    def monitor_all(self, skip_replenish=False): return []
class _Reconciler:
    def __init__(self, ex): self.ex = ex


def test_cycle_writes_equity_snapshot(store):
    repo = EquitySnapshotRepository(store)
    mgr = _Manager(_Adapter())
    run_monitor_cycle(_Reconciler(mgr.executor), mgr, equity_repo=repo,
                      snapshot_interval_sec=0)
    rows = repo.list_range(0)
    assert len(rows) == 1 and rows[0].equity == 499.0


def test_cycle_survives_balance_error(store):
    repo = EquitySnapshotRepository(store)
    mgr = _Manager(_Adapter(raise_=True))
    # 取余额抛错不应让 cycle 崩
    run_monitor_cycle(_Reconciler(mgr.executor), mgr, equity_repo=repo,
                      snapshot_interval_sec=0)
    assert repo.list_range(0) == []        # 没写，但没崩
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_equity_snapshot_cycle.py -v`
Expected: FAIL — `TypeError: run_monitor_cycle() got an unexpected keyword argument 'equity_repo'`

- [ ] **Step 3: Write minimal implementation**

(a) `gridtrade/config.py`：`DeployConfig` 加字段（在 `scheduler_run_on_start` 等之后）：

```python
    equity_snapshot_interval_sec: float = 300.0
```

`load_deploy_config` 的 `DeployConfig(...)` 加：

```python
        equity_snapshot_interval_sec=_f(env, 'EQUITY_SNAPSHOT_INTERVAL_SEC', 300.0),
```

(b) `gridtrade/runtime/cycles.py`：`run_monitor_cycle` 签名加 `equity_repo=None, snapshot_interval_sec=300`；在 `return` 之前（消费指令之后）加：

```python
    if equity_repo is not None:
        try:
            bal = manager.executor.adapter.fetch_balance()
            equity_repo.add_if_due(bal.equity, getattr(bal, 'cash', None),
                                   interval_sec=int(snapshot_interval_sec))
        except Exception as exc:
            log('[monitor] equity snapshot skipped: %r' % exc)
```

(c) `gridtrade/runtime/factory.py`：`Runtime` dataclass 加 `equity: object = None`；`build_runtime` 的 `return Runtime(...)` 加 `equity=EquitySnapshotRepository(store)`，并在文件顶部 import：`from gridtrade.state.equity import EquitySnapshotRepository`。

(d) `gridtrade/runtime/monitor.py`：`run_monitor` 里组 `ctrl_kw` 的分支（`if cycle_fn is run_monitor_cycle:`）追加：

```python
            ctrl_kw['equity_repo'] = rt.equity
            ctrl_kw['snapshot_interval_sec'] = rt.config.equity_snapshot_interval_sec
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_equity_snapshot_cycle.py -v`
然后回归：`TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime -q`
Expected: PASS（新测 2 passed；runtime 既有不回归——新参可选）

- [ ] **Step 5: Commit**

```bash
git add gridtrade/config.py gridtrade/runtime/cycles.py gridtrade/runtime/factory.py gridtrade/runtime/monitor.py tests/runtime/test_equity_snapshot_cycle.py
git commit -m "feat(analytics): monitor 节流写 equity 快照（容错不崩 + factory/config 接线）"
```

---

### Task 4: 真实手续费铺表（RecentFill/GridOverviewRow + 模板）

**Files:**
- Modify: `gridtrade/dashboard/queries.py`（`RecentFill` 加 `fee`；`GridOverviewRow` 加 `fee_paid`；构造处补值）
- Modify: `gridtrade/dashboard/templates/detail.html`（Recent Fills 加 fee 列）
- Modify: `gridtrade/dashboard/templates/history.html`（全局成交流加 fee 列）
- Modify: `gridtrade/dashboard/templates/overview.html`（加每网格累计 fee 列）
- Test: `tests/dashboard/test_fee_in_tables.py`

**Interfaces:**
- Consumes: `grid_fills.fee`（已存在）；`Fill.fee`（FillRepository 已带）；`AccountingRepository.get(grid_id).fee_paid`。
- Produces:
  - `RecentFill` 加 `fee: float = 0.0`；`build_records` 构造 RecentFill 时 `fee=f._mapping['fee']`。
  - `GridOverviewRow` 加 `fee_paid: float = 0.0`；`build_overview` 取 `acc.fee_paid`。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_fee_in_tables.py
from gridtrade.dashboard.queries import build_records, build_overview
from gridtrade.state.fills import FillRepository
from gridtrade.state.grids import GridRepository
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.models import Fill, Grid, ACTIVE


class _PriceAdapter:
    def fetch_price(self, s): return 100.0


def test_recent_fill_carries_fee(store):
    FillRepository(store).add_if_new(Fill(trade_id='t1', grid_id='g1', line_index=0,
                                          side='buy', price=90.0, size=1.0, fee=0.27, ts=5000))
    dto = build_records(store)
    assert dto.recent_fills[0].fee == 0.27


def test_overview_row_carries_cumulative_fee(store):
    GridRepository(store).create(Grid(id='g1', exchange='hyperliquid',
                                      symbol='BTC/USDT:USDT', status=ACTIVE))
    accs = AccountingRepository(store); accs.init('g1')
    acc = accs.get('g1'); acc.fee_paid = 1.23; accs.save(acc)
    rows = build_overview(store, _PriceAdapter())
    assert rows[0].fee_paid == 1.23
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_fee_in_tables.py -v`
Expected: FAIL — `AttributeError: 'RecentFill' object has no attribute 'fee'`（或构造 TypeError）

- [ ] **Step 3: Write minimal implementation**

`queries.py`：
- `RecentFill` dataclass 加字段 `fee: float = 0.0`。
- `build_records` 构造 `RecentFill(...)` 处加 `fee=f._mapping['fee']`。
- `GridOverviewRow` dataclass 加字段 `fee_paid: float = 0.0`。
- `build_overview` 里取 `fee = acc.fee_paid if acc else 0.0`，构造 `GridOverviewRow(...)` 加 `fee_paid=fee`。

`detail.html`：Recent Fills 表头加 `<th>fee</th>`，每行加 `<td>{{ f.fee | fmt_num }}</td>`。
`history.html`：全局成交流表头加 `<th>fee</th>`，每行加 `<td>{{ f.fee | fmt_num }}</td>`。
`overview.html`：表头加 `<th>fee</th>`，每行加 `<td>{{ r.fee_paid | fmt_num }}</td>`。

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_fee_in_tables.py -v`
然后全 dashboard 回归：`TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard -q`
Expected: PASS（新测 2 passed；P1/P2 dashboard 测试不回归）

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/queries.py gridtrade/dashboard/templates/detail.html gridtrade/dashboard/templates/history.html gridtrade/dashboard/templates/overview.html tests/dashboard/test_fee_in_tables.py
git commit -m "feat(analytics): 真实手续费铺表（成交流水 fee 列 + 总览每网格累计 fee）"
```

---

### Task 5: SVG 图表纯函数（charts.py）

**Files:**
- Create: `gridtrade/dashboard/charts.py`
- Test: `tests/dashboard/test_charts.py`

**Interfaces:**
- Produces（纯函数，确定坐标映射）：
  - `def line_chart(series: List[List[tuple]], *, width: int = 720, height: int = 200, pad: int = 10) -> str`——`series` 为多条线，每条是 `[(x, y), ...]`（x/y 数值）。所有点归一化：x 映射到 `[pad, width-pad]`，y 映射到 `[height-pad, pad]`（上小下大，故高值在上）。每条线一个 `<polyline>`。空（所有 series 空）返回占位 SVG。
  - `def bar_chart(bars: List[tuple], *, width: int = 720, height: int = 200, pad: int = 10) -> str`——`bars` 为 `[(label, value), ...]`，按最大值归一化高度，每个一个 `<rect>`。空返回占位。
  - `def stacked_bar(groups: List[tuple], *, width: int = 720, height: int = 200, pad: int = 10) -> str`——`groups` 为 `[(label, [(seg, value), ...]), ...]`，每组堆叠 `<rect>`。空返回占位。
  - 占位：`'<svg ...><text ...>暂无数据</text></svg>'`。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_charts.py
from gridtrade.dashboard.charts import line_chart, bar_chart, stacked_bar


def test_line_chart_maps_points():
    # 单线两点 (0,0),(10,10)；width=height=100,pad=10 → x:0->10,10->90; y:0->90,10->10
    svg = line_chart([[(0, 0), (10, 10)]], width=100, height=100, pad=10)
    assert '<svg' in svg and '<polyline' in svg
    assert '10.0,90.0' in svg and '90.0,10.0' in svg


def test_line_chart_empty_placeholder():
    svg = line_chart([], width=100, height=100)
    assert '暂无数据' in svg and '<polyline' not in svg


def test_bar_chart_rects():
    svg = bar_chart([('a', 5.0), ('b', 10.0)], width=100, height=100, pad=10)
    assert svg.count('<rect') == 2
    # 最大值 10 → 满高(80)，5 → 半高(40)
    assert 'height="80.0"' in svg and 'height="40.0"' in svg


def test_bar_chart_empty():
    assert '暂无数据' in bar_chart([], width=100, height=100)


def test_stacked_bar_segments():
    svg = stacked_bar([('g1', [('buy', 3.0), ('sell', 1.0)])], width=100, height=100, pad=10)
    assert svg.count('<rect') == 2     # 两段堆叠
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_charts.py -v`
Expected: FAIL — `ModuleNotFoundError: gridtrade.dashboard.charts`

- [ ] **Step 3: Write minimal implementation**

```python
# gridtrade/dashboard/charts.py
"""服务端内联 SVG 图表：纯函数，确定坐标映射，可单测。空数据返回占位。"""
from typing import List, Tuple


def _placeholder(width: int, height: int) -> str:
    return ('<svg viewBox="0 0 %d %d" class="chart">'
            '<text x="%d" y="%d" text-anchor="middle" fill="#999">暂无数据</text>'
            '</svg>' % (width, height, width // 2, height // 2))


def line_chart(series: List[List[Tuple]], *, width: int = 720, height: int = 200,
               pad: int = 10) -> str:
    pts = [p for s in series for p in s]
    if not pts:
        return _placeholder(width, height)
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    dx = (xmax - xmin) or 1.0
    dy = (ymax - ymin) or 1.0
    iw = width - 2 * pad
    ih = height - 2 * pad

    def sx(x): return pad + (x - xmin) / dx * iw
    def sy(y): return pad + (ymax - y) / dy * ih    # 高值在上

    polylines = []
    for s in series:
        if not s:
            continue
        coords = ' '.join('%.1f,%.1f' % (sx(x), sy(y)) for x, y in s)
        polylines.append('<polyline fill="none" stroke="#6cf" stroke-width="1.5" '
                         'points="%s"/>' % coords)
    return ('<svg viewBox="0 0 %d %d" class="chart">%s</svg>'
            % (width, height, ''.join(polylines)))


def bar_chart(bars: List[Tuple], *, width: int = 720, height: int = 200,
              pad: int = 10) -> str:
    if not bars:
        return _placeholder(width, height)
    vmax = max(abs(v) for _, v in bars) or 1.0
    iw = width - 2 * pad
    ih = height - 2 * pad
    n = len(bars)
    bw = iw / n * 0.7
    gap = iw / n
    rects = []
    for i, (_label, v) in enumerate(bars):
        h = abs(v) / vmax * ih
        x = pad + i * gap + (gap - bw) / 2
        y = pad + (ih - h)
        rects.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#4caf50"/>'
                     % (x, y, bw, h))
    return ('<svg viewBox="0 0 %d %d" class="chart">%s</svg>'
            % (width, height, ''.join(rects)))


def stacked_bar(groups: List[Tuple], *, width: int = 720, height: int = 200,
                pad: int = 10) -> str:
    if not groups:
        return _placeholder(width, height)
    totals = [sum(abs(v) for _, v in segs) for _, segs in groups]
    vmax = max(totals) or 1.0
    iw = width - 2 * pad
    ih = height - 2 * pad
    n = len(groups)
    bw = iw / n * 0.7
    gap = iw / n
    colors = ['#4caf50', '#e53935', '#6cf', '#fb0']
    rects = []
    for i, (_label, segs) in enumerate(groups):
        x = pad + i * gap + (gap - bw) / 2
        y_bottom = pad + ih
        for j, (_seg, v) in enumerate(segs):
            h = abs(v) / vmax * ih
            y_bottom -= h
            rects.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>'
                         % (x, y_bottom, bw, h, colors[j % len(colors)]))
    return ('<svg viewBox="0 0 %d %d" class="chart">%s</svg>'
            % (width, height, ''.join(rects)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_charts.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/charts.py tests/dashboard/test_charts.py
git commit -m "feat(analytics): charts.py 纯函数 SVG（line/bar/stacked + 空数据占位）"
```

---

### Task 6: analytics.py — 权益/已实现曲线序列

**Files:**
- Create: `gridtrade/dashboard/analytics.py`
- Test: `tests/dashboard/test_analytics_curves.py`

**Interfaces:**
- Consumes: 直读 `order_records`（`closed_at`, `total_pnl`）；`EquitySnapshotRepository.list_range`。
- Produces（追加到 analytics.py）：
  - `def realized_curve(store, *, start_ms: int = 0) -> List[tuple]`——order_records 中 `closed_at >= start_ms` 且非 None，按 closed_at 升序累加 total_pnl，返回 `[(closed_at, cum_pnl), ...]`。
  - `def equity_curve(store, *, start_ms: int = 0) -> List[tuple]`——`EquitySnapshotRepository(store).list_range(start_ms)` → `[(ts, equity), ...]`。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_analytics_curves.py
from gridtrade.dashboard.analytics import realized_curve, equity_curve
from gridtrade.state.records import RecordRepository
from gridtrade.state.equity import EquitySnapshotRepository
from gridtrade.state.models import Record


def test_realized_curve_cumulative(store):
    recs = RecordRepository(store)
    recs.add(Record(id='r1', exchange='x', symbol='BTC', tag='gt0', total_pnl=10.0, closed_at=1000))
    recs.add(Record(id='r2', exchange='x', symbol='ETH', tag='gt0', total_pnl=-4.0, closed_at=2000))
    recs.add(Record(id='open', exchange='x', symbol='SOL', tag='gt0', closed_at=None))  # 未平不计
    assert realized_curve(store) == [(1000, 10.0), (2000, 6.0)]
    assert realized_curve(store, start_ms=1500) == [(2000, -4.0)]   # 范围过滤后从该窗起累加


def test_equity_curve(store):
    repo = EquitySnapshotRepository(store)
    repo.add_if_due(499.0, None, interval_sec=0, now_ms_fn=lambda: 1000)
    repo.add_if_due(505.0, None, interval_sec=0, now_ms_fn=lambda: 2000)
    assert equity_curve(store) == [(1000, 499.0), (2000, 505.0)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_analytics_curves.py -v`
Expected: FAIL — `ModuleNotFoundError: gridtrade.dashboard.analytics`

- [ ] **Step 3: Write minimal implementation**

```python
# gridtrade/dashboard/analytics.py
"""只读复盘聚合：曲线/归因/分布/退出原因。纯计算，不写库、不调行情。"""
from typing import List, Tuple

from sqlalchemy import select

from gridtrade.state.equity import EquitySnapshotRepository
from gridtrade.state.models import order_records


def realized_curve(store, *, start_ms: int = 0) -> List[Tuple]:
    with store.engine.connect() as c:
        rows = c.execute(
            select(order_records.c.closed_at, order_records.c.total_pnl)
            .where(order_records.c.closed_at.isnot(None),
                   order_records.c.closed_at >= start_ms)
            .order_by(order_records.c.closed_at)
        ).all()
    out = []
    cum = 0.0
    for closed_at, pnl in rows:
        cum += (pnl or 0.0)
        out.append((int(closed_at), cum))
    return out


def equity_curve(store, *, start_ms: int = 0) -> List[Tuple]:
    snaps = EquitySnapshotRepository(store).list_range(start_ms)
    return [(s.ts, s.equity) for s in snaps]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_analytics_curves.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/analytics.py tests/dashboard/test_analytics_curves.py
git commit -m "feat(analytics): realized_curve + equity_curve 序列"
```

---

### Task 7: analytics.py — tag 盈亏归因

**Files:**
- Modify: `gridtrade/dashboard/analytics.py`
- Test: `tests/dashboard/test_analytics_tag.py`

**Interfaces:**
- Consumes: 直读 `order_records`（`tag`, `total_pnl`, `pnl_ratio`, `opened_at`, `closed_at`）；`grid_fills`（`grid_id`, `fee`）；`order_records.grid_id` 关联 fee。
- Produces:
  - `@dataclass TagStat(tag, count, total_pnl, total_fee, net_pnl, win_count, win_rate, avg_hold_ms, max_drawdown)`
  - `def tag_attribution(store, *, start_ms: int = 0) -> List[TagStat]`——按 tag 聚合已平记录（closed_at>=start_ms）：count、total_pnl（sum）、win_count（total_pnl>0）、win_rate、avg_hold_ms（closed_at-opened_at 均值，缺则跳过）、max_drawdown（该 tag 累计已实现曲线的峰值回撤）、total_fee（该 tag 各 grid_id 的 grid_fills.fee 之和）、net_pnl（total_pnl - total_fee）。按 tag 升序。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_analytics_tag.py
from gridtrade.dashboard.analytics import tag_attribution
from gridtrade.state.records import RecordRepository
from gridtrade.state.fills import FillRepository
from gridtrade.state.models import Record, Fill


def test_tag_attribution(store):
    recs = RecordRepository(store)
    recs.add(Record(id='r1', exchange='x', symbol='BTC', tag='gt0', grid_id='g1',
                    total_pnl=10.0, opened_at=1000, closed_at=4000))
    recs.add(Record(id='r2', exchange='x', symbol='ETH', tag='gt0', grid_id='g2',
                    total_pnl=-4.0, opened_at=2000, closed_at=5000))
    fills = FillRepository(store)
    fills.add_if_new(Fill(trade_id='t1', grid_id='g1', line_index=0, side='buy',
                          price=1.0, size=1.0, fee=0.3, ts=1500))
    fills.add_if_new(Fill(trade_id='t2', grid_id='g2', line_index=0, side='sell',
                          price=1.0, size=1.0, fee=0.2, ts=2500))
    s = {t.tag: t for t in tag_attribution(store)}['gt0']
    assert s.count == 2
    assert s.total_pnl == 6.0
    assert s.total_fee == 0.5
    assert round(s.net_pnl, 4) == 5.5            # 6.0 - 0.5
    assert s.win_count == 1 and round(s.win_rate, 4) == 0.5
    assert s.avg_hold_ms == 3000                 # (3000+3000)/2
    assert round(s.max_drawdown, 4) == 4.0       # 峰值 10 → 谷 6，回撤 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_analytics_tag.py -v`
Expected: FAIL — `ImportError: cannot import name 'tag_attribution'`

- [ ] **Step 3: Write minimal implementation** (append to `analytics.py`)

```python
# --- 追加到 gridtrade/dashboard/analytics.py ---
from dataclasses import dataclass
from typing import Optional

from gridtrade.state.models import grid_fills


@dataclass
class TagStat:
    tag: str
    count: int
    total_pnl: float
    total_fee: float
    net_pnl: float
    win_count: int
    win_rate: float
    avg_hold_ms: Optional[float]
    max_drawdown: float


def _max_drawdown(cum_series) -> float:
    peak = float('-inf')
    mdd = 0.0
    for v in cum_series:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd if mdd != float('-inf') else 0.0


def tag_attribution(store, *, start_ms: int = 0) -> List[TagStat]:
    with store.engine.connect() as c:
        recs = c.execute(
            select(order_records.c.tag, order_records.c.grid_id,
                   order_records.c.total_pnl, order_records.c.opened_at,
                   order_records.c.closed_at)
            .where(order_records.c.closed_at.isnot(None),
                   order_records.c.closed_at >= start_ms)
            .order_by(order_records.c.closed_at)
        ).all()
        fee_rows = c.execute(
            select(grid_fills.c.grid_id, grid_fills.c.fee)
        ).all()
    fee_by_grid = {}
    for gid, fee in fee_rows:
        fee_by_grid[gid] = fee_by_grid.get(gid, 0.0) + (fee or 0.0)

    agg = {}
    for tag, gid, pnl, opened, closed in recs:
        a = agg.setdefault(tag, {'count': 0, 'pnl': 0.0, 'win': 0, 'fee': 0.0,
                                 'holds': [], 'cum': [], 'run': 0.0})
        a['count'] += 1
        a['pnl'] += (pnl or 0.0)
        if (pnl or 0.0) > 0:
            a['win'] += 1
        a['fee'] += fee_by_grid.get(gid, 0.0)
        if opened is not None and closed is not None:
            a['holds'].append(closed - opened)
        a['run'] += (pnl or 0.0)
        a['cum'].append(a['run'])

    out = []
    for tag in sorted(agg):
        a = agg[tag]
        avg_hold = (sum(a['holds']) / len(a['holds'])) if a['holds'] else None
        out.append(TagStat(
            tag=tag, count=a['count'], total_pnl=a['pnl'], total_fee=a['fee'],
            net_pnl=a['pnl'] - a['fee'], win_count=a['win'],
            win_rate=(a['win'] / a['count'] if a['count'] else 0.0),
            avg_hold_ms=avg_hold, max_drawdown=_max_drawdown(a['cum'])))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_analytics_tag.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/analytics.py tests/dashboard/test_analytics_tag.py
git commit -m "feat(analytics): tag_attribution（总盈亏/fee/净盈亏/胜率/持仓/回撤）"
```

---

### Task 8: analytics.py — 成交分布（四维）+ 退出原因统计

**Files:**
- Modify: `gridtrade/dashboard/analytics.py`
- Test: `tests/dashboard/test_analytics_dist.py`

**Interfaces:**
- Consumes: 直读 `grid_fills`（`side`, `line_index`, `fee`, `ts`）；`order_records`（`exit_reason`, `total_pnl`, `closed_at`）。
- Produces:
  - `@dataclass FillDist(by_hour: List[tuple], by_side: List[tuple], by_line: List[tuple], fee_cum: List[tuple])`——`by_hour`：按 `ts // 3600000` 桶计数 `[(hour_bucket, count), ...]` 升序；`by_side`：`[('buy', n), ('sell', n)]`；`by_line`：`[(line_index, count), ...]` 按 line 升序；`fee_cum`：按 ts 升序累加 fee `[(ts, cum_fee), ...]`。
  - `def fill_distribution(store, *, start_ms: int = 0) -> FillDist`
  - `@dataclass ExitStat(reason, count, share, avg_pnl)`
  - `def exit_reason_stats(store, *, start_ms: int = 0) -> List[ExitStat]`——按 exit_reason 计数、占比（count/总数）、平均 total_pnl。按 count 降序。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_analytics_dist.py
from gridtrade.dashboard.analytics import fill_distribution, exit_reason_stats
from gridtrade.state.fills import FillRepository
from gridtrade.state.records import RecordRepository
from gridtrade.state.models import Fill, Record


def test_fill_distribution(store):
    f = FillRepository(store)
    f.add_if_new(Fill(trade_id='t1', grid_id='g', line_index=0, side='buy', price=1, size=1, fee=0.1, ts=1000))
    f.add_if_new(Fill(trade_id='t2', grid_id='g', line_index=1, side='sell', price=1, size=1, fee=0.2, ts=3_600_000 + 1000))
    f.add_if_new(Fill(trade_id='t3', grid_id='g', line_index=0, side='buy', price=1, size=1, fee=0.3, ts=3_600_000 + 2000))
    d = fill_distribution(store)
    assert dict(d.by_side) == {'buy': 2, 'sell': 1}
    assert dict(d.by_line) == {0: 2, 1: 1}
    assert d.fee_cum[-1][1] == 0.6                  # 累计费 0.1+0.2+0.3
    assert len(d.by_hour) == 2                      # 两个小时桶


def test_exit_reason_stats(store):
    recs = RecordRepository(store)
    recs.add(Record(id='r1', exchange='x', symbol='B', tag='t', total_pnl=10.0,
                    exit_reason='take_profit', closed_at=1000))
    recs.add(Record(id='r2', exchange='x', symbol='E', tag='t', total_pnl=-4.0,
                    exit_reason='stop_loss', closed_at=2000))
    recs.add(Record(id='r3', exchange='x', symbol='S', tag='t', total_pnl=6.0,
                    exit_reason='take_profit', closed_at=3000))
    by = {s.reason: s for s in exit_reason_stats(store)}
    assert by['take_profit'].count == 2
    assert round(by['take_profit'].share, 4) == round(2/3, 4)
    assert by['take_profit'].avg_pnl == 8.0         # (10+6)/2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_analytics_dist.py -v`
Expected: FAIL — `ImportError: cannot import name 'fill_distribution'`

- [ ] **Step 3: Write minimal implementation** (append to `analytics.py`)

```python
# --- 追加到 gridtrade/dashboard/analytics.py ---
@dataclass
class FillDist:
    by_hour: List[Tuple]
    by_side: List[Tuple]
    by_line: List[Tuple]
    fee_cum: List[Tuple]


def fill_distribution(store, *, start_ms: int = 0) -> FillDist:
    with store.engine.connect() as c:
        rows = c.execute(
            select(grid_fills.c.side, grid_fills.c.line_index,
                   grid_fills.c.fee, grid_fills.c.ts)
            .where(grid_fills.c.ts >= start_ms)
            .order_by(grid_fills.c.ts)
        ).all()
    hour = {}; side = {}; line = {}
    fee_cum = []; run = 0.0
    for s, li, fee, ts in rows:
        hb = int(ts) // 3_600_000
        hour[hb] = hour.get(hb, 0) + 1
        side[s] = side.get(s, 0) + 1
        line[li] = line.get(li, 0) + 1
        run += (fee or 0.0)
        fee_cum.append((int(ts), run))
    by_side = [(k, side.get(k, 0)) for k in ('buy', 'sell') if k in side]
    return FillDist(
        by_hour=sorted(hour.items()),
        by_side=by_side,
        by_line=sorted(line.items()),
        fee_cum=fee_cum)


@dataclass
class ExitStat:
    reason: str
    count: int
    share: float
    avg_pnl: float


def exit_reason_stats(store, *, start_ms: int = 0) -> List[ExitStat]:
    with store.engine.connect() as c:
        rows = c.execute(
            select(order_records.c.exit_reason, order_records.c.total_pnl)
            .where(order_records.c.closed_at.isnot(None),
                   order_records.c.closed_at >= start_ms)
        ).all()
    agg = {}
    for reason, pnl in rows:
        r = reason or 'unknown'
        a = agg.setdefault(r, {'count': 0, 'pnl': 0.0})
        a['count'] += 1
        a['pnl'] += (pnl or 0.0)
    total = sum(a['count'] for a in agg.values()) or 1
    out = [ExitStat(reason=r, count=a['count'], share=a['count'] / total,
                    avg_pnl=a['pnl'] / a['count'] if a['count'] else 0.0)
           for r, a in agg.items()]
    out.sort(key=lambda s: s.count, reverse=True)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_analytics_dist.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/analytics.py tests/dashboard/test_analytics_dist.py
git commit -m "feat(analytics): fill_distribution（四维）+ exit_reason_stats"
```

---

### Task 9: web /analytics 路由 + 模板 + 范围过滤 + 导航

**Files:**
- Modify: `gridtrade/dashboard/app.py`（`GET /analytics` 路由）
- Create: `gridtrade/dashboard/templates/analytics.html`
- Modify: `gridtrade/dashboard/templates/base.html`（导航加 analytics 链接）
- Test: `tests/dashboard/test_app_analytics.py`

**Interfaces:**
- Consumes: `analytics`（realized_curve/equity_curve/tag_attribution/fill_distribution/exit_reason_stats）；`charts`（line_chart/bar_chart/stacked_bar）；`_user`。
- Produces: `GET /analytics?range=all|7d|30d`——登录门控（未登录 302 /login）；按 range 算 `start_ms`（now - 7/30 天 ms；all→0）；调 analytics 出数据、charts 出 SVG，渲染 analytics.html。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_app_analytics.py
from starlette.testclient import TestClient
from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password
from gridtrade.state.records import RecordRepository
from gridtrade.state.models import Record
from gridtrade.exchanges.base import Balance


class _Adapter:
    client = None
    def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
    def fetch_price(self, s): return 100.0


def _client(store):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek')
    c = TestClient(app, base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    return c


def test_analytics_requires_login(store):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek')
    anon = TestClient(app, base_url='https://testserver')
    r = anon.get('/analytics', follow_redirects=False)
    assert r.status_code == 302 and r.headers['location'].endswith('/login')


def test_analytics_renders_with_data(store):
    RecordRepository(store).add(Record(id='r1', exchange='x', symbol='BTC', tag='gt0',
                                       total_pnl=10.0, exit_reason='take_profit', closed_at=1000))
    r = _client(store).get('/analytics')
    assert r.status_code == 200
    assert '<svg' in r.text            # 图表渲染
    assert 'gt0' in r.text             # tag 归因表
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app_analytics.py -v`
Expected: FAIL — 404（路由未建）

- [ ] **Step 3: Write minimal implementation**

`app.py`：加路由（放在已有路由后、`return app` 前）：

```python
    @app.get('/analytics', response_class=HTMLResponse)
    def analytics_page(request: Request, range: str = 'all'):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        from gridtrade.dashboard import analytics as an
        from gridtrade.dashboard import charts as ch
        from gridtrade.state.models import now_ms
        cutoff = {'7d': 7 * 86400_000, '30d': 30 * 86400_000}.get(range, 0)
        start_ms = (now_ms() - cutoff) if cutoff else 0
        realized = an.realized_curve(store, start_ms=start_ms)
        equity = an.equity_curve(store, start_ms=start_ms)
        dist = an.fill_distribution(store, start_ms=start_ms)
        ctx = {
            'range': range,
            'equity_svg': ch.line_chart([realized, equity]),
            'tags': an.tag_attribution(store, start_ms=start_ms),
            'by_hour_svg': ch.bar_chart([(str(h), n) for h, n in dist.by_hour]),
            'by_side_svg': ch.stacked_bar([('成交', dist.by_side)]) if dist.by_side else ch.bar_chart([]),
            'by_line_svg': ch.bar_chart([(str(li), n) for li, n in dist.by_line]),
            'fee_cum_svg': ch.line_chart([dist.fee_cum]),
            'exits': an.exit_reason_stats(store, start_ms=start_ms),
        }
        return templates.TemplateResponse(request, 'analytics.html', ctx)
```

`analytics.html`：

```html
<!-- gridtrade/dashboard/templates/analytics.html -->
{% extends "base.html" %}{% block content %}
<h1>复盘分析</h1>
<nav class="rangesel">范围：
  <a href="/analytics?range=all">全部</a>
  <a href="/analytics?range=7d">近7天</a>
  <a href="/analytics?range=30d">近30天</a>
  <small>（当前：{{ range }}）</small>
</nav>

<h2>权益 / 已实现盈亏曲线</h2>
{{ equity_svg | safe }}

<h2>tag 盈亏归因</h2>
<table><thead><tr><th>tag</th><th>笔数</th><th>总盈亏</th><th>总fee</th><th>净盈亏</th>
<th>胜率</th><th>平均持仓</th><th>最大回撤</th></tr></thead><tbody>
{% for s in tags %}<tr><td>{{ s.tag }}</td><td>{{ s.count }}</td>
<td class="{{ s.total_pnl | pnl_class }}">{{ s.total_pnl | fmt_num }}</td>
<td>{{ s.total_fee | fmt_num }}</td>
<td class="{{ s.net_pnl | pnl_class }}">{{ s.net_pnl | fmt_num }}</td>
<td>{{ s.win_rate | fmt_pct }}</td>
<td>{{ (s.avg_hold_ms / 3600000) | fmt_num if s.avg_hold_ms else '-' }}h</td>
<td>{{ s.max_drawdown | fmt_num }}</td></tr>{% endfor %}
</tbody></table>

<h2>成交分布</h2>
<h3>活动（按小时桶）</h3>{{ by_hour_svg | safe }}
<h3>买卖方向</h3>{{ by_side_svg | safe }}
<h3>网格线 line_index</h3>{{ by_line_svg | safe }}
<h3>累计手续费</h3>{{ fee_cum_svg | safe }}

<h2>退出原因</h2>
<table><thead><tr><th>原因</th><th>笔数</th><th>占比</th><th>平均盈亏</th></tr></thead><tbody>
{% for e in exits %}<tr><td>{{ e.reason }}</td><td>{{ e.count }}</td>
<td>{{ e.share | fmt_pct }}</td>
<td class="{{ e.avg_pnl | pnl_class }}">{{ e.avg_pnl | fmt_num }}</td></tr>{% endfor %}
</tbody></table>
{% endblock %}
```

> 注：`{{ ... | safe }}` 仅用于本地生成的 SVG 字符串（无用户输入插值，charts.py 全数值生成），不引入 XSS。表格内文本（tag/reason）经默认 autoescape。

`base.html`：导航 `<nav>` 加 `<a href="/analytics">analytics</a>`。

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app_analytics.py -v`
然后全 dashboard 套件：`TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard -q`
Expected: PASS（新测 2 passed；既有 dashboard 测试不回归）

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/app.py gridtrade/dashboard/templates/analytics.html gridtrade/dashboard/templates/base.html tests/dashboard/test_app_analytics.py
git commit -m "feat(analytics): /analytics 路由 + 模板 + 范围过滤 + 导航"
```

---

### Task 10: 文档同步（STATUS / DEPLOY）

**Files:**
- Modify: `docs/STATUS.md`（§5 web 行补 P3 复盘分析 + equity_snapshots）
- Modify: `deploy/DEPLOY.md`（dashboard 段补复盘分析 + EQUITY_SNAPSHOT_INTERVAL_SEC）

**Interfaces:** 无代码；文档同步。

- [ ] **Step 1: 更新 STATUS.md §5 web 行**

在 web 进程描述补一句：

```markdown
  P3 复盘分析：/analytics 页（权益/已实现曲线、tag 归因、成交分布、退出原因，全服务端 SVG）；真实手续费铺表；equity_snapshots 由 monitor 节流写（EQUITY_SNAPSHOT_INTERVAL_SEC，默认 300s）。
```

- [ ] **Step 2: 更新 DEPLOY.md dashboard 段**

追加：

```markdown
### 复盘分析（P3）
- /analytics：权益/已实现盈亏曲线 + tag 盈亏归因 + 成交分布（时间/买卖/line/累计费）+ 退出原因，全部服务端内联 SVG（零 JS）。范围过滤 all/7d/30d。
- equity_snapshots 表随 create_all 自动建（无需 migrate）；monitor 每 EQUITY_SNAPSHOT_INTERVAL_SEC（默认 300s）节流写一行真权益（fetch_balance().equity，含未实现），取余额失败跳过不崩。
- 真实手续费（grid_fills.fee）已铺进成交流水表 / 总览 / tag 归因。
```

- [ ] **Step 3: Commit**

```bash
git add docs/STATUS.md deploy/DEPLOY.md
git commit -m "docs(analytics): STATUS/DEPLOY 同步 P3 复盘分析 + equity_snapshots"
```

---

## Self-Review

**Spec 覆盖（逐节核对 2026-06-30-dashboard-p3-analytics-design.md）：**
- §2 范围：权益曲线(T6 realized+equity / T9 渲染)、tag 归因(T7)、成交分布四维(T8)、退出原因(T8)、fee 铺表(T4)、范围过滤(T9)。✅
- §4.1 equity_snapshots 表 → T1。§4.2 grid_fills.fee 已存在 → T4 读出。✅
- §5.1 EquitySnapshotRepository → T2。§5.2 analytics → T6/T7/T8。§5.3 charts → T5。✅
- §6 视图与 fee 铺表 → T4(fee 表) + T9(四视图渲染)。✅
- §7 monitor 快照写（容错不崩 + 接线）→ T3。✅
- §8 鉴权/安全（/analytics 登录门控、web 零写、SVG 数值生成）→ T9（route gate + safe 仅本地 SVG）。✅
- §9 测试（repo 节流 / analytics / charts / route / 快照写容错 / fee 铺表）→ 各任务 TDD。✅
- §10 开放项（早期点少 / 保留策略 / 降采样 / cash 容错）→ 保留为实现注意；cash 容错在 T3 `getattr(bal,'cash',None)`。✅

**Placeholder 扫描：** 无 TBD/TODO；每 code step 给完整代码+命令。charts.py 坐标映射给了确定公式 + 断言具体坐标值（非占位）。✅

**类型一致：** `EquitySnapshotRepository.add_if_due/list_range/latest_ts`(T2) 在 T3/T6 一致调用；`realized_curve/equity_curve`(T6)、`tag_attribution`+`TagStat`(T7)、`fill_distribution`+`FillDist`+`exit_reason_stats`+`ExitStat`(T8) 在 T9 路由一致消费；`line_chart/bar_chart/stacked_bar`(T5) 在 T9 一致调用；`RecentFill.fee`/`GridOverviewRow.fee_paid`(T4) 与模板一致；`run_monitor_cycle(...,equity_repo,snapshot_interval_sec)`(T3) 与 monitor.py 接线一致；`Balance(equity,cash)` 一致。✅

**安全注记：** T9 模板对 charts 生成的 SVG 用 `| safe`——这些 SVG 全由 charts.py 从数值拼接、无用户/DB 文本插值，不构成 XSS；表格内的 tag/reason 等 DB 文本走默认 autoescape（未 safe）。终审需复核此边界。
