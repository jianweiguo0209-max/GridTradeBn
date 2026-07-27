"""eff1 选币线走 select_grids 的端到端覆盖(本目录唯一**显式传 ranker='eff1'** 的文件)。

conftest 把本目录 env 钉在 rank(机制测试用);这里靠**实参覆盖**(实参 > env > 默认)。
断言三件事:①真按 p12_eff 降序选(不是 rank_sum)②tick 过滤在排名前⇒名次递补
③选币器/tick 旋钮进了缓存 key(换旋钮不串缓存)——③是对齐时补的投毒防线。
"""
import numpy as np
import pandas as pd

from gridtrade.backtest import select_cache as SC
from gridtrade.backtest.backtest_run import select_grids
from gridtrade.backtest.cache import ParquetCache
from gridtrade.exchanges.base import CANDLE_COLS
from gridtrade.backtest.backtest_run import BT_STRATEGY
from tests.backtest.test_selection_replay import FACTORS, _bars

# ⚠ 用真 BT_STRATEGY 而非 test_selection_replay 的最小 STRAT:后者缺 grid_v2_config/
# price_limit ⇒ tick 过滤的 calc_grid_params_v2 抛异常被 fail-open 吞掉、过滤静默失效。
STRAT = dict(BT_STRATEGY, choose_symbols=1)

WS = pd.Timestamp('2024-01-10 00:00:00')
WE = pd.Timestamp('2024-01-11 00:00:00')
SYMS = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT']


def _quiet(*a, **k):
    pass


def _bars_1m(symbol, cross_per_hour, n_hours=400, seed=0, start='2024-01-01'):
    """合成 1m:每小时来回穿越 `cross_per_hour` 次 1% 对数阶梯 ⇒ cross1 可控、mae 很小
    ⇒ p12_eff 单调随 cross_per_hour。用它把「谁该被 eff1 选中」变成确定性事实。"""
    n = n_hours * 60
    t = pd.date_range(start, periods=n, freq='1min')
    rng = np.random.RandomState(seed)
    # 每小时内做 cross_per_hour 个来回:振幅 1.2% 保证跨过阶梯边界
    phase = np.arange(n) % 60
    wave = np.where((phase // max(1, 30 // max(cross_per_hour, 1))) % 2 == 0, 0.0, 0.012)
    close = 100.0 * (1.0 + wave)
    return pd.DataFrame({
        'symbol': symbol, 'candle_begin_time': t,
        'open': close, 'high': close * 1.0002, 'low': close * 0.9998, 'close': close,
        'vol': rng.uniform(1e3, 1e4, n), 'volCcy': rng.uniform(1e3, 1e4, n),
        'quote_volume': rng.uniform(1e6, 1e7, n),
    })[CANDLE_COLS]


def _seed(tmp_path, crosses):
    """1h(选币因子/布网用)+ 1m(p12 标签用)双写。crosses: {symbol: 每小时穿越次数}"""
    cache = ParquetCache(str(tmp_path))
    for i, (s, cph) in enumerate(crosses.items()):
        h = _bars(s, seed=i + 1)
        for day, g in h.groupby(h['candle_begin_time'].dt.strftime('%Y-%m-%d')):
            cache.write('1h', s, day, g.reset_index(drop=True))
        m = _bars_1m(s, cph, seed=i + 1)
        for day, g in m.groupby(m['candle_begin_time'].dt.strftime('%Y-%m-%d')):
            cache.write('1m', s, day, g.reset_index(drop=True))
    return cache


def test_eff1_pick_follows_p12_eff_not_rank_sum(tmp_path_factory):
    """把「穿越最多的币」换个人,选中结果必须跟着换 —— 证明排序键真是 p12_eff。

    只断言「eff1 与 rank 选出的币不同」是巧合依赖的(某个币可能两边都赢);
    让 1m 数据说话、看结论是否跟着动,才是对排序键的真检验。
    1h 数据(rank_sum 的输入)两次完全相同,变的只有 1m ⇒ rank 臂必然不动。
    """
    got = {}
    for tag, crosses in (('BBB', {'AAA/USDT:USDT': 1, 'BBB/USDT:USDT': 10,
                                  'CCC/USDT:USDT': 3}),
                         ('CCC', {'AAA/USDT:USDT': 1, 'BBB/USDT:USDT': 3,
                                  'CCC/USDT:USDT': 10})):
        cache = _seed(tmp_path_factory.mktemp(tag), crosses)
        eff1 = select_grids(cache, SYMS, WS, WE, STRAT, FACTORS, log=_quiet,
                            ranker='eff1', min_ticks=0.0)
        rank = select_grids(cache, SYMS, WS, WE, STRAT, FACTORS, log=_quiet,
                            ranker='rank', min_ticks=0.0)
        assert eff1, 'eff1 一个都没选出来(1m 标签没算成?)'
        assert rank, 'rank 对照臂为空,比较无意义'
        got[tag] = ({r['symbol'] for _rt, _off, r in eff1},
                    {r['symbol'] for _rt, _off, r in rank})
    assert got['BBB'][0] == {'BBB/USDT:USDT'}, 'eff1 该选穿越最多的 BBB,实得 %s' % (got['BBB'][0],)
    assert got['CCC'][0] == {'CCC/USDT:USDT'}, 'eff1 该跟着换到 CCC,实得 %s' % (got['CCC'][0],)
    assert got['BBB'][1] == got['CCC'][1], '1h 没变 ⇒ rank 臂必须不动(对照组失效则本测试无效)'


def test_eff1_tick_filter_promotes_next_candidate(tmp_path):
    cache = _seed(tmp_path, {'AAA/USDT:USDT': 1, 'BBB/USDT:USDT': 10, 'CCC/USDT:USDT': 3})
    top = {'BBB/USDT:USDT': 50.0}          # 榜首给个粗到离谱的 tick ⇒ spacing<3×tick 必剔
    picks = select_grids(cache, SYMS, WS, WE, STRAT, FACTORS, log=_quiet,
                         ranker='eff1', min_ticks=3.0, tick_map=top)
    got = {r['symbol'] for _rt, _off, r in picks}
    assert 'BBB/USDT:USDT' not in got, 'tick 过滤没生效'
    assert got == {'CCC/USDT:USDT'}, '应递补到次高 p12_eff(免费递补),实得 %s' % got


def test_ranker_and_min_ticks_are_in_cache_key(tmp_path):
    """缓存投毒防线:换选币器/tick 阈值必须换 key,否则会命中另一套 picks 且看不出异常。"""
    cache = _seed(tmp_path, {'AAA/USDT:USDT': 1, 'BBB/USDT:USDT': 10})
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT']
    k_rank, _ = SC.compute_key(cache, syms, WS, WE, '1h', 0.0, (), STRAT, FACTORS,
                               ranker='rank', min_ticks=0.0)
    k_eff1, _ = SC.compute_key(cache, syms, WS, WE, '1h', 0.0, (), STRAT, FACTORS,
                               ranker='eff1', min_ticks=0.0)
    k_eff1_t3, _ = SC.compute_key(cache, syms, WS, WE, '1h', 0.0, (), STRAT, FACTORS,
                                  ranker='eff1', min_ticks=3.0)
    assert len({k_rank, k_eff1, k_eff1_t3}) == 3, '三种口径必须互不串 key'
