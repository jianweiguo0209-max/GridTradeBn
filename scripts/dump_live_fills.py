"""按成交额(notional)的逐格实盘成交 —— 与回测理论成交额同口径。只读。"""
import json, os
from sqlalchemy import create_engine, text
url = os.environ['DATABASE_URL']
if url.startswith('postgres://'):
    url = url.replace('postgres://', 'postgresql://', 1)
SQL = """
SELECT g.id AS grid_id, g.symbol, g."offset", g.grid_count, g.low_price, g.high_price,
       g.entry_price, g.cap, g.order_num, g.status, g.created_at,
       r.closed_at, r.pnl_ratio, r.exit_reason,
       COUNT(*)                     FILTER (WHERE f.trade_id NOT LIKE 'ledger:%%') AS trades_raw,
       COALESCE(SUM(f.price*f.size) FILTER (WHERE f.trade_id NOT LIKE 'ledger:%%'),0) AS notional_live,
       COALESCE(SUM(f.size)         FILTER (WHERE f.trade_id NOT LIKE 'ledger:%%'),0) AS qty_live,
       COUNT(DISTINCT (f.line_index,f.side)) FILTER (WHERE f.trade_id NOT LIKE 'ledger:%%') AS lines_hit
FROM grids g
LEFT JOIN grid_fills f ON f.grid_id = g.id
LEFT JOIN order_records r ON r.grid_id = g.id
GROUP BY g.id, g.symbol, g."offset", g.grid_count, g.low_price, g.high_price,
         g.entry_price, g.cap, g.order_num, g.status, g.created_at,
         r.closed_at, r.pnl_ratio, r.exit_reason
ORDER BY g.created_at
"""
out = []
with create_engine(url).connect() as c:
    for row in c.execute(text(SQL)).mappings():
        out.append({k: (float(v) if hasattr(v, 'is_finite') else v) for k, v in dict(row).items()})
print(json.dumps(out, default=str))
