# 交易所解耦重构 P4h 实现计划（ResilientAdapter：把健壮性包到真实 adapter 每个调用）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 P4f 的 `call_with_retry`+`CircuitBreaker` 接到真实交易所调用上：`ResilientAdapter` 包一个内层 `ExchangeAdapter`，把每个方法都经退避重试 + 共享熔断，致命错误立即抛、可重试/限频自动退避、绝不 sys.exit。这样 execution/runtime 拿到的 adapter 天然健壮（需求 1 完整接线），且 execution 层仍 ccxt-free。

**Architecture:** `ResilientAdapter(ExchangeAdapter)` 持内层 adapter + `RetryPolicy` + 可选共享 `CircuitBreaker` + 可注入 `sleep/rng`（测试用）。统一私有 `_call(name, *args, **kwargs)` 经 `call_with_retry` 转发内层同名方法；15 个抽象方法 + 可选 `fetch_mark_ohlcv` 各为一行委托。重试安全靠既有 `client_oid` 幂等（下单/撤单重放安全）。`name` 透传内层。

**Tech Stack:** Python 3.9、ccxt（仅经 resilience 间接）、pytest、注入式 sleep/rng（无真实睡眠/网络）。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（哪些方法该包、签名、熔断共享口径、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；只新增 `gridtrade/exchanges/resilient_adapter.py` 及 `tests/exchanges/test_resilient_adapter.py`；不改其它文件。
- 复用 P4f：`from gridtrade.exchanges.resilience import RetryPolicy, CircuitBreaker, call_with_retry`。
- `ResilientAdapter` 子类化 `ExchangeAdapter`，必须实现全部 15 个抽象方法（否则 ABC 无法实例化）+ 覆盖可选 `fetch_mark_ohlcv`；每个方法签名**逐字对齐** base.py（含关键字参数 post_only/reduce_only/client_oid/since_ms 默认值）。
- 每个方法体仅 `return self._call('<name>', <args...>)`（透传位置与关键字参数）；不改变返回值。
- 测试用最小内层 stub（鸭子类型，仅实现被测方法）；sleep 注入 no-op，**测试零真实睡眠**。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。全量回归须保持绿（基线 184 passed）。

---

## 文件结构（本计划新建）

```
gridtrade/exchanges/
  resilient_adapter.py   # ResilientAdapter(ExchangeAdapter)
tests/exchanges/
  test_resilient_adapter.py
```

公共接口：

```python
class ResilientAdapter(ExchangeAdapter):
    def __init__(self, inner, *, policy=None, breaker=None,
                 sleep=time.sleep, rng=None): ...
    # 实现 ExchangeAdapter 全部方法，逐一经 call_with_retry 转发
```

---

### Task 1: ResilientAdapter（全方法委托 + 重试/熔断/透传）

**Files:**
- Create: `gridtrade/exchanges/resilient_adapter.py`
- Create: `tests/exchanges/test_resilient_adapter.py`

**Interfaces:**
- Consumes: `gridtrade.exchanges.base.ExchangeAdapter`、`gridtrade.exchanges.resilience.{RetryPolicy, CircuitBreaker, call_with_retry}`、内层 adapter 同名方法。
- Produces: `ResilientAdapter`。

- [ ] **Step 1: 写失败测试**

Create `tests/exchanges/test_resilient_adapter.py`:

