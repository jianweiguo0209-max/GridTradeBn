"""净值侧四项检验 —— eff1×b2_c26 Calmar 3.4e17 结案(2026-07-26)。

成交侧三轮指控三轮翻案后(见 aggtrades_density.py:净修正 0.84~1.00),矛头转向净值侧。
本脚本把四个假设逐一证伪,并给出真实机制。**全部只读、不需预热、不改引擎。**

  ① metrics 是否把并行持仓当串行连乘?
     sweep.metrics 对同 lane 同 close_ts 的多格做 cumprod。若一轮开 k 格并行(资金分摊),
     正确应是 1+mean(r) 而非 Π(1+r) ⇒ 会按 k 倍放大对数收益。
     **结论:证伪** —— 走完整 allocate_with_tiers 后每 (rt,offset) 槽恰好 1 格(100%),
     lane 是严格串行的单格流水线,该分支从不开火。
     ⚠ 陷阱:make_picks 返回的是**分配前候选**(choose_symbols=5),直接数会得到"每槽 5 格"
     的假阳性。必须走 allocate_with_tiers。

  ② 趋势行情的亏损是否被漏记?
     **结论:证伪** —— 合成路径实测 跌到带底 −6.08% / 破网 −7.02% / 涨到带顶 −6.16% /
     反复振荡 +10.12%,方向与量级均正确(中性网格涨也亏,符合)。

  ③ 选币标签是否泄漏未来?
     score_audit._label_one: `seg = slice(rt, rt+12h-1min)` ⇒ 标签@rt 覆盖**前视** [rt,rt+12h);
     _load_eff 再做 `rt += 12h`。决策时刻 T 用的是 [T−12h, T) 的已实现数据。
     **结论:证伪** —— 严格 PIT。

  ④ 那 359 倍到底哪来的?
     **真实机制**:波动聚集。过去 cross1 ↔ 未来 cross1 秩相关 +0.74(OOS)~+0.81(IS)。
     eff1 选中币的**未来** cross1 = 全池 4.04×(利润源),而**未来** drift 仅 1.01×(亏损源),
     且灾难尾被截断(drift max 0.40 vs 全池 4.22 = 0.10×)。
     ⇒ 4 倍振荡 + 同等漂移 + 十分之一尾部 = 高收益 + 低回撤。机制自洽,不是缺陷。

  ⑤ Calmar 3.4e17 = 年化算子爆炸(逐位对上):
     ann = (1+359.32)^(365/59) − 1 = 6.558e15;/ 0.019 = 3.452e17。
     ⚠ **推论(影响全项目)**:59 天窗里 Calmar ∝ (1+ret)^6.19 / MDD ——
     收益翻倍 Calmar 涨 73 倍。它名义是风险调整收益,实际是**带 6.19 次幂的收益排序**。

真正遗留的问题是**容量**:359 倍意味着仓位也涨 359 倍(单线 $155 → $5.6万),
而这些币 24h 成交额仅 $2~20M。边是真的,但不可规模化,而回测里没有这堵墙。

用法: blowup_equity_audit.py
"""
import importlib.util
import os
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import sweep as SW
from gridtrade.backtest.backtest_run import allocate_with_tiers
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.grid_engine import grid_order_info, simulate_grid_engine

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
AB = RD + '/ablation'
_s = importlib.util.spec_from_file_location('sc', RD + '/eff1_scan.py')
S = importlib.util.module_from_spec(_s)
_s.loader.exec_module(S)


def check1_concurrency(windows=('OOS', 'W1')):
    print('=' * 74)
    print('① metrics 的 cumprod 是否把并行当串行 —— 数真实并发度')
    SW.set_baseline({})
    for wn in windows:
        pool = pd.read_parquet(S.pool_path(wn))
        picks = S.make_picks(pool, 'K1', wn)
        kept, _ = allocate_with_tiers(picks, DEFAULT_TIER_POLICY, period=SW._S['period'])
        p = pd.DataFrame([{'rt': rt, 'offset': off} for rt, off, _row in kept])
        c = p.groupby(['rt', 'offset']).size()
        print('  %-5s 分配前=%-5d 分配后=%-5d 槽=%-5d 每槽 mean=%.3f max=%d  >1格占比=%.2f%%'
              % (wn, len(picks), len(p), len(c), c.mean(), c.max(),
                 (c > 1).mean() * 100))
    print('  ⇒ 每槽恰好 1 格 ⇒ lane 严格串行,cumprod 分支从不开火。假设证伪。')


