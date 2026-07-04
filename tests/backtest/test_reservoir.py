from decimal import Decimal

import numpy as np
import pandas as pd

import gridtrade.backtest.reservoir as R
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.reservoir import NAMESPACE, candles_1s_to_1m, warm_reservoir_1m
from gridtrade.exchanges.base import CANDLE_COLS


def _dec(vals):
    """模拟 Reservoir 真实 dtype：decimal(20,10) → pandas object(Decimal)。"""
    return pd.Series([Decimal('%.10f' % v) for v in vals], dtype=object)


def _raw_1s(coin, start, n, base=100.0):
    """合成 n 秒的 Reservoir 1s candles（OHLCV 用 Decimal object，贴合真实 schema）。"""
    ts = pd.date_range(start, periods=n, freq='1s', tz='UTC')   # datetime64[ns, UTC]
    px = base + np.arange(n) * 0.01
    return pd.DataFrame({
        'coin': coin, 'timestamp': ts,
        'open': _dec(px), 'high': _dec(px + 0.5), 'low': _dec(px - 0.5), 'close': _dec(px + 0.1),
        'volume': _dec(np.ones(n)), 'volume_quote': _dec(px), 'trade_count': 3,
    })


def test_1s_to_1m_ohlcv_and_schema():
    # 两分钟整（120 秒）BTC + 干扰币 ETH
    raw = pd.concat([_raw_1s('BTC', '2026-03-22 00:00:00', 120, base=100.0),
                     _raw_1s('ETH', '2026-03-22 00:00:00', 120, base=50.0)],
                    ignore_index=True)
    out = candles_1s_to_1m(raw, {'BTC': 'BTC/USDC:USDC'})

    assert set(out) == {'BTC/USDC:USDC'}          # ETH 未在 map → 不产出
    df = out['BTC/USDC:USDC']
    assert list(df.columns) == CANDLE_COLS
    assert len(df) == 2                             # 120s → 2 根 1m
    # bar-begin 口径
    assert df['candle_begin_time'].iloc[0] == pd.Timestamp('2026-03-22 00:00:00')
    assert df['candle_begin_time'].iloc[1] == pd.Timestamp('2026-03-22 00:01:00')
    # 第一根：open=第0秒 open，close=第59秒 close，high/low 为窗口内极值
    assert abs(df['open'].iloc[0] - 100.0) < 1e-9
    assert abs(df['close'].iloc[0] - (100.0 + 59 * 0.01 + 0.1)) < 1e-9
    assert abs(df['high'].iloc[0] - (100.0 + 59 * 0.01 + 0.5)) < 1e-9
    assert abs(df['low'].iloc[0] - (100.0 - 0.5)) < 1e-9
    # 成交量求和；quote_volume 映射自 volume_quote
    assert abs(df['vol'].iloc[0] - 60.0) < 1e-9
    assert df['quote_volume'].iloc[0] > 0
    # tz-naive（与 cache/引擎同口径）
    assert df['candle_begin_time'].dt.tz is None


def test_empty_and_missing_coin():
    assert candles_1s_to_1m(pd.DataFrame(), {'BTC': 'BTC/USDC:USDC'}) == {}
    raw = _raw_1s('BTC', '2026-03-22 00:00:00', 60)
    assert candles_1s_to_1m(raw, {'SOL': 'SOL/USDC:USDC'}) == {}   # 目标币不在数据里


UNI = ['BTC/USDC:USDC', 'ETH/USDC:USDC']
_PAST_DAY = '2025-01-01'          # 已过完的完整历史天
_DAY_MS = 86_400_000


