from gridtrade.state.control import CommandRepository
from gridtrade.state.models import CMD_RUNNING, CMD_DONE, CMD_PENDING


def test_enqueue_claim_finish_cycle(store):
    repo = CommandRepository(store)
    c = repo.enqueue('CLOSE_GRID', '{"grid_id": "g1"}', created_by='admin')
    assert c.status == CMD_PENDING and c.id

    claimed = repo.claim_next()
    assert claimed.id == c.id and claimed.status == CMD_RUNNING
    assert claimed.claimed_at is not None

    assert repo.claim_next() is None         # 已无 PENDING

    repo.finish(c.id, CMD_DONE, 'closed ok')
    recent = repo.list_recent()
    done = [x for x in recent if x.id == c.id][0]
    assert done.status == CMD_DONE and done.result == 'closed ok'
    assert done.finished_at is not None


def test_claim_is_fifo(store):
    repo = CommandRepository(store)
    a = repo.enqueue('CLOSE_GRID', '{}', created_by='admin')
    b = repo.enqueue('CLOSE_GRID', '{}', created_by='admin')
    assert repo.claim_next().id == a.id      # 先进先出
    assert repo.claim_next().id == b.id
