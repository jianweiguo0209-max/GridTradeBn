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
        self.calls = []                      # 有序调用日志(校验 sync 先于 close)
        self.opened = []
        self.fail_on = set()
        self.loaded = True
    def is_loaded(self, gid): return self.loaded
    def sync(self, gid, symbol, *, skip_replenish=False):
        self.calls.append(('sync', gid, symbol))
    def close(self, gid, symbol, reason):
        if gid in self.fail_on: raise RuntimeError('boom %s' % gid)
        self.calls.append(('close', gid, symbol, reason))
    def open(self, exchange, symbol, params, *, offset=0, tag='', cap=None):
        self.opened.append((symbol, tag, cap)); return 'newgrid'
    @property
    def closed(self):                        # 兼容既有断言:仅 close 调用
        return [(c[1], c[2], c[3]) for c in self.calls if c[0] == 'close']


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


def test_close_grid_syncs_before_close(store=None):
    """mode4:命令平仓前先 sync(摄入最新成交),再 close——防陈旧账本减错量留孤儿。且 sync 严格先于 close。"""
    ex = _Executor()
    cmd = ControlCommand(id='c1', type='CLOSE_GRID',
                         payload=json.dumps({'grid_id': 'g1', 'symbol': 'BTC/USDT:USDT', 'reason': 'manual'}))
    execute_command(cmd, _Manager(ex), _Flags(), exchange='fake')
    assert ex.calls == [('sync', 'g1', 'BTC/USDT:USDT'),
                        ('close', 'g1', 'BTC/USDT:USDT', 'manual')]   # sync 严格先于 close


def test_close_grid_skips_sync_when_not_loaded():
    """未 loaded(他进程开的/未监控)→ 跳过 sync(无内存态可摄入),仍 close(用持久态)。"""
    ex = _Executor(); ex.loaded = False
    cmd = ControlCommand(id='c1', type='CLOSE_GRID',
                         payload=json.dumps({'grid_id': 'g1', 'symbol': 'BTC/USDT:USDT', 'reason': 'manual'}))
    execute_command(cmd, _Manager(ex), _Flags(), exchange='fake')
    assert ex.calls == [('close', 'g1', 'BTC/USDT:USDT', 'manual')]   # 无 sync


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
