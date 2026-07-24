"""权重遥测：CcxtAdapter.used_weight_1m 读 header + ResilientAdapter 计数/分钟上报。"""
import threading
from types import SimpleNamespace

from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
from gridtrade.exchanges.resilience import RetryPolicy
from gridtrade.exchanges.resilient_adapter import ResilientAdapter

NOSLEEP = lambda d: None
FAST = RetryPolicy(max_attempts=4, base_delay=0.01, rate_limit_base_delay=0.01,
                   max_delay=0.01)


def _ccxt_with_headers(headers):
    client = SimpleNamespace(id='binanceusdm', last_response_headers=headers)
    return CcxtAdapter(client)


def test_used_weight_reads_header_case_insensitive():
    assert _ccxt_with_headers({'X-MBX-USED-WEIGHT-1M': '1106'}).used_weight_1m() == 1106
    assert _ccxt_with_headers({'x-mbx-used-weight-1m': '53'}).used_weight_1m() == 53


def test_used_weight_none_when_header_missing_or_bad():
    assert _ccxt_with_headers({}).used_weight_1m() is None
    assert _ccxt_with_headers(None).used_weight_1m() is None
    assert _ccxt_with_headers({'X-MBX-USED-WEIGHT-1M': 'nan?'}).used_weight_1m() is None


class _Inner:
    name = 'inner-ex'

    def fetch_price(self, symbol):
        return 123.5

    def fetch_balance(self):
        return 'BAL'


def test_report_on_minute_rollover_then_noop_same_minute():
    adp = ResilientAdapter(_Inner(), policy=FAST, sleep=NOSLEEP)
    adp.fetch_price('X'); adp.fetch_price('X'); adp.fetch_balance()
    lines = []
    adp.report_weight(log=lines.append, now=60.0)    # 首次翻转 → 打点
    assert len(lines) == 1
    assert 'fetch_price=2' in lines[0] and 'fetch_balance=1' in lines[0]
    assert 'w1m=?' in lines[0]                       # inner 无 used_weight_1m → 优雅降级
    adp.report_weight(log=lines.append, now=90.0)    # 同一分钟 → no-op
    assert len(lines) == 1
    adp.report_weight(log=lines.append, now=120.0)   # 新分钟但计数已清零 → 静默
    assert len(lines) == 1
    adp.fetch_price('Y')
    adp.report_weight(log=lines.append, now=180.0)   # 有新调用 → 再打点
    assert len(lines) == 2 and 'fetch_price=1' in lines[1]


def test_report_includes_inner_used_weight():
    inner = _Inner()
    inner.used_weight_1m = lambda: 1106
    adp = ResilientAdapter(inner, policy=FAST, sleep=NOSLEEP)
    adp.fetch_price('X')
    lines = []
    adp.report_weight(log=lines.append, now=60.0)
    assert 'w1m=1106' in lines[0]


def test_report_never_raises_on_internal_failure():
    inner = _Inner()

    def _boom():
        raise RuntimeError('boom')
    inner.used_weight_1m = _boom
    adp = ResilientAdapter(inner, policy=FAST, sleep=NOSLEEP)
    adp.fetch_price('X')
    lines = []
    adp.report_weight(log=lines.append, now=60.0)    # 不抛
    assert any('report failed' in ln for ln in lines)


def test_concurrent_counting_no_loss():
    adp = ResilientAdapter(_Inner(), policy=FAST, sleep=NOSLEEP)
    threads = [threading.Thread(target=lambda: [adp.fetch_price('X') for _ in range(200)])
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = []
    adp.report_weight(log=lines.append, now=60.0)
    assert 'fetch_price=1600' in lines[0]            # 8×200 无丢计
