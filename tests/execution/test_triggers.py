import pandas as pd

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
