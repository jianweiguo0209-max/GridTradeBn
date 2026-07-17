import json
from gridtrade.runtime.commands import consume_one
from gridtrade.state.control import CommandRepository, AuditRepository
from gridtrade.state.models import CMD_DONE, CMD_FAILED


class _Grids:
    def list_active(self): return []
class _Executor:
    def __init__(self): self.grids = _Grids(); self.closed = []; self.synced = []
    def is_loaded(self, gid): return True
    def sync(self, gid, symbol, *, skip_replenish=False): self.synced.append(gid)
    def close(self, gid, symbol, reason): self.closed.append(gid)
class _Manager:
    def __init__(self): self.executor = _Executor()
class _Flags:
    def get(self, name): return False


def test_consume_one_success_marks_done_and_audits(store):
    cmds = CommandRepository(store); audit = AuditRepository(store)
    c = cmds.enqueue('CLOSE_GRID', json.dumps({'grid_id': 'g1', 'symbol': 'BTC/USDT:USDT'}),
                     created_by='admin')
    cid = consume_one(cmds, audit, _Manager(), _Flags(), exchange='hyperliquid')
    assert cid == c.id
    assert cmds.get(c.id).status == CMD_DONE
    assert any(a.action == 'CMD_RESULT' and a.outcome == 'ok' for a in audit.list_recent())


def test_consume_one_failure_marks_failed(store):
    cmds = CommandRepository(store); audit = AuditRepository(store)
    c = cmds.enqueue('OPEN_GRID', json.dumps({'symbol': 'X', 'params': {}, 'tag': 't'}),
                     created_by='admin')

    class _HaltFlags:
        def get(self, name): return name == 'trading_halted'
    consume_one(cmds, audit, _Manager(), _HaltFlags(), exchange='hyperliquid')
    got = cmds.get(c.id)
    assert got.status == CMD_FAILED and 'halted' in (got.result or '').lower()
    assert any(a.outcome == 'fail' for a in audit.list_recent())


def test_consume_one_returns_none_when_empty(store):
    assert consume_one(CommandRepository(store), AuditRepository(store),
                       _Manager(), _Flags(), exchange='hyperliquid') is None
