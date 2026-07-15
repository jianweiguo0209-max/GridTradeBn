"""控制面仓储：标志位 / 指令队列 / 审计。引擎无关，沿用 state 层乐观锁风格。"""
import uuid
from typing import List, Optional

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from gridtrade.state.models import (control_flags, control_commands, ControlCommand,
                                    CMD_PENDING, CMD_RUNNING, now_ms, control_audit,
                                    AuditEntry)


class ControlFlagRepository:
    def __init__(self, store):
        self.engine = store.engine

    def get(self, name: str) -> bool:
        with self.engine.connect() as c:
            row = c.execute(
                select(control_flags.c.value).where(control_flags.c.name == name)
            ).first()
        return bool(row is not None and row[0] == 'true')

    def list_true(self, prefix: str) -> List[str]:
        """值为 true 且名字带前缀的旗标名(外部干预熔断 `intervention:{symbol}` 枚举用)。"""
        with self.engine.connect() as c:
            rows = c.execute(
                select(control_flags.c.name)
                .where(control_flags.c.value == 'true')
                .where(control_flags.c.name.like(prefix + '%'))
            ).all()
        return [r[0] for r in rows]

    def set(self, name: str, value: bool, *, actor: str = '') -> None:
        # 原生 upsert（ON CONFLICT DO UPDATE）取代 INSERT→catch IntegrityError→UPDATE：flag
        # 已存在时（monitor 每轮 set/clear intervention 熔断旗）撞主键刷 PG ERROR。统一口径。
        v = 'true' if value else 'false'
        ts = now_ms()
        ins = (pg_insert if self.engine.dialect.name == 'postgresql' else sqlite_insert)(control_flags)
        stmt = ins.values(name=name, value=v, updated_at=ts, updated_by=actor).on_conflict_do_update(
            index_elements=['name'], set_={'value': v, 'updated_at': ts, 'updated_by': actor})
        with self.engine.begin() as c:
            c.execute(stmt)


_CMD_FIELDS = ('id', 'type', 'payload', 'status', 'result', 'created_at',
               'created_by', 'claimed_at', 'finished_at', 'version')


def _to_cmd(row) -> ControlCommand:
    m = row._mapping
    return ControlCommand(**{f: m[f] for f in _CMD_FIELDS})


class CommandRepository:
    def __init__(self, store):
        self.engine = store.engine

    def enqueue(self, type: str, payload: str, *, created_by: str = '',
                now_ms_fn=now_ms) -> ControlCommand:
        cid = uuid.uuid4().hex
        ts = int(now_ms_fn())
        with self.engine.begin() as c:
            c.execute(insert(control_commands), {
                'id': cid, 'type': type, 'payload': payload, 'status': CMD_PENDING,
                'result': None, 'created_at': ts, 'created_by': created_by,
                'claimed_at': None, 'finished_at': None, 'version': 1,
            })
        return self.get(cid)

    def get(self, command_id: str) -> Optional[ControlCommand]:
        with self.engine.connect() as c:
            row = c.execute(select(control_commands)
                            .where(control_commands.c.id == command_id)).first()
        return _to_cmd(row) if row is not None else None

    def claim_next(self) -> Optional[ControlCommand]:
        for _ in range(2):                       # 并发抢同一条时重试一次
            with self.engine.connect() as c:
                row = c.execute(
                    select(control_commands)
                    .where(control_commands.c.status == CMD_PENDING)
                    .order_by(control_commands.c.created_at, control_commands.c.id)
                    .limit(1)
                ).first()
            if row is None:
                return None
            cmd = _to_cmd(row)
            with self.engine.begin() as c:
                res = c.execute(
                    update(control_commands)
                    .where(control_commands.c.id == cmd.id,
                           control_commands.c.version == cmd.version)
                    .values(status=CMD_RUNNING, claimed_at=now_ms(),
                            version=cmd.version + 1)
                )
            if res.rowcount == 1:
                return self.get(cmd.id)
        return None

    def finish(self, command_id: str, status: str, result: str) -> None:
        with self.engine.begin() as c:
            c.execute(update(control_commands)
                      .where(control_commands.c.id == command_id)
                      .values(status=status, result=result, finished_at=now_ms()))

    def list_recent(self, limit: int = 50) -> List[ControlCommand]:
        with self.engine.connect() as c:
            rows = c.execute(select(control_commands)
                             .order_by(control_commands.c.created_at.desc())
                             .limit(limit)).all()
        return [_to_cmd(r) for r in rows]


_AUDIT_FIELDS = ('id', 'ts', 'actor', 'action', 'target', 'detail', 'outcome')


def _to_audit(row) -> AuditEntry:
    m = row._mapping
    return AuditEntry(**{f: m[f] for f in _AUDIT_FIELDS})


class AuditRepository:
    def __init__(self, store):
        self.engine = store.engine

    def add(self, actor: str, action: str, target: str, *,
            detail: str = '', outcome: str = 'ok') -> AuditEntry:
        aid = uuid.uuid4().hex
        ts = now_ms()
        with self.engine.begin() as c:
            c.execute(insert(control_audit), {
                'id': aid, 'ts': ts, 'actor': actor, 'action': action,
                'target': target, 'detail': detail, 'outcome': outcome,
            })
        with self.engine.connect() as c:
            row = c.execute(select(control_audit)
                            .where(control_audit.c.id == aid)).first()
        return _to_audit(row)

    def list_recent(self, limit: int = 100) -> List[AuditEntry]:
        with self.engine.connect() as c:
            rows = c.execute(select(control_audit)
                             .order_by(control_audit.c.ts.desc())
                             .limit(limit)).all()
        return [_to_audit(r) for r in rows]
