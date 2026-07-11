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


def test_fetch_universe_candles_skips_symbol_that_raises():
    # 一个坏币（BadSymbol/无数据）不该阻塞整个币池——跳过它，继续拉其余
    import ccxt
    import pandas as pd
    from gridtrade.runtime.scheduler import fetch_universe_candles
    from gridtrade.exchanges.base import CANDLE_COLS
    good_df = pd.DataFrame([[0] * len(CANDLE_COLS)], columns=CANDLE_COLS)
    class _Spy:
        def fetch_ohlcv(self, sym, timeframe, start_ms, end_ms):
            if sym == 'BAD/USDC:USDC':
                raise ccxt.BadSymbol('no market')
            return good_df
    out = fetch_universe_candles(_Spy(), ['BAD/USDC:USDC', 'GOOD/USDC:USDC'],
                                 pd.Timestamp('2025-06-24 14:00:00'))
    assert 'GOOD/USDC:USDC' in out and 'BAD/USDC:USDC' not in out


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


def _pace_spy_adapter():
    import pandas as pd
    from gridtrade.exchanges.base import CANDLE_COLS
    good_df = pd.DataFrame([[0] * len(CANDLE_COLS)], columns=CANDLE_COLS)
    class _Spy:
        def fetch_ohlcv(self, sym, timeframe, start_ms, end_ms):
            return good_df
    return _Spy()


def test_fetch_universe_candles_paces_between_symbols_by_default():
    # 默认 pace_ms=None → 用 FETCH_PACE_MS_DEFAULT(2000ms，HL 权重推导)；n 币 sleep n-1 次。
    import pandas as pd
    from gridtrade.runtime.scheduler import fetch_universe_candles, FETCH_PACE_MS_DEFAULT
    assert FETCH_PACE_MS_DEFAULT == 2000.0
    sleeps = []
    syms = ['A/USDC:USDC', 'B/USDC:USDC', 'C/USDC:USDC']
    out = fetch_universe_candles(_pace_spy_adapter(), syms,
                                 pd.Timestamp('2025-06-24 14:00:00'),
                                 sleep=sleeps.append)
    assert len(out) == 3
    assert sleeps == [2.0, 2.0]                    # 3 币 → 2 次间隔，秒为单位


def test_fetch_universe_candles_pace_zero_disables_sleep():
    # pace_ms=0 = 显式关（向后兼容护栏）
    import pandas as pd
    from gridtrade.runtime.scheduler import fetch_universe_candles
    sleeps = []
    out = fetch_universe_candles(_pace_spy_adapter(), ['A/USDC:USDC', 'B/USDC:USDC'],
                                 pd.Timestamp('2025-06-24 14:00:00'),
                                 pace_ms=0, sleep=sleeps.append)
    assert len(out) == 2 and sleeps == []


def test_run_scheduler_once_prefilters_locked_symbols_except_current_tag():
    # 方案A（legacy 半拉黑档2 执行位对齐）：他 tag 持有的币在选币入口剔出票池
    # （连 K 线都不拉，排名自动落次优币）；本轮换仓 tag 自己的币即将释放 → 不剔（允许连任）。
    import pandas as pd
    from gridtrade.core.selection import compute_offset
    from gridtrade.config import DEFAULT_STRATEGY_CONFIG
    from gridtrade.exchanges.base import Instrument
    from gridtrade.state.models import Grid, ACTIVE
    from gridtrade.runtime.scheduler import run_scheduler_once

    rt = _rt()
    now = 1_750_000_000.0
    run_time = pd.Timestamp(now, unit='s').floor('H')
    cur_tag = '%s%d' % (DEFAULT_STRATEGY_CONFIG['strategy_tag'],
                        compute_offset(run_time, rt.config.scheduler_period))
    syms = ['AAA/USDC:USDC', 'BBB/USDC:USDC', 'CCC/USDC:USDC']
    rt.adapter._inner._instruments = [Instrument(s, 0.1, 0.001, 0.001, 'live', 0)
                                      for s in syms]
    grids = rt.manager.executor.grids
    gp = dict(entry_price=100.0, low_price=98.0, high_price=102.0, grid_count=8,
              stop_low_price=97.0, stop_high_price=103.0, cap=100.0, leverage=5.0)
    grids.create(Grid(id='', exchange='fake', symbol='AAA/USDC:USDC',
                      status=ACTIVE, tag=cur_tag, **gp))   # 本 tag：不剔（会被换仓真实关闭，需可 restore）
    # cap=2 语义（2026-07-12 恢复）：他 tag 持有 2 格才触顶被剔（1 格时还有名额、不剔）
    for i, tg in enumerate(('gt98', 'gt99')):
        grids.create(Grid(id='', exchange='fake', symbol='BBB/USDC:USDC',
                          status=ACTIVE, tag=tg, **gp))    # 他 tag ×2：触顶 → 剔
    seen = {}
    def _fake_fetch(adapter, symbols, run_time, **kw):
        seen['symbols'] = list(symbols)
        return {}
    run_scheduler_once(rt, now_fn=lambda: now, fetch_candles=_fake_fetch)
    assert 'BBB/USDC:USDC' not in seen['symbols']          # 他 tag 锁定 → 出票池
    assert 'AAA/USDC:USDC' in seen['symbols']              # 本 tag → 保留
    assert 'CCC/USDC:USDC' in seen['symbols']              # 自由币 → 保留


