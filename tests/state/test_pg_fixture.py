from gridtrade.state.grids import GridRepository
from gridtrade.state.models import Grid


def test_store_fixture_roundtrips(store):
    repo = GridRepository(store)
    g = repo.create(Grid(id='', exchange='fake', symbol='BTC/USDT:USDT', status='PENDING'))
    assert repo.get(g.id).status == 'PENDING'


def test_store_fixture_isolated_between_tests(store):
    # 上一个测试建的 grid 不应残留（SQLite 天然新库；PG 靠 TRUNCATE）
    repo = GridRepository(store)
    assert repo.list_active() == []
