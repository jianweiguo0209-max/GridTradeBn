import pandas as pd

from gridtrade.exchanges.base import CANDLE_COLS, FUNDING_COLS


class PagingClient:
    """模拟分页交易所：每次最多返回 3 根，从 since 起；超过数据末尾返回空。"""
    def __init__(self, start, n, tf_ms=3600_000):
        self.bars = [[start + i * tf_ms, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 10.0 + i]
                     for i in range(n)]
        self.tf_ms = tf_ms
        self.calls = 0

    def parse_timeframe(self, tf):
        return self.tf_ms // 1000

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls += 1
        since = since or 0
        page = [b for b in self.bars if b[0] >= since][:3]
        return page

    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        since = since or 0
        rows = [{'timestamp': b[0], 'fundingRate': 0.0001 * (i + 1)}
                for i, b in enumerate(self.bars) if b[0] >= since][:3]
        return rows


def _adapter(client):
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    return CcxtAdapter(client, name='ccxt')


def test_fetch_ohlcv_paginates_full_range():
    start = 1_700_000_000_000
    client = PagingClient(start, n=10)            # 10 根，每页 3 → 需多页
    a = _adapter(client)
    df = a.fetch_ohlcv('BTC/USDT:USDT', '1h', start, start + 9 * 3600_000)
    assert list(df.columns) == CANDLE_COLS
    assert len(df) == 10                          # 分页拉全
    assert client.calls >= 4                      # 确实分了多页
    assert df['candle_begin_time'].is_monotonic_increasing
    assert df['ts'].is_unique if 'ts' in df.columns else True


def test_fetch_ohlcv_range_filter():
    start = 1_700_000_000_000
    client = PagingClient(start, n=10)
    a = _adapter(client)
    # 只要中间 5 根 [start+2h, start+6h]
    df = a.fetch_ohlcv('BTC/USDT:USDT', '1h', start + 2 * 3600_000, start + 6 * 3600_000)
    assert len(df) == 5


def test_fetch_funding_history_paginates():
    start = 1_700_000_000_000
    client = PagingClient(start, n=8)
    a = _adapter(client)
    df = a.fetch_funding_history('BTC/USDT:USDT', start, start + 7 * 3600_000)
    assert list(df.columns) == FUNDING_COLS
    assert len(df) == 8


def test_existing_fixed_client_still_terminates():
    # 复刻既有 FakeCcxtClient 语义：忽略 since、返回固定 2 行 → 分页须安全终止、dedup 回 2 行
    class FixedClient:
        def parse_timeframe(self, tf):
            return 3600
        def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
            return [[1704067200000, 1.0, 2.0, 0.5, 1.5, 10.0],
                    [1704070800000, 1.5, 2.5, 1.0, 2.0, 20.0]]
    a = _adapter(FixedClient())
    df = a.fetch_ohlcv('BTC/USDT:USDT', '1h', 0, 10 ** 13)
    assert len(df) == 2 and list(df.columns) == CANDLE_COLS


class OneRowClient:
    """Edge page cap: returns at most 1 bar per call from `since`."""
    def __init__(self, start, n, tf_ms=3600_000):
        self.bars = [[start + i * tf_ms, 1.0, 2.0, 0.5, 1.5, 10.0] for i in range(n)]
        self.tf_ms = tf_ms
    def parse_timeframe(self, tf):
        return self.tf_ms // 1000
    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        since = since or 0
        page = [b for b in self.bars if b[0] >= since][:1]
        return page


def test_fetch_ohlcv_single_row_page_cap_fetches_full_range():
    start = 1_700_000_000_000
    a = _adapter(OneRowClient(start, n=10))
    df = a.fetch_ohlcv('BTC/USDT:USDT', '1h', start, start + 9 * 3600_000)
    assert len(df) == 10        # must not stop after the first 1-row page
