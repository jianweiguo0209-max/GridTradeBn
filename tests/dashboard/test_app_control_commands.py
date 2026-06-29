import json
from starlette.testclient import TestClient
from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password
from gridtrade.state.control import ControlFlagRepository, CommandRepository, AuditRepository
from gridtrade.exchanges.base import Balance


class _Adapter:
    client = None
    def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
    def fetch_price(self, s): return 100.0


def _client(store, compute_fn=None):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek',
                     flags=ControlFlagRepository(store), commands=CommandRepository(store),
                     audit=AuditRepository(store), compute_fn=compute_fn)
    c = TestClient(app, base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    return c


def test_close_enqueues_close_grid(store):
    c = _client(store)
    r = c.post('/control/close',
               data={'grid_id': 'g1', 'symbol': 'BTC/USDT:USDT', 'reason': 'manual'},
               follow_redirects=False)
    assert r.status_code == 302
    cmds = CommandRepository(store).list_recent()
    assert len(cmds) == 1 and cmds[0].type == 'CLOSE_GRID'
    assert json.loads(cmds[0].payload)['grid_id'] == 'g1'


def test_open_form_prefills_defaults(store):
    c = _client(store, compute_fn=lambda symbol: {
        'symbol': symbol, 'tag': 'gt0', 'offset': 0,
        'grid_params': {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 10,
                        'stop_low_price': 80.0, 'stop_high_price': 120.0}})
    r = c.get('/open?symbol=BTC/USDT:USDT')
    assert r.status_code == 200
    assert '110' in r.text and 'BTC/USDT:USDT' in r.text


def test_open_post_enqueues_open_grid_with_overridden_cap(store):
    c = _client(store)
    r = c.post('/open', data={'symbol': 'ETH/USDT:USDT', 'low_price': '1', 'high_price': '2',
                              'grid_count': '8', 'stop_low_price': '0.8', 'stop_high_price': '2.2',
                              'cap': '250', 'tag': 'gt0', 'offset': '0'},
               follow_redirects=False)
    assert r.status_code == 302
    cmd = CommandRepository(store).list_recent()[0]
    assert cmd.type == 'OPEN_GRID'
    p = json.loads(cmd.payload)
    assert p['symbol'] == 'ETH/USDT:USDT' and p['cap'] == 250.0
    assert p['params']['grid_count'] == 8
