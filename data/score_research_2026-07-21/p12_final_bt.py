"""p12_cross1 × St4/St5 组合级正式战役(2026-07-25 用户令,brief:
docs/superpowers/specs/2026-07-25-p12-battle-brief.md;留出预注册:
docs/superpowers/specs/2026-07-25-p12-holdout-prereg.md)。

真裁判口径:真选币回放 + 12 offset + 同币 cap2 + 资金分配 + Calmar。

**注入设计(探针 p12_inject_probe.py 已证 ②全池截断恒等)**:
  一次全池选币回放(choose_symbols=BIG)→ 候选表 p12_pool_<win>.parquet;
  `rank` 在全池上算,截断不改其值 ⇒ 按 rank<=5 截断与生产 choose_symbols=5 **逐位一致**
  (29 列全值同,双轮验证)⇒ 一次回放服务所有臂,且锚臂可证 byte-exact。
  臂间唯一差异 = **排序键**:
    anchor  → 生产 rank(rank_sum 加权名次)
    p12top1 → 全票池按 p12_cross1 降序(标签 rt+12h=R;缺标签币不参选=探针口径)
  其后 shock 剔轮 / allocate_with_tiers(cap2) / 资金分配 / 载 1m / 引擎 **全部生产现值不动**。

阶段:
  POOL  每窗一次全池回放 → ablation/p12_pool_<win>.parquet(臂间复用,断点续跑)
  MAIN  判定六窗四臂(布线自检,**不参与裁决**——六窗对本战役全污染)
  HOLD  留出 HOLD-C/D 四臂(**唯一裁决**,判据见预注册 §5,跑前已冻结)
用法: BT_STAGE=POOL|MAIN|HOLD [BT_WINDOWS=W1,W2] .venv/bin/python p12_final_bt.py
"""
import os
import sys
import time
from collections import Counter

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import selection_replay as SR
from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest.backtest_run import (BT_FACTORS, BT_STRATEGY, BT_UNIVERSE_TOP_PCT,
                                             _FUNDING_BACK_MS, allocate_with_tiers,
                                             holding_bars)
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.selection_replay import build_pit_candidates
from gridtrade.backtest.shock_replay import blocked_rts
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.grid_params import GRID_ROW_FACTORS
from gridtrade.core.selection import (compute_offset, needed_factors,
                                      proceed_calc_symbol_factor, select_grid_coin)
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
OUT = RD + '/ablation'
RESULTS = OUT + '/p12_final_results.txt'
STAGE = os.environ.get('BT_STAGE', 'MAIN')

WD_MAIN = {'OOS': ('2026-01-01', '2026-02-28'), 'W1': ('2025-08-15', '2025-10-14'),
           'W2': ('2025-10-15', '2025-12-14'), 'IS': ('2026-03-01', '2026-06-30'),
           'HOLD-A': ('2025-02-01', '2025-03-31'), 'HOLD-B': ('2024-10-01', '2024-11-30')}
WD_HOLD = {'HOLD-C': ('2025-04-01', '2025-05-31'), 'HOLD-D': ('2024-12-01', '2025-01-31')}
WD_ALL = dict(WD_MAIN, **WD_HOLD)
WD = WD_HOLD if STAGE == 'HOLD' else (WD_ALL if STAGE == 'POOL' else WD_MAIN)
_only = os.environ.get('BT_WINDOWS', '')          # 窗过滤:窗级并行用,防两进程撞窗
if _only:
    WD = {k: v for k, v in WD.items() if k in _only.split(',')}

# 判定窗锚(geo_final BASE_TD 存档=**残缺档案**下的历史数)
BASE_ANCHOR = {'OOS': (1.85, 4.7), 'W1': (-2.83, -3.7), 'W2': (6.31, 17.4),
               'IS': (13.11, 11.2), 'HOLD-A': (-2.36, -2.9), 'HOLD-B': (1.58, 4.1)}
# 锚模式(用户令 2026-07-25 补全判定窗档案后):
#   strict = 必须逐位复现 BASE_TD,否则停手 —— **仅在补全前有效**,用于证注入代码保真;
#            该证明已完成:parity 四层逐位 PASS + HOLD-B 锚 +1.58/4.1/格1169 逐位复现。
#   record = 照报与 BASE_TD 的偏差但不中止 —— 补全后的常态:池变了,偏差属预期,
#            不是保真度问题。补全对锚臂与 p12 臂**同等作用**,主判据(预注册§5)未动。
ANCHOR_MODE = os.environ.get('P12_ANCHOR_MODE', 'record')

