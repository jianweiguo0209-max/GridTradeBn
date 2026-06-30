from gridtrade.state.models import equity_snapshots, EquitySnapshot, metadata


def test_equity_snapshots_table_registered():
    assert 'equity_snapshots' in metadata.tables
    cols = set(metadata.tables['equity_snapshots'].columns.keys())
    assert {'id', 'ts', 'equity', 'cash'} <= cols


def test_equity_snapshot_dataclass_defaults():
    s = EquitySnapshot(id='s1', ts=1000, equity=499.0)
    assert s.cash is None
