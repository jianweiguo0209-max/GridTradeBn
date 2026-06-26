"""
用「当前代码」走实盘取数路径，为某个 run_time 产生选币真值（不下单）。

目的：作为 parity 验证的 ground truth。它复用与实盘**完全相同**的当前代码
（fetch_ok_swap_candle_data 实盘取数 + proceed_calc_symbol_factor + select_grid_coin），
唯一区别是不调用 open_grid。然后用 prewarm 的离线缓存回放同一 run_time，对比两者选币。

票池：用与 prewarm 一致的口径（冻结 instruments 的 live USDT 永续），保证 truth 与 replay
用同一份票池，从而把对比聚焦在「实盘取数 vs 离线缓存」这一个接缝上。

时区：必须 TZ=Asia/Shanghai（同实盘服务器 UTC+8），否则 fetch 的截断与 offset 漂移。

用法：
  TZ=Asia/Shanghai ../.venv/bin/python produce_truth.py --run-time "2026-06-26 10:00:00" \
      --cache-dir ../data/bt_parity/cache --out ../data/bt_parity/manifest/truth.csv
"""
import argparse
import os
import sys

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_ACC = os.path.join(os.path.dirname(_HERE), 'account_0')
for _p in (_ACC, os.path.join(_ACC, 'utils'), os.path.join(_ACC, 'api')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ccxt  # noqa: E402
import bt_config as C  # noqa: E402
import prewarm  # noqa: E402
from cache import ParquetCache  # noqa: E402
from selection_replay import compute_offset  # noqa: E402
from api.kline import fetch_ok_swap_candle_data  # noqa: E402  实盘取数函数
from utils.functions import proceed_calc_symbol_factor  # noqa: E402
from utils.fancy_grid_function import select_grid_coin  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description='当前代码实盘路径选币真值生成（不下单）')
    ap.add_argument('--run-time', required=True, help='UTC+8 墙钟，整点，如 "2026-06-26 10:00:00"')
    ap.add_argument('--cache-dir', default=C.CACHE_DIR)
    ap.add_argument('--out', default=os.path.join(C.MANIFEST_DIR, 'truth.csv'))
    args = ap.parse_args()

    T = pd.Timestamp(args.run_time)
    cfg = prewarm._load_strategy_config()
    period = cfg['period']
    offset = compute_offset(T, period, C.UTC_OFFSET)
    print('run_time=%s offset=%d factors=%s' % (T, offset, C.FACTORS))

    # 票池：与 prewarm 一致（冻结 instruments 的 live USDT 永续）
    cache = ParquetCache(args.cache_dir)
    universe, _tick = prewarm.stage_instruments(cache, C.PROXIES)

    exchange = ccxt.okex5({'enableRateLimit': True, 'timeout': 10000,
                           'proxies': C.PROXIES or {}})

    # 实盘取数：逐个币 fetch_ok_swap_candle_data（point-in-time 截断 < T）
    print('[truth] 实盘取数 %d 个币（max_candle_num=%d）...' % (len(universe), cfg['max_candle_num']))
    scd = {}
    n_ok = 0
    for i, s in enumerate(universe):
        sym, df = fetch_ok_swap_candle_data(exchange, s, T, cfg['max_candle_num'])
        if df is not None and len(df) >= 24:
            scd[s] = df
            n_ok += 1
        if (i + 1) % 100 == 0:
            print('[truth] 进度 %d/%d (有效 %d)' % (i + 1, len(universe), n_ok))
    print('[truth] 有效币 %d' % n_ok)

    # 当前代码选币（与实盘 proceed_order_for_strategy_config 一致）
    all_data_df = proceed_calc_symbol_factor(scd, T, period, offset)
    if all_data_df is None or all_data_df.empty:
        print('[truth] 因子为空，无选币'); return
    factor_data = select_grid_coin(all_data_df, C.FACTORS, cfg['weight_list'], cfg['choose_symbols'], T)
    factor_data = factor_data[(factor_data['time'] + pd.to_timedelta(period)) >= T]

    rows = [{'run_time': T, 'offset': offset, 'symbol': r['symbol'], 'rank': r.get('rank')}
            for _, r in factor_data.iterrows()]
    out = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    out.to_csv(args.out, index=False)
    print('[truth] 选中 %d 个 -> %s' % (len(out), args.out))
    print(out.to_string(index=False))


if __name__ == '__main__':
    main()
