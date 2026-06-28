# 交易所解耦重构 P4f 实现计划（健壮性核心：错误分类 + 退避重试 + 熔断，降级不 sys.exit）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现需求 1 的健壮性地基（design.md §8）：交易所调用的**错误分类**（可重试/限频/致命）+ **指数退避+抖动重试** + **熔断**，**绝不 `sys.exit`**（取代 legacy retry_wrapper 的"失败即硬退出"）。守护进程后续用它包装所有 adapter 调用，失败时降级告警 + 续跑。本增量是纯函数式可注入组件，时钟/睡眠/随机全注入，确定性 TDD。

**Architecture:** 放 `gridtrade/exchanges/resilience.py`（exchanges 层，可 import ccxt；execution/runtime 层保持 ccxt-free，只拿到被包装好的 adapter/调用结果）。`classify_error` 按 ccxt 异常层级映射类别（注意 RateLimitExceeded/DDoSProtection 是 NetworkError 子类，须先判限频再判通用可重试）。`CircuitBreaker` 连续失败达阈值则 open、冷却后 half-open 试探。`call_with_retry` 编排：熔断放行检查 → 调用 → 成功记成功/返回；异常按类别处理（致命立即抛、可重试/限频退避重试、耗尽抛最后异常），全程不吞 `KeyboardInterrupt/SystemExit`、不 `sys.exit`。

**Tech Stack:** Python 3.9、ccxt 4.5.61（异常类型）、pytest、注入式 sleep/clock/rng（无真实睡眠、无网络）。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（错误归类边界、熔断状态机、退避公式、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；只新增 `gridtrade/exchanges/resilience.py` 及 `tests/exchanges/test_resilience.py`；不改其它任何文件。
- `gridtrade/exchanges/` 可 import ccxt；`gridtrade/execution/`、`gridtrade/runtime/` 仍不得 import ccxt（本模块不被 core/state import）。
- **绝不 `sys.exit`、绝不吞 `BaseException`**：只捕获 `Exception`（ccxt 错误均 Exception 子类；KeyboardInterrupt/SystemExit 是 BaseException，自然向上传播）。
- 错误分类口径（ccxt 4.5.61，已实测层级）：
  - `'rate_limit'`：`ccxt.RateLimitExceeded` / `ccxt.DDoSProtection`（更长退避基数）。**须在通用 NetworkError 之前判**（二者也是 NetworkError 子类）。
  - `'retryable'`：其余 `ccxt.NetworkError`（含 `RequestTimeout`/`ExchangeNotAvailable`/`OnMaintenance`）。
  - `'fatal'`：`ccxt.ExchangeError`（`AuthenticationError`/`InsufficientFunds`/`InvalidOrder`/`BadRequest`/`PermissionDenied` 等）及一切非 ccxt 异常（默认不重试）。
- 重试安全性依赖既有 `client_oid` 幂等（下单/撤单重放安全），故统一重试策略可接受。
- 退避公式：`delay = min(max_delay, base * 2**(attempt-1)) * (0.5 + rng.random()*0.5)`（full-ish jitter，attempt 从 1 计）；`base` 取 `rate_limit_base_delay`（限频）或 `base_delay`（可重试）。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。全量回归须保持绿（基线 163 passed）。

---

## 文件结构（本计划新建）

```
gridtrade/exchanges/
  resilience.py   # classify_error / CircuitOpenError / RetryPolicy / CircuitBreaker / call_with_retry
tests/exchanges/
  test_resilience.py
```

公共接口：

```python
def classify_error(exc: Exception) -> str: ...   # 'rate_limit' | 'retryable' | 'fatal'

class CircuitOpenError(Exception): ...

@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    rate_limit_base_delay: float = 2.0
    max_delay: float = 8.0

class CircuitBreaker:
    def __init__(self, failure_threshold=5, cooldown=30.0, clock=time.monotonic): ...
    def allow(self) -> bool: ...
    def record_success(self) -> None: ...
    def record_failure(self) -> None: ...

def call_with_retry(fn, policy, *, classify=classify_error, sleep=time.sleep,
                    rng=None, breaker=None): ...   # 返回 fn() 结果或抛出
```

---

### Task 1: classify_error（ccxt 异常 → 类别）

**Files:**
- Create: `gridtrade/exchanges/resilience.py`
- Create: `tests/exchanges/test_resilience.py`

**Interfaces:**
- Produces: `classify_error(exc) -> str`。
- Consumes: `ccxt` 异常类型。

- [ ] **Step 1: 写失败测试**

Create `tests/exchanges/test_resilience.py`:

