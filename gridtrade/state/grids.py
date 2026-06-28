"""GridRepository：网格意图的持久化（活跃唯一 + 乐观锁 + 状态机）。"""
import uuid
from typing import List, Optional

from sqlalchemy import insert, select, update

from gridtrade.state.models import (ACTIVE_STATES, ConcurrencyError, Grid,
                                    StateError, TERMINAL_STATES, can_transition,
                                    grids, now_ms)

_FIELDS = ('id', 'exchange', 'symbol', 'status', 'offset', 'tag', 'direction',
           'entry_price', 'low_price', 'high_price', 'stop_low_price',
           'stop_high_price', 'grid_count', 'order_num', 'leverage', 'cap',
           'created_at', 'updated_at', 'version')


def _to_grid(row) -> Grid:
    m = row._mapping
    return Grid(**{f: m[f] for f in _FIELDS})


class GridRepository:
    def __init__(self, store):
        self.engine = store.engine

    def create(self, grid: Grid) -> Grid:
        gid = grid.id or uuid.uuid4().hex
        ts = now_ms()
        created = grid.created_at or ts
        updated = grid.updated_at or ts
        active_symbol = grid.symbol if grid.status in ACTIVE_STATES else None
        values = {f: getattr(grid, f) for f in _FIELDS}
        values.update(id=gid, created_at=created, updated_at=updated, version=1,
                      active_symbol=active_symbol)
        with self.engine.begin() as c:
            c.execute(insert(grids), values)
        return self.get(gid)

    def get(self, grid_id: str) -> Optional[Grid]:
        with self.engine.begin() as c:
            row = c.execute(select(grids).where(grids.c.id == grid_id)).first()
        return _to_grid(row) if row is not None else None

    def get_active_by_symbol(self, exchange: str, symbol: str) -> Optional[Grid]:
        with self.engine.begin() as c:
            row = c.execute(
                select(grids).where(grids.c.exchange == exchange,
                                    grids.c.active_symbol == symbol)
            ).first()
        return _to_grid(row) if row is not None else None

    def list_active(self) -> List[Grid]:
        with self.engine.begin() as c:
            rows = c.execute(
                select(grids).where(grids.c.status.in_(ACTIVE_STATES))
            ).all()
        return [_to_grid(r) for r in rows]

    def transition_status(self, grid_id: str, new_status: str, *,
                          expected_version: int) -> Grid:
        # 单事务内：重读源态 -> can_transition 重校验 -> 版本守卫写。校验与写共享同一
        # 事务快照，消除「事务外读校验 + 事务内写」的 TOCTOU：并发下源态若已变为非法
        # （如已进终态），重校验直接抛 StateError（语义正确），不再被泛化成
        # ConcurrencyError。版本守卫仍以传入 expected_version 为准（乐观锁不破）。
        # 真并发交错下的红->绿测试延后到存在真实并发 mutator（多监控机 leader 选举/
        # 分片）阶段；本阶段以串行契约守卫（tests/state/test_transition_revalidate.py）。
        with self.engine.begin() as c:
            row = c.execute(select(grids).where(grids.c.id == grid_id)).first()
            if row is None:
                raise ConcurrencyError(f'grid {grid_id} not found')
            current = _to_grid(row)
            if not can_transition(current.status, new_status):
                raise StateError(
                    f'illegal transition {current.status} -> {new_status}')
            # Terminal -> release slot (NULL). Active state -> (re)claim symbol slot.
            # Any other (currently unreachable: all 6 states are terminal or active)
            # -> preserve existing occupancy rather than silently dropping the slot.
            if new_status in TERMINAL_STATES:
                active_symbol = None
            elif new_status in ACTIVE_STATES:
                active_symbol = current.symbol
            else:
                active_symbol = (current.symbol
                                 if current.status in ACTIVE_STATES else None)
            res = c.execute(
                update(grids)
                .where(grids.c.id == grid_id, grids.c.version == expected_version)
                .values(status=new_status, active_symbol=active_symbol,
                        version=expected_version + 1, updated_at=now_ms())
            )
            if res.rowcount == 0:
                raise ConcurrencyError(
                    f'stale version for grid {grid_id}: expected {expected_version}')
        return self.get(grid_id)
