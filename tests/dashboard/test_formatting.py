# tests/dashboard/test_formatting.py
from gridtrade.dashboard.formatting import (ms_to_human, age_human, fmt_num,
                                            fmt_pct, pnl_class)


def test_ms_to_human():
    assert ms_to_human(None) == '-'
    assert ms_to_human(0) == '1970-01-01 00:00:00'


def test_age_human():
    assert age_human(None) == '-'
    assert age_human(5) == '5s'
    assert age_human(90) == '1m'
    assert age_human(7200) == '2h'


def test_fmt_num_and_pct():
    assert fmt_num(None) == '-'
    assert fmt_num(1.2345, 2) == '1.23'
    assert fmt_pct(None) == '-'
    assert fmt_pct(0.1234, 1) == '12.3%'


def test_pnl_class():
    assert pnl_class(3.0) == 'pos'
    assert pnl_class(-3.0) == 'neg'
    assert pnl_class(0.0) == 'zero'
    assert pnl_class(None) == 'zero'
