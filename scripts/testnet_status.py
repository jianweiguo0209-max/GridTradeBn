"""testnet 运行状态只读快照。

设计为在 fly app 机器内执行（经 `fly ssh console -C python` 注入），复用机器上的
DATABASE_URL + HL 凭证 env。不修改任何状态，只查询并打印：
  - 心跳新鲜度（monitor ~5s / scheduler 整点）
  - 控制标志（trading_halted / scheduler_paused）
  - 活跃网格 + 每格挂单状态分布
  - 最近控制指令
  - HL testnet 实时余额（equity / cash）
每个区块独立 try/except，单点失败不影响其余快照。
"""
import os
import time

from sqlalchemy import inspect, text

from gridtrade.state.store import StateStore

NOW_MS = int(time.time() * 1000)


def _age_s(ts):
    try:
        return (NOW_MS - int(ts)) / 1000.0
    except Exception:
        return None


def main():
    print('=== TESTNET STATUS @ %s UTC ==='
          % time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()))
    store = StateStore.from_url(os.environ['DATABASE_URL'])
    eng = store.engine
    tables = set(inspect(eng).get_table_names())

    with eng.connect() as c:
        # 心跳
        print('-- heartbeats --')
        try:
            for r in c.execute(text(
                    'SELECT machine, last_beat_ts FROM heartbeats ORDER BY machine')):
                m = r._mapping
                a = _age_s(m['last_beat_ts'])
                # monitor 应 ~5s 一跳，>30s 视为可疑；scheduler 整点跳，不判稳
                flag = ''
                if m['machine'] == 'monitor' and a is not None and a > 30:
                    flag = '  <-- STALE?'
                print('  %-9s %6.0fs ago%s'
                      % (m['machine'], a if a is not None else -1, flag))
        except Exception as exc:
            print('  failed: %r' % exc)

        # 控制标志
        print('-- control flags --')
        try:
            rows = list(c.execute(text(
                'SELECT name, value, updated_by FROM control_flags ORDER BY name')))
            if not rows:
                print('  (none set)')
            for r in rows:
                m = r._mapping
                on = str(m['value']).lower() in ('true', '1')
                print('  %-18s = %-5s (by %s)%s'
                      % (m['name'], m['value'], m['updated_by'],
                         '  <-- ON' if on else ''))
        except Exception as exc:
            print('  failed: %r' % exc)

        # 活跃网格
        print('-- active grids --')
        try:
            grids = list(c.execute(text(
                "SELECT id, symbol, tag, status FROM grids "
                "WHERE status IN ('ACTIVE','CLOSING') ORDER BY tag")))
            print('  count=%d' % len(grids))
            for r in grids:
                m = r._mapping
                dist = {row._mapping['status']: row._mapping['cnt']
                        for row in c.execute(text(
                            'SELECT status, count(*) AS cnt FROM grid_orders '
                            'WHERE grid_id=:g GROUP BY status'), {'g': m['id']})}
                dist_s = ' '.join('%s=%d' % (k, v) for k, v in sorted(dist.items()))
                print('  %-16s tag=%-7s %-8s orders[%s]  id=%s'
                      % (m['symbol'], m['tag'], m['status'], dist_s or '-', m['id']))
        except Exception as exc:
            print('  failed: %r' % exc)

        # 最近控制指令
        print('-- recent control commands --')
        try:
            for r in c.execute(text(
                    'SELECT type, status, result, created_at FROM control_commands '
                    'ORDER BY created_at DESC LIMIT 5')):
                m = r._mapping
                a = _age_s(m['created_at'])
                res = (repr(m['result'])[:80] if m['result'] else '')
                print('  %-16s %-8s %6.0fs ago  %s'
                      % (m['type'], m['status'], a if a is not None else -1, res))
        except Exception as exc:
            print('  failed: %r' % exc)

        # 权益快照（若有表）
        if 'equity_snapshots' in tables:
            print('-- last equity snapshot (DB) --')
            try:
                cols = [col['name'] for col in inspect(eng).get_columns('equity_snapshots')]
                ts_col = 'ts' if 'ts' in cols else ('created_at' if 'created_at' in cols else cols[0])
                row = list(c.execute(text(
                    'SELECT * FROM equity_snapshots ORDER BY %s DESC LIMIT 1' % ts_col)))
                if row:
                    m = row[0]._mapping
                    print('  ' + '  '.join('%s=%s' % (k, m[k]) for k in cols))
                else:
                    print('  (empty)')
            except Exception as exc:
                print('  failed: %r' % exc)

    # HL 实时余额（一次真实 API 调用）
    print('-- live balance (HL testnet) --')
    try:
        from gridtrade.config import load_deploy_config
        from gridtrade.exchanges.registry import build_adapter
        cfg = load_deploy_config()
        ad = build_adapter({'exchange': cfg.exchange, 'wallet_address': cfg.wallet_address,
                            'private_key': cfg.private_key, 'testnet': cfg.testnet,
                            'quote_currency': cfg.quote_currency})
        b = ad.fetch_balance()
        print('  equity=%.4f %s  cash=%.4f' % (b.equity, ad.quote_currency, b.cash))
    except Exception as exc:
        print('  balance fetch failed: %r' % exc)


if __name__ == '__main__':
    main()
