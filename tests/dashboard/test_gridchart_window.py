# tests/dashboard/test_gridchart_window.py
from gridtrade.dashboard.gridchart import window_bounds, ChartDTO
from gridtrade.state.models import Grid, ACTIVE, CLOSED


def _grid(**kw):
    base = dict(id='g1', exchange='x', symbol='BTC/USDT:USDT', status=ACTIVE,
                created_at=1_000_000, updated_at=1_000_000)
    base.update(kw)
    return Grid(**base)


def test_window_bounds_life_active_uses_created_at_and_now():
    g = _grid(created_at=1_000_000)
    start, end, tf = window_bounds(g, 'life', now_ms_fn=lambda: 1_000_000 + 3600_000)
    assert start == 1_000_000 and end == 1_000_000 + 3600_000   # 活跃：created_at → now
    assert tf == '1m'                       # 1h 跨度 ≤2h → 1m


def test_window_bounds_life_closed_uses_updated_at():
    g = _grid(status=CLOSED, created_at=1_000_000, updated_at=1_000_000 + 6 * 3600_000)
    start, end, tf = window_bounds(g, 'life', now_ms_fn=lambda: 9_999_999_999)
    assert start == 1_000_000
    assert end == 1_000_000 + 6 * 3600_000  # 已平（CLOSED）用 updated_at，不用 now
    assert tf == '5m'                        # 6h 跨度 ≤12h → 5m


def test_window_bounds_fixed_24h():
    g = _grid()
    now = 100_000_000
    start, end, tf = window_bounds(g, '24h', now_ms_fn=lambda: now)
    assert end == now and start == now - 24 * 3600_000
    assert tf == '15m'                       # 24h ≤2d → 15m


def test_window_bounds_bad_value_falls_back_to_life():
    g = _grid(created_at=5_000_000)
    start, _end, _tf = window_bounds(g, 'nonsense', now_ms_fn=lambda: 5_000_000 + 1000)
    assert start == 5_000_000                # 回退 life（用 created_at）


def test_chart_dto_defaults():
    d = ChartDTO(symbol='BTC', window='life', timeframe='1m', start_ms=0, end_ms=1,
                 price_series=[], ohlcv_ok=False, grid_lines=[], open_orders=[],
                 fills=[], entry_price=None, stop_low=None, stop_high=None,
                 current_price=None)
    assert d.ohlcv_ok is False and d.grid_lines == []
