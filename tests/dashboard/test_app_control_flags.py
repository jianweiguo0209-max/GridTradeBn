# tests/dashboard/test_app_control_flags.py
from starlette.testclient import TestClient
from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password
from gridtrade.state.control import ControlFlagRepository, CommandRepository, AuditRepository
from gridtrade.exchanges.base import Balance


class _Adapter:
    client = None
    def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
    def fetch_price(self, s): return 100.0


def _client(store):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000),
                     session_secret='sek',
                     flags=ControlFlagRepository(store),
                     commands=CommandRepository(store),
                     audit=AuditRepository(store))
    c = TestClient(app, base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    return c


def test_halt_sets_flag_and_audits(store):
    c = _client(store)
    r = c.post('/control/halt', data={'action': 'on'}, follow_redirects=False)
    assert r.status_code == 302
    assert ControlFlagRepository(store).get('trading_halted') is True
    assert any(a.action == 'FLAG_SET' for a in AuditRepository(store).list_recent())


def test_panic_requires_confirm_and_enqueues(store):
    c = _client(store)
    bad = c.post('/control/panic', data={'confirm': 'nope'}, follow_redirects=False)
    assert CommandRepository(store).list_recent() == []          # 确认词不对 → 不入队
    ok = c.post('/control/panic', data={'confirm': 'PANIC'}, follow_redirects=False)
    assert ok.status_code == 302
    assert ControlFlagRepository(store).get('trading_halted') is True
    cmds = CommandRepository(store).list_recent()
    assert len(cmds) == 1 and cmds[0].type == 'PANIC_CLOSE_ALL'


def test_control_routes_require_login(store):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek',
                     flags=ControlFlagRepository(store), commands=CommandRepository(store),
                     audit=AuditRepository(store))
    anon = TestClient(app, base_url='https://testserver')
    r = anon.post('/control/halt', data={'action': 'on'}, follow_redirects=False)
    assert r.status_code == 302 and r.headers['location'].endswith('/login')