def _ms(day):
    return int(pd.Timestamp(day).value // 1_000_000)


def test_warm_no_file_on_fetch_failure(tmp_path, monkeypatch):
    """拉取失败(404/报错) → 不写任何文件（含空哨兵），计 retry_later，下次可重取。"""
    cache = ParquetCache(str(tmp_path))
    monkeypatch.setattr(R, '_s3_cp', lambda day, dest, log=print: False)   # 模拟失败
    stat = warm_reservoir_1m(cache, UNI, _ms(_PAST_DAY), _ms(_PAST_DAY) + _DAY_MS - 1)
    assert stat['rows'] == 0 and stat['days'] == 0 and stat['retry_later'] >= 1
    for s in UNI:                                     # 关键：没有落任何缓存文件
        assert not cache.exists(NAMESPACE, s, _PAST_DAY)


def test_warm_current_day_not_cached(tmp_path, monkeypatch):
    """当天(UTC)未过完 → 跳过、不缓存、不触网。"""
    cache = ParquetCache(str(tmp_path))
    calls = []
    monkeypatch.setattr(R, '_s3_cp', lambda day, dest, log=print: calls.append(day) or False)
    today = pd.Timestamp.utcnow().strftime('%Y-%m-%d')
    end_ms = int(pd.Timestamp.utcnow().value // 1_000_000)
    stat = warm_reservoir_1m(cache, UNI, _ms(today), end_ms)
    assert stat['retry_later'] >= 1
    assert calls == []                                # 当天未过完，连下载都不尝试
    for s in UNI:
        assert not cache.exists(NAMESPACE, s, today)


def test_warm_success_writes_and_reuses(tmp_path, monkeypatch):
    """成功拉取 → 写真数据；再跑一次整天命中 → 复用零下载。"""
    cache = ParquetCache(str(tmp_path))

    def fake_cp(day, dest, log=print):
        raw = pd.concat([_raw_1s('BTC', day + ' 00:00:00', 120, base=100.0),
                         _raw_1s('ETH', day + ' 00:00:00', 120, base=50.0)], ignore_index=True)
        raw.to_parquet(dest, index=False)
        return True

    monkeypatch.setattr(R, '_s3_cp', fake_cp)
    stat = warm_reservoir_1m(cache, UNI, _ms(_PAST_DAY), _ms(_PAST_DAY) + _DAY_MS - 1)
    assert stat['days'] == 1 and stat['rows'] == 4    # 2 币 × 2 根 1m
    for s in UNI:
        assert cache.exists(NAMESPACE, s, _PAST_DAY)

    calls = []
    monkeypatch.setattr(R, '_s3_cp', lambda day, dest, log=print: calls.append(day) or True)
    stat2 = warm_reservoir_1m(cache, UNI, _ms(_PAST_DAY), _ms(_PAST_DAY) + _DAY_MS - 1)
    assert stat2['skipped_cached'] == 1 and stat2['days'] == 0 and calls == []


def test_1s_to_1h_matches_manual_agg():
    # 2 小时整（7200 秒）：1s→1H 直采，逐列对手工聚合期望
    raw = _raw_1s('BTC', '2026-03-22 00:00:00', 7200, base=100.0)
    out = R.candles_1s_resample(raw, {'BTC': 'BTC/USDC:USDC'}, '1H')
    df = out['BTC/USDC:USDC']
    assert list(df.columns) == CANDLE_COLS and len(df) == 2
    assert df['candle_begin_time'].iloc[0] == pd.Timestamp('2026-03-22 00:00:00')
    assert df['candle_begin_time'].iloc[1] == pd.Timestamp('2026-03-22 01:00:00')
    # 第一根：open=第0秒 open、close=第3599秒 close、high=第3599秒 high、low=第0秒 low
    assert abs(df['open'].iloc[0] - 100.0) < 1e-9
    assert abs(df['close'].iloc[0] - (100.0 + 3599 * 0.01 + 0.1)) < 1e-9
    assert abs(df['high'].iloc[0] - (100.0 + 3599 * 0.01 + 0.5)) < 1e-9
    assert abs(df['low'].iloc[0] - 99.5) < 1e-9
    assert abs(df['vol'].iloc[0] - 3600.0) < 1e-9
    # quote_volume = Σ volume_quote = Σ px（等差 100.00..135.99）
    assert abs(df['quote_volume'].iloc[0] - sum(100.0 + i * 0.01 for i in range(3600))) < 1e-6
    assert df['candle_begin_time'].dt.tz is None


def test_1h_equals_1m_reaggregated():
    # 一致性：1s→1H 直采 == 1s→1min 再聚 1H（agg 同构 ⇒ 恒等；防重采样口径漂移）
    raw = _raw_1s('BTC', '2026-03-22 00:00:00', 7200, base=100.0)
    smap = {'BTC': 'BTC/USDC:USDC'}
    direct = R.candles_1s_resample(raw, smap, '1H')['BTC/USDC:USDC']
    m = R.candles_1s_resample(raw, smap, '1min')['BTC/USDC:USDC']
    re = (m.set_index('candle_begin_time')
            .resample('1H', label='left', closed='left')
            .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last',
                  'vol': 'sum', 'volCcy': 'sum', 'quote_volume': 'sum'})
            .reset_index())
    for col in ('open', 'high', 'low', 'close', 'vol', 'quote_volume'):
        np.testing.assert_allclose(direct[col].to_numpy('float64'),
                                   re[col].to_numpy('float64'), rtol=1e-12,
                                   err_msg='%s 口径漂移' % col)


