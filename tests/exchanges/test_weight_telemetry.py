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
