"""控制指令执行分发：CLOSE_GRID / OPEN_GRID / PANIC_CLOSE_ALL。只在 monitor 调用。"""
import json

from gridtrade.state.models import ACTIVE_STATES


def execute_command(cmd, manager, flags, *, exchange: str) -> str:
    ex = manager.executor
    p = json.loads(cmd.payload or '{}')
    if cmd.type == 'CLOSE_GRID':
        ex.close(p['grid_id'], p['symbol'], p.get('reason', 'manual'))
        return 'closed %s' % p['grid_id']
    if cmd.type == 'OPEN_GRID':
        if flags.get('trading_halted'):
            raise RuntimeError('trading halted: OPEN refused')
        gid = ex.open(exchange, p['symbol'], p['params'],
                      offset=int(p.get('offset', 0)), tag=p.get('tag', ''),
                      cap=p.get('cap'))
        return 'opened %s -> %s' % (p['symbol'], gid)
    if cmd.type == 'PANIC_CLOSE_ALL':
        active = [g for g in ex.grids.list_active() if g.status in ACTIVE_STATES]
        ok, failed = [], []
        for g in active:
            try:
                ex.close(g.id, g.symbol, 'panic')
                ok.append(g.id)
            except Exception as exc:                 # per-grid 隔离，不中断其他
                failed.append('%s:%r' % (g.id, exc))
        msg = 'panic closed %d ok' % len(ok)
        if failed:
            msg += ', %d failed: %s' % (len(failed), '; '.join(failed))
        return msg
    raise ValueError('unknown command type: %s' % cmd.type)
