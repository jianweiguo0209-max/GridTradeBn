"""容器内 dump 实盘已完成网格 → JSON(供 scripts/recon_live.py 本地对账)。
用法: flyctl ssh console -a <app> -C "python3" < scripts/dump_live_grids.py > grids.json
(offset 是 PG 保留字,已加引号)。默认只导 CLOSED;设 env RECON_ALL=1 导全部。
"""
import json
import os

from sqlalchemy import create_engine, text

url = os.environ['DATABASE_URL']
if url.startswith('postgres://'):
    url = url.replace('postgres://', 'postgresql://', 1)
where = '' if os.environ.get('RECON_ALL') else "WHERE status='CLOSED'"
cols = ('id, symbol, "offset", entry_price, low_price, high_price, stop_low_price, '
        'stop_high_price, grid_count, order_num, cap, created_at, close_reason')
out = []
with create_engine(url).connect() as c:
    for g in c.execute(text('SELECT %s FROM grids %s ORDER BY created_at' % (cols, where))).mappings():
        d = dict(g)
        rec = c.execute(text('SELECT pnl_ratio, exit_reason FROM order_records WHERE grid_id=:g'),
                        {'g': d['id']}).mappings().first()
        d['pnl_ratio'] = float(rec['pnl_ratio']) if rec else 0.0
        out.append({k: (float(v) if isinstance(v, (int, float)) and k != 'created_at' else v)
                    for k, v in d.items()})
print(json.dumps(out, default=str))
