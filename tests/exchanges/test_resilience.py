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


def test_fatal_errors_do_not_trip_breaker():
    # 致命错误（BadSymbol/InvalidOrder…）= 交易所有响应、只是该请求永久失败，
    # 不该计入熔断（否则批量遍历坏币会拉垮全局电路、阻塞下单/监控）。
    from gridtrade.exchanges.resilience import call_with_retry, CircuitBreaker
    cb = CircuitBreaker(failure_threshold=2, cooldown=999.0, clock=lambda: 0.0)
    def fn():
        raise ccxt.BadSymbol('no market')
    for _ in range(6):
        with pytest.raises(ccxt.BadSymbol):
            call_with_retry(fn, _policy(max_attempts=1), sleep=lambda d: None,
                            breaker=cb)
    assert cb.allow() is True       # 6 次致命也不开熔断


def test_retry_success_records_breaker_success():
    from gridtrade.exchanges.resilience import call_with_retry, CircuitBreaker
    cb = CircuitBreaker(failure_threshold=2, cooldown=30.0, clock=lambda: 0.0)
    cb.record_failure()                                # 1 次失败（未 open）
    call_with_retry(lambda: 'ok', _policy(), sleep=lambda d: None,
                    rng=random.Random(0), breaker=cb)
    # 成功后失败计数清零：再失败 1 次仍不该 open
    cb.record_failure()
    assert cb.allow() is True
