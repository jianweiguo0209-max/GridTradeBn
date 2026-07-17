"""真中性(生产默认)首笔成交不该被丢弃 —— 回测/实盘对不上的根因(2026-07-18)。

来龙去脉:
- `5e273ee`(06-28) 从 legacy 迁引擎时带进 get_trade_info 的「首触落在离入场价最近的线上就丢弃」
  规则。该规则与「做多式底仓」(neutral_init=True:开网即在 entry 预置多头)本是一对——底仓已在
  entry 计入,最近线首触会重复计,故丢弃。
- `ab14d22`(07-02) 把 simulate_grid_engine 默认改成 neutral_init=False(纯中性),底仓注入随之关闭,
  但丢弃规则**没跟着关**,在生产路径上空转开火。
- 实盘 grid_executor 逐线挂真限价单(最近线照挂,只跳过恰好 ==entry 的线),该笔是真成交真 PnL。
- 金标 parity 测试显式传 neutral_init=True,恰好绕开生产路径 → bug 长期无覆盖。

同源系统(GP)实证:73% 的格子首笔成交就落在这条线上被引擎丢掉。
"""
import numpy as np
import pandas as pd
import pytest

from gridtrade.core.grid_engine import (get_trade_info, grid_order_info,
                                        grid_touch_info, simulate_grid_engine,
                                        trans_candle_to_tick)

_GP = {'low_price': 100.0, 'high_price': 200.0, 'grid_count': 10,
       'stop_low_price': 90.0, 'stop_high_price': 210.0}
_ENTRY = 150.0
_CLOSEST = 151.57165665      # 价格序列中离 entry=150 最近的线(上方 1.57;下方 141.42 距 8.58)


def _gi():
    return grid_order_info(1000.0, 5.0, 100.0, 200.0, 10, 90.0, 210.0)


def _bars():
    """entry=150 开网,价格上穿最近线 151.57 —— 实盘该线挂 sell 限价单,必成交一笔。"""
    t0 = pd.Timestamp('2026-01-01 00:00:00')
    return pd.DataFrame([
        {'candle_begin_time': t0, 'open': 150.0, 'high': 152.0, 'low': 149.9, 'close': 152.0},
        {'candle_begin_time': t0 + pd.Timedelta(minutes=1),
         'open': 152.0, 'high': 152.0, 'low': 151.9, 'close': 152.0},
    ])


def _touch_df(gi):
    tick_df, _ = trans_candle_to_tick(_bars(), gi)
    return grid_touch_info(tick_df, gi)


def test_closest_line_is_where_live_places_a_real_order():
    """前提①:最近线在 entry 上方 → 实盘 grid_executor 在该线挂 sell(p>entry),不是被跳过的
    p==entry 那条。故其成交是真单真 PnL。"""
    pa = _gi()['价格序列']
    closest = pa[np.argmin(abs(pa - _ENTRY))]
    assert float(closest) == pytest.approx(_CLOSEST)
    assert closest > _ENTRY          # → 实盘 side='sell',非 `else: continue` 的跳过分支


def test_the_crossing_is_real_not_a_t0_artifact():
    """前提②:该触网是两 tick 间的真穿越(149.9→152 上穿 151.57),不是 t=0 幻影。
    grid_touch_info 用 .shift() 判穿越,首 tick 的 shift=NaN 恒不触网 → 丢弃规则并非在补幻影。"""
    td = _touch_df(_gi())
    assert len(td) == 1
    assert td['touch'].iloc[0][0] == pytest.approx(_CLOSEST)
    assert float(td['tick_price'].iloc[0]) == 152.0      # 穿越发生在 149.9→152 这一跳


def test_true_neutral_keeps_first_fill_on_closest_line():
    """真中性:最近线首笔穿越必须留下(实盘该线有真单会成交)。"""
    gi = _gi()
    td = get_trade_info(_touch_df(gi), _ENTRY, gi, drop_first_closest=False)
    assert len(td) == 1, '真中性下最近线首笔成交被丢弃了'
    assert float(td['touch'].iloc[0]) == pytest.approx(_CLOSEST)
    assert float(td['last_touch'].iloc[0]) == pytest.approx(_ENTRY)
    assert float(td['order_dir'].iloc[0]) == -1.0        # 上穿转空,与实盘该线 sell 单一致


def test_long_biased_init_still_drops_first_closest_touch():
    """做多式底仓(legacy 语义)下丢弃规则必须保留 —— 金标 parity 依赖它,零漂移。"""
    gi = _gi()
    assert get_trade_info(_touch_df(gi), _ENTRY, gi, drop_first_closest=True).empty


def test_get_trade_info_default_preserves_legacy_call():
    """legacy/backtest/grid_engine.py 以 3 参调用 → 默认必须维持旧语义(丢弃)。"""
    gi = _gi()
    assert get_trade_info(_touch_df(gi), _ENTRY, gi).empty


def test_simulate_true_neutral_reports_trade_not_untouched():
    """生产入口:真中性下上穿最近线,应记成交,而非'未触网'。"""
    res = simulate_grid_engine(_bars(), _GP, cap=1000.0, leverage=5.0,
                               stop_cfg=None, neutral_init=False)
    assert res['n_trades'] >= 1, '真中性下首笔成交丢失 → 引擎误报未触网'
    assert res['exit_reason'] != '未触网'