# p12 标签源(cross1;run_time = rt + 12h)
# ⚠2026-07-25 档案补全后**全部重建**:cross1 读 1m 算,旧 sc_labels_*/hold_labels_* 的币数
# =当时有 1m 的币数(老窗缺 17~26%),沿用旧标签会让 p12 臂只能在旧币集里选=补全白做。
# 重建统一走 holdout_gate.py stage L → hold_labels_<win>.parquet(含判定六窗)。
LAB = {w: '%s/hold_labels_%s.parquet' % (OUT, w)
       for w in ('W1', 'W2', 'OOS', 'IS', 'HOLD-A', 'HOLD-B', 'HOLD-C', 'HOLD-D')}

# 冻结链(brief §1;基线已是 s030 现值 ⇒ overrides = 净差异,禁止再调)
ST4 = {'stop_loss': 0.04, 'trailing_floor': 0.01, 'trailing_k': 0.15, 'pv_mult': 5}
ST5 = {'stop_loss': 0.05, 'trailing_floor': 0.01, 'trailing_k': 0.15, 'pv_mult': 5}
# 费率轴对比臂(用户令 2026-07-25 加,brief §1 原排除):St5 基座 + 费率单旋钮。
# 权威定义 p12_is_final.py:F30=0.003(松2倍) F99=1.0(不可达=关)。St4/St5 费率恒为现值 0.0015。
# **背景臂,照报不裁**——预注册 §5 主判据仍只认 p12_St5、副判据 p12_s030,一字未改。
# 加臂时点:留出窗 HOLD-C/D 零数字之前,故非事后挑臂(记录见 prereg 执行记录)。
F30 = dict(ST5, funding_stop=0.003)
F99 = dict(ST5, funding_stop=1.0)
ARMS = [('anchor', 'rank', {}),          # 锚:生产选币器 × s030
        ('p12_s030', 'p12', {}),         # 副判据:分离选币器贡献
        ('p12_St5', 'p12', ST5),         # 主判据
        ('p12_St4', 'p12', ST4),         # 背景(照报不裁)
        ('p12_F30', 'p12', F30),         # 背景:费率止损松2倍
        ('p12_F99', 'p12', F99)]         # 背景:费率止损关

# POOL 表 = **过滤 v1.0 / 选币因子 dropna 之前**的 top55% 全池(用户令 2026-07-25:
# "因子 dropna + 过滤 v1.0 不要用在 p12 选币上")。二者是 rank_sum 那条线的策略性设计
# (剔极端弱势币 + 选币因子必须齐全),对 p12 无意义——p12 根本不读 Reg/Sgcz/Er。
#   rank 列:select_grid_coin 产出的生产名次;**未通过过滤/dropna 的行 rank=NaN**。
#   → 锚臂取 rank<=k(与生产逐位同,byte-exact 不受影响)
#   → p12 臂取全表(仅要求布网必需列非空:close/Atr_5/middle_5,物理底线非策略偏好)
POOL_COLS = ['rt', 'offset', 'symbol', 'rank', 'close', 'Atr_5', 'middle_5']


def emit(line):
    open(RESULTS, 'a').write(line + '\n')
    print(line, flush=True)


# ---------------- 阶段 POOL:全池候选表(轮级断点续跑) ----------------
CKPT_EVERY = int(os.environ.get('P12_CKPT_EVERY', '100'))   # 每 N 轮落一次分片


def _ckpt_paths(wn):
    return ('%s/p12_pool_%s.ckdone.txt' % (OUT, wn), '%s/p12_pool_%s.ckpart' % (OUT, wn))


def _load_ckpt(wn):
    """读已落盘分片 + 已完成轮集合。done ⊆ 已落盘 rows(见 flush 顺序),故不会丢行。"""
    done_p, part_pre = _ckpt_paths(wn)
    rows, done = [], set()
    if os.path.exists(done_p):
        done = {ln.strip() for ln in open(done_p, encoding='utf-8') if ln.strip()}
    import glob
    for p in sorted(glob.glob(part_pre + '*.parquet')):
        rows.append(pd.read_parquet(p))
    return (pd.concat(rows, ignore_index=True) if rows else None), done


