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
