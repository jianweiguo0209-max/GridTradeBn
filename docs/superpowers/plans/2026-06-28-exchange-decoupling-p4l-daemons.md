# 交易所解耦重构 P4l 实现计划（守护进程：scheduler 一次性 + monitor 常驻循环）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 两个 Fly Machine 的进程入口（design.md §8）：① `scheduler.py` —— scale-to-zero **一次性**（每次唤醒跑一遍：定 run_time/offset → 拉币池 K 线 → `run_scheduler_cycle` 关旧+开新 → 心跳），跑完即退；② `monitor.py` —— 常驻 **while/sleep 循环**（启动先 `restore_all` 自愈，循环 `run_monitor_cycle` + 心跳，**单轮异常降级续跑不退出**，SIGTERM 优雅停）。可测部分（`run_scheduler_once` / `run_monitor`）用 `build_runtime(fake)` 离线 TDD；`main()` 是 composition root（读真实 env，不单测）。

**Architecture:** scheduler 无 while 循环（fly 定时唤醒一次即退，符合 scale-to-zero）。`fetch_universe_candles` 经 ResilientAdapter（自带重试）逐币拉 1H OHLCV。monitor 循环把 `run_monitor_cycle` 包在 try/except 内：异常 log + 续跑（绝不 sys.exit），每轮 `heartbeats.beat`。`cycle_fn`/`now_fn`/`fetch_candles`/`sleep`/`should_stop` 注入以便确定性测试。

**Tech Stack:** Python 3.9、pandas、signal、pytest、FakeExchange + 内存 SQLite。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（run_time 口径、K 线窗口、优雅停语义、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；新增 `gridtrade/runtime/scheduler.py`、`gridtrade/runtime/monitor.py` 及测试。不改其它文件（仅 import 既有 cycles/universe/factory/config）。
- scheduler **一次性**（无 while）；monitor **常驻**（while + sleep(config.monitor_interval_sec)）。
- monitor 单轮异常：log + 续跑（绝不 sys.exit/抛出）；SIGTERM/SIGINT → 设 stop flag，完成当前轮后退出。
- 心跳：scheduler 跑完 `beat('scheduler')`；monitor 每轮 `beat('monitor')`。
- K 线：`fetch_universe_candles` 逐币 `adapter.fetch_ohlcv(sym,'1H',start_ms,end_ms)`，窗口 = max_candle_num 根 1H；空 df 跳过。
- 可测函数全注入（now_fn/fetch_candles/cycle_fn/sleep/should_stop）；`main()` 不单测。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。全量回归须保持绿（基线 210 passed）。

---

## 文件结构（本计划新建）

```
gridtrade/runtime/scheduler.py   # fetch_universe_candles / run_scheduler_once / main
gridtrade/runtime/monitor.py     # run_monitor / main
tests/runtime/test_scheduler.py
tests/runtime/test_monitor.py
```

公共接口：

```python
# scheduler.py
def fetch_universe_candles(adapter, symbols, run_time, *, timeframe='1H',
                           max_candle_num=160) -> dict: ...
def run_scheduler_once(runtime, *, now_fn=time.time,
                       fetch_candles=fetch_universe_candles) -> dict: ...
def main() -> None: ...

# monitor.py
def run_monitor(runtime, *, once=False, sleep=time.sleep, log=print,
                cycle_fn=run_monitor_cycle, should_stop=None) -> None: ...
def main() -> None: ...
```

---

### Task 1: scheduler.py（一次性：拉币池 K 线 → 关旧+开新 → 心跳）

**Files:**
- Create: `gridtrade/runtime/scheduler.py`
- Create: `tests/runtime/test_scheduler.py`

**Interfaces:**
- Consumes: `resolve_live_universe`、`run_scheduler_cycle`、`TriggerContext`、`compute_offset`、`DEFAULT_STRATEGY_CONFIG`、`Runtime`（adapter/manager/trigger_engine/reconciler/heartbeats/config）。
- Produces: `fetch_universe_candles`、`run_scheduler_once`、`main`。

- [ ] **Step 1: 写失败测试**

Create `tests/runtime/test_scheduler.py`:

