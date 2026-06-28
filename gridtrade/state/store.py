"""StateStore：包装一个 SQLAlchemy Engine。
in_memory() 用 SQLite StaticPool（多次 begin() 共享同一内存库）供测试；
from_url() 供 Postgres 生产（如 postgresql+psycopg2://user:pw@host/db）。
"""
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from gridtrade.state.models import metadata


class StateStore:
    def __init__(self, engine):
        self.engine = engine

    @classmethod
    def from_url(cls, url: str) -> 'StateStore':
        # Fly Postgres 给的是 postgres://，SQLAlchemy 2.0 已移除该 scheme 别名，
        # 规范成 postgresql://（默认 psycopg2 方言）。
        if url.startswith('postgres://'):
            url = 'postgresql://' + url[len('postgres://'):]
        # pool_pre_ping：用前校验连接、失效自动重连（Fly Postgres 关空闲连接，
        # 否则复用死连接报 "server closed the connection unexpectedly"）。
        return cls(create_engine(url, future=True, pool_pre_ping=True))

    @classmethod
    def in_memory(cls) -> 'StateStore':
        engine = create_engine(
            'sqlite://', future=True,
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        return cls(engine)

    def create_all(self) -> None:
        metadata.create_all(self.engine)

    def drop_all(self) -> None:
        metadata.drop_all(self.engine)
