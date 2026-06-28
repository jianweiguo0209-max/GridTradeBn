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
