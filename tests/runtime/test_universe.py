from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument


def _ex(*specs):
    # specs: (symbol, state)
    insts = [Instrument(sym, 0.1, 0.001, 0.001, st, 0) for sym, st in specs]
    return FakeExchange(instruments=insts, price=100.0)


def test_universe_keeps_live_excludes_blacklist():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('ETH/USDC:USDC', 'live'),
             ('SOL/USDC:USDC', 'live'))
    out = resolve_live_universe(ex, blacklist=('ETH/USDC:USDC',))
    assert out == ['BTC/USDC:USDC', 'SOL/USDC:USDC']


def test_universe_drops_non_live():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('OLD/USDC:USDC', 'delisted'))
    assert resolve_live_universe(ex) == ['BTC/USDC:USDC']


def test_universe_empty_blacklist_keeps_all_live():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('ETH/USDC:USDC', 'live'))
    assert resolve_live_universe(ex) == ['BTC/USDC:USDC', 'ETH/USDC:USDC']


def test_universe_whitelist_restricts_to_listed_live():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('ETH/USDC:USDC', 'live'),
             ('SOL/USDC:USDC', 'live'))
    out = resolve_live_universe(ex, whitelist=('ETH/USDC:USDC', 'SOL/USDC:USDC'))
    assert out == ['ETH/USDC:USDC', 'SOL/USDC:USDC']


def test_universe_whitelist_drops_nonlive_and_unknown():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('OLD/USDC:USDC', 'delisted'))
    # OLD 非 live、GHOST 未上市 -> 都不入
    out = resolve_live_universe(ex, whitelist=('BTC/USDC:USDC', 'OLD/USDC:USDC',
                                               'GHOST/USDC:USDC'))
    assert out == ['BTC/USDC:USDC']


def test_blacklist_applies_even_in_whitelist_mode():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('ETH/USDC:USDC', 'live'),
             ('SOL/USDC:USDC', 'live'))
    # 档0：ETH 被硬禁，即使它在白名单里也必须剔除
    out = resolve_live_universe(ex, blacklist=('ETH/USDC:USDC',),
                                whitelist=('BTC/USDC:USDC', 'ETH/USDC:USDC'))
    assert out == ['BTC/USDC:USDC']


def test_universe_min_quote_volume_floor():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('MID/USDC:USDC', 'live'),
             ('LOW/USDC:USDC', 'live'), ('NOVOL/USDC:USDC', 'live'))
    ex.seed_quote_volumes({'BTC/USDC:USDC': 5_000_000.0, 'MID/USDC:USDC': 1_000_000.0,
                           'LOW/USDC:USDC': 100_000.0})   # NOVOL 无量
    # 门槛 1e6：保留 >=1e6（BTC/MID）；LOW 与无量 NOVOL 剔除
    out = resolve_live_universe(ex, min_quote_volume=1_000_000.0)
    assert out == ['BTC/USDC:USDC', 'MID/USDC:USDC']


def test_universe_floor_zero_disabled_keeps_all():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('LOW/USDC:USDC', 'live'))
    ex.seed_quote_volumes({'BTC/USDC:USDC': 5_000_000.0, 'LOW/USDC:USDC': 1.0})
    # 门槛 0 = 停用：不过滤（也不管成交额）
    assert resolve_live_universe(ex, min_quote_volume=0.0) == ['BTC/USDC:USDC', 'LOW/USDC:USDC']


def test_universe_floor_failopen_on_empty_volumes():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('LOW/USDC:USDC', 'live'))
    # 未 seed 成交额 → fetch_24h_quote_volumes 返回 {} → fail-open 不清空票池
    assert resolve_live_universe(ex, min_quote_volume=1_000_000.0) == \
           ['BTC/USDC:USDC', 'LOW/USDC:USDC']


def test_universe_top_volume_pct_keeps_top_ceil():
    # 相对口径：5 币 pct=0.55 → ceil(2.75)=3，量前三入选（spec 2026-07-14-universe-top-volume-pct）
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('A/USDT:USDT', 'live'), ('B/USDT:USDT', 'live'), ('C/USDT:USDT', 'live'),
             ('D/USDT:USDT', 'live'), ('E/USDT:USDT', 'live'))
    ex.seed_quote_volumes({'A/USDT:USDT': 500.0, 'B/USDT:USDT': 400.0,
                           'C/USDT:USDT': 300.0, 'D/USDT:USDT': 200.0,
                           'E/USDT:USDT': 100.0})
    out = resolve_live_universe(ex, top_volume_pct=0.55)
    assert sorted(out) == ['A/USDT:USDT', 'B/USDT:USDT', 'C/USDT:USDT']


def test_universe_top_volume_pct_missing_volume_ranks_last():
    # 无量数据的币按 0 垫底被切
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('A/USDT:USDT', 'live'), ('B/USDT:USDT', 'live'), ('C/USDT:USDT', 'live'))
    ex.seed_quote_volumes({'A/USDT:USDT': 100.0, 'B/USDT:USDT': 50.0})
    out = resolve_live_universe(ex, top_volume_pct=0.55)   # ceil(1.65)=2
    assert sorted(out) == ['A/USDT:USDT', 'B/USDT:USDT']


def test_universe_top_volume_pct_fail_open_on_empty_tickers():
    # ticker 全空 → fail-open 跳过（不清空票池，沿绝对地板既有语义）
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('A/USDT:USDT', 'live'), ('B/USDT:USDT', 'live'))
    out = resolve_live_universe(ex, top_volume_pct=0.55)
    assert sorted(out) == ['A/USDT:USDT', 'B/USDT:USDT']


def test_universe_top_volume_pct_ties_deterministic():
    # 量并列按 symbol 字典序（回测复现确定性）
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('B/USDT:USDT', 'live'), ('A/USDT:USDT', 'live'), ('C/USDT:USDT', 'live'))
    ex.seed_quote_volumes({'A/USDT:USDT': 100.0, 'B/USDT:USDT': 100.0,
                           'C/USDT:USDT': 100.0})
    out = resolve_live_universe(ex, top_volume_pct=0.34)   # ceil(1.02)=2
    assert sorted(out) == ['A/USDT:USDT', 'B/USDT:USDT']


def test_universe_floor_and_pct_stack():
    # 叠加语义：先地板（剔 D）后相对（剩 3 取 ceil(1.65)=2）
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('A/USDT:USDT', 'live'), ('B/USDT:USDT', 'live'), ('C/USDT:USDT', 'live'),
             ('D/USDT:USDT', 'live'))
    ex.seed_quote_volumes({'A/USDT:USDT': 400.0, 'B/USDT:USDT': 300.0,
                           'C/USDT:USDT': 200.0, 'D/USDT:USDT': 50.0})
    out = resolve_live_universe(ex, min_quote_volume=100.0, top_volume_pct=0.55)
    assert sorted(out) == ['A/USDT:USDT', 'B/USDT:USDT']
