from gridtrade.state.control import AuditRepository


def test_audit_add_and_list_recent(store):
    a = AuditRepository(store)
    a.add('admin', 'FLAG_SET', 'trading_halted', detail='{"value": true}')
    a.add('admin', 'CMD_SUBMIT', 'cmd1', detail='{"type": "CLOSE_GRID"}')
    rows = a.list_recent()
    assert len(rows) == 2
    assert rows[0].action in ('FLAG_SET', 'CMD_SUBMIT')   # 降序，两条都在
    assert all(r.actor == 'admin' and r.ts > 0 for r in rows)
