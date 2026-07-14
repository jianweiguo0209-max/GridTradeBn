"""参数扫描 harness（实盘口径，spec 2026-07-15-binance-param-sweep）。

**为什么入库**：现行参数（止损 0.045 / trailing 0.15,0.015 / 带宽 2 / 间距上限 0.04 /
pv 0.005×3 / gearing 3.4）全是在 Hyperliquid 上调出来的，随迁移原样搬到币安；而 GP 时代
的扫参脚本躺在 gitignored `data/tiercmp/` —— 参数怎么调出来的无据可查、无法复现。本模块
把扫参能力放进版本库，`data/sweep/*.csv` 只是产物。

**成本模型**（决定了架构）：
  preload_window  每窗一次：选币(磁盘缓存命中) → shock 剔轮 → cap2 递补 → 载 1m 全序列
                  → 逐格切 bars/funding + 按 27h 前置历史预算 pv 尖峰。**几何无关**。
  run_arm         每臂：几何族重算 calc_grid_params_v2（廉价纯函数）；pv 族重算尖峰；
                  其余族直接复用预载 → 只付仿真的钱。
pv 尖峰按 (mult, n, period) 键缓存复用：非 pv 族的臂共享同一份，省掉最贵的内循环。

**单旋钮原则 + 断言钉现值**：每臂只改一族，其余全取实盘现值；config 一改，本模块的断言
立刻炸（防「基线悄悄变了、报告仍按老基线解读」）。
"""
import os
from dataclasses import dataclass, field

import pandas as pd

from gridtrade.backtest import selection_replay as SR
from gridtrade.backtest.backtest_run import (BT_FACTORS, BT_STRATEGY, BT_UNIVERSE_TOP_PCT,
                                             allocate_with_tiers, holding_bars,
                                             pv_spike_for_window, select_grids,
                                             simulate_tasks)
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.grid_params import calc_grid_params_v2
from gridtrade.core.tier_policy import effective_blacklist

# ---- 实盘现值（唯一事实源=config；此处只做断言与基线快照）----
_S = BT_STRATEGY
_STOP = _S['stop_loss_config']
_V2 = _S['grid_v2_config']

# 断言钉死现值：config 一改本模块立刻炸（spec §6 防口径漂移）
assert abs(_STOP['stop_loss'] - 0.045) < 1e-12, 'stop_loss 现值漂移，扫参网格须同步复核'
assert abs(_STOP['trailing_k'] - 0.15) < 1e-12 and abs(_STOP['trailing_floor'] - 0.015) < 1e-12
assert abs(_STOP['fundingRate_stop_loss'] - 0.0015) < 1e-12
assert abs(_STOP['pv_pnl_thr'] - 0.005) < 1e-12 and _STOP['pv_mult'] == 3 and _STOP['pv_n'] == 100
assert _V2['atr_range_multiplier'] == 2 and abs(_V2['grid_spacing_max'] - 0.04) < 1e-12
assert _V2['grid_count_min'] == 10 and _S['leverage'] == 5

FEE_MAKER = 0.0002        # 币安 USDT-M VIP0
FEE_TAKER = 0.0005
MAX_RATE = 0.68           # gearing = leverage × max_rate = 3.4（实盘 GRID_GEARING）
GEARING = _S['leverage'] * MAX_RATE
SHOCK = (4, 0.025, 2)     # 实盘 SHOCK_K_HOURS / SHOCK_THR / SHOCK_PAUSE_HOURS
TIER_CAND_K = 5

WINDOWS = {           # 调参窗（与 GP 历史扫参同定义，可比）
    'W1': ('2025-08-15', '2025-10-14'),
    'W2': ('2025-10-15', '2025-12-14'),
    'OOS': ('2026-01-01', '2026-02-28'),
    'IS': ('2026-03-01', '2026-06-30'),
}
HOLDOUT = {           # 留出窗（只验收、不参与选择；币安归档独有，早于 HL 数据起点）
    'HOLD-A': ('2025-02-01', '2025-03-31'),
    'HOLD-B': ('2024-10-01', '2024-11-30'),
}