def test_prefilter_equals_shared_tier_policy_semantics():
    # 同源守卫（spec 同源性②）：scheduler 剔锁语义 ≡ 共享 capped_symbols 对同一状态
    # 的判定（现配置 tier2_cap=1）。防止实盘/回测两侧各写各的悄悄漂移。
    from collections import Counter
    from gridtrade.core.tier_policy import TierPolicy, capped_symbols
    held = Counter({'BBB/USDC:USDC': 1})
    universe = ['AAA/USDC:USDC', 'BBB/USDC:USDC', 'CCC/USDC:USDC']
    assert capped_symbols(universe, held, TierPolicy(tier2_cap=1)) == {'BBB/USDC:USDC'}


def _shock_candles(ret_4h, n=6):
    import pandas as pd
    rt_hour = pd.Timestamp(1_750_000_000.0, unit='s').floor('H')
    out = {}
    for i in range(n):
        idx = pd.date_range(rt_hour - pd.Timedelta(hours=8), periods=8, freq='1H')
        close = [100.0] * 4 + [100.0 * (1 + ret_4h)] * 4
        out['S%d/USDC:USDC' % i] = __import__('pandas').DataFrame(
            {'candle_begin_time': idx, 'close': close})
    return out


def test_shock_brake_blocks_opens_then_recovers():
    """MarketShockBrake 集成(spec 2026-07-08):冲击→只关不开+置暂停窗;窗内平静仍暂停;过窗恢复。"""
    from gridtrade.runtime.scheduler import run_scheduler_once
    rt = _rt()                                            # fake 空币池;信号 candles 注入
    t0 = 1_750_000_000.0

    out = run_scheduler_once(rt, now_fn=lambda: t0,
                             fetch_candles=lambda *a, **k: _shock_candles(-0.06))
    assert out['opened'] == [] and out.get('shock_braked') is True     # 冲击→拦
    assert getattr(rt, '_shock_until', None) is not None

    out2 = run_scheduler_once(rt, now_fn=lambda: t0 + 3600,
                              fetch_candles=lambda *a, **k: _shock_candles(0.0))
    assert out2.get('shock_braked') is True               # 窗内(X=2h)平静仍暂停

    out3 = run_scheduler_once(rt, now_fn=lambda: t0 + 3 * 3600,
                              fetch_candles=lambda *a, **k: _shock_candles(0.0))
    assert 'shock_braked' not in out3                     # 过窗恢复


def test_shock_brake_disabled_and_fail_open():
    from gridtrade.runtime.scheduler import run_scheduler_once
    rt = _rt(SHOCK_THR='0')                               # 停用:冲击也不拦
    out = run_scheduler_once(rt, now_fn=lambda: 1_750_000_000.0,
                             fetch_candles=lambda *a, **k: _shock_candles(-0.10))
    assert 'shock_braked' not in out
    rt2 = _rt()                                           # 篮子不足(<5 币) fail-open
    out2 = run_scheduler_once(rt2, now_fn=lambda: 1_750_000_000.0,
                              fetch_candles=lambda *a, **k: _shock_candles(-0.10, n=3))
    assert 'shock_braked' not in out2


def test_cycle_open_disabled_still_closes():
    """open_enabled=False:trigger 不被调用、开格为空;close_by_tag 照常。"""
    from gridtrade.runtime.cycles import run_scheduler_cycle

    class _Grids:
        def list_active(self): return []
    class _Ex:
        grids = _Grids()
    class _Mgr:
        executor = _Ex()
        def close_by_tag(self, tag, reason): self.closed_tag = tag; return ['g1']
        def open_proposals(self, ps): raise AssertionError('braked 时不应开仓')
    class _Trig:
        def collect(self, ctx): raise AssertionError('braked 时不应触发选币')
    class _Rec:
        def restore(self, gid): pass

    mgr = _Mgr()
    out = run_scheduler_cycle(mgr, _Trig(), _Rec(), ctx=None,
                              close_tag='gt3', open_enabled=False)
    assert out == {'closed': ['g1'], 'opened': [], 'shock_braked': True}
    assert mgr.closed_tag == 'gt3'
