"""cf_patrol 纯构造器:fapi kline/funding 行 → 引擎可消费 DataFrame(合成数据,无网络)。"""
import importlib.util
import os

import pytest

def _mod():
    spec = importlib.util.spec_from_file_location('cf_patrol', 'scripts/cf_patrol.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _loadable():
    # cf_patrol 用**硬编码绝对路径**(/Users/thomaschang/...)去 exec 研究资产 cf_eval/cf_report;
    # CI/异机 checkout 路径不同 → 加载即 FileNotFoundError。能加载才跑,否则 skip(本机正常跑)。
    if not os.path.exists('scripts/cf_patrol.py'):
        return False
    try:
        _mod()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _loadable(), reason='cf_patrol 不可加载(研究资产硬编码绝对路径在本机外缺失)')


def test_klines_to_df_columns_and_types():
    m = _mod()
    # fapi kline 数组: [openTime,open,high,low,close,volume,closeTime,quoteVolume,...]
    rows = [[1753228800000, '1.0', '1.2', '0.9', '1.1', '100', 0, '110', 5, '50', '55', '0'],
            [1753228860000, '1.1', '1.3', '1.0', '1.2', '200', 0, '230', 6, '90', '99', '0']]
    df = m.klines_to_df('ABC/USDT:USDT', rows)
    from gridtrade.exchanges.base import CANDLE_COLS
    assert list(df.columns) == list(CANDLE_COLS)   # 9列: symbol..vol/volCcy/quote_volume
    # 1753228800000ms = 2025-07-23 00:00 UTC
    assert df['candle_begin_time'].iloc[0].strftime('%Y-%m-%d %H:%M') == '2025-07-23 00:00'
    assert float(df['quote_volume'].iloc[1]) == 230.0
    assert float(df['vol'].iloc[1]) == 200.0
    assert float(df['volCcy'].iloc[1]) == 200.0    # binance.py:241: volCcy=vol


def test_funding_to_df_schema():
    m = _mod()
    rows = [{'fundingTime': 1753228800000, 'fundingRate': '-0.0001'}]
    df = m.funding_to_df('ABC/USDT:USDT', rows)
    assert list(df.columns) == ['ts', 'symbol', 'fundingRate', 'realizedRate']
    assert df['ts'].iloc[0] == 1753228800000
    assert abs(df['fundingRate'].iloc[0] + 0.0001) < 1e-12