```python
import ccxt
import pytest

from gridtrade.exchanges.resilience import RetryPolicy, CircuitBreaker
from gridtrade.exchanges.resilient_adapter import ResilientAdapter

NOSLEEP = lambda d: None
FAST = RetryPolicy(max_attempts=4, base_delay=0.01, rate_limit_base_delay=0.01,
                   max_delay=0.01)


class _Inner:
    """最小内层 stub：可编排某方法失败 N 次后成功，并记录收到的参数。"""
    name = 'inner-ex'

    def __init__(self):
        self.calls = []
        self._fail = {}        # method -> [remaining_fail_count, exc]

    def fail(self, method, times, exc):
        self._fail[method] = [times, exc]
        return self

    def _maybe_fail(self, method):
        spec = self._fail.get(method)
        if spec and spec[0] > 0:
            spec[0] -= 1
            raise spec[1]

    def fetch_price(self, symbol):
        self.calls.append(('fetch_price', symbol))
        self._maybe_fail('fetch_price')
        return 123.5

    def fetch_balance(self):
        self.calls.append(('fetch_balance',))
        self._maybe_fail('fetch_balance')
        return 'BAL'

    def create_limit_order(self, symbol, side, price, size, *, post_only=False,
                           reduce_only=False, client_oid=None):
        self.calls.append(('create_limit_order', symbol, side, price, size,
                           post_only, reduce_only, client_oid))
        self._maybe_fail('create_limit_order')
        return 'ORDER'


def _resilient(inner, **kw):
    base = dict(policy=FAST, sleep=NOSLEEP)
    base.update(kw)
    return ResilientAdapter(inner, **base)


def test_retryable_method_retries_then_succeeds():
    inner = _Inner().fail('fetch_price', 2, ccxt.RequestTimeout('x'))
    out = _resilient(inner).fetch_price('BTC/USDT:USDT')
    assert out == 123.5
    assert sum(1 for c in inner.calls if c[0] == 'fetch_price') == 3


def test_fatal_method_raises_immediately():
    inner = _Inner().fail('fetch_balance', 5, ccxt.AuthenticationError('bad key'))
    with pytest.raises(ccxt.AuthenticationError):
        _resilient(inner).fetch_balance()
    assert sum(1 for c in inner.calls if c[0] == 'fetch_balance') == 1


def test_kwargs_passthrough_on_write_method():
    inner = _Inner()
    out = _resilient(inner).create_limit_order(
        'BTC/USDT:USDT', 'buy', 100.0, 0.5, post_only=True, client_oid='g:1:0')
    assert out == 'ORDER'
    rec = [c for c in inner.calls if c[0] == 'create_limit_order'][0]
    # ('create_limit_order', symbol, side, price, size, post_only, reduce_only, client_oid)
    assert rec == ('create_limit_order', 'BTC/USDT:USDT', 'buy', 100.0, 0.5,
                   True, False, 'g:1:0')


def test_name_passthrough():
    assert _resilient(_Inner()).name == 'inner-ex'


def test_shared_breaker_trips_across_calls_then_blocks():
    from gridtrade.exchanges.resilience import CircuitOpenError
    cb = CircuitBreaker(failure_threshold=3, cooldown=999.0, clock=lambda: 0.0)
    inner = _Inner().fail('fetch_price', 99, ccxt.NetworkError('down'))
    ra = _resilient(inner, policy=RetryPolicy(max_attempts=1), breaker=cb)
    # 每次调用 1 次尝试即失败并记一次熔断失败；3 次后熔断 open
    for _ in range(3):
        with pytest.raises(ccxt.NetworkError):
            ra.fetch_price('X')
    # 熔断已 open：再调直接 CircuitOpenError，不触达内层
    before = len([c for c in inner.calls if c[0] == 'fetch_price'])
    with pytest.raises(CircuitOpenError):
        ra.fetch_price('X')
    after = len([c for c in inner.calls if c[0] == 'fetch_price'])
    assert after == before


def test_is_exchange_adapter_instance():
    from gridtrade.exchanges.base import ExchangeAdapter
    assert isinstance(_resilient(_Inner()), ExchangeAdapter)
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_resilient_adapter.py -q`
Expected: FAIL（`ModuleNotFoundError: gridtrade.exchanges.resilient_adapter`）。

- [ ] **Step 3: 实现 ResilientAdapter**

Create `gridtrade/exchanges/resilient_adapter.py`:

