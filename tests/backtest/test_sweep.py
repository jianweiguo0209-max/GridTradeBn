"""扫参 harness 守卫（spec 2026-07-15-binance-param-sweep §5）。

关键不变量：
  1. 每族第一臂恒为 BASE（现值），且 BASE 的参数逐项等于 config——防基线漂移后仍按老基线解读；
  2. 单旋钮原则：非基线臂只覆盖本族参数，其余项与基线逐项相同；
  3. tasks_for 的 pv 尖峰按 (mult,n,period) 键复用——同键零重算、异键必重算（省最贵内循环）；
  4. metrics 的 lane 数学（12 offset 各自复利 → 等权平均）与 summarize 同源。
"""
import numpy as np
import pandas as pd
import pytest

from gridtrade.backtest import sweep as SW
from gridtrade.backtest.backtest_run import summarize


def test_baseline_matches_live_config():
    from gridtrade.config import DEFAULT_STOP_CFG, DEFAULT_STRATEGY_CONFIG
    b = SW.baseline()
    assert b['stop_loss'] == DEFAULT_STOP_CFG['stop_loss']
    assert b['trailing_k'] == DEFAULT_STOP_CFG['trailing_k']
    assert b['trailing_floor'] == DEFAULT_STOP_CFG['trailing_floor']
    assert b['funding_stop'] == DEFAULT_STOP_CFG['fundingRate_stop_loss']
    assert b['pv_thr'] == DEFAULT_STOP_CFG['pv_pnl_thr']
    assert b['pv_mult'] == DEFAULT_STOP_CFG['pv_mult']
    assert b['pv_n'] == DEFAULT_STOP_CFG['pv_n']
    v2 = DEFAULT_STRATEGY_CONFIG['grid_v2_config']
    assert b['band'] == v2['atr_range_multiplier']
    assert b['count_min'] == v2['grid_count_min']
    assert b['spacing_max'] == v2['grid_spacing_max']
    # gearing = leverage × max_rate（实盘 GRID_GEARING=3.4）
    assert abs(b['gearing'] - 3.4) < 1e-12


@pytest.mark.parametrize('family', SW.FAMILIES)
def test_first_arm_is_baseline_and_single_knob(family):
    arms = SW.build_arms(family)
    assert arms[0].is_baseline(), '每族第一臂须为 BASE（现值对照）'
    assert arms[0].params() == SW.baseline()
    base = SW.baseline()
    knobs = {
        'stop': {'stop_loss'},
        'trail': {'trailing_k', 'trailing_floor'},
        'pv': {'pv_thr', 'pv_mult', 'pv_n', 'pv_period', 'active_stop_mode'},
        'funding': {'funding_stop'},
        'geom': {'band', 'count_min'},
        'spacing': {'spacing_max'},
        'gearing': {'gearing'},
    }[family]
    for arm in arms[1:]:
        assert set(arm.overrides) <= knobs, '单旋钮原则：%s 越界改了 %s' % (family, arm.overrides)
        p = arm.params()
        for k, v in base.items():           # 未覆盖项须与基线逐项相同
            if k not in arm.overrides:
                assert p[k] == v
    labels = [a.label for a in arms]
    assert len(labels) == len(set(labels)), '臂标签须唯一'


def _fake_wd(n_grids=4):
    """合成 WindowData：价格锯齿（保证触网）、量能恒定（pv 不误报）。"""
    rows, raw = [], []
    t0 = pd.Timestamp('2026-03-01 00:00:00')
    for i in range(n_grids):
        rt = t0 + pd.Timedelta(hours=12 * i)
        idx = pd.date_range(rt - pd.Timedelta(hours=30), rt + pd.Timedelta(hours=12),
                            freq='1min')[:-1]
        px = 10.0 + 0.05 * np.sin(np.arange(len(idx)) / 20.0)
        series = pd.DataFrame({
            'symbol': 'AAA/USDT:USDT', 'candle_begin_time': idx,
            'open': px, 'high': px * 1.002, 'low': px * 0.998, 'close': px,
            'vol': 100.0, 'volCcy': 1000.0, 'quote_volume': 1000.0})
        bars = series[series['candle_begin_time'] >= rt].reset_index(drop=True)
        row = pd.Series({'symbol': 'AAA/USDT:USDT', 'close': 10.0,
                         'Atr_5': 0.02, 'middle_5': 10.0})
        raw.append((rt, i % 12, row, bars, None, series))
        rows.append(rt)
    return SW.WindowData(name='T', start=t0, end=t0 + pd.Timedelta(days=2), days=2,
                         raw=raw, n_blocked=0, n_symbols=1)


