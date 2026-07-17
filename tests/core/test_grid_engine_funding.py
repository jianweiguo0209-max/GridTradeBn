"""资金费只按时刻收一次,不随同刻成交笔数按阶梯多计(2026-07-18)。

bug 机理:
- `get_trade_info` 让同一 tick 跨 N 线的 N 笔成交**共用 tick 时间戳**(time_list = [ct]*touch_times)。
- tick 时间戳 = bar 时间 + {0,15,30,45}s → **只有 p1(开盘,+0s)能撞上整点资金费时刻**(00/08/16 UTC)。
- `cal_equity_curve` 的 outer merge 后,该时刻有 K 行(candle × K 笔 p1 成交);
  `fr_fee = hold_num*close*fundingRate` **逐行各算一次**,而 hold_num 是逐笔递增的阶梯;
  `expanding().sum()` 把整个阶梯加起来 → 收 h1+h2+…+hK 而非只收 hK。
- 触发条件:资金费时刻那根 bar 的**开盘跳空跨 ≥2 条线**(小市值币常见)。多计倍数 = (K+1)/2。

fr_fee 是**存量**(按仓位)量,算在**流量**(按笔)的行网格上 —— 这是口径错配,不是舍入。
K=0/1 时引擎本就正确(单行、按成交后仓位收一次),故修复保持该约定,只让 K≥2 也只收一次。
"""
import pandas as pd
import pytest

from gridtrade.core.grid_engine import (cal_equity_curve, get_trade_info,
                                        grid_order_info, grid_touch_info,
                                        trans_candle_to_tick)

_FUND_TS = pd.Timestamp('2026-01-01 16:00:00')      # 整点资金费时刻(UTC)
_RATE = 0.01
_ENTRY = 150.0


def _gi():
    return grid_order_info(1000.0, 5.0, 100.0, 200.0, 10, 90.0, 210.0)


def _funding_df():
    return pd.DataFrame([{'ts': int(_FUND_TS.value // 1_000_000), 'fundingRate': _RATE}])


def _run(open_at_fund_ts):
    """bar0 停在 entry;bar1(资金费时刻)按 open_at_fund_ts 跳空;bar2 持平。"""
    gi = _gi()
    bars = pd.DataFrame([
        {'candle_begin_time': pd.Timestamp('2026-01-01 15:59:00'),
         'open': _ENTRY, 'high': 150.1, 'low': 149.9, 'close': _ENTRY},
        {'candle_begin_time': _FUND_TS, 'open': open_at_fund_ts,
         'high': open_at_fund_ts + 0.2, 'low': open_at_fund_ts - 0.1, 'close': open_at_fund_ts},
        {'candle_begin_time': pd.Timestamp('2026-01-01 16:01:00'), 'open': open_at_fund_ts,
         'high': open_at_fund_ts + 0.1, 'low': open_at_fund_ts - 0.1, 'close': open_at_fund_ts},
    ])
    tick_df, _ = trans_candle_to_tick(bars, gi)
    td = get_trade_info(grid_touch_info(tick_df, gi), _ENTRY, gi, drop_first_closest=False)
    cd = bars[['candle_begin_time', 'open', 'close']].copy()
    cd['symbol'] = 'X'
    out = cal_equity_curve(cd, td, 0.0002, 1000.0, 0.0005, _funding_df())
    at = out[out['candle_begin_time'] == _FUND_TS]
    return out, at


def _honest(at, px):
    """诚实值:只按该时刻**终仓**收一次(与 K=1 时引擎既有约定一致)。"""
    return float(at['hold_num'].iloc[-1]) * px * _RATE


def test_gap_across_two_lines_charges_funding_once_not_staircase():
    """开盘跳空跨 2 条线 → 该时刻 2 行,资金费不得按 h1+h2 阶梯收。"""
    out, at = _run(165.0)                         # 150→165 跨 151.57 与 162.45 两条线
    assert len(at) == 2, '前提:该时刻应有 2 行(K=2),否则本测试没测到东西'
    assert float(at['fundingRate'].iloc[0]) == pytest.approx(_RATE)
    assert float(out['fr_fee'].iloc[-1]) == pytest.approx(_honest(at, 165.0), rel=1e-9)


def test_gap_across_three_lines_charges_funding_once():
    """跨 3 条线 → 阶梯多计倍数本应是 (3+1)/2=2×,修复后须为 1×。"""
    out, at = _run(178.0)                         # 150→178 跨 151.57/162.45/174.11 三条线
    assert len(at) == 3, '前提:该时刻应有 3 行(K=3)'
    assert float(out['fr_fee'].iloc[-1]) == pytest.approx(_honest(at, 178.0), rel=1e-9)


def test_single_line_cross_unchanged():
    """K=1:引擎本就正确(按成交后仓位收一次),修复不得改变它。"""
    out, at = _run(155.0)                         # 150→155 只跨 151.57 一条线
    assert len(at) == 1, '前提:该时刻应只有 1 行(K=1)'
    assert float(out['fr_fee'].iloc[-1]) == pytest.approx(_honest(at, 155.0), rel=1e-9)


def test_no_trade_at_funding_ts_unchanged():
    """K=0:资金费时刻无成交,按 ffill 的持仓收一次 —— 修复不得改变它。"""
    gi = _gi()
    bars = pd.DataFrame([
        {'candle_begin_time': pd.Timestamp('2026-01-01 15:59:00'),
         'open': _ENTRY, 'high': 155.5, 'low': 149.9, 'close': 155.0},   # 前一根就跨了线
        {'candle_begin_time': _FUND_TS, 'open': 155.0, 'high': 155.1, 'low': 154.9, 'close': 155.0},
        {'candle_begin_time': pd.Timestamp('2026-01-01 16:01:00'),
         'open': 155.0, 'high': 155.1, 'low': 154.9, 'close': 155.0},
    ])
    tick_df, _ = trans_candle_to_tick(bars, gi)
    td = get_trade_info(grid_touch_info(tick_df, gi), _ENTRY, gi, drop_first_closest=False)
    cd = bars[['candle_begin_time', 'open', 'close']].copy()
    cd['symbol'] = 'X'
    out = cal_equity_curve(cd, td, 0.0002, 1000.0, 0.0005, _funding_df())
    at = out[out['candle_begin_time'] == _FUND_TS]
    assert len(at) == 1 and float(at['hold_num'].iloc[0]) != 0.0, '前提:K=0 且持有仓位'
    assert float(out['fr_fee'].iloc[-1]) == pytest.approx(_honest(at, 155.0), rel=1e-9)
