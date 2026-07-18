"""GateRejectionRepository：门拒绝审计落库(spec 2026-07-18-margin-gate-exchange-im 追加)。

动机(2026-07-18 mainnet 实证)：02:00 MET 被 MarginGate 拒,拒因只打 stdout,fly logs
几分钟滚掉 → 根因排查靠容器内重演。拒绝动作必须持久化可查(psql/面板)。
"""
from gridtrade.state.gate_audit import GateRejectionRepository
from gridtrade.state.store import StateStore


def _repo():
    store = StateStore.in_memory()
    store.create_all()
    return GateRejectionRepository(store)


def test_add_and_list_recent_roundtrip():
    repo = _repo()
    repo.add(exchange='binance', symbol='MET/USDT:USDT', tag='gt2',
             gate='MarginGate', reason='available 510.9 < required 751.4')
    rows = repo.list_recent(limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r['symbol'] == 'MET/USDT:USDT' and r['gate'] == 'MarginGate'
    assert r['tag'] == 'gt2' and 'required' in r['reason']
    assert r['ts'] > 0 and r['created_at'] > 0


def test_list_recent_desc_and_limit():
    repo = _repo()
    for i in range(5):
        repo.add(exchange='binance', symbol='S%d/USDT:USDT' % i, tag='gt0',
                 gate='MinNotionalGate', reason='r%d' % i)
    rows = repo.list_recent(limit=3)
    assert len(rows) == 3
    assert rows[0]['symbol'] == 'S4/USDT:USDT'      # 最新在前
