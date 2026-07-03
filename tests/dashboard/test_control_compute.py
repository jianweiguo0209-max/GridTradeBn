# tests/dashboard/test_control_compute.py
from gridtrade.dashboard.control_compute import compute_proposals, defaults_for_symbol


class _Proposal:
    def __init__(self, symbol, gp): self.symbol = symbol; self.grid_params = gp
    tag = 'gt0'; offset = 0


class _TriggerEngine:
    def __init__(self, props): self._p = props
    def collect(self, ctx): return self._p


class _RT:
    def __init__(self, props):
        self.trigger_engine = _TriggerEngine(props)
        self.adapter = object()
        self.config = type('C', (), {'exchange': 'fake', 'blacklist': (), 'whitelist': (),
                                     'scheduler_period': '12H'})()


def _fake_fetch(adapter, universe, run_time, **kw): return {}


def test_compute_proposals_flattens(monkeypatch):
    import gridtrade.dashboard.control_compute as m
    monkeypatch.setattr(m, 'resolve_live_universe', lambda *a, **k: ['BTC/USDT:USDT'])
    rt = _RT([_Proposal('BTC/USDT:USDT', {'low_price': 90.0, 'high_price': 110.0,
                                          'grid_count': 10, 'stop_low_price': 80.0,
                                          'stop_high_price': 120.0})])
    out = compute_proposals(rt, now_fn=lambda: 0.0, fetch_candles=_fake_fetch)
    assert out[0]['symbol'] == 'BTC/USDT:USDT'
    assert out[0]['grid_params']['grid_count'] == 10
    assert out[0]['tag'] == 'gt0'


def test_defaults_for_symbol_filters(monkeypatch):
    import gridtrade.dashboard.control_compute as m
    monkeypatch.setattr(m, 'resolve_live_universe', lambda *a, **k: ['BTC/USDT:USDT', 'ETH/USDT:USDT'])
    rt = _RT([_Proposal('BTC/USDT:USDT', {'low_price': 1}), _Proposal('ETH/USDT:USDT', {'low_price': 2})])
    d = defaults_for_symbol(rt, 'ETH/USDT:USDT', now_fn=lambda: 0.0, fetch_candles=_fake_fetch)
    assert d['symbol'] == 'ETH/USDT:USDT' and d['grid_params']['low_price'] == 2
    assert defaults_for_symbol(rt, 'NOPE', now_fn=lambda: 0.0, fetch_candles=_fake_fetch) is None
