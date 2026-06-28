import json
import os

import numpy as np

from tests.golden.gen_grid_engine_golden import make_1m_bars

_GOLDEN = os.path.join(os.path.dirname(__file__), '..', 'golden', 'grid_engine_golden.json')


def _golden():
    with open(_GOLDEN, encoding='utf-8') as f:
        return json.load(f)


def test_grid_order_info_matches_golden():
    from gridtrade.core.grid_engine import grid_order_info
    g = _golden()['grid_order_info']
    gi = grid_order_info(1000.0, 5.0, 90.0, 110.0, 40, 88.0, 112.0)
    np.testing.assert_allclose([float(x) for x in gi['价格序列']],
                               g['price_array'], rtol=1e-9, atol=1e-12)
    assert abs(float(gi['每笔数量']) - g['order_num']) < 1e-9
    assert float(gi['终止最低价']) == g['stop_low']
    assert float(gi['终止最高价']) == g['stop_high']


def test_simulate_grid_engine_matches_golden():
    from gridtrade.core.grid_engine import simulate_grid_engine
    g = _golden()['simulate']
    bars = make_1m_bars()
    grid_params = {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 40,
                   'stop_low_price': 88.0, 'stop_high_price': 112.0}
    stop_cfg = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618,
                'fundingRate_stop_loss': 0.0015}
    res = simulate_grid_engine(bars, grid_params, cap=1000.0, leverage=5.0, stop_cfg=stop_cfg)
    assert abs(res['pnl_ratio'] - g['pnl_ratio']) < 1e-9
    assert abs(res['net_value_final'] - g['net_value_final']) < 1e-9
    assert res['exit_reason'] == g['exit_reason']
    assert int(res['n_trades']) == g['n_trades']
    assert bool(res['broke']) == g['broke']
    assert bool(res['terminated']) == g['terminated']
    assert bool(res['blown_up']) == g['blown_up']
