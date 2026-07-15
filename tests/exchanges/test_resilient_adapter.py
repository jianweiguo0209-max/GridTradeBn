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


# ---- 三路电路 + 写锁（monitor per-grid 并行化前提）----

def test_breakers_per_category_independent():
    # 单端点故障只熔断所属类别：fetch_my_trades(account_read) 打穿电路后，
    # 行情读(market_read)与交易写(trade_write)照常（根治 7-02 单端点拖垮全局事故形态）。
    from gridtrade.exchanges.resilience import CircuitOpenError
    from gridtrade.exchanges.resilient_adapter import default_breakers

    class _Inner2(_Inner):
        def fetch_my_trades(self, symbol, since_ms=None):
            self.calls.append(('fetch_my_trades', symbol))
            self._maybe_fail('fetch_my_trades')
            return []

    brs = {k: CircuitBreaker(failure_threshold=2, cooldown=999.0, clock=lambda: 0.0)
           for k in default_breakers()}
    inner = _Inner2().fail('fetch_my_trades', 99, ccxt.NetworkError('500'))
    ra = _resilient(inner, policy=RetryPolicy(max_attempts=1), breakers=brs)
    for _ in range(2):
        with pytest.raises(ccxt.NetworkError):
            ra.fetch_my_trades('X')
    with pytest.raises(CircuitOpenError):
        ra.fetch_my_trades('X')                    # account_read 电路已开
    assert ra.fetch_price('X') == 123.5            # market_read 不受影响
    assert ra.create_limit_order('X', 'buy', 1.0, 1.0) == 'ORDER'   # trade_write 不受影响


def test_breaker_and_breakers_mutually_exclusive():
    with pytest.raises(ValueError):
        _resilient(_Inner(), breaker=CircuitBreaker(),
                   breakers={'market_read': CircuitBreaker()})


def test_write_calls_serialized_reads_concurrent():
    # HL nonce 约束：写调用绝不并发重叠（全局写锁）；读调用可真并发。
    import threading

    class _Probe:
        name = 'probe'
        def __init__(self):
            self._mu = threading.Lock()
            self.write_inflight = 0
            self.write_max = 0
            self.read_barrier = threading.Barrier(2, timeout=5.0)
            self.read_overlap = True
        def create_limit_order(self, symbol, side, price, size, **kw):
            with self._mu:
                self.write_inflight += 1
                self.write_max = max(self.write_max, self.write_inflight)
            import time as _t; _t.sleep(0.03)
            with self._mu:
                self.write_inflight -= 1
            return 'ORDER'
        def fetch_price(self, symbol):
            try:
                self.read_barrier.wait()   # 两个读线程须同时在内层 → 证明读未被串行
            except threading.BrokenBarrierError:
                self.read_overlap = False
            return 1.0

    probe = _Probe()
    ra = _resilient(probe)
    ws = [threading.Thread(target=lambda: ra.create_limit_order('X', 'buy', 1.0, 1.0))
          for _ in range(4)]
    for t in ws: t.start()
    for t in ws: t.join()
    assert probe.write_max == 1                    # 写从未重叠

    rs = [threading.Thread(target=lambda: ra.fetch_price('X')) for _ in range(2)]
    for t in rs: t.start()
    for t in rs: t.join()
    assert probe.read_overlap is True              # 读确有并发


def test_write_lock_released_during_retry_backoff():
    # 锁包单次尝试而非整个重试循环：一个写在退避 sleep 期间，另一写可进入。
    import threading

    class _FlakyWrite:
        name = 'flaky'
        def __init__(self):
            self.order = []
            self._failed_once = False
        def create_limit_order(self, symbol, side, price, size, **kw):
            self.order.append(side)
            if side == 'buy' and not self._failed_once:
                self._failed_once = True
                raise ccxt.RequestTimeout('flaky')
            return 'ORDER'
        def create_market_order(self, symbol, side, size, **kw):
            self.order.append('mkt-' + side)
            return 'ORDER'

    inner = _FlakyWrite()
    entered_backoff = threading.Event()
    resume = threading.Event()
    def blocking_sleep(d):
        entered_backoff.set()
        assert resume.wait(timeout=5.0)
    ra = ResilientAdapter(inner, policy=FAST, sleep=blocking_sleep)
    t1 = threading.Thread(target=lambda: ra.create_limit_order('X', 'buy', 1.0, 1.0))
    t1.start()
    assert entered_backoff.wait(timeout=5.0)       # t1 已在退避 sleep（锁应已释放）
    done = threading.Event()
    t2 = threading.Thread(target=lambda: (ra.create_market_order('X', 'sell', 1.0),
                                          done.set()))
    t2.start()
    assert done.wait(timeout=5.0)                  # t1 退避期间 t2 的写能完成
    resume.set()
    t1.join(); t2.join()
    assert inner.order == ['buy', 'mkt-sell', 'buy']


