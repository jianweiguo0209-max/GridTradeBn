# data/score_research_2026-07-21/cf_run.py
"""P1 驱动(spec §4 Phase1):逐选币轮全票池反事实双口径。
轮=(run_time,offset) 取自 pdetail_<win>(战役 byte 精确生产格=实际选中记录);
池=build_pit_candidates 生产语义(top55%+PIT,universe 已剔黑名单);
Atr_5 查 sc_factors/hold_factors 面板;选中币恒评(池外 in_pool=False 标记)。
池覆盖=本地1m归档有该窗数据的币(缺档币计 skip_m1;选中格被跳过=硬失败 exit1)。
产物 ablation/cf_<win>.parquet。用法: cf_run.py <WIN> [stride] [limit]
stride=每N轮取1(算力降采样,配对设计统计无损;正式跑按冒烟耗时定)。
"""
import importlib.util
import os
import sys
import time

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.selection_replay import build_pit_candidates, load_full_series
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_spec = importlib.util.spec_from_file_location('cf_eval', RD + '/cf_eval.py')
cf_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cf_eval)

WD = {'W1': ('2025-08-15', '2025-10-14'), 'W2': ('2025-10-15', '2025-12-14'),
      'OOS': ('2026-01-01', '2026-02-28'), 'IS': ('2026-03-01', '2026-06-30'),
      'HOLD-A': ('2025-02-01', '2025-03-31'), 'HOLD-B': ('2024-10-01', '2024-11-30'),
      # IS 半窗(2026-07-24): 4月窗工作集~8GB 独占16GB机;切半各~4GB 可与他窗2并发。
      # 产物 cf_IS-a/b 由调度器 concat 成 cf_IS.parquet(逐格协议不变,采样相位切点重置)
      'IS-a': ('2026-03-01', '2026-04-30'), 'IS-b': ('2026-05-01', '2026-06-30')}
SRC = {'IS-a': 'IS', 'IS-b': 'IS'}   # 半窗别名: 因子/pdetail 用母窗文件
FAC = {w: '%s/sc_factors_%s.parquet' % (RD, w) for w in ('W1', 'W2', 'OOS', 'IS')}
FAC.update({w: '%s/ablation/hold_factors_%s.parquet' % (RD, w)
            for w in ('HOLD-A', 'HOLD-B')})
TOP_VOLUME_PCT = 0.55        # 生产 env UNIVERSE_TOP_VOLUME_PCT 现值(spec §1)
M1_CAP = 320                 # 1m LRU 上限:须>单轮池币数(~260),否则轮内清缓存=IO 爆炸


