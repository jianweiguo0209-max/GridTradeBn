import pandas as pd
import pytest

from gridtrade.execution.gates import GridProposal
from gridtrade.execution.triggers import (TriggerContext, TriggerCondition,
                                          TriggerEngine)


def _ctx(**kw):
    base = dict(exchange='okx', run_time=pd.Timestamp('2025-06-24 14:00:00'))
    base.update(kw)
    return TriggerContext(**base)


def _prop(symbol, source):
    return GridProposal(exchange='okx', symbol=symbol,
                        grid_params={'low_price': 1.0, 'high_price': 2.0,
                                     'grid_count': 5, 'stop_low_price': 0.5,
                                     'stop_high_price': 2.5},
                        source=source)


class _FixedTrigger(TriggerCondition):
    def __init__(self, props):
        self._props = props
    def propose(self, ctx):
        return list(self._props)


def test_engine_concatenates_all_trigger_proposals():
    t1 = _FixedTrigger([_prop('BTC/USDT:USDT', 't1')])
    t2 = _FixedTrigger([_prop('ETH/USDT:USDT', 't2'),
                        _prop('SOL/USDT:USDT', 't2')])
    engine = TriggerEngine([t1, t2])
    out = engine.collect(_ctx())
    assert [p.symbol for p in out] == ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']
    assert [p.source for p in out] == ['t1', 't2', 't2']


def test_engine_empty_triggers_returns_empty():
    assert TriggerEngine([]).collect(_ctx()) == []


def test_engine_passes_same_context_to_each_trigger():
    seen = []
    class _Spy(TriggerCondition):
        def propose(self, ctx):
            seen.append(ctx)
            return []
    ctx = _ctx()
    TriggerEngine([_Spy(), _Spy()]).collect(ctx)
    assert seen == [ctx, ctx]


def _strategy_config(**kw):
    base = dict(period='12H', strategy_tag='acc1at', choose_symbols=3,
                price_limit=[0.1, 0.1], stop_limit=0.05, leverage=3,
                grid_version=1, grid_v2_config={}, weight_list=[1, 1],
                max_candle_num=100)
    base.update(kw)
    return base


def _factor_row(symbol, rank, run_time, close=100.0, atr_5=0.02, middle_5=100.0):
    return {'symbol': symbol, 'rank': rank, 'time': run_time,
            'close': close, 'Atr_5': atr_5, 'middle_5': middle_5}


def test_scheduled_trigger_maps_selection_rows_to_proposals_v1():
    from gridtrade.execution.triggers import ScheduledSelectionTrigger
    run_time = pd.Timestamp('2025-06-24 14:00:00')
    rows = pd.DataFrame([_factor_row('BTC/USDT:USDT', 1, run_time),
                         _factor_row('ETH/USDT:USDT', 2, run_time, close=200.0)])
    trig = ScheduledSelectionTrigger(_strategy_config(), {'Reg_v2_2': True, 'Sgcz_2': True},
                                     [1, 1],
                                     select_fn=lambda scd, rt, off: rows)
    out = trig.propose(TriggerContext(exchange='okx', run_time=run_time,
                                      symbol_candle_data={'BTC/USDT:USDT': None}))
    assert [p.symbol for p in out] == ['BTC/USDT:USDT', 'ETH/USDT:USDT']
    # raw-float 网格几何（close=100, atr=0.02, middle=100, price_limit=0.1, stop=0.05）
    btc = out[0].grid_params
    assert btc['high_price'] == pytest.approx(106.0)
    assert btc['low_price'] == pytest.approx(94.0)
    assert btc['stop_high_price'] == pytest.approx(115.5)
    assert btc['stop_low_price'] == pytest.approx(85.5)
    assert btc['grid_count'] == 9
    # 提议元数据：source / tag / exchange / offset
    assert out[0].source == 'ScheduledSelectionTrigger'
    assert out[0].exchange == 'okx'
    from gridtrade.core.selection import compute_offset
    off = compute_offset(run_time, '12H')
    assert out[0].offset == off and out[0].tag == 'acc1at%d' % off


def test_scheduled_trigger_stashes_ranked_on_ctx():
    """record-and-replay(2026-07-17):propose 把排名 picks(符号+因子值+名次)写回
    ctx.selection_ranked/offset,供 scheduler 落 selection_snapshots。fail-soft。"""
    from gridtrade.execution.triggers import ScheduledSelectionTrigger
    from gridtrade.core.selection import compute_offset
    run_time = pd.Timestamp('2025-06-24 14:00:00')

    def _row(sym, rank, reg, sgcz):
        r = _factor_row(sym, rank, run_time)
        r.update({'Reg_v2_2': reg, 'Sgcz_2': sgcz, 'rank_sum': float(rank * 2)})
        return r
    rows = pd.DataFrame([_row('BTC/USDT:USDT', 1, 0.5, -0.03),
                         _row('ETH/USDT:USDT', 2, 0.2, -0.05)])
    trig = ScheduledSelectionTrigger(_strategy_config(), {'Reg_v2_2': True, 'Sgcz_2': True},
                                     [1, 1], select_fn=lambda scd, rt, off: rows)
    ctx = TriggerContext(exchange='okx', run_time=run_time,
                         symbol_candle_data={'BTC/USDT:USDT': None})
    trig.propose(ctx)
    assert ctx.selection_offset == compute_offset(run_time, '12H')
    assert ctx.selection_ranked is not None and len(ctx.selection_ranked) == 2
    top = ctx.selection_ranked[0]
    assert top['symbol'] == 'BTC/USDT:USDT' and top['rank'] == 1 and top['rank_sum'] == 2.0
    assert top['factors']['Reg_v2_2'] == 0.5 and top['factors']['Sgcz_2'] == -0.03


