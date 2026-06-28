"""一次性脚本：用原始 backtest/grid_engine.py 生成网格引擎金标。
运行：TZ=Asia/Shanghai .venv/bin/python tests/golden/gen_grid_engine_golden.py
重构后由 parity 测试用相同输入比对 core 引擎输出（零漂移）。
"""
import json
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..'))
_BT = os.path.join(_ROOT, 'backtest')
if _BT not in sys.path:
    sys.path.insert(0, _BT)


def make_1m_bars(n=600, seed=7, start=100.0):
    """确定性合成 1m OHLCV（含 quote_volume/symbol），用于网格引擎仿真。"""
    rng = np.random.RandomState(seed)
    rets = rng.normal(0, 0.0008, size=n)
    close = start * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0005, size=n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0005, size=n)))
    qv = rng.uniform(1e5, 1e6, size=n)
    t = pd.date_range('2024-03-01', periods=n, freq='1min')
    return pd.DataFrame({
        'candle_begin_time': t, 'open': open_, 'high': high, 'low': low,
        'close': close, 'quote_volume': qv, 'symbol': 'BTC/USDT:USDT',
    })


def main():
    from grid_engine import grid_order_info, simulate_grid_engine  # 原始 backtest 实现

    gi = grid_order_info(1000.0, 5.0, 90.0, 110.0, 40, 88.0, 112.0)
    gi_out = {
        'price_array': [float(x) for x in gi['价格序列']],
        'order_num': float(gi['每笔数量']),
        'stop_low': float(gi['终止最低价']),
        'stop_high': float(gi['终止最高价']),
    }

    bars = make_1m_bars()
    grid_params = {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 40,
                   'stop_low_price': 88.0, 'stop_high_price': 112.0}
    stop_cfg = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618,
                'fundingRate_stop_loss': 0.0015}
    res = simulate_grid_engine(bars, grid_params, cap=1000.0, leverage=5.0,
                               stop_cfg=stop_cfg)
    sim_out = {
        'pnl_ratio': float(res['pnl_ratio']),
        'net_value_final': float(res['net_value_final']),
        'terminated': bool(res['terminated']),
        'exit_reason': res['exit_reason'],
        'blown_up': bool(res['blown_up']),
        'n_trades': int(res['n_trades']),
        'broke': bool(res['broke']),
    }

    out = {'grid_order_info': gi_out, 'simulate': sim_out}
    with open(os.path.join(_HERE, 'grid_engine_golden.json'), 'w') as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print('grid_engine golden written; n_trades=%d exit=%s pnl=%.6f'
          % (sim_out['n_trades'], sim_out['exit_reason'], sim_out['pnl_ratio']))


if __name__ == '__main__':
    main()