def baseline():
    """实盘现值基线（每族的对照臂）。"""
    return {
        'stop_loss': _STOP['stop_loss'],
        'trailing_k': _STOP['trailing_k'],
        'trailing_floor': _STOP['trailing_floor'],
        'funding_stop': _STOP['fundingRate_stop_loss'],
        'pv_thr': _STOP['pv_pnl_thr'],
        'pv_mult': _STOP['pv_mult'],
        'pv_n': _STOP['pv_n'],
        'pv_period': _STOP['pv_period'],
        'active_stop_mode': _S.get('active_stop_mode', 'pv'),
        'band': _V2['atr_range_multiplier'],
        'count_min': _V2['grid_count_min'],
        'spacing_max': _V2['grid_spacing_max'],
        'gearing': GEARING,
    }


@dataclass
class Arm:
    """一个参数臂：label + 相对 baseline 的覆盖项（单旋钮原则：只动一族）。"""
    family: str
    label: str
    overrides: dict = field(default_factory=dict)

    def params(self):
        return dict(baseline(), **self.overrides)

    def is_baseline(self):
        return not self.overrides


@dataclass
class WindowData:
    """每窗预载一次的几何无关格集（跨臂复用）。"""
    name: str
    start: pd.Timestamp
    end: pd.Timestamp
    days: int
    raw: list          # [(rt, offset, row, bars_df, funding_df, series_df)]
    n_blocked: int
    n_symbols: int


