# tests/dashboard/test_app.py
import pytest
from starlette.testclient import TestClient

from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password, LoginThrottle
from gridtrade.state.grids import GridRepository
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.models import Grid, ACTIVE
from gridtrade.exchanges.base import Balance


class _Adapter:
    client = None

    def fetch_balance(self):
        return Balance(equity=499.0, cash=400.0)

    def fetch_price(self, symbol):
        return 100.0


def _client(store, throttle=None):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000),
                     session_secret='sekret', throttle=throttle)
    # base_url must be https:// so the Secure cookie set by POST /login
    # is retained by the test client (httpx drops Secure cookies over http://)
    return TestClient(app, base_url='https://testserver')


def test_unauthenticated_redirects_to_login(store):
    c = _client(store)
    r = c.get('/', follow_redirects=False)
    assert r.status_code == 302
    assert r.headers['location'].endswith('/login')


def test_login_then_overview_shows_grid(store):
    GridRepository(store).create(Grid(id='g1', exchange='hyperliquid',
                                      symbol='BTC/USDT:USDT', status=ACTIVE))
    AccountingRepository(store).init('g1')
    c = _client(store)
    r = c.post('/login', data={'username': 'admin', 'password': 'pw'},
               follow_redirects=False)
    assert r.status_code == 302
    home = c.get('/')
    assert home.status_code == 200
    assert 'BTC/USDT:USDT' in home.text


def test_wrong_password_then_lockout(store):
    thr = LoginThrottle(max_attempts=3, lockout_sec=3600, now_fn=lambda: 1000.0)
    c = _client(store, throttle=thr)
    for _ in range(3):
        bad = c.post('/login', data={'username': 'admin', 'password': 'nope'})
        assert 'error' in bad.text.lower() or bad.status_code == 401
    # 锁定后，即使密码正确也拒
    locked = c.post('/login', data={'username': 'admin', 'password': 'pw'})
    assert 'locked' in locked.text.lower() or locked.status_code == 429
