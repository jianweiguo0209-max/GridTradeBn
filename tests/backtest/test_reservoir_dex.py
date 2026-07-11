# tests/backtest/test_reservoir_dex.py — builder-dex 装载（spec 2026-07-12-builder-dex）
import pandas as pd

import gridtrade.backtest.reservoir as R
from gridtrade.backtest.cache import ParquetCache
from tests.backtest.test_reservoir import _raw_1s, _ms, _DAY_MS

_PAST = '2025-11-20'


def test_reservoir_coin_naming():
    # 主 dex：base 原样
    assert R.reservoir_coin('BTC/USDC:USDC') == 'BTC'
    assert R.reservoir_coin('BTC/USDC:USDC', dex='hyperliquid') == 'BTC'
    # builder：剥 'XYZ-' 前缀 + 'xyz:' 命名（档案实测 'xyz:XYZ100'）
    assert R.reservoir_coin('XYZ-TSLA/USDC:USDC', dex='xyz') == 'xyz:TSLA'
    assert R.reservoir_coin('XYZ-XYZ100/USDC:USDC', dex='xyz') == 'xyz:XYZ100'
    # 防御：base 无该前缀时不剥（不产生错位）
    assert R.reservoir_coin('TSLA/USDC:USDC', dex='xyz') == 'xyz:TSLA'


def test_s3_key_per_dex():
    assert R.S3_KEY_FMT % ('hyperliquid', '2025-01-01') == \
        'by_dex/hyperliquid/candles/1s/date=2025-01-01/candles.parquet'
    assert R.S3_KEY_FMT % ('xyz', '2025-11-20') == \
        'by_dex/xyz/candles/1s/date=2025-11-20/candles.parquet'


def _mixed_universe():
    return ['BTC/USDC:USDC', 'XYZ-TSLA/USDC:USDC'], {'XYZ-TSLA/USDC:USDC': 'xyz'}


def test_warm_multi_dex_writes_both_groups(tmp_path, monkeypatch):
    """主 dex + xyz 各自日文件下载、coin 命名各自匹配、两组都落缓存。"""
    cache = ParquetCache(str(tmp_path))
    uni, dex_map = _mixed_universe()
    calls = []

    def fake_cp(day, dest, *, dex='hyperliquid', **kw):
        calls.append(dex)
        coin = 'BTC' if dex == 'hyperliquid' else 'xyz:TSLA'
        _raw_1s(coin, day + ' 00:00:00', 7200, base=100.0).to_parquet(dest, index=False)
        return True

    monkeypatch.setattr(R, '_s3_cp', fake_cp)
    stat = R.warm_reservoir_ohlcv(cache, uni, _ms(_PAST), _ms(_PAST) + _DAY_MS - 1,
                                  dex_map=dex_map)
    assert sorted(calls) == ['hyperliquid', 'xyz']       # 每 dex 各下一次
    for s in uni:
        assert cache.exists('1h', s, _PAST) and cache.exists('1m', s, _PAST)
    assert len(cache.read('1h', 'XYZ-TSLA/USDC:USDC', _PAST)) == 2
    assert stat['1h']['rows'] == 4                        # 2 币 × 2 根


def test_warm_dex_failure_isolated_no_cross_sentinel(tmp_path, monkeypatch):
    """xyz 日文件 404（如 dex 上线前）→ 该组零写入（不落假哨兵），主 dex 正常写；
    这是旧'跨 dex 假哨兵'丢格机制的回归测试。"""
    cache = ParquetCache(str(tmp_path))
    uni, dex_map = _mixed_universe()

    def fake_cp(day, dest, *, dex='hyperliquid', **kw):
        if dex == 'xyz':
            return False
        _raw_1s('BTC', day + ' 00:00:00', 7200, base=100.0).to_parquet(dest, index=False)
        return True

    monkeypatch.setattr(R, '_s3_cp', fake_cp)
    stat = R.warm_reservoir_ohlcv(cache, uni, _ms(_PAST), _ms(_PAST) + _DAY_MS - 1,
                                  dex_map=dex_map)
    assert cache.exists('1m', 'BTC/USDC:USDC', _PAST)
    assert not cache.exists('1m', 'XYZ-TSLA/USDC:USDC', _PAST)   # 关键：无假哨兵
    assert stat['retry_later'] >= 1
    # 第二次跑：主 dex 组已命中零重下，只重试 xyz
    calls = []
    def fake_cp2(day, dest, *, dex='hyperliquid', **kw):
        calls.append(dex)
        if dex == 'xyz':
            _raw_1s('xyz:TSLA', day + ' 00:00:00', 7200, base=200.0).to_parquet(dest, index=False)
            return True
        raise AssertionError('主 dex 组已命中不应重下')
    monkeypatch.setattr(R, '_s3_cp', fake_cp2)
    R.warm_reservoir_ohlcv(cache, uni, _ms(_PAST), _ms(_PAST) + _DAY_MS - 1, dex_map=dex_map)
    assert calls == ['xyz']
    assert cache.exists('1m', 'XYZ-TSLA/USDC:USDC', _PAST)


def test_warm_default_no_dexmap_single_main_call(tmp_path, monkeypatch):
    """dex_map=None → 单主 dex 文件一次下载（现状路径回归锚）。"""
    cache = ParquetCache(str(tmp_path))
    calls = []

    def fake_cp(day, dest, *, dex='hyperliquid', **kw):
        calls.append(dex)
        raw = pd.concat([_raw_1s('BTC', day + ' 00:00:00', 7200, base=100.0),
                         _raw_1s('ETH', day + ' 00:00:00', 7200, base=50.0)],
                        ignore_index=True)
        raw.to_parquet(dest, index=False)
        return True

    monkeypatch.setattr(R, '_s3_cp', fake_cp)
    R.warm_reservoir_ohlcv(cache, ['BTC/USDC:USDC', 'ETH/USDC:USDC'],
                           _ms(_PAST), _ms(_PAST) + _DAY_MS - 1)
    assert calls == ['hyperliquid']
