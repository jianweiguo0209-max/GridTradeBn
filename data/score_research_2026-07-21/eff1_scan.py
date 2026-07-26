"""eff1 九窗全样本参数扫描(2026-07-26,brief 2026-07-26-eff1-fullsample-scan-brief.md)。

**性质红线**:知识/稳健性验证。扫描结果**不得回喂改动**预注册 v3 冻结的候选与链;
第二段按 v4 已写死的规则(逐轴平台中点/轴平保现值/P4 不参与选点)合成 eff1-opt。
**HOLD-F/JUL26 为终审专属,任何扫描变体禁止在其上运行**(27臂同场消费 HOLD-E 的教训)。

人群 = eff1:`p12_eff = cross1/(1+100·mae)`(标签 rt+12h=R 平移)全池降序 top-1,
缺标签币不参选。窗 = 判定九窗 W1/W2/OOS/IS/HOLD-A/B/C/D/E 全轮。

**层与共享**(preload 是大头,按 picks 是否变化分组):
  P1 几何 band×count  → 只改 gp 几何,picks 不变 ⇒ **共享基座 wd**
  P3 固损             → 只改 stop_cfg      ⇒ **共享基座 wd**
  P5 链余轴           → 只改 stop_cfg/pv   ⇒ **共享基座 wd**
  P2 top-K{2,3}       → 改 picks(生产 choose_symbols 语义带真 cap) ⇒ 各自 preload
  P4 mae 系数{50,200} → 改 eff 公式即改 picks ⇒ 各自 preload(零重建:直接用既有标签重算)
用法: EFF1_LAYER=P1|P2|P3|P4|P5 [BT_WINDOWS=W1,W2] eff1_scan.py
"""
import os
import re
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
from gridtrade.backtest.backtest_run import (BT_UNIVERSE_TOP_PCT, _FUNDING_BACK_MS,
                                             allocate_with_tiers, holding_bars)
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.shock_replay import blocked_rts
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
OUT = RD + '/ablation'
RESULTS = OUT + '/eff1_scan_results.txt'
LAYER = os.environ.get('EFF1_LAYER', 'P1')

WD9 = {'W1': ('2025-08-15', '2025-10-14'), 'W2': ('2025-10-15', '2025-12-14'),
       'OOS': ('2026-01-01', '2026-02-28'), 'IS': ('2026-03-01', '2026-06-30'),
       'HOLD-A': ('2025-02-01', '2025-03-31'), 'HOLD-B': ('2024-10-01', '2024-11-30'),
       'HOLD-C': ('2025-04-01', '2025-05-31'), 'HOLD-D': ('2024-12-01', '2025-01-31'),
       'HOLD-E': ('2025-06-01', '2025-08-14')}
WD = dict(WD9)
_only = os.environ.get('BT_WINDOWS', '')
if _only:
    WD = {k: v for k, v in WD.items() if k in _only.split(',')}
# ⚠终审窗硬隔离:即便误传也拒跑
for _bad in ('HOLD-F', 'JUL26'):
    WD.pop(_bad, None)

LAB = {w: '%s/hold_labels_%s.parquet' % (OUT, w) for w in WD9}

# ---- 实盘 n_fills 可信闸门(2026-07-26 用 prod 库 160 个已关闭格标定)----
# 实盘真实成交:mean 3.49 / median 3 / q0.9=8 / max 18;每小时中位 0.33 笔。
# grid_count=16(生产现值几何,60 样本):mean 4.03。回测同几何 mean 5.3 ⇒ **高估 32%**,
# 尾部 max 48 vs 实盘 18 ⇒ 高估 2.7 倍(4-tick 路径近似 + maker 100% 成交假设所致)。
# ⇒ 回测 n_fills 越界即为「外推出已知宇宙」,其收益不可信,不得进选点。
FILL_OK, FILL_DOUBT = 8.0, 18.0        # q0.9 / max


def fill_trust(nf):
    return 'OK' if nf <= FILL_OK else ('存疑' if nf <= FILL_DOUBT else '越界')


def pool_path(wn):
    for p in ('%s/p12_pool_%s.parquet' % (OUT, wn), '%s/rsp_pool_%s.parquet' % (OUT, wn)):
        if os.path.exists(p):
            return p
    raise FileNotFoundError(wn)


