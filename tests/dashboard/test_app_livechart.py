# tests/dashboard/test_app_livechart.py
from starlette.testclient import TestClient
from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password
from gridtrade.state.grids import GridRepository
from gridtrade.state.models import Grid, ACTIVE
from gridtrade.exchanges.base import Balance


class _Adapter:
    client = None
    def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
    def fetch_price(self, s): return 100.0
    def fetch_ohlcv(self, s, tf, a, b):
        import pandas as pd
        return pd.DataFrame()                      # 空 K 线 → 降级，仍 200


def _app(store):
    return create_app(store, _Adapter(), username='admin',
                      password_hash=hash_password('pw', iterations=1000), session_secret='sek')


def _seed(store):
    GridRepository(store).create(Grid(id='g1', exchange='x', symbol='BTC/USDT:USDT',
                                      status=ACTIVE, created_at=1000,
                                      low_price=90.0, high_price=110.0, grid_count=10,
                                      stop_low_price=80.0, stop_high_price=120.0,
                                      cap=100.0, leverage=5.0, entry_price=100.0))


def test_chart_requires_login(store):
    _seed(store)
    anon = TestClient(_app(store), base_url='https://testserver')
    r = anon.get('/grid/g1/chart', follow_redirects=False)
    assert r.status_code == 302 and r.headers['location'].endswith('/login')


def test_chart_returns_svg_fragment(store):
    _seed(store)
    c = TestClient(_app(store), base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    r = c.get('/grid/g1/chart?window=6h')
    assert r.status_code == 200 and '<svg' in r.text


def test_chart_missing_grid_404(store):
    c = TestClient(_app(store), base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    assert c.get('/grid/nope/chart').status_code == 404


def test_detail_page_has_livechart_manual_refresh(store):
    _seed(store)
    c = TestClient(_app(store), base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    html = c.get('/grid/g1').text
    assert 'id="livechart"' in html
    assert '/grid/g1/chart' in html
    assert 'setInterval' not in html                 # 不自动轮询
    assert 'id="chart-refresh"' in html              # 手动刷新按钮