def main(wn, stride=1, limit=None):
    out_p = '%s/ablation/cf_%s.parquet' % (RD, wn)
    if os.path.exists(out_p) and limit is None:
        print('[%s] SKIP(已有产物)' % wn, flush=True)
        return
    w0, w1 = WD[wn]
    src = SRC.get(wn, wn)
    pdet = pd.read_parquet('%s/ablation/pdetail_%s.parquet' % (RD, src))
    fac = pd.read_parquet(FAC[src])[['rt', 'symbol', 'Atr_5']]
    atr = {(pd.Timestamp(r.rt), r.symbol): float(r.Atr_5) for r in fac.itertuples()}
    rounds = pdet[['run_time', 'offset']].drop_duplicates().sort_values('run_time')
    w0t, w1t = pd.Timestamp(w0), pd.Timestamp(w1) + pd.Timedelta(days=1)
    rounds = rounds[(rounds['run_time'] >= w0t) & (rounds['run_time'] < w1t)]  # 半窗切片(全窗=无操作)
    rounds = rounds.iloc[::max(1, int(stride))]
    if limit is not None:
        rounds = rounds.head(limit)
    picks_by_rt = pdet.groupby('run_time')['symbol'].apply(set).to_dict()
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    syms = sorted(set(V.list_archive_symbols()) - set(bl))
    cache = ParquetCache(V.default_cache_root())
    lo1h = pd.Timestamp(w0) - pd.Timedelta(days=10)
    hi1h = pd.Timestamp(w1) + pd.Timedelta(days=2)
    series = load_full_series(cache, syms, '1h')
    for s_ in list(series):                       # 裁窗省内存
        df = series[s_]
        df = df[(df['candle_begin_time'] >= lo1h) & (df['candle_begin_time'] < hi1h)]
        if len(df) < 24:
            del series[s_]
        else:
            series[s_] = df.reset_index(drop=True)
    m1lo = pd.Timestamp(w0) - pd.Timedelta(days=2)
    m1hi = pd.Timestamp(w1) + pd.Timedelta(days=2)
    m1_map, fd_map = {}, {}
    rows, t0 = [], time.time()
    n_skip_atr = n_skip_m1 = n_skip_eval = n_skip_exc = n_pick_skip = 0
    for i, rr in enumerate(rounds.itertuples()):
        rt = pd.Timestamp(rr.run_time)
        pool = set(build_pit_candidates(
            series, rt, max_candle_num=160, min_quote_volume=0.0,
            top_volume_pct=TOP_VOLUME_PCT, blacklist=()).keys())
        picks = picks_by_rt.get(rr.run_time, set())
        for sym in sorted(pool | picks):
            a5 = atr.get((rt, sym))
            if a5 is None or not np.isfinite(a5):
                n_skip_atr += 1
                if sym in picks:
                    n_pick_skip += 1
                continue
            m1 = m1_map.get(sym)
            if m1 is None:
                m1 = cache.read_all_days('1m', sym)
                if m1 is not None and not m1.empty:
                    m1 = m1[(m1['candle_begin_time'] >= m1lo)
                            & (m1['candle_begin_time'] < m1hi)].reset_index(drop=True)
                m1_map[sym] = m1
            if m1 is None or len(m1) == 0:
                n_skip_m1 += 1
                if sym in picks:
                    n_pick_skip += 1
                continue
            fd = fd_map.get(sym)
            if fd is None:
                fd = cache.read_all_days('funding', sym)
                fd_map[sym] = fd
            try:
                out = cf_eval.eval_grid(m1, fd, rt, a5, geometry='v2')
            except Exception:
                n_skip_exc += 1
                if sym in picks:
                    n_pick_skip += 1
                continue
            if out is None:
                n_skip_eval += 1
                if sym in picks:
                    n_pick_skip += 1
                continue
            rows.append({'run_time': rt, 'offset': int(rr.offset), 'symbol': sym,
                         'in_pool': sym in pool, 'picked': sym in picks,
                         'Atr_5': a5, **out})
        if len(m1_map) > M1_CAP:
            m1_map.clear()
            fd_map.clear()
        if (i + 1) % 10 == 0:
            print('[%s] 轮 %d/%d 行=%d skip(atr/m1/eval/exc)=%d/%d/%d/%d %.1fs/轮'
                  % (wn, i + 1, len(rounds), len(rows), n_skip_atr, n_skip_m1,
                     n_skip_eval, n_skip_exc, (time.time() - t0) / (i + 1)), flush=True)
    df = pd.DataFrame(rows)
    if limit is None:
        df.to_parquet(out_p)
    n_out = int((df['picked'] & ~df['in_pool']).sum()) if len(df) else 0
    print('[%s] DONE 轮=%d 行=%d skip(atr/m1/eval/exc)=%d/%d/%d/%d 选中跳过=%d 池外选中=%d'
          % (wn, len(rounds), len(df), n_skip_atr, n_skip_m1, n_skip_eval,
             n_skip_exc, n_pick_skip, n_out), flush=True)
    if n_pick_skip:
        print('[%s] FAIL 选中格被跳过=%d——数据缺口破坏选中vs池对比,fail-loud' % (wn, n_pick_skip),
              flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main(sys.argv[1],
         int(sys.argv[2]) if len(sys.argv) > 2 else 1,
         int(sys.argv[3]) if len(sys.argv) > 3 else None)