def build_pool(cache, wn, s0, e0):
    out_p = '%s/p12_pool_%s.parquet' % (OUT, wn)
    if os.path.exists(out_p):
        emit('[POOL/%s] SKIP(已有)' % wn)
        return
    done_p, part_pre = _ckpt_paths(wn)
    prev, done = _load_ckpt(wn)
    if done:
        emit('[POOL/%s] RESUME 已完成轮=%d 已存行=%d'
             % (wn, len(done), 0 if prev is None else len(prev)))
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    syms = sorted(set(V.list_archive_symbols()) - set(bl))
    ws = pd.Timestamp(s0)
    we = pd.Timestamp(e0) + pd.Timedelta(days=1)
    rts_all = [pd.Timestamp(t) for t in pd.date_range(ws, we, freq='1H')]
    rts = [rt for rt in rts_all if str(rt) not in done]
    if not rts:
        emit('[POOL/%s] 全轮已完成,合并分片' % wn)
    else:
        t0 = time.time()
        series = SR.load_full_series(cache, syms, '1h')
        lo = ws - pd.Timedelta(days=10)             # 因子回看留量(同 cf_run)
        for s_ in list(series):                     # 裁窗省内存
            df = series[s_]
            df = df[(df['candle_begin_time'] >= lo) & (df['candle_begin_time'] < we)]
            if len(df) < 24:
                del series[s_]
            else:
                series[s_] = df.reset_index(drop=True)
        emit('[POOL/%s] 载1h %.1fmin 有效币=%d 待跑轮=%d/%d'
             % (wn, (time.time() - t0) / 60, len(series), len(rts), len(rts_all)))
        st = dict(BT_STRATEGY, choose_symbols=9999)  # 全池:rank 全池算,截断不改其值
        state = {'buf': [], 'pend': [], 'part': len(
            __import__('glob').glob(part_pre + '*.parquet')), 'n': 0}
        t0 = time.time()

        def flush():
            """先落 rows 分片、再记 done —— 保证 done ⊆ 已落盘行,崩溃最多重算 CKPT 轮。"""
            if state['buf']:
                pd.DataFrame(state['buf'], columns=POOL_COLS).to_parquet(
                    '%s%04d.parquet' % (part_pre, state['part']))
                state['part'] += 1
                state['buf'] = []
            if state['pend']:
                with open(done_p, 'a', encoding='utf-8') as f:
                    f.write('\n'.join(state['pend']) + '\n')
                state['pend'] = []

        # 自建循环(取代 _select_over_run_times):须在 select_grid_coin **之前**截获全池,
        # 否则过滤 v1.0/dropna 已把 p12 的候选剔掉。锚臂用的 rank 仍由 select_grid_coin
        # 原样产出、参数不变 ⇒ byte-exact 不受影响(p12_anchor_parity.py 复验)。
        needed = needed_factors(BT_FACTORS) | set(GRID_ROW_FACTORS)
        devnull = open(os.devnull, 'w')
        try:
            for rt in rts:
                rt = pd.Timestamp(rt)
                off = compute_offset(rt, st['period'])
                cand = build_pit_candidates(
                    series, rt, max_candle_num=st['max_candle_num'],
                    min_quote_volume=0.0, top_volume_pct=BT_UNIVERSE_TOP_PCT, blacklist=bl)
                if cand:
                    import contextlib
                    with contextlib.redirect_stdout(devnull):
                        all_df = proceed_calc_symbol_factor(cand, rt, st['period'], off,
                                                            needed=needed, batch=True)
                        ranked = None
                        if all_df is not None and not all_df.empty:
                            ranked = select_grid_coin(all_df.copy(), BT_FACTORS,
                                                      st['weight_list'],
                                                      st['choose_symbols'], rt)
                    if all_df is not None and not all_df.empty:
                        rk = {}
                        if ranked is not None and not ranked.empty:
                            keep = ranked[(ranked['time']
                                           + pd.to_timedelta(st['period'])) >= rt]
                            rk = dict(zip(keep['symbol'], keep['rank']))
                        valid = all_df[(all_df['time']
                                        + pd.to_timedelta(st['period'])) >= rt]
                        for r in valid.itertuples(index=False):
                            state['buf'].append(
                                (rt, int(off), r.symbol,
                                 float(rk.get(r.symbol, float('nan'))),
                                 float(r.close), float(r.Atr_5), float(r.middle_5)))
                state['pend'].append(str(rt))
                state['n'] += 1
                if state['n'] % CKPT_EVERY == 0:
                    flush()
                    emit('[POOL/%s] ckpt 轮=%d/%d %.1fmin'
                         % (wn, state['n'], len(rts), (time.time() - t0) / 60))
        finally:
            devnull.close()
        flush()
    df, _ = _load_ckpt(wn)
    if df is None or df.empty:
        emit('[POOL/%s] !!! 无候选行' % wn)
        return
    df = df.sort_values(['rt', 'rank']).reset_index(drop=True)
    df.to_parquet(out_p)
    for p in __import__('glob').glob(part_pre + '*.parquet'):   # 晋升成品后清 ckpt
        os.remove(p)
    if os.path.exists(done_p):
        os.remove(done_p)
    emit('[POOL/%s] DONE 行=%d 轮=%d 币=%d 候选/轮=%.0f'
         % (wn, len(df), df['rt'].nunique(), df['symbol'].nunique(),
            len(df) / max(df['rt'].nunique(), 1)))


