# tests/dashboard/test_app_analytics.py
from starlette.testclient import TestClient
from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password
from gridtrade.state.records import RecordRepository
from gridtrade.state.models import Record
from gridtrade.exchanges.base import Balance


class _Adapter:
    client = None
    def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
    def fetch_price(self, s): return 100.0


def _client(store):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek')
    c = TestClient(app, base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    return c


def test_analytics_requires_login(store):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek')
    anon = TestClient(app, base_url='https://testserver')
    r = anon.get('/analytics', follow_redirects=False)
    assert r.status_code == 302 and r.headers['location'].endswith('/login')


def test_analytics_renders_with_data(store):
    RecordRepository(store).add(Record(id='r1', exchange='x', symbol='BTC', tag='gt0',
                                       total_pnl=10.0, exit_reason='take_profit', closed_at=1000))
    r = _client(store).get('/analytics')
    assert r.status_code == 200
    assert '<svg' in r.text            # 图表渲染
    assert 'gt0' in r.text             # tag 归因表
