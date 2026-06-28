# 交易所解耦重构 P4j 实现计划（币池解析 + 心跳表 + 黑名单配置）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐守护进程组装前缺的三块离线可测件（用户 2026-06-28 决策）：① config 加 `blacklist`（env 驱动，币池=HL 全部永续**排除黑名单**）；② `heartbeats` 表 + `HeartbeatRepository`（写库心跳行，fly 判活靠它）；③ `resolve_live_universe(adapter, blacklist)`（list_instruments → state=='live' 且不在黑名单的符号）。守护进程 while/sleep + 真实 HL adapter factory + 基础设施产物归 P4k。

**Architecture:** 黑名单内容是用户后填的运营数据，故 config 只提供机制（env `BLACKLIST_SYMBOLS` 逗号分隔 → tuple，默认空）。心跳是 state 层一张极简表（machine 主键 → last_beat_ts），upsert 写、connect 读（沿 P4a 读路径约定）。币池解析是 runtime 纯函数，吃 adapter（拿 ResilientAdapter 也行）吐符号列表。

**Tech Stack:** Python 3.9、SQLAlchemy 2.0 Core、pytest、FakeExchange + 内存 SQLite。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（黑名单口径、心跳字段、币池过滤条件、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；改 `gridtrade/config.py`（+ blacklist 字段）、`gridtrade/state/models.py`（+ heartbeats 表 + Heartbeat dataclass）；新增 `gridtrade/state/heartbeats.py`、`gridtrade/runtime/universe.py` 及对应测试。不改 core/exchanges/backtest/已有 execution。
- 心跳读路径用 `engine.connect()`、写路径用 `engine.begin()`（沿 P4a 约定）。
- 币池过滤：只保留 `instrument.state == 'live'` 且 `instrument.symbol not in blacklist`。
- 黑名单内容不写死（用户后填）；config 只解析 env `BLACKLIST_SYMBOLS`（逗号分隔，去空白，空串→空 tuple）。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。全量回归须保持绿（基线 195 passed）。

---

## 文件结构（本计划新建/修改）

```
gridtrade/config.py            # 修改：DeployConfig + blacklist: tuple；env BLACKLIST_SYMBOLS
gridtrade/state/models.py      # 修改：+ heartbeats 表 + Heartbeat dataclass
gridtrade/state/heartbeats.py  # 新增：HeartbeatRepository（beat / get / list_all）
gridtrade/runtime/universe.py  # 新增：resolve_live_universe(adapter, blacklist=())
tests/test_config.py           # 修改：+ blacklist 解析
tests/state/test_heartbeats.py # 新增
tests/runtime/test_universe.py # 新增
```

---

### Task 1: config blacklist 字段

**Files:**
- Modify: `gridtrade/config.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `DeployConfig.blacklist: tuple`；env `BLACKLIST_SYMBOLS`（逗号分隔）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_config.py` 末尾追加：

```python
def test_blacklist_parsing():
    assert load_deploy_config(env={}).blacklist == ()
    cfg = load_deploy_config(env={'BLACKLIST_SYMBOLS': 'BTC, ETH ,SOL'})
    assert cfg.blacklist == ('BTC', 'ETH', 'SOL')      # 去空白
    assert load_deploy_config(env={'BLACKLIST_SYMBOLS': ''}).blacklist == ()
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/test_config.py -k blacklist -q`
Expected: FAIL（`AttributeError: 'DeployConfig' object has no attribute 'blacklist'`）。

- [ ] **Step 3: 实现**

`gridtrade/config.py`：在 `DeployConfig` 末尾加字段 `blacklist: tuple`；新增解析助手并在 `load_deploy_config` 里赋值：

```python
def _csv(env, key):
    v = env.get(key)
    if not v:
        return ()
    return tuple(s.strip() for s in v.split(',') if s.strip())
```

在 `DeployConfig` dataclass 字段末尾加：

```python
    blacklist: tuple = ()
```

在 `load_deploy_config(...)` 的 `return DeployConfig(` 调用末尾加参数：

