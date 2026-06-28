import pandas as pd


def _cache(tmp_path):
    from gridtrade.backtest.cache import ParquetCache
    return ParquetCache(str(tmp_path))


def _df():
    return pd.DataFrame({'ts': [1, 2], 'close': [10.0, 11.0]})


def test_write_read_exists(tmp_path):
    c = _cache(tmp_path)
    assert c.exists('1h', 'BTC/USDT:USDT', '2024-01-01') is False
    c.write('1h', 'BTC/USDT:USDT', '2024-01-01', _df())
    assert c.exists('1h', 'BTC/USDT:USDT', '2024-01-01') is True
    got = c.read('1h', 'BTC/USDT:USDT', '2024-01-01')
    assert list(got['close']) == [10.0, 11.0]


def test_read_missing_returns_none(tmp_path):
    assert _cache(tmp_path).read('1h', 'X', '2024-01-01') is None


def test_write_empty_sentinel_exists(tmp_path):
    c = _cache(tmp_path)
    c.write_empty('1h', 'X', '2024-01-01', columns=['ts', 'close'])
    assert c.exists('1h', 'X', '2024-01-01') is True       # 空哨兵也算已缓存
    got = c.read('1h', 'X', '2024-01-01')
    assert got is not None and len(got) == 0


def test_read_all_days_merges(tmp_path):
    c = _cache(tmp_path)
    c.write('1h', 'X', '2024-01-01', pd.DataFrame({'ts': [1], 'close': [10.0]}))
    c.write('1h', 'X', '2024-01-02', pd.DataFrame({'ts': [2], 'close': [11.0]}))
    alld = c.read_all_days('1h', 'X')
    assert len(alld) == 2 and set(alld['ts']) == {1, 2}


def test_read_all_days_none_when_absent(tmp_path):
    assert _cache(tmp_path).read_all_days('1h', 'NOPE') is None
