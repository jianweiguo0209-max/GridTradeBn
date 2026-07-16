"""对账快照仓储(2026-07-17,实盘 vs 回测执行/名次对齐)。

universe_snapshots 记了"进入排名的集合",但两类实时决策输入没记 → 离线精确复现不了:
  1. 因子值/最终名次 —— choose_symbols=1 的 razor-thin top-1(实证 3/6)。
  2. pv 量能尖峰 / funding 费率 —— pv 离线重算过度触发、demo funding 与 mainnet 不符(exit 45%)。
本模块两张表把这两类信号也存下来(record-and-replay),回测复放读它 → byte 级对齐。
写入均 fail-soft(调用方 try/except 包住,绝不阻断交易),幂等(复合主键重跑覆盖为最新)。
"""
import json

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from gridtrade.state.models import now_ms, selection_snapshots, signal_snapshots


def _ins(engine, table):
    return (pg_insert if engine.dialect.name == 'postgresql' else sqlite_insert)(table)


class SelectionSnapshotRepository:
    """每 tick 一行:offset + 排名因子表 + 实选 picks。回测复放读 ranked 重现名次。"""

    def __init__(self, store):
        self.engine = store.engine

    def add(self, exchange: str, run_time_ms: int, offset: int, ranked, picks) -> None:
        """幂等写入。ranked=[{symbol,factors,rank_sum,rank}] 名次升序;picks=选中币列表。"""
        values = {'exchange': exchange, 'run_time': int(run_time_ms), 'offset': int(offset),
                  'ranked': json.dumps(ranked, ensure_ascii=False),
                  'picks': json.dumps(sorted(picks), ensure_ascii=False),
                  'created_at': now_ms()}
        stmt = _ins(self.engine, selection_snapshots).values(**values).on_conflict_do_update(
            index_elements=['exchange', 'run_time'],
            set_={'offset': values['offset'], 'ranked': values['ranked'],
                  'picks': values['picks'], 'created_at': values['created_at']})
        with self.engine.begin() as c:
            c.execute(stmt)

    def get(self, exchange: str, run_time_ms: int):
        """{'offset','ranked','picks','created_at'} 或 None。"""
        with self.engine.connect() as c:
            row = c.execute(
                select(selection_snapshots)
                .where(selection_snapshots.c.exchange == exchange)
                .where(selection_snapshots.c.run_time == int(run_time_ms))
            ).first()
        if row is None:
            return None
        m = row._mapping
        return {'offset': m['offset'], 'ranked': json.loads(m['ranked']),
                'picks': json.loads(m['picks']), 'created_at': m['created_at']}

    def list_range(self, exchange: str, start_ms: int, end_ms: int):
        """[{'run_time','offset','ranked','picks'}] 升序——离线重放驱动数据。"""
        with self.engine.connect() as c:
            rows = c.execute(
                select(selection_snapshots)
                .where(selection_snapshots.c.exchange == exchange)
                .where(selection_snapshots.c.run_time >= int(start_ms))
                .where(selection_snapshots.c.run_time <= int(end_ms))
                .order_by(selection_snapshots.c.run_time)
            ).all()
        out = []
        for r in rows:
            m = r._mapping
            out.append({'run_time': m['run_time'], 'offset': m['offset'],
                        'ranked': json.loads(m['ranked']), 'picks': json.loads(m['picks'])})
        return out


class SignalSnapshotRepository:
    """止损相关信号事件(pv_spike==1 或 funding 超阈),按 (grid_id, ts) 幂等。
    稀疏记录:没记的 bar 视为 pv_spike=0。回测复放读它重现实盘 pv/资金费率止损时序。"""

    def __init__(self, store):
        self.engine = store.engine

    def add(self, grid_id: str, ts_ms: int, symbol: str, *, pv_spike: int = 0,
            funding_rate: float = 0.0, pnl_ratio=None) -> None:
        values = {'grid_id': grid_id, 'ts': int(ts_ms), 'symbol': symbol,
                  'pv_spike': int(pv_spike), 'funding_rate': float(funding_rate),
                  'pnl_ratio': (float(pnl_ratio) if pnl_ratio is not None else None),
                  'created_at': now_ms()}
        stmt = _ins(self.engine, signal_snapshots).values(**values).on_conflict_do_update(
            index_elements=['grid_id', 'ts'],
            set_={'pv_spike': values['pv_spike'], 'funding_rate': values['funding_rate'],
                  'pnl_ratio': values['pnl_ratio'], 'created_at': values['created_at']})
        with self.engine.begin() as c:
            c.execute(stmt)

    def list_for_grid(self, grid_id: str):
        """某格的信号事件时序(升序)——回测复放该格 pv/funding 止损。"""
        with self.engine.connect() as c:
            rows = c.execute(
                select(signal_snapshots)
                .where(signal_snapshots.c.grid_id == grid_id)
                .order_by(signal_snapshots.c.ts)
            ).all()
        return [{'ts': r._mapping['ts'], 'symbol': r._mapping['symbol'],
                 'pv_spike': r._mapping['pv_spike'], 'funding_rate': r._mapping['funding_rate'],
                 'pnl_ratio': r._mapping['pnl_ratio']} for r in rows]

    def list_range(self, start_ms: int, end_ms: int):
        """时间窗内全部信号事件(升序)。"""
        with self.engine.connect() as c:
            rows = c.execute(
                select(signal_snapshots)
                .where(signal_snapshots.c.ts >= int(start_ms))
                .where(signal_snapshots.c.ts <= int(end_ms))
                .order_by(signal_snapshots.c.ts)
            ).all()
        return [{'grid_id': r._mapping['grid_id'], 'ts': r._mapping['ts'],
                 'symbol': r._mapping['symbol'], 'pv_spike': r._mapping['pv_spike'],
                 'funding_rate': r._mapping['funding_rate'], 'pnl_ratio': r._mapping['pnl_ratio']}
                for r in rows]
