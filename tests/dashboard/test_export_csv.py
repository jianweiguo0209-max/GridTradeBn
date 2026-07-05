# tests/dashboard/test_export_csv.py
import csv
import io
from datetime import datetime, timezone

from starlette.testclient import TestClient

from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.fills import FillRepository
from gridtrade.state.grids import GridRepository
from gridtrade.state.models import CLOSED, Fill, Grid, Record
from gridtrade.state.records import RecordRepository
from gridtrade.exchanges.base import Balance


class _Adapter:
    client = None
    def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
    def fetch_price(self, s): return 100.0


def _app(store):
    return create_app(store, _Adapter(), username='admin',
                      password_hash=hash_password('pw', iterations=1000), session_secret='sek')


def _client(store):
    c = TestClient(_app(store), base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    return c


def _ms(y, m, d, hh=0, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)


def _seed(store):
    """g1 在窗口 [2026-06-01, 2026-06-30] 内关闭；g2 在窗口外（7 月）关闭。"""
    gr = GridRepository(store)
    rr = RecordRepository(store)
    ar = AccountingRepository(store)
    fr = FillRepository(store)

    gr.create(Grid(id='g1', exchange='hl', symbol='ETH/USDC:USDC', status=CLOSED,
                   tag='gt01', direction='neutral', offset=3, entry_price=100.0,
                   low_price=90.0, high_price=110.0, grid_count=10,
                   order_num=0.5, leverage=5.0, cap=100.0,
                   stop_low_price=80.0, stop_high_price=120.0,
                   created_at=_ms(2026, 6, 15, 0)))
    rr.add(Record(id='r1', grid_id='g1', exchange='hl', symbol='ETH/USDC:USDC',
                  tag='gt01', offset=3, opened_at=_ms(2026, 6, 15, 0),
                  closed_at=_ms(2026, 6, 15, 12), sz=100.0,
                  total_pnl=2.5, pnl_ratio=0.025, exit_reason='stop_high'))
    ar.init('g1')
    acc = ar.get('g1')
    acc.realized_pnl = 3.0
    acc.fee_paid = 0.4
    acc.funding_paid = 0.1
    acc.net_position = -0.5
    acc.avg_price = 104.0
    acc.pnl_ratio_max = 0.03
    ar.save(acc)
    fr.add_if_new(Fill(trade_id='t1', grid_id='g1', line_index=4, side='buy',
                       price=98.0, size=0.5, fee=0.02, ts=_ms(2026, 6, 15, 1)))
    fr.add_if_new(Fill(trade_id='t2', grid_id='g1', line_index=5, side='buy',
                       price=100.0, size=0.5, fee=0.02, ts=_ms(2026, 6, 15, 2)))
    fr.add_if_new(Fill(trade_id='t3', grid_id='g1', line_index=5, side='sell',
                       price=102.0, size=0.5, fee=0.02, ts=_ms(2026, 6, 15, 3)))

    gr.create(Grid(id='g2', exchange='hl', symbol='OP/USDC:USDC', status=CLOSED,
                   tag='gt02', low_price=1.0, high_price=2.0, grid_count=10,
                   created_at=_ms(2026, 7, 1, 0)))
    rr.add(Record(id='r2', grid_id='g2', exchange='hl', symbol='OP/USDC:USDC',
                  tag='gt02', opened_at=_ms(2026, 7, 1, 0), closed_at=_ms(2026, 7, 2, 0),
                  total_pnl=-1.0, pnl_ratio=-0.01, exit_reason='stop_low'))
    fr.add_if_new(Fill(trade_id='t9', grid_id='g2', line_index=1, side='buy',
                       price=1.5, size=10.0, fee=0.01, ts=_ms(2026, 7, 1, 1)))


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


GRID_URL = '/analytics/export/grids.csv?start=2026-06-01&end=2026-06-30'
FILL_URL = '/analytics/export/fills.csv?start=2026-06-01&end=2026-06-30'


def test_export_requires_login(store):
    anon = TestClient(_app(store), base_url='https://testserver')
    for url in (GRID_URL, FILL_URL):
        r = anon.get(url, follow_redirects=False)
        assert r.status_code == 302 and r.headers['location'].endswith('/login')


def test_grids_csv_row_values(store):
    _seed(store)
    r = _client(store).get(GRID_URL)
    assert r.status_code == 200
    assert 'text/csv' in r.headers['content-type']
    assert 'grids_2026-06-01_2026-06-30.csv' in r.headers['content-disposition']
    rows = _rows(r.text)
    assert len(rows) == 1                      # 窗口外的 g2 不出现
    g = rows[0]
    assert g['grid_id'] == 'g1' and g['symbol'] == 'ETH/USDC:USDC'
    assert g['status'] == 'CLOSED' and g['tag'] == 'gt01' and g['offset'] == '3'
    assert g['opened_at_utc'] == '2026-06-15T00:00:00Z'
    assert g['closed_at_utc'] == '2026-06-15T12:00:00Z'
    assert float(g['duration_hours']) == 12.0
    assert float(g['grid_step']) == 2.0        # (110-90)/10
    assert g['exit_reason'] == 'stop_high'
    assert float(g['total_pnl']) == 2.5 and float(g['pnl_ratio']) == 0.025
    assert float(g['realized_pnl']) == 3.0 and float(g['fee_paid']) == 0.4
    assert float(g['net_pnl']) == 2.5          # 3.0 - 0.4 - 0.1
    assert float(g['net_position']) == -0.5 and float(g['pnl_ratio_max']) == 0.03
    assert g['fills_count'] == '3' and g['buy_fills'] == '2' and g['sell_fills'] == '1'
    assert float(g['filled_volume']) == 1.5
    assert float(g['filled_notional']) == 150.0     # 49+50+51
    assert g['lines_touched'] == '2'                # line 4 和 5
    assert float(g['avg_buy_price']) == 99.0        # (98*0.5+100*0.5)/1.0
    assert float(g['avg_sell_price']) == 102.0
    assert float(g['fills_per_hour']) == 0.25       # 3 / 12h
    assert g['first_fill_utc'] == '2026-06-15T01:00:00Z'
    assert g['last_fill_utc'] == '2026-06-15T03:00:00Z'


def test_grids_csv_empty_window_has_header_only(store):
    _seed(store)
    r = _client(store).get('/analytics/export/grids.csv?start=2025-01-01&end=2025-01-31')
    assert r.status_code == 200
    lines = [l for l in r.text.splitlines() if l.strip()]
    assert len(lines) == 1 and lines[0].startswith('grid_id,')


def test_fills_csv_scoped_to_window_grids(store):
    _seed(store)
    r = _client(store).get(FILL_URL)
    assert r.status_code == 200
    assert 'fills_2026-06-01_2026-06-30.csv' in r.headers['content-disposition']
    rows = _rows(r.text)
    assert len(rows) == 3                      # 只有 g1 的成交，g2 的 t9 不出现
    assert {x['grid_id'] for x in rows} == {'g1'}
    f = rows[0]
    assert f['trade_id'] == 't1' and f['side'] == 'buy'
    assert f['ts_utc'] == '2026-06-15T01:00:00Z'
    assert float(f['notional']) == 49.0        # 98*0.5
    assert f['symbol'] == 'ETH/USDC:USDC' and f['tag'] == 'gt01'


def test_export_bad_params_400(store):
    c = _client(store)
    for url in ('/analytics/export/grids.csv',                          # 缺参数
                '/analytics/export/grids.csv?start=06-01&end=2026-06-30',  # 格式错
                '/analytics/export/grids.csv?start=2026-06-30&end=2026-06-01',  # 倒置
                '/analytics/export/fills.csv?start=x&end=y'):
        assert c.get(url).status_code == 400, url


def test_analytics_page_has_export_form(store):
    r = _client(store).get('/analytics')
    assert r.status_code == 200
    assert '/analytics/export/grids.csv' in r.text
    assert '/analytics/export/fills.csv' in r.text
    assert 'type="date"' in r.text
