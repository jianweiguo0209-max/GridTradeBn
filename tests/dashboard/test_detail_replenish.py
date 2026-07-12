# tests/dashboard/test_detail_replenish.py — 网格详情页补单记录小节
"""补单判别:初始批次 client_oid seq==line(开格循环生成);sync 补对侧/E2 兜底补挂
seq 为后续计数或 restore 高位(10M+) → seq!=line 即补单(memory quantized-size-fallback-bug
修复后补单成为高频可见事件,用户要求详情页可见,2026-07-13)。"""
from gridtrade.dashboard.queries import build_grid_detail, is_replenish_order
from gridtrade.state.grids import GridRepository
from gridtrade.state.orders import OrderRepository
from gridtrade.state.models import Grid, GridOrder


def _mk(store):
    g = GridRepository(store).create(grid=Grid(id='', exchange='x', symbol='BTC/USDC:USDC',
                                               status='ACTIVE', offset=0, tag='gt0'))
    o = OrderRepository(store)
    o.upsert(GridOrder(client_oid='%s:3:3' % g.id, grid_id=g.id, line_index=3,
                       side='buy', price=99.0, size=1.0, status='open'))
    o.upsert(GridOrder(client_oid='%s:4:10000001' % g.id, grid_id=g.id, line_index=4,
                       side='sell', price=100.0, size=1.0, status='open'))
    o.upsert(GridOrder(client_oid='%s:3:12' % g.id, grid_id=g.id, line_index=3,
                       side='buy', price=99.0, size=1.0, status='closed'))
    return g


def test_is_replenish_order_classification(store):
    g = _mk(store)
    rows = {o.client_oid: o for o in OrderRepository(store).list_by_grid(g.id)}
    assert not is_replenish_order(rows['%s:3:3' % g.id])          # 初始:seq==line
    assert is_replenish_order(rows['%s:4:10000001' % g.id])       # restore 高位补单
    assert is_replenish_order(rows['%s:3:12' % g.id])             # 进程内补单


def test_detail_dto_and_page_show_replenishments(store):
    g = _mk(store)
    dto = build_grid_detail(store, g.id)
    assert len(dto.replenishments) == 2
    assert all(is_replenish_order(o) for o in dto.replenishments)

    from starlette.testclient import TestClient
    from gridtrade.dashboard.app import create_app
    from gridtrade.dashboard.auth import hash_password
    from gridtrade.exchanges.base import Balance

    class _A:
        client = None
        def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
        def fetch_price(self, s): return 100.0

    app = create_app(store, _A(), username='u',
                     password_hash=hash_password('p', iterations=1000), session_secret='s')
    c = TestClient(app, base_url='https://t')
    c.post('/login', data={'username': 'u', 'password': 'p'})
    html = c.get('/grid/%s' % g.id).text
    assert '补单记录' in html
    assert html.count(':10000001') == 0                            # 不泄漏 oid,展示业务字段
    assert '暂无补单' not in html                                  # 有记录时不显示空态
