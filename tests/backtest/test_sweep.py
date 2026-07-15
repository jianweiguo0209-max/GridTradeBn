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


# ---- 边界自动扩展（用户定 2026-07-15：最优在边界=网格没铺够，真最优还在外面）----

def _res(family, rows):
    """rows: [(arm, calmar, ret)] → 单窗结果 df（扩边判定用）。"""
    return pd.DataFrame([{'family': family, 'window': 'OOS', 'arm': a, 'calmar': c,
                          'ret': r, 'mdd': 0.02, 'n_broke': 0, 'n_blown': 0}
                         for a, c, r in rows])


@pytest.mark.parametrize('family', SW.FAMILIES)
def test_parse_coord_roundtrips_every_arm_label(family):
    """每个数值臂的 label 须能解析回它自己的坐标——扩边靠它从 CSV 重建历轮点。"""
    base = SW.baseline()
    for arm in SW.build_arms(family):
        c = SW.parse_coord(family, arm.label)
        if 'OFF' in arm.label:
            assert c is None
            continue
        assert c is not None, '%s 的 label 解析失败: %s' % (family, arm.label)
        for dim in SW.DIM_LIMITS[family]:
            expected = arm.overrides.get(dim, base[dim])
            if expected is None:            # 关闭型维（如 trailing 现值=None）：不比数值
                assert c[dim] is None
                continue
            assert abs(float(c[dim]) - float(expected)) < 1e-9, arm.label


def test_expand_extends_below_when_winner_at_lower_edge():
    """stop 赢家在下界 0.030 → 沿下方外推（步长=最外两点间距 0.005）。"""
    df = _res('stop', [('BASE(现值)', 20.0, 0.05), ('sl=0.030', 41.8, 0.067),
                       ('sl=0.035', 39.6, 0.070), ('sl=0.040', 27.7, 0.060),
                       ('sl=0.055', 21.2, 0.050), ('sl=0.080', 11.1, 0.033)])
    arms, note = SW.expand_arms('stop', df, n_new=3)
    got = sorted(round(a.overrides['stop_loss'], 4) for a in arms)
    assert got == [0.015, 0.020, 0.025], got
    assert '下界' in note


def test_expand_stops_when_winner_interior():
    df = _res('stop', [('sl=0.030', 10.0, 0.02), ('sl=0.035', 41.8, 0.07),
                       ('sl=0.040', 12.0, 0.03)])
    arms, note = SW.expand_arms('stop', df, n_new=3)
    assert arms == [] and '内部' in note


def test_expand_respects_hard_limit():
    """撞硬限即截断（stop 下限 0.005）——不会外推出无意义/危险的值。"""
    df = _res('stop', [('sl=0.010', 50.0, 0.08), ('sl=0.015', 40.0, 0.07)])
    arms, _ = SW.expand_arms('stop', df, n_new=3)
    vals = [a.overrides['stop_loss'] for a in arms]
    assert vals and all(v >= SW.DIM_LIMITS['stop']['stop_loss'][0] - 1e-9 for v in vals), vals


def test_expand_second_round_sees_first_round_points():
    """第二轮须从 CSV 重建历轮点（否则会把同一批点反复外推）。"""
    df = _res('stop', [('sl=0.030', 41.8, 0.067), ('sl=0.035', 39.6, 0.070),
                       ('sl=0.025', 45.0, 0.072), ('sl=0.020', 48.0, 0.075),
                       ('sl=0.015', 52.0, 0.080)])      # 赢家 0.015 = 新下界
    arms, note = SW.expand_arms('stop', df, n_new=2)
    vals = sorted(a.overrides['stop_loss'] for a in arms)
    assert vals == [0.005, 0.010], vals      # 从 0.015 继续下推，撞硬限 0.005 截断


def test_expand_vetoes_blown_arms():
    """破网/爆仓臂不得成为扩边赢家（否决线）。"""
    df = pd.DataFrame([
        {'family': 'gearing', 'window': 'OOS', 'arm': 'gearing=4.4', 'calmar': 99.0,
         'ret': 0.2, 'mdd': 0.05, 'n_broke': 0, 'n_blown': 7},      # 爆仓 → 否决
        {'family': 'gearing', 'window': 'OOS', 'arm': 'gearing=2.9', 'calmar': 30.0,
         'ret': 0.1, 'mdd': 0.02, 'n_broke': 0, 'n_blown': 0},
        {'family': 'gearing', 'window': 'OOS', 'arm': 'BASE(现值)', 'calmar': 20.0,
         'ret': 0.08, 'mdd': 0.02, 'n_broke': 0, 'n_blown': 0},
    ])
    ranked = SW.rank_arms(df)
    assert bool(ranked[ranked['arm'] == 'gearing=4.4']['vetoed'].iloc[0])
    assert ranked.iloc[0]['arm'] == 'gearing=2.9', '爆仓臂不得排第一'


