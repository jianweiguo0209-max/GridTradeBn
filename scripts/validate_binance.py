"""真实币安端到端验证（联网、非 pytest）：小窗口 Vision 预热 + 离线回测。
跑：TZ=Asia/Shanghai .venv/bin/python scripts/validate_binance.py
证明同一份回测代码在币安数据上可拉数回测（验收②的最小前哨）。"""
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from gridtrade.backtest import backtest_run as BR
from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache

SYMS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'DOGE/USDT:USDT']


def main():
    end = pd.Timestamp.utcnow().normalize().tz_localize(None) - pd.Timedelta(days=2)
    start = end - pd.Timedelta(days=21)
    warm = start - pd.Timedelta(days=14)
    ms = lambda t: int(t.value // 1_000_000)
    cache = ParquetCache(V.default_cache_root())
    t0 = time.time()
    print('[validate] 1h+1m+funding 预热 %s -> %s' % (warm.date(), end.date()))
    print(V.warm_vision(cache, SYMS, ms(warm), ms(end),
                        timeframes=('1h', '1m', 'funding')))
    df = BR.run_backtest(cache, SYMS, start, end, BR.BT_STRATEGY, BR.BT_FACTORS,
                         timeframe='1h', sim_timeframe='1m', workers=2)
    print('[validate] %.1fs, %d grids' % (time.time() - t0, len(df)))
    for k, v in BR.summarize(df).items():
        print('  %s: %s' % (k, v))


if __name__ == '__main__':
    main()
