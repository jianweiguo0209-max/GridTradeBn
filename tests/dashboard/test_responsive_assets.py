from pathlib import Path
from starlette.testclient import TestClient
from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password
from gridtrade.exchanges.base import Balance

_DIR = Path(__file__).resolve().parents[2] / 'gridtrade' / 'dashboard'


class _Adapter:
    client = None
    def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
    def fetch_price(self, s): return 100.0


def test_css_has_media_query_and_danger():
    css = (_DIR / 'static' / 'app.css').read_text()
    assert '@media' in css
    assert '.danger' in css


def test_base_has_viewport_meta(store):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek')
    c = TestClient(app, base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    html = c.get('/').text
    assert 'name="viewport"' in html and 'width=device-width' in html
