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


def test_fetch_universe_candles_uses_lowercase_1h_timeframe():
    # ccxt 用小写时间单位（'1h'）；'1H' 会 NotSupported('timeframe unit H ...')
    import pandas as pd
    from gridtrade.runtime.scheduler import fetch_universe_candles
    seen = {}
    class _Spy:
        def fetch_ohlcv(self, sym, timeframe, start_ms, end_ms):
            seen['tf'] = timeframe
            return pd.DataFrame()
    fetch_universe_candles(_Spy(), ['BTC/USDC:USDC'],
                           pd.Timestamp('2025-06-24 14:00:00'))
    assert seen['tf'] == '1h'


def test_seconds_to_next_hour():
    from gridtrade.runtime.scheduler import _seconds_to_next_hour
    assert _seconds_to_next_hour(1_750_000_000.0) == 3200
    assert _seconds_to_next_hour(3600.0) == 3600       # 整点 -> 整一小时
    assert _seconds_to_next_hour(3601.0) == 3599


def test_run_scheduler_run_on_start_runs_immediately():
    from gridtrade.runtime.scheduler import run_scheduler
    rt = _rt()
    calls, sleeps = [], []
    run_scheduler(rt, once=True, run_on_start=True, sleep=sleeps.append,
                  now_fn=lambda: 1_750_000_000.0,
                  run_once_fn=lambda runtime, now_fn: calls.append('run'))
    assert calls == ['run'] and sleeps == []   # 启动即跑、无 sleep


def test_run_scheduler_sleeps_to_hour_then_runs_when_not_run_on_start():
    from gridtrade.runtime.scheduler import run_scheduler
    rt = _rt()
    calls, sleeps = [], []
    run_scheduler(rt, once=True, run_on_start=False, sleep=sleeps.append,
                  now_fn=lambda: 1_750_000_000.0,
                  run_once_fn=lambda runtime, now_fn: calls.append('run'))
    assert sleeps == [3200] and calls == ['run']   # 先睡到整点再跑


def test_run_scheduler_loops_until_should_stop():
    from gridtrade.runtime.scheduler import run_scheduler
    rt = _rt()
    n = {'i': 0}
    def _run(runtime, now_fn):
        n['i'] += 1
    run_scheduler(rt, sleep=lambda d: None, now_fn=lambda: 1_750_000_000.0,
                  run_once_fn=_run, should_stop=lambda: n['i'] >= 3)
    assert n['i'] == 3


def test_run_scheduler_degrades_on_error_and_continues():
    from gridtrade.runtime.scheduler import run_scheduler
    rt = _rt()
    logs = []
    def _boom(runtime, now_fn):
        raise RuntimeError('boom')
    run_scheduler(rt, once=True, run_on_start=True, sleep=lambda d: None,
                  now_fn=lambda: 1_750_000_000.0, log=logs.append,
                  run_once_fn=_boom)
    assert any('boom' in s or 'degraded' in s for s in logs)
