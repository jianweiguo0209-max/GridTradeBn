from gridtrade.config import load_deploy_config
from gridtrade.runtime.factory import build_runtime


def _rt(**kw):
    env = {'EXCHANGE': 'fake'}
    env.update(kw)
    return build_runtime(load_deploy_config(env=env))


def test_run_scheduler_once_empty_universe_no_opens_and_beats():
    from gridtrade.runtime.scheduler import run_scheduler_once
    rt = _rt()                      # fake 无 instruments -> 空币池
    out = run_scheduler_once(rt, now_fn=lambda: 1_750_000_000.0)
    assert out['opened'] == [] and out['closed'] == []
    assert rt.heartbeats.get('scheduler') is not None


def test_run_scheduler_once_uses_injected_candles_and_fetch():
    from gridtrade.runtime.scheduler import run_scheduler_once
    seen = {}
    def _fake_fetch(adapter, symbols, run_time, **kw):
        seen['symbols'] = list(symbols)
        return {}
    rt = _rt()
    run_scheduler_once(rt, now_fn=lambda: 1_750_000_000.0,
                       fetch_candles=_fake_fetch)
    assert seen['symbols'] == []   # 空币池传给 fetch


def test_fetch_universe_candles_skips_empty_and_collects_nonempty():
    import pandas as pd
    from gridtrade.runtime.scheduler import fetch_universe_candles
    from gridtrade.exchanges.fake import FakeExchange
    from gridtrade.exchanges.base import Instrument, CANDLE_COLS
    ex = FakeExchange(instruments=[Instrument('BTC/USDC:USDC', 0.1, 0.001, 0.001,
                                              'live', 0)], price=100.0)
    df = pd.DataFrame([[0] * len(CANDLE_COLS)], columns=CANDLE_COLS)
    ex.seed_ohlcv('BTC/USDC:USDC', df)
    out = fetch_universe_candles(ex, ['BTC/USDC:USDC', 'NONE/USDC:USDC'],
                                 pd.Timestamp('2025-06-24 14:00:00'),
                                 max_candle_num=10)
    assert 'BTC/USDC:USDC' in out and 'NONE/USDC:USDC' not in out
