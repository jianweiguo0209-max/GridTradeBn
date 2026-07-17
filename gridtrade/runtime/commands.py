"""控制指令执行分发：CLOSE_GRID / OPEN_GRID / PANIC_CLOSE_ALL / RESOLVE_INTERVENTION。
只在 monitor 调用。外部干预熔断币(spec 2026-07-12 组件三)拒绝一切交易所写入指令——
账本已与交易所背离,盲动更险;先 RESOLVE_INTERVENTION(dashboard 按钮)再操作。"""
import json
from typing import Optional

from gridtrade.state.models import ACTIVE_STATES, CMD_DONE, CMD_FAILED

INTERVENTION_PREFIX = 'intervention:'   # 单源:cycles/scheduler/dashboard 从此处 import


def execute_command(cmd, manager, flags, *, exchange: str) -> str:
    def _braked(symbol):
        return bool(flags.get(INTERVENTION_PREFIX + symbol))

    ex = manager.executor
    p = json.loads(cmd.payload or '{}')
    if cmd.type == 'CLOSE_GRID':
        if _braked(p['symbol']):
            raise RuntimeError('intervention braked %s: CLOSE refused, resolve first'
                               % p['symbol'])
        # mode4:命令平仓补 sync-before(监控轮有此不变量,指令路径此前绕过)——先摄入最新成交,
        # 否则近 5s 未入账的成交会让 close 用陈旧 claim 减错量、漏平留孤儿(与并发成交竞态)。
        if ex.is_loaded(p['grid_id']):
            ex.sync(p['grid_id'], p['symbol'], skip_replenish=True)
        ex.close(p['grid_id'], p['symbol'], p.get('reason', 'manual'))
        return 'closed %s' % p['grid_id']
    if cmd.type == 'OPEN_GRID':
        # 注:手动开仓直调 ex.open、不经 list_instruments/_include_market 的 COIN 过滤——
        # 手动开 TradFi 代币化永续仍可下单。账户快照三路读并非均漏:仓位
        # (fetch_positions_all 按 to_canonical+want 过滤全账户持仓)与价格
        # (fetch_prices_all 批量未命中时逐币回退 fetch_price)均不依赖 _id_map,照常可见;
        # 唯资金费(fetch_funding_payments_all)靠 _id_map 把原生 symbol 换回 canonical 才能
        # 归位——_id_map 已剔除该品类,income 行匹配不到任何 want 键且无回退,交易所实扣
        # 资金费在该格账本里静默永久丢失(半碎:资金费史/PnL 失真)。此为有意取舍
        # (spec 2026-07-15 §4.2):作为对"手动玩 TradFi"的隐性劝阻,非 bug。
        if flags.get('trading_halted'):
            raise RuntimeError('trading halted: OPEN refused')
        if _braked(p['symbol']):
            raise RuntimeError('intervention braked %s: OPEN refused, resolve first'
                               % p['symbol'])
        gid = ex.open(exchange, p['symbol'], p['params'],
                      offset=int(p.get('offset', 0)), tag=p.get('tag', ''),
                      cap=p.get('cap'))
        return 'opened %s -> %s' % (p['symbol'], gid)
    if cmd.type == 'PANIC_CLOSE_ALL':
        active = [g for g in ex.grids.list_active() if g.status in ACTIVE_STATES]
        ok, failed, skipped = [], [], []
        for g in active:
            if _braked(g.symbol):                    # 熔断币账本不可信,盲平更险 → 跳过并上报
                skipped.append(g.id)
                continue
            try:
                if ex.is_loaded(g.id):               # mode4:平仓前补 sync-before(同 CLOSE_GRID)
                    ex.sync(g.id, g.symbol, skip_replenish=True)
                ex.close(g.id, g.symbol, 'panic')
                ok.append(g.id)
            except Exception as exc:                 # per-grid 隔离，不中断其他
                failed.append('%s:%r' % (g.id, exc))
        msg = 'panic closed %d ok' % len(ok)
        if failed:
            msg += ', %d failed: %s' % (len(failed), '; '.join(failed))
        if skipped:
            msg += ', %d braked-skipped: %s' % (len(skipped), '; '.join(skipped))
        return msg
    if cmd.type == 'RESOLVE_INTERVENTION':
        sym = p['symbol']
        flags.set(INTERVENTION_PREFIX + sym, False,
                  actor=cmd.created_by or 'dashboard')
        return 'intervention resolved %s' % sym
    raise ValueError('unknown command type: %s' % cmd.type)


def consume_one(commands, audit, manager, flags, *, exchange: str) -> Optional[str]:
    """认领→执行→DONE/FAILED→审计"""
    cmd = commands.claim_next()
    if cmd is None:
        return None
    try:
        result = execute_command(cmd, manager, flags, exchange=exchange)
        commands.finish(cmd.id, CMD_DONE, result)
        audit.add(cmd.created_by or 'system', 'CMD_RESULT', cmd.id,
                  detail=result, outcome='ok')
    except Exception as exc:
        commands.finish(cmd.id, CMD_FAILED, repr(exc))
        audit.add(cmd.created_by or 'system', 'CMD_RESULT', cmd.id,
                  detail=repr(exc), outcome='fail')
    return cmd.id
