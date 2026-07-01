from gridtrade.state.grids import GridRepository
from gridtrade.state.models import Grid


def _new_grid(repo):
    return repo.create(Grid(id='', exchange='hl', symbol='BTC/USDC:USDC',
                            status='PENDING'))


def test_fuse_oids_default_none(store):
    repo = GridRepository(store)
    g = _new_grid(repo)
    assert g.fuse_low_oid is None and g.fuse_high_oid is None


def test_set_fuse_oids_persists(store):
    repo = GridRepository(store)
    g = _new_grid(repo)
    repo.set_fuse_oids(g.id, low_oid='OID_LOW', high_oid='OID_HIGH')
    g2 = repo.get(g.id)
    assert g2.fuse_low_oid == 'OID_LOW'
    assert g2.fuse_high_oid == 'OID_HIGH'


def test_set_fuse_oids_partial_leaves_other(store):
    repo = GridRepository(store)
    g = _new_grid(repo)
    repo.set_fuse_oids(g.id, low_oid='OID_LOW', high_oid='OID_HIGH')
    repo.set_fuse_oids(g.id, low_oid='NEW_LOW')          # 只更新 low
    g2 = repo.get(g.id)
    assert g2.fuse_low_oid == 'NEW_LOW'
    assert g2.fuse_high_oid == 'OID_HIGH'                 # high 不动


def test_set_fuse_oids_does_not_bump_version(store):
    repo = GridRepository(store)
    g = _new_grid(repo)
    repo.set_fuse_oids(g.id, low_oid='OID_LOW')
    assert repo.get(g.id).version == g.version            # 元数据更新不动乐观锁
