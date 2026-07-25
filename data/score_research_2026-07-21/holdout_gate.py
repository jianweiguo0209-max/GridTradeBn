"""留出门(2026-07-21 用户令"先看1"):p12_cross1→pnl_m30_c16 过 HOLD-A/B 零接触窗。

**预注册判据(跑前写死,不许挪门柱)**:
  主检验: z_p12_cross1(全池截面z) 与 pnl_m30_c16(E0纯网格,带宽3×ATR,16格) 的 pooled
          Spearman,HOLD-A 与 HOLD-B **均为正且 |IC|>0.08** → PASS;否则 FAIL(=格点幻影)。
  对照:   同信号 vs pnl_m20_c10(现役形状)——验"几何依赖"论(m30_c16 应显著更强)。
  次级:   其余12特征照报但只作背景;低漂移半样 pnl↔cross1 收割保真;两格点均值/p5。
窗: HOLD-A 2025-02-01~2025-03-31(腰斩逆风) / HOLD-B 2024-10-01~2024-11-30(牛市)。
构造完全复刻 score_audit(标签)+phase2_score(p12/p24 特征)+geo_sweep(引擎/几何/抽样)。
阶段断点: L 标签(rt 范围前扩24h供 p12/p24)→F 因子→E 引擎(Atr三分层700格,rng=42)→R 报告。
用法: holdout_gate.py <HOLD-A|HOLD-B> [stage=LFER]   (workers=2/窗,双窗并行合计4=护栏内)
"""
import os
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.backtest_run import holding_bars, _FUNDING_BACK_MS
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.sweep import FEE_MAKER, FEE_TAKER, MAX_RATE, GEARING, _S
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.factors import cal_factor
from gridtrade.core.grid_engine import simulate_grid_engine
from gridtrade.core.selection import trans_period_for_grid
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
OUT = RD + '/ablation'
N_WORKERS = int(os.environ.get('HG_WORKERS', '2'))
WD = {'HOLD-A': ('2025-02-01', '2025-03-31'), 'HOLD-B': ('2024-10-01', '2024-11-30'),
      # p12 组合战役新留出(2026-07-25,brief §3):零接触窗,只用 stage L 建 cross1 标签
      # (战役选币的因子由 select_grids 现算,不需要 F/E/R 的 IC 面板)
      'HOLD-C': ('2025-04-01', '2025-05-31'), 'HOLD-D': ('2024-12-01', '2025-01-31'),
      # 判定六窗(2026-07-25 档案补全后必须重建标签):cross1 本身读 1m 算,旧标签币数
      # =当时有 1m 的币数(HOLD-B 212/HOLD-A 229/W1 323...),补了 1m 却不重建标签,
      # p12 臂仍只能在旧币集里选 —— 补全等于白做。产物统一落 hold_labels_<win>.parquet。
      'W1': ('2025-08-15', '2025-10-14'), 'W2': ('2025-10-15', '2025-12-14'),
      'OOS': ('2026-01-01', '2026-02-28'), 'IS': ('2026-03-01', '2026-06-30'),
      # RSP111 战役唯一裁决窗(2026-07-26;数据构建在预注册 664bc19 之后)
      'HOLD-E': ('2025-06-01', '2025-08-14')}
FCOLS = ['Reg_v2_5', 'Sgcz_5', 'Er_2', 'S_shape_5', 'Atr_5']
CELLS = {'m30_c16': (3.0, 16), 'm20_c10': (2.0, 10)}
N_PER_WIN = 700


