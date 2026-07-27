import numpy as np
import pandas as pd

from gridtrade.config import DEFAULT_STRATEGY_CONFIG
from gridtrade.execution.triggers import build_eff1_select_fn


class FeedStub:
    def __init__(self, lab):
        self._lab = lab

    def labels(self, run_time):
        return self._lab


def _candles(syms, rt, n=200):
    t = pd.date_range(rt - pd.Timedelta(hours=n), periods=n, freq='1h')
    out = {}
    for s in syms:
        out[s] = pd.DataFrame({'candle_begin_time': t, 'symbol': s,
                               'open': 100.0, 'high': 101.0, 'low': 99.0,
                               'close': 100.0, 'vol': 5.0, 'volCcy': 500.0,
                               'quote_volume': 500.0})
    return out


def test_eff1_ranks_by_eff_desc_symbol_tiebreak_and_truncates():
    rt = pd.Timestamp('2026-07-27 03:00:00')
    lab = {'AAA/USDT': dict(p12_cross1=8.0, p12_mae=0.02, p12_eff=8.0 / 3.0),
           'BBB/USDT': dict(p12_cross1=9.0, p12_mae=0.01, p12_eff=4.5),
           'CCC/USDT': dict(p12_cross1=9.0, p12_mae=0.01, p12_eff=4.5)}
    fn = build_eff1_select_fn(DEFAULT_STRATEGY_CONFIG, FeedStub(lab))
    out = fn(_candles(['AAA/USDT', 'BBB/USDT', 'CCC/USDT', 'NOLAB/USDT'], rt), rt, 3)
    # fixture 修正(brief 原断言 len(out)==1 与真实行为不符,文档见 task-7-report.md):
    # n=200 根 1h K 线过 proceed_calc_symbol_factor 的真实 resample 会收出多个已收盘
    # 12H 周期(与回测 eff1_scan.make_picks 对整窗按 rt 分组排名批量返回同口径、也与
    # _default_select_fn 走 select_grid_coin 的既有行为一致)——select_fn 直调(绕开
    # ScheduledSelectionTrigger.propose 的 `(time+period)>=run_time` 新鲜度过滤)天然
    # 按“每个历史周期各截断 choose_symbols 个”返回,不是全表只出 choose_symbols 行。
    # 这里改为按 time 分组校验截断/排名不变式,不依赖具体周期数。
    assert len(out) >= 1
    assert (out.groupby('time').size() == DEFAULT_STRATEGY_CONFIG['choose_symbols']).all()
    assert set(out['symbol']) == {'BBB/USDT'}       # eff 同分 ⇒ symbol 升序,B 在 C 前
    assert 'NOLAB/USDT' not in set(out['symbol'])   # 缺标签不参选
    assert {'p12_eff', 'p12_cross1', 'p12_mae', 'rank'} <= set(out.columns)


def test_eff1_warns_when_tick_table_empty_but_selection_unaffected():
    """Important #2:tick_map_fn() 返回空表时,MIN_TICKS 过滤静默失效是 eff1 回测有效性
    前提(3.9x 虚高)被关闭——必须每轮 WARN 一次;同时选币行为不变(空表 ⇒ 不剔)。"""
    rt = pd.Timestamp('2026-07-27 03:00:00')
    lab = {'TOP/USDT': dict(p12_cross1=9.0, p12_mae=0.01, p12_eff=4.5),
           'SECOND/USDT': dict(p12_cross1=5.0, p12_mae=0.01, p12_eff=2.5)}
    logs = []
    fn = build_eff1_select_fn(DEFAULT_STRATEGY_CONFIG, FeedStub(lab),
                              tick_map_fn=lambda: {}, min_ticks=3.0,
                              log=lambda msg: logs.append(msg))
    out = fn(_candles(['TOP/USDT', 'SECOND/USDT'], rt), rt, 3)
    assert len(out) >= 1
    assert set(out['symbol']) == {'TOP/USDT'}       # 空表未过滤 ⇒ 排名不变,TOP 照常胜出
    assert any('WARN' in m for m in logs)


def test_eff1_tick_filter_promotes_next():
    rt = pd.Timestamp('2026-07-27 03:00:00')
    lab = {'TOP/USDT': dict(p12_cross1=9.0, p12_mae=0.01, p12_eff=4.5),
           'SECOND/USDT': dict(p12_cross1=5.0, p12_mae=0.01, p12_eff=2.5)}
    fn = build_eff1_select_fn(DEFAULT_STRATEGY_CONFIG, FeedStub(lab),
                              tick_map_fn=lambda: {'TOP/USDT': 50.0},  # 粗 tick ⇒ 剔
                              min_ticks=3.0)
    out = fn(_candles(['TOP/USDT', 'SECOND/USDT'], rt), rt, 3)
    # 同上:多历史周期各自递补到 SECOND/USDT,不假设总行数恰为 1。
    assert len(out) >= 1
    assert set(out['symbol']) == {'SECOND/USDT'}    # 递补
    assert (out.groupby('time').size() == DEFAULT_STRATEGY_CONFIG['choose_symbols']).all()
