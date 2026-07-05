"""StateStore：包装一个 SQLAlchemy Engine。
in_memory() 用每实例独立 SQLite 临时文件 + 连接池（测试/离线模式）；
from_url() 供 Postgres 生产（如 postgresql+psycopg2://user:pw@host/db）。
"""
import os
import tempfile

from sqlalchemy import create_engine, event

from gridtrade.state.models import metadata


class StateStore:
    def __init__(self, engine, tmp_path=None):
        self.engine = engine
        self._tmp_path = tmp_path   # in_memory 的临时库文件；dispose_and_cleanup 删除

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
        """一次性库（测试/离线）。曾用 :memory:+StaticPool（单连接），monitor
        per-grid 并行后多线程并发游标互踩（InterfaceError）；改为每实例独立临时
        文件 + 默认连接池：每线程独立连接，写冲突走 SQLITE_BUSY+timeout 等待，
        并发语义与生产 PG 对齐。journal/synchronous 调成内存级速度。"""
        fd, path = tempfile.mkstemp(prefix='gridtrade-', suffix='.db')
        os.close(fd)
        engine = create_engine(
            'sqlite:///' + path, future=True,
            connect_args={'check_same_thread': False, 'timeout': 30},
        )

        @event.listens_for(engine, 'connect')
        def _fast_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute('PRAGMA journal_mode=MEMORY')
            cur.execute('PRAGMA synchronous=OFF')
            cur.close()

        return cls(engine, tmp_path=path)

    def dispose_and_cleanup(self) -> None:
        """测试收尾：释放连接池并删除 in_memory 的临时库文件（PG store 只 dispose）。"""
        self.engine.dispose()
        if self._tmp_path is not None:
            try:
                os.unlink(self._tmp_path)
            except OSError:
                pass

    def create_all(self) -> None:
        metadata.create_all(self.engine)

    def drop_all(self) -> None:
        metadata.drop_all(self.engine)