```python
from gridtrade.config import load_deploy_config
from gridtrade.runtime.factory import build_runtime


def _rt(**kw):
    env = {'EXCHANGE': 'fake'}
    env.update(kw)
    return build_runtime(load_deploy_config(env=env))


def test_run_scheduler_once_empty_universe_no_opens_and_beats():
    from gridtrade.runtime.scheduler import run_scheduler_once
    rt = _rt()                      # fake 无 instruments -> 空币池
    out = run_scheduler_once(rt, now_fn=lambda: 1_750_000_000.0)
    assert out['opened'] == [] and out['closed'] == []
    assert rt.heartbeats.get('scheduler') is not None


def test_run_scheduler_once_uses_injected_candles_and_fetch():
    from gridtrade.runtime.scheduler import run_scheduler_once
    seen = {}
    def _fake_fetch(adapter, symbols, run_time, **kw):
        seen['symbols'] = list(symbols)
        return {}
    rt = _rt()
    run_scheduler_once(rt, now_fn=lambda: 1_750_000_000.0,
                       fetch_candles=_fake_fetch)
    assert seen['symbols'] == []   # 空币池传给 fetch


def test_fetch_universe_candles_skips_empty_and_collects_nonempty():
    import pandas as pd
    from gridtrade.runtime.scheduler import fetch_universe_candles
    from gridtrade.exchanges.fake import FakeExchange
    from gridtrade.exchanges.base import Instrument, CANDLE_COLS
    ex = FakeExchange(instruments=[Instrument('BTC/USDC:USDC', 0.1, 0.001, 0.001,
                                              'live', 0)], price=100.0)
    df = pd.DataFrame([[0, 1, 1, 1, 1, 1, 1]], columns=CANDLE_COLS)
    ex.seed_ohlcv('BTC/USDC:USDC', df)
    out = fetch_universe_candles(ex, ['BTC/USDC:USDC', 'NONE/USDC:USDC'],
                                 pd.Timestamp('2025-06-24 14:00:00'),
                                 max_candle_num=10)
    assert 'BTC/USDC:USDC' in out and 'NONE/USDC:USDC' not in out
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_scheduler.py -q`
Expected: FAIL（`ModuleNotFoundError: gridtrade.runtime.scheduler`）。

- [ ] **Step 3: 实现 scheduler.py**

Create `gridtrade/runtime/scheduler.py`:

```python
"""scheduler 机入口（scale-to-zero 一次性）：关旧 tag → 选币 → 准入 → 开新 → 心跳。"""
import time

import pandas as pd

from gridtrade.config import DEFAULT_STRATEGY_CONFIG, load_deploy_config
from gridtrade.core.selection import compute_offset
from gridtrade.execution.triggers import TriggerContext
from gridtrade.runtime.cycles import run_scheduler_cycle
from gridtrade.runtime.factory import build_runtime
from gridtrade.runtime.universe import resolve_live_universe


def fetch_universe_candles(adapter, symbols, run_time, *, timeframe='1H',
                           max_candle_num=160) -> dict:
    end_ms = int(pd.Timestamp(run_time).timestamp() * 1000)
    start_ms = end_ms - max_candle_num * 3600 * 1000   # 1H 根
    out = {}
    for sym in symbols:
        df = adapter.fetch_ohlcv(sym, timeframe, start_ms, end_ms)
        if df is not None and not df.empty:
            out[sym] = df
    return out


def run_scheduler_once(runtime, *, now_fn=time.time,
                       fetch_candles=fetch_universe_candles) -> dict:
    rt = runtime
    run_time = pd.Timestamp(now_fn(), unit='s').floor('H')
    period = rt.config.scheduler_period
    offset = compute_offset(run_time, period, rt.config.utc_offset)
    tag = '%s%d' % (DEFAULT_STRATEGY_CONFIG['strategy_tag'], offset)
    universe = resolve_live_universe(rt.adapter, rt.config.blacklist)
    candles = fetch_candles(rt.adapter, universe, run_time,
                            max_candle_num=DEFAULT_STRATEGY_CONFIG['max_candle_num'])
    ctx = TriggerContext(rt.config.exchange, run_time, candles)
    result = run_scheduler_cycle(rt.manager, rt.trigger_engine, rt.reconciler,
                                 ctx, close_tag=tag)
    rt.heartbeats.beat('scheduler')
    return result


def main() -> None:   # composition root（不单测）
    rt = build_runtime(load_deploy_config())
    out = run_scheduler_once(rt)
    print('[scheduler] closed=%d opened=%d' % (len(out['closed']),
                                               len(out['opened'])))
```

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_scheduler.py -q`
Expected: 3 PASS。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/runtime/scheduler.py tests/runtime/test_scheduler.py
git commit -m "feat(runtime): scheduler daemon (one-shot: universe candles -> cycle -> heartbeat) (P4l)"
```

---

### Task 2: monitor.py（常驻循环：自愈 → 对账+止损 → 心跳，降级续跑 + 优雅停）

**Files:**
- Create: `gridtrade/runtime/monitor.py`
- Create: `tests/runtime/test_monitor.py`

**Interfaces:**
- Consumes: `restore_all`、`run_monitor_cycle`、`Runtime`（reconciler/manager/heartbeats/config）。
- Produces: `run_monitor(runtime, *, once=False, sleep=time.sleep, log=print, cycle_fn=run_monitor_cycle, should_stop=None)`、`main`。

- [ ] **Step 1: 写失败测试**

Create `tests/runtime/test_monitor.py`:

