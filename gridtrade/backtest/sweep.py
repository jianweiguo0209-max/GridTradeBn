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

# ---- 边界自动扩展（用户定 2026-07-15）----
# 最优值落在网格边界 = 网格没铺够、真最优还在外面 → 沿该方向加点重扫，直到最优落在内部
# 或撞上硬限。硬限是物理/安全边界，不是调参偏好：
#   stop_loss    下限 0.005（-0.5% cap，再紧就是在噪声上止损）；上限 0.20（等同不设）
#   trailing_k/floor 同理（floor 下限 0.002=0.2%）
#   pv_thr       **可为负**（legacy 默认 -0.015=只在已亏时才撤）；范围 ±0.03
#   pv_mult      下限 1.2（尖峰判定退化为"任何高于均值"）；上限 8
#   funding_stop 下限 1bp；上限 2%（币安 8h 结算，超此即等同不设）
#   band         下限 0.75（区间窄于 0.75×ATR 基本必破网）；上限 10
#   count_min    下限 2（至少一买一卖）；上限 60（受 grid_count_max=149 与最小名义额约束）
#   spacing_max  下限 0.005（须 > grid_spacing_min=0.003）；上限 0.20
#   gearing      下限 1.0；上限 6.0（再高账户级 AL 敞口失控，且爆仓否决线会先淘汰）
DIM_LIMITS = {
    'stop': {'stop_loss': (0.005, 0.20)},
    'trail': {'trailing_k': (0.02, 0.60), 'trailing_floor': (0.002, 0.06)},
    'pv': {'pv_thr': (-0.03, 0.03), 'pv_mult': (1.2, 8.0)},
    'funding': {'funding_stop': (0.0001, 0.02)},
    'geom': {'band': (0.75, 10.0), 'count_min': (2, 60)},
    'spacing': {'spacing_max': (0.005, 0.20)},
    'gearing': {'gearing': (1.0, 6.0)},
}
_INT_DIMS = {'count_min'}


def _arm_label(family, coord):
    """扩边臂的标签须与 build_arms 的格式逐字一致（CSV 按 label 合并、去重）。"""
    fmt = {
        'stop': lambda c: 'sl=%.3f' % c['stop_loss'],
        'trail': lambda c: 'k=%.2f,fl=%.3f' % (c['trailing_k'], c['trailing_floor']),
        'funding': lambda c: 'fr=%.4f' % c['funding_stop'],
        'geom': lambda c: 'band=%s,cmin=%d' % (_num(c['band']), int(c['count_min'])),
        'spacing': lambda c: 'sp_max=%.2f' % c['spacing_max'],
        'gearing': lambda c: 'gearing=%.1f' % c['gearing'],
    }
    if family == 'pv':                       # pv 标签依覆盖项而定（与 build_arms 同形）
        has_thr = 'pv_thr' in coord
        has_mult = 'pv_mult' in coord
        if has_thr and has_mult:
            return 'thr=%.4f,mult=%s' % (coord['pv_thr'], _num(coord['pv_mult']))
        if has_thr:
            return 'thr=%.4f' % coord['pv_thr']
        return 'mult=%s' % _num(coord['pv_mult'])
    return fmt[family](coord)


def _num(v):
    """2 与 2.0 须同形（标签唯一性 = 合并去重的键）。"""
    f = float(v)
    return int(f) if f == int(f) else f


_LABEL_KEYS = {           # label 里的键 → baseline 维名（解析用；须与 _arm_label 同源）
    'sl': 'stop_loss', 'k': 'trailing_k', 'fl': 'trailing_floor',
    'thr': 'pv_thr', 'mult': 'pv_mult', 'fr': 'funding_stop',
    'band': 'band', 'cmin': 'count_min', 'sp_max': 'spacing_max', 'gearing': 'gearing',
}


