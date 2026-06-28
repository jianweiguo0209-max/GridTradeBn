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