def _label_one(args):
    """复刻 score_audit._label_one(rt 前扩由调用方通过 w0 完成)。"""
    sym, w0s, w1s = args
    cache = ParquetCache(V.default_cache_root())
    lo = pd.Timestamp(w0s)
    hi = pd.Timestamp(w1s) + pd.Timedelta(days=1)
    rts = pd.date_range(lo, hi, freq='1h')[:-1]
    m1 = cache.read_all_days('1m', sym)
    if m1 is None or m1.empty:
        return []
    m1 = m1[(m1['candle_begin_time'] >= lo - pd.Timedelta(minutes=1))
            & (m1['candle_begin_time'] < hi + pd.Timedelta(hours=12))]
    if len(m1) < 800:
        return []
    m1 = m1.sort_values('candle_begin_time').set_index('candle_begin_time')
    c = m1['close'].astype(float)
    h = m1['high'].astype(float)
    l = m1['low'].astype(float)
    step = np.floor(np.log(np.clip(c.values, 1e-18, None)) / np.log(1.01))
    dstep = np.abs(np.diff(step, prepend=step[0]))
    sd = pd.Series(dstep, index=c.index)
    rows = []
    for rt in rts:
        seg = slice(rt, rt + pd.Timedelta(hours=12) - pd.Timedelta(minutes=1))
        cs = c.loc[seg]
        if len(cs) < 600:
            continue
        o = cs.iloc[0]
        rows.append((rt, sym, float(sd.loc[seg].sum()),
                     abs(float(cs.iloc[-1] / o - 1.0)),
                     max(abs(float(h.loc[seg].max() / o - 1.0)),
                         abs(float(l.loc[seg].min() / o - 1.0)))))
    return rows


def _factor_one(args):
    """复刻 score_audit._factor_one。"""
    sym, w0s, w1s = args
    cache = ParquetCache(V.default_cache_root())
    lo = pd.Timestamp(w0s)
    hi = pd.Timestamp(w1s) + pd.Timedelta(days=1)
    h1 = cache.read_all_days('1h', sym)
    if h1 is None or len(h1) < 24:
        return []
    rows = []
    for off in range(12):
        p2 = trans_period_for_grid(h1.copy(), '12H', exg_dict={}, offset=off)
        p2 = p2.dropna(subset=['close'])
        if len(p2) < 3:
            continue
        p2 = p2.reset_index()
        try:
            f = cal_factor(p2.copy())
        except Exception:
            continue
        f['rt'] = f['candle_begin_time'] + pd.Timedelta(hours=12)
        f = f[(f['rt'] >= lo) & (f['rt'] < hi)]
        for _, r in f.iterrows():
            rows.append((r['rt'], sym, off) + tuple(float(r.get(c, np.nan)) for c in FCOLS))
    return rows


def universe():
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    return sorted(set(V.list_archive_symbols()) - set(bl))


def stage_L(wn, w0, w1):
    out_p = '%s/hold_labels_%s.parquet' % (OUT, wn)
    if os.path.exists(out_p):
        print('[L/%s] SKIP' % wn, flush=True)
        return
    from concurrent.futures import ProcessPoolExecutor
    w0x = (pd.Timestamp(w0) - pd.Timedelta(hours=24)).strftime('%Y-%m-%d %H:%M')
    rows = []
    with ProcessPoolExecutor(max_workers=N_WORKERS) as ex:
        for out in ex.map(_label_one, [(s_, w0x, w1) for s_ in universe()], chunksize=8):
            rows.extend(out)
    df = pd.DataFrame(rows, columns=['rt', 'symbol', 'cross1', 'drift', 'mae'])
    df.to_parquet(out_p)
    print('[L/%s] DONE 行=%d 币=%d' % (wn, len(df), df['symbol'].nunique()), flush=True)


def stage_F(wn, w0, w1):
    out_p = '%s/hold_factors_%s.parquet' % (OUT, wn)
    if os.path.exists(out_p):
        print('[F/%s] SKIP' % wn, flush=True)
        return
    from concurrent.futures import ProcessPoolExecutor
    rows = []
    with ProcessPoolExecutor(max_workers=N_WORKERS) as ex:
        for out in ex.map(_factor_one, [(s_, w0, w1) for s_ in universe()], chunksize=8):
            rows.extend(out)
    df = pd.DataFrame(rows, columns=['rt', 'symbol', 'offset'] + FCOLS)
    df.to_parquet(out_p)
    print('[F/%s] DONE 行=%d 币=%d' % (wn, len(df), df['symbol'].nunique()), flush=True)


