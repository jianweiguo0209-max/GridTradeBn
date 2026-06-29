from gridtrade.state.models import GridOrder


def _repo(store):
    from gridtrade.state.orders import OrderRepository
    return OrderRepository(store)


def _order(**kw):
    base = dict(client_oid='g1:0', grid_id='g1', line_index=0, side='buy',
                price=100.0, size=1.0, status='open')
    base.update(kw)
    return GridOrder(**base)


def test_upsert_insert_then_get(store):
    repo = _repo(store)
    o = repo.upsert(_order())
    assert o.created_at > 0 and o.updated_at > 0
    got = repo.get('g1:0')
    assert got.grid_id == 'g1' and got.line_index == 0 and got.status == 'open'


def test_upsert_updates_status_preserves_created_at(store):
    repo = _repo(store)
    first = repo.upsert(_order())
    updated = repo.upsert(_order(status='closed', exchange_order_id='X7'))
    assert updated.status == 'closed' and updated.exchange_order_id == 'X7'
    assert updated.created_at == first.created_at
    assert updated.updated_at >= first.updated_at


def test_list_by_grid_and_open_filter(store):
    repo = _repo(store)
    repo.upsert(_order(client_oid='g1:0', line_index=0))
    repo.upsert(_order(client_oid='g1:1', line_index=1, side='sell', status='closed'))
    repo.upsert(_order(client_oid='g2:0', grid_id='g2', line_index=0))
    all_g1 = repo.list_by_grid('g1')
    assert {o.client_oid for o in all_g1} == {'g1:0', 'g1:1'}
    open_g1 = repo.list_open_by_grid('g1')
    assert {o.client_oid for o in open_g1} == {'g1:0'}


def test_get_missing_returns_none(store):
    assert _repo(store).get('nope') is None