def parse_coord(family, label):
    """臂标签 → 数值坐标（无坐标的 OFF/模式臂返回 None）。

    从**标签**解析而非从 build_arms 查表：扩边新增的臂不在初始网格里，而扫参会分多次调用
    （逐窗跑 / 断点续跑），必须能从 CSV 里的历史行重建坐标——否则第二轮扩边会看不见第一轮
    加的点、把同一批点反复外推。
    """
    dims = DIM_LIMITS[family]
    if label.startswith('BASE'):
        return {d: baseline()[d] for d in dims}
    if 'OFF' in label:
        return None
    coord = {d: baseline()[d] for d in dims}
    for part in label.split(','):
        if '=' not in part:
            return None
        k, v = part.split('=', 1)
        dim = _LABEL_KEYS.get(k.strip())
        if dim is None or dim not in dims:
            return None
        try:
            coord[dim] = int(v) if dim in _INT_DIMS else float(v)
        except ValueError:
            return None
    return coord


def arm_coords(family, labels):
    """{label: coord}——只保留能解析出数值坐标的臂（OFF 类臂被排除在边界判定之外）。"""
    out = {}
    for lb in labels:
        c = parse_coord(family, lb)
        if c is not None:
            out[lb] = c
    return out


def rank_arms(df, *, windows=None, complete_only=False):
    """按用户定的判定标准排序（spec §2）：主序 Calmar，并列键 worst-window Calmar；
    破网/爆仓 → 直接淘汰（否决线）。返回按优劣降序的 DataFrame（一行一臂）。

    complete_only=True：**只排「在全部判定窗都有结果」的臂**——16G 机器上扫参是逐窗分次跑的，
    新加的臂在跑完最后一窗前只有部分窗口的行；若让它们参与排序，同一轮扩边的 4 次调用会
    因 CSV 增长而给出不同判定（跨窗不一致）。扩边判定恒用此模式。"""
    d = df if windows is None else df[df['window'].isin(windows)]
    if d.empty:
        return d
    agg = d.groupby('arm').agg(
        n_windows=('window', 'nunique'),
        calmar_mean=('calmar', 'mean'), calmar_worst=('calmar', 'min'),
        ret_mean=('ret', 'mean'), ret_worst=('ret', 'min'),
        mdd_worst=('mdd', 'max'), n_broke=('n_broke', 'sum'), n_blown=('n_blown', 'sum'),
    ).reset_index()
    if complete_only:
        need = d['window'].nunique()
        agg = agg[agg['n_windows'] == need]
    agg['vetoed'] = (agg['n_broke'] > 0) | (agg['n_blown'] > 0)   # 否决线
    return agg.sort_values(['vetoed', 'calmar_worst', 'calmar_mean'],
                           ascending=[True, False, False]).reset_index(drop=True)


