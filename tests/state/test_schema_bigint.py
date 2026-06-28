"""毫秒时间戳/游标列必须是 BigInteger（Postgres INT8）。

SQLite 的 Integer 是动态 64 位，测不出溢出；Postgres 的 Integer=INT4（上限 2.1e9），
而 now_ms() 的毫秒时间戳约 1.78e12 会溢出。此测试守护这些列的类型。
"""
import sqlalchemy as sa

from gridtrade.state import models


def test_ms_timestamp_and_cursor_columns_are_bigint():
    cols = [
        models.grids.c.created_at, models.grids.c.updated_at,
        models.grid_orders.c.created_at, models.grid_orders.c.updated_at,
        models.grid_accounting.c.funding_cursor,
        models.grid_accounting.c.updated_at,
        models.order_records.c.opened_at, models.order_records.c.closed_at,
        models.order_records.c.created_at,
        models.grid_fills.c.ts, models.grid_fills.c.created_at,
        models.heartbeats.c.last_beat_ts,
    ]
    for col in cols:
        assert isinstance(col.type, sa.BigInteger), '%s should be BigInteger' % col
