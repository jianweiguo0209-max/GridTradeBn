# 交易所解耦重构 P4n 实现计划（scheduler 改常驻自走 process group + 整点对齐）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 scheduler 从「scale-to-zero 定时机」改成「常驻 fly process group + 自己睡到整点跑」，使 `fly deploy`（CI/CD）一条命令同时更新 monitor + scheduler（消除定时机镜像不随部署更新的问题）。整点对齐避免部署时 mid-hour 重处理当前 offset（否则会关掉刚开的网格再重开、白白来回交易）；加 `SCHEDULER_RUN_ON_START`（默认 false）供 testnet 调试立即跑一次。同步更新 `fly.toml`（加 scheduler process group）与 `DEPLOY.md`（删定时机步骤）。

**Architecture:** `run_scheduler` 循环：可选启动即跑一次（run_on_start）→ 循环「sleep 到下个整点 → run_scheduler_once → 检查 should_stop」。单轮异常降级 log + 续跑（绝不 sys.exit），SIGTERM 优雅停（同 monitor）。`_seconds_to_next_hour` 纯函数可测。config 加 `scheduler_run_on_start`。

**Tech Stack:** Python 3.9、signal、pytest、注入式 sleep/now_fn/run_once_fn/should_stop。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。**

## Global Constraints

- Python 3.9；改 `gridtrade/config.py`（+ scheduler_run_on_start）、`gridtrade/runtime/scheduler.py`（+ _seconds_to_next_hour / run_scheduler / main 改循环+信号）、`deploy/fly.toml`（+ scheduler process group）、`deploy/DEPLOY.md`（删定时机步骤）；改 `tests/test_config.py`、`tests/runtime/test_scheduler.py`。不改其它。
- scheduler 默认仅整点跑（run_on_start=False）；run_on_start=True 时启动立即跑一次（testnet 调试）。
- 单轮异常：log + 续跑，绝不 sys.exit；SIGTERM/SIGINT 优雅停。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。全量回归须保持绿（基线 219 passed）。

---

### Task 1: config scheduler_run_on_start

**Files:** Modify `gridtrade/config.py`、`tests/test_config.py`

- [ ] **Step 1: 写失败测试** —— 在 `tests/test_config.py` 末尾追加：

```python
def test_scheduler_run_on_start_flag():
    assert load_deploy_config(env={}).scheduler_run_on_start is False
    assert load_deploy_config(
        env={'SCHEDULER_RUN_ON_START': 'true'}).scheduler_run_on_start is True
```

- [ ] **Step 2: 跑测试确认红**
Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/test_config.py -k run_on_start -q`
Expected: FAIL（AttributeError）。

- [ ] **Step 3: 实现** —— `DeployConfig` 加字段 `scheduler_run_on_start: bool = False`；`load_deploy_config` 的 return 里加 `scheduler_run_on_start=_b(env, 'SCHEDULER_RUN_ON_START', False),`。

- [ ] **Step 4: 跑测试确认绿**
Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/test_config.py -q`
Expected: 全 PASS。

- [ ] **Step 5: 提交**
```bash
git add gridtrade/config.py tests/test_config.py
git commit -m "feat(config): SCHEDULER_RUN_ON_START flag (P4n)"
```

---

### Task 2: run_scheduler 常驻循环 + 整点对齐 + 信号

**Files:** Modify `gridtrade/runtime/scheduler.py`、`tests/runtime/test_scheduler.py`

**Interfaces:**
- Produces: `_seconds_to_next_hour(now_epoch) -> int`；`run_scheduler(runtime, *, once=False, sleep=time.sleep, now_fn=time.time, log=print, run_once_fn=run_scheduler_once, should_stop=None, run_on_start=False)`；`main` 改用 run_scheduler + 信号。

- [ ] **Step 1: 写失败测试** —— 在 `tests/runtime/test_scheduler.py` 末尾追加：

```python
def test_seconds_to_next_hour():
    from gridtrade.runtime.scheduler import _seconds_to_next_hour
    assert _seconds_to_next_hour(1_750_000_000.0) == 3200
    assert _seconds_to_next_hour(3600.0) == 3600       # 整点 -> 整一小时
    assert _seconds_to_next_hour(3601.0) == 3599


def test_run_scheduler_run_on_start_runs_immediately():
    from gridtrade.runtime.scheduler import run_scheduler
    rt = _rt()
    calls, sleeps = [], []
    run_scheduler(rt, once=True, run_on_start=True, sleep=sleeps.append,
                  now_fn=lambda: 1_750_000_000.0,
                  run_once_fn=lambda runtime, now_fn: calls.append('run'))
    assert calls == ['run'] and sleeps == []   # 启动即跑、无 sleep


def test_run_scheduler_sleeps_to_hour_then_runs_when_not_run_on_start():
    from gridtrade.runtime.scheduler import run_scheduler
    rt = _rt()
    calls, sleeps = [], []
    run_scheduler(rt, once=True, run_on_start=False, sleep=sleeps.append,
                  now_fn=lambda: 1_750_000_000.0,
                  run_once_fn=lambda runtime, now_fn: calls.append('run'))
    assert sleeps == [3200] and calls == ['run']   # 先睡到整点再跑


def test_run_scheduler_loops_until_should_stop():
    from gridtrade.runtime.scheduler import run_scheduler
    rt = _rt()
    n = {'i': 0}
    def _run(runtime, now_fn):
        n['i'] += 1
    run_scheduler(rt, sleep=lambda d: None, now_fn=lambda: 1_750_000_000.0,
                  run_once_fn=_run, should_stop=lambda: n['i'] >= 3)
    assert n['i'] == 3


def test_run_scheduler_degrades_on_error_and_continues():
    from gridtrade.runtime.scheduler import run_scheduler
    rt = _rt()
    logs = []
    def _boom(runtime, now_fn):
        raise RuntimeError('boom')
    run_scheduler(rt, once=True, run_on_start=True, sleep=lambda d: None,
                  now_fn=lambda: 1_750_000_000.0, log=logs.append,
                  run_once_fn=_boom)
    assert any('boom' in s or 'degraded' in s for s in logs)
```

