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


def test_analytics_charts_have_legend(store):
    RecordRepository(store).add(Record(id='r9', exchange='x', symbol='BTC', tag='gt0',
                                       total_pnl=5.0, exit_reason='take_profit', closed_at=1000))
    r = _client(store).get('/analytics')
    assert r.status_code == 200
    assert '已实现' in r.text                 # 已实现盈亏曲线图例
    assert '真权益' not in r.text             # 权益线已取消（2026-07-11 用户定）


def _day_ms(day: str) -> int:
    from gridtrade.dashboard.export_csv import parse_day_ms
    return parse_day_ms(day)


def test_analytics_date_range_filters_all_sections(store):
    repo = RecordRepository(store)
    repo.add(Record(id='in1', exchange='x', symbol='BTC', tag='gtIN',
                    total_pnl=5.0, exit_reason='take_profit',
                    closed_at=_day_ms('2026-07-05') + 1000))
    repo.add(Record(id='out1', exchange='x', symbol='ETH', tag='gtOUT',
                    total_pnl=3.0, exit_reason='take_profit',
                    closed_at=_day_ms('2026-07-20') + 1000))
    c = _client(store)
    r = c.get('/analytics', params={'start': '2026-07-01', 'end': '2026-07-10'})
    assert r.status_code == 200
    assert 'gtIN' in r.text and 'gtOUT' not in r.text   # 范围外记录不进归因表
    assert '2026-07-01 ~ 2026-07-10' in r.text          # 当前范围标注
    r_all = c.get('/analytics')                          # 无日期参数 → 预设周期行为不变
    assert 'gtIN' in r_all.text and 'gtOUT' in r_all.text


def test_analytics_date_range_open_ended_and_invalid(store):
    RecordRepository(store).add(Record(id='r2', exchange='x', symbol='BTC', tag='gt0',
                                       total_pnl=1.0, exit_reason='take_profit',
                                       closed_at=_day_ms('2026-07-05')))
    c = _client(store)
    assert c.get('/analytics', params={'start': '2026-07-01'}).status_code == 200   # 只填起
    assert c.get('/analytics', params={'end': '2026-07-10'}).status_code == 200     # 只填止
    assert c.get('/analytics', params={'start': 'bogus'}).status_code == 400        # 非法格式
    assert c.get('/analytics', params={'start': '2026-07-10',
                                       'end': '2026-07-01'}).status_code == 400     # 起晚于止