```python
"""ResilientAdapter：把 P4f 健壮性（退避重试 + 熔断）包到内层 ExchangeAdapter 每个调用。

execution/runtime 拿到本适配器即天然健壮（需求 1）。重试安全靠 client_oid 幂等。
"""
import time
from typing import List, Optional

import pandas as pd

from gridtrade.exchanges.base import (Balance, ExchangeAdapter, FundingPayment,
                                      Instrument, Order, Position, Trade)
from gridtrade.exchanges.resilience import RetryPolicy, call_with_retry


class ResilientAdapter(ExchangeAdapter):
    def __init__(self, inner, *, policy=None, breaker=None,
                 sleep=time.sleep, rng=None):
        self._inner = inner
        self.name = getattr(inner, 'name', 'resilient')
        self._policy = policy or RetryPolicy()
        self._breaker = breaker
        self._sleep = sleep
        self._rng = rng

    def _call(self, _name, *args, **kwargs):
        inner_fn = getattr(self._inner, _name)
        return call_with_retry(lambda: inner_fn(*args, **kwargs), self._policy,
                               sleep=self._sleep, rng=self._rng,
                               breaker=self._breaker)

    # ---- 行情（公共）----
    def list_instruments(self) -> List[Instrument]:
        return self._call('list_instruments')

    def fetch_ohlcv(self, symbol: str, timeframe: str,
                    start_ms: int, end_ms: int) -> pd.DataFrame:
        return self._call('fetch_ohlcv', symbol, timeframe, start_ms, end_ms)

    def fetch_funding_history(self, symbol: str,
                             start_ms: int, end_ms: int) -> pd.DataFrame:
        return self._call('fetch_funding_history', symbol, start_ms, end_ms)

    def fetch_price(self, symbol: str) -> float:
        return self._call('fetch_price', symbol)

    # ---- 账户/交易（私有）----
    def fetch_balance(self) -> Balance:
        return self._call('fetch_balance')

    def fetch_positions(self, symbol: str) -> Position:
        return self._call('fetch_positions', symbol)

    def create_limit_order(self, symbol: str, side: str, price: float, size: float,
                           *, post_only: bool = False, reduce_only: bool = False,
                           client_oid: Optional[str] = None) -> Order:
        return self._call('create_limit_order', symbol, side, price, size,
                          post_only=post_only, reduce_only=reduce_only,
                          client_oid=client_oid)

    def create_market_order(self, symbol: str, side: str, size: float,
                            *, reduce_only: bool = False,
                            client_oid: Optional[str] = None) -> Order:
        return self._call('create_market_order', symbol, side, size,
                          reduce_only=reduce_only, client_oid=client_oid)

    def cancel_order(self, symbol: str, order_id: str) -> None:
        return self._call('cancel_order', symbol, order_id)

    def cancel_all(self, symbol: str) -> None:
        return self._call('cancel_all', symbol)

    def fetch_open_orders(self, symbol: str) -> List[Order]:
        return self._call('fetch_open_orders', symbol)

    def fetch_my_trades(self, symbol: str,
                        since_ms: Optional[int] = None) -> List[Trade]:
        return self._call('fetch_my_trades', symbol, since_ms=since_ms)

    def set_leverage(self, symbol: str, leverage: float) -> None:
        return self._call('set_leverage', symbol, leverage)

    def exchange_status(self) -> str:
        return self._call('exchange_status')

    def fetch_funding_payments(self, symbol: str,
                               since_ms: Optional[int] = None) -> List[FundingPayment]:
        return self._call('fetch_funding_payments', symbol, since_ms=since_ms)

    # ---- 可选：标记价 K线 ----
    def fetch_mark_ohlcv(self, symbol: str, timeframe: str,
                         start_ms: int, end_ms: int) -> pd.DataFrame:
        return self._call('fetch_mark_ohlcv', symbol, timeframe, start_ms, end_ms)
```

- [ ] **Step 4: 跑测试确认绿 + 全量回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_resilient_adapter.py -q`
Expected: 全 PASS（6）。

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全量 PASS（≥ 184 + 新增）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/resilient_adapter.py tests/exchanges/test_resilient_adapter.py
git commit -m "feat(exchanges): ResilientAdapter wraps adapter calls with retry+breaker (P4h)"
```

---

## Self-Review

- **Spec 覆盖**：design.md §8「所有交易所调用：超时+退避+错误分类+熔断；绝不 sys.exit」—— ResilientAdapter 把 P4f 包到全部 15 个 adapter 方法 + 可选 mark。
- **签名对齐**：每个方法逐字对齐 base.py（含 `*` 后 post_only/reduce_only/client_oid/since_ms 默认值）；`_call` 透传位置与关键字参数。
- **幂等前提**：写方法重试安全靠 client_oid 幂等（既有约束）。
- **可测性**：sleep 注入 no-op、FAST policy，测试零真实睡眠；isinstance(ExchangeAdapter) 验证 ABC 全实现。
- **Placeholder 扫描**：无 TBD/TODO；每步完整代码 + 精确命令/预期。
- **类型一致**：`ResilientAdapter.__init__(inner, *, policy, breaker, sleep, rng)` 与测试 `_resilient` 一致；`_call` 经 `call_with_retry(fn, policy, sleep, rng, breaker)`（P4f 签名）。