def test_rank_complete_only_excludes_partially_run_arms():
    """逐窗分次跑时，新臂在跑完最后一窗前不得参与排序——否则同一轮扩边的 4 次调用
    会因 CSV 增长而给出不同判定（跨窗不一致）。"""
    rows = []
    for w in ('OOS', 'W1'):
        rows.append({'family': 'stop', 'window': w, 'arm': 'sl=0.030', 'calmar': 40.0,
                     'ret': 0.06, 'mdd': 0.02, 'n_broke': 0, 'n_blown': 0})
    rows.append({'family': 'stop', 'window': 'OOS', 'arm': 'sl=0.020', 'calmar': 99.0,
                 'ret': 0.09, 'mdd': 0.01, 'n_broke': 0, 'n_blown': 0})   # 只跑了一窗
    df = pd.DataFrame(rows)
    ranked = SW.rank_arms(df, complete_only=True)
    assert list(ranked['arm']) == ['sl=0.030'], '半跑完的臂须被排除'
    assert 'sl=0.020' in set(SW.rank_arms(df)['arm']), 'complete_only=False 时不过滤'


def test_expand_subdivides_when_step_overshoots_hard_limit():
    """步长外推越限、但边界与硬限之间仍有空间 → 须细分该区间，不得静默跳过。

    funding 实证：赢家 0.0005、硬限 0.0001、步长 0.0005（最外两点间距）→ 一步跨到 0 以下
    被清空，0.0002/0.0003/0.0004 从未被测试。"""
    df = _res('funding', [('fr=0.0005', 26.4, 0.066), ('fr=0.0010', 23.0, 0.061),
                          ('BASE(现值)', 20.0, 0.050), ('fr=0.0025', 20.4, 0.058)])
    arms, note = SW.expand_arms('funding', df, n_new=3)
    vals = sorted(a.overrides['funding_stop'] for a in arms)
    assert vals == [0.0001, 0.0002, 0.0003, 0.0004], vals   # 硬限本身 + 区间细分点
    assert '细分' in note
    lo = SW.DIM_LIMITS['funding']['funding_stop'][0]
    assert all(v >= lo - 1e-12 for v in vals)


def test_expand_no_subdivision_when_edge_equals_limit():
    """边界已等于硬限 → 无空间可分，如实报「撞硬限」。"""
    df = _res('funding', [('fr=0.0001', 30.0, 0.07), ('fr=0.0005', 26.4, 0.066)])
    arms, note = SW.expand_arms('funding', df, n_new=3)
    assert arms == [] and '硬限' in note


def test_arms_missing_window_backfills_incomplete_arms():
    """逐窗分次跑 + 中途扩边 → CSV 里会出现「只跑了部分窗」的臂；必须能补跑。

    死锁实证（2026-07-15）：扩边臂在 OOS 生成并跑完，后续窗口的扩边判定发现它已在 CSV 里
    （seen 命中）→ 不再提议 → 而 complete_only 排名又把它排除（缺窗）→ 永远补不齐、成为死行。
    """
    rows = [{'family': 'pv', 'window': w, 'arm': 'BASE(现值)', 'calmar': 20.0, 'ret': 0.05,
             'mdd': 0.02, 'n_broke': 0, 'n_blown': 0} for w in ('OOS', 'W1', 'W2', 'IS')]
    rows.append({'family': 'pv', 'window': 'OOS', 'arm': 'mult=5', 'calmar': 50.0,
                 'ret': 0.09, 'mdd': 0.02, 'n_broke': 0, 'n_blown': 0})   # 只跑了 OOS
    df = pd.DataFrame(rows)
    assert [a.label for a in SW.arms_missing_window('pv', df, 'OOS')] == []
    for w in ('W1', 'W2', 'IS'):
        miss = SW.arms_missing_window('pv', df, w)
        assert [a.label for a in miss] == ['mult=5'], w
        assert miss[0].overrides == {'pv_mult': 5}, '补跑臂须还原出正确的覆盖项'


