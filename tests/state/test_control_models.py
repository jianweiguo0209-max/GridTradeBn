from gridtrade.state.models import (control_flags, control_commands, control_audit,
                                    ControlFlag, ControlCommand, AuditEntry,
                                    CMD_PENDING, metadata)


def test_control_tables_registered_in_metadata():
    names = set(metadata.tables)
    assert {'control_flags', 'control_commands', 'control_audit'} <= names


def test_control_dataclasses_defaults():
    f = ControlFlag(name='trading_halted', value='true')
    assert f.updated_at == 0 and f.updated_by == ''
    c = ControlCommand(id='c1', type='CLOSE_GRID', payload='{}')
    assert c.status == CMD_PENDING and c.version == 1 and c.result is None
    a = AuditEntry(id='a1', ts=1, actor='admin', action='FLAG_SET', target='trading_halted')
    assert a.outcome == 'ok' and a.detail == ''