def expand_arms(family, df, *, n_new=3, windows=None):
    """最优臂若落在某维网格**边界**上 → 沿该方向外推 n_new 个新点（步长=该维最外侧两点
    的间距，撞硬限即截断）。返回 (新臂列表, 说明)。最优在内部 → 返回 ([], 原因)。

    多维族（trail/pv/geom）逐维独立判定：某维在边界就沿该维外推，其余维固定在赢家坐标上。
    """
    dims = DIM_LIMITS[family]
    coords = arm_coords(family, sorted(set(df['arm'])))         # 含历轮扩边点（从 CSV 重建）
    ranked = rank_arms(df, windows=windows, complete_only=True)  # 只认全窗跑完的臂
    ranked = ranked[~ranked['vetoed']]
    winners = [a for a in ranked['arm'] if a in coords]        # 跳过 OFF 类臂（无坐标）
    if not winners:
        return [], '无有效数值臂（全被否决或仅 OFF 臂）'
    win = coords[winners[0]]
    new_arms, notes = [], []
    seen = {tuple(sorted(c.items())) for c in coords.values()}   # 含历轮已跑点，防重复
    for dim, (lo_lim, hi_lim) in dims.items():
        tested = sorted({c[dim] for c in coords.values()})
        if len(tested) < 2:
            continue
        v = win[dim]
        at_lo, at_hi = (v <= tested[0] + 1e-12), (v >= tested[-1] - 1e-12)
        if not (at_lo or at_hi):
            continue
        if at_lo:
            step = tested[1] - tested[0]
            cand = [tested[0] - step * (i + 1) for i in range(n_new)]
            cand = [c for c in cand if c >= lo_lim - 1e-12]
            edge, room, lim = '下界 %s' % _num(tested[0]), tested[0] - lo_lim, lo_lim
        else:
            step = tested[-1] - tested[-2]
            cand = [tested[-1] + step * (i + 1) for i in range(n_new)]
            cand = [c for c in cand if c <= hi_lim + 1e-12]
            edge, room, lim = '上界 %s' % _num(tested[-1]), hi_lim - tested[-1], hi_lim
        # 步长外推全被硬限截断，但边界与硬限之间**仍有空间** → 改为在这段区间内细分。
        # 否则「步长 > 剩余空间」时整片区域被静默跳过（funding 实证：赢家 0.0005、硬限
        # 0.0001、步长 0.0005 → 一步跨到 0 以下被清空，0.0002/3/4 从未被测）。
        subdivided = False
        if not cand and room > 1e-12:
            v0 = tested[0] if at_lo else tested[-1]
            gap = (v0 - lim) if at_lo else (lim - v0)
            cand = [lim + gap * i / float(n_new + 1) for i in range(n_new + 1)] if at_lo \
                else [lim - gap * i / float(n_new + 1) for i in range(n_new + 1)]
            subdivided = True
        cand = [int(round(c)) if dim in _INT_DIMS else round(float(c), 6) for c in cand]
        cand = [c for c in cand if lo_lim - 1e-12 <= c <= hi_lim + 1e-12]
        if subdivided:
            edge += '（步长越限 → 在硬限 %s 与边界间细分）' % _num(lim)
        if not cand:
            notes.append('%s 赢家在%s但已撞硬限 %s' % (dim, edge, (lo_lim, hi_lim)))
            continue
        for c in cand:
            coord = dict(win, **{dim: c})
            key = tuple(sorted(coord.items()))
            if key in seen:
                continue
            seen.add(key)
            ov = {k: val for k, val in coord.items() if val != baseline()[k]}
            if not ov:
                continue
            new_arms.append(Arm(family, _arm_label(family, ov), ov))
        notes.append('%s 赢家在%s → 外推 %s' % (dim, edge, [_num(c) for c in cand]))
    if not new_arms:
        notes.append('最优在网格内部（或已撞硬限）→ 无需扩边')
    return new_arms, '; '.join(notes)


def _run_arms(wd, arms, fam, pv_cache, *, workers, log):
    """在一个窗上跑一批臂 → rows。"""
    import time
    rows = []
    for arm in arms:
        t0 = time.time()
        df = run_arm(wd, arm, pv_cache, workers=workers)
        m = metrics(df, wd.days)
        rows.append(dict(family=fam, window=wd.name, arm=arm.label,
                         is_base=arm.is_baseline(), **m))
        log('[sweep] %-7s %-6s %-20s ret=%+.2f%% mdd=-%.2f%% calmar=%.1f (%.0fs)'
            % (fam, wd.name, arm.label, m['ret'] * 100, m['mdd'] * 100,
               m['calmar'], time.time() - t0))
    return rows


def read_results(out_dir, family):
    path = os.path.join(out_dir, '%s_results.csv' % family)
    return pd.read_csv(path) if os.path.exists(path) else pd.DataFrame()


