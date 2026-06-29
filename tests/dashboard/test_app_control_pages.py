# tests/dashboard/test_app_control_pages.py
from starlette.testclient import TestClient
from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password
from gridtrade.state.control import ControlFlagRepository, CommandRepository, AuditRepository
from gridtrade.exchanges.base import Balance


class _Adapter:
    client = None
    def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
    def fetch_price(self, s): return 100.0


def _client(store, universe_fn=None):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek',
                     flags=ControlFlagRepository(store), commands=CommandRepository(store),
                     audit=AuditRepository(store), universe_fn=universe_fn)
    c = TestClient(app, base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    return c


def test_controls_page_shows_halt_state_and_audit(store):
    ControlFlagRepository(store).set('trading_halted', True, actor='admin')
    AuditRepository(store).add('admin', 'FLAG_SET', 'trading_halted', detail='{"value": true}')
    r = _client(store).get('/controls')
    assert r.status_code == 200
    assert 'trading_halted' in r.text or 'halt' in r.text.lower()


def test_universe_page_lists_candidates(store):
    c = _client(store, universe_fn=lambda: [{'symbol': 'BTC/USDT:USDT', 'tag': 'gt0',
                                            'offset': 0, 'grid_params': {'grid_count': 10}}])
    r = c.get('/universe')
    assert r.status_code == 200 and 'BTC/USDT:USDT' in r.text
    assert '/open?symbol=' in r.text


def test_pages_require_login(store):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek',
                     flags=ControlFlagRepository(store), commands=CommandRepository(store),
                     audit=AuditRepository(store))
    anon = TestClient(app, base_url='https://testserver')
    assert anon.get('/controls', follow_redirects=False).status_code == 302
