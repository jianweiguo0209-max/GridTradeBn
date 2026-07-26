"""容器内 dump 实盘逐格成交笔数 → JSON(供回测 n_fills 校准)。

用途:回测引擎的成交模型是「1m K线→4 tick 路径近似 + 穿越判定 + maker 100% 成交」,
密格几何下 n_fills 外推到已验证区域的 28 倍(b2_c26 151 vs b3_c16 5.3),
需用实盘真实成交笔数校准「参与率」。

**只读**:仅 SELECT,不写任何表。
用法: flyctl ssh console -a gridtrade-bi-prod -C "python3" < scripts/dump_live_fills.py > live_fills.json
"""
import json
import os

from sqlalchemy import create_engine, text

url = os.environ['DATABASE_URL']
if url.startswith('postgres://'):
    url = url.replace('postgres://', 'postgresql://', 1)

# 逐格:真实成交笔数(排除 ledger: 合成行=内部转仓/关格 reduce 记账,非真实撮合)
SQL = """
SELECT g.id                AS grid_id,
       g.symbol            AS symbol,
       g."offset"          AS "offset",
       g.grid_count        AS grid_count,
       g.low_price         AS low_price,
       g.high_price        AS high_price,
       g.entry_price       AS entry_price,
       g.cap               AS cap,
       g.status            AS status,
       g.created_at        AS created_at,
       r.closed_at         AS closed_at,
       r.pnl_ratio         AS pnl_ratio,
       r.exit_reason       AS exit_reason,
       COUNT(f.trade_id) FILTER (WHERE f.trade_id NOT LIKE 'ledger:%%') AS n_fills_real,
       COUNT(f.trade_id)                                                AS n_fills_all
FROM grids g
LEFT JOIN grid_fills   f ON f.grid_id = g.id
LEFT JOIN order_records r ON r.grid_id = g.id
GROUP BY g.id, g.symbol, g."offset", g.grid_count, g.low_price, g.high_price,
         g.entry_price, g.cap, g.status, g.created_at,
         r.closed_at, r.pnl_ratio, r.exit_reason
ORDER BY g.created_at
"""

out = []
with create_engine(url).connect() as c:
    for row in c.execute(text(SQL)).mappings():
        d = {}
        for k, v in dict(row).items():
            d[k] = float(v) if hasattr(v, 'is_finite') else v
        out.append(d)
print(json.dumps(out, default=str))
