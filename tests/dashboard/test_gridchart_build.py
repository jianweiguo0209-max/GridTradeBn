import pandas as pd
from unittest.mock import patch
from gridtrade.dashboard.gridchart import build_grid_chart
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.state.grids import GridRepository
from gridtrade.state.orders import OrderRepository
from gridtrade.state.fills import FillRepository
from gridtrade.state.models import Grid, GridOrder, Fill, ACTIVE


def _seed(store):
    GridRepository(store).create(Grid(
        id='g1', exchange='fake', symbol='BTC/USDT:USDT', status=ACTIVE,
        created_at=1_000_000, entry_price=100.0,
        low_price=90.0, high_price=110.0, grid_count=10,
        stop_low_price=80.0, stop_high_price=120.0, cap=100.0, leverage=5.0))
    OrderRepository(store).upsert(GridOrder(client_oid='o1', grid_id='g1', line_index=1,
                                            side='buy', price=95.0, size=1.0, status='open'))
    FillRepository(store).add_if_new(Fill(trade_id='t1', grid_id='g1', line_index=2,
                                          side='sell', price=105.0, size=1.0, fee=0.1, ts=1_500_000))


def _candles():
    return pd.DataFrame({
        'symbol': ['BTC/USDT:USDT', 'BTC/USDT:USDT'],
        'candle_begin_time': pd.to_datetime([1_000_000, 1_060_000], unit='ms'),
        'open': [100.0, 101.0], 'high': [102.0, 103.0], 'low': [99.0, 100.0],
        'close': [101.0, 102.5], 'vol': [1.0, 1.0], 'volCcy': [1.0, 1.0],
        'quote_volume': [1.0, 1.0],
    })


def test_build_populates_all_layers(store):
    _seed(store)
    fake = FakeExchange(); fake.seed_ohlcv('BTC/USDT:USDT', _candles()); fake.set_price('BTC/USDT:USDT', 102.0)
    dto = build_grid_chart(store, fake, 'g1', 'life', now_ms_fn=lambda: 2_000_000)
    assert dto is not None
    assert dto.ohlcv_ok is True
    assert dto.price_series == [(1_000_000, 101.0), (1_060_000, 102.5)]
    assert len(dto.grid_lines) >= 2 and min(dto.grid_lines) >= 90.0 - 1e-9
    assert dto.open_orders == [(95.0, 'buy')]
    assert dto.fills == [(1_500_000, 105.0, 'sell')]
    assert dto.entry_price == 100.0 and dto.stop_low == 80.0 and dto.stop_high == 120.0
    assert dto.current_price == 102.0


def test_build_degrades_on_ohlcv_error(store):
    _seed(store)

    class _BadOhlcv(FakeExchange):
        def fetch_ohlcv(self, *a, **k):
            raise RuntimeError('rate limited')
    fake = _BadOhlcv(); fake.set_price('BTC/USDT:USDT', 102.0)
    dto = build_grid_chart(store, fake, 'g1', 'life', now_ms_fn=lambda: 2_000_000)
    assert dto.ohlcv_ok is False and dto.price_series == []
    assert len(dto.grid_lines) >= 2                # DB/纯函数层仍在
    assert dto.open_orders == [(95.0, 'buy')]


def test_build_missing_grid_returns_none(store):
    assert build_grid_chart(store, FakeExchange(), 'nope', 'life') is None


def test_build_degrades_on_missing_price_sequence_key(store):
    """Ensure that if grid_order_info returns dict without '价格序列' key, chart still builds."""
    _seed(store)
    fake = FakeExchange(); fake.seed_ohlcv('BTC/USDT:USDT', _candles()); fake.set_price('BTC/USDT:USDT', 102.0)

    # Monkeypatch grid_order_info to return empty dict (no '价格序列' key)
    with patch('gridtrade.dashboard.gridchart.grid_order_info', return_value={}):
        dto = build_grid_chart(store, fake, 'g1', 'life', now_ms_fn=lambda: 2_000_000)

    # Chart should build successfully with empty grid_lines, not crash
    assert dto is not None
    assert dto.grid_lines == []
    assert dto.price_series == [(1_000_000, 101.0), (1_060_000, 102.5)]