```python
        blacklist=_csv(env, 'BLACKLIST_SYMBOLS'),
```

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/test_config.py -q`
Expected: 全 PASS（6）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/config.py tests/test_config.py
git commit -m "feat(config): BLACKLIST_SYMBOLS env -> DeployConfig.blacklist (P4j)"
```

---

### Task 2: heartbeats 表 + HeartbeatRepository

**Files:**
- Modify: `gridtrade/state/models.py`
- Create: `gridtrade/state/heartbeats.py`
- Create: `tests/state/test_heartbeats.py`

**Interfaces:**
- Produces: `gridtrade.state.models.{heartbeats, Heartbeat}`；`HeartbeatRepository(store)` 的 `beat(machine, ts=None) -> Heartbeat`、`get(machine) -> Optional[Heartbeat]`、`list_all() -> List[Heartbeat]`。

- [ ] **Step 1: 写失败测试**

Create `tests/state/test_heartbeats.py`:

```python
from gridtrade.state.models import Heartbeat


def _repo():
    from gridtrade.state.store import StateStore
    from gridtrade.state.heartbeats import HeartbeatRepository
    s = StateStore.in_memory(); s.create_all()
    return HeartbeatRepository(s)


def test_beat_inserts_then_updates_same_machine():
    repo = _repo()
    hb1 = repo.beat('monitor', ts=1000)
    assert isinstance(hb1, Heartbeat)
    assert hb1.machine == 'monitor' and hb1.last_beat_ts == 1000
    hb2 = repo.beat('monitor', ts=2000)        # 同机器 -> upsert 更新
    assert hb2.last_beat_ts == 2000
    assert repo.get('monitor').last_beat_ts == 2000


def test_get_missing_returns_none():
    assert _repo().get('nope') is None


def test_list_all_returns_all_machines():
    repo = _repo()
    repo.beat('monitor', ts=10)
    repo.beat('scheduler', ts=20)
    got = {h.machine: h.last_beat_ts for h in repo.list_all()}
    assert got == {'monitor': 10, 'scheduler': 20}


def test_beat_default_ts_is_positive():
    repo = _repo()
    hb = repo.beat('monitor')
    assert hb.last_beat_ts > 0
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_heartbeats.py -q`
Expected: FAIL（`ImportError: cannot import name 'Heartbeat'` 或 `gridtrade.state.heartbeats`）。

- [ ] **Step 3: 实现**

在 `gridtrade/state/models.py` 加表与 dataclass（紧随 grid_fills 表之后；`Table/Column/String/Integer/metadata` 已 import）：

```python
heartbeats = Table(
    'heartbeats', metadata,
    Column('machine', String, primary_key=True),
    Column('last_beat_ts', Integer, nullable=False),
)
```

在 models.py 的 dataclass 区（与 Fill 等并列）加：

```python
@dataclass
class Heartbeat:
    machine: str
    last_beat_ts: int
```

Create `gridtrade/state/heartbeats.py`:

```python
"""HeartbeatRepository：机器心跳行（machine -> last_beat_ts）。fly 判活/告警靠它。"""
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy import insert, select, update

from gridtrade.state.models import Heartbeat, heartbeats, now_ms

_FIELDS = ('machine', 'last_beat_ts')


def _to_hb(row) -> Heartbeat:
    m = row._mapping
    return Heartbeat(**{f: m[f] for f in _FIELDS})


class HeartbeatRepository:
    def __init__(self, store):
        self.engine = store.engine

    def beat(self, machine: str, ts: Optional[int] = None) -> Heartbeat:
        ts = int(ts) if ts is not None else now_ms()
        try:
            with self.engine.begin() as c:
                c.execute(insert(heartbeats),
                          {'machine': machine, 'last_beat_ts': ts})
        except sa.exc.IntegrityError:
            with self.engine.begin() as c:
                c.execute(update(heartbeats)
                          .where(heartbeats.c.machine == machine)
                          .values(last_beat_ts=ts))
        return self.get(machine)

    def get(self, machine: str) -> Optional[Heartbeat]:
        with self.engine.connect() as c:
            row = c.execute(
                select(heartbeats).where(heartbeats.c.machine == machine)).first()
        return _to_hb(row) if row is not None else None

    def list_all(self) -> List[Heartbeat]:
        with self.engine.connect() as c:
            rows = c.execute(select(heartbeats)).all()
        return [_to_hb(r) for r in rows]
```

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_heartbeats.py -q`
Expected: 全 PASS（4）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/state/models.py gridtrade/state/heartbeats.py tests/state/test_heartbeats.py
git commit -m "feat(state): heartbeats table + HeartbeatRepository (P4j)"
```