```python
import ccxt
import pytest

from gridtrade.exchanges.resilience import classify_error


@pytest.mark.parametrize('exc', [
    ccxt.RequestTimeout('t'),
    ccxt.ExchangeNotAvailable('t'),
    ccxt.OnMaintenance('t'),
    ccxt.NetworkError('t'),
])
def test_classify_retryable(exc):
    assert classify_error(exc) == 'retryable'


@pytest.mark.parametrize('exc', [
    ccxt.RateLimitExceeded('t'),
    ccxt.DDoSProtection('t'),
])
def test_classify_rate_limit(exc):
    assert classify_error(exc) == 'rate_limit'


@pytest.mark.parametrize('exc', [
    ccxt.AuthenticationError('t'),
    ccxt.InsufficientFunds('t'),
    ccxt.InvalidOrder('t'),
    ccxt.BadRequest('t'),
    ValueError('not ccxt'),
])
def test_classify_fatal(exc):
    assert classify_error(exc) == 'fatal'
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_resilience.py -q`
Expected: FAIL（`ModuleNotFoundError: gridtrade.exchanges.resilience`）。

- [ ] **Step 3: 实现 classify_error**

Create `gridtrade/exchanges/resilience.py`:

```python
"""交易所调用健壮性：错误分类 + 退避重试 + 熔断（需求 1）。

绝不 sys.exit、绝不吞 BaseException。守护进程用 call_with_retry 包装所有 adapter
调用，耗尽/致命时由上层降级告警 + 续跑（不硬退出）。本模块在 exchanges 层，可 import
ccxt；execution/runtime 层只拿被包装的结果，保持 ccxt-free。
"""
import time
from dataclasses import dataclass

import ccxt


def classify_error(exc: Exception) -> str:
    # RateLimitExceeded/DDoSProtection 也是 NetworkError 子类，须先判限频。
    if isinstance(exc, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
        return 'rate_limit'
    if isinstance(exc, ccxt.NetworkError):
        return 'retryable'
    if isinstance(exc, ccxt.ExchangeError):
        return 'fatal'
    return 'fatal'
```

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_resilience.py -q`
Expected: 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/resilience.py tests/exchanges/test_resilience.py
git commit -m "feat(exchanges): classify_error for ccxt error categories (P4f)"
```

---

### Task 2: CircuitBreaker（连续失败熔断 + 冷却 half-open）

**Files:**
- Modify: `gridtrade/exchanges/resilience.py`
- Modify: `tests/exchanges/test_resilience.py`

**Interfaces:**
- Produces: `CircuitBreaker(failure_threshold=5, cooldown=30.0, clock=time.monotonic)`，`allow()`、`record_success()`、`record_failure()`。

- [ ] **Step 1: 写失败测试**

在 `tests/exchanges/test_resilience.py` 末尾追加：

```python
class _Clock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t


def test_breaker_opens_after_threshold_then_blocks():
    from gridtrade.exchanges.resilience import CircuitBreaker
    clk = _Clock()
    cb = CircuitBreaker(failure_threshold=3, cooldown=30.0, clock=clk)
    assert cb.allow() is True
    cb.record_failure(); cb.record_failure()
    assert cb.allow() is True          # 未达阈值
    cb.record_failure()                # 第 3 次 -> open
    assert cb.allow() is False


def test_breaker_half_open_after_cooldown_and_close_on_success():
    from gridtrade.exchanges.resilience import CircuitBreaker
    clk = _Clock()
    cb = CircuitBreaker(failure_threshold=2, cooldown=30.0, clock=clk)
    cb.record_failure(); cb.record_failure()   # open
    assert cb.allow() is False
    clk.t = 30.0                                # 冷却到点 -> half-open 放行
    assert cb.allow() is True
    cb.record_success()                         # 试探成功 -> closed，失败计数清零
    assert cb.allow() is True


def test_breaker_reopens_on_failure_during_half_open():
    from gridtrade.exchanges.resilience import CircuitBreaker
    clk = _Clock()
    cb = CircuitBreaker(failure_threshold=2, cooldown=30.0, clock=clk)
    cb.record_failure(); cb.record_failure()   # open at t=0
    clk.t = 30.0
    assert cb.allow() is True                   # half-open
    cb.record_failure()                         # 试探失败 -> 重新 open（在 t=30）
    assert cb.allow() is False
    clk.t = 59.9
    assert cb.allow() is False                  # 冷却未到
    clk.t = 60.0
    assert cb.allow() is True                   # 再次 half-open
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_resilience.py -k breaker -q`
Expected: FAIL（`ImportError: cannot import name 'CircuitBreaker'`）。

