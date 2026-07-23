# data/score_research_2026-07-21/cf_anchor.py
"""锚门(P1 验收,spec §4/§7):同 2,796 标准格双口径逐位复现。
  锚A: eval_grid(geometry='v2').pnl_s030 == s030_calib_<win>.pnl
  锚B: eval_grid(geometry='geo').pnl_e0  == geo_<win>.pnl_m30_c16
附报 E0@V2−E0@geo 分布(V2 clamp 角落量化,不设门)。锚不平→查保真度,禁放宽。
用法: cf_anchor.py <W1|W2|OOS|IS> [limit]
"""
import importlib.util
import os
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_spec = importlib.util.spec_from_file_location('cf_eval', RD + '/cf_eval.py')
cf_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cf_eval)


def main(wn, limit=None):
    s030 = pd.read_parquet('%s/ablation/s030_calib_%s.parquet' % (RD, wn))
    geo = pd.read_parquet('%s/ablation/geo_%s.parquet' % (RD, wn))
    pairs = s030[['rt', 'symbol', 'pnl']].merge(
        geo[['rt', 'symbol', 'Atr_5', 'pnl_m30_c16']], on=['rt', 'symbol'], how='inner')
    print('[%s] pairs=%d (s030=%d geo=%d)' % (wn, len(pairs), len(s030), len(geo)),
          flush=True)
    if limit is not None:
        pairs = pairs.head(limit)
    cache = ParquetCache(V.default_cache_root())
    m1_map, fd_map = {}, {}
    bad_a, bad_b, e0diff, n = [], [], [], 0
    for i, r in pairs.reset_index(drop=True).iterrows():
        sym, rt = r['symbol'], pd.Timestamp(r['rt'])
        m1 = m1_map.get(sym)
        if m1 is None:
            m1 = cache.read_all_days('1m', sym)
            m1_map[sym] = m1
        fd = fd_map.get(sym)
        if fd is None:
            fd = cache.read_all_days('funding', sym)
            fd_map[sym] = fd
        rv = cf_eval.eval_grid(m1, fd, rt, r['Atr_5'], geometry='v2')
        rg = cf_eval.eval_grid(m1, fd, rt, r['Atr_5'], geometry='geo')
        if rv is None:
            bad_a.append((r['rt'], sym, 'NONE', r['pnl']))
        elif rv['pnl_s030'] != r['pnl']:
            bad_a.append((r['rt'], sym, rv['pnl_s030'], r['pnl']))
        if rg is None:
            bad_b.append((r['rt'], sym, 'NONE', r['pnl_m30_c16']))
        elif rg['pnl_e0'] != r['pnl_m30_c16']:
            bad_b.append((r['rt'], sym, rg['pnl_e0'], r['pnl_m30_c16']))
        if rv is not None and rg is not None:
            n += 1
            e0diff.append(rv['pnl_e0'] - rg['pnl_e0'])
        if len(m1_map) > 120:
            m1_map.clear()
            fd_map.clear()
        if (i + 1) % 100 == 0:
            print('[%s] %d/%d badA=%d badB=%d' % (wn, i + 1, len(pairs),
                  len(bad_a), len(bad_b)), flush=True)
    d = pd.Series(e0diff, dtype=float) * 1e4
    line = ('[%s] n=%d | 锚A(s030) mismatch=%d | 锚B(geoE0) mismatch=%d | '
            'E0@V2−E0@geo bp: 中位 %+.2f p5 %+.1f p95 %+.1f 非零占比 %.2f'
            % (wn, n, len(bad_a), len(bad_b),
               d.median(), d.quantile(0.05), d.quantile(0.95), float((d != 0).mean())))
    print(line, flush=True)
    with open('%s/ablation/cf_anchor_results.txt' % RD, 'a') as f:
        f.write(line + '\n')
    for t in (bad_a[:5] + bad_b[:5]):
        print('  MISMATCH', t, flush=True)
    if bad_a or bad_b:
        sys.exit(1)
    print('[%s] ANCHOR PASS' % wn, flush=True)


if __name__ == '__main__':
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None)
