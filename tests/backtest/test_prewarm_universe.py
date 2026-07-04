from gridtrade.exchanges.base import Instrument


class _DS:
    def __init__(self, insts):
        self._insts = insts
    def list_instruments(self):
        return self._insts


def test_resolve_universe_subtracts_blacklist():
    from gridtrade.backtest.prewarm import resolve_universe
    ds = _DS([Instrument('BTC/USDC:USDC', 0.1, 0.001, 0.001, 'live', 0),
              Instrument('ETH/USDC:USDC', 0.1, 0.001, 0.001, 'live', 0),
              Instrument('OLD/USDC:USDC', 0.1, 0.001, 0.001, 'expired', 0)])
    out = resolve_universe(ds, blacklist=('ETH/USDC:USDC',))
    assert out == ['BTC/USDC:USDC']           # live −黑名单，去重排序；OLD 非 live 剔
    assert resolve_universe(ds) == ['BTC/USDC:USDC', 'ETH/USDC:USDC']   # 无黑名单
