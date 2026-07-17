"""真中性（生产路径 neutral_init=False）金标 —— 堵住 07-02 改造留下的测试盲区。

背景：test_grid_engine_parity.py 的 legacy 金标显式传 neutral_init=True（做多式底仓），
生产实际走的 neutral_init=False **此前零覆盖** —— 「首触丢弃」bug 正是活在这个盲区里
（详见 test_grid_engine_first_fill.py）。

**本金标的性质与 legacy 金标不同**：legacy 金标是独立真值；本金标由**修复后的 core 引擎自身**
生成（legacy 带同款 bug、不能当真中性的真值源，见 gen_grid_engine_neutral_golden.py），
故只是**漂移哨兵**——正确性锚点在 test_grid_engine_first_fill.py 的语义测试。
数值若需变更，跑 tests/golden/gen_grid_engine_neutral_golden.py 重生成，并在 PR 里说明为何该变。
"""
import json
import os

from tests.golden.gen_grid_engine_golden import make_1m_bars

_HERE = os.path.dirname(__file__)
_NEUTRAL = os.path.join(_HERE, '..', 'golden', 'grid_engine_neutral_golden.json')
_LEGACY = os.path.join(_HERE, '..', 'golden', 'grid_engine_golden.json')

_GRID_PARAMS = {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 40,
                'stop_low_price': 88.0, 'stop_high_price': 112.0}
_STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618,
             'fundingRate_stop_loss': 0.0015}


def _load(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _run(neutral_init):
    from gridtrade.core.grid_engine import simulate_grid_engine
    return simulate_grid_engine(make_1m_bars(), _GRID_PARAMS, cap=1000.0, leverage=5.0,
                                stop_cfg=_STOP_CFG, neutral_init=neutral_init)


def test_simulate_true_neutral_matches_neutral_golden():
    """生产路径零漂移。"""
    g = _load(_NEUTRAL)['simulate_neutral']
    res = _run(neutral_init=False)
    assert abs(res['pnl_ratio'] - g['pnl_ratio']) < 1e-9
    assert abs(res['net_value_final'] - g['net_value_final']) < 1e-9
    assert res['exit_reason'] == g['exit_reason']
    assert int(res['n_trades']) == g['n_trades']
    assert bool(res['broke']) == g['broke']
    assert bool(res['terminated']) == g['terminated']
    assert bool(res['blown_up']) == g['blown_up']


def test_neutral_path_genuinely_diverges_from_long_biased_golden():
    """两条路径必须实质不同 —— 否则本金标形同虚设（说明 neutral_init 没真正生效）。
    做多式会在 entry 预置 grids_above 笔多头，成交数显著多于纯中性。"""
    neutral = _load(_NEUTRAL)['simulate_neutral']
    legacy = _load(_LEGACY)['simulate']
    assert neutral['n_trades'] < legacy['n_trades']       # 18 < 36：无底仓注入
    assert neutral['pnl_ratio'] != legacy['pnl_ratio']