def check2_loss_accounting():
    print('=' * 74)
    print('② 趋势亏损是否漏记 —— 合成路径')
    SW.set_baseline({})
    entry, band, n = 100.0, 0.123, 26              # b2_c26 实测几何
    lo, hi = entry * (1 - band), entry * (1 + band)
    gp = {'low_price': lo, 'high_price': hi, 'grid_count': n,
          'stop_low_price': lo * 0.99, 'stop_high_price': hi * 1.01}

    def run(path, label):
        t = pd.date_range('2026-01-01', periods=len(path), freq='1min')
        p = np.asarray(path, float)
        df = pd.DataFrame({'candle_begin_time': t, 'open': p, 'high': p,
                           'low': p, 'close': p, 'volume': 1.0, 'quote_volume': 1e7})
        r = simulate_grid_engine(df, gp, cap=1000.0, leverage=3.0, fee=0.0002,
                                 c_rate_taker=0.0005, max_rate=SW.MAX_RATE,
                                 min_amount=0.0, stop_cfg=None, funding_df=None,
                                 neutral_init=False)
        print('  %-14s pnl=%+.4f ($%+.1f)  n_trades=%-4s exit=%s'
              % (label, r['pnl_ratio'], r['pnl_ratio'] * 1000, r.get('n_trades'),
                 r.get('exit_reason')))
    run(list(np.linspace(entry, lo, 720)), '跌到带底')
    run(list(np.linspace(entry, lo * 0.985, 720)), '跌破网')
    run(list(np.linspace(entry, hi, 720)), '涨到带顶')
    osc = []
    for _ in range(12):
        osc += list(np.linspace(entry, lo, 30)) + list(np.linspace(lo, entry, 30))
    run(osc, '反复振荡')
    print('  ⇒ 亏损方向与量级均正确(中性网格涨也亏)。假设证伪。')


def check4_mechanism(windows=('OOS', 'W1', 'IS')):
    print('=' * 74)
    print('④ 359 倍的真实机制 —— eff1 选中币的**未来**标签')
    for wn in windows:
        p = '%s/hold_labels_%s.parquet' % (AB, wn)
        if not os.path.exists(p):
            continue
        lab = pd.read_parquet(p)
        lab['eff'] = lab['cross1'] / (1.0 + 100.0 * lab['mae'])
        past = lab.copy()
        past['rt'] = past['rt'] + pd.Timedelta(hours=12)
        past = past.rename(columns={'eff': 'past_eff', 'cross1': 'past_cross1'})
        j = lab.merge(past[['rt', 'symbol', 'past_eff', 'past_cross1']],
                      on=['rt', 'symbol']).dropna(subset=['past_eff', 'eff'])
        pick = (j.sort_values(['rt', 'past_eff', 'symbol'], ascending=[True, False, True])
                 .groupby('rt').head(1))
        rho = np.corrcoef(j['past_cross1'].rank(), j['cross1'].rank())[0, 1]
        print('  %-5s 过去↔未来 cross1 秩相关=%+.3f | 选中/全池: 未来cross1 %.2fx  '
              '未来drift %.2fx  drift-max %.2fx'
              % (wn, rho, pick['cross1'].mean() / j['cross1'].mean(),
                 pick['drift'].mean() / j['drift'].mean(),
                 pick['drift'].max() / j['drift'].max()))
    print('  ⇒ 4 倍振荡 + 同等漂移 + 十分之一灾难尾 ⇒ 高收益低回撤。机制自洽,非缺陷。')


def check5_calmar(ret=359.32, mdd=0.019, days=59):
    print('=' * 74)
    print('⑤ Calmar 3.4e17 的算术来源')
    ann = (1 + ret) ** (365.0 / days) - 1
    print('  ann = (1+%.2f)^(365/%d) − 1 = %.4g   calmar = ann/%.3f = %.4g'
          % (ret, days, ann, mdd, ann / mdd))
    print('  ⇒ 年化算子爆炸,非独立信息。推论:%d 天窗里 Calmar ∝ (1+ret)^%.2f / MDD'
          % (days, 365.0 / days))
    print('     ⇒ 收益翻倍 Calmar 涨 %.0f 倍 —— 名为风险调整,实为带幂的收益排序。'
          % 2 ** (365.0 / days))


if __name__ == '__main__':
    check1_concurrency()
    check2_loss_accounting()
    check4_mechanism()
    check5_calmar()
    print('=' * 74)
    print('③ 标签泄漏:见 score_audit._label_one —— seg=[rt,rt+12h) 前视 + _load_eff rt+=12h')
    print('   ⇒ 决策时刻 T 用 [T−12h, T) 已实现数据,严格 PIT。假设证伪(读码即证,无需运行)。')