def test_account_batch_methods_wrapped_with_categories():
    # _all 读方法走电路：4 个账户读共 account_read 一路，prices_all 归 market_read。
    from gridtrade.exchanges.resilience import CircuitOpenError
    from gridtrade.exchanges.resilient_adapter import default_breakers

    class _Inner3(_Inner):
        def fetch_my_trades_all(self, symbols, since_ms=None):
            self.calls.append(('fetch_my_trades_all', tuple(symbols)))
            self._maybe_fail('fetch_my_trades_all')
            return []
        def fetch_positions_all(self, symbols):
            self.calls.append(('fetch_positions_all', tuple(symbols)))
            return {}
        def fetch_prices_all(self, symbols):
            self.calls.append(('fetch_prices_all', tuple(symbols)))
            return {s: 1.0 for s in symbols}

    brs = {k: CircuitBreaker(failure_threshold=2, cooldown=999.0, clock=lambda: 0.0)
           for k in default_breakers()}
    inner = _Inner3().fail('fetch_my_trades_all', 99, ccxt.NetworkError('down'))
    ra = _resilient(inner, policy=RetryPolicy(max_attempts=1), breakers=brs)
    for _ in range(2):
        with pytest.raises(ccxt.NetworkError):
            ra.fetch_my_trades_all(['X'])
    with pytest.raises(CircuitOpenError):
        ra.fetch_positions_all(['X'])          # 同 account_read 路被熔断
    assert ra.fetch_prices_all(['X']) == {'X': 1.0}   # market_read 不受影响


def test_fetch_max_leverages_delegates_to_inner():
    """lev_caps 接线断层回归(2026-07-12 mainnet 实证):ResilientAdapter 曾漏代理
    fetch_max_leverages → 落到基类默认 {} → lev_caps 静默失效(fail-open 掩盖,
    VVV maxlev=3 开出双格)。包装层必须穿透内层非空 map。"""
    class _MLInner(_Inner):
        def fetch_max_leverages(self):
            self._maybe_fail('fetch_max_leverages')
            self.calls.append(('fetch_max_leverages',))
            return {'VVV/USDC:USDC': 3.0, 'PUMP/USDC:USDC': 10.0}

    inner = _MLInner()
    ra = ResilientAdapter(inner, policy=FAST, sleep=NOSLEEP)
    out = ra.fetch_max_leverages()
    assert out == {'VVV/USDC:USDC': 3.0, 'PUMP/USDC:USDC': 10.0}   # 绝不允许 {}
    assert ('fetch_max_leverages',) in inner.calls


def test_fetch_leverage_tiers_delegates_to_inner():
    """档位表接线断层回归(同 fetch_max_leverages 教训):ResilientAdapter 逐方法显式
    转发、无 __getattr__——漏代理 fetch_leverage_tiers → 落基类默认 [] → open 设杠杆
    永不生效(实盘架空,单测因 GridExecutor 直挂 FakeExchange 抓不到)。包装层必须穿透
    内层非空档位表。"""
    class _LTInner(_Inner):
        def fetch_leverage_tiers(self, symbol):
            self._maybe_fail('fetch_leverage_tiers')
            self.calls.append(('fetch_leverage_tiers', symbol))
            return [{'maxLeverage': 5, 'maxNotional': 5000.0}]

    inner = _LTInner()
    ra = ResilientAdapter(inner, policy=FAST, sleep=NOSLEEP)
    out = ra.fetch_leverage_tiers('X/USDT:USDT')
    assert out == [{'maxLeverage': 5, 'maxNotional': 5000.0}]   # 绝不允许基类默认 []
    assert ('fetch_leverage_tiers', 'X/USDT:USDT') in inner.calls
