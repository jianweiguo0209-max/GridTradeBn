"""真实 Hyperliquid 端到端验证（需求 9）：联网小窗口 prewarm + 离线回测。
跑：TZ=Asia/Shanghai .venv/bin/python scripts/validate_hl.py
注：联网、耗时；非 pytest 套件。证明同一份回测代码经配置即可在 HL 上拉数回测。
"""
import os
import sys
import time

import ccxt
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.datasource import DataSource
from gridtrade.backtest import prewarm as PW
from gridtrade.backtest.backtest_run import run_backtest, summarize


class RetryingHyperliquidAdapter(HyperliquidAdapter):
    """对 HL 端点的间歇性 5xx/网络错误做指数退避重试（验证脚本用；不污染 core）。"""
    def _retry(self, fn, *a, **k):
        last = None
        for i in range(12):     # HL /info 偶发持续数十秒的 500 突发，需较长重试窗口
            try:
                return fn(*a, **k)
            except (ccxt.ExchangeNotAvailable, ccxt.NetworkError, ccxt.RequestTimeout) as e:
                last = e
                time.sleep(min(2.0 * (i + 1), 8.0))
        raise last

    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
        return self._retry(super().fetch_ohlcv, symbol, timeframe, start_ms, end_ms)

    def fetch_funding_history(self, symbol, start_ms, end_ms):
        return self._retry(super().fetch_funding_history, symbol, start_ms, end_ms)

UNIVERSE = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'AVAX/USDT:USDT',
            'ARB/USDT:USDT', 'OP/USDT:USDT', 'LINK/USDT:USDT', 'DOGE/USDT:USDT']

STRATEGY = {
    'period': '12H', 'max_candle_num': 160, 'weight_list': [1, 1, 1],
    'choose_symbols': 1, 'leverage': 5, 'price_limit': [0.25, 0.25], 'stop_limit': 0.01,
    'grid_version': 2,
    'grid_v2_config': {'atr_range_multiplier': 3, 'range_pct_min': 0.05, 'range_pct_max': 0.25,
                       'grid_spacing_atr_ratio': 0.5, 'grid_spacing_min': 0.003,
                       'grid_spacing_max': 0.02, 'grid_count_min': 25, 'grid_count_max': 149,
                       'stop_buffer_ratio': 0.01},
    'stop_loss_config': {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618,
                         'fundingRate_stop_loss': 0.0015},
}
FACTORS = {'Reg_v2_5': True, 'Sgcz_5': True, 'Er_2': True}


def main():
    cache = ParquetCache(os.path.join(os.path.dirname(__file__), '..', 'data', 'hl_validate'))
    adapter = RetryingHyperliquidAdapter(ccxt.hyperliquid({'enableRateLimit': True, 'timeout': 30000}))
    ds = DataSource(adapter, cache, timeframe='1h')

    # 窗口：最近 ~10 天（+暖机）。实时验证脚本，用本机时钟作锚（非确定性测试，可接受）。
    one_h = 3600_000
    end_ms = int(time.time() * 1000)
    warm_start = end_ms - 22 * 24 * one_h     # 含暖机
    win_start = end_ms - 10 * 24 * one_h
    print('[HL] window end=%s start=%s' % (pd.to_datetime(end_ms, unit='ms'),
                                           pd.to_datetime(win_start, unit='ms')))

    t0 = time.time()
    stat = PW.prewarm_ohlcv(ds, UNIVERSE, warm_start, end_ms)
    print('[HL] prewarm ohlcv:', stat, '%.1fs' % (time.time() - t0))

    df = run_backtest(cache, UNIVERSE, pd.to_datetime(win_start, unit='ms'),
                      pd.to_datetime(end_ms, unit='ms'), STRATEGY, FACTORS, utc_offset=0,
                      timeframe='1h')
    print('\n===== HL 回测汇总 =====')
    for k, v in summarize(df).items():
        print('  %s: %s' % (k, v))
    if not df.empty:
        print('\n样本:\n', df.head(10).to_string(index=False))
    print('\n[HL] 验证完成：同一回测代码经配置在 Hyperliquid 上拉数+回测成功。')


if __name__ == '__main__':
    main()
