"""Tests for fee columns in dashboard tables."""
from gridtrade.dashboard.queries import build_records, build_overview
from gridtrade.state.fills import FillRepository
from gridtrade.state.grids import GridRepository
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.models import Fill, Grid, ACTIVE


class _PriceAdapter:
    def fetch_price(self, s):
        return 100.0


def test_recent_fill_carries_fee(store):
    FillRepository(store).add_if_new(Fill(trade_id='t1', grid_id='g1', line_index=0,
                                          side='buy', price=90.0, size=1.0, fee=0.27, ts=5000))
    dto = build_records(store)
    assert dto.recent_fills[0].fee == 0.27


def test_overview_row_carries_cumulative_fee(store):
    GridRepository(store).create(Grid(id='g1', exchange='hyperliquid',
                                      symbol='BTC/USDT:USDT', status=ACTIVE))
    accs = AccountingRepository(store)
    accs.init('g1')
    acc = accs.get('g1')
    acc.fee_paid = 1.23
    accs.save(acc)
    rows = build_overview(store, _PriceAdapter())
    assert rows[0].fee_paid == 1.23
