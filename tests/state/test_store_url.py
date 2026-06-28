from gridtrade.state.store import StateStore


def test_from_url_normalizes_bare_postgres_scheme():
    # Fly Postgres 给的是 postgres://，SQLAlchemy 2.0 拒绝该 scheme。
    # create_engine 惰性不连库，只验证 scheme 被规范成 postgresql。
    store = StateStore.from_url('postgres://u:p@h:5432/db')
    assert store.engine.url.drivername.startswith('postgresql')


def test_from_url_keeps_explicit_driver():
    store = StateStore.from_url('postgresql+psycopg2://u:p@h:5432/db')
    assert store.engine.url.drivername == 'postgresql+psycopg2'


def test_from_url_keeps_sqlite():
    store = StateStore.from_url('sqlite://')
    assert store.engine.url.drivername == 'sqlite'


def test_from_url_enables_pool_pre_ping():
    # Fly Postgres 会关闭空闲连接；pre_ping 用前校验+重连，避免 "server closed the connection"
    store = StateStore.from_url('postgresql+psycopg2://u:p@h/db')
    assert store.engine.pool._pre_ping is True