- [ ] **Step 3: 实现 CircuitBreaker**

在 `gridtrade/exchanges/resilience.py` 末尾追加：

```python
class CircuitBreaker:
    """连续失败达阈值则 open；冷却到点 half-open 放行一次试探；成功 close、失败重 open。"""

    def __init__(self, failure_threshold=5, cooldown=30.0, clock=time.monotonic):
        self.failure_threshold = int(failure_threshold)
        self.cooldown = float(cooldown)
        self.clock = clock
        self._failures = 0
        self._opened_at = None
        self._half_open = False

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if self.clock() - self._opened_at >= self.cooldown:
            self._half_open = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._half_open = False

    def record_failure(self) -> None:
        if self._half_open:
            self._opened_at = self.clock()   # 试探失败 -> 重新 open
            self._half_open = False
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = self.clock()
```

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_resilience.py -q`
Expected: 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/resilience.py tests/exchanges/test_resilience.py
git commit -m "feat(exchanges): CircuitBreaker open/half-open state machine (P4f)"
```

---

### Task 3: RetryPolicy + call_with_retry（退避重试编排，绝不 sys.exit）

**Files:**
- Modify: `gridtrade/exchanges/resilience.py`
- Modify: `tests/exchanges/test_resilience.py`

**Interfaces:**
- Consumes: `classify_error`、`CircuitBreaker`、`CircuitOpenError`。
- Produces: `RetryPolicy`、`CircuitOpenError`、`call_with_retry(fn, policy, *, classify=classify_error, sleep=time.sleep, rng=None, breaker=None)`。

- [ ] **Step 1: 写失败测试**

在 `tests/exchanges/test_resilience.py` 末尾追加：

```python
import random


def _policy(**kw):
    from gridtrade.exchanges.resilience import RetryPolicy
    base = dict(max_attempts=4, base_delay=0.5, rate_limit_base_delay=2.0, max_delay=8.0)
    base.update(kw)
    return RetryPolicy(**base)


def test_retry_returns_on_first_success():
    from gridtrade.exchanges.resilience import call_with_retry
    sleeps = []
    out = call_with_retry(lambda: 42, _policy(), sleep=sleeps.append,
                          rng=random.Random(0))
    assert out == 42 and sleeps == []


def test_retry_retries_retryable_then_succeeds():
    from gridtrade.exchanges.resilience import call_with_retry
    calls = {'n': 0}
    def fn():
        calls['n'] += 1
        if calls['n'] < 3:
            raise ccxt.RequestTimeout('boom')
        return 'ok'
    sleeps = []
    out = call_with_retry(fn, _policy(), sleep=sleeps.append, rng=random.Random(0))
    assert out == 'ok' and calls['n'] == 3 and len(sleeps) == 2


def test_retry_fatal_raises_immediately_without_retry():
    from gridtrade.exchanges.resilience import call_with_retry
    calls = {'n': 0}
    def fn():
        calls['n'] += 1
        raise ccxt.InsufficientFunds('no money')
    sleeps = []
    with pytest.raises(ccxt.InsufficientFunds):
        call_with_retry(fn, _policy(), sleep=sleeps.append, rng=random.Random(0))
    assert calls['n'] == 1 and sleeps == []


def test_retry_exhausts_and_raises_last_error():
    from gridtrade.exchanges.resilience import call_with_retry
    calls = {'n': 0}
    def fn():
        calls['n'] += 1
        raise ccxt.NetworkError('still down')
    sleeps = []
    with pytest.raises(ccxt.NetworkError):
        call_with_retry(fn, _policy(max_attempts=4), sleep=sleeps.append,
                        rng=random.Random(0))
    assert calls['n'] == 4 and len(sleeps) == 3        # 4 次尝试、3 次退避
    assert all(0 < d <= 8.0 for d in sleeps)           # 退避有界


def test_retry_rate_limit_uses_larger_base():
    from gridtrade.exchanges.resilience import call_with_retry
    def fn():
        raise ccxt.RateLimitExceeded('429')
    sleeps = []
    with pytest.raises(ccxt.RateLimitExceeded):
        call_with_retry(fn, _policy(max_attempts=2, base_delay=0.5,
                                    rate_limit_base_delay=2.0),
                        sleep=sleeps.append, rng=random.Random(0))
    # 限频首退避基数=2.0 -> delay in [1.0, 2.0]（>普通 base_delay 0.5 的上界）
    assert len(sleeps) == 1 and sleeps[0] >= 1.0


def test_retry_open_breaker_raises_circuit_open_without_calling():
    from gridtrade.exchanges.resilience import (call_with_retry, CircuitBreaker,
                                               CircuitOpenError)
    cb = CircuitBreaker(failure_threshold=1, cooldown=999.0, clock=lambda: 0.0)
    cb.record_failure()                                # open
    calls = {'n': 0}
    def fn():
        calls['n'] += 1
        return 'x'
    with pytest.raises(CircuitOpenError):
        call_with_retry(fn, _policy(), sleep=lambda d: None,
                        rng=random.Random(0), breaker=cb)
    assert calls['n'] == 0                              # 熔断时根本不调用


def test_retry_success_records_breaker_success():
    from gridtrade.exchanges.resilience import call_with_retry, CircuitBreaker
    cb = CircuitBreaker(failure_threshold=2, cooldown=30.0, clock=lambda: 0.0)
    cb.record_failure()                                # 1 次失败（未 open）
    call_with_retry(lambda: 'ok', _policy(), sleep=lambda d: None,
                    rng=random.Random(0), breaker=cb)
    # 成功后失败计数清零：再失败 1 次仍不该 open
    cb.record_failure()
    assert cb.allow() is True
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_resilience.py -k "retry" -q`
Expected: FAIL（`ImportError: cannot import name 'call_with_retry'` / `RetryPolicy`）。