def preload_window(cache, universe, name, start, end, *, workers=1, log=print):
    """选币(缓存命中) → shock 剔轮 → cap2 递补 → 载 1m → 切 bars/funding。几何无关。"""
    from gridtrade.backtest.shock_replay import blocked_rts
    ws = pd.Timestamp(start)
    we = pd.Timestamp(end) + pd.Timedelta(days=1)      # end 含当天（与 CLI 同口径）
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    picks = select_grids(cache, universe, ws, we, BT_STRATEGY, BT_FACTORS,
                         timeframe='1h', min_quote_volume=0.0,
                         top_volume_pct=BT_UNIVERSE_TOP_PCT, blacklist=bl,
                         workers=workers, candidates_per_rt=TIER_CAND_K, log=log)
    blocked = blocked_rts(cache, universe, ws, we, '1h', *SHOCK,
                          min_quote_volume=0.0, top_volume_pct=BT_UNIVERSE_TOP_PCT)
    picks = [p for p in picks if p[0] not in blocked]
    picks, _stats = allocate_with_tiers(picks, DEFAULT_TIER_POLICY, period=_S['period'])
    syms = sorted({row['symbol'] for _, _, row in picks})
    series = SR.load_full_series(cache, syms, '1m')
    funding_by_sym = {}
    raw = []
    for rt, offset, row in picks:
        sym = row['symbol']
        if sym not in series:
            continue
        bars = holding_bars(series[sym], rt, _S['period'])
        if len(bars) == 0:
            continue
        if sym not in funding_by_sym:
            funding_by_sym[sym] = cache.read_all_days('funding', sym)
        fd = funding_by_sym[sym]
        if fd is not None and not fd.empty:
            lo = int(bars['candle_begin_time'].min().value // 1_000_000)
            hi = int(bars['candle_begin_time'].max().value // 1_000_000)
            fd = fd[(fd['ts'] >= lo) & (fd['ts'] <= hi)]
        raw.append((rt, int(offset), row, bars, fd, series[sym]))
    days = int((pd.Timestamp(end) - pd.Timestamp(start)).days) + 1
    log('[sweep] %s preload: grids=%d syms=%d blocked_rt=%d days=%d'
        % (name, len(raw), len(syms), len(blocked), days))
    return WindowData(name=name, start=ws, end=we, days=days, raw=raw,
                      n_blocked=len(blocked), n_symbols=len(syms))


def _pv_key(p):
    return (p['pv_mult'], p['pv_n'], p['pv_period'])


def tasks_for(wd, params, pv_cache):
    """按臂参数组装 data_tasks（几何重算 + pv 尖峰按 key 复用）。"""
    v2 = dict(_V2, atr_range_multiplier=params['band'],
              grid_count_min=params['count_min'], grid_spacing_max=params['spacing_max'])
    key = _pv_key(params)
    if key not in pv_cache:
        pv_cfg = {'mult': params['pv_mult'], 'n': params['pv_n'], 'period': params['pv_period']}
        pv_cache[key] = [pv_spike_for_window(series, bars, pv_cfg)
                         for _rt, _off, _row, bars, _fd, series in wd.raw]
    pv_list = pv_cache[key]
    tasks = []
    for i, (rt, off, row, bars, fd, _series) in enumerate(wd.raw):
        px = calc_grid_params_v2(row=row, price_limit=_S['price_limit'],
                                 stop_limit=_S['stop_limit'], v2_config=v2)
        gp = dict(low_price=px['low_price'], high_price=px['high_price'],
                  grid_count=px['grid_count'], stop_high_price=px['stop_high_price'],
                  stop_low_price=px['stop_low_price'])
        tasks.append((rt, off, row['symbol'], float(row['close']), gp, bars, fd, pv_list[i]))
    return tasks


def run_arm(wd, arm, pv_cache, *, workers=1):
    """跑一个臂 → 明细 DataFrame。"""
    p = arm.params()
    tasks = tasks_for(wd, p, pv_cache)
    stop_cfg = {'stop_loss': p['stop_loss'], 'trailing_k': p['trailing_k'],
                'trailing_floor': p['trailing_floor'],
                'fundingRate_stop_loss': p['funding_stop'],
                'pv_pnl_thr': p['pv_thr'], 'pv_mult': p['pv_mult'],
                'pv_period': p['pv_period'], 'pv_n': p['pv_n']}
    pv_cfg = {'pnl_thr': p['pv_thr'], 'mult': p['pv_mult'],
              'n': p['pv_n'], 'period': p['pv_period']}
    # gearing = leverage × max_rate；扫 gearing 时固定 max_rate、改 leverage（等价、少一个自由度）
    lev = p['gearing'] / MAX_RATE
    return simulate_tasks(tasks, leverage=lev, fee_rate=FEE_MAKER, taker_rate=FEE_TAKER,
                          max_rate=MAX_RATE, stop_cfg=stop_cfg,
                          active_stop_mode=p['active_stop_mode'], pv_cfg=pv_cfg,
                          workers=workers)


def metrics(df, days):
    """Calmar 主序 + 组合收益/MDD/胜率/破网爆仓/成交量（口径见 spec §2）。
    组合净值 = 12 offset lane 各自按平仓时刻复利 → 等权平均（与 summarize 同源）。"""
    if df is None or df.empty:
        return {'n_grids': 0, 'ret': 0.0, 'ann': 0.0, 'mdd': 0.0, 'calmar': 0.0,
                'win_rate': 0.0, 'n_fills': 0.0, 'n_broke': 0, 'n_blown': 0,
                'n_fixstop': 0, 'n_pvstop': 0, 'worst_grid': 0.0}
    d = df.copy()
    d['close_ts'] = d['run_time'] + pd.to_timedelta(_S['period'])
    events = sorted(d['close_ts'].unique())
    lanes = {}
    for off, g in d.sort_values('close_ts').groupby('offset'):
        eq = (1.0 + g.set_index('close_ts')['pnl_ratio']).cumprod()
        eq = eq[~eq.index.duplicated(keep='last')]
        lanes[off] = eq.reindex(events, method='ffill').fillna(1.0)
    port = pd.DataFrame(lanes).mean(axis=1)
    ret = float(port.iloc[-1]) - 1.0
    mdd = float((1.0 - port / port.cummax()).max())
    ann = (1.0 + ret) ** (365.0 / max(days, 1)) - 1.0
    traded = d[d['exit_reason'] != '未触网']
    er = d['exit_reason'].value_counts()
    return {
        'n_grids': int(len(d)),
        'ret': ret, 'ann': ann, 'mdd': mdd,
        'calmar': (ann / mdd) if mdd > 1e-9 else float('inf'),
        'win_rate': float((traded['pnl_ratio'] > 0).mean()) if len(traded) else 0.0,
        'n_fills': float(d['n_fills'].mean()),
        'n_broke': int(er.get('破网', 0)),
        'n_blown': int(er.get('爆仓', 0)),
        'n_fixstop': int(er.get('固定止损', 0)),
        'n_pvstop': int(er.get('pv主动止损', 0)),
        'worst_grid': float(d['pnl_ratio'].min()),
    }


# ---- 参数网格（spec §4；粗体现值即 baseline，各族第一臂恒为基线对照）----
def build_arms(family):
    b = baseline()
    arms = [Arm(family, 'BASE(现值)')]
    if family == 'stop':
        for sl in (0.030, 0.035, 0.040, 0.055, 0.065, 0.080):
            arms.append(Arm(family, 'sl=%.3f' % sl, {'stop_loss': sl}))
        arms.append(Arm(family, 'sl=OFF', {'stop_loss': 9.9}))     # 9.9=事实停用（仅破网/爆仓兜底）
    elif family == 'trail':
        for k in (0.10, 0.15, 0.25):
            for fl in (0.010, 0.015, 0.025):
                if abs(k - b['trailing_k']) < 1e-12 and abs(fl - b['trailing_floor']) < 1e-12:
                    continue
                arms.append(Arm(family, 'k=%.2f,fl=%.3f' % (k, fl),
                                {'trailing_k': k, 'trailing_floor': fl}))
        arms.append(Arm(family, 'trail=OFF', {'trailing_k': 9.9, 'trailing_floor': 9.9}))
    elif family == 'pv':
        for thr in (0.0, 0.0025, 0.0075, 0.010):
            arms.append(Arm(family, 'thr=%.4f' % thr, {'pv_thr': thr}))
        for mult in (2, 4):
            arms.append(Arm(family, 'mult=%d' % mult, {'pv_mult': mult}))
            for thr in (0.0025, 0.0075):
                arms.append(Arm(family, 'thr=%.4f,mult=%d' % (thr, mult),
                                {'pv_thr': thr, 'pv_mult': mult}))
        arms.append(Arm(family, 'pv=OFF', {'active_stop_mode': 'none'}))
    elif family == 'funding':
        for f in (0.0005, 0.0010, 0.0025):
            arms.append(Arm(family, 'fr=%.4f' % f, {'funding_stop': f}))
        arms.append(Arm(family, 'fr=OFF', {'funding_stop': 9.9}))
    elif family == 'geom':
        for band in (1.5, 2, 3, 4, 5):
            for cmin in (5, 10, 20, 30):
                if band == b['band'] and cmin == b['count_min']:
                    continue
                arms.append(Arm(family, 'band=%s,cmin=%d' % (band, cmin),
                                {'band': band, 'count_min': cmin}))
    elif family == 'spacing':
        for sm in (0.02, 0.03, 0.06, 0.08):
            arms.append(Arm(family, 'sp_max=%.2f' % sm, {'spacing_max': sm}))
    elif family == 'gearing':
        for g in (2.4, 2.9, 3.9, 4.4):
            arms.append(Arm(family, 'gearing=%.1f' % g, {'gearing': g}))
    else:
        raise ValueError('未知参数族: %s' % family)
    return arms


FAMILIES = ('stop', 'trail', 'pv', 'funding', 'geom', 'spacing', 'gearing')


def sweep(cache, universe, families, window_names, *, workers=1, out_dir=None, log=print):
    """按族扫描 → 每族一份 CSV（arm × window × 指标）。返回 {family: DataFrame}。"""
    import time
    out = {}
    preloaded = {}
    for fam in families:
        arms = build_arms(fam)
        rows = []
        for wname in window_names:
            ws, we = (WINDOWS.get(wname) or HOLDOUT[wname])
            if wname not in preloaded:
                preloaded[wname] = preload_window(cache, universe, wname, ws, we,
                                                  workers=workers, log=log)
            wd = preloaded[wname]
            pv_cache = {}
            for arm in arms:
                t0 = time.time()
                df = run_arm(wd, arm, pv_cache, workers=workers)
                m = metrics(df, wd.days)
                rows.append(dict(family=fam, window=wname, arm=arm.label,
                                 is_base=arm.is_baseline(), **m))
                log('[sweep] %-7s %-4s %-18s ret=%+.2f%% mdd=-%.2f%% calmar=%.1f (%.0fs)'
                    % (fam, wname, arm.label, m['ret'] * 100, m['mdd'] * 100,
                       m['calmar'], time.time() - t0))
        out[fam] = pd.DataFrame(rows)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            out[fam].to_csv(os.path.join(out_dir, '%s_results.csv' % fam), index=False)
    return out