def test_scheduled_trigger_empty_selection_returns_empty():
    from gridtrade.execution.triggers import ScheduledSelectionTrigger
    run_time = pd.Timestamp('2025-06-24 14:00:00')
    trig = ScheduledSelectionTrigger(_strategy_config(), {'Reg_v2_2': True}, [1],
                                     select_fn=lambda scd, rt, off: pd.DataFrame())
    out = trig.propose(TriggerContext(exchange='okx', run_time=run_time))
    assert out == []


def test_scheduled_trigger_v2_uses_v2_params():
    from gridtrade.execution.triggers import ScheduledSelectionTrigger
    from gridtrade.core.grid_params import calc_grid_params_v2
    run_time = pd.Timestamp('2025-06-24 14:00:00')
    v2cfg = {'atr_range_multiplier': 2.0, 'range_pct_min': 0.01, 'range_pct_max': 0.2,
             'grid_spacing_atr_ratio': 0.5, 'grid_spacing_min': 0.005,
             'grid_spacing_max': 0.05, 'grid_count_min': 5, 'grid_count_max': 100,
             'stop_buffer_ratio': 0.1}
    cfg = _strategy_config(grid_version=2, grid_v2_config=v2cfg)
    row = _factor_row('BTC/USDT:USDT', 1, run_time)
    rows = pd.DataFrame([row])
    trig = ScheduledSelectionTrigger(cfg, {'Reg_v2_2': True}, [1],
                                     select_fn=lambda scd, rt, off: rows)
    out = trig.propose(TriggerContext(exchange='okx', run_time=run_time))
    expected = calc_grid_params_v2(row=row, price_limit=[0.1, 0.1], stop_limit=0.05,
                                   v2_config=v2cfg)
    assert out[0].grid_params == expected


def test_scheduled_trigger_sorts_by_rank():
    from gridtrade.execution.triggers import ScheduledSelectionTrigger
    run_time = pd.Timestamp('2025-06-24 14:00:00')
    # 乱序 rank，断言提议按 rank 升序
    rows = pd.DataFrame([_factor_row('C/USDT:USDT', 3, run_time),
                         _factor_row('A/USDT:USDT', 1, run_time),
                         _factor_row('B/USDT:USDT', 2, run_time)])
    trig = ScheduledSelectionTrigger(_strategy_config(), {'Reg_v2_2': True}, [1],
                                     select_fn=lambda scd, rt, off: rows)
    out = trig.propose(TriggerContext(exchange='okx', run_time=run_time))
    assert [p.symbol for p in out] == ['A/USDT:USDT', 'B/USDT:USDT', 'C/USDT:USDT']


def test_scheduled_trigger_records_s_shape_when_present():
    """s 仪表(2026-07-21):S_shape_5 只记录不排名——列存在时落进 factors,缺列不炸。"""
    from gridtrade.execution.triggers import ScheduledSelectionTrigger
    run_time = pd.Timestamp('2025-06-24 14:00:00')

    def _row(sym, rank, s_shape=None):
        r = _factor_row(sym, rank, run_time)
        r.update({'Reg_v2_2': 0.1, 'rank_sum': float(rank)})
        if s_shape is not None:
            r['S_shape_5'] = s_shape
        return r

    rows = pd.DataFrame([_row('BTC/USDT:USDT', 1, s_shape=1.42)])
    trig = ScheduledSelectionTrigger(_strategy_config(), {'Reg_v2_2': True}, [1],
                                     select_fn=lambda scd, rt, off: rows)
    ctx = TriggerContext(exchange='okx', run_time=run_time,
                         symbol_candle_data={'BTC/USDT:USDT': None})
    trig.propose(ctx)
    assert ctx.selection_ranked[0]['factors']['S_shape_5'] == 1.42
    # 缺列(旧数据/异常路径)→ 不记不炸
    rows2 = pd.DataFrame([_row('BTC/USDT:USDT', 1)])
    trig2 = ScheduledSelectionTrigger(_strategy_config(), {'Reg_v2_2': True}, [1],
                                      select_fn=lambda scd, rt, off: rows2)
    ctx2 = TriggerContext(exchange='okx', run_time=run_time,
                          symbol_candle_data={'BTC/USDT:USDT': None})
    trig2.propose(ctx2)
    assert 'S_shape_5' not in ctx2.selection_ranked[0]['factors']
