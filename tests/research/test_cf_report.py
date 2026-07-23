"""cf_report 指标数学(合成数据,无资产依赖):alpha/捕获/遗憾/ab_compare 已知答案。"""
import importlib.util
import os

import pandas as pd
import pytest

RD = 'data/score_research_2026-07-21'

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(RD, 'cf_report.py')),
    reason='cf_report not present')


def _mod():
    spec = importlib.util.spec_from_file_location(
        'cf_report', os.path.join(RD, 'cf_report.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _toy_cf():
    rows = []
    # 轮1: 池 A:+10bp B:0 C:-10bp,选中 A(=top1) → alpha=+10bp, hit=1, regret=0
    # 轮2: 同池,选中 B → alpha=0, hit=0, regret=+10bp
    for rt, pick in (('2026-01-01 00:00', 'A'), ('2026-01-01 01:00', 'B')):
        for sym, e0 in (('A', 0.0010), ('B', 0.0), ('C', -0.0010)):
            rows.append({'run_time': pd.Timestamp(rt), 'offset': 0, 'symbol': sym,
                         'in_pool': True, 'picked': sym == pick, 'Atr_5': 0.02,
                         'pnl_e0': e0, 'reason_e0': '窗口结束',
                         'pnl_s030': e0 / 2, 'reason_s030': 'x'})
    return pd.DataFrame(rows)


def test_per_round_metrics_math():
    m = _mod()
    d = m.per_round_metrics(_toy_cf())
    assert len(d) == 2
    r1, r2 = d.iloc[0], d.iloc[1]
    assert r1['hit'] == 1 and abs(r1['alpha_e0'] - 0.0010) < 1e-12
    assert abs(r1['regret']) < 1e-12
    assert r2['hit'] == 0 and abs(r2['regret'] - 0.0010) < 1e-12
    assert abs(r2['alpha_e0']) < 1e-12


def test_aggregate_tax_and_capture():
    m = _mod()
    cf = _toy_cf()
    agg = m.aggregate(m.per_round_metrics(cf), cf)
    assert agg['capture'] == 0.5                       # 2 轮命中 1
    # 止损链税 = e0 − s030 = e0/2;选中桶 {+10bp,0} → 税均 +2.5bp
    assert abs(agg['tax_picks_bp'] - 2.5) < 1e-9
    assert agg['picks_outside_pool'] == 0


def test_ab_compare_disjoint_only():
    m = _mod()
    cf = _toy_cf()
    rt1, rt2 = pd.Timestamp('2026-01-01 00:00'), pd.Timestamp('2026-01-01 01:00')
    pa = pd.DataFrame({'run_time': [rt1, rt2], 'symbol': ['A', 'B']})
    pb = pd.DataFrame({'run_time': [rt1, rt2], 'symbol': ['A', 'C']})
    r = m.ab_compare(cf, pa, pb)
    assert r['n_disjoint'] == 2                        # (rt2,B) vs (rt2,C)
    assert abs(r['mean_diff_bp'] - 10.0) < 1e-9        # 0 − (−10bp)