# ---------------- 建臂 picks(唯一臂间差异=排序键) ----------------
def _load_p12(wn):
    lab = pd.read_parquet(LAB[wn])[['rt', 'symbol', 'cross1']]
    lab = lab.rename(columns={'cross1': 'p12'})
    lab['rt'] = lab['rt'] + pd.Timedelta(hours=12)   # 标签 rt=T 描述[T,T+12h) → 轮 R=T+12h
    return lab


def make_picks(pool, ranker, wn, k=SW.TIER_CAND_K):
    """全池候选 → 按 ranker 排序取 top-k 候选(喂 allocate_with_tiers 做 cap2 递补)。

    锚臂:rank 非空且 <=k —— rank 由 select_grid_coin 原样产出(含过滤 v1.0/dropna),
          与生产逐位同。
    p12 臂:用**全表**(不受过滤 v1.0/选币因子 dropna 约束,用户令 2026-07-25),
          仅要求布网必需列非空(close/Atr_5/middle_5)——物理底线,缺了开不出格。
    """
    d = pool
    if ranker == 'p12':
        d = d[np.isfinite(d['close']) & np.isfinite(d['Atr_5'])
              & np.isfinite(d['middle_5'])]
        lab = _load_p12(wn)
        d = d.merge(lab, on=['rt', 'symbol'], how='inner')   # 缺标签币不参选(探针口径)
        # p12 降序;并列按 symbol 字典序稳定 tiebreak(可复现)
        d = d.sort_values(['rt', 'p12', 'symbol'], ascending=[True, False, True])
        d = d.groupby('rt', sort=False).head(k).copy()
        d['rank'] = d.groupby('rt', sort=False).cumcount() + 1.0
    else:
        d = d[np.isfinite(d['rank']) & (d['rank'] <= k)].sort_values(['rt', 'rank'])
    picks = []
    for r in d.itertuples(index=False):
        row = pd.Series({'symbol': r.symbol, 'rank': r.rank, 'close': r.close,
                         'Atr_5': r.Atr_5, 'middle_5': r.middle_5, 'time': r.rt})
        picks.append((r.rt, int(r.offset), row))
    return picks