def build_features(wn):
    """phase2_score.load_window 同构:p12/p24 特征 + 全池截面 z。"""
    lab = pd.read_parquet('%s/hold_labels_%s.parquet' % (OUT, wn))
    fac = pd.read_parquet('%s/hold_factors_%s.parquet' % (OUT, wn))
    lab['eff'] = lab['cross1'] / (1.0 + 100.0 * lab['mae'])
    p12 = lab.rename(columns={c: 'p12_' + c for c in ('cross1', 'drift', 'mae', 'eff')}).copy()
    p12['rt'] = p12['rt'] + pd.Timedelta(hours=12)
    p24 = lab.rename(columns={c: 'q_' + c for c in ('cross1', 'drift', 'mae', 'eff')}).copy()
    p24['rt'] = p24['rt'] + pd.Timedelta(hours=24)
    j = fac.merge(lab[['rt', 'symbol', 'cross1', 'drift', 'mae', 'eff']], on=['rt', 'symbol'])
    j = j.merge(p12[['rt', 'symbol', 'p12_cross1', 'p12_drift', 'p12_mae', 'p12_eff']],
                on=['rt', 'symbol'], how='left')
    j = j.merge(p24[['rt', 'symbol', 'q_cross1', 'q_drift', 'q_mae', 'q_eff']],
                on=['rt', 'symbol'], how='left')
    for c in ('cross1', 'drift', 'mae', 'eff'):
        j['p24_' + c] = (j['p12_' + c] + j['q_' + c]) / 2.0
    j = j.drop(columns=['q_cross1', 'q_drift', 'q_mae', 'q_eff'])
    feats = FCOLS + ['p12_cross1', 'p12_drift', 'p12_mae', 'p12_eff',
                     'p24_cross1', 'p24_drift', 'p24_mae', 'p24_eff']
    j = j.dropna(subset=feats + ['eff', 'drift'])
    for c in feats:
        g = j.groupby('rt')[c]
        j['z_' + c] = (j[c] - g.transform('mean')) / g.transform('std').replace(0, np.nan)
    return j.dropna(subset=['z_' + c for c in feats])


