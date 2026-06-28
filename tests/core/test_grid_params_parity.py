import json
import os

_GOLDEN = os.path.join(os.path.dirname(__file__), '..', 'golden', 'grid_params_golden.json')
V2_CONFIG = {
    'atr_range_multiplier': 3, 'range_pct_min': 0.05, 'range_pct_max': 0.25,
    'grid_spacing_atr_ratio': 0.5, 'grid_spacing_min': 0.003, 'grid_spacing_max': 0.02,
    'grid_count_min': 25, 'grid_count_max': 149, 'stop_buffer_ratio': 0.01,
}
ROW = {'close': 123.45, 'Atr_5': 0.04, 'middle_5': 122.0}
KEYS = ['high_price', 'low_price', 'stop_high_price', 'stop_low_price', 'grid_count']


def test_grid_params_match_golden():
    from gridtrade.core.grid_params import calc_grid_params_v1, calc_grid_params_v2
    with open(_GOLDEN) as f:
        golden = json.load(f)
    v1 = calc_grid_params_v1(ROW, price_limit=[0.25, 0.25], stop_limit=0.01)
    v2 = calc_grid_params_v2(ROW, price_limit=[0.25, 0.25], stop_limit=0.01, v2_config=V2_CONFIG)
    for k in KEYS:
        assert abs(float(v1[k]) - float(golden['v1'][k])) < 1e-9, f'v1 {k}'
        assert abs(float(v2[k]) - float(golden['v2'][k])) < 1e-9, f'v2 {k}'


def test_format_price_no_scientific_notation():
    from gridtrade.core.grid_params import _format_price
    s = _format_price(0.000012345, 8)
    assert 'e' not in s and 'E' not in s          # 不能是科学计数法
    assert abs(float(s) - round(0.000012345, 8)) < 1e-12   # 数值正确