---

### Task 3: resolve_live_universe（HL 全部永续 - 黑名单）

**Files:**
- Create: `gridtrade/runtime/universe.py`
- Create: `tests/runtime/test_universe.py`

**Interfaces:**
- Consumes: `adapter.list_instruments() -> List[Instrument]`（Instrument 有 `.symbol`/`.state`）。
- Produces: `resolve_live_universe(adapter, blacklist=()) -> List[str]`。

- [ ] **Step 1: 写失败测试**

Create `tests/runtime/test_universe.py`:

```python
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument


def _ex(*specs):
    # specs: (symbol, state)
    insts = [Instrument(sym, 0.1, 0.001, 0.001, st, 0) for sym, st in specs]
    return FakeExchange(instruments=insts, price=100.0)


def test_universe_keeps_live_excludes_blacklist():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('ETH/USDC:USDC', 'live'),
             ('SOL/USDC:USDC', 'live'))
    out = resolve_live_universe(ex, blacklist=('ETH/USDC:USDC',))
    assert out == ['BTC/USDC:USDC', 'SOL/USDC:USDC']


def test_universe_drops_non_live():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('OLD/USDC:USDC', 'delisted'))
    assert resolve_live_universe(ex) == ['BTC/USDC:USDC']


def test_universe_empty_blacklist_keeps_all_live():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('ETH/USDC:USDC', 'live'))
    assert resolve_live_universe(ex) == ['BTC/USDC:USDC', 'ETH/USDC:USDC']
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_universe.py -q`
Expected: FAIL（`ModuleNotFoundError: gridtrade.runtime.universe`）。

- [ ] **Step 3: 实现**

Create `gridtrade/runtime/universe.py`:

```python
"""币池解析：HL 全部永续中保留 state=='live' 且不在黑名单的符号（用户决策）。"""
from typing import List


def resolve_live_universe(adapter, blacklist=()) -> List[str]:
    bl = set(blacklist)
    return [i.symbol for i in adapter.list_instruments()
            if i.state == 'live' and i.symbol not in bl]
```

- [ ] **Step 4: 跑测试确认绿 + 全量回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_universe.py -q`
Expected: 全 PASS（3）。

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全量 PASS（≥ 195 + 新增）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/runtime/universe.py tests/runtime/test_universe.py
git commit -m "feat(runtime): resolve_live_universe (live perps minus blacklist) (P4j)"
```

---

## Self-Review

- **决策对齐**：币池=HL 全部永续 + 黑名单排除（Task 1 config 机制 + Task 3 解析）；心跳=写库心跳行（Task 2）。黑名单内容用户后填（env），不写死。
- **Spec 覆盖**：design.md §8「心跳写库 + fly 健康检查」（heartbeats 表）；选币币池来源（resolve_live_universe，替代 legacy ccxt_fetch_ok_exchangeinfo + black_dict）。
- **读写路径**：心跳读 connect()/写 begin()（沿 P4a）。
- **Placeholder 扫描**：无 TBD/TODO；每步完整代码 + 精确命令/预期。
- **类型一致**：`DeployConfig.blacklist: tuple`；`Heartbeat(machine, last_beat_ts)` 在 models/repo/测试一致；`HeartbeatRepository.{beat,get,list_all}` 签名一致；`resolve_live_universe(adapter, blacklist=())` 一致。
