import json
import pytest
from gridtrade.runtime.commands import execute_command
from gridtrade.state.models import ControlCommand


class _Grid:
    def __init__(self, gid, symbol): self.id = gid; self.symbol = symbol; self.status = 'ACTIVE'


class _Grids:
    def __init__(self, grids): self._g = grids
    def list_active(self): return self._g


class _Executor:
    def __init__(self, grids=()):
        self.grids = _Grids(list(grids))
        self.closed = []; self.opened = []
        self.fail_on = set()
    def close(self, gid, symbol, reason):
        if gid in self.fail_on: raise RuntimeError('boom %s' % gid)
        self.closed.append((gid, symbol, reason))
    def open(self, exchange, symbol, params, *, offset=0, tag='', cap=None):
        self.opened.append((symbol, tag, cap)); return 'newgrid'


class _Manager:
    def __init__(self, executor): self.executor = executor


class _Flags:
    def __init__(self, halted=False): self._h = halted
    def get(self, name): return self._h if name == 'trading_halted' else False


def test_close_grid_calls_executor_close():
    ex = _Executor()
    cmd = ControlCommand(id='c1', type='CLOSE_GRID',
                         payload=json.dumps({'grid_id': 'g1', 'symbol': 'BTC/USDT:USDT', 'reason': 'manual'}))
    out = execute_command(cmd, _Manager(ex), _Flags(), exchange='hyperliquid')
    assert ex.closed == [('g1', 'BTC/USDT:USDT', 'manual')]
    assert 'g1' in out


def test_open_grid_refused_when_halted():
    ex = _Executor()
    cmd = ControlCommand(id='c2', type='OPEN_GRID',
                         payload=json.dumps({'symbol': 'BTC/USDT:USDT', 'params': {}, 'tag': 'gt0', 'offset': 0}))
    with pytest.raises(RuntimeError):
        execute_command(cmd, _Manager(ex), _Flags(halted=True), exchange='hyperliquid')
    assert ex.opened == []


def test_open_grid_passes_cap_override():
    ex = _Executor()
    cmd = ControlCommand(id='c3', type='OPEN_GRID',
                         payload=json.dumps({'symbol': 'ETH/USDT:USDT', 'params': {'low_price': 1},
                                             'tag': 'gt0', 'offset': 0, 'cap': 250.0}))
    out = execute_command(cmd, _Manager(ex), _Flags(), exchange='hyperliquid')
    assert ex.opened == [('ETH/USDT:USDT', 'gt0', 250.0)]
    assert 'ETH/USDT:USDT' in out


def test_panic_closes_all_with_isolation():
    ex = _Executor([_Grid('g1', 'BTC/USDT:USDT'), _Grid('g2', 'ETH/USDT:USDT')])
    ex.fail_on = {'g2'}
    cmd = ControlCommand(id='c4', type='PANIC_CLOSE_ALL', payload='{"reason": "panic"}')
    out = execute_command(cmd, _Manager(ex), _Flags(), exchange='hyperliquid')
    assert ('g1', 'BTC/USDT:USDT', 'panic') in ex.closed     # 健康网格照平
    assert 'failed' in out and 'g2' in out                    # 坏网格记入摘要、不中断
