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
from gridtrade.backtest import safe_workers
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
    ap.add_argument('--mode', choices=('base', 'expand'), default='base',
                    help='base=跑初始网格；expand=从已有 CSV 判定边界并外推新点'
                         '（最优在边界=网格没铺够；一轮=对每个窗各调用一次）')
    ap.add_argument('--baseline', default='',
                    help='Pass 2 坐标下降：把基线换成 Pass 1 各族冠军的组合，'
                         '如 pv_mult=5,band=1.5,count_min=20,spacing_max=0.01。'
                         '**必须配 --out 指向新目录**——臂标签只编码被扫维度，'
                         '不同基线下同名臂含义不同，混写会串味')
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
    workers = safe_workers(os.environ.get('BT_WORKERS', '1'))  # 夹 ≤半数核心，防超订假死

    if args.baseline:
        if not args.out:
            raise SystemExit('--baseline 必须配 --out 指向新目录（不同基线的 CSV 不可混写）')
        ov = {}
        for kv in args.baseline.split(','):
            k, _, v = kv.partition('=')
            k = k.strip()
            try:
                ov[k] = int(v) if k in ('pv_mult', 'pv_n', 'count_min') else float(v)
            except ValueError:
                ov[k] = v.strip()          # active_stop_mode / pv_period 等字符串维
        SW.set_baseline(ov)
        print('[sweep] Pass2 基线覆盖: %s' % ov, flush=True)
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))

    print('[sweep] families=%s windows=%s workers=%d universe=%d'
          % (','.join(families), ','.join(wnames), workers, len(universe)), flush=True)
    print('[sweep] 基线(实盘现值): %s' % SW.baseline(), flush=True)
    t0 = time.time()
    res = SW.sweep(cache, universe, families, wnames, workers=workers, out_dir=out_dir,
                   mode=args.mode)
    print('[sweep] done %.0fs → %s' % (time.time() - t0, os.path.abspath(out_dir)), flush=True)
    for fam, df in res.items():
        print('\n===== %s（Calmar 主序，worst-window 并列键；vetoed=破网/爆仓）=====' % fam,
              flush=True)
        print(SW.rank_arms(df).to_string(index=False), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
