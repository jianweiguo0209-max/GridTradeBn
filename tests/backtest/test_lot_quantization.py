"""回测下单量按真实 stepSize 向下截断,对齐实盘(2026-07-18)。

分歧:实盘 grid_executor 下单前走 adapter.quantize_amount → ccxt amount_to_precision,币安精度
模式下是 **TRUNCATE 向下**(wire_qty <= order_num 恒成立、永不向上);回测此前硬传 min_amount=0.0,
把引擎自带的同款截断(grid_order_info: order_num - order_num % min_amount)关掉了 → 用原始 float、
系统性高估下单量。

量级:cap=1000(回测恒用值)下缩量中位仅 ~0.01% —— 交易所把低价币配粗步长(59% 的币 step=1.0)
但那些币 order_num 本就大,高价币配细步长(BTC=0.001)。**真正的失真在 cap 本身**:回测恒用 1000,
实盘 cap=clamp(equity×0.2451, 20, 1e5),权益低时该项快速放大(cap=$24.5 时缩量中位 0.40%)。
"""
import pandas as pd
import pytest

from gridtrade.backtest import lot_sizes
from gridtrade.backtest.backtest_run import simulate_tasks

_SYM = 'X/USDT:USDT'
_T0 = pd.Timestamp('2026-01-01 10:00:00')
_GP = {'low_price': 100.0, 'high_price': 200.0, 'grid_count': 10,
       'stop_low_price': 90.0, 'stop_high_price': 210.0}


def _task():
    """entry=150,价格上穿数条线 → 有成交,盈亏随下单量线性缩放。"""
    bars = pd.DataFrame([
        {'candle_begin_time': _T0, 'open': 150.0, 'high': 152.0, 'low': 149.9, 'close': 152.0},
        {'candle_begin_time': _T0 + pd.Timedelta(minutes=1),
         'open': 152.0, 'high': 176.0, 'low': 152.0, 'close': 176.0},
        {'candle_begin_time': _T0 + pd.Timedelta(minutes=2),
         'open': 176.0, 'high': 176.1, 'low': 175.9, 'close': 176.0},
    ])
    return (_T0, 0, _SYM, 150.0, _GP, bars, None, None)


def _run(lot_by_sym):
    df = simulate_tasks([_task()], leverage=5.0, stop_cfg=None,
                        active_stop_mode='none', lot_by_sym=lot_by_sym)
    return float(df['pnl_ratio'].iloc[0])


def test_step_truncation_shrinks_position_vs_no_rounding():
    """step=1.0 且 order_num≈2.135 → 截断到 2.0(缩 ~6%) → |盈亏| 必须同比缩小。
    传 {} (缺表) 则退化为不取整 = 旧行为。"""
    raw = _run({})                       # 不取整(旧行为)
    quantized = _run({_SYM: 1.0})        # 按 step=1.0 向下截断
    assert quantized != raw, 'stepSize 没被送进引擎 —— 量化接线断了'
    assert abs(quantized) < abs(raw), '截断后仓位更小,|盈亏| 必须更小'


def test_truncation_never_rounds_up():
    """TRUNCATE 语义:永远向下,绝不向上(实盘 ccxt amount_to_precision 即如此)。
    order_num≈2.135、step=0.5 → 2.0;若实现成四舍五入会得 2.0(同值),故用 2.6→step 0.5 验:
    这里直接查引擎侧不变量更稳。"""
    from gridtrade.core.grid_engine import grid_order_info
    raw = grid_order_info(1000.0, 5.0, 100.0, 200.0, 10, 90.0, 210.0, min_amount=0.0)
    for step in (0.5, 1.0, 0.01):
        q = grid_order_info(1000.0, 5.0, 100.0, 200.0, 10, 90.0, 210.0, min_amount=step)
        assert q['每笔数量'] <= raw['每笔数量'] + 1e-12, 'step=%s 时向上取整了' % step
        # 必须是 step 的整数倍
        assert abs(q['每笔数量'] / step - round(q['每笔数量'] / step)) < 1e-9


def test_missing_symbol_falls_back_to_no_rounding():
    """缺该币 → 退化为不取整(fail-soft,不得抛)。"""
    assert _run({'OTHER/USDT:USDT': 1.0}) == pytest.approx(_run({}), rel=1e-12)


def test_lot_sizes_cache_roundtrip(tmp_path):
    root = str(tmp_path)
    lot_sizes.save(root, {'A/USDT:USDT': 1.0, 'B/USDT:USDT': 0.001})
    assert lot_sizes.load(root) == {'A/USDT:USDT': 1.0, 'B/USDT:USDT': 0.001}


def test_lot_sizes_load_missing_is_fail_soft(tmp_path):
    """缺文件 → {} 而非抛(回测不得因元数据缺失而挂)。"""
    assert lot_sizes.load(str(tmp_path)) == {}


def test_fetch_from_adapter_keeps_only_positive_steps():
    """step<=0 = 交易所未给/未知 → 剔除(否则会把 0 当步长、取整成 0)。"""
    class _Ins:
        def __init__(self, symbol, lot):
            self.symbol, self.lot = symbol, lot

    class _Adp:
        def list_instruments(self):
            return [_Ins('A/USDT:USDT', 1.0), _Ins('B/USDT:USDT', 0.0), _Ins('C/USDT:USDT', None)]

    assert lot_sizes.fetch_from_adapter(_Adp()) == {'A/USDT:USDT': 1.0}
