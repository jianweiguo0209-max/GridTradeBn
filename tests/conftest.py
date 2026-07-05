import os
import time

# 统一钉死 TZ=东八区，保证测试确定性（选币代码本身已不再依赖机器 TZ）。
os.environ['TZ'] = 'Asia/Shanghai'
time.tzset()

import pytest
from sqlalchemy import text

from gridtrade.state.store import StateStore
from gridtrade.state.models import metadata


def _truncate_all(st):
    names = ', '.join(t.name for t in metadata.sorted_tables)
    with st.engine.begin() as c:
        c.execute(text('TRUNCATE %s RESTART IDENTITY CASCADE' % names))


@pytest.fixture
def store():
    """双模式：TEST_DATABASE_URL 有值走 Postgres（每测 TRUNCATE 隔离），否则内存 SQLite。"""
    url = os.environ.get('TEST_DATABASE_URL')
    if url:
        st = StateStore.from_url(url)
        st.create_all()
        _truncate_all(st)
        yield st
        st.engine.dispose()
    else:
        st = StateStore.in_memory()
        st.create_all()
        yield st
        st.dispose_and_cleanup()   # 临时库文件即刻删除（in_memory 已改文件后端）


@pytest.fixture
def pg_store():
    """PG-only：无 TEST_DATABASE_URL 则跳过（真并发测试用，SQLite 造不出真竞态）。"""
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('set TEST_DATABASE_URL to run Postgres-only tests')
    st = StateStore.from_url(url)
    st.create_all()
    _truncate_all(st)
    yield st
    st.engine.dispose()
