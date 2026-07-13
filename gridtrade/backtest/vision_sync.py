"""vision 归档独立预热 CLI（spec 2026-07-14 §6.1）：
  .venv/bin/python -m gridtrade.backtest.vision_sync 2019-09-01 2026-07-13 \
      --tf 1h,1m,funding [--symbols BTC/USDT:USDT,...] [--workers 8]
不传 --symbols 则从归档目录列举全量合约（含退市）。幂等可断点续跑。"""
import argparse

import pandas as pd

from gridtrade.backtest import vision
from gridtrade.backtest.cache import ParquetCache


def main(argv=None):
    ap = argparse.ArgumentParser(description='data.binance.vision 归档预热')
    ap.add_argument('start')                      # YYYY-MM-DD
    ap.add_argument('end')                        # YYYY-MM-DD（含当天）
    ap.add_argument('--tf', default='1m', help='逗号分隔: 1m,1h,funding')
    ap.add_argument('--symbols', default='', help='canonical 逗号分隔；空=归档全量')
    ap.add_argument('--quote', default='USDT')
    ap.add_argument('--workers', type=int, default=None)
    args = ap.parse_args(argv)

    start_ms = int(pd.Timestamp(args.start).value // 1_000_000)
    end_ms = int((pd.Timestamp(args.end) + pd.Timedelta(days=1)).value
                 // 1_000_000) - 1
    tfs = tuple(t.strip() for t in args.tf.split(',') if t.strip())
    if args.symbols.strip():
        universe = [s.strip() for s in args.symbols.split(',') if s.strip()]
    else:
        universe = vision.list_archive_symbols(quote=args.quote)
        print('[vision_sync] 归档全量 %d 合约（含退市）' % len(universe))
    cache = ParquetCache(vision.default_cache_root())
    st = vision.warm_vision(cache, universe, start_ms, end_ms, timeframes=tfs,
                            quote=args.quote, workers=args.workers)
    print('[vision_sync] done:', st)
    return st


if __name__ == '__main__':
    main()