def preload_from_picks(cache, picks, wn, s0, e0, universe):
    """shock 剔轮 → cap2 递补 → 载 1m → 切 bars/funding(preload_window 后半段同构)。"""
    ws = pd.Timestamp(s0)
    we = pd.Timestamp(e0) + pd.Timedelta(days=1)
    blocked = blocked_rts(cache, universe, ws, we, '1h', *SW.SHOCK,
                          min_quote_volume=0.0, top_volume_pct=BT_UNIVERSE_TOP_PCT)
    picks = [p for p in picks if p[0] not in blocked]
    picks, _st = allocate_with_tiers(picks, DEFAULT_TIER_POLICY, period=SW._S['period'])
    syms = sorted({row['symbol'] for _, _, row in picks})
    series = SR.load_full_series(cache, syms, '1m')
    funding_by_sym = {}
    raw = []
    for rt, offset, row in picks:
        sym = row['symbol']
        if sym not in series:
            continue
        bars = holding_bars(series[sym], rt, SW._S['period'])
        if len(bars) == 0:
            continue
        if sym not in funding_by_sym:
            funding_by_sym[sym] = cache.read_all_days('funding', sym)
        fd = funding_by_sym[sym]
        if fd is not None and not fd.empty:
            lo = int(bars['candle_begin_time'].min().value // 1_000_000)
            hi = int(bars['candle_begin_time'].max().value // 1_000_000)
            fd = fd[(fd['ts'] >= lo - _FUNDING_BACK_MS) & (fd['ts'] <= hi)]
        raw.append((rt, int(offset), row, bars, fd, series[sym]))
    days = int((pd.Timestamp(e0) - pd.Timestamp(s0)).days) + 1
    return SW.WindowData(name=wn, start=ws, end=we, days=days, raw=raw,
                         n_blocked=len(blocked), n_symbols=len(syms))


def _done():
    done = set()
    names = {a[0] for a in ARMS}
    try:
        for ln in open(RESULTS, encoding='utf-8'):
            p = '%s/' % STAGE
            if ln.startswith(p) and ': ' in ln:
                wn, rest = ln[len(p):].split(': ', 1)
                if wn in WD and rest.split()[0] in names:
                    done.add((wn, rest.split()[0]))
    except FileNotFoundError:
        pass
    return done


def main():
    cache = ParquetCache(V.default_cache_root())
    w_sim = int(os.environ.get('BT_WORKERS', '4'))
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
    SW.set_baseline({})                              # 基线=live config(已是 s030)
    emit('== p12_final %s 开跑 %s ==' % (STAGE, time.strftime('%m-%d %H:%M')))
    if STAGE == 'POOL':
        for wn, (s0, e0) in WD.items():
            build_pool(cache, wn, s0, e0)
        emit('P12_FINAL_POOL_DONE')
        return
    done = _done()
    for wn, (s0, e0) in WD.items():
        todo = [a for a in ARMS if (wn, a[0]) not in done]
        if not todo:
            emit('[%s] SKIP' % wn)
            continue
        pool_p = '%s/p12_pool_%s.parquet' % (OUT, wn)
        if not os.path.exists(pool_p):
            emit('[%s] 缺候选表 %s —— 先跑 BT_STAGE=POOL' % (wn, pool_p))
            continue
        pool = pd.read_parquet(pool_p)
        n_anchor_grids = None
        # 同选币器的臂 picks 完全相同(仅链参数异)→ 共享 wd 与 pv_cache,省重复载 1m
        for ranker in ('rank', 'p12'):
            arms_r = [a for a in todo if a[1] == ranker]
            if not arms_r:
                continue
            t0 = time.time()
            picks = make_picks(pool, ranker, wn)
            wd = preload_from_picks(cache, picks, wn, s0, e0, universe)
            tpre = (time.time() - t0) / 60
            emit('[%s] %s preload %.1fmin 格=%d 币=%d blocked=%d'
                 % (wn, ranker, tpre, len(wd.raw), wd.n_symbols, wd.n_blocked))
            pv_cache = {}
            for name, _rk, ov in arms_r:
                t0 = time.time()
                df = SW.run_arm(wd, SW.Arm('p12', name, ov), pv_cache, workers=w_sim)
                m = SW.metrics(df, wd.days)
                er = Counter(df['exit_reason'])
                top = ', '.join('%s:%d' % (k[:4], v) for k, v in er.most_common(4))
                emit('%s/%s: %-9s ret%+7.2f mdd%6.2f calmar%7.1f 格%d 破%d 爆%d 固%d pv%d '
                     '最差%+.3f %.1fmin | %s'
                     % (STAGE, wn, name, m['ret'] * 100, -m['mdd'] * 100, m['calmar'],
                        m['n_grids'], m['n_broke'], m['n_blown'], m['n_fixstop'],
                        m['n_pvstop'], m['worst_grid'], (time.time() - t0) / 60, top))
                if name == 'anchor':
                    n_anchor_grids = m['n_grids']
                    if wn in BASE_ANCHOR:
                        br, bc = BASE_ANCHOR[wn]
                        dev = (abs(m['ret'] * 100 - br) > 0.02
                               or abs(m['calmar'] - bc) > 0.15)
                        if ANCHOR_MODE == 'strict':  # 补全前:保真度门,不复现即停手
                            if dev:
                                emit('  !!! 锚不复现(%+.2f/%.1f vs %+.2f/%.1f),中止本窗'
                                     % (m['ret'] * 100, m['calmar'], br, bc))
                                return
                            emit('  [锚校验] OK 逐位复现 BASE_TD')
                        else:                        # 补全后:照报偏差,新锚即新基准
                            emit('  [锚基准] 补全后 %+.2f/%.1f vs 补全前存档 %+.2f/%.1f%s'
                                 % (m['ret'] * 100, m['calmar'], br, bc,
                                    '(档案已补全,偏差属预期)' if dev else '(与存档一致)'))
                    else:                            # 留出窗:无存档→结构自检(预注册 §5.6)
                        emit('  [锚结构自检] 格=%d offset=%d 破%d 爆%d'
                             % (m['n_grids'], df['offset'].nunique(),
                                m['n_broke'], m['n_blown']))
                elif n_anchor_grids:                 # 预注册 §5.5:轮数<锚95% → 不可比
                    r = m['n_grids'] / n_anchor_grids
                    if r < 0.95:
                        emit('  !!! %s 有效格数 %d = 锚的 %.1f%% < 95%% → 该窗不可比(预注册 §5.5)'
                             % (name, m['n_grids'], r * 100))
        emit('[%s] DONE' % wn)
    emit('P12_FINAL_%s_DONE' % STAGE)


if __name__ == '__main__':
    main()
