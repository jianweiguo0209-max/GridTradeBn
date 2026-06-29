"""控制面仓储：标志位 / 指令队列 / 审计。引擎无关，沿用 state 层乐观锁风格。"""
import sqlalchemy as sa
from sqlalchemy import insert, select, update

from gridtrade.state.models import control_flags, now_ms


class ControlFlagRepository:
    def __init__(self, store):
        self.engine = store.engine

    def get(self, name: str) -> bool:
        with self.engine.connect() as c:
            row = c.execute(
                select(control_flags.c.value).where(control_flags.c.name == name)
            ).first()
        return bool(row is not None and row[0] == 'true')

    def set(self, name: str, value: bool, *, actor: str = '') -> None:
        v = 'true' if value else 'false'
        ts = now_ms()
        try:
            with self.engine.begin() as c:
                c.execute(insert(control_flags),
                          {'name': name, 'value': v, 'updated_at': ts,
                           'updated_by': actor})
        except sa.exc.IntegrityError:
            with self.engine.begin() as c:
                c.execute(update(control_flags)
                          .where(control_flags.c.name == name)
                          .values(value=v, updated_at=ts, updated_by=actor))