def _fake_cp_2coins(day, dest, log=print):
    """两币 × 7200 秒（2 根 1h / 120 根 1m 不足——用 7200s 产 2 根 1h、120 根 1m）。"""
    raw = pd.concat([_raw_1s('BTC', day + ' 00:00:00', 7200, base=100.0),
                     _raw_1s('ETH', day + ' 00:00:00', 7200, base=50.0)], ignore_index=True)
    raw.to_parquet(dest, index=False)
    return True


def test_warm_ohlcv_writes_both_namespaces(tmp_path, monkeypatch):
    cache = ParquetCache(str(tmp_path))
    monkeypatch.setattr(R, '_s3_cp', _fake_cp_2coins)
    stat = R.warm_reservoir_ohlcv(cache, UNI, _ms(_PAST_DAY), _ms(_PAST_DAY) + _DAY_MS - 1)
    assert stat['1h']['days'] == 1 and stat['1m']['days'] == 1
    assert stat['1m']['rows'] == 2 * 120      # 2 币 × 120 根 1m（7200s）
    assert stat['1h']['rows'] == 2 * 2        # 2 币 × 2 根 1h
    for s in UNI:
        assert cache.exists('1h', s, _PAST_DAY) and cache.exists('1m', s, _PAST_DAY)
    df = cache.read('1h', 'BTC/USDC:USDC', _PAST_DAY)
    assert len(df) == 2 and list(df.columns) == CANDLE_COLS


def test_warm_ohlcv_idempotent_and_partial_refill(tmp_path, monkeypatch):
    import os as _os
    cache = ParquetCache(str(tmp_path))
    monkeypatch.setattr(R, '_s3_cp', _fake_cp_2coins)
    R.warm_reservoir_ohlcv(cache, UNI, _ms(_PAST_DAY), _ms(_PAST_DAY) + _DAY_MS - 1)

    calls = []
    def _counting_cp(day, dest, log=print):
        calls.append(day)
        return _fake_cp_2coins(day, dest, log=log)
    monkeypatch.setattr(R, '_s3_cp', _counting_cp)
    # 全命中 → skip、零下载
    st2 = R.warm_reservoir_ohlcv(cache, UNI, _ms(_PAST_DAY), _ms(_PAST_DAY) + _DAY_MS - 1)
    assert st2['skipped_cached'] == 1 and calls == []
    # 删掉 1h 一边 → 重下补齐两边（幂等条件=所有 timeframe 全命中）
    for s in UNI:
        _os.remove(_os.path.join(str(tmp_path), '1h', s, _PAST_DAY + '.parquet'))
    st3 = R.warm_reservoir_ohlcv(cache, UNI, _ms(_PAST_DAY), _ms(_PAST_DAY) + _DAY_MS - 1)
    assert calls == [_PAST_DAY] and st3['1h']['days'] == 1
    for s in UNI:
        assert cache.exists('1h', s, _PAST_DAY)


def test_warm_1m_wrapper_old_format_and_no_1h(tmp_path, monkeypatch):
    # 薄包装：返回旧格式 dict、只写 1m 不写 1h
    cache = ParquetCache(str(tmp_path))
    monkeypatch.setattr(R, '_s3_cp', _fake_cp_2coins)
    stat = warm_reservoir_1m(cache, UNI, _ms(_PAST_DAY), _ms(_PAST_DAY) + _DAY_MS - 1)
    assert set(stat) == {'days', 'rows', 'skipped_cached', 'retry_later'}
    assert stat['days'] == 1 and stat['rows'] == 2 * 120
    for s in UNI:
        assert cache.exists('1m', s, _PAST_DAY)
        assert not cache.exists('1h', s, _PAST_DAY)
