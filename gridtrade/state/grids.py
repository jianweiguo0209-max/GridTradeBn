"""GridRepository：网格意图的持久化（活跃槽位 + 乐观锁 + 状态机）。

槽位方案（cap=2 全套改造，spec 2026-07-06-tiered-*）：UNIQUE(exchange, active_symbol)
原样保留，active_symbol 存 'SYM#slot'（slot=0..cap-1）——同币最多 cap 个活跃格，
抢槽仍是 DB 原子操作（并发双开的 TOCTOU 防线不因 cap>1 而丢失）。
"""
import uuid
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy import func, insert, select, update

from gridtrade.state.models import (ACTIVE_STATES, ConcurrencyError, Grid,
                                    SlotExhausted, StateError, TERMINAL_STATES,
                                    can_transition, grids, now_ms)

_UNLIMITED_SLOT_BOUND = 64   # cap=不限(None) 时的槽位实际上界（防御性；现实并发远小于此）

_FIELDS = ('id', 'exchange', 'symbol', 'status', 'offset', 'tag', 'direction',
           'entry_price', 'low_price', 'high_price', 'stop_low_price',
           'stop_high_price', 'grid_count', 'order_num', 'leverage', 'cap',
           'created_at', 'updated_at', 'version', 'fuse_low_oid', 'fuse_high_oid')

_UNSET = object()


def _to_grid(row) -> Grid:
    m = row._mapping
    return Grid(**{f: m[f] for f in _FIELDS})


class GridRepository:
    def __init__(self, store):
        self.engine = store.engine

    def create(self, grid: Grid, *, max_slots=None) -> Grid:
        """max_slots：本币种活跃槽上限；None → 按 DEFAULT_TIER_POLICY.cap_for 推导
        （名单单源；cap 不限时用 _UNLIMITED_SLOT_BOUND 兜底）。活跃态逐槽尝试插入，
        UNIQUE 冲突换下一槽；槽满抛 ConcurrencyError（门链竞态漏网时的 DB 级兜底）。"""
        gid = grid.id or uuid.uuid4().hex
        ts = now_ms()
        created = grid.created_at or ts
        updated = grid.updated_at or ts
        values = {f: getattr(grid, f) for f in _FIELDS}
        values.update(id=gid, created_at=created, updated_at=updated, version=1)
        if grid.status not in ACTIVE_STATES:
            values['active_symbol'] = None
            with self.engine.begin() as c:
                c.execute(insert(grids), values)
            return self.get(gid)
        if max_slots is None:
            from gridtrade.config import DEFAULT_TIER_POLICY
            from gridtrade.core.tier_policy import cap_for
            max_slots = cap_for(grid.symbol, DEFAULT_TIER_POLICY)
        limit = int(max_slots) if max_slots else _UNLIMITED_SLOT_BOUND
        for slot in range(limit):
            values['active_symbol'] = '%s#%d' % (grid.symbol, slot)
            try:
                with self.engine.begin() as c:
                    c.execute(insert(grids), values)
                return self.get(gid)
            except sa.exc.IntegrityError:
                continue                      # 槽被占 → 试下一槽
        raise SlotExhausted('no free symbol slot for %s on %s (cap=%d)'
                               % (grid.symbol, grid.exchange, limit))

    def get(self, grid_id: str) -> Optional[Grid]:
        with self.engine.connect() as c:
            row = c.execute(select(grids).where(grids.c.id == grid_id)).first()
        return _to_grid(row) if row is not None else None

    def get_active_by_symbol(self, exchange: str, symbol: str) -> Optional[Grid]:
        """任一活跃格（槽位前缀匹配 'SYM#%'；canonical 符号不含 '#'，无误配）。"""
        with self.engine.connect() as c:
            row = c.execute(
                select(grids).where(grids.c.exchange == exchange,
                                    grids.c.active_symbol.like(symbol + '#%'))
            ).first()
        return _to_grid(row) if row is not None else None

    def count_active_by_symbol(self, exchange: str, symbol: str) -> int:
        """本币活跃格数（选币剔锁/预览用）。"""
        with self.engine.connect() as c:
            n = c.execute(
                select(func.count()).select_from(grids)
                .where(grids.c.exchange == exchange,
                       grids.c.active_symbol.like(symbol + '#%'))
            ).scalar()
        return int(n or 0)

    def list_active(self) -> List[Grid]:
        with self.engine.connect() as c:
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
            # Terminal -> release slot (NULL). Active -> 保留既有槽位后缀（勿用裸 symbol
            # 覆写，否则丢 slot 编号且撞 UNIQUE）。理论不可达的 terminal->active 兜底
            # 抢 #0（撞则由 UNIQUE 拒绝）。
            cur_active = row._mapping['active_symbol']
            if new_status in TERMINAL_STATES:
                active_symbol = None
            elif new_status in ACTIVE_STATES:
                active_symbol = cur_active if cur_active is not None \
                    else current.symbol + '#0'
            else:
                active_symbol = cur_active
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

    def set_fuse_oids(self, grid_id, *, low_oid=_UNSET, high_oid=_UNSET) -> None:
        vals = {}
        if low_oid is not _UNSET:
            vals['fuse_low_oid'] = low_oid
        if high_oid is not _UNSET:
            vals['fuse_high_oid'] = high_oid
        if not vals:
            return
        vals['updated_at'] = now_ms()
        with self.engine.begin() as c:
            c.execute(update(grids).where(grids.c.id == grid_id).values(**vals))
