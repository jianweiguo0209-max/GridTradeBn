"""一次性脚本：生成**真中性（生产路径 neutral_init=False）**网格引擎金标。
运行：TZ=Asia/Shanghai .venv/bin/python tests/golden/gen_grid_engine_neutral_golden.py

与 gen_grid_engine_golden.py 的本质区别 —— 金标源不同，性质也不同：
- grid_engine_golden.json 由 **legacy 引擎**生成，是**独立真值**，校验「做多式底仓」迁移零漂移。
- 本文件由**修复后的 core 引擎自身**生成，是**特征化(characterization)金标**：只防未来重构
  静默漂移，不冒充独立真值。

为什么真中性不能用 legacy 当金标源：legacy(legacy/backtest/grid_engine.py) 默认 neutral_init=True，
且无条件调 get_trade_info(touch_df, entry, gi) —— 它**自己就带同款「首触丢弃」bug**。拿它跑
neutral_init=False 只会把 bug 固化成「真值」。故生产路径的正确性锚点是
tests/core/test_grid_engine_first_fill.py 的语义测试，本金标只做漂移哨兵。
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..'))
if _ROOT not in sys.path:                      # 直接 python 跑此脚本时项目根不在 path
    sys.path.insert(0, _ROOT)

from tests.golden.gen_grid_engine_golden import make_1m_bars   # noqa: E402


def main():
    from gridtrade.core.grid_engine import simulate_grid_engine   # 修复后的 core 引擎

    bars = make_1m_bars()
    grid_params = {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 40,
                   'stop_low_price': 88.0, 'stop_high_price': 112.0}
    stop_cfg = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618,
                'fundingRate_stop_loss': 0.0015}
    res = simulate_grid_engine(bars, grid_params, cap=1000.0, leverage=5.0,
                               stop_cfg=stop_cfg, neutral_init=False)
    out = {'simulate_neutral': {
        'pnl_ratio': float(res['pnl_ratio']),
        'net_value_final': float(res['net_value_final']),
        'terminated': bool(res['terminated']),
        'exit_reason': res['exit_reason'],
        'blown_up': bool(res['blown_up']),
        'n_trades': int(res['n_trades']),
        'broke': bool(res['broke']),
    }}
    with open(os.path.join(_HERE, 'grid_engine_neutral_golden.json'), 'w') as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print('neutral golden written; n_trades=%d exit=%s pnl=%.9f'
          % (out['simulate_neutral']['n_trades'], out['simulate_neutral']['exit_reason'],
             out['simulate_neutral']['pnl_ratio']))


if __name__ == '__main__':
    main()
