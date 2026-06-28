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