def test_pv_cache_reused_across_same_key_and_recomputed_on_change(monkeypatch):
    wd = _fake_wd()
    calls = {'n': 0}
    real = SW.pv_spike_for_window

    def _counted(series, bars, pv_cfg):
        calls['n'] += 1
        return real(series, bars, pv_cfg)

    monkeypatch.setattr(SW, 'pv_spike_for_window', _counted)
    pv_cache = {}
    base = SW.baseline()
    SW.tasks_for(wd, base, pv_cache)
    assert calls['n'] == len(wd.raw)              # 首次：逐格算
    SW.tasks_for(wd, dict(base, stop_loss=0.03), pv_cache)
    assert calls['n'] == len(wd.raw)              # 同 pv key（非 pv 族）：零重算
    SW.tasks_for(wd, dict(base, pv_mult=4), pv_cache)
    assert calls['n'] == 2 * len(wd.raw)          # pv key 变：必重算


def test_geometry_arm_changes_grid_params():
    wd = _fake_wd(1)
    pv_cache = {}
    base_task = SW.tasks_for(wd, SW.baseline(), pv_cache)[0]
    wide_task = SW.tasks_for(wd, dict(SW.baseline(), band=5), pv_cache)[0]
    base_gp, wide_gp = base_task[4], wide_task[4]
    assert wide_gp['high_price'] > base_gp['high_price'], '带宽↑ → 网格区间应变宽'
    assert wide_gp['low_price'] < base_gp['low_price']


def test_metrics_matches_summarize_portfolio_return():
    rng = np.random.RandomState(7)
    n = 60
    df = pd.DataFrame({
        'run_time': [pd.Timestamp('2026-03-01') + pd.Timedelta(hours=i) for i in range(n)],
        'offset': [i % 12 for i in range(n)],
        'symbol': 'AAA/USDT:USDT',
        'pnl_ratio': rng.normal(0.001, 0.01, size=n),
        'exit_reason': ['窗口结束'] * n,
        'n_fills': 5,
    })
    m = SW.metrics(df, days=30)
    assert abs(m['ret'] - summarize(df)['portfolio_return']) < 1e-9, 'lane 数学须与 summarize 同源'
    assert m['n_grids'] == n and m['mdd'] >= 0.0


def test_metrics_empty_is_safe():
    m = SW.metrics(pd.DataFrame(), days=30)
    assert m['n_grids'] == 0 and m['calmar'] == 0.0


def test_merge_csv_accumulates_across_windows(tmp_path):
    """逐窗分次跑须累积、不覆盖（16G 机器一次只驻留一窗序列）；同键重跑=覆盖旧行。"""
    d = str(tmp_path)
    a = pd.DataFrame([{'family': 'stop', 'window': 'OOS', 'arm': 'BASE', 'ret': 0.05}])
    SW._merge_csv(d, 'stop', a)
    b = pd.DataFrame([{'family': 'stop', 'window': 'IS', 'arm': 'BASE', 'ret': 0.14}])
    out = SW._merge_csv(d, 'stop', b)
    assert set(out['window']) == {'OOS', 'IS'}, '第二窗须累积、不覆盖第一窗'
    c = pd.DataFrame([{'family': 'stop', 'window': 'OOS', 'arm': 'BASE', 'ret': 0.99}])
    out = SW._merge_csv(d, 'stop', c)
    assert len(out) == 2 and float(out[(out['window'] == 'OOS')]['ret'].iloc[0]) == 0.99, \
        '同 (window,arm) 重跑须覆盖旧行'