- [ ] **Step 3: 实现 RetryPolicy + call_with_retry**

在 `gridtrade/exchanges/resilience.py` 顶部（classify_error 之后）加 `RetryPolicy` 与 `CircuitOpenError`，并在文件末尾加 `call_with_retry`：

```python
@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    rate_limit_base_delay: float = 2.0
    max_delay: float = 8.0


class CircuitOpenError(Exception):
    pass
```

```python
import random as _random


def call_with_retry(fn, policy, *, classify=classify_error, sleep=time.sleep,
                    rng=None, breaker=None):
    """调用 fn()，按 policy 退避重试。绝不 sys.exit、绝不吞 BaseException。

    熔断 open -> 抛 CircuitOpenError（不调用 fn）；致命 -> 立即抛；可重试/限频 ->
    退避重试，耗尽抛最后异常。成功/失败都喂给 breaker。
    """
    if rng is None:
        rng = _random.Random()
    attempt = 0
    while True:
        if breaker is not None and not breaker.allow():
            raise CircuitOpenError('circuit open')
        try:
            result = fn()
        except Exception as exc:        # 只捕 Exception；BaseException(如 KeyboardInterrupt)自然上抛
            kind = classify(exc)
            if breaker is not None:
                breaker.record_failure()
            if kind == 'fatal':
                raise
            attempt += 1
            if attempt >= policy.max_attempts:
                raise
            base = (policy.rate_limit_base_delay if kind == 'rate_limit'
                    else policy.base_delay)
            raw = min(policy.max_delay, base * (2 ** (attempt - 1)))
            sleep(raw * (0.5 + rng.random() * 0.5))
            continue
        else:
            if breaker is not None:
                breaker.record_success()
            return result
```

- [ ] **Step 4: 跑测试确认绿 + 全量回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_resilience.py -q`
Expected: 全 PASS。

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全量 PASS（≥ 163 + 新增 resilience 测试）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/resilience.py tests/exchanges/test_resilience.py
git commit -m "feat(exchanges): RetryPolicy + call_with_retry (backoff+jitter, no sys.exit) (P4f)"
```

---

## Self-Review

- **Spec 覆盖**：design.md §8 健壮性 —— 错误分类（可重试/限频/致命，Task 1）+ 指数退避+抖动重试（Task 3）+ 熔断（Task 2）+ **绝不 sys.exit**（Task 3 只捕 Exception、耗尽抛而非退出，上层降级续跑）。
- **ccxt 层级正确性**：RateLimitExceeded/DDoSProtection 先于通用 NetworkError 判（实测二者是 NetworkError 子类）。
- **确定性**：clock/sleep/rng 全注入，无真实睡眠/网络；退避有界 `≤ max_delay`。
- **幂等前提**：重试安全靠既有 client_oid 幂等（下单/撤单重放安全）。
- **Placeholder 扫描**：无 TBD/TODO；每步完整代码 + 精确命令/预期。
- **类型一致**：`call_with_retry` 签名（fn/policy/classify/sleep/rng/breaker）与测试一致；`RetryPolicy` 字段（max_attempts/base_delay/rate_limit_base_delay/max_delay）一致；`CircuitBreaker` 方法（allow/record_success/record_failure）一致。