# ---- 层定义(全部先验固定,禁调) ----
# (标签, picks变体key, 链overrides, 几何overrides)
GEO_BASE = {'band': 3, 'count_min': 16}          # s030 生产现值
P1 = [('geo_b%s_c%d' % (b, c), 'K1', {}, {'band': b, 'count_min': c})
      for b in (2, 2.5, 3) for c in (16, 22, 26)]
# P2 top-K 已按用户令 2026-07-26 取消扫描(K>1 同时改变选币广度与组合结构
# ——资金分配/同币cap一起变,单轴归因不成立;top-K 保持现值 1)。
# 相应地 v4 §4「topK 显著优可入合成」条款自动失效,合成不含此轴。
P2 = []
# P1R 裸网格对照(用户令 2026-07-26"不止固定止损,是所有主动"):关闭全部主动退出
# ——固损/追踪/pv/费率四闸全停,只留「窗口结束」与「破网」。破网正是 band 边界的直接
# 表达,故此层测的是**纯几何效应**,不受任何主动止损截断。
# 诊断用途(与 P4 同性质,**不参与选点**):
#   若 P1R 与 P1 的相对排序一致 ⇒ 固损/pv 未扭曲几何轴,P1 选点可信;
#   若排序翻转或幅度显著变化 ⇒ 几何与链存在交互,须在报告中显式披露并据此决定选点口径。
# 干扰量级先验(HOLD-E 实测退出构成):窗口结束 79.7% / pv 14.7% / 固损 1.8% / 破网 ~0。
RAW_OFF = {'stop_loss': 9.9, 'trailing_k': 9.9, 'trailing_floor': 9.9,
           'active_stop_mode': 'none', 'funding_stop': 9.9}
P1R = [('geoRAW_b%s_c%d' % (b, c), 'K1', dict(RAW_OFF), {'band': b, 'count_min': c})
       for b in (2, 2.5, 3) for c in (16, 22, 26)]
P3 = [('stop%.1f' % s, 'K1', {'stop_loss': s / 100.0}, {}) for s in (1, 1.5, 2, 2.5, 3)]
P4 = [('mae%d' % m, 'MAE%d' % m, {}, {}) for m in (50, 200)]
P5 = ([('trF%d' % f, 'K1', {'trailing_floor': f / 100.0}, {}) for f in (1, 4)]
      + [('trK0.15', 'K1', {'trailing_k': 0.15}, {})]
      + [('pvthr-0.5', 'K1', {'pv_thr': -0.005}, {}),
         ('pvthr-2', 'K1', {'pv_thr': -0.02}, {}),
         ('pvmult5', 'K1', {'pv_mult': 5}, {})]
      + [('fund0.3', 'K1', {'funding_stop': 0.003}, {}),
         ('fundOFF', 'K1', {'funding_stop': 1.0}, {})])
LAYERS = {'P1': P1, 'P1R': P1R, 'P2': P2, 'P3': P3, 'P4': P4, 'P5': P5}
# 多层合并:EFF1_LAYER=P1,P3,P5 —— 三层同为 K1 变体,共享一次 preload(省 2/3 装载)
ARMS = [a for L in LAYER.split(',') for a in LAYERS[L]]
ARM_LAYER = {a[0]: L for L in LAYER.split(',') for a in LAYERS[L]}


def emit(line):
    open(RESULTS, 'a').write(line + '\n')
    print(line, flush=True)


def _load_eff(wn, mae_coef=100.0):
    lab = pd.read_parquet(LAB[wn])[['rt', 'symbol', 'cross1', 'mae']]
    lab['p12_eff'] = lab['cross1'] / (1.0 + mae_coef * lab['mae'])
    lab['rt'] = lab['rt'] + pd.Timedelta(hours=12)
    return lab[['rt', 'symbol', 'p12_eff']]