def arms_missing_window(family, df, wname):
    """CSV 里已存在、但**本窗缺结果**的臂 → 补跑（自愈）。

    没有它会死锁：逐窗分次跑时若某窗漏跑了某臂（中途改代码/中断/新扩的臂晚于该窗生成），
    该臂就永远不完整 → complete_only 排名把它排除 → 下一轮扩边又因它已在 CSV 里(seen 命中)
    而不再提议 → 补不齐、成为死行（funding 的 0.0001~0.0004 实证）。"""
    if df.empty:
        return []
    have = set(zip(df['arm'], df['window']))
    by_label = {a.label: a for a in build_arms(family)}      # OFF/模式臂只能从这里找回
    out = []
    for lb in sorted(set(df['arm'])):
        if (lb, wname) in have:
            continue
        if lb in by_label:
            out.append(by_label[lb])
            continue
        c = parse_coord(family, lb)
        if c is None:
            continue                                        # 无坐标又不在网格里 → 弃
        ov = {k: v for k, v in c.items() if v != baseline()[k]}
        out.append(Arm(family, lb, ov))                     # 保留原 label（=合并键）
    return out


def sweep(cache, universe, families, window_names, *, workers=1, out_dir=None,
          mode='base', log=print):
    """按族扫描 → 每族一份 CSV（arm × window × 指标）。返回 {family: DataFrame}。

    mode='base'   跑 build_arms 的初始网格（Pass 1）。
    mode='expand' **边界自动扩展**（用户定 2026-07-15）：从已有 CSV 判定——最优臂若落在某维
                  网格边界上（= 网格没铺够、真最优还在外面），沿该方向外推新点再跑；最优落
                  在内部或撞上 DIM_LIMITS 硬限则本族跳过。

    16G 机器上一次只能驻留一个窗口的 1m 序列 → 扫参是**逐窗分次调用**的；因此扩边的「轮」
    由外层 shell 驱动（每轮把 4 个窗各跑一次），判定用 complete_only（只认全窗跑完的臂），
    保证同一轮的 4 次调用给出一致的扩边集合。
    """
    out = {}
    preloaded = {}

    def _wd(wname):
        if wname not in preloaded:
            ws, we = (WINDOWS.get(wname) or HOLDOUT[wname])
            preloaded[wname] = preload_window(cache, universe, wname, ws, we,
                                              workers=workers, log=log)
        return preloaded[wname]

    for fam in families:
        new_arms = []
        hist = pd.DataFrame()
        if mode == 'base':
            new_arms = build_arms(fam)
        else:
            hist = read_results(out_dir, fam)
            if hist.empty:
                log('[sweep] %s 无历史结果，跳过扩边（先跑 --mode base）' % fam)
                continue
            judge = sorted(set(hist['window']))
            new_arms, note = expand_arms(fam, hist, windows=judge)
            log('[sweep] %s 扩边判定(依据窗口 %s): %s' % (fam, ','.join(judge), note))
        rows = []
        for wname in window_names:
            # 先自愈：CSV 里已有、但本窗缺结果的臂（逐窗分次跑天然会产生这种空洞）
            arms = list(new_arms)
            if not hist.empty:
                miss = arms_missing_window(fam, hist, wname)
                miss = [a for a in miss if a.label not in {n.label for n in new_arms}]
                if miss:
                    log('[sweep] %s %s 补跑缺窗臂 %d 个: %s'
                        % (fam, wname, len(miss), [a.label for a in miss]))
                arms = miss + arms
            if not arms:
                continue
            rows += _run_arms(_wd(wname), arms, fam, {}, workers=workers, log=log)
        if not rows:
            continue
        df = pd.DataFrame(rows)
        out[fam] = _merge_csv(out_dir, fam, df) if out_dir else df
    return out


def _merge_csv(out_dir, family, df):
    """按 (family, window, arm) 合并进已有 CSV——逐窗分次跑（16G 机器上一次只能驻留一窗
    的 1m 序列）不会互相覆盖；同键重跑=覆盖旧行（断点续跑/改网格后重跑都安全）。"""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, '%s_results.csv' % family)
    if os.path.exists(path):
        old = pd.read_csv(path)
        keys = set(zip(df['window'], df['arm']))
        old = old[~old.apply(lambda r: (r['window'], r['arm']) in keys, axis=1)]
        df = pd.concat([old, df], ignore_index=True)
    df = df.sort_values(['window', 'arm']).reset_index(drop=True)
    df.to_csv(path, index=False)
    return df