- [ ] **Step 2: 跑测试确认红**
Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_scheduler.py -k "next_hour or run_scheduler" -q`
Expected: FAIL（ImportError）。

- [ ] **Step 3: 实现** —— `gridtrade/runtime/scheduler.py`：顶部加 `import signal`；在 `run_scheduler_once` 之后加：

```python
def _seconds_to_next_hour(now_epoch) -> int:
    return 3600 - (int(now_epoch) % 3600)


def _safe_run(runtime, run_once_fn, now_fn, log):
    try:
        run_once_fn(runtime, now_fn=now_fn)
    except Exception as exc:          # 降级：记录 + 续跑，绝不退出
        log('[scheduler] degraded: %r' % exc)


def run_scheduler(runtime, *, once=False, sleep=time.sleep, now_fn=time.time,
                  log=print, run_once_fn=run_scheduler_once,
                  should_stop=None, run_on_start=False):
    if run_on_start:
        _safe_run(runtime, run_once_fn, now_fn, log)
        if once:
            return
    while True:
        sleep(_seconds_to_next_hour(now_fn()))
        _safe_run(runtime, run_once_fn, now_fn, log)
        if once:
            return
        if should_stop is not None and should_stop():
            return
```

并把 `main()` 改为循环 + 信号：

```python
def main() -> None:   # composition root（不单测）
    rt = build_runtime(load_deploy_config())
    stop = {'flag': False}

    def _graceful(signum, frame):
        stop['flag'] = True

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    run_scheduler(rt, should_stop=lambda: stop['flag'],
                  run_on_start=rt.config.scheduler_run_on_start)
```

- [ ] **Step 4: 跑测试确认绿 + 全量回归**
Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_scheduler.py -q`
Expected: 全 PASS（含原 3 + 新 5）。
Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全量 PASS（≥ 219 + 新增）。

- [ ] **Step 5: 提交**
```bash
git add gridtrade/runtime/scheduler.py tests/runtime/test_scheduler.py
git commit -m "feat(runtime): scheduler resident loop (hour-aligned, run-on-start opt, graceful) (P4n)"
```

---

### Task 3: fly.toml scheduler process group + DEPLOY.md 更新（infra）

**Files:** Modify `deploy/fly.toml`、`deploy/DEPLOY.md`

- [ ] **Step 1: fly.toml 加 scheduler process group**

`[processes]` 改为：
```toml
[processes]
  monitor = "python -m gridtrade.runtime.monitor"
  scheduler = "python -m gridtrade.runtime.scheduler"
```
`[[vm]]` 的 processes 改为：
```toml
  processes = ["monitor", "scheduler"]
```
并在 `[env]` 加（调试期可开）：
```toml
  # SCHEDULER_RUN_ON_START = "true"   # testnet 调试：启动即跑一次；稳定后删/置 false
```

- [ ] **Step 2: DEPLOY.md 删定时机步骤**

删掉「## 5. 建 scheduler 定时机」整段（含 `fly machine run --schedule`），改为说明：scheduler 现为 fly.toml process group，`fly deploy` 自动随 monitor 一起部署/更新；调试期可设 `SCHEDULER_RUN_ON_START=true` 让它启动即跑一遍。

- [ ] **Step 3: 全量回归 + 提交**
Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全量 PASS。
```bash
git add deploy/fly.toml deploy/DEPLOY.md
git commit -m "chore(deploy): scheduler as fly process group; drop scheduled-machine step (P4n)"
```

---

## Self-Review

- **解决冲突**：scheduler 成为 process group → `fly deploy`（CI/CD）一键更新 monitor+scheduler，契合「main 改动部署一切」。
- **正确性**：默认仅整点跑，避免部署 mid-hour churn；run_on_start 仅调试用。
- **健壮性**：降级不退出 + 优雅停，同 monitor。
- **可测**：_seconds_to_next_hour 纯函数；run_scheduler 全注入。main() composition root 不单测。
- **类型一致**：`run_scheduler(...run_once_fn=run_scheduler_once...)`；run_once_fn 以 `now_fn=` 调用，与 `run_scheduler_once(runtime, *, now_fn, fetch_candles)` 兼容。
