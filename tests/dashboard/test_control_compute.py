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
                                     'scheduler_period': '12H',
                                     'min_quote_volume_24h': 1000000.0})()


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


def test_compute_proposals_forwards_min_quote_volume_floor(monkeypatch):
    # Fix1 回归：dashboard 调用 resolve_live_universe 须转发成交额地板（第4参），
    # 否则生产下 dashboard 选币会用未设地板的 universe（scheduler.py 已正确转发）。
    import gridtrade.dashboard.control_compute as m
    captured = {}

    def _fake_resolve(adapter, blacklist, whitelist, min_quote_volume_24h=0.0):
        captured['min_quote_volume_24h'] = min_quote_volume_24h
        return ['BTC/USDT:USDT']

    monkeypatch.setattr(m, 'resolve_live_universe', _fake_resolve)
    rt = _RT([_Proposal('BTC/USDT:USDT', {'low_price': 90.0, 'high_price': 110.0,
                                          'grid_count': 10, 'stop_low_price': 80.0,
                                          'stop_high_price': 120.0})])
    compute_proposals(rt, now_fn=lambda: 0.0, fetch_candles=_fake_fetch)
    assert captured['min_quote_volume_24h'] == rt.config.min_quote_volume_24h == 1000000.0


def test_defaults_for_symbol_filters(monkeypatch):
    import gridtrade.dashboard.control_compute as m
    monkeypatch.setattr(m, 'resolve_live_universe', lambda *a, **k: ['BTC/USDT:USDT', 'ETH/USDT:USDT'])
    rt = _RT([_Proposal('BTC/USDT:USDT', {'low_price': 1}), _Proposal('ETH/USDT:USDT', {'low_price': 2})])
    d = defaults_for_symbol(rt, 'ETH/USDT:USDT', now_fn=lambda: 0.0, fetch_candles=_fake_fetch)
    assert d['symbol'] == 'ETH/USDT:USDT' and d['grid_params']['low_price'] == 2
    assert defaults_for_symbol(rt, 'NOPE', now_fn=lambda: 0.0, fetch_candles=_fake_fetch) is None


def test_compute_proposals_prefilters_locked_symbols(monkeypatch):
    # 方案A 同口径：dashboard 候选池预览也剔除已被活跃网格锁定的币（无换仓 tag 豁免——
    # 预览语境下没有"正在关"的 tag），预览结果与此刻真正能开的集合一致。
    import gridtrade.dashboard.control_compute as m
    monkeypatch.setattr(m, 'resolve_live_universe',
                        lambda *a, **k: ['AAA/USDT:USDT', 'BBB/USDT:USDT'])

    class _Grid:
        symbol = 'BBB/USDT:USDT'; tag = 'gtX'
    class _Grids:
        # cap=2 语义：2 格才触顶被剔出预览票池
        def list_active(self): return [_Grid(), _Grid()]
    class _Ex:
        grids = _Grids()
    class _Mgr:
        executor = _Ex()

    rt = _RT([])
    rt.manager = _Mgr()
    seen = {}
    def _capture(adapter, universe, run_time, **kw):
        seen['universe'] = list(universe)
        return {}
    compute_proposals(rt, now_fn=lambda: 1_750_000_000.0, fetch_candles=_capture)
    assert seen['universe'] == ['AAA/USDT:USDT']            # BBB 被锁 → 出票池