def make_picks(pool, variant, wn):
    """eff1 降序 top-K。variant: K1/K2/K3(广度) 或 MAE50/MAE200(公式系数)。"""
    k, coef = 1, 100.0
    if variant.startswith('K'):
        k = int(variant[1:])
    elif variant.startswith('MAE'):
        coef = float(variant[3:])
    d = pool[np.isfinite(pool['close']) & np.isfinite(pool['Atr_5'])
             & np.isfinite(pool['middle_5'])]
    d = d.merge(_load_eff(wn, coef), on=['rt', 'symbol'], how='inner')
    d = d.sort_values(['rt', 'p12_eff', 'symbol'], ascending=[True, False, True])
    # 生产 choose_symbols 语义:取 top-K 候选喂 allocate_with_tiers(真 cap 递补)
    kk = max(k, SW.TIER_CAND_K) if k == 1 else k
    d = d.groupby('rt', sort=False).head(kk).copy()
    d['rank'] = d.groupby('rt', sort=False).cumcount() + 1.0
    picks = []
    for r in d.itertuples(index=False):
        row = pd.Series({'symbol': r.symbol, 'rank': r.rank, 'close': r.close,
                         'Atr_5': r.Atr_5, 'middle_5': r.middle_5, 'time': r.rt})
        picks.append((r.rt, int(r.offset), row))
    return picks


def preload(cache, picks, wn, s0, e0, universe):
    ws, we = pd.Timestamp(s0), pd.Timestamp(e0) + pd.Timedelta(days=1)
    blocked = blocked_rts(cache, universe, ws, we, '1h', *SW.SHOCK,
                          min_quote_volume=0.0, top_volume_pct=BT_UNIVERSE_TOP_PCT)
    picks = [p for p in picks if p[0] not in blocked]
    picks, _st = allocate_with_tiers(picks, DEFAULT_TIER_POLICY, period=SW._S['period'])
    syms = sorted({row['symbol'] for _, _, row in picks})
    series = SR.load_full_series(cache, syms, '1m')
    fmap, raw = {}, []
    for rt, offset, row in picks:
        sym = row['symbol']
        if sym not in series:
            continue
        bars = holding_bars(series[sym], rt, SW._S['period'])
        if len(bars) == 0:
            continue
        if sym not in fmap:
            fmap[sym] = cache.read_all_days('funding', sym)
        fd = fmap[sym]
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
            m0 = re.match(r'^(P\w+)/([\w-]+): ', ln)
            if m0:
                wn, rest = m0.group(2), ln[m0.end():]
                if wn in WD and rest.split()[0] in names:
                    done.add((wn, rest.split()[0]))
    except FileNotFoundError:
        pass
    return done


def main():
    cache = ParquetCache(V.default_cache_root())
    w_sim = int(os.environ.get('BT_WORKERS', '3'))
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
    SW.set_baseline({})
    emit('== eff1_scan %s 开跑 %s (%d格×%d窗) =='
         % (LAYER, time.strftime('%m-%d %H:%M'), len(ARMS), len(WD)))
    done = _done()
    for wn, (s0, e0) in WD.items():
        todo = [a for a in ARMS if (wn, a[0]) not in done]
        if not todo:
            emit('[%s] SKIP' % wn)
            continue
        pool = pd.read_parquet(pool_path(wn))
        by_var = {}
        for a in todo:
            by_var.setdefault(a[1], []).append(a)
        for variant, arms_v in by_var.items():     # 同 picks 变体共享 preload 与 pv_cache
            t0 = time.time()
            wd = preload(cache, make_picks(pool, variant, wn), wn, s0, e0, universe)
            emit('[%s] %s preload %.1fmin 格=%d 币=%d'
                 % (wn, variant, (time.time() - t0) / 60, len(wd.raw), wd.n_symbols))
            pv_cache = {}
            for name, _v, chain_ov, geo_ov in arms_v:
                t0 = time.time()
                ov = dict(chain_ov)
                ov.update(geo_ov)                  # band/count_min 走 sweep 的几何维度
                df = SW.run_arm(wd, SW.Arm('eff1', name, ov), pv_cache, workers=w_sim)
                m = SW.metrics(df, wd.days)
                er = Counter(df['exit_reason'])
                top = ', '.join('%s:%d' % (k[:4], v) for k, v in er.most_common(3))
                nf = float(df['n_fills'].mean())
                emit('%s/%s: %-12s ret%+9.2f mdd%6.2f calmar%10.4g fills%7.1f[%s] '
                     '格%d 破%d 爆%d %.1fmin | %s'
                     % (ARM_LAYER.get(name, LAYER), wn, name, m['ret'] * 100,
                        -m['mdd'] * 100, m['calmar'], nf, fill_trust(nf),
                        m['n_grids'], m['n_broke'], m['n_blown'],
                        (time.time() - t0) / 60, top))
        emit('[%s] DONE' % wn)
    emit('EFF1_SCAN_%s_DONE' % LAYER)


if __name__ == '__main__':
    main()