def stage_E(wn):
    out_p = '%s/hold_grids_%s.parquet' % (OUT, wn)
    if os.path.exists(out_p):
        print('[E/%s] SKIP' % wn, flush=True)
        return
    j = build_features(wn)
    j = j[np.isfinite(j['Atr_5'])]
    j['atr_bucket'] = pd.qcut(j['Atr_5'], 3, labels=False, duplicates='drop')
    picks = pd.concat([g.sample(n=min(N_PER_WIN // 3, len(g)), random_state=42)
                       for _, g in j.groupby('atr_bucket')], ignore_index=True)
    cache = ParquetCache(V.default_cache_root())
    series_map, funding_map = {}, {}
    rows, n_skip = [], 0
    for i, r in picks.iterrows():
        sym, rt = r['symbol'], pd.Timestamp(r['rt'])
        try:
            m1 = series_map.get(sym)
            if m1 is None:
                m1 = cache.read_all_days('1m', sym)
                series_map[sym] = m1
            if m1 is None or m1.empty:
                n_skip += 1
                continue
            bars = holding_bars(m1, rt, _S['period'])
            if len(bars) < 600:
                n_skip += 1
                continue
            fd = funding_map.get(sym)
            if fd is None:
                fd = cache.read_all_days('funding', sym)
                funding_map[sym] = fd
            if fd is not None and not fd.empty:
                lo = int(bars['candle_begin_time'].min().value // 1_000_000)
                hi = int(bars['candle_begin_time'].max().value // 1_000_000)
                fd = fd[(fd['ts'] >= lo - _FUNDING_BACK_MS) & (fd['ts'] <= hi)]
            close = float(bars['open'].iloc[0])
            rec = r.drop(labels=['atr_bucket']).to_dict()
            for name, (m, c) in CELLS.items():
                rr = min(max(m * float(r['Atr_5']), 0.02), 0.5)
                gp = {'high_price': close * (1 + rr), 'low_price': close * (1 - rr),
                      'stop_high_price': close * (1 + rr) * 1.01,
                      'stop_low_price': close * (1 - rr) * 0.99, 'grid_count': c}
                res = simulate_grid_engine(
                    bars, gp, cap=1000.0, leverage=GEARING / MAX_RATE,
                    fee=FEE_MAKER, c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
                    stop_cfg=None, funding_df=fd, pv_spike_df=None,
                    neutral_init=False, active_stop_mode='none')
                rec['pnl_' + name] = float(res['pnl_ratio'])
                rec['reason_' + name] = res.get('exit_reason', '?')
            rows.append(rec)
        except Exception as e:
            n_skip += 1
            if n_skip > 200:
                print('[E/%s] ABORT 熔断 skip=%d last=%r' % (wn, n_skip, e), flush=True)
                raise
        if len(series_map) > 120:
            series_map.clear()
            funding_map.clear()
        if (i + 1) % 100 == 0:
            print('[E/%s] %d/%d skip=%d' % (wn, i + 1, len(picks), n_skip), flush=True)
    pd.DataFrame(rows).to_parquet(out_p)
    print('[E/%s] DONE n=%d skip=%d' % (wn, len(rows), n_skip), flush=True)


def sp(a, b):
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 30:
        return np.nan
    return np.corrcoef(pd.Series(a[m]).rank(), pd.Series(b[m]).rank())[0, 1]


def stage_R(wn):
    d = pd.read_parquet('%s/hold_grids_%s.parquet' % (OUT, wn))
    n = len(d)
    print('\n===== %s 留出门(n=%d) =====' % (wn, n), flush=True)
    ic = sp(d['z_p12_cross1'].values, d['pnl_m30_c16'].values)
    icc = sp(d['z_p12_cross1'].values, d['pnl_m20_c10'].values)
    print('主检验 z_p12_cross1↔pnl_m30_c16: %+.3f (≈t=%.1f) | 判线 >+0.08: %s'
          % (ic, ic * np.sqrt(n), 'PASS' if ic > 0.08 else 'FAIL'), flush=True)
    print('对照   z_p12_cross1↔pnl_m20_c10: %+.3f' % icc, flush=True)
    feats = FCOLS + ['p12_cross1', 'p12_drift', 'p12_mae', 'p12_eff',
                     'p24_cross1', 'p24_drift', 'p24_mae', 'p24_eff']
    print('次级(背景): 13特征 vs pnl_m30_c16 / pnl_m20_c10', flush=True)
    for c in feats:
        print('  %-11s %+.3f / %+.3f' % (c, sp(d['z_' + c].values, d['pnl_m30_c16'].values),
                                         sp(d['z_' + c].values, d['pnl_m20_c10'].values)),
              flush=True)
    lo = d[d['drift'] < d['drift'].median()]
    print('低漂移半样收割保真 pnl↔cross1: m30_c16 %+.3f | m20_c10 %+.3f'
          % (sp(lo['pnl_m30_c16'].values, lo['cross1'].values),
             sp(lo['pnl_m20_c10'].values, lo['cross1'].values)), flush=True)
    for c in CELLS:
        p = d['pnl_' + c]
        print('  %-8s mean %+.1fbp median %+.1fbp p5 %+.1fbp win %.2f'
              % (c, p.mean() * 1e4, p.median() * 1e4, p.quantile(0.05) * 1e4,
                 (p > 0).mean()), flush=True)


def main(wn, stage='LFER'):
    w0, w1 = WD[wn]
    if 'L' in stage:
        stage_L(wn, w0, w1)
    if 'F' in stage:
        stage_F(wn, w0, w1)
    if 'E' in stage:
        stage_E(wn)
    if 'R' in stage:
        stage_R(wn)
    print('[%s] HOLDOUT_GATE_DONE' % wn, flush=True)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'LFER')
