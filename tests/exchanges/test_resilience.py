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


def test_binance_418_ban_classified_rate_limit():
    # 币安 418(IP ban)/-1003 → ccxt DDoSProtection/RateLimitExceeded → rate_limit
    # （退避用 rate_limit_base_delay 长冷却，spec 2026-07-14 §九）
    import ccxt
    from gridtrade.exchanges.resilience import classify_error
    assert classify_error(ccxt.DDoSProtection('418 I am a teapot')) == 'rate_limit'
    assert classify_error(ccxt.RateLimitExceeded('-1003 TOO_MANY_REQUESTS')) == 'rate_limit'


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


def test_resilient_forwards_fetch_leverage_tiers_map():
    # 基类默认 {} 会静默吸掉全量档位表(同 fetch_leverage_tiers/max_leverage 教训 b55a632)
    from gridtrade.exchanges.resilient_adapter import ResilientAdapter

    class _Inner:
        def fetch_leverage_tiers_map(self):
            return {'X/USDT:USDT': [{'maxLeverage': 10, 'maxNotional': 1.0}]}

    ra = ResilientAdapter(_Inner())
    assert ra.fetch_leverage_tiers_map()['X/USDT:USDT'][0]['maxLeverage'] == 10


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


def test_rate_limit_burst_absorbed_by_retry_does_not_open_breaker():
    # 429 单次尝试中性：突发被退避吸收（重试内成功）→ 熔断零计数。
    # 差分 load-bearing：旧逻辑每次尝试 record_failure，threshold=1 会立即 open。
    from gridtrade.exchanges.resilience import CircuitBreaker, call_with_retry
    breaker = CircuitBreaker(failure_threshold=1, cooldown=30.0)
    calls = {'n': 0}
    def fn():
        calls['n'] += 1
        if calls['n'] < 3:
            raise ccxt.RateLimitExceeded('429')
        return 'ok'
    out = call_with_retry(fn, _policy(), sleep=lambda s: None,
                          rng=random.Random(0), breaker=breaker)
    assert out == 'ok'
    assert breaker.allow() is True          # 电路未开（旧逻辑此处已 open）


def test_rate_limit_exhausted_counts_once_into_breaker():
    # 429 重试耗尽仍失败 → 恰好计 1 次熔断（持续不可用该开电路→MarginGate fail-closed
    # 挡开仓，避免在残缺市场数据上选币）。threshold=1 下单次耗尽即 open 可直接断言。
    from gridtrade.exchanges.resilience import CircuitBreaker, call_with_retry
    breaker = CircuitBreaker(failure_threshold=1, cooldown=30.0)
    def fn():
        raise ccxt.RateLimitExceeded('still 429')
    with pytest.raises(ccxt.RateLimitExceeded):
        call_with_retry(fn, _policy(max_attempts=3), sleep=lambda s: None,
                        rng=random.Random(0), breaker=breaker)
    assert breaker.allow() is False         # 耗尽计入 → 电路 open


def test_network_error_still_counts_every_attempt():
    # 真网络故障行为不变：每次尝试都计数（threshold=2、max_attempts=4 → 第 2 次尝试后即 open，
    # 后续 allow()=False 提前抛 CircuitOpenError）。
    from gridtrade.exchanges.resilience import (CircuitBreaker, CircuitOpenError,
                                                call_with_retry)
    breaker = CircuitBreaker(failure_threshold=2, cooldown=30.0)
    def fn():
        raise ccxt.NetworkError('down')
    with pytest.raises((ccxt.NetworkError, CircuitOpenError)):
        call_with_retry(fn, _policy(max_attempts=4), sleep=lambda s: None,
                        rng=random.Random(0), breaker=breaker)
    assert breaker.allow() is False


# ---- 并发语义（monitor per-grid 并行化前提）----

def test_half_open_allows_exactly_one_probe_across_threads():
    # 半开只放一个探针：并发 allow() 恰好一个 True，其余 False（旧无锁版会放一群探针）。
    import threading
    from gridtrade.exchanges.resilience import CircuitBreaker
    clk = _Clock()
    cb = CircuitBreaker(failure_threshold=1, cooldown=30.0, clock=clk)
    cb.record_failure()                 # open
    clk.t = 30.0                        # 冷却到点
    results = []
    barrier = threading.Barrier(8)
    def worker():
        barrier.wait()
        results.append(cb.allow())
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sum(results) == 1


def test_probe_thread_reentry_allowed_other_threads_blocked():
    # 探针线程自身可重入（429 中性重试期间 allow 会被再次调用），别的线程仍被挡。
    import threading
    from gridtrade.exchanges.resilience import CircuitBreaker
    clk = _Clock()
    cb = CircuitBreaker(failure_threshold=1, cooldown=30.0, clock=clk)
    cb.record_failure()
    clk.t = 30.0
    assert cb.allow() is True           # 本线程取得探针
    assert cb.allow() is True           # 同线程重入放行（重试循环第 2 次尝试）
    other = []
    t = threading.Thread(target=lambda: other.append(cb.allow()))
    t.start(); t.join()
    assert other == [False]             # 他线程被挡
    cb.record_success()                 # 探针成功 → 全关
    t2 = threading.Thread(target=lambda: other.append(cb.allow()))
    t2.start(); t2.join()
    assert other == [False, True]


def test_probe_failure_reopens_and_next_cooldown_grants_new_probe():
    import threading
    from gridtrade.exchanges.resilience import CircuitBreaker
    clk = _Clock()
    cb = CircuitBreaker(failure_threshold=1, cooldown=30.0, clock=clk)
    cb.record_failure()
    clk.t = 30.0
    assert cb.allow() is True           # 探针
    cb.record_failure()                 # 探针失败 → 重新 open（t=30）
    assert cb.allow() is False
    clk.t = 60.0
    got = []
    t = threading.Thread(target=lambda: got.append(cb.allow()))
    t.start(); t.join()
    assert got == [True]                # 新冷却到点，其他线程也能拿到新探针
