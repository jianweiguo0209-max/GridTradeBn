"""scheduler 选币轮:label_feed 存在则先 update(candles 键集, run_time)再进 cycle;
update 崩溃只降级(print 一行,轮次照常完成)。镜像 test_scheduler.py 的 _rt()/注入
fetch_candles 写法(fake 交易所空 universe,pool-guard 分母为 0 恒真,shock 默认关)。"""
import pandas as pd

from gridtrade.config import load_deploy_config
from gridtrade.runtime.factory import build_runtime


def _rt(**kw):
    env = {'EXCHANGE': 'fake'}
    env.update(kw)
    return build_runtime(load_deploy_config(env=env))


class _SpyFeed:
    def __init__(self, raise_exc=None):
        self.calls = []
        self._raise = raise_exc

    def update(self, symbols, run_time):
        self.calls.append((list(symbols), run_time))
        if self._raise is not None:
            raise self._raise


def _fake_fetch_two(adapter, symbols, run_time, **kw):
    # 空 df(而非 object())——走到 select_fn 内部 proceed_calc_symbol_factor 时
    # 按"无数据"正常短路返回,不炸;测试只关心 label-feed 钩子本身。
    return {'BTC/USDC:USDC': pd.DataFrame(), 'ETH/USDC:USDC': pd.DataFrame()}


def test_scheduler_calls_label_feed_update_with_candle_keys_and_run_time():
    from gridtrade.runtime.scheduler import run_scheduler_once
    rt = _rt()
    feed = _SpyFeed()
    rt.label_feed = feed
    now = 1_750_000_000.0
    out = run_scheduler_once(rt, now_fn=lambda: now, fetch_candles=_fake_fetch_two)
    assert len(feed.calls) == 1
    seen_symbols, seen_run_time = feed.calls[0]
    assert set(seen_symbols) == {'BTC/USDC:USDC', 'ETH/USDC:USDC'}
    assert seen_run_time == pd.Timestamp(now, unit='s').floor('H')
    assert out['opened'] == [] and out['closed'] == []


def test_scheduler_label_feed_update_failure_degrades_without_raising():
    from gridtrade.runtime.scheduler import run_scheduler_once
    rt = _rt()
    feed = _SpyFeed(raise_exc=RuntimeError('boom'))
    rt.label_feed = feed
    out = run_scheduler_once(rt, now_fn=lambda: 1_750_000_000.0,
                             fetch_candles=_fake_fetch_two)
    assert len(feed.calls) == 1        # update 被调用(且抛了),轮次仍正常返回
    assert 'opened' in out and 'closed' in out


def test_scheduler_no_label_feed_is_noop():
    # 回退档(ranker='rank')⇒ label_feed=None,选币轮不受影响。
    # ⚠ stub 显式置 None：默认档已于 2026-07-27 改为 eff1(会装出 LabelFeed)。
    from gridtrade.runtime.scheduler import run_scheduler_once
    rt = _rt()
    rt.label_feed = None
    assert rt.label_feed is None
    out = run_scheduler_once(rt, now_fn=lambda: 1_750_000_000.0,
                             fetch_candles=_fake_fetch_two)
    assert out['opened'] == [] and out['closed'] == []
