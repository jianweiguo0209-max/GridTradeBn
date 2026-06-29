# tests/state/test_transition_concurrency.py
"""真并发 TOCTOU 测试：transition_status 单事务版本守卫在真线程竞争下只放一个赢家。
需 Postgres（SQLite StaticPool 单连接造不出真竞态）。跑法：
  docker run -d --name gridpg -e POSTGRES_PASSWORD=grid -e POSTGRES_DB=gridtrade -p 5432:5432 postgres:16
  TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade \
    .venv/bin/python -m pytest tests/state/test_transition_concurrency.py
"""
import threading

from gridtrade.state.grids import GridRepository
from gridtrade.state.models import (Grid, ACTIVE, OPENING, CLOSING,
                                    ConcurrencyError, StateError)


def _race(store, grid_id, expected_version, new_status, n):
    repo = GridRepository(store)
    barrier = threading.Barrier(n)
    wins, errors = [], []
    lock = threading.Lock()

    def worker():
        barrier.wait()                       # 所有线程同刻开打，最大化竞争
        try:
            g = repo.transition_status(grid_id, new_status,
                                       expected_version=expected_version)
            with lock:
                wins.append(g)
        except (ConcurrencyError, StateError) as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return wins, errors


def test_concurrent_same_version_exactly_one_winner(pg_store):
    repo = GridRepository(pg_store)
    g = repo.create(Grid(id='', exchange='fake', symbol='BTC/USDT:USDT', status='PENDING'))
    wins, errors = _race(pg_store, g.id, g.version, OPENING, n=8)
    assert len(wins) == 1                                  # 恰好一个赢
    assert len(errors) == 7                                # 其余全被拒
    # 每个输家都拿到语义合法的并发/状态错误（非静默成功、非崩溃）
    assert all(isinstance(e, (ConcurrencyError, StateError)) for e in errors)
    final = repo.get(g.id)
    assert final.status == 'OPENING'
    assert final.version == g.version + 1                  # 只 +1 一次，无双赢/丢更新


def test_concurrent_double_close_only_one_wins(pg_store):
    repo = GridRepository(pg_store)
    g = repo.create(Grid(id='', exchange='fake', symbol='ETH/USDT:USDT', status='PENDING'))
    g = repo.transition_status(g.id, OPENING, expected_version=g.version)
    g = repo.transition_status(g.id, ACTIVE, expected_version=g.version)
    wins, errors = _race(pg_store, g.id, g.version, CLOSING, n=2)
    assert len(wins) == 1 and len(errors) == 1             # 一赢一拒 -> 不双平
    assert isinstance(errors[0], (ConcurrencyError, StateError))
    final = repo.get(g.id)
    assert final.status == 'CLOSING'
    assert final.version == g.version + 1