```python
from gridtrade.config import load_deploy_config
from gridtrade.runtime.factory import build_runtime


def _rt(**kw):
    env = {'EXCHANGE': 'fake'}
    env.update(kw)
    return build_runtime(load_deploy_config(env=env))


def test_run_monitor_once_restores_and_beats():
    from gridtrade.runtime.monitor import run_monitor
    rt = _rt()
    run_monitor(rt, once=True, sleep=lambda d: None)
    assert rt.heartbeats.get('monitor') is not None


def test_run_monitor_degrades_on_cycle_error_and_still_beats():
    from gridtrade.runtime.monitor import run_monitor
    rt = _rt()
    logs = []
    def _boom(reconciler, manager):
        raise RuntimeError('cycle blew up')
    # 单轮异常 -> 捕获 + log + 心跳 + 不抛出
    run_monitor(rt, once=True, sleep=lambda d: None, log=logs.append,
                cycle_fn=_boom)
    assert any('cycle blew up' in s or 'degraded' in s for s in logs)
    assert rt.heartbeats.get('monitor') is not None


def test_run_monitor_loops_until_should_stop():
    from gridtrade.runtime.monitor import run_monitor
    rt = _rt()
    n = {'i': 0}
    def _cycle(reconciler, manager):
        n['i'] += 1
        return {}
    # should_stop 第 3 轮后停
    run_monitor(rt, sleep=lambda d: None, cycle_fn=_cycle,
                should_stop=lambda: n['i'] >= 3)
    assert n['i'] == 3
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_monitor.py -q`
Expected: FAIL（`ModuleNotFoundError: gridtrade.runtime.monitor`）。

> 注：`gridtrade/runtime/monitor.py` 是新文件；既有单网格步在 `gridtrade/execution/monitor.py`（`monitor_grid`），两者不冲突。

- [ ] **Step 3: 实现 monitor.py**

Create `gridtrade/runtime/monitor.py`:

```python
"""monitor 机入口（常驻）：启动自愈 restore_all，循环 run_monitor_cycle + 心跳。

单轮异常降级 log+续跑（绝不 sys.exit）；SIGTERM/SIGINT 优雅停（完成当前轮后退出）。
"""
import signal
import time

from gridtrade.config import load_deploy_config
from gridtrade.runtime.cycles import restore_all, run_monitor_cycle
from gridtrade.runtime.factory import build_runtime


def run_monitor(runtime, *, once=False, sleep=time.sleep, log=print,
                cycle_fn=run_monitor_cycle, should_stop=None):
    rt = runtime
    restore_all(rt.reconciler)            # 重启自愈一次
    while True:
        try:
            cycle_fn(rt.reconciler, rt.manager)
        except Exception as exc:          # 降级：记录 + 续跑，绝不退出
            log('[monitor] degraded: %r' % exc)
        rt.heartbeats.beat('monitor')
        if once:
            return
        if should_stop is not None and should_stop():
            return
        sleep(rt.config.monitor_interval_sec)


def main() -> None:   # composition root（不单测）
    rt = build_runtime(load_deploy_config())
    stop = {'flag': False}

    def _graceful(signum, frame):
        stop['flag'] = True

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    run_monitor(rt, should_stop=lambda: stop['flag'])
```

- [ ] **Step 4: 跑测试确认绿 + 全量回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_monitor.py -q`
Expected: 3 PASS。

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全量 PASS（≥ 210 + 新增）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/runtime/monitor.py tests/runtime/test_monitor.py
git commit -m "feat(runtime): monitor daemon (restore -> cycle loop, degrade-not-exit, graceful stop) (P4l)"
```

---

## Self-Review

- **决策对齐**：scheduler 每小时唤醒 + scale-to-zero（一次性，无 while）；monitor 常驻 ~5s 循环（config.monitor_interval_sec）；心跳写库（每轮 beat）。
- **Spec 覆盖**：design.md §8 两机角色循环体 + 健壮性「降级不 sys.exit」（monitor 单轮 try/except 续跑）+ 重启自愈（restore_all）+ 优雅停（SIGTERM）。
- **可测性**：run_scheduler_once / run_monitor 全注入（now_fn/fetch_candles/cycle_fn/sleep/should_stop），build_runtime(fake) 离线测；main() 是 composition root 不单测。
- **命名不冲突**：runtime/monitor.py（机循环）vs execution/monitor.py（monitor_grid 单步）。
- **Placeholder 扫描**：无 TBD/TODO；每步完整代码 + 精确命令/预期。
- **类型一致**：`run_scheduler_once(runtime, *, now_fn, fetch_candles) -> dict`；`run_monitor(runtime, *, once, sleep, log, cycle_fn, should_stop)`；`cycle_fn(reconciler, manager)` 与 `run_monitor_cycle` 签名一致；`fetch_candles(adapter, symbols, run_time, max_candle_num=...)` 与注入签名一致。