def test_arms_missing_window_recovers_off_arms():
    """OFF 类臂（无数值坐标）也要能从 build_arms 找回来补跑。"""
    rows = [{'family': 'pv', 'window': 'OOS', 'arm': 'pv=OFF', 'calmar': 5.0, 'ret': 0.01,
             'mdd': 0.03, 'n_broke': 0, 'n_blown': 0}]
    miss = SW.arms_missing_window('pv', pd.DataFrame(rows), 'W1')
    assert [a.label for a in miss] == ['pv=OFF']
    assert miss[0].overrides == {'active_stop_mode': 'none'}


# ---- Pass 2（坐标下降）：可覆盖基线 ----

def test_set_baseline_overrides_and_arms_are_relative_to_it():
    """Pass 2 把基线换成 Pass 1 各族冠军的组合；臂的覆盖项相对新基线。"""
    try:
        b = SW.set_baseline({'pv_mult': 5, 'band': 1.5, 'count_min': 20})
        assert b['pv_mult'] == 5 and b['band'] == 1.5 and b['count_min'] == 20
        assert b['stop_loss'] == SW.live_baseline()['stop_loss']    # 未覆盖项仍取实盘值
        arms = SW.build_arms('pv')
        assert arms[0].is_baseline()
        assert arms[0].params()['pv_mult'] == 5, 'BASE 臂须是新基线'
        labels = [a.label for a in arms]
        assert 'mult=5' not in labels, '与新基线重合的点须去重（它就是 BASE）'
        assert 'mult=3' in labels, '旧现值须作为一个可比的臂出现'
        m3 = [a for a in arms if a.label == 'mult=3'][0]
        assert m3.params()['band'] == 1.5, '其余维度须取新基线（这正是坐标下降的意义）'
        geom = SW.build_arms('geom')
        assert 'band=1.5,cmin=20' not in [a.label for a in geom]    # = 新基线 → 去重
    finally:
        SW.set_baseline({})


def test_set_baseline_rejects_unknown_dim():
    with pytest.raises(ValueError, match='未知基线维度'):
        SW.set_baseline({'no_such_knob': 1})
    assert SW.baseline() == SW.live_baseline()


def test_label_precision_is_lossless_no_collision():
    """臂标签是 CSV 合并主键——精度不足会让不同参数塌缩成同一标签、互相覆盖。

    实证(2026-07-15)：spacing 的 '%.2f' 把 0.005/0.00625/0.0075/0.00875 全印成 'sp_max=0.01'，
    四个臂互相覆盖、还盖掉真正的 0.01。守卫：候选量化后，标签解析回来须逐位一致，且不同量化
    值的标签互不相同。"""
    checks = {
        'spacing': ('spacing_max', [0.005, 0.00625, 0.0075, 0.00875, 0.01, 0.02, 0.04]),
        'funding': ('funding_stop', [0.0001, 0.0002, 0.0003, 0.0004, 0.0005]),
        'gearing': ('gearing', [2.4, 3.4, 4.4, 4.9, 5.4, 5.9]),
        'stop': ('stop_loss', [0.015, 0.02, 0.025, 0.03, 0.045]),
    }
    for fam, (dim, vals) in checks.items():
        labels = {}
        for v in vals:
            q = SW.quantize(dim, v)
            ov = {dim: q}
            lbl = SW._arm_label(fam, dict(SW.baseline(), **ov))
            back = SW.parse_coord(fam, lbl)[dim]
            assert abs(back - q) < 1e-9, '%s %s: 标签 %s 解析回 %s ≠ %s' % (fam, v, lbl, back, q)
            assert lbl not in labels or labels[lbl] == q, \
                '%s: 值 %s 与 %s 塌缩成同一标签 %s' % (fam, v, labels[lbl], lbl)
            labels[lbl] = q


def test_expand_candidates_are_quantized(monkeypatch):
    """扩边产出的臂，其覆盖值须已量化（= 标签能无损表达的值）。"""
    df = _res('spacing', [('sp_max=0.02', 25.5, 0.14), ('sp_max=0.03', 16.0, 0.11),
                          ('BASE(现值)', 20.0, 0.05)])
    # 人为把赢家做到下界以触发细分
    df = pd.concat([df, _res('spacing', [('sp_max=0.02', 25.5, 0.14)])], ignore_index=True)
    arms, _ = SW.expand_arms('spacing', df, n_new=3)
    for a in arms:
        v = a.overrides['spacing_max']
        assert SW.quantize('spacing_max', v) == v, '%s 未量化' % v
        # 标签解析回来一致
        assert abs(SW.parse_coord('spacing', a.label)['spacing_max'] - v) < 1e-12
