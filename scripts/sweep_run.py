"""参数扫描 CLI（spec 2026-07-15-binance-param-sweep）。

    TZ=Asia/Shanghai BT_WORKERS=8 .venv/bin/python -m scripts.sweep_run \
        --family stop --windows W1,W2,OOS,IS

    # 全族（过夜）
    TZ=Asia/Shanghai BT_WORKERS=8 .venv/bin/python -m scripts.sweep_run --family all

产物：data/sweep/{family}_results.csv（arm × window × 指标）。
数据须已预热（四窗预热见 backtest_run CLI；留出窗首跑会自动下载）。
"""
import argparse
import os
import sys
import time

from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.core.tier_policy import effective_blacklist
from gridtrade.config import DEFAULT_TIER_POLICY


def main(argv=None):
    ap = argparse.ArgumentParser(description='参数扫描（实盘口径，单旋钮）')
    ap.add_argument('--family', default='all',
                    help='逗号分隔：%s 或 all' % '|'.join(SW.FAMILIES))
    ap.add_argument('--windows', default='W1,W2,OOS,IS',
                    help='逗号分隔窗口名（调参窗 %s / 留出窗 %s）'
                         % (','.join(SW.WINDOWS), ','.join(SW.HOLDOUT)))
    ap.add_argument('--out', default=None, help='CSV 输出目录（默认 <repo>/data/sweep）')
    args = ap.parse_args(argv)

    families = SW.FAMILIES if args.family == 'all' else tuple(
        f.strip() for f in args.family.split(',') if f.strip())
    for f in families:
        if f not in SW.FAMILIES:
            raise SystemExit('未知参数族: %s（可选 %s）' % (f, '|'.join(SW.FAMILIES)))
    wnames = [w.strip() for w in args.windows.split(',') if w.strip()]
    for w in wnames:
        if w not in SW.WINDOWS and w not in SW.HOLDOUT:
            raise SystemExit('未知窗口: %s' % w)

    root = V.default_cache_root()
    cache = ParquetCache(root)
    out_dir = args.out or os.path.join(root, '..', 'sweep')
    workers = int(os.environ.get('BT_WORKERS', '1'))
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))

    print('[sweep] families=%s windows=%s workers=%d universe=%d'
          % (','.join(families), ','.join(wnames), workers, len(universe)), flush=True)
    print('[sweep] 基线(实盘现值): %s' % SW.baseline(), flush=True)
    t0 = time.time()
    res = SW.sweep(cache, universe, families, wnames, workers=workers, out_dir=out_dir)
    print('[sweep] done %.0fs → %s' % (time.time() - t0, os.path.abspath(out_dir)), flush=True)
    for fam, df in res.items():
        print('\n===== %s =====' % fam, flush=True)
        piv = df.pivot_table(index='arm', columns='window', values='calmar')
        piv['mean_ret'] = df.pivot_table(index='arm', columns='window',
                                         values='ret').mean(axis=1)
        piv['worst_calmar'] = df.pivot_table(index='arm', columns='window',
                                             values='calmar').min(axis=1)
        print(piv.sort_values('worst_calmar', ascending=False).to_string(), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
